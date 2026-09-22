"""Comparison engine.

Scans every enabled folder pair, categorises each item (left only, newer, …),
assigns a sync action from the job's variant and optionally detects moved files.
The result is the reviewable *plan* shown in the grid and executed by the syncer.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import db, fsutil
from .models import VARIANT_TABLES, Job
from .scanner import Entry, Matcher, ScanResult, scan

ProgressCB = Callable[[dict], None]

DELETE_ACTIONS = {"delete_left", "delete_right"}


@dataclass(slots=True)
class Row:
    id: int
    pair: int                      # index into CompareResult.pairs
    rel: str
    kind: str                      # f / d / l
    l: Optional[Entry]
    r: Optional[Entry]
    category: str
    action: str
    default: str
    note: str = ""
    locked: bool = False           # cannot be changed (unreadable folder, etc.)
    partner: int = -1              # move pairing
    move_from: str = ""
    depth: int = 0


@dataclass
class PairInfo:
    id: str
    left: str
    right: str
    left_fs: str = ""
    right_fs: str = ""
    left_scan: Optional[ScanResult] = None
    right_scan: Optional[ScanResult] = None
    has_baseline: bool = False


@dataclass
class CompareResult:
    id: str
    job: Job
    pairs: list[PairInfo] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    duration: float = 0.0
    cancelled: bool = False


# ------------------------------------------------------------- helpers -------
def _times_equal(a: float, b: float, tol: float, shifts: list[int]) -> bool:
    d = abs(a - b)
    if d <= tol:
        return True
    return any(abs(d - h * 3600) <= tol for h in shifts if h)


def _changed(e: Entry, base: Optional[tuple[int, float]], tol: float, shifts: list[int]) -> bool:
    if base is None:
        return True
    if e.kind == "d":
        return False
    return e.size != base[0] or not _times_equal(e.mtime, base[1], tol, shifts)


def _under(rel: str, dirs: set[str]) -> bool:
    if not dirs:
        return False
    if "" in dirs:
        return True
    parts = rel.split("/")
    for i in range(1, len(parts)):
        if "/".join(parts[:i]) in dirs:
            return True
    return False


def valid_actions(row: Row) -> list[str]:
    acts = ["none"]
    if row.l is not None:
        acts += ["copy_lr", "delete_left"]
    if row.r is not None:
        acts += ["copy_rl", "delete_right"]
    if row.move_from:
        acts.append(row.default if row.default.startswith("move_") else "move_right")
    return acts


# ---------------------------------------------------------- categorise -------
def _categorise(l: Optional[Entry], r: Optional[Entry], variant: str, tol: float, shifts: list[int]) -> str:
    if l is None:
        return "right_only"
    if r is None:
        return "left_only"
    if l.kind != r.kind:
        return "conflict"
    if l.kind == "d":
        return "equal"
    if variant == "size":
        return "equal" if l.size == r.size else "different"
    if variant == "content":
        return "equal" if l.size == r.size else "different"   # same size → byte check later
    if _times_equal(l.mtime, r.mtime, tol, shifts):
        return "equal" if l.size == r.size else "different"
    return "left_newer" if l.mtime > r.mtime else "right_newer"


def _two_way_action(row: Row, base: dict, has_base: bool, conflict: str, tol: float, shifts: list[int]) -> tuple[str, str]:
    """Returns (action, note) for a two-way sync using the last-sync baseline."""
    l, r, cat = row.l, row.r, row.category
    if cat == "equal":
        return "none", ""
    b = base.get(row.rel)

    def resolve(reason: str) -> tuple[str, str]:
        if conflict == "left" and l is not None:
            return "copy_lr", reason + " — left wins"
        if conflict == "right" and r is not None:
            return "copy_rl", reason + " — right wins"
        if conflict == "newer":
            lm = l.mtime if l else -1
            rm = r.mtime if r else -1
            if lm >= rm and l is not None:
                return "copy_lr", reason + " — newer wins"
            if r is not None:
                return "copy_rl", reason + " — newer wins"
        return "none", reason

    if not has_base:   # first run: nothing is ever deleted
        if cat == "left_only":
            return "copy_lr", ""
        if cat == "right_only":
            return "copy_rl", ""
        if cat == "left_newer":
            return "copy_lr", ""
        if cat == "right_newer":
            return "copy_rl", ""
        return resolve("Differs, no previous sync to decide direction")

    if cat == "left_only":
        if b is None:
            return "copy_lr", "New on left"
        if _changed(l, b, tol, shifts):
            return resolve("Deleted on right but changed on left")
        return "delete_left", "Deleted on right since last sync"
    if cat == "right_only":
        if b is None:
            return "copy_rl", "New on right"
        if _changed(r, b, tol, shifts):
            return resolve("Deleted on left but changed on right")
        return "delete_right", "Deleted on left since last sync"
    if l is None or r is None or l.kind != r.kind:
        return resolve("Item type differs")
    lc = _changed(l, b, tol, shifts)
    rc = _changed(r, b, tol, shifts)
    if lc and not rc:
        return "copy_lr", "Changed on left"
    if rc and not lc:
        return "copy_rl", "Changed on right"
    return resolve("Changed on both sides")


# -------------------------------------------------------------- compare -------
def run_compare(
    job: Job,
    cancel: Optional[threading.Event] = None,
    progress: Optional[ProgressCB] = None,
) -> CompareResult:
    cancel = cancel or threading.Event()
    t0 = time.time()
    res = CompareResult(id=uuid.uuid4().hex[:12], job=job)
    cmp = job.compare
    shifts = [int(h) for h in cmp.ignore_time_shift if int(h)]
    tol = max(0.0, float(cmp.time_tolerance))

    def emit(**kw):
        if progress:
            progress(kw)

    pairs = [p for p in job.pairs if p.enabled and p.left and p.right]
    if not pairs:
        res.errors.append("No enabled folder pairs with both sides set.")
        return res

    next_id = 0
    for pi, pair in enumerate(pairs):
        info = PairInfo(id=pair.id, left=os.path.realpath(pair.left), right=os.path.realpath(pair.right))
        info.left_fs = fsutil.fstype(info.left)
        info.right_fs = fsutil.fstype(info.right)
        res.pairs.append(info)

        extra: list[str] = []
        vp = job.sync.versioning_path
        for root in (info.left, info.right):
            if vp and os.path.realpath(vp).startswith(root.rstrip("/") + "/"):
                extra.append("/" + os.path.relpath(os.path.realpath(vp), root) + "/")
        matcher = Matcher(job.filter, extra)

        for side in ("left", "right"):
            root = getattr(info, side)
            if not os.path.isdir(root):
                res.errors.append(f"{side.title()} folder does not exist: {root}")
                sr = ScanResult(root=root)
                sr.unreadable.add("")
            else:
                emit(phase=f"Scanning {side}", pair=pi + 1, pairs=len(pairs), path=root, items=0)
                sr = scan(root, matcher, cmp, cancel,
                          lambda n, s=side, r=root: emit(phase=f"Scanning {s}", pair=pi + 1,
                                                         pairs=len(pairs), path=r, items=n))
                res.errors.extend(sr.errors[:200])
            setattr(info, f"{side}_scan", sr)
            if cancel.is_set():
                res.cancelled = True
                return res

        L = info.left_scan.entries
        R = info.right_scan.entries
        unread = info.left_scan.unreadable | info.right_scan.unreadable
        variant = job.sync.variant
        base: dict = {}
        if variant == "two_way":
            base = db.load_baseline(job.id, pair.id)
            info.has_baseline = bool(base)
        table = VARIANT_TABLES.get(variant) or job.sync.custom

        keys = sorted(set(L) | set(R), key=lambda k: tuple((p.lower(), p) for p in k.split("/")))
        emit(phase="Comparing", pair=pi + 1, pairs=len(pairs), items=len(keys))
        pair_rows: list[Row] = []
        content_checks: list[Row] = []
        for rel in keys:
            l = L.get(rel)
            r = R.get(rel)
            kind = (l or r).kind
            cat = _categorise(l, r, cmp.variant, tol, shifts)
            row = Row(id=next_id, pair=pi, rel=rel, kind=kind, l=l, r=r, category=cat,
                      action="none", default="none", depth=rel.count("/"))
            next_id += 1
            if l and r and l.kind != r.kind:
                row.note = "A file and a folder share this name"
            if _under(rel, unread) or (kind == "d" and rel in unread):
                row.locked = True
                row.note = "Folder could not be read on one side — left untouched for safety"
            if cmp.variant == "content" and cat == "equal" and kind == "f" and l and r:
                content_checks.append(row)
            pair_rows.append(row)

        # Byte-by-byte comparison for same-size files (content mode).
        if content_checks:
            total = sum(r.l.size for r in content_checks) or 1
            done = 0
            last = 0.0
            for row in content_checks:
                if cancel.is_set():
                    res.cancelled = True
                    return res
                same = fsutil.files_identical(os.path.join(info.left, row.rel),
                                              os.path.join(info.right, row.rel), cancel)
                if not same:
                    row.category = "different"
                done += row.l.size
                now = time.time()
                if now - last > 0.3:
                    last = now
                    emit(phase="Comparing content", pair=pi + 1, pairs=len(pairs),
                         percent=round(done * 100 / total, 1), bytes=done, total=total)

        # Assign actions.
        for row in pair_rows:
            if row.locked:
                continue
            if variant == "two_way":
                act, note = _two_way_action(row, base, info.has_baseline, job.sync.conflict, tol, shifts)
                if note and not row.note:
                    row.note = note
            else:
                act = table.get(row.category, "none")
            if act not in valid_actions(row):
                act = "none"
            row.action = row.default = act

        _fix_directory_actions(pair_rows)  # indexes are local to this pair
        if cmp.detect_moves:
            emit(phase="Detecting moved files", pair=pi + 1, pairs=len(pairs))
            _detect_moves(pair_rows, info, tol)
        res.rows.extend(pair_rows)

    res.duration = time.time() - t0
    emit(phase="Done", items=len(res.rows))
    return res


def _descendants(rows: list[Row], idx: int):
    """Rows are path-sorted, so a folder's contents directly follow it."""
    row = rows[idx]
    prefix = row.rel + "/"
    j = idx + 1
    while j < len(rows) and rows[j].pair == row.pair and rows[j].rel.startswith(prefix):
        yield rows[j]
        j += 1


def _fix_directory_actions(rows: list[Row]) -> None:
    """A folder may only be deleted if everything inside it is deleted too."""
    # Walk deepest-first so a kept child folder also keeps its parents.
    for idx in range(len(rows) - 1, -1, -1):
        row = rows[idx]
        if row.kind != "d" or row.action not in DELETE_ACTIONS:
            continue
        for other in _descendants(rows, idx):
            side_has = other.r if row.action == "delete_right" else other.l
            if side_has is not None and other.action != row.action:
                row.action = row.default = "none"
                row.note = "Kept: contains items that are not being deleted"
                break


def _detect_moves(rows: list[Row], info: PairInfo, tol: float) -> None:
    """Turn a (copy X, delete Y) pair into a rename when Y is provably the same file."""
    for copy_act, del_act, move_act, src_side, dst_side in (
        ("copy_lr", "delete_right", "move_right", "left", "right"),
        ("copy_rl", "delete_left", "move_left", "right", "left"),
    ):
        copies: dict[tuple, list[Row]] = {}
        deletes: dict[tuple, list[Row]] = {}
        for row in rows:
            if row.kind != "f" or row.locked:
                continue
            if row.action == copy_act and (row.r if src_side == "left" else row.l) is None:
                e = row.l if src_side == "left" else row.r
                if e.size >= 64 * 1024:                       # tiny files: just copy
                    copies.setdefault((e.size,), []).append(row)
            elif row.action == del_act:
                e = row.r if dst_side == "right" else row.l
                deletes.setdefault((e.size,), []).append(row)
        for key, cands in copies.items():
            dels = deletes.get(key)
            if not dels or len(cands) != 1 or len(dels) != 1:
                continue
            c, d = cands[0], dels[0]
            ce = c.l if src_side == "left" else c.r
            de = d.r if dst_side == "right" else d.l
            if abs(ce.mtime - de.mtime) > tol:
                continue
            src_root = info.left if src_side == "left" else info.right
            dst_root = info.right if dst_side == "right" else info.left
            h1 = fsutil.sample_hash(os.path.join(src_root, c.rel))
            h2 = fsutil.sample_hash(os.path.join(dst_root, d.rel))
            if not h1 or h1 != h2:
                continue
            c.action = c.default = move_act
            c.move_from = d.rel
            c.partner = d.id
            c.note = f"Moved from {d.rel}"
            d.action = d.default = "none"
            d.partner = c.id
            d.note = f"Moved to {c.rel}"


# ------------------------------------------------------------- overrides -----
def set_action(res: CompareResult, ids: list[int], action: str) -> int:
    """Apply a manual action to rows (folders cascade to their contents)."""
    changed = 0
    targets: list[Row] = []
    for i in ids:
        if 0 <= i < len(res.rows):
            targets.append(res.rows[i])
            if res.rows[i].kind == "d":
                targets.extend(_descendants(res.rows, i))
    seen = set()
    for row in targets:
        if row.id in seen or row.locked:
            continue
        seen.add(row.id)
        act = row.default if action == "default" else action
        if act not in valid_actions(row) and not (act.startswith("move_") and row.move_from):
            continue
        if row.move_from and not act.startswith("move_"):
            # Breaking a move: the source row goes back to being a delete.
            partner = res.rows[row.partner] if 0 <= row.partner < len(res.rows) else None
            if partner and partner.action == "none":
                partner.action = "delete_right" if row.default == "move_right" else "delete_left"
                partner.note = ""
            if act == row.default:
                act = "copy_lr" if row.default == "move_right" else "copy_rl"
        if row.action != act:
            row.action = act
            changed += 1
    return changed


# -------------------------------------------------------------- summary -------
def summary(res: CompareResult) -> dict:
    cats: dict[str, int] = {}
    acts: dict[str, int] = {}
    bytes_lr = bytes_rl = 0
    for r in res.rows:
        cats[r.category] = cats.get(r.category, 0) + 1
        acts[r.action] = acts.get(r.action, 0) + 1
        if r.action == "copy_lr" and r.l and r.kind == "f":
            bytes_lr += r.l.size
        elif r.action == "copy_rl" and r.r and r.kind == "f":
            bytes_rl += r.r.size
    pairs = []
    for p in res.pairs:
        pairs.append({
            "id": p.id, "left": p.left, "right": p.right, "left_fs": p.left_fs, "right_fs": p.right_fs,
            "left_files": p.left_scan.files if p.left_scan else 0,
            "right_files": p.right_scan.files if p.right_scan else 0,
            "left_bytes": p.left_scan.bytes if p.left_scan else 0,
            "right_bytes": p.right_scan.bytes if p.right_scan else 0,
            "left_free": (fsutil.disk_usage(p.left) or {}).get("free"),
            "right_free": (fsutil.disk_usage(p.right) or {}).get("free"),
            "has_baseline": p.has_baseline,
        })
    return {
        "id": res.id, "job_id": res.job.id, "created": res.created, "duration": round(res.duration, 2),
        "total": len(res.rows), "categories": cats, "actions": acts,
        "bytes_lr": bytes_lr, "bytes_rl": bytes_rl, "errors": res.errors[:100],
        "error_count": len(res.errors), "pairs": pairs, "cancelled": res.cancelled,
        "variant": res.job.sync.variant,
    }


def row_to_dict(res: CompareResult, r: Row) -> dict:
    return {
        "id": r.id, "pair": r.pair, "rel": r.rel, "kind": r.kind, "depth": r.depth,
        "l": [r.l.size, r.l.mtime] if r.l else None,
        "r": [r.r.size, r.r.mtime] if r.r else None,
        "category": r.category, "action": r.action, "default": r.default,
        "note": r.note, "locked": r.locked, "move_from": r.move_from,
        "valid": valid_actions(r),
    }


def filter_rows(res: CompareResult, categories: Optional[set[str]], actions: Optional[set[str]],
                search: str, show_equal: bool) -> list[int]:
    s = search.lower().strip()
    out = []
    for r in res.rows:
        if not show_equal and r.category == "equal" and r.action == "none":
            continue
        if categories and r.category not in categories:
            continue
        if actions and r.action not in actions:
            continue
        if s and s not in r.rel.lower():
            continue
        out.append(r.id)
    return out
