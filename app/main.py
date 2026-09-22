"""Proto-Sync HTTP API + static UI."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hmac
import json
import os
import secrets
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, compare, config, db, events, fsutil, notify
from .models import ACTIONS, CATEGORIES, DEFAULT_EXCLUDES, FolderPair, Job, NotifyTarget, Trigger
from .runner import runner
from .scheduler import scheduler

STATIC = Path(__file__).resolve().parent.parent / "static"


def seed() -> None:
    if db.list_jobs() or not config.SEED_DEFAULT_JOB or db.kv_get("seeded"):
        return
    job = Job(
        name="Entertainment Server → Archive",
        description="Imported from media-backup.sh: one-way mirror with deletions, mount sentinels, "
                    "empty-source guard and a nightly schedule.",
        pairs=[FolderPair(left=config.SEED_LEFT, right=config.SEED_RIGHT)],
    )
    job.filter.exclude = list(DEFAULT_EXCLUDES)
    job.sync.variant = "mirror"
    job.schedule.triggers = [Trigger(type="daily", time="03:00", enabled=False)]
    db.save_job(job)
    db.kv_set("seeded", True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    events.bind_loop(asyncio.get_running_loop())
    db.conn()
    fsutil.set_size_units(db.kv_get("size_units", "si"))
    seed()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title="Proto-Sync", version=__version__, lifespan=lifespan)


# ------------------------------------------------------------------ auth ----
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.AUTH_USER and config.AUTH_PASSWORD:
        path = request.url.path
        if not (path.startswith("/api/hooks/") or path == "/api/health"):
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = hmac.compare_digest(user, config.AUTH_USER) and hmac.compare_digest(pw, config.AUTH_PASSWORD)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Proto-Sync"'})
    return await call_next(request)


# ----------------------------------------------------------- caching ----
@app.middleware("http")
async def static_revalidate(request: Request, call_next):
    """Make browsers re-check UI files on every load.

    Without an explicit Cache-Control header browsers apply heuristic caching
    and may keep serving an old app.js / util.js after an update. "no-cache"
    still uses the cache, but asks first: unchanged files cost a tiny 304.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _job_or_404(job_id: str) -> Job:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _session_or_404(sid: str):
    res = runner.session(sid)
    if not res:
        raise HTTPException(404, "This comparison has expired — compare again")
    return res


def _validate_paths(job: Job) -> None:
    for p in job.pairs:
        for side in (p.left, p.right):
            if side and not fsutil.path_allowed(side):
                raise HTTPException(400, f"{side} is outside the allowed roots: {', '.join(config.ALLOWED_ROOTS)}")
        if p.left and p.right:
            l, r = os.path.realpath(p.left), os.path.realpath(p.right)
            if l == r or l.startswith(r.rstrip("/") + "/") or r.startswith(l.rstrip("/") + "/"):
                raise HTTPException(400, f"Folders overlap: {p.left} and {p.right}. Pick separate trees.")


# ------------------------------------------------------------------ misc ----
@app.get("/api/health")
def health():
    return {"ok": True, "version": __version__}


@app.get("/api/info")
def info():
    rsync_v = ""
    if shutil.which("rsync"):
        try:
            rsync_v = subprocess.run(["rsync", "--version"], capture_output=True, text=True, timeout=5).stdout.splitlines()[0]
        except Exception:  # noqa: BLE001
            pass
    return {
        "version": __version__, "rsync": rsync_v, "roots": config.ALLOWED_ROOTS,
        "max_runs": config.MAX_CONCURRENT_RUNS, "categories": CATEGORIES, "actions": ACTIONS,
        "log_retention_days": db.kv_get("log_retention_days", 30),
        "size_units": db.kv_get("size_units", "si"),
        "auth": bool(config.AUTH_USER and config.AUTH_PASSWORD),
        "timezone": str(scheduler.sched.timezone),
    }


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    if "log_retention_days" in body:
        db.kv_set("log_retention_days", max(1, int(body["log_retention_days"])))
    if "size_units" in body:
        units = "iec" if body["size_units"] == "iec" else "si"
        db.kv_set("size_units", units)
        fsutil.set_size_units(units)
    return info()


# ------------------------------------------------------------------ jobs ----
def _job_card(job: Job, next_runs: dict) -> dict:
    last = db.last_run(job.id)
    return {**json.loads(job.model_dump_json()), "last_run": last, "next_run": next_runs.get(job.id),
            "busy": runner.job_busy(job.id), "comparing": job.id in runner.compares}


@app.get("/api/jobs")
def list_jobs():
    nr = scheduler.next_runs()
    return [_job_card(j, nr) for j in db.list_jobs()]


@app.post("/api/jobs")
def create_job(body: dict = Body(default={})):
    body.pop("id", None)
    body.pop("webhook_token", None)
    job = Job.model_validate(body or {})
    _validate_paths(job)
    db.save_job(job)
    scheduler.reload()
    return _job_card(job, scheduler.next_runs())


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _job_or_404(job_id)
    card = _job_card(job, scheduler.next_runs())
    card["triggers_info"] = scheduler.trigger_info(job)
    return card


@app.put("/api/jobs/{job_id}")
def update_job(job_id: str, body: dict = Body(...)):
    old = _job_or_404(job_id)
    body["id"] = job_id
    body["webhook_token"] = old.webhook_token
    body["created"] = old.created
    job = Job.model_validate(body)
    _validate_paths(job)
    if job.sync.variant == "two_way" and old.sync.variant != "two_way":
        db.clear_baseline(job_id)
    if [(p.id, p.left, p.right) for p in job.pairs] != [(p.id, p.left, p.right) for p in old.pairs]:
        db.clear_baseline(job_id)
    db.save_job(job)
    runner.drop_sessions(job_id)
    scheduler.reload()
    return get_job(job_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _job_or_404(job_id)
    if runner.job_busy(job_id):
        raise HTTPException(409, "Stop the running sync first")
    db.delete_job(job_id)
    runner.drop_sessions(job_id)
    scheduler.reload()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/duplicate")
def duplicate_job(job_id: str):
    job = _job_or_404(job_id)
    data = json.loads(job.model_dump_json())
    for k in ("id", "webhook_token", "created", "updated"):
        data.pop(k, None)
    data["name"] = f"{job.name} (copy)"
    for t in data["schedule"]["triggers"]:
        t["enabled"] = False
    return create_job(data)


@app.post("/api/jobs/{job_id}/token")
def rotate_token(job_id: str):
    job = _job_or_404(job_id)
    job.webhook_token = secrets.token_urlsafe(18)
    db.save_job(job)
    return {"webhook_token": job.webhook_token}


@app.get("/api/jobs/{job_id}/export")
def export_job(job_id: str):
    job = _job_or_404(job_id)
    data = json.loads(job.model_dump_json())
    data.pop("webhook_token", None)
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{job.name}.protosync.json"'})


@app.post("/api/jobs/import")
def import_job(body: dict = Body(...)):
    return create_job(body)


@app.post("/api/jobs/{job_id}/reset-baseline")
def reset_baseline(job_id: str):
    _job_or_404(job_id)
    db.clear_baseline(job_id)
    return {"ok": True}


# --------------------------------------------------------------- compare ----
@app.post("/api/jobs/{job_id}/compare")
def start_compare(job_id: str):
    job = _job_or_404(job_id)
    try:
        runner.start_compare(job)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.delete("/api/jobs/{job_id}/compare")
def cancel_compare(job_id: str):
    return {"ok": runner.cancel_compare(job_id)}


@app.get("/api/jobs/{job_id}/session")
def latest_session(job_id: str):
    res = runner.latest_session(job_id)
    return compare.summary(res) if res else None


@app.get("/api/sessions/{sid}")
def get_session(sid: str):
    return compare.summary(_session_or_404(sid))


def _parse_filters(cats: str, acts: str) -> tuple[Optional[set], Optional[set]]:
    c = {x for x in cats.split(",") if x} or None
    a = {x for x in acts.split(",") if x} or None
    return c, a


_filter_cache: dict[tuple, list[int]] = {}


def _filtered(res, cats: str, acts: str, q: str, equal: bool, version: int) -> list[int]:
    key = (res.id, cats, acts, q, equal, version)
    hit = _filter_cache.get(key)
    if hit is None:
        c, a = _parse_filters(cats, acts)
        hit = compare.filter_rows(res, c, a, q, equal)
        if len(_filter_cache) > 20:
            _filter_cache.clear()
        _filter_cache[key] = hit
    return hit


_versions: dict[str, int] = {}


@app.get("/api/sessions/{sid}/rows")
def session_rows(sid: str, offset: int = 0, limit: int = Query(200, le=2000), cats: str = "", acts: str = "",
                 q: str = "", equal: bool = False):
    res = _session_or_404(sid)
    ids = _filtered(res, cats, acts, q, equal, _versions.get(sid, 0))
    window = ids[offset: offset + limit]
    return {"total": len(ids), "offset": offset,
            "rows": [compare.row_to_dict(res, res.rows[i]) for i in window]}


@app.get("/api/sessions/{sid}/ids")
def session_ids(sid: str, cats: str = "", acts: str = "", q: str = "", equal: bool = False):
    res = _session_or_404(sid)
    return _filtered(res, cats, acts, q, equal, _versions.get(sid, 0))


@app.post("/api/sessions/{sid}/actions")
def set_actions(sid: str, body: dict = Body(...)):
    res = _session_or_404(sid)
    action = body.get("action", "")
    if action not in ACTIONS and action != "default":
        raise HTTPException(400, f"Unknown action {action}")
    if body.get("all_filtered"):
        f = body.get("filters", {})
        ids = _filtered(res, f.get("cats", ""), f.get("acts", ""), f.get("q", ""), bool(f.get("equal")),
                        _versions.get(sid, 0))
    else:
        ids = [int(i) for i in body.get("ids", [])]
    changed = compare.set_action(res, ids, action)
    _versions[sid] = _versions.get(sid, 0) + 1
    return {"changed": changed, "summary": compare.summary(res)}


@app.post("/api/sessions/{sid}/exclude")
def exclude_rows(sid: str, body: dict = Body(...)):
    """Add the selected items to the job's exclude filter (like FFS 'Exclude via filter')."""
    res = _session_or_404(sid)
    job = _job_or_404(res.job.id)
    pats = []
    for i in body.get("ids", []):
        row = res.rows[int(i)]
        pats.append("/" + row.rel + ("/" if row.kind == "d" else ""))
    for p in pats:
        if p not in job.filter.exclude:
            job.filter.exclude.append(p)
    db.save_job(job)
    compare.set_action(res, [int(i) for i in body.get("ids", [])], "none")
    _versions[sid] = _versions.get(sid, 0) + 1
    return {"added": pats, "summary": compare.summary(res)}


@app.post("/api/sessions/{sid}/sync")
def sync_session(sid: str, body: dict = Body(default={})):
    res = _session_or_404(sid)
    job = _job_or_404(res.job.id)
    try:
        run_id = runner.start_run(job, "manual", bool(body.get("dry_run")), session=res, force=bool(body.get("force")))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"run_id": run_id}


# ------------------------------------------------------------------ runs ----
@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str, body: dict = Body(default={})):
    job = _job_or_404(job_id)
    try:
        run_id = runner.start_run(job, "manual", bool(body.get("dry_run")), force=bool(body.get("force")))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"run_id": run_id}


@app.get("/api/runs")
def list_runs(job_id: Optional[str] = None, limit: int = Query(100, le=500), offset: int = 0):
    return db.list_runs(job_id, limit, offset)


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int, download: bool = False):
    run = get_run(run_id)
    path = run.get("log_path") or str(config.LOG_DIR / f"run-{run_id}.log")
    if not os.path.isfile(path):
        return PlainTextResponse("(no log was written for this run)")
    if download:
        return FileResponse(path, filename=f"protosync-run-{run_id}.log", media_type="text/plain")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        data = fh.read()
    if len(data) > 4_000_000:
        data = data[:1_000_000] + "\n\n… log truncated for display — download the full file …\n\n" + data[-2_000_000:]
    return PlainTextResponse(data)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: int):
    return {"ok": runner.cancel(run_id)}


@app.post("/api/runs/{run_id}/pause")
def pause_run(run_id: int):
    return {"ok": runner.pause(run_id, True)}


@app.post("/api/runs/{run_id}/resume")
def resume_run(run_id: int):
    return {"ok": runner.pause(run_id, False)}


@app.get("/api/active")
def active():
    return runner.snapshot()


# --------------------------------------------------------------- webhook ----
@app.api_route("/api/hooks/{job_id}", methods=["GET", "POST"])
def webhook(job_id: str, token: str = "", dry_run: bool = False):
    job = db.get_job(job_id)
    if not job or not token or not hmac.compare_digest(token, job.webhook_token):
        raise HTTPException(403, "Invalid job or token")
    try:
        run_id = runner.start_run(job, "webhook", dry_run)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"run_id": run_id, "queued": run_id is None}


# -------------------------------------------------------------- schedule ----
@app.get("/api/schedule")
def schedule_overview():
    nr = scheduler.next_runs()
    return [{"job_id": j.id, "name": j.name, "enabled": j.schedule.enabled, "next_run": nr.get(j.id),
             "triggers": scheduler.trigger_info(j), "skip": db.kv_get(f"skip:{j.id}")} for j in db.list_jobs()]


@app.post("/api/schedule/preview")
def schedule_preview(body: dict = Body(...)):
    """Next fire times for an unsaved trigger — powers the live preview in the editor."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    t = Trigger.model_validate(body)
    tz = scheduler.sched.timezone
    try:
        if t.type == "cron":
            trig = CronTrigger.from_crontab(t.cron, timezone=tz)
        elif t.type == "daily":
            hh, mm = (t.time or "03:00").split(":")
            trig = CronTrigger(day_of_week=",".join(map(str, sorted(set(t.days)))) or "0-6",
                               hour=int(hh), minute=int(mm), timezone=tz)
        elif t.type == "interval":
            trig = IntervalTrigger(minutes=max(1, t.every_minutes), timezone=tz)
        else:
            return {"times": []}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Invalid schedule: {e}")
    now = dt.datetime.now(tz)
    times, prev = [], None
    cur = now
    for _ in range(5):
        nxt = trig.get_next_fire_time(prev, cur)
        if not nxt:
            break
        times.append(nxt.timestamp())
        prev = nxt
        cur = nxt + dt.timedelta(seconds=1)
    return {"times": times}


# --------------------------------------------------------------------- fs ---
@app.get("/api/fs/browse")
def fs_browse(path: str = ""):
    try:
        return fsutil.browse(path)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except FileNotFoundError:
        raise HTTPException(404, f"{path} does not exist")
    except OSError as e:
        raise HTTPException(400, str(e))


@app.get("/api/fs/volumes")
def fs_volumes():
    return fsutil.volumes()


@app.get("/api/fs/check")
def fs_check(path: str, sentinel: str = ".mounted"):
    real = os.path.realpath(path) if path else ""
    return {
        "path": real, "allowed": bool(real) and fsutil.path_allowed(real), "exists": bool(real) and os.path.isdir(real),
        "sentinel": bool(real) and bool(sentinel) and os.path.exists(os.path.join(real, sentinel)),
        "fstype": fsutil.fstype(real) if real and os.path.isdir(real) else "",
        "system_disk": bool(real) and os.path.isdir(real) and fsutil.same_device(real, "/"),
        "usage": fsutil.disk_usage(real) if real and os.path.isdir(real) else None,
    }


@app.post("/api/fs/sentinel")
def fs_sentinel(body: dict = Body(...)):
    path = os.path.realpath(body.get("path", ""))
    name = body.get("name") or ".mounted"
    if "/" in name or name in (".", ".."):
        raise HTTPException(400, "Sentinel must be a plain file name")
    if not fsutil.path_allowed(path) or not os.path.isdir(path):
        raise HTTPException(400, f"{path} is not an allowed, existing folder")
    Path(path, name).touch(exist_ok=True)
    return {"ok": True}


@app.post("/api/fs/mkdir")
def fs_mkdir(body: dict = Body(...)):
    path = os.path.realpath(body.get("path", ""))
    if not fsutil.path_allowed(path):
        raise HTTPException(403, "Outside allowed roots")
    os.makedirs(path, exist_ok=True)
    return {"ok": True, "path": path}


# ----------------------------------------------------------------- notify ---
@app.post("/api/notify/test")
def notify_test(body: dict = Body(...)):
    target = NotifyTarget.model_validate(body)
    try:
        notify.test_target(target)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    return {"ok": True}


# ------------------------------------------------------------------- SSE ----
@app.get("/api/events")
async def sse(request: Request):
    q = events.subscribe()

    async def gen():
        try:
            yield f"event: hello\ndata: {json.dumps(runner.snapshot(), default=str)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- static ----
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info", proxy_headers=True)


if __name__ == "__main__":
    run()
