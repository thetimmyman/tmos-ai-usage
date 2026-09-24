import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage_collector as collector


class CodexStatsTest(unittest.TestCase):
    def contribution(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-test.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return collector._codex_contribution(path, datetime(2026, 9, 24, tzinfo=timezone.utc))

    def totals(self, result):
        day_tokens = sum(row["tokens"] for row in result["days"].values())
        prompts = sum(row["prompts"] for row in result["days"].values())
        model_tokens = sum(row["total_tokens"] for row in result["models"].values())
        return day_tokens, prompts, model_tokens

    def test_response_usage_records_win_over_repeated_token_count_snapshots(self):
        usage1 = {"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 30,
                  "cache_write_input_tokens": 5, "total_tokens": 110}
        usage2 = {"input_tokens": 80, "output_tokens": 20, "cached_input_tokens": 25,
                  "cache_write_input_tokens": 3, "total_tokens": 100}
        cumulative = {"total_tokens": 210}
        rows = [
            {"type": "turn_context", "timestamp": "2026-09-24T12:00:00Z", "payload": {"model": "test-model"}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:01Z", "payload": {"type": "task_started"}},
            {"type": "token_usage_record", "timestamp": "2026-09-24T12:00:02Z", "payload": {
                "response_id": "response-a", "usage": usage1, "turn_token_usage": usage1,
                "thread_token_usage": {"total_tokens": 110}}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:02Z", "payload": {"type": "token_count", "info": {
                "last_token_usage": usage1, "total_token_usage": {"total_tokens": 110}}}},
            {"type": "token_usage_record", "timestamp": "2026-09-24T12:00:03Z", "payload": {
                "response_id": "response-b", "usage": usage2,
                "turn_token_usage": cumulative, "thread_token_usage": cumulative}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:03Z", "payload": {"type": "token_count", "info": {
                "last_token_usage": usage2, "total_token_usage": cumulative}}},
            # A repeated canonical record is deduplicated by response_id.
            {"type": "token_usage_record", "timestamp": "2026-09-24T12:00:04Z", "payload": {
                "response_id": "response-b", "usage": usage2}},
        ]
        result = self.contribution(rows)
        self.assertEqual(self.totals(result), (210, 1, 210))
        model = result["models"]["test-model"]
        self.assertEqual(model["input_tokens"], 180)
        self.assertEqual(model["cache_read_tokens"], 55)
        self.assertEqual(model["cache_write_tokens"], 8)

    def test_legacy_snapshots_skip_duplicate_cumulative_total(self):
        first = {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100}
        second = {"input_tokens": 120, "output_tokens": 15, "total_tokens": 135}
        rows = [
            {"type": "turn_context", "timestamp": "2026-09-24T12:00:00Z", "payload": {"model": "legacy-model"}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:01Z", "payload": {"type": "task_started"}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:02Z", "payload": {"type": "token_count", "info": {
                "last_token_usage": first, "total_token_usage": {"total_tokens": 100}}}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:03Z", "payload": {"type": "token_count", "info": {
                "last_token_usage": first, "total_token_usage": {"total_tokens": 100}}}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:04Z", "payload": {"type": "token_count", "info": {
                "last_token_usage": second, "total_token_usage": {"total_tokens": 235}}}},
        ]
        result = self.contribution(rows)
        self.assertEqual(self.totals(result), (235, 1, 235))

    def test_legacy_snapshots_without_running_total_deduplicate_adjacent_repeat(self):
        usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
        rows = [
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:01Z", "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
            {"type": "event_msg", "timestamp": "2026-09-24T12:00:02Z", "payload": {"type": "token_count", "info": {"last_token_usage": usage}}},
        ]
        self.assertEqual(self.totals(self.contribution(rows)), (15, 0, 0))


if __name__ == "__main__":
    unittest.main()
