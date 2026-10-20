"""Read-only official Antigravity CLI meters and local transcript metadata.

The CLI owns authentication. No OAuth tokens, prompts, or response text are exported.
"""
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path


MIN_VERSION = (1, 3, 5)
MAX_OUTPUT = 256 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_TRANSCRIPT = 32 * 1024 * 1024


class CliError(ValueError):
    """A safe reason code, never a CLI error body or credential."""


def _run(args, timeout):
    # File-backed output keeps subprocess diagnostics out of logs and memory bounded.
    # agy's self-updater runs in the background of any invocation. A read-only monitor must never
    # let a scheduled quota check replace the installed CLI, so the child runs with it switched off.
    env = {**os.environ, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=diagnostic, timeout=timeout, check=False, env=env)
        except subprocess.TimeoutExpired as exc:
            raise CliError("timeout") from exc
        except OSError as exc:
            raise CliError("unavailable") from exc
        output.seek(0)
        raw = output.read(MAX_OUTPUT + 1)
        if len(raw) > MAX_OUTPUT:
            raise CliError("oversized")
        if result.returncode:
            diagnostic.seek(0)
            error = diagnostic.read(MAX_OUTPUT).lower()
            if any(word in error for word in (b"authentication required", b"unauthenticated", b"sign in", b"login required")):
                raise CliError("unauthenticated")
            if b"flag provided but not defined" in error and b"log-file" in error:
                raise CliError("no_log_file")
        return result.returncode, raw


LOG_NAME = "agy-cli.log"


def _emptied(path):
    """`path`, created private or emptied for the next run, or None when it cannot be."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600))
    except (OSError, ValueError):
        return None
    return path


@contextlib.contextmanager
def _log_file(path):
    """The one log file a quota check writes: `path`, else a private directory removed after it.

    Without `--log-file`, agy starts a new timestamped log in its own folder on every run (about
    20 KB), so a refresh every five minutes would leave about 290 files a day. agy rewrites the
    file it is given; emptying it first keeps it one run long even if a release appended to it.
    """
    kept = _emptied(path) if path is not None else None
    if kept is not None:
        yield kept
        return
    with tempfile.TemporaryDirectory(prefix="tmos-agy-") as scratch:
        yield Path(scratch) / LOG_NAME


def cli_json(log_file=None):
    binary = os.environ.get("TMOS_AGY_BIN") or shutil.which("agy")
    if not binary:
        fallback = Path.home() / ".local/bin/agy"
        if fallback.is_file():
            binary = str(fallback)
    if not binary:
        raise CliError("unavailable")
    code, raw = _run([binary, "--version"], 3)
    version = re.fullmatch(rb"\s*(\d+)\.(\d+)\.(\d+)\s*", raw)
    if code or not version or tuple(int(v) for v in version.groups()) < MIN_VERSION:
        # Older releases can treat a slash command as a model prompt. Never try it.
        raise CliError("old_version")
    usage = [binary, "-p", "/usage", "--output-format", "json", "--print-timeout", "15s"]
    with _log_file(log_file) as log:
        # A release without `--log-file` raises `no_log_file` and is not run again without it: every
        # check would leave another log in agy's own folder, with nothing to bound them.
        code, raw = _run(usage + ["--log-file", str(log)], 18)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CliError("failed" if code else "invalid_json") from exc
    if not isinstance(payload, dict):
        raise CliError("invalid_json")
    if code or payload.get("status") != "SUCCESS":
        error = str(payload.get("error", "")).lower()
        if any(word in error for word in ("authentication", "sign in", "unauthenticated", "login")):
            raise CliError("unauthenticated")
        raise CliError("failed")
    return payload


def parse_usage(c, payload, now=None):
    now = now or c._now()
    command = payload.get("command") if isinstance(payload, dict) else None
    data = command.get("data") if isinstance(command, dict) and command.get("name") == "usage" else None
    groups = data.get("groups") if isinstance(data, dict) else None
    parsed = []
    if isinstance(groups, list) and payload.get("status") == "SUCCESS":
        for group in groups[:32]:
            if not isinstance(group, dict) or not isinstance(group.get("name"), str):
                continue
            windows = {}
            buckets = group.get("buckets")
            for bucket in (buckets[:32] if isinstance(buckets, list) else []):
                if not isinstance(bucket, dict):
                    continue
                name = {"weekly": "week", "5h": "5h"}.get(bucket.get("window"))
                fraction = bucket.get("remaining_fraction")
                if not name or type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
                    continue
                windows[name] = c.window(name, (1 - fraction) * 100,
                                         c._parse_iso(bucket.get("reset_time")), now)
            if windows:
                parsed.append({"name": c._scrub(group["name"]), "windows":
                               sorted(windows.values(), key=lambda w: c.WINDOW_ORDER[w["name"]])})
    if not parsed:
        return c.unknown("antigravity", "Official agy /usage returned no readable quota groups", source="cli")
    gemini = next((g for g in parsed if g["name"] == "Gemini Models"), None)
    result = c.record("antigravity", status="ok", source="cli", observed_at=now,
                      windows=gemini["windows"] if gemini else [],
                      note="Meters show Gemini quota; other model groups have separate allowances. Subscription tier is not reported by agy.")
    result["quota_groups"] = parsed
    return result


def collect(c):
    settings = c.read_json(Path.home() / ".gemini/antigravity-cli/settings.json")
    if isinstance(settings, dict) and settings.get("modelProvider") == "gemini":
        return c.unknown("antigravity", "agy is configured for Gemini API billing; use its Google subscription sign-in for quota", source="cli")
    try:
        return parse_usage(c, cli_json(c.STATE_DIR / LOG_NAME))
    except CliError as exc:
        reasons = {
            "unavailable": "Install the official Antigravity CLI (agy) to read its quota",
            "old_version": "Update agy to 1.3.5 or newer for read-only JSON quota checks",
            "no_log_file": "Update agy to read its quota: this release has no --log-file, so every check would leave another log file",
            "unauthenticated": "Run agy interactively and sign in with your Google subscription account",
            "timeout": "Official agy quota check timed out; retry after checking agy",
        }
        return c.unknown("antigravity", reasons.get(str(exc), "Official agy quota check failed; check agy interactively"),
                         source="cli", status="unauthenticated" if str(exc) == "unauthenticated" else "unknown")


# A conversation keeps two exports of the same numbered steps. Neither is canonical: either can be
# truncated or left behind at a checkpoint, so the reader merges them by step number.
TRANSCRIPTS = ("transcript_full.jsonl", "transcript.jsonl")


def _exports(logs):
    """The conversation's exports that exist, the full export first."""
    return [logs / name for name in TRANSCRIPTS if (logs / name).is_file()]


def _is_anchor(path):
    """Whether `path` stands for its conversation in the scan: its most recently written export.

    One file per conversation enters the scan, before the file cap applies, so duplicate exports
    never take a place. The newest export carries the conversation's last activity, which is what
    the 30-day window has to see. On a tie, the full export stands.
    """
    if path.name not in TRANSCRIPTS:
        return False
    newest = None
    for export in _exports(path.parent):
        try:
            mtime = export.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, export)
    return newest is not None and newest[1] == path


def _merge(exports, parse):
    """One conversation's steps from its exports' steps, each step counted once.

    `exports` holds each export's steps and its file's mtime, the full export first. The exports
    number the same steps, so a step either one holds is counted, from the full export's copy when
    both hold it. If they disagree about a step they both hold, their numbering has diverged and
    only one export is counted, so nothing is counted twice: the one holding more steps, and on a
    tie the more recent one, whose newest step is the latest, then whose file was written last.
    `parse` reads a step's `created_at`.
    """
    def standing(export):
        steps, mtime = export
        times = [when.timestamp() for when in (parse(step[2]) for step in steps.values()) if when]
        return len(steps), max(times, default=-math.inf), mtime

    merged = {}
    for steps, _ in exports:
        if any(merged[i][:3] != step[:3] for i, step in steps.items() if i in merged):
            return max(exports, key=standing)[0]
        for i, step in steps.items():
            merged.setdefault(i, step)
    return merged


def _contribution(c, path, now):
    stats = c._Stats("")
    session = hashlib.sha256(str(path.parents[2]).encode()).hexdigest()
    cutoff = now - timedelta(days=c.STATS_DAYS)
    cacheable = True
    partial = False
    exports = []
    for export in _exports(path.parent):
        steps, cut = _read_steps(export)
        if steps is not None:
            try:
                mtime = export.stat().st_mtime
            except OSError:
                mtime = -math.inf
            exports.append((steps, mtime))
            partial = partial or cut
    for _, (kind, source, created, incoming, outgoing) in sorted(_merge(exports, c._parse_iso).items()):
        when = c._parse_iso(created)
        if when and when > now:
            cacheable = False
            continue
        if not when or when < cutoff:
            continue
        if kind == "USER_INPUT" and source == "USER_EXPLICIT":
            stats.add_prompt(when, session)
        elif kind == "PLANNER_RESPONSE" and source == "MODEL":
            if type(incoming) is not int or type(outgoing) is not int or min(incoming, outgoing) < 0:
                continue
            bucket = c._blank_bucket()
            bucket.update(input_tokens=incoming, output_tokens=outgoing, total_tokens=incoming + outgoing)
            stats.add_tokens(when, session, None, bucket, bucket["total_tokens"])
    return {"days": stats.days, "models": stats.models, "cacheable": cacheable, "partial": partial}


def _read_steps(path):
    """One export's DONE steps by step number, and whether the size bound cut the file.

    Only what the counts need is kept: never content, thinking or tool calls. An unreadable
    export yields None.
    """
    steps = {}
    partial = False
    try:
        with path.open("rb") as stream:
            read = 0
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_TRANSCRIPT:
                # New steps are appended, so the end of an oversized transcript holds its recent
                # activity. Read the most recent MAX_TRANSCRIPT bytes from the first whole line in
                # them, and report the file as partly counted rather than complete.
                partial = True
                stream.seek(size - MAX_TRANSCRIPT - 1)
                if stream.read(1) != b"\n":
                    while read < MAX_TRANSCRIPT:
                        skipped = stream.readline(MAX_LINE + 1)
                        read += len(skipped)
                        if not skipped or skipped.endswith(b"\n"):
                            break
            while read < MAX_TRANSCRIPT:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                read += len(line)
                if len(line) > MAX_LINE:
                    # Refuse the rest of an oversized record; never parse a partial line.
                    while line and not line.endswith(b"\n") and read < MAX_TRANSCRIPT:
                        line = stream.readline(MAX_LINE + 1)
                        read += len(line)
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(row, dict) or row.get("status") != "DONE":
                    continue
                step = row.get("step_index")
                if type(step) is not int or step < 0 or step in steps:
                    continue
                steps[step] = (row.get("type"), row.get("source"), row.get("created_at"),
                               row.get("input_tokens"), row.get("output_tokens"))
    except OSError:
        return None, False
    return steps, partial


def scan_stats(c, now=None, root=None, state_dir=None):
    now = now or c._now()
    base = Path(root) if root else Path.home() / ".gemini/antigravity-cli/brain"
    settings = c.read_json(base.parent / "settings.json")
    if isinstance(settings, dict) and settings.get("modelProvider") == "gemini":
        return c._no_stats("", {}, "agy is configured for Gemini API billing; subscription activity is unavailable")
    # One anchor per conversation reads both exports; summary and chunk copies are never counted.
    parts, scanned = c._scan_transcripts(base, now, lambda p, t: _contribution(c, p, t), state_dir,
                                         "*/.system_generated/logs/transcript*.jsonl", include=_is_anchor,
                                         siblings=lambda p: _exports(p.parent))
    partial = sum(1 for part in parts if part.get("partial"))
    stats = c._absorb(parts, now)
    stats.source = "~/.gemini/antigravity-cli/brain"
    stats.scan = {**scanned, "files_partial": partial}
    result = stats.finish(now)
    if result["available"]:
        result["note"] = "Local activity only. Tokens are reported input + output; cache overlap and model names are unspecified. History may include other sign-ins."
        if partial:
            mib = MAX_TRANSCRIPT // (1024 * 1024)
            result["note"] += (f" Incomplete: {partial} transcript(s) over {mib} MiB; only the most recent {mib} MiB"
                               " of each is counted.")
    return result
