"""Deterministic dispatch unit test for `lea/lsp_daemon.py` -- no Lean, no
`lake`, milliseconds. Drives `LeanDaemon` through a fake `_Transport` that emits
server frames in an adversarial order while two threads check distinct URIs.

This is the fast, permanent guard against the cross-talk class that the old
single-shared-queue daemon shipped (H1): a check returning another document's
verdict, or a check starving because its notifications were dropped. It also
pins the two things the real Lean server actually does that the rewrite had to
handle (both measured against v4.29.0 by tests/lsp/ probes):

  1. The server sends its OWN requests (`workspace/inlayHint/refresh`,
     `client/registerCapability`, ...) whose ids COLLIDE with our request ids.
     A dispatcher that matches on id alone reads one as the answer to a
     `waitForDiagnostics` and returns a verdict computed too early. Here the
     collision frame carries the *same id* as check A's waitForDiagnostics and
     is delivered before A's real diagnostics -- a broken dispatcher returns
     "clean" for A; the correct one acks it and keeps waiting.

  2. Notifications for an unrelated uri, and uri-less notifications
     (`window/logMessage`), must be routed away from / ignored by both checks.

Run:  uv run python -m tests.lsp.test_dispatch_unit
Exits 0 if every check passes, 1 otherwise.
"""
import sys
import threading
import time
from pathlib import Path

from lea.lsp_daemon import LeanDaemon

_FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  ok   {name}" if cond else f"  FAIL {name}")
    if not cond:
        _FAILURES.append(name)


class _FakeTransport:
    """Records what the daemon sends; lets the test feed server frames back.

    Auto-answers `initialize` so `daemon.start()` completes; every other
    server->client message is driven explicitly via `feed()`.
    """

    def __init__(self):
        self.sent: list[dict] = []
        self._on_message = None
        self._cond = threading.Condition()

    def start(self, on_message) -> bool:
        self._on_message = on_message
        return True

    def send(self, msg: dict) -> None:
        with self._cond:
            self.sent.append(msg)
            self._cond.notify_all()
        if msg.get("method") == "initialize":
            self._on_message({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}})

    def poll(self):
        return None

    def close(self) -> None:
        pass

    def feed(self, msg: dict) -> None:
        self._on_message(msg)

    def wait_until(self, predicate, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        with self._cond:
            while not predicate(self.sent):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


def _diag(line: int, message: str) -> dict:
    return {"severity": 1, "range": {"start": {"line": line, "character": 0}}, "message": message}


def _publish(uri: str, version: int, diagnostics: list) -> dict:
    return {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "version": version, "diagnostics": diagnostics}}


def _file_progress(uri: str, processing: list) -> dict:
    return {"jsonrpc": "2.0", "method": "$/lean/fileProgress",
            "params": {"textDocument": {"uri": uri}, "processing": processing}}


def test_concurrent_distinct_uris_no_crosstalk_and_id_collision_safe():
    ft = _FakeTransport()
    d = LeanDaemon("/workspace", transport_factory=lambda: ft)
    assert d.start(), "fake handshake should succeed"

    path_a = "/workspace/proofs/Lea/A.lean"
    path_b = "/workspace/proofs/Lea/B.lean"
    uri_a = Path(path_a).as_uri()
    uri_b = Path(path_b).as_uri()

    results: dict[str, str] = {}

    def run(path, key):
        results[key] = d.check(path, "-- content\n")

    ta = threading.Thread(target=run, args=(path_a, "A"))
    tb = threading.Thread(target=run, args=(path_b, "B"))
    ta.start(); tb.start()

    # Both checks have sent their waitForDiagnostics (uri, version, id).
    def wfd_by_uri():
        return {m["params"]["uri"]: m for m in ft.sent
                if m.get("method") == "textDocument/waitForDiagnostics"}

    got = ft.wait_until(lambda _: len(wfd_by_uri()) >= 2)
    check("both checks issued waitForDiagnostics", got)

    wfd = wfd_by_uri()
    id_a, ver_a = wfd[uri_a]["id"], wfd[uri_a]["params"]["version"]
    id_b, ver_b = wfd[uri_b]["id"], wfd[uri_b]["params"]["version"]

    # ---- adversarial server stream ----
    ft.feed(_file_progress(uri_a, [{"range": {}}]))          # A still elaborating
    ft.feed(_publish(uri_b, ver_b, [_diag(0, "B boom")]))    # B's result BEFORE A's
    ft.feed(_publish("file:///workspace/proofs/Lea/C.lean", 1, [_diag(0, "C boom")]))  # unrelated: drop
    ft.feed({"jsonrpc": "2.0", "method": "window/logMessage",
             "params": {"type": 3, "message": "noise"}})     # uri-less: drop
    # Server REQUEST whose id COLLIDES with A's waitForDiagnostics id, delivered
    # BEFORE A's real diagnostics. A correct dispatcher acks it and A keeps
    # waiting; a broken one resolves A now and returns "clean".
    ft.feed({"jsonrpc": "2.0", "id": id_a, "method": "workspace/inlayHint/refresh"})
    ft.feed(_publish(uri_a, ver_a, [_diag(2, "A boom")]))    # A's real result, after the collision
    ft.feed(_file_progress(uri_a, []))                       # done
    ft.feed(_file_progress(uri_b, []))
    ft.feed({"jsonrpc": "2.0", "id": id_b, "result": {}})    # B's wfd response, before A's
    ft.feed({"jsonrpc": "2.0", "id": id_a, "result": {}})    # A's wfd response

    ta.join(timeout=5); tb.join(timeout=5)
    check("both checks returned", not ta.is_alive() and not tb.is_alive())

    ra, rb = results.get("A", ""), results.get("B", "")
    check("A got A's diagnostics, not B's", "A boom" in ra and "B boom" not in ra)
    check("A's path is attributed correctly", "A.lean" in ra)
    check("B got B's diagnostics, not A's", "B boom" in rb and "A boom" not in rb)
    check("B's path is attributed correctly", "B.lean" in rb)
    # If the id collision had been mis-routed as A's response, A would have
    # drained before "A boom" was fed and returned the clean string.
    check("id-colliding server request did not short-circuit A",
          ra != "OK — no errors, no warnings.")
    acked = [m for m in ft.sent if m.get("id") == id_a and m.get("result", "x") is None]
    check("the colliding server request was acked (null result)", len(acked) == 1)


def test_clean_file_returns_ok():
    ft = _FakeTransport()
    d = LeanDaemon("/workspace", transport_factory=lambda: ft)
    assert d.start()
    path = "/workspace/proofs/Lea/Clean.lean"
    uri = Path(path).as_uri()

    out = {}
    t = threading.Thread(target=lambda: out.__setitem__("r", d.check(path, "-- ok\n")))
    t.start()
    ft.wait_until(lambda s: any(m.get("method") == "textDocument/waitForDiagnostics" for m in s))
    wfd = next(m for m in ft.sent if m.get("method") == "textDocument/waitForDiagnostics")
    ft.feed(_publish(uri, wfd["params"]["version"], []))   # empty = clean
    ft.feed({"jsonrpc": "2.0", "id": wfd["id"], "result": {}})
    t.join(timeout=5)
    check("clean file returns OK", out.get("r") == "OK — no errors, no warnings.")


def test_stream_close_unblocks_a_waiting_check():
    ft = _FakeTransport()
    d = LeanDaemon("/workspace", transport_factory=lambda: ft)
    assert d.start()
    path = "/workspace/proofs/Lea/Dies.lean"

    err = {}

    def run():
        try:
            d.check(path, "-- x\n")
        except Exception as e:  # noqa: BLE001
            err["e"] = e

    t = threading.Thread(target=run)
    t.start()
    ft.wait_until(lambda s: any(m.get("method") == "textDocument/waitForDiagnostics" for m in s))
    ft.feed(None)  # transport EOF
    t.join(timeout=5)
    check("check raised instead of hanging on stream close", "e" in err)
    check("daemon marked broken after stream close", d.broken is True)


def main():
    print("lsp_daemon dispatch unit tests:")
    test_concurrent_distinct_uris_no_crosstalk_and_id_collision_safe()
    test_clean_file_returns_ok()
    test_stream_close_unblocks_a_waiting_check()
    print()
    if _FAILURES:
        print(f"FAILED ({len(_FAILURES)}): {', '.join(_FAILURES)}")
        sys.exit(1)
    print("All lsp_daemon dispatch unit tests passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
