"""Explicit, private wrapper for recording native CLI task outcomes.

Runs the supplied argv without a shell. A successful process or test run never
validates a task; validation requires the separate explicit accept command.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid

import outcome_ledger


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError("label must contain 1–120 printable characters")
    return value.strip()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _private_dir(path):
    path = Path(path).expanduser()
    if path.is_symlink():
        raise ValueError("state directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("state path must be a directory")
    os.chmod(path, 0o700)
    return path


def _paths(state_dir, task_id):
    root = _private_dir(state_dir)
    private = _private_dir(root / "task-runner")
    key = hashlib.sha256(task_id.encode()).hexdigest()
    return root / "outcome-ledger.json", private, key


def _atomic(path, raw):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".task-runner-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _locked(directory, key, blocking=True):
    path = directory / "locks" / (key + ".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("task lock must be a regular file")
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _load_task(ledger_path, task_id):
    with outcome_ledger._locked(ledger_path, False) as path:
        ledger = outcome_ledger._load(path)
    return ledger["tasks"].get(hashlib.sha256(task_id.encode()).hexdigest())


def _meta_path(directory, key):
    return directory / ("task-" + key + ".json")


def _read_meta(path, task_id):
    if not path.exists():
        return {"task_id": task_id, "latest_execution": None, "latest_verification": None,
                "acceptance": None}
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_mode & 0o077:
        raise ValueError("task metadata must be a private regular file")
    value = json.loads(path.read_text())
    if value.get("task_id") != task_id:
        raise ValueError("task metadata identity mismatch")
    return value


def _private_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("private receipt is missing, nonregular, or has unsafe permissions")
    return json.loads(path.read_text())


def _artifact(path):
    if not path:
        return None
    source = Path(path).expanduser()
    if source.is_symlink():
        raise ValueError("artifact must not be a symlink")
    target = source.resolve(strict=True)
    if not target.is_file():
        raise ValueError("artifact must be a regular file")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return str(target), digest.hexdigest()


def _event(task_id, kind, **fields):
    return {"event_id": str(uuid.uuid4()), "task_id": task_id, "type": kind, **fields}


def _execute(argv):
    started = _stamp()
    try:
        proc = subprocess.run(argv, shell=False, check=False)
        return proc.returncode, started, _stamp(), None
    except OSError as exc:
        # Do not retain the command or OS error text; both can contain secrets.
        return None, started, _stamp(), type(exc).__name__


def run_task(state_dir, provider, cohort, task_id, argv, artifact=None, label=None):
    if label is not None: label = _label(label)
    if not argv:
        raise ValueError("run requires a command after --")
    ledger, directory, key = _paths(state_dir, task_id)
    fd = _locked(directory, key)
    try:
        task = _load_task(ledger, task_id)
        now = _stamp()
        events = []
        if task is None:
            events.append(_event(task_id, "task_registered", provider=provider,
                                 cohort=cohort, started_at=now))
        elif task["provider"] != provider or task["cohort"] != cohort:
            raise ValueError("provider and cohort must match the registered task")
        elif task["status"] != "pending":
            reopened = max(now, task["last_event_at"])
            events.append(_event(task_id, "task_reopened", occurred_at=reopened))
            now = reopened
        events.append(_event(task_id, "turns_recorded", turns=1, errors=0, occurred_at=now))
        outcome_ledger.ingest(ledger, events)

        execution_id = str(uuid.uuid4())
        meta_path = _meta_path(directory, key)
        meta = _read_meta(meta_path, task_id)
        if label is not None:
            meta["label"] = _label(label)
        started = _stamp()
        receipt = {"kind": "run", "task_id": task_id, "execution_id": execution_id,
                   "argv_sha256": _hash(_json(list(argv))), "started_at": started,
                   "ended_at": None, "exit_code": None, "launch_error": None,
                   "artifact_sha256": None, "artifact_bound": bool(artifact), "status": "running"}
        meta.update(latest_execution=receipt, latest_execution_artifact_path=(str(Path(artifact).expanduser().resolve()) if artifact else None),
                    latest_verification=None, acceptance=None)
        _atomic(directory / ("run-" + execution_id + ".json"), _json(receipt))
        _atomic(meta_path, _json(meta))

        exit_code, _, ended, launch_error = _execute(list(argv))
        artifact_hash, artifact_error = None, None
        if artifact:
            try:
                artifact_path, artifact_hash = _artifact(artifact)
            except (OSError, ValueError):
                artifact_error = "ArtifactUnavailable"
                artifact_path = str(Path(artifact).expanduser().absolute())
        else:
            artifact_path = None
        receipt.update(ended_at=ended, exit_code=exit_code, launch_error=launch_error,
                       artifact_sha256=artifact_hash, artifact_error=artifact_error,
                       status="complete")
        _atomic(directory / ("run-" + execution_id + ".json"), _json(receipt))
        meta["latest_execution"] = receipt
        meta.update(latest_execution=receipt, latest_execution_artifact_path=artifact_path,
                    latest_verification=None, acceptance=None)
        _atomic(meta_path, _json(meta))
        return receipt
    finally:
        os.close(fd)


def verify_task(state_dir, task_id, reviewer, argv, check_label=None):
    if check_label is not None: check_label = _label(check_label)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("verify requires a reviewer identity")
    if not argv:
        raise ValueError("verify requires a nonempty check command after --")
    ledger, directory, key = _paths(state_dir, task_id)
    fd = _locked(directory, key)
    try:
        task = _load_task(ledger, task_id)
        if task is None or task["status"] != "pending":
            raise ValueError("verification requires a registered pending task")
        meta_path = _meta_path(directory, key)
        meta = _read_meta(meta_path, task_id)
        execution = meta.get("latest_execution")
        if not execution or execution.get("status") != "complete" or execution.get("launch_error"):
            raise ValueError("run the task before verifying it")
        meta.update(latest_verification=None, acceptance=None)
        _atomic(meta_path, _json(meta))
        artifact_path = meta.get("latest_execution_artifact_path")
        before_hash, artifact_error = None, None
        if execution.get("artifact_bound"):
            try:
                _, before_hash = _artifact(artifact_path)
            except (OSError, ValueError):
                artifact_error = "ArtifactUnavailable"
            if artifact_error or before_hash != execution.get("artifact_sha256"):
                raise ValueError("run artifact is missing or changed; rerun before verification")
        exit_code, started, ended, launch_error = _execute(list(argv))
        after_hash = None
        if execution.get("artifact_bound"):
            try:
                _, after_hash = _artifact(artifact_path)
            except (OSError, ValueError):
                after_hash = None
        record = {"kind": "verification", "task_id": task_id,
                  "execution_id": execution["execution_id"], "reviewer": reviewer.strip(),
                  "argv_sha256": _hash(_json(list(argv))), "started_at": started,
                  "ended_at": ended, "exit_code": exit_code, "launch_error": launch_error,
                  "artifact_sha256": before_hash, "artifact_bound": bool(execution.get("artifact_bound")),
                  "artifact_unchanged": before_hash == after_hash if execution.get("artifact_bound") else None}
        if check_label is not None:
            record["check_label"] = _label(check_label)
        record["verification_sha256"] = _hash(_json(record))
        _atomic(directory / ("verification-" + record["verification_sha256"] + ".json"), _json(record))
        meta.update(latest_verification=record, acceptance=None)
        _atomic(meta_path, _json(meta))
        return record
    finally:
        os.close(fd)


def accept_task(state_dir, task_id, reviewer, expected_run=None, expected_verification=None):
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("accept requires a reviewer identity")
    reviewer = reviewer.strip()
    ledger, directory, key = _paths(state_dir, task_id)
    fd = _locked(directory, key, blocking=expected_run is None)
    try:
        meta_path = _meta_path(directory, key)
        meta = _read_meta(meta_path, task_id)
        task = _load_task(ledger, task_id)
        was_validated = task is not None and task["status"] == "validated"
        prior = meta.get("acceptance")
        if task is None or task["status"] not in ("pending", "validated"):
            raise ValueError("acceptance requires a registered pending task")
        execution = meta.get("latest_execution")
        verification = meta.get("latest_verification")
        if expected_run is not None and (not execution or execution.get("execution_id") != expected_run):
            raise ValueError("task changed since review; reload the review queue")
        if expected_verification is not None and (not verification or verification.get("verification_sha256") != expected_verification):
            raise ValueError("verification changed since review; reload the review queue")
        if (not execution or execution.get("status") != "complete" or execution.get("launch_error")
                or execution.get("artifact_error") or not verification
                or verification.get("execution_id") != execution.get("execution_id")
                or verification.get("exit_code") != 0 or verification.get("launch_error")):
            raise ValueError("acceptance requires a started run and successful verification of that latest run")
        run_receipt = _private_json(directory / ("run-" + execution["execution_id"] + ".json"))
        if run_receipt != execution:
            raise ValueError("latest run receipt changed")
        verification_path = directory / ("verification-" + verification["verification_sha256"] + ".json")
        verification_receipt = _private_json(verification_path)
        verification_unsigned = {k: v for k, v in verification_receipt.items() if k != "verification_sha256"}
        if (_hash(_json(verification_unsigned)) != verification["verification_sha256"]
                or verification_receipt != verification):
            raise ValueError("latest verification receipt changed")
        artifact_path = meta.get("latest_execution_artifact_path")
        if execution.get("artifact_bound"):
            try:
                _, current_artifact_hash = _artifact(artifact_path)
            except (OSError, ValueError) as exc:
                raise ValueError("verified artifact is missing or unsafe") from exc
            if (current_artifact_hash != execution.get("artifact_sha256")
                    or verification.get("artifact_unchanged") is not True):
                raise ValueError("verified artifact changed after checks")
        if was_validated:
            if task.get("validation_evidence"):
                digest = task["validation_evidence"][-1]
                proof_path = ledger.with_name(ledger.name + ".evidence") / (digest + ".bin")
                proof = _private_json(proof_path)
                unsigned = {k: v for k, v in proof.items() if k not in ("schema_version", "attestation_sha256")}
                if (proof.get("task_id") == task_id and proof.get("reviewer") == reviewer
                        and proof.get("model_run_id") == execution["execution_id"]
                        and proof.get("verification_sha256") == verification["verification_sha256"]
                        and _hash(_json(unsigned)) == proof.get("attestation_sha256")):
                    meta["acceptance"] = {"reviewer": reviewer, "evidence_sha256": digest,
                                           "execution_id": execution["execution_id"]}
                    _atomic(meta_path, _json(meta))
                    return {"status": "validated", "duplicate": True, "recovered": not bool(prior)}
            raise ValueError("task is already finalized")
        if prior:
            raise ValueError("pending task already has acceptance metadata; reconcile state before retrying")
        accepted_at = _stamp()
        attestation = {"task_id": task_id, "model_run_id": execution["execution_id"],
                       "verification_sha256": verification["verification_sha256"],
                       "reviewer": reviewer, "accepted_at": accepted_at,
                       "blocking_checks_passed": True, "semantic_acceptance": True,
                       "artifact_bound": bool(execution.get("artifact_bound")),
                       "artifact_sha256": execution.get("artifact_sha256")}
        attestation["attestation_sha256"] = _hash(_json(attestation))
        evidence = {"schema_version": 1, **attestation}
        raw = _json(evidence)
        digest = _hash(raw)
        evidence_dir = _private_dir(directory / "evidence")
        evidence_path = evidence_dir / (digest + ".json")
        _atomic(evidence_path, raw)
        event_time = max(_stamp(), task["last_event_at"])
        outcome_ledger.ingest(ledger, [_event(task_id, "task_finalized", status="validated",
            occurred_at=event_time, evidence_path=str(evidence_path), evidence_sha256=digest)])
        meta["acceptance"] = {"reviewer": reviewer, "evidence_sha256": digest,
                               "execution_id": execution["execution_id"]}
        _atomic(meta_path, _json(meta))
        return {"status": "validated", "duplicate": False, "evidence_sha256": digest}
    finally:
        os.close(fd)


def finalize_task(state_dir, task_id, status, expected_run=None, reviewer=None):
    if status not in ("failed", "abandoned"):
        raise ValueError("explicit final status must be failed or abandoned")
    ledger, directory, key = _paths(state_dir, task_id)
    fd = _locked(directory, key, blocking=expected_run is None)
    try:
        if expected_run is not None:
            meta = _read_meta(_meta_path(directory, key), task_id)
            execution = meta.get("latest_execution")
            if not execution or execution.get("execution_id") != expected_run:
                raise ValueError("task changed since review; reload the review queue")
        task = _load_task(ledger, task_id)
        if task is None or task["status"] != "pending":
            raise ValueError("explicit finalization requires a registered pending task")
        at = max(_stamp(), task["last_event_at"])
        event = _event(task_id, "task_finalized", status=status, occurred_at=at)
        if reviewer is not None:
            reviewer = _label(reviewer)
            decision = {"task_id":task_id, "event_id":event["event_id"], "reviewer":reviewer,
                        "status":status, "execution_id":expected_run, "decided_at":at}
            # Retain intent before the ledger mutation; the event ID proves whether it applied.
            _atomic(directory / ("decision-" + event["event_id"] + ".json"), _json(decision))
        outcome_ledger.ingest(ledger, [event])
        return {"status": status}
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    def common(command):
        command.add_argument("--state-dir", required=True)
        command.add_argument("--task-id", required=True)
    run = sub.add_parser("run", help="record a pending CLI task and run argv without a shell")
    common(run); run.add_argument("--provider", required=True, choices=sorted(outcome_ledger.PROVIDERS))
    run.add_argument("--cohort", required=True); run.add_argument("--artifact", help="optional output file to hash-bind through verification")
    run.add_argument("--label", help="short private task label for the review queue")
    run.add_argument("command", nargs=argparse.REMAINDER)
    verify = sub.add_parser("verify", help="run one explicit check for the latest task execution")
    common(verify); verify.add_argument("--reviewer", required=True); verify.add_argument("--check-label", help="short description of the check being run"); verify.add_argument("command", nargs=argparse.REMAINDER)
    accept = sub.add_parser("accept", help="attest semantic acceptance after successful checks")
    common(accept); accept.add_argument("--reviewer", required=True)
    for action in ("fail", "abandon"):
        command = sub.add_parser(action, help=f"explicitly finalize a pending task as {action}ed")
        common(command)
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            result = run_task(args.state_dir, args.provider, args.cohort, args.task_id, command, args.artifact, args.label)
            print(json.dumps(result, sort_keys=True))
            return result["exit_code"] if result["exit_code"] is not None else 127
        if args.action == "verify":
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            result = verify_task(args.state_dir, args.task_id, args.reviewer, command, args.check_label)
            print(json.dumps(result, sort_keys=True))
            return result["exit_code"] if result["exit_code"] is not None else 127
        if args.action == "accept":
            result = accept_task(args.state_dir, args.task_id, args.reviewer)
        else:
            result = finalize_task(args.state_dir, args.task_id,
                                   "failed" if args.action == "fail" else "abandoned")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, outcome_ledger.LedgerError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
