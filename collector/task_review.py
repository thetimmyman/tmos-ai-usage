"""Sanitized, receipt-bound task review for the native dashboard. No command execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import task_runner as runner
import outcome_ledger

HEX = re.compile(r'[a-f0-9]{64}\Z')
RUN = re.compile(r'[a-f0-9-]{36}\Z')
MAX_META = 64 * 1024


def _read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('review metadata must be a private regular file')
        raw = stream.read(MAX_META + 1)
        if len(raw) > MAX_META:
            raise ValueError('review metadata exceeds size limit')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('invalid review metadata')
    return value


def _resolve(directory, ref):
    if not isinstance(ref, str) or not HEX.fullmatch(ref): raise ValueError('invalid task reference')
    meta = _read(Path(directory) / 'task-runner' / ('task-' + ref + '.json'))
    identity = outcome_ledger._atom(meta.get('task_id'), 'task_id')
    if hashlib.sha256(identity.encode()).hexdigest() != ref: raise ValueError('task reference mismatch')
    return identity, meta


def _ready(directory, meta, task):
    if task.get('status') != 'pending': return False, 'Task is already finalized.'
    run, check = meta.get('latest_execution'), meta.get('latest_verification')
    if not isinstance(run, dict) or run.get('status') != 'complete' or run.get('launch_error'):
        return False, 'Complete a captured task run first.'
    if not RUN.fullmatch(str(run.get('execution_id', ''))): return False, 'Run receipt is invalid.'
    if run.get('artifact_error'): return False, 'The run artifact is unavailable.'
    if not isinstance(check, dict) or check.get('execution_id') != run['execution_id']:
        return False, 'Run verification checks for the latest execution.'
    if check.get('exit_code') != 0 or check.get('launch_error'):
        return False, 'The latest verification checks did not pass.'
    digest = check.get('verification_sha256')
    if not isinstance(digest, str) or not HEX.fullmatch(digest): return False, 'Verification receipt is invalid.'
    base = Path(directory) / 'task-runner'
    try:
        if _read(base / ('run-' + run['execution_id'] + '.json')) != run:
            return False, 'Run receipt changed.'
        retained = _read(base / ('verification-' + digest + '.json'))
        unsigned = {k:v for k,v in retained.items() if k != 'verification_sha256'}
        if retained != check or runner._hash(runner._json(unsigned)) != digest:
            return False, 'Verification receipt changed.'
        if run.get('artifact_bound'):
            _, actual = runner._artifact(meta.get('latest_execution_artifact_path'))
            if actual != run.get('artifact_sha256') or check.get('artifact_unchanged') is not True:
                return False, 'Artifact changed; rerun and verify before accepting.'
    except (OSError, ValueError, TypeError):
        return False, 'Required private evidence is unavailable.'
    return True, 'Checks passed. Review the result before accepting this task.'


def review_list(directory, limit=100):
    directory = Path(directory)
    root = directory / 'task-runner'
    result = {'schema_version':1, 'tasks':[], 'truncated':False, 'unavailable':0}
    if not root.exists(): return result
    if root.is_symlink() or not root.is_dir(): raise ValueError('invalid review directory')
    candidates = []
    for i, entry in enumerate(os.scandir(root)):
        if i >= 10000:
            result['truncated'] = True; break
        if re.fullmatch(r'task-[a-f0-9]{64}\.json', entry.name):
            try: candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
            except OSError: result['unavailable'] += 1
    result['truncated'] |= len(candidates) > limit
    for _, name in sorted(candidates, reverse=True)[:limit]:
        ref = name[5:-5]
        try:
            identity, meta = _resolve(directory, ref)
            task = runner._load_task(directory / 'outcome-ledger.json', identity)
            if task is None: raise ValueError('missing ledger task')
            ready, reason = _ready(directory, meta, task)
            run = meta.get('latest_execution') if isinstance(meta.get('latest_execution'),dict) else {}
            check = meta.get('latest_verification') if isinstance(meta.get('latest_verification'),dict) else {}
            label = runner._label(meta['label']) if meta.get('label') else 'Task ' + ref[:10]
            row = {'task_ref':ref, 'label':label, 'provider':task['provider'], 'cohort':task['cohort'],
                   'status':task['status'], 'turns':task['turns'], 'errors':task['errors'],
                   'registered_at':task['started_at'], 'can_accept':ready, 'reason':reason,
                   'run':{k:run.get(k) for k in ('execution_id','started_at','ended_at','exit_code','artifact_bound','artifact_sha256')},
                   'verification':{k:check.get(k) for k in ('reviewer','started_at','ended_at','exit_code','verification_sha256','artifact_unchanged')}}
            row['verification']['check_label'] = runner._label(check['check_label']) if check.get('check_label') else 'Check command (unlabelled)'
            result['tasks'].append(row)
        except (OSError, ValueError, TypeError, KeyError, outcome_ledger.LedgerError):
            result['unavailable'] += 1
    return result


def decide(directory, ref, reviewer, decision, expected_run, expected_verification=None, semantic_accepted=False):
    if not isinstance(expected_run, str) or not RUN.fullmatch(expected_run):
        raise ValueError('Select a captured run and reload the review queue.')
    if decision == 'accept' and (not semantic_accepted or not isinstance(expected_verification, str) or not HEX.fullmatch(expected_verification)):
        raise ValueError('Acceptance requires reviewed checks and explicit semantic confirmation.')
    reviewer = runner._label(reviewer)
    identity, _ = _resolve(directory, ref)
    if decision == 'accept':
        result = runner.accept_task(directory, identity, reviewer, expected_run, expected_verification)
    elif decision in ('fail','abandon'):
        if not isinstance(reviewer,str) or not reviewer.strip(): raise ValueError('Reviewer identity is required.')
        result = runner.finalize_task(directory, identity, 'failed' if decision == 'fail' else 'abandoned', expected_run, reviewer)
    else: raise ValueError('Unknown review decision.')
    return {'ok':True, **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    listing = sub.add_parser('list'); listing.add_argument('--state-dir',required=True)
    action = sub.add_parser('decide'); action.add_argument('--state-dir',required=True)
    for field in ('task-ref','reviewer','decision','expected-run'): action.add_argument('--'+field,required=True)
    action.add_argument('--expected-verification'); action.add_argument('--semantic-accepted',action='store_true')
    args = parser.parse_args(argv)
    try:
        value = review_list(args.state_dir) if args.action == 'list' else decide(args.state_dir,args.task_ref,args.reviewer,args.decision,args.expected_run,args.expected_verification,args.semantic_accepted)
        print(json.dumps(value,allow_nan=False)); return 0
    except (OSError,ValueError,TypeError,KeyError,outcome_ledger.LedgerError):
        print(json.dumps({'ok':False,'error':'Review could not be completed. Reload the queue and check the task evidence.'})); return 2


if __name__ == '__main__': sys.exit(main())
