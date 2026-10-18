"""Profile real checks in a disposable Lake project with read-only shared packages.

Run with the adapter Python: --output /tmp/lean-check-profile.json. Requires the
supported macOS confined executor. No provider calls, UI or production DB writes.
"""
import argparse
import cProfile
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import pstats
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CODE = '''namespace Lea.Misc
theorem multiple_of_even_is_even (n m : Nat) (hn : Even n) (hdiv : n ∣ m) : Even m := by
  rcases hn with ⟨k, rfl⟩
  rcases hdiv with ⟨c, rfl⟩
  exact ⟨k * c, Nat.add_mul k k c⟩
end Lea.Misc
'''


def profile(root, scenario='parity'):
    from lea import check_evidence, lsp_daemon, tools
    from lea.check_runtime import CheckBudget
    from lea.config import LeaConfig
    from lea.lean_checks import check_file
    from lea.runctx import run_context
    path = root / 'proofs/session-with-hyphen/Lea/Misc/Main.lean'
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ['LEA_OWNED_PROCESS_DIR'] = str(root / 'owned-processes')
    cases = []
    variants = [
        ('missing_even_import', 'import Mathlib.Data.Nat.Basic\n' + CODE, 'error'),
        ('unqualified_lemma', 'import Mathlib.Algebra.Group.Even\n' + CODE.replace('Nat.add_mul', 'add_mul'), 'error'),
        ('corrected_proof', 'import Mathlib.Algebra.Group.Even\n' + CODE, 'ok'),
        ('same_imports_edit', '-- new proof revision\nimport Mathlib.Algebra.Group.Even\n' + CODE, 'ok'),
        ('unchanged_warm', '-- new proof revision\nimport Mathlib.Algebra.Group.Even\n' + CODE, 'ok'),
    ]
    if scenario == 'quadratic':
        square = '''namespace Lea.Misc
theorem independent_square (x y : ℝ) :
    (x + y)^2 + (x - y)^2 = 2*x^2 + 2*y^2 := by ring
end Lea.Misc
'''
        variants = [
            ('definition_draft', 'import Mathlib.Data.Real.Basic\nnamespace Lea.Misc\ndef descent_energy (a x : ℝ) : ℝ := (1/2)*(x-a)^2\nend Lea.Misc\n', None),
            ('missing_real_import', 'import Mathlib.Tactic.Ring.RingNF\n' + square, 'error'),
            ('corrected_quadratic', 'import Mathlib.Data.Real.Basic\nimport Mathlib.Tactic.Ring.RingNF\n' + square, 'ok'),
            ('same_imports_edit', '-- new revision\nimport Mathlib.Data.Real.Basic\nimport Mathlib.Tactic.Ring.RingNF\n' + square, 'ok'),
            ('unchanged_warm', '-- new revision\nimport Mathlib.Data.Real.Basic\nimport Mathlib.Tactic.Ring.RingNF\n' + square, 'ok'),
        ]
    try:
        for name, code, expected in variants:
            path.write_text(code)
            stats = cProfile.Profile()
            start = time.monotonic()
            stats.enable()
            result = check_file(path, str(root), budget=CheckBudget(timeout=300 if scenario == 'quadratic' else 120))
            stats.disable()
            assert expected is None or result.status == expected, result.output
            top = sorted(pstats.Stats(stats).stats.items(), key=lambda item:item[1][3], reverse=True)[:20]
            cases.append({'case':name,'status':result.status,'wall_ms':round((time.monotonic()-start)*1000,2),
                'execution':check_evidence.public_execution(result.execution), 'detail':result.output[-2000:],
                'environment_files':len(result.execution.get('environment',{}).get('files',{})),
                'evidence_json_bytes':len(json.dumps(result.execution)),
                'profile':[{'function':key[2], 'file':Path(key[0]).name,'line':key[1],
                            'calls':value[1], 'self_seconds':round(value[2],4),
                            'cumulative_seconds':round(value[3],4)} for key,value in top]})
            print(json.dumps({'case':name,'status':result.status,'timings_ms':result.execution['timings_ms']}),flush=True)
        config = LeaConfig(model='offline', max_turns=1,
            selected_result={'declaration':'Lea.Misc.independent_square' if scenario == 'quadratic' else 'Lea.Misc.multiple_of_even_is_even','kind':'theorem'})
        with run_context(working_dir=str(path.parent), config=config):
            result = tools._generated_lean_check(str(path))
        assert not tools._lean_check_has_error(result), str(result)
        assert result.execution['selected_result']['user_name'] == config.selected_result['declaration']
        cases.append({'case':'compiler_selected_result','status':'ok',
                      'execution':check_evidence.public_execution(result.execution)})
    finally:
        lsp_daemon._shutdown_all()
    return {'version':1,'scenario':scenario,'captured_at':datetime.now(timezone.utc).isoformat(),
        'system':platform.system(),'architecture':platform.machine(),
        'isolation':'macOS Seatbelt; all writes confined to disposable project, shared packages read-only; network denied',
        'timing_semantics':'Inclusive phase times; preparation contains dependency resolution/build. Queue and cleanup may overlap other phase times. cProfile cumulative times are nested.',
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(check_evidence.__file__), ROOT/'apps/lea-standalone/prover/lea/lean_checks.py',
                      ROOT/'apps/lea-standalone/prover/lea/check_runtime.py', ROOT/'apps/lea-standalone/prover/lea/tools.py',
                      ROOT/'apps/lea-standalone/prover/lea/result_selection.py',
                      ROOT/'apps/lea-standalone/prover/lea/LibraryInspect.lean', Path(__file__).resolve()]},
        'cases':cases}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT/'apps/lea-standalone/prover/workspace')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--scenario', choices=['parity', 'quadratic'], default='parity')
    parser.add_argument('--prepared',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.prepared:
        args.output.write_text(json.dumps(profile(args.prepared, args.scenario),indent=2)+'\n')
        return
    from lea import execution
    if not execution.capability()['available']:
        raise SystemExit('Profiling requires the supported confined executor; no unconfined fallback.')
    with tempfile.TemporaryDirectory(prefix='lea-check-profile-') as temp:
        root=Path(temp).resolve()
        for name in ('lean-toolchain','lakefile.lean','lake-manifest.json'):
            shutil.copyfile(args.workspace/name,root/name)
        packages=root/'.lake/packages';packages.mkdir(parents=True)
        for package in (args.workspace/'.lake/packages').iterdir():
            (packages/package.name).symlink_to(package.resolve(),target_is_directory=True)
        (root/'tmp').mkdir()
        policy=execution.profile({'writable_roots':[str(root)]})
        output=root/'profile.json'
        env={**os.environ,'TMPDIR':str(root/'tmp'),'PYTHONDONTWRITEBYTECODE':'1',
             'LEA_MODEL_CATALOG_OFFLINE':'true','LITELLM_LOCAL_MODEL_COST_MAP':'True'}
        subprocess.run(['sandbox-exec','-p',policy,sys.executable,str(Path(__file__).resolve()),
            '--prepared',str(root),'--output',str(output),'--scenario',args.scenario],env=env,check=True,timeout=1200)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(output,args.output)
        result=json.loads(output.read_text())
        print(json.dumps({'saved':str(args.output),'cases':[
            {'case':c['case'],'timings_ms':c['execution'].get('timings_ms')} for c in result['cases']]}))


if __name__=='__main__':main()
