#!/usr/bin/env bash
# scripts/check.sh — everything that must be true before a release, in one command.
#
#   1. the collector parses its fixtures offline, including the negative controls that must NOT be
#      invented (empty transcript trees, a reply with no usage, a malformed payload) and the
#      provider-definition fixtures, both the ones that load and the ones that must be rejected
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

echo "== request-log importer: offline accounting and privacy checks"
"$PY" -m unittest discover -s collector -p 'test_*.py' || rc=1
echo "== subscription ranking: shared model checks"
node scripts/test-value.cjs || rc=1

echo "== collector: fixture selftest (offline)"
"$PY" collector/usage_collector.py --selftest
self_rc=$?
echo "== collector: selftest exit=$self_rc"
[ $self_rc -eq 0 ] || rc=1

echo "== collector: degraded end-to-end (every endpoint refused, empty HOME, fixture definitions)"
TMPRUN=$(mktemp -d)
DEAD=http://127.0.0.1:9
# The definition fixtures replace both providers.d directories, so a gate run is deterministic, is
# offline, and cannot reach a real endpoint: every fixture URL is a dead local port. The reject
# directory is included on purpose — an unusable definition has to show up in the document too.
FIXDEFS="$PWD/collector/fixtures/providers.d:$PWD/collector/fixtures/providers.reject.d"
env TMOS_USAGE_STATE_DIR="$TMPRUN" HOME="$TMPRUN" \
    OPENCODE_GO_BASE="$DEAD" CLINE_API_BASE="$DEAD" COMMAND_CODE_API_BASE="$DEAD" \
    CLAUDE_API_BASE="$DEAD" CODEX_BACKEND_BASE="$DEAD" \
    TMOS_USAGE_PROVIDERS_DIR="$FIXDEFS" \
    "$PY" collector/usage_collector.py --once >/dev/null 2>&1
degraded_rc=$?
"$PY" - "$TMPRUN/usage.json" <<'PYEOF'
import json, sys
doc = json.load(open(sys.argv[1]))
rows = doc["providers"]
bad = [r["provider"] for r in rows if r["status"] == "ok"]
builtins = {"claude-code", "codex", "clinepass", "command-code", "opencode-go"}
ids = [r["provider"] for r in rows]
missing = sorted(builtins - set(ids))
assert not missing, f"built-in providers missing from the document: {missing}"
extra = [p for p in ids if p not in builtins]
assert extra, "the definition fixtures produced no provider rows at all"
assert not bad, f"a refused endpoint must never report ok: {bad}"
assert all(r["note"] for r in rows), "every degraded provider owes a one-line reason"
assert all("stats" in r for r in rows), "every provider carries a stats block, available or not"
broken = [p for p in ids if p.startswith("definition:")]
assert broken, "an unusable definition must still appear in the document, with its reason"
print(f"providers={len(rows)} statuses=" + ",".join(sorted({r['status'] for r in rows})))
print("cache is valid JSON; every provider degraded with a reason and a stats block")
PYEOF
check_rc=$?
rm -rf "$TMPRUN"
echo "== collector: degraded run exit=$degraded_rc, cache check exit=$check_rc"
[ $degraded_rc -eq 0 ] && [ $check_rc -eq 0 ] || rc=1

# The two commands a user writes and tests a definition with, exercised against the fixtures:
# --list-providers must never need the network, a probe of a refused endpoint must still exit 0
# (the reason is the answer), and an id that nobody defines must exit 2.
echo "== collector: provider definitions (list + probe, no network)"
env TMOS_USAGE_PROVIDERS_DIR="$FIXDEFS" "$PY" collector/usage_collector.py --list-providers >/dev/null 2>&1
list_rc=$?
env TMOS_USAGE_PROVIDERS_DIR="$FIXDEFS" "$PY" collector/usage_collector.py --probe refused >/dev/null 2>&1
probe_rc=$?
env TMOS_USAGE_PROVIDERS_DIR="$FIXDEFS" "$PY" collector/usage_collector.py --probe nobody >/dev/null 2>&1
absent_rc=$?
echo "== collector: --list-providers exit=$list_rc, --probe refused exit=$probe_rc, --probe unknown exit=$absent_rc"
[ $list_rc -eq 0 ] && [ $probe_rc -eq 0 ] && [ $absent_rc -eq 2 ] || rc=1

echo "== qml: syntax (runtime imports checked by native smoke test)"
QMLFORMAT=${QMLFORMAT:-/usr/lib/qt6/bin/qmlformat}
if command -v "$QMLFORMAT" >/dev/null 2>&1; then
    for file in *.qml; do
        "$QMLFORMAT" "$file" >/dev/null || rc=1
    done
else
    echo "SKIP qmlformat not installed"
fi
if command -v quickshell >/dev/null 2>&1 && [ -d /usr/share/omarchy/shell/Commons ]; then
    bash scripts/test-xray.sh || rc=1
else
    echo "SKIP native runtime: requires Quickshell and Omarchy"
fi

echo "== manifest: validate against the shell's own rules"
if command -v omarchy >/dev/null 2>&1; then
    omarchy plugin validate . || rc=1
else
    echo "SKIP  omarchy not installed (not an Omarchy host)"
fi

echo "== summary: $([ $rc -eq 0 ] && echo PASS || echo FAIL)"
exit $rc
