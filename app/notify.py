"""Notifications and shell hooks."""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import urllib.request
from email.message import EmailMessage

from .fsutil import human
from .models import Job, NotifyTarget

STATUS_ICON = {"success": "✅", "warning": "⚠️", "failed": "❌", "cancelled": "⏹", "blocked": "🛑"}


def _post(url: str, body: bytes, headers: dict[str, str], timeout: int = 15) -> None:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def build_message(job: Job, run: dict) -> tuple[str, str]:
    st = run.get("stats", {})
    status = run.get("status", "?")
    title = f"{STATUS_ICON.get(status, '')} {job.name}: {status}".strip()
    lines = [
        f"Copied {st.get('files_copied', 0)} file(s), {human(st.get('bytes_copied', 0))}",
        f"Deleted {st.get('files_deleted', 0)}, moved {st.get('moved', 0)}",
        f"Errors {st.get('errors', 0)} · took {int(st.get('duration', 0))}s",
    ]
    if run.get("dry_run"):
        lines.insert(0, "Dry run — nothing was changed")
    if run.get("error"):
        lines.append(run["error"])
    return title, "\n".join(lines)


def send(target: NotifyTarget, job: Job, run: dict) -> None:
    title, text = build_message(job, run)
    failed = run.get("status") in ("failed", "blocked")
    if target.type == "ntfy":
        base = target.url.rstrip("/") or "https://ntfy.sh"
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                   "Priority": "high" if failed else "default",
                   "Tags": "floppy_disk"}
        if target.token:
            headers["Authorization"] = f"Bearer {target.token}"
        _post(f"{base}/{target.topic}", text.encode(), headers)
    elif target.type == "discord":
        color = 0xE26D5A if failed else 0x7BC47F
        payload = {"embeds": [{"title": title, "description": text, "color": color}]}
        _post(target.url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    elif target.type == "gotify":
        url = f"{target.url.rstrip('/')}/message?token={target.token}"
        payload = {"title": title, "message": text, "priority": 8 if failed else 4}
        _post(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    elif target.type == "webhook":
        payload = {"job": {"id": job.id, "name": job.name}, "run": run, "title": title, "message": text}
        headers = {"Content-Type": "application/json"}
        if target.token:
            headers["Authorization"] = f"Bearer {target.token}"
        _post(target.url, json.dumps(payload, default=str).encode(), headers)
    elif target.type == "email":
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = target.email_from or target.smtp_user
        msg["To"] = target.email_to
        msg.set_content(text)
        if target.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(target.smtp_host, target.smtp_port, context=ssl.create_default_context(), timeout=20)
        else:
            server = smtplib.SMTP(target.smtp_host, target.smtp_port, timeout=20)
            if target.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if target.smtp_user:
                server.login(target.smtp_user, target.smtp_password)
            server.send_message(msg)


def should_notify(job: Job, run: dict) -> bool:
    when = job.notify.when
    status = run.get("status")
    if when == "never":
        return False
    if when == "always":
        return True
    if when == "failure":
        return status in ("failed", "warning", "blocked")
    st = run.get("stats", {})
    changed = st.get("files_copied", 0) + st.get("files_deleted", 0) + st.get("moved", 0)
    return changed > 0 or status in ("failed", "warning", "blocked")


def dispatch(job: Job, run: dict, log=None) -> None:
    if not should_notify(job, run):
        return
    for t in job.notify.targets:
        if not t.enabled:
            continue
        try:
            send(t, job, run)
            if log:
                log(f"Notification sent via {t.type}")
        except Exception as e:  # noqa: BLE001 — never let a notifier break a run
            if log:
                log(f"Notification via {t.type} failed: {e}")


def run_hook(command: str, env_extra: dict[str, str], timeout: int, log) -> int:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    log(f"Running hook: {command}")
    try:
        p = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"Hook timed out after {timeout}s")
        return 124
    for line in (p.stdout + p.stderr).splitlines()[-40:]:
        log(f"  hook> {line}")
    log(f"Hook exited with {p.returncode}")
    return p.returncode


def test_target(target: NotifyTarget) -> None:
    fake = Job(name="Proto-Sync test")
    send(target, fake, {"status": "success", "stats": {"files_copied": 3, "bytes_copied": 123456789}})
