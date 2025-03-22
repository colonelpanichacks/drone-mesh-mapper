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
detection_history = []  # For CSV logging and KML generation

# Global variable to store the selected serial port.
SELECTED_PORT = None
BAUD_RATE = 115200

# Global stale threshold in seconds (default 5 minutes = 300 seconds).
staleThreshold = 300

# Create CSV and KML filenames using the current timestamp at startup.
startup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_FILENAME = f"detections_{startup_timestamp}.csv"
KML_FILENAME = f"detections_{startup_timestamp}.kml"

# Write CSV header.
with open(CSV_FILENAME, mode='w', newline='') as csvfile:
    fieldnames = ['timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long', 'drone_altitude', 'pilot_lat', 'pilot_long']
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()

def generate_kml():
    kml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        f'<name>Detections {startup_timestamp}</name>'
    ]
    for mac, det in tracked_pairs.items():
        kml_lines.append(f'<Placemark><name>Drone {mac}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale>'
                         '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon>'
                         '</IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("drone_long",0)},{det.get("drone_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
        kml_lines.append(f'<Placemark><name>Pilot {mac}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale>'
                         '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon>'
                         '</IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("pilot_long",0)},{det.get("pilot_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
    kml_lines.append('</Document></kml>')
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated KML file:", KML_FILENAME)

def update_detection(detection):
    mac = detection.get("mac")
    if not mac:
        return
    # Map incoming fields.
    detection["lat"] = detection.get("drone_lat", 0)
    detection["long"] = detection.get("drone_long", 0)
    detection["altitude"] = detection.get("drone_altitude", 0)
    detection["pilot_lat"] = detection.get("pilot_lat", 0)
    detection["pilot_long"] = detection.get("pilot_long", 0)
    detection["last_update"] = time.time()
    tracked_pairs[mac] = detection
    detection_history.append(detection.copy())
    print("Updated tracked_pairs:", tracked_pairs)
    # Append detection to CSV.
    with open(CSV_FILENAME, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=['timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long', 'drone_altitude', 'pilot_lat', 'pilot_long'])
        writer.writerow({
            'timestamp': datetime.now().isoformat(),
            'mac': mac,
            'rssi': detection.get('rssi', ''),
            'drone_lat': detection.get('drone_lat', ''),
            'drone_long': detection.get('drone_long', ''),
            'drone_altitude': detection.get('drone_altitude', ''),
            'pilot_lat': detection.get('pilot_lat', ''),
            'pilot_long': detection.get('pilot_long', '')
        })
    generate_kml()

# --- HTML Page with Layer Dropdown and Drone/Pilot Mapping ---
# This HTML contains the layer dropdown (top left) and a filter box (upper right).
# The detection mapping logic (markers, paths, zooming) remains unchanged.
HTML_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mesh Mapper</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <style>
    #map { height: 100vh; }
    body, html { margin: 0; padding: 0; }
    /* Top Left: Layer dropdown control */
    #layerControl {
      position: absolute;
      top: 10px;
      left: 10px;
      background: rgba(0,0,0,0.8);
      padding: 5px;
      border: 1px solid lime;
      border-radius: 10px;
      color: lime;
      font-family: monospace;
      z-index: 1000;
    }
    #layerControl select {
      background-color: purple;
      color: lime;
      border: none;
      padding: 3px;
    }
    /* Upper Right: Filter box for drone/pilot combos */
    #filterBox {
      position: absolute;
      top: 10px;
      right: 10px;
      background: rgba(0,0,0,0.8);
      padding: 10px;
      border: 1px solid lime;
      border-radius: 10px;
      color: lime;
      font-family: monospace;
      max-height: 80vh;
      overflow-y: auto;
      z-index: 1000;
    }
    #filterBox button {
      margin-top: 5px;
      /* Buttons now size to their text */
      background: lime;
      color: black;
      border: none;
      padding: 5px;
      cursor: pointer;
      font-family: monospace;
    }
    /* Highlight for selected drone/pilot combo in the list */
    .selected-combo {
      background-color: #ff00ff !important;  /* Hot cyberpunk purple */
      color: lime !important;
    }
  </style>
</head>
<body>
<div id="map"></div>
<div id="layerControl">
  <label>Basemap:</label>
  <select id="layerSelect">
    <option value="osmStandard">OSM Standard</option>
    <option value="osmHumanitarian">OSM Humanitarian</option>
    <option value="cartoPositron">CartoDB Positron</option>
    <option value="cartoDarkMatter">CartoDB Dark Matter</option>
    <option value="esriWorldImagery">Esri World Imagery</option>
    <option value="esriWorldTopo">Esri World TopoMap</option>
    <option value="esriDarkGray">Esri Dark Gray Canvas</option>
    <option value="openTopoMap">OpenTopoMap</option>
    <!-- Add more layers here if desired -->
  </select>
</div>
<div id="filterBox">
  <h3>Drone/Pilot Combos</h3>
  <ul id="comboList"></ul>
  <button id="filterButton">Filter by Selected Combo</button>
  <button id="clearFilterButton">Clear Filter</button>
</div>
<script>
// Define tile layers.
const osmStandard = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19
});
const osmHumanitarian = L.tileLayer('https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', {
  attribution: '© Humanitarian OpenStreetMap Team',
  maxZoom: 19
});
const cartoPositron = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap contributors, © CARTO',
  maxZoom: 19
});
const cartoDarkMatter = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap contributors, © CARTO',
  maxZoom: 19
});
const esriWorldImagery = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri — Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
  maxZoom: 19
});
const esriWorldTopo = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri — Source: USGS, NOAA, NAVTEQ, and Esri',
  maxZoom: 19
});
const esriDarkGray = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri — Esri, DeLorme, NAVTEQ',
  maxZoom: 16
});
const openTopoMap = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenTopoMap contributors',
  maxZoom: 17
});

// Initialize the map with the default layer.
const map = L.map('map', {
  center: [0, 0],
  zoom: 2,
  layers: [osmStandard]
});

// Handle basemap selection.
document.getElementById("layerSelect").addEventListener("change", function() {
  let value = this.value;
  let newLayer;
  if (value === "osmStandard") newLayer = osmStandard;
  else if (value === "osmHumanitarian") newLayer = osmHumanitarian;
  else if (value === "cartoPositron") newLayer = cartoPositron;
  else if (value === "cartoDarkMatter") newLayer = cartoDarkMatter;
  else if (value === "esriWorldImagery") newLayer = esriWorldImagery;
  else if (value === "esriWorldTopo") newLayer = esriWorldTopo;
  else if (value === "esriDarkGray") newLayer = esriDarkGray;
  else if (value === "openTopoMap") newLayer = openTopoMap;
  map.eachLayer(function(layer) {
    if (layer.options && layer.options.attribution) {
      map.removeLayer(layer);
    }
  });
  newLayer.addTo(map);
  // Visual feedback for selection.
  this.style.backgroundColor = "purple";
  this.style.color = "lime";
  setTimeout(() => {
    this.style.backgroundColor = "purple";
    this.style.color = "lime";
  }, 500);
});

// --- Filtering Logic ---
let filterMAC = null;
let comboList = document.getElementById("comboList");

// Update the combo list based on current detections.
function updateComboList(data, currentTime) {
  comboList.innerHTML = "";
  for (const mac in data) {
    if (!data[mac].last_update || (currentTime - data[mac].last_update > 300)) continue;
    let li = document.createElement("li");
    li.textContent = mac;
    li.style.cursor = "pointer";
    li.style.marginBottom = "5px";
    li.style.padding = "3px";
    li.style.border = "1px solid lime";
    li.addEventListener("click", function() {
      // Remove highlight from other items.
      let items = comboList.getElementsByTagName("li");
      for (let item of items) { item.classList.remove("selected-combo"); }
      li.classList.add("selected-combo");
      filterMAC = mac;
    });
    comboList.appendChild(li);
  }
}

// Utility to compute a unique color based on the MAC string.
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
    iconSize: [30, 30],
    iconAnchor: [15, 15]
  });
}

// Generate popup content from detection JSON.
function generatePopupContent(detection) {
  let content = '';
  for (const key in detection) {
    content += key + ': ' + detection[key] + '<br>';
  }
  return content;
}

// Global marker and path storage.
const droneMarkers = {};
const pilotMarkers = {};
const droneCircles = {};
const pilotCircles = {};
const dronePolylines = {};
const pilotPolylines = {};
const dronePathCoords = {};
const pilotPathCoords = {};

let firstDetectionZoomed = false;

async function updateData() {
  try {
    const response = await fetch('/api/detections');
    const data = await response.json();
    const currentTime = Date.now() / 1000;
    
    updateComboList(data, currentTime);
    
    for (const mac in data) {
      const det = data[mac];
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
      
      if (filterMAC && mac !== filterMAC) continue;
      
      const droneLat = det.lat, droneLng = det.long;
      const pilotLat = det.pilot_lat, pilotLng = det.pilot_long;
      
      const validDrone = (droneLat !== 0 && droneLng !== 0);
      const validPilot = (pilotLat !== 0 && pilotLng !== 0);
      if (!validDrone && !validPilot) continue;
      
      const color = colorFromMac(mac);
      
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
          // Use UFO emoji for drone.
          droneMarkers[mac] = L.marker([droneLat, droneLng], {icon: createIcon('🛸', color)})
                                .bindPopup(generatePopupContent(det))
                                .addTo(map);
          // Add click event to marker to select combo.
          droneMarkers[mac].on("click", function() {
            let items = comboList.getElementsByTagName("li");
            for (let item of items) { item.classList.remove("selected-combo"); }
            // Find the li corresponding to this MAC.
            for (let item of items) {
              if (item.textContent === mac) {
                item.classList.add("selected-combo");
                break;
              }
            }
            filterMAC = mac;
          });
        }
        if (droneCircles[mac]) {
          droneCircles[mac].setLatLng([droneLat, droneLng]);
        } else {
          droneCircles[mac] = L.circleMarker([droneLat, droneLng], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
                              .addTo(map);
        }
        if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
        const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
        if (!lastDrone || lastDrone[0] !== droneLat || lastDrone[1] !== droneLng) {
          dronePathCoords[mac].push([droneLat, droneLng]);
        }
        if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
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
          pilotMarkers[mac].on("click", function() {
            let items = comboList.getElementsByTagName("li");
            for (let item of items) { item.classList.remove("selected-combo"); }
            for (let item of items) {
              if (item.textContent === mac) {
                item.classList.add("selected-combo");
                break;
              }
            }
            filterMAC = mac;
          });
        }
        if (pilotCircles[mac]) {
          pilotCircles[mac].setLatLng([pilotLat, pilotLng]);
        } else {
          pilotCircles[mac] = L.circleMarker([pilotLat, pilotLng], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
                              .addTo(map);
        }
        if (!pilotPathCoords[mac]) { pilotPathCoords[mac] = []; }
        const lastPilot = pilotPathCoords[mac][pilotPathCoords[mac].length - 1];
        if (!lastPilot || lastPilot[0] !== pilotLat || lastPilot[1] !== pilotLng) {
          pilotPathCoords[mac].push([pilotLat, pilotLng]);
        }
        if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); }
        pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {color: color, dashArray: '5,5'}).addTo(map);
      }
    }
  } catch (error) {
    console.error("Error fetching detection data:", error);
  }
}
setInterval(updateData, 1000);
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

@app.route('/api/detections_history', methods=['GET'])
def api_detections_history():
    features = []
    for det in detection_history:
        if det.get("lat", 0) == 0 and det.get("long", 0) == 0:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "mac": det.get("mac"),
                "rssi": det.get("rssi"),
                "time": datetime.fromtimestamp(det.get("last_update")).isoformat(),
                "details": det
            },
            "geometry": {
                "type": "Point",
                "coordinates": [det.get("long"), det.get("lat")]
            }
        })
    return jsonify({
        "type": "FeatureCollection",
        "features": features
    })

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
