import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import report_ingest as r
from test_request_log_import import row

class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.inbox=self.root/'imports';self.inbox.mkdir()
    def put(self,name,id='one'):
        p=self.inbox/name;p.write_text(json.dumps({'items':[row(id=id)],'truncated':False,'until':3000}));return p
    def test_idempotent_private_and_repair(self):
        p=self.put('one.json');original=p.read_bytes()
        self.assertEqual(r.ingest(self.root)['imported'],1)
        (self.root/'request-summary.json').unlink()
        self.assertEqual(r.ingest(self.root)['unchanged'],1)
        summary=json.loads((self.root/'request-summary.json').read_text())
        self.assertEqual(summary['totals']['requests'],1)
        self.assertEqual(p.read_bytes(),original)
        self.assertEqual((self.root/'import-index.json').stat().st_mode & 0o777,0o600)
        self.assertNotIn('private-value',(self.root/'request-ledger.json').read_text())
    def test_malformed_symlink_and_limits_do_not_block_valid(self):
        self.put('valid.json');(self.inbox/'bad.json').write_text('oops')
        (self.inbox/'link.json').symlink_to(self.inbox/'valid.json')
        result=r.ingest(self.root);self.assertEqual(result['imported'],1);self.assertEqual(result['rejected'],2)
        self.put('big.json')
        with patch.object(r,'MAX_BYTES',3):self.assertGreater(r.ingest(self.root)['rejected'],0)
    def test_rotation_progress(self):
        for n in range(5):self.put(str(n)+'.json',str(n))
        with patch.object(r,'MAX_FILES',2):
            for _ in range(3):r.ingest(self.root)
        self.assertEqual(json.loads((self.root/'request-summary.json').read_text())['totals']['requests'],5)
    def test_crash_after_ledger_before_summary(self):
        self.put('one.json')
        original=r.atomic_json
        def fail(path,value):
            if path.name=='request-summary.json':raise OSError('test crash')
            original(path,value)
        with patch.object(r,'atomic_json',side_effect=fail):self.assertEqual(r.ingest(self.root)['rejected'],1)
        self.assertEqual(r.ingest(self.root)['imported'],1)
        self.assertEqual(json.loads((self.root/'request-summary.json').read_text())['totals']['requests'],1)

    def test_csv_with_bad_json_is_idempotent_and_recovers(self):
        from test_console_report import ConsoleReportTest
        fixture=ConsoleReportTest()
        (self.inbox/'usage.csv').write_bytes(fixture.blob([fixture.row()]))
        (self.inbox/'bad.json').write_text('invalid')
        self.assertEqual(r.ingest(self.root)['imported'],1)
        self.assertEqual(r.ingest(self.root)['unchanged'],1)
        (self.root/'console-summary.json').unlink()
        self.assertEqual(r.ingest(self.root)['imported'],1)

    def test_old_csv_does_not_replace_newer_api_snapshot(self):
        import os
        from test_console_report import ConsoleReportTest
        fixture=ConsoleReportTest()
        source=self.inbox/'usage.csv'
        source.write_bytes(fixture.blob([fixture.row()]))
        os.utime(source,(100,100))
        prior={'source_modified_at':200,'metrics':{'Console records':42}}
        (self.root/'console-summary.json').write_text(json.dumps(prior))
        self.assertEqual(r.ingest(self.root)['imported'],1)
        self.assertEqual(json.loads((self.root/'console-summary.json').read_text()),prior)

    def test_future_csv_is_rejected_without_replacing_snapshot(self):
        import os,time
        from test_console_report import ConsoleReportTest
        fixture=ConsoleReportTest();source=self.inbox/'usage.csv'
        source.write_bytes(fixture.blob([fixture.row()]))
        future=time.time()+86400;os.utime(source,(future,future))
        self.assertEqual(r.ingest(self.root)['rejected'],1)
        self.assertFalse((self.root/'console-summary.json').exists())
