import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import usage_collector as collector


NOW = datetime(2026, 6, 30, 12, tzinfo=timezone.utc)


class UsageHistoryTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.home = Path(scratch.name)
        self.state = self.home / "state"
        self.state.mkdir()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": ""}))
        self.enterContext(patch.object(collector, "STATE_DIR", self.state))

    def claude(self, name, days_ago, tokens, root=None):
        path = (root or self.home / ".claude/projects") / (name + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        when = NOW - timedelta(days=days_ago)
        row = {"timestamp": when.isoformat(), "message": {"usage": {"input_tokens": tokens}}}
        path.write_text(json.dumps(row) + "\n")
        os.utime(path, (when.timestamp(), when.timestamp()))
        return path

    def test_busy_window_includes_older_sessions(self):
        self.claude("outside", 32, 10000)
        self.claude("older", 29, 27)
        for index in range(600):
            self.claude(f"recent-{index}", 0, 1)
        stats = collector.stats_for("claude-code", NOW)
        self.assertEqual([(r["date"], r["tokens"]) for r in stats["daily"]],
                         [("2026-06-01", 27), ("2026-06-30", 600)])

    @unittest.skipUnless(shutil.which("zstd"), "requires the zstd command")
    def test_compressed_rollout_and_plain_twin_count_once(self):
        root = self.home / ".codex/sessions"
        root.mkdir(parents=True)
        row = {"type": "token_usage_record", "timestamp": NOW.isoformat(),
               "payload": {"usage": {"total_tokens": 19}}}
        data = json.dumps(row).encode() + b"\n"
        packed = subprocess.check_output(["zstd", "-qc"], input=data)
        (root / "rollout-archived.jsonl.zst").write_bytes(packed)
        (root / "rollout-active.jsonl.zst").write_bytes(packed)
        (root / "rollout-active.jsonl").write_bytes(data)
        (root / "rollout-bad.jsonl.zst").write_bytes(packed[:-2])
        stats = collector.stats_for("codex", NOW)
        self.assertEqual(stats["totals"]["tokens"], 38)
        self.assertEqual(stats["totals"]["sessions"], 2)
        self.assertIn("incomplete", stats["note"].lower())

    def test_claude_roots_share_sessions_and_keep_distinct_agents(self):
        primary = self.home / ".claude/projects"
        secondary = self.home / "alternative/projects"
        configured = self.home / "configured"
        shared = self.claude("original", 2, 70, primary)
        row = json.loads(shared.read_text())
        row["sessionId"] = "shared-session"
        shared.write_text(json.dumps(row))
        duplicate = self.claude("renamed-copy", 2, 70, secondary)
        duplicate.write_text(json.dumps(row))
        agent = self.claude("agent-child", 2, 5, secondary)
        row["agentId"] = "child"
        row["message"]["usage"]["input_tokens"] = 5
        agent.write_text(json.dumps(row))
        self.claude("separate", 1, 11, secondary)
        self.claude("another", 1, 2, configured / "projects")
        (self.state / "history-sources.json").write_text(json.dumps(
            {"claude_projects": [str(secondary), str(primary)]}))
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(configured)}):
            stats = collector.stats_for("claude-code", NOW)
        self.assertEqual(stats["totals"]["tokens"], 88)
        self.assertEqual(stats["totals"]["sessions"], 4)

    def test_pruned_days_survive_while_recomputed_days_replace_stored_counts(self):
        pruned = self.claude("older", 29, 45)
        self.claude("current", 0, 24)
        first = collector.stats_for("claude-code", NOW)
        self.assertEqual(first["totals"]["tokens"], 69)
        pruned.unlink()
        self.claude("current", 0, 4)
        second = collector.stats_for("claude-code", NOW)
        self.assertEqual(second["totals"]["tokens"], 49)
        self.assertEqual(second["daily"][0], first["daily"][0])
        self.assertEqual(second["today"]["tokens"], 4)
        self.assertFalse(collector.stats_for("codex", NOW)["available"])
        later = collector.stats_for("claude-code", NOW + timedelta(days=2))
        self.assertEqual([row["tokens"] for row in later["daily"]], [4])

    @unittest.skipUnless(shutil.which("zstd"), "requires the zstd command")
    def test_incomplete_archive_read_keeps_previous_day_until_retry_succeeds(self):
        root = self.home / ".codex/sessions"
        root.mkdir(parents=True)
        row = {"type": "token_usage_record", "timestamp": NOW.isoformat(),
               "payload": {"usage": {"total_tokens": 9}}}
        data = json.dumps(row).encode() + b"\n"
        path = root / "rollout-closed.jsonl.zst"
        path.write_bytes(subprocess.check_output(["zstd", "-qc"], input=data))
        (root / "rollout-live.jsonl").write_bytes(data)
        self.assertEqual(collector.stats_for("codex", NOW)["today"]["tokens"], 18)
        path.write_bytes(subprocess.check_output(["zstd", "-qc"], input=data + b" " * 200))
        with patch.object(collector, "MAX_ROLLOUT_BYTES", len(data)):
            limited = collector.stats_for("codex", NOW)
        self.assertEqual(limited["today"]["tokens"], 18)
        self.assertIn("incomplete", limited["note"].lower())
        self.assertEqual(collector.stats_for("codex", NOW)["today"]["tokens"], 18)

    def test_stored_history_outlives_the_chart_window(self):
        source = self.claude("older", 0, 7)
        collector.stats_for("claude-code", NOW)
        source.unlink()
        later = collector.stats_for("claude-code", NOW + timedelta(days=89))
        self.assertFalse(later["available"])
        history = self.state / "daily-claude-code.json"
        self.assertEqual(json.loads(history.read_text())["2026-06-30"]["tokens"], 7)
        collector.stats_for("claude-code", NOW + timedelta(days=91))
        self.assertEqual(json.loads(history.read_text()), {})
