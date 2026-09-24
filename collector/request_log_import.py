#!/usr/bin/env python3
"""Import OpenCode request-log exports as a private, deduplicated usage ledger.

This is observed request usage, not verified task completion or invoice cost.
No headers, prompts, API-key identifiers, location or arbitrary metadata survive.
The source export is never changed. Overlapping exports deduplicate by workspace
and log-row ID (requestID can repeat); conflicts require reconciliation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from collections import Counter


TOKEN_FIELDS = ("inputTokens", "outputTokens", "reasoningTokens", "cacheReadTokens", "cacheWriteTokens")
RECORD_FIELDS = {"request_key", "correlation_key", "account_key", "session_key", "source",
                 "provider", "model", "requested_model", "product", "usage_path", "protocol",
                 "app", "outcome", "reported_cost", "cost_unit", "tokens", "task_id",
                 "validation_outcome", "startedAt", "finishedAt", "durationMs",
                 "timeToFirstTokenMs", "statusCode", "attemptCount", "attempts", "record_hash"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: expected nonempty string")
    return value


def number(value, field, *, integer=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field}: expected finite nonnegative number or null")
    if integer and not isinstance(value, int):
        raise ValueError(f"{field}: expected integer")
    return value


def normalize(row):
    if not isinstance(row, dict):
        raise ValueError("request must be an object")
    if row.get("category") != "inference":
        raise ValueError("only inference request exports are supported")
    workspace = text(row.get("workspaceID"), "workspaceID")
    request = text(row.get("requestID"), "requestID")
    row_id = text(row.get("id"), "id")
    session = row.get("sessionID")
    if session is not None:
        text(session, "sessionID")
    out = {
        "request_key": digest(["opencode-export-v1", workspace, row_id]),
        "correlation_key": digest(["opencode", workspace, request]),
        "account_key": digest(["opencode", workspace]),
        "session_key": digest(["opencode", workspace, session]) if session else None,
        "source": "opencode-request-export",
        "provider": text(row.get("provider"), "provider"),
        "model": text(row.get("model"), "model"),
        "requested_model": text(row["requestedModel"], "requestedModel") if row.get("requestedModel") is not None else None,
        "product": text(row.get("product"), "product"),
        "usage_path": text(row["path"], "path") if row.get("path") is not None else None,
        "protocol": text(row["protocol"], "protocol") if row.get("protocol") is not None else None,
        "app": text(row["app"], "app") if row.get("app") is not None else None,
        "outcome": text(row.get("outcome"), "outcome"),
        "reported_cost": number(row.get("cost"), "cost"),
        "cost_unit": "unverified-provider-cost-unit",
        "tokens": {k: number(row.get(k), k, integer=True) for k in TOKEN_FIELDS},
        "task_id": None,
        "validation_outcome": None,
    }
    for k in ("startedAt", "finishedAt", "durationMs", "timeToFirstTokenMs", "statusCode", "attemptCount"):
        out[k] = number(row.get(k), k, integer=True)
    if out["startedAt"] is None:
        raise ValueError("startedAt is required")
    if out["finishedAt"] is not None and out["finishedAt"] < out["startedAt"]:
        raise ValueError("finishedAt precedes startedAt")
    attempts = row.get("attempts")
    if attempts is not None and not isinstance(attempts, list):
        raise ValueError("attempts must be a list or null")
    out["attempts"] = None if attempts is None else []
    for a in attempts or []:
        if not isinstance(a, dict):
            raise ValueError("attempt must be an object")
        out["attempts"].append({"provider": text(a.get("provider"), "attempt.provider"),
                                "model": text(a.get("model"), "attempt.model"),
                                "statusCode": number(a.get("statusCode"), "attempt.statusCode", integer=True),
                                "durationMs": number(a.get("durationMs"), "attempt.durationMs", integer=True)})
    if attempts is not None and out["attemptCount"] is not None and len(attempts) != out["attemptCount"]:
        raise ValueError("attemptCount does not match exported attempts")
    if out["attempts"] and out["statusCode"] is not None and out["attempts"][-1]["statusCode"] is not None and out["attempts"][-1]["statusCode"] != out["statusCode"]:
        raise ValueError("final attempt status does not match request status")
    out["record_hash"] = digest(out)
    return out


def parse_export(raw):
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("expected OpenCode export object with items array")
    if type(payload.get("truncated")) is not bool:
        raise ValueError("export must declare truncated true/false")
    records = [normalize(r) for r in payload["items"]]
    source = {"sha256": hashlib.sha256(raw).hexdigest(), "truncated": payload["truncated"],
              "until": number(payload.get("until"), "until", integer=True), "exported_rows": len(records)}
    return source, records


def merge(ledger, source, records):
    if ledger.get("schema_version") != 1:
        raise ValueError("unsupported ledger schema")
    by_id = {}
    for r in [*ledger["records"], *records]:
        if set(r) != RECORD_FIELDS or set(r["tokens"]) != set(TOKEN_FIELDS):
            raise ValueError("unexpected or missing ledger fields")
        for a in r["attempts"] or []:
            if set(a) != {"provider", "model", "statusCode", "durationMs"}:
                raise ValueError("unexpected or missing attempt fields")
        if r.get("record_hash") != digest({k: v for k, v in r.items() if k != "record_hash"}):
            raise ValueError("ledger record hash mismatch")
        previous = by_id.get(r["request_key"])
        if previous is not None and previous != r:
            raise ValueError("conflicting observations of one request; reconciliation required")
        by_id[r["request_key"]] = r
    sources = {s["sha256"]: s for s in ledger["sources"]}
    sources[source["sha256"]] = source
    return {"schema_version": 1, "sources": list(sources.values()),
            "records": sorted(by_id.values(), key=lambda r: (r["startedAt"], r["request_key"]))}


def percentile(values, fraction):
    values = sorted(v for v in values if v is not None)
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def metrics(rows):
    attempts = [a for r in rows for a in r["attempts"] or []]
    costs = [r["reported_cost"] for r in rows if r["reported_cost"] is not None]
    tokens = {}
    for field in TOKEN_FIELDS:
        known = [r["tokens"][field] for r in rows if r["tokens"][field] is not None]
        tokens[field] = {"sum": sum(known) if known else None, "known_requests": len(known)}
    return {
        "requests": len(rows),
        "successful_requests": sum(r["outcome"] == "succeeded" and r["statusCode"] is not None and 200 <= r["statusCode"] < 300 for r in rows),
        "request_outcomes": dict(Counter(r["outcome"] for r in rows)),
        "observed_attempts": len(attempts),
        "requests_with_attempt_details": sum(r["attempts"] is not None for r in rows),
        "observed_retry_attempts": sum(max(0, len(r["attempts"]) - 1) for r in rows if r["attempts"] is not None),
        "attempt_http_errors": sum(a["statusCode"] is not None and a["statusCode"] >= 400 for a in attempts),
        "attempt_server_errors": sum(a["statusCode"] is not None and a["statusCode"] >= 500 for a in attempts),
        "attempt_rate_limits": sum(a["statusCode"] == 429 for a in attempts),
        "attempts_without_status": sum(a["statusCode"] is None for a in attempts),
        "reported_cost_sum": math.fsum(costs) if costs else None,
        "requests_with_cost": len(costs),
        "cost_unit": "unverified-provider-cost-unit",
        "tokens": tokens,
        "duration_ms": {"p50": percentile([r["durationMs"] for r in rows], .5), "p95": percentile([r["durationMs"] for r in rows], .95), "known_requests": sum(r["durationMs"] is not None for r in rows)},
        "ttft_ms": {"p50": percentile([r["timeToFirstTokenMs"] for r in rows], .5), "p95": percentile([r["timeToFirstTokenMs"] for r in rows], .95), "known_requests": sum(r["timeToFirstTokenMs"] is not None for r in rows)},
        "validated_tasks": None, "reworked_tasks": None, "cost_per_validated_task": None,
    }


def summarize(ledger):
    rows = ledger["records"]
    groups = {}
    for r in rows:
        key = (r["provider"], r["product"], r["account_key"], r["model"])
        groups.setdefault(key, []).append(r)
    return {"schema_version": 1,
            "coverage": {"complete_period": False, "scope": "imported request observations only",
                         "contains_truncated_export": any(s["truncated"] for s in ledger["sources"]),
                         "source_exports": len(ledger["sources"]),
                         "export_until_ms": sorted({s["until"] for s in ledger["sources"] if s["until"] is not None}),
                         "first_started_at_ms": min((r["startedAt"] for r in rows), default=None),
                         "last_started_at_ms": max((r["startedAt"] for r in rows), default=None)},
            "totals": metrics(rows),
            "by_model": [{"provider": key[0], "product": key[1], "account_key": key[2], "model": key[3], **metrics(values)} for key, values in sorted(groups.items())],
            "by_app": dict(Counter(r["app"] or "unknown" for r in rows)),
            "note": "Request success is not task validation. Cost units and billing semantics need provider verification. Token categories are not summed because provider inclusion semantics differ."}


def import_file(export_path, ledger_path):
    if export_path.resolve() == ledger_path.resolve():
        raise ValueError("ledger must not overwrite the source export")
    source, records = parse_export(export_path.read_bytes())
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(str(ledger_path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"schema_version": 1, "sources": [], "records": []}
        updated = merge(ledger, source, records)
        fd, temp = tempfile.mkstemp(prefix=".request-import-", dir=ledger_path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(updated, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, ledger_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return summarize(updated)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--summary", type=Path, help="atomically write the private dashboard summary")
    args = parser.parse_args()
    try:
        if args.summary and args.summary.resolve() in (args.export.resolve(), args.ledger.resolve()):
            raise ValueError("summary must not overwrite source or ledger")
        result = import_file(args.export, args.ledger)
        if args.summary:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix='.request-summary-', dir=args.summary.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(result, stream, indent=2, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, args.summary)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.exit(2, f"Import failed: {error}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
