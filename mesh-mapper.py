#!/usr/bin/env python3
import requests
import json
import logging
import threading
import serial
import serial.tools.list_ports
import time
import csv
import os
from datetime import datetime
from flask import Flask, request, jsonify, redirect, url_for, render_template_string
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ----------------------
# Global Variables & Files
# ----------------------
tracked_pairs = {}
detection_history = []  # For CSV logging and KML generation

SELECTED_PORT = None
BAUD_RATE = 115200
staleThreshold = 300  # Global stale threshold in seconds (default 5 minutes)
serial_connected = False

startup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
# Updated detections CSV header to include faa_data.
CSV_FILENAME = f"detections_{startup_timestamp}.csv"
KML_FILENAME = f"detections_{startup_timestamp}.kml"
FAA_LOG_FILENAME = "faa_log.csv"  # FAA log CSV remains basic

# Write CSV header for detections.
with open(CSV_FILENAME, mode='w', newline='') as csvfile:
    fieldnames = [
        'timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long',
        'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
    ]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()

# Create FAA log CSV with header if not exists.
if not os.path.exists(FAA_LOG_FILENAME):
    with open(FAA_LOG_FILENAME, mode='w', newline='') as csvfile:
        fieldnames = ['timestamp', 'mac', 'remote_id', 'faa_response']
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

# ----------------------
# FAA Cache Persistence
# ----------------------
FAA_CACHE_FILE = "faa_cache.csv"
FAA_CACHE = {}

# Load FAA cache from file
if os.path.exists(FAA_CACHE_FILE):
    try:
        with open(FAA_CACHE_FILE, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                key = (row['mac'], row['remote_id'])
                FAA_CACHE[key] = json.loads(row['faa_response'])
    except Exception as e:
        print("Error loading FAA cache:", e)

def write_to_faa_cache(mac, remote_id, faa_data):
    key = (mac, remote_id)
    FAA_CACHE[key] = faa_data
    try:
        file_exists = os.path.isfile(FAA_CACHE_FILE)
        with open(FAA_CACHE_FILE, "a", newline='') as csvfile:
            fieldnames = ["mac", "remote_id", "faa_response"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "mac": mac,
                "remote_id": remote_id,
                "faa_response": json.dumps(faa_data)
            })
    except Exception as e:
        print("Error writing to FAA cache:", e)

# ----------------------
# KML Generation (including FAA data)
# ----------------------
def generate_kml():
    kml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        f'<name>Detections {startup_timestamp}</name>'
    ]
    for mac, det in tracked_pairs.items():
        remoteIdStr = ""
        if det.get("basic_id"):
            remoteIdStr = " (RemoteID: " + det.get("basic_id") + ")"
        if det.get("faa_data"):
            remoteIdStr += " FAA: " + json.dumps(det.get("faa_data"))
        # Drone placemark
        kml_lines.append(f'<Placemark><name>Drone {mac}{remoteIdStr}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale>'
                         '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon>'
                         '</IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("drone_long",0)},{det.get("drone_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
        # Pilot placemark
        kml_lines.append(f'<Placemark><name>Pilot {mac}{remoteIdStr}</name>')
        kml_lines.append('<Style><IconStyle><scale>1.2</scale>'
                         '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon>'
                         '</IconStyle></Style>')
        kml_lines.append(f'<Point><coordinates>{det.get("pilot_long",0)},{det.get("pilot_lat",0)},0</coordinates></Point>')
        kml_lines.append('</Placemark>')
    kml_lines.append('</Document></kml>')
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated KML file:", KML_FILENAME)

# ----------------------
# Detection Update & CSV Logging
# ----------------------
def update_detection(detection):
    mac = detection.get("mac")
    remote_id = detection.get("basic_id")
    if not mac:
        return
    detection["drone_lat"] = detection.get("drone_lat", 0)
    detection["drone_long"] = detection.get("drone_long", 0)
    detection["drone_altitude"] = detection.get("drone_altitude", 0)
    detection["pilot_lat"] = detection.get("pilot_lat", 0)
    detection["pilot_long"] = detection.get("pilot_long", 0)
    detection["last_update"] = time.time()
    if mac and remote_id:
        cached = FAA_CACHE.get((mac, remote_id))
        if cached:
            detection["faa_data"] = cached
    if mac in tracked_pairs and "faa_data" in tracked_pairs[mac]:
        detection["faa_data"] = tracked_pairs[mac]["faa_data"]
    tracked_pairs[mac] = detection
    detection_history.append(detection.copy())
    print("Updated tracked_pairs:", tracked_pairs)
    with open(CSV_FILENAME, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            'timestamp', 'mac', 'rssi', 'drone_lat', 'drone_long',
            'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
        ])
        writer.writerow({
            'timestamp': datetime.now().isoformat(),
            'mac': mac,
            'rssi': detection.get('rssi', ''),
            'drone_lat': detection.get('drone_lat', ''),
            'drone_long': detection.get('drone_long', ''),
            'drone_altitude': detection.get('drone_altitude', ''),
            'pilot_lat': detection.get('pilot_lat', ''),
            'pilot_long': detection.get('pilot_long', ''),
            'basic_id': detection.get('basic_id', ''),
            'faa_data': json.dumps(detection.get('faa_data', {}))
        })
    generate_kml()

# ----------------------
# Global Follow Lock & Color Overrides
# ----------------------
followLock = {"type": None, "id": None, "enabled": False}
colorOverrides = {}

# ----------------------
# FAA Query Helper Functions
# ----------------------
def create_retry_session(retries=3, backoff_factor=2, status_forcelist=(502, 503, 504)):
    logging.debug("Creating retry-enabled session with custom headers for FAA query.")
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://uasdoc.faa.gov/listdocs",
        "client": "external"
    })
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session

def refresh_cookie(session):
    homepage_url = "https://uasdoc.faa.gov/listdocs"
    logging.debug("Refreshing FAA cookie by requesting homepage: %s", homepage_url)
    try:
        response = session.get(homepage_url, timeout=30)
        logging.debug("FAA homepage response code: %s", response.status_code)
    except requests.exceptions.RequestException as e:
        logging.exception("Error refreshing FAA cookie: %s", e)

def query_remote_id(session, remote_id):
    endpoint = "https://uasdoc.faa.gov/api/v1/serialNumbers"
    params = {
        "itemsPerPage": 8,
        "pageIndex": 0,
        "orderBy[0]": "updatedAt",
        "orderBy[1]": "DESC",
        "findBy": "serialNumber",
        "serialNumber": remote_id
    }
    logging.debug("Querying FAA API endpoint: %s with params: %s", endpoint, params)
    try:
        response = session.get(endpoint, params=params, timeout=30)
        logging.debug("FAA Request URL: %s", response.url)
        if response.status_code != 200:
            logging.error("FAA HTTP error: %s - %s", response.status_code, response.reason)
            return None
        return response.json()
    except Exception as e:
        logging.exception("Error querying FAA API: %s", e)
        return None

# ----------------------
# New FAA Query API Endpoint
# ----------------------
@app.route('/api/query_faa', methods=['POST'])
def api_query_faa():
    data = request.get_json()
    mac = data.get("mac")
    remote_id = data.get("remote_id")
    if not mac or not remote_id:
        return jsonify({"status": "error", "message": "Missing mac or remote_id"}), 400
    session = create_retry_session()
    refresh_cookie(session)
    faa_result = query_remote_id(session, remote_id)
    if faa_result is None:
        return jsonify({"status": "error", "message": "FAA query failed"}), 500
    if mac in tracked_pairs:
        tracked_pairs[mac]["faa_data"] = faa_result
    else:
        tracked_pairs[mac] = {"basic_id": remote_id, "faa_data": faa_result}
    write_to_faa_cache(mac, remote_id, faa_result)
    timestamp = datetime.now().isoformat()
    try:
        with open(FAA_LOG_FILENAME, "a", newline='') as csvfile:
            fieldnames = ["timestamp", "mac", "remote_id", "faa_response"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writerow({
                "timestamp": timestamp,
                "mac": mac,
                "remote_id": remote_id,
                "faa_response": json.dumps(faa_result)
            })
    except Exception as e:
        print("Error writing to FAA log CSV:", e)
    generate_kml()
    return jsonify({"status": "ok", "faa_data": faa_result})

# ----------------------
# HTML & JS (UI) Section
# ----------------------
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
    #layerControl select { background-color: #333; color: lime; border: none; padding: 3px; }
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
    #filterHeader { display: flex; justify-content: space-between; align-items: center; }
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
    .selected { background-color: rgba(255,255,255,0.2); }
    .leaflet-popup-content-wrapper { background-color: black; color: lime; font-family: monospace; border: 2px solid lime; border-radius: 10px; }
    .leaflet-popup-tip { background: lime; }
    button { margin-top: 5px; padding: 5px; border: none; background-color: #333; color: lime; cursor: pointer; }
    select { background-color: #333; color: lime; border: none; padding: 3px; }
    .leaflet-control-zoom-in, .leaflet-control-zoom-out { background-color: black; color: lime; border: 1px solid lime; }
    .leaflet-control-zoom-in:hover, .leaflet-control-zoom-out:hover { background-color: #222; }
    input#aliasInput { background-color: #222; color: #FF00FF; border: 1px solid #FF00FF; padding: 3px; }
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
<div id="serialStatus">Disconnected</div>
<script>
// Persisted data load.
if (localStorage.getItem('colorOverrides')) {
  try { window.colorOverrides = JSON.parse(localStorage.getItem('colorOverrides')); }
  catch(e){ window.colorOverrides = {}; }
} else { window.colorOverrides = {}; }

if (localStorage.getItem('historicalDrones')) {
  try { window.historicalDrones = JSON.parse(localStorage.getItem('historicalDrones')); }
  catch(e){ window.historicalDrones = {}; }
} else { window.historicalDrones = {}; }

let persistedCenter = localStorage.getItem('mapCenter');
let persistedZoom = localStorage.getItem('mapZoom');
if (persistedCenter) {
  try { persistedCenter = JSON.parse(persistedCenter); } catch(e){ persistedCenter = null; }
} else { persistedCenter = null; }
persistedZoom = persistedZoom ? parseInt(persistedZoom) : null;

var aliases = {};
var colorOverrides = window.colorOverrides;
const STALE_THRESHOLD = 300;
var comboListItems = {};

async function updateAliases() {
  try {
    const response = await fetch('/api/aliases');
    aliases = await response.json();
    updateComboList(window.tracked_pairs);
  } catch (error) { console.error("Error fetching aliases:", error); }
}

function safeSetView(latlng, zoom=18) {
  let currentZoom = map.getZoom();
  let newZoom = zoom > currentZoom ? zoom : currentZoom;
  map.setView(latlng, newZoom);
}

var followLock = { type: null, id: null, enabled: false };

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
  if(observerMarker) { observerMarker.setIcon(createIcon('😎', 'blue')); }
}

function lockObserver() { followLock = { type: 'observer', id: 'observer', enabled: true }; updateObserverPopupButtons(); }
function unlockObserver() { followLock = { type: null, id: null, enabled: false }; updateObserverPopupButtons(); }
function updateObserverPopupButtons() {
  var observerLocked = (followLock.enabled && followLock.type === 'observer');
  var lockBtn = document.getElementById("lock-observer");
  var unlockBtn = document.getElementById("unlock-observer");
  if(lockBtn) { lockBtn.style.backgroundColor = observerLocked ? "green" : ""; lockBtn.textContent = observerLocked ? "Locked on Observer" : "Lock on Observer"; }
  if(unlockBtn) { unlockBtn.style.backgroundColor = observerLocked ? "" : "green"; unlockBtn.textContent = observerLocked ? "Unlock Observer" : "Unlocked Observer"; }
}

function generatePopupContent(detection, markerType) {
  let content = '';
  let aliasText = aliases[detection.mac] ? aliases[detection.mac] : "No Alias";
  content += '<strong>ID:</strong> <span style="color:#FF00FF;">' + aliasText + '</span> (MAC: ' + detection.mac + ')<br>';
  
  if (detection.basic_id) {
    content += '<div style="border:2px solid #FF00FF; padding:5px; margin:5px 0;">FAA RemoteID: ' + detection.basic_id + '</div>';
    // Button for querying FAA API.
    content += '<button onclick="queryFaaAPI(\\\'' + detection.mac + '\\\', \\\'' + detection.basic_id + '\\\')" id="queryFaaButton_' + detection.mac + '">Query FAA API</button>';
    // FAA data display container.
    content += '<div id="faaResult_' + detection.mac + '" style="margin-top:5px;">';
    if (detection.faa_data) {
      let faaData = detection.faa_data;
      let item = null;
      if (faaData.data && faaData.data.items && faaData.data.items.length > 0) {
        item = faaData.data.items[0];
      }
      if (item) {
        // Only display specific fields in the desired order.
        const fields = ["makeName", "modelName", "series", "trackingNumber", "complianceCategories", "updatedAt"];
        content += '<div style="border:2px solid #FF69B4; padding:5px; margin:5px 0;">';
        fields.forEach(function(field) {
          let value = item[field] !== undefined ? item[field] : "";
          content += `<div><span style="color:#FF00FF;">${field}:</span> <span style="color:#00FF00;">${value}</span></div>`;
        });
        content += '</div>';
      } else {
        content += '<div style="border:2px solid #FF69B4; padding:5px; margin:5px 0;">No FAA data available</div>';
      }
    }
    content += '</div><br>';
  }
  
  for (const key in detection) {
    if (['mac', 'basic_id', 'last_update', 'userLocked', 'lockTime', 'faa_data'].indexOf(key) === -1) {
      content += key + ': ' + detection[key] + '<br>';
    }
  }
  
  if (detection.drone_lat && detection.drone_long && detection.drone_lat != 0 && detection.drone_long != 0) {
    content += '<a target="_blank" href="https://www.google.com/maps/search/?api=1&query=' 
             + detection.drone_lat + ',' + detection.drone_long + '">View Drone on Google Maps</a><br>';
  }
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    content += '<a target="_blank" href="https://www.google.com/maps/search/?api=1&query=' 
             + detection.pilot_lat + ',' + detection.pilot_long + '">View Pilot on Google Maps</a><br>';
  }
  
  content += `<hr style="border: 1px solid lime;">
              <label for="aliasInput">Alias:</label>
              <input type="text" id="aliasInput" onclick="event.stopPropagation();" ontouchstart="event.stopPropagation();" 
                     style="background-color: #222; color: #FF00FF; border: 1px solid #FF00FF;" 
                     value="${aliases[detection.mac] ? aliases[detection.mac] : ''}"><br>
              <button onclick="saveAlias('${detection.mac}')">Save Alias</button>
              <button onclick="clearAlias('${detection.mac}')">Clear Alias</button><br>`;
  
  content += `<div style="border-top:2px solid lime; margin:10px 0;"></div>`;
  
  var isDroneLocked = (followLock.enabled && followLock.type === 'drone' && followLock.id === detection.mac);
  var droneLockButton = `<button id="lock-drone-${detection.mac}" onclick="lockMarker('drone', '${detection.mac}')" 
                      style="background-color: ${isDroneLocked ? 'green' : ''};">
                      ${isDroneLocked ? 'Locked on Drone' : 'Lock on Drone'}
                    </button>`;
  var droneUnlockButton = `<button id="unlock-drone-${detection.mac}" onclick="unlockMarker('drone', '${detection.mac}')" 
                      style="background-color: ${isDroneLocked ? '' : 'green'};">
                      ${isDroneLocked ? 'Unlock Drone' : 'Unlocked Drone'}
                    </button>`;
  var isPilotLocked = (followLock.enabled && followLock.type === 'pilot' && followLock.id === detection.mac);
  var pilotLockButton = `<button id="lock-pilot-${detection.mac}" onclick="lockMarker('pilot', '${detection.mac}')" 
                      style="background-color: ${isPilotLocked ? 'green' : ''};">
                      ${isPilotLocked ? 'Locked on Pilot' : 'Lock on Pilot'}
                    </button>`;
  var pilotUnlockButton = `<button id="unlock-pilot-${detection.mac}" onclick="unlockMarker('pilot', '${detection.mac}')" 
                      style="background-color: ${isPilotLocked ? '' : 'green'};">
                      ${isPilotLocked ? 'Unlock Pilot' : 'Unlocked Pilot'}
                    </button>`;
  content += `${droneLockButton} ${droneUnlockButton} <br>
                ${pilotLockButton} ${pilotUnlockButton}`;
  
  let defaultHue = colorOverrides[detection.mac] !== undefined ? colorOverrides[detection.mac] : (function(){
      let hash = 0;
      for (let i = 0; i < detection.mac.length; i++){
          hash = detection.mac.charCodeAt(i) + ((hash << 5) - hash);
      }
      return Math.abs(hash) % 360;
  })();
  content += `<div style="margin-top:10px;">
    <label for="colorSlider_${detection.mac}" style="display:block; color:lime;">Color:</label>
    <input type="range" id="colorSlider_${detection.mac}" min="0" max="360" value="${defaultHue}" style="width:100%;" onchange="updateColor('${detection.mac}', this.value)">
  </div>`;
  
  return content;
}

// New function to query the FAA API.
async function queryFaaAPI(mac, remote_id) {
    const button = document.getElementById("queryFaaButton_" + mac);
    if (button) {
        button.disabled = true;
        const originalText = button.textContent;
        button.textContent = "Querying...";
        button.style.backgroundColor = "gray";
    }
    try {
        const response = await fetch('/api/query_faa', {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mac: mac, remote_id: remote_id})
        });
        const result = await response.json();
        if (result.status === "ok") {
            const faaDiv = document.getElementById("faaResult_" + mac);
            if (faaDiv) {
                let faaData = result.faa_data;
                let item = null;
                if (faaData.data && faaData.data.items && faaData.data.items.length > 0) {
                  item = faaData.data.items[0];
                }
                if (item) {
                  const fields = ["makeName", "modelName", "series", "trackingNumber", "complianceCategories", "updatedAt"];
                  let html = '<div style="border:2px solid #FF69B4; padding:5px; margin:5px 0;">';
                  fields.forEach(function(field) {
                    let value = item[field] !== undefined ? item[field] : "";
                    html += `<div><span style="color:#FF00FF;">${field}:</span> <span style="color:#00FF00;">${value}</span></div>`;
                  });
                  html += '</div>';
                  faaDiv.innerHTML = html;
                } else {
                  faaDiv.innerHTML = '<div style="border:2px solid #FF69B4; padding:5px; margin:5px 0;">No FAA data available</div>';
                }
            }
        } else {
            alert("FAA API error: " + result.message);
        }
    } catch(error) {
        console.error("Error querying FAA API:", error);
    } finally {
        const button = document.getElementById("queryFaaButton_" + mac);
        if (button) {
            button.disabled = false;
            button.style.backgroundColor = "#333";
            button.textContent = "Query FAA API";
        }
    }
}

function lockMarker(markerType, id) {
  followLock = { type: markerType, id: id, enabled: true };
  updateMarkerButtons('drone', id);
  updateMarkerButtons('pilot', id);
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
  if(lockBtn) { lockBtn.style.backgroundColor = isLocked ? "green" : ""; lockBtn.textContent = isLocked ? "Locked on " + markerType.charAt(0).toUpperCase() + markerType.slice(1) : "Lock on " + markerType.charAt(0).toUpperCase() + markerType.slice(1); }
  if(unlockBtn) { unlockBtn.style.backgroundColor = isLocked ? "" : "green"; unlockBtn.textContent = isLocked ? "Unlock " + markerType.charAt(0).toUpperCase() + markerType.slice(1) : "Unlocked " + markerType.charAt(0).toUpperCase() + markerType.slice(1); }
}

function openAliasPopup(mac) {
  let detection = window.tracked_pairs[mac] || {};
  let latlng = null;
  if (droneMarkers[mac]) { latlng = droneMarkers[mac].getLatLng(); }
  else if (pilotMarkers[mac]) { latlng = pilotMarkers[mac].getLatLng(); }
  else { latlng = map.getCenter(); }
  let content = generatePopupContent(Object.assign({mac: mac}, detection), 'alias');
  L.popup({className: 'leaflet-popup-content-wrapper'})
    .setLatLng(latlng)
    .setContent(content)
    .openOn(map);
}

async function saveAlias(mac) {
  let alias = document.getElementById("aliasInput").value;
  try {
    const response = await fetch('/api/set_alias', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mac: mac, alias: alias}) });
    const data = await response.json();
    if (data.status === "ok") {
      updateAliases();
      let detection = window.tracked_pairs[mac] || {mac: mac};
      let content = generatePopupContent(detection, 'alias');
      L.popup().setContent(content).openOn(map);
    }
  } catch (error) { console.error("Error saving alias:", error); }
}

async function clearAlias(mac) {
  try {
    const response = await fetch('/api/clear_alias/' + mac, {method: 'POST'});
    const data = await response.json();
    if (data.status === "ok") {
      updateAliases();
      let detection = window.tracked_pairs[mac] || {mac: mac};
      let content = generatePopupContent(detection, 'alias');
      L.popup().setContent(content).openOn(map);
    }
  } catch (error) { console.error("Error clearing alias:", error); }
}

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
  center: persistedCenter || [0, 0],
  zoom: persistedZoom || 2,
  layers: [cartoDarkMatter]
});

map.on('moveend', function() {
  let center = map.getCenter();
  let zoom = map.getZoom();
  localStorage.setItem('mapCenter', JSON.stringify(center));
  localStorage.setItem('mapZoom', zoom);
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
    if (layer.options && layer.options.attribution) { map.removeLayer(layer); }
  });
  newLayer.addTo(map);
  this.style.backgroundColor = "rgba(0,0,0,0.8)";
  this.style.color = "lime";
  setTimeout(() => { this.style.backgroundColor = "rgba(0,0,0,0.8)"; this.style.color = "lime"; }, 500);
});

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
let historicalDrones = window.historicalDrones;
let firstDetectionZoomed = false;

let observerMarker = null;

if (navigator.geolocation) {
  navigator.geolocation.watchPosition(function(position) {
    const lat = position.coords.latitude;
    const lng = position.coords.longitude;
    const observerIcon = createIcon('😎', 'blue');
    if (!observerMarker) {
      observerMarker = L.marker([lat, lng], {icon: observerIcon})
                        .bindPopup(generateObserverPopup())
                        .addTo(map)
                        .on('popupopen', function() { updateObserverPopupButtons(); })
                        .on('click', function() { safeSetView(observerMarker.getLatLng(), 18); });
      safeSetView([lat, lng], 18);
    } else { observerMarker.setLatLng([lat, lng]); }
    if (followLock.enabled && followLock.type === 'observer') { map.setView([lat, lng], map.getZoom()); }
  }, function(error) { console.error("Error watching location:", error); }, { enableHighAccuracy: true, maximumAge: 10000, timeout: 5000 });
} else { console.error("Geolocation is not supported by this browser."); }

function zoomToDrone(mac, detection) {
  if (detection && detection.drone_lat && detection.drone_long && detection.drone_lat != 0 && detection.drone_long != 0) {
    safeSetView([detection.drone_lat, detection.drone_long], 18);
  }
}

function showHistoricalDrone(mac, detection) {
  const color = get_color_for_mac(mac);
  if (!droneMarkers[mac]) {
    droneMarkers[mac] = L.marker([detection.drone_lat, detection.drone_long], {icon: createIcon('🛸', color)})
                           .bindPopup(generatePopupContent(detection, 'drone'))
                           .addTo(map)
                           .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
  } else {
    droneMarkers[mac].setLatLng([detection.drone_lat, detection.drone_long]);
    droneMarkers[mac].setPopupContent(generatePopupContent(detection, 'drone'));
  }
  if (!droneCircles[mac]) {
    droneCircles[mac] = L.circleMarker([detection.drone_lat, detection.drone_long],
                                       {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
                           .addTo(map);
  } else { droneCircles[mac].setLatLng([detection.drone_lat, detection.drone_long]); }
  if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
  const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
  if (!lastDrone || lastDrone[0] != detection.drone_lat || lastDrone[1] != detection.drone_long) { dronePathCoords[mac].push([detection.drone_lat, detection.drone_long]); }
  if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
  dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    if (!pilotMarkers[mac]) {
      pilotMarkers[mac] = L.marker([detection.pilot_lat, detection.pilot_long], {icon: createIcon('👤', color)})
                             .bindPopup(generatePopupContent(detection, 'pilot'))
                             .addTo(map)
                             .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
    } else {
      pilotMarkers[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
      pilotMarkers[mac].setPopupContent(generatePopupContent(detection, 'pilot'));
    }
    if (!pilotCircles[mac]) {
      pilotCircles[mac] = L.circleMarker([detection.pilot_lat, detection.pilot_long],
                                          {radius: 12, color: color, fillColor: color, fillOpacity: 0.7})
                            .addTo(map);
    } else { pilotCircles[mac].setLatLng([detection.pilot_lat, detection.pilot_long]); }
  }
}

function colorFromMac(mac) {
  let hash = 0;
  for (let i = 0; i < mac.length; i++) { hash = mac.charCodeAt(i) + ((hash << 5) - hash); }
  let h = Math.abs(hash) % 360;
  return 'hsl(' + h + ', 70%, 50%)';
}

function get_color_for_mac(mac) {
  if (colorOverrides.hasOwnProperty(mac)) { return "hsl(" + colorOverrides[mac] + ", 70%, 50%)"; }
  return colorFromMac(mac);
}

function updateComboList(data) {
  const activePlaceholder = document.getElementById("activePlaceholder");
  const inactivePlaceholder = document.getElementById("inactivePlaceholder");
  const currentTime = Date.now() / 1000;
  
  persistentMACs.forEach(mac => {
    let detection = data[mac];
    let isActive = detection && ((currentTime - detection.last_update) <= 300);
    let item = comboListItems[mac];
    if (!item) {
      item = document.createElement("div");
      comboListItems[mac] = item;
      item.className = "drone-item";
      item.addEventListener("dblclick", () => {
         restorePaths();
         if (historicalDrones[mac]) {
             delete historicalDrones[mac];
             localStorage.setItem('historicalDrones', JSON.stringify(historicalDrones));
             if (droneMarkers[mac]) { map.removeLayer(droneMarkers[mac]); delete droneMarkers[mac]; }
             if (pilotMarkers[mac]) { map.removeLayer(pilotMarkers[mac]); delete pilotMarkers[mac]; }
             item.classList.remove("selected");
             map.closePopup();
         } else {
             historicalDrones[mac] = Object.assign({}, detection, { userLocked: true, lockTime: Date.now()/1000 });
             localStorage.setItem('historicalDrones', JSON.stringify(historicalDrones));
             showHistoricalDrone(mac, historicalDrones[mac]);
             item.classList.add("selected");
             openAliasPopup(mac);
             if (detection && detection.drone_lat && detection.drone_long && detection.drone_lat != 0 && detection.drone_long != 0) {
                 safeSetView([detection.drone_lat, detection.drone_long], 18);
             }
         }
      });
    }
    item.textContent = aliases[mac] ? aliases[mac] : mac;
    const color = get_color_for_mac(mac);
    item.style.borderColor = color;
    item.style.color = color;
    if (isActive) {
      if (item.parentNode !== activePlaceholder) { activePlaceholder.appendChild(item); }
    } else {
      if (item.parentNode !== inactivePlaceholder) { inactivePlaceholder.appendChild(item); }
    }
  });
}

async function updateData() {
  try {
    const response = await fetch('/api/detections');
    const data = await response.json();
    window.tracked_pairs = data;
    const currentTime = Date.now() / 1000;
    for (const mac in data) { if (!persistentMACs.includes(mac)) { persistentMACs.push(mac); } }
    for (const mac in data) {
      if (historicalDrones[mac]) {
        if (data[mac].last_update > historicalDrones[mac].lockTime || (currentTime - historicalDrones[mac].lockTime) > 300) {
          delete historicalDrones[mac];
          localStorage.setItem('historicalDrones', JSON.stringify(historicalDrones));
          if (droneBroadcastRings[mac]) { map.removeLayer(droneBroadcastRings[mac]); delete droneBroadcastRings[mac]; }
        } else { continue; }
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
      const droneLat = det.drone_lat, droneLng = det.drone_long;
      const pilotLat = det.pilot_lat, pilotLng = det.pilot_long;
      const validDrone = (droneLat !== 0 && droneLng !== 0);
      const validPilot = (pilotLat !== 0 && pilotLng !== 0);
      if (!validDrone && !validPilot) continue;
      const color = get_color_for_mac(mac);
      if (!firstDetectionZoomed && validDrone) {
        firstDetectionZoomed = true;
        safeSetView([droneLat, droneLng], 18);
      }
      if (validDrone) {
        if (droneMarkers[mac]) {
          droneMarkers[mac].setLatLng([droneLat, droneLng]);
          if (!droneMarkers[mac].isPopupOpen()) { droneMarkers[mac].setPopupContent(generatePopupContent(det, 'drone')); }
        } else {
          droneMarkers[mac] = L.marker([droneLat, droneLng], {icon: createIcon('🛸', color)})
                                .bindPopup(generatePopupContent(det, 'drone'))
                                .addTo(map)
                                .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
        }
        if (droneCircles[mac]) { droneCircles[mac].setLatLng([droneLat, droneLng]); }
        else { droneCircles[mac] = L.circleMarker([droneLat, droneLng], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7}).addTo(map); }
        if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
        const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
        if (!lastDrone || lastDrone[0] != droneLat || lastDrone[1] != droneLng) { dronePathCoords[mac].push([droneLat, droneLng]); }
        if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
        dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);
        if (currentTime - det.last_update <= 15) {
          if (droneBroadcastRings[mac]) { droneBroadcastRings[mac].setLatLng([droneLat, droneLng]); }
          else { droneBroadcastRings[mac] = L.circleMarker([droneLat, droneLng], {radius: 16, color: "lime", fill: false, weight: 3}).addTo(map); }
        } else {
          if (droneBroadcastRings[mac]) { map.removeLayer(droneBroadcastRings[mac]); delete droneBroadcastRings[mac]; }
        }
        if (followLock.enabled && followLock.type === 'drone' && followLock.id === mac) { map.setView([droneLat, droneLng], map.getZoom()); }
      }
      if (validPilot) {
        if (pilotMarkers[mac]) {
          pilotMarkers[mac].setLatLng([pilotLat, pilotLng]);
          if (!pilotMarkers[mac].isPopupOpen()) { pilotMarkers[mac].setPopupContent(generatePopupContent(det, 'pilot')); }
        } else {
          pilotMarkers[mac] = L.marker([pilotLat, pilotLng], {icon: createIcon('👤', color)})
                                .bindPopup(generatePopupContent(det, 'pilot'))
                                .addTo(map)
                                .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
        }
        if (pilotCircles[mac]) { pilotCircles[mac].setLatLng([pilotLat, pilotLng]); }
        else { pilotCircles[mac] = L.circleMarker([pilotLat, pilotLng], {radius: 12, color: color, fillColor: color, fillOpacity: 0.7}).addTo(map); }
        if (!pilotPathCoords[mac]) { pilotPathCoords[mac] = []; }
        const lastPilot = pilotPathCoords[mac][pilotPathCoords[mac].length - 1];
        if (!lastPilot || lastPilot[0] != pilotLat || lastPilot[1] != pilotLng) { pilotPathCoords[mac].push([pilotLat, pilotLng]); }
        if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); }
        pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {color: color, dashArray: '5,5'}).addTo(map);
        if (followLock.enabled && followLock.type === 'pilot' && followLock.id === mac) { map.setView([pilotLat, pilotLng], map.getZoom()); }
      }
    }
    updateComboList(data);
    updateAliases();
  } catch (error) { console.error("Error fetching detection data:", error); }
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
    if (data.connected) { statusDiv.textContent = 'Connected'; statusDiv.style.color = 'lime'; }
    else { statusDiv.textContent = 'Disconnected'; statusDiv.style.color = 'red'; }
  } catch (error) { console.error("Error fetching serial status:", error); }
}
setInterval(updateSerialStatus, 1000);
updateSerialStatus();

setInterval(updateData, 200);
updateData();

function updateLockFollow() {
  if (followLock.enabled) {
    if (followLock.type === 'observer' && observerMarker) { map.setView(observerMarker.getLatLng(), map.getZoom()); }
    else if (followLock.type === 'drone' && droneMarkers[followLock.id]) { map.setView(droneMarkers[followLock.id].getLatLng(), map.getZoom()); }
    else if (followLock.type === 'pilot' && pilotMarkers[followLock.id]) { map.setView(pilotMarkers[followLock.id].getLatLng(), map.getZoom()); }
  }
}
setInterval(updateLockFollow, 200);

document.getElementById("filterToggle").addEventListener("click", function() {
  const content = document.getElementById("filterContent");
  if (content.style.display === "none") { content.style.display = "block"; this.textContent = "[-]"; }
  else { content.style.display = "none"; this.textContent = "[+]"; }
});

async function restorePaths() {
  try {
    const response = await fetch('/api/paths');
    const data = await response.json();
    for (const mac in data.dronePaths) {
      let isActive = false;
      if (tracked_pairs[mac] && ((Date.now()/1000) - tracked_pairs[mac].last_update) <= STALE_THRESHOLD) { isActive = true; }
      if (!isActive && !historicalDrones[mac]) continue;
      dronePathCoords[mac] = data.dronePaths[mac];
      if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
      const color = get_color_for_mac(mac);
      dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);
    }
    for (const mac in data.pilotPaths) {
      let isActive = false;
      if (tracked_pairs[mac] && ((Date.now()/1000) - tracked_pairs[mac].last_update) <= STALE_THRESHOLD) { isActive = true; }
      if (!isActive && !historicalDrones[mac]) continue;
      pilotPathCoords[mac] = data.pilotPaths[mac];
      if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); }
      const color = get_color_for_mac(mac);
      pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {color: color, dashArray: '5,5'}).addTo(map);
    }
  } catch (error) { console.error("Error restoring paths:", error); }
}
setInterval(restorePaths, 200);
restorePaths();

function updateColor(mac, hue) {
  hue = parseInt(hue);
  colorOverrides[mac] = hue;
  localStorage.setItem('colorOverrides', JSON.stringify(colorOverrides));
  var newColor = "hsl(" + hue + ", 70%, 50%)";
  if (droneMarkers[mac]) { droneMarkers[mac].setIcon(createIcon('🛸', newColor)); droneMarkers[mac].setPopupContent(generatePopupContent(tracked_pairs[mac], 'drone')); }
  if (pilotMarkers[mac]) { pilotMarkers[mac].setIcon(createIcon('👤', newColor)); pilotMarkers[mac].setPopupContent(generatePopupContent(tracked_pairs[mac], 'pilot')); }
  if (droneCircles[mac]) { droneCircles[mac].setStyle({ color: newColor, fillColor: newColor }); }
  if (pilotCircles[mac]) { pilotCircles[mac].setStyle({ color: newColor, fillColor: newColor }); }
  if (dronePolylines[mac]) { dronePolylines[mac].setStyle({ color: newColor }); }
  if (pilotPolylines[mac]) { pilotPolylines[mac].setStyle({ color: newColor }); }
  var listItems = document.getElementsByClassName("drone-item");
  for (var i = 0; i < listItems.length; i++) {
    if (listItems[i].textContent.includes(mac)) { listItems[i].style.borderColor = newColor; listItems[i].style.color = newColor; }
  }
}
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
    body { background-color: black; color: lime; font-family: monospace; text-align: center; }
    pre { font-size: 16px; margin: 20px auto; }
    form { display: inline-block; text-align: left; }
    li { list-style: none; margin: 10px 0; }
    select { background-color: #333; color: lime; border: none; padding: 3px; }
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
        if det.get("drone_lat", 0) == 0 and det.get("drone_long", 0) == 0:
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
                "coordinates": [det.get("drone_long"), det.get("drone_lat")]
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

@app.route('/api/paths', methods=['GET'])
def api_paths():
    drone_paths = {}
    pilot_paths = {}
    for det in detection_history:
        mac = det.get("mac")
        if not mac:
            continue
        d_lat = det.get("drone_lat", 0)
        d_long = det.get("drone_long", 0)
        if d_lat != 0 and d_long != 0:
            drone_paths.setdefault(mac, []).append([d_lat, d_long])
        p_lat = det.get("pilot_lat", 0)
        p_long = det.get("pilot_long", 0)
        if p_lat != 0 and p_long != 0:
            pilot_paths.setdefault(mac, []).append([p_lat, p_long])
    def dedupe(path):
        if not path:
            return path
        new_path = [path[0]]
        for point in path[1:]:
            if point != new_path[-1]:
                new_path.append(point)
        return new_path
    for mac in drone_paths: drone_paths[mac] = dedupe(drone_paths[mac])
    for mac in pilot_paths: pilot_paths[mac] = dedupe(pilot_paths[mac])
    return jsonify({"dronePaths": drone_paths, "pilotPaths": pilot_paths})

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
