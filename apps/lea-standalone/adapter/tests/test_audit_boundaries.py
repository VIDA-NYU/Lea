"""October audit regressions using real boundaries and isolated transports."""
import io
import urllib.request
import urllib.error
from email.message import Message
from queue import Queue
from urllib.response import addinfourl

import pytest

from app import bridge, db, runbroker, store
from app.config import LeaConfig
from lea import agent, http_tools
from lea.interface import Finished
from lea.providers import Usage


@pytest.mark.parametrize("code", [200, 401])
def test_direct_http_responses_are_useful_and_do_not_echo_credentials(monkeypatch, code):
    secret = "nonpattern-http-credential"
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("X-api-key") == secret
            body = io.BytesIO(f"service detail {secret}".encode())
            if code != 200:
                raise urllib.error.HTTPError(request.full_url, code, "Unauthorized", Message(), body)
            return addinfourl(body, Message(), request.full_url, 200)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(http_tools, "_is_public", lambda host: True)
    monkeypatch.setenv("AUDIT_HTTP_KEY", secret)
    result = http_tools.make_handler({"url": "https://public.example/", "auth_key_name": "AUDIT_HTTP_KEY",
                                     "auth_header": "X-Api-Key: {key}"})({})
    assert "service detail" in result
    assert secret not in result
    assert "[redacted]" in result


@pytest.mark.parametrize("destination", [
    "http://127.0.0.1:8001/api/settings", "https://192.168.1.1/",
    "http://public.example/", "https://other.example/",
])
def test_http_redirect_never_sends_a_second_request(monkeypatch, destination):
    seen = []

    class Transport(urllib.request.HTTPSHandler):
        def https_open(self, request):
            seen.append((request.full_url, request.get_header("Authorization")))
            headers = Message()
            if len(seen) == 1:
                headers["Location"] = destination
            result = addinfourl(io.BytesIO(b"response"), headers, request.full_url,
                               302 if len(seen) == 1 else 200)
            result.msg = "Found" if len(seen) == 1 else "OK"
            return result

    class HTTPTransport(urllib.request.HTTPHandler):
        def http_open(self, request):
            return Transport().https_open(request)

    build = urllib.request.build_opener
    def opener(*handlers):
        return build(urllib.request.ProxyHandler({}), Transport(), HTTPTransport(), *handlers)
    monkeypatch.setattr(urllib.request, "_opener", opener())
    monkeypatch.setattr(urllib.request, "build_opener", opener)
    monkeypatch.setattr(http_tools, "_is_public", lambda host: host == "public.example")
    monkeypatch.setenv("AUDIT_HTTP_KEY", "dummy-credential")
    result = http_tools.make_handler({"url": "https://public.example/",
                                     "auth_key_name": "AUDIT_HTTP_KEY"})({})
    assert seen == [("https://public.example/", "Bearer dummy-credential")]
    assert "redirect" in result.lower()
    assert "dummy-credential" not in result




def test_finished_detail_is_redacted_in_storage_live_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "audit.sqlite3")
    db.init_db()
    monkeypatch.setenv("OPENAI_API_KEY", "nonpattern-audit-credential")
    secret = "nonpattern-audit-credential"
    session = store.create_session("Audit")
    run = store.create_run(session["id"], "test", None, 3)
    events = Queue()
    def fake(*args, **kwargs):
        yield Finished("completed", "Review this", 1, session["id"], "test", Usage(), 0, {},
                       result_kind="needs_review", result_detail=f"Provider rejected {secret}")
    monkeypatch.setattr(bridge, "run_events", fake)
    bridge.run_lea(bridge.RunnerContext(session["id"], run["id"], "Prove True",
                    LeaConfig(model="test", max_turns=3, lea_root=tmp_path), events))
    with db.connect() as conn:
        persisted = conn.execute("select result_detail from runs where id=?", (run["id"],)).fetchone()[0]
    assert secret not in persisted
    done = list(events.queue)[-1]
    assert done["type"] == "done"
    assert secret not in done["payload"]["result_detail"]
    # Old database rows are sanitized on public reads without rewriting history.
    with db.connect() as conn:
        conn.execute("update runs set result_detail=? where id=?", (f"Old error {secret}", run["id"]))
    assert secret not in store.get_run(run["id"])["result_detail"]
    assert secret not in store.get_run_status(run["id"])["result_detail"]
    assert secret not in str(store.session_detail(session["id"])["runs"])
    broker = runbroker.create(run["id"])
    bridge.publish_terminal_from_row(run["id"])
    assert secret not in broker.events_after(0)[-1]["payload"]["result_detail"]
