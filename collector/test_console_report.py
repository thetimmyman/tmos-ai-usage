import csv,io,json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import console_report as c

class ConsoleReportTest(unittest.TestCase):
    def blob(self,rows):
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=['id','provider','model','billing_source','cost_micro_cents','created_at',*c.TOKEN_FIELDS,'user_email'])
        writer.writeheader();writer.writerows(rows);return stream.getvalue().encode()
    def row(self,**kw):
        return {'id':'1','provider':'opencode','model':'demo','billing_source':'managed-inference','cost_micro_cents':'125000000','created_at':'2026-09-24T12:00:00Z',**{k:'10' for k in c.TOKEN_FIELDS},'user_email':'private@example.com',**kw}
    def test_exact_documented_units_dedup_privacy(self):
        r=c.parse(self.blob([self.row(),self.row()]))
        self.assertEqual(r['metrics']['Console billed USD (workspace)'],1.25)
        self.assertEqual(r['metrics']['Console records'],1)
        self.assertNotIn('private@example',json.dumps(r))
        self.assertNotIn('validated_tasks',r)
    def test_unknown_tokens_and_schema(self):
        r=c.parse(self.blob([self.row(input_tokens='')]))
        self.assertIsNone(r['metrics']['Console input tokens'])
        with self.assertRaises(ValueError):c.parse(b'wrong,headers\na,b')
        with self.assertRaises(ValueError):c.parse(self.blob([self.row(cost_micro_cents='nan')]))
        with self.assertRaises(ValueError):c.parse(self.blob([self.row(),self.row(output_tokens='11')]))

    def test_api_snapshot_orders_against_manual_exports_without_retaining_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(c.os.environ, {'OPENCODE_CONSOLE_SERVICE_KEY':'test-secret'}):
                with patch.object(c.urllib.request,'urlopen',return_value=io.BytesIO(self.blob([self.row()]))):
                    report=c.fetch(directory)
            self.assertEqual(report['source_modified_at'],c.timestamp(report['observed_at']).timestamp())
            stored=(Path(directory)/'console-summary.json').read_text()
            self.assertNotIn('test-secret',stored)
            self.assertNotIn('private@example',stored)
