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
    fieldnames = [
        'timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long',
        'drone_altitude', 'pilot_lat', 'pilot_long'
    ]
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
        kml_lines.append(
            '<Style><IconStyle><scale>1.2</scale>'
            '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon>'
            '</IconStyle></Style>'
        )
        kml_lines.append(
            f'<Point><coordinates>{det.get("drone_long",0)},'
            f'{det.get("drone_lat",0)},0</coordinates></Point>'
        )
        kml_lines.append('</Placemark>')
        kml_lines.append(f'<Placemark><name>Pilot {mac}</name>')
        kml_lines.append(
            '<Style><IconStyle><scale>1.2</scale>'
            '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon>'
            '</IconStyle></Style>'
        )
        kml_lines.append(
            f'<Point><coordinates>{det.get("pilot_long",0)},'
            f'{det.get("pilot_lat",0)},0</coordinates></Point>'
        )
        kml_lines.append('</Placemark>')
    kml_lines.append('</Document></kml>')
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated KML file:", KML_FILENAME)

def update_detection(detection):
    mac = detection.get("mac")
    if not mac:
        return

    old_record = tracked_pairs.get(mac, {})

    # Update only if present, otherwise keep old values
    if "drone_lat" in detection:
        old_record["drone_lat"] = detection["drone_lat"]
    if "drone_long" in detection:
        old_record["drone_long"] = detection["drone_long"]
    if "drone_altitude" in detection:
        old_record["drone_altitude"] = detection["drone_altitude"]
    if "pilot_lat" in detection:
        old_record["pilot_lat"] = detection["pilot_lat"]
    if "pilot_long" in detection:
        old_record["pilot_long"] = detection["pilot_long"]

    # Also store simplified lat/long
    if "drone_lat" in detection:
        old_record["lat"] = detection["drone_lat"]
    elif "lat" not in old_record:
        old_record["lat"] = 0

    if "drone_long" in detection:
        old_record["long"] = detection["drone_long"]
    elif "long" not in old_record:
        old_record["long"] = 0

    if "rssi" in detection:
        old_record["rssi"] = detection["rssi"]

    # last update always
    old_record["last_update"] = time.time()
    tracked_pairs[mac] = old_record

    # Keep a record in detection_history
    detection_for_history = {
        "mac": mac,
        "rssi": old_record.get("rssi", ""),
        "drone_lat": old_record.get("drone_lat", 0),
        "drone_long": old_record.get("drone_long", 0),
        "drone_altitude": old_record.get("drone_altitude", 0),
        "pilot_lat": old_record.get("pilot_lat", 0),
        "pilot_long": old_record.get("pilot_long", 0),
        "last_update": old_record["last_update"]
    }
    detection_history.append(detection_for_history)

    print("Updated tracked_pairs:", tracked_pairs)

    # Append detection to CSV
    with open(CSV_FILENAME, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            'timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long',
            'drone_altitude', 'pilot_lat', 'pilot_long'
        ])
        writer.writerow({
            'timestamp': datetime.now().isoformat(),
            'mac': mac,
            'rssi': detection_for_history.get('rssi', ''),
            'drone_lat': detection_for_history.get('drone_lat', ''),
            'drone_long': detection_for_history.get('drone_long', ''),
            'drone_altitude': detection_for_history.get('drone_altitude', ''),
            'pilot_lat': detection_for_history.get('pilot_lat', ''),
            'pilot_long': detection_for_history.get('pilot_long', '')
        })

    generate_kml()

# ---------- HTML ----------
HTML_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Mesh Mapper</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <style>
    #map { height: 100vh; }
    body, html { margin: 0; padding: 0; }
    #layerControl {
      position: absolute;
      bottom: 10px;
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
      background-color: rgba(0,0,0,0.8);
      color: lime;
      border: none;
      padding: 3px;
    }
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
    .drone-item {
      display: inline-block;
      border: 1px solid;
      margin: 2px;
      padding: 3px;
      cursor: pointer;
    }
    .placeholder {
      border: 1px solid lime;
      min-height: 100px;
      margin-top: 10px;
      overflow-y: auto;
      max-height: 200px;
    }
    .selected {
      background-color: rgba(255,255,255,0.2);
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
  </select>
</div>
<div id="filterBox">
  <h3>Active Drones</h3>
  <div id="activePlaceholder" class="placeholder"></div>
  <h3>Inactive Drones</h3>
  <div id="inactivePlaceholder" class="placeholder"></div>
</div>

<script>
// =============== FRONTEND CONFIG & GLOBALS ===============
const STALE_THRESHOLD = 300; // 5 min
let persistentMACs = [];

// Track live markers, polylines, etc.
const droneMarkers = {};
const pilotMarkers = {};
const droneCircles = {};
const pilotCircles = {};
const dronePolylines = {};
const pilotPolylines = {};
const dronePathCoords = {};
const pilotPathCoords = {};
const droneBroadcastRings = {};

// For storing entire detection history from the server
// Example structure: fullHistoryByMac = { "ab:cd:ef": [ {lat, long, last_update, ...}, {...}, ... ] }
let fullHistoryByMac = {};

// For toggling historical mode
let historicalDrones = {};

let firstDetectionZoomed = false;


// =============== MAP INITIALIZATION ===============
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
  attribution: 'Tiles © Esri',
  maxZoom: 19
});
const esriWorldTopo = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri',
  maxZoom: 19
});
const esriDarkGray = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri',
  maxZoom: 16
});
const openTopoMap = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenTopoMap contributors',
  maxZoom: 17
});

const map = L.map('map', {
  center: [0, 0],
  zoom: 2,
  layers: [osmStandard]
});

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
  this.style.backgroundColor = "rgba(0,0,0,0.8)";
  this.style.color = "lime";
  setTimeout(() => {
    this.style.backgroundColor = "rgba(0,0,0,0.8)";
    this.style.color = "lime";
  }, 500);
});


// =============== FETCH + MANAGE HISTORICAL DATA ===============
async function fetchFullHistory() {
  try {
    const res = await fetch('/api/detections_history');
    const geojson = await res.json();
    // Clear out old data
    fullHistoryByMac = {};
    // Each feature has geometry.coordinates = [long, lat]
    for (const feat of geojson.features) {
      const mac = feat.properties.mac;
      if (!fullHistoryByMac[mac]) {
        fullHistoryByMac[mac] = [];
      }
      // We'll store it in simpler form
      const lat = feat.geometry.coordinates[1];
      const lng = feat.geometry.coordinates[0];
      const time = feat.properties.details.last_update || 0;
      // we can store pilot lat/long too if you want
      fullHistoryByMac[mac].push({
        lat: lat,
        lng: lng,
        time: time
      });
    }
    // Sort each MAC's array by time ascending
    for (const mac in fullHistoryByMac) {
      fullHistoryByMac[mac].sort((a,b)=> a.time - b.time);
    }
  } catch (e) {
    console.error("Failed to fetch detections_history", e);
  }
}

// Call this once on load, or periodically if you expect the detection history to keep changing
fetchFullHistory();

// =============== MAIN PERIODIC UPDATE: LIVE DATA ===============
async function updateData() {
  try {
    const response = await fetch('/api/detections');
    const data = await response.json();
    const currentTime = Date.now() / 1000;

    // Add new MACs to persistent list
    for (const mac in data) {
      if (!persistentMACs.includes(mac)) {
        persistentMACs.push(mac);
      }
    }

    for (const mac in data) {
      const det = data[mac];

      // If this MAC is in historical mode, check if we have new data to break out
      if (historicalDrones[mac]) {
        if (det.last_update > historicalDrones[mac].lockTime) {
          // Fresh data arrived after we locked => exit historical mode
          removeHistoricalLayers(mac); // remove polylines/rings from map
          delete historicalDrones[mac];
        } else {
          // Still forcibly in historical mode
          continue;
        }
      }

      // Stale check
      if (!det.last_update || (currentTime - det.last_update > STALE_THRESHOLD)) {
        removeDroneFromMap(mac);
        continue;
      }

      const droneLat = det.lat;
      const droneLng = det.long;
      const pilotLat = det.pilot_lat;
      const pilotLng = det.pilot_long;
      const validDrone = (droneLat !== 0 && droneLng !== 0);
      const validPilot = (pilotLat !== 0 && pilotLng !== 0);

      if (!validDrone && !validPilot) continue;

      if (!firstDetectionZoomed && validDrone) {
        firstDetectionZoomed = true;
        map.setView([droneLat, droneLng], 14);
      }

      // normal "live mode" updates...
      updateDroneMarker(mac, det);
      updatePilotMarker(mac, det);
    }

    updateComboList(data);
  } catch (error) {
    console.error("Error fetching detection data:", error);
  }
}

function removeHistoricalLayers(mac) {
  // If you used special historical polylines or rings, remove them here.
  if (droneBroadcastRings[mac]) {
    map.removeLayer(droneBroadcastRings[mac]);
    delete droneBroadcastRings[mac];
  }
  // Possibly remove a “historical polyline” or “historical marker” if you kept it separate.
}

// =============== UTILS TO UPDATE MARKERS ===============
function colorFromMac(mac) {
  let hash = 0;
  for (let i = 0; i < mac.length; i++) {
    hash = mac.charCodeAt(i) + ((hash << 5) - hash);
  }
  let h = Math.abs(hash) % 360;
  return 'hsl(' + h + ', 70%, 50%)';
}

function createIcon(emoji, color) {
  return L.divIcon({
    html: '<div style="font-size: 24px; color:' + color + ';">' + emoji + '</div>',
    className: '',
    iconSize: [30, 30],
    iconAnchor: [15, 15]
  });
}

function generatePopupContent(detection) {
  let content = '';
  for (const key in detection) {
    content += key + ': ' + detection[key] + '<br>';
  }
  return content;
}

function updateDroneMarker(mac, det) {
  const color = colorFromMac(mac);
  const droneLat = det.lat;
  const droneLng = det.long;
  if (droneLat && droneLng) {
    // Marker
    if (droneMarkers[mac]) {
      droneMarkers[mac].setLatLng([droneLat, droneLng]);
      droneMarkers[mac].setPopupContent(generatePopupContent(det));
    } else {
      droneMarkers[mac] = L.marker([droneLat, droneLng], {icon: createIcon('🛸', color)})
        .bindPopup(generatePopupContent(det))
        .addTo(map);
    }
    // Circle
    if (droneCircles[mac]) {
      droneCircles[mac].setLatLng([droneLat, droneLng]);
    } else {
      droneCircles[mac] = L.circleMarker([droneLat, droneLng], {
        radius: 12,
        color: color,
        fillColor: color,
        fillOpacity: 0.7
      }).addTo(map);
    }
    // Path
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

    // Broadcast ring if < 15s old
    const currentTime = Date.now()/1000;
    if (currentTime - det.last_update <= 15) {
      if (droneBroadcastRings[mac]) {
        droneBroadcastRings[mac].setLatLng([droneLat, droneLng]);
      } else {
        droneBroadcastRings[mac] = L.circleMarker([droneLat, droneLng], {
          radius: 16,
          color: "lime",
          fill: false,
          weight: 3
        }).addTo(map);
      }
    } else {
      if (droneBroadcastRings[mac]) {
        map.removeLayer(droneBroadcastRings[mac]);
        delete droneBroadcastRings[mac];
      }
    }
  }
}

function updatePilotMarker(mac, det) {
  const color = colorFromMac(mac);
  const pilotLat = det.pilot_lat;
  const pilotLng = det.pilot_long;
  if (pilotLat && pilotLng) {
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
      pilotCircles[mac] = L.circleMarker([pilotLat, pilotLng], {
        radius: 12,
        color: color,
        fillColor: color,
        fillOpacity: 0.7
      }).addTo(map);
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
    pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {
      color: color,
      dashArray: '5,5'
    }).addTo(map);
  }
}

function removeDroneFromMap(mac) {
  if (droneMarkers[mac]) { map.removeLayer(droneMarkers[mac]); delete droneMarkers[mac]; }
  if (pilotMarkers[mac]) { map.removeLayer(pilotMarkers[mac]); delete pilotMarkers[mac]; }
  if (droneCircles[mac]) { map.removeLayer(droneCircles[mac]); delete droneCircles[mac]; }
  if (pilotCircles[mac]) { map.removeLayer(pilotCircles[mac]); delete pilotCircles[mac]; }
  if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); delete dronePolylines[mac]; }
  if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); delete pilotPolylines[mac]; }
  if (droneBroadcastRings[mac]) { map.removeLayer(droneBroadcastRings[mac]); delete droneBroadcastRings[mac]; }
  delete dronePathCoords[mac];
  delete pilotPathCoords[mac];
}

// =============== HISTORICAL MODE ===============
function showFullHistorical(mac) {
  // Remove any existing historical layers
  removeDroneFromMap(mac);

  const color = colorFromMac(mac);
  // 1) Build a list of coords from fullHistoryByMac for that MAC
  let coords = [];
  if (fullHistoryByMac[mac]) {
    // We'll only add coords that are not 0,0
    coords = fullHistoryByMac[mac].filter(p => p.lat !== 0 && p.lng !== 0);
  }
  if (coords.length < 1) {
    console.log("No historical coords for MAC", mac);
    return;
  }
  // 2) Add as a polyline
  const latlngs = coords.map(c => [c.lat, c.lng]);
  const poly = L.polyline(latlngs, { color }).addTo(map);
  dronePolylines[mac] = poly; // re-use the same dictionary if you want

  // 3) Place a marker at the last coordinate
  const last = coords[coords.length - 1];
  const marker = L.marker([last.lat, last.lng], {
    icon: createIcon('🕒', color)
  }).addTo(map);
  droneMarkers[mac] = marker;
}

function toggleHistorical(mac, detection) {
  const currentTime = Date.now()/1000;
  if (historicalDrones[mac]) {
    // Turn off historical
    delete historicalDrones[mac];
    removeDroneFromMap(mac);
  } else {
    // Turn ON historical
    historicalDrones[mac] = {
      userLocked: true,
      lockTime: currentTime
    };
    // Show the full path from earliest to latest
    showFullHistorical(mac);
  }
}

// =============== UI LIST: Active vs Inactive ===============
function updateComboList(data) {
  const activePlaceholder = document.getElementById("activePlaceholder");
  const inactivePlaceholder = document.getElementById("inactivePlaceholder");
  activePlaceholder.innerHTML = "";
  inactivePlaceholder.innerHTML = "";
  const now = Date.now()/1000;

  persistentMACs.forEach(mac => {
    const item = document.createElement("div");
    item.textContent = mac;
    const color = colorFromMac(mac);
    item.style.borderColor = color;
    item.style.color = color;
    item.className = "drone-item";

    const det = data[mac];
    const isActive = det && (now - det.last_update <= STALE_THRESHOLD);

    if (isActive) {
      // Single click to zoom
      item.addEventListener("click", () => {
        if (det && det.lat && det.long && det.lat !== 0 && det.long !== 0) {
          map.setView([det.lat, det.long], 14);
        }
      });
      activePlaceholder.appendChild(item);
    } else {
      // Double-click for historical toggle
      item.addEventListener("dblclick", () => {
        toggleHistorical(mac, det);
        if (historicalDrones[mac]) {
          item.classList.add("selected");
        } else {
          item.classList.remove("selected");
        }
      });
      inactivePlaceholder.appendChild(item);
    }
  });
}

// =============== SCHEDULING ===============
setInterval(updateData, 1500);  // or 1000ms if you prefer
updateData();
</script>
</body>
</html>
'''

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
    """
    Returns a FeatureCollection of ALL detections from detection_history.
    The front-end uses this to build fullHistoryByMac for historical toggling.
    """
    features = []
    for det in detection_history:
        lat = det.get("drone_lat", 0)
        lng = det.get("drone_long", 0)
        # Skip worthless coords if you want
        # if lat == 0 and lng == 0: continue

        # We'll store them as a single Feature with geometry = drone lat/long.
        feat = {
            "type": "Feature",
            "properties": {
                "mac": det.get("mac"),
                "rssi": det.get("rssi"),
                "time": datetime.fromtimestamp(det.get("last_update")).isoformat() \
                        if det.get("last_update") else None,
                "details": det
            },
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat]
            }
        }
        features.append(feat)
    return jsonify({
        "type": "FeatureCollection",
        "features": features
    })

@app.route('/api/reactivate/<mac>', methods=['POST'])
def reactivate(mac):
    if mac in tracked_pairs:
        tracked_pairs[mac]['last_update'] = time.time()
        print(f"Reactivated {mac}")
        return jsonify({"status": "reactivated", "mac": mac})
    else:
        return jsonify({"status": "error", "message": "MAC not found"}), 404

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
