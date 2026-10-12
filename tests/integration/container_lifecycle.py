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
    return subprocess.check_output(['docker', *args], text=True, timeout=40).strip()


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
    image = 'lea-runtime-audit:proof-evidence-v1'
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
        shutil.copytree(ROOT / 'packages/lea-model-catalog', context / 'catalog')
        shutil.copyfile(app / 'docker/entrypoint.sh', context / 'entrypoint.sh')
        (context / 'Dockerfile').write_text('''FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
COPY prover ./prover
COPY adapter ./adapter
COPY catalog /packages/lea-model-catalog
COPY entrypoint.sh ./entrypoint.sh
WORKDIR /app/adapter
RUN uv sync --frozen --no-dev
RUN chmod +x /app/entrypoint.sh
ENV LEA_ROOT=/app/prover LEA_SHARED_DATA_DIR=/app/data
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
                    '-p', '127.0.0.1::8001', '-v', f'{data}:/app/data',
                    '-v', f'{config}:/app/config', image)
                containers.append(container)
                port = docker('port', container, '8001/tcp').split(':')[-1]
                return container, 'http://127.0.0.1:' + port

            container, base = launch()
            before = ready(base)
            assert before['restart_supported'] is True
            request(base, '/api/runtime/restart', {})
            after = ready(base, before['runtime_id'])
            request(base, '/api/runtime/restart', {'shutdown': True})
            assert docker('wait', container) == '0'
            # The same volume can be owned by a subsequent adapter after shutdown.
            second, second_base = launch()
            reused = ready(second_base)
            request(second_base, '/api/runtime/restart', {'shutdown': True})
            assert docker('wait', second) == '0'
            print(json.dumps({'restart_resumed_readiness': True, 'shutdown_exit_code': 0,
                'data_ownership_released': True, 'runtime_ids':
                [before['runtime_id'], after['runtime_id'], reused['runtime_id']]}))
        except BaseException:
            for container in containers:
                print(docker('logs', container))
            raise
        finally:
            for container in containers:
                docker('rm', '-f', container)


if __name__ == '__main__':
    main()
