import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import billing_ledger
import comparison_publish as publisher
import inference_reports
import outcome_ledger


START = "2026-08-01T00:00:00Z"
END = "2026-09-01T00:00:00Z"


class ComparisonPublishTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def _seed_outcomes(self, provider="codex", status="validated", pending=False):
        task_id = "task-" + provider
        suffix = provider
        events = [{"type": "task_registered", "event_id": "register-" + suffix, "task_id": task_id,
                   "provider": provider, "cohort": "fixes", "started_at": "2026-08-05T12:00:00Z"},
                  {"type": "turns_recorded", "event_id": "turns-" + suffix, "task_id": task_id,
                   "turns": 2, "errors": 1, "occurred_at": "2026-08-05T12:01:00Z"}]
        if not pending:
            occurred = "2026-08-05T12:03:00Z"
            final = {"type": "task_finalized", "event_id": "final-" + suffix, "task_id": task_id,
                     "status": status, "occurred_at": occurred}
            if status == "validated":
                proof = {"schema_version": 1, "task_id": task_id, "model_run_id": "run-1",
                         "verification_sha256": "1" * 64, "reviewer": "reviewer",
                         "attestation_sha256": "2" * 64, "accepted_at": "2026-08-05T12:02:00Z",
                         "blocking_checks_passed": True, "semantic_acceptance": True}
                evidence = self.state / "proof.json"
                evidence.write_text(json.dumps(proof, sort_keys=True, separators=(",", ":")) + "\n")
                final["evidence_path"] = str(evidence)
                final["evidence_sha256"] = hashlib.sha256(evidence.read_bytes()).hexdigest()
            events.append(final)
        outcome_ledger.ingest(self.state / "outcome-ledger.json", events)

    def _invoice(self, provider="codex", start="2026-07-01T00:00:00Z", end="2026-09-01T00:00:00Z"):
        source = self.state / (provider + "-invoice.bin")
        source.write_bytes(b"private original invoice evidence for " + provider.encode())
        record = {"id": "invoice-1", "provider": provider, "currency": "USD",
                  "paid_at": "2026-07-02T00:00:00Z", "base_usd": 24, "discount_usd": 5,
                  "tax_usd": 1, "fees_usd": 0, "paid_usd": 20,
                  "service_start": start, "service_end": end,
                  "source_ref": "private invoice source"}
        billing_ledger.import_invoice(self.state, record, source)
        return source

    def publish(self, providers=("codex",), **kwargs):
        return publisher.publish(self.state, START, END, "2026-08", "fixes", providers,
                                 attest_complete=kwargs.get("attest_complete", True),
                                 attested_by="test operator")

    def test_full_publication_verifies_projection_and_private_leaves(self):
        self._seed_outcomes()
        source = self._invoice()
        result = self.publish()
        document = json.loads((self.state / "economics.json").read_text())
        context = document["context"]
        refs = document["providers"]["codex"]
        projection = inference_reports.economics_projection(self.state, refs, context, "codex")
        self.assertEqual(projection["validated_tasks"], 1)
        self.assertEqual(projection["turns"], 2)
        self.assertEqual(projection["spend_usd"], 10.0)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual((self.state / "economics.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "evidence").stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(p.read_bytes() == source.read_bytes() for p in (self.state / "evidence").iterdir()))
        self.assertNotIn("task-1", (self.state / "economics.json").read_text())

    def test_attestation_is_mandatory_and_failed_publish_preserves_old_doc(self):
        self._seed_outcomes()
        self._invoice()
        self.publish()
        before = (self.state / "economics.json").read_bytes()
        with self.assertRaises(publisher.PublishError):
            self.publish(attest_complete=False)
        with self.assertRaises(publisher.PublishError):
            self.publish(providers=("codex", "command-code"))
        self.assertEqual((self.state / "economics.json").read_bytes(), before)

    def test_pending_and_noncovering_or_unknown_billing_intervals_refuse_publish(self):
        self._seed_outcomes(pending=True)
        self._invoice()
        with self.assertRaises(publisher.PublishError):
            self.publish()
        (self.state / "outcome-ledger.json").unlink()
        (self.state / "outcome-ledger.json.lock").unlink()
        (self.state / "outcome-ledger.json.evidence").mkdir()
        self._seed_outcomes()
        (self.state / "billing-ledger.json").unlink()
        (self.state / "billing.lock").unlink()
        self._invoice(start="2026-08-15T00:00:00Z")
        with self.assertRaises(publisher.PublishError):
            self.publish()
        (self.state / "billing-ledger.json").unlink()
        (self.state / "billing.lock").unlink()
        shutil.rmtree(self.state / "billing-evidence")
        self._invoice(start=None, end=None)
        with self.assertRaises(publisher.PublishError):
            self.publish()
        self.assertFalse((self.state / "economics.json").exists())

    def test_tampered_outcome_proof_refuses_publish(self):
        self._seed_outcomes()
        self._invoice()
        ledger = outcome_ledger._load(self.state / "outcome-ledger.json")
        proof_hash = ledger["tasks"][hashlib.sha256(b"task-codex").hexdigest()]["validation_evidence"][0]
        proof = self.state / "outcome-ledger.json.evidence" / (proof_hash + ".bin")
        proof.write_text("changed")
        with self.assertRaises(outcome_ledger.LedgerError):
            self.publish()
        self.assertFalse((self.state / "economics.json").exists())

    def test_one_invoice_evidence_cannot_be_credited_to_two_providers(self):
        self._seed_outcomes("codex")
        self._seed_outcomes("command-code")
        self._invoice("codex")
        self._invoice("command-code")
        ledger_path = self.state / "billing-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        digests = {row["provider"]: row["source_sha256"] for row in ledger["invoices"]}
        next(row for row in ledger["invoices"] if row["provider"] == "command-code")["source_sha256"] = digests["codex"]
        ledger_path.write_text(json.dumps(ledger))
        with self.assertRaisesRegex(publisher.PublishError, "multiple providers"):
            self.publish(("codex", "command-code"))
        self.assertFalse((self.state / "economics.json").exists())

    def test_unresolved_outcome_inbox_blocks_strict_publication(self):
        self._seed_outcomes()
        self._invoice()
        inbox = self.state / "outcome-events"
        inbox.mkdir()
        (inbox / "pending.jsonl").write_text('{"type":')
        with self.assertRaisesRegex(publisher.PublishError, "unresolved"):
            self.publish()
        self.assertFalse((self.state / "economics.json").exists())


if __name__ == "__main__":
    unittest.main()
