"""Bounded private Pi metadata writer. OS advisory locking survives process death."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from datetime import datetime

MAX_EVENTS = 5000
MAX_BYTES = 2 * 1024 * 1024
PROVIDERS = {'command-code', 'clinepass', 'opencode-go', 'codex'}


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_BYTES:
            raise ValueError('unsafe metadata file')
        data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES: raise ValueError('metadata too large')
        return data


def atomic(path, raw):
    if len(raw) > MAX_BYTES: raise ValueError('metadata too large')
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try: os.unlink(temp)
        except FileNotFoundError: pass


def write(directory, item):
    if (set(item) != {'task_id', 'event_id', 'provider', 'occurred_at', 'errors', 'assistant_error'}
            or not re.fullmatch(r'pi:[a-f0-9]{64}', item['task_id'])
            or not re.fullmatch(r'pi-turn:[a-f0-9]{64}', item['event_id'])
            or item['provider'] not in PROVIDERS or type(item['errors']) is not int or item['errors'] != 0
            or type(item['assistant_error']) is not bool):
        raise ValueError('invalid observation')
    at = datetime.fromisoformat(item['occurred_at'].replace('Z', '+00:00'))
    if at.tzinfo is None: raise ValueError('timezone required')
    directory = Path(directory)
    if not directory.is_absolute(): raise ValueError('absolute state path required')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077: raise ValueError('private directory required')
    filename = directory / (item['task_id'].replace(':', '-') + '.jsonl')
    fd = os.open(str(filename) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077: raise ValueError('unsafe lock')
        fcntl.flock(lock, fcntl.LOCK_EX)  # parent kills this helper after two seconds
        try: events = [json.loads(line) for line in read_private(filename).splitlines() if line.strip()]
        except FileNotFoundError: events = []
        if len(events) > MAX_EVENTS or any(e.get('task_id') != item['task_id'] for e in events):
            raise ValueError('invalid export')
        if item['assistant_error']:
            sidecar = Path(str(filename) + '.assistant-errors.json')
            try:
                value = json.loads(read_private(sidecar))
                ids = value['event_ids']
                if value['schema_version'] != 1 or value.get('kind') != 'pi_assistant_errors_not_verified_upstream' or not isinstance(ids, list) or len(ids) > MAX_EVENTS or any(not re.fullmatch(r'pi-turn:[a-f0-9]{64}', i) for i in ids):
                    raise ValueError('invalid error observations')
            except FileNotFoundError: ids = []
            if item['event_id'] not in ids:
                if len(ids) >= MAX_EVENTS: raise ValueError('error observation limit')
                ids.append(item['event_id'])
                atomic(sidecar, (json.dumps({'schema_version': 1, 'kind': 'pi_assistant_errors_not_verified_upstream', 'event_ids': ids}) + '\n').encode())
        if any(e.get('event_id') == item['event_id'] for e in events): return
        if len(events) >= MAX_EVENTS: raise ValueError('export limit')
        if not events:
            events.append({'type': 'task_registered', 'event_id': item['task_id'] + ':registered',
                'task_id': item['task_id'], 'provider': item['provider'], 'cohort': 'pi-observed-provider-session',
                'started_at': item['occurred_at']})
        if events[0]['type'] != 'task_registered' or events[0]['provider'] != item['provider'] or any(e['type'] != 'turns_recorded' for e in events[1:]):
            raise ValueError('invalid export')
        previous = events[-1].get('occurred_at', events[0]['started_at'])
        stamp = max(datetime.fromisoformat(previous.replace('Z', '+00:00')), at).isoformat().replace('+00:00', 'Z')
        events.append({'type': 'turns_recorded', 'event_id': item['event_id'], 'task_id': item['task_id'],
            'turns': 1, 'errors': 0, 'occurred_at': stamp})
        atomic(filename, ('\n'.join(json.dumps(e) for e in events) + '\n').encode())


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(4097)
        if len(raw) > 4096: raise ValueError('observation too large')
        write(sys.argv[1], json.loads(raw))
    except Exception:
        sys.exit(1)  # never print credentials, paths or provider text
