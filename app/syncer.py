"""Sync executor.

Takes a compare plan and carries it out as safely as possible:

  preflight  → mount/sentinel checks, empty-source guard, mass-delete guard, free space
  moves      → renames on the target side (detected moved files)
  folders    → create new folders
  copies     → rsync --files-from per direction (atomic temp files, resumable partials,
               overwritten files kept via --backup-dir when recycle/versioning is on)
  deletions  → permanent / recycle folder / versioning folder, re-checked before each one
  verify     → stat check of every copied file (always), optional checksum verify+repair
  finish     → fsync, two-way baseline, change-tree log, retention cleanup, hooks, notify
"""
from __future__ import annotations

import datetime as dt
import errno
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config, db, events, fsutil
from .compare import CompareResult, Row, run_compare
from .models import Job

STAMP_FMT = "%Y-%m-%d_%H%M%S"
STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{6})")
PROGRESS_RE = re.compile(rb"^\s*([\d,.]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d{2}:\d{2})")
TOCHK_RE = re.compile(rb"to-chk=(\d+)/(\d+)")
RETRYABLE_RC = {10, 11, 12, 23, 30, 35}


class Cancelled(Exception):
    pass


class Blocked(Exception):
    """A safety guard stopped the run before anything was changed."""


@dataclass
class PairPlan:
    index: int
    left: str
    right: str
    rows: list[Row] = field(default_factory=list)
    moves: list[Row] = field(default_factory=list)
    mkdirs: list[tuple[str, Row]] = field(default_factory=list)       # (side, row)
    copies_lr: list[Row] = field(default_factory=list)
    copies_rl: list[Row] = field(default_factory=list)
    del_files: list[tuple[str, Row]] = field(default_factory=list)    # (side, row)
    del_dirs: list[tuple[str, Row]] = field(default_factory=list)


def render_tree(paths: list[str]) -> list[str]:
    tree: dict = {}
    for p in sorted(set(paths), key=str.lower):
        node = tree
        for part in p.strip("/").split("/"):
            node = node.setdefault(part, {})
    out: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        items = sorted(node, key=str.lower)
        for i, name in enumerate(items):
            last = i == len(items) - 1
            out.append(prefix + ("└─ " if last else "├─ ") + name)
            walk(node[name], prefix + ("   " if last else "│  "))

    walk(tree, "  ")
    return out


class Syncer:
    def __init__(self, run_id: int, job: Job, dry_run: bool, trigger: str,
                 session: Optional[CompareResult] = None, force: bool = False,
                 cancel: Optional[threading.Event] = None, resume: Optional[threading.Event] = None,
                 on_progress: Optional[Callable[[dict], None]] = None):
        self.run_id = run_id
        self.job = job
        self.dry_run = dry_run
        self.trigger = trigger
        self.session = session
        self.force = force
        self.cancel = cancel or threading.Event()
        self.resume = resume or threading.Event()
        self.resume.set()
        self.on_progress = on_progress
        self.stamp = dt.datetime.now().strftime(STAMP_FMT)
        self.log_path = str(config.LOG_DIR / f"run-{run_id}.log")
        self._log_fh = open(self.log_path, "a", encoding="utf-8")
        self._proc: Optional[subprocess.Popen] = None
        self._paused_proc = False
        self.abort_reason = ""
        self.recent: list[str] = []
        self.changes: dict[str, list[str]] = {"created": [], "updated": [], "deleted": [], "moved": []}
        self.stats = {
            "files_copied": 0, "bytes_copied": 0, "files_deleted": 0, "dirs_created": 0,
            "dirs_deleted": 0, "moved": 0, "errors": 0, "warnings": 0, "skipped": 0,
            "verify_mismatches": 0, "duration": 0, "bytes_planned": 0,
        }
        self.progress = {
            "run_id": run_id, "job_id": job.id, "job_name": job.name, "phase": "Starting",
            "percent": 0.0, "bytes_done": 0, "bytes_total": 0, "items_done": 0, "items_total": 0,
            "rate": 0.0, "eta": None, "current": "", "paused": False, "dry_run": dry_run,
            "started": time.time(),
        }
        self._verifying = False
        self._rate_t = time.monotonic()
        self._rate_b = 0

    # --------------------------------------------------------------- output --
    def log(self, msg: str, level: str = "info") -> None:
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {'' if level == 'info' else level.upper() + ': '}{msg}"
        try:
            self._log_fh.write(line + "\n")
            self._log_fh.flush()
        except ValueError:
            pass
        self.recent.append(line)
        del self.recent[:-300]
        if level == "error":
            self.stats["errors"] += 1
        elif level == "warn":
            self.stats["warnings"] += 1
        events.publish("run_log", {"run_id": self.run_id, "line": line})

    def _emit(self, force: bool = False) -> None:
        p = self.progress
        if p["bytes_total"]:
            p["percent"] = round(min(100.0, p["bytes_done"] * 100 / p["bytes_total"]), 1)
        elif p["items_total"]:
            p["percent"] = round(min(100.0, p["items_done"] * 100 / p["items_total"]), 1)
        now = time.monotonic()
        if now - self._rate_t >= 1.0:
            inst = (p["bytes_done"] - self._rate_b) / (now - self._rate_t)
            p["rate"] = inst if not p["rate"] else p["rate"] * 0.7 + inst * 0.3
            self._rate_t, self._rate_b = now, p["bytes_done"]
            remaining = max(0, p["bytes_total"] - p["bytes_done"])
            p["eta"] = int(remaining / p["rate"]) if p["rate"] > 1 else None
        if self.on_progress:
            self.on_progress(p)
        events.publish("run_progress", p, throttle_key=None if force else f"run{self.run_id}", min_interval=0.25)

    def phase(self, name: str) -> None:
        self.progress["phase"] = name
        self.log(f"--- {name} ---")
        self._emit(force=True)

    def check_cancel(self) -> None:
        while not self.resume.is_set():
            if self.cancel.is_set():
                break
            self.resume.wait(0.5)
        if self.cancel.is_set():
            raise Cancelled(self.abort_reason or "Cancelled by user")

    # ---------------------------------------------------------- preflight ---
    def _roots(self) -> list[tuple[str, str]]:
        return [(os.path.realpath(p.left), os.path.realpath(p.right))
                for p in self.job.pairs if p.enabled and p.left and p.right]

    def preflight(self) -> None:
        s = self.job.safety
        if not shutil.which("rsync"):
            raise Blocked("rsync is not installed")
        roots = self._roots()
        if not roots:
            raise Blocked("No enabled folder pairs with both sides set")
        for left, right in roots:
            for label, root in (("Left", left), ("Right", right)):
                if not fsutil.path_allowed(root):
                    raise Blocked(f"{label} folder {root} is outside the allowed roots")
                if not os.path.isdir(root):
                    raise Blocked(f"{label} folder is missing or not mounted: {root}")
                if s.require_sentinel and s.sentinel_file:
                    sp = os.path.join(root, s.sentinel_file)
                    if not os.path.exists(sp):
                        raise Blocked(f"{label} drive not verified: {sp} is missing "
                                      f"(create it once with: touch '{sp}')")
                if s.require_mountpoint and fsutil.same_device(root, "/"):
                    raise Blocked(f"{label} folder {root} is on the system disk — its drive is not mounted")
            self.log(f"Pair ready: {left} [{fsutil.fstype(left)}] → {right} [{fsutil.fstype(right)}]")

    def _check_mounts_once(self) -> Optional[str]:
        s = self.job.safety
        for left, right in self._roots():
            for root in (left, right):
                if not os.path.isdir(root):
                    return f"{root} disappeared"
                if s.require_sentinel and s.sentinel_file and not os.path.exists(os.path.join(root, s.sentinel_file)):
                    return f"Sentinel vanished from {root} — drive dropped?"
        return None

    def _mount_watch(self, stop: threading.Event) -> None:
        while not stop.wait(5):
            result: list[Optional[str]] = []
            t = threading.Thread(target=lambda: result.append(self._check_mounts_once()), daemon=True)
            t.start()
            t.join(20)
            reason = result[0] if result else "A drive stopped responding (I/O hang for 20s)"
            if reason:
                self.abort_reason = f"Aborted: {reason}"
                self.log(self.abort_reason, "error")
                self.cancel.set()
                self._kill_proc()
                return

    # ---------------------------------------------------------------- plan ---
    def build_plans(self, res: CompareResult) -> list[PairPlan]:
        plans = [PairPlan(i, p.left, p.right) for i, p in enumerate(res.pairs)]
        for row in res.rows:
            plan = plans[row.pair]
            plan.rows.append(row)
            a = row.action
            if a == "none" or row.locked:
                continue
            if a in ("move_left", "move_right"):
                plan.moves.append(row)
            elif a in ("copy_lr", "copy_rl"):
                if row.kind == "d":
                    plan.mkdirs.append(("right" if a == "copy_lr" else "left", row))
                elif a == "copy_lr":
                    plan.copies_lr.append(row)
                else:
                    plan.copies_rl.append(row)
            elif a in ("delete_left", "delete_right"):
                side = "left" if a == "delete_left" else "right"
                (plan.del_dirs if row.kind == "d" else plan.del_files).append((side, row))
        for p in plans:
            p.del_dirs.sort(key=lambda sr: sr[1].rel.count("/"), reverse=True)
            p.mkdirs.sort(key=lambda sr: sr[1].rel.count("/"))
        return plans

    def guards(self, res: CompareResult, plans: list[PairPlan]) -> None:
        s = self.job.safety
        variant = self.job.sync.variant
        for plan, info in zip(plans, res.pairs):
            lf = info.left_scan.files if info.left_scan else 0
            rf = info.right_scan.files if info.right_scan else 0
            le = len(info.left_scan.entries) if info.left_scan else 0
            re_ = len(info.right_scan.entries) if info.right_scan else 0
            deletes_r = sum(1 for side, _ in plan.del_files if side == "right")
            deletes_l = sum(1 for side, _ in plan.del_files if side == "left")
            if s.empty_source_guard:
                if le == 0 and re_ > 0 and (deletes_r or variant == "two_way"):
                    raise Blocked(f"Left folder {plan.left} looks empty while the right has {re_} items. "
                                  "Refusing to mirror an empty source.")
                if variant == "two_way" and re_ == 0 and le > 0 and deletes_l:
                    raise Blocked(f"Right folder {plan.right} looks empty. Refusing to propagate deletions.")
            for side, count, total in (("right", deletes_r, rf), ("left", deletes_l, lf)):
                if not count:
                    continue
                pct = count * 100 / max(1, total)
                if s.max_delete_percent and pct > s.max_delete_percent and not self.force:
                    raise Blocked(f"Would delete {count} of {total} files ({pct:.0f}%) on the {side} side — "
                                  f"over the {s.max_delete_percent:.0f}% safety limit. Review the preview and "
                                  "confirm to override.")
                if s.max_delete_count and count > s.max_delete_count and not self.force:
                    raise Blocked(f"Would delete {count} files on the {side} side — over the limit of "
                                  f"{s.max_delete_count}. Review the preview and confirm to override.")
            if s.check_free_space:
                perm = self.job.sync.deletion == "permanent"
                for side, copies, root in (("right", plan.copies_lr, plan.right), ("left", plan.copies_rl, plan.left)):
                    if not copies:
                        continue
                    need = 0
                    largest = 0
                    for r in copies:
                        src = r.l if side == "right" else r.r
                        old = r.r if side == "right" else r.l
                        need += src.size - (old.size if (old and perm) else 0)
                        largest = max(largest, src.size)
                    need += largest                     # temp file during transfer
                    usage = fsutil.disk_usage(root)
                    if usage and need > usage["free"]:
                        raise Blocked(f"Not enough space on the {side} side ({root}): needs "
                                      f"{fsutil.human(need)}, {fsutil.human(usage['free'])} free")

    # ----------------------------------------------------------- deletion ---
    def _versioning_root(self, plan: PairPlan, side: str) -> str:
        sync = self.job.sync
        root = plan.left if side == "left" else plan.right
        if sync.deletion == "recycle" or not sync.versioning_path:
            return os.path.join(root, config.TRASH_DIRNAME, self.stamp)
        base = os.path.realpath(sync.versioning_path)
        if len(self._roots()) > 1 or self.job.sync.variant == "two_way":
            base = os.path.join(base, f"pair{plan.index + 1}-{side}")
        if sync.versioning_style == "timestamp_folder":
            return os.path.join(base, self.stamp)
        return base

    def _move_aside(self, path: str, dest: str) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest):
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest)
            else:
                os.remove(dest)
        try:
            os.rename(path, dest)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            shutil.move(path, dest)

    def delete_item(self, plan: PairPlan, side: str, row: Row) -> bool:
        root = plan.left if side == "left" else plan.right
        path = os.path.join(root, row.rel)
        expected = row.l if side == "left" else row.r
        try:
            st = os.stat(path) if self.job.compare.symlinks == "follow" else os.lstat(path)
        except FileNotFoundError:
            return True
        if row.kind != "d" and expected is not None:
            if st.st_size != expected.size or abs(st.st_mtime - expected.mtime) > max(2.0, self.job.compare.time_tolerance):
                self.log(f"Skipped delete (changed since compare): {side}/{row.rel}", "warn")
                self.stats["skipped"] += 1
                return False
        mode = self.job.sync.deletion
        if row.kind == "d":
            try:
                os.rmdir(path)
                self.stats["dirs_deleted"] += 1
                return True
            except OSError as e:
                if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    self.log(f"Kept folder (not empty, contains filtered items): {side}/{row.rel}", "warn")
                    return False
                raise
        if mode == "permanent":
            os.remove(path)
        else:
            dest = os.path.join(self._versioning_root(plan, side), row.rel)
            if mode == "versioning" and self.job.sync.versioning_style == "timestamp_file":
                dest = f"{dest}.{self.stamp}"
            self._move_aside(path, dest)
        self.stats["files_deleted"] += 1
        return True

    # -------------------------------------------------------------- rsync ---
    def _rsync_base(self, plan: PairPlan) -> list[str]:
        sync = self.job.sync
        cmd = ["rsync", "-rltD", "--from0", "--out-format=@@%i|%n", "--info=progress2", "--no-inc-recursive"]
        fs_l = fsutil.fstype(plan.left)
        fs_r = fsutil.fstype(plan.right)
        posix = fs_l not in fsutil.NO_PERM_FS and fs_r not in fsutil.NO_PERM_FS
        if sync.preserve_permissions == "yes" or (sync.preserve_permissions == "auto" and posix):
            cmd.append("--perms")
        else:
            cmd.append("--no-perms")
        if sync.preserve_permissions == "yes":
            cmd += ["--owner", "--group"]
        if sync.preserve_xattrs and posix:
            cmd += ["-X", "-A"]
        window = self.job.compare.time_tolerance
        if fs_l in fsutil.COARSE_TIME_FS or fs_r in fsutil.COARSE_TIME_FS:
            window = max(window, 2)
        cmd.append(f"--modify-window={int(round(window))}")
        if self.job.compare.symlinks == "follow":
            cmd.append("--copy-links")
        if sync.resume_partial:
            cmd.append(f"--partial-dir={config.PARTIAL_DIRNAME}")
        if sync.bandwidth_limit_kbps:
            cmd.append(f"--bwlimit={int(sync.bandwidth_limit_kbps)}")
        return cmd

    def _kill_proc(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                if self._paused_proc:
                    p.send_signal(signal.SIGCONT)
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
            except ProcessLookupError:
                pass

    def _run_rsync(self, cmd: list[str], rels: list[str], label: str) -> tuple[int, set[str]]:
        fd, listfile = tempfile.mkstemp(prefix="filelist-", dir=str(config.DATA_DIR))
        with os.fdopen(fd, "wb") as fh:
            for r in rels:
                fh.write(r.encode("utf-8", "surrogateescape") + b"\0")
        full = cmd[:-2] + [f"--files-from={listfile}"] + cmd[-2:]
        self.log(f"{label}: rsync {len(rels)} item(s)")
        transferred: set[str] = set()
        stderr_lines: list[str] = []
        try:
            self._proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          start_new_session=True)

            def read_err():
                for raw in self._proc.stderr:
                    stderr_lines.append(raw.decode("utf-8", "replace").rstrip())

            et = threading.Thread(target=read_err, daemon=True)
            et.start()
            buf = b""
            base = self.progress["bytes_done"]
            items_base = self.progress["items_done"]
            while True:
                if not self.resume.is_set() and not self._paused_proc:
                    self._proc.send_signal(signal.SIGSTOP)
                    self._paused_proc = True
                    self.progress["paused"] = True
                    self._emit(force=True)
                if self.resume.is_set() and self._paused_proc:
                    self._proc.send_signal(signal.SIGCONT)
                    self._paused_proc = False
                    self.progress["paused"] = False
                    self._emit(force=True)
                if self.cancel.is_set():
                    self._kill_proc()
                    break
                if self._paused_proc:
                    time.sleep(0.3)
                    continue
                chunk = self._proc.stdout.read1(65536) if hasattr(self._proc.stdout, "read1") else self._proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for part in parts:
                    if part.startswith(b"@@"):
                        try:
                            item, name = part[2:].decode("utf-8", "surrogateescape").split("|", 1)
                        except ValueError:
                            continue
                        if item.startswith((">", "<", "c")):
                            transferred.add(name.rstrip("/"))
                            self.progress["current"] = name
                            if not self._verifying:
                                self.progress["items_done"] += 1
                    else:
                        m = PROGRESS_RE.match(part)
                        if m:
                            if self._verifying:
                                c = TOCHK_RE.search(part)
                                if c:
                                    left, total = int(c.group(1)), int(c.group(2))
                                    self.progress["items_done"] = items_base + (total - left)
                            else:
                                done = int(m.group(1).replace(b",", b"").replace(b".", b""))
                                self.progress["bytes_done"] = min(self.progress["bytes_total"], base + done)
                            self._emit()
            rc = self._proc.wait()
            et.join(2)
        finally:
            self._proc = None
            try:
                os.remove(listfile)
            except OSError:
                pass
        for line in stderr_lines[-200:]:
            if line.strip():
                self.log(f"rsync: {line}", "warn" if rc in (0, 24) else "error")
        return rc, transferred

    def _stat_ok(self, src: str, dst: str) -> Optional[bool]:
        """True = copied correctly, False = mismatch, None = source vanished."""
        try:
            s = os.lstat(src) if self.job.compare.symlinks != "follow" else os.stat(src)
        except FileNotFoundError:
            return None
        try:
            d = os.lstat(dst) if self.job.compare.symlinks != "follow" else os.stat(dst)
        except FileNotFoundError:
            return False
        tol = max(2.0, self.job.compare.time_tolerance)
        return s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) <= tol

    def copy_side(self, plan: PairPlan, rows: list[Row], direction: str) -> None:
        if not rows:
            return
        src_root, dst_root = (plan.left, plan.right) if direction == "lr" else (plan.right, plan.left)
        dst_side = "right" if direction == "lr" else "left"
        cmd = self._rsync_base(plan)
        if self.job.sync.deletion in ("recycle", "versioning"):
            bdir = self._versioning_root(plan, dst_side)
            cmd += ["--backup", f"--backup-dir={bdir}"]
            if self.job.sync.deletion == "versioning" and self.job.sync.versioning_style == "timestamp_file":
                cmd.append(f"--suffix=.{self.stamp}")
            else:
                cmd.append("--suffix=")
        cmd += [src_root.rstrip("/") + "/", dst_root.rstrip("/") + "/"]
        pending = {r.rel: r for r in rows}
        existed = {r.rel for r in rows if (r.r if direction == "lr" else r.l) is not None}
        attempts = self.job.safety.retries + 1
        for attempt in range(1, attempts + 1):
            self.check_cancel()
            label = f"Copy {'left → right' if direction == 'lr' else 'right → left'}"
            if attempt > 1:
                label += f" (retry {attempt - 1})"
            rc, _ = self._run_rsync(cmd, list(pending), label)
            self.check_cancel()
            if rc not in (0, 23, 24, *RETRYABLE_RC):
                raise RuntimeError(f"rsync failed with exit code {rc}")
            # Verify every file of this batch by size + mtime.
            still: dict[str, Row] = {}
            for rel, row in pending.items():
                ok = self._stat_ok(os.path.join(src_root, rel), os.path.join(dst_root, rel))
                if ok is None:
                    self.log(f"Source vanished before copy: {rel}", "warn")
                    self.stats["skipped"] += 1
                elif ok:
                    size = (row.l if direction == "lr" else row.r).size
                    self.stats["files_copied"] += 1
                    self.stats["bytes_copied"] += size
                    tag = "updated" if rel in existed else "created"
                    self.changes[tag].append(f"{dst_side}/{rel}")
                    self._copied.setdefault(plan.index, {}).setdefault(direction, []).append(rel)
                else:
                    still[rel] = row
            pending = still
            if not pending:
                return
            if attempt < attempts:
                self.log(f"{len(pending)} file(s) not copied correctly, retrying in {self.job.safety.retry_delay}s", "warn")
                for _ in range(max(1, self.job.safety.retry_delay) * 2):
                    self.check_cancel()
                    time.sleep(0.5)
        for rel in list(pending)[:200]:
            self.log(f"Failed to copy: {rel}", "error")
        self.stats["errors"] += max(0, len(pending) - 200)
        self._failed.update((plan.index, r) for r in pending)
        if self.job.safety.on_error == "stop":
            raise RuntimeError(f"{len(pending)} file(s) failed to copy")

    def verify(self, plans: list[PairPlan]) -> None:
        mode = self.job.sync.verify
        if mode == "off":
            return
        self.phase("Verifying (checksum)" if mode == "full" else "Verifying copied files (checksum)")
        for plan in plans:
            for direction in ("lr", "rl"):
                rels = list(self._copied.get(plan.index, {}).get(direction, []))
                if mode == "full" and direction == "lr":
                    # Whole-mirror integrity check: everything that is supposed to be identical.
                    rels += [r.rel for r in plan.rows if r.kind == "f" and r.l and r.r
                             and r.action == "none" and r.category == "equal"]
                if not rels:
                    continue
                src, dst = (plan.left, plan.right) if direction == "lr" else (plan.right, plan.left)
                cmd = self._rsync_base(plan) + ["--checksum", src.rstrip("/") + "/", dst.rstrip("/") + "/"]
                self.progress.update(bytes_total=0, bytes_done=0, items_total=len(rels), items_done=0, rate=0.0)
                self._verifying = True
                try:
                    rc, repaired = self._run_rsync(cmd, rels, "Checksum verify & repair")
                finally:
                    self._verifying = False
                if repaired:
                    self.stats["verify_mismatches"] += len(repaired)
                    for r in sorted(repaired)[:200]:
                        self.log(f"Checksum mismatch repaired: {r}", "warn")
                if rc not in (0, 24):
                    self.log(f"Verify pass ended with rsync code {rc}", "error")

    # ---------------------------------------------------------- retention ---
    def cleanup(self, plans: list[PairPlan]) -> None:
        sync = self.job.sync
        if sync.deletion == "recycle" and sync.recycle_retention_days > 0:
            cutoff = dt.datetime.now() - dt.timedelta(days=sync.recycle_retention_days)
            for plan in plans:
                for root in (plan.left, plan.right):
                    tdir = os.path.join(root, config.TRASH_DIRNAME)
                    if not os.path.isdir(tdir):
                        continue
                    for name in os.listdir(tdir):
                        try:
                            when = dt.datetime.strptime(name, STAMP_FMT)
                        except ValueError:
                            continue
                        if when < cutoff:
                            shutil.rmtree(os.path.join(tdir, name), ignore_errors=True)
                            self.log(f"Emptied recycle folder {tdir}/{name}")
        if sync.deletion == "versioning" and sync.versioning_path and \
                (sync.versioning_max_age_days or sync.versioning_keep_max):
            self._cleanup_versions(os.path.realpath(sync.versioning_path))

    def _cleanup_versions(self, base: str) -> None:
        sync = self.job.sync
        if not os.path.isdir(base):
            return
        versions: dict[str, list[tuple[str, str]]] = {}     # logical rel -> [(stamp, path)]
        for dirpath, _dirs, files in os.walk(base):
            relroot = os.path.relpath(dirpath, base)
            for f in files:
                full = os.path.join(dirpath, f)
                if sync.versioning_style == "timestamp_file":
                    m = re.search(r"\.(\d{4}-\d{2}-\d{2}_\d{6})$", f)
                    if not m:
                        continue
                    key = os.path.join(relroot, f[: m.start()])
                    versions.setdefault(key, []).append((m.group(1), full))
                else:
                    parts = relroot.split(os.sep)
                    idx = next((i for i, p in enumerate(parts) if STAMP_RE.fullmatch(p)), None)
                    if idx is None:
                        continue
                    key = os.path.join(*parts[:idx], *parts[idx + 1:], f)
                    versions.setdefault(key, []).append((parts[idx], full))
        cutoff = (dt.datetime.now() - dt.timedelta(days=sync.versioning_max_age_days)).strftime(STAMP_FMT) \
            if sync.versioning_max_age_days else None
        removed = 0
        for key, vers in versions.items():
            vers.sort(reverse=True)                      # newest first
            for i, (stamp, path) in enumerate(vers):
                keep = i < max(0, sync.versioning_keep_min)
                too_many = sync.versioning_keep_max and i >= sync.versioning_keep_max
                too_old = cutoff is not None and stamp < cutoff
                if not keep and (too_many or too_old):
                    try:
                        os.remove(path)
                        removed += 1
                    except OSError:
                        pass
        for dirpath, dirs, files in os.walk(base, topdown=False):
            if dirpath != base and not dirs and not files:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        if removed:
            self.log(f"Versioning cleanup removed {removed} old version(s)")

    # --------------------------------------------------------------- main ---
    def execute(self) -> dict:
        t0 = time.time()
        status, error = "success", ""
        self._copied: dict[int, dict[str, list[str]]] = {}
        self._failed: set[tuple[int, str]] = set()
        watch_stop = threading.Event()
        res: Optional[CompareResult] = None
        plans: list[PairPlan] = []
        self.log(f"=== {self.job.name}: run #{self.run_id} ({self.trigger}) ===")
        if self.dry_run:
            self.log(">>> DRY RUN: nothing will be written <<<")
        try:
            self.phase("Checking drives")
            self.preflight()

            if self.job.notify.pre_command and not self.dry_run:
                from .notify import run_hook
                rc = run_hook(self.job.notify.pre_command, self._hook_env("running"),
                              self.job.notify.command_timeout, self.log)
                if rc != 0 and self.job.notify.pre_command_abort:
                    raise Blocked(f"Pre-sync command failed with exit code {rc}")

            if self.session is not None and self.session.job.id == self.job.id:
                res = self.session
                age = time.time() - res.created
                self.log(f"Using reviewed comparison from {int(age // 60)} min ago")
            else:
                self.phase("Comparing")
                res = run_compare(self.job, self.cancel, self._compare_progress)
            if res.cancelled:
                raise Cancelled(self.abort_reason or "Cancelled during compare")
            for e in res.errors[:50]:
                self.log(e, "warn")

            plans = self.build_plans(res)
            self.guards(res, plans)
            self._plan_totals(plans)
            self._log_plan(plans)

            if self.dry_run:
                self._dry_changes(plans)
                status = "success"
                return self._finish(t0, status, error, res, plans, watch_stop)

            if self.job.safety.watch_mount_during_run:
                threading.Thread(target=self._mount_watch, args=(watch_stop,), daemon=True).start()

            for plan in plans:
                self._execute_plan(plan)

            self.verify(plans)
            self.check_cancel()
            if self.job.safety.flush_to_disk:
                self.phase("Flushing to disk")
                os.sync()
            if self.job.sync.variant == "two_way":
                self._update_baseline(res, plans)
            self.cleanup(plans)
            if self.stats["errors"]:
                status = "warning"
        except Blocked as e:
            status, error = "blocked", str(e)
            self.log(str(e), "error")
        except Cancelled as e:
            status, error = "cancelled", str(e)
            self.log(str(e), "warn")
        except Exception as e:  # noqa: BLE001
            status, error = "failed", f"{type(e).__name__}: {e}"
            self.log(error, "error")
        return self._finish(t0, status, error, res, plans, watch_stop)

    def _execute_plan(self, plan: PairPlan) -> None:
        # Moves first: cheap renames that save whole re-copies.
        if plan.moves:
            self.phase(f"Moving {len(plan.moves)} file(s)")
            for row in plan.moves:
                self.check_cancel()
                root = plan.right if row.action == "move_right" else plan.left
                src, dst = os.path.join(root, row.move_from), os.path.join(root, row.rel)
                try:
                    if os.path.lexists(dst):
                        raise FileExistsError(dst)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.rename(src, dst)
                    self.stats["moved"] += 1
                    side = "right" if row.action == "move_right" else "left"
                    self.changes["moved"].append(f"{side}/{row.move_from}  ⇒  {row.rel}")
                except OSError as e:
                    self.log(f"Move failed ({row.move_from} → {row.rel}): {e}; will copy instead", "warn")
                    (plan.copies_lr if row.action == "move_right" else plan.copies_rl).append(row)
                self.progress["items_done"] += 1
        if plan.mkdirs:
            self.phase("Creating folders")
            for side, row in plan.mkdirs:
                self.check_cancel()
                root = plan.right if side == "right" else plan.left
                path = os.path.join(root, row.rel)
                try:
                    if not os.path.isdir(path):
                        os.makedirs(path, exist_ok=True)
                        self.stats["dirs_created"] += 1
                        self.changes["created"].append(f"{side}/{row.rel}/")
                except OSError as e:
                    self.log(f"Could not create folder {side}/{row.rel}: {e}", "error")
        if self.job.sync.delete_timing == "before":
            self._deletions(plan)
        if plan.copies_lr:
            self.phase(f"Copying {len(plan.copies_lr)} file(s) left → right")
            self.copy_side(plan, plan.copies_lr, "lr")
        if plan.copies_rl:
            self.phase(f"Copying {len(plan.copies_rl)} file(s) right → left")
            self.copy_side(plan, plan.copies_rl, "rl")
        if self.job.sync.delete_timing == "after":
            self._deletions(plan)
        if self.job.sync.prune_empty_dirs:
            self._prune_new_empty_dirs(plan)

    def _deletions(self, plan: PairPlan) -> None:
        if not plan.del_files and not plan.del_dirs:
            return
        label = {"permanent": "Deleting", "recycle": "Moving to recycle folder",
                 "versioning": "Moving to versioning folder"}[self.job.sync.deletion]
        self.phase(f"{label}: {len(plan.del_files)} file(s)")
        for side, row in plan.del_files + plan.del_dirs:
            self.check_cancel()
            try:
                if self.delete_item(plan, side, row):
                    self.changes["deleted"].append(f"{side}/{row.rel}{'/' if row.kind == 'd' else ''}")
            except OSError as e:
                self.log(f"Delete failed for {side}/{row.rel}: {e}", "error")
                if self.job.safety.on_error == "stop":
                    raise
            self.progress["items_done"] += 1
            self._emit()

    def _prune_new_empty_dirs(self, plan: PairPlan) -> None:
        """Remove folders created this run that ended up empty (rsync --prune-empty-dirs)."""
        for side, row in reversed(plan.mkdirs):
            root = plan.right if side == "right" else plan.left
            src_root = plan.left if side == "right" else plan.right
            path = os.path.join(root, row.rel)
            try:
                if not os.listdir(path) and os.path.isdir(os.path.join(src_root, row.rel)) \
                        and os.listdir(os.path.join(src_root, row.rel)):
                    os.rmdir(path)      # source not empty but nothing copied (all filtered)
            except OSError:
                pass

    def _update_baseline(self, res: CompareResult, plans: list[PairPlan]) -> None:
        self.phase("Saving two-way sync state")
        for plan, info in zip(plans, res.pairs):
            base = db.load_baseline(self.job.id, info.id)
            for row in plan.rows:
                if (plan.index, row.rel) in self._failed:
                    continue
                lp, rp = os.path.join(plan.left, row.rel), os.path.join(plan.right, row.rel)
                try:
                    ls, rs = os.lstat(lp), os.lstat(rp)
                except OSError:
                    base.pop(row.rel, None)
                    continue
                if row.kind == "d":
                    base[row.rel] = (0, 0.0)
                elif ls.st_size == rs.st_size and abs(ls.st_mtime - rs.st_mtime) <= max(2.0, self.job.compare.time_tolerance):
                    base[row.rel] = (ls.st_size, ls.st_mtime)
            db.replace_baseline(self.job.id, info.id, base)

    # ---------------------------------------------------------- reporting ---
    def _compare_progress(self, p: dict) -> None:
        self.progress["current"] = p.get("path", "") or ""
        if "items" in p:
            self.progress["current"] = f"{p.get('phase', '')}: {p['items']:,} items"
        self.progress["phase"] = p.get("phase", "Comparing")
        self._emit()
        if self.cancel.is_set():
            raise Cancelled(self.abort_reason or "Cancelled")

    def _plan_totals(self, plans: list[PairPlan]) -> None:
        total = sum((r.l.size for p in plans for r in p.copies_lr), 0) + \
            sum((r.r.size for p in plans for r in p.copies_rl), 0)
        items = sum(len(p.copies_lr) + len(p.copies_rl) + len(p.del_files) + len(p.del_dirs) + len(p.moves)
                    for p in plans)
        self.stats["bytes_planned"] = total
        self.progress.update(bytes_total=total, items_total=items, bytes_done=0, items_done=0)

    def _log_plan(self, plans: list[PairPlan]) -> None:
        for p in plans:
            self.log(f"Plan pair {p.index + 1}: copy →{len(p.copies_lr)} ←{len(p.copies_rl)}, "
                     f"folders {len(p.mkdirs)}, moves {len(p.moves)}, delete {len(p.del_files)} file(s) "
                     f"+ {len(p.del_dirs)} folder(s), total {fsutil.human(self.stats['bytes_planned'])}")

    def _dry_changes(self, plans: list[PairPlan]) -> None:
        self.stats["would_copy"] = sum(len(p.copies_lr) + len(p.copies_rl) for p in plans)
        self.stats["would_copy_bytes"] = self.stats["bytes_planned"]
        self.stats["would_delete"] = sum(len(p.del_files) for p in plans)
        self.stats["would_move"] = sum(len(p.moves) for p in plans)
        for p in plans:
            for r in p.copies_lr:
                self.changes["updated" if r.r else "created"].append(f"right/{r.rel}")
            for r in p.copies_rl:
                self.changes["updated" if r.l else "created"].append(f"left/{r.rel}")
            for side, r in p.mkdirs:
                self.changes["created"].append(f"{side}/{r.rel}/")
            for side, r in p.del_files + p.del_dirs:
                self.changes["deleted"].append(f"{side}/{r.rel}")
            for r in p.moves:
                self.changes["moved"].append(f"{'right' if r.action == 'move_right' else 'left'}/{r.move_from}  ⇒  {r.rel}")

    def _write_trees(self) -> None:
        titles = {"created": "CREATED", "updated": "UPDATED", "moved": "MOVED", "deleted": "DELETED"}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            for key, title in titles.items():
                items = self.changes[key]
                fh.write("\n" + "=" * 70 + f"\n {title}{' (planned)' if self.dry_run else ''}  ({len(items)} item(s))\n" + "=" * 70 + "\n")
                if not items:
                    fh.write("  (none)\n")
                elif key == "moved":
                    for m in sorted(items)[:20000]:
                        fh.write(f"  {m}\n")
                else:
                    for line in render_tree(items[:50000]):
                        fh.write(line + "\n")
                    if len(items) > 50000:
                        fh.write(f"  … and {len(items) - 50000} more\n")

    def _hook_env(self, status: str) -> dict:
        return {
            "PROTOSYNC_JOB": self.job.name, "PROTOSYNC_JOB_ID": self.job.id, "PROTOSYNC_RUN_ID": self.run_id,
            "PROTOSYNC_STATUS": status, "PROTOSYNC_DRY_RUN": int(self.dry_run), "PROTOSYNC_LOG": self.log_path,
            "PROTOSYNC_FILES_COPIED": self.stats["files_copied"], "PROTOSYNC_BYTES_COPIED": self.stats["bytes_copied"],
            "PROTOSYNC_FILES_DELETED": self.stats["files_deleted"], "PROTOSYNC_ERRORS": self.stats["errors"],
            "PROTOSYNC_CHANGED": self.stats["files_copied"] + self.stats["files_deleted"] + self.stats["moved"],
            "PROTOSYNC_PAIRS": ";".join(f"{l}|{r}" for l, r in self._roots()),
        }

    def _finish(self, t0, status, error, res, plans, watch_stop) -> dict:
        watch_stop.set()
        self.stats["duration"] = round(time.time() - t0, 1)
        self.progress.update(phase="Finished", current="", eta=None,
                             percent=100.0 if status == "success" else self.progress["percent"])
        try:
            self._write_trees()
        except OSError:
            pass
        self.log(f"=== {status.upper()} in {self.stats['duration']}s: copied {self.stats['files_copied']} "
                 f"({fsutil.human(self.stats['bytes_copied'])}), deleted {self.stats['files_deleted']}, "
                 f"moved {self.stats['moved']}, errors {self.stats['errors']} ===")
        if self.job.notify.post_command and not self.dry_run:
            want = self.job.notify.post_command_when
            ok = status in ("success", "warning")
            if want == "always" or (want == "success" and ok) or (want == "failure" and not ok):
                from .notify import run_hook
                run_hook(self.job.notify.post_command, self._hook_env(status),
                         self.job.notify.command_timeout, self.log)
        self._emit(force=True)
        try:
            self._log_fh.close()
        except OSError:
            pass
        return {"status": status, "error": error, "stats": self.stats, "log_path": self.log_path}
