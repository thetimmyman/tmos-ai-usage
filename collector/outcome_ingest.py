"""Consume explicitly exported harness events without reading transcript contents.

Rejected, interrupted, or deleted-but-unresolved files block strict publication. Restore or
correct them under the same filename, then refresh; a missing ledger is rebuilt by replaying the
retained exports in bounded fair batches.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import outcome_ledger
from contextlib import contextmanager
from subscription_value import atomic_json

MAX_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
MAX_STATUS_FILES = 1000
MAX_STATUS_BYTES = 100 * 1024 * 1024
INDEX_VERSION = 1


@contextmanager
def _lock(directory, exclusive):
    path = Path(directory) / 'outcome-ingest.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('outcome import lock is not regular')
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'a+') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
    finally:
        try: os.close(fd)
        except OSError: pass


def _index_path(directory): return Path(directory) / 'outcome-ingest-index.json'


def _load_index(directory):
    path = _index_path(directory)
    if not path.exists(): return {'version': INDEX_VERSION, 'cursor': '', 'files': {}}
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('outcome import index must be a private regular file')
    value = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(value, dict) or value.get('version') != INDEX_VERSION
            or not isinstance(value.get('cursor'), str) or not isinstance(value.get('files'), dict)):
        raise ValueError('outcome import index is malformed')
    for name, row in value['files'].items():
        if (not isinstance(name, str) or Path(name).name != name or not isinstance(row, dict)
                or not isinstance(row.get('sha256'), str) or len(row['sha256']) != 64
                or row.get('status') not in ('complete', 'pending', 'rejected')):
            raise ValueError('outcome import index is malformed')
    return value


def _read_events(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError('not regular')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES: raise ValueError('export too large')
    digest = hashlib.sha256(raw).hexdigest()
    lines = raw.splitlines()
    incomplete = False
    if raw and not raw.endswith(b'\n') and lines:
        try: final = json.loads(lines[-1])
        except (ValueError, UnicodeError):
            incomplete = True
            lines.pop()
        else:
            # JSONL convention usually asks for a final newline, but complete JSON is enough.
            pass
    events = []
    try:
        events = [json.loads(line) for line in lines if line.strip()]
    except (ValueError, UnicodeError) as exc:
        raise ValueError('malformed complete event line') from exc
    return raw, digest, events, incomplete


def _current_files(inbox):
    if not inbox.exists(): return []
    if inbox.is_symlink() or not inbox.is_dir(): raise ValueError('outcome inbox must be a real directory')
    return sorted((p for p in inbox.iterdir() if p.suffix.lower() == '.jsonl'), key=lambda p: p.name)


def _unresolved(directory, index, files):
    rows = index['files']
    if len(files) > MAX_STATUS_FILES or sum(p.stat().st_size for p in files if p.exists()) > MAX_STATUS_BYTES:
        # Fail closed rather than hashing an unbounded inbox during ranking publication.
        return 1, 0
    present = {p.name: p for p in files}
    pending = rejected = 0
    for name, path in present.items():
        previous = rows.get(name)
        if previous is None:
            pending += 1
            continue
        try:
            _, digest, _, _ = _read_events(path)
        except (OSError, ValueError, TypeError):
            rejected += 1
            continue
        if digest != previous['sha256']:
            pending += 1
        elif previous['status'] == 'pending': pending += 1
        elif previous['status'] == 'rejected': rejected += 1
    for name, row in rows.items():
        if name not in present and row['status'] in ('pending', 'rejected'):
            rejected += 1
    if any(row['status'] == 'complete' for row in rows.values()):
        ledger = Path(directory) / 'outcome-ledger.json'
        if not ledger.exists():
            pending += 1
        else:
            try:
                with outcome_ledger._locked(ledger, False) as locked_path:
                    outcome_ledger._load(locked_path)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                rejected += 1
    return pending, rejected


def status(directory):
    """Return unresolved inbox counts; stable processed event files are fully represented in ledger."""
    directory = Path(directory)
    with _lock(directory, False):
        index = _load_index(directory)
        pending, rejected = _unresolved(directory, index, _current_files(directory / 'outcome-events'))
        return {'pending_files': pending, 'rejected_files': rejected,
                'unresolved_files': pending + rejected}


def ingest(directory):
    directory = Path(directory)
    inbox = directory / 'outcome-events'
    result = {'applied': 0, 'duplicates': 0, 'rejected_files': 0, 'pending_files': 0,
              'note': 'Rejected or interrupted exports remain unresolved until corrected or restored under the same filename; missing ledgers are rebuilt by replaying retained exports.'}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock(directory, True):
        index = _load_index(directory)
        files = _current_files(inbox)
        if not (directory / 'outcome-ledger.json').exists():
            # Force replay of every retained export after ledger loss. Mark prior claims pending
            # so strict publication stays blocked until fair rotation rebuilds them all.
            for row in index['files'].values():
                if row['status'] == 'complete': row['status'] = 'pending'
        names = [p.name for p in files]
        cursor = index['cursor']
        order = [p for p in files if p.name > cursor] + [p for p in files if p.name <= cursor]
        for path in order[:MAX_FILES]:
            prior = index['files'].get(path.name)
            try:
                raw, digest, events, incomplete = _read_events(path)
                if len(raw) == 0: raise ValueError('empty event export')
                if prior and prior['sha256'] == digest and prior['status'] == 'complete':
                    result['duplicates'] += 1
                    index['cursor'] = path.name
                    continue
                counts = outcome_ledger.ingest(directory / 'outcome-ledger.json', events)
                result['applied'] += counts['applied']
                result['duplicates'] += counts['duplicates']
                status_value = 'pending' if incomplete else 'complete'
                index['files'][path.name] = {'sha256': digest, 'status': status_value}
                index['cursor'] = path.name
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                # Never follow or read a rejected source again here: it may be a FIFO,
                # symlink, or oversized file. A changed source will be retried next pass.
                digest = hashlib.sha256(('rejected:' + path.name).encode('utf-8')).hexdigest()
                index['files'][path.name] = {'sha256': digest, 'status': 'rejected'}
                index['cursor'] = path.name
        atomic_json(_index_path(directory), index)
        pending, rejected = _unresolved(directory, index, files)
        result['pending_files'] = pending
        result['rejected_files'] = rejected
    return result


def attach(document, directory, now=None):
    now=now or datetime.now(timezone.utc)
    path=Path(directory)/'outcome-ledger.json'
    for row in document['providers']:
        row['outcomes']={'available':False,'coverage':{'complete':False},
                         'note':'No validated-task source connected for this subscription.'}
        if not path.exists():continue
        try:
            summary=outcome_ledger.summarize_all(path,row['provider'],(now-timedelta(days=30)).isoformat(),now.isoformat())
            summary['available']=summary['counts']['tasks']>0
            row['outcomes']=summary
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            row['outcomes']['note']='Outcome evidence unavailable or invalid; not ranked.'
