"""Require the tested immutable binaries before an explicitly dispatched push."""
import argparse
import json
from pathlib import Path
import re
import subprocess

SCENARIOS={'0019_custom_tools','0031_usage_details'}
REHEARSAL={'distinct_strict_binaries':True,'same_database_and_workspace_volumes':True,
    'schema_downgrade':False,'backup_restore':False,'volume_recreation':False,
    'candidate_history_and_receipt_preserved':True,'prepared_publication_recovered':True,
    'uncertain_claim_suspended':True,'old_claim_fenced':True,'baseline_build_identity_restored':True}


def validate(baseline,candidate,rollback,source,architecture):
    for bundle in (baseline,candidate):
        assert bundle.get('passed') is True and bundle.get('native') is True,'Native qualification did not pass'
        assert bundle['host_architecture']==bundle['docker_architecture']==architecture,'Native architecture evidence differs from the release'
        assert {r.get('from_revision') for r in bundle['qualification']['scenarios']}==SCENARIOS,'Historical migration coverage is incomplete'
        for scenario in bundle['qualification']['scenarios']:
            assert all(value is True for key,value in scenario.items() if key!='from_revision'),'A production scenario failed'
        compiler=bundle['qualification']['compiler']
        assert all(compiler.get(key) is True for key in ('complete_mathlib','real_compilation','safeverify_accepts_proof','safeverify_rejects_sorry')),'Compiler/kernel qualification is incomplete'
        assert set(bundle['roles'])=={'api','worker','companion'},'Incomplete role bundle'
        for role in bundle['roles'].values():
            assert re.fullmatch(r'sha256:[0-9a-f]{64}',role['id']),'Invalid immutable role ID'
            assert role['architecture']==architecture and role['source']==bundle['source'],'Role provenance mismatch'
    assert candidate['source']==source,'Qualified source does not match the dispatched release'
    assert rollback.get('passed') is True and rollback.get('rehearsal')==REHEARSAL,'Rollback qualification did not pass'
    assert rollback.get('baseline')==baseline['roles'] and rollback.get('candidate')==candidate['roles'],'Rollback used a different bundle'
    assert all(baseline['roles'][role]['id']!=candidate['roles'][role]['id'] for role in ('api','worker')),'Rollback retagged identical binaries'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--source',required=True);parser.add_argument('--arch',choices=['amd64','arm64'],required=True)
    parser.add_argument('--role',choices=['api','worker','companion'],required=True);args=parser.parse_args()
    baseline,candidate,rollback=[json.loads((args.directory/(name+'.json')).read_text()) for name in ('baseline','candidate','rollback')]
    validate(baseline,candidate,rollback,args.source,args.arch)
    selected=candidate['roles'][args.role]
    info=json.loads(subprocess.check_output(['docker','image','inspect',selected['id']],text=True))[0]
    assert info['Id']==selected['id'] and info['Architecture']==args.arch,'Loaded binary differs from qualification'
    assert info['Config']['Labels']['org.opencontainers.image.revision']==args.source,'Loaded source differs from qualification'
    print(selected['id'])


if __name__=='__main__':main()
