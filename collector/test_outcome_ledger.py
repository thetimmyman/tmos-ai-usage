import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outcome_ledger as ledger


class OutcomeLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "private" / "outcomes.json"
        self.start = "2026-09-01T00:00:00-04:00"
        self.evidence = self.root / "validation.json"
        self.evidence.write_text(json.dumps({"schema_version": 1, "task_id": "task-A",
            "model_run_id": "run-1", "verification_sha256": "1" * 64,
            "reviewer": "reviewer-1", "attestation_sha256": "2" * 64,
            "accepted_at": "2026-09-24T11:59:00Z", "blocking_checks_passed": True,
            "semantic_acceptance": True}) + "\n")
        self.evidence_hash = hashlib.sha256(self.evidence.read_bytes()).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, task="task-A", event_id="e-register", started_at=None):
        return {"type": "task_registered", "event_id": event_id, "task_id": task,
                "provider": "codex", "cohort": "bounded-fixes", "started_at": started_at or self.start}

    def finalize(self, task="task-A", event_id="e-final", status="validated", occurred_at="2026-09-24T12:00:00Z"):
        event = {"type": "task_finalized", "event_id": event_id, "task_id": task,
                 "status": status, "occurred_at": occurred_at}
        if status == "validated":
            event.update(evidence_path=str(self.evidence), evidence_sha256=self.evidence_hash)
        return event

    def test_idempotent_flow_rework_and_observed_half_open_summary(self):
        events = [self.register(), self.register(task="task-B", event_id="e-register-B",
                                                  started_at="2026-10-01T00:00:00Z"),
                  {"type": "turns_recorded", "event_id": "e-turn-1", "task_id": "task-A",
                   "turns": 2, "errors": 1, "occurred_at": "2026-09-24T11:00:00-04:00"},
                  self.finalize(occurred_at="2026-09-24T16:00:00Z"),
                  {"type": "task_reopened", "event_id": "e-reopen", "task_id": "task-A",
                   "occurred_at": "2026-09-25T00:00:00Z"},
                  {"type": "turns_recorded", "event_id": "e-turn-2", "task_id": "task-A",
                   "turns": 1, "errors": 2, "occurred_at": "2026-09-25T01:00:00Z"},
                  self.finalize(event_id="e-final-2", status="failed", occurred_at="2026-09-25T02:00:00Z")]
        result = ledger.ingest(self.path, events)
        self.assertEqual(result, {"applied": 7, "duplicates": 0})
        self.assertEqual(ledger.ingest(self.path, events), {"applied": 0, "duplicates": 7})
        summary = ledger.summarize(self.path, "codex", "bounded-fixes",
                                   "2026-09-01T04:00:00Z", "2026-10-01T00:00:00Z")
        self.assertEqual(summary["counts"], {"tasks": 1, "pending": 0, "validated": 0,
                         "failed": 1, "abandoned": 0, "reworked": 1, "turns": 3, "errors": 3})
        self.assertEqual(summary["coverage"], {"kind": "observed ledger events", "complete": False})
        self.assertNotIn("task-A", json.dumps(summary))
        stored = self.path.read_text()
        self.assertNotIn("task-A", stored)
        self.assertNotIn(str(self.evidence), stored)
        self.assertNotIn("semantic_acceptance", stored)
        retained = self.path.with_name(self.path.name + ".evidence") / (self.evidence_hash + ".bin")
        self.assertEqual(retained.read_bytes(), self.evidence.read_bytes())
        self.assertEqual(retained.stat().st_mode & 0o777, 0o600)
        self.evidence.unlink()
        self.assertEqual(ledger.ingest(self.path, [self.finalize(occurred_at="2026-09-24T16:00:00Z")]),
                         {"applied": 0, "duplicates": 1})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.with_name(self.path.name + ".lock").stat().st_mode & 0o777, 0o600)

    def test_success_or_completion_cannot_be_validation(self):
        ledger.ingest(self.path, [self.register()])
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [{"type": "assistant_completed", "event_id": "e1", "task_id": "task-A"}])
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [{"type": "task_finalized", "event_id": "e2", "task_id": "task-A",
                                      "status": "validated", "occurred_at": "2026-09-24T12:00:00Z"}])
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [{"type": "task_finalized", "event_id": "e3", "task_id": "task-A",
                                      "status": "validated", "occurred_at": "2026-09-24T12:00:00Z",
                                      "evidence_path": str(self.evidence), "evidence_sha256": "0" * 64}])
        self.assertEqual(ledger.summarize(self.path, "codex", "bounded-fixes",
                       "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")["counts"]["pending"], 1)

    def test_conflicting_event_id_rolls_back_entire_batch(self):
        event = self.register()
        ledger.ingest(self.path, [event])
        before = self.path.read_bytes()
        duplicate_but_conflicting = {**event, "provider": "codex"}
        batch = [{"type": "turns_recorded", "event_id": "new-turn", "task_id": "task-A",
                  "turns": 4, "errors": 0, "occurred_at": "2026-09-24T12:00:00Z"},
                 {**duplicate_but_conflicting, "provider": "codex", "cohort": "other"}]
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, batch)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ledger.summarize(self.path, "codex", "bounded-fixes",
                       "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")["counts"]["turns"], 0)

    def test_transitions_and_timezone_validation(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [self.register(started_at="2026-09-01T00:00:00")])
        ledger.ingest(self.path, [self.register()])
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [{"type": "task_reopened", "event_id": "early", "task_id": "task-A",
                                      "occurred_at": "2026-09-24T00:00:00Z"}])
        ledger.ingest(self.path, [self.finalize(status="abandoned")])
        ledger.ingest(self.path, [{"type": "task_reopened", "event_id": "reopen", "task_id": "task-A",
                                  "occurred_at": "2026-09-25T00:00:00Z"}])
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [{"type": "task_finalized", "event_id": "bad-status", "task_id": "task-A",
                                      "status": "success", "occurred_at": "2026-09-25T01:00:00Z"}])

    def test_monotonic_event_times_and_historical_snapshot_guard(self):
        ledger.ingest(self.path, [self.register()])
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [{"type": "task_finalized", "event_id": "before-start",
                "task_id": "task-A", "status": "failed", "occurred_at": "2026-08-31T23:59:59Z"}])
        ledger.ingest(self.path, [self.finalize(status="failed")])
        with self.assertRaises(ledger.LedgerError):
            ledger.summarize(self.path, "codex", "bounded-fixes", "2026-09-01T00:00:00Z",
                             "2026-09-24T11:59:59Z")

    def test_fractional_timestamp_interval_and_summary_all_by_cohort(self):
        a = self.register("task-A", "reg-A", "2026-09-24T12:00:00.100Z")
        b = self.register("task-B", "reg-B", "2026-09-24T12:00:00.110Z")
        b["cohort"] = "other-cohort"
        ledger.ingest(self.path, [a, b])
        start, end = "2026-09-24T12:00:00.090Z", "2026-09-24T12:00:00.105Z"
        only_a = ledger.summarize(self.path, "codex", "bounded-fixes", start, end)
        self.assertEqual(only_a["counts"]["tasks"], 1)
        all_cohorts = ledger.summarize_all(self.path, "codex", start, "2026-09-24T12:00:00.120Z")
        self.assertEqual(all_cohorts["counts"]["tasks"], 2)
        self.assertEqual(set(all_cohorts["by_cohort"]), {"bounded-fixes", "other-cohort"})

    def test_one_evidence_receipt_cannot_validate_distinct_tasks(self):
        task_b = self.register("task-B", "reg-B")
        ledger.ingest(self.path, [self.register(), task_b, self.finalize()])
        second = self.finalize("task-B", "final-B")
        with self.assertRaises(ledger.LedgerError):
            ledger.ingest(self.path, [second])
        summary = ledger.summarize(self.path, "codex", "bounded-fixes",
                                   "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        self.assertEqual(summary["counts"]["validated"], 1)
        self.assertEqual(summary["counts"]["pending"], 1)

    def test_summary_fails_closed_on_missing_or_tampered_retained_proof(self):
        ledger.ingest(self.path, [self.register(), self.finalize()])
        retained = self.path.with_name(self.path.name + ".evidence") / (self.evidence_hash + ".bin")
        retained.write_text("tampered")
        with self.assertRaises(ledger.LedgerError):
            ledger.summarize(self.path, "codex", "bounded-fixes",
                             "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
        retained.write_bytes(self.evidence.read_bytes())
        retained.unlink()
        with self.assertRaises(ledger.LedgerError):
            ledger.summarize(self.path, "codex", "bounded-fixes",
                             "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")

    def test_ledger_rejects_unsafe_permissions_and_summary_keeps_ids_private(self):
        ledger.ingest(self.path, [self.register()])
        os.chmod(self.path, 0o644)
        with self.assertRaises(ledger.LedgerError):
            ledger.summarize(self.path, "codex", "bounded-fixes",
                             "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
