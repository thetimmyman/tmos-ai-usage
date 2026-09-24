"""Read observed Pi user turns; emit counts only, never transcript content."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALIASES = {'commandcode': 'command-code', 'command-code': 'command-code',
           'clinepass': 'clinepass', 'opencode-go': 'opencode-go', 'openai-codex': 'codex'}


def scan(root=None, now=None):
    root = Path(root) if root is not None else Path.home() / '.pi/agent/sessions'
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    result = {}
    seen = set()
    if not root.exists(): return result
    candidates = []
    for path in root.rglob('*.jsonl'):
        try: candidates.append((path.stat().st_mtime, path))
        except OSError: continue
    files = [p for _,p in sorted(candidates, reverse=True)[:600]]
    for path in files:
        pending = []
        session = str(path.resolve())
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for index,line in enumerate(stream):
                    try: row = json.loads(line)
                    except ValueError: continue
                    if not isinstance(row,dict): continue
                    if row.get('type') == 'session':
                        session = str(row.get('id') or session)
                        continue
                    m = row.get('message')
                    if row.get('type') != 'message' or not isinstance(m,dict): continue
                    raw = row.get('timestamp') or m.get('timestamp')
                    try:
                        when = datetime.fromtimestamp(raw / 1000,timezone.utc) if type(raw) in (int,float) else datetime.fromisoformat(str(raw).replace('Z','+00:00'))
                        if when.tzinfo is None: continue
                    except (ValueError,TypeError,OverflowError,OSError): continue
                    if m.get('role') == 'user':
                        if cutoff <= when <= now:
                            pending.append((session,str(row.get('id') or index)))
                    elif m.get('role') == 'assistant' and pending:
                        raw_provider = m.get('provider')
                        provider = ALIASES.get(raw_provider) if isinstance(raw_provider, str) else None
                        # Attribution is the first assistant provider after the user turn.
                        # Unknown/API providers are deliberately not charged to subscriptions.
                        if provider:
                            bucket = result.setdefault(provider, {'turns':0,'sessions':set()})
                            for key in pending:
                                if key not in seen:
                                    seen.add(key);bucket['turns'] += 1;bucket['sessions'].add(session)
                        pending = []
        except OSError:
            continue
    return {k:{'turns':v['turns'],'sessions':len(v['sessions'])} for k,v in result.items()}
