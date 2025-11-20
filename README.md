# <div align="center">  **Drone Remote ID Mapper** </div>

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://www.python.org/)
[![ESP32](https://img.shields.io/badge/ESP32-Compatible-green.svg)](https://www.espressif.com/)
[![Flask](https://img.shields.io/badge/Flask-2.0+-red.svg)](https://flask.palletsprojects.com/)

**Real-time drone detection, mapping, and Remote ID compliance monitoring**

[Quick Start](#quick-start) • [Features](#features) • [API Reference](#api-reference) • [Hardware](#hardware-setup)

<img src="eye.png" alt="Drone Detection Eye" style="width:50%; height:25%;">

</div>

---

## **Hardware Options**

### **Ready-to-Use Solution**
Pre-built detection hardware designed specifically for this project:

<a href="https://www.tindie.com/stores/colonel_panic/?ref=offsite_badges&utm_source=sellers_colonel_panic&utm_medium=badges&utm_campaign=badge_large">
    <img src="https://d2ss6ovg47m0r5.cloudfront.net/badges/tindie-larges.png" alt="I sell on Tindie" width="200" height="104">
</a>

**Complete kits with all components included**  
**Pre-flashed firmware ready to use**  


**Completely Standalone Operation**
- **No Raspberry Pi Required**: Boards operate independently for mesh detection
- **No Computer Needed**: Self-contained drone detection and mesh communication
- **Instant Setup**: Just power on and start detecting

**Optional Mapper Integration**
- **Standalone mesh detection** works great on its own
- **Add the mapper software** for enhanced visualization and logging
- **Best of both worlds**: Mesh detection + centralized monitoring

### **DIY Build Option**

Build your own detection system using readily available components:

**Required Components:**
- **Xiao ESP32-S3** (dual-core with WiFi + Bluetooth)
- **Heltec WiFi LoRa 32 V3** (for mesh networking)
- Basic wiring connections

**Perfect for:**
- Learning and experimentation
- Custom modifications
- Budget-conscious builds
- Educational projects

---

## **Overview**

Advanced drone detection system that captures and maps Remote ID broadcasts from drones using ESP32 hardware. Features real-time web interface, persistent tracking across sessions, and comprehensive data export capabilities.

---

## **Quick Start**

### **Installation**

1. **Clone repository**
   ```bash
   git clone https://github.com/colonelpanichacks/drone-mesh-mapper.git
   cd drone-mesh-mapper
   ```

2. **Install dependencies**
   ```bash
   cd drone-mapper
   pip3 install -r requirements.txt
   
   # Or use the dependency installer script
   python3 install_requiremants.py
   ```

3. **Flash ESP32 firmware**
   - Navigate to `remoteid-mesh-s3/` or `remoteid-node-mode-s3/` directory
   - Use PlatformIO to flash firmware:
     ```bash
     cd remoteid-mesh-s3
     pio run -e seeed_xiao_esp32s3 -t upload
     ```
   - Configure WiFi channel and mesh settings

4. **Run Mapper**
   ```bash
   cd drone-mapper
   python3 mesh-mapper.py
   ```
   
   The web interface will be available at `http://localhost:5000`

### **Auto-Start Setup (Linux)**

Use the cronstall script to automatically start the mapper on system reboot:

```bash
# Navigate to cronstall folder
cd drone-mapper/cronstall

# Install cron job (automatically finds mesh-mapper.py in parent directory)
python3 cron.py

# Or specify custom path to mesh-mapper.py
python3 cron.py --path /opt/mesh-mapper/drone-mapper/mesh-mapper.py
```

The cronstall script will:
- Automatically locate `mesh-mapper.py` in the parent directory
- Set up a cron job to start the mapper on every system reboot
- Configure proper paths and working directory

---

## **Core Features**

### **Real-time Mapping**
- **Live Detection Display**: Interactive map showing drone positions as they're detected
- **Flight Path Tracking**: Visual trails showing drone and pilot movement over time
- **Persistent Sessions**: Drones remain visible across application restarts
- **Multi-device Support**: Handle multiple ESP32 receivers simultaneously

### **Data Management**
- **Detection History**: Complete log of all drone encounters with timestamps (GeoJSON format)
- **Device Aliases**: Assign friendly names to frequently seen drones (up to 200 aliases per ESP32-S3 device)
- **Alias Upload to ESP32**: Upload aliases directly to ESP32-S3 devices for persistent storage
- **Alias File Upload**: Upload aliases from JSON files (replaces all existing aliases)
- **Data Export**: Download aliases as JSON, detection history as GeoJSON

### **ESP32 Integration**
- **Auto-detection**: Automatically finds and connects to ESP32 devices
- **Port Management**: Save and restore USB port configurations
- **Status Monitoring**: Real-time connection health and data flow indicators
- **Command Interface**: Send diagnostic commands to connected hardware
- **Alias Storage**: ESP32-S3 devices store up to 200 aliases in NVS (Non-Volatile Storage) for persistence
- **Alias-First Output**: Serial output includes alias first, then MAC address when available

### **Web Interface**
- **Real-time Updates**: WebSocket-powered live data streaming
- **Mobile Responsive**: Works on desktop, tablet, and mobile devices
- **Multiple Views**: Map, detection list, and device status panels

### **Configuration & Monitoring**
- **Headless Operation**: Run without web interface for dedicated deployments
- **Debug Logging**: Detailed logging for troubleshooting and development
- **FAA Integration**: Automatic Remote ID registration lookups
- **Webhook Support**: External system integration via HTTP callbacks

---

## **Usage**

### **Command Line Options**

```bash
python3 mesh-mapper.py [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--headless` | Run without web interface | false |
| `--debug` | Enable debug logging | false |
| `--web-port PORT` | Web interface port | 5000 |
| `--port-interval SECONDS` | Port monitoring interval | 10 |
| `--no-auto-start` | Disable automatic port connection | false |

### **Examples**

```bash
# Standard operation with web interface
cd drone-mapper
python3 mesh-mapper.py

# Headless operation for dedicated server
cd drone-mapper
python3 mesh-mapper.py --headless --debug

# Custom web port with verbose logging
cd drone-mapper
python3 mesh-mapper.py --web-port 8080 --debug

# Disable auto-connection to saved ports
cd drone-mapper
python3 mesh-mapper.py --no-auto-start
```

---



## **API Reference**

### **Core Endpoints**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Main web interface |
| `GET` | `/api/detections` | Current active drone detections |
| `POST` | `/api/detections` | Submit new detection data |
| `GET` | `/api/detections_history` | Historical detection data (GeoJSON) |
| `GET` | `/api/paths` | Flight path data for visualization |
| `POST` | `/api/reactivate/<mac>` | Reactivate inactive drone detection |

### **Device Management**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/aliases` | Get device aliases |
| `POST` | `/api/set_alias` | Set friendly name for device |
| `POST` | `/api/clear_alias/<mac>` | Remove device alias |
| `POST` | `/api/upload_aliases` | Upload aliases to ESP32-S3 devices (optionally accepts JSON file to replace all aliases) |
| `GET` | `/api/ports` | Available serial ports |
| `GET` | `/api/serial_status` | ESP32 connection status |
| `GET` | `/api/selected_ports` | Currently configured ports |

### **FAA & External Integration**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/faa/<identifier>` | FAA registration lookup |
| `POST` | `/api/query_faa` | Manual FAA query |
| `POST` | `/api/set_webhook_url` | Configure webhook endpoint |
| `GET` | `/api/get_webhook_url` | Get current webhook URL |
| `GET` | `/api/webhook_url` | Get current webhook URL (alternative endpoint) |
| `POST` | `/api/webhook_popup` | Webhook notification handler |

### **Data Export**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/download/aliases` | Download device aliases as JSON |
| `GET` | `/download/csv` | Download current session detections as CSV |
| `GET` | `/download/kml` | Download current session detections as KML |
| `GET` | `/download/cumulative_detections.csv` | Download cumulative detections as CSV |
| `GET` | `/download/cumulative.kml` | Download cumulative detections as KML |

### **System Management**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/diagnostics` | System health and performance |
| `POST` | `/api/debug_mode` | Toggle debug logging |
| `POST` | `/api/send_command` | Send command to ESP32 devices |
| `GET` | `/select_ports` | Port selection interface |
| `POST` | `/select_ports` | Update port configuration |
| `GET` | `/sw.js` | Service worker for PWA support |

### **WebSocket Events**

Real-time events pushed to connected clients:

- `detections` - Updated drone detection data
- `detection` - Single drone detection update
- `paths` - Updated flight path data  
- `serial_status` - ESP32 connection status changes
- `aliases` - Device alias updates
- `faa_cache` - FAA lookup results
- `cumulative_log` - Cumulative detection log updates

---

## **Hardware Setup**

### **Supported ESP32 Boards**
- **Xiao ESP32-C3** (Single core, WiFi only)
- **Xiao ESP32-S3** (Dual core, WiFi + Bluetooth)  
- **ESP32-DevKit** (Development and testing)
- **Custom PCBs** (See Tindie store link below)

### **Wiring for Mesh Integration**
```
ESP32 Pin | Mesh Radio Pin
----------|---------------
TX1 (d4)  | RX 19
RX1 (d5)  | TX 20
3.3V      | VCC
GND       | GND
```

---

## **Performance**

| Metric | Performance |
|--------|-------------|
| **Detection Latency** | < 500ms average |
| **Concurrent Drones** | 50+ simultaneous |
| **Memory Usage** | < 100MB typical |
| **Storage Efficiency** | ~1KB per detection |
| **Network Throughput** | 1000+ detections/min |

---

## **Project Structure**

```
drone-mesh-mapper/
├── drone-mapper/              # Main application folder
│   ├── mesh-mapper.py        # Main Python application
│   ├── requirements.txt       # Python dependencies
│   ├── install_requiremants.py  # Dependency installer
│   └── cronstall/             # Auto-start setup scripts
│       └── cron.py           # Cron job installer for Linux
├── remoteid-mesh-s3/         # ESP32-S3 firmware (mesh mode)
│   ├── src/
│   │   └── main.cpp          # Firmware source code
│   └── platformio.ini        # PlatformIO configuration
├── remoteid-node-mode-s3/    # ESP32-S3 firmware (node mode)
│   ├── src/
│   │   └── main.cpp          # Firmware source code
│   └── platformio.ini        # PlatformIO configuration
└── README.md                 # This file
```

## **Latest Updates**

### **ESP32-S3 Alias Support**
- Aliases can now be uploaded directly to ESP32-S3 devices
- Supports up to 200 aliases per device
- Aliases are stored in NVS (Non-Volatile Storage) and persist across reboots
- Serial output includes alias first, then MAC address when available
- Upload aliases via web interface with optional JSON file upload (replaces all existing aliases)

### **File Organization**
- Reorganized project structure with `drone-mapper/` folder
- Separated firmware, application, and installation scripts
- Improved project maintainability and clarity

### **Cronstall Script**
- Moved `cronstall/` folder inside `drone-mapper/` for better organization
- Simplified script - no GitHub downloads, just installs cron job for existing mesh-mapper.py
- Automatically finds mesh-mapper.py in parent directory
- Automatically sets up cron jobs for auto-start on Linux systems

## **Troubleshooting**

### **Common Issues**

**ESP32 Not Detected**
```bash
# Check USB connection
ls -la /dev/tty* | grep USB

# Verify driver installation  
dmesg | grep tty
```

**Web Interface Not Loading**
```bash
# Check if service is running
netstat -tlnp | grep :5000
```

**No Drone Detections**
- Verify ESP32 firmware is properly flashed
- Check WiFi channel configuration (default: channel 6)
- Ensure drones are transmitting Remote ID (required in many jurisdictions)

---

## **License**

This project is licensed under the MIT License 

---

## **Acknowledgments**

- **Cemaxacutor** 
- **Luke Switzer** 
- **OpenDroneID Community** - Standards and specifications
- Thank you PCBway for the awesome boards! The combination of their top tier quality, competitive pricing, fast turnaround times, and stellar customer service makes PCBWay the go-to choice for professional PCB fabrication, whether you're prototyping innovative mesh detection systems or scaling up for full production runs.
https://www.pcbway.com/
  <div align="center"> <img src="boards.png" alt="boards" style="width:50%; height:25%;">


---

## **Hardware Store**

Get professional PCBs and complete kits:

<a href="https://www.tindie.com/stores/colonel_panic/?ref=offsite_badges&utm_source=sellers_colonel_panic&utm_medium=badges&utm_campaign=badge_large">
    <img src="https://d2ss6ovg47m0r5.cloudfront.net/badges/tindie-larges.png" alt="I sell on Tindie" width="200" height="104">
</a>

---

<div align="center">

**If this project helped you, please give it a star!**

Made with love by the Drone Detection Community

</div> 
