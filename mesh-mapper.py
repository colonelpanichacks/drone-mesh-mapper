from flask import Flask, request, jsonify, redirect, url_for, render_template_string
import threading
import serial
import serial.tools.list_ports
import json
import time
import csv
from datetime import datetime

app = Flask(__name__)

tracked_pairs = {}

# Global variable to store the selected serial port.
SELECTED_PORT = None
BAUD_RATE = 115200

# Create CSV and KML filenames using the current timestamp at startup.
startup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_FILENAME = f"detections_{startup_timestamp}.csv"
KML_FILENAME = f"detections_{startup_timestamp}.kml"

# Write CSV header
with open(CSV_FILENAME, mode='w', newline='') as csvfile:
    fieldnames = ['timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long', 'drone_altitude', 'pilot_lat', 'pilot_long']
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()

def generate_kml():
    kml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<kml xmlns="http://www.opengis.net/kml/2.2">',
                 '<Document>',
                 f'<name>Detections {startup_timestamp}</name>']
    for mac, det in tracked_pairs.items():
        kml_lines.append(f'<Placemark><name>Drone {mac}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon></IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("drone_long",0)},{det.get("drone_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
        kml_lines.append(f'<Placemark><name>Pilot {mac}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon></IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("pilot_long",0)},{det.get("pilot_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
    kml_lines.append('</Document></kml>')
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated KML file:", KML_FILENAME)

# HTML page for the map display using OpenStreetMap standard tiles.
HTML_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Drone and Pilot Tracker (OSM)</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <style>
    #map { height: 100vh; }
    body, html { margin: 0; padding: 0; }
  </style>
</head>
<body>
<div id="map"></div>
<script>
// Utility: Compute a unique color based on the MAC string.
function colorFromMac(mac) {
  let hash = 0;
  for (let i = 0; i < mac.length; i++) {
    hash = mac.charCodeAt(i) + ((hash << 5) - hash);
  }
  let h = Math.abs(hash) % 360;
  return 'hsl(' + h + ', 70%, 50%)';
}

// Create a custom icon using a div with an emoji.
function createIcon(emoji, color) {
  return L.divIcon({
    html: '<div style="font-size: 24px; color:' + color + ';">' + emoji + '</div>',
    className: '',
    iconSize: [30, 30]
  });
}

// Generate detailed popup content from detection JSON.
function generatePopupContent(detection) {
  let content = '';
  for (const key in detection) {
    content += key + ': ' + detection[key] + '<br>';
  }
  return content;
}

// Create the map using the OpenStreetMap standard tile layer.
const map = L.map('map').setView([0, 0], 2);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19
}).addTo(map);

const droneMarkers = {};
const pilotMarkers = {};
const droneCircles = {};
const pilotCircles = {};

const dronePolylines = {};
const pilotPolylines = {};

const dronePathCoords = {};
const pilotPathCoords = {};

// Global flag to indicate if we've zoomed to the first detection.
let firstDetectionZoomed = false;

async function updateData() {
  try {
    const response = await fetch('/api/detections');
    const data = await response.json();
    const currentTime = Date.now() / 1000; // current epoch seconds
    for (const mac in data) {
      const det = data[mac];
      // If the detection hasn't been updated in 5 minutes, remove its markers.
      if (!det.last_update || (currentTime - det.last_update > 300)) {
        if (droneMarkers[mac]) { map.removeLayer(droneMarkers[mac]); delete droneMarkers[mac]; }
        if (pilotMarkers[mac]) { map.removeLayer(pilotMarkers[mac]); delete pilotMarkers[mac]; }
        if (droneCircles[mac]) { map.removeLayer(droneCircles[mac]); delete droneCircles[mac]; }
        if (pilotCircles[mac]) { map.removeLayer(pilotCircles[mac]); delete pilotCircles[mac]; }
        if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); delete dronePolylines[mac]; }
        if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); delete pilotPolylines[mac]; }
        delete dronePathCoords[mac];
        delete pilotPathCoords[mac];
        continue;
      }
      
      const droneLat = det.lat;
      const droneLng = det.long;
      const pilotLat = det.pilot_lat;
      const pilotLng = det.pilot_long;
      
      // Only update if valid (non-zero) coordinates exist.
      const validDrone = (droneLat !== 0 && droneLng !== 0);
      const validPilot = (pilotLat !== 0 && pilotLng !== 0);
      if (!validDrone && !validPilot) continue;
      
      const color = colorFromMac(mac);
      
      // Zoom to the first valid drone detection.
      if (!firstDetectionZoomed && validDrone) {
        firstDetectionZoomed = true;
        map.setView([droneLat, droneLng], 14);
      }
      
      // Drone marker and circle.
      if (validDrone) {
        if (droneMarkers[mac]) {
          droneMarkers[mac].setLatLng([droneLat, droneLng]);
          droneMarkers[mac].setPopupContent(generatePopupContent(det));
        } else {
          droneMarkers[mac] = L.marker([droneLat, droneLng], {icon: createIcon('🚁', color)})
                                .bindPopup(generatePopupContent(det))
                                .addTo(map);
        }
        if (droneCircles[mac]) {
          droneCircles[mac].setLatLng([droneLat, droneLng]);
        } else {
          droneCircles[mac] = L.circleMarker([droneLat, droneLng], {radius: 8, color: color, fillColor: color, fillOpacity: 0.7})
                              .addTo(map);
        }
        if (!dronePathCoords[mac]) {
          dronePathCoords[mac] = [];
        }
        const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
        if (!lastDrone || lastDrone[0] !== droneLat || lastDrone[1] !== droneLng) {
          dronePathCoords[mac].push([droneLat, droneLng]);
        }
        if (dronePolylines[mac]) {
          map.removeLayer(dronePolylines[mac]);
        }
        dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);
      }
      
      // Pilot marker and circle.
      if (validPilot) {
        if (pilotMarkers[mac]) {
          pilotMarkers[mac].setLatLng([pilotLat, pilotLng]);
          pilotMarkers[mac].setPopupContent(generatePopupContent(det));
        } else {
          pilotMarkers[mac] = L.marker([pilotLat, pilotLng], {icon: createIcon('👤', color)})
                              .bindPopup(generatePopupContent(det))
                              .addTo(map);
        }
        if (pilotCircles[mac]) {
          pilotCircles[mac].setLatLng([pilotLat, pilotLng]);
        } else {
          pilotCircles[mac] = L.circleMarker([pilotLat, pilotLng], {radius: 8, color: color, fillColor: color, fillOpacity: 0.7})
                              .addTo(map);
        }
        if (!pilotPathCoords[mac]) {
          pilotPathCoords[mac] = [];
        }
        const lastPilot = pilotPathCoords[mac][pilotPathCoords[mac].length - 1];
        if (!lastPilot || lastPilot[0] !== pilotLat || lastPilot[1] !== pilotLng) {
          pilotPathCoords[mac].push([pilotLat, pilotLng]);
        }
        if (pilotPolylines[mac]) {
          map.removeLayer(pilotPolylines[mac]);
        }
        pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {color: color, dashArray: '5,5'}).addTo(map);
      }
    }
  } catch (error) {
    console.error("Error fetching detection data:", error);
  }
}
setInterval(updateData, 5000);
updateData();
</script>
</body>
</html>
'''

# HTML for the port selection page.
PORT_SELECTION_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Select USB Serial Port</title>
  <style>
    body {
      background-color: black;
      color: lime;
      font-family: monospace;
      text-align: center;
    }
    pre {
      font-size: 16px;
      margin: 20px auto;
    }
    form {
      display: inline-block;
      text-align: left;
    }
    li {
      list-style: none;
      margin: 10px 0;
    }
  </style>
</head>
<body>
  <pre>{{ ascii_art }}</pre>
  <h1>Select USB Serial Port</h1>
  <form method="POST" action="/select_port">
    <ul>
      {% for port in ports %}
        <li>
          <input type="radio" name="port" value="{{ port.device }}" required>
          {{ loop.index }}: {{ port.device }} - {{ port.description }}
        </li>
      {% endfor %}
    </ul>
    <button type="submit">Select Port</button>
  </form>
</body>
</html>
'''

from flask import render_template_string

# ASCII art to display at the top.
ASCII_ART = r"""
  \  |              |             __ \         |                |           
 |\/ |   _ \   __|  __ \          |   |   _ \  __|   _ \   __|  __|         
 |   |   __/ \__ \  | | | _____|  |   |   __/  |     __/   (     |           
_|_ \| \___| ____/ _| |_|        ____/ \\_|_| \__| \___| \___| \__|         
 |   |   __|  _ \   __ \    _ \       |\/ |   _` |  __ \   __ \    _ \   __|
 |   |  |    (   |  |   |   __/       |   |  (   |  |   |  |   |   __/  |   
____/  _|   \___/  _|  _| \___|      _|  _| \__,_|  .__/   .__/  \___| _|   
                                                   _|     _|                
"""

@app.route('/select_port', methods=['GET'])
def select_port():
    ports = list(serial.tools.list_ports.comports())
    return render_template_string(PORT_SELECTION_PAGE, ports=ports, ascii_art=ASCII_ART)

@app.route('/select_port', methods=['POST'])
def set_port():
    global SELECTED_PORT
    SELECTED_PORT = request.form.get('port')
    print("Selected Serial Port:", SELECTED_PORT)
    start_serial_thread()
    return redirect(url_for('index'))

@app.route('/')
def index():
    if SELECTED_PORT is None:
        return redirect(url_for('select_port'))
    return HTML_PAGE

@app.route('/api/detections', methods=['GET'])
def api_detections():
    return jsonify(tracked_pairs)

@app.route('/api/detections', methods=['POST'])
def post_detection():
    detection = request.get_json()
    update_detection(detection)
    return jsonify({"status": "ok"}), 200

def update_detection(detection):
    mac = detection.get("mac")
    if not mac:
        return
    detection["lat"] = detection.get("drone_lat", 0)
    detection["long"] = detection.get("drone_long", 0)
    detection["altitude"] = detection.get("drone_altitude", 0)
    detection["pilot_lat"] = detection.get("pilot_lat", 0)
    detection["pilot_long"] = detection.get("pilot_long", 0)
    detection["last_update"] = time.time()
    tracked_pairs[mac] = detection
    print("Updated tracked_pairs:", tracked_pairs)
    # Log detection to CSV.
    with open(CSV_FILENAME, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=['timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long', 'drone_altitude', 'pilot_lat', 'pilot_long'])
        log_row = {
            'timestamp': datetime.now().isoformat(),
            'mac': mac,
            'rssi': detection.get('rssi', ''),
            'drone_lat': detection.get('drone_lat', ''),
            'drone_long': detection.get('drone_long', ''),
            'drone_altitude': detection.get('drone_altitude', ''),
            'pilot_lat': detection.get('pilot_lat', ''),
            'pilot_long': detection.get('pilot_long', '')
        }
        writer.writerow(log_row)
    generate_kml()

def generate_kml():
    kml_lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<kml xmlns="http://www.opengis.net/kml/2.2">',
                 '<Document>',
                 f'<name>Detections {startup_timestamp}</name>']
    for mac, det in tracked_pairs.items():
        kml_lines.append(f'<Placemark><name>Drone {mac}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon></IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("drone_long",0)},{det.get("drone_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
        kml_lines.append(f'<Placemark><name>Pilot {mac}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon></IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("pilot_long",0)},{det.get("pilot_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
    kml_lines.append('</Document></kml>')
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated KML file:", KML_FILENAME)

import csv
startup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_FILENAME = f"detections_{startup_timestamp}.csv"
KML_FILENAME = f"detections_{startup_timestamp}.kml"
with open(CSV_FILENAME, mode='w', newline='') as csvfile:
    fieldnames = ['timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long', 'drone_altitude', 'pilot_lat', 'pilot_long']
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()
generate_kml()

def serial_reader():
    try:
        ser = serial.Serial(SELECTED_PORT, BAUD_RATE, timeout=1)
        print(f"Opened serial port {SELECTED_PORT} at {BAUD_RATE} baud.")
    except Exception as e:
        print(f"Error opening serial port {SELECTED_PORT}: {e}")
        return

    while True:
        try:
            if ser.in_waiting:
                line = ser.readline().decode('utf-8').strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    print("Ignoring non-JSON line:", line)
                    continue
                try:
                    detection = json.loads(line)
                    update_detection(detection)
                    print("Received detection:", detection)
                except json.JSONDecodeError:
                    print("Failed to decode JSON from line:", line)
            else:
                time.sleep(0.1)
        except Exception as e:
            print(f"Error reading serial: {e}")
            time.sleep(1)

def start_serial_thread():
    thread = threading.Thread(target=serial_reader, daemon=True)
    thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
