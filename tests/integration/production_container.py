"""Validate the actual production image; never build a substitute Lean image.

Each container and mount belongs to this test. There are no model calls, fixed
host ports, production-volume writes, registry writes, or Docker-wide cleanup.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def docker(*args, timeout=120):
    return subprocess.check_output(['docker', *args], text=True, timeout=timeout,
                                   stderr=subprocess.STDOUT if args[0] == 'logs' else None).strip()


def request(base, endpoint, body=None):
    req = Request(base + endpoint, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Content-Type': 'application/json'} if body is not None else {})
    with urlopen(req, timeout=180) as response:
        return json.load(response)


def ready(base, previous=None, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = request(base, '/api/ready')
            if result['ready'] and result['runtime_id'] != previous:
                return result
        except OSError:
            pass
        time.sleep(.25)
    raise AssertionError('Container failed readiness')


def replace_source(container, home, path, content):
    """Assert the host edit is visible through Docker's mount before checking it.

    Docker Desktop mount propagation is asynchronous. Waiting for the actual
    source bytes (never for an API verdict) keeps the freshness assertion strict.
    """
    path.write_text(content)
    mounted = Path('/app/prover/workspace') / path.relative_to(home)
    docker('exec', container, '/app/adapter/.venv/bin/python', '-c',
           'import sys,time; from pathlib import Path; '
           'p=Path(sys.argv[1]); expected=sys.argv[2]; deadline=time.monotonic()+5; '
           '\nwhile p.read_text()!=expected and time.monotonic()<deadline: time.sleep(.05)'
           '\nassert p.read_text()==expected, "Host edit did not reach the mounted source"',
           str(mounted), content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    results = []
    containers = []
    runtime = None
    try:
        architecture = docker('image', 'inspect', args.image, '--format', '{{.Architecture}}')
        node = json.loads(docker('run', '--rm', '--entrypoint', '/usr/local/bin/node',
            args.image, '--print', 'JSON.stringify({architecture:process.arch,version:process.version})'))
        assert node['architecture'] == {'arm64':'arm64', 'amd64':'x64'}[architecture], node
        lean = docker('run', '--rm', '--workdir', '/app/prover/workspace',
            '--entrypoint', '/root/.elan/bin/lake', args.image, 'env', 'lean', '--version')
        assert '4.29.0' in lean and {'arm64':'aarch64', 'amd64':'x86_64'}[architecture] in lean, lean
        python = docker('run', '--rm', '--entrypoint', '/app/adapter/.venv/bin/python',
            args.image, '-c', 'import sys; print(sys.version.split()[0])')
        assert python.startswith('3.13.'), python
        runtime = {'image_architecture':architecture, 'node':node, 'lean':lean, 'python':python}
        print(json.dumps(runtime), flush=True)
        with tempfile.TemporaryDirectory(prefix='lea-production-container-') as tmp:
            for revision in ('0019_custom_tools', '0031_usage_details'):
                home = Path(tmp) / revision
                home.mkdir()
                mounts = []
                for source, target in [('data','/app/data'),('config','/app/config'),('proofs','/app/prover/workspace/proofs'),('projects','/app/prover/workspace/projects'),('overleaf','/app/apps/overleaf-extension/.overleaf-lean-stub')]:
                    (home / source).mkdir()
                    mounts += ['-v', f'{home / source}:{target}']
                docker('run','--rm',*mounts,'-v',f'{ROOT / "tests/integration/container_seed.py"}:/validation/seed.py:ro',
                       '-e','LEA_SHARED_DATA_DIR=/app/data','--workdir','/app/adapter',
                       '--entrypoint','/app/adapter/.venv/bin/python',args.image,'/validation/seed.py',revision)
                container = docker('run','-d','--name','lea-production-test-'+uuid4().hex[:12],
                    '-p','127.0.0.1::8001','-p','127.0.0.1::31245',*mounts,args.image)
                containers.append(container)
                base = 'http://127.0.0.1:' + docker('port',container,'8001/tcp').split(':')[-1]
                companion = 'http://127.0.0.1:' + docker('port',container,'31245/tcp').split(':')[-1]
                first = ready(base)
                assert first['restart_supported'] is True
                request(companion,'/health')
                with urlopen(base, timeout=5) as response:
                    assert b'<html' in response.read().lower()
                detail = request(base,'/api/sessions/s')
                assert detail['code_steps'][0]['check_status'] == 'ok'
                assert detail['code_steps'][0]['check_current'] is False
                assert detail['code_steps'][0]['code'].startswith('import Lea.Readiness.Helper')
                checked = request(base,'/api/sessions/s/lean-check',{'path':'Proof.lean'})
                assert checked['status'] == 'ok', checked
                assert request(base,'/api/sessions/s')['code_steps'][-1]['check_current'] is True
                verified = request(base,'/api/sessions/s/verify',{'path':'Proof.lean'})
                assert verified['status'] == 'ok' and verified['current'] is True, verified
                helper = home / 'proofs/Lea/Readiness/Helper.lean'
                original = helper.read_text()
                replace_source(container, home, helper, original.replace('by trivial','by bad_tactic'))
                stale = request(base,'/api/sessions/s')['code_steps'][-1]
                assert stale['check_current'] is False, stale
                broken = request(base,'/api/sessions/s/lean-check',{'path':'Proof.lean'})
                assert broken['status'] == 'error', broken
                replace_source(container, home, helper, original)
                assert request(base,'/api/sessions/s/lean-check',{'path':'Proof.lean'})['status'] == 'ok'
                assert request(base,'/api/sessions/s')['code_steps'][-1]['check_current'] is True
                proof = home / 'proofs/Lea/Readiness/Proof.lean'
                checked_source = proof.read_text()
                invalid_source = 'theorem overwritten : False := by trivial\n'
                replace_source(container, home, proof, invalid_source)
                assert request(base,'/api/sessions/s')['code_steps'][-1]['check_current'] is False
                try:
                    request(base,'/api/sessions/s/lean-check',{'path':'Proof.lean'})
                except HTTPError as exc:
                    assert exc.code == 409
                    assert json.load(exc)['detail']['code'] == 'proof_revision_changed'
                else:
                    raise AssertionError('External overwrite bypassed the revision guard')
                assert request(base,'/api/sessions/s')['code_steps'][-1]['code'] == checked_source
                # Save the explicit replacement as its own immutable revision,
                # then require a real compiler failure rather than old success.
                request(base,'/api/sessions/s/file',{'path':'Proof.lean','content':invalid_source})
                assert request(base,'/api/sessions/s/lean-check',{'path':'Proof.lean'})['status'] == 'error'
                request(base,'/api/sessions/s/file',{'path':'Proof.lean','content':checked_source})
                assert request(base,'/api/sessions/s/lean-check',{'path':'Proof.lean'})['status'] == 'ok'
                assert request(base,'/api/sessions/s')['code_steps'][-1]['check_current'] is True
                request(base,'/api/runtime/restart',{})
                second = ready(base,first['runtime_id'])
                request(companion,'/health')
                assert request(base,'/api/sessions/s')['code_steps'][-1]['check_current'] is True
                request(base,'/api/runtime/restart',{'shutdown':True})
                assert docker('wait',container) == '0'
                results.append({'from_revision':revision, 'both_backends':True, 'real_compiler':True,
                    'safe_verify':True,'source_overwrite_invalidates':True,'external_source_conflict_http_status':409,
                    'saved_invalid_revision_fails_compiler':True,'dependency_rebuild_failure':True,'history_preserved':True,
                    'restart_ready':second['runtime_id'] != first['runtime_id'], 'shutdown_exit_code':0})
            # Break one broad Mathlib artifact in a disposable container layer.
            # Startup must repair it before either backend starts accepting work.
            broken = docker('run','-d','--name','lea-cache-test-'+uuid4().hex[:12],
                '-p','127.0.0.1::8001','-p','127.0.0.1::31245','--entrypoint','bash',args.image,'-c',
                'rm /app/prover/workspace/.lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean; exec /app/entrypoint.sh')
            containers.append(broken)
            base = 'http://127.0.0.1:' + docker('port',broken,'8001/tcp').split(':')[-1]
            ready(base,timeout=600)
            logs = docker('logs',broken)
            assert 'Mathlib repair complete' in logs, logs
            request(base,'/api/runtime/restart',{'shutdown':True})
            assert docker('wait',broken) == '0'
            results.append({'partial_mathlib_cache_repaired_before_admission':True})
    except BaseException:
        for container in containers:
            print(docker('logs',container))
        raise
    finally:
        for container in containers:
            docker('rm','-f',container)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'image':args.image,'runtime':runtime,
                                          'completed_scenarios':results},indent=2)+'\n')
    print(json.dumps(results))


if __name__ == '__main__':
    main()
