"""Runtime settings, all overridable by environment variables."""
from __future__ import annotations

import os
from pathlib import Path


def _list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.split(",") if p.strip()]


DATA_DIR = Path(os.environ.get("PROTOSYNC_DATA", "/data")).resolve()
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "protosync.db"
LOCK_DIR = DATA_DIR / "locks"

# Only these trees can be browsed / used as sync roots from the UI.
ALLOWED_ROOTS = [str(Path(p).resolve()) for p in _list("PROTOSYNC_ROOTS", "/mnt,/media,/srv,/run/media")]

HOST = os.environ.get("PROTOSYNC_HOST", "0.0.0.0")
PORT = int(os.environ.get("PROTOSYNC_PORT", "8475"))

# Optional HTTP Basic auth (both must be set to enable).
AUTH_USER = os.environ.get("PROTOSYNC_USER", "")
AUTH_PASSWORD = os.environ.get("PROTOSYNC_PASSWORD", "")

# How many sync runs may execute at the same time (others queue).
MAX_CONCURRENT_RUNS = max(1, int(os.environ.get("PROTOSYNC_MAX_RUNS", "1")))

# Seed a job equivalent to the original media-backup.sh on first start.
SEED_DEFAULT_JOB = os.environ.get("PROTOSYNC_SEED_JOB", "1") == "1"
SEED_LEFT = os.environ.get("PROTOSYNC_SEED_LEFT", "/mnt/media")
SEED_RIGHT = os.environ.get("PROTOSYNC_SEED_RIGHT", "/mnt/media-archive1")

# Internal names always ignored by scans and never touched by syncs.
INTERNAL_PREFIX = ".protosync-"
TRASH_DIRNAME = ".protosync-trash"
PARTIAL_DIRNAME = ".protosync-partial"

for d in (DATA_DIR, LOG_DIR, LOCK_DIR):
    d.mkdir(parents=True, exist_ok=True)
