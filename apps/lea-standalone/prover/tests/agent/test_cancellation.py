"""Stop cancels live I/O and prevents further work, without live providers."""
import asyncio
import os
import shlex
import sys
import threading
import time
from types import SimpleNamespace as NS

import pytest

from lea import agent, providers, execution
from lea.cancellation import RunCancelled
from lea.config import LeaConfig
from lea.events import Finished
from lea.runctx import run_context

REAL_STREAM = agent.stream


@pytest.mark.parametrize("streaming", [False, True])
def test_successful_requests_keep_usage_and_close_the_stream(monkeypatch, streaming):
    closed = threading.Event()
    class Response:
        def __aiter__(self):
            return self.chunks()
        async def chunks(self):
            yield NS(choices=[NS(delta=NS(content="Hello"), finish_reason=None)], usage=None)
            yield NS(choices=[], usage=NS(prompt_tokens=10, completion_tokens=2))
        async def aclose(self):
            closed.set()
    async def complete(**kwargs):
        if streaming:
            return Response()
        return NS(choices=[NS(message=NS(content="Hello", tool_calls=[]))], usage=NS(prompt_tokens=10, completion_tokens=2))
    monkeypatch.setattr(providers.litellm, "acompletion", complete)
    with run_context(should_stop=lambda: False):
        for _ in range(2):
            events = list(providers.stream("gemini/test", "SYS", [], [], streaming=streaming))
            assert events[0].text == "Hello"
            assert events[-1].usage == providers.Usage(10, 2)
    if streaming:
        assert closed.is_set()


def test_cancelling_one_run_keeps_another_runs_request_alive():
    from lea.cancellation import iterate_async
    started = [threading.Event(), threading.Event()]
    closed = [threading.Event(), threading.Event()]
    stops = [threading.Event(), threading.Event()]
    errors = []
    loops = []
    def invoke(index):
        async def request():
            loops.append(asyncio.get_running_loop())
            started[index].set()
            try:
                await asyncio.sleep(60)
                yield "finished"
            finally:
                closed[index].set()
        try:
            with run_context(should_stop=stops[index].is_set):
                list(iterate_async(request))
        except RunCancelled:
            errors.append(index)
    workers = [threading.Thread(target=invoke, args=(i,), daemon=True) for i in range(2)]
    for worker in workers:
        worker.start()
    assert all(event.wait(2) for event in started)
    try:
        stops[0].set()
        workers[0].join(2)
        assert closed[0].is_set()
        assert not closed[1].is_set()
        assert workers[1].is_alive()
        assert loops[0] is loops[1]  # provider connection pools stay on one live loop
    finally:
        stops[1].set()
        for worker in workers:
            worker.join(2)
    assert sorted(errors) == [0, 1]


@pytest.mark.parametrize("phase", ["connecting", "streaming", "blocking"])
def test_stop_cancels_provider_task_and_closes_stream(monkeypatch, phase):
    started, cancelled, closed, stop = (threading.Event() for _ in range(4))
    events, errors = [], []

    class Response:
        def __aiter__(self):
            return self.chunks()

        async def chunks(self):
            yield NS(choices=[NS(delta=NS(content="Partial reply"), finish_reason=None)], usage=None)
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        async def aclose(self):
            closed.set()

    async def complete(**kwargs):
        if phase != "streaming":
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        return Response()

    monkeypatch.setattr(providers.litellm, "acompletion", complete)
    monkeypatch.setattr(agent, "stream", REAL_STREAM)
    monkeypatch.setattr(agent, "load_system_prompt", lambda *a, **kw: "SYS")
    monkeypatch.setattr(agent, "build_toolset", lambda *a: ([], {}))
    cfg = LeaConfig(model="gemini/test", max_turns=3, tools=[], stream=phase != "blocking")

    def drive():
        try:
            events.extend(agent.run_events(cfg, [{"role": "user", "content": "test"}], should_stop=stop.is_set))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    assert started.wait(3)
    stop.set()
    worker.join(3)
    assert not worker.is_alive()
    assert errors == []
    assert cancelled.wait(1)
    if phase == "streaming":
        assert closed.is_set()
        assert events[-1].transcript["messages"][-1]["content"] == [{"type": "text", "text": "Partial reply"}]
    assert isinstance(events[-1], Finished)
    assert events[-1].reason == "interrupted"
    assert events[-1].usage_status == "partial"


def test_stop_before_followup_never_classifies_intent(monkeypatch):
    monkeypatch.setattr(agent, "load_system_prompt", lambda *a, **kw: "SYS")
    monkeypatch.setattr(agent, "_classify_intent", lambda *a: pytest.fail("model request after Stop"))
    cfg = LeaConfig(model="gemini/test", max_turns=3, tools=[])
    events = list(agent.run_events(cfg, [{"role": "assistant", "content": []},
        {"role": "user", "content": "follow-up"}], should_stop=lambda: True))
    assert events[-1].reason == "interrupted"


@pytest.mark.parametrize("project", [False, True])
def test_stop_skips_remaining_tools_in_every_chat(monkeypatch, project):
    stop = threading.Event()
    calls = []

    def stream(*a, **kw):
        yield providers.ToolCall("first", {})
        yield providers.ToolCall("second", {})
        yield providers.Done(providers.Usage(4, 2), 0.1)

    def first(args):
        calls.append("first")
        stop.set()
        return "ok"

    monkeypatch.setattr(agent, "stream", stream)
    monkeypatch.setattr(agent, "load_system_prompt", lambda *a, **kw: "SYS")
    monkeypatch.setattr(agent, "build_toolset", lambda *a: ([], {"first": first,
        "second": lambda _: calls.append("second")}))
    cfg = LeaConfig(model="gemini/test", max_turns=3, tools=[], project_library=project)
    events = list(agent.run_events(cfg, [{"role": "user", "content": "test"}], should_stop=stop.is_set))
    assert calls == ["first"]
    assert events[-1].reason == "interrupted"
    assert events[-1].usage == providers.Usage(4, 2)
    assert events[-1].cost == 0.1


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_stop_kills_shell_descendants_before_they_can_write(tmp_path):
    stop = threading.Event()
    ready, marker = tmp_path / "ready", tmp_path / "should-not-exist"
    child = "import time; from pathlib import Path; time.sleep(0.6); Path(%r).write_text('late')" % str(marker)
    parent = "import subprocess,time; from pathlib import Path; subprocess.Popen([%r,'-c',%r]); Path(%r).touch(); time.sleep(60)" % (sys.executable, child, str(ready))
    errors = []

    def run():
        try:
            with run_context(should_stop=stop.is_set):
                execution.run(shlex.join([sys.executable, "-c", parent]), shell=True,
                              capture_output=True, text=True, timeout=60)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    stop.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RunCancelled)
    time.sleep(0.7)
    assert not marker.exists()


def test_stop_cancels_mcp_request_on_its_own_loop():
    from lea.mcp import MCPManager
    manager = MCPManager({})
    loop = asyncio.new_event_loop()
    manager._loop = loop
    started, cancelled, stop = (threading.Event() for _ in range(3))

    async def call(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    manager._acall = call
    loop_worker = threading.Thread(target=loop.run_forever, daemon=True)
    loop_worker.start()
    errors = []
    def invoke():
        try:
            with run_context(should_stop=stop.is_set):
                manager._make_handler("test", "slow")({})
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        assert started.wait(2)
        stop.set()
        worker.join(2)
        assert not worker.is_alive()
        assert cancelled.wait(1)
        assert len(errors) == 1 and isinstance(errors[0], RunCancelled)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_worker.join(2)
        loop.close()


def test_cancelled_mcp_call_notifies_server_with_its_request_id():
    from lea.mcp import MCPManager
    notifications = []
    started = asyncio.Event()

    class Session:
        _request_id = 42
        async def call_tool(self, *args):
            self._request_id += 1
            started.set()
            await asyncio.sleep(60)
        async def send_notification(self, notification):
            notifications.append(notification.model_dump())

    async def scenario():
        manager = MCPManager({})
        manager._sessions["test"] = Session()
        task = asyncio.create_task(manager._acall("test", "slow", {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert notifications[0]["method"] == "notifications/cancelled"
    assert notifications[0]["params"]["requestId"] == 42


def test_stop_closes_declared_http_tool_connection(monkeypatch):
    from lea import http_tools
    started, closed, stop = (threading.Event() for _ in range(3))

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            closed.set()
        async def request(self, *args, **kwargs):
            started.set()
            await asyncio.sleep(60)

    monkeypatch.setattr(http_tools, "check_url", lambda url: url)
    monkeypatch.setattr(http_tools.httpx, "AsyncClient", Client)
    errors = []
    def invoke():
        try:
            with run_context(should_stop=stop.is_set):
                http_tools.make_handler({"url": "https://test.invalid"})({})
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    assert started.wait(2)
    stop.set()
    worker.join(2)
    assert not worker.is_alive()
    assert closed.is_set()
    assert len(errors) == 1 and isinstance(errors[0], RunCancelled)


def test_stop_cancels_mcp_initialization_and_reaps_its_loop(monkeypatch):
    from lea.mcp import MCPManager
    started, closed, stop = (threading.Event() for _ in range(3))
    manager = MCPManager({"test": {}})
    async def connect(*args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            closed.set()
    monkeypatch.setattr(manager, "_connect_one", connect)
    errors = []
    def start():
        try:
            with run_context(should_stop=stop.is_set):
                manager.start(register=False)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=start, daemon=True)
    worker.start()
    assert started.wait(2)
    stop.set()
    worker.join(3)
    assert not worker.is_alive()
    assert closed.is_set()
    assert manager._loop is None
    assert len(errors) == 1 and isinstance(errors[0], RunCancelled)


def test_stop_reaches_a_real_mcp_server_before_closing_its_session(tmp_path):
    from lea.mcp import MCPManager
    ready, cancelled = tmp_path / "ready", tmp_path / "cancelled"
    server = tmp_path / "server.py"
    server.write_text(f'''import asyncio
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("stop-test")
@server.tool()
async def slow() -> str:
    Path({str(ready)!r}).touch()
    try:
        await asyncio.sleep(30)
        return "finished"
    finally:
        Path({str(cancelled)!r}).touch()
server.run(transport="stdio")
''')
    manager = MCPManager({"test": {"command": sys.executable, "args": [str(server)]}})
    stop, errors = threading.Event(), []
    def invoke():
        try:
            with run_context(should_stop=stop.is_set):
                manager._make_handler("test", "slow")({})
        except BaseException as exc:
            errors.append(exc)
    manager.start(register=False)
    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        stop.set()
        worker.join(2)
        deadline = time.monotonic() + 3
        while not cancelled.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancelled.exists()  # the remote tool stopped while the session was live
        assert manager.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RunCancelled)
    finally:
        stop.set()
        worker.join(2)
        manager.stop()
