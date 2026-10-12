"""Exercise the production entrypoint in a small, keyless Docker image.

Run with the adapter Python from the repository root. Only temporary data and
test-owned containers are used. Mathlib and the frontend are unnecessary here.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=40,
                                   stderr=subprocess.STDOUT if args[0] == 'logs' else None).strip()


def request(base, endpoint, body=None):
    req = Request(base + endpoint, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'} if body is not None else {})
    with urlopen(req, timeout=2) as response:
        return json.load(response)


def ready(base, previous=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            result = request(base, '/api/ready')
            if result['ready'] and result['runtime_id'] != previous:
                return result
        except OSError:
            pass
        time.sleep(.1)
    raise AssertionError('Container did not become ready with a new runtime identity')


def main():
    image = 'lea-runtime-audit:main-readiness-v1'
    containers = []
    with tempfile.TemporaryDirectory(prefix='lea-container-lifecycle-') as tmp:
        home = Path(tmp)
        context = home / 'image'
        app = ROOT / 'apps/lea-standalone'
        for name in ('app', 'migrations'):
            shutil.copytree(app / 'adapter' / name, context / 'adapter' / name,
                            ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('pyproject.toml', 'uv.lock', 'run_api.py'):
            shutil.copyfile(app / 'adapter' / name, context / 'adapter' / name)
        shutil.copytree(app / 'prover/lea', context / 'prover/lea',
                        ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('pyproject.toml', 'README.md'):
            shutil.copyfile(app / 'prover' / name, context / 'prover' / name)
        (context / 'prover/workspace/proofs').mkdir(parents=True)
        shutil.copytree(ROOT / 'packages/lea-model-catalog', context / 'packages/lea-model-catalog')
        shutil.copytree(ROOT / 'scripts', context / 'scripts')
        for name in ('companion', 'shared', 'extension'):
            shutil.copytree(ROOT / 'apps/overleaf-extension' / name, context / 'apps/overleaf-extension' / name)
        runtime_helper = Path('apps/lea-standalone/src/app/lib/leanCheckRuntime.mjs')
        (context / runtime_helper).parent.mkdir(parents=True)
        shutil.copyfile(ROOT / runtime_helper, context / runtime_helper)
        shutil.copyfile(app / 'docker/prepare-mathlib.sh', context / 'prepare-mathlib.sh')
        (context / 'prover/workspace/lakefile.lean').write_text('-- small lifecycle fixture; no real Lean')
        # This image tests supervision only. The production_container suite must
        # separately test real Mathlib/Lean/SafeVerify with the actual Dockerfile.
        (context / 'lake').write_text('#!/bin/sh\nsleep "${LEA_TEST_PREPARE_SLEEP:-0}"\nexit "${LEA_TEST_PREPARE_EXIT:-0}"\n')
        shutil.copyfile(app / 'docker/entrypoint.sh', context / 'entrypoint.sh')
        (context / 'Dockerfile').write_text('''FROM node:22-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
COPY prover ./prover
COPY adapter ./adapter
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY packages /app/packages
COPY scripts /app/scripts
COPY apps /app/apps
COPY lake /usr/local/bin/lake
COPY prepare-mathlib.sh /app/prepare-mathlib.sh
COPY entrypoint.sh ./entrypoint.sh
WORKDIR /app/adapter
RUN uv sync --frozen --no-dev
RUN chmod +x /app/entrypoint.sh /app/prepare-mathlib.sh /usr/local/bin/lake
ENV LEA_ROOT=/app/prover LEA_SHARED_DATA_DIR=/app/data LEA_COMPANION_HOST=0.0.0.0 LEA_API_BASE_URL=http://127.0.0.1:8001
HEALTHCHECK --interval=1s --timeout=2s CMD .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/ready')"
ENTRYPOINT ["/app/entrypoint.sh"]
''')
        subprocess.run(['docker', 'build', '-q', '-t', image, str(context)], check=True, timeout=180)
        data = home / 'data'
        config = home / 'config'
        data.mkdir()
        config.mkdir()
        try:
            def launch():
                container = docker('run', '-d', '--name', 'lea-lifecycle-' + uuid4().hex[:12],
                    '-p', '127.0.0.1::8001', '-p', '127.0.0.1::31245', '-v', f'{data}:/app/data',
                    '-v', f'{config}:/app/config', image)
                containers.append(container)
                port = docker('port', container, '8001/tcp').split(':')[-1]
                return container, 'http://127.0.0.1:' + port

            container, base = launch()
            before = ready(base)
            companion_port = docker('port', container, '31245/tcp').split(':')[-1]
            companion_base = 'http://127.0.0.1:' + companion_port
            request(companion_base, '/health')
            assert before['restart_supported'] is True
            request(base, '/api/runtime/restart', {})
            after = ready(base, before['runtime_id'])
            request(companion_base, '/health')
            request(base, '/api/runtime/restart', {'shutdown': True})
            assert docker('wait', container) == '0'
            # The same volume can be owned by a subsequent adapter after shutdown.
            second, second_base = launch()
            reused = ready(second_base)
            request(second_base, '/api/runtime/restart', {'shutdown': True})
            assert docker('wait', second) == '0'
            failed = docker('run', '-d', '--name', 'lea-prepare-failure-' + uuid4().hex[:12],
                            '-e', 'LEA_TEST_PREPARE_EXIT=9', image)
            containers.append(failed)
            assert docker('wait', failed) == '9'
            failed_logs = docker('logs', failed)
            assert 'backends will not start' in failed_logs
            assert 'starting adapter' not in failed_logs
            third, third_base = launch()
            ready(third_base)
            docker('exec',third,'/app/adapter/.venv/bin/python','-c',
                "import os,signal; from pathlib import Path; "
                "pids=[int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit() "
                "and (p/'cmdline').exists() and (p/'cmdline').read_bytes().split(b'\\0')[:2]==[b'node',b'companion/server.mjs']]; "
                "assert len(pids)==1; os.kill(pids[0],signal.SIGKILL)")
            assert docker('wait',third) == '137'
            preparing = docker('run','-d','--name','lea-prepare-stop-'+uuid4().hex[:12],
                               '-e','LEA_TEST_PREPARE_SLEEP=30',image)
            containers.append(preparing)
            docker('stop','--time','5',preparing)
            assert docker('wait',preparing) == '0'
            print(json.dumps({'both_backends': True, 'preparation_failure_rejects_admission': True,
                'companion_exit_stops_adapter': True, 'preparation_signal_exits_cleanly': True,
                'restart_resumed_readiness': True, 'shutdown_exit_code': 0,
                'data_ownership_released': True, 'runtime_ids':
                [before['runtime_id'], after['runtime_id'], reused['runtime_id']]}))
        except BaseException:
            for container in containers:
                print(docker('logs', container))
            raise
        finally:
            for container in containers:
                docker('rm', '-f', container)
            docker('image', 'rm', image)


if __name__ == '__main__':
    main()
