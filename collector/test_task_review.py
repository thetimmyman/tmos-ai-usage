import hashlib
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
import task_runner as runner
import task_review as review

class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = 'private-task-identity'
        self.ref = hashlib.sha256(self.task.encode()).hexdigest()
        self.artifact = self.root/'private-output.txt'
    def run_task(self):
        return runner.run_task(self.root,'codex','synthetic-test',self.task,
            [sys.executable,'-c','from pathlib import Path;import sys;Path(sys.argv[1]).write_text("result")',str(self.artifact)],
            str(self.artifact),label='Example change')
    def verify(self):
        return runner.verify_task(self.root,self.task,'test-reviewer',[sys.executable,'-c','assert 2+2==4'],check_label='Example assertions')
    def decision(self, run, check, **kwargs):
        return review.decide(self.root,self.ref,'test-reviewer','accept',run['execution_id'],check['verification_sha256'],**kwargs)
    def test_projection_and_explicit_acceptance(self):
        run=self.run_task();check=self.verify()
        result=review.review_list(self.root);row=result['tasks'][0]
        self.assertTrue(row['can_accept']);self.assertEqual(row['label'],'Example change')
        self.assertEqual(row['verification']['check_label'],'Example assertions')
        for secret in [self.task,str(self.artifact),'argv_sha256','latest_execution_artifact_path']:
            self.assertNotIn(secret,json.dumps(result))
        with self.assertRaises(ValueError): self.decision(run,check)
        self.assertEqual(self.decision(run,check,semantic_accepted=True)['status'],'validated')
        self.assertTrue(self.decision(run,check,semantic_accepted=True)['duplicate'])
        self.assertFalse(review.review_list(self.root)['tasks'][0]['can_accept'])
    def test_stale_execution_and_verification_refused(self):
        old=self.run_task();oldcheck=self.verify();newcheck=self.verify()
        with self.assertRaises(ValueError): self.decision(old,oldcheck,semantic_accepted=True)
        self.run_task()
        with self.assertRaises(ValueError): self.decision(old,newcheck,semantic_accepted=True)
        with self.assertRaises(ValueError): review.decide(self.root,self.ref,'r','fail',old['execution_id'])
    def test_changed_artifact_and_receipt_refused(self):
        run=self.run_task();check=self.verify();self.artifact.write_text('changed')
        self.assertFalse(review.review_list(self.root)['tasks'][0]['can_accept'])
        with self.assertRaises(ValueError): self.decision(run,check,semantic_accepted=True)
        self.artifact.write_text('result')
        receipt=self.root/'task-runner'/('verification-'+check['verification_sha256']+'.json')
        receipt.write_text('{}')
        self.assertFalse(review.review_list(self.root)['tasks'][0]['can_accept'])
    def test_review_list_does_not_wait_for_running_task_and_decision_refuses_lock(self):
        run=self.run_task();check=self.verify()
        lock=runner._locked(self.root/'task-runner',self.ref)
        try:
            self.assertEqual(len(review.review_list(self.root)['tasks']),1)
            with self.assertRaises(BlockingIOError): self.decision(run,check,semantic_accepted=True)
        finally: os.close(lock)
    def test_fail_and_no_check_state(self):
        run=self.run_task();self.assertFalse(review.review_list(self.root)['tasks'][0]['can_accept'])
        result=review.decide(self.root,self.ref,'r','fail',run['execution_id'])
        self.assertEqual(result['status'],'failed')
        decision=json.loads(next((self.root/'task-runner').glob('decision-*.json')).read_text())
        self.assertEqual(decision['reviewer'],'r')
        self.assertEqual(decision['execution_id'],run['execution_id'])
    def test_bad_metadata_isolated_and_no_tasks_empty(self):
        self.assertEqual(review.review_list(self.root)['tasks'],[])
        self.run_task();path=self.root/'task-runner'/('task-'+self.ref+'.json')
        path.write_text('{bad')
        self.assertEqual(review.review_list(self.root)['unavailable'],1)
        with self.assertRaises(ValueError): review.decide(self.root,'../escape','r','fail','bad')
