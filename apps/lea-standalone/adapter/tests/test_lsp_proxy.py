"""v2.2 · L1/L2 — the LSP-over-WebSocket proxy's pure pieces.

The full pump needs a live `lake serve`, so these pin the parts we can test
hermetically: the `file://` URI rewriting in both directions (the seam that maps a
browser's virtual document path to the real on-disk file, D60/D64), the
Content-Length framing, and Lake-root resolution. If the rewrite is wrong the Lean
server opens the wrong file (or none); if the framing is wrong nothing parses.
"""

import asyncio
import json

import pytest

from app import lsp_proxy

PREFIX = "/abs/workspace"


def test_rewrite_client_to_server_prefixes_uris():
    msg = {
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///proofs/s1/Main.lean", "text": "x"}},
    }
    out = lsp_proxy._apply_client_to_server(msg, PREFIX)
    assert out["params"]["textDocument"]["uri"] == "file:///abs/workspace/proofs/s1/Main.lean"


def test_rewrite_prefixes_root_uri_and_root_path():
    msg = {"method": "initialize", "params": {
        "rootUri": "file://", "rootPath": "/", "capabilities": {}}}
    out = lsp_proxy.rewrite_client_to_server(msg, PREFIX)
    assert out["params"]["rootUri"] == f"file://{PREFIX}"
    # rootPath is deprecated (the server uses rootUri); a trailing slash is harmless.
    assert out["params"]["rootPath"].rstrip("/") == PREFIX


def test_definition_requests_are_not_rewritten():
    # lean4web leaves textDocument/definition URIs alone (remapped on the response);
    # rewriting them would break go-to-definition.
    msg = {"method": "textDocument/definition", "params": {
        "textDocument": {"uri": "file:///proofs/s1/Main.lean"}}}
    out = lsp_proxy._apply_client_to_server(msg, PREFIX)
    assert out["params"]["textDocument"]["uri"] == "file:///proofs/s1/Main.lean"


def test_rewrite_server_to_client_strips_prefix():
    msg = {"method": "textDocument/publishDiagnostics", "params": {
        "uri": f"file://{PREFIX}/proofs/s1/Main.lean", "diagnostics": []}}
    out = lsp_proxy.rewrite_server_to_client(msg, PREFIX)
    assert out["params"]["uri"] == "file:///proofs/s1/Main.lean"


def test_rewrite_round_trips_nested_lists():
    # locations come back as arrays of {uri, range}; every uri must be stripped.
    server_msg = {"result": [
        {"uri": f"file://{PREFIX}/a.lean", "range": {}},
        {"uri": f"file://{PREFIX}/b.lean", "range": {}},
    ]}
    out = lsp_proxy.rewrite_server_to_client(server_msg, PREFIX)
    assert [loc["uri"] for loc in out["result"]] == ["file:///a.lean", "file:///b.lean"]


def test_encode_produces_content_length_frame():
    frame = lsp_proxy._encode({"jsonrpc": "2.0", "id": 1})
    header, _, body = frame.partition(b"\r\n\r\n")
    assert header == f"Content-Length: {len(body)}".encode()
    assert json.loads(body) == {"jsonrpc": "2.0", "id": 1}


def test_read_message_parses_a_framed_message():
    payload = {"jsonrpc": "2.0", "method": "hi", "params": {}}
    frame = lsp_proxy._encode(payload)

    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        return await lsp_proxy._read_message(reader)

    assert asyncio.run(run()) == payload


def test_read_message_returns_none_at_eof():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return await lsp_proxy._read_message(reader)

    assert asyncio.run(run()) is None


def test_resolve_target_finds_lake_root_and_relative_name(tmp_path):
    root = tmp_path / "workspace"
    (root / "proofs" / "s1").mkdir(parents=True)
    (root / "lakefile.toml").write_text("")
    proof = root / "proofs" / "s1" / "Main.lean"
    proof.write_text("theorem t : True := trivial")

    lake_root, file_name = lsp_proxy.resolve_target(str(proof))
    assert lake_root == root.resolve()
    assert file_name == "proofs/s1/Main.lean"


def test_resolve_target_raises_without_lakefile(tmp_path):
    proof = tmp_path / "Main.lean"
    proof.write_text("x")
    with pytest.raises(FileNotFoundError):
        lsp_proxy.resolve_target(str(proof))


# --- AUDIT-2026-07-24 S1: the URI rewrite is a blind prefix --------------------

def test_a_traversing_uri_is_not_redirected_at_a_real_file(tmp_path):
    """`rewrite_client_to_server` prefixes `file://` with the Lake root and nothing
    else, so a client-supplied `..` resolved to a real file anywhere on the host — and
    the Lean server would then read it and report its contents back through
    diagnostics and hover. The client controls this string; nothing else validated it.
    """
    lake_root = tmp_path / "workspace"
    lake_root.mkdir()
    prefix = str(lake_root)

    message = {
        "method": "textDocument/didOpen",
        "params": {"textDocument": {"uri": "file:///../../../../etc/passwd"}},
    }
    lsp_proxy.rewrite_client_to_server(message, prefix)

    uri = message["params"]["textDocument"]["uri"]
    assert "/etc/passwd" not in uri or not uri.startswith(f"file://{prefix}")
    assert uri == "file:///../../../../etc/passwd", "the escaping uri must be left unprefixed"


def test_a_rootpath_that_escapes_the_root_is_refused(tmp_path):
    lake_root = tmp_path / "workspace"
    lake_root.mkdir()
    (tmp_path / "secrets").mkdir()

    escaping = "/../secrets"          # resolves to tmp_path/secrets — outside the root
    message = {"params": {"rootPath": escaping}}
    lsp_proxy.rewrite_client_to_server(message, str(lake_root))
    assert message["params"]["rootPath"] == escaping, "an escaping rootPath must stay unprefixed"


def test_a_rootpath_that_normalizes_back_inside_is_allowed(tmp_path):
    """`..` is not itself the test — containment after normalization is. A path that
    detours and comes back is legitimate, and refusing it would be a false positive."""
    lake_root = tmp_path / "workspace"
    (lake_root / "proofs").mkdir(parents=True)

    message = {"params": {"rootPath": "/proofs/../proofs"}}
    lsp_proxy.rewrite_client_to_server(message, str(lake_root))
    assert message["params"]["rootPath"] == str(lake_root / "proofs/../proofs")


def test_a_normal_uri_is_still_rewritten(tmp_path):
    """The rewrite has to keep working — this is how the browser's virtual path
    becomes the real file (D60/D64)."""
    lake_root = tmp_path / "workspace"
    (lake_root / "proofs" / "s1").mkdir(parents=True)
    prefix = str(lake_root)

    message = {"params": {"textDocument": {"uri": "file:///proofs/s1/p.lean"}}}
    lsp_proxy.rewrite_client_to_server(message, prefix)

    assert message["params"]["textDocument"]["uri"] == f"file://{prefix}/proofs/s1/p.lean"


# --- AUDIT-2026-07-24 X3: bound the `lake serve` processes --------------------

def test_session_slots_are_bounded_and_released(monkeypatch):
    """Every WebSocket connection spawned a `lake serve` — an 8 MiB-buffered process
    that loads Mathlib and is multi-GB resident — with nothing capping them."""
    import threading

    monkeypatch.setattr(lsp_proxy, "MAX_SESSIONS", 3)
    monkeypatch.setattr(lsp_proxy, "_session_slots", threading.BoundedSemaphore(3))

    assert [lsp_proxy.acquire_session_slot() for _ in range(3)] == [True, True, True]
    assert lsp_proxy.acquire_session_slot() is False, "the cap must hold"

    lsp_proxy.release_session_slot()
    assert lsp_proxy.acquire_session_slot() is True, "a freed slot is reusable"


def test_releasing_more_than_was_acquired_is_harmless(monkeypatch):
    """The route's `finally` runs unconditionally, including on paths that never
    acquired — an over-release must not raise out of teardown."""
    import threading

    monkeypatch.setattr(lsp_proxy, "_session_slots", threading.BoundedSemaphore(1))
    lsp_proxy.release_session_slot()
    lsp_proxy.release_session_slot()
    assert lsp_proxy.acquire_session_slot() is True


def test_stop_is_idempotent(tmp_path):
    """The route calls `stop()` in a `finally` even though `pump` also stops the
    process, so that an error between `start()` and `pump()` cannot orphan it."""
    proxy = lsp_proxy.LspProxy(tmp_path, str(tmp_path))
    asyncio.run(proxy.stop())   # never started
    asyncio.run(proxy.stop())   # and again
