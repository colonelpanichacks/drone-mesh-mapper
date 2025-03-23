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
    """
    Update a detection in tracked_pairs and detection_history.

    IMPORTANT FIX:
    Instead of overwriting lat/long with 0 if missing, we preserve existing
    lat/long from the previously stored detection. This ensures the drone
    icon/path won't disappear simply because the next packet lacked location.
    """
    mac = detection.get("mac")
    if not mac:
        return

    # Fetch the old record if it exists, otherwise empty dict.
    old_record = tracked_pairs.get(mac, {})

    # Update fields only if the new data is present. Otherwise keep the old value.
    # For convenience, store them in the "drone_*" keys as well as a direct lat/long if needed.
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

    # Also store a simpler lat/long for easier usage on the frontend (mirrors "drone_*").
    if "drone_lat" in detection:
        old_record["lat"] = detection["drone_lat"]
    elif "lat" not in old_record:
        # If old_record never had lat, fallback to 0
        old_record["lat"] = 0

    if "drone_long" in detection:
        old_record["long"] = detection["drone_long"]
    elif "long" not in old_record:
        old_record["long"] = 0

    # Retain or update the RSSI if present
    if "rssi" in detection:
        old_record["rssi"] = detection["rssi"]

    # Always update last_update
    old_record["last_update"] = time.time()

    # Overwrite with the new data
    tracked_pairs[mac] = old_record

    # Also keep in detection_history
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

    # Append detection to CSV.
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

    # Update the KML with the new/updated info
    generate_kml()


# --- HTML Page with Layer Dropdown and Drone/Pilot Mapping ---
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
    /* Bottom Left: Layer dropdown control */
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
    /* Upper Right: Persistent scrollable list split into Active and Inactive drones */
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
    /* Styling for drone items: inline-block so they only take as much width as needed */
    .drone-item {
      display: inline-block;
      border: 1px solid;
      margin: 2px;
      padding: 3px;
      cursor: pointer;
    }
    /* Placeholder boxes that now will be populated with drones */
    .placeholder {
      border: 1px solid lime;
      min-height: 100px;
      margin-top: 10px;
      overflow-y: auto;
      max-height: 200px;
    }
    /* Highlighted inactive drone in historical view */
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
// Configurable stale threshold in seconds (matching your server's setting).
const STALE_THRESHOLD = 300;

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
  // Visual feedback.
  this.style.backgroundColor = "rgba(0,0,0,0.8)";
  this.style.color = "lime";
  setTimeout(() => {
    this.style.backgroundColor = "rgba(0,0,0,0.8)";
    this.style.color = "lime";
  }, 500);
});

// Global persistent MAC list.
let persistentMACs = [];

// Global marker and path storage.
const droneMarkers = {};
const pilotMarkers = {};
const droneCircles = {};
const pilotCircles = {};
const dronePolylines = {};
const pilotPolylines = {};
const dronePathCoords = {};
const pilotPathCoords = {};
// Storage for the broadcast rings.
const droneBroadcastRings = {};
// Global storage for drones in historical view.
let historicalDrones = {};

let firstDetectionZoomed = false;

// Function to zoom to a drone's location.
function zoomToDrone(mac, detection) {
  if (detection && detection.lat && detection.long && detection.lat != 0 && detection.long != 0) {
    map.setView([detection.lat, detection.long], 14);
  } else {
    alert("No valid drone location for " + mac);
  }
}

// Function to show an inactive drone in historical view.
// It draws the historical markers, paths, and a purple ring.
function showHistoricalDrone(mac, detection) {
  const color = colorFromMac(mac);
  // Drone marker and circle.
  if (!droneMarkers[mac]) {
    droneMarkers[mac] = L.marker([detection.lat, detection.long], {icon: createIcon('🛸', color)})
      .bindPopup(generatePopupContent(detection))
      .addTo(map);
  } else {
    droneMarkers[mac].setLatLng([detection.lat, detection.long]);
    droneMarkers[mac].setPopupContent(generatePopupContent(detection));
  }
  if (!droneCircles[mac]) {
    droneCircles[mac] = L.circleMarker([detection.lat, detection.long], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
      .addTo(map);
  } else {
    droneCircles[mac].setLatLng([detection.lat, detection.long]);
  }
  if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
  const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
  if (!lastDrone || lastDrone[0] !== detection.lat || lastDrone[1] !== detection.long) {
    dronePathCoords[mac].push([detection.lat, detection.long]);
  }
  if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
  dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);

  // Pilot marker and circle.
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    if (!pilotMarkers[mac]) {
      pilotMarkers[mac] = L.marker([detection.pilot_lat, detection.pilot_long], {icon: createIcon('👤', color)})
        .bindPopup(generatePopupContent(detection))
        .addTo(map);
    } else {
      pilotMarkers[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
      pilotMarkers[mac].setPopupContent(generatePopupContent(detection));
    }
    if (!pilotCircles[mac]) {
      pilotCircles[mac] = L.circleMarker([detection.pilot_lat, detection.pilot_long], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
        .addTo(map);
    } else {
      pilotCircles[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
    }
    if (!pilotPathCoords[mac]) { pilotPathCoords[mac] = []; }
    const lastPilot = pilotPathCoords[mac][pilotPathCoords[mac].length - 1];
    if (!lastPilot || lastPilot[0] !== detection.pilot_lat || lastPilot[1] !== detection.pilot_long) {
      pilotPathCoords[mac].push([detection.pilot_lat, detection.pilot_long]);
    }
    if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); }
    pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {color: color, dashArray: '5,5'}).addTo(map);
  }
  // Historical purple ring.
  if (droneBroadcastRings[mac]) { map.removeLayer(droneBroadcastRings[mac]); }
  droneBroadcastRings[mac] = L.circleMarker([detection.lat, detection.long], {
    radius: 16,
    color: "purple",
    fill: false,
    weight: 3
  }).addTo(map);
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

// Update the placeholders (active and inactive drones).
function updateComboList(data) {
  const activePlaceholder = document.getElementById("activePlaceholder");
  const inactivePlaceholder = document.getElementById("inactivePlaceholder");
  activePlaceholder.innerHTML = "";
  inactivePlaceholder.innerHTML = "";
  const currentTime = Date.now() / 1000;
  persistentMACs.forEach(mac => {
    const item = document.createElement("div");
    item.textContent = mac;
    const color = colorFromMac(mac);
    item.style.borderColor = color;
    item.style.color = color;
    item.className = "drone-item";
    
    let detection = data[mac];
    // Active if last_update <= STALE_THRESHOLD
    if (detection && (currentTime - detection.last_update <= STALE_THRESHOLD)) {
      // Single click to zoom.
      item.addEventListener("click", () => {
        zoomToDrone(mac, detection);
      });
      activePlaceholder.appendChild(item);
    } else {
      // Inactive: double-click to toggle historical.
      item.addEventListener("dblclick", () => {
        if (historicalDrones[mac]) {
          // Toggle off historical view.
          delete historicalDrones[mac];
          if (droneBroadcastRings[mac]) {
            map.removeLayer(droneBroadcastRings[mac]);
            delete droneBroadcastRings[mac];
          }
          item.classList.remove("selected");
        } else {
          // Turn on historical view and lock it.
          // We'll store the "lockTime" to allow ignoring new data if it arrives after toggling.
          historicalDrones[mac] = Object.assign({}, detection, {
            userLocked: true,
            lockTime: currentTime
          });
          showHistoricalDrone(mac, historicalDrones[mac]);
          item.classList.add("selected");
        }
      });
      inactivePlaceholder.appendChild(item);
    }
  });
}

async function updateData() {
  try {
    const response = await fetch('/api/detections');
    const data = await response.json();
    const currentTime = Date.now() / 1000;
    
    // Add any new MAC addresses to the persistent list.
    for (const mac in data) {
      if (!persistentMACs.includes(mac)) {
        persistentMACs.push(mac);
      }
    }
    
    // For each drone, check if it's in historical mode.
    for (const mac in data) {
      const det = data[mac];
      if (historicalDrones[mac]) {
        // If new detection arrives after user locked historical, or if >5 min from lock
        if (
          det.last_update > historicalDrones[mac].lockTime ||
          (currentTime - historicalDrones[mac].lockTime) > 300
        ) {
          // Stop historical mode so real data is used or it vanishes if stale
          delete historicalDrones[mac];
          if (droneBroadcastRings[mac]) {
            map.removeLayer(droneBroadcastRings[mac]);
            delete droneBroadcastRings[mac];
          }
        } else {
          // Still in historical mode; skip updating from live data
          continue;
        }
      }

      // If stale, remove from map entirely (unless user locked it above).
      if (!det.last_update || (currentTime - det.last_update > STALE_THRESHOLD)) {
        if (droneMarkers[mac]) { map.removeLayer(droneMarkers[mac]); delete droneMarkers[mac]; }
        if (pilotMarkers[mac]) { map.removeLayer(pilotMarkers[mac]); delete pilotMarkers[mac]; }
        if (droneCircles[mac]) { map.removeLayer(droneCircles[mac]); delete droneCircles[mac]; }
        if (pilotCircles[mac]) { map.removeLayer(pilotCircles[mac]); delete pilotCircles[mac]; }
        if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); delete dronePolylines[mac]; }
        if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); delete pilotPolylines[mac]; }
        if (droneBroadcastRings[mac]) { map.removeLayer(droneBroadcastRings[mac]); delete droneBroadcastRings[mac]; }
        delete dronePathCoords[mac];
        delete pilotPathCoords[mac];
        continue;
      }
      
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

      // Active drone marker & circle
      if (validDrone) {
        if (droneMarkers[mac]) {
          droneMarkers[mac].setLatLng([droneLat, droneLng]);
          droneMarkers[mac].setPopupContent(generatePopupContent(det));
        } else {
          droneMarkers[mac] = L.marker([droneLat, droneLng], {icon: createIcon('🛸', color)})
            .bindPopup(generatePopupContent(det))
            .addTo(map);
        }
        if (droneCircles[mac]) {
          droneCircles[mac].setLatLng([droneLat, droneLng]);
        } else {
          droneCircles[mac] = L.circleMarker([droneLat, droneLng], {
            radius: 12, color: color, fillColor: color, fillOpacity: 0.7
          }).addTo(map);
        }
        if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
        const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
        if (!lastDrone || lastDrone[0] !== droneLat || lastDrone[1] !== droneLng) {
          dronePathCoords[mac].push([droneLat, droneLng]);
        }
        if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
        dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);

        // Broadcast ring for active drones: lime if detected within 15s
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

      // Active pilot marker & circle
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
          pilotCircles[mac] = L.circleMarker([pilotLat, pilotLng], {
            radius: 12, color: color, fillColor: color, fillOpacity: 0.7
          }).addTo(map);
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
    updateComboList(data);
  } catch (error) {
    console.error("Error fetching detection data:", error);
  }
}

// Utility to create a custom icon.
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

// Poll the server every second
setInterval(updateData, 1000);
updateData();
</script>
</body>
</html>
'''

# --- HTML for the Port Selection Page ---
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
    features = []
    for det in detection_history:
        # If you only want historical features with actual lat/long:
        if det.get("drone_lat", 0) == 0 and det.get("drone_long", 0) == 0:
            continue
        features.append({
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
                "coordinates": [det.get("drone_long", 0), det.get("drone_lat", 0)]
            }
        })
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
