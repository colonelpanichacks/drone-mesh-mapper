import os
import time
import json
import csv
import logging
import colorsys
import threading
import subprocess
import socket
import requests
import urllib3
import serial
import serial.tools.list_ports
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify, redirect, url_for, render_template, render_template_string, send_file
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

 # Helper: consistent color per MAC via hashing
def get_color_for_mac(mac: str) -> str:
    # Compute hue from MAC string hash
    hue = sum(ord(c) for c in mac) % 360
    r, g, b = colorsys.hsv_to_rgb(hue/360.0, 1.0, 1.0)
    ri, gi, bi = int(r*255), int(g*255), int(b*255)
    # Return ABGR format
    return f"ff{bi:02x}{gi:02x}{ri:02x}"


# Server-side webhook URL (set via API)
WEBHOOK_URL = None

def set_server_webhook_url(url: str):
    global WEBHOOK_URL
    WEBHOOK_URL = url

app = Flask(__name__)
# Initialize Socket.IO for browser and peer-server synchronization

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------
# Global Variables & Files
# ----------------------
tracked_pairs = {}
detection_history = []  # For CSV logging and KML generation

# Changed: Instead of one selected port, we allow up to three.
SELECTED_PORTS = {}  # key will be 'port1', 'port2', 'port3'
BAUD_RATE = 115200
staleThreshold = 60  # Global stale threshold in seconds (changed from 300 seconds -> 1 minute)
# For each port, we track its connection status.
serial_connected_status = {}  # e.g. {"port1": True, "port2": False, ...}
# Mapping to merge fragmented detections: port -> last seen mac
last_mac_by_port = {}

# Track open serial objects for cleanup
serial_objs = {}
serial_objs_lock = threading.Lock()

startup_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
# Updated detections CSV header to include faa_data.
CSV_FILENAME = os.path.join(BASE_DIR, f"detections_{startup_timestamp}.csv")
KML_FILENAME = os.path.join(BASE_DIR, f"detections_{startup_timestamp}.kml")
FAA_LOG_FILENAME = os.path.join(BASE_DIR, "faa_log.csv")  # FAA log CSV remains basic

# Cumulative KML file for all detections
CUMULATIVE_KML_FILENAME = os.path.join(BASE_DIR, "cumulative.kml")
# Initialize cumulative KML on first run
if not os.path.exists(CUMULATIVE_KML_FILENAME):
    with open(CUMULATIVE_KML_FILENAME, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">\n')
        f.write('<Document>\n')
        f.write(f'<name>Cumulative Detections</name>\n')
        f.write('</Document>\n</kml>')

# Write CSV header for detections.
with open(CSV_FILENAME, mode='w', newline='') as csvfile:
    fieldnames = [
        'timestamp', 'alias', 'mac', 'rssi', 'drone_lat', 'drone_long',
        'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
    ]
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    writer.writeheader()

# Cumulative CSV file for all detections
CUMULATIVE_CSV_FILENAME = os.path.join(BASE_DIR, f"cumulative_detections.csv")
# Initialize cumulative CSV on first run
if not os.path.exists(CUMULATIVE_CSV_FILENAME):
    with open(CUMULATIVE_CSV_FILENAME, mode='w', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            'timestamp', 'alias', 'mac', 'rssi', 'drone_lat', 'drone_long',
            'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
        ])
        writer.writeheader()

# Create FAA log CSV with header if not exists.
if not os.path.exists(FAA_LOG_FILENAME):
    with open(FAA_LOG_FILENAME, mode='w', newline='') as csvfile:
        fieldnames = ['timestamp', 'mac', 'remote_id', 'faa_response']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

# --- Alias Persistence ---
ALIASES_FILE = os.path.join(BASE_DIR, "aliases.json")
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
FAA_CACHE_FILE = os.path.join(BASE_DIR, "faa_cache.csv")
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
    # Build sorted list of all MACs seen so far
    macs = sorted({d['mac'] for d in detection_history})

    # Dynamically generate one distinct ABGR color per MAC
    mac_colors = {}
    n = len(macs) or 1
    for i, m in enumerate(macs):
        # evenly‐spaced hue 0.0–1.0
        h = i / n
        # full saturation & value → RGB floats 0–1
        r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
        # scale to 0–255 ints
        ri, gi, bi = int(r*255), int(g*255), int(b*255)
        # format for KML (ABGR hex, alpha=ff)
        mac_colors[m] = f"ff{bi:02x}{gi:02x}{ri:02x}"

    # Start KML document template
    kml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">',
        '<Document>',
        f'<name>Detections {startup_timestamp}</name>'
    ]

    for mac in macs:
        alias = ALIASES.get(mac, "")
        aliasStr = f"{alias} " if alias else ""
        color    = mac_colors[mac]

        # --- Flights grouped by staleThreshold, each in its own Folder ---
        flight_idx = 1
        last_ts = None
        current_flight = []
        for det in detection_history:
            if det.get('mac') != mac:
                continue
            lat, lon = det.get('drone_lat'), det.get('drone_long')
            ts = det.get('last_update')
            if lat and lon:
                # break flight on time gap
                if last_ts and (ts - last_ts) > staleThreshold:
                    # flush current flight
                    if len(current_flight) >= 1:
                        # start folder
                        kml_lines.append('<Folder>')
                        # include start timestamp for this flight
                        start_dt  = datetime.fromtimestamp(current_flight[0][2])
                        start_str = start_dt.strftime('%Y-%m-%d %H:%M:%S')
                        kml_lines.append(f'<name>Flight {flight_idx} {aliasStr}{mac} ({start_str})</name>')
                        # drone path
                        coords = " ".join(f"{x[0]},{x[1]},0" for x in current_flight)
                        kml_lines.append(f'<Placemark><Style><LineStyle><color>{color}</color><width>2</width></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>')
                        # end icons
                        end_lon, end_lat, end_ts = current_flight[-1]
                        kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon></IconStyle></Style><Point><coordinates>{end_lon},{end_lat},0</coordinates></Point></Placemark>')
                        # pilot path inside same flight
                        start_ts = current_flight[0][2]
                        pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in detection_history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and d.get('last_update')>=start_ts and d.get('last_update')<=end_ts]
                        if len(pilot_pts) >= 1:
                            pc = " ".join(f"{p[0]},{p[1]},0" for p in pilot_pts)
                            kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                            plon, plat = pilot_pts[-1]
                            kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
                        kml_lines.append('</Folder>')
                        flight_idx += 1
                    current_flight = []
                # accumulate this point
                current_flight.append((lon, lat, ts))
                last_ts = ts
        # flush final flight if any
        if current_flight:
            kml_lines.append('<Folder>')
            # include start timestamp for this flight
            start_dt  = datetime.fromtimestamp(current_flight[0][2])
            start_str = start_dt.strftime('%Y-%m-%d %H:%M:%S')
            kml_lines.append(f'<name>Flight {flight_idx} {aliasStr}{mac} ({start_str})</name>')
            coords = " ".join(f"{x[0]},{x[1]},0" for x in current_flight)
            kml_lines.append(f'<Placemark><Style><LineStyle><color>{color}</color><width>2</width></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>')
            end_lon, end_lat, end_ts = current_flight[-1]
            kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon></IconStyle></Style><Point><coordinates>{end_lon},{end_lat},0</coordinates></Point></Placemark>')
            pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in detection_history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and d.get('last_update')>=current_flight[0][2] and d.get('last_update')<=end_ts]
            if pilot_pts:
                pc = " ".join(f"{p[0]},{p[1]},0" for p in pilot_pts)
                kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                plon, plat = pilot_pts[-1]
                kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
            kml_lines.append('</Folder>')
    # Close document
    kml_lines.append('</Document></kml>')

    # Write only session KML
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated session KML:", KML_FILENAME)


# New generate_cumulative_kml function
def generate_cumulative_kml():
    """
    Build cumulative KML by reading the cumulative CSV and grouping detections into flights.
    """
    # Read cumulative CSV history
    history = []
    with open(CUMULATIVE_CSV_FILENAME, newline='') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            # Parse timestamp
            ts = datetime.fromisoformat(row['timestamp'])
            row['last_update'] = ts
            # Convert coordinates
            row['drone_lat'] = float(row['drone_lat']) if row['drone_lat'] else 0.0
            row['drone_long'] = float(row['drone_long']) if row['drone_long'] else 0.0
            row['pilot_lat'] = float(row['pilot_lat']) if row['pilot_lat'] else 0.0
            row['pilot_long'] = float(row['pilot_long']) if row['pilot_long'] else 0.0
            history.append(row)

    # Determine unique MACs and assign colors
    macs = sorted({d['mac'] for d in history})
    mac_colors = {}
    n = len(macs) or 1
    for i, m in enumerate(macs):
        h = i / n
        r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
        ri, gi, bi = int(r*255), int(g*255), int(b*255)
        mac_colors[m] = f"ff{bi:02x}{gi:02x}{ri:02x}"

    # Start KML
    kml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">',
        '<Document>',
        '<name>Cumulative Detections</name>'
    ]

    # For each MAC, group history into flights with staleThreshold
    for mac in macs:
        alias = ALIASES.get(mac, "")
        aliasStr = f"{alias} " if alias else ""
        color = mac_colors[mac]

        flight_idx = 1
        last_ts = None
        current_flight = []

        for det in history:
            if det.get('mac') != mac:
                continue
            lat = det['drone_lat']
            lon = det['drone_long']
            ts = det['last_update']
            if lat and lon:
                if last_ts and (ts - last_ts).total_seconds() > staleThreshold:
                    # flush flight
                    if current_flight:
                        # open folder
                        kml_lines.append('<Folder>')
                        # include start timestamp for this flight
                        start_dt  = current_flight[0][2]  # already a datetime
                        start_str = start_dt.strftime('%Y-%m-%d %H:%M:%S')
                        kml_lines.append(f'<name>Flight {flight_idx} {aliasStr}{mac} ({start_str})</name>')
                        # drone path
                        coords = " ".join(f"{lo},{la},0" for lo, la, _ in current_flight)
                        kml_lines.append(f'<Placemark><Style><LineStyle><color>{color}</color><width>2</width></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>')
                        # drone end icon
                        end_lo, end_la, end_ts = current_flight[-1]
                        kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon></IconStyle></Style><Point><coordinates>{end_lo},{end_la},0</coordinates></Point></Placemark>')
                        # pilot path
                        start_ts = current_flight[0][2]
                        pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and start_ts <= d['last_update'] <= end_ts]
                        if pilot_pts:
                            pc = " ".join(f"{plo},{pla},0" for plo, pla in pilot_pts)
                            kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                            plon, plat = pilot_pts[-1]
                            kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
                        # close folder
                        kml_lines.append('</Folder>')
                        flight_idx += 1
                    current_flight = []
                # accumulate
                current_flight.append((lon, lat, ts))
                last_ts = ts

        # flush last flight
        if current_flight:
            kml_lines.append('<Folder>')
            # include start timestamp for this flight
            start_dt  = current_flight[0][2]  # already a datetime
            start_str = start_dt.strftime('%Y-%m-%d %H:%M:%S')
            kml_lines.append(f'<name>Flight {flight_idx} {aliasStr}{mac} ({start_str})</name>')
            coords = " ".join(f"{lo},{la},0" for lo, la, _ in current_flight)
            kml_lines.append(f'<Placemark><Style><LineStyle><color>{color}</color><width>2</width></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>')
            end_lo, end_la, end_ts = current_flight[-1]
            kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></Icon></IconStyle></Style><Point><coordinates>{end_lo},{end_la},0</coordinates></Point></Placemark>')
            start_ts = current_flight[0][2]
            pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and start_ts <= d['last_update'] <= end_ts]
            if pilot_pts:
                pc = " ".join(f"{plo},{pla},0" for plo, pla in pilot_pts)
                kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                plon, plat = pilot_pts[-1]
                kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></Icon></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
            kml_lines.append('</Folder>')

    # Close KML and write
    kml_lines.append('</Document></kml>')
    with open(CUMULATIVE_KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))


# Generate initial KML so the file exists from startup
generate_kml()
generate_cumulative_kml()


# ----------------------
# Detection Update & CSV Logging
# ----------------------
def update_detection(detection):
    mac = detection.get("mac")
    if not mac:
        return
    prev = tracked_pairs.get(mac)

    # Retrieve new drone coordinates from the detection
    new_drone_lat = detection.get("drone_lat", 0)
    new_drone_long = detection.get("drone_long", 0)
    valid_drone = (new_drone_lat != 0 and new_drone_long != 0)

    if not valid_drone:
        print(f"No-GPS detection for {mac}; forwarding for popup and webhook.")
        # Forward this no-GPS detection to the client
        tracked_pairs[mac] = detection
        detection_history.append(detection.copy())
        # Server-side webhook firing for no-GPS detection
        if WEBHOOK_URL:
            try:
                requests.post(WEBHOOK_URL, json=detection, timeout=5)
            except Exception as e:
                logging.error(f"Server webhook error: {e}")
        # Write to session CSV even for no-GPS
        with open(CSV_FILENAME, mode='a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=[
                'timestamp', 'alias', 'mac', 'rssi', 'drone_lat', 'drone_long',
                'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
            ])
            writer.writerow({
                'timestamp': datetime.now().isoformat(),
                'alias': ALIASES.get(mac, ''),
                'mac': mac,
                'rssi': detection.get('rssi', ''),
                'drone_lat': new_drone_lat,
                'drone_long': new_drone_long,
                'drone_altitude': detection.get('drone_altitude', ''),
                'pilot_lat': detection.get('pilot_lat', ''),
                'pilot_long': detection.get('pilot_long', ''),
                'basic_id': detection.get('basic_id', ''),
                'faa_data': json.dumps(detection.get('faa_data', {}))
            })

        # Append to cumulative CSV for no-GPS
        with open(CUMULATIVE_CSV_FILENAME, mode='a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=[
                'timestamp', 'alias', 'mac', 'rssi', 'drone_lat', 'drone_long',
                'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
            ])
            writer.writerow({
                'timestamp': datetime.now().isoformat(),
                'alias': ALIASES.get(mac, ''),
                'mac': mac,
                'rssi': detection.get('rssi', ''),
                'drone_lat': new_drone_lat,
                'drone_long': new_drone_long,
                'drone_altitude': detection.get('drone_altitude', ''),
                'pilot_lat': detection.get('pilot_lat', ''),
                'pilot_long': detection.get('pilot_long', ''),
                'basic_id': detection.get('basic_id', ''),
                'faa_data': json.dumps(detection.get('faa_data', {}))
            })
        # Regenerate full cumulative KML
        generate_cumulative_kml()

        # Cache FAA data even for no-GPS
        if detection.get('basic_id'):
            write_to_faa_cache(mac, detection['basic_id'], detection.get('faa_data', {}))
        generate_kml()
        return

    # Otherwise, use the provided non-zero coordinates.
    detection["drone_lat"] = new_drone_lat
    detection["drone_long"] = new_drone_long
    detection["drone_altitude"] = detection.get("drone_altitude", 0)
    detection["pilot_lat"] = detection.get("pilot_lat", 0)
    detection["pilot_long"] = detection.get("pilot_long", 0)
    detection["last_update"] = time.time()

    # Preserve previous basic_id if new detection lacks one
    if not detection.get("basic_id") and mac in tracked_pairs and tracked_pairs[mac].get("basic_id"):
        detection["basic_id"] = tracked_pairs[mac]["basic_id"]
    remote_id = detection.get("basic_id")
    # Try exact cache lookup by (mac, remote_id), then fallback to any cached data for this mac, then to previous tracked_pairs entry
    if mac:
        # Exact match if basic_id provided
        if remote_id:
            key = (mac, remote_id)
            if key in FAA_CACHE:
                detection["faa_data"] = FAA_CACHE[key]
        # Fallback: any cached FAA data for this mac
        if "faa_data" not in detection:
            for (c_mac, _), faa_data in FAA_CACHE.items():
                if c_mac == mac:
                    detection["faa_data"] = faa_data
                    break
        # Fallback: last known FAA data in tracked_pairs
        if "faa_data" not in detection and mac in tracked_pairs and "faa_data" in tracked_pairs[mac]:
            detection["faa_data"] = tracked_pairs[mac]["faa_data"]
        # Always cache FAA data by MAC and current basic_id for fallback
        if "faa_data" in detection:
            write_to_faa_cache(mac, detection.get("basic_id", ""), detection["faa_data"])

    tracked_pairs[mac] = detection
    # Broadcast this detection to all connected clients and peer servers
    try:
        socketio.emit('detection', detection, broadcast=True)
    except Exception:
        pass
    detection_history.append(detection.copy())
    print("Updated tracked_pairs:", tracked_pairs)
    with open(CSV_FILENAME, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            'timestamp', 'alias', 'mac', 'rssi', 'drone_lat', 'drone_long',
            'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
        ])
        writer.writerow({
            'timestamp': datetime.now().isoformat(),
            'alias': ALIASES.get(mac, ''),
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
    # Append to cumulative CSV
    with open(CUMULATIVE_CSV_FILENAME, mode='a', newline='') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=[
            'timestamp', 'alias', 'mac', 'rssi', 'drone_lat', 'drone_long',
            'drone_altitude', 'pilot_lat', 'pilot_long', 'basic_id', 'faa_data'
        ])
        writer.writerow({
            'timestamp': datetime.now().isoformat(),
            'alias': ALIASES.get(mac, ''),
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
    # Regenerate full cumulative KML
    generate_cumulative_kml()
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
# Webhook popup API Endpoint 
# ----------------------
@app.route('/api/webhook_popup', methods=['POST'])
def webhook_popup():
    data = request.get_json()
    webhook_url = data.get("webhook_url")
    if not webhook_url:
        return jsonify({"status": "error", "reason": "No webhook URL provided"}), 400
    try:
        clean_data = data.get("payload", {})
        response = requests.post(webhook_url, json=clean_data, timeout=5)
        return jsonify({"status": "ok", "response": response.status_code}), 200
    except Exception as e:
        logging.error(f"Webhook send error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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
    # Fallback: if FAA API query failed or returned no records, try cached FAA data by MAC
    if not faa_result or not faa_result.get("data", {}).get("items"):
        for (c_mac, _), cached_data in FAA_CACHE.items():
            if c_mac == mac:
                faa_result = cached_data
                break
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
# FAA Data GET API Endpoint (by MAC or basic_id)
# ----------------------

@app.route('/api/faa/<identifier>', methods=['GET'])
def api_get_faa(identifier):
    """
    Retrieve cached FAA data by MAC address or by basic_id (remote ID).
    """
    # First try lookup by MAC
    if identifier in tracked_pairs and 'faa_data' in tracked_pairs[identifier]:
        return jsonify({'status': 'ok', 'faa_data': tracked_pairs[identifier]['faa_data']})
    # Then try lookup by basic_id
    for mac, det in tracked_pairs.items():
        if det.get('basic_id') == identifier and 'faa_data' in det:
            return jsonify({'status': 'ok', 'faa_data': det['faa_data']})
    # Fallback: search cached FAA data by remote_id first, then by MAC
    for (c_mac, c_rid), faa_data in     FAA_CACHE.items():
        if c_rid == identifier:
            return jsonify({'status': 'ok', 'faa_data': faa_data})
    for (c_mac, c_rid), faa_data in FAA_CACHE.items():
        if c_mac == identifier:
            return jsonify({'status': 'ok', 'faa_data': faa_data})
    return jsonify({'status': 'error', 'message': 'No FAA data found for this identifier'}), 404



# ----------------------


# ----------------------
# HTML & JS (UI) Section
# ----------------------
# Updated: The selection page now has three dropdowns.
PORT_SELECTION_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Select USB Serial Ports</title>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700&display=swap" rel="stylesheet">
  <style>
    /* Highlight non-GPS drones in inactive list */
    #inactivePlaceholder .drone-item.no-gps {
      border: 2px solid lightblue !important;
      background-color: transparent !important;
      color: inherit !important;
    }
    .leaflet-tile {
      border: none !important;
      box-shadow: none !important;
      background-color: transparent !important;
      image-rendering: crisp-edges !important;
    }
    .leaflet-container {
      background-color: black !important;
    }
    body {
      margin: 0;
      padding: 0;
      font-family: 'Orbitron', monospace;
      background-color: #0a001f;
      color: #0ff;
      text-shadow: 0 0 8px #0ff, 0 0 16px #f0f;
      text-align: center;
      zoom: 1.15;
    }
    pre { font-size: 16px; margin: 10px auto; }
    form {
      display: inline-block;
      text-align: center;
    }
    li { list-style: none; margin: 10px 0; }
    select {
      background-color: #333;
      color: lime;
      border: none;
      padding: 3px;
      margin-bottom: 5px;
      box-shadow: 0 0 4px #0ff;
    }
    label { font-size: 18px; }
    button[type="submit"] {
      display: block;
      margin: 1em auto 5px auto;
      padding: 5px;
      border: 1px solid lime;
      background-color: #333;
      color: lime;
      font-family: 'Orbitron', monospace;
      cursor: pointer;
      outline: none;
      border-radius: 10px;
      box-shadow: 0 0 8px #f0f, 0 0 16px #0ff;
    }
    pre.logo-art {
      display: inline-block;
      margin: 0 auto;
      margin-bottom: 10px;
    }
    pre.ascii-art {
      margin: 0;
      padding: 5px;
      background: linear-gradient(to right, blue, purple, pink, lime, green);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      font-family: monospace;
      font-size: 90%;
    }
    h1 {
      font-size: 18px;
      font-family: 'Orbitron', monospace;
      margin: 1em 0 4px 0;
    }
    /* Rounded toggle switch styling */
    .switch {
      position: relative; display: inline-block; width: 40px; height: 20px;
    }
    .switch input {
      opacity: 0; width: 0; height: 0;
    }
    .switch .slider {
      position: absolute;
      cursor: pointer;
      top: 0; left: 0; right: 0; bottom: 0;
      background-color: #555;
      transition: .4s;
      border-radius: 20px;
    }
    .switch .slider:before {
      position: absolute;
      content: "";
      height: 16px; width: 16px;
      left: 2px; top: 2px;
      background-color: lime;
      border: 1px solid #9B30FF;
      transition: .4s;
      border-radius: 50%;
    }
    .switch input:checked + .slider {
      background-color: lime;
    }
    .switch input:checked + .slider:before {
      transform: translateX(20px);
    }
  </style>
</head>
<body>
  <pre class="ascii-art logo-art">{{ logo_ascii }}</pre>
  <h1>Select Up to 3 USB Serial Ports</h1>
  <form method="POST" action="/select_ports">
    <label>Port 1:</label><br>
    <select id="port1" name="port1">
      <option value="">--None--</option>
      {% for port in ports %}
        <option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>
      {% endfor %}
    </select><br>
    <label>Port 2:</label><br>
    <select id="port2" name="port2">
      <option value="">--None--</option>
      {% for port in ports %}
        <option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>
      {% endfor %}
    </select><br>
    <label>Port 3:</label><br>
    <select id="port3" name="port3">
      <option value="">--None--</option>
      {% for port in ports %}
        <option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>
      {% endfor %}
    </select><br>
    <div style="margin-bottom:8px;"></div>
    <div style="margin-top:4px; margin-bottom:4px; text-align:center;">
      <label for="webhookUrl" style="font-size:18px; font-family:'Orbitron', monospace; color:#87CEEB;">Webhook URL</label><br>
      <input type="text" id="webhookUrl" placeholder="https://example.com/webhook"
             style="width:300px; background-color:#222; color:#87CEEB; border:1px solid #FF00FF; padding:4px; font-size:1em; outline:none;">
    </div>
    <div style="margin-top:4px; margin-bottom:4px; text-align:center;">
      <button id="updateWebhookButton" style="border:1px solid lime; background-color:#333; color:#FF00FF; font-family:'Orbitron',monospace; padding:4px 8px; cursor:pointer; border-radius:4px;">
        Update Webhook
      </button>
    </div>
    <button id="beginMapping" type="submit" style="
        display: block;
        margin: 15px auto 0;
        padding: 8px 15px;
        min-width: 150px;
        border: 1px solid lime;
        background-color: #333;
        color: lime;
        font-family: 'Orbitron', monospace;
        font-size: 1.2em;
        text-shadow: 0 0 8px #0ff;
        cursor: pointer;
    ">
      Begin Mapping
    </button>
    <div style="margin-bottom:8px;"></div>
  </form>
  <pre class="ascii-art">{{ bottom_ascii }}</pre>
  <script>
    function refreshPortOptions() {
      fetch('/api/ports')
        .then(res => res.json())
        .then(data => {
          ['port1','port2','port3'].forEach(name => {
            const select = document.getElementById(name);
            if (!select) return;
            const current = select.value;
            select.innerHTML = '<option value="">--None--</option>' +
              data.ports.map(p => `<option value="${p.device}">${p.device} - ${p.description}</option>`).join('');
            select.value = current;
          });
        })
        .catch(err => console.error('Error refreshing ports:', err));
    }
    var refreshInterval = setInterval(refreshPortOptions, 2000);
    ['port1','port2','port3'].forEach(function(name) {
      var select = document.getElementById(name);
      if (select) {
        ['focus', 'mousedown'].forEach(function(evt) {
          select.addEventListener(evt, function() { clearInterval(refreshInterval); });
        });
        select.addEventListener('change', function() { clearInterval(refreshInterval); });
      }
    });
    window.onload = function() {
      refreshPortOptions();
    }
    const webhookInput = document.getElementById('webhookUrl');
    const storedWebhookUrl = localStorage.getItem('popupWebhookUrl') || '';
    webhookInput.value = storedWebhookUrl;
    webhookInput.addEventListener('change', () => {
      localStorage.setItem('popupWebhookUrl', webhookInput.value.trim());
    });
    document.getElementById('updateWebhookButton').addEventListener('click', function(e) {
      e.preventDefault();
      const url = document.getElementById('webhookUrl').value.trim();
      localStorage.setItem('popupWebhookUrl', url);
      console.log('Webhook URL updated to', url);
    });

  </script>
</body>
</html>
'''

    # Updated: The main mapping page now shows serial statuses for all selected USB devices.
HTML_PAGE = '''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mesh Mapper</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <style>
    /* Hide tile seams on all map layers */
    .leaflet-tile {
      border: none !important;
      box-shadow: none !important;
      background-color: transparent !important;
      image-rendering: crisp-edges !important;
      transition: none !important;
    }
    .leaflet-container {
      background-color: black !important;
    }
    /* Toggle switch styling */
    .switch { position: relative; display: inline-block; vertical-align: middle; width: 40px; height: 20px; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #555; transition: .4s; border-radius: 20px; }
    .slider:before {
      position: absolute;
      content: "";
      height: 16px;
      width: 16px;
      left: 2px;
      top: 50%;
      background-color: lime;
      border: 1px solid #9B30FF;
      transition: .4s;
      border-radius: 50%;
      transform: translateY(-50%);
    }
    .switch input:checked + .slider { background-color: lime; }
    .switch input:checked + .slider:before {
      transform: translateX(20px) translateY(-50%);
      border: 1px solid #9B30FF;
    }
    body, html {
      margin: 0;
      padding: 0;
      background-color: #0a001f;
      font-family: 'Orbitron', monospace;
    }
    #map { height: 100vh; }
    /* Layer control styling (bottom left) reduced by 30% */
    #layerControl {
      position: absolute;
      bottom: 10px;
      left: 10px;
      background: rgba(0,0,0,0.8);
      padding: 3.5px; /* reduced from 5px */
      border: 0.7px solid lime; /* reduced border thickness */
      border-radius: 7px; /* reduced from 10px */
      color: #FF00FF;
      font-family: monospace;
      font-size: 0.7em; /* scale font by 70% */
      z-index: 1000;
    }
    /* Basemap label always neon pink */
    #layerControl > label {
      color: #FF00FF;
    }
    #layerControl select,
    #layerControl select option {
      background-color: #333;
      color: lime;
      border: none;
      padding: 2.1px;
      font-size: 0.7em;
    }
    
        #filterBox {
          position: absolute;
          top: 10px;
          right: 10px;
          background: rgba(0,0,0,0.8);
          padding: 8px;
          width: 18.75vw;
          border: 1px solid lime;
          border-radius: 10px;
          color: lime;
          font-family: monospace;
          max-height: 95vh;
          overflow-y: auto;
          overflow-x: hidden;
          z-index: 1000;
        }
        @media (max-width: 600px) {
          #filterBox {
            width: 37.5vw;
            max-width: 90vw;
          }
        }
        /* Auto-size inputs inside filterBox */
        #filterBox input[type="text"],
        #filterBox input[type="password"],
        #filterBox input[type="range"],
        #filterBox select {
          width: auto !important;
          min-width: 0;
        }
    #filterBox.collapsed #filterContent {
      display: none;
    }
    /* Tighten header when collapsed */
    #filterBox.collapsed {
      padding: 4px;
      width: auto;
    }
    #filterBox.collapsed #filterHeader {
      padding: 0;
    }
    #filterBox.collapsed #filterHeader h3 {
      display: inline-block;
      flex: none;
      width: auto;
      margin: 0;
      color: #FF00FF;
    }
# Add margin to filterToggle when collapsed
    #filterBox.collapsed #filterHeader #filterToggle {
      margin-left: 5px;
    }
    #filterBox:not(.collapsed) #filterHeader h3 {
      display: none;
    }
    #filterHeader {
      display: flex;
      align-items: center;
    }
    #filterBox:not(.collapsed) #filterHeader {
      justify-content: flex-end;
    }
    #filterHeader h3 {
      flex: none;
      text-align: center;
      margin: 0;
      font-size: 1em;
      display: block;
      width: 100%;
      color: #FF00FF;
    }
    
    /* USB status box styling (bottom right) - now even with the map layer select */
    #serialStatus {
      position: absolute;
      bottom: 10px;
      right: 10px;
      background: rgba(0,0,0,0.8);
      padding: 3px; /* reduced from 5px */
      border: 0.7px solid lime; /* reduced border thickness */
      border-radius: 7px; /* reduced from 10px */
      color: lime;
      font-family: monospace;
      font-size: 0.7em; /* scale font by 70% */
      z-index: 1000;
    }
    #serialStatus div { margin-bottom: 5px; }
    /* Remove extra bottom padding from the last USB item */
    #serialStatus div:last-child { margin-bottom: 0; }
    
    .usb-name { color: #FF00FF; } /* Neon pink for device names */
    .drone-item {
      display: inline-block;
      border: 1px solid;
      margin: 2px;
      padding: 3px;
      cursor: pointer;
    }
    .drone-item.no-gps {
      position: relative;
      border: 1px solid deepskyblue !important;
    }
    #activePlaceholder .drone-item.no-gps:hover::after {
      content: "no gps lock";
      position: absolute;
      bottom: 100%;
      left: 50%;
      transform: translateX(-50%);
      background-color: black;
      color: #FF00FF;               /* neon pink text */
      padding: 4px 6px;
      border: 1px solid #FF00FF;    /* neon pink border */
      border-radius: 2px;
      white-space: nowrap;
      font-family: monospace;
      font-size: 0.75em;
      z-index: 2000;
    }
    /* Highlight recently seen drones */
    .drone-item.recent {
      box-shadow: 0 0 0 1px lime;
    }
    .placeholder {
      border: 2px solid transparent;
      border-image: linear-gradient(to right, lime 85%, yellow 15%) 1;
      border-radius: 5px;
      min-height: 100px;
      margin-top: 5px;
      overflow-y: auto;
      max-height: 200px;
    }
    .selected { background-color: rgba(255,255,255,0.2); }
    .leaflet-popup > .leaflet-popup-content-wrapper { background-color: black; color: lime; font-family: monospace; border: 2px solid lime; border-radius: 10px;
      width: 220px !important;
      max-width: 220px;
      zoom: 1.15;
    }
    .leaflet-popup-content {
      font-size: 0.75em;
      line-height: 1.2em;
      white-space: normal;
    }
    .leaflet-popup-tip { background: lime; }
    /* Collapse inner Leaflet popup layers into the outer wrapper */
    .leaflet-popup-content {
      background: transparent !important;
      padding: 0 !important;
      box-shadow: none !important;
      color: inherit !important;
    }
    .leaflet-popup-tip-container,
    .leaflet-popup-tip {
      background: transparent !important;
      box-shadow: none !important;
    }
    /* Collapse inner popup layers for no-GPS popups */
    .leaflet-popup.no-gps-popup > .leaflet-popup-content-wrapper {
      /* ensure outer wrapper styling persists */
      background-color: black !important;
      color: lime !important;
    }
    .leaflet-popup.no-gps-popup .leaflet-popup-content {
      background: transparent !important;
      padding: 0 !important;
      box-shadow: none !important;
      color: inherit !important;
    }
    .leaflet-popup.no-gps-popup .leaflet-popup-tip-container,
    .leaflet-popup.no-gps-popup .leaflet-popup-tip {
      background: transparent !important;
      box-shadow: none !important;
    }
    button {
      margin-top: 4px;
      padding: 3px;
      font-size: 0.8em;
      border: 1px solid lime;
      background-color: #333;
      color: lime;
      cursor: pointer;
      width: auto;
    }
    select {
      background-color: #333;
      color: lime;
      border: none;
      padding: 3px;
    }
    .leaflet-control-zoom-in, .leaflet-control-zoom-out {
      background: rgba(0,0,0,0.8);
      color: lime;
      border: 1px solid lime;
      border-radius: 5px;
    }
    /* Style zoom control container to match drone box */
    .leaflet-control-zoom.leaflet-bar {
      background: rgba(0,0,0,0.8);
      border: 1px solid lime;
      border-radius: 10px;
    }
    .leaflet-control-zoom.leaflet-bar a {
      background: transparent;
      color: lime;
      border: none;
      width: 30px;
      height: 30px;
      line-height: 30px;
      text-align: center;
      padding: 0;
      user-select: none;
      caret-color: transparent;
      cursor: pointer;
      outline: none;
    }
    .leaflet-control-zoom.leaflet-bar a:focus {
      outline: none;
      caret-color: transparent;
    }
    .leaflet-control-zoom.leaflet-bar a:hover {
      background: rgba(255,255,255,0.1);
    }
    .leaflet-control-zoom-in:hover, .leaflet-control-zoom-out:hover { background-color: #222; }
    input#aliasInput {
      background-color: #222;
      color: #87CEEB;         /* pastel blue (updated) */
      border: 1px solid #FF00FF;
      padding: 4px;
      font-size: 1.06em;
      caret-color: #87CEEB;
      outline: none;
    }
    .leaflet-popup-content-wrapper input:not(#aliasInput) {
      caret-color: transparent;
    }
    /* Popup button styling */
    .leaflet-popup-content-wrapper button {
      display: inline-block;
      margin: 2px 4px 2px 0;
      padding: 4px 6px;
      font-size: 0.9em;
      width: auto;
      background-color: #333;
      border: 1px solid lime;
      color: lime;
      box-shadow: none;
      text-shadow: none;
    }

    /* Locked button styling */
    .leaflet-popup-content-wrapper button[style*="background-color: green"] {
      background-color: green;
      color: black;
      border-color: green;
    }

    /* Hover effect */
    .leaflet-popup-content-wrapper button:hover {
      background-color: rgba(255,255,255,0.1);
    }
    .leaflet-popup-content-wrapper input[type="text"],
    .leaflet-popup-content-wrapper input[type="range"] {
      font-size: 0.75em;
      padding: 2px;
    }
    /* Disable tile transitions to prevent blur and hide tile seams */
    .leaflet-tile {
      display: block;
      margin: 0;
      padding: 0;
      transition: none !important;
      image-rendering: crisp-edges;
      background-color: black;
      border: none !important;
      box-shadow: none !important;
    }
    .leaflet-container {
      background-color: black;
    }
    /* Disable text cursor in drone list and filter toggle */
    .drone-item, #filterToggle {
      user-select: none;
      caret-color: transparent;
      outline: none;
    }
    .drone-item:focus, #filterToggle:focus {
      outline: none;
      caret-color: transparent;
    }
    /* Cyberpunk styling for filter headings */
    #filterContent > h3:nth-of-type(1) {
      color: #FF00FF;         /* Active Drones in magenta */
      text-align: center;     /* center text */
      font-size: 1.1em;       /* slightly larger font */
    }
    #filterContent > h3:nth-of-type(2) {
      color: #FF00FF;        /* more magenta */
      text-align: center;    /* center text */
      font-size: 1.1em;      /* slightly larger font */
    }
    /* Lime-green hacky dashes around filter headers */
    #filterContent > h3 {
      display: block;
      width: 100%;
      text-align: center;
      margin: 0.5em 0;
    }
    #filterContent > h3::before,
    #filterContent > h3::after {
      content: '---';
      color: lime;
      margin: 0 6px;
    }
    /* Download buttons styling */
    #downloadButtons {
      display: flex;
      width: 100%;
      gap: 4px;
      margin-top: 8px;
    }
    #downloadButtons button {
      flex: 1;
      margin: 0;
      padding: 4px;
      font-size: 0.8em;
      border: 1px solid lime;
      border-radius: 5px;
      background-color: #333;
      color: lime;
      font-family: monospace;
      cursor: pointer;
    }
    #downloadButtons button:focus {
      outline: none;
      caret-color: transparent;
    }
    /* Gradient blue border flush with heading */
    #downloadSection {
      padding: 0 8px 8px 8px;  /* no top padding so border is flush with heading */
      margin-top: 12px;
    }
    /* Gradient for Download Logs header */
    #downloadSection .downloadHeader {
      margin: 10px 0 5px 0;
      text-align: center;
      background: linear-gradient(to right, lime, yellow);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    /* Staleout slider styling – match popup sliders */
    #staleoutSlider {
      -webkit-appearance: none;
      width: 100%;
      height: 3px;
      background: transparent;
      border: none;
      outline: none;
    }
    #staleoutSlider::-webkit-slider-runnable-track {
      width: 100%;
      height: 3px;
      background: #9B30FF;
      border: none;
      border-radius: 0;
    }
    #staleoutSlider::-webkit-slider-thumb {
      -webkit-appearance: none;
      height: 16px;
      width: 16px;
      background: lime;
      border: 1px solid #9B30FF;
      margin-top: -6.5px;
      border-radius: 50%;
      cursor: pointer;
    }
    /* Firefox */
    #staleoutSlider::-moz-range-track {
      width: 100%;
      height: 3px;
      background: #9B30FF;
      border: none;
      border-radius: 0;
    }
    #staleoutSlider::-moz-range-thumb {
      height: 16px;
      width: 16px;
      background: lime;
      border: 1px solid #9B30FF;
      margin-top: -6.5px;
      border-radius: 50%;
      cursor: pointer;
    }
    /* IE */
    #staleoutSlider::-ms-fill-lower,
    #staleoutSlider::-ms-fill-upper {
      background: #9B30FF;
      border: none;
      border-radius: 2px;
    }
    #staleoutSlider::-ms-thumb {
      height: 16px;
      width: 16px;
      background: lime;
      border: 1px solid #9B30FF;
      border-radius: 50%;
      cursor: pointer;
      margin-top: -6.5px;
    }

    /* Popup range sliders styling */
    .leaflet-popup-content-wrapper input[type="range"] {
      -webkit-appearance: none;
      width: 100%;
      height: 3px;
      background: transparent;
      border: none;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-webkit-slider-thumb {
      -webkit-appearance: none;
      height: 16px;
      width: 16px;
      background: lime;
      border: 1px solid #9B30FF;
      margin-top: -6.5px;
      border-radius: 50%;
      cursor: pointer;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-moz-range-thumb {
      height: 16px;
      width: 16px;
      background: lime;
      border: 1px solid #9B30FF;
      margin-top: -6.5px;
      border-radius: 50%;
      cursor: pointer;
    }
    /* Ensure popup sliders have the same track styling */
    .leaflet-popup-content-wrapper input[type="range"]::-webkit-slider-runnable-track {
      width: 100%;
      height: 3px;
      background: #9B30FF;
      border: 1px solid lime;
      border-radius: 0;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-moz-range-track {
      width: 100%;
      height: 3px;
      background: #9B30FF;
      border: 1px solid lime;
      border-radius: 0;
    }

    /* 1) Remove rounded corners from all sliders */
    /* WebKit */
    input[type="range"]::-webkit-slider-runnable-track,
    input[type="range"]::-webkit-slider-thumb {
      border-radius: 0;
    }
    /* Firefox */
    input[type="range"]::-moz-range-track,
    input[type="range"]::-moz-range-thumb {
      border-radius: 0;
    }
    /* IE */
    input[type="range"]::-ms-fill-lower,
    input[type="range"]::-ms-fill-upper,
    input[type="range"]::-ms-thumb {
      border-radius: 0;
    }

    /* 2) Smaller, side-by-side Observer buttons */
    .leaflet-popup-content-wrapper #lock-observer,
    .leaflet-popup-content-wrapper #unlock-observer {
      display: inline-block;
      font-size: 0.9em;
      padding: 4px 6px;
      margin: 2px 4px 2px 0;
    }
</style>
    <style>
      /* Remove glow and shadows on text boxes, selects, and buttons */
      input, select, button {
        text-shadow: none !important;
        box-shadow: none !important;
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
    <option value="esriWorldImagery" selected>Esri World Imagery</option>
    <option value="esriWorldTopo">Esri World TopoMap</option>
    <option value="esriDarkGray">Esri Dark Gray Canvas</option>
    <option value="openTopoMap">OpenTopoMap</option>
  </select>
</div>
<div id="filterBox">
  <div id="filterHeader">
    <h3>Drones</h3>
    <span id="filterToggle" style="cursor: pointer; font-size: 20px;">[-]</span>
  </div>
  <div id="filterContent">
    <h3>Active Drones</h3>
    <div id="activePlaceholder" class="placeholder"></div>
    <h3>Inactive Drones</h3>
    <div id="inactivePlaceholder" class="placeholder"></div>
    <!-- Staleout Slider -->
    <div style="margin-top:8px; display:flex; flex-direction:column; align-items:stretch; width:100%; box-sizing:border-box;">
      <label style="color:lime; font-family:monospace; margin-bottom:4px; display:block; width:100%; text-align:center;">Staleout Time</label>
      <input type="range" id="staleoutSlider" min="1" max="5" step="1" value="1" 
             style="width:100%; border:1px solid lime; margin-bottom:4px;">
      <div id="staleoutValue" style="color:lime; font-family:monospace; width:100%; text-align:center;">1 min</div>
    </div>
    <!-- Downloads Section -->
    <div id="downloadSection">
      <h4 class="downloadHeader">Download Logs</h4>
      <div id="downloadButtons">
        <button id="downloadCsv">CSV</button>
        <button id="downloadKml">KML</button>
        <button id="downloadAliases">Aliases</button>
      </div>
      <div id="downloadCumulativeButtons" style="display:flex; gap:4px; justify-content:center; margin-top:4px;">
        <button id="downloadCumulativeCsv">Cumulative CSV</button>
        <button id="downloadCumulativeKml">Cumulative KML</button>
      </div>
    </div>
    <!-- Node Mode block -->
    <div style="margin-top:8px; display:flex; flex-direction:column; align-items:center;">
      <label style="color:lime; font-family:monospace; margin-bottom:4px;">Node Mode</label>
      <label class="switch">
        <input type="checkbox" id="nodeModeMainSwitch">
        <span class="slider"></span>
      </label>
      <div style="color:#FF00FF; font-family:monospace; font-size:0.75em; text-align:center; margin-top:4px;">
        Polls detections every second instead of every 100 ms to reduce CPU/battery use in Node Mode
      </div>
    </div>
    <button id="settingsButton"
            style="display:block;
                   width:calc(100% - 16px);
                   margin:6px 8px;
                   padding:6px;
                   border:1px solid lime;
                   background-color:#333;
                   color:lime;
                   font-family:monospace;
                   font-size:0.9em;
                   border-radius:5px;
                   cursor:pointer;"
            onclick="window.location.href='/select_ports'">
      Settings
    </button>
    <div style="color:#FF00FF; font-family:monospace; font-size:0.75em; text-align:center; margin-top:4px;">
      Return to port selection and settings screen
    </div>
  </div>
</div>
<div id="serialStatus">
  <!-- USB port statuses will be injected here -->
</div>
<script>
  // Do not clear trackedPairs; persist across reloads
  // Track drones already alerted for no GPS
  const alertedNoGpsDrones = new Set();
  // Round tile positions to integer pixels to eliminate seams
  L.DomUtil.setPosition = (function() {
    var original = L.DomUtil.setPosition;
    return function(el, point) {
      var rounded = L.point(Math.round(point.x), Math.round(point.y));
      original.call(this, el, rounded);
    };
  })();
// --- Node Mode Main Switch & Polling Interval Sync ---
document.addEventListener('DOMContentLoaded', () => {
  // Restore filter collapsed state
  const filterBox = document.getElementById('filterBox');
  const filterToggle = document.getElementById('filterToggle');
  const wasCollapsed = localStorage.getItem('filterCollapsed') === 'true';
  if (wasCollapsed) {
    filterBox.classList.add('collapsed');
    filterToggle.textContent = '[+]';
  }
  // restore follow-lock on reload
  const storedLock = localStorage.getItem('followLock');
  if (storedLock) {
    try {
      followLock = JSON.parse(storedLock);
      if (followLock.type === 'observer') {
        updateObserverPopupButtons();
      } else if (followLock.type === 'drone' || followLock.type === 'pilot') {
        updateMarkerButtons(followLock.type, followLock.id);
      }
    } catch (e) { console.error('Failed to restore followLock', e); }
  }
  // Ensure Node Mode default is off if unset
  if (localStorage.getItem('nodeMode') === null) {
    localStorage.setItem('nodeMode', 'false');
  }
  const mainSwitch = document.getElementById('nodeModeMainSwitch');
  if (mainSwitch) {
    // Sync toggle with stored setting
    mainSwitch.checked = (localStorage.getItem('nodeMode') === 'true');
    mainSwitch.onchange = () => {
      const enabled = mainSwitch.checked;
      localStorage.setItem('nodeMode', enabled);
      clearInterval(updateDataInterval);
      updateDataInterval = setInterval(updateData, enabled ? 1000 : 100);
      // Sync popup toggle if open
      const popupSwitch = document.getElementById('nodeModePopupSwitch');
      if (popupSwitch) popupSwitch.checked = enabled;
    };
  }
  // Start polling based on current setting
  updateData();
  updateDataInterval = setInterval(updateData, mainSwitch && mainSwitch.checked ? 1000 : 100);
  // Adaptive polling: slow down during map interactions
  map.on('zoomstart dragstart', () => {
    clearInterval(updateDataInterval);
    updateDataInterval = setInterval(updateData, 500);
  });
  map.on('zoomend dragend', () => {
    clearInterval(updateDataInterval);
    const interval = mainSwitch && mainSwitch.checked ? 1000 : 100;
    updateDataInterval = setInterval(updateData, interval);
  });

  // Staleout slider initialization
  const staleoutSlider = document.getElementById('staleoutSlider');
  const staleoutValue = document.getElementById('staleoutValue');
  if (staleoutSlider && typeof STALE_THRESHOLD !== 'undefined') {
    staleoutSlider.value = STALE_THRESHOLD / 60;
    staleoutValue.textContent = (STALE_THRESHOLD / 60) + ' min';
    staleoutSlider.oninput = () => {
      const minutes = parseInt(staleoutSlider.value, 10);
      STALE_THRESHOLD = minutes * 60;
      staleoutValue.textContent = minutes + ' min';
      localStorage.setItem('staleoutMinutes', minutes.toString());
    };
  }
  // Filter box toggle persistence
  if (filterToggle && filterBox) {
    filterToggle.addEventListener('click', function() {
      filterBox.classList.toggle('collapsed');
      filterToggle.textContent = filterBox.classList.contains('collapsed') ? '[+]' : '[-]';
      // Persist filter collapsed state
      localStorage.setItem('filterCollapsed', filterBox.classList.contains('collapsed'));
    });
  }
});
// Fallback collapse handler to ensure filter toggle works
document.getElementById("filterToggle").addEventListener("click", function() {
  const box = document.getElementById("filterBox");
  const isCollapsed = box.classList.toggle("collapsed");
  this.textContent = isCollapsed ? "[+]" : "[-]";
  localStorage.setItem('filterCollapsed', isCollapsed);
});
// Configure tile loading for smooth zoom transitions
L.Map.prototype.options.fadeAnimation = true;
L.Map.prototype.options.zoomAnimation = true;
L.TileLayer.prototype.options.updateWhenZooming = true;
L.TileLayer.prototype.options.updateWhenIdle = true;
// Use default tileSize for crisp rendering
L.TileLayer.prototype.options.detectRetina = false;
// Keep a moderate tile buffer for smoother panning
L.TileLayer.prototype.options.keepBuffer = 50;
// Disable aggressive preloading to avoid stutters
L.TileLayer.prototype.options.preload = false;
// On window load, restore persisted detection data (trackedPairs) and re-add markers.
window.onload = function() {
  let stored = localStorage.getItem("trackedPairs");
  if (stored) {
    try {
      let storedPairs = JSON.parse(stored);
      window.tracked_pairs = storedPairs;
      for (const mac in storedPairs) {
        let det = storedPairs[mac];
        let color = get_color_for_mac(mac);
        // Restore drone marker if valid coordinates exist.
        if (det.drone_lat && det.drone_long && det.drone_lat != 0 && det.drone_long != 0) {
          if (!droneMarkers[mac]) {
            droneMarkers[mac] = L.marker([det.drone_lat, det.drone_long], {icon: createIcon('🛸', color), pane: 'droneIconPane'})
                                  .bindPopup(generatePopupContent(det, 'drone'))
                                  .addTo(map);
          }
        }
        // Restore pilot marker if valid coordinates exist.
        if (det.pilot_lat && det.pilot_long && det.pilot_lat != 0 && det.pilot_long != 0) {
          if (!pilotMarkers[mac]) {
            pilotMarkers[mac] = L.marker([det.pilot_lat, det.pilot_long], {icon: createIcon('👤', color), pane: 'pilotIconPane'})
                                  .bindPopup(generatePopupContent(det, 'pilot'))
                                  .addTo(map);
          }
        }
      }
      // Prevent webhook/alert firing for restored drones on page reload
      Object.keys(window.tracked_pairs).forEach(mac => alertedNoGpsDrones.add(mac));
    } catch(e) {
      console.error("Error parsing trackedPairs from localStorage", e);
    }
  }
}

if (localStorage.getItem('colorOverrides')) {
  try { window.colorOverrides = JSON.parse(localStorage.getItem('colorOverrides')); }
  catch(e){ window.colorOverrides = {}; }
} else { window.colorOverrides = {}; }

// Restore historical drones from localStorage
if (localStorage.getItem('historicalDrones')) {
  try { window.historicalDrones = JSON.parse(localStorage.getItem('historicalDrones')); }
  catch(e) { window.historicalDrones = {}; }
} else {
  window.historicalDrones = {};
}

// Restore map center and zoom from localStorage
let persistedCenter = localStorage.getItem('mapCenter');
let persistedZoom = localStorage.getItem('mapZoom');
if (persistedCenter) {
  try { persistedCenter = JSON.parse(persistedCenter); } catch(e) { persistedCenter = null; }
} else {
  persistedCenter = null;
}
persistedZoom = persistedZoom ? parseInt(persistedZoom, 10) : null;

// Application-level globals
var aliases = {};
var colorOverrides = window.colorOverrides;

// Load stale-out minutes from localStorage (default 1) and compute threshold in seconds
if (localStorage.getItem('staleoutMinutes') === null) {
  localStorage.setItem('staleoutMinutes', '1');
}
let STALE_THRESHOLD = parseInt(localStorage.getItem('staleoutMinutes'), 10) * 60;

var comboListItems = {};

async function updateAliases() {
  try {
    const response = await fetch(window.location.origin + '/api/aliases');
    aliases = await response.json();
    updateComboList(window.tracked_pairs);
      // Persist detection state across page reloads
      localStorage.setItem("trackedPairs", JSON.stringify(window.tracked_pairs));
  } catch (error) { console.error("Error fetching aliases:", error); }
}

function safeSetView(latlng, zoom=18) {
  const currentZoom = map.getZoom();
  // make sure we have a Leaflet LatLng
  const target = L.latLng(latlng);
  // if it's already on-screen, do just a small “quarter” zoom
  if (map.getBounds().contains(target)) {
    const smallZoom = currentZoom + (zoom - currentZoom) * 0.25;
    map.flyTo(target, smallZoom, { duration: 0.4 });
    return;
  }
  // otherwise do the full zoom-out + zoom-in
  const midZoom = Math.max(Math.min(currentZoom, zoom) - 3, 8);
  map.flyTo(target, midZoom, { duration: 0.3 });
  setTimeout(() => {
    map.flyTo(target, zoom, { duration: 0.5 });
  }, 300);
}

// Transient terminal-style popup for drone events
function showTerminalPopup(det, isNew) {
  // Remove any existing popup
  const old = document.getElementById('dronePopup');
  if (old) old.remove();

  // Build a new popup container
  const popup = document.createElement('div');
  popup.id = 'dronePopup';
  const isMobile = window.innerWidth <= 600;
  Object.assign(popup.style, {
    position: 'fixed',
    top: isMobile ? '50px' : '10px',
    left: '50%',
    transform: 'translateX(-50%)',
    background: 'rgba(0,0,0,0.8)',
    color: 'lime',
    fontFamily: 'monospace',
    whiteSpace: 'normal',
    padding: isMobile ? '2px 4px' : '4px 8px',
    border: '1px solid lime',
    borderRadius: '4px',
    zIndex: 2000,
    opacity: 0.9,
    fontSize: isMobile ? '0.6em' : '',
    maxWidth: isMobile ? '80vw' : 'none',
    display: 'inline-block',
    textAlign: 'center',
  });

  // Build concise popup text
  const alias = aliases[det.mac];
  const rid   = det.basic_id || 'N/A';
  let header;
  if (!det.drone_lat || !det.drone_long || det.drone_lat === 0 || det.drone_long === 0) {
    header = 'Drone with no GPS lock detected';
  } else if (alias) {
    header = `Known drone detected – ${alias}`;
  } else {
    header = isNew ? 'New drone detected' : 'Previously seen non-aliased drone detected';
  }
  const content = alias
    ? `${header} - RID:${rid} MAC:${det.mac}`
    : `${header} - RID:${rid} MAC:${det.mac}`;
  // Build popup HTML and button using new logic
  // Build popup text
  // (BEGIN PATCHED BUTTON & POPUP LOGIC)
  // Build popup text
  const isMobileBtn = window.innerWidth <= 600;
  const headerDiv = `<div>${content}</div>`;
  let buttonDiv = '';
  if (det.drone_lat && det.drone_long && det.drone_lat !== 0 && det.drone_long !== 0) {
    const btnStyle = [
      'display:block',
      'width:100%',
      'margin-top:4px',
      'padding:' + (isMobileBtn ? '2px 0' : '4px 6px'),
      'border:1px solid #FF00FF',
      'border-radius:4px',
      'background:transparent',
      'color:lime',
      'font-size:' + (isMobileBtn ? '0.8em' : '0.9em'),
      'cursor:pointer'
    ].join('; ');
    buttonDiv = `<div><button id="zoomBtn" style="${btnStyle}">Zoom to Drone</button></div>`;
  }
  popup.innerHTML = headerDiv + buttonDiv;

  if (buttonDiv) {
    const zoomBtn = popup.querySelector('#zoomBtn');
    zoomBtn.addEventListener('click', () => {
      zoomBtn.style.backgroundColor = 'purple';
      setTimeout(() => { zoomBtn.style.backgroundColor = 'transparent'; }, 200);
      safeSetView([det.drone_lat, det.drone_long]);
    });
  }
  // (END PATCHED BUTTON & POPUP LOGIC)

  // --- Webhook logic (scoped, non-intrusive) ---
  try {
    const webhookUrl = localStorage.getItem('popupWebhookUrl');
    if (webhookUrl && webhookUrl.startsWith("http")) {
      const alias = aliases[det.mac];
      let header;
      if (!det.drone_lat || !det.drone_long || det.drone_lat === 0 || det.drone_long === 0) {
        header = 'Drone with no GPS lock detected';
      } else if (alias) {
        header = `Known drone detected – ${alias}`;
      } else {
        header = isNew ? 'New drone detected' : 'Previously seen non-aliased drone detected';
      }
      const payload = {
        alert: header,
        mac: det.mac,
        basic_id: det.basic_id || null,
        alias: alias || null,
        drone_lat: det.drone_lat || null,
        drone_long: det.drone_long || null,
        pilot_lat: det.pilot_lat || null,
        pilot_long: det.pilot_long || null,
        faa_data: (det.faa_data && det.faa_data.data && Array.isArray(det.faa_data.data.items) && det.faa_data.data.items.length > 0)
          ? det.faa_data.data.items[0]
          : null,
        drone_gmap: det.drone_lat && det.drone_long ? `https://www.google.com/maps?q=${det.drone_lat},${det.drone_long}` : null,
        pilot_gmap: det.pilot_lat && det.pilot_long ? `https://www.google.com/maps?q=${det.pilot_lat},${det.pilot_long}` : null,
        isNew: isNew
      };
      fetch('/api/webhook_popup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ payload, webhook_url: webhookUrl })
      }).catch(e => console.warn('Silent webhook fail:', e));
    }
  } catch (e) {
    console.warn('Webhook logic skipped due to error', e);
  }
  // --- End webhook logic ---

  document.body.appendChild(popup);

  // Auto-remove after 4 seconds
  setTimeout(() => popup.remove(), 4000);
}

var followLock = { type: null, id: null, enabled: false };

function generateObserverPopup() {
  var observerLocked = (followLock.enabled && followLock.type === 'observer');
  var storedObserverEmoji = localStorage.getItem('observerEmoji') || "😎";
  return `
  <div>
    <strong>Observer Location</strong><br>
    <label for="observerEmoji">Select Observer Icon:</label>
    <select id="observerEmoji" onchange="updateObserverEmoji()">
       <option value="😎" ${storedObserverEmoji === "😎" ? "selected" : ""}>😎</option>
       <option value="👽" ${storedObserverEmoji === "👽" ? "selected" : ""}>👽</option>
       <option value="🤖" ${storedObserverEmoji === "🤖" ? "selected" : ""}>🤖</option>
       <option value="🏎️" ${storedObserverEmoji === "🏎️" ? "selected" : ""}>🏎️</option>
       <option value="🕵️‍♂️" ${storedObserverEmoji === "🕵️‍♂️" ? "selected" : ""}>🕵️‍♂️</option>
       <option value="🥷" ${storedObserverEmoji === "🥷" ? "selected" : ""}>🥷</option>
       <option value="👁️" ${storedObserverEmoji === "👁️" ? "selected" : ""}>👁️</option>
    </select><br>
    <div style="display:flex; gap:4px; justify-content:center; margin-top:4px;">
        <button id="lock-observer" onclick="lockObserver()" style="background-color: ${observerLocked ? 'green' : ''};">
          ${observerLocked ? 'Locked on Observer' : 'Lock on Observer'}
        </button>
        <button id="unlock-observer" onclick="unlockObserver()" style="background-color: ${observerLocked ? '' : 'green'};">
          ${observerLocked ? 'Unlock Observer' : 'Unlocked Observer'}
        </button>
    </div>
  </div>
  `;
}

// Updated function: now saves the selected observer icon to localStorage and updates the observer marker.
function updateObserverEmoji() {
  var select = document.getElementById("observerEmoji");
  var selectedEmoji = select.value;
  localStorage.setItem('observerEmoji', selectedEmoji);
  if (observerMarker) {
    observerMarker.setIcon(createIcon(selectedEmoji, 'blue'));
  }
}

function lockObserver() { followLock = { type: 'observer', id: 'observer', enabled: true }; updateObserverPopupButtons();
  localStorage.setItem('followLock', JSON.stringify(followLock));
}
function unlockObserver() { followLock = { type: null, id: null, enabled: false }; updateObserverPopupButtons();
  localStorage.setItem('followLock', JSON.stringify(followLock));
}
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
  content += '<strong>ID:</strong> <span id="aliasDisplay_' + detection.mac + '" style="color:#FF00FF;">' + aliasText + '</span> (MAC: ' + detection.mac + ')<br>';
  
  if (detection.basic_id || detection.faa_data) {
    if (detection.basic_id) {
      content += '<div style="border:2px solid #FF00FF; padding:5px; margin:5px 0;">FAA RemoteID: ' + detection.basic_id + '</div>';
    }
    if (detection.basic_id) {
      content += '<button onclick="queryFaaAPI(\\\'' + detection.mac + '\\\', \\\'' + detection.basic_id + '\\\')" id="queryFaaButton_' + detection.mac + '">Query FAA API</button>';
    }
    content += '<div id="faaResult_' + detection.mac + '" style="margin-top:5px;">';
    if (detection.faa_data) {
      let faaData = detection.faa_data;
      let item = null;
      if (faaData.data && faaData.data.items && faaData.data.items.length > 0) {
        item = faaData.data.items[0];
      }
      if (item) {
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
                     style="background-color: #222; color: #87CEEB; border: 1px solid #FF00FF;" 
                     value="${aliases[detection.mac] ? aliases[detection.mac] : ''}"><br>
              <div style="display:flex; align-items:center; justify-content:space-between; width:100%; margin-top:4px;">
                <button
                  onclick="saveAlias('${detection.mac}'); this.style.backgroundColor='purple'; setTimeout(()=>this.style.backgroundColor='#333',300);"
                  style="flex:1; margin:0 2px; padding:4px 0;"
                >Save Alias</button>
                <button
                  onclick="clearAlias('${detection.mac}'); this.style.backgroundColor='purple'; setTimeout(()=>this.style.backgroundColor='#333',300);"
                  style="flex:1; margin:0 2px; padding:4px 0;"
                >Clear Alias</button>
              </div>`;
  
  content += `<div style="border-top:2px solid lime; margin:10px 0;"></div>`;
  
    var isDroneLocked = (followLock.enabled && followLock.type === 'drone' && followLock.id === detection.mac);
    var droneLockButton = `<button id="lock-drone-${detection.mac}" onclick="lockMarker('drone', '${detection.mac}')" style="flex:${isDroneLocked ? 1.2 : 0.8}; margin:0 2px; padding:4px 0; background-color: ${isDroneLocked ? 'green' : ''};">
      ${isDroneLocked ? 'Locked on Drone' : 'Lock on Drone'}
    </button>`;
    var droneUnlockButton = `<button id="unlock-drone-${detection.mac}" onclick="unlockMarker('drone', '${detection.mac}')" style="flex:${isDroneLocked ? 0.8 : 1.2}; margin:0 2px; padding:4px 0; background-color: ${isDroneLocked ? '' : 'green'};">
      ${isDroneLocked ? 'Unlock Drone' : 'Unlocked Drone'}
    </button>`;
    var isPilotLocked = (followLock.enabled && followLock.type === 'pilot' && followLock.id === detection.mac);
    var pilotLockButton = `<button id="lock-pilot-${detection.mac}" onclick="lockMarker('pilot', '${detection.mac}')" style="flex:${isPilotLocked ? 1.2 : 0.8}; margin:0 2px; padding:4px 0; background-color: ${isPilotLocked ? 'green' : ''};">
      ${isPilotLocked ? 'Locked on Pilot' : 'Lock on Pilot'}
    </button>`;
    var pilotUnlockButton = `<button id="unlock-pilot-${detection.mac}" onclick="unlockMarker('pilot', '${detection.mac}')" style="flex:${isPilotLocked ? 0.8 : 1.2}; margin:0 2px; padding:4px 0; background-color: ${isPilotLocked ? '' : 'green'};">
      ${isPilotLocked ? 'Unlock Pilot' : 'Unlocked Pilot'}
    </button>`;
    content += `
      <div style="display:flex; align-items:center; justify-content:space-between; width:100%; margin-top:4px;">
        ${droneLockButton}
        ${droneUnlockButton}
      </div>
      <div style="display:flex; align-items:center; justify-content:space-between; width:100%; margin-top:4px;">
        ${pilotLockButton}
        ${pilotUnlockButton}
      </div>`;
  
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

      // Node Mode toggle in popup

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
        const response = await fetch(window.location.origin + '/api/query_faa', {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({mac: mac, remote_id: remote_id})
        });
        const result = await response.json();
        if (result.status === "ok") {
            // Immediately update the in-memory tracked_pairs with the returned FAA data
            if (window.tracked_pairs && window.tracked_pairs[mac]) {
              window.tracked_pairs[mac].faa_data = result.faa_data;
            }
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
            // Immediately refresh popups with new FAA data
            const key = result.mac || mac;
            if (typeof tracked_pairs !== "undefined" && tracked_pairs[key]) {
              if (droneMarkers[key]) {
                droneMarkers[key].setPopupContent(generatePopupContent(tracked_pairs[key], 'drone'));
                if (droneMarkers[key].isPopupOpen()) {
                  droneMarkers[key].openPopup();
                }
              }
              if (pilotMarkers[key]) {
                pilotMarkers[key].setPopupContent(generatePopupContent(tracked_pairs[key], 'pilot'));
                if (pilotMarkers[key].isPopupOpen()) {
                  pilotMarkers[key].openPopup();
                }
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
  // Remember previous lock so we can clear its buttons
  const prevId = followLock.id;
  // Set new lock
  followLock = { type: markerType, id: id, enabled: true };
  // Update buttons for this id in both drone and pilot sections
  updateMarkerButtons('drone', id);
  updateMarkerButtons('pilot', id);
  localStorage.setItem('followLock', JSON.stringify(followLock));
  // If another id was locked before, clear its button states
  if (prevId && prevId !== id) {
    updateMarkerButtons('drone', prevId);
    updateMarkerButtons('pilot', prevId);
  }
}

function unlockMarker(markerType, id) {
  if (followLock.enabled && followLock.type === markerType && followLock.id === id) {
    // Clear the lock
    followLock = { type: null, id: null, enabled: false };
    // Update buttons for this id in both drone and pilot sections
    updateMarkerButtons('drone', id);
    updateMarkerButtons('pilot', id);
    localStorage.setItem('followLock', JSON.stringify(followLock));
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
  let content = generatePopupContent(Object.assign({mac: mac}, detection), 'alias');
  if (droneMarkers[mac]) {
    droneMarkers[mac].setPopupContent(content).openPopup();
  } else if (pilotMarkers[mac]) {
    pilotMarkers[mac].setPopupContent(content).openPopup();
  } else {
    L.popup({className: 'leaflet-popup-content-wrapper'})
      .setLatLng(map.getCenter())
      .setContent(content)
      .openOn(map);
  }
}

// Updated saveAlias: now it updates the open popup without closing it.
async function saveAlias(mac) {
  let alias = document.getElementById("aliasInput").value;
  try {
    const response = await fetch(window.location.origin + '/api/set_alias', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mac: mac, alias: alias}) });
    const data = await response.json();
    if (data.status === "ok") {
      // Immediately update local alias map so popup content uses new alias
      aliases[mac] = alias;
      updateAliases();
      let detection = window.tracked_pairs[mac] || {mac: mac};
      let content = generatePopupContent(detection, 'alias');
      let currentPopup = map.getPopup();
      if (currentPopup) {
         currentPopup.setContent(content);
      } else {
         L.popup().setContent(content).openOn(map);
      }
      // Immediately update the drone list aliases
      updateComboList(window.tracked_pairs);
      // Flash the updated alias in the popup
      const aliasSpan = document.getElementById('aliasDisplay_' + mac);
      if (aliasSpan) {
        aliasSpan.textContent = alias;
        // Force reflow to apply immediate flash
        aliasSpan.getBoundingClientRect();
        const prevBg = aliasSpan.style.backgroundColor;
        aliasSpan.style.backgroundColor = 'purple';
        setTimeout(() => { aliasSpan.style.backgroundColor = prevBg; }, 300);
      }
      // Ensure the alias list updates immediately
      updateComboList(window.tracked_pairs);
    }
  } catch (error) { console.error("Error saving alias:", error); }
}

async function clearAlias(mac) {
  try {
    const response = await fetch(window.location.origin + '/api/clear_alias/' + mac, {method: 'POST'});
    const data = await response.json();
    if (data.status === "ok") {
      updateAliases();
      let detection = window.tracked_pairs[mac] || {mac: mac};
      let content = generatePopupContent(detection, 'alias');
      L.popup().setContent(content).openOn(map);
      // Immediately update the drone list aliases
      updateComboList(window.tracked_pairs);
    }
  } catch (error) { console.error("Error clearing alias:", error); }
}

const osmStandard = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const osmHumanitarian = L.tileLayer('https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', {
  attribution: '© Humanitarian OpenStreetMap Team',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const cartoPositron = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap contributors, © CARTO',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const cartoDarkMatter = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap contributors, © CARTO',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const esriWorldImagery = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const esriWorldTopo = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const esriDarkGray = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri',
  maxNativeZoom: 16,
  maxZoom: 16,
});
const openTopoMap = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenTopoMap contributors',
  maxNativeZoom: 17,
  maxZoom: 17,
});

  // Load persisted basemap selection or default to satellite imagery
  var persistedBasemap = localStorage.getItem('basemap') || 'esriWorldImagery';
  document.getElementById('layerSelect').value = persistedBasemap;
  var initialLayer;
  switch(persistedBasemap) {
    case 'osmStandard': initialLayer = osmStandard; break;
    case 'osmHumanitarian': initialLayer = osmHumanitarian; break;
    case 'cartoPositron': initialLayer = cartoPositron; break;
    case 'cartoDarkMatter': initialLayer = cartoDarkMatter; break;
    case 'esriWorldImagery': initialLayer = esriWorldImagery; break;
    case 'esriWorldTopo': initialLayer = esriWorldTopo; break;
    case 'esriDarkGray': initialLayer = esriDarkGray; break;
    case 'openTopoMap': initialLayer = openTopoMap; break;
    default: initialLayer = esriWorldImagery;
  }

const map = L.map('map', {
  center: persistedCenter || [0, 0],
  zoom: persistedZoom || 2,
  layers: [initialLayer],
  attributionControl: false,
  maxZoom: initialLayer.options.maxZoom
});
var canvasRenderer = L.canvas();
// create custom Leaflet panes for z-ordering
map.createPane('pilotCirclePane');
map.getPane('pilotCirclePane').style.zIndex = 600;
map.createPane('pilotIconPane');
map.getPane('pilotIconPane').style.zIndex = 601;
map.createPane('droneCirclePane');
map.getPane('droneCirclePane').style.zIndex = 650;
map.createPane('droneIconPane');
map.getPane('droneIconPane').style.zIndex = 651;

map.on('moveend zoomend', function() {
  let center = map.getCenter();
  let zoom = map.getZoom();
  localStorage.setItem('mapCenter', JSON.stringify(center));
  localStorage.setItem('mapZoom', zoom);
});

// Update marker icon sizes whenever the map zoom changes
map.on('zoomend', function() {
  // Scale circle and ring radii based on current zoom
  const zoomLevel = map.getZoom();
  const size = Math.max(12, Math.min(zoomLevel * 1.5, 24));
  const circleRadius = size * 0.45;
  Object.keys(droneMarkers).forEach(mac => {
    const color = get_color_for_mac(mac);
    droneMarkers[mac].setIcon(createIcon('🛸', color));
  });
  Object.keys(pilotMarkers).forEach(mac => {
    const color = get_color_for_mac(mac);
    pilotMarkers[mac].setIcon(createIcon('👤', color));
  });
  // Update circle marker sizes
  Object.values(droneCircles).forEach(circle => circle.setRadius(circleRadius));
  Object.values(pilotCircles).forEach(circle => circle.setRadius(circleRadius));
  // Update broadcast ring sizes
  Object.values(droneBroadcastRings).forEach(ring => ring.setRadius(size * 0.34));
  // Update observer icon size based on zoom level
  if (observerMarker) {
    const storedObserverEmoji = localStorage.getItem('observerEmoji') || "😎";
    observerMarker.setIcon(createIcon(storedObserverEmoji, 'blue'));
  }
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
  newLayer.redraw();
  // Clamp zoom to the layer's allowed maxZoom to avoid missing tiles
  const maxAllowed = newLayer.options.maxZoom;
  if (map.getZoom() > maxAllowed) {
    map.setZoom(maxAllowed);
  }
  // update map's allowed max zoom for this layer
  map.options.maxZoom = maxAllowed;
  localStorage.setItem('basemap', value);
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
    // Use stored observer emoji or default to "😎"
    const storedObserverEmoji = localStorage.getItem('observerEmoji') || "😎";
    const observerIcon = createIcon(storedObserverEmoji, 'blue');
    if (!observerMarker) {
      observerMarker = L.marker([lat, lng], {icon: observerIcon})
                        .bindPopup(generateObserverPopup())
                        .addTo(map)
                        .on('popupopen', function() { updateObserverPopupButtons(); })
                        .on('click', function() { safeSetView(observerMarker.getLatLng(), 18); });
    } else { observerMarker.setLatLng([lat, lng]); }
  }, function(error) { console.error("Error watching location:", error); }, { enableHighAccuracy: true, maximumAge: 10000, timeout: 5000 });
} else { console.error("Geolocation is not supported by this browser."); }

function zoomToDrone(mac, detection) {
  // Only zoom if we have valid, non-zero coordinates
  if (
    detection &&
    detection.drone_lat !== undefined &&
    detection.drone_long !== undefined &&
    detection.drone_lat !== 0 &&
    detection.drone_long !== 0
  ) {
    safeSetView([detection.drone_lat, detection.drone_long], 18);
  }
}

function showHistoricalDrone(mac, detection) {
  // Only map drones with valid, non-zero coordinates
  if (
    detection.drone_lat === undefined ||
    detection.drone_long === undefined ||
    detection.drone_lat === 0 ||
    detection.drone_long === 0
  ) {
    return;
  }
  const color = get_color_for_mac(mac);
  if (!droneMarkers[mac]) {
    droneMarkers[mac] = L.marker([detection.drone_lat, detection.drone_long], {
      icon: createIcon('🛸', color),
      pane: 'droneIconPane'
    })
                           .bindPopup(generatePopupContent(detection, 'drone'))
                           .addTo(map)
                           .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
  } else {
    droneMarkers[mac].setLatLng([detection.drone_lat, detection.drone_long]);
    droneMarkers[mac].setPopupContent(generatePopupContent(detection, 'drone'));
  }
  if (!droneCircles[mac]) {
    const zoomLevel = map.getZoom();
    const size = Math.max(12, Math.min(zoomLevel * 1.5, 24));
    droneCircles[mac] = L.circleMarker([detection.drone_lat, detection.drone_long],
                                       {
                                         renderer: canvasRenderer,
                                         pane: 'droneCirclePane',
                                         radius: size * 0.45,
                                         color: color,
                                         fillColor: color,
                                         fillOpacity: 0.7
                                       })
                           .addTo(map);
  } else { droneCircles[mac].setLatLng([detection.drone_lat, detection.drone_long]); }
  if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
  const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
  if (!lastDrone || lastDrone[0] != detection.drone_lat || lastDrone[1] != detection.drone_long) { dronePathCoords[mac].push([detection.drone_lat, detection.drone_long]); }
  if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
  dronePolylines[mac] = L.polyline(dronePathCoords[mac], {
    renderer: canvasRenderer,
    color: color
  }).addTo(map);
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    if (!pilotMarkers[mac]) {
      pilotMarkers[mac] = L.marker([detection.pilot_lat, detection.pilot_long], {
        icon: createIcon('👤', color),
        pane: 'pilotIconPane'
      })
                             .bindPopup(generatePopupContent(detection, 'pilot'))
                             .addTo(map)
                             .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
    } else {
      pilotMarkers[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
      pilotMarkers[mac].setPopupContent(generatePopupContent(detection, 'pilot'));
    }
    if (!pilotCircles[mac]) {
      const zoomLevel = map.getZoom();
      const size = Math.max(12, Math.min(zoomLevel * 1.5, 24));
      pilotCircles[mac] = L.circleMarker([detection.pilot_lat, detection.pilot_long],
                                          {
                                            renderer: canvasRenderer,
                                            pane: 'pilotCirclePane',
                                            radius: size * 0.34,
                                            color: color,
                                            fillColor: color,
                                            fillOpacity: 0.7
                                          })
                            .addTo(map);
    } else { pilotCircles[mac].setLatLng([detection.pilot_lat, detection.pilot_long]); }
    // Historical pilot path (dotted)
    if (!pilotPathCoords[mac]) { pilotPathCoords[mac] = []; }
    const lastPilotHis = pilotPathCoords[mac][pilotPathCoords[mac].length - 1];
    if (!lastPilotHis || lastPilotHis[0] !== detection.pilot_lat || lastPilotHis[1] !== detection.pilot_long) {
      pilotPathCoords[mac].push([detection.pilot_lat, detection.pilot_long]);
    }
    if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); }
    pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {
      renderer: canvasRenderer,
      color: color,
      dashArray: '5,5'
    }).addTo(map);
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
    let isActive = detection && ((currentTime - detection.last_update) <= STALE_THRESHOLD);
    let item = comboListItems[mac];
    if (!item) {
      item = document.createElement("div");
      // Tooltip for drones without valid GPS
      const det = data[mac];
      const hasGps = det && det.drone_lat && det.drone_long && det.drone_lat !== 0 && det.drone_long !== 0;
      if (!hasGps) {
        item.classList.add('no-gps');
        item.setAttribute('data-tooltip', 'awaiting valid gps coordinates');
      }
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
    // Mark items seen in the last 5 second
    const isRecent = detection && ((currentTime - detection.last_update) <= 5);
    item.classList.toggle('recent', isRecent);
    if (isActive) {
      if (item.parentNode !== activePlaceholder) { activePlaceholder.appendChild(item); }
    } else {
      if (item.parentNode !== inactivePlaceholder) { inactivePlaceholder.appendChild(item); }
    }
  });
}

// Only zoom on truly new detections—never on the initial restore
var initialLoad    = true;
var seenDrones     = {};
var seenAliased    = {};
var previousActive = {};
// Initialize seenDrones and previousActive from persisted trackedPairs to suppress reload popups
(function() {
  const stored = localStorage.getItem("trackedPairs");
  if (stored) {
    try {
      const storedPairs = JSON.parse(stored);
      for (const mac in storedPairs) {
        seenDrones[mac] = true;
        // previousActive[mac] = true;
      }
    } catch(e) { console.error("Failed to parse persisted trackedPairs", e); }
  }
})();
async function updateData() {
  try {
    const response = await fetch(window.location.origin + '/api/detections')
    const data = await response.json();
    window.tracked_pairs = data;
    // Persist current detection data to localStorage so that markers & paths remain on reload.
    localStorage.setItem("trackedPairs", JSON.stringify(data));
    const currentTime = Date.now() / 1000;
    for (const mac in data) { if (!persistentMACs.includes(mac)) { persistentMACs.push(mac); } }
    for (const mac in data) {
      if (historicalDrones[mac]) {
        if (data[mac].last_update > historicalDrones[mac].lockTime || (currentTime - historicalDrones[mac].lockTime) > STALE_THRESHOLD) {
          delete historicalDrones[mac];
          localStorage.setItem('historicalDrones', JSON.stringify(historicalDrones));
          if (droneBroadcastRings[mac]) { map.removeLayer(droneBroadcastRings[mac]); delete droneBroadcastRings[mac]; }
        } else { continue; }
      }
      const det = data[mac];
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
        // Mark as inactive to enable revival popups
        previousActive[mac] = false;
        continue;
      }
      const droneLat = det.drone_lat, droneLng = det.drone_long;
      const pilotLat = det.pilot_lat, pilotLng = det.pilot_long;
      const validDrone = (droneLat !== 0 && droneLng !== 0);
      // State-change popup logic
      const alias     = aliases[mac];
      // New state calculation: consider time-based staleness
      const activeNow = validDrone && det.last_update && (currentTime - det.last_update <= STALE_THRESHOLD);
      const wasActive = previousActive[mac] || false;
      const isNew     = !seenDrones[mac];

      // Only fire popup on transition from inactive to active, after initial load, and within stale threshold
      if (!initialLoad && det.last_update && (currentTime - det.last_update <= STALE_THRESHOLD) && !wasActive) {
        showTerminalPopup(det, alias ? false : !seenDrones[mac]);
        seenDrones[mac] = true;
      }
      // Persist for next update
      previousActive[mac] = activeNow;

      const validPilot = (pilotLat !== 0 && pilotLng !== 0);
      // Allow popups after initial load completes
      initialLoad = false;
      if (!validDrone && !validPilot) continue;
      const color = get_color_for_mac(mac);
      // First detection zoom block (keep this block only)
      if (!initialLoad && !firstDetectionZoomed && validDrone) {
        firstDetectionZoomed = true;
        safeSetView([droneLat, droneLng], 18);
      }
      if (validDrone) {
        if (droneMarkers[mac]) {
          droneMarkers[mac].setLatLng([droneLat, droneLng]);
          if (!droneMarkers[mac].isPopupOpen()) { droneMarkers[mac].setPopupContent(generatePopupContent(det, 'drone')); }
        } else {
          droneMarkers[mac] = L.marker([droneLat, droneLng], {
            icon: createIcon('🛸', color),
            pane: 'droneIconPane'
          })
                                .bindPopup(generatePopupContent(det, 'drone'))
                                .addTo(map)
                                // Remove automatic zoom on marker click:
                                //.on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
                                ;
        }
        if (droneCircles[mac]) { droneCircles[mac].setLatLng([droneLat, droneLng]); }
        else {
          const zoomLevel = map.getZoom();
          const size = Math.max(12, Math.min(zoomLevel * 1.5, 24));
          droneCircles[mac] = L.circleMarker([droneLat, droneLng], {
            pane: 'droneCirclePane',
            radius: size * 0.45,
            color: color,
            fillColor: color,
            fillOpacity: 0.7
          }).addTo(map);
        }
        if (!dronePathCoords[mac]) { dronePathCoords[mac] = []; }
        const lastDrone = dronePathCoords[mac][dronePathCoords[mac].length - 1];
        if (!lastDrone || lastDrone[0] != droneLat || lastDrone[1] != droneLng) { dronePathCoords[mac].push([droneLat, droneLng]); }
        if (dronePolylines[mac]) { map.removeLayer(dronePolylines[mac]); }
        dronePolylines[mac] = L.polyline(dronePathCoords[mac], {color: color}).addTo(map);
        if (currentTime - det.last_update <= 5) {
          const dynamicRadius = getDynamicSize() * 0.45;
          const ringWeight = 3 * 0.8;  // 20% thinner
          const ringRadius = dynamicRadius + ringWeight / 2;  // sit just outside the main circle
          if (droneBroadcastRings[mac]) {
            droneBroadcastRings[mac].setLatLng([droneLat, droneLng]);
            droneBroadcastRings[mac].setRadius(ringRadius);
            droneBroadcastRings[mac].setStyle({ weight: ringWeight });
          } else {
            droneBroadcastRings[mac] = L.circleMarker([droneLat, droneLng], {
              pane: 'droneCirclePane',
              radius: ringRadius,
              color: "lime",
              fill: false,
              weight: ringWeight
            }).addTo(map);
          }
        } else {
          if (droneBroadcastRings[mac]) {
            map.removeLayer(droneBroadcastRings[mac]);
            delete droneBroadcastRings[mac];
          }
        }
        // Remove automatic follow-zoom (except for followLock, which is allowed)
        // (auto-zoom disabled except for followLock)
        if (followLock.enabled && followLock.type === 'drone' && followLock.id === mac) { map.setView([droneLat, droneLng], map.getZoom()); }
      }
      if (validPilot) {
        if (pilotMarkers[mac]) {
          pilotMarkers[mac].setLatLng([pilotLat, pilotLng]);
          if (!pilotMarkers[mac].isPopupOpen()) { pilotMarkers[mac].setPopupContent(generatePopupContent(det, 'pilot')); }
        } else {
          pilotMarkers[mac] = L.marker([pilotLat, pilotLng], {
            icon: createIcon('👤', color),
            pane: 'pilotIconPane'
          })
                                .bindPopup(generatePopupContent(det, 'pilot'))
                                .addTo(map)
                                // Remove automatic zoom on marker click:
                                //.on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
                                ;
        }
        if (pilotCircles[mac]) { pilotCircles[mac].setLatLng([pilotLat, pilotLng]); }
        else {
          const zoomLevel = map.getZoom();
          const size = Math.max(12, Math.min(zoomLevel * 1.5, 24));
          pilotCircles[mac] = L.circleMarker([pilotLat, pilotLng], {
            pane: 'pilotCirclePane',
            radius: size * 0.34,
            color: color,
            fillColor: color,
            fillOpacity: 0.7
          }).addTo(map);
        }
        if (!pilotPathCoords[mac]) { pilotPathCoords[mac] = []; }
        const lastPilot = pilotPathCoords[mac][pilotPathCoords[mac].length - 1];
        if (!lastPilot || lastPilot[0] != pilotLat || lastPilot[1] != pilotLng) { pilotPathCoords[mac].push([pilotLat, pilotLng]); }
        if (pilotPolylines[mac]) { map.removeLayer(pilotPolylines[mac]); }
        pilotPolylines[mac] = L.polyline(pilotPathCoords[mac], {color: color, dashArray: '5,5'}).addTo(map);
        // Remove automatic follow-zoom (except for followLock, which is allowed)
        // (auto-zoom disabled except for followLock)
        if (followLock.enabled && followLock.type === 'pilot' && followLock.id === mac) { map.setView([pilotLat, pilotLng], map.getZoom()); }
      }
      // At end of loop iteration, remember this state for next time
      previousActive[mac] = validDrone;
    }
    initialLoad = false;
    updateComboList(data);
    updateAliases();
    // Mark that the first restore/update is done
    initialLoad = false;

    // Handle no-GPS styling and alerts in the inactive list
    for (const mac in data) {
      const det = data[mac];
      const droneElem = comboListItems[mac];
      if (!droneElem) continue;
      if (!det.drone_lat || !det.drone_long || det.drone_lat === 0 || det.drone_long === 0) {
        // Apply no-GPS styling and one-time alert
        droneElem.classList.add('no-gps');
        if (!alertedNoGpsDrones.has(det.mac)) {
          showTerminalPopup(det, true);
          alertedNoGpsDrones.add(det.mac);
        }
      } else {
        // Remove no-GPS styling and reset alert state
        droneElem.classList.remove('no-gps');
        alertedNoGpsDrones.delete(det.mac);
      }
    }
  } catch (error) { console.error("Error fetching detection data:", error); }
}

function createIcon(emoji, color) {
  // Compute a dynamic size based on zoom
  const size = getDynamicSize();
  const actualSize = emoji === '👤' ? Math.round(size * 0.7) : Math.round(size);
  const isize = actualSize;
  const half = Math.round(actualSize / 2);
  return L.divIcon({
    html: `<div style="width:${isize}px; height:${isize}px; font-size:${isize}px; color:${color}; text-align:center; line-height:${isize}px;">${emoji}</div>`,
    className: '',
    iconSize: [isize, isize],
    iconAnchor: [half, half]
  });
}

function getDynamicSize() {
  const zoomLevel = map.getZoom();
  // Clamp between 12px and 24px, then boost by 15%
  const base = Math.max(12, Math.min(zoomLevel * 1.5, 24));
  return base * 1.15;
}

// Updated function: now updates all selected USB port statuses.
async function updateSerialStatus() {
  try {
    const response = await fetch(window.location.origin + '/api/serial_status')
    const data = await response.json();
    const statusDiv = document.getElementById('serialStatus');
    statusDiv.innerHTML = "";
    if (data.statuses) {
      for (const port in data.statuses) {
        const div = document.createElement("div");
        // Device name in neon pink and status color accordingly.
        div.innerHTML = '<span class="usb-name">' + port + '</span>: ' +
          (data.statuses[port] ? '<span style="color: lime;">Connected</span>' : '<span style="color: red;">Disconnected</span>');
        statusDiv.appendChild(div);
      }
    }
  } catch (error) { console.error("Error fetching serial status:", error); }
}
setInterval(updateSerialStatus, 1000);
updateSerialStatus();

// (Node Mode mainSwitch and polling interval are now managed solely by the DOMContentLoaded handler above.)
// Sync popup Node Mode toggle when a popup opens

function updateLockFollow() {
  if (followLock.enabled) {
    if (followLock.type === 'observer' && observerMarker) { map.setView(observerMarker.getLatLng(), map.getZoom()); }
    else if (followLock.type === 'drone' && droneMarkers[followLock.id]) { map.setView(droneMarkers[followLock.id].getLatLng(), map.getZoom()); }
    else if (followLock.type === 'pilot' && pilotMarkers[followLock.id]) { map.setView(pilotMarkers[followLock.id].getLatLng(), map.getZoom()); }
  }
}
setInterval(updateLockFollow, 200);

document.getElementById("filterToggle").addEventListener("click", function() {
  const box = document.getElementById("filterBox");
  const isCollapsed = box.classList.toggle("collapsed");
  this.textContent = isCollapsed ? "[+]" : "[-]";
  // Sync Node Mode toggle with stored setting when filter opens
  const mainSwitch = document.getElementById('nodeModeMainSwitch');
  mainSwitch.checked = (localStorage.getItem('nodeMode') === 'true');
});

async function restorePaths() {
  try {
    const response = await fetch(window.location.origin + '/api/paths')
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
<script>
  // Download buttons click handlers with purple flash
  document.getElementById('downloadCsv').addEventListener('click', function() {
    this.style.backgroundColor = 'purple';
    setTimeout(() => { this.style.backgroundColor = '#333'; }, 300);
    window.location.href = '/download/csv';
  });
  document.getElementById('downloadKml').addEventListener('click', function() {
    this.style.backgroundColor = 'purple';
    setTimeout(() => { this.style.backgroundColor = '#333'; }, 300);
    window.location.href = '/download/kml';
  });
  document.getElementById('downloadAliases').addEventListener('click', function() {
    this.style.backgroundColor = 'purple';
    setTimeout(() => { this.style.backgroundColor = '#333'; }, 300);
    window.location.href = '/download/aliases';
  });
  document.getElementById('downloadCumulativeCsv').addEventListener('click', function() {
    window.location = '/download/cumulative_detections.csv';
  });
  document.getElementById('downloadCumulativeKml').addEventListener('click', function() {
    window.location = '/download/cumulative.kml';
  });
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js')
      .then(reg => console.log('Service Worker registered', reg))
      .catch(err => console.error('Service Worker registration failed', err));
  }
</script>
</body>
</html>
<script>
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js')
      .then(reg => console.log('Service Worker registered', reg))
      .catch(err => console.error('Service Worker registration failed', err));
  }
</script>
'''
# ----------------------
# New route: USB port selection for multiple ports.
# ----------------------
@app.route('/sw.js')
def service_worker():
    sw_code = '''
self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open('tile-cache').then(function(cache) {
      return cache.addAll([]);
    })
  );
});
self.addEventListener('fetch', function(event) {
  var url = event.request.url;
  // Only cache tile requests
  if (url.includes('tile.openstreetmap.org') || url.includes('basemaps.cartocdn.com') || url.includes('server.arcgisonline.com') || url.includes('tile.opentopomap.org')) {
    event.respondWith(
      caches.open('tile-cache').then(function(cache) {
        return cache.match(event.request).then(function(response) {
          return response || fetch(event.request).then(function(networkResponse) {
            cache.put(event.request, networkResponse.clone());
            return networkResponse;
          });
        });
      })
    );
  }
});
'''
    response = app.make_response(sw_code)
    response.headers['Content-Type'] = 'application/javascript'
    return response


# ----------------------
# New route: USB port selection for multiple ports.
# ----------------------
@app.route('/select_ports', methods=['GET'])
def select_ports_get():
    ports = list(serial.tools.list_ports.comports())
    return render_template_string(PORT_SELECTION_PAGE, ports=ports, logo_ascii=LOGO_ASCII, bottom_ascii=BOTTOM_ASCII)


@app.route('/select_ports', methods=['POST'])
def select_ports_post():
    global SELECTED_PORTS
    # Get up to 3 ports; ignore empty values
    for i in range(1, 4):
        port = request.form.get(f'port{i}')
        if port:
            SELECTED_PORTS[f'port{i}'] = port

    # Start serial-reader threads for selected ports
    for port in SELECTED_PORTS.values():
        serial_connected_status[port] = False
        start_serial_thread(port)
    # Send watchdog reset to each microcontroller over USB
    with serial_objs_lock:
        for ser in serial_objs.values():
            try:
                ser.write(b'WATCHDOG_RESET\n')
            except Exception as e:
                logging.error(f"Failed to send watchdog reset: {e}")


    # Redirect to main page
    return redirect(url_for('index'))


# ----------------------
# ASCII art blocks
# ----------------------
BOTTOM_ASCII = r"""
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⣄⣠⣀⡀⣀⣠⣤⣤⣤⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣄⢠⣠⣼⣿⣿⣿⣟⣿⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⠀⢠⣤⣦⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠰⢦⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣟⣾⣿⣽⣿⣿⣅⠈⠉⠻⣿⣿⣿⣿⣿⡿⠇⠀⠀⠀⠀⠀⠉⠀⠀⠀⠀⠀⢀⡶⠒⢉⡀⢠⣤⣶⣶⣿⣷⣆⣀⡀⠀⢲⣖⠒⠀⠀⠀⠀⠀⠀⠀
⢀⣤⣾⣶⣦⣤⣤⣶⣿⣿⣿⣿⣿⣿⣽⡿⠻⣷⣀⠀⢻⣿⣿⣿⡿⠟⠀⠀⠀⠀⠀⠀⣤⣶⣶⣤⣀⣀⣬⣷⣦⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣤⣦⣼⣀⠀
⠈⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠛⠓⣿⣿⠟⠁⠘⣿⡟⠁⠀⠘⠛⠁⠀⠀⢠⣾⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠏⠙⠁
⠀⠀⠸⠟⠋⠀⠈⠙⣿⣿⣿⣿⣿⣿⣷⣦⡄⣿⣿⣿⣆⠀⠀⠀⠀⠀⠀⠀⠀⣼⣆⢘⣿⣯⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡉⠉⢱⡿⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⡿⠦⠀⠀⠀⠀⠀⠀⠀⠙⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⡗⠀⠈⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣿⣿⣿⣿⣿⣿⣿⣿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⢿⣿⣉⣿⡿⢿⢷⣾⣾⣿⣞⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠋⣠⠟⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⣿⠿⠿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣾⣿⣿⣷⣦⣶⣦⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠈⠛⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠻⣿⣤⡖⠛⠶⠤⡀⠀⠀⠀⠀⠀⠀⠀⢰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠁⠙⣿⣿⠿⢻⣿⣿⡿⠋⢩⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠧⣤⣦⣤⣄⡀⠀⠀⠀⠀⠀⠘⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⠘⣧⠀⠈⣹⡻⠇⢀⣿⡆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣤⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢽⣿⣿⣿⣿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠹⣷⣴⣿⣷⢲⣦⣤⡀⢀⡀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢿⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⣷⢀⡄⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠂⠛⣆⣤⡜⣟⠋⠙⠂⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿⣿⠉⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣤⣾⣿⣿⣿⣿⣆⠀⠰⠄⠀⠉⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⣿⣿⡿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⡿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣿⠿⠿⣿⣿⣿⠇⠀⠀⢀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣿⡿⠛⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢻⡇⠀⠀⢀⣼⠗⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⠃⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠁⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠒⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
"""

LOGO_ASCII = r"""
        _____                .__      ________          __                 __       
       /     \   ____   _____|  |__   \______ \   _____/  |_  ____   _____/  |_     
      /  \ /  \_/ __ \ /  ___/  |  \   |    |  \_/ __ \   __\/ __ \_/ ___\   __\    
     /    Y    \  ___/ \___ \|   Y  \  |    `   \  ___/|  | \  ___/\  \___|  |      
     \____|__  /\___  >____  >___|  / /_______  /\___  >__|  \___  >\___  >__|      
             \/     \/     \/     \/          \/     \/     \/          \/     \/          
________                                  _____                                     
\______ \_______  ____   ____   ____     /     \ _____  ______ ______   ___________ 
 |    |  \_  __ \/  _ \ /    \_/ __ \   /  \ /  \\__  \ \____ \\____ \_/ __ \_  __ \
 |    `   \  | \(  <_> )   |  \  ___/  /    Y    \/ __ \|  |_> >  |_> >  ___/|  | \/
/_______  /__|   \____/|___|  /\___  > \____|__  (____  /   __/|   __/ \___  >__|   
        \/                  \/     \/          \/     \/|__|   |__|        \/       
"""

@app.route('/')
def index():
    if (len(SELECTED_PORTS) == 0):
        return redirect(url_for('select_ports_get'))
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

# Updated status endpoint: returns a dict of statuses for each selected USB.
@app.route('/api/ports', methods=['GET'])
def api_ports():
    ports = list(serial.tools.list_ports.comports())
    return jsonify({
        'ports': [{'device': p.device, 'description': p.description} for p in ports]
    })

# Updated status endpoint: returns a dict of statuses for each selected USB.
@app.route('/api/serial_status', methods=['GET'])
def api_serial_status():
    return jsonify({"statuses": serial_connected_status})

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

# ----------------------
# Serial Reader Threads: Each selected port gets its own thread.
# ----------------------
def serial_reader(port):
    ser = None
    while True:
        # Try to open or re-open the serial port
        if ser is None or not getattr(ser, 'is_open', False):
            try:
                ser = serial.Serial(port, BAUD_RATE, timeout=1)
                serial_connected_status[port] = True
                print(f"Opened serial port {port} at {BAUD_RATE} baud.")
                with serial_objs_lock:
                    serial_objs[port] = ser
            except Exception as e:
                serial_connected_status[port] = False
                print(f"Error opening serial port {port}: {e}")
                time.sleep(1)
                continue

        try:
            # Read incoming data
            if ser.in_waiting:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue
                # JSON extraction and detection handling...
                if '{' in line:
                    json_str = line[line.find('{'):]
                else:
                    json_str = line
                try:
                    detection = json.loads(json_str)
                    # MAC tracking logic...
                    if 'mac' in detection:
                        last_mac_by_port[port] = detection['mac']
                    elif port in last_mac_by_port:
                        detection['mac'] = last_mac_by_port[port]
                except json.JSONDecodeError:
                    continue
                if 'remote_id' in detection and 'basic_id' not in detection:
                    detection['basic_id'] = detection['remote_id']
                if 'heartbeat' in detection:
                    continue
                update_detection(detection)
            else:
                time.sleep(0.1)
        except (serial.SerialException, OSError) as e:
            serial_connected_status[port] = False
            print(f"SerialException/OSError on {port}: {e}")
            try:
                if ser and ser.is_open:
                    ser.close()
            except Exception:
                pass
            ser = None
            with serial_objs_lock:
                serial_objs.pop(port, None)
            time.sleep(1)
        except Exception as e:
            serial_connected_status[port] = False
            print(f"Unexpected error on {port}: {e}")
            try:
                if ser and ser.is_open:
                    ser.close()
            except Exception:
                pass
            ser = None
            with serial_objs_lock:
                serial_objs.pop(port, None)
            time.sleep(1)

def start_serial_thread(port):
    thread = threading.Thread(target=serial_reader, args=(port,), daemon=True)
    thread.start()

# Download endpoints for CSV, KML, and Aliases files
@app.route('/download/csv')
def download_csv():
    return send_file(CSV_FILENAME, as_attachment=True)

@app.route('/download/kml')
def download_kml():
    # regenerate KML to include latest detections
    generate_kml()
    return send_file(KML_FILENAME, as_attachment=True)

@app.route('/download/aliases')
def download_aliases():
    # ensure latest aliases are saved to disk
    save_aliases()
    return send_file(ALIASES_FILE, as_attachment=True)


# --- Cumulative download endpoints ---
@app.route('/download/cumulative_detections.csv')
def download_cumulative_csv():
    return send_file(
        CUMULATIVE_CSV_FILENAME,
        mimetype='text/csv',
        as_attachment=True,
        download_name='cumulative_detections.csv'
    )

@app.route('/download/cumulative.kml')
def download_cumulative_kml():
    return send_file(
        CUMULATIVE_KML_FILENAME,
        mimetype='application/vnd.google-earth.kml+xml',
        as_attachment=True,
        download_name='cumulative.kml'
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)


app = Flask(__name__)

# --- FAA Data Cache (simple in-memory for example) ---
FAA_CACHE = {}  # key: (mac, remote_id) or (mac, ""), value: faa_data dict
TRACKED_PAIRS = {}  # key: mac, value: dict with at least 'basic_id' and 'faa_data'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAA_LOG_FILENAME = os.path.join(BASE_DIR, "faa_log.csv")

# --- FAA Query API Endpoint ---
@app.route('/api/query_faa', methods=['POST'])
def api_query_faa():
    data = request.get_json()
    mac = data.get("mac")
    remote_id = data.get("remote_id")
    if not mac or not remote_id:
        return jsonify({"status": "error", "message": "Missing mac or remote_id"}), 400
    # Simulate FAA API query
    faa_result = {
        "data": {
            "items": [
                {
                    "makeName": "ExampleMake",
                    "modelName": "ModelX",
                    "series": "A1",
                    "trackingNumber": "123456",
                    "complianceCategories": "Standard",
                    "updatedAt": datetime.now().isoformat()
                }
            ]
        }
    }
    # Save to cache and tracked_pairs
    FAA_CACHE[(mac, remote_id)] = faa_result
    TRACKED_PAIRS[mac] = {"basic_id": remote_id, "faa_data": faa_result}
    # Log to CSV
    if FAA_LOG_FILENAME:
        try:
            file_exists = os.path.isfile(FAA_LOG_FILENAME)
            with open(FAA_LOG_FILENAME, "a", newline='') as csvfile:
                fieldnames = ["timestamp", "mac", "remote_id", "faa_response"]
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                if not file_exists:
                    writer.writeheader()
                writer.writerow({
                    "timestamp": datetime.now().isoformat(),
                    "mac": mac,
                    "remote_id": remote_id,
                    "faa_response": json.dumps(faa_result)
                })
        except Exception as e:
            print("Error writing to FAA log CSV:", e)
    return jsonify({"status": "ok", "faa_data": faa_result})

# --- FAA Data GET API Endpoint (by MAC or basic_id) ---
@app.route('/api/faa/<identifier>', methods=['GET'])
def api_get_faa(identifier):
    """
    Retrieve cached FAA data by MAC address or by basic_id (remote ID).
    """
    # Lookup by MAC in tracked_pairs
    if identifier in TRACKED_PAIRS and 'faa_data' in TRACKED_PAIRS[identifier]:
        return jsonify({'status': 'ok', 'faa_data': TRACKED_PAIRS[identifier]['faa_data']})
    # Lookup by basic_id in tracked_pairs
    for mac, det in TRACKED_PAIRS.items():
        if det.get('basic_id') == identifier and 'faa_data' in det:
            return jsonify({'status': 'ok', 'faa_data': det['faa_data']})
    # FAA_CACHE search by remote_id then by MAC
    for (c_mac, c_rid), faa_data in FAA_CACHE.items():
        if c_rid == identifier:
            return jsonify({'status': 'ok', 'faa_data': faa_data})
    for (c_mac, c_rid), faa_data in FAA_CACHE.items():
        if c_mac == identifier:
            return jsonify({'status': 'ok', 'faa_data': faa_data})
    return jsonify({'status': 'error', 'message': 'No FAA data found for this identifier'}), 404
