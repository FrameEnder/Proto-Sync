"""Tree scanner with FreeFileSync-style include/exclude, size and age filters."""
from __future__ import annotations

import datetime as dt
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Callable, Optional

from . import config
from .models import CompareSettings, FilterSettings


@dataclass(slots=True)
class Entry:
    size: int
    mtime: float
    kind: str        # 'f' file, 'd' dir, 'l' symlink (direct mode)


@dataclass
class ScanResult:
    root: str
    entries: dict[str, Entry] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    unreadable: set[str] = field(default_factory=set)   # rel dirs we could not list
    files: int = 0
    bytes: int = 0


class _Pattern:
    __slots__ = ("pat", "dir_only", "anchored", "has_slash")

    def __init__(self, raw: str):
        p = raw.strip().replace("\\", "/")
        self.dir_only = p.endswith("/")
        p = p.rstrip("/")
        self.anchored = p.startswith("/")
        p = p.lstrip("/")
        self.pat = p
        self.has_slash = "/" in p

    def match(self, rel: str, name: str, is_dir: bool) -> bool:
        if not self.pat:
            return False
        if self.dir_only and not is_dir:
            return False
        if self.anchored or self.has_slash:
            return fnmatchcase(rel, self.pat)
        return fnmatchcase(name, self.pat)


class Matcher:
    def __init__(self, flt: FilterSettings, extra_excludes: list[str] | None = None):
        inc = [p for p in flt.include if p.strip()]
        self.include_all = (not inc) or any(p.strip() in ("*", "/*", "**") for p in inc)
        self.include = [_Pattern(p) for p in inc]
        self.exclude = [_Pattern(p) for p in list(flt.exclude) + list(extra_excludes or []) if p.strip()]
        self.min_size = flt.min_size
        self.max_size = flt.max_size
        cutoff = None
        if flt.max_age_days:
            cutoff = time.time() - float(flt.max_age_days) * 86400
        if flt.newer_than:
            try:
                t = dt.datetime.fromisoformat(flt.newer_than).timestamp()
                cutoff = max(cutoff or 0, t)
            except ValueError:
                pass
        self.min_mtime = cutoff

    def excluded(self, rel: str, name: str, is_dir: bool) -> bool:
        if name.startswith(config.INTERNAL_PREFIX):
            return True
        return any(p.match(rel, name, is_dir) for p in self.exclude)

    def included(self, rel: str, name: str, is_dir: bool, parent_included: bool) -> bool:
        if self.include_all or parent_included:
            return True
        return any(p.match(rel, name, is_dir) for p in self.include)

    def file_passes(self, size: int, mtime: float) -> bool:
        if self.min_size is not None and size < self.min_size:
            return False
        if self.max_size is not None and size > self.max_size:
            return False
        if self.min_mtime is not None and mtime < self.min_mtime:
            return False
        return True


def scan(
    root: str,
    matcher: Matcher,
    cmp: CompareSettings,
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[int], None]] = None,
) -> ScanResult:
    res = ScanResult(root=root)
    follow = cmp.symlinks == "follow"
    seen_dirs: set[tuple[int, int]] = set()
    try:
        st = os.stat(root)
        seen_dirs.add((st.st_dev, st.st_ino))
    except OSError as e:
        res.errors.append(f"{root}: {e.strerror}")
        res.unreadable.add("")
        return res

    # stack of (abs_dir, rel_dir, parent_included)
    stack: list[tuple[str, str, bool]] = [(root, "", False)]
    counter = 0
    while stack:
        if cancel is not None and cancel.is_set():
            break
        adir, rdir, parent_inc = stack.pop()
        try:
            it = os.scandir(adir)
        except OSError as e:
            res.errors.append(f"{adir}: {e.strerror or e}")
            res.unreadable.add(rdir)
            continue
        with it:
            for e in it:
                name = e.name
                rel = f"{rdir}/{name}" if rdir else name
                try:
                    is_link = e.is_symlink()
                    if is_link and cmp.symlinks == "exclude":
                        continue
                    if is_link and not follow:
                        lst = e.stat(follow_symlinks=False)
                        if matcher.excluded(rel, name, False):
                            continue
                        if not matcher.included(rel, name, False, parent_inc):
                            continue
                        target = os.readlink(e.path)
                        res.entries[rel] = Entry(len(target.encode()), lst.st_mtime, "l")
                        continue
                    st = e.stat(follow_symlinks=True)
                except OSError as ex:
                    res.errors.append(f"{e.path}: {ex.strerror or ex}")
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if matcher.excluded(rel, name, is_dir):
                    continue
                inc = matcher.included(rel, name, is_dir, parent_inc)
                if is_dir:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_dirs:
                        continue            # symlink loop guard
                    seen_dirs.add(key)
                    if inc:
                        res.entries[rel] = Entry(0, st.st_mtime, "d")
                    stack.append((e.path, rel, inc and not matcher.include_all))
                elif stat.S_ISREG(st.st_mode):
                    if not inc or not matcher.file_passes(st.st_size, st.st_mtime):
                        continue
                    res.entries[rel] = Entry(st.st_size, st.st_mtime, "f")
                    res.files += 1
                    res.bytes += st.st_size
                # sockets/fifos/devices are ignored
                counter += 1
                if progress is not None and counter % 2000 == 0:
                    progress(counter)
    if progress is not None:
        progress(counter)
    return res
