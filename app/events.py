"""Tiny pub/sub bus: worker threads publish, SSE connections subscribe."""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None
_subs: set[asyncio.Queue] = set()
_lock = threading.Lock()
_last_emit: dict[str, float] = {}


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    with _lock:
        _subs.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subs.discard(q)


def _put(q: asyncio.Queue, msg: str) -> None:
    if q.full():
        try:
            q.get_nowait()          # drop oldest rather than block a worker
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(msg)


def publish(kind: str, data: Any, throttle_key: str | None = None, min_interval: float = 0.0) -> None:
    """Publish from any thread. Throttled keys drop bursts (progress updates)."""
    if throttle_key and min_interval:
        now = time.monotonic()
        if now - _last_emit.get(throttle_key, 0) < min_interval:
            return
        _last_emit[throttle_key] = now
    if _loop is None:
        return
    msg = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
    with _lock:
        subs = list(_subs)
    for q in subs:
        try:
            _loop.call_soon_threadsafe(_put, q, msg)
        except RuntimeError:
            pass
