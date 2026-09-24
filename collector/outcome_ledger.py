"""Private task outcome ledger; request success is never a validation signal.

Events are JSONL objects with an event_id and one of:

* task_registered: task_id, provider, cohort, started_at
* turns_recorded: task_id, turns, errors, occurred_at (nonnegative increments)
* task_finalized: task_id, status (validated|failed|abandoned), occurred_at
  A validated finalization additionally requires evidence_path and evidence_sha256;
  matching evidence bytes are retained privately by digest before the event is accepted.
* task_reopened: task_id, occurred_at (terminal tasks only)

There is deliberately no request-success or assistant-completed event type. IDs are stored only
as SHA-256 keys, evidence paths are never stored, and summaries contain aggregate counts only.
Validation evidence bytes remain private in a content-addressed side directory; their hashes prove
integrity, not that the checks were semantically adequate. Coverage is always observed/incomplete.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 3
STATUSES = {"pending", "validated", "failed", "abandoned"}
PROVIDERS = {"claude-code", "codex", "clinepass", "command-code", "opencode-go"}
ATOM = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class LedgerError(ValueError):
    """Invalid event, ledger or evidence reference; the ledger was not changed."""


def _atom(value, name):
    if not isinstance(value, str) or not ATOM.fullmatch(value):
        raise LedgerError(f"invalid {name}")
    return value


def _timestamp(value, name):
    if not isinstance(value, str):
        raise LedgerError(f"{name} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"invalid {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_id(value, name):
    return hashlib.sha256(_atom(value, name).encode("utf-8")).hexdigest()


def _nonnegative_int(value, name):
    if type(value) is not int or value < 0:
        raise LedgerError(f"{name} must be a nonnegative integer")
    return value


def _event_normalized(event):
    if not isinstance(event, dict):
        raise LedgerError("each event must be a JSON object")
    kind = event.get("type")
    fields = {
        "task_registered": {"type", "event_id", "task_id", "provider", "cohort", "started_at"},
        "turns_recorded": {"type", "event_id", "task_id", "turns", "errors", "occurred_at"},
        "task_finalized": {"type", "event_id", "task_id", "status", "occurred_at", "evidence_path", "evidence_sha256"},
        "task_reopened": {"type", "event_id", "task_id", "occurred_at"},
    }
    if not isinstance(kind, str) or kind not in fields:
        raise LedgerError("unsupported event type")
    if set(event) - fields[kind]:
        raise LedgerError("event contains unsupported fields")
    required = fields[kind]
    if kind == "task_finalized" and event.get("status") != "validated":
        required = required - {"evidence_path", "evidence_sha256"}
    missing = required - set(event)
    if missing:
        raise LedgerError("event is missing required fields")
    out = {"type": kind, "event_id": _atom(event["event_id"], "event_id"),
           "task_id": _atom(event["task_id"], "task_id")}
    if kind == "task_registered":
        provider = _atom(event["provider"], "provider")
        if provider not in PROVIDERS:
            raise LedgerError("unsupported provider")
        out.update(provider=provider, cohort=_atom(event["cohort"], "cohort"),
                   started_at=_timestamp(event["started_at"], "started_at"))
    elif kind == "turns_recorded":
        out.update(turns=_nonnegative_int(event["turns"], "turns"),
                   errors=_nonnegative_int(event["errors"], "errors"),
                   occurred_at=_timestamp(event["occurred_at"], "occurred_at"))
    elif kind == "task_finalized":
        status = event.get("status")
        if status not in ("validated", "failed", "abandoned"):
            raise LedgerError("invalid final status")
        out.update(status=status, occurred_at=_timestamp(event["occurred_at"], "occurred_at"))
        if status == "validated":
            path = event.get("evidence_path")
            digest = event.get("evidence_sha256")
            if not isinstance(path, str) or not path or not isinstance(digest, str) or not HEX64.fullmatch(digest):
                raise LedgerError("validated status requires an evidence path and SHA-256")
            out.update(evidence_path=path, evidence_sha256=digest)
    else:
        out["occurred_at"] = _timestamp(event["occurred_at"], "occurred_at")
    return out


def _empty_ledger():
    return {"schema_version": SCHEMA_VERSION, "tasks": {}, "events": {}}


def _load(path):
    if not path.exists():
        return _empty_ledger()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise LedgerError("ledger file must be a private regular file (mode 0600)")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LedgerError("ledger file is unreadable or invalid JSON") from exc
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or not isinstance(value.get("tasks"), dict) or not isinstance(value.get("events"), dict)):
        raise LedgerError("unsupported or malformed ledger")
    for key, task in value["tasks"].items():
        if not isinstance(key, str) or not HEX64.fullmatch(key) or not isinstance(task, dict):
            raise LedgerError("malformed task record")
        if (not isinstance(task.get("provider"), str) or task["provider"] not in PROVIDERS
                or not isinstance(task.get("cohort"), str)
                or not isinstance(task.get("status"), str) or task["status"] not in STATUSES
                or type(task.get("turns")) is not int
                or task["turns"] < 0 or type(task.get("errors")) is not int or task["errors"] < 0
                or type(task.get("reopened_count")) is not int or task["reopened_count"] < 0
                or not isinstance(task.get("validation_evidence"), list)
                or not all(isinstance(d, str) and HEX64.fullmatch(d) for d in task["validation_evidence"])):
            raise LedgerError("malformed task record")
        started = _timestamp(task.get("started_at"), "started_at")
        last_event = _timestamp(task.get("last_event_at"), "last_event_at")
        if datetime.fromisoformat(last_event.replace("Z", "+00:00")) < datetime.fromisoformat(started.replace("Z", "+00:00")):
            raise LedgerError("malformed task chronology")
        if "finalized_at" in task:
            _timestamp(task["finalized_at"], "finalized_at")
    if any(not isinstance(k, str) or not HEX64.fullmatch(k) or not isinstance(v, str)
           or not HEX64.fullmatch(v) for k, v in value["events"].items()):
        raise LedgerError("malformed event index")
    evidence_owner = {}
    for task_key, task in value["tasks"].items():
        if task["status"] == "validated" and not task["validation_evidence"]:
            raise LedgerError("validated task has no validation evidence")
        for digest in task["validation_evidence"]:
            if digest in evidence_owner and evidence_owner[digest] != task_key:
                raise LedgerError("validation evidence is reused across tasks")
            evidence_owner[digest] = task_key
            _verify_retained_evidence(path, task_key, digest)
    return value


def _atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".outcomes-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked(path, exclusive):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield path
    finally:
        # fdopen owns/closes fd after entering; close directly if setup failed before then.
        try:
            os.close(fd)
        except OSError:
            pass


def _evidence_directory(ledger_path):
    path = Path(ledger_path)
    return path.with_name(path.name + ".evidence")


def _verify_retained_evidence(ledger_path, task_key, digest):
    directory = _evidence_directory(ledger_path)
    if directory.is_symlink():
        raise LedgerError("evidence directory must not be a symlink")
    path = directory / (digest + ".bin")
    try:
        if path.is_symlink() or not path.is_file():
            raise LedgerError("retained validation evidence is missing or not a regular file")
        info = path.stat()
        if info.st_mode & 0o077:
            raise LedgerError("retained validation evidence must be private (mode 0600)")
        data = path.read_bytes()
    except OSError as exc:
        raise LedgerError("retained validation evidence cannot be read") from exc
    if not data or hashlib.sha256(data).hexdigest() != digest:
        raise LedgerError("retained validation evidence changed")
    try:
        receipt = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise LedgerError("retained validation evidence is invalid JSON") from exc
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or not isinstance(receipt.get("task_id"), str)
            or hashlib.sha256(receipt["task_id"].encode()).hexdigest() != task_key
            or not isinstance(receipt.get("model_run_id"), str) or not receipt["model_run_id"]
            or not isinstance(receipt.get("reviewer"), str) or not receipt["reviewer"].strip()
            or receipt.get("blocking_checks_passed") is not True
            or receipt.get("semantic_acceptance") is not True
            or any(not isinstance(receipt.get(k), str) or not HEX64.fullmatch(receipt[k])
                   for k in ("verification_sha256", "attestation_sha256"))):
        raise LedgerError("retained validation attestation is malformed or mismatched")
    _timestamp(receipt.get("accepted_at"), "retained attestation accepted_at")


def _validate_attestation(event, data):
    if not data:
        raise LedgerError("validation evidence cannot be empty")
    try:
        receipt = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise LedgerError("validation evidence must be a JSON attestation") from exc
    if not isinstance(receipt, dict):
        raise LedgerError("validation evidence must be an attestation object")
    if (receipt.get("schema_version") != 1 or receipt.get("task_id") != event["task_id"]
            or not isinstance(receipt.get("model_run_id"), str) or not receipt["model_run_id"]
            or not isinstance(receipt.get("reviewer"), str) or not receipt["reviewer"].strip()
            or receipt.get("blocking_checks_passed") is not True
            or receipt.get("semantic_acceptance") is not True):
        raise LedgerError("validation evidence is not a matching semantic acceptance attestation")
    if any(not isinstance(receipt.get(k), str) or not HEX64.fullmatch(receipt[k])
           for k in ("verification_sha256", "attestation_sha256")):
        raise LedgerError("validation attestation is missing verification hashes")
    accepted_at = _timestamp(receipt.get("accepted_at"), "attestation accepted_at")
    final_at = _timestamp(event.get("occurred_at"), "occurred_at")
    if datetime.fromisoformat(accepted_at.replace("Z", "+00:00")) > datetime.fromisoformat(final_at.replace("Z", "+00:00")):
        raise LedgerError("validation attestation postdates task finalization")


def _evidence_bytes(ledger_path, event):
    if event["type"] != "task_finalized" or event.get("status") != "validated":
        return None
    directory = _evidence_directory(ledger_path)
    retained = directory / (event["evidence_sha256"] + ".bin")
    try:
        if retained.is_file() and not retained.is_symlink():
            if retained.stat().st_mode & 0o077:
                raise LedgerError("retained validation evidence must be private (mode 0600)")
            data = retained.read_bytes()
        else:
            source = Path(event["evidence_path"])
            if not source.is_file():
                raise LedgerError("validation evidence must exist or have been retained")
            data = source.read_bytes()
    except OSError as exc:
        raise LedgerError("validation evidence cannot be read") from exc
    if hashlib.sha256(data).hexdigest() != event["evidence_sha256"]:
        raise LedgerError("validation evidence SHA-256 mismatch")
    _validate_attestation(event, data)
    return data


def _store_evidence(ledger_path, event, data):
    if data is None:
        return
    directory = _evidence_directory(ledger_path)
    if directory.is_symlink():
        raise LedgerError("evidence directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    destination = directory / (event["evidence_sha256"] + ".bin")
    if destination.exists():
        if destination.is_symlink():
            raise LedgerError("retained evidence must not be a symlink")
        if _evidence_bytes(ledger_path, event) != data:
            raise LedgerError("retained evidence digest collision or tampering")
        return
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        dirfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _apply(ledger, event):
    event_key = _digest_id(event["event_id"], "event_id")
    event_digest = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prior = ledger["events"].get(event_key)
    if prior is not None:
        if prior != event_digest:
            raise LedgerError("event_id was reused with different content")
        return False
    task_key = _digest_id(event["task_id"], "task_id")
    kind = event["type"]
    task = ledger["tasks"].get(task_key)
    if kind == "task_registered":
        if task is not None:
            raise LedgerError("task_id is already registered")
        task = {"provider": event["provider"], "cohort": event["cohort"],
                "started_at": event["started_at"], "status": "pending", "turns": 0,
                "errors": 0, "reopened_count": 0, "validation_evidence": [],
                "last_event_at": event["started_at"]}
        ledger["tasks"][task_key] = task
    else:
        if task is None:
            raise LedgerError("task_id has not been registered")
        previous_at = datetime.fromisoformat(task["last_event_at"].replace("Z", "+00:00"))
        current_at = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        if current_at < previous_at:
            raise LedgerError("task event timestamps must be monotonic")
        if kind == "turns_recorded":
            if task["status"] != "pending":
                raise LedgerError("reopen a terminal task before recording more turns")
            task["turns"] += event["turns"]
            task["errors"] += event["errors"]
        elif kind == "task_finalized":
            if task["status"] != "pending":
                raise LedgerError("task is already terminal")
            task["status"] = event["status"]
            task["finalized_at"] = event["occurred_at"]
            if event["status"] == "validated":
                if any(other_key != task_key and event["evidence_sha256"] in other["validation_evidence"]
                       for other_key, other in ledger["tasks"].items()):
                    raise LedgerError("validation evidence cannot validate distinct tasks")
                task["validation_evidence"].append(event["evidence_sha256"])
        elif kind == "task_reopened":
            if task["status"] == "pending":
                raise LedgerError("only a terminal task can be reopened")
            task["status"] = "pending"
            task["reopened_count"] += 1
            task.pop("finalized_at", None)
        task["last_event_at"] = event["occurred_at"]
    ledger["events"][event_key] = event_digest
    return True


def ingest(ledger_path, events):
    """Validate/apply JSON event dicts atomically; returns new-event and duplicate counts."""
    normalized = [_event_normalized(event) for event in events]
    with _locked(ledger_path, True) as path:
        ledger = _load(path)
        # Resolve evidence before mutating a candidate. A private retained copy makes identical
        # retries work after the producer removes its original artifact.
        evidence = {event["event_id"]: _evidence_bytes(path, event) for event in normalized
                    if event["type"] == "task_finalized" and event.get("status") == "validated"}
        candidate = json.loads(json.dumps(ledger))
        applied = 0
        for event in normalized:
            applied += bool(_apply(candidate, event))
        if applied:
            for event in normalized:
                if event["event_id"] in evidence:
                    _store_evidence(path, event, evidence[event["event_id"]])
            _atomic_write(path, candidate)
        return {"applied": applied, "duplicates": len(normalized) - applied}


def _bound(value, name):
    return _timestamp(value, name)


def _counts(tasks):
    return {"tasks": len(tasks), "pending": sum(t["status"] == "pending" for t in tasks),
            "validated": sum(t["status"] == "validated" for t in tasks),
            "failed": sum(t["status"] == "failed" for t in tasks),
            "abandoned": sum(t["status"] == "abandoned" for t in tasks),
            "reworked": sum(t["reopened_count"] > 0 for t in tasks),
            "turns": sum(t["turns"] for t in tasks), "errors": sum(t["errors"] for t in tasks)}


def _summary_tasks(tasks, end_utc):
    end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    if any(datetime.fromisoformat(t["last_event_at"].replace("Z", "+00:00")) > end_dt for t in tasks):
        raise LedgerError("summary end predates current task state; historical state is not retained")
    return tasks


def _interval_tasks(ledger, provider, start_utc, end_utc):
    start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    if start_dt >= end_dt:
        raise LedgerError("start must precede end")
    tasks = []
    for task in ledger["tasks"].values():
        if task.get("provider") != provider:
            continue
        started = datetime.fromisoformat(task["started_at"].replace("Z", "+00:00"))
        if start_dt <= started < end_dt:
            tasks.append(task)
    return tasks


def summarize(ledger_path, provider, cohort, start, end):
    """Latest-state counts for tasks started in [start,end); refuses stale snapshots."""
    provider = _atom(provider, "provider")
    if provider not in PROVIDERS:
        raise LedgerError("unsupported provider")
    cohort = _atom(cohort, "cohort")
    start_utc, end_utc = _bound(start, "start"), _bound(end, "end")
    with _locked(ledger_path, False) as path:
        ledger = _load(path)
    tasks = [t for t in _interval_tasks(ledger, provider, start_utc, end_utc)
             if t.get("cohort") == cohort]
    tasks = _summary_tasks(tasks, end_utc)
    return {"provider": provider, "cohort": cohort, "interval": {"start": start_utc, "end": end_utc,
            "semantics": "half-open"}, "coverage": {"kind": "observed ledger events", "complete": False},
            "counts": _counts(tasks)}


def summarize_all(ledger_path, provider, start, end):
    """Provider-wide observed counts with separate cohorts; cannot imply ranked eligibility."""
    provider = _atom(provider, "provider")
    if provider not in PROVIDERS:
        raise LedgerError("unsupported provider")
    start_utc, end_utc = _bound(start, "start"), _bound(end, "end")
    with _locked(ledger_path, False) as path:
        ledger = _load(path)
    tasks = _summary_tasks(_interval_tasks(ledger, provider, start_utc, end_utc), end_utc)
    by_cohort = {}
    for cohort in sorted({t["cohort"] for t in tasks}):
        by_cohort[cohort] = _counts([t for t in tasks if t["cohort"] == cohort])
    return {"provider": provider, "interval": {"start": start_utc, "end": end_utc,
            "semantics": "half-open"}, "coverage": {"kind": "observed ledger events", "complete": False},
            "counts": _counts(tasks), "by_cohort": by_cohort}


def _read_events(path):
    events = []
    try:
        with Path(path).open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except ValueError as exc:
                        raise LedgerError(f"invalid JSON on event line {number}") from exc
    except (OSError, UnicodeError) as exc:
        raise LedgerError("events file cannot be read") from exc
    return events


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("ingest", help="atomically apply an events JSONL file")
    add.add_argument("--ledger", type=Path, required=True)
    add.add_argument("--events", type=Path, required=True)
    report = sub.add_parser("summary", help="print aggregate observed coverage for an interval")
    report.add_argument("--ledger", type=Path, required=True)
    report.add_argument("--provider", required=True)
    report.add_argument("--cohort", required=True)
    report.add_argument("--start", required=True)
    report.add_argument("--end", required=True)
    all_report = sub.add_parser("summary-all", help="print provider totals grouped by cohort")
    all_report.add_argument("--ledger", type=Path, required=True)
    all_report.add_argument("--provider", required=True)
    all_report.add_argument("--start", required=True)
    all_report.add_argument("--end", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            result = ingest(args.ledger, _read_events(args.events))
        elif args.command == "summary":
            result = summarize(args.ledger, args.provider, args.cohort, args.start, args.end)
        else:
            result = summarize_all(args.ledger, args.provider, args.start, args.end)
        json.dump(result, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (LedgerError, OSError, TypeError, ValueError) as exc:
        print(f"outcome_ledger: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
