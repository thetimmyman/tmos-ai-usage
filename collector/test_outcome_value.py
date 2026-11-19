import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

import outcome_value as subject


class OutcomeValueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        inbox = self.directory / 'outcome-events'
        inbox.mkdir()
        self.path = inbox / 'model-outcomes.json'
        self.now = datetime(2026, 4, 3, tzinfo=timezone.utc)
        self.doc = {'providers': [{'provider': 'codex', 'activity': {'price': {
            'source_kind': 'user', 'monthly_usd': 60}}}]}
        self.data = {'version': 1, 'start': '2026-04-02T00:00:00Z', 'end': '2026-04-03T00:00:00Z',
                     'activity': [{'model': 'Example A', 'provider': 'codex', 'turns': 3},
                                  {'model': 'Example B', 'provider': 'codex', 'turns': 1}],
                     'events': [self.event(1, 'Example A'), self.event(2, 'Example B')]}

    def event(self, n, model):
        return {'task_id': format(n, '064x'), 'provider': 'codex', 'model': model,
                'started_at': '2026-04-02T05:00:00Z', 'finished_at': '2026-04-02T06:00:00Z',
                'outcome': 'validated', 'validation_kind': 'merged-and-reviewed'}

    def read(self):
        self.path.write_text(json.dumps(self.data))
        subject.attach(self.doc, self.directory, self.now)
        return self.doc['providers'][0]['outcome_value']

    def test_one_fee_split_by_all_observed_turns(self):
        self.data['events'].append(dict(self.event(3, 'Example A'), outcome='rejected',
                                        validation_kind='review-blocked'))
        result = self.read()
        self.assertEqual(result['total']['usd_per_validated'], 1)
        a, b = result['models']
        self.assertEqual((a['sample'], a['validated'], a['usd_per_validated']), (2, 1, 1.5))
        self.assertEqual((b['turns_per_validated'], b['usd_per_validated']), (1, .5))
        self.assertTrue(a['provisional'])
        self.assertEqual(self.read(), result)

    def test_corrections_unknown_prices_and_unknown_turns(self):
        self.doc['providers'][0]['activity']['price'] = {'estimated': True, 'monthly_usd': 42}
        self.data['events'][0].update(outcome='rejected', validation_kind='audit-reopened')
        result = self.read()
        self.assertIsNone(result['models'][0]['turns_per_validated'])
        self.assertIsNone(result['models'][1]['usd_per_validated'])
        self.assertEqual(result['total']['turns_per_validated'], 4)
        self.doc['providers'][0]['activity']['price'] = {'source_kind': 'receipt', 'monthly_usd': 60}
        self.data['activity'][0]['turns'] = None
        result = self.read()
        self.assertIsNone(result['models'][1]['usd_per_validated'])
        self.assertIsNone(result['total']['turns_per_validated'])

    def test_duplicate_tasks_and_invalid_attestations_fail_closed(self):
        self.data['events'].append(self.data['events'][0])
        self.assertEqual(self.read()['status'], 'invalid')
        self.data['events'].pop()
        self.data['events'][0]['validation_kind'] = 'review-blocked'
        self.assertEqual(self.read()['status'], 'invalid')
