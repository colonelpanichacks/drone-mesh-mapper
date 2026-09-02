#!/bin/bash
# ds110_bridge_macos.sh - launch tools/ds110_bridge.py on macOS.
#
# Plain `python tools/ds110_bridge.py` is killed by macOS with SIGABRT the
# instant it touches CoreBluetooth:
#
#   TCC: This app has crashed because it attempted to access privacy-sensitive
#   data without a usage description. The app's Info.plist must contain an
#   NSBluetoothAlwaysUsageDescription key ...
#
# No stock Python ships that key, so there is nothing to grant in System
# Settings - the process dies before it can even ask. This script builds a
# throwaway .app bundle around the interpreter that DOES declare the key (a
# copy of the framework's own Python.app stub, which links the framework by
# absolute path, so it still works outside the framework), re-signs it ad-hoc,
# and runs the bridge inside it. macOS then shows the normal Bluetooth
# permission prompt the first time.
#
# The bundle is built under venv/ (gitignored) and rebuilt whenever the
# interpreter changes. Everything the bridge takes is passed straight through:
#   ./tools/ds110_bridge_macos.sh --list
#   ./tools/ds110_bridge_macos.sh --url http://localhost:5001
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$here/venv/bin/python"
[ -x "$py" ] || py="$(command -v python3)"

if [ "$(uname -s)" != "Darwin" ]; then
    exec "$py" "$here/tools/ds110_bridge.py" "$@"
fi

# Framework stub + site-packages of whichever interpreter we are using.
read -r base_app site_pkgs < <("$py" - <<'PY'
import sys, sysconfig, os
base = sys.base_exec_prefix
print(os.path.join(base, "Resources", "Python.app"),
      sysconfig.get_paths()["purelib"])
PY
)

if [ ! -d "$base_app" ]; then
    echo "ds110_bridge_macos: no Python.app stub at $base_app" >&2
    echo "  (needs a framework build of Python - e.g. Homebrew or python.org)" >&2
    exit 1
fi

app="$here/venv/ds110-host/DroneScoutBridge.app"
stub="$app/Contents/MacOS/Python"

if [ ! -x "$stub" ] || [ "$base_app/Contents/MacOS/Python" -nt "$stub" ]; then
    echo "ds110_bridge_macos: building Bluetooth-capable host bundle..." >&2
    rm -rf "$app"
    mkdir -p "$(dirname "$app")"
    cp -R "$base_app" "$app"
    /usr/libexec/PlistBuddy \
        -c "Set :CFBundleIdentifier tech.colonelpanic.ds110bridge" \
        -c "Set :CFBundleName DroneScoutBridge" \
        -c "Add :NSBluetoothAlwaysUsageDescription string 'Receives drone Remote ID broadcasts relayed by a DroneScout Bridge ds110 and feeds them to mesh-mapper.'" \
        "$app/Contents/Info.plist" >/dev/null
    # Editing Info.plist invalidates the inherited ad-hoc signature; without a
    # valid one the bundle will not launch at all.
    codesign --force --sign - "$app" 2>/dev/null || {
        echo "ds110_bridge_macos: codesign failed" >&2; exit 1; }
fi

# Running the stub directly still aborts: TCC attributes the Bluetooth request
# to the *responsible* process, which for anything started from a shell is the
# terminal - and no terminal declares the key either. Launching through `open`
# makes launchd the parent, so the bundle is responsible for itself and the
# usage description above is the one macOS reads. `open` does not inherit the
# shell environment or the tty, hence --env and the log file we tail back.
# (No -n: LaunchServices rejects a fresh instance of this unregistered bundle
# with error -10810; the running-instance guard below covers duplicates.)
# Log and pidfile must NOT live beside the bundle: writing into that directory
# between build and launch makes LaunchServices reject the open with -10810.
runtime_dir="${TMPDIR:-/tmp}"
pidfile="$runtime_dir/ds110_bridge.pid"
if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    echo "ds110_bridge_macos: bridge already running (pid $(cat "$pidfile"))" >&2
    exit 1
fi

log="${DS110_LOG:-$runtime_dir/ds110_bridge.log}"
: > "$log"
open --stdout "$log" --stderr "$log" \
     --env "PYTHONPATH=$site_pkgs" \
     "$app" --args "$here/tools/ds110_bridge.py" "$@"

# `open` returns as soon as launchd accepts the job; wait for the real pid.
bridge_pid=""
for _ in $(seq 1 40); do
    bridge_pid="$(pgrep -f "^$stub $here/tools/ds110_bridge.py" | head -1 || true)"
    [ -n "$bridge_pid" ] && break
    sleep 0.25
done
if [ -z "$bridge_pid" ]; then
    echo "ds110_bridge_macos: bridge did not start; see $log" >&2
    cat "$log" >&2
    exit 1
fi
echo "$bridge_pid" > "$pidfile"

# Foreground the log so this behaves like a normal run; Ctrl-C stops both.
cleanup() { kill "$bridge_pid" 2>/dev/null || true; rm -f "$pidfile"; }
trap 'cleanup; exit 0' INT TERM
tail -f "$log" &
tailpid=$!
while kill -0 "$bridge_pid" 2>/dev/null; do sleep 1; done
sleep 0.3; kill $tailpid 2>/dev/null || true; rm -f "$pidfile"
