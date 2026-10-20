import contextlib
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
import pi_activity
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
        invoke.assert_called_once_with(c.STATE_DIR / "agy-cli.log")
        import agy_usage
        with patch("agy_usage.shutil.which", return_value="/mock/agy"), \
                patch("agy_usage.subprocess.run") as run:
            def output(args, **kwargs):
                if "--version" in args:
                    kwargs["stdout"].write(b"1.3.5\n")
                else:
                    self.assertEqual(args[1:8], ["-p", "/usage", "--output-format", "json", "--print-timeout", "15s",
                                                 "--log-file"])
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
        # As OpenCode writes them: a v1 prompt names its selected model, a v2 prompt names none.
        v1_user = json.dumps({"role": "user", "model": {"providerID": "opencode-go", "modelID": "m1"}})
        v2_user = json.dumps({"text": "x"})
        tokens = {"input": 10, "output": 5, "total": 15, "cache": {}}
        v1_reply = json.dumps({"role": "assistant", "providerID": "opencode-go", "modelID": "m1", "tokens": tokens})
        v2_reply = json.dumps({"model": {"id": "m1", "providerID": "opencode-go"}, "tokens": tokens})
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
            con.execute(v1, ("a0", "migrated", ms, ms, v1_user))
            con.execute(v1, ("a1", "migrated", ms, ms, v1_reply))
            con.execute(v2, ("a0", "migrated", "user", 0, ms, ms, v2_user))
            con.execute(v2, ("a1", "migrated", "assistant", 1, ms, ms, v2_reply))
            # Still only in v1, and only in v2.
            con.execute(v1, ("b0", "legacy", ms, ms, v1_user))
            con.execute(v1, ("b1", "legacy", ms, ms, v1_reply))
            con.execute(v2, ("c0", "new", "user", 0, ms, ms, v2_user))
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


def _conversation(root, name, full=None, short=None, mtimes=(None, None)):
    """Write a conversation's exports (None leaves one as it is) and, optionally, their mtimes."""
    logs = root / name / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for file, rows, mtime in (("transcript_full.jsonl", full, mtimes[0]), ("transcript.jsonl", short, mtimes[1])):
        if rows is None:
            continue
        (logs / file).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        if mtime is not None:
            os.utime(logs / file, (mtime, mtime))


def _prompt(step, when):
    return {"step_index": step, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE",
            "created_at": when.isoformat()}


def _reply(step, when, incoming, outgoing):
    return {"step_index": step, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
            "created_at": when.isoformat(), "input_tokens": incoming, "output_tokens": outgoing}


class SecondReviewTest(unittest.TestCase):
    def test_steps_only_the_short_export_still_holds_are_counted(self):
        # The full export stopped at a checkpoint and the short export carried on: both are read,
        # and the steps they share are counted once.
        early = NOW - timedelta(hours=1)
        full = [_prompt(0, early), _reply(1, early, 100, 20)]
        short = full + [_prompt(2, NOW), _reply(3, NOW, 50, 10)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            _conversation(root, "conversation", full, short, (early.timestamp(), NOW.timestamp()))
            stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["prompts"], 2)
        self.assertEqual(stats["totals"]["tokens"], 180)
        self.assertEqual(stats["totals"]["sessions"], 1)

    def test_a_conversation_whose_full_export_went_stale_stays_in_the_window(self):
        old = NOW - timedelta(days=40)
        full = [_prompt(0, old), _reply(1, old, 100, 20)]
        short = full + [_prompt(2, NOW), _reply(3, NOW, 50, 10)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            _conversation(root, "conversation", full, short, (old.timestamp(), NOW.timestamp()))
            stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertTrue(stats["available"])
        self.assertEqual(stats["totals"]["prompts"], 1)
        self.assertEqual(stats["totals"]["tokens"], 60)

    def test_exports_that_number_steps_differently_are_not_added_together(self):
        # A rewrite renumbered the short export: its step 0 is not the full export's step 0. Adding
        # the two would count steps twice, so only the export holding more steps is counted.
        first, second = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
        full = [_prompt(0, first), _reply(1, first, 100, 20)]
        short = [_prompt(0, second), _reply(1, second, 7, 3), _prompt(2, NOW), _reply(3, NOW, 50, 10)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            _conversation(root, "conversation", full, short, (NOW.timestamp(), NOW.timestamp()))
            stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["prompts"], 2)
        self.assertEqual(stats["totals"]["tokens"], 70)

    def test_duplicate_exports_never_take_places_under_the_file_cap(self):
        with tempfile.TemporaryDirectory() as tmp, patch("usage_collector.STATS_MAX_FILES", 2):
            root = Path(tmp) / "brain"
            for age, name in enumerate(("newest", "middle", "oldest")):
                rows = [_prompt(0, NOW - timedelta(hours=age))]
                stamp = NOW.timestamp() - age * 3600
                _conversation(root, name, rows, rows, (stamp, stamp))
            stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["sessions"], 2)
        self.assertEqual(stats["totals"]["prompts"], 2)
        self.assertEqual(stats["scan"]["files_scanned"], 2)

    def test_a_change_to_the_other_export_refreshes_the_cache(self):
        stamp = NOW.timestamp()
        rows = [_prompt(0, NOW)]
        with tempfile.TemporaryDirectory() as tmp:
            root, state = Path(tmp) / "brain", Path(tmp) / "state"
            _conversation(root, "conversation", rows, rows, (stamp, stamp - 60))
            first = c.scan_antigravity_stats(NOW, root, state)
            # The short export gains a reply while the full export stays the newest file.
            _conversation(root, "conversation", None, rows + [_reply(1, NOW, 50, 10)], (None, stamp - 30))
            second = c.scan_antigravity_stats(NOW, root, state)
        self.assertEqual(first["totals"]["tokens"], 0)
        self.assertEqual(second["totals"]["tokens"], 60)
        self.assertEqual(second["scan"]["files_scanned"], 1)

    def test_agy_runs_with_its_self_updater_switched_off(self):
        import agy_usage
        envs = []
        with patch("agy_usage.shutil.which", return_value="/mock/agy"), \
                patch("agy_usage.subprocess.run") as run, \
                patch.dict(os.environ, {"AGY_CLI_DISABLE_AUTO_UPDATE": "false", "TMOS_SYNTHETIC_ENV": "kept"}):
            def output(args, **kwargs):
                envs.append(kwargs.get("env"))
                kwargs["stdout"].write(b"1.3.5\n" if "--version" in args else json.dumps(quota()).encode())
                return type("Result", (), {"returncode": 0})()
            run.side_effect = output
            self.assertEqual(agy_usage.cli_json()["status"], "SUCCESS")
        self.assertEqual(len(envs), 2)
        for env in envs:
            self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
            self.assertEqual(env["TMOS_SYNTHETIC_ENV"], "kept")

    def test_pi_usage_outside_assistant_messages_counts_toward_tokens(self):
        at = NOW.isoformat()
        rows = [
            {"type": "session", "id": "pi-session"},
            {"type": "model_change", "timestamp": at, "provider": "commandcode", "modelId": "cc/m1"},
            {"type": "message", "timestamp": at, "message": {"role": "user", "content": "x"}},
            {"type": "message", "timestamp": at, "message": {
                "role": "assistant", "provider": "commandcode", "model": "cc/m1",
                "usage": {"input": 10, "output": 5, "totalTokens": 15}}},
            # A compaction names no model: the model in use made it.
            {"type": "compaction", "timestamp": at, "summary": "s", "firstKeptEntryId": "a",
             "usage": {"input": 90, "output": 10, "totalTokens": 100}},
            # A usage entry names its own model, whatever is in use.
            {"type": "usage", "kind": "cache_warm", "timestamp": at, "provider": "commandcode", "model": "cc/m1",
             "usage": {"input": 0, "output": 0, "cacheRead": 50, "cacheWrite": 0, "totalTokens": 50}},
            {"type": "usage", "kind": "cache_warm", "timestamp": at, "provider": "openrouter", "model": "x/y",
             "usage": {"input": 1000, "totalTokens": 1000}},
            # After a switch to another provider, a compaction is that provider's.
            {"type": "model_change", "timestamp": at, "provider": "openrouter", "modelId": "x/y"},
            {"type": "branch_summary", "timestamp": at, "fromId": "b", "summary": "s",
             "usage": {"input": 7, "totalTokens": 7}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sessions = home / ".pi/agent/sessions/project"
            sessions.mkdir(parents=True)
            (sessions / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            with patch.dict(os.environ, {"HOME": str(home)}):
                stats = c.scan_command_code_stats(NOW, None, home / "state")
        self.assertEqual(stats["totals"]["tokens"], 165)
        self.assertEqual(stats["totals"]["prompts"], 1)
        self.assertEqual(stats["scan"]["pi_prompts"], 1)
        model = next(row for row in stats["models"] if row["id"] == "m1")
        self.assertEqual((model["total_tokens"], model["cache_read_tokens"]), (165, 50))


@contextlib.contextmanager
def _pi_env(home, **configured):
    """HOME set to `home`, and the Pi directory variables exactly as given (unset otherwise)."""
    with patch.dict(os.environ, {"HOME": str(home), **configured}):
        for name in ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"):
            if name not in configured:
                os.environ.pop(name, None)
        yield


def _pi_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _pi_turn(n, when, tokens):
    """A prompt and its Command Code reply, as Pi writes them: entries with IDs."""
    at = when.isoformat()
    return [{"type": "message", "id": f"u{n}", "timestamp": at, "message": {"role": "user", "content": "x"}},
            {"type": "message", "id": f"a{n}", "timestamp": at, "message": {
                "role": "assistant", "provider": "commandcode", "model": "cc/m1",
                "usage": {"input": tokens, "totalTokens": tokens}}}]


class ThirdReviewTest(unittest.TestCase):
    def test_divergent_exports_holding_as_many_steps_count_the_more_recent(self):
        first, second = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
        stale = [_prompt(0, first), _reply(1, first, 100, 20)]
        recent = [_prompt(0, second), _reply(1, second, 7, 3)]
        for full, short in ((stale, recent), (recent, stale)):
            with self.subTest(full_export_is_stale=full is stale), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "brain"
                stamps = tuple((first if rows is stale else second).timestamp() for rows in (full, short))
                _conversation(root, "conversation", full, short, stamps)
                stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
                self.assertEqual((stats["totals"]["prompts"], stats["totals"]["tokens"]), (1, 10))

    def test_divergent_exports_with_the_same_newest_step_count_the_one_written_last(self):
        at = NOW - timedelta(hours=1)
        full = [_prompt(0, at), _reply(1, at, 100, 20)]
        short = [_prompt(0, at), _reply(1, at - timedelta(minutes=5), 7, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            _conversation(root, "conversation", full, short, (at.timestamp(), NOW.timestamp()))
            stats = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["tokens"], 10)

    def test_pi_history_is_read_from_the_configured_agent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _pi_file(home / "worker/sessions/--project--/s.jsonl", [{"type": "session", "id": "w"}] + _pi_turn(1, NOW, 15))
            # The default directory is not Pi's while another one is configured.
            _pi_file(home / ".pi/agent/sessions/--project--/d.jsonl", [{"type": "session", "id": "d"}] + _pi_turn(2, NOW, 99))
            with _pi_env(home, PI_CODING_AGENT_DIR=str(home / "worker")):
                stats = c.scan_command_code_stats(NOW, None, home / "state")
                turns = pi_activity.scan(now=NOW)
        self.assertEqual((stats["totals"]["prompts"], stats["totals"]["tokens"]), (1, 15))
        self.assertEqual(stats["source"], "~/.commandcode/projects + ~/worker/sessions")
        self.assertEqual(turns, {"command-code": {"turns": 1, "sessions": 1}})

    def test_pi_session_directory_follows_pis_own_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "worker"
            agent.mkdir()
            (agent / "settings.json").write_text(json.dumps({"sessionDir": "~/from-settings"}))
            cases = (
                ({"PI_CODING_AGENT_SESSION_DIR": str(home / "from-env")}, home / "from-env"),
                ({"PI_CODING_AGENT_SESSION_DIR": "~/from-env"}, home / "from-env"),
                ({}, home / "from-settings"),
                # Pi resolves a relative value from where it was started, which is not knowable here.
                ({"PI_CODING_AGENT_SESSION_DIR": "relative"}, agent / "sessions"),
            )
            for extra, expected in cases:
                with self.subTest(extra=extra), _pi_env(home, PI_CODING_AGENT_DIR=str(agent), **extra):
                    self.assertEqual(pi_activity.sessions_dir(), expected)
            (agent / "settings.json").write_text(json.dumps({"sessionDir": "relative"}))
            with _pi_env(home, PI_CODING_AGENT_DIR=str(agent)):
                self.assertEqual(pi_activity.sessions_dir(), agent / "sessions")
            with _pi_env(home):
                self.assertEqual(pi_activity.sessions_dir(), home / ".pi/agent/sessions")

    def test_opencode_go_key_is_read_from_the_configured_pi_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / "worker"
            agent.mkdir()
            (agent / "auth.json").write_text(json.dumps({"opencode-go": {"key": "synthetic-key"}}))
            keys = []
            with _pi_env(home, PI_CODING_AGENT_DIR=str(agent)), \
                    patch("usage_collector.http_json", side_effect=lambda url, key: keys.append(key) or {}), \
                    patch("usage_collector.parse_opencode", return_value={"provider": "opencode-go"}):
                os.environ.pop("OPENCODE_GO_API_KEY", None)
                c.collect_opencode()
        self.assertEqual(keys, ["synthetic-key"])

    def test_future_dated_pi_rows_wait_for_their_time_and_are_never_cached(self):
        later = NOW + timedelta(days=1)
        rows = [{"type": "session", "id": "s"}] + _pi_turn(1, NOW, 15) + _pi_turn(2, later, 20) + [
            {"type": "usage", "id": "w1", "kind": "cache_warm", "timestamp": later.isoformat(),
             "provider": "commandcode", "model": "cc/m1", "usage": {"cacheRead": 5, "totalTokens": 5}}]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _pi_file(home / ".pi/agent/sessions/--project--/s.jsonl", rows)
            with _pi_env(home):
                first = c.scan_command_code_stats(NOW, None, home / "state")
                again = c.scan_command_code_stats(NOW, None, home / "state")
                after = c.scan_command_code_stats(later + timedelta(minutes=1), None, home / "state")
        self.assertEqual((first["totals"]["prompts"], first["totals"]["tokens"]), (1, 15))
        self.assertEqual([row["date"] for row in first["daily"]], [NOW.date().isoformat()])
        self.assertEqual((again["scan"]["files_scanned"], again["scan"]["files_cached"]), (1, 0))
        self.assertEqual((after["totals"]["prompts"], after["totals"]["tokens"]), (2, 40))

    def test_a_forked_pi_session_counts_the_history_it_copied_once(self):
        first, second, third = (NOW - timedelta(hours=h) for h in (3, 2, 1))
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sessions = home / ".pi/agent/sessions/--project--"
            # /fork and /clone copy the parent's entries, IDs and timestamps included, and name the
            # parent file in the new header.
            parent = _pi_file(sessions / "p.jsonl", [{"type": "session", "id": "p"}] + _pi_turn(1, first, 15))
            child = _pi_file(sessions / "c.jsonl", [{"type": "session", "id": "c", "parentSession": str(parent)}]
                             + _pi_turn(1, first, 15) + _pi_turn(2, second, 20))
            _pi_file(sessions / "g.jsonl", [{"type": "session", "id": "g", "parentSession": str(child)}]
                     + _pi_turn(1, first, 15) + _pi_turn(2, second, 20) + _pi_turn(3, third, 25))
            with _pi_env(home):
                stats = c.scan_command_code_stats(NOW, None, home / "state")
                turns = pi_activity.scan(now=NOW)
                parent.unlink()
                orphaned = c.scan_command_code_stats(NOW, None, home / "state")
                orphaned_turns = pi_activity.scan(now=NOW)
        self.assertEqual((stats["totals"]["prompts"], stats["totals"]["tokens"]), (3, 60))
        self.assertEqual(stats["scan"]["pi_prompts"], 3)
        self.assertEqual(turns, {"command-code": {"turns": 3, "sessions": 3}})
        # With the parent gone, the child's copies are the only ones left, and they count.
        self.assertEqual((orphaned["totals"]["prompts"], orphaned["totals"]["tokens"]), (3, 60))
        self.assertEqual(orphaned_turns, {"command-code": {"turns": 3, "sessions": 2}})


def _opencode_db(path, v2=(), v1=()):
    """An OpenCode database laid out as OpenCode writes it, rows inserted last first.

    `v2` rows are `(session, seq, type, data)`; `v1` rows are `(session, data)`, written in the
    same millisecond (NOW, or a time given last) with ids in the order given. Reading in insertion
    order would be wrong.
    """
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL, "
                "time_updated integer NOT NULL, data text NOT NULL)")
    con.execute("CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL, "
                "seq integer NOT NULL, time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)")
    def ms(rest):
        return int((rest[0] if rest else NOW).timestamp() * 1000)
    for n, (session, seq, kind, data, *rest) in reversed(list(enumerate(v2))):
        con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"v2-{n:03d}", session, kind, seq, ms(rest), ms(rest), json.dumps(data)))
    for n, (session, data, *rest) in reversed(list(enumerate(v1))):
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                    (f"v1-{n:03d}", session, ms(rest), ms(rest), json.dumps(data)))
    con.commit()
    con.close()
    return path


def _opencode_tokens(input_tokens, output=0, reasoning=0, read=0, write=0, total=None):
    tokens = {"input": input_tokens, "output": output, "reasoning": reasoning, "cache": {"read": read, "write": write}}
    return tokens if total is None else {**tokens, "total": total}


class FourthReviewTest(unittest.TestCase):
    def test_opencode_counts_only_what_opencode_go_answered(self):
        go = {"id": "m1", "providerID": "opencode-go", "variant": "default"}
        other = {"id": "claude-x", "providerID": "anthropic", "variant": "default"}
        local = {"id": "qwen", "providerID": "halogen", "variant": "default"}
        prompt = {"text": "x", "time": {"created": 0}}  # a v2 prompt names no provider

        def reply(model, n):
            return {"agent": "build", "model": model, "tokens": _opencode_tokens(n)}
        v2 = [
            ("go", 0, "user", prompt), ("go", 1, "assistant", reply(go, 10)), ("go", 2, "assistant", reply(go, 5)),
            ("other", 0, "user", prompt), ("other", 1, "assistant", reply(other, 1000)),
            ("local", 0, "user", prompt), ("local", 1, "assistant", reply(local, 2000)),
            # Switched provider mid-session: a prompt belongs to the provider that answered it.
            ("switch", 0, "user", prompt), ("switch", 1, "assistant", reply(other, 100)),
            ("switch", 2, "user", prompt), ("switch", 3, "assistant", reply(go, 7)),
            # Not answered yet: whose it is is not known yet.
            ("waiting", 0, "user", prompt),
        ]
        v1 = [
            ("legacy", {"role": "user", "model": {"providerID": "anthropic", "modelID": "claude-x"}}),
            ("legacy", {"role": "assistant", "providerID": "anthropic", "modelID": "claude-x",
                        "tokens": _opencode_tokens(500, total=500)}),
            ("legacy", {"role": "user", "model": {"providerID": "opencode-go", "modelID": "m2"}}),
            ("legacy", {"role": "assistant", "providerID": "opencode-go", "modelID": "m2",
                        "tokens": _opencode_tokens(20, total=20)}),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = _opencode_db(Path(tmp) / "opencode.db", v2, v1)
            stats = c.scan_opencode_stats(NOW, db, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["prompts"], 3)
        self.assertEqual(stats["totals"]["tokens"], 42)
        self.assertEqual(stats["totals"]["sessions"], 3)
        self.assertEqual(sorted(model["id"] for model in stats["models"]), ["m1", "m2"])

    def test_opencode_reasoning_is_output_and_part_of_the_total(self):
        go = {"id": "m1", "providerID": "opencode-go"}
        v2 = [("s", 0, "user", {"text": "x"}),
              ("s", 1, "assistant", {"model": go, "tokens": _opencode_tokens(10, 5, 20, 3, 2)})]
        # OpenCode's own total, where it stored one, is kept as it is.
        v1 = [("t", {"role": "assistant", "providerID": "opencode-go", "modelID": "m1",
                     "tokens": _opencode_tokens(1, 1, 1, total=3)})]
        with tempfile.TemporaryDirectory() as tmp:
            db = _opencode_db(Path(tmp) / "opencode.db", v2, v1)
            stats = c.scan_opencode_stats(NOW, db, Path(tmp) / "state")
        self.assertEqual(stats["totals"]["tokens"], 43)
        model = stats["models"][0]
        self.assertEqual((model["output_tokens"], model["total_tokens"]), (27, 43))

    def test_agy_quota_checks_keep_one_log_file(self):
        import agy_usage
        calls = []

        def fake(args, **kwargs):
            if "--version" in args:
                kwargs["stdout"].write(b"1.4.3\n")
            else:
                calls.append(list(args))
                log = Path(args[args.index("--log-file") + 1])
                with log.open("a") as stream:  # appends, as some agy release might
                    stream.write(f"run {len(calls)}\n")
                kwargs["stdout"].write(json.dumps(quota()).encode())
            return type("Result", (), {"returncode": 0})()
        with tempfile.TemporaryDirectory() as tmp, \
                patch("agy_usage.shutil.which", return_value="/mock/agy"), \
                patch("agy_usage.subprocess.run", side_effect=fake):
            log = Path(tmp) / "state" / "agy-cli.log"
            for _ in range(2):
                self.assertEqual(agy_usage.cli_json(log)["status"], "SUCCESS")
            self.assertEqual(log.read_text(), "run 2\n")
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            # With nowhere to keep it, the log goes to a private directory removed after the run.
            agy_usage.cli_json()
            spare = Path(calls[-1][calls[-1].index("--log-file") + 1])
            self.assertNotEqual(spare, log)
            self.assertFalse(spare.parent.exists())
        self.assertEqual(calls[0][1:7], ["-p", "/usage", "--output-format", "json", "--print-timeout", "15s"])

    def test_an_agy_without_the_log_file_flag_is_never_run_without_one(self):
        import agy_usage
        calls = []

        def older(args, **kwargs):
            if "--version" in args:
                kwargs["stdout"].write(b"1.3.5\n")
                return type("Result", (), {"returncode": 0})()
            calls.append(list(args))
            if "--log-file" in args:  # what Go's flag package says about a flag it does not know
                kwargs["stderr"].write(b"flag provided but not defined: -log-file\nUsage of agy:\n")
                return type("Result", (), {"returncode": 2})()
            kwargs["stdout"].write(json.dumps(quota()).encode())  # and a new log in agy's own folder
            return type("Result", (), {"returncode": 0})()
        with tempfile.TemporaryDirectory() as tmp, patch.object(c, "STATE_DIR", Path(tmp) / "state"), \
                patch("agy_usage.shutil.which", return_value="/mock/agy"), \
                patch("agy_usage.subprocess.run", side_effect=older):
            with self.assertRaisesRegex(agy_usage.CliError, "no_log_file"):
                agy_usage.cli_json(Path(tmp) / "agy-cli.log")
            result = c.collect_antigravity()
        # Every refresh would leave another log behind, so the release is not run without the flag.
        self.assertEqual([("--log-file" in args) for args in calls], [True, True])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("--log-file", result["note"])

    def test_the_collector_keeps_agys_log_in_its_state_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(c, "STATE_DIR", Path(tmp) / "state"), \
                patch("agy_usage.cli_json", return_value=quota()) as invoke:
            self.assertEqual(c.collect_antigravity()["status"], "ok")
        invoke.assert_called_once_with(Path(tmp) / "state" / "agy-cli.log")

    def test_a_relative_pi_session_directory_is_reported_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agent = home / ".pi/agent"
            _pi_file(agent / "sessions/--project--/s.jsonl", [{"type": "session", "id": "s"}] + _pi_turn(1, NOW, 15))
            (agent / "settings.json").write_text(json.dumps({"sessionDir": ".pi/sessions"}))
            cases = (({}, "sessionDir"),
                     ({"PI_CODING_AGENT_SESSION_DIR": "relative"}, "PI_CODING_AGENT_SESSION_DIR"),
                     ({"PI_CODING_AGENT_DIR": "relative-agent"}, "PI_CODING_AGENT_DIR"))
            for configured, setting in cases:
                with self.subTest(setting=setting), _pi_env(home, **configured):
                    self.assertEqual(pi_activity.sessions_location(), (agent / "sessions", setting))
                    stats = c.scan_command_code_stats(NOW, None, home / "state")
                    # What Pi wrote to the default directory still counts. What it did not is named.
                    self.assertEqual(stats["totals"]["prompts"], 1)
                    self.assertEqual(stats["scan"]["pi_unfollowed"], setting)
                    self.assertIn("not counted", stats["note"])
                    opencode = c.scan_opencode_stats(NOW, None, home / "state")  # no OpenCode database here
                    self.assertFalse(opencode["available"])
                    self.assertIn("not counted", opencode["note"])
                    document = {"providers": [{"provider": "command-code", "stats": stats}]}
                    subscription_value.attach(document, home, NOW)
                    self.assertIn("not counted", document["providers"][0]["activity"]["reason"])
            (agent / "settings.json").write_text(json.dumps({"sessionDir": str(agent / "sessions")}))
            with _pi_env(home):
                self.assertEqual(pi_activity.sessions_location(), (agent / "sessions", None))
                stats = c.scan_command_code_stats(NOW, None, home / "state")
            self.assertNotIn("pi_unfollowed", stats["scan"])
            self.assertNotIn("not counted", stats.get("note", ""))



class FifthReviewTest(unittest.TestCase):
    def test_forks_of_an_ancestor_the_file_cap_left_out_count_its_history_once(self):
        first, second, third = (NOW - timedelta(hours=h) for h in (3, 2, 1))
        with tempfile.TemporaryDirectory() as tmp, patch("usage_collector.STATS_MAX_FILES", 2), \
                patch("pi_activity.SCAN_MAX_FILES", 2, create=True):
            home = Path(tmp)
            sessions = home / ".pi/agent/sessions/--project--"
            parent = _pi_file(sessions / "p.jsonl", [{"type": "session", "id": "p"}] + _pi_turn(1, first, 15))
            for name, n, when, tokens in (("a", 2, second, 20), ("b", 3, third, 25)):
                _pi_file(sessions / f"{name}.jsonl", [{"type": "session", "id": name, "parentSession": str(parent)}]
                         + _pi_turn(1, first, 15) + _pi_turn(n, when, tokens))
            # The parent is the oldest file, so the two newest (its forks) fill the cap.
            for name, when in (("p", first), ("a", second), ("b", third)):
                os.utime(sessions / f"{name}.jsonl", (when.timestamp(), when.timestamp()))
            with _pi_env(home):
                stats = c.scan_command_code_stats(NOW, None, home / "state")
                turns = pi_activity.scan(now=NOW)
        self.assertEqual((stats["totals"]["prompts"], stats["totals"]["tokens"]), (3, 60))
        self.assertEqual(turns, {"command-code": {"turns": 3, "sessions": 3}})

    def test_a_sibling_export_rewritten_within_the_same_second_is_read_again(self):
        at = NOW - timedelta(hours=1)
        second = int(at.timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "brain"
            # The full export stands for the conversation (written last); the short one is its sibling.
            _conversation(root, "conversation", [_prompt(0, at)], [_prompt(0, at), _reply(1, at, 7, 3)])
            logs = root / "conversation" / ".system_generated" / "logs"
            os.utime(logs / "transcript_full.jsonl", ns=(second * 10**9 + 900_000_000,) * 2)
            os.utime(logs / "transcript.jsonl", ns=(second * 10**9 + 100_000_000,) * 2)
            before = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
            # Rewritten in the same second with as many bytes: only the subsecond mtime moves.
            _conversation(root, "conversation", None, [_prompt(0, at), _reply(1, at, 8, 4)])
            os.utime(logs / "transcript.jsonl", ns=(second * 10**9 + 200_000_000,) * 2)
            after = c.scan_antigravity_stats(NOW, root, Path(tmp) / "state")
        self.assertEqual(before["totals"]["tokens"], 10)
        self.assertEqual(after["totals"]["tokens"], 12)

    def test_future_dated_opencode_rows_wait_for_their_time_and_are_never_cached(self):
        go = {"id": "m1", "providerID": "opencode-go"}
        later = NOW + timedelta(days=1)
        v2 = [("s", 0, "user", {"text": "x"}), ("s", 1, "assistant", {"model": go, "tokens": _opencode_tokens(15)}),
              ("s", 2, "user", {"text": "y"}, later),
              ("s", 3, "assistant", {"model": go, "tokens": _opencode_tokens(20)}, later)]
        v1 = [("t", {"role": "assistant", "providerID": "opencode-go", "modelID": "m1",
                     "tokens": _opencode_tokens(5, total=5)}, later)]
        with tempfile.TemporaryDirectory() as tmp:
            db = _opencode_db(Path(tmp) / "opencode.db", v2, v1)
            first = c.scan_opencode_stats(NOW, db, Path(tmp) / "state")
            again = c.scan_opencode_stats(NOW, db, Path(tmp) / "state")
            after = c.scan_opencode_stats(later + timedelta(minutes=1), db, Path(tmp) / "state")
        self.assertEqual((first["totals"]["prompts"], first["totals"]["tokens"]), (1, 15))
        self.assertEqual([row["date"] for row in first["daily"]], [NOW.date().isoformat()])
        self.assertEqual((again["scan"]["files_scanned"], again["scan"]["files_cached"]), (1, 0))
        self.assertEqual((after["totals"]["prompts"], after["totals"]["tokens"]), (2, 40))


if __name__ == "__main__":
    unittest.main()
