## Drone Remote id to Meshtastic 📡
Minimal WiFi-based Drone Remote ID Scanner
This project is a minimal scanner for WiFi-based Drone Remote ID, supporting both OpenDroneID and French Remote ID formats. It runs on ESP32 (tested with Mesh-Detect boards like the Xiao ESP32-C3) and sends parsed messages over a custom UART to a serial mesh network.

<img src="eye.png" alt="eye" style="width:50%; height:25%;">

---

## Features 🌟

- **WiFi Monitoring:** Listens to WiFi management frames in promiscuous mode.
- **Protocol Support:** Decodes both **OpenDroneID** and **French Remote ID** packets.
- **Mesh Integration:** Sends compact, formatted messages via custom UART (TX: GPIO6, RX: GPIO7) to a mesh network.
- **Efficient & Lightweight:** Minimal scanning and parsing for real-time UAV data.
- **Heartbeat Logging:** Prints periodic heartbeat messages to USB serail to ensure the device is active.

---

## How It Works 🔍

1. **Initialization:**
   - Configures USB Serial (115200 baud) and a custom UART for mesh communication.
   - Initializes the ESP32's WiFi in promiscuous mode to capture management frames.
   - Sets up system events and optimizes the CPU frequency (set to 160 MHz).

2. **Packet Capture & Parsing:**
   - **Packet Handling:** The callback intercepts WiFi management frames.
   - **Identification:** Checks for specific MAC addresses or payload signatures to determine if a packet is for OpenDroneID or French Remote ID.
   - **Data Extraction:** 
     - **OpenDroneID:** Uses functions (e.g., `odid_wifi_receive_message_pack_nan_action_frame`) to decode UAV details such as MAC address, RSSI, location, altitude, speed, and heading.
     - **French ID:** Custom parsing logic extracts operator ID, UAV ID, GPS coordinates, altitude, and other flight parameters.

3. **Message Formatting & Transmission:**
   - Constructs a compact message that includes:
     - **UAV MAC Address & RSSI**
     - **Location Data:** Latitude and longitude (if available)
     - **Flight Data:** Speed, altitude, and heading (if applicable)
   - Transmits the message over the custom UART to integrate with your Meshtastic network.
   - Debug information is printed over USB serial for local monitoring.

4. **Heartbeat Mechanism:**
   - Every 60 seconds, a heartbeat message ("Heartbeat: Device is active and running.") is printed via USB Serial to confirm that the scanner is operational.

---

## Hardware Requirements 🛠️

- **ESP32 Development Board:** Xiao ESP32-C3
- **Custom UART Wiring:** 
  - **TX:** GPIO6  
  - **RX:** GPIO7
- **WiFi Module:** Utilizes the built-in WiFi capabilities of the ESP32 to scan for drone Remote ID signals.

---

## Software Requirements 💻

- **Arduino Framework:** The project is built using the Arduino IDE with ESP32 support.
- **Libraries:**
  - `Arduino.h`
  - `HardwareSerial.h`
  - ESP32-specific WiFi libraries (`esp_wifi.h`, etc.)
  - Custom drone ID libraries: `opendroneid.h` and `odid_wifi.h`

---

## Installation & Setup 🚀

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/yourusername/drone-remote-id-scanner.git
   cd drone-remote-id-scanner

   Build and Flash in Platform.io via VSCode. 


Thanks to Cemaxacutor and Luke Switzer for the underlying code! 
<a href="https://github.com/alphafox02">
<a href="https://github.com/lukeswitz">

## Order a PCB for this project
<a href="https://www.tindie.com/stores/colonel_panic/?ref=offsite_badges&utm_source=sellers_colonel_panic&utm_medium=badges&utm_campaign=badge_large">
    <img src="https://d2ss6ovg47m0r5.cloudfront.net/badges/tindie-larges.png" alt="I sell on Tindie" width="200" height="104">
</a>
