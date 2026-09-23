#!/usr/bin/env python3
"""tools/usage_collector.py — one honest picture of how much AI subscription is left.

Five providers, five first-party sources, one merged document under the tmosd state path
(`~/.local/state/tmos/usage.json`, atomic write). The shell plugin `shell/plugins/tmos.usage`
reads that file and does no network of its own (Omarchy-native rule N-series: a plugin displays,
a collector observes).

Per-provider record (the contract the QML reads):

    {"provider": "claude-code", "plan": "max", "source": "api|cli|local",
     "windows": [{"name": "5h"|"week"|"month", "used_pct": 23.0, "remaining_pct": 77.0,
                  "resets_at": "2026-09-21T22:30:00+00:00", "resets_in_s": 4200}],
     "observed_at": "...Z", "status": "ok|estimate|unauthenticated|unknown|error", "note": "...",
     "balance": {"remaining": 12.34, "funded": 20.0, "spent": 7.66, "currency": "USD",
                 "estimated": true},                             # prepaid providers only
     "stats": {"available": true, "source": "~/.claude/projects", "coverage_days": 30,
               "daily": [{"date": "2026-09-22", "tokens": 36250589, "prompts": 12,
                          "sessions": 3}],                      # whole window, oldest first
               "recent_days": [{...}],                           # the last 7 of `daily`
               "today": {"date": "2026-09-23", "tokens": 0, "prompts": 0, "sessions": 0},
               "totals": {"tokens": 0, "prompts": 0, "sessions": 0, "active_days": 6,
                          "first_date": "", "last_date": ""},
               "models": [{"id": "claude-opus-5", "input_tokens": 0, "output_tokens": 0,
                           "cache_read_tokens": 0, "cache_write_tokens": 0, "total_tokens": 0}],
               "scan": {"files_scanned": 1, "files_cached": 300}}}

A provider TMOS reads no transcript from carries `"available": false` and the reason, never an
empty chart and never a fabricated zero.

Document level:

    {"schema_version": 2, "observed_at": "...Z", "providers": [...],
     "totals": {"tokens_today": 0, "tokens_7d": 0, "providers_reported": 5,
                "providers_with_limits": 5, "providers_with_stats": 2}}

Rules this file keeps (they are the whole point):
  * No collector can crash the run. Every provider is wrapped; a failure becomes
    status="unknown" with a one-line reason, and the other four still report.
  * Key material is never printed, logged, or written to the cache. Credentials are read
    straight into a request header; error text is scrubbed of long opaque tokens before it is
    allowed anywhere near stdout (`_scrub`).
  * Nothing is invented. A number TMOS cannot source is absent, not guessed; a number TMOS
    derives (the ClinePass fallback, Command Code's monthly) says so in `note` and, where the
    derivation is not the provider's own arithmetic, in `status`.
  * Local history comes from each CLI's own transcripts, read once per file and cached under the
    state dir (`stats-cache.json`): the transcript tree is ~440 MB here and this runs on a 5-minute
    timer, so an unchanged file is never parsed twice. See "Local stats" below.
  * A cross-machine snapshot is NOT implemented yet: there is no sync path in this file, so the
    document carries no `sync` block. When it lands it will merge *stats* only — a rate-limit
    window belongs to one account and must never be merged, so a stale copy from another node
    cannot overwrite a live reading on this one.
  * `--selftest` proves the parsers offline, including the readings that must NOT be invented: a
    transcript with no usage rows, an unreadable tree, and a rollout whose rate limits are empty.

Where each number comes from (read from the installed clients, not guessed — see README.md):
  opencode-go   GET https://opencode.ai/zen/go/v1/usage, Bearer key from
                ~/.local/share/opencode/auth.json["opencode-go"].key. Donor shape confirmed
                against ardfard/omarchy-opencode-usage collector.sh (DONOR-REGISTER.md).
  clinepass     GET https://api.cline.bot/api/v1/users/me/plan/usage-limits (the meter),
                status "ok". Falls back to /users/me/plan (caps) + /users/<id>/usages (spend,
                DERIVED windows, status "estimate"). Bearer $CLINE_API_KEY (~/secrets/cline.env)
                or ~/.cline/data/settings/providers.json.
  command-code  GET https://api.commandcode.ai/alpha/billing/credits (5h + weekly meters),
                /alpha/billing/subscriptions (plan, period end). Bearer $COMMAND_CODE_API_KEY
                or ~/.commandcode/auth.json. Endpoint constants read from the installed CLI.
  claude-code   GET https://api.anthropic.com/api/oauth/usage with the CLI's own OAuth access
                token (~/.claude/.credentials.json), read-only. Falls back to a local estimate
                from ~/.claude/projects/*/*.jsonl when the endpoint is unreachable.
  codex         GET https://chatgpt.com/backend-api/wham/usage with the CLI's ChatGPT tokens
                (~/.codex/auth.json). Reports "unauthenticated" with a `codex login` hint when
                that file holds no token.

Usage:
  tools/usage_collector.py --once            collect once, write the cache
  tools/usage_collector.py --once --json     collect once, write the cache, print the document
  tools/usage_collector.py --once --no-stats limits only, skipping the local transcript scan
  tools/usage_collector.py --selftest        parse fixtures offline (no network), incl. a
                                             malformed one that must degrade to "unknown"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 2
TIMEOUT_S = 8.0
USER_AGENT = "tmos-usage-collector/0.1 (+shell/plugins/tmos.usage)"

def _default_state_dir() -> Path:
    """Where the cache lives.

    The plugin ships its own collector, so the default is the plugin's own state directory: nothing
    depends on a TMOS checkout being present, and removing the plugin can take the state with it.
    `TMOS_USAGE_STATE_DIR` overrides it; `TMOS_STATE_DIR` is still honoured for the TMOS monorepo's
    own gate, which points the collector at a scratch directory.
    """
    for name in ("TMOS_USAGE_STATE_DIR", "TMOS_STATE_DIR"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return Path(os.path.expanduser(value))
    return Path(os.path.expanduser("~/.local/state/tmos-ai-usage"))


STATE_DIR = _default_state_dir()
CACHE_PATH = STATE_DIR / "usage.json"


def configure_paths(state_dir: str | None = None) -> Path:
    """Re-point the cache for `--state-dir`. One writer, one path, chosen once per run."""
    global STATE_DIR, CACHE_PATH
    if state_dir:
        STATE_DIR = Path(os.path.expanduser(state_dir))
        CACHE_PATH = STATE_DIR / "usage.json"
    return CACHE_PATH

WINDOW_ORDER = {"5h": 0, "week": 1, "month": 2}

# Window names by duration, for providers that describe a window by its length in seconds.
_DURATION_WINDOWS = ((18000, "5h"), (604800, "week"), (2592000, "month"))

# A token-shaped run: anything long and opaque never reaches stdout or the cache.
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_\-]{24,}|(?:sk|pk|eyJ|cline|usr)[A-Za-z0-9_\-\.]{12,}"
)


# ------------------------------------------------------------------ helpers


def _scrub(text: object) -> str:
    """One short line, with any key-shaped run replaced. Every `note` goes through here."""
    s = " ".join(str(text).split())
    s = _TOKEN_RE.sub("<redacted>", s)
    return s[:180]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    """ISO-8601 with Z, offsets, or fractional seconds; None on anything else."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _from_epoch(value: object, unit: str = "s") -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = value / (1000.0 if unit == "ms" else 1.0)
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _num(value: object) -> float | None:
    """A float from a JSON number, or None for anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _int(value: object) -> int:
    """An integer from whatever a transcript held: never raises, never invents.

    Transcripts are written by other tools, so a counter can arrive as a number, as a numeric
    string, or as something else entirely. A field TMOS cannot read counts as zero — it never
    becomes an exception that takes the whole run down.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError, OverflowError):
            return 0
    return 0


def _clamp_pct(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def window(
    name: str, used_pct: float, resets_at: datetime | None, now: datetime | None = None
) -> dict:
    """One window row. `remaining_pct` and `resets_in_s` are derived here and nowhere else."""
    now = now or _now()
    used = _clamp_pct(used_pct)
    resets_in = None
    if resets_at is not None:
        resets_in = max(0, _int((resets_at - now).total_seconds()))
    return {
        "name": name,
        "used_pct": used,
        "remaining_pct": round(100.0 - used, 1),
        "resets_at": _iso(resets_at),
        "resets_in_s": resets_in,
    }


def window_for_duration(seconds: object) -> str | None:
    """ "5h"/"week"/"month" for a window the provider describes by length (codex)."""
    value = _num(seconds)
    if value is None:
        return None
    for length, name in _DURATION_WINDOWS:
        if abs(value - length) <= length * 0.2:
            return name
    return None


def record(
    provider: str,
    *,
    status: str,
    source: str = "api",
    plan: str | None = None,
    windows: list | None = None,
    note: str = "",
    observed_at: datetime | None = None,
) -> dict:
    rows = sorted(windows or [], key=lambda w: WINDOW_ORDER.get(w.get("name", ""), 99))
    return {
        "provider": provider,
        "plan": plan,
        "windows": rows,
        "source": source,
        "observed_at": _iso(observed_at or _now()),
        "status": status,
        "note": _scrub(note) if note else "",
    }


def unknown(
    provider: str, reason: object, *, source: str = "api", status: str = "unknown"
) -> dict:
    return record(provider, status=status, source=source, note=str(reason))


# One opener for the whole process. `build_opener()` installs the default handlers — redirects
# included — so this behaves exactly as `urlopen` does, with a single audited call site.
_OPENER = urllib.request.build_opener()


def http_json(
    url: str,
    token: str | None = None,
    *,
    headers: dict | None = None,
    timeout: float = TIMEOUT_S,
) -> object:
    """GET one JSON document. The token is placed in a header and never in the URL or a log.

    Only http(s) is opened. Every base URL here can be overridden by an environment variable
    (the gates point them at a dead local port), so the scheme is checked before anything is
    opened: a `file:` base must never turn a usage read into a local file read.
    """
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"refusing to open a {scheme or 'relative'} URL; only http(s) is read"
        )
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        hdrs["Authorization"] = "Bearer " + token
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method="GET")  # nosec B310 - scheme checked above
    with _OPENER.open(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ValueError(f"response was not JSON: {exc}") from exc


def read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def env_or_file_key(env_name: str, env_files: list[Path]) -> str | None:
    """$NAME, else NAME=... from a shell-style env file (~/secrets/*.env). Never printed."""
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value
    for path in env_files:
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line.startswith(env_name + "="):
                continue
            candidate = line.split("=", 1)[1].strip().strip("'\"")
            if candidate:
                return candidate
    return None


# ------------------------------------------------------- parsers (pure, fixture-tested)


def parse_opencode(payload: object, now: datetime | None = None) -> dict:
    """{"usage": {"rolling"|"weekly"|"monthly": {percent, resetsAt, status}}} -> record."""
    usage = (payload or {}).get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return unknown("opencode-go", "usage endpoint returned no `usage` object")
    rows, rate_limited = [], []
    for key, name in (("rolling", "5h"), ("weekly", "week"), ("monthly", "month")):
        win = usage.get(key)
        if not isinstance(win, dict):
            continue
        percent = _num(win.get("percent"))
        if percent is None:
            continue
        rows.append(window(name, percent, _parse_iso(win.get("resetsAt")), now))
        if win.get("status") not in (None, "ok"):
            rate_limited.append(f"{name}:{_scrub(win.get('status'))}")
    if not rows:
        return unknown("opencode-go", "no window carried a numeric percent")
    return record(
        "opencode-go",
        status="ok",
        plan="OpenCode Go",
        windows=rows,
        note="; ".join(rate_limited),
        observed_at=now,
    )


def parse_clinepass_limits(payload: object, now: datetime | None = None) -> dict:
    """/users/me/plan/usage-limits -> data {limits: [{type, percentUsed, resetsAt}]}.

    This is the meter the Cline dashboard's "Usage Limits" panel shows: three windows
    (`five_hour` / `weekly` / `monthly`) each carrying the provider's own percentage and a reset
    timestamp. TMOS does no arithmetic here — the numbers are Cline's, so status is "ok".
    """
    now = now or _now()
    data = (
        payload.get("data")
        if isinstance(payload, dict) and "data" in payload
        else payload
    )
    if not isinstance(data, dict):
        return unknown("clinepass", "usage-limits endpoint returned no `data` object")
    limits = data.get("limits")
    if not isinstance(limits, list):
        return unknown("clinepass", "usage-limits endpoint returned no `limits` array")
    type_name = {"five_hour": "5h", "weekly": "week", "monthly": "month"}
    rows = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        name = type_name.get(kind) if isinstance(kind, str) else None
        percent = _num(item.get("percentUsed"))
        if name is None or percent is None:
            continue
        rows.append(window(name, percent, _parse_iso(item.get("resetsAt")), now))
    if not rows:
        return unknown("clinepass", "no limit carried a numeric percentUsed")
    return record(
        "clinepass",
        status="ok",
        windows=rows,
        note="Cline's own usage-limits meter (percentUsed per window)",
        observed_at=now,
    )


def parse_clinepass(
    plan_payload: object, usage_items: object, now: datetime | None = None
) -> dict:
    """Fallback: ClinePass caps + spend transactions -> derived rolling windows.

    Used only when the /users/me/plan/usage-limits meter is unreachable. Cline still exposes caps
    (`entitlements.cline_pass.inferenceCapThreshold`, per 5h / 7d / 30d) and a ledger of per-request
    `costUsd`, but no ready-made meter in this shape, so TMOS computes one — in-window spend over
    the cap — and says so. Two things are UNKNOWN and keep this path at status="estimate": whether
    `costUsd` and the cap are quoted in the same unit (the CLI never divides them), and whether the
    ledger page we can afford to read reaches back a full 30 days. `resets_at` is when the OLDEST
    in-window charge ages out — a rolling window never resets flat.
    """
    now = now or _now()
    plan = plan_payload.get("plan") if isinstance(plan_payload, dict) else None
    if not isinstance(plan, dict):
        return unknown("clinepass", "plan endpoint returned no `plan` object")
    caps = ((plan.get("entitlements") or {}).get("cline_pass") or {}).get(
        "inferenceCapThreshold"
    ) or {}
    if not isinstance(caps, dict):
        caps = {}
    spec = (
        ("5h", "last5HoursUsageCostUSDPerUser", timedelta(hours=5)),
        ("week", "last7daysUsageCostUSDPerUser", timedelta(days=7)),
        ("month", "last30daysUsageCostUSDPerUser", timedelta(days=30)),
    )
    items = usage_items if isinstance(usage_items, list) else []
    charges: list[tuple[datetime, float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        when = _parse_iso(item.get("createdAt"))
        cost = _num(item.get("costUsd"))
        if when is not None and cost is not None:
            charges.append((when, cost))
    rows = []
    for name, cap_key, span in spec:
        cap = _num(caps.get(cap_key))
        if cap is None or cap <= 0:
            continue
        start = now - span
        in_window = [(when, cost) for when, cost in charges if when >= start]
        spent = sum(cost for _, cost in in_window)
        oldest = min((when for when, _ in in_window), default=None)
        rows.append(
            window(name, spent / cap * 100.0, (oldest + span) if oldest else None, now)
        )
    if not rows:
        return unknown("clinepass", "plan carries no ClinePass inference caps")
    name = plan.get("displayName") or plan.get("name")
    return record(
        "clinepass",
        status="estimate",
        plan=_scrub(name) if name else None,
        windows=rows,
        note="derived by TMOS: in-window spend / plan cap. Cline publishes no meter; the "
        "cap/charge unit is unverified and the ledger read is bounded, so treat these "
        "as a floor. Rolling windows: `resets in` is when the oldest charge ages out",
        observed_at=now,
    )


# Plan id prefix -> (display name, monthly credit allowance). Read from the installed
# Command Code CLI (dist/cli.mjs plan tables); longest prefix wins, as the CLI does.
COMMAND_CODE_PLANS = {
    "individual-go": ("Go", 10),
    "individual-goat": ("GOAT", 70),
    "individual-pro": ("Pro", 30),
    "individual-pro-v1": ("Pro", 80),
    "individual-provider": ("Provider", 15),
    "individual-max": ("Max", 150),
    "individual-ultra": ("Ultra", 300),
    "teams-pro": ("Teams Pro", 40),
}


def _command_code_plan(plan_id: object) -> tuple[str | None, float | None]:
    if not isinstance(plan_id, str) or not plan_id:
        return None, None
    key = plan_id.lower().replace("_", "-")
    for prefix in sorted(COMMAND_CODE_PLANS, key=len, reverse=True):
        if key.startswith(prefix):
            name, credits = COMMAND_CODE_PLANS[prefix]
            return name, credits
    return None, None


def parse_command_code(
    credits_payload: object, subscription_payload: object, now: datetime | None = None
) -> dict:
    """/alpha/billing/credits windowLimits (5h, weekly) + the monthly credit pool."""
    now = now or _now()
    if not isinstance(credits_payload, dict):
        return unknown("command-code", "credits endpoint returned no object")
    limits = credits_payload.get("windowLimits")
    rows = []
    if isinstance(limits, dict):
        for key, name in (("fiveHour", "5h"), ("weekly", "week")):
            win = limits.get(key)
            if not isinstance(win, dict):
                continue
            used, cap = _num(win.get("used")), _num(win.get("cap"))
            if used is None or cap is None or cap <= 0:
                continue
            rows.append(
                window(
                    name, used / cap * 100.0, _from_epoch(win.get("resetAt"), "ms"), now
                )
            )
    sub = (
        subscription_payload.get("data")
        if isinstance(subscription_payload, dict)
        else None
    )
    sub = sub if isinstance(sub, dict) else {}
    plan_name, plan_credits = _command_code_plan(sub.get("planId"))
    credits = credits_payload.get("credits")
    credits = credits if isinstance(credits, dict) else {}
    monthly = _num(credits.get("monthlyCredits")) or 0.0
    purchased = _num(credits.get("purchasedCredits")) or 0.0
    free = _num(credits.get("freeCredits")) or 0.0
    remaining = monthly + purchased + free
    if plan_credits and sub.get("status") == "active":
        pool = max(plan_credits, monthly) + purchased + free
        if pool > 0:
            rows.append(
                window(
                    "month",
                    (pool - remaining) / pool * 100.0,
                    _parse_iso(sub.get("currentPeriodEnd")),
                    now,
                )
            )
    if not rows:
        return unknown(
            "command-code", "no window meter and no plan credit pool in the response"
        )
    note = (
        "month = billing-period credit pool consumed (plan allowance vs credits left)"
    )
    out = record(
        "command-code",
        status="ok",
        plan=plan_name,
        windows=rows,
        note=note,
        observed_at=now,
    )
    # The prepaid ledger, from the same numbers the month window is built on: the allowance that
    # funded the pool, minus what is left. Labelled estimated because the pool is reconstructed
    # from the plan table and today's credits, not read from a ledger the CLI publishes.
    prepaid = purchased + free
    allowance = (
        max(plan_credits or 0.0, monthly)
        if (plan_credits and sub.get("status") == "active")
        else monthly
    )
    funded = allowance + prepaid
    if funded > 0:
        out["balance"] = {
            "remaining": round(remaining, 2),
            "funded": round(funded, 2),
            "spent": round(max(0.0, funded - remaining), 2),
            "currency": "USD",
            "estimated": True,
        }
    return out


def parse_claude_code(
    payload: object, plan: str | None = None, now: datetime | None = None
) -> dict:
    """/api/oauth/usage -> {five_hour|seven_day: {utilization, resets_at}}."""
    if not isinstance(payload, dict):
        return unknown("claude-code", "usage endpoint returned no object")
    rows = []
    for key, name in (("five_hour", "5h"), ("seven_day", "week")):
        win = payload.get(key)
        if not isinstance(win, dict):
            continue
        utilization = _num(win.get("utilization"))
        if utilization is None:
            continue
        rows.append(window(name, utilization, _parse_iso(win.get("resets_at")), now))
    if not rows:
        return unknown(
            "claude-code", "no five_hour/seven_day utilization in the response"
        )
    return record("claude-code", status="ok", plan=plan, windows=rows, observed_at=now)


def parse_codex(payload: object, now: datetime | None = None) -> dict:
    """/backend-api/wham/usage -> rate_limit.{primary,secondary}_window."""
    if not isinstance(payload, dict):
        return unknown("codex", "usage endpoint returned no object")
    limit = payload.get("rate_limit")
    if not isinstance(limit, dict):
        return unknown("codex", "response carries no `rate_limit` object")
    rows, seen = [], set()
    for key in ("primary_window", "secondary_window"):
        win = limit.get(key)
        if not isinstance(win, dict):
            continue
        used = _num(win.get("used_percent"))
        if used is None:
            continue
        name = window_for_duration(win.get("limit_window_seconds"))
        if name is None or name in seen:
            continue
        seen.add(name)
        resets_at = _from_epoch(win.get("reset_at"))
        if resets_at is None:
            after = _num(win.get("reset_after_seconds"))
            resets_at = (
                (now or _now()) + timedelta(seconds=after)
                if after is not None
                else None
            )
        rows.append(window(name, used, resets_at, now))
    if not rows:
        return unknown("codex", "no primary/secondary window carried a used_percent")
    plan = payload.get("plan_type")
    return record(
        "codex",
        status="ok",
        plan=_scrub(plan) if isinstance(plan, str) else None,
        windows=rows,
        observed_at=now,
    )


# Claude Code local fallback: token counts from the CLI's own transcripts. An estimate of
# *activity*, never of the account's real limit, so it is reported as status="estimate" with
# no percentage claim it cannot back.
def parse_claude_local(entries: list, now: datetime | None = None) -> dict:
    """entries: [{"timestamp": iso, "tokens": int}] within the current 5h window."""
    now = now or _now()
    start = now - timedelta(hours=5)
    total = 0
    for entry in entries or []:
        when = _parse_iso(entry.get("timestamp")) if isinstance(entry, dict) else None
        tokens = _num(entry.get("tokens")) if isinstance(entry, dict) else None
        if when is not None and tokens is not None and when >= start:
            total += _int(tokens)
    return record(
        "claude-code",
        status="estimate",
        source="local",
        plan=None,
        windows=[],
        note=f"usage endpoint unreachable; local transcripts show ~{total} tokens in the "
        "last 5h — activity only, not a share of the account limit",
        observed_at=now,
    )


# ------------------------------------------------- local stats (the CLI's own transcripts)
#
# A usage endpoint reports headroom; it never reports what the headroom was spent on. Only the
# CLIs keep that: tokens per day, tokens per model, and how many prompts it took. TMOS reads
# their transcripts — the files the tools wrote themselves, never a second guess — so the panel
# can show history without the display ever doing I/O of its own (constitution.md: an observer
# records, a display displays).
#
#   claude-code  ~/.claude/projects/<project>/<session>.jsonl (+ <session>/subagents/*.jsonl)
#                message.usage.{input,output,cache_read_input,cache_creation_input}_tokens,
#                message.model, and one typed user row per prompt.
#   codex        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
#                event_msg/token_count.info.last_token_usage (the per-turn delta),
#                turn_context.model, and one task_started per turn.
#
# Files are read ONCE: each file's contribution is cached under the state dir keyed by
# (size, mtime), because the transcript tree here is ~440 MB and this runs on a five-minute timer.
# A file's mtime is its last write, so every row inside the window lives in a file touched inside
# the window — pruning by mtime can never drop a day we still report.
#
# A provider TMOS reads no transcript from reports available=false with the reason. It never
# borrows another provider's history, and a scan that finds nothing reports nothing, not zero.

STATS_DAYS = 30
STATS_CACHE_VERSION = 2
STATS_MAX_FILES = 600


def _recent_files(base: Path, pattern: str, since: float) -> list[Path]:
    """Files under `base` matching `pattern`, newest first, bounded, never fatal."""
    if not base.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    for path in base.rglob(pattern):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            found.append((mtime, path))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:STATS_MAX_FILES]]


def _jsonl_rows(path: Path) -> list[dict]:
    """Every parseable JSON object in a JSONL file. An unreadable file yields nothing."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _local_day(when: datetime) -> str:
    """The calendar day where the user is: "today" has to mean their today, not UTC's."""
    return when.astimezone().strftime("%Y-%m-%d")


def _stats_cache_path(state_dir: Path | None = None) -> Path:
    return (state_dir or STATE_DIR) / "stats-cache.json"


def _load_stats_cache(state_dir: Path | None = None) -> dict:
    raw = read_json(_stats_cache_path(state_dir))
    if not isinstance(raw, dict) or raw.get("schema_version") != STATS_CACHE_VERSION:
        return {"schema_version": STATS_CACHE_VERSION, "files": {}}
    if not isinstance(raw.get("files"), dict):
        raw["files"] = {}
    return raw


def _save_stats_cache(cache: dict, state_dir: Path | None = None) -> None:
    try:
        path = _stats_cache_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache) + "\n")
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written costs time, never correctness


def _json_safe(contribution: dict) -> dict:
    """A contribution as the cache must store it: JSON has no sets, so session ids become a list."""
    days = {}
    for date, row in (contribution.get("days") or {}).items():
        sessions = row.get("sessions")
        days[date] = {
            "tokens": _int(row.get("tokens")),
            "prompts": _int(row.get("prompts")),
            "sessions": sorted(str(item) for item in sessions)
            if isinstance(sessions, (list, set, tuple))
            else [],
        }
    models = {}
    for model, bucket in (contribution.get("models") or {}).items():
        models[str(model)] = {key: _int(bucket.get(key)) for key in _blank_bucket()}
    return {"days": days, "models": models}


def _scan_transcripts(
    base: Path, now: datetime, contribution, state_dir: Path | None = None
) -> tuple[list[dict], dict]:
    """Per-file contributions for files touched inside the window, reusing the cache."""
    cache = _load_stats_cache(state_dir)
    entries = cache["files"]
    cutoff = (now - timedelta(days=STATS_DAYS)).timestamp()
    out: list[dict] = []
    seen: set[str] = set()
    scanned = cached = 0
    for path in _recent_files(base, "*.jsonl", cutoff):
        key = str(path)
        seen.add(key)
        try:
            info = path.stat()
        except OSError:
            continue
        hit = entries.get(key)
        if (
            isinstance(hit, dict)
            and hit.get("size") == info.st_size
            and hit.get("mtime") == info.st_mtime
        ):
            out.append(hit)
            cached += 1
            continue
        fresh = _json_safe(contribution(path, now))
        fresh["size"] = info.st_size
        fresh["mtime"] = info.st_mtime
        entries[key] = fresh
        out.append(fresh)
        scanned += 1
    # Forget files that vanished or aged out — but only under this base, since the cache is shared.
    prefix = str(base)
    for key in [k for k in entries if k.startswith(prefix) and k not in seen]:
        entries.pop(key, None)
    cache["files"] = entries
    _save_stats_cache(cache, state_dir)
    return out, {"files_scanned": scanned, "files_cached": cached}


def _blank_bucket() -> dict:
    """One model's token buckets.

    `total_tokens` is carried, not derived: providers disagree about whether cache tokens are
    their own fields or a subset of the input. Anthropic reports them separately (so the reply
    total is the sum of all four); Codex counts cached input *inside* `input_tokens` (so its total
    is input+output and cache_read is reported beside it, never added again).
    """
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }


def _no_stats(source: str, scan: dict | None, reason: str) -> dict:
    """The honest empty case: no history, and why. Never a chart of zeroes."""
    return {
        "available": False,
        "source": source,
        "coverage_days": STATS_DAYS,
        "daily": [],
        "recent_days": [],
        "today": {"date": "", "tokens": 0, "prompts": 0, "sessions": 0},
        "totals": {
            "tokens": 0,
            "prompts": 0,
            "sessions": 0,
            "active_days": 0,
            "first_date": "",
            "last_date": "",
        },
        "models": [],
        "scan": scan or {},
        "note": _scrub(reason),
    }


def _stats_document(
    source: str,
    days: dict,
    models: dict,
    scan: dict,
    now: datetime,
    sessions_total: int | None = None,
) -> dict:
    """The stats contract from a day/model union. One place derives, so one place can be wrong.

    `sessions_total` is the count of *distinct* sessions when the caller still knows their ids; a
    session that worked across midnight is one session, not one per day. Callers that only have
    per-day counts (a reader with no ids) pass nothing and get the sum.
    """
    ordered = []
    for date in sorted(days):
        row = days[date]
        ordered.append(
            {
                "date": date,
                "tokens": _int(row.get("tokens")),
                "prompts": _int(row.get("prompts")),
                "sessions": _int(row.get("sessions")),
            }
        )
    if not ordered:
        return _no_stats(
            source,
            scan,
            f"no transcript in the last {STATS_DAYS} days carried a "
            "token or prompt count",
        )
    model_rows = []
    for model, bucket in models.items():
        row = dict(bucket)
        row["id"] = model
        model_rows.append(row)
    model_rows.sort(key=lambda row: row["total_tokens"], reverse=True)
    today = _local_day(now)
    return {
        "available": True,
        "source": source,
        "coverage_days": STATS_DAYS,
        "daily": ordered,
        "recent_days": ordered[-7:],
        "today": next(
            (row for row in ordered if row["date"] == today),
            {"date": today, "tokens": 0, "prompts": 0, "sessions": 0},
        ),
        "totals": {
            "tokens": sum(row["tokens"] for row in ordered),
            "prompts": sum(row["prompts"] for row in ordered),
            "sessions": sum(row["sessions"] for row in ordered)
            if sessions_total is None
            else sessions_total,
            "active_days": len(
                [row for row in ordered if row["tokens"] > 0 or row["prompts"] > 0]
            ),
            "first_date": ordered[0]["date"],
            "last_date": ordered[-1]["date"],
        },
        "models": model_rows,
        "scan": scan,
    }


class _Stats:
    """One provider's local history. Every sum happens here, so no day is counted twice.

    A day exists only once something has been observed on it: tokens, or a prompt. A row TMOS
    read but could not measure — a tool result, a `token_count` event with `info: null` — creates
    no day at all, so a scan that finds nothing reports nothing instead of a chart of zeroes.
    """

    def __init__(self, source: str, scan: dict | None = None):
        self.source = source
        self.scan = scan or {}
        self.days: dict[str, dict] = {}
        self.models: dict[str, dict] = {}

    def _day(self, when: datetime, session: str) -> dict:
        row = self.days.setdefault(
            _local_day(when), {"tokens": 0, "prompts": 0, "sessions": set()}
        )
        row["sessions"].add(session)
        return row

    def add_tokens(
        self, when: datetime, session: str, model: object, bucket: dict, total: int
    ) -> None:
        if total <= 0:
            return
        self._day(when, session)["tokens"] += total
        if isinstance(model, str) and model:
            acc = self.models.setdefault(model, _blank_bucket())
            for key, value in bucket.items():
                acc[key] += _int(value)

    def add_prompt(self, when: datetime, session: str) -> None:
        self._day(when, session)["prompts"] += 1

    def finish(self, now: datetime) -> dict:
        union: set = set()
        for row in self.days.values():
            union |= row["sessions"]
        days = {
            date: {**row, "sessions": len(row["sessions"])}
            for date, row in self.days.items()
        }
        return _stats_document(
            self.source, days, self.models, self.scan, now, sessions_total=len(union)
        )


def _claude_prompt(row: dict) -> bool:
    """A prompt the user typed — not a tool result echoed back as a "user" row."""
    if str(row.get("type")) != "user":
        return False
    raw_message = row.get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list):
        return any(
            isinstance(part, dict) and part.get("type") == "text" for part in content
        )
    return False


def _claude_contribution(path: Path, now: datetime) -> dict:
    """One Claude Code transcript's day/model contribution.

    Claude Code writes one row per assistant *content block* and repeats the same
    `message.usage` on every row of that reply, so a row-by-row sum multiplies a reply by its
    block count (~1.85x on this machine). One usage per `message.id`, last row winning, is the
    convention that reproduces Omarchy's own claude collector exactly — so that is what this does.
    """
    stats = _Stats("")
    session = path.stem
    replies: dict[str, tuple] = {}
    prompts: list[datetime] = []
    for index, row in enumerate(_jsonl_rows(path)):
        when = _parse_iso(row.get("timestamp"))
        if when is None:
            continue
        raw_message = row.get("message")
        message = raw_message if isinstance(raw_message, dict) else {}
        raw_usage = message.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else None
        if usage is not None:
            bucket = {
                "input_tokens": _num(usage.get("input_tokens")) or 0,
                "output_tokens": _num(usage.get("output_tokens")) or 0,
                "cache_read_tokens": _num(usage.get("cache_read_input_tokens")) or 0,
                "cache_write_tokens": _num(usage.get("cache_creation_input_tokens"))
                or 0,
            }
            key = message.get("id") or row.get("uuid") or row.get("requestId")
            if not isinstance(key, str) or not key:
                key = f"row:{index}"  # an unlabelled reply is counted, never merged by accident
            # Anthropic reports cache tokens as their own fields, so a reply's total is the sum.
            bucket["total_tokens"] = _int(sum(bucket.values()))
            replies[key] = (when, message.get("model"), bucket, bucket["total_tokens"])
        if _claude_prompt(row):
            prompts.append(when)
    for when, model, bucket, total in replies.values():
        stats.add_tokens(when, session, model, bucket, total)
    for when in prompts:
        stats.add_prompt(when, session)
    return {"days": stats.days, "models": stats.models}


def _codex_contribution(path: Path, now: datetime) -> dict:
    """One Codex rollout's day/model contribution.

    A rollout carries two token objects: `total_token_usage` is the session running total and
    `last_token_usage` the turn's own delta. Only the delta is summed — summing the running total
    would multiply every turn by all the turns before it.
    """
    stats = _Stats("")
    session = path.stem
    model: object = None
    for row in _jsonl_rows(path):
        when = _parse_iso(row.get("timestamp"))
        if when is None:
            continue
        kind = str(row.get("type"))
        raw_payload = row.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        if kind == "turn_context":
            payload_model = payload.get("model")
            if isinstance(payload_model, str):
                model = payload_model
            continue
        if kind != "event_msg":
            continue
        event = str(payload.get("type"))
        if event not in ("token_count", "task_started"):
            continue
        if event == "task_started":
            stats.add_prompt(when, session)
            continue
        raw_info = payload.get("info")
        info = raw_info if isinstance(raw_info, dict) else {}
        raw_last = info.get("last_token_usage")
        last = raw_last if isinstance(raw_last, dict) else {}
        bucket = {
            "input_tokens": _num(last.get("input_tokens")) or 0,
            "output_tokens": _num(last.get("output_tokens")) or 0,
            "cache_read_tokens": _num(last.get("cached_input_tokens")) or 0,
            "cache_write_tokens": _num(last.get("cache_write_input_tokens")) or 0,
        }
        # Codex counts cached input inside input_tokens, so the reply total is input+output and
        # cache_read is reported beside it, never added again; the rollout's own total wins.
        total = _int(last.get("total_tokens")) or _int(
            bucket["input_tokens"] + bucket["output_tokens"]
        )
        bucket["total_tokens"] = total
        stats.add_tokens(when, session, model, bucket, total)
    return {"days": stats.days, "models": stats.models}


def _absorb(parts: list[dict], now: datetime) -> _Stats:
    """Fold cached per-file contributions into one provider history, windowed to STATS_DAYS."""
    stats = _Stats("")
    cutoff = _local_day(now - timedelta(days=STATS_DAYS))
    for part in parts:
        for date, row in (part.get("days") or {}).items():
            if date < cutoff:
                continue
            day = stats.days.setdefault(
                date, {"tokens": 0, "prompts": 0, "sessions": set()}
            )
            day["tokens"] += _int(row.get("tokens"))
            day["prompts"] += _int(row.get("prompts"))
            sessions = row.get("sessions")
            if isinstance(sessions, (list, set, tuple)):
                day["sessions"].update(str(item) for item in sessions)
        for model, bucket in (part.get("models") or {}).items():
            acc = stats.models.setdefault(str(model), _blank_bucket())
            for key in acc:
                acc[key] += _int(bucket.get(key))
    return stats


def scan_claude_stats(
    now: datetime | None = None,
    root: Path | str | None = None,
    state_dir: Path | None = None,
) -> dict:
    now = now or _now()
    base = Path(root) if root else Path(os.path.expanduser("~/.claude/projects"))
    parts, scan = _scan_transcripts(base, now, _claude_contribution, state_dir)
    stats = _absorb(parts, now)
    stats.source = "~/.claude/projects"
    stats.scan = scan
    return stats.finish(now)


def scan_codex_stats(
    now: datetime | None = None,
    root: Path | str | None = None,
    state_dir: Path | None = None,
) -> dict:
    now = now or _now()
    base = Path(root) if root else Path(os.path.expanduser("~/.codex/sessions"))
    parts, scan = _scan_transcripts(base, now, _codex_contribution, state_dir)
    stats = _absorb(parts, now)
    stats.source = "~/.codex/sessions"
    stats.scan = scan
    return stats.finish(now)


def _has_window(limits: dict) -> bool:
    """A reading TMOS can actually use: at least one window carrying a percentage."""
    for key in ("primary", "secondary"):
        raw_win = limits.get(key)
        if isinstance(raw_win, dict) and _num(raw_win.get("used_percent")) is not None:
            return True
    return False


def codex_local_rate_limits(
    now: datetime | None = None, root: Path | str | None = None
) -> dict:
    """The limit reading the Codex CLI itself last received, from its own rollout files.

    Codex writes a limit check into every rollout, so when the usage endpoint is unreachable TMOS
    can still report what the CLI was last told, stamped with that reading's own time. It is not
    the live limit, so the record carrying it is status="estimate" and says where it came from.

    The newest reading that actually carries a window wins, not simply the newest reading: a later
    rollout whose limits are all null must not blank out a real reading from earlier.
    """
    now = now or _now()
    base = Path(root) if root else Path(os.path.expanduser("~/.codex/sessions"))
    newest: tuple[datetime, dict] | None = None
    newest_with_windows: tuple[datetime, dict] | None = None
    for path in _recent_files(base, "*.jsonl", (now - timedelta(days=7)).timestamp()):
        for row in _jsonl_rows(path):
            if str(row.get("type")) != "event_msg":
                continue
            raw_payload = row.get("payload")
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            if str(payload.get("type")) != "token_count":
                continue
            raw_limits = payload.get("rate_limits")
            when = _parse_iso(row.get("timestamp"))
            if not isinstance(raw_limits, dict) or when is None:
                continue
            if newest is None or when > newest[0]:
                newest = (when, raw_limits)
            if _has_window(raw_limits) and (
                newest_with_windows is None or when > newest_with_windows[0]
            ):
                newest_with_windows = (when, raw_limits)
    chosen = newest_with_windows or newest
    if chosen is None:
        return {}
    when, limits = chosen
    rows, seen = [], set()
    for key in ("primary", "secondary"):
        raw_win = limits.get(key)
        if not isinstance(raw_win, dict):
            continue
        used = _num(raw_win.get("used_percent"))
        name = window_for_duration((_num(raw_win.get("window_minutes")) or 0) * 60)
        if used is None or name is None or name in seen:
            continue
        seen.add(name)
        rows.append(window(name, used, _from_epoch(raw_win.get("resets_at")), now))
    raw_credits = limits.get("credits")
    return {
        "observed_at": when,
        "windows": rows,
        "credits": raw_credits if isinstance(raw_credits, dict) else {},
    }


STATS_SCANNERS = {"claude-code": scan_claude_stats, "codex": scan_codex_stats}

NO_LOCAL_TRANSCRIPT = {
    "clinepass": "Cline keeps its sessions in ~/.cline/data/db, which TMOS does not read, so there "
    "is no token history to show",
    "command-code": "Command Code keeps no token transcript in a format TMOS reads, so there is no "
    "token history to show",
    "opencode-go": "OpenCode keeps its sessions in ~/.local/share/opencode/opencode.db, which TMOS "
    "does not read, so there is no token history to show",
}


def stats_for(provider: str, now: datetime | None = None) -> dict:
    """Local history for one provider, or an explicit "none" that names the reason."""
    scanner = STATS_SCANNERS.get(provider)
    if scanner is None:
        return _no_stats(
            "", {}, NO_LOCAL_TRANSCRIPT.get(provider, "no local transcript")
        )
    try:
        return scanner(now)
    except Exception as exc:  # a broken transcript must never take the run down
        return _no_stats(
            "", {}, f"{type(exc).__name__}: could not read local transcripts"
        )


# ------------------------------------------------------------------ collectors (I/O)


def collect_opencode() -> dict:
    auth = read_json(Path(os.path.expanduser("~/.local/share/opencode/auth.json")))
    key = None
    if isinstance(auth, dict):
        entry = auth.get("opencode-go")
        if isinstance(entry, dict):
            key = entry.get("key")
    if not key:
        return unknown(
            "opencode-go",
            "no opencode-go key in ~/.local/share/opencode/auth.json; "
            "run `opencode auth login`",
            status="unauthenticated",
        )
    base = os.environ.get("OPENCODE_GO_BASE", "https://opencode.ai/zen/go/v1")
    return parse_opencode(http_json(base + "/usage", key))


def _try_json(url: str, token: str | None = None, **kwargs) -> object | None:
    """One optional read: a failure returns None instead of raising.

    Used for the reads whose absence must not downgrade a measurement that is already good — a
    missing plan name cannot be allowed to throw away a healthy meter.
    """
    try:
        return http_json(url, token, **kwargs)
    except Exception:
        return None


def collect_clinepass() -> dict:
    key = env_or_file_key(
        "CLINE_API_KEY", [Path(os.path.expanduser("~/secrets/cline.env"))]
    )
    if not key:
        providers = read_json(
            Path(os.path.expanduser("~/.cline/data/settings/providers.json"))
        )
        if isinstance(providers, dict):
            settings = ((providers.get("providers") or {}).get("cline") or {}).get(
                "settings"
            ) or {}
            key = settings.get("apiKey") if isinstance(settings, dict) else None
    if not key:
        return unknown(
            "clinepass",
            "no CLINE_API_KEY and no stored Cline account key; run `cline auth login`",
            status="unauthenticated",
        )
    base = os.environ.get("CLINE_API_BASE", "https://api.cline.bot")

    # Primary: Cline's own usage-limits meter (the dashboard's "Usage Limits" panel).
    payload = _try_json(f"{base}/api/v1/users/me/plan/usage-limits", key)
    if payload is not None:
        out = parse_clinepass_limits(payload)
        if out["status"] == "ok":
            plan = _try_json(f"{base}/api/v1/users/me/plan", key)
            plan = (
                plan.get("data") if isinstance(plan, dict) and "data" in plan else plan
            )
            p = plan.get("plan") if isinstance(plan, dict) else None
            name = (
                (p.get("displayName") or p.get("name")) if isinstance(p, dict) else None
            )
            if name:
                out["plan"] = _scrub(name)
            return out
    # Meter unreachable or an odd shape -> fall through to the ledger-derived estimate.

    plan = http_json(f"{base}/api/v1/users/me/plan", key)
    plan = plan.get("data") if isinstance(plan, dict) and "data" in plan else plan
    me = http_json(f"{base}/api/v1/users/me", key)
    me = me.get("data") if isinstance(me, dict) and "data" in me else me
    user_id = me.get("id") if isinstance(me, dict) else None
    items: list = []
    truncated = False
    if user_id:
        cutoff = _now() - timedelta(days=30)
        url = f"{base}/api/v1/users/{user_id}/usages?limit=100"
        for page in range(
            25
        ):  # bounded: 2500 charges covers a heavy month; never unbounded
            payload = http_json(url, key)
            data = (
                payload.get("data")
                if isinstance(payload, dict) and "data" in payload
                else payload
            )
            data = data if isinstance(data, dict) else {}
            raw_batch = data.get("items")
            batch = raw_batch if isinstance(raw_batch, list) else []
            items.extend(batch)
            oldest = min(
                (
                    when
                    for when in (
                        _parse_iso(i.get("createdAt")) if isinstance(i, dict) else None
                        for i in batch
                    )
                    if when is not None
                ),
                default=None,
            )
            token = data.get("nextToken")
            if not batch or not token or (oldest and oldest < cutoff):
                break
            url = f"{base}/api/v1/users/{user_id}/usages?limit=100&nextToken={token}"
            if page == 24:
                truncated = True
    out = parse_clinepass(plan, items)
    if truncated and out["windows"]:
        out["note"] = _scrub(
            out["note"] + "; ledger truncated at 2500 charges, so the week/month "
            "figures under-count"
        )
    return out


def collect_command_code() -> dict:
    key = env_or_file_key(
        "COMMAND_CODE_API_KEY", [Path(os.path.expanduser("~/secrets/command-code.env"))]
    )
    if not key:
        auth = read_json(Path(os.path.expanduser("~/.commandcode/auth.json")))
        key = auth.get("apiKey") if isinstance(auth, dict) else None
    if not key:
        return unknown(
            "command-code",
            "no COMMAND_CODE_API_KEY and no ~/.commandcode/auth.json; run `cmd login`",
            status="unauthenticated",
        )
    base = os.environ.get("COMMAND_CODE_API_BASE", "https://api.commandcode.ai")
    credits = http_json(f"{base}/alpha/billing/credits", key)
    subscription = http_json(f"{base}/alpha/billing/subscriptions", key)
    return parse_command_code(credits, subscription)


def _claude_local_entries(now: datetime) -> list:
    """Token counts from ~/.claude/projects/*/*.jsonl for the last 5h. Read-only, bounded."""
    root = Path(os.path.expanduser("~/.claude/projects"))
    cutoff = (now - timedelta(hours=5)).timestamp()
    entries: list = []
    if not root.is_dir():
        return entries
    files = [p for p in root.glob("*/*.jsonl") if p.stat().st_mtime >= cutoff]
    for path in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
        try:
            lines = path.read_text(errors="replace").splitlines()[-4000:]
        except OSError:
            continue
        for line in lines:
            if '"usage"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            usage = (
                (row.get("message") or {}).get("usage")
                if isinstance(row.get("message"), dict)
                else None
            )
            if not isinstance(usage, dict):
                continue
            tokens = sum(
                _num(usage.get(k)) or 0.0
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            entries.append({"timestamp": row.get("timestamp"), "tokens": tokens})
    return entries


def collect_claude_code() -> dict:
    creds = read_json(Path(os.path.expanduser("~/.claude/.credentials.json")))
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    plan = oauth.get("subscriptionType") if isinstance(oauth, dict) else None
    if not token:
        return unknown(
            "claude-code",
            "no OAuth credentials in ~/.claude/.credentials.json; "
            "run `claude` and sign in",
            status="unauthenticated",
        )
    base = os.environ.get("CLAUDE_API_BASE", "https://api.anthropic.com")
    try:
        payload = http_json(
            base + "/api/oauth/usage",
            token,
            headers={"anthropic-beta": "oauth-2025-04-20"},
        )
    except (
        Exception
    ) as exc:  # endpoint refused/expired token -> local estimate, never a crash
        now = _now()
        out = parse_claude_local(_claude_local_entries(now), now)
        out["plan"] = plan
        out["note"] = _scrub(f"{type(exc).__name__}: {out['note']}")
        return out
    return parse_claude_code(payload, plan=plan)


def collect_codex() -> dict:
    auth = read_json(Path(os.path.expanduser("~/.codex/auth.json")))
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    account = tokens.get("account_id") if isinstance(tokens, dict) else None
    if not token:
        return unknown(
            "codex",
            "no ChatGPT tokens in ~/.codex/auth.json; run `codex login`",
            status="unauthenticated",
        )
    base = os.environ.get("CODEX_BACKEND_BASE", "https://chatgpt.com/backend-api")
    headers = {"User-Agent": "codex-cli"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    try:
        return parse_codex(http_json(base + "/wham/usage", token, headers=headers))
    except (
        Exception
    ) as exc:  # endpoint refused -> the CLI's own last limit reading, labelled
        local = codex_local_rate_limits()
        windows = local.get("windows")
        observed = local.get("observed_at")
        if windows:
            return record(
                "codex",
                status="estimate",
                source="local",
                windows=windows,
                note=f"{type(exc).__name__}: usage endpoint unreachable; these are the "
                "limits the Codex CLI itself last recorded, not a live reading",
                observed_at=observed if isinstance(observed, datetime) else None,
            )
        return unknown(
            "codex",
            f"{type(exc).__name__}: usage endpoint unreachable and no Codex "
            "rollout in the last 7 days recorded a limit check",
        )


COLLECTORS = {
    "claude-code": collect_claude_code,
    "codex": collect_codex,
    "clinepass": collect_clinepass,
    "command-code": collect_command_code,
    "opencode-go": collect_opencode,
}


def document_totals(providers: list[dict]) -> dict:
    """The whole machine's activity in one place: every subscription's tokens counted together.

    Tokens are not comparable between providers and this is not a budget, so the surface labels it
    as activity. Only a provider that actually reported history contributes to it.
    """
    today = week = window = 0
    for row in providers:
        stats = row.get("stats")
        if not isinstance(stats, dict) or not stats.get("available"):
            continue
        current = stats.get("today")
        today += _int(current.get("tokens")) if isinstance(current, dict) else 0
        for day in stats.get("recent_days") or []:
            if isinstance(day, dict):
                week += _int(day.get("tokens"))
        for day in stats.get("daily") or []:
            if isinstance(day, dict):
                window += _int(day.get("tokens"))
    return {
        "tokens_today": today,
        "tokens_7d": week,
        "tokens_window": window,
        "providers_reported": len(providers),
        "providers_with_limits": sum(1 for row in providers if row.get("windows")),
        "providers_with_stats": sum(
            1
            for row in providers
            if isinstance(row.get("stats"), dict) and row["stats"].get("available")
        ),
    }


def collect_all(only: list[str] | None = None, *, local_stats: bool = True) -> dict:
    """Every collector runs behind its own guard: one provider's bad day is not an outage."""
    providers = []
    now = _now()
    for name, fn in COLLECTORS.items():
        if only and name not in only:
            continue
        started = time.monotonic()
        try:
            row = fn()
        except urllib.error.HTTPError as exc:
            row = unknown(name, f"HTTP {exc.code} from the usage endpoint")
        except urllib.error.URLError as exc:
            row = unknown(name, f"network unreachable ({type(exc.reason).__name__})")
        except Exception as exc:  # a parser bug must degrade, not take the run down
            row = unknown(name, f"{type(exc).__name__}: {exc}", status="error")
        row["elapsed_ms"] = _int((time.monotonic() - started) * 1000)
        row["stats"] = (
            stats_for(name, now)
            if local_stats
            else _no_stats("", {}, "local scan skipped (--no-stats)")
        )
        providers.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": _iso(now),
        "providers": providers,
        "totals": document_totals(providers),
    }


def write_cache(document: dict, path: Path = CACHE_PATH) -> Path:
    """Atomic: a reader with a FileView never sees half a document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ selftest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "usage"


def _items_of(payload: object) -> list:
    """The `items` array of a fixture, or [] — a fixture without one is a valid negative control."""
    items = payload.get("items") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def _selftest() -> int:
    """Parse one fixture per provider offline, then a malformed one (the negative control)."""
    failures: list[str] = []
    now = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(
            f"{'PASS' if cond else 'FAIL':<6} {label}{(' — ' + detail) if detail and not cond else ''}"
        )
        if not cond:
            failures.append(label)

    def fx(name: str) -> object:
        document = read_json(FIXTURES / name)
        if document is None:
            raise SystemExit(f"fixture missing or unreadable: {FIXTURES / name}")
        return document

    oc = parse_opencode(fx("opencode-go.json"), now)
    check(
        "opencode-go: ok with 5h/week/month",
        oc["status"] == "ok"
        and [w["name"] for w in oc["windows"]] == ["5h", "week", "month"],
        str(oc),
    )
    check(
        "opencode-go: month 33% used -> 67% left",
        oc["windows"][2]["used_pct"] == 33.0
        and oc["windows"][2]["remaining_pct"] == 67.0,
        str(oc),
    )

    clp = parse_clinepass_limits(fx("clinepass-limits.json"), now)
    check(
        "clinepass: usage-limits meter is reported as ok, not an estimate",
        clp["status"] == "ok"
        and [w["name"] for w in clp["windows"]] == ["5h", "week", "month"],
        str(clp),
    )
    check(
        "clinepass: percentUsed mapped to used/remaining",
        clp["windows"][1]["used_pct"] == 60.0
        and clp["windows"][2]["remaining_pct"] == 15.0,
        str(clp),
    )
    check(
        "clinepass: resets_in_s derived from resetsAt",
        clp["windows"][0]["resets_in_s"] == 9000,
        str(clp),
    )

    cl = parse_clinepass(
        fx("clinepass-plan.json"), _items_of(fx("clinepass-usages.json")), now
    )
    check(
        "clinepass fallback: derived windows are reported as an estimate, never as ok",
        cl["status"] == "estimate" and len(cl["windows"]) == 3,
        str(cl),
    )
    check(
        "clinepass fallback: 5h = 250/1000 of cap = 25%",
        cl["windows"][0]["used_pct"] == 25.0,
        str(cl),
    )
    check(
        "clinepass fallback: out-of-window charge excluded from 5h, counted in week",
        cl["windows"][1]["used_pct"] == 10.0,
        str(cl),
    )

    cc = parse_command_code(
        fx("command-code-credits.json"), fx("command-code-subscription.json"), now
    )
    check(
        "command-code: plan resolved from planId prefix", cc["plan"] == "GOAT", str(cc)
    )
    check(
        "command-code: weekly 28% from used/cap",
        cc["windows"][1]["used_pct"] == 28.0,
        str(cc),
    )
    check(
        "command-code: month from credit pool (70 - 60.3)/70",
        cc["windows"][2]["used_pct"] == 13.9,
        str(cc),
    )

    cd = parse_claude_code(fx("claude-code-usage.json"), plan="max", now=now)
    check(
        "claude-code: five_hour + seven_day",
        [w["name"] for w in cd["windows"]] == ["5h", "week"],
        str(cd),
    )
    check(
        "claude-code: resets_in_s derived from resets_at",
        cd["windows"][0]["resets_in_s"] == 9000,
        str(cd),
    )

    cx = parse_codex(fx("codex-usage.json"), now)
    check(
        "codex: window names from limit_window_seconds",
        [w["name"] for w in cx["windows"]] == ["5h", "week"],
        str(cx),
    )
    check("codex: plan_type surfaced", cx["plan"] == "plus", str(cx))

    # ---- local stats: the transcript readers, and the readings that must NOT be invented ----

    import shutil  # test-only helpers, kept out of the collector's import surface
    import tempfile

    transcripts = FIXTURES / "transcripts"
    scratch: list[Path] = []

    def staged(provider: str, only: str | None = None) -> Path:
        """Fixtures copied to a temp tree stamped with `now`.

        The scan filters files by mtime, so a fixture must never be judged by whatever date this
        repo happened to be checked out on.
        """
        target = Path(tempfile.mkdtemp(prefix="tmos-usage-fixture-")) / provider
        target.mkdir(parents=True)
        source = transcripts / provider
        for path in [source / only] if only else sorted(source.glob("*.jsonl")):
            shutil.copy2(path, target / path.name)
            os.utime(target / path.name, (now.timestamp(), now.timestamp()))
        scratch.append(target.parent)
        return target

    claude = _claude_contribution(transcripts / "claude" / "session-a.jsonl", now)
    claude_days = sorted(claude["days"])
    check(
        "claude transcript: two local days, cache tokens counted in the first",
        len(claude_days) == 2
        and claude["days"][claude_days[0]]["tokens"] == 1350
        and claude["days"][claude_days[1]]["tokens"] == 15,
        str(claude["days"]),
    )
    check(
        "claude transcript: a tool result is not a prompt",
        [claude["days"][day]["prompts"] for day in claude_days] == [1, 1],
        str(claude["days"]),
    )
    check(
        "claude transcript: a repeated message.id is counted once, not per content block",
        sum(claude["days"][day]["tokens"] for day in claude_days) == 1365,
        "msg_A is present on two rows; counting both would give 2715",
    )
    check(
        "claude transcript: tokens bucketed per model with the cache split kept",
        claude["models"]["claude-opus-5"]["cache_read_tokens"] == 1000
        and claude["models"]["claude-opus-5"]["cache_write_tokens"] == 200
        and claude["models"]["claude-sonnet-5"]["output_tokens"] == 5,
        str(claude["models"]),
    )

    codex = _codex_contribution(transcripts / "codex" / "rollout-1.jsonl", now)
    codex_tokens = sum(row["tokens"] for row in codex["days"].values())
    check(
        "codex transcript: per-turn deltas summed, never the session running total",
        codex_tokens == 920,
        f"got {codex_tokens}, expected 920 (=350+570, not 350+920=1270)",
    )
    check(
        "codex transcript: one task_started per turn is a prompt",
        sum(row["prompts"] for row in codex["days"].values()) == 2,
        str(codex["days"]),
    )
    check(
        "codex transcript: the model total agrees with the day total, cache not added twice",
        sum(row["total_tokens"] for row in codex["models"].values()) == codex_tokens,
        f"models={[row['total_tokens'] for row in codex['models'].values()]} day={codex_tokens}",
    )
    check(
        "codex transcript: model taken from turn_context",
        codex["models"]["gpt-6-sol"]["input_tokens"] == 800
        and codex["models"]["gpt-6-sol"]["cache_read_tokens"] == 400,
        str(codex["models"]),
    )

    codex_tree = staged("codex")
    limits = codex_local_rate_limits(now, codex_tree)
    check(
        "codex rollout: the CLI's own newest limit reading becomes 5h/week windows",
        [w["name"] for w in limits["windows"]] == ["5h", "week"]
        and limits["windows"][0]["used_pct"] == 26.0,
        str(limits),
    )

    claude_tree = staged("claude")
    state = claude_tree.parent / "state"
    first = scan_claude_stats(now, claude_tree, state)
    second = scan_claude_stats(now, claude_tree, state)
    model_ids = [row["id"] for row in first["models"]]
    day_tokens = [row["tokens"] for row in first["daily"]]
    check(
        "claude scan: end-to-end history, models ranked by size",
        first["available"]
        and first["totals"]["tokens"] == 1367
        and first["totals"]["sessions"] == 2
        and first["totals"]["active_days"] == 2
        and model_ids == ["claude-opus-5", "claude-sonnet-5"]
        and day_tokens == [1350, 17],
        str(first),
    )
    check(
        "claude scan: an unchanged transcript is never parsed twice",
        first["scan"]["files_scanned"] == 2
        and second["scan"]["files_scanned"] == 0
        and second["scan"]["files_cached"] == 2,
        f"{first['scan']} then {second['scan']}",
    )

    totals = document_totals(
        [
            {"provider": "a", "windows": [{}], "stats": first},
            {"provider": "b", "windows": [], "stats": _no_stats("", {}, "none")},
        ]
    )
    check(
        "document totals: only a provider with history contributes",
        totals["tokens_7d"] == 1367
        and totals["providers_with_stats"] == 1
        and totals["providers_with_limits"] == 1
        and totals["providers_reported"] == 2,
        str(totals),
    )

    # Negative controls: the readings that must NOT be invented.
    empty_tree = Path(tempfile.mkdtemp(prefix="tmos-usage-empty-"))
    scratch.append(empty_tree)
    empty = scan_claude_stats(now, empty_tree, empty_tree / "state")
    check(
        "negative control: an empty transcript tree reports available=false, not zeroes",
        (not empty["available"]) and not empty["daily"] and bool(empty["note"]),
        str(empty),
    )

    silent_tree = staged("codex", only="rollout-empty.jsonl")
    quiet = scan_codex_stats(now, silent_tree, silent_tree.parent / "state")
    check(
        "negative control: a token_count with info:null invents no day",
        (not quiet["available"]) and not quiet["daily"] and not quiet["models"],
        str(quiet),
    )
    quiet_limits = codex_local_rate_limits(now, silent_tree)
    check(
        "negative control: null rate limits yield no window",
        not quiet_limits["windows"],
        str(quiet_limits),
    )
    cline = stats_for("clinepass")
    check(
        "negative control: a provider with no readable transcript names the reason",
        (not cline["available"]) and "db" in cline["note"] and not cline["models"],
        str(cline),
    )

    for path in scratch:
        try:
            shutil.rmtree(path)
        except OSError:
            continue  # a temp tree that will not go away is the OS's problem, not a test failure

    # Negative control: every parser must degrade to "unknown" on garbage, not raise.
    garbage = fx("malformed.json")
    for label, out in (
        ("opencode-go", parse_opencode(garbage, now)),
        ("clinepass-limits", parse_clinepass_limits(garbage, now)),
        ("clinepass", parse_clinepass(garbage, garbage, now)),
        ("command-code", parse_command_code(garbage, garbage, now)),
        ("claude-code", parse_claude_code(garbage, None, now)),
        ("codex", parse_codex(garbage, now)),
    ):
        check(
            f"negative control: malformed {label} -> unknown, no exception",
            out["status"] == "unknown" and out["windows"] == [] and out["note"] != "",
            str(out),
        )

    # The run must survive a collector that throws.
    boom = dict(COLLECTORS)
    try:
        COLLECTORS.clear()
        COLLECTORS["explodes"] = lambda: (_ for _ in ()).throw(
            RuntimeError("planted collector failure")
        )
        doc = collect_all()
        check(
            "negative control: throwing collector -> status=error, run continues",
            len(doc["providers"]) == 1 and doc["providers"][0]["status"] == "error",
            str(doc),
        )
        check(
            "negative control: key-shaped text is scrubbed out of a note",
            "<redacted>" in _scrub("failed with sk-abcdefghijklmnopqrstuvwxyz012345"),
        )
    finally:
        COLLECTORS.clear()
        COLLECTORS.update(boom)

    print(f"selftest: {len(failures)} failure(s)")
    return 1 if failures else 0


# ------------------------------------------------------------------ cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Collect AI subscription usage windows for tmos.usage."
    )
    ap.add_argument(
        "--once", action="store_true", help="collect once and write the cache"
    )
    ap.add_argument(
        "--json", action="store_true", help="print the merged document on stdout"
    )
    ap.add_argument(
        "--only",
        action="append",
        choices=sorted(COLLECTORS),
        help="limit the run to one provider (repeatable)",
    )
    ap.add_argument(
        "--no-stats",
        action="store_true",
        help="skip the local transcript scan (limits only, no token history)",
    )
    ap.add_argument(
        "--selftest", action="store_true", help="parse fixtures offline; no network"
    )
    ap.add_argument(
        "--state-dir",
        default=None,
        help="write the cache here instead of the plugin's own state directory",
    )
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()
    if not args.once:
        ap.error("nothing to do: pass --once (optionally with --json) or --selftest")

    document = collect_all(args.only, local_stats=not args.no_stats)
    try:
        write_cache(document, configure_paths(args.state_dir))
    except OSError as exc:
        print(f"warning: could not write {CACHE_PATH}: {_scrub(exc)}", file=sys.stderr)
    if args.json:
        print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
