#!/usr/bin/env python3
"""Import a PilotDeck driver log as a private, deduplicated usage ledger.

A PilotDeck gateway reports usage for the turn a user asked for and nothing
else. Its memory gate and indexer, its session-title synthesis and (when routed)
its tokenSaver judge reach the model endpoint without a gateway event: measured
on the evaluation run, the endpoint saw 2.4x to 3.7x as many chat completions as
the gateway reported, and their tokens are in neither `turn_completed.usage` nor
`router/stats.jsonl`. A cost ledger that reads only the gateway undercounts by
that factor, so this source reads two streams:

- the gateway event stream, which yields one row per `turn_completed` (the
  user-facing call, with the tokens the gateway reported), one row per sub-agent
  run, and a row for any call the driver announces with usage of its own;
- the egress request log (the forwarder in front of the endpoint), which yields
  one row per upstream request the gateway did not report. Both streams are read
  by one parser in one import, because a request is only hidden relative to the
  turns recorded beside it: a shared egress stream split across imports would be
  counted once per session, and none of it could be attributed at all.

What the log can and cannot say:

- **A hidden call is counted, never priced, unless the driver priced it.** The
  egress log records method, path, port and time, never a body and never usage.
  A hidden row therefore has `tokens_observed: false` and null token columns
  unless the driver labelled that request with usage. Token totals here are a
  lower bound on what the endpoint was asked to do; the calls are the part the
  gateway cannot see, and they are complete.
- **A request the gateway counted is not a second row.** An egress request that
  matches a gateway `model_request_started` within a small window is the call
  the turn row already carries, so it is counted as matched, not re-rowed. That
  one-to-one time match is a heuristic: a hidden call that starts within a few
  milliseconds of a gateway request could be labelled as either, but the request
  is counted exactly once either way, so totals stay right.
- **Purpose comes from the driver, or stays unnamed.** PilotDeck's own event
  stream does not say which sub-call is memory, title or judge, and the egress
  path is `/v1/chat/completions` for all of them. A hidden row is therefore
  `memory_fetch`, `title_generation`, `judge_evaluation` or `subagent_call` only
  when the driver labels it; otherwise it is `hidden_call` with the raw label
  kept. Nothing is guessed, and nothing is dropped.
- **A model is never inferred.** The evaluation showed the tokenSaver judge on a
  different model from the turn, so a session's model is not the hidden calls'
  model. `model` is null where the log does not report one, and
  `model_source` says whether the gateway or the driver supplied it.

Idempotency is by call identity, never by line number or import counter:
`call_hash` is a digest of the event's own identifiers (a turn's `runId`, a
sub-agent's `subagentId`, an egress request's time and ordinal), and the ledger
key is `(session_id, call_hash)`. Re-importing a log, or an appended copy of it,
adds nothing; a provisional row (a turn or sub-agent whose terminal event had
not been written yet) is replaced by the final one when the log grows; anything
else that disagrees on one key is refused for reconciliation rather than
silently overwritten.

The ledger is a private JSON file in the state directory, like the billing and
outcome ledgers; nothing here adds a database. `pilotdeck_usage.sql` is the SQL
projection of the same row, with the unique `(session_id, call_hash)`
constraint and `ON CONFLICT ... DO NOTHING`, for a consumer that stores it in
one.

    pilotdeck.py --log driver.jsonl --ledger ~/.local/state/tmos-ai-usage/pilotdeck-ledger.json
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

APP = "pilotdeck"
SCHEMA_VERSION = 1

USER_DIRECT = "user_direct"
MEMORY_FETCH = "memory_fetch"
TITLE_GENERATION = "title_generation"
JUDGE_EVALUATION = "judge_evaluation"
SUBAGENT_CALL = "subagent_call"
HIDDEN_CALL = "hidden_call"
CALL_TYPES = (
    USER_DIRECT,
    MEMORY_FETCH,
    TITLE_GENERATION,
    JUDGE_EVALUATION,
    SUBAGENT_CALL,
    HIDDEN_CALL,
)
HIDDEN_CALL_TYPES = (MEMORY_FETCH, TITLE_GENERATION, JUDGE_EVALUATION, HIDDEN_CALL)

ROLE_MAIN = "main"
ROLE_SUBAGENT = "subagent"
ROLE_BACKGROUND = "background"

OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_UNKNOWN = "unknown"

# A driver's own name for a call, mapped to the ledger's call type. Aliases are
# whole words because the label is the driver's, not this module's.
LABELS = {
    "user": USER_DIRECT,
    "user_direct": USER_DIRECT,
    "turn": USER_DIRECT,
    "turn_completed": USER_DIRECT,
    "memory": MEMORY_FETCH,
    "memory_fetch": MEMORY_FETCH,
    "memory_retrieval": MEMORY_FETCH,
    "memory_gate": MEMORY_FETCH,
    "memory_extraction": MEMORY_FETCH,
    "memory_index": MEMORY_FETCH,
    "title": TITLE_GENERATION,
    "title_generation": TITLE_GENERATION,
    "auto_title": TITLE_GENERATION,
    "session_title": TITLE_GENERATION,
    "judge": JUDGE_EVALUATION,
    "judge_evaluation": JUDGE_EVALUATION,
    "council": JUDGE_EVALUATION,
    "judge_pass": JUDGE_EVALUATION,
    "token_saver_judge": JUDGE_EVALUATION,
    "subagent": SUBAGENT_CALL,
    "subagent_call": SUBAGENT_CALL,
    "subagent_run": SUBAGENT_CALL,
}

# Gateway events that announce a call of their own with usage. The stream that
# was measured never emits these; a driver that logs more than it did uses them,
# and their usage is retained rather than dropped on the floor.
EVENT_CALLS = {
    "memory_fetch_completed": MEMORY_FETCH,
    "memory_retrieval_completed": MEMORY_FETCH,
    "title_generation_completed": TITLE_GENERATION,
    "title_completed": TITLE_GENERATION,
    "judge_completed": JUDGE_EVALUATION,
    "judge_evaluation_completed": JUDGE_EVALUATION,
}

# Rejection codes. Every rejected line names one of these; none is silent.
TRUNCATED_JSON_AT_EOF = "TRUNCATED_JSON_AT_EOF"
INVALID_JSON = "INVALID_JSON"
NOT_AN_OBJECT = "NOT_AN_OBJECT"
LINE_TOO_LONG = "LINE_TOO_LONG"
UNSUPPORTED_RECORD = "UNSUPPORTED_RECORD"
MISSING_SESSION_ID = "MISSING_SESSION_ID"
SESSION_ID_CONFLICT = "SESSION_ID_CONFLICT"
MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
INVALID_USAGE = "INVALID_USAGE"
UNATTRIBUTED_EGRESS = "UNATTRIBUTED_EGRESS"

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
TOKEN_SOURCES = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_tokens": "cacheReadTokens",
    "cache_write_tokens": "cacheWriteTokens",
    "reasoning_tokens": "reasoningTokens",
}
RECORD_FIELDS = frozenset(
    {
        "app",
        "session_id",
        "call_type",
        "call_hash",
        "source_sha256",
        "role",
        "model",
        "label_source",
        "model_source",
        "provider",
        "subagent_type",
        "label",
        *TOKEN_FIELDS,
        "tokens_observed",
        "started_at_ms",
        "finished_at_ms",
        "duration_ms",
        "session_offset_ms",
        "outcome",
        "final_event_missing",
        "record_hash",
    }
)
RECORD_HASH_EXCLUDED = ("record_hash", "session_offset_ms", "source_sha256")
# ^ derived or provenance: a re-sorted ledger and an appended log must keep a call's hash
SOURCE_FIELDS = frozenset(
    {"source_key", "sha256", "label", "lines", "rejected", "truncated", "supersedes"}
)

HIDDEN_WINDOW_MS = 300_000  # an egress request this close to a session's turns may be its background work
MATCH_WINDOW_MS = (
    1_000  # egress and gateway clocks agreed to within 30 ms in the measured run
)
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_REPORTED_REJECTIONS = 100  # the receipt names the first ones; the counts cover all
CONTROL_KINDS = frozenset(
    {"hello_ok", "tmos_receipt", "marker", "conn", "socket_closed", "receipt"}
)


class Rejected(ValueError):
    """A record this source refuses, with a named code and a human detail."""

    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def digest(value):
    """A stable SHA-256 over JSON, so equal content always has an equal hash."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def log_key(name):
    """A private, stable name for one log, so a re-import replaces that log's rows.

    The key is a digest, never the path: two logs that share a basename stay
    apart, and no directory or account name is written into the ledger.
    """
    return digest(["pilotdeck-source-v1", str(name)])


def record_hash(record):
    """The hash of a record's content, excluding the derived columns."""
    return digest({k: v for k, v in record.items() if k not in RECORD_HASH_EXCLUDED})


def milliseconds(value):
    """`value` as epoch milliseconds, or None when it is not a usable instant.

    A timestamp without a UTC offset is refused rather than read as local time:
    a cost ledger that shifts by the reader's timezone is worse than one gap.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            return None
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        when = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    try:
        return int(when.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def is_true(value):
    """True only for the JSON boolean true, never for a truthy string or number."""
    return isinstance(value, bool) and value


def unterminated(text):
    """True when a JSON object or array opened in `text` never closed.

    This is how a log cut off mid-write is told apart from a line that is merely
    corrupt: the writer was interrupted, so the closing brace is simply absent.
    """
    depth, in_string, escaped = 0, False, False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
    return depth > 0 or in_string


def tokens_from_usage(usage):
    """The five token columns from a usage block, or None for every column.

    Both the gateway's camelCase and a driver's snake_case are accepted, because
    both spellings reach this source in practice.
    """
    if usage is None:
        return dict.fromkeys(TOKEN_FIELDS)
    if not isinstance(usage, dict):
        raise Rejected(
            INVALID_USAGE, f"usage: expected an object, got {type(usage).__name__}"
        )
    found = {}
    for field in TOKEN_FIELDS:
        value = usage.get(TOKEN_SOURCES[field], usage.get(field))
        if value is None:
            found[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Rejected(
                INVALID_USAGE,
                f"usage.{TOKEN_SOURCES[field]}: expected a nonnegative integer or null",
            )
        found[field] = value
    return found


def label_of(*candidates):
    """The first non-empty label among `candidates`, lowercased and unspaced."""
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace(" ", "_").replace("-", "_")
    return None


def short_text(value):
    """A short string field from a log record, or None."""
    return value.strip()[:64] if isinstance(value, str) and value.strip() else None


def call_type_of(label):
    """The call type a driver's label names, or None when it names no known one."""
    return LABELS.get(label) if label else None


def build_row(
    *,
    session_id,
    call_type,
    call_hash,
    role,
    started_at_ms,
    finished_at_ms=None,
    duration_ms=None,
    tokens=None,
    model=None,
    model_source=None,
    label_source=None,
    provider=None,
    subagent_type=None,
    label=None,
    outcome=OUTCOME_UNKNOWN,
    final_event_missing=False,
):
    """One ledger row, with its content hash over everything but the derived columns.

    `source_sha256` is left for `finish()`: the digest of a log exists only after
    the log has been read, and a row that named its source halfway through would
    name nothing.
    """
    row = {
        "app": APP,
        "session_id": session_id,
        "call_type": call_type,
        "call_hash": call_hash,
        "source_sha256": None,
        "role": role,
        "model": model,
        "model_source": model_source,
        "label_source": label_source,
        "provider": provider,
        "subagent_type": subagent_type,
        "label": label,
        "tokens_observed": tokens is not None
        and any(value is not None for value in tokens.values()),
        "started_at_ms": started_at_ms,
        "finished_at_ms": finished_at_ms,
        "duration_ms": duration_ms,
        "session_offset_ms": 0,
        "outcome": outcome,
        "final_event_missing": final_event_missing,
    }
    for field in TOKEN_FIELDS:
        row[field] = tokens.get(field) if tokens else None
    row["record_hash"] = record_hash(row)
    return row


def with_offsets(records):
    """Records ordered by session and offset from that session's first call.

    The offset is derived from the earliest call the ledger holds for the
    session, so it is monotonic by construction and never part of a record's
    hash: an earlier record arriving later shifts offsets, not identities.
    """
    first = {}
    for record in records:
        current = first.get(record["session_id"])
        if current is None or record["started_at_ms"] < current:
            first[record["session_id"]] = record["started_at_ms"]
    ordered = []
    for record in records:
        copy = dict(record)
        copy["session_offset_ms"] = max(
            0, record["started_at_ms"] - first[record["session_id"]]
        )
        ordered.append(copy)
    return sorted(
        ordered, key=lambda r: (r["session_id"], r["session_offset_ms"], r["call_hash"])
    )


def turn_duration(turn):
    """A turn's span in milliseconds, or None while its end is unknown."""
    if turn["finished_at_ms"] is None:
        return None
    return turn["finished_at_ms"] - turn["started_at_ms"]


class PilotDeckLogParser:
    """Stream one driver log and yield one ledger row per call it can evidence.

    One parser reads one stream: session state (turn windows, gateway request
    times) is per log, because that is the unit an egress log is attributable
    within. Reading is line-by-line; what is held in memory is one small object
    per turn and per egress request, never the file.

    Rows come out of `finish()` rather than out of each line, because an egress
    request can only be classified once every turn window it might belong to has
    been read.
    """

    def __init__(
        self,
        *,
        session_id=None,
        hidden_window_ms=HIDDEN_WINDOW_MS,
        match_window_ms=MATCH_WINDOW_MS,
        max_line_bytes=MAX_LINE_BYTES,
        session_per_log=False,
    ):
        self.session_id = session_id
        self.session_per_log = session_per_log
        self.fallback_session = None
        self.source_key = None
        self.hidden_window_ms = hidden_window_ms
        self.match_window_ms = match_window_ms
        self.max_line_bytes = max_line_bytes
        self.sha256 = None
        self.sources = []
        self.records = []
        self.rejections = []
        self.unattributed = []
        self.lines = 0
        self.blank = 0
        self.control = 0
        self.egress_requests = 0
        self.egress_matched = 0
        self.unlabelled_calls = 0
        self.labelled_user_direct = 0
        self.turns_without_usage = 0
        self._turns = {}
        self._open = {}
        self._requests = {}
        self._subagents = {}
        self._rows = []
        self._egress = []
        self._user_calls = []
        self._ordinals = {}

    # -- reading -----------------------------------------------------------

    def read(self, path):
        """Parse one log file; its digest is over the bytes read."""
        try:
            with open(path, "rb") as stream:
                return self.read_stream(
                    stream,
                    label=Path(path).name,
                    source_key=log_key(Path(path).resolve()),
                )
        except OSError as error:
            reason = error.strerror
            if not reason:
                reason = error
            raise ValueError(f"cannot read {Path(path).name}: {reason}") from error

    def read_stream(self, stream, label="", source_key=None):
        """Parse one binary stream; `finish()` answers once every log is read.

        A driver log and the egress log in front of its endpoint are two streams of
        one session: an egress request can only be told apart from the gateway's own
        call against the turn windows of the log it belongs to. One parser therefore
        reads all the streams of one import, and each stream's rows are stamped with
        that stream's digest as soon as the stream ends.
        """
        self.source_key = source_key or log_key(f"stream:{label}")
        # A captured log that records no session id is still one session: the
        # operator says so once here, rather than per record.
        self.fallback_session = (
            Path(label).stem if (self.session_per_log and label) else None
        )
        rows_before, rejections_before, lines_before = (
            len(self._rows),
            len(self.rejections),
            self.lines,
        )
        requests_before = len(self._egress)
        hasher = hashlib.sha256()
        pending = None
        for raw in self._raw_lines(stream):
            hasher.update(raw)
            if pending is not None:
                self._line(*pending)
            pending = (raw, label)
        if pending is not None:
            self._line(*pending, at_eof=True)
        self.sha256 = hasher.hexdigest()
        for row in self._rows[rows_before:]:
            row["source_sha256"] = self.sha256
        for request in self._egress[requests_before:]:
            request["sha256"] = self.sha256
        fresh = self.rejections[rejections_before:]
        self.sources.append(
            {
                "source_key": self.source_key,
                "sha256": self.sha256,
                "label": label,
                "lines": self.lines - lines_before,
                "rejected": len(fresh),
                "truncated": any(r["code"] == TRUNCATED_JSON_AT_EOF for r in fresh),
                "supersedes": [],
            }
        )
        return self

    def _raw_lines(self, stream):
        """Bytes lines, with an over-long line consumed whole and named as such."""
        while True:
            raw = stream.readline(self.max_line_bytes + 1)
            if not raw:
                return
            if len(raw) > self.max_line_bytes and not raw.endswith(b"\n"):
                while True:
                    rest = stream.readline(self.max_line_bytes)
                    if not rest or rest.endswith(b"\n"):
                        break
                yield raw
                continue
            yield raw

    def _line(self, raw, label, at_eof=False):
        self.lines += 1
        text = raw.decode("utf-8", "replace").strip().lstrip("\ufeff")
        if not text:
            self.blank += 1
            return
        if len(raw) > self.max_line_bytes:
            return self._reject(
                label,
                self.lines,
                LINE_TOO_LONG,
                f"line exceeds {self.max_line_bytes} bytes",
            )
        try:
            record = json.loads(text)
        except ValueError as error:
            if unterminated(text):
                code = TRUNCATED_JSON_AT_EOF if at_eof else INVALID_JSON
                detail = (
                    "the writer stopped mid-record"
                    if at_eof
                    else "unterminated record before end of file"
                )
                return self._reject(label, self.lines, code, detail)
            return self._reject(label, self.lines, INVALID_JSON, str(error))
        if not isinstance(record, dict):
            return self._reject(
                label,
                self.lines,
                NOT_AN_OBJECT,
                f"expected a JSON object, got {type(record).__name__}",
            )
        try:
            self._record(record, label, self.lines)
        except Rejected as rejection:
            self._reject(label, self.lines, rejection.code, rejection.detail)

    def _reject(self, label, line, code, detail):
        self.rejections.append(
            {"source": label, "line": line, "code": code, "detail": detail}
        )

    # -- records -----------------------------------------------------------

    def _record(self, record, label, line):
        kind = record.get("kind")
        event = record.get("event")
        if isinstance(event, dict):
            return self._gateway(record, event, label, line)
        if "method" in record and "path" in record:
            return self._egress_request(record, label, line)
        if record.get("call_type"):
            return self._declared_call(record, label, line)
        if kind in CONTROL_KINDS or "marker" in record or "conn" in record:
            self.control += 1
            return None
        raise Rejected(
            UNSUPPORTED_RECORD,
            "neither a gateway event, an egress request, a call record nor a control line",
        )

    def _session(self, record, event=None, required=True, allow_default=True):
        """The session a record belongs to, from the record or from the log's own."""
        candidates = [
            record.get(key)
            for key in ("sessionKey", "session_id", "session", "sessionId")
        ]
        if isinstance(event, dict):
            candidates += [
                event.get(key) for key in ("sessionKey", "sessionId", "session")
            ]
        found = next(
            (
                value.strip()
                for value in candidates
                if isinstance(value, str) and value.strip()
            ),
            None,
        )
        if (
            found is not None
            and self.session_id is not None
            and found != self.session_id
        ):
            raise Rejected(
                SESSION_ID_CONFLICT,
                f"record names {found!r}, the log was opened as {self.session_id!r}",
            )
        if found is not None:
            resolved = found
        elif allow_default:
            resolved = self.fallback_session or self.session_id
        else:
            resolved = None
        if resolved is None and required:
            raise Rejected(
                MISSING_SESSION_ID,
                "no session id on the record and none given for this log",
            )
        return resolved

    def _instant(self, record, event=None):
        """The record's instant in epoch milliseconds, or a named rejection."""
        value = record.get("at", record.get("timestamp", record.get("startedAt")))
        if value is None and isinstance(event, dict):
            value = event.get("at", event.get("timestamp"))
        found = milliseconds(value)
        if found is None:
            raise Rejected(
                MISSING_TIMESTAMP,
                "no usable ISO-8601 or epoch-millisecond timestamp on the record",
            )
        return found

    def _gateway(self, record, event, label, line):
        session = None
        kind = event.get("type")
        if kind in ("hello_ok", "socket_closed"):
            self.control += 1
            return None
        if kind == "turn_started":
            session = self._session(record, event)
            run_id = event.get("runId")
            key = (session, run_id) if isinstance(run_id, str) and run_id else None
            at_ms = self._instant(record, event)
            turn = {
                "session_id": session,
                "run_id": run_id,
                "started_at_ms": at_ms,
                "finished_at_ms": None,
                "tokens": None,
                "outcome": OUTCOME_UNKNOWN,
                "finished": False,
                "models": [],
                "providers": [],
                "errors": 0,
            }
            self._turns[key if key is not None else (session, f"@{at_ms}")] = turn
            self._open[session] = turn
            return None
        if kind == "model_request_started":
            session = self._session(record, event)
            at_ms = self._instant(record, event)
            self._requests.setdefault(session, []).append(at_ms)
            turn = self._open.get(session)
            model = (
                event.get("model")
                if isinstance(event.get("model"), str) and event.get("model").strip()
                else None
            )
            provider = (
                event.get("provider")
                if isinstance(event.get("provider"), str)
                else None
            )
            if turn is not None:
                if model:
                    turn["models"].append(model)
                if provider:
                    turn["providers"].append(provider)
            return None
        if kind == "model_selection_changed":
            session = self._session(record, event)
            turn = self._open.get(session)
            model = (
                event.get("model")
                if isinstance(event.get("model"), str)
                and event.get("model", "").strip()
                else None
            )
            if turn is not None and model and not turn["models"]:
                turn["models"].append(model)
            return None
        if kind == "turn_completed":
            session = self._session(record, event)
            return self._turn_completed(record, event, session)
        if kind == "error":
            session = self._session(record, event)
            turn = self._open.get(session)
            if turn is not None:
                turn["errors"] += 1
            return None
        if kind == "agent_status":
            name = label_of(event.get("event"))
            if name and name.startswith("subagent"):
                return self._subagent(record, event, self._session(record, event), name)
            return None
        if kind in EVENT_CALLS:
            return self._declared_call(
                {
                    **record,
                    "call_type": EVENT_CALLS[kind],
                    "callId": event.get("callId", event.get("id")),
                    "usage": event.get("usage", record.get("usage")),
                    "model": event.get("model", record.get("model")),
                    "provider": event.get("provider", record.get("provider")),
                },
                label,
                line,
            )
        if event.get("usage") is not None:
            # A call with tokens and a purpose this source does not know: counted,
            # named by its own event type, never guessed into a known category.
            return self._declared_call(
                {
                    **record,
                    "call_type": str(kind),
                    "callId": event.get("callId", event.get("id")),
                    "usage": event.get("usage"),
                    "model": event.get("model"),
                    "provider": event.get("provider"),
                },
                label,
                line,
            )
        return None

    def _turn_completed(self, record, event, session):
        at_ms = self._instant(record, event)
        run_id = event.get("runId")
        turn = self._turns.get((session, run_id)) if isinstance(run_id, str) else None
        if turn is None:
            turn = self._open.get(session)
        if turn is None:
            # The log begins at the end of a turn it never saw start; the usage is
            # still the gateway's own report, so it is kept under its instant.
            turn = {
                "session_id": session,
                "run_id": run_id,
                "started_at_ms": at_ms,
                "finished_at_ms": None,
                "tokens": None,
                "outcome": OUTCOME_UNKNOWN,
                "finished": False,
                "models": [],
                "providers": [],
                "errors": 0,
            }
            self._turns[
                (session, run_id if isinstance(run_id, str) and run_id else f"@{at_ms}")
            ] = turn
        turn["finished_at_ms"] = at_ms
        turn["tokens"] = tokens_from_usage(event.get("usage", record.get("usage")))
        reason = event.get("finishReason")
        turn["outcome"] = (
            (OUTCOME_ERROR if turn["errors"] else OUTCOME_SUCCESS)
            if reason == "completed"
            else (
                OUTCOME_ERROR if isinstance(reason, str) and reason else OUTCOME_UNKNOWN
            )
        )
        turn["final"] = is_true(record.get("final"))
        turn["finished"] = True
        self._emit_turn(turn, final_missing=not turn["final"])

    def _emit_turn(self, turn, *, final_missing):
        key = (
            turn["session_id"],
            turn["run_id"]
            if isinstance(turn["run_id"], str) and turn["run_id"]
            else f"@{turn['started_at_ms']}",
        )
        models, providers = turn["models"], turn["providers"]
        model = max(models, key=models.count) if models else None
        provider = max(providers, key=providers.count) if providers else None
        existing = next(
            (
                row
                for row in self._rows
                if row["call_type"] == USER_DIRECT
                and row["session_id"] == turn["session_id"]
                and row["call_hash"] == digest(["pilotdeck-turn-v1", *key])
            ),
            None,
        )
        if existing is not None:
            if final_missing and not existing["final_event_missing"]:
                return  # already emitted as final for this turn
            existing.update(
                {
                    field: value
                    for field, value in build_row(
                        session_id=turn["session_id"],
                        call_type=USER_DIRECT,
                        call_hash=existing["call_hash"],
                        role=ROLE_MAIN,
                        started_at_ms=turn["started_at_ms"],
                        finished_at_ms=turn["finished_at_ms"],
                        duration_ms=turn_duration(turn),
                        tokens=turn["tokens"],
                        model=model,
                        model_source="gateway",
                        provider=provider,
                        outcome=turn["outcome"],
                        final_event_missing=final_missing,
                    ).items()
                    if field != "session_offset_ms"
                }
            )
            return
        self._rows.append(
            build_row(
                session_id=turn["session_id"],
                call_type=USER_DIRECT,
                call_hash=digest(["pilotdeck-turn-v1", *key]),
                role=ROLE_MAIN,
                started_at_ms=turn["started_at_ms"],
                finished_at_ms=turn["finished_at_ms"],
                duration_ms=turn_duration(turn),
                tokens=turn["tokens"],
                model=model,
                model_source="gateway",
                provider=provider,
                outcome=turn["outcome"],
                final_event_missing=final_missing,
            )
        )

    def _subagent(self, record, event, session, name):
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
        subagent_id = detail.get("subagentId")
        if not isinstance(subagent_id, str) or not subagent_id.strip():
            raise Rejected(UNSUPPORTED_RECORD, f"{name}: no subagentId on the event")
        at_ms = self._instant(record, event)
        key = (session, subagent_id)
        call_hash = digest(["pilotdeck-subagent-v1", session, subagent_id])
        finished = name in ("subagent_completed", "subagent_failed")
        tokens = tokens_from_usage(detail.get("usage", event.get("usage")))
        success = detail.get("success")
        outcome = (
            OUTCOME_UNKNOWN
            if not finished
            else (OUTCOME_SUCCESS if is_true(success) else OUTCOME_ERROR)
        )
        duration = (
            detail.get("durationMs")
            if isinstance(detail.get("durationMs"), int)
            else None
        )
        subagent_type = detail.get("subagentType")
        row = self._subagents.get(key)
        if row is None:
            row = build_row(
                session_id=session,
                call_type=SUBAGENT_CALL,
                call_hash=call_hash,
                role=ROLE_SUBAGENT,
                started_at_ms=at_ms,
                finished_at_ms=at_ms if finished else None,
                duration_ms=duration,
                tokens=tokens if any(tokens.values()) else None,
                subagent_type=subagent_type if isinstance(subagent_type, str) else None,
                label=name,
                outcome=outcome,
                final_event_missing=not finished,
            )
            self._subagents[key] = row
            self._rows.append(row)
            return
        if finished:
            row.update(
                {
                    field: value
                    for field, value in build_row(
                        session_id=session,
                        call_type=SUBAGENT_CALL,
                        call_hash=call_hash,
                        role=ROLE_SUBAGENT,
                        started_at_ms=row["started_at_ms"],
                        finished_at_ms=at_ms,
                        duration_ms=duration,
                        tokens=tokens if any(tokens.values()) else None,
                        subagent_type=row["subagent_type"],
                        label=name,
                        outcome=outcome,
                        final_event_missing=False,
                    ).items()
                    if field != "session_offset_ms"
                }
            )
        return

    def _declared_call(self, record, label, line):
        """A call the driver states outright, with its own label, model and usage."""
        session = self._session(record)
        at_ms = self._instant(record)
        raw_label = label_of(
            record.get("call_type"), record.get("label"), record.get("purpose")
        )
        tokens = tokens_from_usage(record.get("usage"))
        known = call_type_of(raw_label)
        call_type = known or HIDDEN_CALL
        identity = record.get("callId", record.get("id"))
        if isinstance(identity, str) and identity.strip():
            call_hash = digest(
                ["pilotdeck-call-v1", session, call_type, identity.strip()]
            )
        else:
            ordinal = self._ordinal(("call", session, at_ms, call_type))
            call_hash = digest(
                ["pilotdeck-call-v1", session, call_type, at_ms, ordinal]
            )
        if known is None:
            self.unlabelled_calls += 1
        row = build_row(
            session_id=session,
            call_type=call_type,
            call_hash=call_hash,
            role=ROLE_MAIN if call_type == USER_DIRECT else ROLE_BACKGROUND,
            started_at_ms=at_ms,
            finished_at_ms=milliseconds(record.get("finishedAt"))
            if record.get("finishedAt") is not None
            else None,
            tokens=tokens if any(tokens.values()) else None,
            model=record.get("model") if isinstance(record.get("model"), str) else None,
            model_source="driver" if isinstance(record.get("model"), str) else None,
            provider=record.get("provider")
            if isinstance(record.get("provider"), str)
            else None,
            label=raw_label,
            label_source=short_text(record.get("label_source")) or "driver",
            outcome=OUTCOME_SUCCESS
            if record.get("outcome") == "success"
            else OUTCOME_UNKNOWN,
        )
        if call_type == USER_DIRECT:
            # A user's own call stated twice -- as a declared call and as the turn
            # the gateway reports -- is one call. Which it is depends on the rest of
            # the log, so it is settled once the whole stream has been read.
            self._user_calls.append(row)
        else:
            self._rows.append(row)

    def _ordinal(self, key):
        """The count of earlier records with the same key, so identical lines stay distinct."""
        self._ordinals[key] = self._ordinals.get(key, -1) + 1
        return self._ordinals[key]

    def _egress_request(self, record, label, line):
        method = record.get("method")
        path = record.get("path")
        if not isinstance(method, str) or not isinstance(path, str):
            raise Rejected(
                UNSUPPORTED_RECORD, "egress request needs a method and a path"
            )
        at_ms = self._instant(record)
        # `--session-id` names the driver log; a shared egress stream is placed
        # by a session id on the request or by the turn window it falls in.
        session = self._session(record, required=False, allow_default=False)
        port = record.get("port") if isinstance(record.get("port"), int) else None
        raw_label = label_of(
            record.get("call_type"), record.get("label"), record.get("purpose")
        )
        ordinal = self._ordinal(("egress", session, at_ms, port, method.upper(), path))
        self.egress_requests += 1
        self._egress.append(
            {
                "session_id": session,
                "started_at_ms": at_ms,
                "port": port,
                "method": method.upper(),
                "path": path,
                "label": raw_label,
                "label_source": short_text(record.get("label_source")),
                "model": record.get("model")
                if isinstance(record.get("model"), str)
                else None,
                "provider": record.get("provider")
                if isinstance(record.get("provider"), str)
                else None,
                "tokens": tokens_from_usage(record.get("usage")),
                "call_hash": digest(
                    [
                        "pilotdeck-egress-v1",
                        session,
                        at_ms,
                        port,
                        method.upper(),
                        path,
                        ordinal,
                    ]
                ),
                "source": label,
                "sha256": None,
                "line": line,
            }
        )

    # -- attribution -------------------------------------------------------

    def _spans(self):
        """Each session's turn span, widened by the hidden-call window."""
        spans = {}
        for turn in self._turns.values():
            start = turn["started_at_ms"]
            end = (
                turn["finished_at_ms"] if turn["finished_at_ms"] is not None else start
            )
            current = spans.get(turn["session_id"])
            spans[turn["session_id"]] = (
                min(start, current[0]) if current else start,
                max(end, current[1]) if current else end,
            )
        return {
            session: (start - self.hidden_window_ms, end + self.hidden_window_ms)
            for session, (start, end) in spans.items()
        }

    def _window_session(self, at_ms, spans):
        """The session whose turns an unattributed egress request sits closest to."""
        best = None
        for session, (start, end) in spans.items():
            if not (start <= at_ms <= end):
                continue
            stamps = self._requests.get(session, [])
            distance = min(
                (abs(at_ms - stamp) for stamp in stamps), default=abs(at_ms - start)
            )
            candidate = (distance, session)
            if best is None or candidate < best:
                best = candidate
        return best[1] if best else None

    def _claimed_by_gateway(self, session, requests):
        """The requests this session's gateway already accounts for, one match each.

        Each `model_request_started` claims the egress request nearest to it, so a
        hidden call that happens to land a few hundred milliseconds earlier cannot
        take the slot of the reported call that is milliseconds away. Both sides are
        needed: the gateway's own request count is what the turn row prices.
        """
        stamps = self._requests.get(session)
        if not stamps or not requests:
            return set()
        times = [request["started_at_ms"] for request in requests]
        claimed = set()
        for stamp in sorted(stamps):
            index = bisect.bisect_left(times, stamp)
            best = None
            for candidate in (index - 1, index, index + 1, index - 2, index + 2):
                if not 0 <= candidate < len(times) or candidate in claimed:
                    continue
                distance = abs(times[candidate] - stamp)
                if distance <= self.match_window_ms and (
                    best is None or distance < best[0]
                ):
                    best = (distance, candidate)
            if best is not None:
                claimed.add(best[1])
        return {requests[index]["call_hash"] for index in claimed}

    def _attribute_egress(self):
        """One row per upstream request the gateway did not report, or why it could not be placed."""
        spans = self._spans()
        placed = {}
        rows = []
        for request in sorted(
            self._egress, key=lambda r: (r["started_at_ms"], r["call_hash"])
        ):
            if request["method"] != "POST":
                self.control += 1  # a metadata read is not a model call
                continue
            session = request["session_id"] or self._window_session(
                request["started_at_ms"], spans
            )
            if session is None:
                # Not a rejected line: a request of a shared egress stream that no
                # turn window here accounts for. Named, counted, and left out of the
                # token rows rather than guessed into a session.
                self.unattributed.append(
                    {
                        "source": request["source"],
                        "line": request["line"],
                        "code": UNATTRIBUTED_EGRESS,
                        "detail": "no session on the request and no turn window claims it; "
                        "import it with the driver log of the session it belongs to, or put a "
                        "sessionKey on the request line",
                    }
                )
                continue
            placed.setdefault(session, []).append(request)
        for session, requests in placed.items():
            claimed = self._claimed_by_gateway(session, requests)
            for request in requests:
                if request["call_hash"] in claimed:
                    self.egress_matched += 1
                    continue
                call_type = call_type_of(request["label"])
                if call_type == USER_DIRECT:
                    # The driver says this is the user's own call: the turn row already
                    # carries its tokens, so a second row would count them twice.
                    self.labelled_user_direct += 1
                    continue
                if call_type is None:
                    self.unlabelled_calls += 1
                    call_type = HIDDEN_CALL
                tokens = request["tokens"] if any(request["tokens"].values()) else None
                row = build_row(
                    session_id=session,
                    call_type=call_type,
                    call_hash=request["call_hash"],
                    role=ROLE_BACKGROUND,
                    started_at_ms=request["started_at_ms"],
                    tokens=tokens,
                    model=request["model"],
                    model_source="driver" if request["model"] else None,
                    provider=request["provider"],
                    label=request["label"],
                    label_source=request["label_source"],
                )
                row["source_sha256"] = request["sha256"]
                rows.append(row)
        return rows

    def finish(self):
        """Complete the rows once the whole stream has been read."""
        for turn in self._turns.values():
            if not turn["finished"]:
                self.turns_without_usage += 1
        turns_by_session = {
            turn["session_id"] for turn in self._turns.values() if turn["finished"]
        }
        for row in self._user_calls:
            if row["session_id"] in turns_by_session:
                self.labelled_user_direct += (
                    1  # the turn row already carries these tokens
                )
            else:
                self._rows.append(row)
        rows = [*self._rows, *self._attribute_egress()]
        self.records = with_offsets(rows)
        return self.records


def merge(ledger, sources, records):
    """Merge one import's rows into the ledger, keyed by session and call hash.

    Two rules, both of which a cost ledger needs:

    - **A row is identified by `(session_id, call_hash)`**, so this is
      `INSERT ... ON CONFLICT (session_id, call_hash) DO NOTHING`. A provisional
      row, whose terminal event had not been written when it was imported, is
      replaced by that terminal event. Any other disagreement on one key is a
      real conflict and is refused, never silently overwritten.
    - **An earlier version of a log is superseded, not accumulated.** The driver
      appends to its log, and an appended event can change an earlier judgement:
      an upstream request that looked hidden becomes a call the gateway reported.
      A log also decides what its sibling streams mean, so the import unit is the
      set of logs read together: when any one of them changed, rows produced by
      the previous version of any of them are dropped before the new rows go in.
      The ledger then states what the current logs say, not the union of
      everything they ever said.
    """
    if ledger.get("schema_version") != SCHEMA_VERSION or ledger.get("app") != APP:
        raise ValueError("unsupported pilotdeck ledger")
    if not sources:
        raise ValueError("an import must name its sources")
    superseded = set()
    for source in sources:
        if set(source) != SOURCE_FIELDS:
            raise ValueError("unexpected or missing source fields")
        superseded.update(source["supersedes"])
    by_key = {}
    for record in [
        *[r for r in ledger["records"] if r["source_sha256"] not in superseded],
        *records,
    ]:
        if set(record) != RECORD_FIELDS:
            raise ValueError("unexpected or missing ledger fields")
        if record.get("record_hash") != record_hash(record):
            raise ValueError("ledger record hash mismatch")
        if record["app"] != APP or record["call_type"] not in CALL_TYPES:
            raise ValueError("ledger record is not a pilotdeck call")
        key = (record["session_id"], record["call_hash"])
        previous = by_key.get(key)
        if previous is None:
            by_key[key] = record
        elif previous["record_hash"] == record["record_hash"]:
            continue
        elif previous["final_event_missing"] and not record["final_event_missing"]:
            by_key[key] = record
        else:
            raise ValueError(
                f"conflicting observations of {record['session_id']}/{record['call_hash']}; "
                "reconciliation required"
            )
    entries = {entry["source_key"]: entry for entry in ledger["sources"]}
    for source in sources:
        entries[source["source_key"]] = source
    return {
        "schema_version": SCHEMA_VERSION,
        "app": APP,
        "sources": [entries[key] for key in sorted(entries)],
        "records": with_offsets(list(by_key.values())),
    }


def empty_ledger():
    return {"schema_version": SCHEMA_VERSION, "app": APP, "sources": [], "records": []}


def tally(
    records,
    rejections,
    parser_counts,
    added=0,
    known=0,
    replaced=0,
    unchanged_sources=0,
    retracted=0,
    unattributed=(),
):
    """The telemetry receipt: what was read, what was hidden, and what it cost."""
    by_type = {}
    for call_type in CALL_TYPES:
        group = [record for record in records if record["call_type"] == call_type]
        by_type[call_type] = {
            "rows": len(group),
            "input_tokens": sum(
                r["input_tokens"] for r in group if r["input_tokens"] is not None
            )
            or None,
            "output_tokens": sum(
                r["output_tokens"] for r in group if r["output_tokens"] is not None
            )
            or None,
            "tokens_observed_rows": sum(1 for r in group if r["tokens_observed"]),
            "sessions": len({r["session_id"] for r in group}),
        }
    codes = {}
    for rejection in rejections:
        codes[rejection["code"]] = codes.get(rejection["code"], 0) + 1
    hidden = [r for r in records if r["call_type"] in HIDDEN_CALL_TYPES]
    observed = [r for r in records if r["tokens_observed"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "app": APP,
        "receipt": {
            "lines_processed": parser_counts["lines"],
            "lines_blank": parser_counts["blank"],
            "lines_control": parser_counts["control"],
            "lines_rejected": len(rejections),
            "rejections_by_code": codes,
            "sources_read": parser_counts["sources_read"],
            "sources_unchanged": unchanged_sources,
            "sources_truncated": parser_counts["sources_truncated"],
            "records_in_ledger": len(records),
            "records_new": added,
            "records_known": known,
            "records_replaced": replaced,
            "records_retracted": retracted,
            "user_turns": by_type[USER_DIRECT]["rows"],
            "hidden_calls_detected": len(hidden),
            "hidden_calls": {
                call_type: by_type[call_type]["rows"] for call_type in HIDDEN_CALL_TYPES
            },
            "subagent_calls": by_type[SUBAGENT_CALL]["rows"],
            "egress_requests": parser_counts["egress_requests"],
            "egress_matched_to_gateway": parser_counts["egress_matched"],
            "egress_unattributed": len(unattributed),
            "labelled_user_direct": parser_counts["labelled_user_direct"],
            "turns_without_usage": parser_counts["turns_without_usage"],
            "input_tokens": sum(
                r["input_tokens"] for r in observed if r["input_tokens"] is not None
            )
            or None,
            "output_tokens": sum(
                r["output_tokens"] for r in observed if r["output_tokens"] is not None
            )
            or None,
            "tokens_by_call_type": {
                call_type: {
                    "input_tokens": entry["input_tokens"],
                    "output_tokens": entry["output_tokens"],
                    "tokens_observed_rows": entry["tokens_observed_rows"],
                }
                for call_type, entry in by_type.items()
            },
            "rows_without_tokens": len(records) - len(observed),
            "rows_without_model": sum(1 for r in records if r["model"] is None),
            "hidden_calls_unlabelled": parser_counts["unlabelled_calls"],
        },
        "rejections": rejections[:MAX_REPORTED_REJECTIONS],
        "unattributed": list(unattributed)[:MAX_REPORTED_REJECTIONS],
        "coverage": {
            "complete": False,
            "scope": "driver event stream and egress request log only",
            "hidden_call_tokens_observable": False,
            "contains_truncated_source": bool(parser_counts["sources_truncated"]),
        },
        "note": "A hidden call is counted but not priced: the egress log carries no tokens, so "
        "token totals are a lower bound unless the driver labelled the call with usage. "
        "A model is never inferred for a call the log does not name.",
    }


def summarize(records, sources):
    """A dashboard-shaped view of the whole ledger, never of one import."""
    sessions = sorted({record["session_id"] for record in records})
    observed = [r for r in records if r["tokens_observed"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "app": APP,
        "coverage": {
            "complete": False,
            "sessions": len(sessions),
            "sources": len(sources),
            "contains_truncated_source": any(entry["truncated"] for entry in sources),
            "first_started_at_ms": min(
                (r["started_at_ms"] for r in records), default=None
            ),
            "last_started_at_ms": max(
                (r["started_at_ms"] for r in records), default=None
            ),
        },
        "totals": {
            "records": len(records),
            "calls_by_type": {
                call_type: sum(1 for r in records if r["call_type"] == call_type)
                for call_type in CALL_TYPES
            },
            "hidden_calls": sum(
                1 for r in records if r["call_type"] in HIDDEN_CALL_TYPES
            ),
            "input_tokens": {
                "sum": sum(
                    r["input_tokens"] for r in observed if r["input_tokens"] is not None
                )
                or None,
                "known_rows": sum(1 for r in observed if r["input_tokens"] is not None),
            },
            "output_tokens": {
                "sum": sum(
                    r["output_tokens"]
                    for r in observed
                    if r["output_tokens"] is not None
                )
                or None,
                "known_rows": sum(
                    1 for r in observed if r["output_tokens"] is not None
                ),
            },
            "rows_without_tokens": len(records) - len(observed),
        },
        "note": "Hidden-call tokens are not observable from this source; the calls are complete, "
        "their price is not. Coverage is never complete for a period.",
    }


def import_file(
    log_path,
    ledger_path,
    *,
    session_id=None,
    session_per_log=False,
    hidden_window_ms=HIDDEN_WINDOW_MS,
    match_window_ms=MATCH_WINDOW_MS,
    strict=False,
):
    """Import one log into the ledger, idempotently, and return the receipt."""
    return import_paths(
        [log_path],
        ledger_path,
        session_id=session_id,
        session_per_log=session_per_log,
        hidden_window_ms=hidden_window_ms,
        match_window_ms=match_window_ms,
        strict=strict,
    )


def import_paths(
    log_paths,
    ledger_path,
    *,
    session_id=None,
    session_per_log=False,
    hidden_window_ms=HIDDEN_WINDOW_MS,
    match_window_ms=MATCH_WINDOW_MS,
    strict=False,
):
    """Import logs into one ledger under an exclusive lock and an atomic replace.

    The logs of one import are read together, because a driver log is what makes
    the egress log beside it mean anything: an upstream request is only hidden
    relative to the turns the driver recorded, and a session is only nameable
    relative to those turns too. Splitting them across imports would count every
    request in a shared egress stream once per session.
    """
    ledger_path = Path(ledger_path)
    for path in log_paths:
        if Path(path).resolve() == ledger_path.resolve():
            raise ValueError("the ledger must not overwrite a source log")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(str(ledger_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "w"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ledger = (
                json.loads(ledger_path.read_text())
                if ledger_path.exists()
                else empty_ledger()
            )
        except ValueError as error:
            raise ValueError(
                f"the pilotdeck ledger is not valid JSON: {error}"
            ) from error
        parser = PilotDeckLogParser(
            session_id=session_id,
            session_per_log=session_per_log,
            hidden_window_ms=hidden_window_ms,
            match_window_ms=match_window_ms,
        )
        for path in log_paths:
            parser.read(path)
        rows = parser.finish()
        rejections = parser.rejections
        if strict and rejections:
            codes = sorted({rejection["code"] for rejection in rejections})
            raise Rejected(
                rejections[0]["code"],
                f"refused to import a damaged log: {', '.join(codes)}",
            )
        counts = {
            key: getattr(parser, key)
            for key in (
                "lines",
                "blank",
                "control",
                "egress_requests",
                "egress_matched",
                "unlabelled_calls",
                "labelled_user_direct",
                "turns_without_usage",
            )
        }
        counts["sources_read"] = len(parser.sources)
        counts["sources_truncated"] = sum(
            1 for entry in parser.sources if entry["truncated"]
        )
        current = {entry["source_key"]: entry for entry in ledger["sources"]}
        stale = [
            entry
            for entry in parser.sources
            if current.get(entry["source_key"], {}).get("sha256") != entry["sha256"]
        ]
        unchanged = len(parser.sources) - len(stale)
        added = known = replaced = retracted = 0
        if stale:
            # One log of the set changed, so the set is recomputed: the previous
            # version of every one of these logs is superseded, and the rows it
            # produced are dropped before the new rows go in.
            before = {
                (record["session_id"], record["call_hash"]): record["record_hash"]
                for record in ledger["records"]
            }
            superseded = set()
            for entry in parser.sources:
                previous = current.get(entry["source_key"])
                if previous is not None:
                    entry["supersedes"] = [*previous["supersedes"], previous["sha256"]]
                    superseded.add(previous["sha256"])
            superseded_keys = {
                (record["session_id"], record["call_hash"])
                for record in ledger["records"]
                if record["source_sha256"] in superseded
            }
            ledger = merge(ledger, parser.sources, rows)
            after = {
                (record["session_id"], record["call_hash"]): record["record_hash"]
                for record in ledger["records"]
            }
            added = sum(1 for key in after if key not in before)
            replaced = sum(
                1 for key in after if key in before and after[key] != before[key]
            )
            known = sum(
                1 for key in after if key in before and after[key] == before[key]
            )
            retracted = len(superseded_keys - set(after))
            write_ledger(ledger_path, ledger)
    report = tally(
        ledger["records"],
        rejections,
        counts,
        added=added,
        known=known,
        replaced=replaced,
        unchanged_sources=unchanged,
        retracted=retracted,
        unattributed=parser.unattributed,
    )
    report["ledger"] = summarize(ledger["records"], ledger["sources"])
    return report


def write_ledger(path, ledger):
    """Replace the ledger atomically, so a reader never sees a half-written file."""
    fd, temp = tempfile.mkstemp(prefix=".pilotdeck-ledger-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(ledger, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--log",
        type=Path,
        action="append",
        required=True,
        help="driver event log (repeatable); may be the gateway stream, the egress request log or both",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        type=Path,
        help="private JSON ledger to update in place",
    )
    parser.add_argument(
        "--summary", type=Path, help="atomically write the private dashboard summary"
    )
    parser.add_argument(
        "--session-per-log",
        action="store_true",
        help="treat each log as one session, named after the file, for a capture that records no session id",
    )
    parser.add_argument(
        "--session-id",
        help="session id for a log that does not carry one on every record",
    )
    parser.add_argument(
        "--hidden-window-seconds",
        type=float,
        default=HIDDEN_WINDOW_MS / 1000,
        help="how long after a session's turns its background calls may still be attributed to it",
    )
    parser.add_argument(
        "--match-window-ms",
        type=int,
        default=MATCH_WINDOW_MS,
        help="how close an egress request must be to a gateway request to be the same call",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="refuse the whole import when any line is rejected",
    )
    args = parser.parse_args(argv)
    try:
        if args.summary and args.summary.resolve() in (
            args.ledger.resolve(),
            *[p.resolve() for p in args.log],
        ):
            raise ValueError("summary must not overwrite a source log or the ledger")
        report = import_paths(
            args.log,
            args.ledger,
            session_id=args.session_id,
            session_per_log=args.session_per_log,
            hidden_window_ms=int(args.hidden_window_seconds * 1000),
            match_window_ms=args.match_window_ms,
            strict=args.strict,
        )
        if args.summary:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            write_ledger(args.summary, report["ledger"])
    except (ValueError, OSError) as error:
        parser.exit(2, f"Import failed: {error}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
