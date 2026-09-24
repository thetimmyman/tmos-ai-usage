import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from types import SimpleNamespace
import inference_reports


class HistoryTest(unittest.TestCase):
    def run_report(self, directory, pages, account='user', max_pages=1):
        responses = [{'id': account}] + pages
        self.urls = []
        def get(url, key):
            self.urls.append(url)
            value = responses.pop(0)
            if isinstance(value, Exception): raise value
            return value
        client = SimpleNamespace(http_json=get, _now=lambda: datetime.now(timezone.utc), _iso=lambda d:d.isoformat())
        return inference_reports.cline_report(client, 'secret-key', 'https://example.com', max_pages, directory)

    def page(self, identity, tokens=5, cursor=None):
        return {'items':[{'id':identity, 'promptTokens':tokens, 'aiModelName':'model', 'metadata':'private prompt'}], 'nextToken':cursor}

    def test_resume_deduplicate_and_rescan_newest(self):
        with tempfile.TemporaryDirectory() as directory:
            a = self.run_report(directory, [self.page('a', cursor='next')])
            self.assertTrue(a['coverage']['backfill_pending'])
            b = self.run_report(directory, [self.page('b')])
            self.assertIn('cursor=next', self.urls[-1])
            self.assertEqual(b['metrics']['Input tokens'], 10)
            self.assertTrue(b['coverage']['endpoint_exhausted'])
            c = self.run_report(directory, [self.page('a', tokens=7)])
            self.assertNotIn('cursor=', self.urls[-1])
            self.assertEqual(c['metrics']['Observed billing records'], 2)
            self.assertEqual(c['metrics']['Input tokens'], 12)
            self.assertFalse(c['coverage']['complete'])
            raw = next(Path(directory).glob('cline-history-*.json')).read_text()
            for private in ['private prompt','secret-key','example.com']:
                self.assertNotIn(private, raw)
            self.assertEqual(next(Path(directory).glob('cline-history-*.json')).stat().st_mode & 0o777, 0o600)

    def test_failure_keeps_records_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_report(directory, [self.page('a', cursor='next')])
            b = self.run_report(directory, [RuntimeError('secret details')])
            self.assertEqual(b['metrics']['Observed billing records'], 1)
            self.assertIn('unavailable', b['note'])
            self.assertNotIn('secret details', json.dumps(b))
            self.run_report(directory, [self.page('b')])
            self.assertIn('cursor=next', self.urls[-1])

    def test_account_isolation_and_storage_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_report(directory, [self.page('a', cursor='next')])
            b = self.run_report(directory, [self.page('b')], account='other')
            self.assertEqual(b['metrics']['Observed billing records'], 1)
            self.assertNotIn('cursor=', self.urls[-1])
            with patch('cline_history.MAX_RECORDS',1):
                c = self.run_report(directory, [self.page('b')])
            self.assertEqual(c['metrics']['Observed billing records'], 1)
            self.assertIn('storage limit', c['note'])
            self.assertTrue(c['coverage']['backfill_pending'])

    def test_repeated_cursor_resets_without_dropping_records(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_report(directory, [self.page('a', cursor='same')])
            c = self.run_report(directory, [self.page('a', cursor='same')])
            self.assertIn('Repeated', c['note'])
            self.assertEqual(c['metrics']['Observed billing records'], 1)
            self.run_report(directory, [self.page('b')])
            self.assertNotIn('cursor=', self.urls[-1])

    def test_corrupt_or_symlinked_checkpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_report(directory, [self.page('a')])
            path = next(Path(directory).glob('cline-history-*.json'))
            path.write_text('{}')
            with self.assertRaises(ValueError): self.run_report(directory, [self.page('b')])
            path.unlink(); path.symlink_to('/dev/null')
            with self.assertRaises(OSError): self.run_report(directory, [self.page('b')])

    def test_checkpoint_fields_and_cursor_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            self.run_report(directory, [self.page('a')])
            path = next(Path(directory).glob('cline-history-*.json'))
            original = json.loads(path.read_text())
            bad = json.loads(json.dumps(original)); bad['cursor'] = 'x' * 8193
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError): self.run_report(directory, [self.page('b')])
            bad = json.loads(json.dumps(original)); next(iter(bad['records'].values()))['promptTokens'] = True
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError): self.run_report(directory, [self.page('b')])
            bad = json.loads(json.dumps(original)); next(iter(bad['records'].values()))['prompt'] = 'not allowed'
            path.write_text(json.dumps(bad))
            with self.assertRaises(ValueError): self.run_report(directory, [self.page('b')])
