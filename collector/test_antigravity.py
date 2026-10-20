import copy
import json
import os
import sqlite3
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


def _transcript(root, rows):
    logs = root / "synthetic-session" / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript_full.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


class ReviewFindingsTest(unittest.TestCase):
    def test_oversized_transcript_counts_its_recent_tail_and_says_so(self):
        early = {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": NOW.isoformat()}
        filler = [{"step_index": i, "type": "GENERIC", "source": "MODEL", "status": "DONE", "created_at": NOW.isoformat(), "pad": "x" * 200}
                  for i in range(1, 60)]
        late = [{"step_index": 60, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": NOW.isoformat()},
                {"step_index": 61, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE", "created_at": NOW.isoformat(),
                 "input_tokens": 100, "output_tokens": 20}]
        with tempfile.TemporaryDirectory() as tmp, patch("agy_usage.MAX_TRANSCRIPT", 4096):
            root = Path(tmp) / "brain"
            _transcript(root, [early, *filler, *late])
            self.assertGreater((root / "synthetic-session/.system_generated/logs/transcript_full.jsonl").stat().st_size, 2 * 4096)
            state = Path(tmp) / "state"
            stats = c.scan_antigravity_stats(NOW, root, state)
            cached = c.scan_antigravity_stats(NOW, root, state)
        # The recent prompt and reply at the end are counted; the early prompt lies before the tail.
        self.assertEqual(stats["totals"]["prompts"], 1)
        self.assertEqual(stats["totals"]["tokens"], 120)
        self.assertEqual(stats["scan"]["files_partial"], 1)
        self.assertIn("Incomplete: 1 transcript(s)", stats["note"])
        self.assertEqual(cached["scan"]["files_cached"], 1)
        self.assertEqual(cached["scan"]["files_partial"], 1)

    def test_transcript_within_the_limit_is_complete(self):
        rows = [{"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": NOW.isoformat()}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            _transcript(root, rows)
            stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertEqual(stats["scan"]["files_partial"], 0)
        self.assertNotIn("Incomplete", stats["note"])

    def test_opencode_reads_v1_only_sessions_beside_v2_and_counts_each_message_once(self):
        ms = int(NOW.timestamp() * 1000)
        user = json.dumps({"role": "user"})
        tokens = {"input": 10, "output": 5, "total": 15, "cache": {}}
        v1_reply = json.dumps({"role": "assistant", "modelID": "m1", "tokens": tokens})
        v2_reply = json.dumps({"role": "assistant", "model": {"id": "m1"}, "tokens": tokens})
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "opencode.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL, "
                        "time_updated integer NOT NULL, data text NOT NULL)")
            con.execute("CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL, "
                        "seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)")
            v1 = "INSERT INTO message VALUES (?, ?, ?, ?, ?)"
            v2 = "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)"
            # Migrated: the same messages under the same ids in both tables.
            con.execute(v1, ("a0", "migrated", ms, ms, user))
            con.execute(v1, ("a1", "migrated", ms, ms, v1_reply))
            con.execute(v2, ("a0", "migrated", "user", 0, ms, ms, user))
            con.execute(v2, ("a1", "migrated", "assistant", 1, ms, ms, v2_reply))
            # Still only in v1, and only in v2.
            con.execute(v1, ("b0", "legacy", ms, ms, user))
            con.execute(v1, ("b1", "legacy", ms, ms, v1_reply))
            con.execute(v2, ("c0", "new", "user", 0, ms, ms, user))
            con.execute(v2, ("c1", "new", "assistant", 1, ms, ms, v2_reply))
            con.commit()
            con.close()
            stats = c.scan_opencode_stats(NOW, db, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["prompts"], 3)
        self.assertEqual(stats["totals"]["sessions"], 3)
        self.assertEqual(stats["totals"]["tokens"], 45)
        self.assertEqual([m["id"] for m in stats["models"]], ["m1"])

    def test_pi_turns_already_in_a_history_are_not_added_again(self):
        document = {"providers": [
            {"provider": "command-code", "stats": {
                "available": True, "coverage_days": 30, "source": "~/.commandcode/projects + ~/.pi/agent/sessions",
                "totals": {"prompts": 5, "sessions": 2}, "scan": {"files_scanned": 2, "files_cached": 0, "pi_prompts": 3, "pi_sessions": 1}}},
            {"provider": "clinepass", "stats": {
                "available": True, "coverage_days": 30, "source": "~/.cline", "totals": {"prompts": 4, "sessions": 1}, "scan": {}}},
        ]}
        observed = {"command-code": {"turns": 3, "sessions": 1}, "clinepass": {"turns": 2, "sessions": 1}}
        with tempfile.TemporaryDirectory() as tmp, patch("pi_activity.scan", return_value=observed):
            subscription_value.attach(document, tmp, NOW)
        merged, separate = (row["activity"] for row in document["providers"])
        self.assertEqual((merged["observed_turns"], merged["pi_turns"], merged["cli_turns"], merged["observed_sessions"]), (5, 3, 2, 2))
        self.assertEqual(merged["source"], "~/.commandcode/projects + ~/.pi/agent/sessions")
        # A provider whose history holds no Pi sessions still gets them added, once.
        self.assertEqual((separate["observed_turns"], separate["pi_turns"], separate["cli_turns"], separate["observed_sessions"]), (6, 2, 4, 2))

    def test_pi_session_counts_once_from_collector_to_ranking(self):
        rows = [{"type": "session", "id": "pi-session"},
                {"type": "message", "timestamp": NOW.isoformat(), "message": {"role": "user", "content": "x"}},
                {"type": "message", "timestamp": NOW.isoformat(), "message": {
                    "role": "assistant", "provider": "commandcode", "model": "cc/m1",
                    "usage": {"input": 10, "output": 5, "totalTokens": 15}}}]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sessions = home / ".pi/agent/sessions/project"
            sessions.mkdir(parents=True)
            (sessions / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            with patch.dict(os.environ, {"HOME": str(home)}):
                stats = c.scan_command_code_stats(NOW, None, home / "state")
                document = {"providers": [{"provider": "command-code", "stats": stats}]}
                subscription_value.attach(document, home, NOW)
        self.assertEqual(stats["totals"]["prompts"], 1)
        self.assertEqual(stats["scan"]["pi_prompts"], 1)
        activity = document["providers"][0]["activity"]
        self.assertEqual((activity["observed_turns"], activity["pi_turns"], activity["cli_turns"]), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()
