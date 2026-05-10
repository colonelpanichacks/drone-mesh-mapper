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

## Gotchas

1. **Node ID required**: Remote nodes must send `node_id` in JSON for dedup to work
2. **GPIO pin variations**: `remoteid-mesh` uses GPIO6/7 (different from other variants)
3. **BLE vs WiFi**: Both run concurrently on dual-core S3 — WiFi faster, BLE better indoor
4. **Memory limits**: Max 8 simultaneous UAVs per remote node (eviction on overflow)
5. **Heltec V3 Meshtastic config**: Must enable serial module (`serial.enabled true`, `serial.mode TEXTMSG`, `serial.baud BAUD_115200`)
6. **5GHz channels**: C5 variant scans UNII-3 (149, 153, 157, 161, 165), 50ms dwell each
7. **FAA cache**: 3-tier fallback: `(mac, basic_id)` → `(mac, *)` → previous tracked entry
8. **Thread safety**: Serial reads use locks — detections arrive from multiple USB ports simultaneously
9. **CSV logging**: Every detection written immediately to prevent data loss on crash
10. **Webhook rate limiting**: Triggered on detection transitions only (new/reactivation)

## Supported Platforms

- **Raspberry Pi**: Automated installer at `RPI/install_rpi.py` + `rpi_dependancies.py`
- **macOS/Linux**: Standard Python + PlatformIO setup
