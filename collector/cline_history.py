"""Private, resumable Cline billing-history capture; request records are not tasks."""
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import stat
from urllib.parse import quote, urlencode

from subscription_value import atomic_json

MAX_RECORDS = 100_000
MAX_BYTES = 32 * 1024 * 1024
FIELDS = ('promptTokens', 'completionTokens', 'cachedTokens', 'costUsd')


def collect(client, key, base, user, max_pages, directory):
    from inference_reports import numeric, unwrap
    # Scope by authenticated account and endpoint, without retaining either or its key.
    scope = hashlib.sha256(json.dumps([base, str(user)]).encode()).hexdigest()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ('cline-history-' + scope + '.json')
    with os.fdopen(os.open(directory / 'cline-history.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = {'schema': 1, 'records': {}, 'cursor': None, 'endpoint_exhausted': False}
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('History must be a regular file')
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('History exceeds storage bound')
            state = json.loads(raw)
            if (not isinstance(state, dict) or state.get('schema') != 1
                    or not isinstance(state.get('records'), dict)
                    or len(state['records']) > MAX_RECORDS
                    or any(not isinstance(v, dict) for v in state['records'].values())
                    or state.get('cursor') is not None and (not isinstance(state['cursor'], str) or len(state['cursor']) > 8192)):
                raise ValueError('Invalid history checkpoint')
            for identity, item in state['records'].items():
                if (not re.fullmatch(r'[0-9a-f]{64}', identity)
                        or set(item) != set(FIELDS) | {'aiModelName'}
                        or not isinstance(item['aiModelName'], str) or len(item['aiModelName']) > 120
                        or any(item[field] is not None and numeric(item[field]) is None for field in FIELDS)):
                    raise ValueError('Invalid history record')
        records = state['records']
        cursor = state['cursor']
        cursors = {cursor} if cursor else set()
        pages = 0
        exhausted = False
        reason = 'History backfill continues on the next refresh.'
        for _ in range(max_pages):
            params = {'limit': 100}
            if cursor:
                params['cursor'] = cursor
            try:
                data = unwrap(client.http_json(base + '/api/v1/users/' + quote(str(user), safe='') + '/usages?' + urlencode(params), key))
                page, next_cursor = data.get('items'), data.get('nextToken')
                if (not isinstance(page, list) or len(page) > 100
                        or any(not isinstance(i, dict) or not isinstance(i.get('id'), str) or not i['id'] for i in page)
                        or next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 8192)):
                    raise ValueError('Invalid history page')
                incoming = {}
                for item in page:
                    identity = hashlib.sha256(item['id'].encode()).hexdigest()
                    incoming[identity] = {**{field: numeric(item.get(field)) for field in FIELDS},
                                          'aiModelName': str(item.get('aiModelName') or 'Unknown model')[:120]}
                if len(records.keys() | incoming.keys()) > MAX_RECORDS:
                    reason = 'Local history storage limit reached; partial history retained.'
                    break
            except Exception:
                reason = 'History page unavailable; retained records remain available. Backfill retries next refresh.'
                break
            pages += 1
            merged = {**records, **incoming}
            # Bound serialized bytes as well as record count (model names may be Unicode).
            if len(json.dumps({**state, 'records': merged, 'cursor': next_cursor}).encode()) > MAX_BYTES - 1024:
                reason = 'Local history storage limit reached; partial history retained.'
                break
            records.update(incoming)
            if not next_cursor:
                exhausted = True
                cursor = None
                reason = 'Reached the end of this provider history walk. The next refresh starts a new walk for updates.'
                break
            if next_cursor in cursors:
                # A bad/expired cursor must not trap all future refreshes.
                cursor = None
                reason = 'Repeated provider cursor; retained records preserved and next refresh restarts the walk.'
                break
            cursors.add(next_cursor)
            cursor = next_cursor
        state.update(cursor=cursor, endpoint_exhausted=exhausted,
                     observed_at=client._iso(client._now()))
        atomic_json(path, state)
        return list(records.values()), pages, exhausted, reason
