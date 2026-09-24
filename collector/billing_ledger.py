"""Private invoice ledger. Cash paid and service-period expense remain separate."""
import argparse
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from subscription_value import atomic_json

MAX_EVIDENCE_BYTES = 20 * 1024 * 1024


def read_evidence(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('invoice evidence must be a regular file')
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if not raw or len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError('invoice evidence must be nonempty and at most 20 MiB')
    return raw


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp must include timezone')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return result.astimezone(timezone.utc)


def money(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('invalid USD amount')
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid USD amount') from exc
    if not d.is_finite() or d < 0 or d > Decimal('1000000000') or d != d.quantize(Decimal('.01')):
        raise ValueError('USD must be finite, nonnegative, and exact cents')
    return int(d * 100)


def normalize(record):
    if not isinstance(record, dict):
        raise ValueError('expected invoice object')
    for k in ('id', 'provider'):
        if not isinstance(record.get(k), str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', record[k]):
            raise ValueError('invalid ' + k)
    if record.get('currency') != 'USD':
        raise ValueError('only documented USD receipts supported')
    paid_at = timestamp(record['paid_at']).isoformat()
    amounts = {k: money(record[k]) for k in ('base_usd', 'discount_usd', 'tax_usd', 'paid_usd')}
    amounts['fees_usd'] = money(record.get('fees_usd', 0))
    if amounts['discount_usd'] > amounts['base_usd']:
        raise ValueError('discount exceeds invoice base')
    if amounts['base_usd'] - amounts['discount_usd'] + amounts['tax_usd'] + amounts['fees_usd'] != amounts['paid_usd']:
        raise ValueError('invoice amounts do not reconcile')
    start, end = record.get('service_start'), record.get('service_end')
    if any(v is not None and not isinstance(v, str) for v in (start, end)):
        raise ValueError('invalid service endpoint')
    if bool(start) != bool(end):
        raise ValueError('service interval requires both endpoints')
    precision = record.get('service_precision', 'timestamp')
    if precision not in ('timestamp', 'calendar_date_utc'):
        raise ValueError('invalid service precision')
    if start and precision == 'calendar_date_utc':
        start = start + 'T00:00:00Z' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', start) else start
        end = end + 'T00:00:00Z' if re.fullmatch(r'\d{4}-\d{2}-\d{2}', end) else end
    if start:
        start, end = timestamp(start), timestamp(end)
        if start >= end:
            raise ValueError('invalid service interval')
        start, end = start.isoformat(), end.isoformat()
    source = record.get('source_ref')
    if not isinstance(source, str) or not source.strip() or len(source) > 2000:
        raise ValueError('source reference required')
    digest = record.get('source_sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('source evidence digest required')
    return {'id': record['id'], 'provider': record['provider'], 'currency': 'USD',
            'paid_at': paid_at, **{k: v / 100 for k, v in amounts.items()},
            'service_start': start or None, 'service_end': end or None, 'service_precision': precision,
            'source_ref': source, 'source_sha256': digest}


def import_invoice(directory, record, evidence_path):
    """Content-address original evidence; idempotent import rejects conflicting IDs."""
    if not isinstance(record, dict):
        raise ValueError('expected invoice object')
    raw = read_evidence(evidence_path)
    digest = hashlib.sha256(raw).hexdigest()
    if record.get('source_sha256') not in (None, digest):
        raise ValueError('source evidence digest mismatch')
    row = normalize({**record, 'source_sha256': digest})
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(directory / 'billing.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'billing-ledger.json'
        if path.is_symlink():
            raise ValueError('billing ledger must not be a symlink')
        ledger = json.loads(path.read_text()) if path.exists() else {'version': 1, 'invoices': []}
        if not isinstance(ledger, dict) or ledger.get('version') != 1 or not isinstance(ledger.get('invoices'), list):
            raise ValueError('invalid billing ledger')
        if any(not isinstance(r, dict) for r in ledger['invoices']):
            raise ValueError('invalid invoice row')
        old = next((r for r in ledger['invoices'] if (r['provider'], r['id']) == (row['provider'], row['id'])), None)
        if old:
            evidence_file = directory / 'billing-evidence' / old['source_sha256']
            if hashlib.sha256(read_evidence(evidence_file)).hexdigest() != old['source_sha256']:
                raise ValueError('stored billing evidence changed')
            if normalize(old) != row:
                raise ValueError('conflicting invoice ID; preserve original evidence')
            return False
        if any(r.get('source_sha256') == digest for r in ledger['invoices']):
            raise ValueError('evidence already attributed to another invoice')
        evidence = directory / 'billing-evidence'
        if evidence.is_symlink():
            raise ValueError('billing evidence directory must not be a symlink')
        evidence.mkdir(mode=0o700, exist_ok=True)
        os.chmod(evidence, 0o700)
        target = evidence / digest
        if target.is_symlink():
            raise ValueError('billing evidence must not be a symlink')
        if target.exists():
            if hashlib.sha256(read_evidence(target)).hexdigest() != digest:
                raise ValueError('stored billing evidence changed')
        else:
            # Immutable evidence appears before the ledger references it.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        ledger['invoices'].append(row)
        atomic_json(path, ledger)
    return True


def summary(directory, provider, start, end):
    """Allowlisted projection. Never infer complete coverage from a few invoices."""
    start, end = timestamp(start), timestamp(end)
    if start >= end:
        raise ValueError('invalid report interval')
    directory = Path(directory)
    try:
        ledger = json.loads((directory / 'billing-ledger.json').read_text())
    except FileNotFoundError:
        ledger = {'version': 1, 'invoices': []}
    if not isinstance(ledger, dict) or ledger.get('version') != 1 or not isinstance(ledger.get('invoices'), list):
        raise ValueError('invalid billing ledger')
    rows, seen, evidence_seen = [], set(), set()
    cash = 0
    expense = Decimal(0)
    unknown = 0
    known = 0
    for source in ledger['invoices']:
        if not isinstance(source, dict):
            raise ValueError('invalid invoice row')
        if source.get('provider') != provider:
            continue
        row = normalize(source)
        if row['id'] in seen:
            raise ValueError('duplicate invoice')
        seen.add(row['id'])
        if row['source_sha256'] in evidence_seen:
            raise ValueError('duplicate invoice evidence')
        evidence_seen.add(row['source_sha256'])
        evidence_file = directory / 'billing-evidence' / row['source_sha256']
        if evidence_file.is_symlink() or not stat.S_ISREG(evidence_file.stat().st_mode) or evidence_file.stat().st_mode & 0o077:
            raise ValueError('billing evidence must be private regular file')
        raw = read_evidence(evidence_file)
        if hashlib.sha256(raw).hexdigest() != row['source_sha256']:
            raise ValueError('billing evidence changed')
        payment_in_period = start <= timestamp(row['paid_at']) < end
        service_overlap = False
        allocation = None
        if row['service_start']:
            a, b = timestamp(row['service_start']), timestamp(row['service_end'])
            overlap = max(timedelta(0), min(end, b) - max(start, a))
            service_overlap = overlap > timedelta(0)
            allocation = Decimal(money(row['paid_usd'])) * Decimal(str(overlap.total_seconds())) / Decimal(str((b-a).total_seconds())) / 100
            expense += allocation
            known += payment_in_period or service_overlap
        elif timestamp(row['paid_at']) < end:
            unknown += 1
        if payment_in_period:
            cash += money(row['paid_usd'])
        if payment_in_period or service_overlap:
            rows.append({k: row[k] for k in ('paid_at', 'base_usd', 'discount_usd', 'tax_usd', 'fees_usd', 'paid_usd', 'service_start', 'service_end', 'service_precision')})
    return {'period_start': start.isoformat(), 'period_end': end.isoformat(),
            'cash_paid_usd': cash / 100 if rows else None,
            'allocated_known_usd': float(expense.quantize(Decimal('.000001'), rounding=ROUND_HALF_UP)) if known else None,
            'unknown_service_periods': unknown, 'coverage_complete': False,
            'invoices': sorted(rows, key=lambda r: r['paid_at'], reverse=True),
            'note': 'Observed invoices only. Payments include tax, fees and discounts. Known service periods are allocated by elapsed time; date-only invoice boundaries use midnight UTC as an accounting convention; missing invoices and service dates prevent complete period cost.'}


def attach(document, directory, now=None):
    now = now or datetime.now(timezone.utc)
    for row in document['providers']:
        try:
            row['billing'] = summary(directory, row['provider'], (now-timedelta(days=30)).isoformat(), now.isoformat())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            row['billing'] = {'coverage_complete': False, 'invoices': [], 'note': 'Billing evidence unavailable or invalid. No complete period cost.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/tmos-ai-usage')
    parser.add_argument('--invoice', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    try:
        changed = import_invoice(args.state_dir, json.loads(args.invoice.read_text()), args.evidence)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, 'Invoice import failed: ' + str(exc) + '\n')
    print('Imported invoice' if changed else 'Invoice already imported')


if __name__ == '__main__':
    main()
