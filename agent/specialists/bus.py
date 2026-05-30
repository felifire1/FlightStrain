"""In-process pub/sub bus for specialist findings.

Tonight: a single in-memory queue with JSONL persistence so the demo is
replayable and the chat panel can hydrate on reconnect.

Tomorrow / production: drop-in replaceable with NATS, Redis Streams, Kafka,
or kagent's native event bus. The public methods (publish/subscribe) stay
the same.

Usage from a specialist:
    from agent.specialists.bus import bus
    bus.publish(finding)

Usage from the chat server (server.py):
    from agent.specialists.bus import bus
    for finding in bus.subscribe():
        push_via_sse(finding.chat_render())
"""
from __future__ import annotations
import json
import threading
from collections import deque
from pathlib import Path
from queue import Queue, Empty
from typing import Iterator

from .base import Event, Finding

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_PATH = ROOT / "data" / "events" / "findings.jsonl"


class Bus:
    def __init__(self, ring_size: int = 1000, dedup_window_sec: float = 120.0):
        self._subs: list[Queue] = []
        self._lock = threading.Lock()
        self._ring: deque[Finding] = deque(maxlen=ring_size)
        self._dedup_window = dedup_window_sec
        # (specialist, summary) -> last_publish_ts; drop re-publishes inside window
        self._dedup: dict[tuple[str, str], float] = {}

    # ----- publishing -----

    def publish(self, item: Finding | Event) -> bool:
        """Push to all subscribers. Returns False if deduped."""
        import time as _t
        if isinstance(item, Finding):
            key = (item.specialist, item.summary)
            now = _t.time()
            last = self._dedup.get(key, 0.0)
            if now - last < self._dedup_window:
                return False  # already saw this recently
            self._dedup[key] = now
        with self._lock:
            if isinstance(item, Finding):
                self._ring.append(item)
                self._persist(item)
            for q in self._subs:
                q.put_nowait(item)
        return True

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()
            self._dedup.clear()

    def _persist(self, finding: Finding) -> None:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with LOG_PATH.open("a") as f:
                f.write(json.dumps(finding.to_dict()) + "\n")
        except OSError:
            pass  # don't let disk problems break the live pipeline

    # ----- subscribing -----

    def subscribe(self) -> Iterator[Finding | Event]:
        """Blocking iterator. Each call returns a new subscription stream.
        Server-side uses this in a thread and pushes over SSE/WebSocket."""
        q: Queue = Queue()
        with self._lock:
            self._subs.append(q)
            # Replay the recent ring so a fresh subscriber gets context
            for item in list(self._ring):
                q.put_nowait(item)
        try:
            while True:
                try:
                    yield q.get(timeout=1.0)
                except Empty:
                    continue
        finally:
            with self._lock:
                if q in self._subs:
                    self._subs.remove(q)

    def latest(self, n: int = 20) -> list[Finding]:
        """Snapshot of the most recent findings — used by the coordinator
        when synthesizing a reply to a user question."""
        with self._lock:
            return list(self._ring)[-n:]

    def filter(self, specialist: str | None = None, min_severity: int = 0) -> list[Finding]:
        with self._lock:
            return [
                f for f in self._ring
                if (specialist is None or f.specialist == specialist)
                and f.severity >= min_severity
            ]


# Module-level singleton — every import sees the same instance.
bus = Bus()
