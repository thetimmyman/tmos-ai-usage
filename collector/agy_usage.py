"""Read-only official Antigravity CLI meters and local transcript metadata.

The CLI owns authentication. No OAuth tokens, prompts, or response text are exported.
"""
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
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=diagnostic, timeout=timeout, check=False)
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
        return result.returncode, raw


def cli_json():
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
    code, raw = _run([binary, "-p", "/usage", "--output-format", "json",
                      "--print-timeout", "15s"], 18)
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
        return parse_usage(c, cli_json())
    except CliError as exc:
        reasons = {
            "unavailable": "Install the official Antigravity CLI (agy) to read its quota",
            "old_version": "Update agy to 1.3.5 or newer for read-only JSON quota checks",
            "unauthenticated": "Run agy interactively and sign in with your Google subscription account",
            "timeout": "Official agy quota check timed out; retry after checking agy",
        }
        return c.unknown("antigravity", reasons.get(str(exc), "Official agy quota check failed; check agy interactively"),
                         source="cli", status="unauthenticated" if str(exc) == "unauthenticated" else "unknown")


def _contribution(c, path, now):
    stats = c._Stats("")
    session = hashlib.sha256(str(path.parents[2]).encode()).hexdigest()
    cutoff = now - timedelta(days=c.STATS_DAYS)
    seen = set()
    cacheable = True
    try:
        with path.open("rb") as stream:
            read = 0
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
                if type(step) is not int or step < 0 or step in seen:
                    continue
                when = c._parse_iso(row.get("created_at"))
                if when and when > now:
                    cacheable = False
                    continue
                if not when or when < cutoff:
                    continue
                seen.add(step)
                if row.get("type") == "USER_INPUT" and row.get("source") == "USER_EXPLICIT":
                    stats.add_prompt(when, session)
                elif row.get("type") == "PLANNER_RESPONSE" and row.get("source") == "MODEL":
                    incoming = row.get("input_tokens")
                    outgoing = row.get("output_tokens")
                    if type(incoming) is not int or type(outgoing) is not int or min(incoming, outgoing) < 0:
                        continue
                    bucket = c._blank_bucket()
                    bucket.update(input_tokens=incoming, output_tokens=outgoing, total_tokens=incoming + outgoing)
                    stats.add_tokens(when, session, None, bucket, bucket["total_tokens"])
    except OSError:
        pass
    return {"days": stats.days, "models": stats.models, "cacheable": cacheable}


def scan_stats(c, now=None, root=None, state_dir=None):
    now = now or c._now()
    base = Path(root) if root else Path.home() / ".gemini/antigravity-cli/brain"
    settings = c.read_json(base.parent / "settings.json")
    if isinstance(settings, dict) and settings.get("modelProvider") == "gemini":
        return c._no_stats("", {}, "agy is configured for Gemini API billing; subscription activity is unavailable")
    # Full export preferred; summary and chunk copies must never be counted again.
    def include(path):
        return path.name == "transcript_full.jsonl" or (
            path.name == "transcript.jsonl" and not (path.parent / "transcript_full.jsonl").is_file())
    parts, scanned = c._scan_transcripts(base, now, lambda p, t: _contribution(c, p, t), state_dir,
                                         "*/.system_generated/logs/transcript*.jsonl", include=include)
    stats = c._absorb(parts, now)
    stats.source = "~/.gemini/antigravity-cli/brain"
    stats.scan = scanned
    result = stats.finish(now)
    if result["available"]:
        result["note"] = "Local activity only. Tokens are reported input + output; cache overlap and model names are unspecified. History may include other sign-ins."
    return result
