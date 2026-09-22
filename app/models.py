"""Job configuration schema. Everything a job does is described here."""
from __future__ import annotations

import secrets
import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field

# ----------------------------------------------------------------- vocab -----
Category = Literal[
    "equal", "left_only", "right_only", "left_newer", "right_newer", "different", "conflict",
]
Action = Literal[
    "none", "copy_lr", "copy_rl", "delete_left", "delete_right", "move_left", "move_right",
]

CATEGORIES: list[str] = [
    "left_only", "right_only", "left_newer", "right_newer", "different", "conflict", "equal",
]
ACTIONS: list[str] = [
    "copy_lr", "copy_rl", "delete_left", "delete_right", "move_left", "move_right", "none",
]

# Fixed category → action tables for the built-in variants (two-way is DB driven).
VARIANT_TABLES: dict[str, dict[str, str]] = {
    "mirror": {
        "left_only": "copy_lr", "right_only": "delete_right", "left_newer": "copy_lr",
        "right_newer": "copy_lr", "different": "copy_lr", "conflict": "copy_lr", "equal": "none",
    },
    "update": {
        "left_only": "copy_lr", "right_only": "none", "left_newer": "copy_lr",
        "right_newer": "none", "different": "copy_lr", "conflict": "none", "equal": "none",
    },
}

DEFAULT_EXCLUDES = [
    ".mounted",
    ".Trash-*",
    "$RECYCLE.BIN/",
    "System Volume Information/",
    "lost+found/",
    "*.partial",
    ".DS_Store",
]


def _id() -> str:
    return uuid.uuid4().hex[:12]


# ------------------------------------------------------------- sections ------
class FolderPair(BaseModel):
    id: str = Field(default_factory=_id)
    left: str = ""
    right: str = ""
    enabled: bool = True


class CompareSettings(BaseModel):
    variant: Literal["time_size", "content", "size"] = "time_size"
    time_tolerance: float = 2.0            # seconds (rsync --modify-window)
    ignore_time_shift: list[int] = []      # whole-hour offsets treated as equal (DST)
    symlinks: Literal["exclude", "direct", "follow"] = "direct"
    detect_moves: bool = True


class FilterSettings(BaseModel):
    include: list[str] = ["*"]
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    min_size: Optional[int] = None         # bytes
    max_size: Optional[int] = None         # bytes
    max_age_days: Optional[float] = None   # only files modified within N days
    newer_than: Optional[str] = None       # ISO date, only files modified after


class SyncSettings(BaseModel):
    variant: Literal["mirror", "update", "two_way", "custom"] = "mirror"
    custom: dict[str, str] = Field(default_factory=lambda: dict(VARIANT_TABLES["mirror"]))
    conflict: Literal["skip", "newer", "left", "right"] = "skip"   # two-way conflicts
    deletion: Literal["permanent", "recycle", "versioning"] = "recycle"
    recycle_retention_days: int = 14
    versioning_path: str = ""
    versioning_style: Literal["replace", "timestamp_folder", "timestamp_file"] = "timestamp_folder"
    versioning_max_age_days: int = 0       # 0 = keep forever
    versioning_keep_min: int = 1
    versioning_keep_max: int = 0           # 0 = unlimited
    delete_timing: Literal["after", "before"] = "after"
    preserve_permissions: Literal["auto", "yes", "no"] = "auto"
    preserve_xattrs: bool = False
    prune_empty_dirs: bool = True
    verify: Literal["off", "copied", "full"] = "off"
    bandwidth_limit_kbps: int = 0
    resume_partial: bool = True


class SafetySettings(BaseModel):
    sentinel_file: str = ".mounted"
    require_sentinel: bool = True
    require_mountpoint: bool = False
    empty_source_guard: bool = True
    max_delete_percent: float = 50.0       # 0 = off
    max_delete_count: int = 0              # 0 = off
    check_free_space: bool = True
    retries: int = 2
    retry_delay: int = 15
    on_error: Literal["continue", "stop"] = "continue"
    flush_to_disk: bool = True
    watch_mount_during_run: bool = True


class Trigger(BaseModel):
    id: str = Field(default_factory=_id)
    type: Literal["cron", "interval", "daily", "once", "startup", "realtime", "mount", "after_job"] = "daily"
    enabled: bool = True
    mode: Literal["sync", "dry_run"] = "sync"
    cron: str = "0 3 * * *"
    every_minutes: int = 360
    time: str = "03:00"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])   # 0 = Monday
    at: Optional[str] = None               # ISO datetime (once)
    idle_seconds: int = 30                 # realtime debounce
    poll: bool = False                     # realtime: polling observer instead of inotify
    after_job_id: str = ""
    after_on: Literal["success", "failure", "any"] = "success"
    jitter_seconds: int = 0


class ScheduleSettings(BaseModel):
    enabled: bool = True
    triggers: list[Trigger] = []
    window_start: str = ""                 # "HH:MM" — empty means any time
    window_end: str = ""
    queue_if_running: bool = True


class NotifyTarget(BaseModel):
    id: str = Field(default_factory=_id)
    type: Literal["ntfy", "discord", "gotify", "webhook", "email"] = "ntfy"
    enabled: bool = True
    url: str = ""
    topic: str = ""
    token: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = ""


class NotifySettings(BaseModel):
    when: Literal["always", "failure", "changes", "never"] = "failure"
    targets: list[NotifyTarget] = []
    pre_command: str = ""
    pre_command_abort: bool = True
    post_command: str = ""
    post_command_when: Literal["always", "success", "failure"] = "success"
    command_timeout: int = 600


class Job(BaseModel):
    id: str = Field(default_factory=_id)
    name: str = "New job"
    description: str = ""
    pairs: list[FolderPair] = Field(default_factory=lambda: [FolderPair()])
    compare: CompareSettings = Field(default_factory=CompareSettings)
    filter: FilterSettings = Field(default_factory=FilterSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    webhook_token: str = Field(default_factory=lambda: secrets.token_urlsafe(18))
    created: float = 0
    updated: float = 0


class ActionOverride(BaseModel):
    ids: list[int]
    action: str


class RunRequest(BaseModel):
    dry_run: bool = False
