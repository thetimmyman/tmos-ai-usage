#!/usr/bin/env python3
"""tools/usage_collector.py — one honest picture of how much AI subscription is left.

Five built-in providers, five first-party sources, and any provider you define as data, in one
merged document under the tmosd state path (`~/.local/state/tmos/usage.json`, atomic write). The
shell plugin `shell/plugins/tmos.usage`
reads that file and does no network of its own (Omarchy-native rule N-series: a plugin displays,
a collector observes).

Per-provider record (the contract the QML reads):

    {"provider": "claude-code", "plan": "max", "source": "api|cli|local",
     "windows": [{"name": "5h"|"week"|"month", "used_pct": 23.0, "remaining_pct": 77.0,
                  "resets_at": "2026-09-21T22:30:00+00:00", "resets_in_s": 4200}],
     "observed_at": "...Z", "status": "ok|estimate|unauthenticated|unknown|error", "note": "...",
     "balance": {"remaining": 12.34, "funded": 20.0, "spent": 7.66, "currency": "USD",
                 "estimated": true},                             # prepaid providers only
     "label": "Acme AI",                             # definition providers only: the display name
     "definition": "~/.config/tmos-ai-usage/providers.d/acme.json",   # where a definition came from
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

    {"schema_version": 3, "observed_at": "...Z", "providers": [...],
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
  * A provider can also be *data*: a validated `providers.d/*.json` definition, written without
    touching this file. Built-in adapters always win their id, and a definition that is malformed,
    incomplete, or names a credential that is not there appears in the document anyway — non-ok,
    with a one-line reason — rather than quietly producing nothing.
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

  plus any provider you define as data: `providers.d/*.json`, one endpoint and one JSON document
  mapped onto the same windows/balance contract. The plugin ships none — the five above stay code
  on purpose, because each needs something this format cannot express (a second endpoint,
  pagination, an OAuth refresh, a plan lookup table, a local fallback, or local history). See
  "provider definitions" below for what a definition may say, and README.md for the same split as
  a table.

Usage:
  tools/usage_collector.py --once            collect once, write the cache
  tools/usage_collector.py --once --json     collect once, write the cache, print the document
  tools/usage_collector.py --once --no-stats limits only, skipping the local transcript scan
  tools/usage_collector.py --list-providers  every provider TMOS would collect and where it comes
                                             from; no network
  tools/usage_collector.py --probe <id>      read one provider now and show what came back and
                                             where it came from
  tools/usage_collector.py --selftest        parse fixtures offline (no network), incl. a
                                             malformed one that must degrade to "unknown"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 3
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
    base: Path,
    now: datetime,
    contribution,
    state_dir: Path | None = None,
    pattern: str = "*.jsonl",
) -> tuple[list[dict], dict]:
    """Per-file contributions for files touched inside the window, reusing the cache.

    `pattern` is the file kind a provider keeps its history in: JSONL rollouts for Claude, Codex and
    Command Code, plain JSON documents for Cline.
    """
    cache = _load_stats_cache(state_dir)
    entries = cache["files"]
    cutoff = (now - timedelta(days=STATS_DAYS)).timestamp()
    out: list[dict] = []
    seen: set[str] = set()
    scanned = cached = 0
    for path in _recent_files(base, pattern, cutoff):
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


def _command_code_contribution(path: Path, now: datetime) -> dict:
    """One Command Code rollout's day/model contribution.

    Command Code writes one JSONL per session under `~/.commandcode/projects/<slug>/`: a `session`
    row, then `message` rows, the assistant ones carrying
    `usage.{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens}` and a `model`. Its cache
    counters sit beside the input count the way Anthropic reports them, so a reply's total is the
    sum of the four and the cache tokens keep buckets of their own.

    `<uuid>.checkpoints.jsonl` sits next to a rollout and carries no usage at all, so it
    contributes nothing: a file that measured nothing must not create a day.
    """
    if ".checkpoints." in path.name:
        return {"days": {}, "models": {}}
    stats = _Stats("")
    session = path.name.split(".")[0]
    for row in _jsonl_rows(path):
        when = _parse_iso(row.get("timestamp"))
        if when is None:
            continue
        raw_message = row.get("message")
        message = raw_message if isinstance(raw_message, dict) else {}
        raw_usage = row.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else None
        if usage is not None:
            bucket = {
                "input_tokens": _num(usage.get("inputTokens")) or 0,
                "output_tokens": _num(usage.get("outputTokens")) or 0,
                "cache_read_tokens": _num(usage.get("cacheReadTokens")) or 0,
                "cache_write_tokens": _num(usage.get("cacheWriteTokens")) or 0,
            }
            bucket["total_tokens"] = _int(sum(bucket.values()))
            stats.add_tokens(
                when, session, row.get("model"), bucket, bucket["total_tokens"]
            )
        if str(message.get("role")) == "user":
            stats.add_prompt(when, session)
    return {"days": stats.days, "models": stats.models}


def scan_command_code_stats(
    now: datetime | None = None,
    root: Path | str | None = None,
    state_dir: Path | None = None,
) -> dict:
    now = now or _now()
    base = Path(root) if root else Path(os.path.expanduser("~/.commandcode/projects"))
    parts, scan = _scan_transcripts(base, now, _command_code_contribution, state_dir)
    stats = _absorb(parts, now)
    stats.source = "~/.commandcode/projects"
    stats.scan = scan
    return stats.finish(now)


def _opencode_db_path() -> Path:
    override = (os.environ.get("OPENCODE_DB") or "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path(os.path.expanduser("~/.local/share/opencode/opencode.db"))


def _stat_signature(paths: list[Path]) -> str:
    """A cache key that also moves when a SQLite WAL moves.

    A write lands in `-wal` long before the main database file's mtime changes, so a signature over
    the database alone would serve a stale read for as long as the WAL lives. `-shm` is deliberately
    NOT part of the signature: it is the shared-memory index and SQLite touches it even on a
    read-only open, which would make every read look like a change and defeat the cache.
    """
    parts = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            parts.append(f"{path.name}:absent")
            continue
        parts.append(f"{path.name}:{info.st_size}:{_int(info.st_mtime)}")
    return "|".join(parts)


def _opencode_contribution(path: Path, now: datetime) -> dict:
    """OpenCode's own database, read read-only: tokens and model per assistant reply.

    OpenCode keeps sessions in SQLite rather than in transcripts, and `message.data` is a JSON
    blob per message: assistant rows carry
    `tokens.{total,input,output,reasoning,cache.read,cache.write}` and a `modelID`, while the user
    rows are the prompts. The provider's own `total` is used as given rather than re-derived.

    Opened `mode=ro`: a running OpenCode holds the write lock and a WAL, and this never writes to
    the database. A row whose JSON does not parse is skipped, never guessed at.
    """
    stats = _Stats("")
    cutoff_ms = _int((now - timedelta(days=STATS_DAYS)).timestamp() * 1000)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only; never writes
    try:
        for session_id, created, data in con.execute(
            "SELECT session_id, time_created, data FROM message WHERE time_created >= ?",
            (cutoff_ms,),
        ):
            when = _from_epoch(created, "ms")
            if when is None:
                continue
            try:
                payload = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            session = str(session_id or "")
            raw_tokens = payload.get("tokens")
            tokens = raw_tokens if isinstance(raw_tokens, dict) else None
            model = payload.get("modelID")
            if not isinstance(model, str) or not model:
                raw_model = payload.get("model")
                model = (
                    raw_model.get("modelID") if isinstance(raw_model, dict) else None
                )
            if tokens is not None:
                raw_cache = tokens.get("cache")
                cache = raw_cache if isinstance(raw_cache, dict) else {}
                bucket = {
                    "input_tokens": _num(tokens.get("input")) or 0,
                    "output_tokens": _num(tokens.get("output")) or 0,
                    "cache_read_tokens": _num(cache.get("read")) or 0,
                    "cache_write_tokens": _num(cache.get("write")) or 0,
                }
                total = _int(tokens.get("total")) or _int(sum(bucket.values()))
                bucket["total_tokens"] = total
                stats.add_tokens(when, session, model, bucket, total)
            if str(payload.get("role")) == "user":
                stats.add_prompt(when, session)
    finally:
        con.close()
    return {"days": stats.days, "models": stats.models}


def scan_opencode_stats(
    now: datetime | None = None,
    root: Path | str | None = None,
    state_dir: Path | None = None,
) -> dict:
    """OpenCode history, cached on the database's own signature with its WAL included."""
    now = now or _now()
    db = Path(root) if root else _opencode_db_path()
    if not db.is_file():
        return _no_stats(
            "",
            {},
            f"OpenCode keeps its sessions in {db}, which does not exist on this machine",
        )
    cache = _load_stats_cache(state_dir)
    key = str(db)
    signature = _stat_signature([db, Path(f"{db}-wal")])
    entry = cache["files"].get(key)
    if isinstance(entry, dict) and entry.get("signature") == signature:
        contribution = entry
        scan = {"files_scanned": 0, "files_cached": 1}
    else:
        contribution = _json_safe(_opencode_contribution(db, now))
        contribution["signature"] = signature
        cache["files"][key] = contribution
        _save_stats_cache(cache, state_dir)
        scan = {"files_scanned": 1, "files_cached": 0}
    stats = _absorb([contribution], now)
    stats.source = "~/.local/share/opencode/opencode.db"
    stats.scan = scan
    return stats.finish(now)


def _cline_contribution(path: Path, now: datetime) -> dict:
    """One Cline session's day/model contribution.

    Cline writes two files per session: `<id>.json` (the run — provider, model, `started_at`, and a
    token rollup under `metadata.aggregateUsage`) and `<id>.messages.json` (the transcript, whose
    per-message `metrics` carry the same counters). The session file is the one that names the
    model, so it is the source here and its aggregate is taken exactly once: reading both files
    would count every reply twice.

    Cline records no prompt count, so none is claimed — a field it does not keep stays absent
    rather than being estimated.
    """
    if ".messages." in path.name:
        return {"days": {}, "models": {}}
    document = read_json(path)
    if not isinstance(document, dict):
        return {"days": {}, "models": {}}
    when = _parse_iso(document.get("started_at"))
    if when is None:
        return {"days": {}, "models": {}}
    raw_metadata = document.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_usage = metadata.get("aggregateUsage") or metadata.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    bucket = {
        "input_tokens": _num(usage.get("inputTokens")) or 0,
        "output_tokens": _num(usage.get("outputTokens")) or 0,
        "cache_read_tokens": _num(usage.get("cacheReadTokens")) or 0,
        "cache_write_tokens": _num(usage.get("cacheWriteTokens")) or 0,
    }
    bucket["total_tokens"] = _int(sum(bucket.values()))
    stats = _Stats("")
    session = str(document.get("session_id") or path.stem)
    stats.add_tokens(
        when, session, document.get("model"), bucket, bucket["total_tokens"]
    )
    return {"days": stats.days, "models": stats.models}


def scan_cline_stats(
    now: datetime | None = None,
    root: Path | str | None = None,
    state_dir: Path | None = None,
) -> dict:
    """Cline history, from its session documents (`.messages.json` siblings are ignored)."""
    now = now or _now()
    base = Path(root) if root else Path(os.path.expanduser("~/.cline/data/sessions"))
    parts, scan = _scan_transcripts(base, now, _cline_contribution, state_dir, "*.json")
    stats = _absorb(parts, now)
    stats.source = "~/.cline/data/sessions"
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


STATS_SCANNERS = {
    "claude-code": scan_claude_stats,
    "codex": scan_codex_stats,
    "command-code": scan_command_code_stats,
    "opencode-go": scan_opencode_stats,
    "clinepass": scan_cline_stats,
}

# Providers TMOS reads no history for. Empty today: all five keep something readable, and this stays
# as the honest fallback — a provider with no local history must say so rather than draw an empty
# chart, and a provider added later lands here until its reader exists.
NO_LOCAL_TRANSCRIPT: dict[str, str] = {}


def stats_for(provider: str, now: datetime | None = None, reason: str | None = None) -> dict:
    """Local history for one provider, or an explicit "none" that names the reason.

    `reason` lets a caller that knows better explain itself: a definition-based provider has no
    transcript reader by construction, and saying so is more useful than "no local transcript".
    """
    scanner = STATS_SCANNERS.get(provider)
    if scanner is None:
        return _no_stats(
            "", {}, reason or NO_LOCAL_TRANSCRIPT.get(provider, "no local transcript")
        )
    try:
        return scanner(now)
    except Exception as exc:  # a broken transcript must never take the run down
        return _no_stats(
            "", {}, f"{type(exc).__name__}: could not read local transcripts"
        )


# ------------------------------------------------- provider definitions (data, not code)
#
# A provider whose whole reading is "one endpoint, one JSON document, a path to a number" does not
# need a Python release: it needs a definition. Definitions live in `providers.d/*.json`, are
# validated strictly before they are used, and a definition that is malformed, incomplete, or names
# a credential that is not there still appears in the document — non-ok, with a one-line reason —
# exactly the way a failing built-in adapter does. It never silently produces nothing.
#
# What a definition CAN express (every case below was observed in a real provider, and each has a
# fixture under fixtures/providers.d):
#
#   percent field           claude-code   five_hour.utilization
#   used/cap ratio pair     command-code  windowLimits.weekly.{used,cap}   (a ratio, not a percent)
#   nested paths            any           "a.b.c" reaches response["a"]["b"]["c"]
#   object keyed by window  opencode-go   usage.{rolling,weekly,monthly}.percent
#   array keyed by a field  clinepass     data.limits[].{type,percentUsed}
#   ISO-8601 reset          claude-code   five_hour.resets_at
#   epoch reset             codex         reset_at (s); command-code resetAt (ms)
#   reset as a duration     codex         reset_after_seconds
#   window from a duration  codex         limit_window_seconds -> 5h | week | month
#   prepaid balance         command-code  credits.balance, with a currency
#
# What it CANNOT express, and therefore why each of the five built-ins stays code: a second
# endpoint (clinepass reads three, command-code two), pagination (clinepass' charge ledger), an
# OAuth refresh, a plan/allowance lookup table (command-code's planId -> credits), a local
# transcript fallback (claude-code, codex), an unverified unit reconciliation (clinepass'
# cap-vs-charge unit), and local token history (all five; `stats` is a scanned shape, not a mapped
# one). README.md carries this same split as a table. The format is an extension path, not a
# replacement.
#
# Security is a property of the schema, not of review: a definition may only NAME a credential —
# an environment variable, or a JSON file plus a dotted field. No key anywhere in this schema
# accepts a secret, `Authorization` may not be set from a definition, and unknown keys are rejected
# rather than ignored, so `"token": "sk-..."` fails validation *by name* instead of being quietly
# accepted. Every read goes through `http_json`, so only http(s) is ever opened, and every reason a
# definition produces is scrubbed by `_scrub` before it can reach the cache.

DEFINITION_VERSION = 1
DEFINITION_MAX_BYTES = 64 * 1024
_DEF_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_DOTTED_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_RESET_FORMATS = ("iso", "epoch_s", "epoch_ms", "duration_s")
# Keys that look like they hold a secret. Never legal anywhere in a definition.
_SECRET_KEYS = frozenset(
    {"token", "api_key", "apikey", "key", "secret", "password", "bearer", "authorization"}
)
_HEADER_DENY = frozenset({"authorization", "cookie", "proxy-authorization"})
_MISSING = object()

# What `--probe` says about a built-in adapter: where its credential is read from and which
# endpoint it asks. Display metadata only, never used to read anything — and the selftest asserts
# every built-in has an entry, so a sixth adapter fails the gate until this table is filled in too.
BUILTIN_SOURCES = {
    "claude-code": (
        "~/.claude/.credentials.json [claudeAiOauth.accessToken]",
        "https://api.anthropic.com/api/oauth/usage ($CLAUDE_API_BASE overrides it)",
    ),
    "codex": (
        "~/.codex/auth.json [tokens.access_token]",
        "https://chatgpt.com/backend-api/wham/usage ($CODEX_BACKEND_BASE overrides it)",
    ),
    "clinepass": (
        "$CLINE_API_KEY or ~/secrets/cline.env, else ~/.cline/data/settings/providers.json",
        "https://api.cline.bot/api/v1/users/me/plan/usage-limits ($CLINE_API_BASE overrides it)",
    ),
    "command-code": (
        "$COMMAND_CODE_API_KEY or ~/secrets/command-code.env, else ~/.commandcode/auth.json",
        "https://api.commandcode.ai/alpha/billing/credits ($COMMAND_CODE_API_BASE overrides it)",
    ),
    "opencode-go": (
        "~/.local/share/opencode/auth.json [opencode-go.key]",
        "https://opencode.ai/zen/go/v1/usage ($OPENCODE_GO_BASE overrides it)",
    ),
}


def _clean(value: object) -> str | None:
    """A trimmed non-empty string, or None. Every field in a definition is a string on purpose."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _dig(document: object, path: str):
    """Follow a dotted path through objects; `_MISSING` when absent. Never raises, never indexes."""
    node = document
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _display_path(path: Path) -> str:
    """A home-collapsed path, for anything a reason or a list has to show a user."""
    text = str(path)
    home = os.path.expanduser("~")
    if home and text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    return text


def _def_extra_keys(obj: dict, allowed: set) -> list[str]:
    """Every key a definition used that the schema does not define. Strict by design."""
    return sorted(str(key) for key in set(obj) - allowed)


def _def_extra_message(extra: list[str], where: str = "") -> str:
    prefix = f"{where}: " if where else ""
    named = [key for key in extra if key.lower() in _SECRET_KEYS]
    if named:
        return (
            f"{prefix}unsupported key {named[0]!r}: a definition names a credential, "
            "it never inlines one"
        )
    return f"{prefix}unsupported key(s) {', '.join(repr(key) for key in extra)}"


def _definition_error_id(path: Path) -> str:
    """A row id for a definition that could not be used.

    A provider id can never contain a colon, so this can never collide with a provider — including
    the one the broken definition was trying to be.
    """
    return f"definition:{path.stem}"


def _def_row(row_id: str, origin: str, reason: str, status: str = "error") -> dict:
    """A definition that cannot be used still reports, in a non-ok state, with a one-line reason."""
    row = unknown(row_id, reason, source="api", status=status)
    row["definition"] = origin
    return row


def definitions_dirs() -> list[Path]:
    """Where definitions are read from, in order. `TMOS_USAGE_PROVIDERS_DIR` replaces both.

    The plugin's own directory travels with the plugin (and is empty by default: the built-ins are
    code). The user directory is where a definition normally lives. Both are read in that order and
    the last one read wins a collision, so a user definition overrides a plugin one — and the
    definition that lost is reported, never dropped.
    """
    override = (os.environ.get("TMOS_USAGE_PROVIDERS_DIR") or "").strip()
    if override:
        return [Path(os.path.expanduser(part)) for part in override.split(os.pathsep) if part]
    return [
        Path(__file__).resolve().parent.parent / "providers.d",
        Path(os.path.expanduser("~/.config/tmos-ai-usage/providers.d")),
    ]


def _validate_window(raw: object, index: int) -> dict | str:
    """One `windows[]` entry: the spec, or a one-line reason it is not usable."""
    where = f"windows[{index}]"
    if not isinstance(raw, dict):
        return f"{where} must be an object"
    kind = _clean(raw.get("kind"))
    shape = {
        "percent": {"kind", "window", "window_from_duration", "path", "reset"},
        "ratio": {"kind", "window", "window_from_duration", "used", "cap", "reset"},
        "map": {"kind", "from", "keys", "percent", "reset", "status"},
        "list": {"kind", "from", "by", "keys", "percent", "reset", "status"},
    }
    if kind not in shape:
        return f"{where}.kind is {kind!r}; the supported mappings are {', '.join(sorted(shape))}"
    extra = _def_extra_keys(raw, shape[kind])
    if extra:
        return f"{where}: {_def_extra_message(extra)}"

    window_name = _clean(raw.get("window"))
    if window_name is not None and window_name not in WINDOW_ORDER:
        return f"{where}.window must be one of {', '.join(WINDOW_ORDER)}"
    from_duration = _clean(raw.get("window_from_duration"))
    if from_duration is not None and not _DOTTED_RE.match(from_duration):
        return f"{where}.window_from_duration must be a dotted path into the response"
    if kind in ("percent", "ratio"):
        if window_name is None and from_duration is None:
            return (
                f"{where} needs `window`, or `window_from_duration` for a provider that names a "
                "window by its length"
            )
        if window_name is not None and from_duration is not None:
            return f"{where} sets both `window` and `window_from_duration`; keep one"

    reset_spec = None
    reset = raw.get("reset")
    if reset is not None:
        if not isinstance(reset, dict):
            return f"{where}.reset must be an object like {{'path': 'resets_at', 'format': 'iso'}}"
        extra = _def_extra_keys(reset, {"path", "format"})
        if extra:
            return f"{where}.reset: {_def_extra_message(extra)}"
        path = _clean(reset.get("path"))
        fmt = _clean(reset.get("format"))
        if path is None or not _DOTTED_RE.match(path):
            return f"{where}.reset.path must be a dotted path into the response"
        if fmt not in _RESET_FORMATS:
            return f"{where}.reset.format must be one of {', '.join(_RESET_FORMATS)}"
        reset_spec = {"path": path, "format": fmt}

    if kind in ("percent", "ratio"):
        for key in (["path"] if kind == "percent" else ["used", "cap"]):
            value = _clean(raw.get(key))
            if value is None or not _DOTTED_RE.match(value):
                return f"{where}.{key} must be a dotted path into the response"
        spec = {
            "kind": kind,
            "window": window_name,
            "window_from_duration": from_duration,
            "reset": reset_spec,
        }
        if kind == "percent":
            spec["path"] = _clean(raw.get("path"))
        else:
            spec["used"] = _clean(raw.get("used"))
            spec["cap"] = _clean(raw.get("cap"))
        return spec

    container = _clean(raw.get("from"))
    if container is None or not _DOTTED_RE.match(container):
        return f"{where}.from must be a dotted path to the object or array of windows"
    raw_keys = raw.get("keys")
    if not isinstance(raw_keys, dict) or not raw_keys:
        return (
            f"{where}.keys must map the provider's own field to a window, "
            'for example {"five_hour": "5h"}'
        )
    keys: dict = {}
    for key, name in raw_keys.items():
        if not isinstance(key, str) or not key.strip():
            return f"{where}.keys has a non-string field name"
        if name not in WINDOW_ORDER:
            return f"{where}.keys[{key!r}] must be one of {', '.join(WINDOW_ORDER)}"
        keys[key] = name
    if len(set(keys.values())) != len(keys):
        return f"{where}.keys maps two provider fields onto the same window"
    percent = _clean(raw.get("percent"))
    if percent is None or not _DOTTED_RE.match(percent):
        return f"{where}.percent must be a dotted path inside each item"
    spec = {
        "kind": kind,
        "from": container,
        "keys": keys,
        "percent": percent,
        "reset": reset_spec,
        "status": None,
    }
    status_path = _clean(raw.get("status"))
    if status_path is not None:
        if not _DOTTED_RE.match(status_path):
            return f"{where}.status must be a dotted path inside each item"
        spec["status"] = status_path
    if kind == "list":
        by = _clean(raw.get("by"))
        if by is None or not _DOTTED_RE.match(by):
            return f"{where}.by must be a dotted path naming the field that selects the window"
        spec["by"] = by
    return spec


def validate_definition(document: object, origin: str) -> tuple[dict | None, str | None]:
    """Strict shape check of one `providers.d` file. Returns (spec, None) or (None, reason).

    Strict on purpose: an unknown key is an error, not something to skip. That is what makes
    "never inline a secret" a property of the format rather than a guideline for reviewers.
    """

    def bad(reason: str) -> tuple[None, str]:
        # The reason leads and the file trails: `load_definitions` appends the location, because
        # `_scrub` caps a note at 180 characters and a long install path must never eat the message.
        return None, reason

    if not isinstance(document, dict):
        return bad("not a JSON object")
    version = document.get("definition_version")
    if version != DEFINITION_VERSION:
        return bad(f"definition_version must be {DEFINITION_VERSION}, got {version!r}")
    provider_id = _clean(document.get("id"))
    if provider_id is None or not _DEF_ID_RE.match(provider_id):
        return bad('`id` must be a lowercase name such as "acme" or "acme-ai"')
    extra = _def_extra_keys(
        document,
        {
            "definition_version",
            "id",
            "label",
            "endpoint",
            "windows",
            "balance",
            "plan_path",
            "_source",
        },
    )
    if extra:
        return bad(_def_extra_message(extra))
    label = _clean(document.get("label"))
    if label is not None and len(label) > 60:
        return bad("`label` is longer than 60 characters")

    endpoint = document.get("endpoint")
    if not isinstance(endpoint, dict):
        return bad("`endpoint` must be an object")
    extra = _def_extra_keys(endpoint, {"url", "base_url_env", "credential", "headers", "timeout_s"})
    if extra:
        return bad(_def_extra_message(extra, "endpoint"))
    url = _clean(endpoint.get("url"))
    if url is None:
        return bad("`endpoint.url` is required")
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        return bad(f"`endpoint.url` must be http(s); {scheme or 'that'} is not read")
    base_url_env = _clean(endpoint.get("base_url_env"))
    if base_url_env is not None and not _ENV_NAME_RE.match(base_url_env):
        return bad(f"`endpoint.base_url_env` must be an env var name, got {base_url_env!r}")
    headers = endpoint.get("headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        return bad("`endpoint.headers` must be an object of string values")
    denied = sorted(key for key in headers if key.lower() in _HEADER_DENY)
    if denied:
        return bad(
            f"`endpoint.headers.{denied[0]}` may not be set by a definition; "
            "name a credential instead"
        )
    timeout = endpoint.get("timeout_s", TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        return bad("`endpoint.timeout_s` must be a number of seconds in (0, 30]")

    credential = endpoint.get("credential")
    if not isinstance(credential, dict):
        return bad("`endpoint.credential` is required: {kind: env|json_file|none, ...}")
    kind = _clean(credential.get("kind"))
    if kind == "none":
        if set(credential) != {"kind"}:
            return bad("credential kind `none` takes no other keys")
        resolved = {"kind": "none"}
    elif kind == "env":
        extra = _def_extra_keys(credential, {"kind", "name", "files"})
        if extra:
            return bad(_def_extra_message(extra, "credential"))
        name = _clean(credential.get("name"))
        if name is None or not _ENV_NAME_RE.match(name):
            return bad("credential kind `env` needs an uppercase env var name in `name`")
        files = credential.get("files", [])
        if not isinstance(files, list) or not all(
            isinstance(item, str) and item.strip() for item in files
        ):
            return bad("`credential.files` must be a list of env-file paths")
        resolved = {
            "kind": "env",
            "name": name,
            "files": [os.path.expanduser(item.strip()) for item in files],
        }
    elif kind == "json_file":
        extra = _def_extra_keys(credential, {"kind", "path", "field"})
        if extra:
            return bad(_def_extra_message(extra, "credential"))
        path = _clean(credential.get("path"))
        field = _clean(credential.get("field"))
        if path is None:
            return bad("credential kind `json_file` needs `path`")
        if field is None or not _DOTTED_RE.match(field):
            return bad('credential kind `json_file` needs a dotted `field`')
        resolved = {"kind": "json_file", "path": os.path.expanduser(path), "field": field}
    else:
        return bad(f"unsupported credential kind {kind!r} (env | json_file | none)")

    windows = document.get("windows")
    if not isinstance(windows, list) or not windows:
        return bad("`windows` must be a non-empty array")
    specs: list[dict] = []
    seen: set[str] = set()
    for index, raw_window in enumerate(windows):
        window_spec = _validate_window(raw_window, index)
        if isinstance(window_spec, str):
            return bad(window_spec)
        name = window_spec.get("window")
        if name is not None:
            if name in seen:
                return bad(f"windows[{index}] repeats window {name!r}")
            seen.add(name)
        specs.append(window_spec)

    balance_spec = None
    balance = document.get("balance")
    if balance is not None:
        if not isinstance(balance, dict):
            return bad("`balance` must be an object")
        extra = _def_extra_keys(
            balance, {"remaining", "funded", "currency", "currency_path", "estimated"}
        )
        if extra:
            return bad(_def_extra_message(extra, "balance"))
        remaining = _clean(balance.get("remaining"))
        if remaining is None or not _DOTTED_RE.match(remaining):
            return bad("`balance.remaining` must be a dotted path into the response")
        funded = _clean(balance.get("funded"))
        if funded is not None and not _DOTTED_RE.match(funded):
            return bad("`balance.funded` must be a dotted path into the response")
        currency = _clean(balance.get("currency")) or "USD"
        if not re.match(r"^[A-Za-z]{3}$", currency):
            return bad("`balance.currency` must be a three-letter code")
        currency_path = _clean(balance.get("currency_path"))
        if currency_path is not None and not _DOTTED_RE.match(currency_path):
            return bad("`balance.currency_path` must be a dotted path into the response")
        estimated = balance.get("estimated", False)
        if not isinstance(estimated, bool):
            return bad("`balance.estimated` must be true or false")
        balance_spec = {
            "remaining": remaining,
            "funded": funded,
            "currency": currency.upper(),
            "currency_path": currency_path,
            "estimated": estimated,
        }

    plan_path = _clean(document.get("plan_path"))
    if plan_path is not None and not _DOTTED_RE.match(plan_path):
        return bad("`plan_path` must be a dotted path into the response")

    return {
        "id": provider_id,
        "label": label,
        "origin": origin,
        "url": url,
        "base_url_env": base_url_env,
        "headers": dict(headers),
        "timeout_s": float(timeout),
        "credential": resolved,
        "windows": specs,
        "balance": balance_spec,
        "plan_path": plan_path,
    }, None
def _definition_reset(reset_spec: dict | None, anchor: object, now: datetime) -> datetime | None:
    """The reset a window spec points at, in whichever of the four forms it named."""
    if not reset_spec:
        return None
    raw = _dig(anchor, reset_spec["path"])
    if raw is _MISSING:
        return None
    fmt = reset_spec["format"]
    if fmt == "iso":
        return _parse_iso(raw)
    if fmt == "epoch_s":
        return _from_epoch(raw, "s")
    if fmt == "epoch_ms":
        return _from_epoch(raw, "ms")
    # `duration_s`: the provider says "in N seconds" instead of when. Resolved against this run's
    # clock, which is the only clock the provider left us.
    seconds = _num(raw)
    return now + timedelta(seconds=seconds) if seconds is not None else None


def _definition_notes(item: object, status_path: str | None, name: str, notes: list[str]) -> None:
    """A window the provider itself marked as throttled keeps that word, as parse_opencode does."""
    if not status_path:
        return
    raw = _dig(item, status_path)
    if isinstance(raw, str) and raw.strip() and raw.strip().lower() != "ok":
        notes.append(f"{name}:{_scrub(raw)}")


def _definition_window_name(wspec: dict, payload: dict) -> str | None:
    """The window a spec names — spelled out, or derived from a length the provider published."""
    if wspec["window_from_duration"]:
        return window_for_duration(_dig(payload, wspec["window_from_duration"]))
    return wspec["window"]


def _definition_rows(spec: dict, payload: dict, now: datetime) -> tuple[list[dict], list[str]]:
    """Map a response onto window rows. Every branch skips what it cannot read; nothing invented."""
    rows: list[dict] = []
    notes: list[str] = []
    seen: set[str] = set()

    for wspec in spec["windows"]:
        kind = wspec["kind"]
        if kind == "percent":
            name = _definition_window_name(wspec, payload)
            used = _num(_dig(payload, wspec["path"]))
            if name is None or name in seen or used is None:
                continue
            seen.add(name)
            rows.append(window(name, used, _definition_reset(wspec["reset"], payload, now), now))
            continue
        if kind == "ratio":
            name = _definition_window_name(wspec, payload)
            used = _num(_dig(payload, wspec["used"]))
            cap = _num(_dig(payload, wspec["cap"]))
            if name is None or name in seen or used is None or cap is None or cap <= 0:
                continue
            seen.add(name)
            rows.append(
                window(name, used / cap * 100.0, _definition_reset(wspec["reset"], payload, now), now)
            )
            continue
        container = _dig(payload, wspec["from"])
        pairs: list = []
        if kind == "map":
            if isinstance(container, dict):
                pairs = [(key, container.get(key)) for key in wspec["keys"]]
        elif isinstance(container, list):
            pairs = [(None, item) for item in container]
        for key, item in pairs:
            if not isinstance(item, dict):
                continue
            if key is not None:
                name = wspec["keys"][key]
            else:
                raw_by = _dig(item, wspec["by"])
                name = wspec["keys"].get(raw_by) if isinstance(raw_by, str) else None
            if name is None or name in seen:
                continue
            percent = _num(_dig(item, wspec["percent"]))
            if percent is None:
                continue
            seen.add(name)
            rows.append(window(name, percent, _definition_reset(wspec["reset"], item, now), now))
            _definition_notes(item, wspec["status"], name, notes)
    return rows, notes


def _definition_balance(spec: dict, payload: dict) -> dict | None:
    """The prepaid ledger a spec pointed at, or nothing at all — never a fabricated zero."""
    bspec = spec["balance"]
    if not bspec:
        return None
    remaining = _num(_dig(payload, bspec["remaining"]))
    if remaining is None:
        return None
    currency = bspec["currency"]
    raw_currency = _dig(payload, bspec["currency_path"]) if bspec["currency_path"] else _MISSING
    if isinstance(raw_currency, str) and re.match(r"^[A-Za-z]{3}$", raw_currency.strip()):
        currency = raw_currency.strip().upper()
    out = {
        "remaining": round(remaining, 2),
        "currency": currency,
        "estimated": bspec["estimated"],
    }
    funded = _num(_dig(payload, bspec["funded"])) if bspec["funded"] else None
    if funded is not None:
        out["funded"] = round(funded, 2)
        out["spent"] = round(max(0.0, funded - remaining), 2)
    return out


def _definition_reason(spec: dict, reason: str) -> str:
    """A definition's reason names its own file first: that is the file the user has to edit.

    It matters because `_scrub` cannot tell a long identifier from a token: an environment variable
    named in a reason is redacted when its name runs to 24 characters or more. Naming the file
    first keeps the reason actionable either way.
    """
    # Reason first, location last. `_scrub` truncates a note at 180 characters, so a long install
    # path at the tail can only ever clip the file, never the explanation.
    return f"{reason} (see {spec['origin']})"


def parse_definition(spec: dict, payload: object, now: datetime | None = None) -> dict:
    """One definition's response, mapped onto the window/balance contract. Pure: no I/O."""
    now = now or _now()
    if not isinstance(payload, dict):
        out = unknown(
            spec["id"],
            _definition_reason(spec, "the endpoint returned a JSON document that is not an object"),
        )
    else:
        rows, notes = _definition_rows(spec, payload, now)
        if not rows:
            out = unknown(
                spec["id"],
                _definition_reason(spec, "no configured window carried a number in the response"),
            )
        else:
            out = record(
                spec["id"], status="ok", windows=rows, note="; ".join(notes), observed_at=now
            )
            if spec["plan_path"]:
                raw_plan = _dig(payload, spec["plan_path"])
                if isinstance(raw_plan, str) and raw_plan.strip():
                    out["plan"] = _scrub(raw_plan)
            balance = _definition_balance(spec, payload)
            if balance:
                out["balance"] = balance
    # A definition's row always says how to name it and which file it came from, at every status:
    # a broken definition is exactly the case where a user needs to see where it came from.
    if spec["label"]:
        out["label"] = spec["label"]
    out["definition"] = spec["origin"]
    return out


def _credential_text(credential: dict) -> str:
    """How a credential is described without ever describing its value."""
    kind = credential["kind"]
    if kind == "none":
        return "none (public endpoint)"
    if kind == "env":
        files = [_display_path(Path(item)) for item in credential["files"]]
        return " or ".join([f"${credential['name']}"] + files)
    return f"{_display_path(Path(credential['path']))} [{credential['field']}]"


def resolve_credential(credential: dict) -> tuple[str | None, str | None]:
    """The token a definition named, or a one-line reason it is not there.

    The reason names the *location* that was checked and never a value, so a definition that
    cannot authenticate is explained in the document with no secret anywhere near it.
    """
    kind = credential["kind"]
    if kind == "none":
        return None, None
    if kind == "env":
        token = env_or_file_key(credential["name"], [Path(item) for item in credential["files"]])
        if not token:
            where = ", ".join(_display_path(Path(item)) for item in credential["files"])
            return None, f"${credential['name']} is not set and is not in {where or 'any env file'}"
        return token, None
    document = read_json(Path(credential["path"]))
    if document is None:
        return None, f"{_display_path(Path(credential['path']))} is missing or is not JSON"
    value = _dig(document, credential["field"])
    if not isinstance(value, str) or not value.strip():
        return None, f"{_display_path(Path(credential['path']))} carries no {credential['field']}"
    return value.strip(), None


def _definition_url(spec: dict) -> str:
    """The URL to read: the definition's own, with its host swapped when `base_url_env` is set.

    The same knob the built-in adapters have, for the same reason: the gate points every endpoint
    at a dead local port, so a run can be *proven* not to touch the network. The path is kept.
    """
    base_env = spec["base_url_env"]
    override = (os.environ.get(base_env) or "").strip() if base_env else ""
    if not override:
        return spec["url"]
    original = urllib.parse.urlsplit(spec["url"])
    replacement = urllib.parse.urlsplit(override)
    return urllib.parse.urlunsplit(
        (replacement.scheme, replacement.netloc, original.path, original.query, original.fragment)
    )


def collect_definition(spec: dict) -> dict:
    """One definition: resolve the credential it named, read its one endpoint, map the document."""
    token, reason = resolve_credential(spec["credential"])
    if reason is not None:
        return _definition_failure(spec, reason, "unauthenticated")
    return parse_definition(
        spec,
        http_json(
            _definition_url(spec),
            token,
            headers=spec["headers"] or None,
            timeout=spec["timeout_s"],
        ),
    )


def _definition_failure(spec: dict, reason: str, status: str) -> dict:
    """A definition's read failed: a row that says so, never a provider that quietly disappears."""
    row = unknown(spec["id"], _definition_reason(spec, reason), status=status)
    if spec["label"]:
        row["label"] = spec["label"]
    row["definition"] = spec["origin"]
    return row


def load_definitions(dirs: list[Path] | None = None) -> tuple[list[dict], list[dict]]:
    """Read every `providers.d/*.json`: (usable specs, one error row per definition not usable).

    Nothing is dropped in silence. Malformed JSON, a missing key, an unsupported mapping, an
    inlined secret, and an id a built-in already owns each become a row in the document with a
    one-line reason. Built-in adapters always win their id; between definition files the last one
    read wins, and the one that lost is reported.
    """
    specs: dict[str, dict] = {}
    rows: list[dict] = []
    for directory in dirs if dirs is not None else definitions_dirs():
        try:
            files = sorted(path for path in directory.glob("*.json") if path.is_file())
        except OSError:
            continue
        for path in files:
            origin = _display_path(path)
            error_id = _definition_error_id(path)
            try:
                if path.stat().st_size > DEFINITION_MAX_BYTES:
                    rows.append(
                        _def_row(
                            error_id,
                            origin,
                            f"larger than {DEFINITION_MAX_BYTES} bytes; not read (see {origin})",
                        )
                    )
                    continue
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                rows.append(
                    _def_row(
                        error_id, origin, f"unreadable ({type(exc).__name__}) (see {origin})"
                    )
                )
                continue
            try:
                document = json.loads(raw)
            except ValueError as exc:
                rows.append(
                    _def_row(error_id, origin, f"not JSON ({_scrub(exc)}) (see {origin})")
                )
                continue
            spec, error = validate_definition(document, origin)
            if spec is None:
                reason = error or "invalid definition"
                rows.append(_def_row(error_id, origin, f"{reason} (see {origin})"))
                continue
            provider_id = spec["id"]
            if provider_id in COLLECTORS:
                rows.append(
                    _def_row(
                        error_id,
                        origin,
                        f"ignored — the built-in adapter {provider_id!r} wins on an id "
                        f"collision (see {origin})",
                    )
                )
                continue
            holder = specs.get(provider_id)
            if holder is not None:
                rows.append(
                    _def_row(
                        _definition_error_id(Path(holder["origin"])),
                        holder["origin"],
                        f"ignored — a definition read later defines id {provider_id!r} and "
                        f"wins (see {holder['origin']})",
                    )
                )
            specs[provider_id] = spec
    return [specs[key] for key in sorted(specs)], rows
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
    """Every collector runs behind its own guard: one provider's bad day is not an outage.

    Definition-based providers are collected the same way, and guarded the same way: a definition
    that cannot be loaded, and one whose endpoint cannot be read, each contribute a row carrying a
    one-line reason instead of disappearing from the document.
    """
    providers = []
    now = _now()
    definition_reason = "definition-based provider: TMOS reads no local transcript for it"
    specs, definition_rows = load_definitions()
    for row in definition_rows:
        if only and row["provider"] not in only:
            continue
        row["elapsed_ms"] = 0
        row["stats"] = (
            stats_for(row["provider"], now, definition_reason)
            if local_stats
            else _no_stats("", {}, "local scan skipped (--no-stats)")
        )
        providers.append(row)
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
    for spec in specs:
        if only and spec["id"] not in only:
            continue
        started = time.monotonic()
        try:
            row = collect_definition(spec)
        except urllib.error.HTTPError as exc:
            row = _definition_failure(
                spec, f"HTTP {exc.code} from the definition endpoint", "unknown"
            )
        except urllib.error.URLError as exc:
            row = _definition_failure(
                spec, f"network unreachable ({type(exc.reason).__name__})", "unknown"
            )
        except Exception as exc:  # a definition must never take the run down
            row = _definition_failure(spec, f"{type(exc).__name__}: {exc}", "error")
        row["elapsed_ms"] = _int((time.monotonic() - started) * 1000)
        row["stats"] = (
            stats_for(spec["id"], now, definition_reason)
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
    """Every fixture, offline: the five parsers, the malformed ones, and the definitions.

    Hermetic by construction. `TMOS_USAGE_PROVIDERS_DIR` is pinned to a directory that does not
    exist for the whole run, so a definition installed on this machine can neither change what an
    assertion sees nor be fetched — the parsers' half of this test must not touch the network, and
    neither must anything else. The definition tests below point that variable at their own
    fixtures and restore it to this pinned value afterwards.
    """
    os.environ["TMOS_USAGE_PROVIDERS_DIR"] = str(FIXTURES / "selftest-pinned-no-definitions")
    failures: list[str] = []
    now = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(
            f"{'PASS' if cond else 'FAIL':<6} {label}{(' — ' + detail) if detail and not cond else ''}"
        )
        if not cond:
            failures.append(label)

    def load_fixture(path: Path) -> object:
        document = read_json(path)
        if document is None:
            raise SystemExit(f"fixture missing or unreadable: {path}")
        return document

    def fx(name: str) -> object:
        return load_fixture(FIXTURES / name)

    def def_dir(name: str) -> Path:
        return FIXTURES.parent / name

    def resp(name: str) -> object:
        return load_fixture(def_dir("providers.responses") / name)

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
        for path in (
            [source / only]
            if only
            else sorted(p for p in source.glob("*") if p.is_file())
        ):
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

    command = _command_code_contribution(
        transcripts / "command-code" / "aaaa-bbbb.jsonl", now
    )
    command_tokens = sum(row["tokens"] for row in command["days"].values())
    check(
        "command-code rollout: reply usage summed, prompts counted from user rows",
        command_tokens == 1820
        and sum(row["prompts"] for row in command["days"].values()) == 1,
        str(command),
    )
    check(
        "command-code rollout: input/output/cache buckets stay separate",
        command["models"]["deepseek/deepseek-v4-flash"]["input_tokens"] == 1200
        and command["models"]["deepseek/deepseek-v4-flash"]["cache_read_tokens"] == 500
        and command["models"]["deepseek/deepseek-v4-flash"]["total_tokens"] == 1820,
        str(command["models"]),
    )
    check(
        "negative control: a checkpoints file contributes no day and no model",
        _command_code_contribution(
            transcripts / "command-code" / "aaaa-bbbb.checkpoints.jsonl", now
        )
        == {"days": {}, "models": {}},
        "",
    )

    command_tree = staged("command-code")
    command_stats = scan_command_code_stats(
        now, command_tree, command_tree.parent / "state"
    )
    check(
        "command-code scan: end-to-end history from a rollout tree",
        command_stats["available"]
        and command_stats["totals"]["tokens"] == 1820
        and command_stats["totals"]["prompts"] == 1
        and command_stats["totals"]["sessions"] == 1
        and command_stats["models"][0]["id"] == "deepseek/deepseek-v4-flash",
        str(command_stats),
    )

    # OpenCode keeps its sessions in SQLite. The fixture is built here rather than committed as a
    # binary blob, so the reader is proven against the real table shape.
    opencode_db = Path(tempfile.mkdtemp(prefix="tmos-usage-opencode-")) / "opencode.db"
    scratch.append(opencode_db.parent)
    con = sqlite3.connect(str(opencode_db))
    con.execute(
        "CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, "
        "time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL)"
    )
    stamp = _int(now.timestamp() * 1000)
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m1", "ses_1", stamp, stamp, json.dumps({"role": "user"})),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        (
            "m2",
            "ses_1",
            stamp,
            stamp,
            json.dumps(
                {
                    "role": "assistant",
                    "modelID": "gpt-5-nano",
                    "tokens": {
                        "total": 11882,
                        "input": 11651,
                        "output": 42,
                        "reasoning": 189,
                        "cache": {"read": 100, "write": 0},
                    },
                }
            ),
        ),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        (
            "m3",
            "ses_1",
            stamp,
            stamp,
            json.dumps({"role": "assistant", "tokens": None}),
        ),
    )
    con.commit()
    con.close()
    opencode = scan_opencode_stats(now, opencode_db, opencode_db.parent / "state")
    check(
        "opencode: the provider's own token total is used, not re-derived",
        opencode["available"] and opencode["totals"]["tokens"] == 11882,
        str(opencode),
    )
    check(
        "opencode: user rows are prompts, and a reply with no tokens invents nothing",
        opencode["totals"]["prompts"] == 1 and opencode["totals"]["active_days"] == 1,
        str(opencode),
    )
    check(
        "opencode: model and cache buckets read from the message payload",
        opencode["models"][0]["id"] == "gpt-5-nano"
        and opencode["models"][0]["cache_read_tokens"] == 100,
        str(opencode["models"]),
    )
    check(
        "opencode: a second read of an unchanged database is served from the cache",
        scan_opencode_stats(now, opencode_db, opencode_db.parent / "state")["scan"][
            "files_cached"
        ]
        == 1,
        "the WAL-aware signature must stay stable across reads",
    )

    cline = _cline_contribution(
        transcripts / "clinepass" / "1790017816235_k9ukm.json", now
    )
    cline_tokens = sum(row["tokens"] for row in cline["days"].values())
    check(
        "cline session: the usage rollup is taken once, not summed with its aggregate twin",
        cline_tokens == 5749,
        f"got {cline_tokens}; summing usage + aggregateUsage would give 11498",
    )
    check(
        "cline session: model named, and no prompt count is claimed for a field it lacks",
        cline["models"]["deepseek-v4.1-flash"]["total_tokens"] == 5749
        and not any(row["prompts"] for row in cline["days"].values()),
        str(cline),
    )
    check(
        "negative control: a Cline transcript file contributes nothing, so nothing double counts",
        _cline_contribution(
            transcripts / "clinepass" / "1790017816235_k9ukm.messages.json", now
        )
        == {"days": {}, "models": {}},
        "",
    )

    cline_tree = staged("clinepass")
    cline_stats = scan_cline_stats(now, cline_tree, cline_tree.parent / "state")
    check(
        "cline scan: end-to-end history from a session directory",
        cline_stats["available"]
        and cline_stats["totals"]["tokens"] == 5749
        and cline_stats["totals"]["sessions"] == 1,
        str(cline_stats),
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
    # Every shipped provider now has a reader, so the fallback is proven with an id that has none:
    # a provider TMOS cannot read must say so, never draw an empty chart.
    unreadable = stats_for("no-such-provider")
    check(
        "negative control: a provider with no reader names the reason and claims no history",
        (not unreadable["available"])
        and unreadable["note"] == "no local transcript"
        and not unreadable["models"],
        str(unreadable),
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

    # ------------------------------------------ provider definitions (data, not code)
    # The format is data, so its proof is data: one fixture per mapping the format claims (parsed
    # offline against a response fixture), and one fixture per way a definition can fail. Every
    # failure must surface as a row with a reason — a definition that silently produces nothing is
    # the one outcome this layer has to make impossible.

    specs, load_errors = load_definitions([def_dir("providers.d")])
    by_id = {spec["id"]: spec for spec in specs}
    check(
        "definitions: every example under fixtures/providers.d loads",
        sorted(by_id) == ["acme", "nimbus", "orbital", "quarry", "relay"],
        f"loaded={sorted(by_id)} errors={[row['note'] for row in load_errors]}",
    )
    check(
        "definitions: a directory that does not exist loads nothing and raises nothing",
        load_definitions([def_dir("no-such-directory")]) == ([], []),
        "a missing providers.d must be normal, not an error",
    )
    check(
        "definitions: the selftest is hermetic — a definition installed here changes nothing",
        load_definitions() == ([], []),
        str(load_definitions()),
    )
    check(
        "definitions: every built-in adapter has a --probe provenance entry",
        set(BUILTIN_SOURCES) == set(COLLECTORS),
        str(sorted(set(BUILTIN_SOURCES) ^ set(COLLECTORS))),
    )

    acme = parse_definition(by_id["acme"], resp("acme.json"), now)
    check(
        "definition percent: nested paths become 5h/week with their ISO resets",
        acme["status"] == "ok"
        and [w["name"] for w in acme["windows"]] == ["5h", "week"]
        and acme["windows"][0]["used_pct"] == 23.0
        and acme["windows"][0]["remaining_pct"] == 77.0
        and acme["windows"][0]["resets_in_s"] == 9000,
        str(acme),
    )
    check(
        "definition balance: remaining/funded/spent, labelled as the definition asked",
        acme.get("balance", {}).get("remaining") == 12.34
        and acme["balance"]["funded"] == 20.0
        and acme["balance"]["spent"] == 7.66
        and isinstance(acme["balance"]["estimated"], bool)
        and acme["balance"]["estimated"],
        str(acme.get("balance")),
    )
    check(
        "definition: label, plan and origin ride along for the surface and for --probe",
        acme.get("label") == "Acme AI"
        and acme["definition"].endswith("providers.d/acme.json")
        and acme.get("plan") == "Acme Pro",
        f"{acme.get('label')} {acme['definition']} {acme.get('plan')}",
    )

    nimbus = parse_definition(by_id["nimbus"], resp("nimbus.json"), now)
    check(
        "definition ratio: a used/cap pair becomes a percentage, not a percent off the wire",
        nimbus["status"] == "ok"
        and [w["name"] for w in nimbus["windows"]] == ["week"]
        and nimbus["windows"][0]["used_pct"] == 28.0,
        str(nimbus),
    )
    check(
        "definition ratio: an epoch-millisecond reset is read as a time",
        nimbus["windows"][0]["resets_at"] == "2026-09-24T20:00:00Z"
        and nimbus["windows"][0]["resets_in_s"] == 259200,
        str(nimbus["windows"][0]),
    )

    orbital = parse_definition(by_id["orbital"], resp("orbital.json"), now)
    check(
        "definition map: an object keyed by the provider's own names maps onto the windows",
        orbital["status"] == "ok"
        and [w["name"] for w in orbital["windows"]] == ["5h", "week", "month"]
        and orbital["windows"][2]["used_pct"] == 33.0,
        str(orbital),
    )
    check(
        "definition map: a window the provider itself marked throttled keeps that word",
        orbital["note"] == "week:rate_limited",
        orbital["note"],
    )

    quarry = parse_definition(by_id["quarry"], resp("quarry.json"), now)
    check(
        "definition list: an array selected by one of its own fields maps onto windows",
        quarry["status"] == "ok"
        and [w["name"] for w in quarry["windows"]] == ["5h", "week", "month"]
        and quarry["windows"][1]["used_pct"] == 60.0,
        str(quarry),
    )

    relay = parse_definition(by_id["relay"], resp("relay.json"), now)
    check(
        "definition window_from_duration: a length the provider published names the window",
        [w["name"] for w in relay["windows"]] == ["5h", "week"],
        str(relay),
    )
    check(
        "definition reset as a duration: resolved against this run's clock",
        relay["windows"][0]["resets_in_s"] == 852 and relay.get("plan") == "plus",
        str(relay["windows"][0]),
    )

    # Negative controls. Each must fail visibly: an unusable definition that produces no row at all
    # is the bug this format cannot be allowed to have.
    reject_specs, reject_rows = load_definitions([def_dir("providers.reject.d")])
    reject_by_id = {spec["id"]: spec for spec in reject_specs}
    problems = " | ".join(row["note"] for row in reject_rows)
    check(
        "negative control: a definition that is not JSON -> a row with a reason",
        "definition:broken" in [row["provider"] for row in reject_rows]
        and "not JSON" in problems,
        problems,
    )
    check(
        "negative control: an unsupported mapping -> a row naming the key",
        "windows[0].kind is 'graphql'" in problems,
        problems,
    )
    check(
        "negative control: a secret inlined in a definition -> refused by name",
        "unsupported key 'token'" in problems and "never inlines one" in problems,
        problems,
    )
    check(
        "negative control: a definition reusing a built-in id loses and says so",
        "wins on an id collision" in problems,
        problems,
    )
    check(
        "negative control: a malformed response shape -> unknown, never a fabricated window",
        parse_definition(by_id["acme"], fx("malformed.json"), now)["status"] == "unknown",
        str(parse_definition(by_id["acme"], fx("malformed.json"), now)),
    )
    absent = collect_definition(reject_by_id["absent"]) if "absent" in reject_by_id else {}
    check(
        "negative control: a credential that is not there -> unauthenticated, and it says which",
        absent.get("status") == "unauthenticated"
        and "ABSENT_PROVIDER_API_KEY" in absent.get("note", "")
        and absent.get("definition", "").endswith("missing-credential.json"),
        str(absent),
    )
    # The reason has to survive the scrubber. `_scrub` caps a note at 180 characters, and the
    # plugin's install path is longer than a checkout's, so a reason that trails its message behind
    # a path loses the message exactly where a user needs it most.
    long_origin = "~/" + "d" * 90 + "/providers.d/inline-secret.json"
    trimmed = _scrub(
        "unsupported key 'token': a definition names a credential, it never inlines one "
        f"(see {long_origin})"
    )
    check(
        "definitions: a reason leads with the message, so a long install path cannot eat it",
        "never inlines one" in trimmed and len(trimmed) <= 180,
        trimmed,
    )

    # A definition is a provider end to end, not only inside the mapping: `--only` selects it, the
    # run guards it, and it carries the same stats and timing block every other row carries.
    previous_dirs = os.environ.get("TMOS_USAGE_PROVIDERS_DIR")
    try:
        os.environ["TMOS_USAGE_PROVIDERS_DIR"] = str(def_dir("providers.reject.d"))
        refused_doc = collect_all(["refused"], local_stats=False)
    finally:
        if previous_dirs is None:
            os.environ.pop("TMOS_USAGE_PROVIDERS_DIR", None)
        else:
            os.environ["TMOS_USAGE_PROVIDERS_DIR"] = previous_dirs
    refused_row = refused_doc["providers"][0] if refused_doc["providers"] else {}
    check(
        "negative control: a refused definition endpoint -> a reason, never an ok reading",
        refused_doc["schema_version"] == SCHEMA_VERSION
        and len(refused_doc["providers"]) == 1
        and refused_row.get("provider") == "refused"
        and refused_row.get("status") not in (None, "ok")
        and bool(refused_row.get("note"))
        and "stats" in refused_row
        and "elapsed_ms" in refused_row,
        str(refused_doc["providers"]),
    )

    print(f"selftest: {len(failures)} failure(s)")
    return 1 if failures else 0


# ------------------------------------------------------------------ cli


def _window_names(spec: dict) -> list[str]:
    """The windows a definition says it maps, for `--list-providers`."""
    names: list[str] = []
    for wspec in spec["windows"]:
        if wspec["kind"] in ("percent", "ratio"):
            declared = [wspec["window"] or "by length"]
        else:
            declared = sorted(
                set(wspec["keys"].values()), key=lambda name: WINDOW_ORDER.get(name, 99)
            )
        for name in declared:
            if name not in names:
                names.append(name)
    return names


def _list_providers(specs: list[dict], definition_rows: list[dict], as_json: bool) -> int:
    """Everything TMOS would collect, and where each one comes from. Never touches the network.

    This is the command a user runs while writing a definition: a definition that cannot be used
    is listed with its reason instead of quietly not appearing.
    """
    rows = []
    for provider_id in sorted(COLLECTORS):
        credential, endpoint = BUILTIN_SOURCES.get(provider_id, ("", ""))
        rows.append(
            {
                "provider": provider_id,
                "kind": "built-in",
                "origin": "collector/usage_collector.py",
                "credential": credential,
                "endpoint": endpoint,
                "windows": "the provider's own",
                "problem": "",
            }
        )
    for spec in specs:
        rows.append(
            {
                "provider": spec["id"],
                "kind": "definition",
                "origin": spec["origin"],
                "credential": _credential_text(spec["credential"]),
                "endpoint": spec["url"],
                "windows": ",".join(_window_names(spec)),
                "problem": "",
            }
        )
    for row in definition_rows:
        rows.append(
            {
                "provider": row["provider"],
                "kind": "problem",
                "origin": row.get("definition", ""),
                "credential": "",
                "endpoint": "",
                "windows": "",
                "problem": row["note"],
            }
        )
    if as_json:
        print(json.dumps({"providers": rows}, indent=2))
        return 0
    for row in rows:
        print(f"{row['kind']:<10} {row['provider']:<20} {row['origin']}")
        for label, value in (
            ("credential", row["credential"]),
            ("endpoint", f"GET {row['endpoint']}" if row["endpoint"] else ""),
            ("windows", row["windows"]),
            ("problem", row["problem"]),
        ):
            if value:
                print(f"{'':<10} {'':<20} {label + ':':<11}{value}")
    print(
        f"\n{len(COLLECTORS)} built-in adapter(s), {len(specs)} usable definition(s), "
        f"{len(definition_rows)} definition(s) that cannot be used."
    )
    return 0


def _print_probe(row: dict) -> None:
    """What was read, in the shape the collector stores it: a reason where a number is missing."""
    print(f"status:     {row['status']}" + (f"  ({row['note']})" if row["note"] else ""))
    if row.get("plan"):
        print(f"plan:       {row['plan']}")
    for item in row["windows"]:
        reset = (
            f"resets {item['resets_at']} (in {item['resets_in_s']}s)"
            if item["resets_in_s"] is not None
            else "no reset published"
        )
        print(
            f"window      {item['name']:<5} {item['used_pct']:>5.1f}% used, "
            f"{item['remaining_pct']:>5.1f}% left, {reset}"
        )
    if not row["windows"]:
        print("windows     none")
    balance = row.get("balance")
    if isinstance(balance, dict):
        funded = f" of {balance['funded']}" if "funded" in balance else ""
        tail = " (estimated)" if balance.get("estimated") else ""
        print(f"balance     {balance['remaining']}{funded} {balance.get('currency', 'USD')}{tail}")


def _probe(
    provider_id: str, specs: list[dict], definition_rows: list[dict], as_json: bool
) -> int:
    """Read one provider now, and say what came back and where it came from.

    A provider in a non-ok state is a successful probe: the reason *is* the answer. Only an id that
    does not exist at all exits non-zero.
    """
    spec = next((item for item in specs if item["id"] == provider_id), None)
    if spec is None and provider_id not in COLLECTORS:
        broken = next((row for row in definition_rows if row["provider"] == provider_id), None)
        if broken is None:
            print(
                f"no provider {provider_id!r}; --list-providers shows what there is",
                file=sys.stderr,
            )
            return 2
        print(f"provider:   {provider_id}")
        print(f"origin:     definition {broken.get('definition', '')}")
        print(f"status:     {broken['status']}")
        print(f"reason:     {broken['note']}")
        return 0
    if spec is not None:
        print(f"provider:   {spec['id']}")
        print(f"origin:     definition {spec['origin']}")
        print(f"label:      {spec['label'] or spec['id']}")
        print(f"credential: {_credential_text(spec['credential'])}")
        # The token is deliberately dropped on the floor: `--probe` reports whether a credential is
        # readable, never the credential itself.
        _, reason = resolve_credential(spec["credential"])
        print(f"            -> {'readable' if reason is None else reason}")
        print(f"endpoint:   GET {_definition_url(spec)}")
        try:
            row = collect_definition(spec)
        except urllib.error.HTTPError as exc:
            row = _definition_failure(spec, f"HTTP {exc.code} from the endpoint", "unknown")
        except urllib.error.URLError as exc:
            row = _definition_failure(
                spec, f"network unreachable ({type(exc.reason).__name__})", "unknown"
            )
        except Exception as exc:
            row = _definition_failure(spec, f"{type(exc).__name__}: {exc}", "error")
    else:
        credential, endpoint = BUILTIN_SOURCES.get(provider_id, ("", ""))
        print(f"provider:   {provider_id}")
        print("origin:     built-in adapter in collector/usage_collector.py")
        print(f"credential: {credential}")
        print(f"endpoint:   GET {endpoint}")
        try:
            row = COLLECTORS[provider_id]()
        except Exception as exc:
            row = unknown(provider_id, f"{type(exc).__name__}: {exc}", status="error")
    if as_json:
        print(json.dumps(row, indent=2))
        return 0
    _print_probe(row)
    return 0


def main(argv: list[str] | None = None) -> int:
    specs, definition_rows = load_definitions()
    ap = argparse.ArgumentParser(
        description="Collect AI subscription usage windows for tmos.usage."
    )
    ap.add_argument(
        "--once", action="store_true", help="collect once and write the cache"
    )
    ap.add_argument(
        "--json", action="store_true", help="print the document (or the probe) on stdout"
    )
    ap.add_argument(
        "--only",
        action="append",
        choices=sorted(set(COLLECTORS) | {spec["id"] for spec in specs}),
        help="limit the run to one provider (repeatable); a definition id works too",
    )
    ap.add_argument(
        "--no-stats",
        action="store_true",
        help="skip the local transcript scan (limits only, no token history)",
    )
    ap.add_argument(
        "--list-providers",
        action="store_true",
        help="list every provider TMOS would collect, built-in or definition; no network",
    )
    ap.add_argument(
        "--probe",
        metavar="ID",
        default=None,
        help="read one provider now and show what came back and where it came from",
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
    if args.list_providers:
        return _list_providers(specs, definition_rows, args.json)
    if args.probe:
        return _probe(args.probe, specs, definition_rows, args.json)
    if not args.once:
        ap.error(
            "nothing to do: pass --once (optionally with --json), --list-providers, "
            "--probe ID, or --selftest"
        )

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
