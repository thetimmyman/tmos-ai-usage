"""Optional OpenCode Console CSV reports; workspace charges aren't Go plan expense."""
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import os
from pathlib import Path
import urllib.request
from billing_ledger import timestamp
from subscription_value import atomic_json

URL='https://opencode.ai/console/api/v1/usage/export?scope=organization&range=30d'
MAX_BYTES=20*1024*1024
TOKEN_FIELDS=('input_tokens','output_tokens','reasoning_tokens','cache_read_tokens','cache_write_5m_tokens','cache_write_1h_tokens')


def integer(value, optional=False):
    if optional and value=='':return None
    if not isinstance(value,str) or not value.isascii() or not value.isdigit():raise ValueError('invalid CSV count')
    return int(value)


def parse(raw, observed_at=None):
    if len(raw)>MAX_BYTES:raise ValueError('CSV exceeds safe report limit')
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    required={'id','provider','model','billing_source','cost_micro_cents','created_at',*TOKEN_FIELDS}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):raise ValueError('unsupported Console CSV schema')
    records={}
    for source in reader:
        if len(records)>=100000:raise ValueError('CSV record limit exceeded')
        if not source['id']:raise ValueError('missing record identity')
        row={'provider':source['provider'],'model':source['model'],'service':source.get('service') or 'inference',
             'funding':source['billing_source'],'at':timestamp(source['created_at']).isoformat(),
             'microcents':integer(source['cost_micro_cents']),
             **{k:integer(source[k],True) for k in TOKEN_FIELDS}}
        if source['id'] in records and records[source['id']]!=row:raise ValueError('conflicting CSV record')
        records[source['id']]=row
    rows=list(records.values())
    metrics={'Console records':len(rows),'Console billed USD (workspace)':float(Decimal(sum(r['microcents'] for r in rows))/Decimal(100000000))}
    for field in TOKEN_FIELDS:
        values=[r[field] for r in rows if r['service']=='inference']
        metrics['Console '+field.replace('_',' ')]=sum(values) if values and all(v is not None for v in values) else None
    dates=sorted(r['at'] for r in rows)
    return {'source':'OpenCode Console workspace CSV','observed_at':observed_at or datetime.now(timezone.utc).isoformat(),
            'source_sha256':hashlib.sha256(raw).hexdigest(),'metrics':metrics,
            'first_record_at':dates[0] if dates else None,'last_record_at':dates[-1] if dates else None,
            'note':'Workspace export snapshot, which may include other members, API products and web search. Console microcents converted at 100,000,000 per USD. Zero charges may include free, BYOK or unclassified usage. These are not Go subscription payments or validated tasks.'}


def fetch(directory):
    # Explicitly opt-in. Never reuse a personal Go/Zen key or a browser session token.
    key=os.environ.get('OPENCODE_CONSOLE_SERVICE_KEY')
    if not key:return None
    req=urllib.request.Request(URL,headers={'Authorization':'Bearer '+key,'Accept':'text/csv','User-Agent':'TMOS-AI-Usage/0.3'})
    with urllib.request.urlopen(req,timeout=15) as response:raw=response.read(MAX_BYTES+1)
    report=parse(raw)
    # Use the retrieval instant for snapshot ordering alongside manual exports.
    # Without this, an older CSV could overwrite a freshly fetched API report.
    report['source_modified_at']=timestamp(report['observed_at']).timestamp()
    atomic_json(Path(directory)/'console-summary.json',report)
    return report
