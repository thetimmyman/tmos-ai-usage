#!/usr/bin/env bash
# Exercise the real QML writer and file watcher while a slow refresh is in flight.
set -euo pipefail
cd "$(dirname "$0")/.."
testdir=$(mktemp -d)
trap 'rm -rf "$testdir"' EXIT
ln -s "$PWD" "$testdir/Usage"
for part in /usr/share/omarchy/shell/*/; do ln -s "$part" "$testdir/$(basename "$part")"; done
mkdir "$testdir/state"
cat > "$testdir/collector.py" <<'PY'
import json, os, pathlib, time
p = pathlib.Path(os.environ['TMOS_USAGE_STATE_DIR'])
settings = json.loads((p/'subscriptions.json').read_text()) if (p/'subscriptions.json').exists() else {}
time.sleep(.6)
rows = []
for provider, default in [('codex',200),('opencode-go',10)]:
    fee = settings.get(provider, {}).get('amount_usd', default)
    rows.append({'provider':provider,'activity':{'observed_turns':10,'window_days':30,'price':{'amount_usd':fee,'monthly_usd':fee,'cycle':'month'}}})
tmp=p/'next.json'
tmp.write_text(json.dumps({'comparison_mode':'activity','providers':rows}))
os.replace(tmp,p/'usage.json')
PY
cat > "$testdir/shell.qml" <<'QML'
import QtQuick
import Quickshell
import "Usage"
ShellRoot {
    UsageSource { id: source }
    Timer { interval: 50; running: true; onTriggered: source.refresh() }
    Timer { interval: 200; running: true; onTriggered: source.savePrice('codex','212','month') }
    Timer { interval: 2000; running: true; onTriggered: {
        var codex = source.providers.filter(p => p.id === 'codex')[0];
        var go = source.providers.filter(p => p.id === 'opencode-go')[0];
        if (source.priceError || !source.priceStatus.includes('$212.00') || !codex || codex.activity.price.amount_usd !== 212 || !go || go.activity.price.amount_usd !== 10)
            console.error('ERROR save/queued refresh: ' + source.priceError + ' ' + source.priceStatus + ' ' + JSON.stringify(source.providers));
        else console.log('PRICE_SAVE_RUNTIME_OK');
        Qt.quit();
    } }
}
QML
env -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen TMOS_USAGE_STATE_DIR="$testdir/state" TMOS_USAGE_COLLECTOR="$testdir/collector.py" timeout 15 quickshell -p "$testdir" --no-color > "$testdir/log" 2>&1 || { cat "$testdir/log"; exit 1; }
if grep -E 'ERROR|ReferenceError|TypeError|Unable to assign|Binding loop|is not defined|Cannot assign|Failed to load' "$testdir/log"; then exit 1; fi
grep PRICE_SAVE_RUNTIME_OK "$testdir/log"
python3 - "$testdir/state/subscriptions.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1]))
assert s['codex']['amount_usd']==212 and s['codex']['cycle']=='month'
assert 'opencode-go' not in s, 'Saving Codex must not write another provider'
PY
