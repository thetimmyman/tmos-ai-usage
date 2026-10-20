"""Read observed Pi user turns; emit counts only, never transcript content.

This module also says where Pi keeps its history and which entries a forked session copied, for
every reader of Pi sessions.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALIASES = {'commandcode': 'command-code', 'command-code': 'command-code',
           'clinepass': 'clinepass', 'opencode-go': 'opencode-go', 'openai-codex': 'codex'}
HEADER_MAX = 64 * 1024  # a session header is one small JSON line
LINEAGE_MAX = 16  # parentSession links followed from one session, at most


def _absolute(value):
    """`value` as an absolute path with `~` expanded, else None (relative, empty or unexpandable)."""
    try:
        candidate = Path(value).expanduser() if value else None
    except (RuntimeError, ValueError):
        return None
    return candidate if candidate is not None and candidate.is_absolute() else None


def agent_dir():
    """Pi's agent directory, and why: an absolute `PI_CODING_AGENT_DIR` (`~` expands), else `~/.pi/agent`.

    Pi resolves a relative value from the directory it was started in, which a collector cannot
    know, so a relative value falls back to the default. Setup installs the Pi integration here.
    """
    default = Path.home() / '.pi' / 'agent'
    configured = os.environ.get('PI_CODING_AGENT_DIR', '').strip()
    if not configured:
        return default, 'using the default Pi agent directory'
    candidate = _absolute(configured)
    if candidate is not None:
        return candidate, 'using configured absolute Pi agent directory'
    return default, 'PI_CODING_AGENT_DIR is not absolute; using the default Pi agent directory'


def sessions_dir():
    """Where Pi writes its sessions, in Pi's own order of precedence.

    `PI_CODING_AGENT_SESSION_DIR`, else the agent settings' `sessionDir`, else `sessions` in the
    agent directory. A relative value, a project's own `sessionDir` and a `--session-dir` flag all
    depend on where Pi was started, which is not knowable here, so they leave the default.
    """
    agent = agent_dir()[0]
    configured = os.environ.get('PI_CODING_AGENT_SESSION_DIR', '').strip()
    if not configured:
        try:
            settings = json.loads((agent / 'settings.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            settings = None
        value = settings.get('sessionDir') if isinstance(settings, dict) else None
        configured = value.strip() if isinstance(value, str) else ''
    return _absolute(configured) or agent / 'sessions'


def display(path):
    """`path` as a source label, written from `~` when it is under the home directory."""
    path = Path(path)
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return '~' if relative == Path('.') else '~/' + relative.as_posix()


def _resolved(path):
    try:
        return str(Path(path).resolve())
    except (OSError, RuntimeError, ValueError):
        return str(path)


class Lineage:
    """The entries each scanned session copied from a scanned ancestor, so each is counted once.

    `/fork`, `/clone` and `--fork` copy entries into the new session file with their IDs and
    timestamps, and the new header names the source file in `parentSession`. A copy is counted in
    the earliest scanned session that holds it: a session skips the entries of every scanned
    ancestor, and counts the copies whose ancestors are not scanned.
    """

    def __init__(self, files):
        self.files = {_resolved(path): path for path in files}
        names = {}
        for path in files:
            names.setdefault(path.name, []).append(path)
        # Pi names a session file after its time and session ID, so a moved directory still finds it.
        self.names = {name: paths[0] for name, paths in names.items() if len(paths) == 1}
        self._links = {}
        self._ids = {}

    def _link(self, path):
        key = _resolved(path)
        if key not in self._links:
            parent = None
            try:
                with open(path, 'rb') as stream:
                    line = stream.readline(HEADER_MAX + 1)
                row = json.loads(line) if len(line) <= HEADER_MAX else None
            except (OSError, ValueError, UnicodeDecodeError):
                row = None
            if isinstance(row, dict) and row.get('type') == 'session' and isinstance(row.get('parentSession'), str):
                parent = row['parentSession'].strip() or None
            self._links[key] = parent
        return self._links[key]

    def _locate(self, link):
        candidate = Path(link)
        if candidate.is_absolute():
            found = self.files.get(_resolved(candidate))
            if found is not None:
                return found
        found = self.names.get(candidate.name)
        if found is not None:
            return found
        try:
            return candidate if candidate.is_absolute() and candidate.is_file() else None
        except (OSError, ValueError):
            return None

    def ancestors(self, path):
        """The scanned sessions `path` descends from, nearest first."""
        found, seen, current = [], {_resolved(path)}, path
        for _ in range(LINEAGE_MAX):
            link = self._link(current)
            target = self._locate(link) if link else None
            if target is None:
                break
            key = _resolved(target)
            if key in seen:
                break
            seen.add(key)
            if key in self.files:
                found.append(self.files[key])
            current = target
        return found

    def ids(self, path):
        """The entry IDs a session file holds."""
        key = _resolved(path)
        if key not in self._ids:
            ids = set()
            try:
                with open(path, encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(row, dict) and row.get('type') != 'session' and isinstance(row.get('id'), str):
                            ids.add(row['id'])
            except OSError:
                pass
            self._ids[key] = frozenset(ids)
        return self._ids[key]

    def inherited(self, ancestors):
        """The entry IDs a session holds as copies of its scanned ancestors' entries."""
        found = set()
        for path in ancestors:
            found |= self.ids(path)
        return frozenset(found)


def scan(root=None, now=None):
    root = Path(root) if root is not None else sessions_dir()
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
    lineage = Lineage(files)
    for path in files:
        pending = []
        session = str(path.resolve())
        copied = lineage.inherited(lineage.ancestors(path))
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
                    if isinstance(row.get('id'), str) and row['id'] in copied: continue  # counted in the ancestor it was copied from
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
