"""Keep background work inside the test database lifetime."""
import os
os.environ["LEA_PROCESS_ROLE"]="test"
from threading import Thread

import pytest
from evidence_fixtures import certified_unit_inputs


@pytest.fixture(autouse=True)
def isolated_run_workers(monkeypatch,tmp_path):
    # Depending on monkeypatch makes this teardown run before DB_PATH is restored.
    from app import bridge
    monkeypatch.setenv('LEA_COMPANION_IMPORT_DIR',str(tmp_path/'legacy-companion'))

    threads = []

    def tracked_thread(*args, **kwargs):
        thread = Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(bridge, "_stop_events", {})
    monkeypatch.setattr(bridge, "Thread", tracked_thread)
    yield
    from app import observation_maintenance, command_host
    command_host.close()
    observation_maintenance.close()

    for event in list(bridge._stop_events.values()):
        event.set()
    for thread in threads:
        if thread.ident is not None:
            thread.join(timeout=15)
            assert not thread.is_alive(), f"test worker {thread.name} did not stop"
