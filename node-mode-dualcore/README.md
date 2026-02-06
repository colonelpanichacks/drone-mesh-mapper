# Drone Mesh Mapper - Node Mode

**colonelpanichacks**

Two firmwares for the Seeed XIAO ESP32S3 paired with a Heltec V3 running Meshtastic. Remote nodes detect drones. Home node receives detections from the mesh and feeds them to [mesh-mapper.py](https://github.com/colonelpanichacks/drone-mesh-mapper).

---

## Architecture

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                        REMOTE NODE (field)                      │
  │  XIAO ESP32S3 ──UART──> Heltec V3 (Meshtastic) ──LoRa──>      │
  │  WiFi + BLE drone       GPIO5 TX -> Heltec RX                  │
  │  detection               GPIO6 RX <- Heltec TX                 │
  │  (Open Drone ID)                                                │
  └─────────────────────────────────────────────────────────────────┘
                                  │
                           LoRa Mesh Network
                          (multiple hops OK)
                                  │
  ┌─────────────────────────────────────────────────────────────────┐
  │                        HOME NODE (base)                         │
  │  Heltec V3 (Meshtastic) ──UART──> XIAO ESP32S3 ──USB──>       │
  │  receives mesh data       GPIO6 RX <- Heltec TX    computer    │
  │                           GPIO5 TX -> Heltec RX    running     │
  │  NO detection.            dedup engine             mesh-mapper  │
  │  Just a smart bridge.                                           │
  └─────────────────────────────────────────────────────────────────┘
```

## The Multi-Node Problem

When you deploy 5 remote nodes and a drone flies overhead, all 5 nodes detect the same drone and send JSON over the mesh. Without dedup, `mesh-mapper.py` would see 5 duplicate detections for 1 drone.

### How We Solve It

**Remote nodes** tag every detection with a unique `node_id` derived from their ESP32 hardware MAC:

```json
{"mac":"aa:bb:cc:dd:ee:ff","rssi":-62,"drone_lat":34.050000,"drone_long":-118.240000,"drone_altitude":120,"pilot_lat":34.048000,"pilot_long":-118.238000,"basic_id":"FA-12345","node_id":"A1B2"}
```

**Home node** runs a dedup engine keyed on drone MAC address:

- **First detection** for a new drone MAC: **forwarded instantly** (zero delay)
- **Duplicates** from other nodes within **500ms**: **dropped** (same detection event from different nodes)
- **After 500ms**: next detection goes through (drone moved, new position data)
- **Result**: near real-time tracking, no multi-node spam

Remote nodes send detections as fast as they happen with no artificial rate limiting. Meshtastic handles its own channel queuing. The 500ms dedup window at the home node is tight enough to squash the burst of multi-node duplicates while letting every new position update flow through.

---

## Hardware

### Per Node (Remote or Home)

| Component | Purpose |
|---|---|
| **Seeed XIAO ESP32S3** | Detection (remote) or bridge (home) |
| **Heltec WiFi LoRa 32 V3** | Meshtastic mesh radio |
| **3 wires** | TX, RX, GND between XIAO and Heltec |

### Wiring

```
XIAO ESP32S3          Heltec V3
─────────────         ──────────
GPIO5 (TX)  ───────>  RX
GPIO6 (RX)  <───────  TX
GND         ────────  GND
```

Same wiring for both remote and home nodes. Only the firmware differs.

---

## Building & Flashing

Requires [PlatformIO](https://platformio.org/).

### Build Both

```bash
pio run
```

### Flash Remote Node (detection board)

```bash
pio run -e remote_node -t upload
```

### Flash Home Node (receiving bridge)

```bash
pio run -e home_node -t upload
```

### Monitor Serial Output

```bash
pio run -e remote_node -t monitor
pio run -e home_node -t monitor
```

---

## Firmware Details

### Remote Node (`main_remote.cpp`)

Dual-core drone detection firmware.

- **Core 0**: WiFi promiscuous mode sniffing Open Drone ID NAN action frames and beacon vendor IEs
- **Core 1**: BLE scanning for Open Drone ID BLE advertisements
- Sends JSON to USB Serial (local monitoring) and UART Serial1 (Heltec V3 mesh)
- Each detection tagged with unique `node_id` for home node dedup
- No rate limiting -- fires as fast as it detects
- LED blinks on each detection
- Heartbeat every 60s
- **RAM: 20.3% | Flash: 38.2%**

### Home Node (`main_home.cpp`)

Lean mesh-to-USB bridge with dedup. No detection.

- Reads JSON lines from Heltec V3 over UART
- Deduplicates by drone MAC (500ms window, first-in wins)
- Forwards clean data to USB Serial for `mesh-mapper.py`
- Non-JSON lines (Meshtastic debug) forwarded with `[MESH]` prefix
- Bidirectional: USB-to-UART pass-through for sending commands to the Heltec
- Heartbeat every 30s with active drone count
- Stats every 60s (received/forwarded/suppressed counts)
- Stale dedup entries auto-cleared after 30s
- LED blinks on each forwarded message
- **RAM: 6.1% | Flash: 8.3%**

---

## JSON Format

All drone detections use this JSON format (one per line):

```json
{
  "mac": "aa:bb:cc:dd:ee:ff",
  "rssi": -62,
  "drone_lat": 34.050000,
  "drone_long": -118.240000,
  "drone_altitude": 120,
  "pilot_lat": 34.048000,
  "pilot_long": -118.238000,
  "basic_id": "FA-12345",
  "node_id": "A1B2"
}
```

| Field | Description |
|---|---|
| `mac` | Drone's broadcast MAC address |
| `rssi` | Signal strength at detecting node |
| `drone_lat` / `drone_long` | Drone GPS position (from Remote ID) |
| `drone_altitude` | Altitude MSL in meters |
| `pilot_lat` / `pilot_long` | Operator/pilot GPS position |
| `basic_id` | FAA Remote ID registration |
| `node_id` | Which remote node detected it (4-char hex from ESP32 MAC) |

---

## Project Structure

```
node-mode/
├── platformio.ini        # Two build environments: remote_node, home_node
├── src/
│   ├── main_remote.cpp   # Remote node - WiFi+BLE detection + mesh send
│   ├── main_home.cpp     # Home node - UART bridge + dedup engine
│   ├── opendroneid.c     # Open Drone ID protocol decoder
│   ├── opendroneid.h     # ODID data structures
│   ├── odid_wifi.h       # ODID WiFi frame parsing
│   └── wifi.c            # WiFi NAN/beacon ODID extraction
├── .gitignore
└── README.md
```

---

## Heltec V3 Meshtastic Setup

The Heltec V3 boards run stock [Meshtastic firmware](https://meshtastic.org/). Enable the **Serial Module** in Meshtastic settings:

1. Flash Meshtastic to both Heltec V3 boards
2. Enable Serial Module: `meshtastic --set serial.enabled true`
3. Set Serial Mode to **TEXTMSG**: `meshtastic --set serial.mode TEXTMSG`
4. Set baud rate to **115200**: `meshtastic --set serial.baud BAUD_115200`
5. Set the serial pins to match wiring (RX/TX pins the Heltec uses to talk to the XIAO)

Both Heltec boards should be on the same Meshtastic channel/encryption key.

---

## Usage with mesh-mapper.py

1. Flash **remote node** firmware to field XIAO boards
2. Flash **home node** firmware to the base XIAO board
3. Set up Meshtastic on all Heltec V3 boards (same channel)
4. Wire each XIAO to its Heltec V3 (TX, RX, GND)
5. Plug the home XIAO into the computer running mesh-mapper.py via USB
6. Run: `python3 mesh-mapper.py`
7. mesh-mapper.py auto-detects the USB serial port and starts mapping

---

## License

MIT
