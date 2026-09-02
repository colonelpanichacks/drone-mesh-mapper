# Drone Mesh Mapper – Real-Time Drone Remote ID Detection & Mapping

Detects FAA Remote ID broadcasts from drones via ESP32 firmware nodes, relays detections over Meshtastic mesh, and displays them on a live web map.

## Architecture

- **Python Backend** (`mesh-mapper.py`): Flask + SocketIO server — serial port management, detection dedup, FAA lookups, session persistence, KML/GeoJSON/CSV export
- **Firmware Variants** (multiple PlatformIO projects):
  - `node-mode-dualcore/` — ESP32-S3 dual-core: remote detection node + home dedup node
  - `remoteid-mesh-dualcore/` — Dual-band (BLE + WiFi) detection with mesh relay
  - `remoteid-mesh/` — Original single-core mesh variant
  - `remoteid-c5-5g/` — ESP32-C5 5GHz WiFi Remote ID detection (UNII-3 channels)
- **Web UI**: Live map, detection table, flight path tracking, export controls

## Tech Stack

- **Python**: Flask, Flask-SocketIO, WebSockets, serial port management
- **Firmware**: PlatformIO C++, ESP32-S3 / ESP32-C5, BLE + WiFi promiscuous mode
- **Mesh**: Meshtastic LoRa mesh for multi-node relay
- **Map**: Leaflet.js with real-time WebSocket updates

## Build & Run

### Server
```bash
pip install -r requirements.txt
python mesh-mapper.py
# Use --no-auto-start to disable auto-connection to known serial ports
```

### Firmware (node-mode-dualcore)
```bash
cd node-mode-dualcore
pio run -e remote    # Remote detection node
pio run -e home      # Home dedup/relay node
pio run -t upload
```

### Firmware (ESP32-C5 5GHz)
```bash
cd remoteid-c5-5g
pio run -t upload
```

## Detection Flow

1. Remote nodes capture BLE advertisements + WiFi action frames with ODID payloads
2. Detections serialized as JSON, sent over Meshtastic mesh to home node
3. Home node deduplicates (500ms window) and forwards to Python server via USB serial
4. Server performs FAA registration lookup, broadcasts to web clients via WebSocket

## Key Configuration

- `selected_ports.json` — Known USB serial ports for auto-connection
- Dedup window: 500ms (tunable via `DEDUP_WINDOW_MS` in `main_home.cpp`)
- Stale detection: Marked inactive but NOT deleted (30+ min → `"inactive_old"`)
- KML generation: Throttled to 30s intervals

## DroneScout Bridge ds110 Integration (2026-08-30)

Adds support for the BlueMark DroneScout Bridge ds110 (triple-band Remote ID
receiver dongle) as a host-side receiver. The ds110 re-broadcasts everything it
hears (2.4/5/5.8 GHz) as BT4 Legacy Advertising ODID frames so phones can
receive them; we now ingest that relay directly over the host's Bluetooth.

**Files in this changeset:**
- `tools/ds110_bridge.py` (NEW) — bleak-based BLE listener. Decodes ODID
  BasicID/Location/System messages (layouts match firmware `opendroneid.h`),
  tracks per-drone state, POSTs to `/api/detections` in the exact firmware
  JSON schema. Keys relayed drones by synthesized MAC from `sha1(basic_id)`
  (avoids the all-relayed-drones-share-the-bridge's-advertiser-MAC collapse
  that both `node-mode-dualcore` and `remoteid-mesh-dualcore` have on their
  BLE path). Heartbeats `POST /api/receiver_status` every 15 s.
  `--list` = debug mode (print ads, no POST), `--url` = mapper override.
- `mesh-mapper.py` —
  - `update_detection()`: filters the ds110 idle self-advertisement
    (`DroneScout Bridge`, both spellings) and merges cross-path duplicates by
    `basic_id` via new `basic_id_index` (direct node detection + ds110 relay
    of the same drone = one map entry; first-seen MAC key wins).
  - New `POST /api/receiver_status` endpoint + `receiver_status` dict +
    `combined_connection_status()`: HTTP receivers render in the same
    connection-status UI panel as USB serial ports (stale after 45 s). Covers
    `/api/serial_status`, `/api/diagnostics`, and the `serial_status` socket
    event; no JS changes needed.
- `tools/ds110_bridge_macos.sh` (NEW) — required launcher on macOS. TCC aborts
  (SIGABRT, exit 134, no traceback) any Python touching CoreBluetooth without
  `NSBluetoothAlwaysUsageDescription`, and no stock interpreter declares it.
  The script copies the framework's `Python.app` stub into `venv/ds110-host/`,
  adds the key, re-signs ad-hoc, and launches it with `open` — running the
  stub directly still aborts, because TCC blames the *responsible* process
  (your terminal), not the bundle. Passes args through; tails the log so it
  behaves like a foreground run. Two non-obvious constraints, both found the
  hard way: `open -n` fails with LaunchServices -10810 on this unregistered
  bundle, and so does any `open` whose `--stdout`/`--stderr` log sits in the
  bundle's own directory — hence log + pidfile live in `$TMPDIR`.
- `requirements.txt` — added `bleak>=0.21` (only needed by the bridge script).
- `README.md` — ds110 section under Features + `tools/` tree entry.

**Run:**
```bash
./venv/bin/python mesh-mapper.py --web-port 5001   # 5001, not 5000 — see gotcha #11
./tools/ds110_bridge_macos.sh                     # macOS; see gotcha #13
```

**Verified:** end-to-end POST ingest, placeholder suppression (both BLE and
node paths), Connected/Disconnected UI flips, cross-path dedup (2 POSTs, 1
entry). NOT yet field-tested with a real drone — the assumption that the ds110
relay preserves original `basic_id`s is unconfirmed; if it doesn't, keying
falls back to per-advertiser-MAC (pre-existing behavior).

**Note:** project uses a local `venv/` (python3.12) because the system
python3.14 has a broken eventlet/pyOpenSSL combo that crashes flask-socketio
at startup.

## Gotchas

1. **Node ID required**: Remote nodes must send `node_id` in JSON for dedup to work
2. **GPIO pin variations**: `remoteid-mesh` uses GPIO6/7 (different from other variants)
3. **BLE vs WiFi**: Both run concurrently on dual-core S3 — WiFi faster, BLE better indoor
5. **Heltec V3 Meshtastic config**: Must enable serial module (`serial.enabled true`, `serial.mode TEXTMSG`, `serial.baud BAUD_115200`)
6. **5GHz channels**: C5 variant scans UNII-3 (149, 153, 157, 161, 165), 50ms dwell each
7. **FAA cache**: 3-tier fallback: `(mac, basic_id)` → `(mac, *)` → previous tracked entry
8. **Thread safety**: Serial reads use locks — detections arrive from multiple USB ports simultaneously
9. **CSV logging**: Every detection written immediately to prevent data loss on crash
10. **Webhook rate limiting**: Triggered on detection transitions only (new/reactivation)
11. **DroneScout Bridge ds110**: `tools/ds110_bridge.py` ingests the ds110's BT4 Legacy relay over host BLE (bleak) and POSTs to `/api/detections`; relayed drones keyed by synthesized MAC from `basic_id`. Its idle `DroneScout Bridge` self-advertisement is filtered in `update_detection()`. Connection status: the script heartbeats `POST /api/receiver_status` every 15 s; the server merges HTTP receivers into `combined_connection_status()` (stale after 45 s) so they render in the same UI panel as USB ports. Note: on this dev machine `127.0.0.1:5000` is owned by another app — run the mapper with `--web-port 5001` and use `http://localhost:5001` (the bridge script's default).
13. **macOS Bluetooth/TCC**: never run `tools/ds110_bridge.py` with a bare `python` on macOS — it dies with SIGABRT/exit 134 and *no output at all* (the crash reason is only in `~/Library/Logs/DiagnosticReports/Python-*.ips`, namespace TCC). Use `tools/ds110_bridge_macos.sh`. Same trap for any future host-side BLE tool.
12. **Cross-path dedup**: `update_detection()` merges by `basic_id` via `basic_id_index` — the same drone seen directly by a node and relayed through the DroneScout Bridge (different MAC keys) yields ONE tracked entry; first-seen key wins, later updates fold in (so `source_port` reflects the most recent path).

## Supported Platforms

- **Raspberry Pi**: Automated installer at `RPI/install_rpi.py` + `rpi_dependancies.py`
- **macOS/Linux**: Standard Python + PlatformIO setup
