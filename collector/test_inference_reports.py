import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
import inference_reports as reports

class ReportsTest(unittest.TestCase):
    def client(self, responses):
        def get(url, key):
            self.urls.append(url)
            return responses.pop(0)
        self.urls = []
        return SimpleNamespace(http_json=get, _now=lambda: datetime.now(timezone.utc), _iso=lambda x: x.isoformat())
    def test_command_allowlist_and_scope(self):
        c = self.client([{'org': {'id': 'a&b'}}, {'totalCount': 3, 'completedCount': 2, 'failedCount': 1, 'totalCredits': 4, 'secret': 'never'}])
        r = reports.command_report(c, 'secret', 'https://example.com', {'currentPeriodStart': '2026-09-01T00:00Z'})
        self.assertIn('orgId=a%26b', self.urls[-1])
        self.assertEqual(r['metrics']['Successful requests'], 2)
        self.assertNotIn('secret', json.dumps(r))
        self.assertNotIn('validated_tasks', r)
    def test_cline_partial_metrics_and_privacy(self):
        c = self.client([{'data': {'id': 'a/b'}}, {'data': {'items': [{'id': '1', 'promptTokens': 5, 'costUsd': .2, 'metadata': 'secret'}, {'id': '2', 'promptTokens': 6}]}}])
        r = reports.cline_report(c, 'key', 'https://example.com')
        self.assertIn('a%2Fb', self.urls[-1])
        self.assertEqual(r['metrics']['Input tokens'], 11)
        self.assertIsNone(r['metrics']['Reported cost (unverified units)'])
        self.assertNotIn('secret', json.dumps(r))
    def test_cline_cursor_deduplication_and_repeated_page(self):
        page = {'items': [{'id':'1', 'promptTokens':5}], 'nextToken':'a&b'}
        c = self.client([{'id':'u'}, page, {'items':[{'id':'1','promptTokens':5},{'id':'2','promptTokens':7}], 'nextToken':'next'}, {'items':[{'id':'2','promptTokens':7}], 'nextToken':'next'}])
        r = reports.cline_report(c,'key','https://example.com')
        self.assertIn('cursor=a%26b',self.urls[2])
        self.assertEqual(r['metrics']['Input tokens'],12)
        self.assertEqual(r['metrics']['Observed billing records'],2)
        self.assertFalse(r['collection']['endpoint_exhausted'])
        self.assertIn('Repeated',r['note'])
    def test_cline_page_failure_preserves_results(self):
        c = self.client([{'id':'u'}, {'items':[{'id':'1','promptTokens':5}], 'nextToken':'next'}])
        r = reports.cline_report(c,'key','https://example.com')
        self.assertEqual(r['metrics']['Input tokens'],5)
        self.assertFalse(r['collection']['endpoint_exhausted'])
        self.assertIn('unavailable',r['note'])
    def test_unknown_numeric(self):
        for n in [True, -1, float('nan'), '3', None]: self.assertIsNone(reports.numeric(n))
    @patch("pi_activity.scan", return_value={})
    def test_missing_local_data(self, _scan):
        with tempfile.TemporaryDirectory() as d:
            doc = {'providers':[{'provider':'opencode-go'}]}
            reports.attach_local(doc, d)
            self.assertNotIn('economics_context', doc)
            Path(d, 'economics.json').write_text('null')
            reports.attach_local(doc, d)
            self.assertNotIn('economics_context', doc)

class EvidenceTest(unittest.TestCase):
    @patch("pi_activity.scan", return_value={})
    def test_receipts_projection_and_tamper(self, _scan):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'evidence').mkdir()
            context = {'period': '2026-09', 'cohort': 'fixes'}
            common = {**context, 'provider': 'codex', 'coverage_complete': True}
            outcomes = {**common, 'population_task_ids': ['one', 'two'], 'tasks': [
                {'id': 'one', 'status': 'validated', 'turns': 3, 'reworked': True, 'evidence_sha256': 'a'*64},
                {'id': 'two', 'status': 'failed', 'turns': 1, 'reworked': False, 'evidence_sha256': 'b'*64}]}
            spend = {**common, 'basis': 'recognized_subscription_and_metered_usd', 'charges': [
                {'id': 'bill', 'recognized_usd': 10, 'evidence_sha256': 'c'*64}]}
            for entry in outcomes['tasks'] + spend['charges']:
                raw = json.dumps({'synthetic_evidence_for':entry['id']}).encode()
                digest = hashlib.sha256(raw).hexdigest()
                entry['evidence_sha256'] = digest
                (directory / 'evidence' / (digest+'.json')).write_bytes(raw)
            refs = {}
            for kind, obj in [('outcomes', outcomes), ('spend', spend)]:
                raw = json.dumps(obj).encode()
                digest = hashlib.sha256(raw).hexdigest()
                refs[kind+'_sha256'] = digest
                (directory / 'evidence' / (digest+'.json')).write_bytes(raw)
            (directory / 'economics.json').write_text(json.dumps({'context': {**context, 'secret':'not exported'}, 'providers': {'codex': {**refs, 'secret':'not exported'}}}))
            doc = {'providers': [{'provider':'codex'}]}
            reports.attach_local(doc, directory)
            e = doc['providers'][0]['economics']
            self.assertEqual((e['validated_tasks'],e['failed_tasks'],e['reworked_tasks'],e['turns'],e['spend_usd']), (1,1,1,4,10))
            self.assertNotIn('secret', json.dumps(doc))
            (directory / 'evidence' / (refs['spend_sha256']+'.json')).write_text('{}')
            reports.attach_local(doc, directory)
            self.assertIsNone(doc['providers'][0]['economics'])

if __name__ == '__main__': unittest.main()
