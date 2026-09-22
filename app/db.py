"""SQLite storage: jobs, run history, two-way baselines, key/value state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from . import config
from .models import Job

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    position INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    job_name TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 0,
    started REAL NOT NULL,
    finished REAL,
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job_id, started DESC);
CREATE TABLE IF NOT EXISTS baseline (
    job_id TEXT NOT NULL,
    pair_id TEXT NOT NULL,
    rel TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    PRIMARY KEY (job_id, pair_id, rel)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            # Runs left 'running' by a crash/restart are marked as interrupted.
            _conn.execute(
                "UPDATE runs SET status='interrupted', finished=?, error='Service stopped during run' "
                "WHERE status IN ('running','queued')", (time.time(),))
            _conn.commit()
        return _conn


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        c = conn()
        cur = c.execute(sql, tuple(params))
        c.commit()
        return cur


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, tuple(params)).fetchall()


# ------------------------------------------------------------------ jobs -----
def list_jobs() -> list[Job]:
    return [Job.model_validate_json(r["data"]) for r in query("SELECT data FROM jobs ORDER BY position, rowid")]


def get_job(job_id: str) -> Optional[Job]:
    rows = query("SELECT data FROM jobs WHERE id=?", (job_id,))
    return Job.model_validate_json(rows[0]["data"]) if rows else None


def save_job(job: Job) -> Job:
    now = time.time()
    if not job.created:
        job.created = now
    job.updated = now
    with _lock:
        pos = query("SELECT COALESCE(MAX(position),0)+1 AS p FROM jobs")[0]["p"]
        execute(
            "INSERT INTO jobs(id, position, data) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (job.id, pos, job.model_dump_json()),
        )
    return job


def delete_job(job_id: str) -> None:
    with _lock:
        execute("DELETE FROM jobs WHERE id=?", (job_id,))
        execute("DELETE FROM baseline WHERE job_id=?", (job_id,))


# ------------------------------------------------------------------ runs -----
def create_run(job: Job, trigger: str, dry_run: bool, status: str = "queued") -> int:
    cur = execute(
        "INSERT INTO runs(job_id, job_name, trigger, status, dry_run, started) VALUES(?,?,?,?,?,?)",
        (job.id, job.name, trigger, status, int(dry_run), time.time()),
    )
    return int(cur.lastrowid)


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    if "stats" in fields and not isinstance(fields["stats"], str):
        fields["stats"] = json.dumps(fields["stats"])
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def run_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["stats"] = json.loads(d.get("stats") or "{}")
    d["dry_run"] = bool(d["dry_run"])
    return d


def list_runs(job_id: Optional[str] = None, limit: int = 100, offset: int = 0) -> list[dict]:
    if job_id:
        rows = query("SELECT * FROM runs WHERE job_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (job_id, limit, offset))
    else:
        rows = query("SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
    return [run_to_dict(r) for r in rows]


def get_run(run_id: int) -> Optional[dict]:
    rows = query("SELECT * FROM runs WHERE id=?", (run_id,))
    return run_to_dict(rows[0]) if rows else None


def last_run(job_id: str) -> Optional[dict]:
    rows = query("SELECT * FROM runs WHERE job_id=? AND status NOT IN ('queued','running') "
                 "ORDER BY id DESC LIMIT 1", (job_id,))
    return run_to_dict(rows[0]) if rows else None


def prune_runs(keep_days: int) -> list[str]:
    cutoff = time.time() - keep_days * 86400
    rows = query("SELECT log_path FROM runs WHERE started < ? AND status NOT IN ('queued','running')", (cutoff,))
    execute("DELETE FROM runs WHERE started < ? AND status NOT IN ('queued','running')", (cutoff,))
    return [r["log_path"] for r in rows if r["log_path"]]


# -------------------------------------------------------------- baseline -----
def load_baseline(job_id: str, pair_id: str) -> dict[str, tuple[int, float]]:
    rows = query("SELECT rel, size, mtime FROM baseline WHERE job_id=? AND pair_id=?", (job_id, pair_id))
    return {r["rel"]: (r["size"], r["mtime"]) for r in rows}


def replace_baseline(job_id: str, pair_id: str, entries: dict[str, tuple[int, float]]) -> None:
    with _lock:
        c = conn()
        c.execute("DELETE FROM baseline WHERE job_id=? AND pair_id=?", (job_id, pair_id))
        c.executemany(
            "INSERT INTO baseline(job_id, pair_id, rel, size, mtime) VALUES(?,?,?,?,?)",
            ((job_id, pair_id, rel, s, m) for rel, (s, m) in entries.items()),
        )
        c.commit()


def clear_baseline(job_id: str) -> None:
    execute("DELETE FROM baseline WHERE job_id=?", (job_id,))


# -------------------------------------------------------------------- kv -----
def kv_get(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def kv_set(key: str, value: Any) -> None:
    execute("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
