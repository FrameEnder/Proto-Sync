"""Owns everything that is running: compare sessions, sync runs, the queue and locks."""
from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config, db, events, notify
from .compare import CompareResult, run_compare, summary
from .models import Job
from .syncer import Syncer


@dataclass
class ActiveRun:
    run_id: int
    job: Job
    trigger: str
    dry_run: bool
    cancel: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)
    syncer: Optional[Syncer] = None
    status: str = "queued"
    progress: dict = field(default_factory=dict)


@dataclass
class ActiveCompare:
    job_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: dict = field(default_factory=dict)
    started: float = field(default_factory=time.time)


class Runner:
    MAX_SESSIONS = 6

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: "OrderedDict[str, CompareResult]" = OrderedDict()
        self.compares: dict[str, ActiveCompare] = {}
        self.active: dict[int, ActiveRun] = {}
        self.pending: dict[str, tuple[str, bool]] = {}      # job_id -> (trigger, dry_run)
        self.sem = threading.Semaphore(config.MAX_CONCURRENT_RUNS)
        self.finished_hooks: list[Callable[[Job, dict], None]] = []

    # ------------------------------------------------------------ compare ---
    def start_compare(self, job: Job) -> None:
        with self.lock:
            if job.id in self.compares:
                raise RuntimeError("A comparison for this job is already running")
            ac = ActiveCompare(job.id)
            self.compares[job.id] = ac

        def work():
            def prog(p):
                ac.progress = p
                events.publish("compare_progress", {"job_id": job.id, **p},
                               throttle_key=f"cmp{job.id}", min_interval=0.25)
            try:
                res = run_compare(job, ac.cancel, prog)
                with self.lock:
                    self.sessions[res.id] = res
                    while len(self.sessions) > self.MAX_SESSIONS:
                        self.sessions.popitem(last=False)
                events.publish("compare_done", {"job_id": job.id, "summary": summary(res)})
            except Exception as e:  # noqa: BLE001
                events.publish("compare_done", {"job_id": job.id, "error": f"{type(e).__name__}: {e}"})
            finally:
                with self.lock:
                    self.compares.pop(job.id, None)

        threading.Thread(target=work, name=f"compare-{job.id}", daemon=True).start()

    def cancel_compare(self, job_id: str) -> bool:
        ac = self.compares.get(job_id)
        if ac:
            ac.cancel.set()
        return bool(ac)

    def session(self, sid: str) -> Optional[CompareResult]:
        return self.sessions.get(sid)

    def latest_session(self, job_id: str) -> Optional[CompareResult]:
        for res in reversed(self.sessions.values()):
            if res.job.id == job_id:
                return res
        return None

    def drop_sessions(self, job_id: str) -> None:
        with self.lock:
            for sid in [k for k, v in self.sessions.items() if v.job.id == job_id]:
                self.sessions.pop(sid, None)

    # ---------------------------------------------------------------- runs ---
    def job_busy(self, job_id: str) -> bool:
        return any(a.job.id == job_id for a in self.active.values())

    def start_run(self, job: Job, trigger: str, dry_run: bool = False,
                  session: Optional[CompareResult] = None, force: bool = False) -> Optional[int]:
        with self.lock:
            if self.job_busy(job.id):
                if job.schedule.queue_if_running and trigger != "manual":
                    self.pending[job.id] = (trigger, dry_run)
                    events.publish("toast", {"level": "info", "text": f"{job.name} is running — queued one more run"})
                    return None
                raise RuntimeError(f"{job.name} is already running")
            run_id = db.create_run(job, trigger, dry_run)
            ar = ActiveRun(run_id, job, trigger, dry_run)
            ar.resume.set()
            self.active[run_id] = ar
        events.publish("run_started", {"run_id": run_id, "job_id": job.id, "trigger": trigger, "dry_run": dry_run})
        threading.Thread(target=self._work, args=(ar, session, force), name=f"run-{run_id}", daemon=True).start()
        return run_id

    def _lock_paths(self, ar: ActiveRun) -> list:
        roots = sorted({os.path.realpath(p) for pair in ar.job.pairs if pair.enabled
                        for p in (pair.left, pair.right) if p})
        handles = []
        for root in roots:
            name = hashlib.sha1(root.encode()).hexdigest()[:16] + ".lock"
            fh = open(config.LOCK_DIR / name, "w")
            waited = False
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waited:
                        waited = True
                        ar.progress = {"run_id": ar.run_id, "job_id": ar.job.id, "job_name": ar.job.name,
                                       "phase": f"Waiting — another job is using {root}", "percent": 0}
                        events.publish("run_progress", ar.progress)
                    if ar.cancel.wait(2):
                        fh.close()
                        for h in handles:
                            h.close()
                        return []
            handles.append(fh)
        return handles

    def _work(self, ar: ActiveRun, session: Optional[CompareResult], force: bool) -> None:
        result: dict = {"status": "failed", "error": "", "stats": {}, "log_path": ""}
        handles: list = []
        acquired = False
        try:
            ar.progress = {"run_id": ar.run_id, "job_id": ar.job.id, "job_name": ar.job.name,
                           "phase": "Queued — waiting for a free slot", "percent": 0}
            events.publish("run_progress", ar.progress)
            while not self.sem.acquire(timeout=1):
                if ar.cancel.is_set():
                    break
            else:
                acquired = True
            if not acquired:
                result.update(status="cancelled", error="Cancelled while queued")
                return
            handles = self._lock_paths(ar)
            if ar.cancel.is_set():
                result.update(status="cancelled", error="Cancelled while waiting for a lock")
                return
            ar.status = "running"
            db.update_run(ar.run_id, status="running", started=time.time())
            syncer = Syncer(ar.run_id, ar.job, ar.dry_run, ar.trigger, session, force,
                            ar.cancel, ar.resume, on_progress=lambda p: setattr(ar, "progress", p))
            ar.syncer = syncer
            db.update_run(ar.run_id, log_path=syncer.log_path)
            result = syncer.execute()
        except Exception as e:  # noqa: BLE001
            result.update(status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            for h in handles:
                try:
                    h.close()
                except OSError:
                    pass
            if acquired:
                self.sem.release()
            db.update_run(ar.run_id, status=result["status"], finished=time.time(),
                          stats=result.get("stats", {}), error=result.get("error", ""),
                          **({"log_path": result["log_path"]} if result.get("log_path") else {}))
            with self.lock:
                self.active.pop(ar.run_id, None)
                pending = self.pending.pop(ar.job.id, None)
            run = db.get_run(ar.run_id) or {}
            events.publish("run_done", run)
            if result["status"] == "success" and not ar.dry_run:
                self.drop_sessions(ar.job.id)
            try:
                notify.dispatch(ar.job, run)
            except Exception:  # noqa: BLE001
                pass
            for hook in list(self.finished_hooks):
                try:
                    hook(ar.job, run)
                except Exception:  # noqa: BLE001
                    pass
            if pending:
                job = db.get_job(ar.job.id)
                if job:
                    time.sleep(2)
                    try:
                        self.start_run(job, pending[0] + " (queued)", pending[1])
                    except RuntimeError:
                        pass

    def cancel(self, run_id: int) -> bool:
        ar = self.active.get(run_id)
        if not ar:
            return False
        ar.cancel.set()
        ar.resume.set()
        if ar.syncer:
            ar.syncer.abort_reason = "Cancelled by user"
            ar.syncer._kill_proc()
        return True

    def pause(self, run_id: int, paused: bool) -> bool:
        ar = self.active.get(run_id)
        if not ar:
            return False
        if paused:
            ar.resume.clear()
        else:
            ar.resume.set()
        ar.progress["paused"] = paused
        events.publish("run_progress", ar.progress)
        return True

    def snapshot(self) -> dict:
        return {
            "runs": [{"run_id": a.run_id, "job_id": a.job.id, "job_name": a.job.name, "trigger": a.trigger,
                      "dry_run": a.dry_run, "status": a.status, "progress": a.progress,
                      "recent": (a.syncer.recent[-80:] if a.syncer else [])}
                     for a in self.active.values()],
            "compares": [{"job_id": c.job_id, "progress": c.progress, "started": c.started}
                         for c in self.compares.values()],
        }


runner = Runner()
