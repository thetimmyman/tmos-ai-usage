"""Publish strict task-value receipts from local ledgers after explicit completeness attestation.

This is an opt-in CLI only; it never runs during collection or routes inference. The operator's
--attest-complete flag asserts that the outcome ledger contains the whole named cohort and that
the billing ledger contains every recognized invoice/expense for each provider. The program
checks ledger consistency, terminal outcomes, evidence hashes, known service intervals and
gap-free period coverage. It cannot prove that the operator's completeness assertion is true.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_HALF_UP
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import billing_ledger
import inference_reports
import outcome_ledger
import outcome_ingest
from subscription_value import atomic_json

PROVIDERS = outcome_ledger.PROVIDERS
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
CONTEXT_VALUE = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
BASIS = "recognized_subscription_and_metered_usd"


class PublishError(ValueError):
    """Publication refused; economics.json was not replaced."""


def _utc(value, name):
    try:
        result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublishError(f"invalid {name}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise PublishError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_private(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise PublishError("source evidence must be a private regular file")
    raw = path.read_bytes()
    if not raw:
        raise PublishError("source evidence cannot be empty")
    return raw


@contextmanager
def _billing_shared_lock(directory):
    lock_path = Path(directory) / "billing.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise PublishError("billing lock cannot be opened") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PublishError("billing lock must be a regular file")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_outcomes(state_dir, provider, cohort, start, end):
    path = Path(state_dir) / "outcome-ledger.json"
    with outcome_ledger._locked(path, False) as locked_path:
        data = outcome_ledger._load(locked_path)
        try:
            ledger_raw = _read_private(locked_path)
        except OSError as exc:
            raise PublishError("outcome ledger missing") from exc
    selected = []
    for task_hash, task in data["tasks"].items():
        if task["provider"] != provider or task["cohort"] != cohort:
            continue
        started = _utc(task["started_at"], "task start")
        if not (start <= started < end):
            continue
        last_event = _utc(task["last_event_at"], "task last event")
        if last_event >= end:
            raise PublishError("outcome task changed after comparison end; current state is not historical")
        if task["status"] == "pending":
            raise PublishError("cohort includes pending task; outcomes are incomplete")
        if task["status"] not in ("validated", "failed", "abandoned"):
            raise PublishError("unknown terminal task status")
        selected.append((task_hash, task))
    if not selected:
        raise PublishError(f"no observed tasks for {provider} in requested cohort and interval")
    return path, ledger_raw, selected


def _read_billing(state_dir, provider, start, end, selected_evidence_owners=None):
    directory = Path(state_dir)
    ledger_path = directory / "billing-ledger.json"
    with _billing_shared_lock(directory):
        if ledger_path.is_symlink() or not ledger_path.is_file() or ledger_path.stat().st_mode & 0o077:
            raise PublishError("billing ledger missing or not private")
        try:
            ledger_raw = ledger_path.read_bytes()
            ledger = json.loads(ledger_raw)
        except (OSError, ValueError, UnicodeError) as exc:
            raise PublishError("billing ledger unreadable") from exc
        if not isinstance(ledger, dict) or ledger.get("version") != 1 or not isinstance(ledger.get("invoices"), list):
            raise PublishError("billing ledger schema invalid")
        invoices = []
        evidence_directory = directory / "billing-evidence"
        if evidence_directory.is_symlink() or not evidence_directory.is_dir() or evidence_directory.stat().st_mode & 0o077:
            raise PublishError("billing evidence directory missing or not private")
        ids, evidence_ids = set(), set()
        for source in ledger["invoices"]:
            if not isinstance(source, dict):
                raise PublishError("billing ledger has malformed invoice")
            if source.get("provider") != provider:
                continue
            try:
                row = billing_ledger.normalize(source)
            except (ValueError, KeyError, TypeError) as exc:
                raise PublishError("billing invoice is malformed") from exc
            if row["id"] in ids or row["source_sha256"] in evidence_ids:
                raise PublishError("duplicate invoice or duplicate evidence for provider")
            if selected_evidence_owners is not None:
                owner = selected_evidence_owners.get(row["source_sha256"])
                if owner is not None and owner != provider:
                    raise PublishError("one invoice evidence leaf cannot be attributed to multiple providers")
                selected_evidence_owners[row["source_sha256"]] = provider
            ids.add(row["id"])
            evidence_ids.add(row["source_sha256"])
            if not row["service_start"] or not row["service_end"]:
                raise PublishError("invoice has unknown service interval")
            evidence_path = directory / "billing-evidence" / row["source_sha256"]
            raw = _read_private(evidence_path)
            if _sha(raw) != row["source_sha256"]:
                raise PublishError("billing source evidence hash mismatch")
            invoices.append((row, raw))
    if not invoices:
        raise PublishError(f"no billing invoices for {provider}")
    # The intervals must continuously cover the entire comparison window. This proves observed
    # invoice-date coverage only; --attest-complete is still required for missing-invoice risk.
    intervals = sorted((_utc(row["service_start"], "service start"),
                        _utc(row["service_end"], "service end")) for row, _ in invoices)
    cursor = start
    for a, b in intervals:
        if b <= cursor:
            continue
        if a > cursor:
            break
        cursor = max(cursor, b)
        if cursor >= end:
            break
    if cursor < end:
        raise PublishError("known service intervals have a gap or do not cover the full comparison period")
    return ledger_raw, invoices


def _micros(delta):
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _coverage_allocation(row, start, end):
    a, b = _utc(row["service_start"], "service start"), _utc(row["service_end"], "service end")
    overlap = max(timedelta(0), min(end, b) - max(start, a))
    if overlap <= timedelta(0):
        return None
    total_micros = _micros(b - a)
    overlap_micros = _micros(overlap)
    cents = billing_ledger.money(row["paid_usd"])
    return Decimal(cents) / Decimal(100) * Decimal(overlap_micros) / Decimal(total_micros), overlap_micros, total_micros


def _add_leaf(leaves, raw):
    digest = _sha(raw)
    old = leaves.get(digest)
    if old is not None and old != raw:
        raise PublishError("content-address collision")
    leaves[digest] = raw
    return digest


def _outcome_receipt(leaves, state_dir, provider, cohort, period, start, end, tasks, ledger_raw, attested_by):
    ledger_digest = _add_leaf(leaves, ledger_raw)
    projected = []
    for task_hash, task in tasks:
        original_proofs = []
        for proof_hash in task["validation_evidence"]:
            path = Path(state_dir) / "outcome-ledger.json.evidence" / (proof_hash + ".bin")
            raw = _read_private(path)
            if _sha(raw) != proof_hash:
                raise PublishError("outcome semantic evidence hash mismatch")
            original_proofs.append(_add_leaf(leaves, raw))
        task_leaf = {"schema_version": 1, "task_id_sha256": task_hash, "provider": provider,
                     "cohort": cohort, "started_at": task["started_at"], "last_event_at": task["last_event_at"],
                     "status": task["status"], "turns": task["turns"], "errors": task["errors"],
                     "reopened_count": task["reopened_count"], "validation_evidence_sha256": original_proofs,
                     "source_ledger_sha256": ledger_digest}
        task_leaf_raw = _json_bytes(task_leaf)
        task_leaf_hash = _add_leaf(leaves, task_leaf_raw)
        projected.append({"id": task_hash, "status": task["status"], "turns": task["turns"],
                          "reworked": task["reopened_count"] > 0, "evidence_sha256": task_leaf_hash})
    context = {"period": period, "cohort": cohort}
    return {**context, "provider": provider, "coverage_complete": True,
            "population_task_ids": sorted(row["id"] for row in projected), "tasks": projected,
            "producer_attestation": {"explicit_complete_flag": True, "attested_by": attested_by,
                "attested_at": _iso(datetime.now(timezone.utc)),
                "scope": "all registered tasks matching provider/cohort/start interval; operator asserts population completeness",
                "limitations": "hashes establish snapshot integrity; they cannot establish task quality or prove omitted work does not exist"}}


def _spend_receipt(leaves, provider, cohort, period, start, end, invoices, ledger_raw, attested_by):
    source_ledger_digest = _add_leaf(leaves, ledger_raw)
    allocations, exact_total = [], Decimal(0)
    with localcontext() as context:
        context.prec = 50
        for row, raw in invoices:
            result = _coverage_allocation(row, start, end)
            if result is None:
                continue
            allocated, overlap_micros, total_micros = result
            exact_total += allocated
            source_digest = row["source_sha256"]
            copied_source_hash = _add_leaf(leaves, raw)
            if source_digest != copied_source_hash:
                raise PublishError("billing source evidence changed")
            allocations.append({"invoice_id_sha256": _sha((provider + ":" + row["id"]).encode()),
                "source_sha256": source_digest, "service_start": row["service_start"],
                "service_end": row["service_end"], "paid_usd": f"{Decimal(billing_ledger.money(row['paid_usd'])) / Decimal(100):.2f}",
                "overlap_microseconds": overlap_micros, "service_microseconds": total_micros,
                "allocated_usd": format(allocated, "f")})
        if not allocations:
            raise PublishError("no invoice service expense overlaps comparison period")
        # Sum exact Decimal allocations before a single conversion to the numeric projection.
        rounded_total = exact_total.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    allocation_leaf = {"schema_version": 1, "provider": provider, "period": period,
        "period_start": _iso(start), "period_end": _iso(end), "allocations": allocations,
        "exact_allocated_usd": format(exact_total, "f"), "source_ledger_sha256": source_ledger_digest}
    allocation_raw = _json_bytes(allocation_leaf)
    allocation_hash = _add_leaf(leaves, allocation_raw)
    charge_id = "subscription-expense-" + _sha((provider + ":" + period + ":" + _iso(start) + ":" + _iso(end)).encode())[:32]
    return {"period": period, "cohort": cohort, "provider": provider, "coverage_complete": True,
        "basis": BASIS, "charges": [{"id": charge_id, "recognized_usd": float(rounded_total),
            "evidence_sha256": allocation_hash}],
        "producer_attestation": {"explicit_complete_flag": True, "attested_by": attested_by,
            "attested_at": _iso(datetime.now(timezone.utc)),
            "scope": "all recognized invoices with continuous known service coverage; operator asserts no omitted spend",
            "limitations": "expense includes invoice discounts, taxes and fees and allocates idle subscription cost; API list prices and promotional face value are excluded"}}


def _existing_refs(state_dir, context, replacing):
    path = Path(state_dir) / "economics.json"
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise PublishError("existing economics document must be a private regular file")
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PublishError("existing economics document is invalid; leave it unchanged") from exc
    if not isinstance(old, dict) or not isinstance(old.get("context"), dict) or not isinstance(old.get("providers"), dict):
        raise PublishError("existing economics document is malformed; leave it unchanged")
    if old["context"] != context:
        return {}
    keep = {}
    for provider, refs in old["providers"].items():
        if provider in replacing or not isinstance(refs, dict):
            continue
        try:
            inference_reports.economics_projection(state_dir, refs, context, provider)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        keep[provider] = {"outcomes_sha256": refs["outcomes_sha256"], "spend_sha256": refs["spend_sha256"]}
    return keep


def _store_leaves(directory, leaves):
    evidence = directory / "evidence"
    if evidence.is_symlink():
        raise PublishError("evidence directory must not be a symlink")
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(evidence, 0o700)
    for digest, raw in leaves.items():
        if _sha(raw) != digest:
            raise PublishError("prepared evidence hash mismatch")
        target = evidence / (digest + ".json")
        if target.is_symlink():
            raise PublishError("existing evidence leaf must not be a symlink")
        if target.exists():
            if not stat.S_ISREG(target.stat().st_mode) or target.stat().st_mode & 0o077:
                raise PublishError("existing evidence leaf is not private")
            if target.read_bytes() != raw:
                raise PublishError("existing evidence leaf changed")
            continue
        fd, temporary = tempfile.mkstemp(prefix=".comparison-", dir=evidence)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def publish(state_dir, start, end, period, cohort, providers, *, attest_complete=False, attested_by=None):
    """Build and atomically publish one declared context from actual local ledgers."""
    directory = Path(state_dir)
    if not attest_complete:
        raise PublishError("--attest-complete is required to publish strict rankings")
    if not isinstance(period, str) or not CONTEXT_VALUE.fullmatch(period):
        raise PublishError("invalid period")
    if not isinstance(cohort, str) or not CONTEXT_VALUE.fullmatch(cohort):
        raise PublishError("invalid cohort")
    start_dt, end_dt = _utc(start, "start"), _utc(end, "end")
    now = datetime.now(timezone.utc)
    if start_dt >= end_dt or end_dt > now:
        raise PublishError("comparison interval must be positive and end no later than now")
    provider_list = list(providers)
    if not provider_list or len(set(provider_list)) != len(provider_list):
        raise PublishError("at least one unique provider is required")
    if any(provider not in PROVIDERS for provider in provider_list):
        raise PublishError("unsupported provider")
    actor = attested_by or getpass.getuser()
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 120:
        raise PublishError("invalid attestor")

    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "economics.lock"
    if lock_path.is_symlink():
        raise PublishError("publisher lock must not be a symlink")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PublishError("publisher lock must be a regular file")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            # One consistent multi-provider source snapshot: ingestion may continue elsewhere,
            # but neither ledger can change between provider reads or before publication.
            with outcome_ingest._lock(directory, False):
              with outcome_ledger._locked(directory / "outcome-ledger.json", False):
                pending = outcome_ingest._load_index(directory)
                unresolved = outcome_ingest._unresolved(directory, pending,
                    outcome_ingest._current_files(directory / "outcome-events"))
                if unresolved[0] or unresolved[1]:
                    raise PublishError("outcome inbox has unresolved pending or rejected event files")
                with _billing_shared_lock(directory):
                    leaves, refs, selected_evidence_owners = {}, {}, {}
                    for provider in provider_list:
                        _, ledger_raw, tasks = _read_outcomes(directory, provider, cohort, start_dt, end_dt)
                        billing_raw, invoices = _read_billing(directory, provider, start_dt, end_dt,
                                                              selected_evidence_owners)
                        outcome = _outcome_receipt(leaves, directory, provider, cohort, period,
                                                   start_dt, end_dt, tasks, ledger_raw, actor)
                        spend = _spend_receipt(leaves, provider, cohort, period, start_dt, end_dt, invoices,
                                               billing_raw, actor)
                        outcome_raw, spend_raw = _json_bytes(outcome), _json_bytes(spend)
                        refs[provider] = {"outcomes_sha256": _add_leaf(leaves, outcome_raw),
                                          "spend_sha256": _add_leaf(leaves, spend_raw)}
                    context = {"period": period, "cohort": cohort}
                    # Validate all requests before writing any files. Existing unrelated provider refs
                    # survive only when the context is identical and every old receipt still verifies.
                    preserved = _existing_refs(directory, context, set(refs))
                    _store_leaves(directory, leaves)
                    full_refs = {**preserved, **refs}
                    for provider in provider_list:
                        inference_reports.economics_projection(directory, full_refs[provider], context, provider)
                    atomic_json(directory / "economics.json", {"context": context, "providers": full_refs})
                    return {"context": context, "providers": sorted(refs), "preserved": sorted(preserved),
                            "outcomes_sha256": {p: r["outcomes_sha256"] for p, r in refs.items()},
                            "spend_sha256": {p: r["spend_sha256"] for p, r in refs.items()},
                            "attested_by": actor, "coverage_complete": True}
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/tmos-ai-usage")
    parser.add_argument("--start", required=True, help="timezone-aware inclusive period start")
    parser.add_argument("--end", required=True, help="timezone-aware exclusive period end")
    parser.add_argument("--period", required=True, help="stable comparison period identifier")
    parser.add_argument("--cohort", required=True, help="exact task cohort identifier")
    parser.add_argument("--provider", action="append", required=True, choices=sorted(PROVIDERS))
    parser.add_argument("--attest-complete", action="store_true",
                        help="assert complete cohort population and recognized invoice coverage for every provider")
    parser.add_argument("--attestor", help="name recorded with the explicit completeness attestation")
    args = parser.parse_args(argv)
    try:
        result = publish(args.state_dir, args.start, args.end, args.period, args.cohort,
                         args.provider, attest_complete=args.attest_complete, attested_by=args.attestor)
    except (PublishError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        parser.exit(2, "Comparison publish refused; existing document unchanged: " + str(exc) + "\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
