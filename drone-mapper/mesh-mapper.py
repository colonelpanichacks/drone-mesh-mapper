import os
import time
import json
import csv
import logging
import colorsys
import threading
import requests
import urllib3
import serial
import serial.tools.list_ports
import signal
import sys
import argparse
from datetime import datetime, timedelta
from typing import Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from flask import Flask, request, jsonify, redirect, url_for, render_template, render_template_string, send_file
from flask_socketio import SocketIO, emit
from collections import deque
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------
# Enhanced Logging Setup
# ----------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[
        logging.FileHandler('mapper.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Add a debug mode flag that can be toggled
DEBUG_MODE = False

def set_debug_mode(enabled=True):
    """Enable or disable debug logging"""
    global DEBUG_MODE
    DEBUG_MODE = enabled
    if enabled:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.info("Debug logging enabled")
    else:
        logging.getLogger().setLevel(logging.INFO)
        logger.info("Debug logging disabled")

# ----------------------
# Global Configuration
# ----------------------
HEADLESS_MODE = False
AUTO_START_ENABLED = True
PORT_MONITOR_INTERVAL = 10  # seconds
SHUTDOWN_EVENT = threading.Event()

# ----------------------
# Performance Optimizations
# ----------------------
MAX_DETECTION_HISTORY = 1000  # Limit detection history size
MAX_FAA_CACHE_SIZE = 500      # Limit FAA cache size
KML_GENERATION_INTERVAL = 30  # Only regenerate KML every 30 seconds
last_kml_generation = 0
last_cumulative_kml_generation = 0

def cleanup_old_detections():
    """Mark stale detections as inactive instead of removing them to preserve session persistence"""
    current_time = time.time()
    
    for mac, detection in tracked_pairs.items():
        last_update = detection.get('last_update', 0)
        # Instead of deleting, mark as inactive for very old detections (30+ minutes)
        if current_time - last_update > staleThreshold * 30:  # 30x stale threshold (30 minutes)
            detection['status'] = 'inactive_old'  # Mark as very old but keep in session
        elif current_time - last_update > staleThreshold * 3:  # 3x stale threshold (3 minutes)
            detection['status'] = 'inactive'  # Mark as inactive but keep in session
    
    # Only clean up FAA cache, but keep drone detections for session persistence
    if len(FAA_CACHE) > MAX_FAA_CACHE_SIZE:
        keys_to_remove = list(FAA_CACHE.keys())[:100]
        for key in keys_to_remove:
            del FAA_CACHE[key]

def start_cleanup_timer():
    """Start periodic cleanup every 5 minutes"""
    def cleanup_timer():
        while not SHUTDOWN_EVENT.is_set():
            cleanup_old_detections()
            time.sleep(300)  # 5 minutes
    
    cleanup_thread = threading.Thread(target=cleanup_timer, daemon=True)
    cleanup_thread.start()
    logger.info("Cleanup timer started")

# ----------------------
# Signal Handlers for Graceful Shutdown
# ----------------------
def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    SHUTDOWN_EVENT.set()
    
    # Close all serial connections
    with serial_objs_lock:
        for port, ser in serial_objs.items():
            try:
                if ser and ser.is_open:
                    logger.info(f"Closing serial connection to {port}")
                    ser.close()
            except Exception as e:
                logger.error(f"Error closing serial port {port}: {e}")
    
    logger.info("Shutdown complete")
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

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
    save_webhook_url()  # Save to disk whenever URL is updated

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # Enable Socket.IO

# Define emit_serial_status early to avoid NameError in threads
def emit_serial_status():
    try:
        socketio.emit('serial_status', serial_connected_status, )
    except Exception as e:
        logger.debug(f"Error emitting serial status: {e}")
        pass  # Ignore if no clients connected or serialization error

def emit_aliases():
    try:
        socketio.emit('aliases', ALIASES, )
    except Exception as e:
        logger.debug(f"Error emitting aliases: {e}")

def emit_detections():
    try:
        # Convert tracked_pairs to a JSON-serializable format
        serializable_pairs = {}
        for key, value in tracked_pairs.items():
            # Ensure key is a string
            str_key = str(key)
            # Ensure value is JSON-serializable
            if isinstance(value, dict):
                serializable_pairs[str_key] = value
            else:
                serializable_pairs[str_key] = str(value)
        socketio.emit('detections', serializable_pairs, )
    except Exception as e:
        logger.debug(f"Error emitting detections: {e}")

def emit_paths():
    try:
        socketio.emit('paths', get_paths_for_emit(), )
    except Exception as e:
        logger.debug(f"Error emitting paths: {e}")

def emit_cumulative_log():
    try:
        socketio.emit('cumulative_log', get_cumulative_log_for_emit(), )
    except Exception as e:
        logger.debug(f"Error emitting cumulative log: {e}")

def emit_faa_cache():
    try:
        # Convert FAA_CACHE to JSON-serializable format
        serializable_cache = {}
        for key, value in FAA_CACHE.items():
            # Convert tuple keys to strings
            str_key = str(key) if isinstance(key, tuple) else key
            serializable_cache[str_key] = value
        socketio.emit('faa_cache', serializable_cache, )
    except Exception as e:
        logger.debug(f"Error emitting FAA cache: {e}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------
# Webhook URL Persistence (must be early in file)
# ----------------------
WEBHOOK_URL_FILE = os.path.join(BASE_DIR, "webhook_url.json")

def save_webhook_url():
    """Save the current webhook URL to disk"""
    global WEBHOOK_URL
    try:
        with open(WEBHOOK_URL_FILE, "w") as f:
            json.dump({"webhook_url": WEBHOOK_URL}, f)
        logger.debug(f"Webhook URL saved to {WEBHOOK_URL_FILE}")
    except Exception as e:
        logger.error(f"Error saving webhook URL: {e}")

def load_webhook_url():
    """Load the webhook URL from disk on startup"""
    global WEBHOOK_URL
    if os.path.exists(WEBHOOK_URL_FILE):
        try:
            with open(WEBHOOK_URL_FILE, "r") as f:
                data = json.load(f)
                WEBHOOK_URL = data.get("webhook_url", None)
                if WEBHOOK_URL:
                    logger.info(f"Loaded saved webhook URL: {WEBHOOK_URL}")
                else:
                    logger.info("No webhook URL found in saved file")
        except Exception as e:
            logger.error(f"Error loading webhook URL: {e}")
            WEBHOOK_URL = None
    else:
        logger.info("No saved webhook URL file found")
        WEBHOOK_URL = None

# ----------------------
# Global Variables & Files
# ----------------------
tracked_pairs = {}
detection_history = deque(maxlen=MAX_DETECTION_HISTORY)  # Limit size to prevent memory growth

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
PORTS_FILE = os.path.join(BASE_DIR, "selected_ports.json")
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

# --- Port Persistence ---
def save_selected_ports():
    global SELECTED_PORTS
    try:
        with open(PORTS_FILE, "w") as f:
            json.dump(SELECTED_PORTS, f)
    except Exception as e:
        print("Error saving selected ports:", e)

def load_selected_ports():
    global SELECTED_PORTS
    if os.path.exists(PORTS_FILE):
        try:
            with open(PORTS_FILE, "r") as f:
                SELECTED_PORTS = json.load(f)
        except Exception as e:
            print("Error loading selected ports:", e)

def auto_connect_to_saved_ports():
    """
    Check if any previously saved ports are available and auto-connect to them.
    Returns True if at least one port was connected, False otherwise.
    """
    global SELECTED_PORTS
    
    if not SELECTED_PORTS:
        logger.info("No saved ports found for auto-connection")
        return False
    
    # Get currently available ports
    available_ports = {p.device for p in serial.tools.list_ports.comports()}
    logger.debug(f"Available ports: {available_ports}")
    
    # Check which saved ports are still available
    available_saved_ports = {}
    for port_key, port_device in SELECTED_PORTS.items():
        if port_device in available_ports:
            available_saved_ports[port_key] = port_device
    
    if not available_saved_ports:
        logger.warning("No previously used ports are currently available")
        return False
    
    logger.info(f"Auto-connecting to previously used ports: {list(available_saved_ports.values())}")
    
    # Update SELECTED_PORTS to only include available ports
    SELECTED_PORTS = available_saved_ports
    
    # Start serial threads for available ports
    for port in SELECTED_PORTS.values():
        serial_connected_status[port] = False
        start_serial_thread(port)
        logger.info(f"Started serial thread for port: {port}")
    
    # Send watchdog reset to each microcontroller over USB
    time.sleep(2)  # Give threads time to establish connections
    with serial_objs_lock:
        for port, ser in serial_objs.items():
            try:
                if ser and ser.is_open:
                    ser.write(b'WATCHDOG_RESET\n')
                    logger.debug(f"Sent watchdog reset to {port}")
            except Exception as e:
                logger.error(f"Failed to send watchdog reset to {port}: {e}")
    
    return True

# ----------------------
# Enhanced Port Monitoring
# ----------------------
def monitor_ports():
    """
    Continuously monitor for port availability changes and auto-connect when possible.
    This runs in a separate thread for headless operation.
    """
    logger.info("Starting port monitoring thread...")
    last_available_ports = set()
    
    while not SHUTDOWN_EVENT.is_set():
        try:
            # Get currently available ports
            current_ports = {p.device for p in serial.tools.list_ports.comports()}
            
            # Check if port availability has changed
            if current_ports != last_available_ports:
                logger.info(f"Port availability changed. Current ports: {current_ports}")
                
                # If we have saved ports but no active connections, try to auto-connect
                if SELECTED_PORTS and not any(serial_connected_status.values()):
                    logger.info("Attempting auto-connection to saved ports...")
                    if auto_connect_to_saved_ports():
                        logger.info("Auto-connection successful! Mapping is now active.")
                    else:
                        logger.info("Auto-connection failed. Waiting for ports...")
                
                # Check for disconnected ports
                for port in list(serial_connected_status.keys()):
                    if port not in current_ports and serial_connected_status.get(port, False):
                        logger.warning(f"Port {port} disconnected")
                        serial_connected_status[port] = False
                        
                        # Broadcast the updated status immediately
                        emit_serial_status()
                        
                        with serial_objs_lock:
                            if port in serial_objs:
                                try:
                                    serial_objs[port].close()
                                except:
                                    pass
                                del serial_objs[port]
                
                last_available_ports = current_ports.copy()
            
            # Wait before next check
            SHUTDOWN_EVENT.wait(PORT_MONITOR_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error in port monitoring: {e}")
            SHUTDOWN_EVENT.wait(5)  # Wait 5 seconds before retrying

def start_port_monitoring():
    """Start the port monitoring thread"""
    if AUTO_START_ENABLED:
        monitor_thread = threading.Thread(target=monitor_ports, daemon=True)
        monitor_thread.start()
        logger.info("Port monitoring thread started")

# ----------------------
# Enhanced Status Reporting
# ----------------------
def log_system_status():
    """Log current system status for headless monitoring"""
    logger.info("=== SYSTEM STATUS ===")
    logger.info(f"Selected ports: {SELECTED_PORTS}")
    logger.info(f"Serial connection status: {serial_connected_status}")
    logger.info(f"Active detections: {len(detection_history)}")
    logger.info(f"Tracked MACs: {len(set(d.get('mac') for d in detection_history if d.get('mac')))}")
    logger.info(f"Headless mode: {HEADLESS_MODE}")
    logger.info("====================")

def start_status_logging():
    """Start periodic status logging for headless operation"""
    def status_logger():
        while not SHUTDOWN_EVENT.is_set():
            log_system_status()
            SHUTDOWN_EVENT.wait(300)  # Log status every 5 minutes
    
    if HEADLESS_MODE:
        status_thread = threading.Thread(target=status_logger, daemon=True)
        status_thread.start()
        logger.info("Status logging thread started")

def start_websocket_broadcaster():
    """Start background task to broadcast WebSocket updates every 5 seconds (optimized)"""
    def broadcaster():
        while not SHUTDOWN_EVENT.is_set():
            try:
                # Only emit if there are connected clients to reduce CPU usage
                if hasattr(socketio, 'server') and hasattr(socketio.server, 'manager'):
                    # Emit critical data more frequently
                    emit_detections()
                    emit_serial_status()
                    
                    # Emit less critical data less frequently
                    if int(time.time()) % 10 == 0:  # Every 10 seconds
                        emit_paths()
                        emit_aliases()
                    
                    if int(time.time()) % 30 == 0:  # Every 30 seconds
                        emit_cumulative_log()
                        emit_faa_cache()
            except Exception as e:
                # Ignore errors if no clients connected
                pass
            
            # Wait 5 seconds instead of 2 to reduce CPU usage
            for _ in range(50):  # 50 * 0.1 = 5 seconds, but check shutdown every 0.1s
                if SHUTDOWN_EVENT.is_set():
                    break
                time.sleep(0.1)
    
    
    broadcaster_thread = threading.Thread(target=broadcaster, daemon=True)
    broadcaster_thread.start()
    logger.info("WebSocket broadcaster thread started")

# ----------------------
# FAA Cache Persistence
# ----------------------
FAA_CACHE_FILENAME = os.path.join(BASE_DIR, "faa_cache.csv")
FAA_CACHE = {}

# Load FAA cache from disk if it exists
if os.path.exists(FAA_CACHE_FILENAME):
    try:
        with open(FAA_CACHE_FILENAME, newline='') as csvfile:
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
        file_exists = os.path.isfile(FAA_CACHE_FILENAME)
        with open(FAA_CACHE_FILENAME, "a", newline='') as csvfile:
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

    # Use consistent color generation function
    mac_colors = {}
    for mac in macs:
        mac_colors[mac] = get_color_for_mac(mac)

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
                        # drone start icon
                        start_lon, start_lat, start_ts = current_flight[0]
                        kml_lines.append(f'<Placemark><name>Drone Start {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/airports.png</href></IconStyle></Style><Point><coordinates>{start_lon},{start_lat},0</coordinates></Point></Placemark>')
                        # drone end icon
                        end_lon, end_lat, end_ts = current_flight[-1]
                        kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></IconStyle></Style><Point><coordinates>{end_lon},{end_lat},0</coordinates></Point></Placemark>')
                        # pilot path inside same flight
                        start_ts = current_flight[0][2]
                        pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in detection_history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and d.get('last_update')>=start_ts and d.get('last_update')<=end_ts]
                        if len(pilot_pts) >= 1:
                            pc = " ".join(f"{p[0]},{p[1]},0" for p in pilot_pts)
                            kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                            plon, plat = pilot_pts[-1]
                            kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
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
            # drone start icon
            start_lon, start_lat, start_ts = current_flight[0]
            kml_lines.append(f'<Placemark><name>Drone Start {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/airports.png</href></IconStyle></Style><Point><coordinates>{start_lon},{start_lat},0</coordinates></Point></Placemark>')
            end_lon, end_lat, end_ts = current_flight[-1]
            kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></IconStyle></Style><Point><coordinates>{end_lon},{end_lat},0</coordinates></Point></Placemark>')
            pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in detection_history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and d.get('last_update')>=current_flight[0][2] and d.get('last_update')<=end_ts]
            if pilot_pts:
                pc = " ".join(f"{p[0]},{p[1]},0" for p in pilot_pts)
                kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                plon, plat = pilot_pts[-1]
                kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
            kml_lines.append('</Folder>')
    # Close document
    kml_lines.append('</Document></kml>')

    # Write only session KML
    with open(KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated session KML:", KML_FILENAME)

def generate_kml_throttled():
    """Only regenerate KML if enough time has passed"""
    global last_kml_generation
    current_time = time.time()
    
    if current_time - last_kml_generation > KML_GENERATION_INTERVAL:
        generate_kml()
        last_kml_generation = current_time

def generate_cumulative_kml_throttled():
    """Only regenerate cumulative KML if enough time has passed"""
    global last_cumulative_kml_generation
    current_time = time.time()
    
    if current_time - last_cumulative_kml_generation > KML_GENERATION_INTERVAL:
        generate_cumulative_kml()
        last_cumulative_kml_generation = current_time

# New generate_cumulative_kml function
def generate_cumulative_kml():
    """
    Build cumulative KML by reading the cumulative CSV and grouping detections into flights.
    """
    # Check if cumulative CSV exists
    if not os.path.exists(CUMULATIVE_CSV_FILENAME):
        print(f"Warning: Cumulative CSV file {CUMULATIVE_CSV_FILENAME} does not exist yet.")
        return
    
    # Read cumulative CSV history
    history = []
    try:
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
    except Exception as e:
        print(f"Error reading cumulative CSV: {e}")
        return

    # Determine unique MACs and assign consistent colors
    macs = sorted({d['mac'] for d in history})
    mac_colors = {}
    for mac in macs:
        mac_colors[mac] = get_color_for_mac(mac)

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
                        # drone start icon
                        start_lo, start_la, start_ts = current_flight[0]
                        kml_lines.append(f'<Placemark><name>Drone Start {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/airports.png</href></IconStyle></Style><Point><coordinates>{start_lo},{start_la},0</coordinates></Point></Placemark>')
                        # drone end icon
                        end_lo, end_la, end_ts = current_flight[-1]
                        kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></IconStyle></Style><Point><coordinates>{end_lo},{end_la},0</coordinates></Point></Placemark>')
                        # pilot path
                        start_ts = current_flight[0][2]
                        pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and start_ts <= d['last_update'] <= end_ts]
                        if pilot_pts:
                            pc = " ".join(f"{plo},{pla},0" for plo, pla in pilot_pts)
                            kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                            plon, plat = pilot_pts[-1]
                            kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
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
            # drone start icon
            start_lo, start_la, start_ts = current_flight[0]
            kml_lines.append(f'<Placemark><name>Drone Start {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/airports.png</href></IconStyle></Style><Point><coordinates>{start_lo},{start_la},0</coordinates></Point></Placemark>')
            end_lo, end_la, end_ts = current_flight[-1]
            kml_lines.append(f'<Placemark><name>Drone End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/heliport.png</href></IconStyle></Style><Point><coordinates>{end_lo},{end_la},0</coordinates></Point></Placemark>')
            start_ts = current_flight[0][2]
            pilot_pts = [(d['pilot_long'], d['pilot_lat']) for d in history if d.get('mac')==mac and d.get('pilot_lat') and d.get('pilot_long') and start_ts <= d['last_update'] <= end_ts]
            if pilot_pts:
                pc = " ".join(f"{plo},{pla},0" for plo, pla in pilot_pts)
                kml_lines.append(f'<Placemark><name>Pilot Path {flight_idx} {aliasStr}{mac}</name><Style><LineStyle><color>{color}</color><width>2</width><gx:dash/></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>{pc}</coordinates></LineString></Placemark>')
                plon, plat = pilot_pts[-1]
                kml_lines.append(f'<Placemark><name>Pilot End {flight_idx} {aliasStr}{mac}</name><Style><IconStyle><color>{color}</color><scale>1.2</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/man.png</href></IconStyle></Style><Point><coordinates>{plon},{plat},0</coordinates></Point></Placemark>')
            kml_lines.append('</Folder>')

    # Close document
    kml_lines.append('</Document></kml>')

    # Write cumulative KML
    with open(CUMULATIVE_KML_FILENAME, "w") as f:
        f.write("\n".join(kml_lines))
    print("Updated cumulative KML:", CUMULATIVE_KML_FILENAME)


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
        print(f"No-GPS detection for {mac}; forwarding for processing.")
        # Set last_update for no-GPS detections so they can be tracked for timeout
        detection["last_update"] = time.time()
        # Mark as active since this is a fresh detection
        detection["status"] = "active"
        
        # Preserve previous basic_id if new detection lacks one (same logic as GPS section)
        if not detection.get("basic_id") and mac in tracked_pairs and tracked_pairs[mac].get("basic_id"):
            detection["basic_id"] = tracked_pairs[mac]["basic_id"]
        
        # Comprehensive FAA data persistence logic for no-GPS detections
        remote_id = detection.get("basic_id")
        if mac:
            # Exact match if basic_id provided
            if remote_id:
                key = (mac, remote_id)
                if key in FAA_CACHE:
                    detection["faa_data"] = FAA_CACHE[key]
            # Fallback: any cached FAA data for this mac (regardless of basic_id)
            if "faa_data" not in detection:
                for (c_mac, _), faa_data in FAA_CACHE.items():
                    if c_mac == mac:
                        detection["faa_data"] = faa_data
                        break
            # Fallback: last known FAA data in tracked_pairs
            if "faa_data" not in detection and mac in tracked_pairs and "faa_data" in tracked_pairs[mac]:
                detection["faa_data"] = tracked_pairs[mac]["faa_data"]
            # Always cache FAA data by MAC and current basic_id for future lookups
            if "faa_data" in detection:
                write_to_faa_cache(mac, detection.get("basic_id", ""), detection["faa_data"])
        
        # Forward this no-GPS detection to the client
        tracked_pairs[mac] = detection
        detection_history.append(detection.copy())
        
        # Backend webhook logic for all detections (GPS and no-GPS) - enabled
        should_trigger, is_new = should_trigger_webhook_earliest(detection, mac)
        if should_trigger:
            trigger_backend_webhook_earliest(detection, is_new)
        
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
        generate_cumulative_kml_throttled()
        generate_kml_throttled()
        
        # Reduce WebSocket emissions - only emit detection, not all data types
        try:
            socketio.emit('detection', detection, )
        except Exception:
            pass
        
        # Cache FAA data even for no-GPS
        if detection.get('basic_id'):
            write_to_faa_cache(mac, detection['basic_id'], detection.get('faa_data', {}))
        return

    # Otherwise, use the provided non-zero coordinates.
    detection["drone_lat"] = new_drone_lat
    detection["drone_long"] = new_drone_long
    detection["drone_altitude"] = detection.get("drone_altitude", 0)
    detection["pilot_lat"] = detection.get("pilot_lat", 0)
    detection["pilot_long"] = detection.get("pilot_long", 0)
    detection["last_update"] = time.time()
    # Mark as active since this is a fresh detection
    detection["status"] = "active"

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
    
    # Backend webhook logic for GPS detections - enabled
    should_trigger, is_new = should_trigger_webhook_earliest(detection, mac)
    if should_trigger:
        trigger_backend_webhook_earliest(detection, is_new)
    
    # Broadcast this detection to all connected clients and peer servers
    try:
        socketio.emit('detection', detection, )
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
    generate_cumulative_kml_throttled()
    generate_kml_throttled()
    
    # Emit real-time updates via WebSocket (if available in this context)
    try:
        emit_detections()
        emit_paths()
        emit_cumulative_log()
        emit_faa_cache()
    except NameError:
        # Emit functions not available in this thread context
        pass
    except Exception as e:
        # Handle JSON serialization errors gracefully
        logger.debug(f"WebSocket emit error: {e}")
        pass

# ----------------------
# Global Follow Lock & Color Overrides
# ----------------------
followLock = {"type": None, "id": None, "enabled": False}
colorOverrides = {}

# Backend webhook tracking variables
backend_seen_drones = set()
backend_previous_active = {}
backend_alerted_no_gps = set()

# ----------------------
# Webhook Functions (EARLY DEFINITION - must be before update_detection)
# ----------------------

def should_trigger_webhook_earliest(detection, mac):
    """
    Determine if a webhook should be triggered based on the same logic as frontend popups.
    Returns (should_trigger, is_new_detection)
    """
    global backend_seen_drones, backend_previous_active, backend_alerted_no_gps
    
    current_time = time.time()
    
    # Debug logging
    logging.debug(f"Webhook check for {mac}: detection={detection}")
    logging.debug(f"Webhook check: current_time={current_time}, last_update={detection.get('last_update')}")
    
    # Check if detection is within stale threshold (30 seconds)
    if not detection.get('last_update') or (current_time - detection['last_update'] > 30):
        logging.debug(f"Webhook check for {mac}: FAILED stale check - last_update={detection.get('last_update')}")
        return False, False
    
    # GPS drone logic
    drone_lat = detection.get('drone_lat', 0)
    drone_long = detection.get('drone_long', 0)
    pilot_lat = detection.get('pilot_lat', 0) 
    pilot_long = detection.get('pilot_long', 0)
    
    valid_drone = (drone_lat != 0 and drone_long != 0)
    has_gps = valid_drone or (pilot_lat != 0 and pilot_long != 0)
    has_recent_transmission = detection.get('last_update') and (current_time - detection['last_update'] <= 5)
    is_no_gps_drone = not has_gps and has_recent_transmission
    
    # Calculate state
    active_now = valid_drone and detection.get('last_update') and (current_time - detection['last_update'] <= 30)
    was_active = backend_previous_active.get(mac, False)
    is_new = mac not in backend_seen_drones
    
    logging.debug(f"Webhook check for {mac}: valid_drone={valid_drone}, active_now={active_now}, was_active={was_active}, is_new={is_new}")
    
    should_trigger = False
    popup_is_new = False
    
    # GPS drone webhook logic - trigger on transition from inactive to active
    if not was_active and active_now:
        should_trigger = True
        alias = ALIASES.get(mac)
        popup_is_new = not alias and is_new
        logging.info(f"Webhook trigger for {mac}: GPS drone transition to active")
    
    # No-GPS drone webhook logic - trigger once per detection session
    elif is_no_gps_drone and mac not in backend_alerted_no_gps:
        should_trigger = True
        popup_is_new = True
        backend_alerted_no_gps.add(mac)
        logging.info(f"Webhook trigger for {mac}: No-GPS drone detected")
    
    logging.debug(f"Webhook check for {mac}: should_trigger={should_trigger}, popup_is_new={popup_is_new}")
    
    # Update tracking state
    if should_trigger:
        backend_seen_drones.add(mac)
    backend_previous_active[mac] = active_now
    
    # Clean up no-GPS alerts when transmission stops
    if not has_recent_transmission:
        backend_alerted_no_gps.discard(mac)
    
    return should_trigger, popup_is_new

def trigger_backend_webhook_earliest(detection, is_new_detection):
    """
    Send webhook with same payload format as frontend popups
    """
    logging.info(f"Backend webhook called for {detection.get('mac')} - WEBHOOK_URL: {WEBHOOK_URL}")
    
    if not WEBHOOK_URL or not WEBHOOK_URL.startswith("http"):
        logging.warning(f"Backend webhook skipped - invalid URL: {WEBHOOK_URL}")
        return
    
    try:
        mac = detection.get('mac')
        alias = ALIASES.get(mac) if mac else None
        
        # Determine header message (same logic as frontend)
        if not detection.get('drone_lat') or not detection.get('drone_long') or detection.get('drone_lat') == 0 or detection.get('drone_long') == 0:
            header = 'Drone with no GPS lock detected'
        elif alias:
            header = f'Known drone detected – {alias}'
        else:
            header = 'New drone detected' if is_new_detection else 'Previously seen non-aliased drone detected'
        
        logging.info(f"Backend webhook for {mac}: {header}")
        
        # Build payload (same format as frontend)
        payload = {
            'alert': header,
            'mac': mac,
            'basic_id': detection.get('basic_id'),
            'alias': alias,
            'drone_lat': detection.get('drone_lat') if detection.get('drone_lat') != 0 else None,
            'drone_long': detection.get('drone_long') if detection.get('drone_long') != 0 else None,
            'pilot_lat': detection.get('pilot_lat') if detection.get('pilot_lat') != 0 else None,
            'pilot_long': detection.get('pilot_long') if detection.get('pilot_long') != 0 else None,
            'faa_data': None,  # Will be populated below
            'drone_gmap': None,
            'pilot_gmap': None,
            'isNew': is_new_detection
        }
        
        # Add FAA data if available
        faa_data = detection.get('faa_data')
        if faa_data and isinstance(faa_data, dict) and faa_data.get('data') and isinstance(faa_data['data'].get('items'), list) and len(faa_data['data']['items']) > 0:
            payload['faa_data'] = faa_data['data']['items'][0]
        
        # Add Google Maps links
        if payload['drone_lat'] and payload['drone_long']:
            payload['drone_gmap'] = f"https://www.google.com/maps?q={payload['drone_lat']},{payload['drone_long']}"
        if payload['pilot_lat'] and payload['pilot_long']:
            payload['pilot_gmap'] = f"https://www.google.com/maps?q={payload['pilot_lat']},{payload['pilot_long']}"
        
        # Send webhook
        logging.info(f"Sending webhook to {WEBHOOK_URL} with payload: {payload}")
        response = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        logging.info(f"Backend webhook sent for {mac}: {response.status_code}")
        
    except requests.exceptions.Timeout:
        logging.error(f"Backend webhook timeout for {detection.get('mac', 'unknown')}: URL {WEBHOOK_URL} timed out after 10 seconds")
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Backend webhook connection error for {detection.get('mac', 'unknown')}: Unable to reach {WEBHOOK_URL} - {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Backend webhook request error for {detection.get('mac', 'unknown')}: {e}")
    except Exception as e:
        logging.error(f"Backend webhook error for {detection.get('mac', 'unknown')}: {e}")


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
        response = requests.post(webhook_url, json=clean_data, timeout=10)
        return jsonify({"status": "ok", "response": response.status_code}), 200
    except requests.exceptions.Timeout:
        logging.error(f"Webhook timeout for URL: {webhook_url}")
        return jsonify({"status": "error", "message": "Webhook request timed out after 10 seconds"}), 408
    except requests.exceptions.ConnectionError as e:
        logging.error(f"Webhook connection error for URL {webhook_url}: {e}")
        return jsonify({"status": "error", "message": f"Connection error: Unable to reach webhook URL"}), 503
    except requests.exceptions.RequestException as e:
        logging.error(f"Webhook request error for URL {webhook_url}: {e}")
        return jsonify({"status": "error", "message": f"Request error: {str(e)}"}), 500
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
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mesh Mapper - Settings</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Orbitron:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #080810;
      --bg-card: #0d0d18;
      --bg-input: #12121f;
      --border-primary: #00ff88;
      --border-secondary: #6366f1;
      --border-accent: #f0abfc;
      --text-primary: #e0e0e0;
      --text-cyan: #00ffd5;
      --text-green: #00ff88;
      --text-pink: #f0abfc;
      --text-muted: #6b7280;
      --glow-green: rgba(0, 255, 136, 0.15);
      --glow-pink: rgba(240, 171, 252, 0.15);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      padding: 20px;
      min-height: 100vh;
      font-family: 'JetBrains Mono', monospace;
      background: var(--bg-dark);
      background-image: 
        radial-gradient(ellipse at top, rgba(99, 102, 241, 0.08) 0%, transparent 50%),
        radial-gradient(ellipse at bottom, rgba(0, 255, 136, 0.05) 0%, transparent 50%);
      color: var(--text-primary);
      display: flex;
      flex-direction: column;
      align-items: center;
    }
    .container {
      width: 100%;
      max-width: 480px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      overflow: visible;
    }
    .logo-section {
      text-align: center;
      margin-bottom: 8px;
      overflow: visible;
      display: flex;
      justify-content: center;
    }
    pre.ascii-art {
      margin: 0;
      padding: 8px;
      font-size: clamp(6px, 1.8vw, 10px);
      line-height: 1.1;
      background: linear-gradient(135deg, #6366f1, #00ff88, #f0abfc);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      font-family: 'JetBrains Mono', monospace;
      white-space: pre;
      overflow: visible;
      text-align: center;
    }
    .bottom-art {
      margin-top: 16px;
      display: flex;
      justify-content: center;
    }
    h1 {
      font-family: 'Orbitron', sans-serif;
      font-size: 1.1rem;
      font-weight: 600;
      color: var(--text-cyan);
      margin: 0;
      letter-spacing: 1px;
      text-transform: uppercase;
    }
    .card {
      background: var(--bg-card);
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 8px;
      padding: 16px;
    }
    .card-header {
      font-family: 'Orbitron', sans-serif;
      font-size: 0.75rem;
      font-weight: 500;
      color: var(--text-pink);
      text-transform: uppercase;
      letter-spacing: 1.5px;
      margin-bottom: 12px;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(240, 171, 252, 0.2);
    }
    .port-group {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .port-item {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .port-item label {
      font-size: 0.8rem;
      color: var(--text-green);
      min-width: 50px;
      font-weight: 500;
    }
    select {
      flex: 1;
      background: var(--bg-input);
      color: var(--text-cyan);
      border: 1px solid rgba(0, 255, 136, 0.3);
      border-radius: 4px;
      padding: 8px 10px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    select:hover, select:focus {
      border-color: var(--border-primary);
      outline: none;
      box-shadow: 0 0 0 2px var(--glow-green);
    }
    select option {
      background: var(--bg-input);
      color: var(--text-cyan);
    }
    .input-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .input-group label {
      font-size: 0.75rem;
      color: var(--text-pink);
      font-weight: 500;
    }
    input[type="text"], input[type="file"] {
      background: var(--bg-input);
      color: var(--text-cyan);
      border: 1px solid rgba(240, 171, 252, 0.3);
      border-radius: 4px;
      padding: 10px 12px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.8rem;
      width: 100%;
      transition: all 0.2s ease;
    }
    input[type="text"]:hover, input[type="text"]:focus {
      border-color: var(--border-accent);
      outline: none;
      box-shadow: 0 0 0 2px var(--glow-pink);
    }
    input[type="text"]::placeholder {
      color: var(--text-muted);
    }
    input[type="file"] {
      font-size: 0.7rem;
      cursor: pointer;
    }
    input[type="file"]::file-selector-button {
      background: rgba(240, 171, 252, 0.15);
      color: var(--text-pink);
      border: 1px solid rgba(240, 171, 252, 0.4);
      border-radius: 3px;
      padding: 4px 10px;
      margin-right: 10px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.7rem;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    input[type="file"]::file-selector-button:hover {
      background: rgba(240, 171, 252, 0.25);
    }
    .btn {
      font-family: 'Orbitron', sans-serif;
      font-size: 0.75rem;
      font-weight: 500;
      padding: 10px 16px;
      border-radius: 4px;
      cursor: pointer;
      transition: all 0.2s ease;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .btn-secondary {
      background: transparent;
      color: var(--text-pink);
      border: 1px solid rgba(240, 171, 252, 0.4);
    }
    .btn-secondary:hover {
      background: rgba(240, 171, 252, 0.1);
      border-color: var(--border-accent);
    }
    .btn-primary {
      background: linear-gradient(135deg, rgba(0, 255, 136, 0.15), rgba(0, 255, 213, 0.15));
      color: var(--text-green);
      border: 1px solid var(--border-primary);
      width: 100%;
    }
    .btn-primary:hover {
      background: linear-gradient(135deg, rgba(0, 255, 136, 0.25), rgba(0, 255, 213, 0.25));
      box-shadow: 0 0 20px var(--glow-green), 0 0 40px rgba(0, 255, 136, 0.1);
    }
    .btn-upload {
      background: transparent;
      color: var(--text-cyan);
      border: 1px solid rgba(0, 255, 213, 0.4);
      width: 100%;
    }
    .btn-upload:hover {
      background: rgba(0, 255, 213, 0.1);
      border-color: var(--text-cyan);
    }
    .status-text {
      font-size: 0.75rem;
      color: var(--text-cyan);
      margin-top: 6px;
      min-height: 18px;
    }
    .button-row {
      display: flex;
      gap: 8px;
      margin-top: 8px;
    }
    .button-row .btn {
      flex: 1;
    }
    form {
      display: contents;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="logo-section">
      <pre class="ascii-art">{{ logo_ascii }}</pre>
    </div>
    
    <h1>Device Configuration</h1>
    
  <form method="POST" action="/select_ports">
      <div class="card">
        <div class="card-header">Serial Ports</div>
        <div class="port-group">
          <div class="port-item">
            <label>Port 1</label>
    <select id="port1" name="port1">
              <option value="">-- None --</option>
      {% for port in ports %}
        <option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>
      {% endfor %}
            </select>
          </div>
          <div class="port-item">
            <label>Port 2</label>
    <select id="port2" name="port2">
              <option value="">-- None --</option>
      {% for port in ports %}
        <option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>
      {% endfor %}
            </select>
          </div>
          <div class="port-item">
            <label>Port 3</label>
    <select id="port3" name="port3">
              <option value="">-- None --</option>
      {% for port in ports %}
        <option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>
      {% endfor %}
            </select>
    </div>
      </div>
    </div>

      <div class="card">
        <div class="card-header">Webhook</div>
        <div class="input-group">
          <label for="webhookUrl">Endpoint URL</label>
          <input type="text" id="webhookUrl" placeholder="https://example.com/webhook">
        </div>
        <div class="button-row">
          <button type="button" id="updateWebhookButton" class="btn btn-secondary">Update Webhook</button>
        </div>
      </div>

      <div class="card">
        <div class="card-header">Aliases</div>
        <div class="input-group">
          <label for="aliasesFileInput">Upload Aliases JSON (Optional)</label>
          <input type="file" id="aliasesFileInput" accept=".json">
        </div>
        <button type="button" id="uploadAliasesButton" class="btn btn-upload">Upload to ESP32-S3</button>
        <div id="uploadAliasesStatus" class="status-text"></div>
      </div>

      <button id="beginMapping" type="submit" class="btn btn-primary">Begin Mapping</button>
  </form>
    
    <div class="bottom-art">
  <pre class="ascii-art">{{ bottom_ascii }}</pre>
    </div>
  </div>
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

    function loadSelectedPorts() {
      fetch('/api/selected_ports')
        .then(res => res.json())
        .then(data => {
          const selectedPorts = data.selected_ports || {};
          // Populate dropdowns with currently selected ports
          ['port1', 'port2', 'port3'].forEach(name => {
            const select = document.getElementById(name);
            if (select && selectedPorts[name]) {
              select.value = selectedPorts[name];
            }
          });
        })
        .catch(err => console.error('Error loading selected ports:', err));
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
      // Load currently selected ports after refreshing port options
      setTimeout(loadSelectedPorts, 100);
    }
    const webhookInput = document.getElementById('webhookUrl');
    
    // Load current webhook URL from backend on page load
    loadCurrentWebhookUrl();
    
    async function loadCurrentWebhookUrl() {
      try {
        const response = await fetch('/api/get_webhook_url');
        const result = await response.json();
        console.log('Webhook URL load result:', result);
        if (result.status === 'ok') {
          document.getElementById('webhookUrl').value = result.webhook_url || '';
          console.log('Webhook URL loaded:', result.webhook_url || '(empty)');
        } else {
          console.warn('Failed to load webhook URL:', result.message);
        }
      } catch (e) {
        console.warn('Could not load webhook URL:', e);
      }
    }
    
    document.getElementById('updateWebhookButton').addEventListener('click', async function(e) {
      e.preventDefault();
      const url = document.getElementById('webhookUrl').value.trim();
      const button = this;
      
      try {
        // Send webhook URL update via API
        const response = await fetch('/api/set_webhook_url', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ webhook_url: url })
        });
        
        const result = await response.json();
        
        if (result.status === 'ok') {
          // Flash purple to indicate success
          const originalStyle = button.style.cssText;
          button.style.backgroundColor = '#9B30FF';
          button.style.borderColor = '#9B30FF';
          button.style.color = 'white';
          button.style.textShadow = '0 0 8px #9B30FF';
          
          // Also update the hidden input for when Begin Mapping is clicked
          let webhookInput = document.getElementById('hiddenWebhookUrl');
          if (!webhookInput) {
            webhookInput = document.createElement('input');
            webhookInput.type = 'hidden';
            webhookInput.id = 'hiddenWebhookUrl';
            webhookInput.name = 'webhook_url';
            document.querySelector('form').appendChild(webhookInput);
          }
          webhookInput.value = url;
          
          // Reset button style after flash
          setTimeout(() => {
            button.style.cssText = originalStyle;
          }, 300);
          
        } else {
          console.error('Error updating webhook:', result.message);
          // Flash red for error
          const originalStyle = button.style.cssText;
          button.style.backgroundColor = '#ff0000';
          button.style.borderColor = '#ff0000';
          button.style.color = 'white';
          
          setTimeout(() => {
            button.style.cssText = originalStyle;
          }, 300);
        }
      } catch (error) {
        console.error('Error updating webhook:', error);
        // Flash red for error
        const originalStyle = button.style.cssText;
        button.style.backgroundColor = '#ff0000';
        button.style.borderColor = '#ff0000';
        button.style.color = 'white';
        
        setTimeout(() => {
          button.style.cssText = originalStyle;
        }, 300);
      }
    });

    // Upload aliases to ESP32-S3 (handles file upload if file is selected)
    document.getElementById('uploadAliasesButton').addEventListener('click', async function(e) {
      e.preventDefault();
      const fileInput = document.getElementById('aliasesFileInput');
      const button = this;
      const statusDiv = document.getElementById('uploadAliasesStatus');
      const originalText = button.textContent;
      const originalStyle = button.style.cssText;
      
      button.disabled = true;
      button.textContent = 'Uploading...';
      statusDiv.textContent = '';
      
      try {
        // Check if a file is selected
        let aliasesToUpload = null;
        if (fileInput.files && fileInput.files.length > 0) {
          const file = fileInput.files[0];
          if (!file.name.endsWith('.json')) {
            statusDiv.textContent = 'Error: Please select a JSON file';
            statusDiv.style.color = '#ff0000';
            button.disabled = false;
            button.textContent = originalText;
            setTimeout(() => {
              statusDiv.textContent = '';
            }, 3000);
            return;
          }
          
          try {
            const fileContent = await file.text();
            aliasesToUpload = JSON.parse(fileContent);
            if (!isObject(aliasesToUpload)) {
              throw new Error('Aliases must be a JSON object');
            }
          } catch (error) {
            statusDiv.textContent = `Error: Invalid JSON file - ${error.message}`;
            statusDiv.style.color = '#ff0000';
            button.disabled = false;
            button.textContent = originalText;
            setTimeout(() => {
              statusDiv.textContent = '';
            }, 3000);
            return;
          }
        }
        
        const response = await fetch('/api/upload_aliases', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ aliases: aliasesToUpload })
        });
        
        const result = await response.json();
        
        if (result.status === 'ok') {
          button.style.backgroundColor = '#00ff00';
          button.style.borderColor = '#00ff00';
          button.style.color = 'black';
          statusDiv.textContent = `Success: ${result.message}`;
          statusDiv.style.color = '#00ff00';
          
          // Clear file input if file was uploaded
          if (fileInput.files && fileInput.files.length > 0) {
            fileInput.value = '';
          }
          
          setTimeout(() => {
            button.style.cssText = originalStyle;
            button.textContent = originalText;
            statusDiv.textContent = '';
          }, 2000);
        } else {
          button.style.backgroundColor = '#ff0000';
          button.style.borderColor = '#ff0000';
          button.style.color = 'white';
          statusDiv.textContent = `Error: ${result.message}`;
          statusDiv.style.color = '#ff0000';
          
          setTimeout(() => {
            button.style.cssText = originalStyle;
            button.textContent = originalText;
            statusDiv.textContent = '';
          }, 3000);
        }
      } catch (error) {
        console.error('Error uploading aliases:', error);
        button.style.backgroundColor = '#ff0000';
        button.style.borderColor = '#ff0000';
        button.style.color = 'white';
        statusDiv.textContent = `Error: ${error.message}`;
        statusDiv.style.color = '#ff0000';
        
        setTimeout(() => {
          button.style.cssText = originalStyle;
          button.textContent = originalText;
          statusDiv.textContent = '';
        }, 3000);
      } finally {
        button.disabled = false;
      }
    });
    
    function isObject(value) {
      return value !== null && typeof value === 'object' && !Array.isArray(value);
    }

    // Ensure webhook URL is included when Begin Mapping form is submitted
    document.getElementById('beginMapping').addEventListener('click', function(e) {
      const url = document.getElementById('webhookUrl').value.trim();
      
      // Add webhook URL to the form as a hidden input
      const form = document.querySelector('form');
      let webhookInput = document.getElementById('hiddenWebhookUrl');
      if (!webhookInput) {
        webhookInput = document.createElement('input');
        webhookInput.type = 'hidden';
        webhookInput.id = 'hiddenWebhookUrl';
        webhookInput.name = 'webhook_url';
        form.appendChild(webhookInput);
      }
      webhookInput.value = url;
      
      // Let the form submit normally
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="preconnect" href="https://cdn.socket.io">
  <link rel="preconnect" href="https://unpkg.com">
  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin=""/>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Orbitron:wght@400;500;600;700&display=swap" rel="stylesheet" media="print" onload="this.media='all'">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
  <style>
    :root {
      --bg-dark: #080810;
      --bg-panel: rgba(8, 8, 16, 0.92);
      --bg-input: #12121f;
      --border-primary: #00ff88;
      --border-secondary: #6366f1;
      --border-accent: #f0abfc;
      --text-primary: #e0e0e0;
      --text-cyan: #00ffd5;
      --text-green: #00ff88;
      --text-pink: #f0abfc;
      --text-muted: #6b7280;
    }
    /* Hide tile seams on all map layers */
    .leaflet-tile {
      border: none !important;
      box-shadow: none !important;
      background-color: transparent !important;
      image-rendering: crisp-edges !important;
      transition: none !important;
    }
    .leaflet-container {
      background-color: #080810 !important;
    }
    /* Toggle switch styling */
    .switch { position: relative; display: inline-block; vertical-align: middle; width: 36px; height: 18px; }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #2d2d3d; transition: .2s; border-radius: 18px; }
    .slider:before {
      position: absolute;
      content: "";
      height: 14px;
      width: 14px;
      left: 2px;
      top: 50%;
      background-color: var(--text-green);
      border: 1px solid var(--border-secondary);
      transition: .2s;
      border-radius: 50%;
      transform: translateY(-50%);
    }
    .switch input:checked + .slider { background-color: rgba(0, 255, 136, 0.3); }
    .switch input:checked + .slider:before {
      transform: translateX(18px) translateY(-50%);
    }
    * { box-sizing: border-box; }
    body, html {
      margin: 0;
      padding: 0;
      background-color: var(--bg-dark);
      font-family: 'JetBrains Mono', monospace;
    }
    #map { height: 100vh; }
    
    /* Control Panel */
    #filterBox {
      position: absolute;
      top: 10px;
      right: 10px;
      background: var(--bg-panel);
      backdrop-filter: blur(12px);
      padding: 12px;
      width: 260px;
      max-width: 28vw;
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 8px;
      color: var(--text-primary);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.75rem;
      max-height: 90vh;
      overflow-y: auto;
      overflow-x: hidden;
      z-index: 1000;
    }
    #filterBox::-webkit-scrollbar { width: 4px; }
    #filterBox::-webkit-scrollbar-track { background: transparent; }
    #filterBox::-webkit-scrollbar-thumb { background: var(--border-secondary); border-radius: 2px; }
    
    /* Mobile responsive styles */
    @media (max-width: 768px) {
      #filterBox {
        width: 85vw;
        max-width: 320px;
        right: 8px;
        top: 10px;
        font-size: 0.7rem;
        max-height: 75vh;
      }
      .leaflet-popup {
        max-width: 280px !important;
      }
      .leaflet-popup-content-wrapper {
        max-width: 270px !important;
        padding: 6px !important;
        box-sizing: border-box !important;
      }
      .leaflet-popup-content {
        margin: 4px !important;
        font-size: 0.7rem !important;
        width: 250px !important;
        max-width: 250px !important;
        box-sizing: border-box !important;
        overflow: visible !important;
      }
      .leaflet-popup-content > div {
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
      }
      .leaflet-popup-content input[type="range"] {
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
        margin: 0 !important;
      }
      .leaflet-popup-content input[type="text"] {
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
      }
      .leaflet-popup-content select {
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
      }
      .leaflet-popup-content button {
        min-height: 32px !important;
        font-size: 0.65rem !important;
        padding: 6px 8px !important;
        touch-action: manipulation;
      }
      .leaflet-popup-content a {
        display: inline-block;
        padding: 2px 0;
      }
      #replayControlBar {
        width: 95vw !important;
        max-width: 360px !important;
        padding: 8px 12px !important;
        gap: 8px !important;
        flex-wrap: wrap;
        justify-content: center;
      }
      .placeholder {
        max-height: 120px;
      }
    }
    @media (max-width: 480px) {
      #filterBox {
        width: 88vw;
        max-width: 280px;
        right: 6px;
        top: 8px;
        font-size: 0.65rem;
        max-height: 70vh;
      }
      .drone-item {
        font-size: 0.6rem;
        padding: 3px 5px;
      }
      .leaflet-popup {
        max-width: 250px !important;
      }
      .leaflet-popup-content-wrapper {
        max-width: 240px !important;
        padding: 5px !important;
      }
      .leaflet-popup-content {
        font-size: 0.6rem !important;
        width: 220px !important;
        max-width: 220px !important;
        margin: 3px !important;
        overflow: visible !important;
      }
      .leaflet-popup-content > div {
        width: 100% !important;
        max-width: 100% !important;
        box-sizing: border-box !important;
      }
      .leaflet-popup-content button {
        min-height: 30px !important;
        font-size: 0.55rem !important;
        padding: 5px 6px !important;
        touch-action: manipulation;
      }
      .leaflet-popup-content input[type="range"] {
        width: 100% !important;
        height: 24px !important;
        margin: 4px 0 !important;
      }
      .leaflet-popup-content input[type="range"]::-webkit-slider-thumb {
        height: 20px !important;
        width: 20px !important;
      }
      .leaflet-popup-content input[type="range"]::-moz-range-thumb {
        height: 20px !important;
        width: 20px !important;
      }
      .leaflet-popup-content input[type="text"] {
        min-height: 32px !important;
        font-size: 16px !important; /* Prevents iOS zoom */
      }
      .leaflet-popup-content select {
        min-height: 32px !important;
        font-size: 16px !important; /* Prevents iOS zoom */
      }
    }
        #filterBox input[type="text"],
        #filterBox input[type="password"],
        #filterBox input[type="range"],
        #filterBox select {
      width: 100% !important;
          min-width: 0;
        }
    #filterBox.collapsed #filterContent { display: none; }
    #filterBox.collapsed {
      padding: 8px 12px;
      width: auto;
    }
    #filterBox.collapsed #filterHeader { padding: 0; }
    #filterBox.collapsed #filterHeader h3 {
      display: inline-block;
      flex: none;
      width: auto;
      margin: 0;
      color: var(--text-pink);
    }
    #filterBox.collapsed #filterHeader #filterToggle { margin-left: 8px; }
    #filterBox:not(.collapsed) #filterHeader h3 { display: none; }
    #filterHeader {
      display: flex;
      align-items: center;
    }
    #filterBox:not(.collapsed) #filterHeader { justify-content: flex-end; }
    #filterHeader h3 {
      flex: none;
      text-align: center;
      margin: 0;
      font-family: 'Orbitron', sans-serif;
      font-size: 0.8rem;
      font-weight: 500;
      display: block;
      width: 100%;
      color: var(--text-pink);
    }
    #filterToggle {
      color: var(--text-cyan);
      font-size: 1rem;
      cursor: pointer;
      padding: 2px 6px;
      border-radius: 4px;
      transition: background 0.2s;
    }
    #filterToggle:hover { background: rgba(0, 255, 213, 0.1); }
    
    /* Section Headers */
    .section-header {
      font-family: 'Orbitron', sans-serif;
      font-size: 0.65rem;
      font-weight: 500;
      color: var(--text-pink);
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 12px 0 8px 0;
      padding-bottom: 4px;
      border-bottom: 1px solid rgba(240, 171, 252, 0.2);
    }
    #filterContent > h3 {
      font-family: 'Orbitron', sans-serif;
      font-size: 0.65rem;
      font-weight: 500;
      color: var(--text-pink);
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 12px 0 8px 0;
      text-align: left;
    }
    #filterContent > h3::before, #filterContent > h3::after { display: none; }
    
    /* USB Status */
    #serialStatus { font-size: 0.7rem; }
    #serialStatus div { margin-bottom: 3px; padding: 4px 0; }
    #serialStatus div:last-child { margin-bottom: 0; }
    .usb-name { color: var(--text-pink); font-weight: 500; }
    
    /* Drone Items */
    .drone-item {
      display: inline-block;
      border: 1px solid;
      margin: 2px;
      padding: 4px 6px;
      cursor: pointer;
      border-radius: 3px;
      font-size: 0.7rem;
      transition: all 0.15s ease;
    }
    .drone-item:hover { background: rgba(255,255,255,0.05); }
    .drone-item.no-gps {
      position: relative;
      border: 1px solid #38bdf8 !important;
    }
    .drone-item.recent:not(.no-gps) {
      box-shadow: 0 0 0 1px var(--text-green), 0 0 8px rgba(0, 255, 136, 0.3);
    }
    .placeholder {
      background: rgba(0, 0, 0, 0.3);
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 6px;
      min-height: 80px;
      margin-top: 6px;
      padding: 6px;
      overflow-y: auto;
      max-height: 180px;
    }
    .placeholder::-webkit-scrollbar { width: 3px; }
    .placeholder::-webkit-scrollbar-track { background: transparent; }
    .placeholder::-webkit-scrollbar-thumb { background: var(--border-secondary); border-radius: 2px; }
    .selected { background-color: rgba(99, 102, 241, 0.2); }
    
    /* Leaflet Popup Styling - Clean, no scroll, mobile friendly */
    .leaflet-popup {
      max-width: 290px !important;
    }
    .leaflet-popup > .leaflet-popup-content-wrapper { 
      background: var(--bg-panel);
      backdrop-filter: blur(12px);
      color: var(--text-green); 
      font-family: 'JetBrains Mono', monospace; 
      border: 1px solid rgba(0, 255, 136, 0.4); 
      border-radius: 8px;
      padding: 8px;
      max-width: 280px;
      overflow: visible !important;
      box-sizing: border-box;
    }
    .leaflet-popup-content {
      font-size: 0.75rem;
      line-height: 1.35;
      white-space: normal;
      margin: 6px !important;
      width: 260px !important;
      max-width: 260px !important;
      overflow: visible !important;
      box-sizing: border-box;
      word-wrap: break-word;
      overflow-wrap: break-word;
    }
    .leaflet-popup-content > div {
      width: 100% !important;
      max-width: 100% !important;
      box-sizing: border-box !important;
    }
    .leaflet-popup-content input,
    .leaflet-popup-content select,
    .leaflet-popup-content button {
      box-sizing: border-box !important;
    }
    .leaflet-popup-tip {
      background: rgba(0, 255, 136, 0.4) !important; 
    }
    .leaflet-popup-close-button {
      color: var(--text-pink) !important;
      font-size: 20px !important;
      padding: 6px 10px !important;
      z-index: 1000;
      line-height: 1;
    }
    .leaflet-popup-close-button:hover {
      color: var(--text-green) !important;
    }
    .leaflet-popup.no-gps-popup > .leaflet-popup-content-wrapper {
      background: var(--bg-panel) !important;
      color: var(--text-green) !important;
    }
    .leaflet-popup.no-gps-popup .leaflet-popup-content {
      margin: 6px !important;
    }
    .leaflet-popup.no-gps-popup .leaflet-popup-tip-container,
    .leaflet-popup.no-gps-popup .leaflet-popup-tip {
      background: transparent !important;
      box-shadow: none !important;
    }
    
    /* Buttons */
    button {
      margin-top: 4px;
      padding: 6px 10px;
      font-size: 0.7rem;
      font-family: 'JetBrains Mono', monospace;
      border: 1px solid rgba(0, 255, 136, 0.4);
      background: rgba(0, 255, 136, 0.08);
      color: var(--text-green);
      cursor: pointer;
      border-radius: 4px;
      transition: all 0.15s ease;
    }
    button:hover {
      background: rgba(0, 255, 136, 0.15);
      border-color: var(--text-green);
    }
    select {
      background: var(--bg-input);
      color: var(--text-cyan);
      border: 1px solid rgba(99, 102, 241, 0.3);
      padding: 6px 8px;
      border-radius: 4px;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.7rem;
    }
    
    /* Zoom Controls */
    .leaflet-control-zoom.leaflet-bar {
      background: var(--bg-panel);
      border: 1px solid rgba(0, 255, 136, 0.3);
      border-radius: 6px;
      overflow: hidden;
    }
    .leaflet-control-zoom.leaflet-bar a {
      background: transparent;
      color: var(--text-green);
      border: none;
      border-bottom: 1px solid rgba(0, 255, 136, 0.2);
      width: 28px;
      height: 28px;
      line-height: 28px;
      text-align: center;
      font-size: 1rem;
      user-select: none;
      cursor: pointer;
      transition: background 0.15s;
    }
    .leaflet-control-zoom.leaflet-bar a:last-child { border-bottom: none; }
    .leaflet-control-zoom.leaflet-bar a:hover { background: rgba(0, 255, 136, 0.1); }
    .leaflet-control-zoom.leaflet-bar a:focus { outline: none; }
    
    /* Alias Input */
    input#aliasInput {
      background: var(--bg-input);
      color: var(--text-cyan);
      border: 1px solid rgba(240, 171, 252, 0.4);
      padding: 6px 8px;
      font-size: 0.8rem;
      font-family: 'JetBrains Mono', monospace;
      border-radius: 4px;
      outline: none;
      transition: all 0.15s;
    }
    input#aliasInput:focus {
      border-color: var(--text-pink);
      box-shadow: 0 0 0 2px rgba(240, 171, 252, 0.15);
    }
    .leaflet-popup-content-wrapper input:not(#aliasInput) { caret-color: transparent; }
    
    /* Popup Buttons */
    .leaflet-popup-content-wrapper button {
      display: inline-block;
      margin: 2px 3px 2px 0;
      padding: 5px 8px;
      font-size: 0.7rem;
      background: rgba(0, 255, 136, 0.08);
      border: 1px solid rgba(0, 255, 136, 0.4);
      color: var(--text-green);
      border-radius: 4px;
      transition: all 0.15s;
    }
    .leaflet-popup-content-wrapper button:hover {
      background: rgba(0, 255, 136, 0.15);
    }
    .leaflet-popup-content-wrapper button[style*="background-color: green"] {
      background: rgba(0, 255, 136, 0.3) !important;
      color: #fff;
      border-color: var(--text-green);
    }
    .leaflet-popup-content-wrapper input[type="text"],
    .leaflet-popup-content-wrapper input[type="range"] {
      font-size: 0.75rem;
      padding: 4px;
    }
    
    /* Tile Layer */
    .leaflet-tile {
      display: block;
      margin: 0;
      padding: 0;
      transition: none !important;
      image-rendering: crisp-edges;
      background-color: var(--bg-dark);
      border: none !important;
      box-shadow: none !important;
    }
    .leaflet-container { background-color: var(--bg-dark); }
    
    /* Disable cursor */
    .drone-item, #filterToggle {
      user-select: none;
      caret-color: transparent;
      outline: none;
    }
    .drone-item:focus, #filterToggle:focus {
      outline: none;
      caret-color: transparent;
    }
    
    /* Download Section */
    #downloadButtons {
      display: flex;
      width: 100%;
      gap: 4px;
      margin-top: 6px;
    }
    #downloadButtons button {
      flex: 1;
      margin: 0;
      padding: 6px 4px;
      font-size: 0.65rem;
    }
    #downloadSection {
      padding: 0;
      margin-top: 8px;
    }
    #downloadSection .downloadHeader {
      font-family: 'Orbitron', sans-serif;
      font-size: 0.65rem;
      font-weight: 500;
      color: var(--text-pink);
      text-transform: uppercase;
      letter-spacing: 1px;
      margin: 8px 0 6px 0;
      padding-bottom: 4px;
      border-bottom: 1px solid rgba(240, 171, 252, 0.2);
      background: none;
      -webkit-background-clip: unset;
      -webkit-text-fill-color: unset;
    }
    
    /* Slider Styling */
    #staleoutSlider {
      -webkit-appearance: none;
      width: 100%;
      height: 4px;
      background: rgba(99, 102, 241, 0.3);
      border: none;
      border-radius: 2px;
      outline: none;
    }
    #staleoutSlider::-webkit-slider-thumb {
      -webkit-appearance: none;
      height: 14px;
      width: 14px;
      background: var(--text-green);
      border: 2px solid var(--border-secondary);
      margin-top: -5px;
      border-radius: 50%;
      cursor: pointer;
      transition: all 0.15s;
    }
    #staleoutSlider::-webkit-slider-thumb:hover {
      box-shadow: 0 0 8px rgba(0, 255, 136, 0.5);
    }
    #staleoutSlider::-webkit-slider-runnable-track {
      width: 100%;
      height: 4px;
      background: rgba(99, 102, 241, 0.3);
      border: none;
      border-radius: 2px;
    }
    #staleoutSlider::-moz-range-track {
      width: 100%;
      height: 4px;
      background: rgba(99, 102, 241, 0.3);
      border: none;
      border-radius: 2px;
    }
    #staleoutSlider::-moz-range-thumb {
      height: 14px;
      width: 14px;
      background: var(--text-green);
      border: 2px solid var(--border-secondary);
      border-radius: 50%;
      cursor: pointer;
    }

    /* Popup Sliders */
    .leaflet-popup-content-wrapper input[type="range"] {
      -webkit-appearance: none;
      width: 100%;
      height: 4px;
      background: rgba(99, 102, 241, 0.3);
      border: none;
      border-radius: 2px;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-webkit-slider-thumb {
      -webkit-appearance: none;
      height: 14px;
      width: 14px;
      background: var(--text-green);
      border: 2px solid var(--border-secondary);
      margin-top: -5px;
      border-radius: 50%;
      cursor: pointer;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-webkit-slider-runnable-track {
      width: 100%;
      height: 4px;
      background: rgba(99, 102, 241, 0.3);
      border: none;
      border-radius: 2px;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-moz-range-track {
      width: 100%;
      height: 4px;
      background: rgba(99, 102, 241, 0.3);
      border: none;
      border-radius: 2px;
    }
    .leaflet-popup-content-wrapper input[type="range"]::-moz-range-thumb {
      height: 14px;
      width: 14px;
      background: var(--text-green);
      border: 2px solid var(--border-secondary);
      border-radius: 50%;
      cursor: pointer;
    }

    /* Observer buttons */
    .leaflet-popup-content-wrapper #lock-observer,
    .leaflet-popup-content-wrapper #unlock-observer {
      display: inline-block;
      font-size: 0.7rem;
      padding: 5px 8px;
      margin: 2px 3px 2px 0;
    }
    
    /* Cumulative download buttons */
    #downloadCumulativeButtons {
      display: flex;
      gap: 4px;
      margin-top: 4px;
    }
    #downloadCumulativeButtons button {
      flex: 1;
      margin: 0;
      padding: 6px 4px;
      font-size: 0.6rem;
      }
    </style>
</head>
<body>
<!-- Loading Overlay -->
<div id="loadingOverlay" style="position:fixed;top:0;left:0;width:100%;height:100%;background:#080810;z-index:99999;display:flex;flex-direction:column;align-items:center;justify-content:center;">
  <div style="color:#00ff88;font-family:'Orbitron',monospace;font-size:1.5em;margin-bottom:20px;">MESH MAPPER</div>
  <div style="width:50px;height:50px;border:3px solid rgba(99,102,241,0.3);border-top:3px solid #00ff88;border-radius:50%;animation:spin 1s linear infinite;"></div>
  <div style="color:#6b7280;font-size:0.8em;margin-top:15px;">Loading...</div>
</div>
<style>@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}</style>
<script>window.addEventListener('load',function(){setTimeout(function(){document.getElementById('loadingOverlay').style.display='none';},100);});</script>

<div id="map"></div>
<div id="filterBox">
  <div id="filterHeader">
    <h3>Drones</h3>
    <span id="filterToggle">[-]</span>
  </div>
  <div id="filterContent">
    <!-- View Mode Selector -->
    <div style="margin-bottom:12px;">
      <label style="color:var(--text-muted); font-size:0.65rem; margin-bottom:6px; display:block; text-transform:uppercase; letter-spacing:0.5px;">View Mode</label>
      <select id="viewModeSelect" style="width:100%;">
        <option value="live">Live Tracking</option>
        <option value="session">Session History (KML)</option>
        <option value="cumulative">All History (KML)</option>
      </select>
    </div>
    
    <!-- Live View Content -->
    <div id="liveViewContent">
      <h3>Active</h3>
    <div id="activePlaceholder" class="placeholder"></div>
      <h3>Inactive</h3>
    <div id="inactivePlaceholder" class="placeholder"></div>
      
      <div style="margin-top:12px;">
        <label style="color:var(--text-muted); font-size:0.65rem; margin-bottom:6px; display:block; text-transform:uppercase; letter-spacing:0.5px;">Staleout Time</label>
        <input type="range" id="staleoutSlider" min="1" max="5" step="1" value="1">
        <div id="staleoutValue" style="color:var(--text-cyan); font-size:0.7rem; text-align:center; margin-top:4px;">1 min</div>
    </div>
    </div>
    
    <!-- Historical View Content -->
    <div id="historicalViewContent" style="display:none;">
      <!-- Stats Panel -->
      <div style="padding:10px; background:rgba(99,102,241,0.1); border:1px solid rgba(99,102,241,0.3); border-radius:6px; margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between; font-size:0.7rem;">
          <div><span style="color:var(--text-pink);">Mode:</span> <span id="histModeLabel" style="color:var(--text-cyan);">-</span></div>
          <div><span style="color:var(--text-pink);">Drones:</span> <span id="histDroneCount" style="color:var(--text-green);">0</span></div>
          <div><span style="color:var(--text-pink);">Tracks:</span> <span id="histPathCount" style="color:var(--text-green);">0</span></div>
        </div>
        <div style="font-size:0.65rem; color:var(--text-muted); margin-top:6px;">
          <span>Visible: <span id="histVisibleCount" style="color:var(--text-cyan);">0</span></span>
          <span style="margin-left:10px;">Hidden: <span id="histHiddenCount" style="color:var(--text-pink);">0</span></span>
        </div>
      </div>
      
      <!-- Refresh/Clear Buttons -->
      <div style="display:flex; gap:6px; margin-bottom:10px;">
        <button id="refreshHistoricalBtn" style="flex:1;">Refresh Now</button>
        <button id="clearHistoricalBtn" style="flex:1; background:rgba(240,171,252,0.1); border-color:rgba(240,171,252,0.4); color:var(--text-pink);">Clear Map</button>
      </div>
      
      <!-- Date/Time Filter -->
      <div style="margin-bottom:10px; padding:8px; background:rgba(0,0,0,0.2); border-radius:4px;">
        <label style="color:var(--text-muted); font-size:0.6rem; text-transform:uppercase; letter-spacing:0.5px; display:block; margin-bottom:6px;">Date/Time Filter</label>
        <div style="display:flex; gap:4px; margin-bottom:6px;">
          <input type="datetime-local" id="histDateFrom" style="flex:1; padding:4px; font-size:0.65rem; background:var(--bg-input); border:1px solid rgba(99,102,241,0.3); border-radius:3px; color:var(--text-cyan);">
          <span style="color:var(--text-muted); font-size:0.7rem; align-self:center;">to</span>
          <input type="datetime-local" id="histDateTo" style="flex:1; padding:4px; font-size:0.65rem; background:var(--bg-input); border:1px solid rgba(99,102,241,0.3); border-radius:3px; color:var(--text-cyan);">
        </div>
        <div style="display:flex; gap:4px;">
          <button id="applyDateFilterBtn" style="flex:1; padding:4px; font-size:0.65rem;">Apply Filter</button>
          <button id="clearDateFilterBtn" style="flex:1; padding:4px; font-size:0.65rem; background:transparent; border-color:rgba(240,171,252,0.4); color:var(--text-pink);">Clear</button>
        </div>
      </div>
      
      <!-- Search/Filter -->
      <div style="margin-bottom:8px;">
        <input type="text" id="histSearchInput" placeholder="Search MAC, OUI, alias..." style="width:100%; padding:8px; font-size:0.7rem; background:var(--bg-input); border:1px solid rgba(99,102,241,0.3); border-radius:4px; color:var(--text-cyan);">
      </div>
      
      <!-- Show/Hide All + Hide Active Toggle -->
      <div style="display:flex; gap:4px; margin-bottom:8px;">
        <button id="showAllHistBtn" style="flex:1; padding:4px; font-size:0.65rem;">Show All</button>
        <button id="hideAllHistBtn" style="flex:1; padding:4px; font-size:0.65rem; background:transparent; border-color:rgba(240,171,252,0.4); color:var(--text-pink);">Hide All</button>
      </div>
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px; padding:6px; background:rgba(0,0,0,0.2); border-radius:4px;">
        <label class="switch" style="flex-shrink:0;">
          <input type="checkbox" id="hideActiveToggle">
          <span class="slider"></span>
        </label>
        <span style="font-size:0.65rem; color:var(--text-muted);">Hide currently active drones</span>
      </div>
      
      <!-- Drone List -->
      <h3>Historical Drones</h3>
      <div id="histDronePlaceholder" class="placeholder" style="min-height:100px; max-height:220px;"></div>
    </div>
    
    <div id="downloadSection">
      <h4 class="downloadHeader">Downloads</h4>
      <div id="downloadButtons">
        <button id="downloadCsv">CSV</button>
        <button id="downloadKml">KML</button>
        <button id="downloadAliases">Aliases</button>
      </div>
      <div id="downloadCumulativeButtons">
        <button id="downloadCumulativeCsv">All CSV</button>
        <button id="downloadCumulativeKml">All KML</button>
      </div>
    </div>
    
    <div style="margin-top:12px;">
      <h4 class="downloadHeader" style="margin-bottom:8px;">Basemap</h4>
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
    
    <button id="settingsButton" onclick="window.location.href='/select_ports'" style="width:100%; margin-top:12px;">
      Settings
    </button>
    
    <div style="margin-top:12px; padding:8px; background:rgba(0,0,0,0.3); border:1px solid rgba(99,102,241,0.3); border-radius:6px;">
      <div style="font-size:0.6rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px;">Device Status</div>
      <div id="serialStatus" style="font-size:0.7rem; line-height:1.5;">
        <!-- USB port statuses will be injected here via WebSocket -->
      </div>
    </div>
  </div>
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

// --- Socket.IO real-time updates ---
const socket = io();

// On connect, optionally log or show status
socket.on('connected', function(data) {
  console.log(data.message);
});

// Listen for real-time detection events (single detection)
socket.on('detection', function(detection) {
  if (!window.tracked_pairs) window.tracked_pairs = {};
  window.tracked_pairs[detection.mac] = detection;
  localStorage.setItem("trackedPairs", JSON.stringify(window.tracked_pairs));
  updateComboList(window.tracked_pairs);
  updateAliases();
  // ... update markers, popups, etc. ...
});

// Listen for full detections state
socket.on('detections', function(allDetections) {
  window.tracked_pairs = allDetections;
  localStorage.setItem("trackedPairs", JSON.stringify(window.tracked_pairs));
  updateComboList(window.tracked_pairs);
  updateAliases();
  // ... update markers, popups, etc. ...
});

// Listen for real-time serial status events
socket.on('serial_status', function(statuses) {
  const statusDiv = document.getElementById('serialStatus');
  statusDiv.innerHTML = "";
  if (statuses) {
    for (const port in statuses) {
      const div = document.createElement("div");
      div.innerHTML = '<span class="usb-name">' + port + '</span>: ' +
        (statuses[port] ? '<span style="color: lime;">Connected</span>' : '<span style="color: red;">Disconnected</span>');
      statusDiv.appendChild(div);
    }
  }
});

// Listen for real-time aliases updates
socket.on('aliases', function(newAliases) {
  aliases = newAliases;
  updateComboList(window.tracked_pairs);
});

// Listen for real-time paths updates
socket.on('paths', function(paths) {
  // Update dronePaths and pilotPaths, redraw polylines, etc.
  // You may want to call restorePaths() or similar logic here
  // ...
});

// Listen for real-time cumulative log updates
socket.on('cumulative_log', function(log) {
  // Optionally update UI with new log data
  // ...
});

// Listen for real-time FAA cache updates
socket.on('faa_cache', function(faaCache) {
  // Optionally update UI with new FAA data
  // ...
});

// Remove all polling for detections, serial status, aliases, paths, cumulative log, FAA cache, etc.
// All UI updates are now handled by Socket.IO events above.
// ... existing code ...

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
  // if it's already on-screen, do just a small "quarter" zoom
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

// Global variable to track the current popup timeout
let currentPopupTimeout = null;

// Transient terminal-style popup for drone events
function showTerminalPopup(det, isNew) {
  // Clear any existing timeout first
  if (currentPopupTimeout) {
    clearTimeout(currentPopupTimeout);
    currentPopupTimeout = null;
  }

  // Remove any existing popup
  const old = document.getElementById('dronePopup');
  if (old) old.remove();

  // Build a new popup container
  const popup = document.createElement('div');
  popup.id = 'dronePopup';
  const isMobile = window.innerWidth <= 600;
  Object.assign(popup.style, {
    position: 'fixed',
    top: isMobile ? '50px' : '12px',
    left: '50%',
    transform: 'translateX(-50%)',
    background: 'rgba(8, 8, 16, 0.95)',
    backdropFilter: 'blur(12px)',
    color: '#00ff88',
    fontFamily: "'JetBrains Mono', monospace",
    whiteSpace: 'normal',
    padding: isMobile ? '8px 12px' : '10px 16px',
    border: '1px solid rgba(0, 255, 136, 0.4)',
    borderRadius: '6px',
    zIndex: 2000,
    fontSize: isMobile ? '0.7rem' : '0.75rem',
    maxWidth: isMobile ? '85vw' : '500px',
    display: 'inline-block',
    textAlign: 'center',
    boxShadow: '0 4px 20px rgba(0, 0, 0, 0.5)',
  });

  // Build concise popup text
  const alias = aliases[det.mac];
  const rid   = det.basic_id || 'N/A';
  let header;
  if (!det.drone_lat || !det.drone_long || det.drone_lat === 0 || det.drone_long === 0) {
    header = 'Drone with no GPS lock detected';
  } else if (alias) {
    header = `Known drone detected - ${alias}`;
  } else {
    header = isNew ? 'New drone detected' : 'Previously seen non-aliased drone detected';
  }
  const content = alias
    ? `${header} - RID:${rid} MAC:${det.mac}`
    : `${header} - RID:${rid} MAC:${det.mac}`;
  // Build popup HTML and button using new logic
  // Build popup text
  const isMobileBtn = window.innerWidth <= 600;
  const headerDiv = `<div style="line-height:1.4;">${content}</div>`;
  let buttonDiv = '';
  if (det.drone_lat && det.drone_long && det.drone_lat !== 0 && det.drone_long !== 0) {
    const btnStyle = [
      'display:block',
      'width:100%',
      'margin-top:8px',
      'padding:' + (isMobileBtn ? '6px 0' : '8px 12px'),
      'border:1px solid rgba(240, 171, 252, 0.5)',
      'border-radius:4px',
      'background:rgba(240, 171, 252, 0.1)',
      'color:#f0abfc',
      "font-family:'JetBrains Mono', monospace",
      'font-size:' + (isMobileBtn ? '0.7rem' : '0.75rem'),
      'cursor:pointer',
      'transition:all 0.15s ease'
    ].join('; ');
    buttonDiv = `<div><button id="zoomBtn" style="${btnStyle}">Zoom to Drone</button></div>`;
  }
  popup.innerHTML = headerDiv + buttonDiv;

  if (buttonDiv) {
    const zoomBtn = popup.querySelector('#zoomBtn');
    zoomBtn.addEventListener('click', () => {
      zoomBtn.style.background = 'rgba(240, 171, 252, 0.3)';
      setTimeout(() => { zoomBtn.style.background = 'rgba(240, 171, 252, 0.1)'; }, 200);
      safeSetView([det.drone_lat, det.drone_long]);
    });
  }
  // --- Webhook logic (scoped, non-intrusive) ---
  // Webhooks are now handled automatically by the backend
  // Backend triggers webhooks using the same detection logic as these popups
  // --- End webhook logic ---

  document.body.appendChild(popup);

  // Set a new 5-second timeout and store the reference
  currentPopupTimeout = setTimeout(() => {
    const popupToRemove = document.getElementById('dronePopup');
    if (popupToRemove) {
      popupToRemove.remove();
    }
    currentPopupTimeout = null;
  }, 5000);
}

var followLock = { type: null, id: null, enabled: false };

function generateObserverPopup() {
  var observerLocked = (followLock.enabled && followLock.type === 'observer');
  var storedObserverEmoji = localStorage.getItem('observerEmoji') || "😎";
  return `
  <div>
    <strong style="color:#f0abfc;">Observer Location</strong><br>
    <div style="margin:8px 0;">
      <label for="observerEmoji" style="color:#6b7280;font-size:0.85em;display:block;margin-bottom:4px;">Icon</label>
      <select id="observerEmoji" onchange="updateObserverEmoji()" style="width:100%;">
       <option value="😎" ${storedObserverEmoji === "😎" ? "selected" : ""}>😎</option>
       <option value="👽" ${storedObserverEmoji === "👽" ? "selected" : ""}>👽</option>
       <option value="🤖" ${storedObserverEmoji === "🤖" ? "selected" : ""}>🤖</option>
       <option value="🏎️" ${storedObserverEmoji === "🏎️" ? "selected" : ""}>🏎️</option>
       <option value="🕵️‍♂️" ${storedObserverEmoji === "🕵️‍♂️" ? "selected" : ""}>🕵️‍♂️</option>
       <option value="🥷" ${storedObserverEmoji === "🥷" ? "selected" : ""}>🥷</option>
       <option value="👁️" ${storedObserverEmoji === "👁️" ? "selected" : ""}>👁️</option>
      </select>
    </div>
    <div style="display:flex; gap:4px; margin-top:8px;">
        <button id="lock-observer" onclick="lockObserver()" style="flex:1;${observerLocked ? 'background:rgba(0,255,136,0.25);border-color:var(--text-green);' : ''}">
          ${observerLocked ? 'Locked' : 'Lock'}
        </button>
        <button id="unlock-observer" onclick="unlockObserver()" style="flex:1;${!observerLocked ? 'background:rgba(99,102,241,0.2);border-color:var(--border-secondary);' : ''}">
          ${observerLocked ? 'Unlock' : 'Unlocked'}
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
  var mac = detection.mac;
  var aliasText = aliases[mac] ? aliases[mac] : "No Alias";
  var isPilot = markerType && markerType.toLowerCase().includes('pilot');
  var isDroneLocked = (followLock.enabled && followLock.type === 'drone' && followLock.id === mac);
  var isPilotLocked = (followLock.enabled && followLock.type === 'pilot' && followLock.id === mac);
  var defaultHue = colorOverrides[mac] !== undefined ? colorOverrides[mac] : (function(){
      var hash = 0;
      for (var i = 0; i < mac.length; i++){ hash = mac.charCodeAt(i) + ((hash << 5) - hash); }
      return Math.abs(hash) % 360;
  })();
  
  var content = '<div style="font-family:JetBrains Mono,monospace;width:100%;max-width:260px;box-sizing:border-box;">';
  
  // Header
  content += '<strong id="aliasDisplay_' + mac + '" style="color:#f0abfc;font-size:0.9em;word-break:break-all;">' + aliasText + '</strong><br>';
  content += '<span style="color:#6b7280;font-size:0.7em;word-break:break-all;">MAC: ' + mac + '</span><br>';
  content += '<span style="color:#00ffd5;font-size:0.7em;">' + (isPilot ? 'Pilot Location' : 'Drone Location') + '</span>';
  
  // RemoteID section
  if (detection.basic_id) {
    content += '<div style="border:1px solid rgba(240,171,252,0.4);background:rgba(240,171,252,0.08);padding:4px;margin:6px 0;border-radius:4px;font-size:0.75em;word-break:break-all;"><span style="color:#f0abfc;">RemoteID:</span> <span style="color:#00ffd5;">' + detection.basic_id + '</span></div>';
    content += '<button onclick="event.stopPropagation();queryFaaAPI(\\'' + mac + '\\', \\'' + detection.basic_id + '\\')" id="queryFaaButton_' + mac + '" style="width:100%;font-size:0.65em;padding:4px;margin-bottom:4px;">Query FAA</button>';
  } else {
    content += '<div style="color:#6b7280;font-size:0.7em;margin:4px 0;">No RemoteID detected</div>';
  }
  
  // FAA Data section
  content += '<div id="faaResult_' + mac + '">';
  if (detection.faa_data && detection.faa_data.data && detection.faa_data.data.items && detection.faa_data.data.items.length > 0) {
    var item = detection.faa_data.data.items[0];
    content += '<div style="border:1px solid rgba(99,102,241,0.4);background:rgba(99,102,241,0.08);padding:4px;margin:4px 0;border-radius:4px;font-size:0.7em;">';
    if (item.makeName) content += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Make:</span> <span style="color:#00ff88;">' + item.makeName + '</span></div>';
    if (item.modelName) content += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Model:</span> <span style="color:#00ff88;">' + item.modelName + '</span></div>';
    if (item.series) content += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Series:</span> <span style="color:#00ff88;">' + item.series + '</span></div>';
    if (item.trackingNumber) content += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Tracking:</span> <span style="color:#00ff88;">' + item.trackingNumber + '</span></div>';
    content += '</div>';
  }
  content += '</div>';
  
  // Coordinates section
  content += '<div style="margin:6px 0;padding:4px;background:rgba(0,0,0,0.2);border-radius:4px;font-size:0.7em;">';
  if (detection.drone_lat && detection.drone_long && detection.drone_lat != 0 && detection.drone_long != 0) {
    content += '<div><span style="color:#6b7280;">Drone:</span> <span style="color:#e0e0e0;">' + parseFloat(detection.drone_lat).toFixed(6) + ', ' + parseFloat(detection.drone_long).toFixed(6) + '</span></div>';
  }
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    content += '<div><span style="color:#6b7280;">Pilot:</span> <span style="color:#e0e0e0;">' + parseFloat(detection.pilot_lat).toFixed(6) + ', ' + parseFloat(detection.pilot_long).toFixed(6) + '</span></div>';
  }
  if (detection.drone_alt !== undefined) content += '<div><span style="color:#6b7280;">Altitude:</span> <span style="color:#e0e0e0;">' + detection.drone_alt + 'm</span></div>';
  if (detection.drone_speed !== undefined) content += '<div><span style="color:#6b7280;">Speed:</span> <span style="color:#e0e0e0;">' + detection.drone_speed + '</span></div>';
  content += '</div>';
  
  // Google Maps links
  content += '<div style="font-size:0.7em;margin:4px 0;">';
  if (detection.drone_lat && detection.drone_long && detection.drone_lat != 0 && detection.drone_long != 0) {
    content += '<a target="_blank" href="https://www.google.com/maps/search/?api=1&query=' + detection.drone_lat + ',' + detection.drone_long + '" style="color:#00ffd5;" onclick="event.stopPropagation();">Drone Maps</a>';
  }
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    if (detection.drone_lat && detection.drone_long) content += ' | ';
    content += '<a target="_blank" href="https://www.google.com/maps/search/?api=1&query=' + detection.pilot_lat + ',' + detection.pilot_long + '" style="color:#00ffd5;" onclick="event.stopPropagation();">Pilot Maps</a>';
  }
  content += '</div>';
  
  // Alias input section
  content += '<div style="border-top:1px solid rgba(99,102,241,0.3);margin-top:6px;padding-top:6px;">';
  content += '<label style="color:#6b7280;font-size:0.7em;display:block;margin-bottom:3px;">Set Alias:</label>';
  content += '<input type="text" id="aliasInput" onclick="event.stopPropagation();" ontouchstart="event.stopPropagation();" value="' + (aliases[mac] || '') + '" style="width:100%;display:block;box-sizing:border-box;font-size:0.7em;padding:5px;background:#12121f;color:#e0e0e0;border:1px solid rgba(99,102,241,0.3);border-radius:3px;">';
  content += '<div style="display:flex;gap:4px;margin-top:4px;">';
  content += '<button onclick="event.stopPropagation();saveAlias(\\'' + mac + '\\')" style="flex:1;font-size:0.65em;padding:5px;">Save</button>';
  content += '<button onclick="event.stopPropagation();clearAlias(\\'' + mac + '\\')" style="flex:1;font-size:0.65em;padding:5px;">Clear</button>';
  content += '</div></div>';
  
  // Lock buttons section
  content += '<div style="border-top:1px solid rgba(99,102,241,0.3);margin-top:6px;padding-top:6px;">';
  content += '<div style="display:flex;gap:3px;margin-bottom:3px;">';
  content += '<button id="lock-drone-' + mac + '" onclick="event.stopPropagation();lockMarker(\\'drone\\', \\'' + mac + '\\')" style="flex:1;font-size:0.6em;padding:4px;' + (isDroneLocked ? 'background:rgba(0,255,136,0.25);' : '') + '">' + (isDroneLocked ? 'Drone Locked' : 'Lock Drone') + '</button>';
  content += '<button id="unlock-drone-' + mac + '" onclick="event.stopPropagation();unlockMarker(\\'drone\\', \\'' + mac + '\\')" style="flex:1;font-size:0.6em;padding:4px;' + (!isDroneLocked ? 'background:rgba(99,102,241,0.2);' : '') + '">' + (isDroneLocked ? 'Unlock' : 'Unlocked') + '</button>';
  content += '</div>';
  content += '<div style="display:flex;gap:3px;">';
  content += '<button id="lock-pilot-' + mac + '" onclick="event.stopPropagation();lockMarker(\\'pilot\\', \\'' + mac + '\\')" style="flex:1;font-size:0.6em;padding:4px;' + (isPilotLocked ? 'background:rgba(0,255,136,0.25);' : '') + '">' + (isPilotLocked ? 'Pilot Locked' : 'Lock Pilot') + '</button>';
  content += '<button id="unlock-pilot-' + mac + '" onclick="event.stopPropagation();unlockMarker(\\'pilot\\', \\'' + mac + '\\')" style="flex:1;font-size:0.6em;padding:4px;' + (!isPilotLocked ? 'background:rgba(99,102,241,0.2);' : '') + '">' + (isPilotLocked ? 'Unlock' : 'Unlocked') + '</button>';
  content += '</div></div>';
  
  // Color slider section
  content += '<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.2);">';
  content += '<label style="display:block;color:#6b7280;font-size:0.7em;margin-bottom:3px;">Marker Color</label>';
  content += '<input type="range" id="colorSlider_' + mac + '" min="0" max="360" value="' + defaultHue + '" style="width:100%;display:block;box-sizing:border-box;" onchange="updateColor(\\'' + mac + '\\', this.value)" onclick="event.stopPropagation();">';
  content += '</div>';
  
  content += '</div>';
  return content;
}

// New function to query the FAA API.
async function queryFaaAPI(mac, remote_id) {
    // Disable both live and historical FAA buttons
    const button = document.getElementById("queryFaaButton_" + mac);
    const histButton = document.getElementById("histQueryFaaBtn_" + mac);
    if (button) {
        button.disabled = true;
        button.textContent = "Querying...";
        button.style.backgroundColor = "gray";
    }
    if (histButton) {
        histButton.disabled = true;
        histButton.textContent = "Querying...";
        histButton.style.backgroundColor = "gray";
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
            // Also update historical drone data if exists
            if (historicalDroneData) {
              Object.keys(historicalDroneData).forEach(id => {
                if (historicalDroneData[id].mac === mac) {
                  historicalDroneData[id].faa_data = result.faa_data;
                }
              });
            }
            
            // Build FAA result HTML
            let faaData = result.faa_data;
            let item = null;
            if (faaData.data && faaData.data.items && faaData.data.items.length > 0) {
              item = faaData.data.items[0];
            }
            let html = '';
            if (item) {
              html = '<div style="border:1px solid rgba(99,102,241,0.4);background:rgba(99,102,241,0.08);padding:4px;margin:4px 0;border-radius:4px;font-size:0.7em;">';
              if (item.makeName) html += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Make:</span> <span style="color:#00ff88;">' + item.makeName + '</span></div>';
              if (item.modelName) html += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Model:</span> <span style="color:#00ff88;">' + item.modelName + '</span></div>';
              if (item.series) html += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Series:</span> <span style="color:#00ff88;">' + item.series + '</span></div>';
              if (item.trackingNumber) html += '<div style="margin:1px 0;"><span style="color:#f0abfc;">Tracking:</span> <span style="color:#00ff88;">' + item.trackingNumber + '</span></div>';
              html += '</div>';
            } else {
              html = '<div style="border:1px solid rgba(99,102,241,0.3);padding:4px;margin:4px 0;border-radius:4px;color:#6b7280;font-size:0.65em;">No FAA data available</div>';
            }
            
            // Update both live and historical FAA result divs
            const faaDiv = document.getElementById("faaResult_" + mac);
            const histFaaDiv = document.getElementById("histFaaResult_" + mac);
            if (faaDiv) faaDiv.innerHTML = html;
            if (histFaaDiv) histFaaDiv.innerHTML = html;
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
        // Reset live button
        const button = document.getElementById("queryFaaButton_" + mac);
        if (button) {
            button.disabled = false;
            button.style.backgroundColor = "";
            button.textContent = "Query FAA";
        }
        // Reset historical button
        const histButton = document.getElementById("histQueryFaaBtn_" + mac);
        if (histButton) {
            histButton.disabled = false;
            histButton.style.backgroundColor = "";
            histButton.textContent = "Query FAA";
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
  this.style.color = "#FF00FF";
  setTimeout(() => { this.style.backgroundColor = "rgba(0,0,0,0.8)"; this.style.color = "#FF00FF"; }, 500);
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
    
    // Handle no-GPS styling with 5-second transmission timeout
    const det = data[mac];
    const hasGps = det && det.drone_lat && det.drone_long && det.drone_lat !== 0 && det.drone_long !== 0;
    const hasRecentTransmission = det && det.last_update && ((currentTime - det.last_update) <= 5);
    
    // Apply no-GPS styling only if drone has no GPS AND has recent transmission (within 5 seconds)
    if (!hasGps && hasRecentTransmission) {
      item.classList.add('no-gps');
    } else {
      item.classList.remove('no-gps');
    }
    
    // Mark ACTIVE items seen in the last 5 seconds - never flash inactive drones
    const isRecent = isActive && detection && ((currentTime - detection.last_update) <= 5);
    item.classList.toggle('recent', isRecent);
    
    // Move to appropriate container
    if (isActive) {
      if (item.parentNode !== activePlaceholder) { activePlaceholder.appendChild(item); }
    } else {
      // Ensure inactive drones never have the recent class
      item.classList.remove('recent');
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
      // ALSO handle no-GPS drones here in centralized popup logic
      const hasGps = validDrone || (pilotLat !== 0 && pilotLng !== 0);
      const hasRecentTransmission = det.last_update && (currentTime - det.last_update <= 5);
      const isNoGpsDrone = !hasGps && hasRecentTransmission;
      
      let shouldShowPopup = false;
      let popupIsNew = false;
      
      if (!initialLoad && det.last_update && (currentTime - det.last_update <= STALE_THRESHOLD)) {
        // GPS drone popup logic
        if (!wasActive && activeNow) {
          shouldShowPopup = true;
          popupIsNew = alias ? false : !seenDrones[mac];
        }
        // No-GPS drone popup logic (centralized here)
        else if (isNoGpsDrone && !alertedNoGpsDrones.has(mac)) {
          shouldShowPopup = true;
          popupIsNew = true;
        }
      }
      
      if (shouldShowPopup) {
        showTerminalPopup(det, popupIsNew);
        seenDrones[mac] = true;
        if (isNoGpsDrone) {
          alertedNoGpsDrones.add(mac);
        }
      }
      // Persist for next update
      previousActive[mac] = activeNow;

      const validPilot = (pilotLat !== 0 && pilotLng !== 0);
      
      // Handle no-GPS drones that are still transmitting (mapping only, no popup)
      if (isNoGpsDrone) {
        // Ensure this MAC is in the persistent list for display
        if (!persistentMACs.includes(mac)) { persistentMACs.push(mac); }
      } else if (!hasRecentTransmission) {
        // Reset alert state when transmission stops
        alertedNoGpsDrones.delete(mac);
      }
      
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
      
      const hasGps = det.drone_lat && det.drone_long && det.drone_lat !== 0 && det.drone_long !== 0;
      const hasRecentTransmission = det.last_update && ((currentTime - det.last_update) <= 5);
      
      if (!hasGps && hasRecentTransmission) {
        // Apply no-GPS styling and one-time alert for drones with no GPS but recent transmission
        droneElem.classList.add('no-gps');
        if (!alertedNoGpsDrones.has(det.mac)) {
          // Duplicate alert removed - already handled in main loop
          // showTerminalPopup(det, true);
          alertedNoGpsDrones.add(det.mac);
        }
      } else {
        // Remove no-GPS styling and reset alert state when GPS is acquired or transmission stops
        droneElem.classList.remove('no-gps');
        if (!hasRecentTransmission) {
          alertedNoGpsDrones.delete(det.mac);
        }
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

  // =====================
  // Historical View Mode
  // =====================
  let currentViewMode = 'live';
  let historicalDroneData = {}; // { macOrId: { name, alias, mac, color, visible, timestamp, droneMarker, pilotMarker, dronePath, pilotPath } }
  
  const viewModeSelect = document.getElementById('viewModeSelect');
  const liveViewContent = document.getElementById('liveViewContent');
  const historicalViewContent = document.getElementById('historicalViewContent');
  const refreshHistoricalBtn = document.getElementById('refreshHistoricalBtn');
  const clearHistoricalBtn = document.getElementById('clearHistoricalBtn');
  const histSearchInput = document.getElementById('histSearchInput');
  const histDronePlaceholder = document.getElementById('histDronePlaceholder');
  const showAllHistBtn = document.getElementById('showAllHistBtn');
  const hideAllHistBtn = document.getElementById('hideAllHistBtn');
  const applyDateFilterBtn = document.getElementById('applyDateFilterBtn');
  const clearDateFilterBtn = document.getElementById('clearDateFilterBtn');
  const hideActiveToggle = document.getElementById('hideActiveToggle');
  
  let historicalRefreshInterval = null;
  let isLoadingHistorical = false;
  let hideActiveDrones = false;
  
  // Auto-load historical data
  async function loadHistoricalData(fitBounds = true) {
    if (isLoadingHistorical || currentViewMode === 'live') return;
    isLoadingHistorical = true;
    
    const endpoint = currentViewMode === 'session' ? '/api/kml/session' : '/api/kml/cumulative';
    
    try {
      const response = await fetch(endpoint);
      const data = await response.json();
      
      if (data.status === 'ok') {
        // Store current visibility states before clearing
        const visibilityStates = {};
        Object.keys(historicalDroneData).forEach(id => {
          visibilityStates[id] = historicalDroneData[id].visible;
        });
        
        clearHistoricalData();
        parseAndPlotKML(data.kml, fitBounds);
        
        // Restore visibility states
        Object.keys(visibilityStates).forEach(id => {
          if (historicalDroneData[id] && !visibilityStates[id]) {
            setDroneVisibility(id, false);
          }
        });
        
        updateHistoricalStats();
        renderHistoricalDroneList();
      }
    } catch (error) {
      console.error('Error loading historical KML:', error);
    } finally {
      isLoadingHistorical = false;
    }
  }
  
  // Start auto-refresh for historical view
  function startHistoricalRefresh() {
    if (historicalRefreshInterval) clearInterval(historicalRefreshInterval);
    // Refresh every 30 seconds (was 5 - too aggressive)
    historicalRefreshInterval = setInterval(() => {
      loadHistoricalData(false); // Don't fit bounds on auto-refresh
    }, 30000);
  }
  
  // Stop auto-refresh
  function stopHistoricalRefresh() {
    if (historicalRefreshInterval) {
      clearInterval(historicalRefreshInterval);
      historicalRefreshInterval = null;
    }
  }
  
  viewModeSelect.addEventListener('change', function() {
    currentViewMode = this.value;
    
    if (currentViewMode === 'live') {
      liveViewContent.style.display = 'block';
      historicalViewContent.style.display = 'none';
      stopHistoricalRefresh();
      clearHistoricalData();
      // Show live markers
      Object.values(droneMarkers).forEach(m => m.addTo(map));
      Object.values(pilotMarkers).forEach(m => m.addTo(map));
      Object.values(dronePolylines).forEach(p => p.addTo(map));
      Object.values(pilotPolylines).forEach(p => p.addTo(map));
      Object.values(droneCircles).forEach(c => c.addTo(map));
      Object.values(pilotCircles).forEach(c => c.addTo(map));
      if (observerMarker) observerMarker.addTo(map);
    } else {
      liveViewContent.style.display = 'none';
      historicalViewContent.style.display = 'block';
      document.getElementById('histModeLabel').textContent = currentViewMode === 'session' ? 'Session' : 'Cumulative';
      // Hide live markers
      Object.values(droneMarkers).forEach(m => map.removeLayer(m));
      Object.values(pilotMarkers).forEach(m => map.removeLayer(m));
      Object.values(dronePolylines).forEach(p => map.removeLayer(p));
      Object.values(pilotPolylines).forEach(p => map.removeLayer(p));
      Object.values(droneCircles).forEach(c => map.removeLayer(c));
      Object.values(pilotCircles).forEach(c => map.removeLayer(c));
      if (observerMarker) map.removeLayer(observerMarker);
      // Auto-load historical data
      loadHistoricalData(true);
      startHistoricalRefresh();
    }
  });
  
  refreshHistoricalBtn.addEventListener('click', function() {
    loadHistoricalData(true);
  });
  
  // Hide active drones toggle
  hideActiveToggle.addEventListener('change', function() {
    hideActiveDrones = this.checked;
    applyActiveFilter();
    updateHistoricalStats();
    renderHistoricalDroneList();
  });
  
  // Apply active drone filter
  function applyActiveFilter() {
    if (!hideActiveDrones) return;
    
    // Hide drones that are currently active
    Object.keys(historicalDroneData).forEach(id => {
      const mac = historicalDroneData[id].mac;
      if (isDroneActive(mac)) {
        setDroneVisibility(id, false);
      }
    });
  }
  
  // Check if a drone is currently active (same logic as live view)
  function isDroneActive(mac) {
    if (!window.tracked_pairs || !window.tracked_pairs[mac]) return false;
    const currentTime = Date.now() / 1000;
    const det = window.tracked_pairs[mac];
    return det && det.last_update && ((currentTime - det.last_update) <= STALE_THRESHOLD);
  }
  
  clearHistoricalBtn.addEventListener('click', function() {
    clearHistoricalData();
    updateHistoricalStats();
    renderHistoricalDroneList();
  });
  
  // Search filter
  histSearchInput.addEventListener('input', function() {
    renderHistoricalDroneList();
  });
  
  // Show/Hide all
  showAllHistBtn.addEventListener('click', function() {
    Object.keys(historicalDroneData).forEach(id => {
      setDroneVisibility(id, true);
    });
    updateHistoricalStats();
    renderHistoricalDroneList();
  });
  
  hideAllHistBtn.addEventListener('click', function() {
    Object.keys(historicalDroneData).forEach(id => {
      setDroneVisibility(id, false);
    });
    updateHistoricalStats();
    renderHistoricalDroneList();
  });
  
  // Date filter
  applyDateFilterBtn.addEventListener('click', function() {
    const fromDate = document.getElementById('histDateFrom').value;
    const toDate = document.getElementById('histDateTo').value;
    const fromTime = fromDate ? new Date(fromDate).getTime() : 0;
    const toTime = toDate ? new Date(toDate).getTime() : Infinity;
    
    Object.keys(historicalDroneData).forEach(id => {
      const drone = historicalDroneData[id];
      const droneTime = drone.timestamp ? new Date(drone.timestamp).getTime() : Date.now();
      const inRange = droneTime >= fromTime && droneTime <= toTime;
      setDroneVisibility(id, inRange);
    });
    updateHistoricalStats();
    renderHistoricalDroneList();
  });
  
  clearDateFilterBtn.addEventListener('click', function() {
    document.getElementById('histDateFrom').value = '';
    document.getElementById('histDateTo').value = '';
    Object.keys(historicalDroneData).forEach(id => {
      setDroneVisibility(id, true);
    });
    updateHistoricalStats();
    renderHistoricalDroneList();
  });
  
  function setDroneVisibility(id, visible) {
    const drone = historicalDroneData[id];
    if (!drone) return;
    drone.visible = visible;
    
    // Main markers
    if (visible) {
      if (drone.droneStartMarker) drone.droneStartMarker.addTo(map);
      if (drone.droneEndMarker) drone.droneEndMarker.addTo(map);
      if (drone.pilotStartMarker) drone.pilotStartMarker.addTo(map);
      if (drone.pilotEndMarker) drone.pilotEndMarker.addTo(map);
      // Start/End circles
      if (drone.startCircle) drone.startCircle.addTo(map);
      if (drone.endCircle) drone.endCircle.addTo(map);
      if (drone.pilotStartCircle) drone.pilotStartCircle.addTo(map);
      if (drone.pilotEndCircle) drone.pilotEndCircle.addTo(map);
    } else {
      if (drone.droneStartMarker) map.removeLayer(drone.droneStartMarker);
      if (drone.droneEndMarker) map.removeLayer(drone.droneEndMarker);
      if (drone.pilotStartMarker) map.removeLayer(drone.pilotStartMarker);
      if (drone.pilotEndMarker) map.removeLayer(drone.pilotEndMarker);
      // Start/End circles
      if (drone.startCircle) map.removeLayer(drone.startCircle);
      if (drone.endCircle) map.removeLayer(drone.endCircle);
      if (drone.pilotStartCircle) map.removeLayer(drone.pilotStartCircle);
      if (drone.pilotEndCircle) map.removeLayer(drone.pilotEndCircle);
    }
    
    // Flight paths
    if (drone.flights) {
      drone.flights.forEach(flight => {
        if (visible && flight.visible) {
          if (flight.dronePath) flight.dronePath.addTo(map);
          if (flight.pilotPath) flight.pilotPath.addTo(map);
        } else {
          if (flight.dronePath) map.removeLayer(flight.dronePath);
          if (flight.pilotPath) map.removeLayer(flight.pilotPath);
        }
      });
    }
  }
  
  function updateHistoricalStats() {
    const total = Object.keys(historicalDroneData).length;
    const visible = Object.values(historicalDroneData).filter(d => d.visible).length;
    const hidden = total - visible;
    // Count total flights across all drones
    let flightCount = 0;
    Object.values(historicalDroneData).forEach(d => {
      if (d.flights) flightCount += d.flights.length;
    });
    
    document.getElementById('histDroneCount').textContent = total;
    document.getElementById('histPathCount').textContent = flightCount;
    document.getElementById('histVisibleCount').textContent = visible;
    document.getElementById('histHiddenCount').textContent = hidden;
  }
  
  function renderHistoricalDroneList() {
    const searchTerm = histSearchInput.value.toLowerCase();
    histDronePlaceholder.innerHTML = '';
    const currentTime = Date.now() / 1000;
    
    const sortedIds = Object.keys(historicalDroneData).sort((a, b) => {
      const aName = historicalDroneData[a].alias || historicalDroneData[a].mac || a;
      const bName = historicalDroneData[b].alias || historicalDroneData[b].mac || b;
      return aName.localeCompare(bName);
    });
    
    console.log('Rendering historical drone list, IDs:', sortedIds);
    
    sortedIds.forEach(id => {
      const drone = historicalDroneData[id];
      const displayName = drone.alias || drone.mac || id;
      const mac = drone.mac || '';
      const oui = mac.substring(0, 8);
      
      // Check if this drone is currently active in live tracking (same logic as updateComboList)
      let isActive = false;
      let isRecent = false;
      if (window.tracked_pairs && window.tracked_pairs[mac]) {
        const det = window.tracked_pairs[mac];
        isActive = det && det.last_update && ((currentTime - det.last_update) <= STALE_THRESHOLD);
        isRecent = det && det.last_update && ((currentTime - det.last_update) <= 5);
      }
      
      // Filter by search term
      if (searchTerm && !displayName.toLowerCase().includes(searchTerm) && 
          !mac.toLowerCase().includes(searchTerm) && !oui.toLowerCase().includes(searchTerm)) {
        return;
      }
      
      // Filter out active drones if toggle is on
      if (hideActiveDrones && isActive) {
        return;
      }
      
      // Use EXACT same class logic as live view updateComboList
      const item = document.createElement('div');
      item.className = 'drone-item';
      if (isRecent) item.classList.add('recent');
      if (!drone.visible) {
        item.style.opacity = '0.5';
        item.style.textDecoration = 'line-through';
      }
      
      item.textContent = displayName;
      item.style.borderColor = drone.color;
      item.style.color = drone.visible ? drone.color : '#555';
      
      // Click to toggle visibility
      item.addEventListener('click', function(e) {
        setDroneVisibility(id, !drone.visible);
        updateHistoricalStats();
        renderHistoricalDroneList();
      });
      
      // Double-click to zoom
      item.addEventListener('dblclick', function(e) {
        e.stopPropagation();
        if (drone.droneMarkers && drone.droneMarkers.length > 0) {
          const latlng = drone.droneMarkers[0].getLatLng();
          map.setView(latlng, 16);
          drone.droneMarkers[0].openPopup();
        }
      });
      
      histDronePlaceholder.appendChild(item);
    });
  }
  
  function clearHistoricalData() {
    // Stop any replay
    stopReplay();
    
    Object.values(historicalDroneData).forEach(drone => {
      // Main markers
      if (drone.droneStartMarker) map.removeLayer(drone.droneStartMarker);
      if (drone.droneEndMarker) map.removeLayer(drone.droneEndMarker);
      if (drone.pilotStartMarker) map.removeLayer(drone.pilotStartMarker);
      if (drone.pilotEndMarker) map.removeLayer(drone.pilotEndMarker);
      
      // Start/End circles
      if (drone.startCircle) map.removeLayer(drone.startCircle);
      if (drone.endCircle) map.removeLayer(drone.endCircle);
      if (drone.pilotStartCircle) map.removeLayer(drone.pilotStartCircle);
      if (drone.pilotEndCircle) map.removeLayer(drone.pilotEndCircle);
      
      // Flight paths
      if (drone.flights) {
        drone.flights.forEach(flight => {
          if (flight.dronePath) map.removeLayer(flight.dronePath);
          if (flight.pilotPath) map.removeLayer(flight.pilotPath);
        });
      }
    });
    historicalDroneData = {};
    histDronePlaceholder.innerHTML = '';
  }
  
  // Replay state
  let replayState = {
    active: false,
    droneId: null,
    flightIdx: null,
    marker: null,
    pilotMarker: null,
    dronePath: null,
    pilotPath: null,
    pathIdx: 0,
    maxLength: 0,
    coords: [],
    pilotCoords: [],
    speed: 1,
    paused: false,
    intervalId: null,
    droneName: '',
    droneColor: '',
    totalPoints: 0,
    droneData: null
  };
  
  // Generate popup for replay markers with all the details
  function generateReplayPopup(droneData, markerType) {
    const mac = droneData.mac || '';
    const currentAlias = aliases[mac] || droneData.alias || '';
    const displayName = currentAlias || mac || 'Unknown';
    const isPilot = markerType === 'pilot';
    const id = mac; // Use mac as id for replay
    
    let content = `<div style="font-family:'JetBrains Mono',monospace;width:100%;max-width:260px;box-sizing:border-box;">`;
    
    // Header
    content += `<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
      <strong id="replayAliasDisplay_${mac}" style="color:#f0abfc;font-size:0.9em;word-break:break-all;flex:1;">${displayName}</strong>
      <span style="color:#00ff88;font-size:0.65em;background:rgba(0,255,136,0.15);padding:2px 6px;border-radius:3px;">REPLAY</span>
    </div>`;
    content += `<span style="color:#6b7280;font-size:0.65em;word-break:break-all;">MAC: ${mac}</span><br>`;
    content += `<span style="color:#00ffd5;font-size:0.65em;">${isPilot ? 'Pilot Location' : 'Drone Location'}</span>`;
    
    // Alias input section
    content += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.3);width:100%;box-sizing:border-box;">
      <label style="color:#6b7280;font-size:0.65em;display:block;margin-bottom:3px;">Set Alias:</label>
      <input type="text" id="replayAliasInput_${mac}" value="${currentAlias}" onclick="event.stopPropagation();" 
             style="width:100%;display:block;box-sizing:border-box;font-size:0.7em;padding:6px;background:#12121f;color:#e0e0e0;border:1px solid rgba(99,102,241,0.3);border-radius:3px;">
      <div style="display:flex;gap:3px;margin-top:4px;width:100%;">
        <button onclick="event.stopPropagation();saveReplayAlias('${mac}')" style="flex:1;font-size:0.65em;padding:6px;">Save</button>
        <button onclick="event.stopPropagation();clearReplayAlias('${mac}')" style="flex:1;font-size:0.65em;padding:6px;">Clear</button>
      </div>
    </div>`;
    
    // Timestamp
    if (droneData.timestamp) {
      content += `<br><span style="color:#6b7280;font-size:0.6em;">${droneData.timestamp}</span>`;
    }
    
    // RemoteID and FAA Query
    const liveDetection = window.tracked_pairs && window.tracked_pairs[mac];
    const basicId = (liveDetection && liveDetection.basic_id) || droneData.basic_id || '';
    
    if (basicId) {
      content += `<div style="border:1px solid rgba(240,171,252,0.4);background:rgba(240,171,252,0.08);padding:4px;margin:6px 0;border-radius:4px;font-size:0.65em;word-break:break-all;">
        <span style="color:#f0abfc;">RemoteID:</span> <span style="color:#00ffd5;">${basicId}</span>
      </div>`;
      // FAA Query button
      content += `<button onclick="event.stopPropagation();queryFaaAPI('${mac}','${basicId}')" style="width:100%;font-size:0.55em;padding:3px;margin-bottom:4px;">Query FAA</button>`;
    }
    
    // FAA data - show if cached
    const faaData = (liveDetection && liveDetection.faa_data) || droneData.faa_data;
    if (faaData && faaData.data && faaData.data.items && faaData.data.items.length > 0) {
      const item = faaData.data.items[0];
      content += `<div style="border:1px solid rgba(99,102,241,0.4);background:rgba(99,102,241,0.08);padding:4px;margin:4px 0;border-radius:4px;font-size:0.6em;">`;
      if (item.makeName) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Make:</span> <span style="color:#00ff88;">${item.makeName}</span></div>`;
      if (item.modelName) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Model:</span> <span style="color:#00ff88;">${item.modelName}</span></div>`;
      if (item.series) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Series:</span> <span style="color:#00ff88;">${item.series}</span></div>`;
      if (item.trackingNumber) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Tracking:</span> <span style="color:#00ff88;">${item.trackingNumber}</span></div>`;
      content += `</div>`;
    }
    
    // Coordinates
    if (droneData.droneCoords && droneData.droneCoords.length > 0) {
      const first = droneData.droneCoords[0];
      const last = droneData.droneCoords[droneData.droneCoords.length - 1];
      content += `<div style="margin:6px 0;padding:4px;background:rgba(0,0,0,0.2);border-radius:4px;font-size:0.6em;">
        <div><span style="color:#6b7280;">Points:</span> <span style="color:#00ff88;">${droneData.droneCoords.length}</span></div>
        <div><span style="color:#6b7280;">Start:</span> <span style="color:#e0e0e0;">${first[0].toFixed(5)}, ${first[1].toFixed(5)}</span></div>
        <div><span style="color:#6b7280;">End:</span> <span style="color:#e0e0e0;">${last[0].toFixed(5)}, ${last[1].toFixed(5)}</span></div>
      </div>`;
      
      // Google Maps links
      content += `<div style="font-size:0.6em;margin:4px 0;">
        <a target="_blank" href="https://www.google.com/maps/search/?api=1&query=${first[0]},${first[1]}" style="color:#00ffd5;">Start</a> | 
        <a target="_blank" href="https://www.google.com/maps/search/?api=1&query=${last[0]},${last[1]}" style="color:#00ffd5;">End</a> (Maps)
      </div>`;
    }
    
    content += `</div>`;
    return content;
  }
  
  
  // Generate popup content for historical drone with color control and replay
  function generateHistoricalPopup(id, droneData, markerType) {
    const displayName = droneData.alias || droneData.mac || id;
    const mac = droneData.mac || '';
    const isPilot = markerType.toLowerCase().includes('pilot');
    
    // Convert hex color to hue for slider
    let hue = 120; // default green
    if (droneData.color) {
      if (droneData.color.startsWith('#')) {
        const hex = droneData.color.slice(1);
        const r = parseInt(hex.slice(0,2), 16) / 255;
        const g = parseInt(hex.slice(2,4), 16) / 255;
        const b = parseInt(hex.slice(4,6), 16) / 255;
        const max = Math.max(r, g, b), min = Math.min(r, g, b);
        if (max !== min) {
          const d = max - min;
          let h;
          if (max === r) h = ((g - b) / d + (g < b ? 6 : 0)) / 6;
          else if (max === g) h = ((b - r) / d + 2) / 6;
          else h = ((r - g) / d + 4) / 6;
          hue = Math.round(h * 360);
        }
      } else if (droneData.color.startsWith('hsl')) {
        const hslMatch = droneData.color.match(/hsl[(]([0-9]+)/);
        if (hslMatch) hue = parseInt(hslMatch[1]);
      }
    }
    
    // Build flight options
    const flightCount = droneData.flights ? droneData.flights.length : 0;
    let flightOptions = '<option value="all">All Flights</option>';
    if (droneData.flights) {
      droneData.flights.forEach((f, i) => {
        const time = f.timestamp ? ' (' + f.timestamp.split(' ')[1] + ')' : '';
        flightOptions += '<option value="' + i + '">Flight ' + f.num + time + '</option>';
      });
    }
    
    // Get current alias from global aliases object
    const currentAlias = aliases[mac] || droneData.alias || '';
    const aliasDisplay = currentAlias || mac || id;
    
    // Start building content - use 100% width for mobile compatibility
    let content = `<div style="font-family:'JetBrains Mono',monospace;width:100%;max-width:260px;box-sizing:border-box;">`;
    
    // Header with alias
    content += `<strong id="histAliasDisplay_${mac}" style="color:#f0abfc;font-size:0.95em;word-break:break-all;">${aliasDisplay}</strong><br>`;
    content += `<span style="color:#6b7280;font-size:0.7em;word-break:break-all;">MAC: ${mac}</span><br>`;
    content += `<span style="color:#00ffd5;font-size:0.7em;">${isPilot ? 'Pilot Location' : 'Drone Location'}</span>`;
    
    if (droneData.timestamp) {
      content += `<br><span style="color:#6b7280;font-size:0.65em;">${droneData.timestamp}</span>`;
    }
    
    // Alias input section
    content += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.3);width:100%;box-sizing:border-box;">
      <label style="color:#6b7280;font-size:0.65em;display:block;margin-bottom:3px;">Set Alias:</label>
      <input type="text" id="histAliasInput_${mac}" value="${currentAlias}" onclick="event.stopPropagation();" 
             style="width:100%;display:block;box-sizing:border-box;font-size:0.7em;padding:6px;background:#12121f;color:#e0e0e0;border:1px solid rgba(99,102,241,0.3);border-radius:3px;">
      <div style="display:flex;gap:3px;margin-top:4px;width:100%;">
        <button onclick="event.stopPropagation();saveHistoricalAlias('${mac}','${id}')" style="flex:1;font-size:0.65em;padding:6px;">Save</button>
        <button onclick="event.stopPropagation();clearHistoricalAlias('${mac}','${id}')" style="flex:1;font-size:0.65em;padding:6px;">Clear</button>
      </div>
    </div>`;
    
    // Check for RemoteID from live detection data or droneData
    const liveDetection = window.tracked_pairs && window.tracked_pairs[mac];
    const basicId = (liveDetection && liveDetection.basic_id) || droneData.basic_id || '';
    
    if (basicId) {
      content += `<div style="border:1px solid rgba(240,171,252,0.4);background:rgba(240,171,252,0.08);padding:4px;margin:6px 0;border-radius:4px;font-size:0.7em;word-break:break-all;">
        <span style="color:#f0abfc;">RemoteID:</span> <span style="color:#00ffd5;">${basicId}</span>
      </div>`;
      // FAA Query button
      content += `<button onclick="event.stopPropagation();queryFaaAPI('${mac}','${basicId}')" id="histQueryFaaBtn_${mac}" style="width:100%;font-size:0.6em;padding:4px;margin-bottom:4px;">Query FAA</button>`;
      content += `<div id="histFaaResult_${mac}"></div>`;
    }
    
    // FAA Data section - show if already cached
    const faaData = (liveDetection && liveDetection.faa_data) || droneData.faa_data;
    if (faaData && faaData.data && faaData.data.items && faaData.data.items.length > 0) {
      const item = faaData.data.items[0];
      content += `<div style="border:1px solid rgba(99,102,241,0.4);background:rgba(99,102,241,0.08);padding:4px;margin:4px 0;border-radius:4px;font-size:0.65em;">`;
      if (item.makeName) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Make:</span> <span style="color:#00ff88;">${item.makeName}</span></div>`;
      if (item.modelName) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Model:</span> <span style="color:#00ff88;">${item.modelName}</span></div>`;
      if (item.series) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Series:</span> <span style="color:#00ff88;">${item.series}</span></div>`;
      if (item.trackingNumber) content += `<div style="margin:1px 0;"><span style="color:#f0abfc;">Tracking:</span> <span style="color:#00ff88;">${item.trackingNumber}</span></div>`;
      content += `</div>`;
    }
    
    // Coordinates info
    if (droneData.droneCoords && droneData.droneCoords.length > 0) {
      const first = droneData.droneCoords[0];
      const last = droneData.droneCoords[droneData.droneCoords.length - 1];
      content += `<div style="margin:6px 0;padding:4px;background:rgba(0,0,0,0.2);border-radius:4px;font-size:0.65em;">
        <div><span style="color:#6b7280;">Points:</span> <span style="color:#00ff88;">${droneData.droneCoords.length}</span></div>
        <div><span style="color:#6b7280;">Start:</span> <span style="color:#e0e0e0;">${first[0].toFixed(5)}, ${first[1].toFixed(5)}</span></div>
        <div><span style="color:#6b7280;">End:</span> <span style="color:#e0e0e0;">${last[0].toFixed(5)}, ${last[1].toFixed(5)}</span></div>
      </div>`;
      
      // Google Maps links
      content += `<div style="font-size:0.65em;margin:4px 0;">
        <a target="_blank" href="https://www.google.com/maps/search/?api=1&query=${first[0]},${first[1]}" style="color:#00ffd5;">Start</a> | 
        <a target="_blank" href="https://www.google.com/maps/search/?api=1&query=${last[0]},${last[1]}" style="color:#00ffd5;">End</a> (Maps)
      </div>`;
    }
    
    // Track Color slider
    content += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.3);width:100%;box-sizing:border-box;">
      <label style="color:#6b7280;font-size:0.65em;display:block;margin-bottom:3px;">Track Color</label>
      <input type="range" id="histColorSlider_${id}" min="0" max="360" value="${hue}" 
             style="width:100%;display:block;box-sizing:border-box;" onchange="updateHistoricalColor('${id}', this.value)">
    </div>`;
    
    // Flights section
    if (flightCount > 0) {
      content += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.3);width:100%;box-sizing:border-box;">
        <label style="color:#6b7280;font-size:0.65em;display:block;margin-bottom:3px;">Flights (${flightCount})</label>
        <select id="flightSelect_${id}" style="width:100%;display:block;font-size:0.7em;padding:6px;background:#12121f;color:#e0e0e0;border:1px solid rgba(99,102,241,0.3);border-radius:3px;box-sizing:border-box;" onchange="filterFlight('${id}', this.value)">
          ${flightOptions}
        </select>
      </div>`;
      
      // Replay controls
      content += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.3);width:100%;box-sizing:border-box;">
        <label style="color:#00ff88;font-size:0.65em;display:block;margin-bottom:3px;">Replay</label>
        <div style="display:flex;gap:3px;margin-bottom:4px;width:100%;">
          <button onclick="event.stopPropagation();startReplay('${id}')" style="flex:1;font-size:0.65em;padding:6px;">▶ Play</button>
          <button onclick="event.stopPropagation();pauseReplay()" style="flex:1;font-size:0.65em;padding:6px;">⏸</button>
          <button onclick="event.stopPropagation();stopReplay()" style="flex:1;font-size:0.65em;padding:6px;">⏹</button>
        </div>
        <div style="display:flex;gap:2px;width:100%;">
          <button onclick="event.stopPropagation();setReplaySpeed(0.5)" style="flex:1;font-size:0.6em;padding:4px;">0.5x</button>
          <button onclick="event.stopPropagation();setReplaySpeed(1)" style="flex:1;font-size:0.6em;padding:4px;">1x</button>
          <button onclick="event.stopPropagation();setReplaySpeed(2)" style="flex:1;font-size:0.6em;padding:4px;">2x</button>
          <button onclick="event.stopPropagation();setReplaySpeed(5)" style="flex:1;font-size:0.6em;padding:4px;">5x</button>
        </div>
      </div>`;
    }
    
    // Show/Hide and Zoom buttons
    content += `<div style="display:flex;gap:4px;margin-top:6px;padding-top:6px;border-top:1px solid rgba(99,102,241,0.3);width:100%;box-sizing:border-box;">
      <button onclick="event.stopPropagation();toggleHistoricalVisibility('${id}')" style="flex:1;font-size:0.7em;padding:8px;">${droneData.visible ? 'Hide' : 'Show'}</button>
      <button onclick="event.stopPropagation();zoomToHistoricalDrone('${id}')" style="flex:1;font-size:0.7em;padding:8px;">Zoom</button>
    </div>`;
    
    content += `</div>`;
    return content;
  }
  
  // Filter to show only specific flight paths (markers stay visible)
  window.filterFlight = function(id, flightVal) {
    const drone = historicalDroneData[id];
    if (!drone || !drone.flights) return;
    
    drone.flights.forEach((flight, idx) => {
      const show = (flightVal === 'all' || parseInt(flightVal) === idx);
      flight.visible = show;
      
      // Only toggle paths - markers are always visible if drone is visible
      if (show && drone.visible) {
        if (flight.dronePath) flight.dronePath.addTo(map);
        if (flight.pilotPath) flight.pilotPath.addTo(map);
      } else {
        if (flight.dronePath) map.removeLayer(flight.dronePath);
        if (flight.pilotPath) map.removeLayer(flight.pilotPath);
      }
    });
  };
  
  // Save alias from historical popup
  window.saveHistoricalAlias = async function(mac, id) {
    const input = document.getElementById('histAliasInput_' + mac);
    if (!input) return;
    
    const alias = input.value.trim();
    try {
      const response = await fetch(window.location.origin + '/api/set_alias', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac: mac, alias: alias})
      });
      const data = await response.json();
      if (data.status === "ok") {
        // Update global alias map
        aliases[mac] = alias;
        
        // Update historical drone data
        if (historicalDroneData[id]) {
          historicalDroneData[id].alias = alias;
        }
        
        // Update display in popup
        const displayEl = document.getElementById('histAliasDisplay_' + mac);
        if (displayEl) {
          displayEl.textContent = alias || mac;
          displayEl.style.backgroundColor = 'rgba(0,255,136,0.2)';
          setTimeout(() => displayEl.style.backgroundColor = '', 300);
        }
        
        // Update historical drone list
        renderHistoricalDroneList();
        
        // Also update live view if applicable
        updateAliases();
        updateComboList(window.tracked_pairs);
        
        console.log('Alias saved:', mac, alias);
      }
    } catch(e) {
      console.error('Error saving alias:', e);
    }
  };
  
  // Clear alias from historical popup
  window.clearHistoricalAlias = async function(mac, id) {
    try {
      const response = await fetch(window.location.origin + '/api/set_alias', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac: mac, alias: ''})
      });
      const data = await response.json();
      if (data.status === "ok") {
        // Clear from global alias map
        delete aliases[mac];
        
        // Update historical drone data
        if (historicalDroneData[id]) {
          historicalDroneData[id].alias = '';
        }
        
        // Clear input and update display
        const input = document.getElementById('histAliasInput_' + mac);
        if (input) input.value = '';
        
        const displayEl = document.getElementById('histAliasDisplay_' + mac);
        if (displayEl) {
          displayEl.textContent = mac;
          displayEl.style.backgroundColor = 'rgba(240,171,252,0.2)';
          setTimeout(() => displayEl.style.backgroundColor = '', 300);
        }
        
        // Update historical drone list
        renderHistoricalDroneList();
        
        // Also update live view if applicable
        updateAliases();
        updateComboList(window.tracked_pairs);
        
        console.log('Alias cleared:', mac);
      }
    } catch(e) {
      console.error('Error clearing alias:', e);
    }
  };
  
  // Save alias from replay popup
  window.saveReplayAlias = async function(mac) {
    const input = document.getElementById('replayAliasInput_' + mac);
    if (!input) return;
    
    const alias = input.value.trim();
    try {
      const response = await fetch(window.location.origin + '/api/set_alias', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac: mac, alias: alias})
      });
      const data = await response.json();
      if (data.status === "ok") {
        // Update global alias map
        aliases[mac] = alias;
        
        // Update historical drone data if exists
        if (historicalDroneData[mac]) {
          historicalDroneData[mac].alias = alias;
        }
        
        // Update replay state
        if (replayState.droneData) {
          replayState.droneData.alias = alias;
          replayState.droneName = alias || mac;
        }
        
        // Update display in popup
        const displayEl = document.getElementById('replayAliasDisplay_' + mac);
        if (displayEl) {
          displayEl.textContent = alias || mac;
          displayEl.style.backgroundColor = 'rgba(0,255,136,0.2)';
          setTimeout(() => displayEl.style.backgroundColor = '', 300);
        }
        
        // Update control bar
        showReplayControls();
        
        // Update lists
        renderHistoricalDroneList();
        updateAliases();
        updateComboList(window.tracked_pairs);
        
        console.log('Replay alias saved:', mac, alias);
      }
    } catch(e) {
      console.error('Error saving replay alias:', e);
    }
  };
  
  // Clear alias from replay popup
  window.clearReplayAlias = async function(mac) {
    try {
      const response = await fetch(window.location.origin + '/api/set_alias', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mac: mac, alias: ''})
      });
      const data = await response.json();
      if (data.status === "ok") {
        // Clear from global alias map
        delete aliases[mac];
        
        // Update historical drone data if exists
        if (historicalDroneData[mac]) {
          historicalDroneData[mac].alias = '';
        }
        
        // Update replay state
        if (replayState.droneData) {
          replayState.droneData.alias = '';
          replayState.droneName = mac;
        }
        
        // Clear input and update display
        const input = document.getElementById('replayAliasInput_' + mac);
        if (input) input.value = '';
        
        const displayEl = document.getElementById('replayAliasDisplay_' + mac);
        if (displayEl) {
          displayEl.textContent = mac;
          displayEl.style.backgroundColor = 'rgba(240,171,252,0.2)';
          setTimeout(() => displayEl.style.backgroundColor = '', 300);
        }
        
        // Update control bar
        showReplayControls();
        
        // Update lists
        renderHistoricalDroneList();
        updateAliases();
        updateComboList(window.tracked_pairs);
        
        console.log('Replay alias cleared:', mac);
      }
    } catch(e) {
      console.error('Error clearing replay alias:', e);
    }
  };
  
  // Start replay - simulates both drone and pilot paths simultaneously
  window.startReplay = function(id) {
    const drone = historicalDroneData[id];
    if (!drone || !drone.flights) return;
    
    // Stop any existing replay
    stopReplay();
    
    // Get selected flight or all flights
    const selectEl = document.getElementById('flightSelect_' + id);
    const flightVal = selectEl ? selectEl.value : 'all';
    replayState.flightIdx = flightVal;
    
    // Collect path coordinates
    let droneCoords = [];
    let pilotCoords = [];
    
    if (flightVal === 'all') {
      drone.flights.forEach(f => {
        if (f.droneCoords && f.droneCoords.length > 0) {
          droneCoords = droneCoords.concat(f.droneCoords);
        } else if (f.dronePath) {
          droneCoords = droneCoords.concat(f.dronePath.getLatLngs());
        }
        if (f.pilotCoords && f.pilotCoords.length > 0) {
          pilotCoords = pilotCoords.concat(f.pilotCoords);
        } else if (f.pilotPath) {
          pilotCoords = pilotCoords.concat(f.pilotPath.getLatLngs());
        }
      });
    } else {
      const flight = drone.flights[parseInt(flightVal)];
      if (flight) {
        if (flight.droneCoords && flight.droneCoords.length > 0) {
          droneCoords = flight.droneCoords;
        } else if (flight.dronePath) {
          droneCoords = flight.dronePath.getLatLngs();
        }
        if (flight.pilotCoords && flight.pilotCoords.length > 0) {
          pilotCoords = flight.pilotCoords;
        } else if (flight.pilotPath) {
          pilotCoords = flight.pilotPath.getLatLngs();
        }
      }
    }
    
    if (droneCoords.length === 0 && pilotCoords.length === 0) {
      console.log('No path data to replay');
      return;
    }
    
    // Hide static start/end markers and circles during replay
    if (drone.droneStartMarker) map.removeLayer(drone.droneStartMarker);
    if (drone.droneEndMarker) map.removeLayer(drone.droneEndMarker);
    if (drone.pilotStartMarker) map.removeLayer(drone.pilotStartMarker);
    if (drone.pilotEndMarker) map.removeLayer(drone.pilotEndMarker);
    if (drone.startCircle) map.removeLayer(drone.startCircle);
    if (drone.endCircle) map.removeLayer(drone.endCircle);
    if (drone.pilotStartCircle) map.removeLayer(drone.pilotStartCircle);
    if (drone.pilotEndCircle) map.removeLayer(drone.pilotEndCircle);
    
    // Hide existing flight paths - we'll show replay paths instead
    if (drone.flights) {
      drone.flights.forEach(f => {
        if (f.dronePath) map.removeLayer(f.dronePath);
        if (f.pilotPath) map.removeLayer(f.pilotPath);
      });
    }
    
    // Use the longer path as the reference for timing
    const maxLength = Math.max(droneCoords.length, pilotCoords.length);
    
    // Store drone info for popup
    replayState.droneName = drone.alias || drone.mac;
    replayState.droneColor = drone.color;
    replayState.totalPoints = droneCoords.length;
    
    // Add coords to drone for popup
    drone.droneCoords = droneCoords;
    drone.pilotCoords = pilotCoords;
    replayState.droneData = drone;
    
    // Create replay path polylines (show the track during replay!)
    if (droneCoords.length > 1) {
      replayState.dronePath = L.polyline(droneCoords, {
        color: drone.color || '#00ff88',
        weight: 3,
        opacity: 0.8
      }).addTo(map);
    }
    if (pilotCoords.length > 1) {
      replayState.pilotPath = L.polyline(pilotCoords, {
        color: drone.color || '#00ffd5',
        weight: 2,
        opacity: 0.6,
        dashArray: '5, 5'
      }).addTo(map);
    }
    
    // Create drone replay marker with full popup
    if (droneCoords.length > 0) {
      replayState.marker = L.marker(droneCoords[0], {
        icon: createIcon('🛸', drone.color),
        zIndexOffset: 1000
      }).bindPopup(generateReplayPopup(drone, 'drone'), {autoPan: false, maxWidth: 280}).addTo(map);
    }
    
    // Create pilot replay marker with full popup
    if (pilotCoords.length > 0) {
      replayState.pilotMarker = L.marker(pilotCoords[0], {
        icon: createIcon('👤', drone.color),
        zIndexOffset: 1000
      }).bindPopup(generateReplayPopup(drone, 'pilot'), {autoPan: false, maxWidth: 280}).addTo(map);
    }
    
    // Store state
    replayState.active = true;
    replayState.droneId = id;
    replayState.pathIdx = 0;
    replayState.paused = false;
    replayState.coords = droneCoords;
    replayState.pilotCoords = pilotCoords;
    replayState.maxLength = maxLength;
    
    // Zoom to include both start points
    const startBounds = [];
    if (droneCoords.length > 0) startBounds.push(droneCoords[0]);
    if (pilotCoords.length > 0) startBounds.push(pilotCoords[0]);
    if (startBounds.length > 0) {
      map.fitBounds(startBounds, { padding: [50, 50], maxZoom: 18 });
    }
    
    // Close any open popups
    map.closePopup();
    
    // Show floating control bar
    showReplayControls();
    
    // Start animation - both markers move together through their respective paths
    const baseInterval = 500; // ms per point at 1x speed (500ms = half second per point)
    replayState.intervalId = setInterval(() => {
      if (replayState.paused) return;
      
      replayState.pathIdx++;
      
      if (replayState.pathIdx >= replayState.maxLength) {
        stopReplay();
        return;
      }
      
      // Move drone marker
      if (replayState.marker && replayState.coords.length > 0) {
        const droneIdx = Math.min(replayState.pathIdx, replayState.coords.length - 1);
        replayState.marker.setLatLng(replayState.coords[droneIdx]);
        
        // Update control bar progress
        updateReplayProgress(droneIdx + 1, replayState.totalPoints);
      }
      
      // Move pilot marker
      if (replayState.pilotMarker && replayState.pilotCoords.length > 0) {
        const pilotIdx = Math.min(replayState.pathIdx, replayState.pilotCoords.length - 1);
        replayState.pilotMarker.setLatLng(replayState.pilotCoords[pilotIdx]);
      }
      
    }, baseInterval / replayState.speed);
    
    console.log('Replay started:', droneCoords.length, 'drone points,', pilotCoords.length, 'pilot points');
  };
  
  // Create floating replay control bar
  function showReplayControls() {
    // Remove existing if any
    let existing = document.getElementById('replayControlBar');
    if (existing) existing.remove();
    
    const bar = document.createElement('div');
    bar.id = 'replayControlBar';
    bar.style.cssText = `
      position: fixed;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      background: rgba(13, 13, 24, 0.92);
      backdrop-filter: blur(10px);
      border: 1px solid rgba(0, 255, 136, 0.4);
      border-radius: 12px;
      padding: 12px 20px;
      z-index: 10000;
      font-family: 'JetBrains Mono', monospace;
      display: flex;
      align-items: center;
      gap: 15px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
    `;
    
    // Get flight info
    const flightInfo = replayState.flightIdx !== null && replayState.flightIdx !== 'all' 
      ? `Flight ${parseInt(replayState.flightIdx) + 1}` 
      : 'All Flights';
    
    bar.innerHTML = `
      <div style="display:flex;flex-direction:column;gap:2px;">
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="color:#00ff88;font-size:0.7em;text-transform:uppercase;letter-spacing:1px;">REPLAY</span>
          <span style="color:#f0abfc;font-size:0.85em;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${replayState.droneName}</span>
        </div>
        <span style="color:#6b7280;font-size:0.65em;">${flightInfo} | ${replayState.totalPoints} pts</span>
      </div>
      <div style="display:flex;align-items:center;gap:6px;">
        <button id="replayPlayPauseBtn" onclick="pauseReplay()" style="background:rgba(0,255,136,0.15);border:1px solid rgba(0,255,136,0.4);color:#00ff88;padding:8px 12px;border-radius:6px;cursor:pointer;font-size:1em;">⏸</button>
        <button onclick="stopReplay()" style="background:rgba(255,68,68,0.15);border:1px solid rgba(255,68,68,0.4);color:#ff4444;padding:8px 12px;border-radius:6px;cursor:pointer;font-size:1em;">⏹</button>
      </div>
      <div style="display:flex;flex-direction:column;align-items:center;min-width:100px;">
        <span id="replayProgressText" style="color:#00ffd5;font-size:0.75em;">1 / ${replayState.totalPoints}</span>
        <div style="width:100px;height:4px;background:rgba(99,102,241,0.3);border-radius:2px;margin-top:4px;">
          <div id="replayProgressBar" style="width:0%;height:100%;background:#00ff88;border-radius:2px;transition:width 0.1s;"></div>
        </div>
      </div>
      <div style="display:flex;gap:4px;">
        <button onclick="setReplaySpeed(0.5)" style="background:rgba(99,102,241,0.15);border:1px solid rgba(99,102,241,0.4);color:#e0e0e0;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:0.7em;">½x</button>
        <button onclick="setReplaySpeed(1)" style="background:rgba(99,102,241,0.15);border:1px solid rgba(99,102,241,0.4);color:#e0e0e0;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:0.7em;">1x</button>
        <button onclick="setReplaySpeed(2)" style="background:rgba(99,102,241,0.15);border:1px solid rgba(99,102,241,0.4);color:#e0e0e0;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:0.7em;">2x</button>
        <button onclick="setReplaySpeed(5)" style="background:rgba(99,102,241,0.15);border:1px solid rgba(99,102,241,0.4);color:#e0e0e0;padding:4px 8px;border-radius:4px;cursor:pointer;font-size:0.7em;">5x</button>
      </div>
    `;
    
    document.body.appendChild(bar);
  }
  
  // Update replay progress display
  function updateReplayProgress(current, total) {
    const textEl = document.getElementById('replayProgressText');
    const barEl = document.getElementById('replayProgressBar');
    if (textEl) textEl.textContent = current + ' / ' + total;
    if (barEl) barEl.style.width = ((current / total) * 100) + '%';
  }
  
  // Hide replay controls
  function hideReplayControls() {
    const bar = document.getElementById('replayControlBar');
    if (bar) bar.remove();
  }
  
  // Pause replay
  window.pauseReplay = function() {
    replayState.paused = !replayState.paused;
    console.log('Replay', replayState.paused ? 'paused' : 'resumed');
    
    // Update control bar button
    const btn = document.getElementById('replayPlayPauseBtn');
    if (btn) {
      btn.textContent = replayState.paused ? '▶' : '⏸';
    }
  };
  
  // Stop replay
  window.stopReplay = function() {
    if (replayState.intervalId) {
      clearInterval(replayState.intervalId);
      replayState.intervalId = null;
    }
    
    // Hide control bar
    hideReplayControls();
    
    // Remove markers
    if (replayState.marker) {
      map.removeLayer(replayState.marker);
      replayState.marker = null;
    }
    if (replayState.pilotMarker) {
      map.removeLayer(replayState.pilotMarker);
      replayState.pilotMarker = null;
    }
    
    // Remove replay paths
    if (replayState.dronePath) {
      map.removeLayer(replayState.dronePath);
      replayState.dronePath = null;
    }
    if (replayState.pilotPath) {
      map.removeLayer(replayState.pilotPath);
      replayState.pilotPath = null;
    }
    
    // Restore static markers and circles for the drone that was being replayed
    if (replayState.droneId) {
      const drone = historicalDroneData[replayState.droneId];
      if (drone && drone.visible) {
        if (drone.droneStartMarker) drone.droneStartMarker.addTo(map);
        if (drone.droneEndMarker) drone.droneEndMarker.addTo(map);
        if (drone.pilotStartMarker) drone.pilotStartMarker.addTo(map);
        if (drone.pilotEndMarker) drone.pilotEndMarker.addTo(map);
        if (drone.startCircle) drone.startCircle.addTo(map);
        if (drone.endCircle) drone.endCircle.addTo(map);
        if (drone.pilotStartCircle) drone.pilotStartCircle.addTo(map);
        if (drone.pilotEndCircle) drone.pilotEndCircle.addTo(map);
        // Also restore flight paths
        if (drone.flights) {
          drone.flights.forEach(f => {
            if (f.visible !== false) {
              if (f.dronePath) f.dronePath.addTo(map);
              if (f.pilotPath) f.pilotPath.addTo(map);
            }
          });
        }
      }
    }
    
    replayState.active = false;
    replayState.paused = false;
    replayState.pathIdx = 0;
    replayState.droneId = null;
    replayState.droneData = null;
    console.log('Replay stopped');
  };
  
  // Set replay speed
  window.setReplaySpeed = function(speed) {
    replayState.speed = speed;
    console.log('Replay speed set to', speed + 'x');
    
    // Restart interval with new speed if playing
    if (replayState.active && replayState.intervalId) {
      clearInterval(replayState.intervalId);
      const baseInterval = 500;
      replayState.intervalId = setInterval(() => {
        if (replayState.paused) return;
        
        replayState.pathIdx++;
        
        if (replayState.pathIdx >= replayState.maxLength) {
          stopReplay();
          return;
        }
        
        // Move drone marker
        if (replayState.marker && replayState.coords.length > 0) {
          const droneIdx = Math.min(replayState.pathIdx, replayState.coords.length - 1);
          replayState.marker.setLatLng(replayState.coords[droneIdx]);
          updateReplayProgress(droneIdx + 1, replayState.totalPoints);
        }
        
        // Move pilot marker
        if (replayState.pilotMarker && replayState.pilotCoords.length > 0) {
          const pilotIdx = Math.min(replayState.pathIdx, replayState.pilotCoords.length - 1);
          replayState.pilotMarker.setLatLng(replayState.pilotCoords[pilotIdx]);
        }
      }, baseInterval / replayState.speed);
    }
  };
  
  // Update color for historical drone
  window.updateHistoricalColor = function(id, hue) {
    const drone = historicalDroneData[id];
    if (!drone) return;
    
    hue = parseInt(hue);
    const newColor = 'hsl(' + hue + ', 70%, 50%)';
    drone.color = newColor;
    
    // Update main markers
    if (drone.droneStartMarker) drone.droneStartMarker.setIcon(createIcon('🛸', newColor));
    if (drone.droneEndMarker) drone.droneEndMarker.setIcon(createIcon('🛸', newColor));
    if (drone.pilotStartMarker) drone.pilotStartMarker.setIcon(createIcon('👤', newColor));
    if (drone.pilotEndMarker) drone.pilotEndMarker.setIcon(createIcon('👤', newColor));
    
    // Update flight paths
    if (drone.flights) {
      drone.flights.forEach(flight => {
        if (flight.dronePath) flight.dronePath.setStyle({ color: newColor });
        if (flight.pilotPath) flight.pilotPath.setStyle({ color: newColor });
      });
    }
    
    // Update list item
    renderHistoricalDroneList();
  };
  
  // Toggle visibility from popup
  window.toggleHistoricalVisibility = function(id) {
    const drone = historicalDroneData[id];
    if (!drone) return;
    setDroneVisibility(id, !drone.visible);
    updateHistoricalStats();
    renderHistoricalDroneList();
  };
  
  // Zoom to drone from popup
  window.zoomToHistoricalDrone = function(id) {
    const drone = historicalDroneData[id];
    if (!drone) return;
    
    if (drone.droneStartMarker) {
      map.setView(drone.droneStartMarker.getLatLng(), 16);
      drone.droneStartMarker.openPopup();
    } else if (drone.droneEndMarker) {
      map.setView(drone.droneEndMarker.getLatLng(), 16);
    }
  };
  
  function parseAndPlotKML(kmlString, fitBounds = true) {
    const bounds = [];
    
    // First pass: collect all flights per drone
    const droneFlightsRaw = {}; // { id: { mac, color, flights: [ {num, timestamp, droneStart, droneEnd, pilotStart, pilotEnd, dronePath, pilotPath} ] } }
    
    // Use regex to extract all Folder blocks with their content
    const folderRegex = /<Folder>([\s\S]*?)<\/Folder>/g;
    let folderMatch;
    let folderCount = 0;
    
    while ((folderMatch = folderRegex.exec(kmlString)) !== null) {
      folderCount++;
      const folderContent = folderMatch[1];
      
      // Extract folder name
      const nameMatch = folderContent.match(/<name>([^<]+)<\/name>/);
      const folderName = nameMatch ? nameMatch[1] : 'Unknown';
      
      // Extract MAC from folder name
      const macMatch = folderName.match(/([0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2})/i);
      if (!macMatch) continue;
      
      const mac = macMatch[1].toLowerCase();
      const id = mac.replace(/:/g, '');
      
      // Extract flight number
      const flightNumMatch = folderName.match(/Flight\s+(\d+)/i);
      const flightNum = flightNumMatch ? parseInt(flightNumMatch[1]) : 1;
      
      // Extract color from LineStyle or IconStyle
      let color = '#00ff88';
      const colorMatch = folderContent.match(/<color>([a-fA-F0-9]{8})<\/color>/);
      if (colorMatch) {
        const kmlColor = colorMatch[1];
        color = '#' + kmlColor.slice(6, 8) + kmlColor.slice(4, 6) + kmlColor.slice(2, 4);
      }
      
      // Extract timestamp
      const timeMatch = folderName.match(/\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)/);
      const timestamp = timeMatch ? timeMatch[1] : null;
      
      // Initialize drone entry
      if (!droneFlightsRaw[id]) {
        droneFlightsRaw[id] = {
          mac: mac,
          color: color,
          flights: []
        };
      }
      
      // Create flight entry
      const flightEntry = {
        num: flightNum,
        timestamp: timestamp,
        droneStart: null,
        droneEnd: null,
        pilotStart: null,
        pilotEnd: null,
        dronePath: null,
        pilotPath: null,
        visible: true
      };
      
      // Extract all Placemarks
      const placemarkRegex = /<Placemark>([\s\S]*?)<\/Placemark>/g;
      let pmMatch;
      
      while ((pmMatch = placemarkRegex.exec(folderContent)) !== null) {
        const pmContent = pmMatch[1];
        const pmNameMatch = pmContent.match(/<name>([^<]*)<\/name>/);
        const pmName = pmNameMatch ? pmNameMatch[1] : '';
        const nameLower = pmName.toLowerCase();
        const isDrone = nameLower.includes('drone');
        const isPilot = nameLower.includes('pilot');
        const isStart = nameLower.includes('start');
        const isEnd = nameLower.includes('end');
        
        // Check for Point (marker)
        const pointMatch = pmContent.match(/<Point>[\s\S]*?<coordinates>([^<]+)<\/coordinates>[\s\S]*?<\/Point>/);
        if (pointMatch) {
          const coords = pointMatch[1].trim().split(',');
          if (coords.length >= 2) {
            const lng = parseFloat(coords[0]);
            const lat = parseFloat(coords[1]);
            
            if (!isNaN(lat) && !isNaN(lng) && lat !== 0 && lng !== 0) {
              bounds.push([lat, lng]);
              
              if (isPilot) {
                if (isStart) flightEntry.pilotStart = { lat, lng };
                if (isEnd) flightEntry.pilotEnd = { lat, lng };
              } else {
                if (isStart) flightEntry.droneStart = { lat, lng };
                if (isEnd) flightEntry.droneEnd = { lat, lng };
              }
            }
          }
        }
        
        // Check for LineString (path)
        const lineMatch = pmContent.match(/<LineString>[\s\S]*?<coordinates>([^<]+)<\/coordinates>[\s\S]*?<\/LineString>/);
        if (lineMatch) {
          const coordsText = lineMatch[1].trim();
          const coordPairs = coordsText.split(/\s+/).filter(c => c.length > 0);
          const pathCoords = [];
          
          coordPairs.forEach(pair => {
            const parts = pair.split(',');
            if (parts.length >= 2) {
              const lng = parseFloat(parts[0]);
              const lat = parseFloat(parts[1]);
              if (!isNaN(lat) && !isNaN(lng) && lat !== 0 && lng !== 0) {
                pathCoords.push([lat, lng]);
                bounds.push([lat, lng]);
              }
            }
          });
          
          if (pathCoords.length > 1) {
            if (isPilot) {
              flightEntry.pilotPath = pathCoords;
            } else {
              flightEntry.dronePath = pathCoords;
            }
          }
        }
      }
      
      droneFlightsRaw[id].flights.push(flightEntry);
    }
    
    // Second pass: create markers and polylines for each drone
    Object.keys(droneFlightsRaw).forEach(id => {
      const droneRaw = droneFlightsRaw[id];
      const mac = droneRaw.mac;
      
      // Sort flights by number
      droneRaw.flights.sort((a, b) => a.num - b.num);
      
      // Get first start and last end positions
      let firstDroneStart = null;
      let lastDroneEnd = null;
      let firstPilotStart = null;
      let lastPilotEnd = null;
      
      droneRaw.flights.forEach(f => {
        if (f.droneStart && !firstDroneStart) firstDroneStart = f.droneStart;
        if (f.droneEnd) lastDroneEnd = f.droneEnd;
        if (f.pilotStart && !firstPilotStart) firstPilotStart = f.pilotStart;
        if (f.pilotEnd) lastPilotEnd = f.pilotEnd;
      });
      
      // Create historicalDroneData entry
      if (!historicalDroneData[id]) {
        historicalDroneData[id] = {
          name: mac,
          mac: mac,
          alias: aliases[mac] || null,
          color: droneRaw.color,
          visible: true,
          timestamp: droneRaw.flights.length > 0 ? droneRaw.flights[droneRaw.flights.length - 1].timestamp : null,
          flights: [],
          droneStartMarker: null,
          droneEndMarker: null,
          pilotStartMarker: null,
          pilotEndMarker: null
        };
      }
      
      const droneData = historicalDroneData[id];
      
      // Store start/end positions for circles
      droneData.startPos = firstDroneStart;
      droneData.endPos = lastDroneEnd;
      droneData.pilotStartPos = firstPilotStart;
      droneData.pilotEndPos = lastPilotEnd;
      
      // FIRST: Create paths for each flight (so flights array is populated before popup)
      droneRaw.flights.forEach((flight, idx) => {
        const flightObj = {
          num: flight.num,
          timestamp: flight.timestamp,
          visible: true,
          dronePath: null,
          pilotPath: null,
          droneCoords: flight.dronePath || [],
          pilotCoords: flight.pilotPath || []
        };
        
        // Drone path
        if (flight.dronePath) {
          const polyline = L.polyline(flight.dronePath, {
            color: droneData.color,
            weight: 2,
            opacity: 0.8
          }).addTo(map);
          flightObj.dronePath = polyline;
        }
        
        // Pilot path
        if (flight.pilotPath) {
          const polyline = L.polyline(flight.pilotPath, {
            color: droneData.color,
            weight: 2,
            opacity: 0.8,
            dashArray: '5,5'
          }).addTo(map);
          flightObj.pilotPath = polyline;
        }
        
        droneData.flights.push(flightObj);
      });
      
      // THEN: Create markers with popup (now flights array is populated)
      if (firstDroneStart) {
        droneData.droneStartMarker = L.marker([firstDroneStart.lat, firstDroneStart.lng], {
          icon: createIcon('🛸', droneData.color)
        }).bindPopup(generateHistoricalPopup(id, droneData, 'Drone'), {autoPan: true, maxWidth: 280});
        droneData.droneStartMarker.addTo(map);
      }
      
      if (lastDroneEnd && (lastDroneEnd.lat !== firstDroneStart?.lat || lastDroneEnd.lng !== firstDroneStart?.lng)) {
        droneData.droneEndMarker = L.marker([lastDroneEnd.lat, lastDroneEnd.lng], {
          icon: createIcon('🛸', droneData.color)
        }).bindPopup(generateHistoricalPopup(id, droneData, 'Drone'), {autoPan: true, maxWidth: 280});
        droneData.droneEndMarker.addTo(map);
      }
      
      if (firstPilotStart) {
        droneData.pilotStartMarker = L.marker([firstPilotStart.lat, firstPilotStart.lng], {
          icon: createIcon('👤', droneData.color)
        }).bindPopup(generateHistoricalPopup(id, droneData, 'Pilot'), {autoPan: true, maxWidth: 280});
        droneData.pilotStartMarker.addTo(map);
      }
      
      if (lastPilotEnd && (lastPilotEnd.lat !== firstPilotStart?.lat || lastPilotEnd.lng !== firstPilotStart?.lng)) {
        droneData.pilotEndMarker = L.marker([lastPilotEnd.lat, lastPilotEnd.lng], {
          icon: createIcon('👤', droneData.color)
        }).bindPopup(generateHistoricalPopup(id, droneData, 'Pilot'), {autoPan: true, maxWidth: 280});
        droneData.pilotEndMarker.addTo(map);
      }
      
      // Create START circle (green) and END circle (red) for visual identification
      if (firstDroneStart) {
        droneData.startCircle = L.circle([firstDroneStart.lat, firstDroneStart.lng], {
          radius: 15,
          color: '#00ff88',
          fillColor: '#00ff88',
          fillOpacity: 0.3,
          weight: 2
        }).addTo(map);
      }
      
      if (lastDroneEnd) {
        droneData.endCircle = L.circle([lastDroneEnd.lat, lastDroneEnd.lng], {
          radius: 15,
          color: '#ff4444',
          fillColor: '#ff4444',
          fillOpacity: 0.3,
          weight: 2
        }).addTo(map);
      }
      
      // Pilot start/end circles (smaller, dashed)
      if (firstPilotStart) {
        droneData.pilotStartCircle = L.circle([firstPilotStart.lat, firstPilotStart.lng], {
          radius: 12,
          color: '#00ff88',
          fillColor: '#00ff88',
          fillOpacity: 0.2,
          weight: 1,
          dashArray: '4,4'
        }).addTo(map);
      }
      
      if (lastPilotEnd) {
        droneData.pilotEndCircle = L.circle([lastPilotEnd.lat, lastPilotEnd.lng], {
          radius: 12,
          color: '#ff4444',
          fillColor: '#ff4444',
          fillOpacity: 0.2,
          weight: 1,
          dashArray: '4,4'
        }).addTo(map);
      }
    });
    
    // Summary
    const droneIds = Object.keys(historicalDroneData);
    console.log('=== KML PARSE DONE ===');
    console.log('Folders processed:', folderCount);
    console.log('Unique drones:', droneIds.length);
    droneIds.forEach(id => {
      const d = historicalDroneData[id];
      console.log(' -', d.mac, ':', d.flights.length, 'flights');
    });
    
    // Fit bounds
    if (fitBounds && bounds.length > 0) {
      map.fitBounds(bounds, { padding: [50, 50] });
    }
    
    // Apply active filter
    if (hideActiveDrones) {
      applyActiveFilter();
    }
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js')
      .then(reg => console.log('Service Worker registered', reg))
      .catch(err => console.error('Service Worker registration failed', err));
  }
</script>
</body>
</html>
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
    new_selected_ports = {}
    for i in range(1, 4):
        port = request.form.get(f'port{i}')
        if port:
            new_selected_ports[f'port{i}'] = port

    # Handle webhook URL setting
    webhook_url = request.form.get('webhook_url', '').strip()
    try:
        if webhook_url and not webhook_url.startswith(('http://', 'https://')):
            logger.warning(f"Invalid webhook URL format: {webhook_url}")
        else:
            set_server_webhook_url(webhook_url)
            if webhook_url:
                logger.info(f"Webhook URL updated to: {webhook_url}")
            else:
                logger.info("Webhook URL cleared")
    except Exception as e:
        logger.error(f"Error setting webhook URL: {e}")

    # Close connections to ports that are no longer selected
    with serial_objs_lock:
        for port_key, port_device in SELECTED_PORTS.items():
            if port_key not in new_selected_ports or new_selected_ports[port_key] != port_device:
                # This port is no longer selected or changed, close its connection
                if port_device in serial_objs:
                    try:
                        ser = serial_objs[port_device]
                        if ser and ser.is_open:
                            ser.close()
                            logger.info(f"Closed serial connection to {port_device}")
                    except Exception as e:
                        logger.error(f"Error closing serial connection to {port_device}: {e}")
                    finally:
                        serial_objs.pop(port_device, None)
                        serial_connected_status[port_device] = False
    
    # Update selected ports
    SELECTED_PORTS = new_selected_ports

    # Save selected ports for auto-connection on restart
    save_selected_ports()

    # Start serial-reader threads ONLY for newly selected ports
    for port in SELECTED_PORTS.values():
        # Only start thread if port is not already connected
        if not serial_connected_status.get(port, False):
            serial_connected_status[port] = False
            start_serial_thread(port)
            logger.info(f"Started new serial thread for {port}")
        else:
            logger.debug(f"Port {port} already connected, skipping thread creation")
    
    # Send watchdog reset to each connected microcontroller over USB
    time.sleep(1)  # Give new connections time to establish
    with serial_objs_lock:
        for port, ser in serial_objs.items():
            try:
                if ser and ser.is_open:
                    ser.write(b'WATCHDOG_RESET\n')
                    logger.debug(f"Sent watchdog reset to {port}")
            except Exception as e:
                logger.error(f"Failed to send watchdog reset to {port}: {e}")

    # Redirect to main page
    return redirect(url_for('index'))


# ----------------------
# ASCII art blocks
# ----------------------
BOTTOM_ASCII = r"""
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⣄⣠⣀⡀⣀⣠⣤⣤⣤⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣄⢠⣠⣼⣿⣿⣿⣟⣿⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⠀⢠⣤⣦⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠰⢦⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣟⣾⣿⣽⣿⣿⣅⠈⠉⠻⣿⣿⣿⣿⣿⡿⠇⠀⠀⠀⠀⠉⠀⠀⠀⠀⠀⢀⡶⠒⢉⡀⢠⣤⣶⣶⣿⣷⣆⣀⡀⠀⢲⣖⠒⠀⠀⠀⠀⠀⠀⠀
⢀⣤⣾⣶⣦⣤⣤⣶⣿⣿⣿⣿⣿⣿⣽⡿⠻⣷⣀⠀⢻⣿⣿⣿⡿⠟⠀⠀⠀⠀⠀⠀⣤⣶⣶⣤⣀⣀⣬⣷⣦⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣤⣦⣼⣀⠀
⠈⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠛⠓⣿⣿⠟⠁⠘⣿⡟⠁⠀⠘⠛⠁⠀⠀⢠⣾⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠏⠙⠁
⠀⠀⠸⠟⠋⠀⠀⠙⣿⣿⣿⣿⣿⣿⣷⣦⡄⣿⣿⣿⣆⠀⠀⠀⠀⠀⠀⠀⠀⣼⣆⢘⣿⣯⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡉⠉⢱⡿⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⡿⠦⠀⠀⠀⠀⠀⠀⠀⠙⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⡗⠀⠈⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣿⣿⣿⣿⣿⣿⣿⣿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⢿⣿⣉⣿⡿⢿⢷⣾⣾⣿⣞⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠋⣠⠟⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⣿⠿⠿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⣾⣿⣿⣷⣦⣶⣦⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠈⠛⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠻⣿⣤⡖⠛⠶⠤⡀⠀⠀⠀⠀⠀⠀⠀⢰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠁⠙⣿⣿⠿⢻⣿⣿⡿⠋⢩⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠧⣤⣦⣤⣄⡀⠀⠀⠀⠀⠀⠘⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⠘⣧⠀⠈⣹⡻⠇⢀⣿⡆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣤⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢽⣿⣿⣿⣿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣷⣴⣿⣷⢲⣦⣤⡀⢀⡀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢿⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⣷⢀⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠂⠛⣆⣤⡜⣟⠋⠙⠂⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿⣿⠉⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣤⣾⣿⣿⣿⣿⣿⣆⠀⠰⠄⠀⠉⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⣿⣿⡿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⡿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣿⠿⠿⣿⣿⣿⠇⠀⠀⢀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣿⡿⠛⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠁⠀⠀⠀
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
    # Load previously saved ports and attempt auto-connection
    load_selected_ports()
    
    # If no ports are currently selected, try to auto-connect to saved ports
    if len(SELECTED_PORTS) == 0:
        return redirect(url_for('select_ports_get'))
    
    # If we have saved ports but they're not connected, try auto-connecting
    if not any(serial_connected_status.get(port, False) for port in SELECTED_PORTS.values()):
        auto_connected = auto_connect_to_saved_ports()
        if not auto_connected:
            # If auto-connection failed, redirect to port selection
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
        tracked_pairs[mac]['status'] = 'active'  # Mark as active when manually reactivated
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

# New endpoint to get currently selected ports
@app.route('/api/selected_ports', methods=['GET'])
def api_selected_ports():
    return jsonify({"selected_ports": SELECTED_PORTS})

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
    connection_attempts = 0
    max_connection_attempts = 5
    data_received_count = 0
    last_data_time = time.time()
    
    logger.info(f"Starting serial reader thread for port: {port}")
    
    while not SHUTDOWN_EVENT.is_set():
        # Try to open or re-open the serial port
        if ser is None or not getattr(ser, 'is_open', False):
            try:
                ser = serial.Serial(port, BAUD_RATE, timeout=1)
                serial_connected_status[port] = True
                connection_attempts = 0  # Reset counter on successful connection
                logger.info(f"Opened serial port {port} at {BAUD_RATE} baud.")
                with serial_objs_lock:
                    serial_objs[port] = ser
                    
                # Broadcast the updated status immediately
                emit_serial_status()
                    
                # Send a test command to wake up the device (reduce frequency to prevent disconnects)
                try:
                    # Only send watchdog reset once, not continuously
                    if connection_attempts == 0:  # Only on first successful connection
                        time.sleep(0.5)  # Small delay before sending command
                        ser.write(b'WATCHDOG_RESET\n')
                        logger.debug(f"Sent initial watchdog reset to {port}")
                except Exception as e:
                    logger.warning(f"Failed to send watchdog reset to {port}: {e}")
                    
            except Exception as e:
                serial_connected_status[port] = False
                connection_attempts += 1
                logger.error(f"Error opening serial port {port} (attempt {connection_attempts}): {e}")
                
                # Broadcast the updated status immediately
                emit_serial_status()
                
                # If we've failed too many times, wait longer before retrying
                if connection_attempts >= max_connection_attempts:
                    logger.warning(f"Max connection attempts reached for {port}, waiting 30 seconds...")
                    time.sleep(30)
                    connection_attempts = 0  # Reset counter
                else:
                    time.sleep(1)
                continue

        try:
            # Always try to read data, don't rely only on in_waiting
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            
            if line:
                data_received_count += 1
                last_data_time = time.time()
                
                # Log all received data for debugging (limit length to avoid spam)
                if data_received_count <= 10 or data_received_count % 50 == 0:
                    logger.info(f"Data from {port} (#{data_received_count}): {line[:200]}")
                
                # JSON extraction and detection handling...
                json_str = line
                if '{' in line:
                    json_str = line[line.find('{'):]
                    
                try:
                    detection = json.loads(json_str)
                    logger.debug(f"Parsed JSON from {port}: {detection}")
                    
                    # MAC tracking logic...
                    if 'mac' in detection:
                        last_mac_by_port[port] = detection['mac']
                        logger.debug(f"Found MAC in detection: {detection['mac']}")
                    elif port in last_mac_by_port:
                        detection['mac'] = last_mac_by_port[port]
                        logger.debug(f"Using cached MAC for {port}: {detection['mac']}")
                    else:
                        logger.warning(f"No MAC found in detection from {port}: {detection}")
                    
                    # Skip heartbeat messages
                    if 'heartbeat' in detection:
                        logger.debug(f"Skipping heartbeat from {port}")
                        continue
                    
                    # Skip status messages without detection data
                    if not any(key in detection for key in ['mac', 'drone_lat', 'pilot_lat', 'basic_id', 'remote_id']):
                        logger.debug(f"Skipping non-detection message from {port}: {detection}")
                        continue
                        
                    # Normalize remote_id field
                    if 'remote_id' in detection and 'basic_id' not in detection:
                        detection['basic_id'] = detection['remote_id']
                    
                    # Add port information for debugging
                    detection['source_port'] = port
                    
                    # Process the detection
                    logger.info(f"Processing detection from {port}: MAC={detection.get('mac', 'N/A')}, "
                              f"RSSI={detection.get('rssi', 'N/A')}, "
                              f"Drone GPS=({detection.get('drone_lat', 'N/A')}, {detection.get('drone_long', 'N/A')})")
                    
                    update_detection(detection)
                    
                    # Log detection in headless mode
                    if HEADLESS_MODE and detection.get('mac'):
                        logger.info(f"Detection from {port}: MAC {detection['mac']}, "
                                   f"RSSI {detection.get('rssi', 'N/A')}")
                        
                except json.JSONDecodeError as e:
                    # Log non-JSON data for debugging
                    logger.debug(f"Non-JSON data from {port}: {line[:100]}")
                    continue
            else:
                # Short sleep when no data
                time.sleep(0.1)
                
                # Log if we haven't received data in a while
                if time.time() - last_data_time > 30:  # 30 seconds
                    # logger.warning(f"No data received from {port} for {int(time.time() - last_data_time)} seconds")
                    last_data_time = time.time()  # Reset timer to avoid spam
                
        except (serial.SerialException, OSError) as e:
            serial_connected_status[port] = False
            logger.error(f"SerialException/OSError on {port}: {e}")
            
            # Broadcast the updated status immediately
            emit_serial_status()
            
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
            logger.error(f"Unexpected error on {port}: {e}")
            
            # Broadcast the updated status immediately
            emit_serial_status()
            
            try:
                if ser and ser.is_open:
                    ser.close()
            except Exception:
                pass
            ser = None
            with serial_objs_lock:
                serial_objs.pop(port, None)
            time.sleep(1)
    
    logger.info(f"Serial reader thread for {port} shutting down. Total data packets received: {data_received_count}")

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
    # regenerate cumulative KML to include latest detections
    generate_cumulative_kml()
    return send_file(
        CUMULATIVE_KML_FILENAME,
        mimetype='application/vnd.google-earth.kml+xml',
        as_attachment=True,
        download_name='cumulative.kml'
    )

# ----------------------
# Historical KML API Endpoints
# ----------------------
@app.route('/api/kml/session')
def api_kml_session():
    """Return session KML content for map plotting"""
    generate_kml()
    try:
        with open(KML_FILENAME, 'r') as f:
            return jsonify({'status': 'ok', 'kml': f.read(), 'filename': os.path.basename(KML_FILENAME)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/kml/cumulative')
def api_kml_cumulative():
    """Return cumulative KML content for map plotting"""
    generate_cumulative_kml()
    try:
        with open(CUMULATIVE_KML_FILENAME, 'r') as f:
            return jsonify({'status': 'ok', 'kml': f.read(), 'filename': os.path.basename(CUMULATIVE_KML_FILENAME)})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ----------------------
# Startup Auto-Connection
# ----------------------
def startup_auto_connect():
    """
    Load saved ports and attempt auto-connection on startup.
    Enhanced version with better logging and headless support.
    """
    logger.info("=== DRONE MAPPER STARTUP ===")
    logger.info("Loading previously saved ports...")
    load_selected_ports()
    
    # Load webhook URL
    logger.info("Loading previously saved webhook URL...")
    # load_webhook_url()  # Temporarily disabled - will be called later
    
    if SELECTED_PORTS:
        logger.info(f"Found saved ports: {list(SELECTED_PORTS.values())}")
        auto_connected = auto_connect_to_saved_ports()
        if auto_connected:
            logger.info("Auto-connection successful! Mapping is now active.")
            if HEADLESS_MODE:
                logger.info("Running in headless mode - mapping will continue automatically")
        else:
            logger.warning("Auto-connection failed. Port selection will be required.")
            if HEADLESS_MODE:
                logger.info("Headless mode: Will monitor for port availability...")
    else:
        logger.info("No previously saved ports found.")
        if HEADLESS_MODE:
            logger.info("Headless mode: Will monitor for any available ports...")
    
    # Start monitoring and status logging
    start_port_monitoring()
    start_status_logging()
    start_websocket_broadcaster()
    
    logger.info("=== STARTUP COMPLETE ===")

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description='Drone Detection Mapper - Automatically detect and map drone activity',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python mapper.py                    # Start with web interface
  python mapper.py --headless         # Run in headless mode (no web interface)
  python mapper.py --no-auto-start    # Disable automatic port connection
  python mapper.py --port-interval 5  # Check for ports every 5 seconds
  python mapper.py --debug            # Enable debug logging
        """
    )
    
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run in headless mode without web interface'
    )
    
    parser.add_argument(
        '--no-auto-start',
        action='store_true',
        help='Disable automatic port connection and monitoring'
    )
    
    parser.add_argument(
        '--port-interval',
        type=int,
        default=10,
        help='Port monitoring interval in seconds (default: 10)'
    )
    
    parser.add_argument(
        '--web-port',
        type=int,
        default=5000,
        help='Web interface port (default: 5000)'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    
    return parser.parse_args()

def main():
    """Main function with enhanced startup and configuration"""
    global HEADLESS_MODE, AUTO_START_ENABLED, PORT_MONITOR_INTERVAL
    
    # Parse command line arguments
    args = parse_arguments()
    
    # Configure global settings
    HEADLESS_MODE = args.headless
    AUTO_START_ENABLED = not args.no_auto_start
    PORT_MONITOR_INTERVAL = args.port_interval
    
    # Configure logging level
    if args.debug:
        set_debug_mode(True)
    
    # Load webhook URL (now that all functions are defined)
    load_webhook_url()
    
    # Clean session state to prevent lingering from prior sessions
    global backend_seen_drones, backend_previous_active, backend_alerted_no_gps
    global tracked_pairs, detection_history
    backend_seen_drones.clear()
    backend_previous_active.clear()
    backend_alerted_no_gps.clear()
    tracked_pairs.clear()
    detection_history.clear()
    logger.info("Session state cleared - fresh session initialized")
    
    logger.info(f"Starting Drone Mapper...")
    logger.info(f"Headless mode: {HEADLESS_MODE}")
    logger.info(f"Auto-start enabled: {AUTO_START_ENABLED}")
    logger.info(f"Port monitoring interval: {PORT_MONITOR_INTERVAL}s")
    
    # Perform startup auto-connection
    startup_auto_connect()
    
    # Start cleanup timer to prevent memory leaks
    start_cleanup_timer()
    
    if HEADLESS_MODE:
        logger.info("Running in headless mode - press Ctrl+C to stop")
        try:
            # In headless mode, just wait for shutdown signal
            while not SHUTDOWN_EVENT.is_set():
                SHUTDOWN_EVENT.wait(60)  # Check every minute
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        finally:
            signal_handler(signal.SIGTERM, None)
    else:
        logger.info(f"Starting web interface on port {args.web_port}")
        logger.info(f"Access the interface at: http://localhost:{args.web_port}")
        try:
            # Use SocketIO to run the app
            socketio.run(app, host='0.0.0.0', port=args.web_port, debug=False)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        finally:
            signal_handler(signal.SIGTERM, None)


@app.route('/api/diagnostics', methods=['GET'])
def api_diagnostics():
    """Provide detailed diagnostic information for troubleshooting"""
    diagnostics = {
        "timestamp": datetime.now().isoformat(),
        "selected_ports": SELECTED_PORTS,
        "serial_status": serial_connected_status,
        "tracked_pairs": len(tracked_pairs),
        "detection_history_count": len(detection_history),
        "last_mac_by_port": last_mac_by_port,
        "available_ports": [{"device": p.device, "description": p.description} 
                           for p in serial.tools.list_ports.comports()],
        "active_serial_objects": list(serial_objs.keys()) if serial_objs else [],
        "headless_mode": HEADLESS_MODE,
        "auto_start_enabled": AUTO_START_ENABLED,
        "shutdown_event_set": SHUTDOWN_EVENT.is_set(),
        "debug_mode": DEBUG_MODE
    }
    
    # Add recent detections if any exist
    if detection_history:
        recent_detections = detection_history[-5:]  # Last 5 detections
        diagnostics["recent_detections"] = [
            {
                "mac": d.get("mac", "N/A"),
                "timestamp": d.get("last_update", "N/A"),
                "source_port": d.get("source_port", "N/A"),
                "drone_coords": f"({d.get('drone_lat', 'N/A')}, {d.get('drone_long', 'N/A')})",
                "rssi": d.get("rssi", "N/A")
            }
            for d in recent_detections
        ]
    else:
        diagnostics["recent_detections"] = []
    
    return jsonify(diagnostics)

@app.route('/api/debug_mode', methods=['POST'])
def api_toggle_debug():
    """Toggle debug mode on/off"""
    data = request.get_json() or {}
    enabled = data.get('enabled', not DEBUG_MODE)
    set_debug_mode(enabled)
    return jsonify({"debug_mode": DEBUG_MODE, "message": f"Debug mode {'enabled' if DEBUG_MODE else 'disabled'}"})

@app.route('/api/send_command', methods=['POST'])
def api_send_command():
    """Send a test command to serial ports for debugging"""
    data = request.get_json()
    command = data.get('command', 'WATCHDOG_RESET')
    port = data.get('port')  # Optional: send to specific port
    
    results = {}
    
    with serial_objs_lock:
        ports_to_send = [port] if port and port in serial_objs else list(serial_objs.keys())
        
        for p in ports_to_send:
            try:
                ser = serial_objs.get(p)
                if ser and ser.is_open:
                    ser.write(f'{command}\n'.encode())
                    results[p] = "Command sent successfully"
                    logger.info(f"Sent command '{command}' to {p}")
                else:
                    results[p] = "Port not open or not available"
            except Exception as e:
                results[p] = f"Error: {str(e)}"
                logger.error(f"Failed to send command to {p}: {e}")
    
    return jsonify({"command": command, "results": results})

@app.route('/api/upload_aliases', methods=['POST'])
def api_upload_aliases():
    """Upload aliases to all connected ESP32-S3 devices. If aliases are provided in request, replace existing aliases."""
    global ALIASES
    
    try:
        # Check if new aliases are provided in the request
        if request.is_json:
            data = request.get_json()
            new_aliases = data.get('aliases')
            
            if new_aliases is not None:
                # Validate it's a dictionary
                if not isinstance(new_aliases, dict):
                    return jsonify({
                        "status": "error",
                        "message": "Aliases must be a JSON object/dictionary"
                    }), 400
                
                # Wipe old aliases and replace with new ones
                ALIASES = new_aliases
                logger.info(f"Replaced aliases with {len(ALIASES)} new aliases from file")
        
        # Ensure aliases are saved to disk
        save_aliases()
        
        # Prepare aliases JSON
        aliases_json = json.dumps(ALIASES)
        command = f"UPLOAD_ALIASES:{aliases_json}\n"
        
        results = {}
        success_count = 0
        error_count = 0
        
        with serial_objs_lock:
            if not serial_objs:
                return jsonify({
                    "status": "error",
                    "message": "No serial ports connected. Please select ports first."
                }), 400
            
            for port, ser in serial_objs.items():
                try:
                    if ser and ser.is_open:
                        # Send command with aliases
                        ser.write(command.encode('utf-8'))
                        results[port] = "Aliases sent successfully"
                        success_count += 1
                        logger.info(f"Sent aliases to {port}: {len(ALIASES)} aliases")
                    else:
                        results[port] = "Port not open"
                        error_count += 1
                except Exception as e:
                    results[port] = f"Error: {str(e)}"
                    error_count += 1
                    logger.error(f"Failed to send aliases to {port}: {e}")
        
        if success_count > 0:
            message = f"Uploaded {len(ALIASES)} aliases to {success_count} device(s)"
            if error_count > 0:
                message += f" ({error_count} failed)"
            return jsonify({
                "status": "ok",
                "message": message,
                "results": results,
                "alias_count": len(ALIASES)
            })
        else:
            return jsonify({
                "status": "error",
                "message": "Failed to upload aliases to any device",
                "results": results
            }), 500
            
    except Exception as e:
        logger.error(f"Error uploading aliases: {e}")
        return jsonify({
            "status": "error",
            "message": f"Server error: {str(e)}"
        }), 500

# --- SocketIO connection event ---
@socketio.on('connect')
def handle_connect():
    logger.debug("Client connected via WebSocket")
    # Send current state to newly connected client
    emit_detections()
    emit_aliases()
    emit_serial_status()
    emit_paths()
    emit_cumulative_log()
    emit_faa_cache()

# Helper functions to emit all real-time data

def emit_serial_status():
    try:
        socketio.emit('serial_status', serial_connected_status, )
    except Exception as e:
        logger.debug(f"Error emitting serial status: {e}")
        pass  # Ignore if no clients connected or serialization error

def emit_aliases():
    try:
        socketio.emit('aliases', ALIASES, )
    except Exception as e:
        logger.debug(f"Error emitting aliases: {e}")

def emit_detections():
    try:
        # Convert tracked_pairs to a JSON-serializable format
        serializable_pairs = {}
        for key, value in tracked_pairs.items():
            # Ensure key is a string
            str_key = str(key)
            # Ensure value is JSON-serializable
            if isinstance(value, dict):
                serializable_pairs[str_key] = value
            else:
                serializable_pairs[str_key] = str(value)
        socketio.emit('detections', serializable_pairs, )
    except Exception as e:
        logger.debug(f"Error emitting detections: {e}")

def emit_paths():
    try:
        socketio.emit('paths', get_paths_for_emit(), )
    except Exception as e:
        logger.debug(f"Error emitting paths: {e}")

def emit_cumulative_log():
    try:
        socketio.emit('cumulative_log', get_cumulative_log_for_emit(), )
    except Exception as e:
        logger.debug(f"Error emitting cumulative log: {e}")

def emit_faa_cache():
    try:
        # Convert FAA_CACHE to JSON-serializable format
        serializable_cache = {}
        for key, value in FAA_CACHE.items():
            # Convert tuple keys to strings
            str_key = str(key) if isinstance(key, tuple) else key
            serializable_cache[str_key] = value
        socketio.emit('faa_cache', serializable_cache, )
    except Exception as e:
        logger.debug(f"Error emitting FAA cache: {e}")

# Helper to get paths for emit

def get_paths_for_emit():
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
    return {"dronePaths": drone_paths, "pilotPaths": pilot_paths}

# Helper to get cumulative log for emit

def get_cumulative_log_for_emit():
    # Read the cumulative CSV and return as a list of dicts
    try:
        if os.path.exists(CUMULATIVE_CSV_FILENAME):
            with open(CUMULATIVE_CSV_FILENAME, 'r', newline='') as csvfile:
                reader = csv.DictReader(csvfile)
                return list(reader)
        else:
            return []
    except Exception as e:
        logger.error(f"Error reading cumulative log: {e}")
        return []


@app.route('/api/set_webhook_url', methods=['POST'])
def api_set_webhook_url():
    try:
        # Check if request has JSON data
        if not request.is_json:
            return jsonify({"status": "error", "message": "Request must be JSON"}), 400
        
        data = request.get_json()
        
        # Handle case where data is None
        if data is None:
            return jsonify({"status": "error", "message": "Invalid JSON data"}), 400
        
        # Get webhook URL and handle None case
        url = data.get('webhook_url', '')
        if url is None:
            url = ''
        else:
            url = str(url).strip()
        
        # Validate URL format if not empty
        if url and not url.startswith(('http://', 'https://')):
            return jsonify({"status": "error", "message": "Invalid webhook URL - must start with http:// or https://"}), 400
        
        # Additional URL validation for common issues
        if url:
            # Check for localhost variations that might not work
            if 'localhost' in url and not url.startswith('http://localhost'):
                return jsonify({"status": "error", "message": "For localhost URLs, please use http://localhost"}), 400
        
        # Set the webhook URL
        set_server_webhook_url(url)
        
        # Log the update
        if url:
            logger.info(f"Webhook URL updated to: {url}")
        else:
            logger.info("Webhook URL cleared")
        
        return jsonify({"status": "ok", "webhook_url": WEBHOOK_URL})
        
    except Exception as e:
        logger.error(f"Error setting webhook URL: {e}")
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500

@app.route('/api/get_webhook_url', methods=['GET'])
def api_get_webhook_url():
    """Get the current webhook URL"""
    try:
        return jsonify({"status": "ok", "webhook_url": WEBHOOK_URL or ""})
    except Exception as e:
        logger.error(f"Error getting webhook URL: {e}")
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500

@app.route('/api/webhook_url', methods=['GET'])
def api_webhook_url():
    return jsonify({"webhook_url": WEBHOOK_URL or ""})

# --- Webhook URL Persistence ---
WEBHOOK_URL_FILE = os.path.join(BASE_DIR, "webhook_url.json")

def save_webhook_url():
    """Save the current webhook URL to disk"""
    global WEBHOOK_URL
    try:
        with open(WEBHOOK_URL_FILE, "w") as f:
            json.dump({"webhook_url": WEBHOOK_URL}, f)
        logger.debug(f"Webhook URL saved to {WEBHOOK_URL_FILE}")
    except Exception as e:
        logger.error(f"Error saving webhook URL: {e}")

def load_webhook_url():
    """Load the webhook URL from disk on startup"""
    global WEBHOOK_URL
    if os.path.exists(WEBHOOK_URL_FILE):
        try:
            with open(WEBHOOK_URL_FILE, "r") as f:
                data = json.load(f)
                WEBHOOK_URL = data.get("webhook_url", None)
                if WEBHOOK_URL:
                    logger.info(f"Loaded saved webhook URL: {WEBHOOK_URL}")
                else:
                    logger.info("No webhook URL found in saved file")
        except Exception as e:
            logger.error(f"Error loading webhook URL: {e}")
            WEBHOOK_URL = None
    else:
        logger.info("No saved webhook URL file found")
        WEBHOOK_URL = None

def auto_connect_to_saved_ports():
    """
    Check if any previously saved ports are available and auto-connect to them.
    Returns True if at least one port was connected, False otherwise.
    """
    global SELECTED_PORTS
    
    if not SELECTED_PORTS:
        logger.info("No saved ports found for auto-connection")
        return False
    
    # Get currently available ports
    available_ports = {p.device for p in serial.tools.list_ports.comports()}
    logger.debug(f"Available ports: {available_ports}")
    
    # Check which saved ports are still available
    available_saved_ports = {}
    for port_key, port_device in SELECTED_PORTS.items():
        if port_device in available_ports:
            available_saved_ports[port_key] = port_device
    
    if not available_saved_ports:
        logger.warning("No previously used ports are currently available")
        return False
    
    logger.info(f"Auto-connecting to previously used ports: {list(available_saved_ports.values())}")
    
    # Update SELECTED_PORTS to only include available ports
    SELECTED_PORTS = available_saved_ports
    
    # Start serial threads for available ports
    for port in SELECTED_PORTS.values():
        serial_connected_status[port] = False
        start_serial_thread(port)
        logger.info(f"Started serial thread for port: {port}")
    
    # Send watchdog reset to each microcontroller over USB
    time.sleep(2)  # Give threads time to establish connections
    with serial_objs_lock:
        for port, ser in serial_objs.items():
            try:
                if ser and ser.is_open:
                    ser.write(b'WATCHDOG_RESET\n')
                    logger.debug(f"Sent watchdog reset to {port}")
            except Exception as e:
                logger.error(f"Failed to send watchdog reset to {port}: {e}")
    
    return True

# ----------------------
# Webhook Functions (moved here to be available before update_detection)
# ----------------------

if __name__ == '__main__':
    main()
