import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import outcome_ledger
import task_runner


def summary(root):
    return outcome_ledger.summarize_all(root / "outcome-ledger.json", "codex",
        "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z")


def python_cmd(code, *args):
    return [sys.executable, "-c", code, *map(str, args)]


class TaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_run_records_turn_before_execution_and_never_validates(self):
        tmp_path = self.root
        secret = "private-prompt-argv-output"
        cmd = [sys.executable, "collector/task_runner.py", "run", "--state-dir", str(tmp_path),
               "--provider", "codex", "--cohort", "tests", "--task-id", "task-1", "--",
               sys.executable, "-c", "print('" + secret + "')"]
        result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        data = summary(tmp_path)
        self.assertEqual(data["counts"], {"tasks": 1, "pending": 1, "validated": 0, "failed": 0,
            "abandoned": 0, "reworked": 0, "turns": 1, "errors": 0})
        receipt = next((tmp_path / "task-runner").glob("run-*.json")).read_text()
        self.assertNotIn(secret, receipt)
        self.assertIn("argv_sha256", json.loads(receipt))
        self.assertNotIn("stdout", receipt)


    def test_nonzero_cli_exit_is_pending_not_error_or_failed(self):
        tmp_path = self.root
        receipt = task_runner.run_task(tmp_path, "codex", "tests", "task-2",
                                       python_cmd("raise SystemExit(7)"))
        self.assertEqual(receipt["exit_code"], 7)
        self.assertEqual(summary(tmp_path)["counts"]["pending"], 1)
        self.assertEqual(summary(tmp_path)["counts"]["failed"], 0)
        self.assertEqual(summary(tmp_path)["counts"]["errors"], 0)


    def test_command_is_argv_without_shell_interpretation(self):
        tmp_path = self.root
        target = tmp_path / "literal;touch-owned"
        task_runner.run_task(tmp_path, "codex", "tests", "task-3",
            python_cmd("from pathlib import Path; Path(__import__('sys').argv[1]).write_text('ok')", target))
        self.assertEqual(target.read_text(), "ok")
        self.assertFalse((tmp_path / "touch-owned").exists())


    def test_terminal_retry_reopens_same_task_and_counts_rework(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-4", python_cmd("pass"))
        task_runner.finalize_task(tmp_path, "task-4", "abandoned")
        task_runner.run_task(tmp_path, "codex", "tests", "task-4", python_cmd("pass"))
        counts = summary(tmp_path)["counts"]
        self.assertEqual(counts["tasks"], 1)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["turns"], 2)
        self.assertEqual(counts["reworked"], 1)


    def test_verification_must_run_nonempty_successful_checks_but_stays_pending(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-5", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "nonempty"):
            task_runner.verify_task(tmp_path, "task-5", "reviewer", [])
        failed = task_runner.verify_task(tmp_path, "task-5", "reviewer", python_cmd("raise SystemExit(1)"))
        self.assertEqual(failed["exit_code"], 1)
        with self.assertRaisesRegex(ValueError, "successful verification"):
            task_runner.accept_task(tmp_path, "task-5", "reviewer")
        passed = task_runner.verify_task(tmp_path, "task-5", "reviewer", python_cmd("pass"))
        self.assertEqual(passed["exit_code"], 0)
        self.assertEqual(summary(tmp_path)["counts"]["pending"], 1)
        self.assertEqual(summary(tmp_path)["counts"]["validated"], 0)


    def test_accept_requires_latest_verification_and_emits_ledger_schema_idempotently(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-6", python_cmd("pass"))
        verification = task_runner.verify_task(tmp_path, "task-6", "reviewer", python_cmd("pass"))
        accepted = task_runner.accept_task(tmp_path, "task-6", "independent reviewer")
        self.assertEqual(accepted["status"], "validated")
        self.assertFalse(accepted["duplicate"])
        self.assertTrue(task_runner.accept_task(tmp_path, "task-6", "independent reviewer")["duplicate"])
        data = summary(tmp_path)
        self.assertEqual(data["counts"]["validated"], 1)
        self.assertEqual(data["counts"]["pending"], 0)
        ledger = outcome_ledger._load(tmp_path / "outcome-ledger.json")
        evidence_hash = ledger["tasks"][hashlib.sha256(b"task-6").hexdigest()]["validation_evidence"][0]
        evidence = json.loads(((tmp_path / "outcome-ledger.json.evidence") / (evidence_hash + ".bin")).read_text())
        self.assertEqual(evidence["model_run_id"], verification["execution_id"])
        self.assertEqual(evidence["verification_sha256"], verification["verification_sha256"])
        self.assertTrue(evidence["semantic_acceptance"] and evidence["blocking_checks_passed"])
        self.assertNotIn("argv", json.dumps(evidence))
        self.assertNotIn("output", json.dumps(evidence))
        self.assertEqual(os.stat(tmp_path / "task-runner").st_mode & 0o777, 0o700)

    def test_new_execution_invalidates_old_verification(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-7", python_cmd("pass"))
        task_runner.verify_task(tmp_path, "task-7", "reviewer", python_cmd("pass"))
        task_runner.run_task(tmp_path, "codex", "tests", "task-7", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "latest run"):
            task_runner.accept_task(tmp_path, "task-7", "reviewer")

    def test_accept_rejects_verification_if_task_command_never_started(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-9", [str(tmp_path / "missing-command")])
        with self.assertRaisesRegex(ValueError, "run the task"):
            task_runner.verify_task(tmp_path, "task-9", "reviewer", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "started run"):
            task_runner.accept_task(tmp_path, "task-9", "reviewer")

    def test_task_binding_cannot_change_provider_or_cohort(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-8", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "must match"):
            task_runner.run_task(tmp_path, "claude-code", "tests", "task-8", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "must match"):
            task_runner.run_task(tmp_path, "codex", "other", "task-8", python_cmd("pass"))

    def test_interrupted_new_run_invalidates_old_verification(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-10", python_cmd("pass"))
        task_runner.verify_task(tmp_path, "task-10", "reviewer", python_cmd("pass"))
        with mock.patch.object(task_runner, "_execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                task_runner.run_task(tmp_path, "codex", "tests", "task-10", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "started run"):
            task_runner.accept_task(tmp_path, "task-10", "reviewer")

    def test_interrupted_verification_invalidates_previous_success(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-11", python_cmd("pass"))
        task_runner.verify_task(tmp_path, "task-11", "reviewer", python_cmd("pass"))
        with mock.patch.object(task_runner, "_execute", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                task_runner.verify_task(tmp_path, "task-11", "reviewer", python_cmd("pass"))
        with self.assertRaisesRegex(ValueError, "successful verification"):
            task_runner.accept_task(tmp_path, "task-11", "reviewer")

    def test_tampered_verification_receipt_is_rejected(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-12", python_cmd("pass"))
        record = task_runner.verify_task(tmp_path, "task-12", "reviewer", python_cmd("pass"))
        receipt = tmp_path / "task-runner" / ("verification-" + record["verification_sha256"] + ".json")
        receipt.write_text("{}")
        with self.assertRaisesRegex(ValueError, "changed"):
            task_runner.accept_task(tmp_path, "task-12", "reviewer")

    def test_optional_artifact_must_remain_identical_through_acceptance(self):
        tmp_path = self.root
        artifact = tmp_path / "output.patch"
        code = "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('patch')"
        run = task_runner.run_task(tmp_path, "codex", "tests", "task-13", python_cmd(code, artifact), artifact)
        self.assertTrue(run["artifact_bound"])
        task_runner.verify_task(tmp_path, "task-13", "reviewer", python_cmd("pass"))
        artifact.write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            task_runner.accept_task(tmp_path, "task-13", "reviewer")

    def test_accept_recovers_after_ledger_commit_before_metadata_write(self):
        tmp_path = self.root
        task_runner.run_task(tmp_path, "codex", "tests", "task-14", python_cmd("pass"))
        task_runner.verify_task(tmp_path, "task-14", "reviewer", python_cmd("pass"))
        key = hashlib.sha256(b"task-14").hexdigest()
        meta_path = tmp_path / "task-runner" / ("task-" + key + ".json")
        atomic = task_runner._atomic

        def fail_acceptance_meta(path, raw):
            if Path(path) == meta_path and json.loads(raw).get("acceptance"):
                raise OSError("simulated crash after ledger commit")
            return atomic(path, raw)

        with mock.patch.object(task_runner, "_atomic", side_effect=fail_acceptance_meta):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                task_runner.accept_task(tmp_path, "task-14", "reviewer")
        recovered = task_runner.accept_task(tmp_path, "task-14", "reviewer")
        self.assertTrue(recovered["duplicate"])
        self.assertTrue(recovered["recovered"])

    def test_cli_fail_and_abandon_map_to_ledger_statuses(self):
        for task_id, action, expected in (("task-15", "fail", "failed"),
                                          ("task-16", "abandon", "abandoned")):
            task_runner.run_task(self.root, "codex", "tests", task_id, python_cmd("pass"))
            result = subprocess.run([sys.executable, "collector/task_runner.py", action,
                "--state-dir", str(self.root), "--task-id", task_id],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        ledger = outcome_ledger._load(self.root / "outcome-ledger.json")
        for task_id, _, expected in (("task-15", "fail", "failed"), ("task-16", "abandon", "abandoned")):
            record = ledger["tasks"][hashlib.sha256(task_id.encode()).hexdigest()]
            self.assertEqual(record["status"], expected)
