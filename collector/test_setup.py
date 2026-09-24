"""Stdlib-only tests for the onboarding CLI."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup as onboarding


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp_path = Path(self.temp.name)
        self._env_patches = []

    def tearDown(self):
        for patcher in reversed(self._env_patches):
            patcher.stop()

    def set_env(self, **values):
        patcher = mock.patch.dict(os.environ, values, clear=False)
        patcher.start()
        self._env_patches.append(patcher)

    def set_home(self, path):
        self.set_env(HOME=str(path))

    def invoke(self, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = onboarding.main(list(args))
        output = stdout.getvalue().strip()
        self.assertEqual(stderr.getvalue(), "", "CLI output should be consumable from stdout only")
        return code, json.loads(output) if output else None

    def test_status_is_read_only_and_has_full_shape(self):
        self.set_home(self.tmp_path / "home")
        state = self.tmp_path / "state"
        code, result = self.invoke("status", "--state-dir", str(state))
        self.assertEqual(code, 0)
        self.assertEqual(result["schema_version"], 1)
        self.assertFalse(result["initialized"])
        self.assertFalse(result["completed"])
        self.assertEqual(set(result), {"schema_version", "initialized", "completed", "task_command",
                                       "pi_observer", "state_dir", "reports_dir", "outcome_dir", "message"})
        self.assertEqual(result["reports_dir"], str(state / "imports"))
        self.assertEqual(result["outcome_dir"], str(state / "outcome-events"))
        self.assertFalse(state.exists())

    def test_initialize_is_private_idempotent_and_preserves_other_state(self):
        self.set_home(self.tmp_path / "home")
        state = self.tmp_path / "state"
        code, first = self.invoke("initialize", "--state-dir", str(state))
        self.assertEqual(code, 0)
        self.assertTrue(first["initialized"])
        self.assertFalse(first["completed"])
        for path in (state, state / "imports", state / "outcome-events"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        setup_file = state / "setup.json"
        self.assertEqual(stat.S_IMODE(setup_file.stat().st_mode), 0o600)
        subscriptions = state / "subscriptions.json"
        subscriptions.write_text('{"opencode-go":{"price":12}}')
        subscriptions.chmod(0o600)
        original = subscriptions.read_bytes()
        code, second = self.invoke("initialize", "--state-dir", str(state))
        self.assertEqual(code, 0)
        self.assertTrue(second["initialized"])
        self.assertEqual(subscriptions.read_bytes(), original)
        self.assertFalse(json.loads(setup_file.read_text())["completed"])

    def test_finish_requires_initialization_and_does_not_claim_metrics_complete(self):
        self.set_home(self.tmp_path / "home")
        state = self.tmp_path / "state"
        code, error = self.invoke("finish", "--state-dir", str(state))
        self.assertEqual(code, 2)
        self.assertEqual(error, {"error": "initialize the private state directory before finishing setup"})
        self.invoke("initialize", "--state-dir", str(state))
        _, finished = self.invoke("finish", "--state-dir", str(state))
        self.assertTrue(finished["initialized"])
        self.assertTrue(finished["completed"])
        saved = json.loads((state / "setup.json").read_text())
        self.assertTrue(saved["completed"])
        self.assertNotIn("metrics_complete", saved)
        self.assertIn("does not mark any usage or task evidence complete", finished["message"])

    def test_task_command_installs_exact_symlink_and_is_idempotent(self):
        self.set_home(self.tmp_path / "home")
        state = self.tmp_path / "state"
        self.invoke("initialize", "--state-dir", str(state))
        code, result = self.invoke("install-task-command", "--state-dir", str(state))
        destination = Path.home() / ".local" / "bin" / "tmos-ai-task"
        source = onboarding._plugin_root() / "bin" / "tmos-ai-task"
        self.assertEqual(code, 0)
        self.assertTrue(result["task_command"]["installed"])
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.resolve(), source.resolve())
        _, again = self.invoke("install-task-command", "--state-dir", str(state))
        self.assertTrue(again["task_command"]["installed"])

    def test_task_command_conflict_is_reported_without_overwrite(self):
        self.set_home(self.tmp_path / "home")
        destination = Path.home() / ".local" / "bin" / "tmos-ai-task"
        destination.parent.mkdir(parents=True)
        destination.write_text("unrelated command\n")
        code, result = self.invoke("install-task-command", "--state-dir", str(self.tmp_path / "state"))
        self.assertEqual(code, 0)
        self.assertFalse(result["task_command"]["installed"])
        self.assertIn("conflict", result["task_command"]["reason"])
        self.assertEqual(destination.read_text(), "unrelated command\n")

    def test_task_install_refuses_symlink_ancestor_and_errors_are_stdout_json(self):
        home = self.tmp_path / "home"
        outside = self.tmp_path / "outside"
        outside.mkdir()
        home.mkdir()
        (home / ".local").symlink_to(outside, target_is_directory=True)
        self.set_home(home)
        code, error = self.invoke("install-task-command", "--state-dir", str(self.tmp_path / "state"))
        self.assertEqual(code, 2)
        self.assertEqual(error, {"error": "path contains a symlink component"})
        self.assertFalse((outside / "bin").exists())

    def test_task_remove_unlinks_only_exact_managed_symlink(self):
        self.set_home(self.tmp_path / "home")
        state = self.tmp_path / "state"
        self.invoke("install-task-command", "--state-dir", str(state))
        destination = Path.home() / ".local" / "bin" / "tmos-ai-task"
        code, removed = self.invoke("remove-task-command", "--state-dir", str(state))
        self.assertEqual(code, 0)
        self.assertFalse(destination.exists())
        self.assertIn("Removed", removed["message"])
        _, repeated = self.invoke("remove-task-command", "--state-dir", str(state))
        self.assertIn("not installed", repeated["message"])
        destination.write_text("unrelated")
        _, conflict = self.invoke("remove-task-command", "--state-dir", str(state))
        self.assertEqual(destination.read_text(), "unrelated")
        self.assertIn("Conflict", conflict["message"])

    def test_pi_observer_installs_at_configured_absolute_agent_and_detects_pi(self):
        home = self.tmp_path / "home"
        self.set_home(home)
        configured_agent = home / "worker-agent"
        self.set_env(PI_CODING_AGENT_DIR=str(configured_agent))
        fake_bin = self.tmp_path / "bin"
        fake_bin.mkdir()
        fake_pi = fake_bin / "pi"
        fake_pi.write_text("#!/bin/sh\nexit 0\n")
        fake_pi.chmod(0o700)
        self.set_env(PATH=str(fake_bin))
        state = self.tmp_path / "state"
        code, result = self.invoke("install-pi-observer", "--state-dir", str(state))
        destination = configured_agent / "extensions" / "tmos-ai-usage"
        source = onboarding._plugin_root() / "integration" / "pi"
        self.assertEqual(code, 0)
        self.assertTrue(result["pi_observer"]["installed"])
        self.assertTrue(result["pi_observer"]["available"])
        self.assertTrue(destination.is_symlink())
        self.assertEqual(destination.resolve(), source.resolve())
        self.assertIn("new Pi session or /reload", result["pi_observer"]["reason"])
        _, repeated = self.invoke("install-pi-observer", "--state-dir", str(state))
        self.assertTrue(repeated["pi_observer"]["installed"])

    def test_pi_identical_tree_ignores_extra_large_file_but_required_tamper_conflicts(self):
        self.set_home(self.tmp_path / "home")
        self.set_env(PI_CODING_AGENT_DIR=str(self.tmp_path / "agent"))
        source = onboarding._plugin_root() / "integration" / "pi"
        destination = self.tmp_path / "agent" / "extensions" / "tmos-ai-usage"
        destination.parent.mkdir(parents=True)
        shutil.copytree(source, destination)
        extra = destination / "local-extra.bin"
        extra.write_bytes(b"x" * (onboarding.MAX_PI_FILE_BYTES + 1))
        _, accepted = self.invoke("install-pi-observer", "--state-dir", str(self.tmp_path / "state"))
        self.assertTrue(accepted["pi_observer"]["installed"])
        self.assertEqual(extra.stat().st_size, onboarding.MAX_PI_FILE_BYTES + 1)
        (destination / "index.js").write_text("tampered\n")
        _, conflict = self.invoke("install-pi-observer", "--state-dir", str(self.tmp_path / "state"))
        self.assertFalse(conflict["pi_observer"]["installed"])
        self.assertIn("conflict", conflict["pi_observer"]["reason"])
        self.assertEqual((destination / "index.js").read_text(), "tampered\n")

    def test_pi_remove_preserves_copied_extension_directory(self):
        self.set_home(self.tmp_path / "home")
        agent = self.tmp_path / "agent"
        self.set_env(PI_CODING_AGENT_DIR=str(agent))
        self.invoke("install-pi-observer", "--state-dir", str(self.tmp_path / "state"))
        destination = agent / "extensions" / "tmos-ai-usage"
        _, removed = self.invoke("remove-pi-observer", "--state-dir", str(self.tmp_path / "state"))
        self.assertFalse(os.path.lexists(destination))
        self.assertIn("Removed", removed["message"])
        shutil.copytree(onboarding._plugin_root() / "integration" / "pi", destination)
        _, refused = self.invoke("remove-pi-observer", "--state-dir", str(self.tmp_path / "state"))
        self.assertTrue(destination.is_dir())
        self.assertIn("Conflict", refused["message"])

    def test_relative_pi_agent_env_uses_default_with_explanation(self):
        home = self.tmp_path / "home"
        self.set_home(home)
        self.set_env(PI_CODING_AGENT_DIR="relative-agent")
        destination, reason = onboarding._pi_destination()
        self.assertEqual(destination, home / ".pi" / "agent" / "extensions" / "tmos-ai-usage")
        self.assertIn("not absolute", reason)


if __name__ == "__main__":
    unittest.main()
