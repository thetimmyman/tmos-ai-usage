"""Small allowlisted reporting projections; never treat API completions as tasks."""
import hashlib
import re
import json
import math
from pathlib import Path
from urllib.parse import urlencode, quote


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def unwrap(value):
    return value.get('data', value) if isinstance(value, dict) else {}


def command_report(c, key, base, subscription):
    sub = unwrap(subscription)
    since = sub.get('currentPeriodStart')
    if not since:
        return {'note': 'Provider did not return a billing-period start.'}
    who = unwrap(c.http_json(base + '/alpha/whoami?limits=1', key))
    params = {'since': since}
    org = who.get('org') or {}
    if org.get('id'):
        params['orgId'] = org['id']
    data = unwrap(c.http_json(base + '/alpha/usage/summary?' + urlencode(params), key))
    return {'source': 'Command Code usage summary', 'period_start': since,
            'observed_at': c._iso(c._now()), 'note': 'Billing-period request summary. Completed requests are not validated tasks.',
            'metrics': {label: numeric(data.get(field)) for label, field in {
                'Requests': 'totalCount', 'Successful requests': 'completedCount',
                'Failed requests': 'failedCount', 'Input tokens': 'totalTokensIn',
                'Output tokens': 'totalTokensOut', 'Credits consumed': 'totalCredits'}.items()}}


def cline_report(c, key, base):
    me = unwrap(c.http_json(base + '/api/v1/users/me', key))
    user = me.get('id')
    if not user:
        return {'note': 'Provider did not return an account ID.'}
    data = unwrap(c.http_json(base + '/api/v1/users/' + quote(str(user), safe='') + '/usages?limit=100', key))
    items = data.get('items') or []
    metrics = {'Observed billing records': len(items)}
    for field, label in [('promptTokens', 'Input tokens'), ('completionTokens', 'Output tokens'), ('cachedTokens', 'Cached tokens'), ('costUsd', 'Reported cost (unverified units)')]:
        values = [numeric(i.get(field)) for i in items]
        metrics[label] = sum(values) if values and all(v is not None for v in values) else None
    return {'source': 'Cline usage API', 'observed_at': c._iso(c._now()), 'metrics': metrics,
            'note': 'Latest 100 billing records (bounded sample). Provider cost units are unverified and not subscription spend. Task outcomes unavailable.'}


def attach_local(document, directory):
    """Private optional projections. Missing/malformed evidence leaves rankings unavailable."""
    directory = Path(directory)
    try:
        offers = json.loads(Path(__file__).with_name('offer-observations.json').read_text())
        for row in document['providers']:
            row['offers'] = [{**o, 'observed_at': offers['observed_at'], 'fresh_for_seconds': offers['fresh_for_seconds']}
                             for o in offers['offers'] if o['provider'] == row['provider']]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        evidence = json.loads((directory / 'economics.json').read_text())
        context = evidence['context']
        context = {k: context[k] for k in ('period', 'cohort')}
        if not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', v) for v in context.values()):
            raise ValueError('invalid comparison context')
        document['economics_context'] = context
        for row in document['providers']:
            refs = evidence.get('providers', {}).get(row['provider'], {})
            try:
                row['economics'] = economics_projection(directory, refs, context, row['provider'])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                row['economics'] = None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    for row in document['providers']:
        if row['provider'] != 'opencode-go':
            continue
        try:
            report = json.loads((directory / 'request-summary.json').read_text())
            t = report['totals']
            row['report'] = {'source': 'Imported OpenCode request log', 'note': report['note'] + ' Imported sample; incomplete period.',
                'metrics': {'Requests': t['requests'], 'Successful requests': t['successful_requests'],
                'Attempts': t['observed_attempts'], 'Retry attempts': t['observed_retry_attempts'],
                'Upstream HTTP errors': t['attempt_http_errors'], 'Rate limits': t['attempt_rate_limits'],
                'Server errors': t['attempt_server_errors'], 'Latency p50 (ms)': t['duration_ms']['p50'],
                'Latency p95 (ms)': t['duration_ms']['p95']}, 'coverage': {k: report['coverage'].get(k) for k in ('contains_truncated_export', 'first_started_at_ms', 'last_started_at_ms')},
                'models': [{'name': str(m['model'])[:120], 'requests': numeric(m['requests']),
                            'input': numeric(m['tokens']['inputTokens']['sum']),
                            'output': numeric(m['tokens']['outputTokens']['sum']),
                            'cache_read': numeric(m['tokens']['cacheReadTokens']['sum'])}
                           for m in report.get('by_model', [])]}
            for field, label in [('inputTokens', 'Input tokens'), ('outputTokens', 'Output tokens'), ('cacheReadTokens', 'Cache-read tokens'), ('reasoningTokens', 'Reasoning tokens')]:
                row['report']['metrics'][label] = numeric(t['tokens'][field]['sum'])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    import subscription_value
    subscription_value.attach(document, directory)


def economics_projection(directory, refs, context, provider):
    """Derive scores from content-addressed receipts, never editable aggregate counters.

    The task producer is responsible for truthful validation and declaring its full
    comparison population. Hashes establish snapshot integrity, not task quality.
    """
    receipts = {}
    for kind in ('outcomes', 'spend'):
        digest = refs[kind + '_sha256']
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('invalid evidence reference')
        raw = (directory / 'evidence' / (digest + '.json')).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('evidence changed')
        receipt = json.loads(raw)
        if any(receipt.get(k) != v for k, v in context.items()) or receipt.get('provider') != provider:
            raise ValueError('evidence scope mismatch')
        if receipt.get('coverage_complete') is not True:
            raise ValueError('incomplete evidence')
        receipts[kind] = receipt
    outcome = receipts['outcomes']
    tasks = outcome['tasks']
    expected = outcome['population_task_ids']
    if not isinstance(tasks, list) or not isinstance(expected, list):
        raise ValueError('invalid population')
    ids = [t['id'] for t in tasks]
    if len(ids) != len(set(ids)) or len(expected) != len(set(expected)) or set(ids) != set(expected):
        raise ValueError('incomplete or duplicated task population')
    validated = rework = turns = 0
    for task in tasks:
        if task['status'] not in ('validated', 'failed', 'abandoned'):
            raise ValueError('unfinished task population')
        if not re.fullmatch(r'[0-9a-f]{64}', task['evidence_sha256']):
            raise ValueError('missing validation evidence reference')
        if type(task['turns']) is not int or task['turns'] < 0 or type(task['reworked']) is not bool:
            raise ValueError('invalid task metrics')
        validated += task['status'] == 'validated'
        rework += task['reworked']
        turns += task['turns']
    spend = receipts['spend']
    if spend['basis'] != 'recognized_subscription_and_metered_usd':
        raise ValueError('incomparable spend basis')
    charges = spend['charges']
    if not isinstance(charges, list) or not charges:
        raise ValueError('missing spend attestation')
    charge_ids = [c['id'] for c in charges]
    if len(charge_ids) != len(set(charge_ids)):
        raise ValueError('duplicate spend receipts')
    for charge in charges:
        if numeric(charge['recognized_usd']) is None or not re.fullmatch(r'[0-9a-f]{64}', charge['evidence_sha256']):
            raise ValueError('invalid spend evidence')
    total = sum(c['recognized_usd'] for c in charges)
    if numeric(total) is None:
        raise ValueError('non-finite spend')
    return {**context, 'coverage_complete': True, 'evidence_verified': True,
            'outcomes_sha256': refs['outcomes_sha256'], 'spend_sha256': refs['spend_sha256'],
            'validated_tasks': validated, 'reworked_tasks': rework, 'turns': turns,
            'failed_tasks': sum(t['status'] == 'failed' for t in tasks),
            'spend_basis': spend['basis'], 'spend_usd': total}
