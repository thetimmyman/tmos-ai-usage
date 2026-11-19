"""Observed model outcomes. This projection cannot publish certified receipts."""
import calendar
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat

MIN_VALIDATED = 5
MAX_BYTES = 8 * 1024 * 1024
KINDS = {'validated': {'merged-and-reviewed', 'verified-close', 'audit-upheld'},
         'rejected': {'audit-reopened', 'review-blocked'}, 'abandoned': {None}, 'pending': {None}}


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if date.tzinfo is None:
        raise ValueError('timestamp needs offset')
    return date.astimezone(timezone.utc)


def monthly_fraction(start, end):
    fraction = 0
    while start < end:
        days = calendar.monthrange(start.year, start.month)[1]
        boundary = (start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=days))
        stop = min(end, boundary)
        fraction += (stop - start).total_seconds() / (days * 86400)
        start = stop
    return fraction


def read_snapshot(path):
    inbox = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(inbox)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError('untrusted inbox')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=inbox)
    finally:
        os.close(inbox)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('expected private owner-controlled snapshot')
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('snapshot too large')
    return json.loads(raw)


def project(data, providers, now):
    start, end = timestamp(data['start']), timestamp(data['end'])
    if type(data['version']) is not int or data['version'] != 1 or not start < end <= now or end - start > timedelta(days=31):
        raise ValueError('invalid interval')
    groups = {}
    for row in data['activity']:
        model, provider, turns = row['model'], row['provider'], row['turns']
        if (provider not in providers or not isinstance(model, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ./_:+-]{0,79}', model)
            or (provider, model) in groups
            or turns is not None and (type(turns) is not int or not 0 <= turns <= 10**9)):
            raise ValueError('invalid activity')
        groups[provider, model] = dict(model=model, turns=turns, sample=0, validated=0)
    seen = set()
    for event in data['events']:
        task = event['task_id']
        outcome = event['outcome']
        started = timestamp(event['started_at'])
        if (not re.fullmatch(r'[0-9a-f]{64}', task) or task in seen or started > end
                or event['validation_kind'] not in KINDS[outcome]):
            raise ValueError('invalid outcome')
        if outcome == 'pending':
            if event['finished_at'] is not None:
                raise ValueError('pending task has finish')
        else:
            finish = timestamp(event['finished_at'])
            if not max(start, started) <= finish <= end:
                raise ValueError('outcome outside interval')
        seen.add(task)
        group = groups[event['provider'], event['model']]
        group['sample'] += 1
        group['validated'] += int(outcome == 'validated')
    fraction = monthly_fraction(start, end)
    result = {}
    for provider, source in providers.items():
        rows = [r for (p, _), r in sorted(groups.items()) if p == provider]
        if not rows:
            continue
        turns = None if any(r['turns'] is None for r in rows) else sum(r['turns'] for r in rows)
        total = dict(turns=turns, sample=sum(r['sample'] for r in rows),
                     validated=sum(r['validated'] for r in rows))
        price = (source.get('activity') or {}).get('price') or {}
        fee = price.get('monthly_usd')
        spend = (fee * fraction if price.get('source_kind') in ('user', 'receipt')
                 and type(fee) in (float, int) and math.isfinite(fee) and fee >= 0 else None)
        for row in [total, *rows]:
            amount = spend
            if row is not total:
                amount = spend * row['turns'] / turns if spend is not None and turns else None
            count = row['validated']
            row.update(usd_per_validated=amount / count if amount is not None and count else None,
                       turns_per_validated=row['turns'] / count if row['turns'] is not None and count else None,
                       provisional=count < MIN_VALIDATED)
        result[provider] = dict(status='observed', start=data['start'], end=data['end'],
                                total=total, models=rows)
    return result


def attach(document, directory, now=None):
    providers = {r['provider']: r for r in document['providers']}
    try:
        data = read_snapshot(Path(directory) / 'outcome-events' / 'model-outcomes.json')
        result = project(data, providers, now or datetime.now(timezone.utc))
        missing = 'empty'
    except FileNotFoundError:
        result, missing = {}, 'missing'
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        result, missing = {}, 'invalid'
    for provider, row in providers.items():
        row['outcome_value'] = result.get(provider, {'status': missing})
