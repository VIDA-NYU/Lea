"""Isolated audit reproduction; temporary data, no paid model calls.

Run with the adapter virtualenv Python from the repository root.
"""

import json
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

REPO_ROOT = Path(__file__).resolve().parents[2]
repo = REPO_ROOT
with tempfile.TemporaryDirectory(prefix='lea-audit-cli-restart-') as tmp:
    root = Path(tmp)
    prover = root / 'prover'
    (prover / 'workspace/proofs').mkdir(parents=True)
    (prover / 'lea').mkdir()
    (prover / 'pyproject.toml').write_text('[project]\nname = "test"\n')
    (root / 'config').mkdir()
    (root / 'config/lea.local.toml').write_text(f'model = "o4-mini"\nlea_root = {json.dumps(str(prover))}\n')
    (root / 'data').mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, LEA_ITEST_HOME=str(root), LEA_ITEST_PORT=str(port))
    log = (root / 'server.log').open('w')
    child = subprocess.Popen([sys.executable, str(repo / 'tests/integration/adapter_entry.py')],
        env=env, stdout=log, stderr=subprocess.STDOUT)
    def call(endpoint, body=None):
        req = Request(f'http://127.0.0.1:{port}{endpoint}',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'content-type': 'application/json'} if body is not None else {})
        with urlopen(req, timeout=2) as res:
            return json.load(res)
    try:
        ready = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                before = call('/api/health')
                ready = True
                break
            except OSError:
                time.sleep(.1)
        assert ready, 'Isolated server did not start'
        try:
            call('/api/runtime/restart', {'shutdown': True})
            control_status = 200
            control_detail = None
        except HTTPError as error:
            control_status = error.code
            control_detail = json.load(error)['detail']
        time.sleep(.3)
        after = call('/api/health')
        try:
            call('/api/runs', {'message': 'Prove True'})
            admission = 200
        except HTTPError as error:
            admission = error.code
        print(json.dumps({'uvicorn_cli_restart': {'initial_state': before['lifecycle_state'],
            'state_after_shutdown': after['lifecycle_state'], 'health_ok': after['ok'],
            'restart_supported': after['restart_supported'], 'ready': after['ready'],
            'control_http_status': control_status, 'control_detail': control_detail,
            'process_still_running': child.poll() is None, 'new_run_http_status': admission}}, indent=2))
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        log.close()
