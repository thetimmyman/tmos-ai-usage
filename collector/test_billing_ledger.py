import json
import os
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path
import billing_ledger as b

class BillingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.evidence = self.directory/'receipt'; self.evidence.write_text('Synthetic invoice evidence')
        self.row = dict(id='invoice-1', provider='codex', currency='USD', paid_at='2026-09-23T12:47:17Z',
                        base_usd=100, discount_usd=10, tax_usd=5, paid_usd=95,
                        service_start=None, service_end=None, source_ref='private-source')
    def read(self):
        return b.summary(self.directory,'codex','2026-09-01T00:00:00Z','2026-10-01T00:00:00Z')
    def test_receipt_reconciliation_unknown_interval_and_privacy(self):
        self.assertTrue(b.import_invoice(self.directory,self.row,self.evidence))
        self.assertFalse(b.import_invoice(self.directory,self.row,self.evidence))
        r=self.read();self.assertEqual(r['cash_paid_usd'],95)
        self.assertEqual(r['unknown_service_periods'],1)
        self.assertIsNone(r['allocated_known_usd'])
        self.assertFalse(r['coverage_complete'])
        self.assertNotIn('private-source',json.dumps(r))
        self.assertEqual((self.directory/'billing-ledger.json').stat().st_mode & 0o777,0o600)
        with self.assertRaises(ValueError):b.import_invoice(self.directory,{**self.row,'paid_usd':200},self.evidence)
    def test_service_allocation_and_payment_boundaries(self):
        r={**self.row,'paid_at':'2026-08-01T00:00:00Z','base_usd':365,'discount_usd':0,'tax_usd':0,'paid_usd':365,
           'service_start':'2026-08-01T00:00:00Z','service_end':'2027-08-01T00:00:00Z'}
        b.import_invoice(self.directory,r,self.evidence)
        result=self.read();self.assertEqual(result['cash_paid_usd'],0)
        self.assertEqual(result['allocated_known_usd'],30)
        self.assertFalse(result['coverage_complete'])
    def test_invalid_and_conflicting_inputs(self):
        for amount in [True,-1,float('nan'),'1.001','1e100']:
            with self.assertRaises(ValueError):b.money(amount)
        for start,end in [('2026-09-01', '2026-10-01'),('2026-09-01T00:00:00Z',None),(0,False)]:
            with self.assertRaises(ValueError):b.normalize({**self.row,'source_sha256':'a'*64,'service_start':start,'service_end':end})
        b.import_invoice(self.directory,self.row,self.evidence)
        with self.assertRaises(ValueError):b.import_invoice(self.directory,{**self.row,'paid_at':'2026-09-24T00:00:00Z'},self.evidence)
    def test_corruption_and_missing_data(self):
        self.assertIsNone(self.read()['cash_paid_usd'])
        b.import_invoice(self.directory,self.row,self.evidence)
        source=next((self.directory/'billing-evidence').iterdir());source.write_text('changed')
        with self.assertRaises(ValueError):self.read()
        with self.assertRaises(ValueError):b.import_invoice(self.directory,self.row,self.evidence)
        doc={'providers':[{'provider':'codex'}]};b.attach(doc,self.directory)
        self.assertFalse(doc['providers'][0]['billing']['coverage_complete'])

    def test_evidence_is_nonempty_bounded_and_regular(self):
        self.evidence.write_bytes(b'')
        with self.assertRaises(ValueError):b.import_invoice(self.directory,self.row,self.evidence)
        self.evidence.write_bytes(b'1234')
        with patch.object(b,'MAX_EVIDENCE_BYTES',3):
            with self.assertRaises(ValueError):b.import_invoice(self.directory,self.row,self.evidence)
        fifo=self.directory/'fifo';os.mkfifo(fifo)
        with self.assertRaises(ValueError):b.import_invoice(self.directory,self.row,fifo)
        self.assertFalse((self.directory/'billing-ledger.json').exists())

    def test_retained_evidence_is_also_bounded(self):
        b.import_invoice(self.directory,self.row,self.evidence)
        with patch.object(b,'MAX_EVIDENCE_BYTES',3):
            with self.assertRaises(ValueError):self.read()
