# Drone Remote ID to Meshtastic 📡
Minimal WiFi-based Drone Remote ID Scanner  
This project is a minimal scanner for WiFi-based Drone Remote ID based on cemaxacutoer's wifi remote id detection firmware, supporting both OpenDroneI.  It runs on ESP32 (tested with Mesh-Detect boards like the Xiao ESP32-C3) and sends parsed messages over a custom UART to a serial mesh network.

<img src="eye.png" alt="eye" style="width:50%; height:25%;">

---

## Features 🌟

- **WiFi Monitoring:** Listens to WiFi management frames in promiscuous mode.
- **Protocol Support:** Decodes both **OpenDroneID** and **French Remote ID** packets.
- **Mesh Integration:** Sends compact, formatted messages via custom UART (TX: GPIO6, RX: GPIO7) to a mesh network.
- **Real-Time Map Visualization:** Displays drone and pilot positions with unique icons and matching colored markers on an interactive map.
- **Automatic Path Tracking:** Continuously draws the movement paths of each drone and pilot.
- **Stale Data Management:** Removes markers and paths if no valid data is received for more than 5 minutes.
- **Logging & Export:** Logs all detections to a CSV file and generates a KML file for offline analysis.
- **Port Selection:** Provides an intuitive interface to select the correct USB serial device.

---

## How It Works 🔍

1. **Initialization:**  
   - The ESP32 configures USB Serial (115200 baud) and a custom UART for mesh communication.
   - WiFi is set to promiscuous mode so the device can capture management frames.
   - The firmware parses incoming packets (supporting OpenDroneID and French Remote ID formats) and sends a minimal JSON payload over USB Serial.

2. **Data Processing:**  
   - The Flask API receives the JSON data from the ESP32 via USB Serial.
   - Data is parsed, keys are remapped (e.g., `"drone_lat"` becomes `"lat"`), and detections are stored.
   - Each detection is logged to a CSV file and used to update a KML file.

3. **Map Visualization:**  
   - A web-based map (using OpenStreetMap tiles) polls the API every 5 seconds.
   - Drone (🚁) and pilot (👤) markers are displayed with matching colored circle markers.
   - Unique colors are assigned based on the device's MAC address.
   - The map automatically zooms in on the location of the first valid detection.
   - Movement paths are drawn for both drones and pilots.
   - If a device isn’t detected for more than 5 minutes, its markers and paths are removed.

---

## How to Connect and Map 🚀

1. **Connect Your Device:**  
   Plug your ESP32 (with the Drone Remote ID scanner firmware) into your computer via USB.

2. **Start the Flask API:**  
   Run the provided Flask API script. The app will open in your web browser.

3. **Select Your Serial Port:**  
   - When you first access the app, you’ll be presented with a selection page.
   - Choose the correct USB serial port (the one your ESP32 is connected to) from the list.
   - Click "Select Port" to proceed.

4. **View the Map:**  
   - Once a port is selected, the map loads automatically.
   - The map uses OpenStreetMap tiles (supporting zoom up to 19).
   - As your device picks up drone and pilot signals, markers will appear:
     - A drone icon (🚁) for the drone’s location.
     - A person icon (👤) for the pilot’s location.
   - Each pair is shown with matching colored circle markers and dynamic movement paths.
   - The map automatically zooms to the first valid detection for a clear view.

5. **Real-Time Updates:**  
   The map refreshes every 5 seconds, continuously updating with new GPS data and tracking movements. If no valid data is received for over 5 minutes, markers and paths for that device are removed.

6. **Logging & Export:**  
   All detections are logged to a CSV file and a KML file is generated for post-flight analysis. Each run creates new log files with a timestamp in the filename.

---

## API Usage & Functionality 🚀

Our Flask API provides a real-time map along with powerful logging and export features:

- **GET `/api/detections`:**  
  Retrieves the current detection data in JSON format. This is used by the map to update marker positions and paths.

- **POST `/api/detections`:**  
  Accepts new detection data (useful for testing or integration). The API automatically remaps keys (e.g., `"drone_lat"` becomes `"lat"`) and logs each detection with a timestamp.

- **CSV Logging:**  
  Every detection is appended to a CSV file named with the current date and time, so each run gets its own log file.

- **KML Export:**  
  A KML file is continuously regenerated to track each unique device’s (drone and pilot) location, making it easy to analyze the flight paths using mapping software like Google Earth.

This comprehensive API lets you monitor drone and pilot movements live while maintaining detailed logs for further analysis.

---

## Drone Remote ID Firmware (ESP32) Overview 🛠️

The ESP32 firmware (included in this repository) performs the following:

- **WiFi Monitoring:** Captures WiFi management frames in promiscuous mode.
- **Protocol Support:** Decodes both OpenDroneID and French Remote ID messages.
- **Mesh Messaging:** Sends formatted mesh messages via UART (Serial1) without modification.
- **USB JSON Output:** Sends a minimal JSON payload over USB Serial for the Flask API to ingest.

---

## Installation & Setup 🚀

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/yourusername/drone-remote-id-scanner.git
   cd drone-remote-id-scanner
Thanks to Cemaxacutor and Luke Switzer for the underlying code! 


## Order a PCB for this project
<a href="https://www.tindie.com/stores/colonel_panic/?ref=offsite_badges&utm_source=sellers_colonel_panic&utm_medium=badges&utm_campaign=badge_large">
    <img src="https://d2ss6ovg47m0r5.cloudfront.net/badges/tindie-larges.png" alt="I sell on Tindie" width="200" height="104">
</a>
