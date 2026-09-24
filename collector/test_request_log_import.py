import json
import tempfile
import unittest
from pathlib import Path

from request_log_import import digest, import_file, merge, normalize, parse_export, summarize


def row(**updates):
    value = dict(id="log-a", requestID="shared-request", workspaceID="workspace",
                 sessionID="session", category="inference", provider="opencode", model="model-a",
                 requestedModel="model-a", product="go", app="pi", outcome="succeeded",
                 startedAt=1000, finishedAt=2000, durationMs=1000, timeToFirstTokenMs=100,
                 statusCode=200, attemptCount=2, cost=0.0, inputTokens=10, outputTokens=5,
                 reasoningTokens=2, cacheReadTokens=100, cacheWriteTokens=0,
                 attempts=[dict(provider="upstream", model="model-a", statusCode=429, durationMs=50),
                           dict(provider="upstream", model="model-a", statusCode=200, durationMs=950)],
                 requestHeaders={"authorization": "private-value"}, metadata={"prompt": "private-value"})
    value.update(updates)
    return value


def parsed(*rows):
    return parse_export(json.dumps(dict(items=list(rows), truncated=True, until=3000)).encode())


def ledger(*rows):
    source, records = parsed(*rows)
    return merge(dict(schema_version=1, sources=[], records=[]), source, records)


class ImportTests(unittest.TestCase):
    def test_log_id_not_repeated_request_id_is_identity(self):
        data = ledger(row(), row(id="log-b"))
        self.assertEqual(2, len(data["records"]))
        source, records = parsed(row(id="log-b"))
        self.assertEqual(2, len(merge(data, source, records)["records"]))

    def test_retry_errors_are_not_failed_task_or_request(self):
        report = summarize(ledger(row()))
        totals = report["totals"]
        self.assertEqual(1, totals["successful_requests"])
        self.assertEqual(1, totals["attempt_http_errors"])
        self.assertEqual(1, totals["attempt_rate_limits"])
        self.assertEqual(1, totals["observed_retry_attempts"])
        self.assertIsNone(totals["validated_tasks"])
        self.assertTrue(report["coverage"]["contains_truncated_export"])
        self.assertFalse(report["coverage"]["complete_period"])

    def test_privacy_allowlist(self):
        result = json.dumps(ledger(row()))
        for private in ["private-value", "requestHeaders", "metadata", '"workspace"', '"session"']:
            self.assertNotIn(private, result)

    def test_unknown_is_not_zero_and_tokens_are_not_summed(self):
        totals = summarize(ledger(row(cost=None, inputTokens=None)))["totals"]
        self.assertIsNone(totals["reported_cost_sum"])
        self.assertEqual(0, totals["requests_with_cost"])
        self.assertIsNone(totals["tokens"]["inputTokens"]["sum"])
        self.assertNotIn("total_tokens", totals)
        zero = summarize(ledger(row()))["totals"]
        self.assertEqual(0, zero["reported_cost_sum"])
        self.assertEqual(1, zero["requests_with_cost"])

    def test_conflict_and_corruption_fail(self):
        existing = ledger(row())
        source, records = parsed(row(cost=2))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            merge(existing, source, records)
        existing["records"][0]["reported_cost"] = 3
        with self.assertRaisesRegex(ValueError, "hash"):
            merge(existing, source, [])

    def test_bad_export_and_invalid_numbers_fail(self):
        for value in [-1, float("nan"), float("inf"), True]:
            with self.assertRaises(ValueError):
                normalize(row(cost=value))
        with self.assertRaises(ValueError):
            normalize(row(attemptCount=3))
        with self.assertRaises(ValueError):
            parse_export(b'{"items":[]}')
        with self.assertRaises(ValueError):
            normalize(row(startedAt=None))
        with self.assertRaisesRegex(ValueError, "final attempt"):
            normalize(row(statusCode=500))

    def test_contaminated_ledger_is_rejected_even_with_recomputed_hash(self):
        data = ledger(row())
        record = data["records"][0]
        record["requestHeaders"] = {"authorization": "private-value"}
        record["record_hash"] = digest({k: v for k, v in record.items() if k != "record_hash"})
        source, rows = parsed(row())
        with self.assertRaisesRegex(ValueError, "fields"):
            merge(data, source, rows)

    def test_atomic_idempotent_import_preserves_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "export.json"
            target = Path(folder) / "ledger.json"
            source.write_text(json.dumps(dict(items=[row()], truncated=True, until=3000)))
            original = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "source export"):
                import_file(source, source)
            first = import_file(source, target)
            second = import_file(source, target)
            self.assertEqual(first, second)
            self.assertEqual(original, source.read_bytes())
            self.assertEqual(0o600, target.stat().st_mode & 0o777)
            before = target.read_bytes()
            source.write_text(json.dumps(dict(items=[row(cost=2)], truncated=True, until=3000)))
            with self.assertRaises(ValueError):
                import_file(source, target)
            self.assertEqual(before, target.read_bytes())


if __name__ == "__main__":
    unittest.main()
