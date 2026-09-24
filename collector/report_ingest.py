"""Bounded automatic import from the user's explicit private export inbox."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from request_log_import import import_file, summarize
from subscription_value import atomic_json

MAX_FILES = 20
MAX_BYTES = 20 * 1024 * 1024


def ingest(directory, inbox=None):
    directory = Path(directory)
    inbox = Path(inbox) if inbox else directory / 'imports'
    result = {'imported': 0, 'unchanged': 0, 'rejected': 0, 'deferred': 0,
              'note': 'OpenCode request-log JSON and Console CSV inbox. Originals are retained; other schemas need an adapter.'}
    if not inbox.exists():
        return result
    if inbox.is_symlink() or not inbox.is_dir():
        return {**result, 'rejected': 1, 'note': 'Import inbox must be a real directory.'}
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(directory / 'report-ingest.lock', os.O_CREAT | os.O_RDWR, 0o600), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index_path = directory / 'import-index.json'
        ledger_path = directory / 'request-ledger.json'
        summary_path = directory / 'request-summary.json'
        state = json.loads(index_path.read_text()) if index_path.exists() else {'sources':{}, 'cursor':''}
        if not isinstance(state, dict):
            raise ValueError('Invalid import index')
        index = state.get('sources', {})
        cursor = state.get('cursor', '')
        if not isinstance(cursor, str):
            raise ValueError('Invalid import cursor')
        if not isinstance(index, dict) or any(not isinstance(k, str) or len(k) != 64 or v not in ('json', 'csv', True) for k,v in index.items()):
            raise ValueError('Invalid import index')
        if ledger_path.exists():
            # Repair summary even after interruption between ledger and summary publication.
            atomic_json(summary_path, summarize(json.loads(ledger_path.read_text())))
        elif index:
            index = {k:v for k,v in index.items() if v == 'csv'}  # Missing ledger must be rebuilt from retained source files.
        if not (directory / 'console-summary.json').exists():
            index = {k:v for k,v in index.items() if v != 'csv'}
        entries = sorted((p for p in inbox.iterdir() if p.suffix.lower() in ('.json', '.csv')), key=lambda p:p.name)
        # Rotate bounded batches; unchanged/malformed exports cannot starve later files.
        entries = [p for p in entries if p.name > cursor] + [p for p in entries if p.name <= cursor]
        result['deferred'] = max(0, len(entries) - MAX_FILES)
        for p in entries[:MAX_FILES]:
            temp = None
            try:
                if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_BYTES:
                    result['rejected'] += 1
                    continue
                fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError('not regular')
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('oversized export')
                digest = hashlib.sha256(raw).hexdigest()
                if digest in index:
                    result['unchanged'] += 1
                    continue
                if p.suffix.lower() == '.csv':
                    import console_report
                    report = console_report.parse(raw)
                    # Files copied from machines with clock skew must not claim a
                    # snapshot from the future over a verified recent API fetch.
                    modified_at = p.stat().st_mtime
                    if modified_at > time.time() + 300:
                        raise ValueError('export modification time is in the future')
                    report['source_modified_at'] = min(modified_at, time.time())
                    console_path = directory / 'console-summary.json'
                    prior = json.loads(console_path.read_text()) if console_path.exists() else {}
                    if report['source_modified_at'] >= prior.get('source_modified_at', 0):
                        atomic_json(console_path, report)
                    index[digest] = 'csv'
                    atomic_json(index_path, {'sources':index, 'cursor':p.name})
                    result['imported'] += 1
                    continue
                fd, temp = tempfile.mkstemp(prefix='.inbox-', dir=directory)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                report = import_file(Path(temp), ledger_path)
                atomic_json(summary_path, report)
                index[digest] = 'json'
                atomic_json(index_path, {'sources':index, 'cursor':p.name})
                result['imported'] += 1
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                result['rejected'] += 1
            finally:
                if temp is not None:
                    Path(temp).unlink(missing_ok=True)
                atomic_json(index_path, {'sources':index, 'cursor':p.name})
    return result
