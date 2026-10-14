import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import usage_collector as c
import subscription_value


NOW = datetime(2026, 10, 14, 18, tzinfo=timezone.utc)


def quota():
    return {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"window": "weekly", "remaining_fraction": 0.8, "reset_time": "2026-10-21T18:00:00Z"},
            {"window": "5h", "remaining_fraction": 0.6, "reset_time": "2026-10-14T23:00:00Z"}]},
        {"name": "Claude and GPT models", "buckets": [
            {"window": "5h", "remaining_fraction": 0.1, "reset_time": "2026-10-14T23:00:00Z"}]}]}}}


class AntigravityTest(unittest.TestCase):
    def test_official_quota_keeps_groups_separate_and_does_not_invent_plan(self):
        result = c.parse_antigravity(quota(), NOW)
        self.assertEqual(result["provider"], "antigravity")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["plan"])
        self.assertEqual([w["name"] for w in result["windows"]], ["5h", "week"])
        self.assertAlmostEqual(result["windows"][0]["remaining_pct"], 60)
        self.assertEqual(result["windows"][0]["resets_in_s"], 18000)
        self.assertEqual(result["quota_groups"][1]["windows"][0]["remaining_pct"], 10)
        self.assertIn("Gemini", result["note"])

    def test_missing_or_invalid_quota_is_unknown_rather_than_zero(self):
        for payload in ({}, {"status": "SUCCESS", "command": {"name": "usage", "data": {}}}):
            result = c.parse_antigravity(payload, NOW)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["windows"], [])
        for bad in (None, True, -0.1, 1.1, "0.8", float("nan")):
            payload = quota()
            payload["command"]["data"]["groups"] = payload["command"]["data"]["groups"][:1]
            for bucket in payload["command"]["data"]["groups"][0]["buckets"]:
                bucket["remaining_fraction"] = bad
            self.assertEqual(c.parse_antigravity(payload, NOW)["status"], "unknown")
        payload = quota()
        payload["command"]["data"]["groups"] = payload["command"]["data"]["groups"][1:]
        result = c.parse_antigravity(payload, NOW)
        self.assertEqual(result["windows"], [])
        self.assertEqual(len(result["quota_groups"]), 1)

    def test_cli_uses_read_only_command_and_does_not_expose_error_bodies(self):
        with patch("agy_usage.cli_json", return_value=quota()) as invoke:
            result = c.collect_antigravity()
        self.assertEqual(result["status"], "ok")
        invoke.assert_called_once_with()
        import agy_usage
        with patch("agy_usage.shutil.which", return_value="/mock/agy"), \
                patch("agy_usage.subprocess.run") as run:
            def output(args, **kwargs):
                if "--version" in args:
                    kwargs["stdout"].write(b"1.3.5\n")
                else:
                    self.assertEqual(args[1:], ["-p", "/usage", "--output-format", "json", "--print-timeout", "15s"])
                    kwargs["stdout"].write(json.dumps(quota()).encode())
                return type("Result", (), {"returncode": 0})()
            run.side_effect = output
            self.assertEqual(agy_usage.cli_json()["status"], "SUCCESS")
        with patch("agy_usage.cli_json", side_effect=agy_usage.CliError("unauthenticated")):
            result = c.collect_antigravity()
        self.assertEqual(result["status"], "unauthenticated")
        self.assertIn("agy", result["note"])

    def test_old_cli_does_not_send_usage_to_a_model(self):
        import agy_usage
        with patch("agy_usage.shutil.which", return_value="/mock/agy"), \
                patch("agy_usage.subprocess.run") as run:
            def old(args, **kwargs):
                kwargs["stdout"].write(b"1.2.0\n")
                return type("Result", (), {"returncode": 0})()
            run.side_effect = old
            with self.assertRaises(agy_usage.CliError):
                agy_usage.cli_json()
            self.assertEqual(run.call_count, 1)

    def test_canonical_transcript_deduplication_window_and_private_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            logs = root / "synthetic-session" / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            rows = [
                {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": NOW.isoformat(), "content": "PRIVATE_PROMPT"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE", "created_at": NOW.isoformat(), "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 70, "content": "PRIVATE_RESPONSE"},
                {"step_index": 2, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": "2026-01-01T00:00:00Z"},
                {"step_index": 3, "type": "GENERIC", "source": "MODEL", "status": "DONE", "created_at": NOW.isoformat(), "input_tokens": 1000},
            ]
            rows.append(copy.deepcopy(rows[1]))
            for name in ("transcript.jsonl", "transcript_full.jsonl"):
                (logs / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            chunks = logs / "chunks" / "transcript_full"
            chunks.mkdir(parents=True)
            (chunks / "00000000.jsonl").write_text((logs / "transcript_full.jsonl").read_text())
            state = Path(tmp) / "state"
            first = c.scan_antigravity_stats(NOW, root, state)
            second = c.scan_antigravity_stats(NOW, root, state)
            self.assertTrue(first["available"])
            self.assertEqual(first["totals"]["prompts"], 1)
            self.assertEqual(first["totals"]["sessions"], 1)
            self.assertEqual(first["totals"]["tokens"], 120)
            self.assertEqual(first["models"], [])
            self.assertEqual(second["scan"]["files_cached"], 1)
            exposed = json.dumps(first) + (state / "stats-cache.json").read_text()
            self.assertNotIn("PRIVATE_", exposed)
            self.assertNotIn("validated", exposed)

    def test_no_transcript_does_not_claim_zero_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(c.scan_antigravity_stats(NOW, Path(tmp), Path(tmp)/"state")["available"])

    def test_response_arriving_after_collection_start_is_picked_up_next_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            logs = root / "synthetic-session" / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            rows = [
                {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": NOW.isoformat()},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE", "created_at": (NOW + timedelta(seconds=5)).isoformat(), "input_tokens": 100, "output_tokens": 20},
            ]
            (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            state = Path(tmp) / "state"
            first = c.scan_antigravity_stats(NOW, root, state)
            second = c.scan_antigravity_stats(NOW + timedelta(seconds=10), root, state)
            self.assertEqual(first["totals"]["tokens"], 0)
            self.assertEqual(second["totals"]["tokens"], 120)
            self.assertEqual(second["totals"]["prompts"], 1)

    def test_api_mode_is_not_labelled_subscription_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            root.mkdir()
            (root.parent / "settings.json").write_text('{"modelProvider":"gemini"}')
            result = c.scan_antigravity_stats(NOW, root, Path(tmp)/"state")
            self.assertFalse(result["available"])
            self.assertIn("API billing", result["note"])
        with patch("usage_collector.read_json", return_value={"modelProvider": "gemini"}), patch("agy_usage.cli_json") as invoke:
            self.assertEqual(c.collect_antigravity()["status"], "unknown")
            invoke.assert_not_called()

    def test_timeout_and_oversize_degrade_without_error_text(self):
        import agy_usage
        import subprocess
        for reason in ("timeout", "oversized", "invalid_json", "failed"):
            with patch("agy_usage.cli_json", side_effect=agy_usage.CliError(reason)):
                result = c.collect_antigravity()
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["windows"], [])
        with patch("agy_usage.subprocess.run", side_effect=subprocess.TimeoutExpired("PRIVATE_ARGUMENT", 3)):
            with self.assertRaises(agy_usage.CliError) as caught:
                agy_usage._run(["agy"], 3)
            self.assertEqual(str(caught.exception), "timeout")
        with patch("agy_usage.subprocess.run") as run:
            def huge(args, **kwargs):
                kwargs["stdout"].write(b"x" * (agy_usage.MAX_OUTPUT + 1))
                return type("Result", (), {"returncode": 0})()
            run.side_effect = huge
            with self.assertRaises(agy_usage.CliError) as caught:
                agy_usage._run(["agy"], 3)
            self.assertEqual(str(caught.exception), "oversized")

    def test_missing_login_stderr_is_classified_without_exporting_it(self):
        import agy_usage
        with patch("agy_usage.subprocess.run") as run:
            def missing(args, **kwargs):
                kwargs["stderr"].write(b"authentication required PRIVATE_TOKEN")
                return type("Result", (), {"returncode": 1})()
            run.side_effect = missing
            with self.assertRaises(agy_usage.CliError) as caught:
                agy_usage._run(["agy"], 3)
            self.assertEqual(str(caught.exception), "unauthenticated")

    def test_provider_integrates_with_collector_and_subscription_price(self):
        with patch("agy_usage.cli_json", return_value=quota()), patch("usage_collector.stats_for", return_value={"available": False}):
            result = c.collect_all(["antigravity"])
        self.assertEqual(result["totals"]["providers_reported"], 1)
        self.assertEqual(result["providers"][0]["status"], "ok")
        with tempfile.TemporaryDirectory() as tmp:
            subscription_value.save_price(tmp, "antigravity", 18, "month")
            self.assertEqual(subscription_value.price_for({"provider": "antigravity"}, json.loads((Path(tmp)/"subscriptions.json").read_text()), NOW)["monthly_usd"], 18)


if __name__ == "__main__":
    unittest.main()
