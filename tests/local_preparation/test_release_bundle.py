"""Publication must select tested binaries and reject incomplete attestations."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('release_bundle',ROOT/'.github/scripts/verify-release-bundle.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)


def reports():
    bundles=[]
    for index,source in enumerate(('b'*40,'c'*40)):
        roles={role:{'id':'sha256:'+str(index*3+number+1)*64,'source':source,'architecture':'amd64'}
            for number,role in enumerate(('api','worker','companion'))}
        bundles.append({'source':source,'roles':roles,'passed':True,'native':True,
            'host_architecture':'amd64','docker_architecture':'amd64','qualification':{
                'scenarios':[{'from_revision':revision,'retained_history':True} for revision in ('0019_custom_tools','0031_usage_details')],
                'compiler':{'complete_mathlib':True,'real_compilation':True,'safeverify_accepts_proof':True,'safeverify_rejects_sorry':True}}})
    rollback={'passed':True,'baseline':copy.deepcopy(bundles[0]['roles']),'candidate':copy.deepcopy(bundles[1]['roles']),
        'rehearsal':{'distinct_strict_binaries':True,'same_database_and_workspace_volumes':True,'schema_downgrade':False,
            'backup_restore':False,'volume_recreation':False,'candidate_history_and_receipt_preserved':True,
            'prepared_publication_recovered':True,'uncertain_claim_suspended':True,'old_claim_fenced':True,'baseline_build_identity_restored':True}}
    return [*bundles,rollback]


class ReleaseBundleTests(unittest.TestCase):
    def validate(self,values):gate.validate(*values,'c'*40,'amd64')

    def test_qualified_pair_is_accepted(self):self.validate(reports())

    def test_different_source_is_rejected(self):
        values=reports();values[1]['source']='d'*40
        with self.assertRaises(AssertionError):self.validate(values)

    def test_emulated_qualification_is_rejected(self):
        values=reports();values[1]['native']=False
        with self.assertRaises(AssertionError):self.validate(values)

    def test_host_architecture_cannot_be_hidden_by_native_flag(self):
        values=reports();values[1]['host_architecture']='arm64'
        with self.assertRaises(AssertionError):self.validate(values)

    def test_wrong_role_architecture_is_rejected(self):
        values=reports();values[1]['roles']['worker']['architecture']='arm64'
        with self.assertRaises(AssertionError):self.validate(values)

    def test_failed_kernel_probe_is_rejected(self):
        values=reports();values[1]['qualification']['compiler']['safeverify_rejects_sorry']=False
        with self.assertRaises(AssertionError):self.validate(values)

    def test_missing_historical_migration_is_rejected(self):
        values=reports();values[0]['qualification']['scenarios'].pop()
        with self.assertRaises(AssertionError):self.validate(values)

    def test_rollback_of_another_candidate_is_rejected(self):
        values=reports();values[2]['candidate']['api']['id']='sha256:'+'f'*64
        with self.assertRaises(AssertionError):self.validate(values)

    def test_backup_restore_does_not_qualify_as_binary_rollback(self):
        values=reports();values[2]['rehearsal']['backup_restore']=True
        with self.assertRaises(AssertionError):self.validate(values)

    def test_retagging_identical_binaries_is_rejected(self):
        values=reports();values[1]['roles']['api']['id']=values[0]['roles']['api']['id']
        values[2]['candidate']=copy.deepcopy(values[1]['roles'])
        with self.assertRaises(AssertionError):self.validate(values)


if __name__=='__main__':unittest.main()
