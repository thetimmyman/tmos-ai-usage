import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pilotdeck

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pilotdeck"
ALPHA = FIXTURES / "session-alpha.jsonl"
TRUNCATED = FIXTURES / "session-truncated.jsonl"
UNSESSIONED = FIXTURES / "session-unsessioned.jsonl"


def lines(*records):
    """A binary JSONL stream from record dicts, so a test can build a log inline."""
    return io.BytesIO(
        b"".join(json.dumps(record).encode() + b"\n" for record in records)
    )


def parse(stream, **options):
    """One log read to its end: the parser accumulates, `finish()` answers."""
    parser = pilotdeck.PilotDeckLogParser(**options)
    parser.read_stream(stream, label="inline")
    return parser, parser.finish()


def parse_together(*streams, **options):
    """Several logs read by one parser, the way a driver log and its egress log are."""
    parser = pilotdeck.PilotDeckLogParser(**options)
    for name, stream in streams:
        parser.read_stream(stream, label=name)
    return parser, parser.finish()


def by_type(rows, call_type):
    return [row for row in rows if row["call_type"] == call_type]


def turn(
    run="run-1",
    session: str | None = "session-x",
    at="2026-01-15T10:00:00.000Z",
    input=100,
    output=10,
    final=True,
    finish="completed",
):
    record = {
        "at": at,
        "kind": "event",
        "seq": 0,
        "final": final,
        "event": {
            "type": "turn_completed",
            "runId": run,
            "finishReason": finish,
            "usage": {"inputTokens": input, "outputTokens": output},
        },
    }
    if session is not None:  # a capture may record no session id at all
        record["sessionKey"] = session
    return record


def egress(at, label=None, port=9931):
    record = {"at": at, "port": port, "method": "POST", "path": "/v1/chat/completions"}
    if label:
        record["call_type"] = label
    return record


class FixtureImportTests(unittest.TestCase):
    def test_user_turn_and_hidden_calls_are_all_captured(self):
        parser, rows = parse(ALPHA.open("rb"))
        self.assertEqual([], parser.rejections)
        self.assertEqual(9, len(rows))
        turns = by_type(rows, pilotdeck.USER_DIRECT)
        self.assertEqual(
            [(1280, 340), (640, 12)],
            [(row["input_tokens"], row["output_tokens"]) for row in turns],
        )
        self.assertEqual({"synthetic-model"}, {row["model"] for row in turns})
        # The memory, title and judge calls the gateway never reports, plus one
        # upstream request the driver did not label.
        self.assertEqual(3, len(by_type(rows, pilotdeck.MEMORY_FETCH)))
        self.assertEqual(1, len(by_type(rows, pilotdeck.TITLE_GENERATION)))
        self.assertEqual(1, len(by_type(rows, pilotdeck.JUDGE_EVALUATION)))
        self.assertEqual(1, len(by_type(rows, pilotdeck.HIDDEN_CALL)))
        self.assertEqual(6, parser.egress_requests - parser.egress_matched)

    def test_every_row_carries_the_required_tags(self):
        _, rows = parse(ALPHA.open("rb"))
        for row in rows:
            self.assertEqual(pilotdeck.APP, row["app"])
            self.assertEqual("session-alpha", row["session_id"])
            self.assertIn(row["call_type"], pilotdeck.CALL_TYPES)
            self.assertIsInstance(row["input_tokens"], (int, type(None)))
            self.assertIsInstance(row["output_tokens"], (int, type(None)))
            self.assertEqual(pilotdeck.RECORD_FIELDS, set(row))
            self.assertEqual(row["record_hash"], pilotdeck.record_hash(row))
            self.assertEqual(
                len(row["source_sha256"]), 64, "every row names the log it came from"
            )

    def test_turn_row_carries_the_gateway_usage_and_its_model(self):
        _, rows = parse(ALPHA.open("rb"))
        first = by_type(rows, pilotdeck.USER_DIRECT)[0]
        self.assertEqual((1280, 340), (first["input_tokens"], first["output_tokens"]))
        self.assertTrue(first["tokens_observed"])
        self.assertEqual("gateway", first["model_source"])
        self.assertEqual("local-endpoint", first["provider"])
        self.assertEqual("success", first["outcome"])
        self.assertEqual(19_906, first["duration_ms"])

    def test_hidden_call_is_counted_but_not_priced_without_a_driver_usage(self):
        _, rows = parse(ALPHA.open("rb"))
        judge = by_type(rows, pilotdeck.JUDGE_EVALUATION)[0]
        self.assertFalse(judge["tokens_observed"])
        self.assertIsNone(judge["input_tokens"])
        self.assertIsNone(judge["output_tokens"])
        # The judge ran on its own model; the session's model is never borrowed for it.
        self.assertEqual("synthetic-judge-model", judge["model"])
        self.assertEqual("driver", judge["model_source"])
        self.assertEqual("local-endpoint", judge["provider"])

    def test_a_driver_labelled_usage_is_kept(self):
        _, rows = parse(ALPHA.open("rb"))
        priced = [
            row
            for row in by_type(rows, pilotdeck.MEMORY_FETCH)
            if row["tokens_observed"]
        ]
        self.assertEqual(1, len(priced))
        self.assertEqual(
            (90, 8), (priced[0]["input_tokens"], priced[0]["output_tokens"])
        )
        self.assertEqual("driver", priced[0]["model_source"])

    def test_subagent_run_is_one_row_without_invented_tokens(self):
        _, rows = parse(ALPHA.open("rb"))
        run = by_type(rows, pilotdeck.SUBAGENT_CALL)
        self.assertEqual(1, len(run))
        self.assertEqual("explore", run[0]["subagent_type"])
        self.assertEqual(9_000, run[0]["duration_ms"])
        self.assertEqual("success", run[0]["outcome"])
        self.assertFalse(run[0]["tokens_observed"])

    def test_prompt_text_never_reaches_a_row(self):
        _, rows = parse(ALPHA.open("rb"))
        self.assertNotIn("SYNTHETIC-SECRET-NEVER-PROJECTED", json.dumps(rows))

    def test_offsets_are_monotonic_within_a_session(self):
        _, rows = parse(ALPHA.open("rb"))
        offsets = [row["session_offset_ms"] for row in rows]
        self.assertEqual(sorted(offsets), offsets)
        self.assertEqual(0, offsets[0])


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name, "pilotdeck-ledger.json")

    def test_the_same_log_twice_adds_nothing(self):
        first = pilotdeck.import_file(ALPHA, self.ledger)
        after_first = self.ledger.read_bytes()
        self.assertEqual(9, first["receipt"]["records_new"])
        self.assertEqual(2010, first["receipt"]["input_tokens"])
        self.assertEqual(360, first["receipt"]["output_tokens"])

        second = pilotdeck.import_file(ALPHA, self.ledger)
        self.assertEqual(0, second["receipt"]["records_new"])
        self.assertEqual(0, second["receipt"]["records_replaced"])
        self.assertEqual(
            0, second["receipt"]["records_known"]
        )  # the source was unchanged, so nothing re-read
        self.assertEqual(1, second["receipt"]["sources_unchanged"])
        self.assertEqual(after_first, self.ledger.read_bytes())

    def test_a_copy_of_the_same_content_adds_nothing(self):
        pilotdeck.import_file(ALPHA, self.ledger)
        copy = Path(self.directory.name, "renamed-copy.jsonl")
        copy.write_bytes(ALPHA.read_bytes())
        report = pilotdeck.import_file(copy, self.ledger)
        # A second file is a second log, but its calls are the same calls.
        self.assertEqual(0, report["receipt"]["records_new"])
        self.assertEqual(9, report["receipt"]["records_known"])
        self.assertEqual(1, report["receipt"]["sources_read"])
        self.assertEqual(
            2,
            report["ledger"]["coverage"]["sources"],
            "both files are recorded as sources",
        )

    def test_an_appended_log_adds_only_the_new_calls(self):
        partial = Path(self.directory.name, "growing.jsonl")
        original = ALPHA.read_bytes().splitlines(keepends=True)
        partial.write_bytes(b"".join(original[:-2]))
        first = pilotdeck.import_file(partial, self.ledger)
        self.assertEqual(7, first["receipt"]["records_new"])
        self.assertEqual(1, first["receipt"]["turns_without_usage"])

        partial.write_bytes(b"".join(original))
        second = pilotdeck.import_file(partial, self.ledger)
        self.assertEqual(2, second["receipt"]["records_new"])
        self.assertEqual(7, second["receipt"]["records_known"])
        self.assertEqual(0, second["receipt"]["turns_without_usage"])
        self.assertEqual(9, second["receipt"]["records_in_ledger"])

    def test_a_provisional_turn_is_replaced_by_its_terminal_event(self):
        partial = Path(self.directory.name, "cut.jsonl")
        head = [
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-p",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-p"},
            },
            turn(run="run-p", session="session-p", final=False),
        ]
        partial.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in head)
        )
        first = pilotdeck.import_file(partial, self.ledger)
        rows = json.loads(self.ledger.read_text())["records"]
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["final_event_missing"])
        self.assertEqual(1, first["receipt"]["records_new"])

        head.append(turn(run="run-p", session="session-p", final=True))
        partial.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in head)
        )
        second = pilotdeck.import_file(partial, self.ledger)
        rows = json.loads(self.ledger.read_text())["records"]
        self.assertEqual(
            1,
            len(rows),
            "the terminal event replaces the provisional row, it does not add one",
        )
        self.assertFalse(rows[0]["final_event_missing"])
        self.assertEqual(1, second["receipt"]["records_replaced"])
        self.assertEqual(0, second["receipt"]["records_new"])

    def test_a_judgement_the_grown_log_overturns_is_retracted(self):
        log = Path(self.directory.name, "egress-first.jsonl")
        head = [
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-s",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-s"},
            },
            egress("2026-01-15T10:00:01.020Z"),
        ]
        log.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in head)
        )
        first = pilotdeck.import_file(log, self.ledger)
        self.assertEqual(
            1,
            first["receipt"]["hidden_calls_detected"],
            "with no gateway event yet, the request looks like a hidden call",
        )

        head.insert(
            1,
            {
                "at": "2026-01-15T10:00:01.000Z",
                "kind": "event",
                "sessionKey": "session-s",
                "seq": 1,
                "final": False,
                "event": {
                    "type": "model_request_started",
                    "model": "synthetic-model",
                    "provider": "local-endpoint",
                    "runId": "run-s",
                },
            },
        )
        head.append(turn(run="run-s", session="session-s"))
        log.write_bytes(
            b"".join(json.dumps(record).encode() + b"\n" for record in head)
        )
        second = pilotdeck.import_file(log, self.ledger)
        rows = json.loads(self.ledger.read_text())["records"]
        self.assertEqual(1, second["receipt"]["records_retracted"])
        self.assertEqual(
            [pilotdeck.USER_DIRECT],
            [row["call_type"] for row in rows],
            "the request the gateway now reports is no longer a hidden call",
        )
        self.assertEqual(1, second["receipt"]["egress_matched_to_gateway"])

    def test_conflicting_content_on_one_key_is_refused(self):
        pilotdeck.import_file(ALPHA, self.ledger)
        conflicting = Path(self.directory.name, "conflict.jsonl")
        conflicting.write_bytes(
            b"".join(
                json.dumps(record).encode() + b"\n"
                for record in [
                    {
                        "at": "2026-01-15T10:00:00.000Z",
                        "kind": "event",
                        "sessionKey": "session-alpha",
                        "seq": 0,
                        "final": False,
                        "event": {"type": "turn_started", "runId": "run-alpha-1"},
                    },
                    turn(
                        run="run-alpha-1",
                        session="session-alpha",
                        input=99_999,
                        output=99_999,
                    ),
                ]
            )
        )
        with self.assertRaisesRegex(ValueError, "conflicting observations"):
            pilotdeck.import_file(conflicting, self.ledger)

    def test_ledger_never_writes_over_a_source_log(self):
        with self.assertRaisesRegex(ValueError, "must not overwrite a source log"):
            pilotdeck.import_file(ALPHA, ALPHA)


class DefensiveParsingTests(unittest.TestCase):
    def test_a_truncated_log_is_rejected_with_a_named_reason(self):
        parser, rows = parse(TRUNCATED.open("rb"))
        codes = {rejection["code"] for rejection in parser.rejections}
        self.assertIn(pilotdeck.TRUNCATED_JSON_AT_EOF, codes)
        truncated = next(
            rejection
            for rejection in parser.rejections
            if rejection["code"] == pilotdeck.TRUNCATED_JSON_AT_EOF
        )
        self.assertEqual(11, truncated["line"])
        self.assertEqual(
            [],
            by_type(rows, pilotdeck.TITLE_GENERATION),
            "the half-written call must never become a row",
        )
        self.assertEqual(1, len(rows), "the calls before the cut are still accounted")
        self.assertEqual(100, rows[0]["input_tokens"])

    def test_each_corrupt_line_is_named_and_nothing_crashes(self):
        parser, rows = parse(TRUNCATED.open("rb"))
        by_line = {
            rejection["line"]: rejection["code"] for rejection in parser.rejections
        }
        self.assertEqual(pilotdeck.INVALID_JSON, by_line[5])
        self.assertEqual(pilotdeck.NOT_AN_OBJECT, by_line[6])
        self.assertEqual(pilotdeck.MISSING_TIMESTAMP, by_line[7])
        self.assertEqual(pilotdeck.INVALID_USAGE, by_line[8])
        self.assertEqual(pilotdeck.UNSUPPORTED_RECORD, by_line[9])
        self.assertEqual(6, len(parser.rejections))
        self.assertEqual(1, len(rows))

    def test_a_truncated_log_is_reported_in_the_receipt_and_the_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory, "ledger.json")
            report = pilotdeck.import_file(TRUNCATED, ledger)
            self.assertEqual(1, report["receipt"]["sources_truncated"])
            self.assertTrue(report["coverage"]["contains_truncated_source"])
            self.assertEqual(6, report["receipt"]["lines_rejected"])
            self.assertEqual(
                1,
                report["receipt"]["rejections_by_code"][
                    pilotdeck.TRUNCATED_JSON_AT_EOF
                ],
            )
            self.assertTrue(report["ledger"]["coverage"]["contains_truncated_source"])

    def test_strict_mode_refuses_the_whole_import(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory, "ledger.json")
            with self.assertRaises(pilotdeck.Rejected) as raised:
                pilotdeck.import_file(TRUNCATED, ledger, strict=True)
            self.assertIn("TRUNCATED_JSON_AT_EOF", str(raised.exception))
            self.assertFalse(
                ledger.exists(), "a refused import leaves the ledger untouched"
            )

    def test_a_log_without_a_session_is_rejected_by_name(self):
        parser, rows = parse(UNSESSIONED.open("rb"))
        codes = [rejection["code"] for rejection in parser.rejections]
        self.assertEqual(3, codes.count(pilotdeck.MISSING_SESSION_ID))
        self.assertEqual(
            [pilotdeck.UNATTRIBUTED_EGRESS],
            [entry["code"] for entry in parser.unattributed],
            "an unplaceable request is named, not counted as a corrupt line",
        )
        self.assertEqual([], rows)

    def test_a_session_on_the_command_line_places_the_same_log(self):
        parser, rows = parse(UNSESSIONED.open("rb"), session_id="session-unsessioned")
        self.assertEqual([], parser.rejections)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, len(by_type(rows, pilotdeck.USER_DIRECT)))
        self.assertEqual("session-unsessioned", rows[0]["session_id"])
        self.assertEqual(
            1, parser.egress_matched, "the log's one request is the gateway's own call"
        )

    def test_a_record_that_contradicts_the_opened_session_is_refused(self):
        parser, rows = parse(lines(turn(session="session-y")), session_id="session-x")
        self.assertEqual(
            [pilotdeck.SESSION_ID_CONFLICT], [r["code"] for r in parser.rejections]
        )
        self.assertEqual([], rows)

    def test_empty_damaged_and_blank_input_does_not_crash(self):
        for payload in (b"", b"\n\n \n", b"\xff\xfe\n", b"not json at all\n"):
            _, rows = parse(io.BytesIO(payload))
            self.assertEqual([], rows)
        parser, _ = parse(io.BytesIO(b"not json at all\n"))
        self.assertEqual(pilotdeck.INVALID_JSON, parser.rejections[0]["code"])

    def test_an_over_long_line_is_named_not_swallowed(self):
        parser, rows = parse(
            lines(
                turn(),
                {
                    "at": "2026-01-15T10:00:01.000Z",
                    "kind": "event",
                    "event": {"type": "assistant_text_delta", "delta": "x" * 500},
                },
            ),
            max_line_bytes=300,
        )
        self.assertEqual(
            [pilotdeck.LINE_TOO_LONG], [r["code"] for r in parser.rejections]
        )
        self.assertEqual(
            1, len(rows), "the over-long line does not take the valid rows with it"
        )

    def test_a_timestamp_without_a_zone_is_refused(self):
        parser, _ = parse(
            lines(
                {
                    "at": "2026-01-15T10:00:00",
                    "kind": "call",
                    "call_type": "memory_fetch",
                    "sessionKey": "session-x",
                    "usage": {"inputTokens": 1},
                }
            )
        )
        self.assertEqual(
            [pilotdeck.MISSING_TIMESTAMP], [r["code"] for r in parser.rejections]
        )

    def test_a_missing_log_names_the_file(self):
        with self.assertRaisesRegex(ValueError, "cannot read absent.jsonl"):
            pilotdeck.import_file(
                Path("/nonexistent/absent.jsonl"),
                Path(tempfile.gettempdir(), "ledger.json"),
            )


class HiddenCallAccountingTests(unittest.TestCase):
    def test_matched_requests_are_not_counted_twice_and_unmatched_ones_are_hidden(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            {
                "at": "2026-01-15T10:00:01.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 1,
                "final": False,
                "event": {
                    "type": "model_request_started",
                    "model": "m",
                    "provider": "p",
                    "runId": "run-1",
                },
            },
            egress("2026-01-15T10:00:01.020Z"),  # the gateway's own call
            egress(
                "2026-01-15T10:00:00.600Z"
            ),  # 400 ms earlier: hidden, must not take its slot
            turn(),
        )
        parser, rows = parse(log)
        self.assertEqual(1, parser.egress_matched)
        self.assertEqual(2, len(rows))
        self.assertEqual(1, len(by_type(rows, pilotdeck.USER_DIRECT)))
        self.assertEqual(1, len(by_type(rows, pilotdeck.HIDDEN_CALL)))
        self.assertEqual(
            100, sum(row["input_tokens"] for row in rows if row["tokens_observed"])
        )

    def test_an_unattributable_request_is_named_not_dropped(self):
        parser, rows = parse(lines(egress("2026-01-15T10:00:00.000Z")))
        self.assertEqual(
            [], parser.rejections, "an unplaceable request is not a corrupt line"
        )
        self.assertEqual(
            [pilotdeck.UNATTRIBUTED_EGRESS],
            [entry["code"] for entry in parser.unattributed],
        )
        self.assertEqual([], rows)
        self.assertEqual(1, parser.egress_requests)

    def test_a_metadata_read_is_control_not_a_model_call(self):
        log = lines(
            {
                "at": "2026-01-15T09:59:59.000Z",
                "port": 9931,
                "method": "GET",
                "path": "/v1/models",
            },
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            turn(),
        )
        parser, rows = parse(log)
        self.assertEqual(1, parser.control)
        self.assertEqual(1, len(rows))

    def test_a_driver_labelled_user_call_is_not_a_second_row(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            egress("2026-01-15T10:00:00.500Z", label="user"),
            turn(),
        )
        parser, rows = parse(log)
        self.assertEqual(1, parser.labelled_user_direct)
        self.assertEqual(1, len(by_type(rows, pilotdeck.USER_DIRECT)))
        self.assertEqual([], by_type(rows, pilotdeck.HIDDEN_CALL))

    def test_a_declared_user_call_beside_its_turn_is_not_counted_twice(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            {
                "at": "2026-01-15T10:00:05.000Z",
                "kind": "call",
                "call_type": "user_direct",
                "sessionKey": "session-x",
                "callId": "call-1",
                "usage": {"inputTokens": 100, "outputTokens": 10},
            },
            turn(),
        )
        parser, rows = parse(log)
        self.assertEqual(1, len(by_type(rows, pilotdeck.USER_DIRECT)))
        self.assertEqual(1, parser.labelled_user_direct)
        self.assertEqual(
            100,
            sum(row["input_tokens"] for row in rows if row["tokens_observed"]),
            "the turn's own report is the price, once",
        )

    def test_a_declared_user_call_without_gateway_turns_still_counts(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:05.000Z",
                "kind": "call",
                "call_type": "user_direct",
                "sessionKey": "session-x",
                "callId": "call-1",
                "usage": {"inputTokens": 100, "outputTokens": 10},
            }
        )
        parser, rows = parse(log)
        self.assertEqual(1, len(by_type(rows, pilotdeck.USER_DIRECT)))
        self.assertEqual(0, parser.labelled_user_direct)

    def test_an_unknown_label_is_counted_and_kept_rather_than_guessed(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            egress("2026-01-15T10:00:02.000Z", label="brand_new_sub_call"),
            turn(),
        )
        parser, rows = parse(log)
        hidden = by_type(rows, pilotdeck.HIDDEN_CALL)
        self.assertEqual(1, len(hidden))
        self.assertEqual("brand_new_sub_call", hidden[0]["label"])
        self.assertEqual(1, parser.unlabelled_calls)

    def test_an_out_of_window_request_is_not_attributed(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            turn(),
            egress("2026-01-15T11:00:00.000Z", label="title_generation"),
        )
        parser, rows = parse(log, hidden_window_ms=60_000)
        self.assertEqual(
            [pilotdeck.UNATTRIBUTED_EGRESS],
            [entry["code"] for entry in parser.unattributed],
        )
        self.assertEqual([], by_type(rows, pilotdeck.TITLE_GENERATION))

    def test_a_driver_log_and_its_egress_log_are_read_together(self):
        driver = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            {
                "at": "2026-01-15T10:00:01.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 1,
                "final": False,
                "event": {
                    "type": "model_request_started",
                    "model": "m",
                    "provider": "p",
                    "runId": "run-1",
                },
            },
            turn(),
        )
        requests = lines(
            egress("2026-01-15T10:00:01.020Z"), egress("2026-01-15T10:00:20.000Z")
        )

        parser, rows = parse_together(("driver", driver), ("egress", requests))
        self.assertEqual(1, parser.egress_matched)
        self.assertEqual([], parser.unattributed)
        self.assertEqual(
            1,
            len(by_type(rows, pilotdeck.HIDDEN_CALL)),
            "the request the gateway never reported is a hidden call",
        )

        alone, _ = parse(io.BytesIO(requests.getvalue()))
        self.assertEqual([], alone.rejections)
        self.assertEqual(
            2,
            len(alone.unattributed),
            "an egress stream read by itself cannot name a session",
        )

    def test_a_shared_egress_log_counts_each_request_once(self):
        session_a = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-a",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-a"},
            },
            {
                "at": "2026-01-15T10:00:01.000Z",
                "kind": "event",
                "sessionKey": "session-a",
                "seq": 1,
                "final": False,
                "event": {
                    "type": "model_request_started",
                    "model": "m",
                    "provider": "p",
                    "runId": "run-a",
                },
            },
            turn(run="run-a", session="session-a", at="2026-01-15T10:00:10.000Z"),
        )
        session_b = lines(
            {
                "at": "2026-01-15T10:05:00.000Z",
                "kind": "event",
                "sessionKey": "session-b",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-b"},
            },
            {
                "at": "2026-01-15T10:05:01.000Z",
                "kind": "event",
                "sessionKey": "session-b",
                "seq": 1,
                "final": False,
                "event": {
                    "type": "model_request_started",
                    "model": "m",
                    "provider": "p",
                    "runId": "run-b",
                },
            },
            turn(run="run-b", session="session-b", at="2026-01-15T10:05:10.000Z"),
        )
        requests = lines(
            egress("2026-01-15T10:00:01.020Z"),
            egress("2026-01-15T10:00:20.000Z"),
            egress("2026-01-15T10:05:01.020Z"),
        )
        with tempfile.TemporaryDirectory() as directory:
            logs = {}
            for name, stream in (
                ("a.jsonl", session_a),
                ("b.jsonl", session_b),
                ("egress.jsonl", requests),
            ):
                logs[name] = Path(directory, name)
                logs[name].write_bytes(stream.getvalue())
            report = pilotdeck.import_paths(
                [logs["a.jsonl"], logs["b.jsonl"], logs["egress.jsonl"]],
                Path(directory, "ledger.json"),
            )
        receipt = report["receipt"]
        self.assertEqual(3, receipt["egress_requests"])
        self.assertEqual(2, receipt["egress_matched_to_gateway"])
        self.assertEqual(1, receipt["hidden_calls_detected"])
        self.assertEqual(0, receipt["egress_unattributed"])
        self.assertEqual(
            3,
            receipt["egress_requests"],
            "matched + hidden + unattributed accounts for every request exactly once",
        )
        self.assertEqual(3, receipt["sources_read"])
        self.assertEqual(2, report["ledger"]["coverage"]["sessions"])

    def test_a_capture_without_session_ids_can_name_each_log_a_session(self):
        capture = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            turn(session=None, at="2026-01-15T10:00:10.000Z"),
        )
        parser = pilotdeck.PilotDeckLogParser(session_per_log=True)
        parser.read_stream(capture, label="attempt-7.jsonl")
        rows = parser.finish()
        self.assertEqual([], parser.rejections)
        self.assertEqual(["attempt-7"], [row["session_id"] for row in rows])

    def test_session_per_log_does_not_place_a_shared_egress_stream(self):
        parser = pilotdeck.PilotDeckLogParser(session_per_log=True)
        parser.read_stream(
            lines(egress("2026-01-15T10:00:00.000Z")), label="egress-requests.jsonl"
        )
        self.assertEqual([], parser.finish())
        self.assertEqual(1, len(parser.unattributed))

    def test_a_labelled_request_keeps_its_purpose_and_its_model(self):
        """The forwarder names memory and title calls from their own body markers."""
        memory = egress("2026-01-15T10:00:00.300Z", label="memory_fetch")
        memory.update(
            {
                "label_source": "response_format",
                "model": "synthetic-memory-model",
                "stream": False,
                "messages": 2,
                "roles": "system,user",
                "tools": None,
                "response_format": "json_object",
                "purpose": None,
            }
        )
        title = egress("2026-01-15T10:00:25.000Z", label="title_generation")
        title.update(
            {
                "label_source": "metadata.purpose",
                "model": "synthetic-model",
                "max_tokens": 4096,
                "purpose": "session_title_generation",
            }
        )
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            memory,
            turn(at="2026-01-15T10:00:20.000Z"),
            title,
        )
        _, rows = parse(log)
        found = {
            row["call_type"]: row
            for row in rows
            if row["call_type"] != pilotdeck.USER_DIRECT
        }
        self.assertEqual(
            {pilotdeck.MEMORY_FETCH, pilotdeck.TITLE_GENERATION}, set(found)
        )
        self.assertEqual(
            "synthetic-memory-model",
            found[pilotdeck.MEMORY_FETCH]["model"],
            "the model now comes from the request body, not from an inference",
        )
        self.assertEqual("driver", found[pilotdeck.MEMORY_FETCH]["model_source"])
        self.assertEqual(
            "response_format",
            found[pilotdeck.MEMORY_FETCH]["label_source"],
            "the row says which marker named it",
        )
        self.assertEqual(
            "metadata.purpose", found[pilotdeck.TITLE_GENERATION]["label_source"]
        )
        self.assertEqual("title_generation", found[pilotdeck.TITLE_GENERATION]["label"])

    def test_a_request_whose_body_was_not_read_is_still_counted(self):
        unread = egress("2026-01-15T10:00:16.400Z")
        unread["body"] = "unread"  # oversized, or a client that left mid-request
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            unread,
            turn(at="2026-01-15T10:00:20.000Z"),
        )
        _, rows = parse(log)
        hidden = by_type(rows, pilotdeck.HIDDEN_CALL)
        self.assertEqual(
            1, len(hidden), "a request counted without its body is still a hidden call"
        )
        self.assertIsNone(hidden[0]["model"])

    def test_a_session_id_names_the_driver_log_not_a_shared_stream(self):
        requests = lines(egress("2026-01-15T10:00:00.000Z"))
        parser, rows = parse(requests, session_id="session-x")
        self.assertEqual([], rows, "without turn windows the request cannot be placed")
        self.assertEqual(1, len(parser.unattributed))

    def test_a_gateway_call_event_with_usage_is_kept(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            },
            {
                "at": "2026-01-15T10:00:00.500Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 1,
                "final": False,
                "event": {
                    "type": "judge_completed",
                    "model": "judge-model",
                    "usage": {"inputTokens": 40, "outputTokens": 4},
                },
            },
            turn(),
        )
        _, rows = parse(log)
        judge = by_type(rows, pilotdeck.JUDGE_EVALUATION)
        self.assertEqual(1, len(judge))
        self.assertTrue(judge[0]["tokens_observed"])
        self.assertEqual((40, 4), (judge[0]["input_tokens"], judge[0]["output_tokens"]))
        self.assertEqual("judge-model", judge[0]["model"])

    def test_turns_without_a_terminal_usage_are_counted_not_invented(self):
        log = lines(
            {
                "at": "2026-01-15T10:00:00.000Z",
                "kind": "event",
                "sessionKey": "session-x",
                "seq": 0,
                "final": False,
                "event": {"type": "turn_started", "runId": "run-1"},
            }
        )
        parser, rows = parse(log)
        self.assertEqual([], rows)
        self.assertEqual(1, parser.turns_without_usage)


class ReceiptTests(unittest.TestCase):
    def test_the_receipt_records_lines_hidden_calls_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            report = pilotdeck.import_file(ALPHA, Path(directory, "ledger.json"))
        receipt = report["receipt"]
        self.assertEqual(30, receipt["lines_processed"])
        self.assertEqual(0, receipt["lines_rejected"])
        self.assertEqual(6, receipt["hidden_calls_detected"])
        self.assertEqual(
            {
                "memory_fetch": 3,
                "title_generation": 1,
                "judge_evaluation": 1,
                "hidden_call": 1,
            },
            receipt["hidden_calls"],
        )
        self.assertEqual(2, receipt["user_turns"])
        self.assertEqual(1, receipt["subagent_calls"])
        self.assertEqual(9, receipt["egress_requests"])
        self.assertEqual(3, receipt["egress_matched_to_gateway"])
        self.assertEqual(2010, receipt["input_tokens"])
        self.assertEqual(360, receipt["output_tokens"])
        self.assertEqual(
            2, receipt["tokens_by_call_type"]["user_direct"]["tokens_observed_rows"]
        )
        self.assertEqual(6, receipt["rows_without_tokens"])

    def test_the_receipt_is_deterministic_for_the_same_input(self):
        with tempfile.TemporaryDirectory() as directory:
            first = pilotdeck.import_file(ALPHA, Path(directory, "a.json"))
            second = pilotdeck.import_file(ALPHA, Path(directory, "b.json"))
        self.assertEqual(json.dumps(first), json.dumps(second))

    def test_the_summary_states_what_it_cannot_price(self):
        with tempfile.TemporaryDirectory() as directory:
            report = pilotdeck.import_file(ALPHA, Path(directory, "ledger.json"))
        summary = report["ledger"]
        self.assertEqual(1, summary["coverage"]["sessions"])
        self.assertFalse(summary["coverage"]["complete"])
        self.assertIn("not observable", summary["note"])
        self.assertEqual(6, summary["totals"]["hidden_calls"])
        self.assertEqual(2010, summary["totals"]["input_tokens"]["sum"])

    def test_the_summary_command_writes_the_dashboard_document(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger, summary = (
                Path(directory, "ledger.json"),
                Path(directory, "summary.json"),
            )
            pilotdeck.main(
                [
                    "--log",
                    str(ALPHA),
                    "--ledger",
                    str(ledger),
                    "--summary",
                    str(summary),
                ]
            )
            document = json.loads(summary.read_text())
        self.assertEqual(2, document["coverage"]["sessions"] + 1)
        self.assertEqual(9, document["totals"]["records"])

    def test_the_cli_refuses_to_write_the_summary_over_a_source(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory, "ledger.json")
            with self.assertRaises(SystemExit) as raised:
                pilotdeck.main(
                    [
                        "--log",
                        str(ALPHA),
                        "--ledger",
                        str(ledger),
                        "--summary",
                        str(ALPHA),
                    ]
                )
            self.assertEqual(2, raised.exception.code)


class SqlProjectionTests(unittest.TestCase):
    """`pilotdeck_usage.sql` is the row's SQL projection; these keep it in step.

    Both the schema and the upsert are read from that file and executed as they
    are written: there is no SQL built here, so a field added to the ledger without
    a column or a parameter fails these tests instead of drifting silently.
    """

    def setUp(self):
        text = "\n".join(
            line
            for line in (Path(__file__).resolve().parent / "pilotdeck_usage.sql")
            .read_text()
            .splitlines()
            if not line.lstrip().startswith("--")
        )
        self.statements = [
            part.strip() + ";" for part in text.split(";") if part.strip()
        ]
        self.upsert = next(
            statement
            for statement in self.statements
            if statement.startswith("INSERT INTO")
        )
        self.schema = [
            statement
            for statement in self.statements
            if not statement.startswith("INSERT INTO")
        ]

    def test_the_table_has_exactly_the_ledger_columns(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript("\n".join(self.schema))
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(pilotdeck_usage)")
        }
        self.assertEqual(set(pilotdeck.RECORD_FIELDS), columns)

    def test_the_identity_is_the_primary_key(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript("\n".join(self.schema))
        keys = [
            row[1]
            for row in connection.execute("PRAGMA table_info(pilotdeck_usage)")
            if row[5]
        ]
        self.assertEqual(["session_id", "call_hash"], keys)
        self.assertIn("ON CONFLICT (session_id, call_hash) DO NOTHING", self.upsert)

    def test_the_upsert_binds_every_ledger_field(self):
        bound = set(re.findall(r":([a-z_0-9]+)", self.upsert))
        self.assertEqual(set(pilotdeck.RECORD_FIELDS), bound)

    def test_a_double_import_adds_no_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory, "ledger.json")
            pilotdeck.import_file(ALPHA, ledger)
            records = json.loads(ledger.read_text())["records"]
        connection = sqlite3.connect(":memory:")
        connection.executescript("\n".join(self.schema))
        for _ in range(2):
            connection.executemany(self.upsert, records)
        self.assertEqual(
            len(records),
            connection.execute("SELECT COUNT(*) FROM pilotdeck_usage").fetchone()[0],
        )
        self.assertEqual(
            7,
            connection.execute(
                "SELECT COUNT(*) FROM pilotdeck_usage WHERE call_type <> 'user_direct'"
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
