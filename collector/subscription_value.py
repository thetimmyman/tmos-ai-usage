"""Observed-activity comparisons and explicit user subscription prices.

These are provisional utilization scores, never validated-task economics.
"""
import argparse
import fcntl
import json
import math
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROVIDERS = ('claude-code', 'codex', 'clinepass', 'command-code', 'opencode-go')
# Dated published estimates, not billed amounts. Exact plan matching only.
REFERENCES = {
    ('claude-code', 'default_claude_max_20x'): (200, 'https://support.claude.com/en/articles/11049741-what-is-the-max-plan'),
    ('claude-code', 'default_claude_max_5x'): (100, 'https://support.claude.com/en/articles/11049741-what-is-the-max-plan'),
    ('command-code', 'GOAT'): (10, 'https://commandcode.ai/docs/resources/pricing-limits'),
    ('opencode-go', 'OpenCode Go'): (10, 'https://opencode.ai/go'),
}
REFERENCE_OBSERVED = '2026-09-24T18:20:00Z'


def amount(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('price must be a finite nonnegative USD amount')
    return value


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.subscription-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def save_price(directory, provider, price, cycle):
    if provider not in PROVIDERS or cycle not in ('month', 'year'):
        raise ValueError('unsupported provider or billing cycle')
    amount(price)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(directory / 'subscriptions.lock', os.O_CREAT | os.O_RDWR, 0o600), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'subscriptions.json'
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict): raise ValueError('invalid subscription settings')
        data[provider] = {'amount_usd': price, 'cycle': cycle,
                          'recorded_at': datetime.now(timezone.utc).isoformat()}
        atomic_json(path, data)


def price_for(row, configured, now):
    provider = row['provider']
    if provider in configured:
        p = configured[provider]
        total = amount(p['amount_usd'])
        if p['cycle'] not in ('month', 'year'): raise ValueError('invalid cycle')
        return {'monthly_usd': total / (12 if p['cycle'] == 'year' else 1),
                'amount_usd': total, 'cycle': p['cycle'], 'basis': 'user-entered subscription fee',
                'source': 'Local subscription settings', 'estimated': False}
    quote = row.get('subscription_quote')
    if provider == 'clinepass' and isinstance(quote, dict):
        total = amount(quote['amount_usd'])
        if quote['cycle'] not in ('month', 'year'): raise ValueError('invalid quote cycle')
        return {'monthly_usd': total / (12 if quote['cycle'] == 'year' else 1),
                'amount_usd': total, 'cycle': quote['cycle'], 'basis': 'provider plan quote — confirm actual fee',
                'source': 'Cline plan API (USD cents per seat)', 'estimated': True}
    ref = REFERENCES.get((provider, row.get('plan_tier') or row.get('plan')))
    observed = datetime.fromisoformat(REFERENCE_OBSERVED.replace('Z', '+00:00'))
    if ref and timedelta(0) <= now - observed <= timedelta(days=30):
        return {'monthly_usd': ref[0], 'amount_usd': ref[0], 'cycle': 'month',
                'basis': 'published price estimate — confirm actual fee', 'estimated': True,
                'source': ref[1], 'observed_at': REFERENCE_OBSERVED}
    return None


def attach(document, directory, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        configured = json.loads((Path(directory) / 'subscriptions.json').read_text())
        if not isinstance(configured, dict): configured = {}
    except (OSError, ValueError): configured = {}
    import pi_activity
    try: pi = pi_activity.scan(now=now)
    except OSError: pi = {}
    # Activity is the useful default while the validated evidence channel is empty.
    document['comparison_mode'] = 'validated' if document['providers'] and all(r.get('economics') for r in document['providers']) else 'activity'
    for row in document['providers']:
        try: price = price_for(row, configured, now)
        except (TypeError, ValueError, KeyError): price = None
        stats = row.get('stats') or {}
        total = stats.get('totals') or {}
        turns = total.get('prompts') if stats.get('available') is True else None
        if type(turns) is not int or turns < 0: turns = None
        pi_counts = pi.get(row['provider'], {})
        if pi_counts.get('turns'):
            turns = (turns or 0) + pi_counts['turns']
        row['activity'] = {'observed_turns': turns,
                           'pi_turns': pi_counts.get('turns', 0),
                           'cli_turns': total.get('prompts') if stats.get('available') else None, 'observed_sessions': (total.get('sessions') or 0) + pi_counts.get('sessions', 0) if stats.get('available') or pi_counts else None,
                           'window_days': stats.get('coverage_days') or (30 if pi_counts else None), 'price': price,
                           'source': str(stats.get('source') or '') + (' + ~/.pi/agent/sessions' if pi_counts.get('turns') else ''),
                           'first_date': total.get('first_date'), 'last_date': total.get('last_date'),
                           'reason': ('Set your subscription price in this provider tab.' if price is None else
                                      'No observed local turns available.' if turns is None else
                                      'Provisional: observed local turns / monthly subscription fee; incomplete harness coverage. Not task quality or cost per completed task.')}
        row['economics_reason'] = ('Validated outcome and recognized-spend receipts connected.' if row.get('economics') else
                                   'Validated task outcomes and matched recognized spend are not connected. Request success does not establish task validation.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--provider', choices=PROVIDERS, required=True)
    parser.add_argument('--amount', type=float, required=True)
    parser.add_argument('--cycle', choices=('month', 'year'), required=True)
    args = parser.parse_args()
    try: save_price(args.state_dir, args.provider, args.amount, args.cycle)
    except (OSError, ValueError, TypeError) as error: parser.exit(2, str(error) + '\n')
