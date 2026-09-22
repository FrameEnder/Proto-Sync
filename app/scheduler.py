"""All automatic triggers.

  cron / interval / daily / once / startup  → APScheduler
  realtime                                  → watchdog observer + idle debounce (like RealTimeSync)
  mount                                     → poller that fires when a drive (sentinel) appears
  after_job                                 → chained off another job's result
  webhook                                   → handled in main.py (/api/hooks/{job}?token=…)
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import random
import threading
import time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import config, db, events
from .models import Job, Trigger
from .runner import runner

log = logging.getLogger("protosync.scheduler")


def _in_window(job: Job, now: Optional[dt.datetime] = None) -> bool:
    s, e = job.schedule.window_start, job.schedule.window_end
    if not s or not e:
        return True
    now = now or dt.datetime.now()
    cur = now.strftime("%H:%M")
    return (s <= cur < e) if s <= e else (cur >= s or cur < e)


def fire(job_id: str, trigger_id: str, label: str) -> None:
    job = db.get_job(job_id)
    if not job or not job.schedule.enabled:
        return
    trig = next((t for t in job.schedule.triggers if t.id == trigger_id), None)
    if trig is None or not trig.enabled:
        return
    if not _in_window(job):
        events.publish("toast", {"level": "info", "text": f"{job.name}: {label} skipped (outside allowed hours)"})
        db.kv_set(f"skip:{job.id}", {"when": time.time(), "why": f"{label} outside allowed hours"})
        return
    try:
        runner.start_run(job, label, dry_run=(trig.mode == "dry_run"))
    except RuntimeError as e:
        log.info("trigger %s skipped: %s", label, e)


class _Debounce:
    def __init__(self, job_id: str, trig: Trigger):
        self.job_id, self.trig = job_id, trig
        self.timer: Optional[threading.Timer] = None
        self.lock = threading.Lock()

    def poke(self, path: str) -> None:
        if f"/{config.INTERNAL_PREFIX}" in path or runner.job_busy(self.job_id):
            return
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(max(2, self.trig.idle_seconds), fire,
                                         args=(self.job_id, self.trig.id, "realtime"))
            self.timer.daemon = True
            self.timer.start()

    def stop(self) -> None:
        with self.lock:
            if self.timer:
                self.timer.cancel()


class Scheduler:
    def __init__(self) -> None:
        self.sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1,
                                                       "misfire_grace_time": 3600})
        self.observers: dict[str, tuple] = {}     # trigger id -> (observer, debounce)
        self.mount_state: dict[str, bool] = {}
        self._stop = threading.Event()
        self._first = True
        self.lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle -
    def start(self) -> None:
        self.sched.start()
        self.sched.add_job(self.maintenance, CronTrigger(hour=4, minute=17), id="sys:maintenance",
                           replace_existing=True)
        runner.finished_hooks.append(self.on_run_finished)
        threading.Thread(target=self._mount_loop, name="mount-poller", daemon=True).start()
        self.reload()

    def shutdown(self) -> None:
        self._stop.set()
        for obs, deb in self.observers.values():
            deb.stop()
            try:
                obs.stop()
            except Exception:  # noqa: BLE001
                pass
        self.sched.shutdown(wait=False)

    # --------------------------------------------------------------- reload -
    def reload(self) -> None:
        with self.lock:
            for j in self.sched.get_jobs():
                if j.id.startswith("trig:"):
                    j.remove()
            wanted_watch: dict[str, tuple[Job, Trigger]] = {}
            for job in db.list_jobs():
                if not job.schedule.enabled:
                    continue
                for t in job.schedule.triggers:
                    if not t.enabled:
                        continue
                    try:
                        self._add(job, t, wanted_watch)
                    except Exception as e:  # noqa: BLE001
                        events.publish("toast", {"level": "error", "text": f"{job.name}: bad {t.type} trigger — {e}"})
            # (Re)build realtime watchers.
            for tid in list(self.observers):
                obs, deb = self.observers.pop(tid)
                deb.stop()
                try:
                    obs.stop()
                except Exception:  # noqa: BLE001
                    pass
            for tid, (job, t) in wanted_watch.items():
                self._watch(job, t)
            self._first = False
        events.publish("schedule_changed", {})

    def _add(self, job: Job, t: Trigger, wanted_watch: dict) -> None:
        jid = f"trig:{job.id}:{t.id}"
        jitter = t.jitter_seconds or None
        args = (job.id, t.id, t.type)
        if t.type == "cron":
            self.sched.add_job(fire, CronTrigger.from_crontab(t.cron, timezone=self.sched.timezone),
                               args=args, id=jid, jitter=jitter)
        elif t.type == "interval":
            self.sched.add_job(fire, IntervalTrigger(minutes=max(1, t.every_minutes)), args=args, id=jid,
                               jitter=jitter)
        elif t.type == "daily":
            hh, mm = (t.time or "03:00").split(":")
            days = ",".join(str(d) for d in sorted(set(t.days))) or "0-6"
            self.sched.add_job(fire, CronTrigger(day_of_week=days, hour=int(hh), minute=int(mm)),
                               args=args, id=jid, jitter=jitter)
        elif t.type == "once" and t.at:
            when = dt.datetime.fromisoformat(t.at)
            if when > dt.datetime.now():
                self.sched.add_job(fire, DateTrigger(run_date=when), args=args, id=jid)
        elif t.type == "startup" and self._first:
            self.sched.add_job(fire, DateTrigger(run_date=dt.datetime.now() + dt.timedelta(seconds=60)),
                               args=args, id=jid)
        elif t.type == "realtime":
            wanted_watch[t.id] = (job, t)

    def _watch(self, job: Job, t: Trigger) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver

        deb = _Debounce(job.id, t)

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: N802
                if event.event_type in ("opened", "closed_no_write"):
                    return
                deb.poke(getattr(event, "src_path", "") or "")

        obs = PollingObserver(timeout=30) if t.poll else Observer()
        paths = []
        for p in job.pairs:
            if not p.enabled:
                continue
            paths.append(p.left)
            if job.sync.variant == "two_way":
                paths.append(p.right)
        handler = Handler()
        for path in paths:
            if os.path.isdir(path):
                try:
                    obs.schedule(handler, path, recursive=True)
                except OSError as e:
                    events.publish("toast", {"level": "error",
                                             "text": f"{job.name}: cannot watch {path} ({e}). "
                                                     "Raise fs.inotify.max_user_watches or enable polling."})
        try:
            obs.start()
            self.observers[t.id] = (obs, deb)
        except OSError as e:
            events.publish("toast", {"level": "error", "text": f"{job.name}: realtime watch failed — {e}"})

    # ---------------------------------------------------------------- mount -
    @staticmethod
    def _available(job: Job) -> bool:
        s = job.safety
        for p in job.pairs:
            if not p.enabled:
                continue
            for root in (p.left, p.right):
                if not root or not os.path.isdir(root):
                    return False
                if s.require_sentinel and s.sentinel_file and not os.path.exists(os.path.join(root, s.sentinel_file)):
                    return False
        return True

    def _mount_loop(self) -> None:
        while not self._stop.wait(20):
            try:
                for job in db.list_jobs():
                    trigs = [t for t in job.schedule.triggers if t.type == "mount" and t.enabled]
                    if not trigs or not job.schedule.enabled:
                        continue
                    now = self._available(job)
                    before = self.mount_state.get(job.id)
                    self.mount_state[job.id] = now
                    if before is False and now:
                        events.publish("toast", {"level": "info", "text": f"{job.name}: drive connected"})
                        for t in trigs:                              # let the drive settle first
                            tm = threading.Timer(min(600, max(5, t.idle_seconds)), fire,
                                                 args=(job.id, t.id, "drive connected"))
                            tm.daemon = True
                            tm.start()
            except Exception as e:  # noqa: BLE001
                log.warning("mount poller: %s", e)

    # ------------------------------------------------------------- chaining -
    def on_run_finished(self, job: Job, run: dict) -> None:
        ok = run.get("status") in ("success", "warning")
        for other in db.list_jobs():
            if other.id == job.id or not other.schedule.enabled:
                continue
            for t in other.schedule.triggers:
                if t.enabled and t.type == "after_job" and t.after_job_id == job.id:
                    if t.after_on == "any" or (t.after_on == "success") == ok:
                        delay = t.jitter_seconds and random.randint(0, t.jitter_seconds) or 0
                        threading.Timer(2 + delay, fire, args=(other.id, t.id, f"after {job.name}")).start()

    # ---------------------------------------------------------------- info --
    def next_runs(self) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for j in self.sched.get_jobs():
            if not j.id.startswith("trig:") or not j.next_run_time:
                continue
            job_id = j.id.split(":")[1]
            ts = j.next_run_time.timestamp()
            if out.get(job_id) is None or ts < out[job_id]:
                out[job_id] = ts
        return out

    def trigger_info(self, job: Job) -> list[dict]:
        info = []
        for t in job.schedule.triggers:
            j = self.sched.get_job(f"trig:{job.id}:{t.id}")
            info.append({"id": t.id, "next": j.next_run_time.timestamp() if j and j.next_run_time else None,
                         "watching": t.id in self.observers})
        return info

    def maintenance(self) -> None:
        days = int(db.kv_get("log_retention_days", 30))
        for path in db.prune_runs(days):
            try:
                os.remove(path)
            except OSError:
                pass
        cutoff = time.time() - days * 86400
        for f in config.LOG_DIR.glob("run-*.log"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


scheduler = Scheduler()
