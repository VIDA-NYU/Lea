"""Thread-safe, invocation-scoped human diagnostics.

Workers enqueue while the generator drains; manual callers may supply a sink.
Tool handlers keep their string return contract. No scope means no delivery.
"""
from __future__ import annotations

import contextvars
from queue import Empty, Queue
import threading

from .events import Diagnostic


class _Collector:
    def __init__(self, sink=None):
        self.queue = Queue()
        self.seen = set()
        self.lock = threading.Lock()
        self.sink = sink


_collector = contextvars.ContextVar("lea_diagnostics", default=None)
SEVERITIES = ("fatal", "step_error", "degraded", "notice")


def begin_scope(*, sink=None):
    return _collector.set(_Collector(sink))


def end_scope(token):
    try:
        _collector.reset(token)
    except ValueError:
        pass


def report(severity, code, message, *, source="tool", remedy=None, once=False, **context):
    collector = _collector.get()
    if collector is None:
        return
    key = (code, context.get("episode_id"))
    with collector.lock:
        if once and key in collector.seen:
            return
        if once:
            collector.seen.add(key)
    event = Diagnostic(severity if severity in SEVERITIES else "notice", code, message,
                       source, remedy, {k: v for k, v in context.items() if v is not None})
    if collector.sink:
        collector.sink(event)
    else:
        collector.queue.put(event)


def drain() -> list[Diagnostic]:
    collector = _collector.get()
    if collector is None:
        return []
    out = []
    while True:
        try:
            out.append(collector.queue.get_nowait())
        except Empty:
            return out
