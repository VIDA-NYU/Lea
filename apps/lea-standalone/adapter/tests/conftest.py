"""Keep background adapter work inside the lifetime of its database fixture."""
from queue import Queue
from threading import Thread

import pytest
from evidence_fixtures import certified_unit_inputs


@pytest.fixture(autouse=True)
def isolated_run_workers(monkeypatch):
    # Depending on monkeypatch makes this teardown run before DB_PATH is restored.
    from app import bridge

    queue = Queue()
    threads = []

    def tracked_thread(*args, **kwargs):
        thread = Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(bridge, "_run_queue", queue)
    monkeypatch.setattr(bridge, "_worker_thread", None)
    monkeypatch.setattr(bridge, "_stop_events", {})
    monkeypatch.setattr(bridge, "Thread", tracked_thread)
    yield

    # Stop admission first, then let every driver finish while its DB still exists.
    queue.put(bridge._DISPATCH_STOP)
    for thread in threads:
        if thread.name == "lea-run-dispatcher" and thread.ident is not None:
            thread.join(timeout=5)
            assert not thread.is_alive(), "test dispatcher did not stop"
    for event in list(bridge._stop_events.values()):
        event.set()
    for thread in threads:
        if thread.name != "lea-run-dispatcher" and thread.ident is not None:
            thread.join(timeout=15)
            assert not thread.is_alive(), f"test worker {thread.name} did not stop"
