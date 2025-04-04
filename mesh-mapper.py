from flask import Flask, request, jsonify, redirect, url_for, render_template_string
import threading
import serial
import serial.tools.list_ports
import json
import time
import csv
import os
from datetime import datetime

app = Flask(__name__)

tracked_pairs = {}
detection_history = []  # For CSV logging and KML generation

# Global variable to store the selected serial port.
SELECTED_PORT = None
BAUD_RATE = 115200

# Global stale threshold in seconds (default 5 minutes = 300 seconds).
staleThreshold = 300

# Global variable for serial connection status.
serial_connected = False

# Create CSV and KML filenames using the current timestamp at startup.
startup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
CSV_FILENAME = f"detections_{startup_timestamp}.csv"
KML_FILENAME = f"detections_{startup_timestamp}.kml"

# Write CSV header.
with open(CSV_FILENAME, mode='w', newline='') as csvfile:
    fieldnames = ['timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long', 'drone_altitude', 'pilot_lat', 'pilot_long']
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()

# --- Alias Persistence ---
ALIASES_FILE = "aliases.json"
ALIASES = {}
if os.path.exists(ALIASES_FILE):
    try:
        with open(ALIASES_FILE, "r") as f:
            ALIASES = json.load(f)
    except Exception as e:
        print("Error loading aliases:", e)

def save_aliases():
    global ALIASES
    try:
        with open(ALIASES_FILE, "w") as f:
            json.dump(ALIASES, f)
    except Exception as e:
        print("Error saving aliases:", e)

# --- KML Generation ---
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

# --- Global Follow Lock Variable ---
followLock = {"type": None, "id": None, "enabled": False}

# --- HTML Page ---
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
    body, html { margin: 0; padding: 0; background-color: black; }
    #map { height: 100vh; }
    /* Basemap control styling */
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
      background-color: #333;
      color: lime;
      border: none;
      padding: 3px;
    }
    /* Filter box styling */
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
    #filterHeader {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    /* Serial connection status styling */
    #serialStatus {
      position: absolute;
      bottom: 30px;
      right: 10px;
      background: rgba(0,0,0,0.8);
      padding: 5px;
      border: 1px solid lime;
      border-radius: 10px;
      color: red;
      font-family: monospace;
      z-index: 1000;
    }
    /* Drone item styling */
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
    /* Leaflet popup dark mode styling */
    .leaflet-popup-content-wrapper {
      background-color: black;
      color: lime;
      font-family: monospace;
      border: 2px solid lime;
      border-radius: 10px;
    }
    .leaflet-popup-tip {
      background: lime;
    }
    /* Button dark mode styling */
    button {
      margin-top: 5px;
      padding: 5px;
      border: none;
      background-color: #333;
      color: lime;
      cursor: pointer;
    }
    /* General select styling for dark mode */
    select {
      background-color: #333;
      color: lime;
      border: none;
      padding: 3px;
    }
    /* Custom styling for Leaflet zoom controls */
    .leaflet-control-zoom-in, .leaflet-control-zoom-out {
      background-color: black;
      color: lime;
      border: 1px solid lime;
    }
    .leaflet-control-zoom-in:hover, .leaflet-control-zoom-out:hover {
      background-color: #222;
    }
    /* Styling for alias input */
    input#aliasInput {
      background-color: #222;
      color: #FF00FF;
      border: 1px solid #FF00FF;
      padding: 3px;
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
    <option value="cartoDarkMatter" selected>CartoDB Dark Matter</option>
    <option value="esriWorldImagery">Esri World Imagery</option>
    <option value="esriWorldTopo">Esri World TopoMap</option>
    <option value="esriDarkGray">Esri Dark Gray Canvas</option>
    <option value="openTopoMap">OpenTopoMap</option>
  </select>
</div>
<div id="filterBox">
  <div id="filterHeader">
    <h3 style="margin: 0;">Drones</h3>
    <span id="filterToggle" style="cursor: pointer; font-size: 20px;">[-]</span>
  </div>
  <div id="filterContent">
    <h3>Active Drones</h3>
    <div id="activePlaceholder" class="placeholder"></div>
    <h3>Inactive Drones</h3>
    <div id="inactivePlaceholder" class="placeholder"></div>
  </div>
</div>
<!-- Serial connection status display -->
<div id="serialStatus">Disconnected</div>
<script>
// Global aliases variable (fetched from the server)
var aliases = {};

// Function to fetch aliases from the server.
async function updateAliases() {
  try {
    const response = await fetch('/api/aliases');
    aliases = await response.json();
    // After updating aliases, update the list display.
    updateComboList(trackedPairs);
  } catch (error) {
    console.error("Error fetching aliases:", error);
  }
}

// New safeSetView: for initial zoom-in, always use zoom level 18.
function safeSetView(latlng, zoom=18) {
  map.setView(latlng, zoom);
}

// Global followLock variable shared among all markers.
var followLock = { type: null, id: null, enabled: false };

// --- Observer Popup Functions ---
function generateObserverPopup() {
  var observerLocked = (followLock.enabled && followLock.type === 'observer');
  return `
  <div>
    <strong>Observer Location</strong><br>
    <label for="observerEmoji">Select Observer Icon:</label>
    <select id="observerEmoji" onchange="updateObserverEmoji()">
       <option value="😎">😎</option>
       <option value="👽">👽</option>
       <option value="🤖">🤖</option>
       <option value="🏎️">🏎️</option>
       <option value="🕵️‍♂️">🕵️‍♂️</option>
       <option value="🥷">🥷</option>
       <option value="👁️">👁️</option>
    </select><br>
    <button id="lock-observer" onclick="lockObserver()" style="background-color: ${observerLocked ? 'green' : ''};">
      ${observerLocked ? 'Locked on Observer' : 'Lock on Observer'}
    </button>
    <button id="unlock-observer" onclick="unlockObserver()" style="background-color: ${observerLocked ? '' : 'green'};">
      ${observerLocked ? 'Unlock Observer' : 'Unlocked Observer'}
    </button>
  </div>
  `;
}

function updateObserverEmoji() {
  var select = document.getElementById("observerEmoji");
  var selectedEmoji = select.value;
  if(observerMarker) {
    observerMarker.setIcon(createIcon(selectedEmoji, 'blue'));
  }
}

function lockObserver() {
  followLock = { type: 'observer', id: 'observer', enabled: true };
  updateObserverPopupButtons();
}

function unlockObserver() {
  followLock = { type: null, id: null, enabled: false };
  updateObserverPopupButtons();
}

function updateObserverPopupButtons() {
  var observerLocked = (followLock.enabled && followLock.type === 'observer');
  var lockBtn = document.getElementById("lock-observer");
  var unlockBtn = document.getElementById("unlock-observer");
  if(lockBtn) {
    lockBtn.style.backgroundColor = observerLocked ? "green" : "";
    lockBtn.textContent = observerLocked ? "Locked on Observer" : "Lock on Observer";
  }
  if(unlockBtn) {
    unlockBtn.style.backgroundColor = observerLocked ? "" : "green";
    unlockBtn.textContent = observerLocked ? "Unlock Observer" : "Unlocked Observer";
  }
}

// --- Marker Popup Functions ---
function generatePopupContent(detection, markerType) {
  var isLocked = (followLock.enabled && followLock.type === markerType && followLock.id === detection.mac);
  var lockButton = `<button id="lock-${markerType}-${detection.mac}" onclick="lockMarker('${markerType}', '${detection.mac}')" style="background-color: ${isLocked ? 'green' : ''};">
                      ${isLocked ? 'Locked on ' + markerType.charAt(0).toUpperCase() + markerType.slice(1) : 'Lock on ' + markerType.charAt(0).toUpperCase() + markerType.slice(1)}
                    </button>`;
  var unlockButton = `<button id="unlock-${markerType}-${detection.mac}" onclick="unlockMarker('${markerType}', '${detection.mac}')" style="background-color: ${isLocked ? '' : 'green'};">
                      ${isLocked ? 'Unlock ' + markerType.charAt(0).toUpperCase() + markerType.slice(1) : 'Unlocked ' + markerType.charAt(0).toUpperCase() + markerType.slice(1)}
                    </button>`;
  let content = '';
  // Always display MAC and alias in the popup.
  let aliasText = aliases[detection.mac] ? aliases[detection.mac] : "No Alias";
  content += '<strong>ID:</strong> ' + aliasText + ' (MAC: ' + detection.mac + ')<br>';
  // Display other fields.
  for (const key in detection) {
    if (key !== 'mac') {
      content += key + ': ' + detection[key] + '<br>';
    }
  }
  // Append alias editor section with custom styling.
  // Added onclick and ontouchstart to stop propagation so the keyboard stays open on mobile.
  content += `<hr style="border: 1px solid lime;">
              <label for="aliasInput">Alias:</label>
              <input type="text" id="aliasInput" onclick="event.stopPropagation();" ontouchstart="event.stopPropagation();" style="background-color: #222; color: #FF00FF; border: 1px solid #FF00FF;" value="${aliases[detection.mac] ? aliases[detection.mac] : ''}"><br>
              <button onclick="saveAlias('${detection.mac}')">Save Alias</button>
              <button onclick="clearAlias('${detection.mac}')">Clear Alias</button><br>`;
  if (detection.drone_lat && detection.drone_long && (detection.drone_lat != 0 || detection.drone_long != 0)) {
    content += `<a href="https://www.google.com/maps/search/?api=1&query=${detection.drone_lat},${detection.drone_long}" target="_blank">Drone Location on Google Maps</a><br>`;
  }
  if (detection.pilot_lat && detection.pilot_long && (detection.pilot_lat != 0 || detection.pilot_long != 0)) {
    content += `<a href="https://www.google.com/maps/search/?api=1&query=${detection.pilot_lat},${detection.pilot_long}" target="_blank">Pilot Location on Google Maps</a><br>`;
  }
  if (markerType === 'drone' || markerType === 'pilot') {
    content += lockButton + unlockButton;
  }
  return content;
}

function lockMarker(markerType, id) {
  followLock = { type: markerType, id: id, enabled: true };
  updateMarkerButtons(markerType, id);
}

function unlockMarker(markerType, id) {
  if (followLock.enabled && followLock.type === markerType && followLock.id === id) {
    followLock = { type: null, id: null, enabled: false };
    updateMarkerButtons(markerType, id);
  }
}

function updateMarkerButtons(markerType, id) {
  var isLocked = (followLock.enabled && followLock.type === markerType && followLock.id === id);
  var lockBtn = document.getElementById("lock-" + markerType + "-" + id);
  var unlockBtn = document.getElementById("unlock-" + markerType + "-" + id);
  if(lockBtn) {
    lockBtn.style.backgroundColor = isLocked ? "green" : "";
    lockBtn.textContent = isLocked ? "Locked on " + markerType.charAt(0).toUpperCase() + markerType.slice(1) : "Lock on " + markerType.charAt(0).toUpperCase() + markerType.slice(1);
  }
  if(unlockBtn) {
    unlockBtn.style.backgroundColor = isLocked ? "" : "green";
    unlockBtn.textContent = isLocked ? "Unlock " + markerType.charAt(0).toUpperCase() + markerType.slice(1) : "Unlocked " + markerType.charAt(0).toUpperCase() + markerType.slice(1);
  }
}

// Function to open an alias editing popup when clicking a list item.
function openAliasPopup(mac) {
  let detection = trackedPairs[mac] || {};
  let latlng = null;
  if (droneMarkers[mac]) {
    latlng = droneMarkers[mac].getLatLng();
  } else if (pilotMarkers[mac]) {
    latlng = pilotMarkers[mac].getLatLng();
  } else {
    latlng = map.getCenter();
  }
  let content = generatePopupContent(Object.assign({mac: mac}, detection), 'alias');
  L.popup({className: 'leaflet-popup-content-wrapper'})
    .setLatLng(latlng)
    .setContent(content)
    .openOn(map);
}

// Functions to save and clear alias via server API.
async function saveAlias(mac) {
  let alias = document.getElementById("aliasInput").value;
  try {
    const response = await fetch('/api/set_alias', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mac: mac, alias: alias})
    });
    const data = await response.json();
    if (data.status === "ok") {
      updateAliases();
      let detection = trackedPairs[mac] || {mac: mac};
      let content = generatePopupContent(detection, 'alias');
      L.popup().setContent(content).openOn(map);
    }
  } catch (error) {
    console.error("Error saving alias:", error);
  }
}

async function clearAlias(mac) {
  try {
    const response = await fetch('/api/clear_alias/' + mac, {method: 'POST'});
    const data = await response.json();
    if (data.status === "ok") {
      updateAliases();
      let detection = trackedPairs[mac] || {mac: mac};
      let content = generatePopupContent(detection, 'alias');
      L.popup().setContent(content).openOn(map);
    }
  } catch (error) {
    console.error("Error clearing alias:", error);
  }
}

// --- Tile Layers Definition ---
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

// --- Initialize the Map ---
// Default to CartoDB Dark Matter.
const map = L.map('map', {
  center: [0, 0],
  zoom: 2,
  layers: [cartoDarkMatter]
});

// --- Basemap Selection Handling ---
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

// --- Global Variables for Markers and Paths ---
let persistentMACs = [];
const droneMarkers = {};
const pilotMarkers = {};
const droneCircles = {};
const pilotCircles = {};
const dronePolylines = {};
const pilotPolylines = {};
const dronePathCoords = {};
const pilotPathCoords = {};
const droneBroadcastRings = {};
let historicalDrones = {};
let firstDetectionZoomed = false;

// Observer marker.
let observerMarker = null;

// --- Observer Geolocation and Tracking ---
if (navigator.geolocation) {
  navigator.geolocation.watchPosition(function(position) {
    const lat = position.coords.latitude;
    const lng = position.coords.longitude;
    const observerIcon = createIcon('😎', 'blue');
    if (!observerMarker) {
      observerMarker = L.marker([lat, lng], {icon: observerIcon})
                        .bindPopup(generateObserverPopup())
                        .addTo(map)
                        .on('popupopen', function() {
                          updateObserverPopupButtons();
                        })
                        .on('click', function() {
                          safeSetView(observerMarker.getLatLng(), 18);
                        });
      safeSetView([lat, lng], 18);
    } else {
      observerMarker.setLatLng([lat, lng]);
    }
    if (followLock.enabled && followLock.type === 'observer') {
      map.setView([lat, lng], map.getZoom());
    }
  }, function(error) {
    console.error("Error watching location:", error);
  }, { enableHighAccuracy: true, maximumAge: 10000, timeout: 5000 });
} else {
  console.error("Geolocation is not supported by this browser.");
}

// --- Functions to Zoom and Display Historical Drones ---
function zoomToDrone(mac, detection) {
  if (detection && detection.lat && detection.long && detection.lat != 0 && detection.long != 0) {
    safeSetView([detection.lat, detection.long], 18);
  }
}

function showHistoricalDrone(mac, detection) {
  const color = colorFromMac(mac);
  if (!droneMarkers[mac]) {
    droneMarkers[mac] = L.marker([detection.lat, detection.long], {icon: createIcon('🛸', color)})
                           .bindPopup(generatePopupContent(detection, 'drone'))
                           .addTo(map)
                           .on('click', function(){
                              safeSetView(this.getLatLng(), 18);
                           });
  } else {
    droneMarkers[mac].setLatLng([detection.lat, detection.long]);
    droneMarkers[mac].setPopupContent(generatePopupContent(detection, 'drone'));
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
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    if (!pilotMarkers[mac]) {
      pilotMarkers[mac] = L.marker([detection.pilot_lat, detection.pilot_long], {icon: createIcon('👤', color)})
                             .bindPopup(generatePopupContent(detection, 'pilot'))
                             .addTo(map)
                             .on('click', function(){
                                 safeSetView(this.getLatLng(), 18);
                             });
    } else {
      pilotMarkers[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
      pilotMarkers[mac].setPopupContent(generatePopupContent(detection, 'pilot'));
    }
    if (!pilotCircles[mac]) {
      pilotCircles[mac] = L.circleMarker([detection.pilot_lat, detection.pilot_long], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
                            .addTo(map);
    } else {
      pilotCircles[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
    }
  }
}

// --- Utility Functions ---
function colorFromMac(mac) {
  let hash = 0;
  for (let i = 0; i < mac.length; i++) {
    hash = mac.charCodeAt(i) + ((hash << 5) - hash);
  }
  let h = Math.abs(hash) % 360;
  return 'hsl(' + h + ', 70%, 50%)';
}

// Update the active/inactive drone lists.
function updateComboList(data) {
  const activePlaceholder = document.getElementById("activePlaceholder");
  const inactivePlaceholder = document.getElementById("inactivePlaceholder");
  activePlaceholder.innerHTML = "";
  inactivePlaceholder.innerHTML = "";
  const currentTime = Date.now() / 1000;
  persistentMACs.forEach(mac => {
    const item = document.createElement("div");
    item.textContent = aliases[mac] ? aliases[mac] : mac;
    const color = colorFromMac(mac);
    item.style.borderColor = color;
    item.style.color = color;
    item.className = "drone-item";
    
    let detection = data[mac];
    if (detection && (currentTime - detection.last_update <= 300)) {
      item.addEventListener("click", () => {
        zoomToDrone(mac, detection);
      });
      item.addEventListener("dblclick", () => {
        openAliasPopup(mac);
      });
      activePlaceholder.appendChild(item);
    } else {
      item.addEventListener("dblclick", () => {
        if (historicalDrones[mac]) {
          delete historicalDrones[mac];
          if (droneBroadcastRings[mac]) {
            map.removeLayer(droneBroadcastRings[mac]);
            delete droneBroadcastRings[mac];
          }
          item.classList.remove("selected");
        } else {
          historicalDrones[mac] = Object.assign({}, detection, { userLocked: true, lockTime: Date.now()/1000 });
          showHistoricalDrone(mac, historicalDrones[mac]);
          item.classList.add("selected");
          safeSetView([detection.lat, detection.long], 18);
        }
      });
      item.addEventListener("click", () => {
        zoomToDrone(mac, detection);
      });
      inactivePlaceholder.appendChild(item);
    }
  });
}

async function updateData() {
  try {
    const response = await fetch('/api/detections');
    const data = await response.json();
    window.trackedPairs = data;
    const currentTime = Date.now() / 1000;
    for (const mac in data) {
      if (!persistentMACs.includes(mac)) {
        persistentMACs.push(mac);
      }
    }
    for (const mac in data) {
      if (historicalDrones[mac]) {
        if (data[mac].last_update > historicalDrones[mac].lockTime ||
            (currentTime - historicalDrones[mac].lockTime) > 300) {
          delete historicalDrones[mac];
          if (droneBroadcastRings[mac]) {
            map.removeLayer(droneBroadcastRings[mac]);
            delete droneBroadcastRings[mac];
          }
        } else {
          continue;
        }
      }
      const det = data[mac];
      if (!det.last_update || (currentTime - det.last_update > 300)) {
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
        safeSetView([droneLat, droneLng], 18);
      }
      if (validDrone) {
        if (droneMarkers[mac]) {
          droneMarkers[mac].setLatLng([droneLat, droneLng]);
          // Only update popup content if it's not open
          if (!droneMarkers[mac].isPopupOpen()) {
            droneMarkers[mac].setPopupContent(generatePopupContent(det, 'drone'));
          }
        } else {
          droneMarkers[mac] = L.marker([droneLat, droneLng], {icon: createIcon('🛸', color)})
                                .bindPopup(generatePopupContent(det, 'drone'))
                                .addTo(map)
                                .on('click', function(){
                                    safeSetView(this.getLatLng(), 18);
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
        if (followLock.enabled && followLock.type === 'drone' && followLock.id === mac) {
          map.setView([droneLat, droneLng], map.getZoom());
        }
      }
      if (validPilot) {
        if (pilotMarkers[mac]) {
          pilotMarkers[mac].setLatLng([pilotLat, pilotLng]);
          // Only update popup content if it's not open
          if (!pilotMarkers[mac].isPopupOpen()) {
            pilotMarkers[mac].setPopupContent(generatePopupContent(det, 'pilot'));
          }
        } else {
          pilotMarkers[mac] = L.marker([pilotLat, pilotLng], {icon: createIcon('👤', color)})
                                .bindPopup(generatePopupContent(det, 'pilot'))
                                .addTo(map)
                                .on('click', function(){
                                    safeSetView(this.getLatLng(), 18);
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
        if (followLock.enabled && followLock.type === 'pilot' && followLock.id === mac) {
          map.setView([pilotLat, pilotLng], map.getZoom());
        }
      }
    }
    updateComboList(data);
    updateAliases();
  } catch (error) {
    console.error("Error fetching detection data:", error);
  }
}

function createIcon(emoji, color) {
  return L.divIcon({
    html: '<div style="font-size: 24px; color:' + color + ';">' + emoji + '</div>',
    className: '',
    iconSize: [30, 30],
    iconAnchor: [15, 15]
  });
}

async function updateSerialStatus() {
  try {
    const response = await fetch('/api/serial_status');
    const data = await response.json();
    const statusDiv = document.getElementById('serialStatus');
    if (data.connected) {
      statusDiv.textContent = 'Connected';
      statusDiv.style.color = 'lime';
    } else {
      statusDiv.textContent = 'Disconnected';
      statusDiv.style.color = 'red';
    }
  } catch (error) {
    console.error("Error fetching serial status:", error);
  }
}
setInterval(updateSerialStatus, 1000);
updateSerialStatus();

setInterval(updateData, 1000);
updateData();

// New function to update the map view based on lock status instantly.
function updateLockFollow() {
  if (followLock.enabled) {
    if (followLock.type === 'observer' && observerMarker) {
      map.setView(observerMarker.getLatLng(), map.getZoom());
    } else if (followLock.type === 'drone' && droneMarkers[followLock.id]) {
      map.setView(droneMarkers[followLock.id].getLatLng(), map.getZoom());
    } else if (followLock.type === 'pilot' && pilotMarkers[followLock.id]) {
      map.setView(pilotMarkers[followLock.id].getLatLng(), map.getZoom());
    }
  }
}
setInterval(updateLockFollow, 200);

document.getElementById("filterToggle").addEventListener("click", function() {
  const content = document.getElementById("filterContent");
  if (content.style.display === "none") {
    content.style.display = "block";
    this.textContent = "[-]";
  } else {
    content.style.display = "none";
    this.textContent = "[+]";
  }
});
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
    select {
      background-color: #333;
      color: lime;
      border: none;
      padding: 3px;
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
    if (SELECTED_PORT is None):
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

@app.route('/api/reactivate/<mac>', methods=['POST'])
def reactivate(mac):
    if mac in tracked_pairs:
        tracked_pairs[mac]['last_update'] = time.time()
        print(f"Reactivated {mac}")
        return jsonify({"status": "reactivated", "mac": mac})
    else:
        return jsonify({"status": "error", "message": "MAC not found"}), 404

@app.route('/api/aliases', methods=['GET'])
def api_aliases():
    return jsonify(ALIASES)

@app.route('/api/set_alias', methods=['POST'])
def api_set_alias():
    data = request.get_json()
    mac = data.get("mac")
    alias = data.get("alias")
    if mac:
        ALIASES[mac] = alias
        save_aliases()
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "MAC missing"}), 400

@app.route('/api/clear_alias/<mac>', methods=['POST'])
def api_clear_alias(mac):
    if mac in ALIASES:
        del ALIASES[mac]
        save_aliases()
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "MAC not found"}), 404

@app.route('/api/serial_status', methods=['GET'])
def api_serial_status():
    return jsonify({"connected": serial_connected})

def serial_reader():
    global serial_connected
    ser = None
    while True:
        if ser is None or not ser.is_open:
            try:
                ser = serial.Serial(SELECTED_PORT, BAUD_RATE, timeout=1)
                serial_connected = True
                print(f"Opened serial port {SELECTED_PORT} at {BAUD_RATE} baud.")
            except Exception as e:
                serial_connected = False
                print(f"Error opening serial port {SELECTED_PORT}: {e}")
                time.sleep(1)
                continue

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
        except (serial.SerialException, OSError) as e:
            serial_connected = False
            print(f"SerialException/OSError: {e}")
            try:
                if ser and ser.is_open:
                    ser.close()
            except Exception as close_error:
                print(f"Error closing serial port: {close_error}")
            ser = None
            time.sleep(1)
        except Exception as e:
            serial_connected = False
            print(f"Error reading serial: {e}")
            try:
                if ser and ser.is_open:
                    ser.close()
            except Exception as close_error:
                print(f"Error closing serial port: {close_error}")
            ser = None
            time.sleep(1)

def start_serial_thread():
    thread = threading.Thread(target=serial_reader, daemon=True)
    thread.start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
