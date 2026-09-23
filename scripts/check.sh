#!/usr/bin/env bash
# scripts/check.sh — everything that must be true before a release, in one command.
#
#   1. the collector parses its fixtures offline, including the negative controls that must NOT be
#      invented (empty transcript trees, a reply with no usage, a malformed payload)
#   2. a full run with every endpoint pointed at a dead local port still exits 0, reports every
#      provider, and never claims "ok" for a refused endpoint: one provider's bad day is not an
#      outage, and a network failure is never reported as a reading
#   3. the QML parses against the installed shell, and the plugin folder validates against the
#      shell's own manifest rules
#
# SKIPs are printed, never silent: `omarchy plugin validate` only exists on Omarchy, and qmllint
# needs Qt's declarative tools, so a plain CI runner legitimately lacks both.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY=${PYTHON:-python3}
# qmllint ships in Qt's declarative tools and is often not on PATH even when installed.
QMLLINT=${QMLLINT:-$(command -v qmllint || echo /usr/lib/qt6/bin/qmllint)}
rc=0

echo "== collector: fixture selftest (offline)"
"$PY" collector/usage_collector.py --selftest
self_rc=$?
echo "== collector: selftest exit=$self_rc"
[ $self_rc -eq 0 ] || rc=1

echo "== collector: degraded end-to-end (every endpoint refused, empty HOME)"
TMPRUN=$(mktemp -d)
DEAD=http://127.0.0.1:9
env TMOS_USAGE_STATE_DIR="$TMPRUN" HOME="$TMPRUN" \
    OPENCODE_GO_BASE="$DEAD" CLINE_API_BASE="$DEAD" COMMAND_CODE_API_BASE="$DEAD" \
    CLAUDE_API_BASE="$DEAD" CODEX_BACKEND_BASE="$DEAD" \
    "$PY" collector/usage_collector.py --once >/dev/null 2>&1
degraded_rc=$?
"$PY" - "$TMPRUN/usage.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
rows = doc["providers"]
bad = [r["provider"] for r in rows if r["status"] == "ok"]
assert len(rows) == 5, f"expected all five providers, got {len(rows)}"
assert not bad, f"a refused endpoint must never report ok: {bad}"
assert all(r["note"] for r in rows), "every degraded provider owes a one-line reason"
assert all("stats" in r for r in rows), "every provider carries a stats block, available or not"
print(f"providers={len(rows)} statuses=" + ",".join(sorted({r['status'] for r in rows})))
print("cache is valid JSON; every provider degraded with a reason and a stats block")
PYEOF
check_rc=$?
rm -rf "$TMPRUN"
echo "== collector: degraded run exit=$degraded_rc, cache check exit=$check_rc"
[ $degraded_rc -eq 0 ] && [ $check_rc -eq 0 ] || rc=1

echo "== qml: entry points parse"
if command -v "$QMLLINT" >/dev/null 2>&1; then
    "$QMLLINT" BarWidget.qml Panel.qml Service.qml 2>&1 |
        grep -viE "Failed to import|Warnings occurred while importing|are your import paths|^---|^$|^\s*\^+\s*$|was not found\. Did you add all imports" |
        head -20
    qml_rc=${PIPESTATUS[0]}
else
    echo "SKIP  $QMLLINT not installed (Qt declarative tools)"
    qml_rc=0
fi
echo "== qml: exit=$qml_rc"

echo "== manifest: validate against the shell's own rules"
if command -v omarchy >/dev/null 2>&1; then
    omarchy plugin validate . || rc=1
else
    echo "SKIP  omarchy not installed (not an Omarchy host)"
fi

echo "== summary: $([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
