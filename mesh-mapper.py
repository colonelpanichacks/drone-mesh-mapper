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
import sqlite3
import socket
import math
import uuid
import sys
import argparse
from datetime import datetime, timedelta
from typing import Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, redirect, url_for, render_template, render_template_string, send_file, make_response
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


# Server-side webhook URLs (set via API).
# WEBHOOK_URL          → fired on drone-detection events + (fallback) geofence alerts
# GEOFENCE_WEBHOOK_URL → optional dedicated URL that overrides WEBHOOK_URL for
#                        geofence enter/exit events. When unset, geofence alerts
#                        fall back to WEBHOOK_URL.
WEBHOOK_URL = None
GEOFENCE_WEBHOOK_URL = None

def set_server_webhook_url(url: str):
    global WEBHOOK_URL
    WEBHOOK_URL = url
    save_webhook_url()  # Save to disk whenever URL is updated

def set_geofence_webhook_url(url: str):
    global GEOFENCE_WEBHOOK_URL
    GEOFENCE_WEBHOOK_URL = url
    save_webhook_url()

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # Enable Socket.IO

# ----------------------
# Offline tiles (MBTiles) + on-demand caching
# ----------------------
TILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiles')
os.makedirs(TILES_DIR, exist_ok=True)

# Live tile sources we know how to pre-cache. Keys mirror the JS basemap ids.
TILE_SOURCES = {
    'osmStandard':      {'url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',                                           'maxZoom': 19, 'fmt': 'png',  'attrib': '© OpenStreetMap'},
    'osmHumanitarian':  {'url': 'https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png',                                       'maxZoom': 20, 'fmt': 'png',  'attrib': '© HOT OSM'},
    'cartoPositron':    {'url': 'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',                                 'maxZoom': 20, 'fmt': 'png',  'attrib': '© CARTO'},
    'cartoDarkMatter':  {'url': 'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png',                                  'maxZoom': 20, 'fmt': 'png',  'attrib': '© CARTO'},
    'esriWorldImagery': {'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',         'maxZoom': 19, 'fmt': 'jpg',  'attrib': '© Esri'},
    'esriWorldTopo':    {'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',        'maxZoom': 19, 'fmt': 'jpg',  'attrib': '© Esri'},
    'esriDarkGray':     {'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', 'maxZoom': 16, 'fmt': 'png', 'attrib': '© Esri'},
    'openTopoMap':      {'url': 'https://a.tile.opentopomap.org/{z}/{x}/{y}.png',                                            'maxZoom': 17, 'fmt': 'png',  'attrib': '© OpenTopoMap'},
}

# Per-mbtiles connection pool. SQLite connections are reused across requests with
# check_same_thread=False; reads are concurrent, writes are serialized via _mbtiles_locks.
_mbtiles_conns: dict = {}
_mbtiles_locks: dict = {}
_mbtiles_pool_lock = threading.Lock()

_NAME_MAX_LEN = 64


def _mbtiles_path(name: str) -> str:
    """Resolve a safe path inside TILES_DIR for the given mbtiles name.
    Rejects empty, overly long, or path-traversal-shaped names.
    Whitelist: ASCII alphanumeric, dash, underscore."""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = name.strip()
    if not name or len(name) > _NAME_MAX_LEN:
        raise ValueError(f"name must be 1-{_NAME_MAX_LEN} chars")
    if not all(c.isalnum() or c in ('-', '_') for c in name):
        raise ValueError("name must be alphanumeric / dash / underscore only")
    return os.path.join(TILES_DIR, f"{name}.mbtiles")


def _validate_bbox(bbox) -> tuple:
    """Validate a [west, south, east, north] bbox. Returns the same tuple,
    clamping wildly out-of-range values rather than rejecting them so panning
    near the poles or past the antimeridian still works.

    Antimeridian crossing (west > east) is allowed and downstream code in
    `_bbox_to_radius_nm` handles it.
    """
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox must be a 4-element list [west, south, east, north]")
    try:
        w, s, e, n = (float(x) for x in bbox)
    except (TypeError, ValueError):
        raise ValueError("bbox values must be numeric")
    for v in (w, s, e, n):
        if math.isnan(v) or math.isinf(v):
            raise ValueError("bbox must contain finite numbers")
    # Wrap longitudes into [-180, 180] so a viewport that panned past ±180
    # (Leaflet's worldCopyJump means Leaflet itself never reports this, but
    # padded bboxes can drift just past) gets normalized.
    def _wrap_lon(x: float) -> float:
        while x > 180.0:  x -= 360.0
        while x < -180.0: x += 360.0
        return x
    w = _wrap_lon(w); e = _wrap_lon(e)
    # Clamp latitudes to the Web-Mercator-safe band.
    s = max(-85.06, min(85.06, s))
    n = max(-85.06, min(85.06, n))
    if s > n:
        s, n = n, s
    # NOTE: do NOT reject when w > e — that's a valid bbox that wraps the
    # antimeridian. Downstream consumers (adsb.lol uses center+radius, not
    # bbox; same for adsb.fi / airplanes.live / OpenSky) handle it.
    if abs(e - w) < 1e-6 or (n - s) < 1e-6:
        raise ValueError("bbox area is too small")
    return (w, s, e, n)


def _validate_zoom_range(zmin, zmax) -> tuple:
    try:
        zmin = int(zmin); zmax = int(zmax)
    except (TypeError, ValueError):
        raise ValueError("zoom values must be integers")
    if not (0 <= zmin <= 22) or not (0 <= zmax <= 22):
        raise ValueError("zoom must be in [0, 22]")
    if zmax < zmin:
        raise ValueError("zMax must be >= zMin")
    return zmin, zmax

def _mbtiles_get(name: str, create: bool = False):
    """Return (conn, write_lock) for the given mbtiles, creating the schema on demand.
    Returns (None, None) if the file doesn't exist and create=False.
    Raises ValueError on bad name; sqlite3.Error if the file is corrupt."""
    path = _mbtiles_path(name)
    if not create and not os.path.exists(path):
        return None, None
    with _mbtiles_pool_lock:
        if name in _mbtiles_conns:
            # Verify the connection is still valid (file may have been deleted from under us)
            existing = _mbtiles_conns[name]
            try:
                existing.execute("SELECT 1").fetchone()
                return existing, _mbtiles_locks[name]
            except sqlite3.Error:
                logger.warning(f"stale mbtiles connection for {name}, rebuilding")
                try: existing.close()
                except Exception: pass
                _mbtiles_conns.pop(name, None)
                _mbtiles_locks.pop(name, None)
        try:
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")  # 10s on lock contention
            if create:
                conn.execute("""CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS tiles (
                    zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
                    PRIMARY KEY (zoom_level, tile_column, tile_row))""")
        except sqlite3.Error as e:
            logger.error(f"failed to open mbtiles {name}: {e}")
            raise
        _mbtiles_conns[name] = conn
        _mbtiles_locks[name] = threading.Lock()
        return conn, _mbtiles_locks[name]


def _mbtiles_close(name: str) -> None:
    """Close and remove a connection from the pool. Idempotent."""
    with _mbtiles_pool_lock:
        conn = _mbtiles_conns.pop(name, None)
        _mbtiles_locks.pop(name, None)
    if conn is not None:
        try: conn.close()
        except Exception: pass

def list_offline_layers():
    """Discover all .mbtiles in TILES_DIR and return display metadata for the UI."""
    out = []
    if not os.path.isdir(TILES_DIR):
        return out
    for fn in sorted(os.listdir(TILES_DIR)):
        if not fn.endswith('.mbtiles'):
            continue
        name = fn[:-len('.mbtiles')]
        path = os.path.join(TILES_DIR, fn)
        try:
            conn, _ = _mbtiles_get(name)
            if conn is None:
                continue
            meta = {row[0]: row[1] for row in conn.execute("SELECT name, value FROM metadata")}
            tile_count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
            # Include WAL/SHM bytes — until SQLite checkpoints, most data lives in the -wal file.
            size = sum(
                os.path.getsize(p) for p in (path, path + '-wal', path + '-shm')
                if os.path.exists(p)
            )
            fmt = (meta.get('format') or 'png').lower()
            kind = 'vector' if fmt in ('pbf', 'mvt') else 'raster'
            # bounds string = "west,south,east,north" per MBTiles 1.1 spec — parse
            # so the UI can fitBounds() to the cached area on layer-select (no more
            # picking an offline layer and getting a black screen because the map
            # is parked over an uncached region of the world).
            bounds = None
            try:
                if meta.get('bounds'):
                    parts = [float(x) for x in meta['bounds'].split(',')]
                    if len(parts) == 4 and all(math.isfinite(p) for p in parts):
                        bounds = parts  # [w, s, e, n]
            except Exception:
                bounds = None
            # If no bounds in metadata, derive from cached tile extent at the lowest zoom
            if bounds is None:
                try:
                    row = conn.execute(
                        "SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row), MIN(zoom_level) "
                        "FROM tiles WHERE zoom_level=(SELECT MIN(zoom_level) FROM tiles)"
                    ).fetchone()
                    if row and row[0] is not None:
                        xmin, xmax, tms_ymin, tms_ymax, z = row
                        n = 1 << z
                        # Convert tile XYZ extent (TMS y) back to lat/lon corners
                        def _t2ll(x, y_xyz, z):
                            lon = x / (1 << z) * 360.0 - 180.0
                            lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y_xyz / (1 << z))))
                            return math.degrees(lat_rad), lon
                        # tms_y -> xyz_y = (n-1) - tms_y
                        ymin_xyz = (n - 1) - tms_ymax  # north edge
                        ymax_xyz = (n - 1) - tms_ymin  # south edge
                        n_lat, w_lon = _t2ll(xmin, ymin_xyz, z)
                        s_lat, e_lon = _t2ll(xmax + 1, ymax_xyz + 1, z)
                        if all(math.isfinite(v) for v in (n_lat, w_lon, s_lat, e_lon)):
                            bounds = [w_lon, s_lat, e_lon, n_lat]
                except Exception:
                    pass
            out.append({
                'name': name,
                'label': meta.get('name', name),
                'format': fmt,
                'kind': kind,
                'minzoom': int(meta.get('minzoom', 0)),
                'maxzoom': int(meta.get('maxzoom', 19)),
                'attribution': meta.get('attribution', '© offline'),
                'tile_count': tile_count,
                'size_bytes': size,
                'bounds': bounds,        # [w, s, e, n] or null
            })
        except Exception as e:
            logger.warning(f"Failed to read mbtiles {fn}: {e}")
    return out

# ----------------------
# Tile cache jobs (in-memory; UI polls these)
# ----------------------
CACHE_JOBS: dict = {}
CACHE_JOBS_LOCK = threading.Lock()

def _deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 1 << zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y

def _tiles_for_bbox(bbox, zmin, zmax):
    """Yield (z, x, y) for every tile in the bbox at each zoom in [zmin, zmax]."""
    w, s, e, n = bbox  # west, south, east, north
    for z in range(zmin, zmax + 1):
        x0, y1 = _deg2num(n, w, z)
        x1, y0 = _deg2num(s, e, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                yield z, x, y

def _count_tiles_for_bbox(bbox, zmin, zmax):
    total = 0
    w, s, e, n = bbox
    for z in range(zmin, zmax + 1):
        x0, y1 = _deg2num(n, w, z)
        x1, y0 = _deg2num(s, e, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total

# Job persistence — survives server restart so a half-done cache can be resumed.
JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache_jobs.json')
_jobs_save_lock = threading.Lock()
# Tunables for resilience
TILE_RETRY_ATTEMPTS = 3                    # per-tile attempts before counting one error
TILE_RETRY_BACKOFFS = (0.5, 2.0, 5.0)      # seconds between attempts
AUTOPAUSE_AFTER_CONSEC_ERRORS = 10         # consecutive failed tiles -> pause (not error)
JOB_SAVE_EVERY_N_TILES = 50                # persist progress every N tiles
TILE_INTER_REQUEST_DELAY = 0.05            # polite throttle between tile fetches
TILE_MAX_BLOB_BYTES = 4 * 1024 * 1024      # reject tiles bigger than 4 MB (corrupt/wrong-mime)
TILE_MIN_BLOB_BYTES = 64                   # reject suspiciously tiny blobs
HTTP_TIMEOUT_SECONDS = 15                  # per-request timeout
DISK_FREE_MIN_BYTES = 64 * 1024 * 1024     # below this, pause before writing more tiles


def _disk_has_space(path: str, min_bytes: int = DISK_FREE_MIN_BYTES) -> bool:
    try:
        st = os.statvfs(os.path.dirname(os.path.abspath(path)) or '.')
        return (st.f_bavail * st.f_frsize) >= min_bytes
    except Exception:
        return True  # if we can't tell, assume yes


def _looks_like_image(blob: bytes, fmt: str) -> bool:
    """Cheap magic-byte sanity check. Vector PBF blobs are gzipped: starts with 0x1f8b."""
    if not blob:
        return False
    if fmt in ('pbf', 'mvt'):
        return blob[:2] == b'\x1f\x8b'
    if fmt in ('jpg', 'jpeg'):
        return blob[:3] == b'\xff\xd8\xff'
    if fmt == 'png':
        return blob[:8] == b'\x89PNG\r\n\x1a\n'
    if fmt == 'webp':
        return blob[:4] == b'RIFF' and blob[8:12] == b'WEBP'
    return True  # unknown format — accept


def _save_cache_jobs():
    """Snapshot CACHE_JOBS to disk. Strips runtime-only fields. Safe to call often."""
    try:
        with _jobs_save_lock, CACHE_JOBS_LOCK:
            snap = []
            for j in CACHE_JOBS.values():
                snap.append({k: v for k, v in j.items() if k != 'cancel'})
            tmp = JOBS_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'jobs': snap}, f)
            os.replace(tmp, JOBS_FILE)
    except Exception as e:
        logger.debug(f"failed to save cache jobs: {e}")


def _load_cache_jobs():
    """Load persisted jobs on startup. Anything 'running' or 'queued' becomes 'paused'
    because the worker thread died with the previous process — user can resume."""
    if not os.path.exists(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"could not read {JOBS_FILE}: {e}")
        return
    revived = 0
    for j in data.get('jobs', []):
        if j.get('status') in ('running', 'queued'):
            j['status'] = 'paused'
            j.setdefault('pause_reason', 'server restart')
        j.setdefault('cancel', False)
        j.setdefault('attempts', 0)
        j.setdefault('consec_errors', 0)
        CACHE_JOBS[j['id']] = j
        revived += 1
    if revived:
        logger.info(f"reloaded {revived} cache job(s) from {JOBS_FILE}")


def _interruptible_sleep(seconds: float, job: dict) -> bool:
    """Sleep up to N seconds, waking early on cancel or shutdown.
    Returns True if woken early (cancel/shutdown)."""
    deadline = time.time() + seconds
    step = 0.05
    while time.time() < deadline:
        if job.get('cancel') or SHUTDOWN_EVENT.is_set():
            return True
        time.sleep(min(step, max(0.0, deadline - time.time())))
    return False


def _cache_worker(job_id):
    """Background tile fetcher. Writes into <name>.mbtiles, updates job progress.

    Resilience features:
    - Resumable: already-cached tiles are skipped via the SQLite primary key.
    - Per-tile retries with exponential backoff (TILE_RETRY_BACKOFFS).
    - Auto-pause on AUTOPAUSE_AFTER_CONSEC_ERRORS consecutive failures.
    - Wakeable sleep: cancel/shutdown signals interrupt waits immediately.
    - Disk-full detection: pauses before writing if free space drops below threshold.
    - Tile blob validation: rejects empty, oversized, or magic-byte-mismatched data.
    - Periodic state persist so a crash + restart picks up cleanly.
    """
    job = CACHE_JOBS.get(job_id)
    if job is None:
        logger.error(f"cache worker invoked for unknown job {job_id}")
        return

    name = job['name']
    source = job['source']
    bbox = job['bbox']
    zmin, zmax = job['zmin'], job['zmax']

    if source not in TILE_SOURCES:
        job['status'] = 'error'
        job['error_msg'] = f"unknown source '{source}'"
        _save_cache_jobs()
        return

    src = TILE_SOURCES[source]
    fmt = src['fmt']

    # Set up the HTTP session in a try/finally so we always release sockets
    sess = requests.Session()
    sess.headers.update({
        'User-Agent': 'drone-mesh-mapper/offline-cacher (https://github.com/colonelpanichacks/drone-mesh-mapper)',
        'Accept': 'image/png, image/jpeg, image/webp, application/x-protobuf, */*',
    })

    try:
        try:
            conn, write_lock = _mbtiles_get(name, create=True)
        except (ValueError, sqlite3.Error) as e:
            job['status'] = 'error'
            job['error_msg'] = f"could not open mbtiles: {e}"
            _save_cache_jobs()
            return

        # Seed/refresh metadata (safe to re-run on resume)
        try:
            with write_lock:
                for k, v in [
                    ('name', name), ('format', fmt), ('type', 'baselayer'),
                    ('version', '1.1'), ('description', f'Cached from {source}'),
                    ('attribution', src['attrib']),
                    ('minzoom', str(zmin)), ('maxzoom', str(zmax)),
                    ('bounds', ','.join(f'{x:.6f}' for x in bbox)),
                ]:
                    conn.execute("INSERT OR REPLACE INTO metadata(name, value) VALUES(?, ?)", (k, v))
        except sqlite3.Error as e:
            job['status'] = 'paused'
            job['pause_reason'] = f"could not write metadata: {e}"
            _save_cache_jobs()
            return

        # Recount what's already cached so progress reflects the truth on resume.
        try:
            already = 0
            for z in range(zmin, zmax + 1):
                already += conn.execute(
                    "SELECT COUNT(*) FROM tiles WHERE zoom_level=?", (z,)).fetchone()[0]
            job['skipped'] = max(job.get('skipped', 0), already)
            job['done'] = max(job.get('done', 0), already)
        except sqlite3.Error as e:
            logger.warning(f"job {job_id}: tile count failed: {e}")

        job['status'] = 'running'
        job['pause_reason'] = ''
        job['started'] = job.get('started') or time.time()
        job['consec_errors'] = 0
        _save_cache_jobs()

        last_save = time.time()
        last_disk_check = 0.0

        for z, x, y in _tiles_for_bbox(bbox, zmin, zmax):
            # Honor cancel + shutdown immediately
            if job.get('cancel'):
                job['status'] = 'cancelled'
                return
            if SHUTDOWN_EVENT.is_set():
                # Mark as paused so the next startup auto-resumes the user
                job['status'] = 'paused'
                job['pause_reason'] = 'server shutting down'
                return

            tms_y = (1 << z) - 1 - y

            # Fast skip if we already have it
            try:
                row = conn.execute(
                    "SELECT 1 FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
                    (z, x, tms_y)).fetchone()
                if row:
                    continue
            except sqlite3.Error as e:
                logger.warning(f"job {job_id}: dedup check failed for {z}/{x}/{y}: {e}")

            # Periodic disk-space check — pause cleanly if low
            if time.time() - last_disk_check > 5.0:
                if not _disk_has_space(_mbtiles_path(name)):
                    job['status'] = 'paused'
                    job['pause_reason'] = 'disk space low - free up space and resume'
                    _save_cache_jobs()
                    return
                last_disk_check = time.time()

            url = src['url'].replace('{z}', str(z)).replace('{x}', str(x)).replace('{y}', str(y))
            ok = False
            for attempt in range(TILE_RETRY_ATTEMPTS):
                if job.get('cancel') or SHUTDOWN_EVENT.is_set():
                    break
                backoff = TILE_RETRY_BACKOFFS[min(attempt, len(TILE_RETRY_BACKOFFS) - 1)]
                try:
                    r = sess.get(url, timeout=HTTP_TIMEOUT_SECONDS)
                    sc = r.status_code
                    blob = r.content if sc == 200 else None

                    if sc == 200 and blob:
                        # Validate blob: size + magic
                        if len(blob) < TILE_MIN_BLOB_BYTES:
                            logger.debug(f"job {job_id}: blob too small ({len(blob)}b) {url}")
                        elif len(blob) > TILE_MAX_BLOB_BYTES:
                            logger.warning(f"job {job_id}: blob too large ({len(blob)}b) {url}")
                        elif not _looks_like_image(blob, fmt):
                            logger.warning(f"job {job_id}: blob magic mismatch ({fmt}) {url}")
                        else:
                            try:
                                with write_lock:
                                    conn.execute(
                                        "INSERT OR REPLACE INTO tiles(zoom_level, tile_column, tile_row, tile_data) "
                                        "VALUES(?,?,?,?)",
                                        (z, x, tms_y, blob))
                                job['fetched'] += 1
                                ok = True
                                break
                            except sqlite3.OperationalError as e:
                                # disk full or db locked beyond timeout
                                msg = str(e).lower()
                                if 'full' in msg or 'no space' in msg:
                                    job['status'] = 'paused'
                                    job['pause_reason'] = 'disk full - free up space and resume'
                                    _save_cache_jobs()
                                    return
                                logger.warning(f"job {job_id}: sqlite write error: {e}")
                    elif sc == 429:
                        # rate limit — back off harder
                        if _interruptible_sleep(backoff * 2, job):
                            break
                        continue
                    elif 500 <= sc < 600:
                        # upstream issue — retry
                        if _interruptible_sleep(backoff, job):
                            break
                        continue
                    elif sc == 404:
                        # tile genuinely doesn't exist (e.g. extreme north over ocean) — skip
                        logger.debug(f"job {job_id}: 404 {url}")
                        break
                    else:
                        if _interruptible_sleep(backoff, job):
                            break
                except requests.RequestException as e:
                    logger.debug(f"job {job_id}: net error attempt {attempt+1}: {e}")
                    if _interruptible_sleep(backoff, job):
                        break

            if ok:
                job['consec_errors'] = 0
            else:
                job['errors'] += 1
                job['consec_errors'] = job.get('consec_errors', 0) + 1
                if job['consec_errors'] >= AUTOPAUSE_AFTER_CONSEC_ERRORS:
                    job['status'] = 'paused'
                    job['pause_reason'] = (
                        f"{AUTOPAUSE_AFTER_CONSEC_ERRORS} consecutive failures - "
                        "press resume to retry")
                    _save_cache_jobs()
                    return

            job['done'] += 1
            # Polite throttle between requests (interruptible)
            _interruptible_sleep(TILE_INTER_REQUEST_DELAY, job)

            # Periodic state persist
            if time.time() - last_save > 2.0 or (job['done'] % JOB_SAVE_EVERY_N_TILES == 0):
                _save_cache_jobs()
                last_save = time.time()

        if job.get('cancel'):
            job['status'] = 'cancelled'
        elif SHUTDOWN_EVENT.is_set():
            job['status'] = 'paused'
            job['pause_reason'] = 'server shutting down'
        else:
            job['status'] = 'done'

    except Exception as e:
        job['status'] = 'error'
        job['error_msg'] = f"{type(e).__name__}: {e}"
        logger.exception(f"cache job {job_id} crashed")
    finally:
        try: sess.close()
        except Exception: pass
        job['finished'] = time.time()
        _save_cache_jobs()

# Define emit_serial_status early to avoid NameError in threads
def emit_serial_status():
    try:
        socketio.emit('serial_status', combined_connection_status(), )
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
    """Save both webhook URLs (detection + geofence) to disk."""
    global WEBHOOK_URL, GEOFENCE_WEBHOOK_URL
    try:
        with open(WEBHOOK_URL_FILE, "w") as f:
            json.dump({
                "webhook_url": WEBHOOK_URL,
                "geofence_webhook_url": GEOFENCE_WEBHOOK_URL,
            }, f)
        logger.debug(f"Webhook URLs saved to {WEBHOOK_URL_FILE}")
    except Exception as e:
        logger.error(f"Error saving webhook URLs: {e}")

def load_webhook_url():
    """Load both webhook URLs from disk on startup. Backward-compat: old files
    that only have `webhook_url` keep working, the geofence URL just defaults
    to None (which means: fall back to WEBHOOK_URL for geofence alerts)."""
    global WEBHOOK_URL, GEOFENCE_WEBHOOK_URL
    if os.path.exists(WEBHOOK_URL_FILE):
        try:
            with open(WEBHOOK_URL_FILE, "r") as f:
                data = json.load(f)
                WEBHOOK_URL = data.get("webhook_url") or None
                GEOFENCE_WEBHOOK_URL = data.get("geofence_webhook_url") or None
                if WEBHOOK_URL:
                    logger.info(f"Loaded detection webhook: {WEBHOOK_URL}")
                if GEOFENCE_WEBHOOK_URL:
                    logger.info(f"Loaded geofence webhook: {GEOFENCE_WEBHOOK_URL}")
                if not WEBHOOK_URL and not GEOFENCE_WEBHOOK_URL:
                    logger.info("No webhook URLs configured")
        except Exception as e:
            logger.error(f"Error loading webhook URLs: {e}")
            WEBHOOK_URL = None
            GEOFENCE_WEBHOOK_URL = None
    else:
        logger.info("No saved webhook URL file found")
        WEBHOOK_URL = None
        GEOFENCE_WEBHOOK_URL = None

# ----------------------
# Global Variables & Files
# ----------------------
tracked_pairs = {}
# basic_id -> tracked_pairs key (mac), to merge the same drone arriving via
# multiple receive paths (direct node detection vs DroneScout Bridge relay,
# which reports under a different/synthesized MAC) into one map entry.
basic_id_index = {}
detection_history = deque(maxlen=MAX_DETECTION_HISTORY)  # Limit size to prevent memory growth

# Changed: Instead of one selected port, we allow up to three.
SELECTED_PORTS = {}  # key will be 'port1', 'port2', 'port3'
BAUD_RATE = 115200
staleThreshold = 60  # Global stale threshold in seconds (changed from 300 seconds -> 1 minute)
# For each port, we track its connection status.
serial_connected_status = {}  # e.g. {"port1": True, "port2": False, ...}
# Non-serial receivers reporting over HTTP (e.g. DroneScout Bridge BLE relay
# via tools/ds110_bridge.py): name -> {"last_seen": ts, "stats": dict}
receiver_status = {}
RECEIVER_TIMEOUT_S = 45  # show Disconnected if no heartbeat within this

def combined_connection_status():
    """Serial port statuses plus HTTP receivers, computed fresh each call."""
    statuses = dict(serial_connected_status)
    now = time.time()
    for name, info in receiver_status.items():
        statuses[name] = (now - info.get("last_seen", 0)) < RECEIVER_TIMEOUT_S
    return statuses
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


# --- Drone OSINT tag persistence (per-MAC labels: police / gov / known / unknown / etc.) ---
DRONE_TAGS_FILE = os.path.join(BASE_DIR, "drone_tags.json")
DRONE_TAGS = {}                # mac (lowercase) -> tag string
DRONE_TAG_VALUES = ('civilian', 'police', 'government', 'military', 'commercial', 'known', 'unknown')
if os.path.exists(DRONE_TAGS_FILE):
    try:
        with open(DRONE_TAGS_FILE, "r") as f:
            DRONE_TAGS = {k.lower(): v for k, v in json.load(f).items() if v}
    except Exception as e:
        print("Error loading drone tags:", e)


def save_drone_tags():
    global DRONE_TAGS
    try:
        tmp = DRONE_TAGS_FILE + '.tmp'
        with open(tmp, "w") as f:
            json.dump(DRONE_TAGS, f, indent=2)
        os.replace(tmp, DRONE_TAGS_FILE)
    except Exception as e:
        print("Error saving drone tags:", e)


def classify_drone(mac: str) -> str:
    """Return the OSINT tag for a drone MAC. Defaults to 'unknown' for any MAC
    we've never seen tagged. Manual override via /api/drone_tags."""
    if not mac:
        return 'unknown'
    return DRONE_TAGS.get(mac.lower(), 'unknown')


# --- Geofencing: persisted polygons / circles + per-drone state + alerts ---
GEOFENCES_FILE = os.path.join(BASE_DIR, "geofences.json")
GEOFENCES = {}                  # id -> fence dict
GEOFENCE_LOCK = threading.Lock()
# Per-fence per-MAC inside-state: GEOFENCE_STATE[fence_id][mac] = bool
GEOFENCE_STATE: dict = {}
# Recent alerts (in-memory ring, last N)
GEOFENCE_ALERTS: list = []
GEOFENCE_ALERTS_MAX = 200

if os.path.exists(GEOFENCES_FILE):
    try:
        with open(GEOFENCES_FILE, "r") as f:
            data = json.load(f)
            for fid, fence in (data.get('fences') or {}).items():
                GEOFENCES[fid] = fence
    except Exception as e:
        print("Error loading geofences:", e)


def save_geofences():
    """Persist GEOFENCES atomically. Safe to call often."""
    try:
        tmp = GEOFENCES_FILE + '.tmp'
        with open(tmp, "w") as f:
            json.dump({'fences': GEOFENCES}, f, indent=2)
        os.replace(tmp, GEOFENCES_FILE)
    except Exception as e:
        logger.debug(f"could not save geofences: {e}")


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two lat/lon points in meters."""
    R = 6371008.8  # mean Earth radius (m)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _point_in_polygon(lat: float, lon: float, ring) -> bool:
    """Ray-casting test. ring is a list of [lat, lon] pairs (polygon vertices,
    not closed). Returns True if (lat, lon) lies inside."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        ai_lat, ai_lon = ring[i][0], ring[i][1]
        aj_lat, aj_lon = ring[j][0], ring[j][1]
        if ((ai_lon > lon) != (aj_lon > lon)):
            # x-coord of intersection of edge with horizontal line y = lat
            slope = (aj_lat - ai_lat) / (aj_lon - ai_lon) if (aj_lon != ai_lon) else float('inf')
            x_int = ai_lat + slope * (lon - ai_lon)
            if lat < x_int:
                inside = not inside
        j = i
    return inside


def _point_in_fence(lat, lon, fence) -> bool:
    if fence.get('type') == 'circle':
        g = fence.get('geometry') or {}
        c = g.get('center') or [0, 0]
        r = float(g.get('radius_m') or 0)
        if r <= 0:
            return False
        return _haversine_m(lat, lon, c[0], c[1]) <= r
    if fence.get('type') == 'polygon':
        g = fence.get('geometry') or {}
        ring = g.get('points') or []
        return _point_in_polygon(lat, lon, ring)
    return False


def _validate_fence(data) -> dict:
    """Validate + canonicalize a fence dict. Raises ValueError on bad input."""
    if not isinstance(data, dict):
        raise ValueError("fence must be an object")
    name = (data.get('name') or '').strip()
    if not name or len(name) > 80:
        raise ValueError("name is required (≤80 chars)")
    ftype = data.get('type')
    if ftype not in ('polygon', 'circle'):
        raise ValueError("type must be 'polygon' or 'circle'")
    geom = data.get('geometry') or {}
    if ftype == 'polygon':
        pts = geom.get('points') or []
        if not isinstance(pts, list) or len(pts) < 3:
            raise ValueError("polygon needs ≥3 points")
        ring = []
        for p in pts:
            if not (isinstance(p, list) and len(p) == 2):
                raise ValueError("polygon points must be [lat, lon] pairs")
            try:
                lat = float(p[0]); lon = float(p[1])
            except (TypeError, ValueError):
                raise ValueError("polygon point coords must be numeric")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("polygon point out of range")
            ring.append([lat, lon])
        geom = {'points': ring}
    else:  # circle
        c = geom.get('center')
        r = geom.get('radius_m')
        if not (isinstance(c, list) and len(c) == 2):
            raise ValueError("circle needs center [lat, lon]")
        try:
            clat = float(c[0]); clon = float(c[1]); rad = float(r or 0)
        except (TypeError, ValueError):
            raise ValueError("circle coords/radius must be numeric")
        if not (-90 <= clat <= 90 and -180 <= clon <= 180):
            raise ValueError("circle center out of range")
        if not (0 < rad <= 1_000_000):
            raise ValueError("circle radius_m must be 0 < r ≤ 1,000,000")
        geom = {'center': [clat, clon], 'radius_m': rad}

    alert_tags = data.get('alert_tags') or []
    if alert_tags and not isinstance(alert_tags, list):
        raise ValueError("alert_tags must be a list")
    alert_tags = [str(t).lower().strip() for t in alert_tags if t]

    # Aircraft tag filter — independent from drone tag filter so a single fence
    # can target e.g. 'military aircraft' without matching every drone too.
    aircraft_tags = data.get('aircraft_tags') or []
    if aircraft_tags and not isinstance(aircraft_tags, list):
        raise ValueError("aircraft_tags must be a list")
    aircraft_tags = [str(t).lower().strip() for t in aircraft_tags if t]

    # Target kind — which moving things this fence watches.
    # 'drone' (default, legacy), 'aircraft', or 'both'.
    target = (data.get('target_kind') or 'drone').strip().lower()
    if target not in ('drone', 'aircraft', 'both'):
        raise ValueError("target_kind must be drone | aircraft | both")

    color = (data.get('color') or '#ff3333').strip()
    if len(color) > 16:
        raise ValueError("color too long")
    # Per-fence webhook URL — overrides BOTH the global webhook and the
    # GEOFENCE_WEBHOOK_URL fallback for events from this specific fence. Empty
    # = use the global geofence webhook (or detection webhook if that's also blank).
    webhook_url = (data.get('webhook_url') or '').strip()
    if webhook_url and not webhook_url.startswith(('http://', 'https://')):
        raise ValueError("webhook_url must start with http:// or https://")
    return {
        'name': name,
        'type': ftype,
        'geometry': geom,
        'alert_on_enter': bool(data.get('alert_on_enter', True)),
        'alert_on_exit':  bool(data.get('alert_on_exit', True)),
        'alert_tags': alert_tags,
        'aircraft_tags': aircraft_tags,
        'target_kind': target,
        'color': color,
        'webhook_url': webhook_url,
        'enabled': bool(data.get('enabled', True)),
    }


def _post_webhook(payload, override_url: str = None):
    """Best-effort POST to the user-configured webhook. Silent on failure.
    Resolution order for geofence events:
      1. `override_url` arg (per-fence URL set on the fence itself)
      2. GEOFENCE_WEBHOOK_URL (global geofence-only URL)
      3. WEBHOOK_URL (global detection URL — geofence falls back to this)
    Non-geofence events always use WEBHOOK_URL."""
    if override_url:
        url = override_url
    elif isinstance(payload, dict) and payload.get('event') == 'geofence' and GEOFENCE_WEBHOOK_URL:
        url = GEOFENCE_WEBHOOK_URL
    else:
        url = WEBHOOK_URL
    if not url:
        return
    try:
        requests.post(url, json=payload, timeout=5,
                      headers={'User-Agent': 'drone-mesh-mapper/geofence'})
    except Exception as e:
        logger.debug(f"geofence webhook to {url} failed: {e}")


def _emit_geofence_alert(fence, mac, transition, lat, lon, drone_tag, alias):
    """Push to clients via socket, fire webhook, ring-buffer it."""
    payload = {
        'ts': time.time(),
        'fence_id': fence.get('id'),
        'fence_name': fence.get('name'),
        'fence_color': fence.get('color', '#ff3333'),
        'mac': mac,
        'alias': alias or '',
        'drone_tag': drone_tag,
        'transition': transition,    # 'enter' or 'exit'
        'lat': lat,
        'lon': lon,
    }
    GEOFENCE_ALERTS.append(payload)
    if len(GEOFENCE_ALERTS) > GEOFENCE_ALERTS_MAX:
        del GEOFENCE_ALERTS[:len(GEOFENCE_ALERTS) - GEOFENCE_ALERTS_MAX]
    try:
        socketio.emit('geofence_alert', payload)
    except Exception:
        pass
    # Webhook in a background thread so we don't block detection processing.
    # Per-fence URL (if set) takes precedence over the global geofence webhook.
    fence_hook = (fence.get('webhook_url') or '').strip() or None
    threading.Thread(
        target=_post_webhook,
        args=({**payload, 'event': 'geofence'}, fence_hook),
        daemon=True,
    ).start()
    logger.info(
        f"GEOFENCE {transition.upper()}: {alias or mac} ({drone_tag}) "
        f"{'entered' if transition == 'enter' else 'left'} '{fence.get('name')}'"
    )


def check_aircraft_against_fences(snapshot):
    """Run every aircraft's current position through every aircraft-targeting
    fence and emit enter/exit alerts. Per-fence per-ICAO state lives in
    GEOFENCE_STATE under the same dict the drone checker uses (ICAO strings can
    coexist with MAC strings since both are unique to their domains)."""
    if not snapshot:
        return
    with GEOFENCE_LOCK:
        for fid, fence in GEOFENCES.items():
            if not fence.get('enabled', True):
                continue
            tk = (fence.get('target_kind') or 'drone').lower()
            if tk == 'drone':
                continue   # drone-only fence
            tag_filter = fence.get('aircraft_tags') or []
            state = GEOFENCE_STATE.setdefault(fid, {})
            for a in snapshot:
                icao = a.get('icao')
                lat = a.get('lat'); lon = a.get('lon')
                if not icao or lat is None or lon is None:
                    continue
                # Tag filter — `tags` from classifier can be a list.
                if tag_filter:
                    a_tags = a.get('tags') or []
                    if isinstance(a_tags, str):
                        a_tags = [a_tags]
                    if not any(t in tag_filter for t in a_tags):
                        continue
                key = 'ac:' + icao   # namespaced key so it can't collide with drone MACs
                inside_now = _point_in_fence(lat, lon, fence)
                inside_prev = state.get(key, None)
                state[key] = inside_now
                if inside_prev is None:
                    continue
                tag_for_alert = (a.get('tags') or ['unknown'])
                if isinstance(tag_for_alert, list):
                    tag_for_alert = tag_for_alert[0] if tag_for_alert else 'unknown'
                if inside_now and not inside_prev and fence.get('alert_on_enter', True):
                    _emit_geofence_alert(fence, icao, 'enter', lat, lon, tag_for_alert,
                                         a.get('callsign') or '')
                elif inside_prev and not inside_now and fence.get('alert_on_exit', True):
                    _emit_geofence_alert(fence, icao, 'exit', lat, lon, tag_for_alert,
                                         a.get('callsign') or '')


def check_drone_against_fences(mac: str, lat: float, lon: float):
    """Run a drone's current position through every enabled fence and emit
    alerts on enter/exit transitions. Cheap; called inline from detection ingest."""
    if mac is None or lat is None or lon is None:
        return
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return
    drone_tag = classify_drone(mac)
    alias = ALIASES.get(mac) if isinstance(ALIASES, dict) else None
    with GEOFENCE_LOCK:
        for fid, fence in GEOFENCES.items():
            if not fence.get('enabled', True):
                continue
            # Target kind filter — drone-targeted fences only watch drones.
            tk = (fence.get('target_kind') or 'drone').lower()
            if tk == 'aircraft':
                continue
            # Tag filter: empty list = any drone triggers; non-empty = only those tags
            tags = fence.get('alert_tags') or []
            if tags and drone_tag not in tags:
                continue
            inside_now = _point_in_fence(lat, lon, fence)
            state = GEOFENCE_STATE.setdefault(fid, {})
            inside_prev = state.get(mac, None)
            state[mac] = inside_now
            if inside_prev is None:
                continue   # first observation — no transition yet
            if inside_now and not inside_prev and fence.get('alert_on_enter', True):
                _emit_geofence_alert(fence, mac, 'enter', lat, lon, drone_tag, alias)
            elif inside_prev and not inside_now and fence.get('alert_on_exit', True):
                _emit_geofence_alert(fence, mac, 'exit',  lat, lon, drone_tag, alias)

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
    # Drop DroneScout Bridge idle self-advertisements (placeholder relay with
    # zeroed GPS) — they are not drones. Seen as "DroneScout Bridge" (raw BLE)
    # and "DroneScoutBridge" (node firmware strips the space).
    bid = detection.get("basic_id")
    if bid and bid.replace(" ", "").lower() == "dronescoutbridge":
        return
    # Dedup across receive paths: if this basic_id is already tracked under a
    # different MAC (e.g. direct node detection vs DroneScout Bridge relay),
    # fold this detection into the existing entry instead of creating a dupe.
    if bid:
        owner = basic_id_index.get(bid)
        if owner and owner != mac and owner in tracked_pairs:
            logger.info(f"Merging {mac} into existing track {owner} (same basic_id {bid})")
            detection["mac"] = mac = owner
        else:
            basic_id_index[bid] = mac
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

    # Geofence transition check — emits enter/exit alerts via socket + webhook
    try:
        d_lat = detection.get('drone_lat')
        d_lon = detection.get('drone_long')
        if d_lat is not None and d_lon is not None and (d_lat or d_lon):
            check_drone_against_fences(mac, d_lat, d_lon)
    except Exception as e:
        logger.debug(f"geofence check failed for {mac}: {e}")

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

# ----------------------
# Offline tiles HTTP routes
# ----------------------
@app.route('/tiles/<name>/<int:z>/<int:x>/<int:y>.<ext>')
def serve_offline_tile(name, z, x, y, ext):
    """Serve a single tile from <name>.mbtiles. Y is XYZ; we flip to TMS for storage lookup.
    Vector tiles (.pbf / .mvt) are stored gzipped in mbtiles per spec — we set Content-Encoding."""
    try:
        conn, _ = _mbtiles_get(name)
    except ValueError:
        return ('', 404)
    if conn is None:
        return ('', 404)
    tms_y = (1 << z) - 1 - y
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=? LIMIT 1",
        (z, x, tms_y)).fetchone()
    if not row:
        return ('', 404)
    fmt = ext.lower()
    if fmt in ('pbf', 'mvt'):
        mime = 'application/x-protobuf'
    elif fmt in ('jpg', 'jpeg'):
        mime = 'image/jpeg'
    elif fmt == 'webp':
        mime = 'image/webp'
    else:
        mime = 'image/png'
    resp = app.make_response(row[0])
    resp.headers['Content-Type'] = mime
    resp.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
    # MBTiles 1.3 spec stores PBF gzipped — browser/MapLibre expect Content-Encoding: gzip
    if fmt in ('pbf', 'mvt') and len(row[0]) >= 2 and row[0][0] == 0x1f and row[0][1] == 0x8b:
        resp.headers['Content-Encoding'] = 'gzip'
    return resp

@app.route('/styles/<name>.json')
def serve_offline_style(name):
    """Generate a MapLibre style JSON for a vector mbtiles. Names ending in '/<style>' pick a style.
    For now we ship one bundled style: 'default-dark'. Drop more JSON files in static/styles/."""
    style_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'styles', 'default-dark.json')
    if not os.path.exists(style_path):
        return jsonify({'error': 'default style missing'}), 500
    try:
        with open(style_path) as f:
            style = json.load(f)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Resolve metadata for the requested layer
    try:
        conn, _ = _mbtiles_get(name)
    except ValueError:
        return ('', 404)
    if conn is None:
        return ('', 404)
    meta = {row[0]: row[1] for row in conn.execute("SELECT name, value FROM metadata")}
    minzoom = int(meta.get('minzoom', 0))
    maxzoom = int(meta.get('maxzoom', 14))
    bounds = meta.get('bounds')
    bounds_arr = [float(x) for x in bounds.split(',')] if bounds else None

    # Inline TileJSON instead of external URL — avoids a second round-trip and works offline.
    tilejson = {
        'tilejson': '2.2.0',
        'name': name,
        'tiles': [request.url_root.rstrip('/') + f'/tiles/{name}/{{z}}/{{x}}/{{y}}.pbf'],
        'minzoom': minzoom,
        'maxzoom': maxzoom,
        'attribution': meta.get('attribution', '© offline'),
        'scheme': 'xyz',
    }
    if bounds_arr and len(bounds_arr) == 4:
        tilejson['bounds'] = bounds_arr

    style['sources']['openmaptiles'] = tilejson
    style['name'] = f'mesh-mapper · {name}'
    resp = app.make_response(jsonify(style))
    resp.headers['Cache-Control'] = 'no-store'  # style is dynamic per-mbtiles
    return resp

@app.route('/api/offline_layers', methods=['GET'])
def api_offline_layers():
    return jsonify({'layers': list_offline_layers(), 'sources': list(TILE_SOURCES.keys())})

@app.route('/api/offline_layers/<name>', methods=['DELETE'])
def api_offline_layer_delete(name):
    try:
        path = _mbtiles_path(name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _mbtiles_close(name)
    removed = []
    for ext in ('', '-wal', '-shm', '.part'):
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(os.path.basename(p))
            except OSError as e:
                logger.warning(f"could not remove {p}: {e}")
    if not removed:
        return jsonify({'error': 'not found'}), 404
    logger.info(f"deleted offline layer '{name}' ({', '.join(removed)})")
    return jsonify({'ok': True, 'removed': removed})

@app.route('/api/cache_tiles', methods=['POST'])
def api_cache_tiles_start():
    data = request.get_json(force=True, silent=True) or {}
    source = data.get('source')
    if source not in TILE_SOURCES:
        return jsonify({'error': f"unknown source; valid: {sorted(TILE_SOURCES.keys())}"}), 400

    try:
        _mbtiles_path(data.get('name', ''))  # validates name shape
        bbox = list(_validate_bbox(data.get('bbox')))
        zmin, zmax = _validate_zoom_range(data.get('zmin', 0), data.get('zmax', 14))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    name = data['name'].strip()

    # Don't let the source's max zoom be exceeded — tiles past that just 404.
    src_max = TILE_SOURCES[source].get('maxZoom', 22)
    if zmax > src_max:
        return jsonify({
            'error': f"{source} only goes to zoom {src_max}; lower zMax",
            'source_max_zoom': src_max,
        }), 400

    # Refuse to touch an existing vector (.pbf) mbtiles with the raster cacher.
    try:
        existing_conn, _ = _mbtiles_get(name)
        if existing_conn is not None:
            row = existing_conn.execute(
                "SELECT value FROM metadata WHERE name='format'").fetchone()
            existing_fmt = (row[0] if row else '').lower()
            if existing_fmt in ('pbf', 'mvt'):
                return jsonify({'error': f"'{name}.mbtiles' is a vector layer; the raster cacher would corrupt it"}), 400
            # Also: refuse to mix raster formats (e.g. PNG into a JPG mbtiles)
            new_fmt = TILE_SOURCES[source]['fmt']
            if existing_fmt and existing_fmt != new_fmt:
                return jsonify({
                    'error': f"existing layer is '{existing_fmt}', source produces '{new_fmt}'; pick a different name"
                }), 400
    except (ValueError, sqlite3.Error) as e:
        logger.debug(f"could not pre-check existing mbtiles for {name}: {e}")

    total = _count_tiles_for_bbox(bbox, zmin, zmax)
    if total > 2_000_000:
        return jsonify({'error': f'too many tiles ({total:,}); narrow bbox or lower max zoom'}), 400
    if total <= 0:
        return jsonify({'error': 'bbox produces zero tiles at the chosen zooms'}), 400

    # Disk space sanity (rough estimate: 15 KB/tile)
    if not _disk_has_space(_mbtiles_path(name), max(DISK_FREE_MIN_BYTES, total * 16384)):
        return jsonify({'error': f'not enough free disk space for ~{total:,} tiles (~{total*15//1024} MB)'}), 507

    job_id = uuid.uuid4().hex[:12]
    job = {
        'id': job_id, 'name': name, 'source': source, 'bbox': bbox,
        'zmin': zmin, 'zmax': zmax, 'total': total,
        'done': 0, 'fetched': 0, 'skipped': 0, 'errors': 0,
        'consec_errors': 0,
        'status': 'queued', 'cancel': False,
        'pause_reason': '',
        'started': None, 'finished': None,
    }
    with CACHE_JOBS_LOCK:
        CACHE_JOBS[job_id] = job
    _save_cache_jobs()
    t = threading.Thread(target=_cache_worker, args=(job_id,), daemon=True)
    t.start()
    return jsonify(job)

@app.route('/api/cache_jobs', methods=['GET'])
def api_cache_jobs():
    with CACHE_JOBS_LOCK:
        return jsonify({'jobs': list(CACHE_JOBS.values())})

@app.route('/api/cache_jobs/<job_id>', methods=['GET'])
def api_cache_job(job_id):
    with CACHE_JOBS_LOCK:
        job = CACHE_JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify(job)

# ----------------------
# Place search (Nominatim) — keeps a tiny LRU cache + 1 req/s rate limit + UA
# ----------------------
_geocode_cache: "dict[str, list]" = {}
_geocode_cache_lock = threading.Lock()
_geocode_rate_lock = threading.Lock()
_last_geocode_call = [0.0]
GEOCODE_CACHE_MAX = 256
GEOCODE_MIN_INTERVAL = 1.05  # seconds; Nominatim asks for <=1 req/s
GEOCODE_TIMEOUT = 8.0
GEOCODE_USER_AGENT = (
    'drone-mesh-mapper/offline-cacher '
    '(https://github.com/colonelpanichacks/drone-mesh-mapper)'
)


@app.route('/api/geocode', methods=['GET'])
def api_geocode():
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    if len(q) > 200:
        return jsonify({'error': 'query too long'}), 400
    key = q.lower()
    with _geocode_cache_lock:
        if key in _geocode_cache:
            cached = _geocode_cache.pop(key)
            _geocode_cache[key] = cached  # touch LRU
            return jsonify({'results': cached, 'cached': True})

    # Cap how long we'll hold the rate-limit lock so a slow upstream can't deadlock us.
    if not _geocode_rate_lock.acquire(timeout=GEOCODE_MIN_INTERVAL + GEOCODE_TIMEOUT + 1):
        return jsonify({'error': 'geocode busy; try again'}), 503
    try:
        wait = GEOCODE_MIN_INTERVAL - (time.time() - _last_geocode_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_geocode_call[0] = time.time()
        try:
            r = requests.get(
                'https://nominatim.openstreetmap.org/search',
                params={'q': q, 'format': 'jsonv2', 'limit': 8, 'addressdetails': 0},
                headers={'User-Agent': GEOCODE_USER_AGENT,
                         'Accept-Language': request.headers.get('Accept-Language', 'en')},
                timeout=GEOCODE_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning(f"geocode network error: {e}")
            return jsonify({'error': f'geocode failed: {e}'}), 502
    finally:
        _geocode_rate_lock.release()

    if r.status_code != 200:
        return jsonify({'error': f'nominatim returned {r.status_code}'}), 502
    try:
        raw = r.json()
    except ValueError:
        return jsonify({'error': 'nominatim returned non-JSON'}), 502

    out = []
    for item in raw if isinstance(raw, list) else []:
        bb = item.get('boundingbox')  # [s, n, w, e] strings
        if not bb or len(bb) != 4:
            continue
        try:
            s, n, w, e = (float(bb[i]) for i in range(4))
            lat = float(item.get('lat', 0))
            lon = float(item.get('lon', 0))
        except (TypeError, ValueError):
            continue
        if any(math.isnan(v) or math.isinf(v) for v in (s, n, w, e, lat, lon)):
            continue
        out.append({
            'name': str(item.get('display_name', ''))[:300],
            'lat': lat,
            'lon': lon,
            'bbox': [w, s, e, n],
            'type': str(item.get('type', '')),
            'category': str(item.get('category', '')),
        })
    with _geocode_cache_lock:
        _geocode_cache[key] = out
        # LRU evict
        while len(_geocode_cache) > GEOCODE_CACHE_MAX:
            _geocode_cache.pop(next(iter(_geocode_cache)))
    return jsonify({'results': out, 'cached': False})


# ----------------------
# ADS-B aircraft OSINT classifier
# ----------------------
# Tags applied to every aircraft regardless of source. The UI uses these for
# filtering (mil-only / LEO-only / etc.) and to colorize the popup.
#
# Sources for the lookup tables:
#   - Mil hex blocks: ICAO 24-bit allocations + community-maintained lists
#     (joelkoz/ICAO24-block-allocations, PlaneAlert, OpenSky military lists)
#   - LEO callsigns: known US/UK police/sheriff aviation units (incomplete by
#     design — extend at will via ADSB_LEO_CALLSIGNS / ADSB_LEO_HEX_PREFIXES)
#   - Squawks: standard ICAO emergency codes
#
# This is heuristic and best-effort. False positives possible. False negatives
# guaranteed for any aircraft using deliberate obfuscation.

# Hex (ICAO 24-bit) ranges that are predominantly military by allocation.
# Format: list of (low, high) pairs, both inclusive, integers.
ADSB_MIL_HEX_RANGES = [
    # United States military (AE0000–AFFFFF is the bulk)
    (0xAE0000, 0xAFFFFF),
    (0xADF7C8, 0xADFFFF),
    # United Kingdom Royal Air Force (43C000–43FFFF)
    (0x43C000, 0x43FFFF),
    # Germany Luftwaffe (3F0000–3FFFFF, partial)
    (0x3F4000, 0x3FBFFF),
    # France military (3B7000–3B7FFF)
    (0x3B7000, 0x3B7FFF),
    # Australia ADF (7C0000–7CFFFF; civilian shares this — many false positives,
    # we cross-check callsign before tagging)
    # Canada CAF (C00000–C0FFFF, partial)
    # NATO (4D8000-4D8FFF)
    (0x4D8000, 0x4D8FFF),
    # Russia mil (140000–14FFFF, partial)
    (0x140000, 0x14FFFF),
]

# Callsign prefixes that strongly indicate military operation
ADSB_MIL_CALLSIGN_PREFIXES = (
    'RCH',   # USAF C-17/C-5 air mobility
    'CNV',   # USN logistics
    'SAM',   # Special Air Mission (USAF VIP)
    'PAT',   # US Army
    'KING',  # USAF rescue
    'SHELL', # USAF tankers
    'BOSS',  # USAF
    'CAB',   # US Army aviation
    'GAJ',   # Various mil
    'GTMO',  # Guantanamo logistics
    'LOBO',  # USAF
    'BISON', # USAF B-1
    'NAVY',  # USN
    'ARMY',  # US Army
    'MARINE','MARINES',  # USMC
    'COAST', # USCG
    'GUARD', # ANG
    'TREK',  # USAF special ops
    'BLACKJACK',
    'CASEY', # USN
    'IRON',  # USAF
    'RHINO', # USN F/A-18 (also civilian)
    'DARK',  # Various spec ops
    'JAKE',  # US Army
    'PANTHER',  # USMC
    'BLUE',  # USAF VIP
    'THUNDER', # USAF Thunderbirds
    'ANGEL',   # Blue Angels
    'BRAVO',   # Various
    'BAT',     # USAF
    'CITGO',   # US mil tankers
    'ETHYL',   # KC-135
    'GOLD',    # USAF VIP
    'ROOK',    # USAF
    'NIGHTHAWK',
    'JOLLY',   # USAF rescue
    'DUKE',    # USAF
    'PYRO',    # USAF
    'TURBO',   # USAF
    'VOODOO',  # USAF
    'WILD',    # USAF
    'ZORRO',   # USAF
    'RAGE',    # USAF
    'RAVEN',   # USAF spec ops
    'HOIST',   # USAF
    'HAVOC',   # USAF
    'MONTY',   # UK RAF
    'ASCOT',   # UK RAF
    'KIWI',    # NZ RNZAF
)

# Callsign prefixes for LEO / law enforcement aviation (US-heavy, extend as needed)
ADSB_LEO_CALLSIGN_PREFIXES = (
    'POLICE','LAPD','NYPD','LASD','LAFD','CHP','SHERIFF','USCG','CBP',
    'TROOPER','TROOPERS','RANGER','PHOENIX','BIRDIE','TIGER','FOX','HAWK',
    'WOLF','BLACK','EAGLE','SPYWARE','DEA','FBI','ATF','USMS','USSS',
    'MARSHAL','MARSHALS','BORDER','HOMELAND','HSI','TFR','SWAT',
)

# Government / executive callsigns
ADSB_GOV_CALLSIGN_PREFIXES = (
    'EXEC1','EXEC1F','AF1','AF2',  # presidential
    'VENUS',     # presidential air mobility (rotates)
    'MAGA01','MAGA02',
    'NAS',       # NASA
    'EAGLE9',    # FBI Hostage Rescue
    'NIGHTWATCH',# E-4B doomsday
)

# Squawk -> tag (must match exactly)
ADSB_SQUAWK_TAGS = {
    '7700': 'emergency',  # general emergency
    '7600': 'emergency',  # comms failure
    '7500': 'hijack',     # unlawful interference
    '7777': 'military',   # USA mil intercept
    '4000': 'military',   # US warning area
}

# ADS-B aircraft category (from message bytes) -> tag hint
ADSB_CATEGORY_TAGS = {
    'A0': None,
    'A1': None,            # light
    'A2': None,            # small
    'A3': None,            # large
    'A5': None,            # heavy
    'A6': None,            # high-performance
    'A7': 'rotorcraft',    # rotorcraft (helicopters often LEO/SAR/news)
    'B6': 'uav',           # unmanned aerial vehicle
    'B7': 'spaceship',
    'C0': None,            # surface vehicle
}


def _hex_in_ranges(hex_str: str, ranges) -> bool:
    if not hex_str or len(hex_str) != 6:
        return False
    try:
        n = int(hex_str, 16)
    except ValueError:
        return False
    return any(lo <= n <= hi for lo, hi in ranges)


def classify_aircraft(a: dict) -> list:
    """Return a list of OSINT tags for an aircraft dict.

    Always returns at least one tag. Order is significant for primary-tag
    rendering: more specific tags come first."""
    icao = (a.get('icao') or '').lower()
    cs = (a.get('callsign') or '').upper().strip()
    cat = (a.get('category') or '').upper()
    sq = str(a.get('squawk') or '').strip()
    tags = []
    seen = set()

    def add(t):
        if t and t not in seen:
            tags.append(t); seen.add(t)

    # Squawks (highest priority — emergencies, intercepts)
    if sq in ADSB_SQUAWK_TAGS:
        add(ADSB_SQUAWK_TAGS[sq])

    # Mil by hex range
    if _hex_in_ranges(icao, ADSB_MIL_HEX_RANGES):
        add('military')

    # Callsign-based
    for p in ADSB_GOV_CALLSIGN_PREFIXES:
        if cs.startswith(p):
            add('government'); break
    for p in ADSB_MIL_CALLSIGN_PREFIXES:
        if cs.startswith(p):
            add('military'); break
    for p in ADSB_LEO_CALLSIGN_PREFIXES:
        if cs.startswith(p) or cs == p:
            add('police'); break

    # Aircraft category hints
    if cat in ADSB_CATEGORY_TAGS and ADSB_CATEGORY_TAGS[cat]:
        add(ADSB_CATEGORY_TAGS[cat])

    # VFR squawk codes — typically GA / private
    if not tags and sq in ('1200', '1201', '1202', '7000'):
        add('private')

    # Commercial vs private fallback (rough — no other tag fired)
    if not tags and cs:
        # Most airlines: 3-letter ICAO + flight number
        if len(cs) >= 4 and cs[:3].isalpha() and cs[3:].lstrip('0').isdigit():
            add('commercial')
        # US registration pattern: N + digits (private)
        elif cs.startswith('N') and len(cs) >= 2 and cs[1].isdigit():
            add('private')

    if not tags:
        add('unknown')
    return tags


# ----------------------
# ADS-B air traffic integration (multi-source, pluggable)
# ----------------------
# Each source returns a normalized list of aircraft dicts:
#   {icao, callsign, lat, lon, alt_baro, velocity, heading, vert_rate, squawk, on_ground, seen}
ADSB_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'adsb_config.json')
ADSB_DEFAULT_INTERVAL = 4           # seconds between polls — tight enough for visible motion
ADSB_STALE_AFTER = 180              # drop aircraft not seen for 3 min (lets a flight survive a few missed cycles + slow updates)
ADSB_MAX_AIRCRAFT = 25000           # generous cap; wide-area polls can return 10k+ aircraft

# In-memory snapshot keyed by ICAO hex
ADSB_AIRCRAFT: dict = {}
ADSB_AIRCRAFT_LOCK = threading.Lock()

# Live config (persisted)
ADSB_CONFIG: dict = {
    'enabled': False,
    'source': 'adsblol',          # default: free, no API key
    'interval': ADSB_DEFAULT_INTERVAL,
    'bbox': None,                 # optional [w, s, e, n] to filter
    'opensky_user': '',           # optional
    'opensky_pass': '',
    'adsbx_key': '',              # optional rapidapi key for ADS-B Exchange
    'dump1090_url': 'http://localhost:8080/data/aircraft.json',
    'beast_host': 'localhost',    # Beast TCP feed (raw Mode-S, port 30005 by default)
    'beast_port': 30005,
    # When a circle-based source (adsb.lol/adsb.fi/airplanes.live) is selected and
    # the viewport is zoomed out past what 250nm-circle tiling can cover, fall back
    # to OpenSky's global feed so the world view actually shows worldwide traffic.
    'auto_global': True,
}


def _adsb_save_config():
    try:
        tmp = ADSB_CONFIG_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(ADSB_CONFIG, f, indent=2)
        os.replace(tmp, ADSB_CONFIG_FILE)
    except Exception as e:
        logger.debug(f"adsb config save failed: {e}")


def _adsb_load_config():
    if not os.path.exists(ADSB_CONFIG_FILE):
        return
    try:
        with open(ADSB_CONFIG_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in ADSB_CONFIG:
                ADSB_CONFIG[k] = v
        logger.info(f"loaded adsb config (source={ADSB_CONFIG['source']}, enabled={ADSB_CONFIG['enabled']})")
    except Exception as e:
        logger.warning(f"could not read {ADSB_CONFIG_FILE}: {e}")


# ----- Per-source fetchers — each returns a list of normalized aircraft dicts -----

def _bbox_to_radius_nm(bbox, pad: float = 1.15) -> tuple:
    """Convert [w, s, e, n] to (lat, lon, radius_nm) for circle-based ADS-B APIs.
    Pads the radius slightly so aircraft right at the bbox edge aren't cut off.
    Handles antimeridian crossing (when west > east, the bbox wraps around)."""
    w, s, e, n = bbox
    lat = (s + n) / 2
    # Antimeridian-aware longitude width and center.
    if w <= e:
        lon_width = e - w
        lon = (w + e) / 2
    else:
        # Bbox wraps the antimeridian (e.g. w=170, e=-170 → width 20° spanning ±180)
        lon_width = (180.0 - w) + (e + 180.0)
        lon_center = (w + e) / 2 + 180.0
        if lon_center > 180.0: lon_center -= 360.0
        lon = lon_center
    # Approximate KM diagonal accounting for longitudinal compression at this lat
    diag_km = math.hypot((n - s) * 111.32,
                         lon_width * 111.32 * max(0.05, math.cos(math.radians(lat))))
    radius_nm = max(5, int((diag_km / 1.852 / 2) * pad))
    radius_nm = min(250, radius_nm)  # adsb.lol/fi cap
    return lat, lon, radius_nm


def _bbox_to_circle_tiles(bbox, max_radius_nm: int = 250, pad: float = 1.10) -> list:
    """Tile a bbox into a grid of (lat, lon, radius_nm) circles when a single
    250nm circle (the per-call cap on adsb.lol / adsb.fi / airplanes.live) can't
    cover the whole viewport. Returns one circle for small bboxes, up to ~16
    circles for continent-scale views.

    Without this, zooming out to the whole US gets you aircraft within 250nm
    of the geographic center (Kansas) — nothing visible in LA, NYC, Miami.
    """
    # Try the single-circle path first.
    c_lat, c_lon, c_r = _bbox_to_radius_nm(bbox, pad=pad)
    if c_r < max_radius_nm:
        return [(c_lat, c_lon, c_r)]

    # Need to tile. Compute how many lat/lon steps to cover the bbox with
    # circles of radius `max_radius_nm`. Each circle covers ~max_radius_nm * 2
    # nm across (we use a smaller effective step so neighboring circles overlap
    # and no plane falls into a seam).
    w, s, e, n = bbox
    # Antimeridian wrap: compute effective east longitude > west to make stepping
    # straightforward, then wrap back when constructing centers.
    e_eff = e if e >= w else e + 360.0
    # Tile step in nm — ~80% of radius so circles overlap.
    step_nm = max_radius_nm * 0.8
    # Center lat used for lon-degree scaling at the bbox center.
    lat_center = (s + n) / 2
    nm_per_deg_lat = 60.0     # constant
    nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(lat_center)))
    step_lat_deg = step_nm / nm_per_deg_lat
    step_lon_deg = step_nm / nm_per_deg_lon
    rows = max(1, math.ceil((n - s) / step_lat_deg))
    cols = max(1, math.ceil((e_eff - w) / step_lon_deg))
    # Cap total to keep API load reasonable. 36 tiles = ~36 quick HTTP gets,
    # fired in parallel by the fetchers so total wall time is still ~1-2s. At
    # 36, full-US zoom-out gets NO seam gaps between circles — every aircraft
    # in the lower 48 is covered.
    total = rows * cols
    if total > 36:
        # Trim by growing the step so we land at <=36 tiles.
        scale = math.sqrt(total / 36.0)
        step_lat_deg *= scale
        step_lon_deg *= scale
        rows = max(1, math.ceil((n - s) / step_lat_deg))
        cols = max(1, math.ceil((e_eff - w) / step_lon_deg))
    # Distribute centers evenly across the bbox.
    tiles = []
    for ri in range(rows):
        # Center of this row
        tlat = s + (ri + 0.5) * ((n - s) / rows)
        for ci in range(cols):
            tlon_unwrapped = w + (ci + 0.5) * ((e_eff - w) / cols)
            tlon = tlon_unwrapped
            while tlon > 180.0:  tlon -= 360.0
            while tlon < -180.0: tlon += 360.0
            tiles.append((tlat, tlon, max_radius_nm))
    return tiles


def _adsb_fetch_adsblol(cfg, sess) -> list:
    """adsb.lol — free, no key. Uses /v2/lat/lon/dist for area-bounded queries.
    A bbox is REQUIRED — the providers do not expose a 'firehose' endpoint.
    For bboxes larger than a single 250nm circle can cover, we tile the bbox
    and aggregate. That's how a US-wide zoom-out shows aircraft coast to coast
    instead of just within 250nm of Kansas.

    Tiles are fetched in parallel via a ThreadPoolExecutor so a 36-tile US-wide
    poll completes in ~1-2s instead of 18-25s sequential."""
    bbox = cfg.get('bbox')
    if not (bbox and len(bbox) == 4):
        raise RuntimeError("adsb.lol needs a bbox; toggle 'Only fetch around current map view' or pan to your AO")
    tiles = _bbox_to_circle_tiles(bbox)
    def _fetch_one(t):
        lat_c, lon_c, radius_nm = t
        url = f"https://api.adsb.lol/v2/lat/{lat_c:.4f}/lon/{lon_c:.4f}/dist/{radius_nm}"
        try:
            r = sess.get(url, timeout=10)
            r.raise_for_status()
            return r.json().get('ac', []) or []
        except Exception:
            return []
    seen_icao = set()
    out = []
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(tiles)))) as pool:
        for ac in pool.map(_fetch_one, tiles):
            for a in ac:
                try:
                    lat, lon = a.get('lat'), a.get('lon')
                    if lat is None or lon is None:
                        continue
                    icao = str(a.get('hex', '')).lower()
                    if not icao or icao in seen_icao:
                        continue
                    seen_icao.add(icao)
                    out.append({
                        'icao': icao,
                        'callsign': (a.get('flight') or '').strip(),
                        'lat': float(lat), 'lon': float(lon),
                        'alt_baro': a.get('alt_baro') if isinstance(a.get('alt_baro'), (int, float)) else None,
                        'velocity': a.get('gs'),       # ground speed (knots)
                        'heading': a.get('track'),
                        'vert_rate': a.get('baro_rate'),
                        'squawk': a.get('squawk'),
                        'on_ground': (a.get('alt_baro') == 'ground'),
                        'category': a.get('category'),
                        'seen': time.time(),
                    })
                except (TypeError, ValueError):
                    continue
    return out


def _adsb_fetch_adsbfi(cfg, sess) -> list:
    """adsb.fi — same shape as adsb.lol, free no key. Bbox required. Tiles
    large bboxes the same way to give full-viewport coverage at any zoom.
    Parallel tile fetch via ThreadPoolExecutor."""
    bbox = cfg.get('bbox')
    if not (bbox and len(bbox) == 4):
        raise RuntimeError("adsb.fi needs a bbox; toggle 'Only fetch around current map view' or pan to your AO")
    tiles = _bbox_to_circle_tiles(bbox)
    def _fetch_one(t):
        lat_c, lon_c, radius_nm = t
        url = f"https://opendata.adsb.fi/api/v2/lat/{lat_c:.4f}/lon/{lon_c:.4f}/dist/{radius_nm}"
        try:
            r = sess.get(url, timeout=10)
            r.raise_for_status()
            return r.json().get('ac', []) or []
        except Exception:
            return []
    seen_icao = set()
    out = []
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(tiles)))) as pool:
        for ac in pool.map(_fetch_one, tiles):
            for a in ac:
                try:
                    lat, lon = a.get('lat'), a.get('lon')
                    if lat is None or lon is None:
                        continue
                    icao = str(a.get('hex', '')).lower()
                    if not icao or icao in seen_icao:
                        continue
                    seen_icao.add(icao)
                    out.append({
                        'icao': icao,
                        'callsign': (a.get('flight') or '').strip(),
                        'lat': float(lat), 'lon': float(lon),
                        'alt_baro': a.get('alt_baro') if isinstance(a.get('alt_baro'), (int, float)) else None,
                        'velocity': a.get('gs'),
                        'heading': a.get('track'),
                        'vert_rate': a.get('baro_rate'),
                        'squawk': a.get('squawk'),
                        'on_ground': (a.get('alt_baro') == 'ground'),
                        'category': a.get('category'),
                        'seen': time.time(),
                    })
                except (TypeError, ValueError):
                    continue
    return out


def _adsb_fetch_opensky(cfg, sess) -> list:
    """OpenSky Network — free anonymous tier (~100 req/day) or higher with auth."""
    bbox = cfg.get('bbox')
    params = {}
    if bbox and len(bbox) == 4:
        params = {'lamin': bbox[1], 'lomin': bbox[0], 'lamax': bbox[3], 'lomax': bbox[2]}
    auth = None
    if cfg.get('opensky_user'):
        auth = (cfg['opensky_user'], cfg.get('opensky_pass') or '')
    r = sess.get('https://opensky-network.org/api/states/all',
                 params=params, auth=auth, timeout=15)
    r.raise_for_status()
    data = r.json() or {}
    out = []
    # state vector: https://openskynetwork.github.io/opensky-api/rest.html#response
    for s in data.get('states') or []:
        try:
            icao24 = s[0]
            callsign = (s[1] or '').strip()
            lon = s[5]; lat = s[6]
            if lat is None or lon is None:
                continue
            out.append({
                'icao': str(icao24).lower(),
                'callsign': callsign,
                'lat': float(lat), 'lon': float(lon),
                'alt_baro': (s[7] * 3.28084) if s[7] is not None else None,  # m -> ft
                'velocity': (s[9] * 1.94384) if s[9] is not None else None,  # m/s -> kt
                'heading': s[10],
                'vert_rate': (s[11] * 196.85) if s[11] is not None else None,  # m/s -> ft/min
                'squawk': s[14],
                'on_ground': bool(s[8]),
                'category': None,
                'seen': time.time(),
            })
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _adsb_fetch_airplaneslive(cfg, sess) -> list:
    """airplanes.live — free, no key. Bbox required."""
    bbox = cfg.get('bbox')
    if not (bbox and len(bbox) == 4):
        raise RuntimeError("airplanes.live needs a bbox; toggle 'Only fetch around current map view' or pan to your AO")
    tiles = _bbox_to_circle_tiles(bbox)
    def _fetch_one(t):
        lat_c, lon_c, radius_nm = t
        url = f"https://api.airplanes.live/v2/point/{lat_c:.4f}/{lon_c:.4f}/{radius_nm}"
        try:
            r = sess.get(url, timeout=10)
            r.raise_for_status()
            return r.json().get('ac', []) or []
        except Exception:
            return []
    seen_icao = set()
    out = []
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(tiles)))) as pool:
        for ac in pool.map(_fetch_one, tiles):
            for a in ac:
                try:
                    lat, lon = a.get('lat'), a.get('lon')
                    if lat is None or lon is None:
                        continue
                    icao = str(a.get('hex', '')).lower()
                    if not icao or icao in seen_icao:
                        continue
                    seen_icao.add(icao)
                    out.append({
                        'icao': icao,
                        'callsign': (a.get('flight') or '').strip(),
                        'lat': float(lat), 'lon': float(lon),
                        'alt_baro': a.get('alt_baro') if isinstance(a.get('alt_baro'), (int, float)) else None,
                        'velocity': a.get('gs'),
                        'heading': a.get('track'),
                        'vert_rate': a.get('baro_rate'),
                        'squawk': a.get('squawk'),
                        'on_ground': (a.get('alt_baro') == 'ground'),
                        'category': a.get('category'),
                        'seen': time.time(),
                    })
                except (TypeError, ValueError):
                    continue
    return out


def _adsb_fetch_adsbexchange(cfg, sess) -> list:
    """ADS-B Exchange via RapidAPI — requires user's API key (paid tier)."""
    key = cfg.get('adsbx_key')
    if not key:
        raise RuntimeError("ADS-B Exchange requires a RapidAPI key (set it in config)")
    bbox = cfg.get('bbox')
    if not bbox or len(bbox) != 4:
        raise RuntimeError("ADS-B Exchange requires a bbox (toggle 'Only fetch around current map view')")
    lat, lon, radius_nm = _bbox_to_radius_nm(bbox)
    url = f"https://adsbexchange-com1.p.rapidapi.com/v2/lat/{lat:.4f}/lon/{lon:.4f}/dist/{radius_nm}/"
    r = sess.get(url, timeout=10, headers={
        'X-RapidAPI-Key': key,
        'X-RapidAPI-Host': 'adsbexchange-com1.p.rapidapi.com',
    })
    r.raise_for_status()
    out = []
    for a in r.json().get('ac', []):
        try:
            lat_, lon_ = a.get('lat'), a.get('lon')
            if lat_ is None or lon_ is None:
                continue
            out.append({
                'icao': str(a.get('hex', '')).lower(),
                'callsign': (a.get('flight') or '').strip(),
                'lat': float(lat_), 'lon': float(lon_),
                'alt_baro': a.get('alt_baro') if isinstance(a.get('alt_baro'), (int, float)) else None,
                'velocity': a.get('gs'),
                'heading': a.get('track'),
                'vert_rate': a.get('baro_rate'),
                'squawk': a.get('squawk'),
                'on_ground': (a.get('alt_baro') == 'ground'),
                'category': a.get('category'),
                'seen': time.time(),
            })
        except (TypeError, ValueError):
            continue
    return out


def _adsb_fetch_dump1090(cfg, sess) -> list:
    """Local dump1090 / readsb / tar1090 — works with ANY SDR (RTL-SDR, HackRF,
    AirSpy, SDRplay) that's piped through one of these decoders. Pick a URL preset
    or override with your own. The most reliable + zero-cost option."""
    url = cfg.get('dump1090_url') or 'http://localhost:8080/data/aircraft.json'
    r = sess.get(url, timeout=5)
    r.raise_for_status()
    data = r.json()
    out = []
    for a in data.get('aircraft', []):
        try:
            lat, lon = a.get('lat'), a.get('lon')
            if lat is None or lon is None:
                continue
            out.append({
                'icao': str(a.get('hex', '')).lower().lstrip('~'),
                'callsign': (a.get('flight') or '').strip(),
                'lat': float(lat), 'lon': float(lon),
                'alt_baro': a.get('alt_baro') if isinstance(a.get('alt_baro'), (int, float)) else None,
                'velocity': a.get('gs'),
                'heading': a.get('track'),
                'vert_rate': a.get('baro_rate'),
                'squawk': a.get('squawk'),
                'on_ground': (a.get('alt_baro') == 'ground'),
                'category': a.get('category'),
                'seen': time.time(),
            })
        except (TypeError, ValueError):
            continue
    return out


# ----- Beast TCP feed (raw Mode-S frames over a TCP socket) -----
# Beast is the universal ADS-B output protocol; spoken by dump1090(-fa/-mutability),
# readsb, modesmixer2, FlightAware feeders, AirSpy adsbspy, HackRF dump1090 forks, etc.
# Default port is 30005. We connect, read frames, decode with pyModeS, and feed the
# same ADSB_AIRCRAFT dict every other source uses.
#
# Credit: this path replicates what tar1090/readsb do externally. pyModeS (junzis)
# does the heavy lifting on Mode-S/CPR decode; we just orchestrate the socket + state.
try:
    import pyModeS as pms  # type: ignore
    _PYMODES_AVAILABLE = True
except ImportError:
    pms = None  # type: ignore
    _PYMODES_AVAILABLE = False

_beast_thread = None
_beast_thread_lock = threading.Lock()
_beast_stop_event = threading.Event()
_beast_status = {'connected': False, 'host': '', 'port': 0, 'error': '', 'frames': 0}


def _beast_unescape(buf: bytes) -> bytes:
    """Beast escapes 0x1a in payloads as 0x1a 0x1a — undo it."""
    return buf.replace(b'\x1a\x1a', b'\x1a')


def _beast_iter_frames(sock):
    """Yield (msg_type, payload_hex) for each frame on the socket. Stops on disconnect."""
    buf = b''
    sock.settimeout(5.0)
    while not _beast_stop_event.is_set() and not SHUTDOWN_EVENT.is_set():
        try:
            chunk = sock.recv(4096)
            if not chunk:
                return  # peer closed
            buf += chunk
        except socket.timeout:
            continue
        # Parse all complete frames in buf
        while True:
            i = buf.find(b'\x1a')
            if i < 0:
                buf = b''
                break
            buf = buf[i:]
            if len(buf) < 2:
                break
            t = buf[1]
            # Frame body sizes (Beast spec): type 0x31=mode-AC short (2B), 0x32=mode-S short (7B),
            # 0x33=mode-S long (14B), 0x34=mode-S status (no body)
            body_size = {0x31: 2, 0x32: 7, 0x33: 14, 0x34: 0}.get(t)
            if body_size is None:
                buf = buf[1:]  # bad type, skip
                continue
            # Each frame: 0x1a TYPE, 6B timestamp, 1B signal level, BODY
            frame_total = 2 + 6 + 1 + body_size
            # Account for in-payload escaping by walking bytes
            # Cheap approach: try to slice out frame_total bytes counting escaped 0x1a
            need = frame_total
            j = 2  # skip 0x1a + type
            while need > 2 and j < len(buf):
                if buf[j] == 0x1a:
                    if j + 1 >= len(buf):
                        break
                    if buf[j + 1] == 0x1a:
                        j += 2  # escaped 0x1a, consume both
                        need -= 1
                        continue
                    else:
                        # End of this frame, start of next
                        break
                j += 1
                need -= 1
            if need > 2:
                # Not enough data yet
                break
            raw = buf[:j]
            buf = buf[j:]
            unescaped = _beast_unescape(raw[2:])  # drop 0x1a TYPE
            if len(unescaped) < 7 + body_size:
                continue
            payload = unescaped[7:7 + body_size]
            if body_size in (7, 14):
                yield t, payload.hex()


def _beast_reader_thread(host: str, port: int):
    """Connect to a Beast feed and decode frames into ADSB_AIRCRAFT.
    Reconnects with exponential backoff. Exits cleanly on stop/shutdown."""
    if not _PYMODES_AVAILABLE:
        _beast_status.update({'connected': False, 'error': 'pyModeS not installed (pip install pyModeS)'})
        logger.warning("Beast TCP requires pyModeS: pip install pyModeS")
        return

    backoff = 1.0
    # Per-aircraft CPR state (odd/even position frames + timestamps)
    cpr_state: dict = {}
    # Optional reference position for local CPR decode (from the user's home/AO)
    bbox = ADSB_CONFIG.get('bbox') or []
    ref_lat = (bbox[1] + bbox[3]) / 2 if len(bbox) == 4 else None
    ref_lon = (bbox[0] + bbox[2]) / 2 if len(bbox) == 4 else None

    while not _beast_stop_event.is_set() and not SHUTDOWN_EVENT.is_set():
        sock = None
        try:
            _beast_status.update({'host': host, 'port': port, 'error': ''})
            sock = socket.create_connection((host, port), timeout=10)
            _beast_status['connected'] = True
            backoff = 1.0
            logger.info(f"Beast: connected to {host}:{port}")

            for msg_type, msg_hex in _beast_iter_frames(sock):
                _beast_status['frames'] += 1
                # Only short (DF 0/4/5/11) or long (DF 16/17/18/19/20/21/24) Mode-S messages
                try:
                    df = pms.df(msg_hex)
                except Exception:
                    continue
                # ADS-B (DF=17 native, DF=18 TIS-B/relay) carries position/identity/velocity
                if df not in (17, 18):
                    continue
                try:
                    icao = pms.adsb.icao(msg_hex).lower()
                    tc = pms.adsb.typecode(msg_hex)
                except Exception:
                    continue
                if not icao:
                    continue

                with ADSB_AIRCRAFT_LOCK:
                    cur = ADSB_AIRCRAFT.get(icao, {
                        'icao': icao, 'callsign': '', 'lat': None, 'lon': None,
                        'alt_baro': None, 'velocity': None, 'heading': None,
                        'vert_rate': None, 'squawk': None, 'on_ground': False,
                        'category': None, 'seen': time.time(),
                    })
                    cur['seen'] = time.time()

                    if 1 <= tc <= 4:
                        # Aircraft identification
                        try: cur['callsign'] = pms.adsb.callsign(msg_hex).strip().rstrip('_')
                        except Exception: pass
                    elif 9 <= tc <= 18 or 20 <= tc <= 22:
                        # Airborne position. Need an odd/even pair within ~10s, OR a ref position.
                        try:
                            alt = pms.adsb.altitude(msg_hex)
                            if alt is not None:
                                cur['alt_baro'] = alt
                        except Exception:
                            pass
                        oe = pms.adsb.oe_flag(msg_hex)
                        st = cpr_state.setdefault(icao, {})
                        st[oe] = (msg_hex, time.time())
                        try:
                            pos = None
                            if ref_lat is not None and ref_lon is not None:
                                pos = pms.adsb.position_with_ref(msg_hex, ref_lat, ref_lon)
                            elif 0 in st and 1 in st and abs(st[0][1] - st[1][1]) < 10:
                                pos = pms.adsb.position(st[0][0], st[1][0], st[0][1], st[1][1])
                            if pos:
                                cur['lat'], cur['lon'] = float(pos[0]), float(pos[1])
                        except Exception:
                            pass
                    elif tc == 19:
                        # Velocity
                        try:
                            v = pms.adsb.velocity(msg_hex)
                            if v:
                                cur['velocity'] = v[0]   # kt
                                cur['heading']  = v[1]   # deg
                                cur['vert_rate'] = v[2]  # ft/min
                        except Exception:
                            pass

                    # Only commit if we have a position (matches what other sources do)
                    if cur.get('lat') is not None and cur.get('lon') is not None:
                        cur['tags'] = classify_aircraft(cur)
                        ADSB_AIRCRAFT[icao] = cur

        except (socket.error, OSError) as e:
            _beast_status.update({'connected': False, 'error': str(e)})
            logger.debug(f"Beast: connect/read error {host}:{port}: {e}")
        except Exception as e:
            _beast_status.update({'connected': False, 'error': str(e)})
            logger.exception("Beast reader crashed")
        finally:
            if sock:
                try: sock.close()
                except Exception: pass
            _beast_status['connected'] = False

        if _beast_stop_event.is_set() or SHUTDOWN_EVENT.is_set():
            return
        # Exponential backoff with cap
        wait = min(60.0, backoff)
        if SHUTDOWN_EVENT.wait(wait):
            return
        backoff = min(60.0, backoff * 2)


def _start_beast_thread():
    """Start the Beast reader if needed. Idempotent."""
    global _beast_thread
    with _beast_thread_lock:
        if _beast_thread and _beast_thread.is_alive():
            return
        _beast_stop_event.clear()
        host = ADSB_CONFIG.get('beast_host') or 'localhost'
        port = int(ADSB_CONFIG.get('beast_port') or 30005)
        _beast_thread = threading.Thread(
            target=_beast_reader_thread, args=(host, port),
            daemon=True, name="beast-reader")
        _beast_thread.start()


def _stop_beast_thread():
    _beast_stop_event.set()


def _adsb_fetch_beast(cfg, sess) -> list:
    """The 'Beast' source isn't a per-poll fetcher — it runs as a persistent thread.
    The poller just snapshots whatever the reader has accumulated."""
    if not _PYMODES_AVAILABLE:
        raise RuntimeError("pyModeS not installed; run `pip install pyModeS` to enable Beast")
    _start_beast_thread()
    # Snapshot from ADSB_AIRCRAFT — the reader updates it directly.
    with ADSB_AIRCRAFT_LOCK:
        return list(ADSB_AIRCRAFT.values())


ADSB_SOURCES = {
    'adsblol':         {'label': 'adsb.lol (free, no key)',           'fetch': _adsb_fetch_adsblol,        'requires_internet': True,  'requires_key': False, 'kind': 'network'},
    'adsbfi':          {'label': 'adsb.fi (free, no key)',            'fetch': _adsb_fetch_adsbfi,         'requires_internet': True,  'requires_key': False, 'kind': 'network'},
    'airplaneslive':   {'label': 'airplanes.live (free, no key)',     'fetch': _adsb_fetch_airplaneslive,  'requires_internet': True,  'requires_key': False, 'kind': 'network'},
    'opensky':         {'label': 'OpenSky Network (free, optional auth)', 'fetch': _adsb_fetch_opensky,    'requires_internet': True,  'requires_key': False, 'kind': 'network'},
    'adsbexchange':    {'label': 'ADS-B Exchange (RapidAPI key)',     'fetch': _adsb_fetch_adsbexchange,   'requires_internet': True,  'requires_key': True,  'kind': 'network'},
    'dump1090':        {'label': 'Local SDR · dump1090 / readsb / tar1090 / PiAware (HackRF, RTL-SDR, AirSpy, SDRplay)',
                                                                       'fetch': _adsb_fetch_dump1090,       'requires_internet': False, 'requires_key': False, 'kind': 'local'},
    'beast':           {'label': 'Beast TCP raw feed (advanced — pipe via dump1090 first)',
                                                                       'fetch': _adsb_fetch_beast,          'requires_internet': False, 'requires_key': False, 'kind': 'local'},
}


# Common URL presets the UI exposes when 'dump1090' is selected. Lets you click
# the SDR/setup you have instead of pasting a URL.
DUMP1090_PRESETS = [
    {'label': 'dump1090-fa default (localhost:8080)',           'url': 'http://localhost:8080/data/aircraft.json'},
    {'label': 'tar1090 / readsb (localhost:8080/tar1090/)',     'url': 'http://localhost:8080/tar1090/data/aircraft.json'},
    {'label': 'PiAware on Raspberry Pi (raspberrypi.local:8080)', 'url': 'http://raspberrypi.local:8080/data/aircraft.json'},
    {'label': 'dump1090 classic (localhost:8754)',              'url': 'http://localhost:8754/data/aircraft.json'},
    {'label': 'HackRF + dump1090 (localhost:8080)',             'url': 'http://localhost:8080/data/aircraft.json'},
    {'label': 'Remote receiver (custom IP, port 8080)',         'url': 'http://192.168.1.X:8080/data/aircraft.json'},
]


# Last-poll metadata exposed via /api/adsb/aircraft so the UI can show truth
ADSB_STATUS = {
    'last_poll': 0.0,
    'last_count': 0,
    'last_source': '',
    'last_error': '',
    'consec_errors': 0,
    'global_mode': False,          # True while the auto-global OpenSky fallback is active
}


# ── Auto-global fallback ──────────────────────────────────────────────────
# The free circle feeds (adsb.lol / adsb.fi / airplanes.live) cap at a 250nm
# radius per call, and we tile a viewport into at most ~36 such circles — enough
# for continental scale, but a world/hemispheric zoom-out would need thousands of
# circles, so it only shows sparse patches. When the viewport gets that wide we
# transparently switch to OpenSky's global /states/all feed (the one source with a
# real firehose), then switch back to the fast circle feed on zoom-in. The user's
# saved source is never mutated — this is a per-cycle override decided from bbox.
_ADSB_CIRCLE_SOURCES = ('adsblol', 'adsbfi', 'airplaneslive')
_adsb_global_active = False          # hysteresis latch (shared single-viewport state)
_adsb_last_global_fetch = 0.0        # wall-clock of the last global upstream hit
ADSB_GLOBAL_MIN_INTERVAL = 30        # seconds — throttle to protect OpenSky's quota


def _adsb_bbox_max_dim_nm(bbox) -> float:
    """Largest viewport dimension in nautical miles (antimeridian-aware)."""
    try:
        w, s, e, n = bbox
    except (TypeError, ValueError):
        return 0.0
    e_eff = e if e >= w else e + 360.0
    lat_center = (s + n) / 2.0
    nm_per_deg_lon = max(1.0, 60.0 * math.cos(math.radians(lat_center)))
    width_nm = (e_eff - w) * nm_per_deg_lon
    height_nm = (n - s) * 60.0
    return max(width_nm, height_nm)


def _adsb_effective_source(cfg) -> tuple:
    """Pick the source to actually poll this cycle: (src_id, is_global).

    Engages the OpenSky global fallback only when (a) the fallback is enabled,
    (b) the configured source is a circle feed, and (c) the viewport is wider
    than circle-tiling can cover. Hysteresis (engage >3200nm, release <2400nm)
    keeps the source from flapping when the user lingers near the threshold."""
    global _adsb_global_active
    configured = cfg.get('source', 'adsblol')
    if not cfg.get('auto_global', True) or configured not in _ADSB_CIRCLE_SOURCES:
        _adsb_global_active = False
        return configured, False
    bbox = cfg.get('bbox')
    if not (bbox and len(bbox) == 4):
        _adsb_global_active = False
        return configured, False
    max_dim = _adsb_bbox_max_dim_nm(bbox)
    if _adsb_global_active:
        if max_dim < 2400:
            _adsb_global_active = False
    elif max_dim > 3200:
        _adsb_global_active = True
    return ('opensky', True) if _adsb_global_active else (configured, False)


def _adsb_poller_loop():
    """Background poller; respects ADSB_CONFIG['enabled'] and SHUTDOWN_EVENT."""
    sess = requests.Session()
    sess.headers.update({'User-Agent': 'drone-mesh-mapper/adsb (https://github.com/colonelpanichacks/drone-mesh-mapper)'})
    # Big connection pool — we fire up to 16 parallel tile fetches per poll, so
    # the default urllib3 pool of 10 thrashes. 32 keeps it cool with headroom.
    _big_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
    sess.mount('https://', _big_adapter)
    sess.mount('http://', _big_adapter)
    while not SHUTDOWN_EVENT.is_set():
        try:
            cfg = dict(ADSB_CONFIG)  # snapshot
            if not cfg.get('enabled'):
                if SHUTDOWN_EVENT.wait(2.0): break
                continue

            src_id, is_global = _adsb_effective_source(cfg)
            src = ADSB_SOURCES.get(src_id)
            if not src:
                ADSB_STATUS['last_error'] = f"unknown source '{src_id}'"
                logger.warning(f"adsb: unknown source '{src_id}'")
                if SHUTDOWN_EVENT.wait(5.0): break
                continue

            ADSB_STATUS['last_source'] = src_id
            ADSB_STATUS['global_mode'] = is_global
            try:
                aircraft = src['fetch'](cfg, sess)
                ADSB_STATUS['consec_errors'] = 0
                ADSB_STATUS['last_error'] = ''
            except requests.RequestException as e:
                ADSB_STATUS['consec_errors'] += 1
                ADSB_STATUS['last_error'] = f"{type(e).__name__}: {e}"
                logger.debug(f"adsb fetch failed ({src_id}): {e}")
                wait = min(60, 2 ** min(ADSB_STATUS['consec_errors'], 5))
                if SHUTDOWN_EVENT.wait(wait): break
                continue
            except Exception as e:
                ADSB_STATUS['consec_errors'] += 1
                ADSB_STATUS['last_error'] = f"{type(e).__name__}: {e}"
                logger.exception(f"adsb fetcher crashed ({src_id})")
                if SHUTDOWN_EVENT.wait(10.0): break
                continue

            now = time.time()
            with ADSB_AIRCRAFT_LOCK:
                # Update / insert with OSINT classification
                for a in aircraft[:ADSB_MAX_AIRCRAFT]:
                    icao = a.get('icao')
                    if not icao:
                        continue
                    a['tags'] = classify_aircraft(a)
                    ADSB_AIRCRAFT[icao] = a
                # Evict stale
                stale = [k for k, v in ADSB_AIRCRAFT.items() if (now - v.get('seen', 0)) > ADSB_STALE_AFTER]
                for k in stale:
                    ADSB_AIRCRAFT.pop(k, None)
                snapshot = list(ADSB_AIRCRAFT.values())

            ADSB_STATUS['last_poll'] = now
            ADSB_STATUS['last_count'] = len(aircraft)
            if is_global:
                globals()['_adsb_last_global_fetch'] = now

            # Geofence check for aircraft — only fences whose target_kind includes
            # aircraft will trigger. Drone fences are skipped automatically.
            try:
                check_aircraft_against_fences(snapshot)
            except Exception:
                logger.debug("aircraft geofence check failed", exc_info=True)

            try:
                socketio.emit('adsb', {
                    'aircraft': snapshot, 'source': src_id, 'count': len(snapshot),
                    'fetched': len(aircraft), 'error': '',
                })
            except Exception:
                pass

            interval = max(2, int(cfg.get('interval', ADSB_DEFAULT_INTERVAL)))
            if is_global:
                # World-wide OpenSky calls are quota-expensive — poll much slower
                # than the regional circle feed. The client keeps reading the cache
                # at its normal cadence; dead reckoning carries planes between.
                interval = max(interval, ADSB_GLOBAL_MIN_INTERVAL)
            if SHUTDOWN_EVENT.wait(interval):
                break
        except Exception:
            logger.exception("adsb poller iteration crashed")
            if SHUTDOWN_EVENT.wait(5.0): break
    try: sess.close()
    except Exception: pass
    logger.info("adsb poller exiting")


_adsb_poller_started = threading.Event()


def _start_adsb_poller():
    if _adsb_poller_started.is_set():
        return
    _adsb_poller_started.set()
    threading.Thread(target=_adsb_poller_loop, daemon=True, name="adsb-poller").start()


def _adsb_kick_fetch():
    """One-shot immediate fetch so the user sees aircraft within ~1 second of
    flicking the toggle, instead of waiting for the next poll cycle. Background
    thread; never blocks the request."""
    global _adsb_last_global_fetch
    cfg = dict(ADSB_CONFIG)
    if not cfg.get('enabled'):
        return
    src_id, is_global = _adsb_effective_source(cfg)
    # At world zoom the kick fires on every pan. Don't burn OpenSky's quota with a
    # fresh global call each time — if we hit it recently, let the cache + the slow
    # poller serve this one.
    if is_global and (time.time() - _adsb_last_global_fetch) < ADSB_GLOBAL_MIN_INTERVAL:
        return
    src = ADSB_SOURCES.get(src_id)
    if not src:
        return
    sess = requests.Session()
    sess.headers.update({'User-Agent': 'drone-mesh-mapper/adsb-kick'})
    _big_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
    sess.mount('https://', _big_adapter)
    sess.mount('http://', _big_adapter)
    try:
        aircraft = src['fetch'](cfg, sess)
        now = time.time()
        with ADSB_AIRCRAFT_LOCK:
            for a in aircraft[:ADSB_MAX_AIRCRAFT]:
                icao = a.get('icao')
                if not icao:
                    continue
                a['tags'] = classify_aircraft(a)
                ADSB_AIRCRAFT[icao] = a
            snapshot = list(ADSB_AIRCRAFT.values())
        ADSB_STATUS['last_poll'] = now
        ADSB_STATUS['last_count'] = len(aircraft)
        ADSB_STATUS['last_source'] = src_id
        ADSB_STATUS['global_mode'] = is_global
        ADSB_STATUS['last_error'] = ''
        if is_global:
            _adsb_last_global_fetch = now
        try:
            socketio.emit('adsb', {
                'aircraft': snapshot, 'source': src_id,
                'count': len(snapshot), 'fetched': len(aircraft), 'error': '',
            })
        except Exception:
            pass
    except Exception as e:
        ADSB_STATUS['last_error'] = f"{type(e).__name__}: {e}"
        try:
            socketio.emit('adsb', {'aircraft': [], 'source': src_id,
                                   'count': 0, 'fetched': 0, 'error': ADSB_STATUS['last_error']})
        except Exception:
            pass
        logger.debug(f"adsb kick fetch failed: {e}")
    finally:
        try: sess.close()
        except Exception: pass


# ----- API -----

@app.route('/api/adsb/sources', methods=['GET'])
def api_adsb_sources():
    return jsonify({
        'sources': [{'id': k, **{kk: vv for kk, vv in v.items() if kk != 'fetch'}}
                    for k, v in ADSB_SOURCES.items()],
        'dump1090_presets': DUMP1090_PRESETS,
    })


@app.route('/api/adsb/config', methods=['GET'])
def api_adsb_config_get():
    # Don't leak credentials in plain text — mask them
    safe = dict(ADSB_CONFIG)
    if safe.get('opensky_pass'):
        safe['opensky_pass'] = '***'
    if safe.get('adsbx_key'):
        safe['adsbx_key'] = '***'
    return jsonify(safe)


@app.route('/api/adsb/config', methods=['POST'])
def api_adsb_config_set():
    data = request.get_json(force=True, silent=True) or {}
    # Whitelist keys
    for k in ('enabled', 'source', 'interval', 'bbox',
              'opensky_user', 'opensky_pass', 'adsbx_key',
              'dump1090_url', 'beast_host', 'beast_port', 'auto_global'):
        if k in data:
            v = data[k]
            if k in ('enabled', 'auto_global'):
                ADSB_CONFIG[k] = bool(v)
            elif k == 'interval':
                try: ADSB_CONFIG[k] = max(2, min(120, int(v)))
                except (TypeError, ValueError): pass
            elif k == 'beast_port':
                try: ADSB_CONFIG[k] = max(1, min(65535, int(v)))
                except (TypeError, ValueError): pass
            elif k == 'source':
                if v in ADSB_SOURCES:
                    ADSB_CONFIG[k] = v
            elif k == 'bbox':
                if v is None:
                    ADSB_CONFIG[k] = None
                else:
                    try:
                        ADSB_CONFIG[k] = list(_validate_bbox(v))
                    except ValueError as e:
                        return jsonify({'error': f'bbox: {e}'}), 400
            elif k in ('opensky_pass', 'adsbx_key'):
                if v != '***':                          # don't overwrite with the masked sentinel
                    ADSB_CONFIG[k] = str(v or '')
            else:
                ADSB_CONFIG[k] = str(v or '')
    _adsb_save_config()
    # Stop Beast reader if user switched away from it or disabled ADS-B entirely
    if not ADSB_CONFIG['enabled'] or ADSB_CONFIG['source'] != 'beast':
        _stop_beast_thread()
    if ADSB_CONFIG['enabled']:
        _start_adsb_poller()
        # Don't make the user wait a full poll cycle to see aircraft after toggling
        threading.Thread(target=_adsb_kick_fetch, daemon=True, name="adsb-kick").start()
    return jsonify({'ok': True})


# ─────────── Simulated drone flight (test/demo) ───────────
# Lets the user spawn a fake drone that flies a configurable path so they can
# verify the whole UI pipeline (markers, paths, popups, geofence triggers,
# webhooks) without needing a real Remote-ID broadcast nearby.
SIM_DRONE = {
    'thread': None,
    'stop_event': None,
    'mac': None,
}
SIM_DRONE_LOCK = threading.Lock()

def _simulate_drone_flight(mac: str, center_lat: float, center_lon: float,
                            radius_m: float, alt_m: float, speed_mps: float,
                            stop_event: threading.Event):
    """Fly a fake drone in a circle around (center_lat, center_lon). Each
    tick writes a fully-formed detection into tracked_pairs and emits the
    `detection` socket event, exactly mimicking a real Remote-ID drone."""
    EARTH_R = 6378137.0
    # Convert radius in meters → angular delta
    dlat_per_m = 1.0 / 111320.0
    dlon_per_m = 1.0 / (111320.0 * math.cos(math.radians(center_lat)))
    # Pilot stays put a bit south of the center (a realistic spotter spot)
    pilot_lat = center_lat - 200 * dlat_per_m
    pilot_lon = center_lon
    # Angular velocity to give us speed_mps along the circle
    omega = speed_mps / max(radius_m, 1.0)   # rad/s
    t0 = time.time()
    tick = 0
    basic_id = 'SIM-DEMO-' + mac.replace(':', '')[-6:].upper()
    while not stop_event.is_set():
        t = time.time() - t0
        theta = omega * t
        d_lat = center_lat + radius_m * dlat_per_m * math.sin(theta)
        d_lon = center_lon + radius_m * dlon_per_m * math.cos(theta)
        # Heading is tangent to the circle (90° + theta in deg, normalized)
        heading_deg = (math.degrees(theta) + 90.0) % 360.0
        # Vertical sway gives a more interesting altitude trace
        alt_now = alt_m + 30.0 * math.sin(theta * 2)
        det = {
            'mac': mac,
            'rssi': -60,
            'drone_lat': float(d_lat),
            'drone_long': float(d_lon),
            'drone_altitude': float(alt_now),
            'pilot_lat': float(pilot_lat),
            'pilot_long': float(pilot_lon),
            'basic_id': basic_id,
            'speed': float(speed_mps),
            'heading': float(heading_deg),
            'last_update': time.time(),
            '_simulated': True,
        }
        try:
            tracked_pairs[mac] = det
            try:
                check_drone_against_fences(mac, d_lat, d_lon)
            except Exception:
                pass
            socketio.emit('detection', det)
        except Exception as e:
            logger.debug(f"sim emit failed: {e}")
        tick += 1
        if stop_event.wait(0.5):    # 2 Hz updates
            break
    logger.info(f"sim drone {mac} stopped after {tick} ticks")

@app.route('/api/simulate_drone/start', methods=['POST'])
def api_simulate_drone_start():
    """Start (or restart) a simulated drone flight. Body params (all optional):
       lat, lon, radius_m, alt_m, speed_mps, mac. Defaults fly a 500 m circle
       around downtown Asheville at 120 m altitude, 15 m/s ground speed."""
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data.get('lat', 35.5951))   # downtown Asheville
        lon = float(data.get('lon', -82.5515))
        radius_m = max(50.0, min(20_000.0, float(data.get('radius_m', 500.0))))
        alt_m    = max(10.0, min(10_000.0, float(data.get('alt_m', 120.0))))
        speed_mps = max(1.0, min(80.0, float(data.get('speed_mps', 15.0))))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid numeric params'}), 400
    mac = (data.get('mac') or 'aa:bb:cc:de:f0:01').lower().strip()
    with SIM_DRONE_LOCK:
        # Stop any existing simulation first
        if SIM_DRONE['thread'] and SIM_DRONE['thread'].is_alive():
            SIM_DRONE['stop_event'].set()
            SIM_DRONE['thread'].join(timeout=2.0)
        ev = threading.Event()
        th = threading.Thread(
            target=_simulate_drone_flight,
            args=(mac, lat, lon, radius_m, alt_m, speed_mps, ev),
            daemon=True,
            name='sim-drone',
        )
        SIM_DRONE['thread'] = th
        SIM_DRONE['stop_event'] = ev
        SIM_DRONE['mac'] = mac
        th.start()
    logger.info(f"sim drone started: mac={mac} center=({lat},{lon}) r={radius_m}m alt={alt_m}m v={speed_mps}m/s")
    return jsonify({
        'ok': True, 'mac': mac, 'lat': lat, 'lon': lon,
        'radius_m': radius_m, 'alt_m': alt_m, 'speed_mps': speed_mps,
    })

@app.route('/api/simulate_drone/stop', methods=['POST'])
def api_simulate_drone_stop():
    """Stop the active simulated drone, if any."""
    with SIM_DRONE_LOCK:
        if SIM_DRONE['thread'] and SIM_DRONE['thread'].is_alive():
            SIM_DRONE['stop_event'].set()
            SIM_DRONE['thread'].join(timeout=2.0)
            mac = SIM_DRONE['mac']
            SIM_DRONE['thread'] = None
            SIM_DRONE['stop_event'] = None
            return jsonify({'ok': True, 'stopped': mac})
    return jsonify({'ok': True, 'stopped': None})

@app.route('/api/adsb/aircraft', methods=['GET'])
def api_adsb_aircraft():
    """Return aircraft from the in-memory cache.

    Optional query params reduce payload size for fast client polling at
    continental zoom:
      ?bbox=w,s,e,n   — return only aircraft inside this bbox (5% padded)
      ?limit=N        — hard cap (default 20000, effectively unlimited unless
                        explicitly capped). Pass 0 for no cap.
      ?fields=mini    — strip non-essential fields (no category, no squawk
                        text) to halve the JSON size when used together
    """
    with ADSB_AIRCRAFT_LOCK:
        snapshot = list(ADSB_AIRCRAFT.values())
    # Bbox filter — cheap point-in-rect; antimeridian-aware.
    bbox_str = request.args.get('bbox')
    if bbox_str:
        try:
            w, s, e, n = [float(x) for x in bbox_str.split(',')]
            # 5% padding so planes on the edge don't pop in/out during micro-pans
            lon_pad = (e - w) * 0.05 if e >= w else 0.5
            lat_pad = (n - s) * 0.05
            s2 = max(-90.0, s - lat_pad)
            n2 = min(90.0, n + lat_pad)
            if e >= w:
                w2 = w - lon_pad
                e2 = e + lon_pad
                snapshot = [a for a in snapshot
                            if a.get('lat') is not None
                            and a.get('lon') is not None
                            and s2 <= a['lat'] <= n2
                            and w2 <= a['lon'] <= e2]
            else:
                # Antimeridian crossing: keep planes east of `w` OR west of `e`
                w2 = w - lon_pad
                e2 = e + lon_pad
                snapshot = [a for a in snapshot
                            if a.get('lat') is not None
                            and a.get('lon') is not None
                            and s2 <= a['lat'] <= n2
                            and (a['lon'] >= w2 or a['lon'] <= e2)]
        except (ValueError, TypeError):
            pass   # bad bbox → just return everything
    # Effectively unlimited by default — return every aircraft we have. Pass
    # ?limit=N to cap explicitly, ?limit=0 to disable any cap.
    try:
        limit_raw = int(request.args.get('limit', 20000))
        if limit_raw <= 0:
            limit = None
        else:
            limit = max(1, min(50000, limit_raw))
    except (ValueError, TypeError):
        limit = 20000
    truncated = False
    if limit is not None and len(snapshot) > limit:
        snapshot = snapshot[:limit]
        truncated = True
    # Optional mini payload — drop fields the map doesn't strictly need to
    # roughly halve the wire size on continental views.
    if request.args.get('fields') == 'mini':
        snapshot = [{
            'icao': a.get('icao'),
            'callsign': a.get('callsign'),
            'lat': a.get('lat'), 'lon': a.get('lon'),
            'alt_baro': a.get('alt_baro'),
            'velocity': a.get('velocity'),
            'heading': a.get('heading'),
            'tags': a.get('tags'),
            'on_ground': a.get('on_ground'),
            'category': a.get('category'),
            # 'seen' = observation time of this fix. The client back-dates its
            # dead-reckoning anchor by (now - seen) so a fix that's already a few
            # seconds old isn't placed behind the marker's extrapolated position
            # (which made markers snap backward on every poll).
            'seen': a.get('seen'),
        } for a in snapshot]
    return jsonify({
        'aircraft': snapshot,
        'count': len(snapshot),
        'truncated': truncated,
        # Report the source actually feeding the cache (so the UI shows "opensky"
        # when the auto-global fallback is engaged), falling back to the configured
        # source before the first poll completes.
        'source': ADSB_STATUS.get('last_source') or ADSB_CONFIG.get('source'),
        'global_mode': ADSB_STATUS.get('global_mode', False),
        'status': dict(ADSB_STATUS),
    })


# Per-aircraft full historical trace from the upstream readsb-style globe feeds.
# The user wants the path toggle to reveal the FULL flight, not just what we
# happened to see while running. adsb.lol / adsb.fi / airplanes.live all expose
# the same readsb trace files at /data/traces/<last2>/trace_full_<icao>.json.
# A trace is a list of [time_offset, lat, lon, altitude, ground_speed, heading,
# flags, vert_rate, ...] tuples relative to the file's "timestamp" base.
_TRACE_HOSTS = {
    'adsblol':       'https://globe.adsb.lol',
    'adsbfi':        'https://globe.adsb.fi',
    'airplaneslive': 'https://globe.airplanes.live',
}

# globe.adsb.fi / globe.airplanes.live return 403 to non-browser User-Agents — so
# the old 'drone-mesh-mapper/trace' UA got rejected and those fallbacks never worked
# (only adsb.lol, which doesn't check UA, ever served traces). A browser-like UA +
# Referer gets HTTP 200 from all of them. This is what actually fixes flight paths.
_TRACE_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')

def _trace_session():
    """Fast, NO-retry session for high-volume trace fetches.

    Traces were being fetched with create_retry_session() — the FAA-query session
    (retries=3, backoff_factor=2). A single slow/hanging host then became
    ~8s timeout x (1 + 3 retries) + 2s/4s/8s backoff, repeated across 2 URLs x 3
    hosts = a MINUTES-long stall per aircraft. So the real flight trace never
    arrived in time and the trail fell back to the dead-reckoned straight line.
    Traces are best-effort: fail fast, move on. Browser-like default headers so the
    globe.* hosts don't 403 us."""
    s = requests.Session()
    s.headers.update({'User-Agent': _TRACE_UA, 'Accept': 'application/json,text/plain,*/*'})
    a = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
    s.mount('https://', a)
    s.mount('http://', a)
    return s

# In-memory trace cache so a path fetched once draws INSTANTLY on the next toggle,
# reap-return, or for another client — instead of re-hitting the upstream globe
# feed every time (the source of the load delay). Positive entries live _TRACE_TTL;
# "no trace" results are negative-cached briefly so planes with no recorded track
# aren't re-hammered on every toggle.
_TRACE_CACHE = {}
_TRACE_CACHE_LOCK = threading.Lock()
_TRACE_TTL = 120.0
_TRACE_NEG_TTL = 45.0
_TRACE_CACHE_MAX = 4000

def _trace_cache_get(icao):
    """Return ('hit', points) / ('neg', None) / None (miss or expired)."""
    with _TRACE_CACHE_LOCK:
        e = _TRACE_CACHE.get(icao)
    if not e:
        return None
    ts, pts = e
    age = time.time() - ts
    if pts is None:
        return ('neg', None) if age < _TRACE_NEG_TTL else None
    return ('hit', pts) if age < _TRACE_TTL else None

def _trace_cache_put(icao, pts):
    with _TRACE_CACHE_LOCK:
        if len(_TRACE_CACHE) >= _TRACE_CACHE_MAX:
            for k in sorted(_TRACE_CACHE, key=lambda k: _TRACE_CACHE[k][0])[:_TRACE_CACHE_MAX // 5]:
                _TRACE_CACHE.pop(k, None)
        _TRACE_CACHE[icao] = (time.time(), pts)

def _fetch_trace_points(icao, sess, max_points=400):
    """Fetch + decimate one aircraft's upstream historical track.
    Returns (points, source) or (None, None). Checks the in-memory cache first,
    then tries the configured source, then the others. Decimates evenly to
    <= max_points (endpoints preserved) so a 2,000-point flight doesn't bloat the
    wire or the polyline render."""
    icao = (icao or '').strip().lower()
    if not icao or not all(c in '0123456789abcdef' for c in icao) or len(icao) > 8:
        return None, None
    cached = _trace_cache_get(icao)
    if cached is not None:
        kind, pts = cached
        return (pts, 'cache') if kind == 'hit' else (None, None)
    last2 = icao[-2:].zfill(2)
    def _parse(r):
        if r.status_code != 200:
            return None
        try:
            trace = (r.json().get('trace') or [])
        except Exception:
            return None
        pts = []
        for row in trace:
            if not isinstance(row, list) or len(row) < 3:
                continue
            lat, lon = row[1], row[2]
            if lat is None or lon is None:
                continue
            try:
                lat = float(lat); lon = float(lon)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            pts.append([lat, lon])
        if not pts:
            return None
        if len(pts) > max_points:
            step = (len(pts) + max_points - 1) // max_points
            dec = pts[::step]
            if dec[-1] != pts[-1]:
                dec.append(pts[-1])
            pts = dec
        return pts
    def _one(s):
        host = _TRACE_HOSTS[s]
        # Per-host Referer (the globe.* hosts want it); (connect, read) timeout so a
        # slow/blocked host fails fast. trace_full preferred, trace_recent fallback.
        for fn in ('trace_full', 'trace_recent'):
            try:
                r = sess.get(f"{host}/data/traces/{last2}/{fn}_{icao}.json",
                             timeout=(2.0, 4), headers={'Referer': host + '/'})
                pts = _parse(r)
                if pts:
                    return pts
            except Exception:
                continue
        return None
    # Race ALL trace hosts in parallel; take the first that returns a usable track.
    # A single slow/redirecting host (e.g. adsb.lol's 302) no longer stalls the whole
    # fetch — latency is the fastest host (~1s), not the sum of sequential timeouts.
    # That sequential walk is exactly why toggling one plane's path felt "sooooo
    # delayed". shutdown(wait=False) so we don't block on the slow losers.
    hosts = list(_TRACE_HOSTS.keys())
    ex = ThreadPoolExecutor(max_workers=len(hosts))
    try:
        futs = {ex.submit(_one, s): s for s in hosts}
        for fut in as_completed(futs):
            try:
                pts = fut.result()
            except Exception:
                pts = None
            if pts:
                _trace_cache_put(icao, pts)
                return pts, futs[fut]
    finally:
        ex.shutdown(wait=False)
    _trace_cache_put(icao, None)   # negative-cache: no recorded trace right now
    return None, None


@app.route('/api/adsb/trace/<icao>', methods=['GET'])
def api_adsb_trace(icao):
    """Single-aircraft historical track (used by the per-plane popup toggle)."""
    pts, s = _fetch_trace_points(icao, _trace_session())
    if pts:
        return jsonify({'ok': True, 'source': s, 'icao': (icao or '').strip().lower(),
                        'points': pts, 'count': len(pts)})
    return jsonify({'ok': False, 'error': 'no trace available'}), 200


@app.route('/api/adsb/traces', methods=['POST'])
def api_adsb_traces():
    """BATCH historical tracks — fetch many ICAOs' traces in parallel server-side
    so the BULK path controls (category chips / ALL IN VIEW) load fast instead of
    crawling through hundreds of one-at-a-time browser requests (capped ~6 per
    host). Body: {icaos:[...]}. Returns {traces:{icao:[[lat,lon],...]}}."""
    data = request.get_json(silent=True) or {}
    raw = data.get('icaos') or []
    if not isinstance(raw, list):
        return jsonify({'error': 'icaos must be a list'}), 400
    clean, seen = [], set()
    for x in raw:
        ic = str(x or '').strip().lower()
        if ic and ic not in seen and len(ic) <= 8 and all(c in '0123456789abcdef' for c in ic):
            seen.add(ic); clean.append(ic)
        if len(clean) >= 150:   # per-request cap; client sends multiple batches
            break
    out = {}
    if clean:
        sess = _trace_session()
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = {ex.submit(_fetch_trace_points, ic, sess): ic for ic in clean}
            for fut in as_completed(futs):
                ic = futs[fut]
                try:
                    pts, _s = fut.result()
                    if pts:
                        out[ic] = pts
                except Exception:
                    pass
    return jsonify({'traces': out, 'count': len(out)})


# ----------------------
# Import MBTiles (URL download or multipart file upload)
# ----------------------
_import_jobs: dict = {}
_import_jobs_lock = threading.Lock()

IMPORT_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB sanity cap


def _validate_mbtiles_file(path: str) -> str:
    """Open an .mbtiles candidate; return '' if valid or an error message."""
    try:
        conn = sqlite3.connect(path)
        try:
            conn.execute("SELECT 1 FROM tiles LIMIT 1").fetchone()
            conn.execute("SELECT 1 FROM metadata LIMIT 1").fetchone()
            return ''
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return f"not a valid SQLite/mbtiles file: {e}"
    except sqlite3.Error as e:
        return f"sqlite error: {e}"


def _import_worker(job_id, url):
    job = _import_jobs[job_id]
    tmp = None
    try:
        try:
            path = _mbtiles_path(job['name'])
        except ValueError as e:
            job['status'] = 'error'
            job['error_msg'] = str(e)
            return
        if os.path.exists(path):
            job['status'] = 'error'
            job['error_msg'] = 'name already exists'
            return

        tmp = path + '.part'
        # Clean up any prior failed import
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

        job['status'] = 'running'
        with requests.get(url, stream=True, timeout=30, allow_redirects=True,
                          headers={'User-Agent': 'drone-mesh-mapper/import'}) as r:
            if r.status_code != 200:
                raise RuntimeError(f'HTTP {r.status_code} from {url}')
            total = 0
            cl = r.headers.get('Content-Length')
            if cl:
                try:
                    total = int(cl)
                    if total > IMPORT_MAX_BYTES:
                        raise RuntimeError(f'file too large ({total} bytes; max {IMPORT_MAX_BYTES})')
                except ValueError:
                    total = 0
            job['total'] = total

            # Pre-flight disk space check
            need = max(DISK_FREE_MIN_BYTES, total * 2 if total else DISK_FREE_MIN_BYTES)
            if not _disk_has_space(path, need):
                raise RuntimeError(f'not enough free disk space for {total} bytes')

            with open(tmp, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if job.get('cancel'):
                        job['status'] = 'cancelled'
                        return
                    if SHUTDOWN_EVENT.is_set():
                        job['status'] = 'cancelled'
                        job['error_msg'] = 'server shutdown during import'
                        return
                    if not chunk:
                        continue
                    f.write(chunk)
                    job['done'] += len(chunk)
                    if job['done'] > IMPORT_MAX_BYTES:
                        raise RuntimeError(f'exceeded max import size ({IMPORT_MAX_BYTES})')

        # Validate
        err = _validate_mbtiles_file(tmp)
        if err:
            raise RuntimeError(err)
        # Atomic move into place
        os.replace(tmp, path)
        tmp = None
        job['status'] = 'done'
        logger.info(f"imported '{job['name']}' from {url} ({job['done']} bytes)")
    except requests.RequestException as e:
        job['status'] = 'error'
        job['error_msg'] = f"network error: {e}"
        logger.warning(f"import job {job_id} failed: {e}")
    except Exception as e:
        job['status'] = 'error'
        job['error_msg'] = f"{type(e).__name__}: {e}"
        logger.exception(f"import job {job_id} crashed")
    finally:
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

@app.route('/api/import_mbtiles', methods=['POST'])
def api_import_mbtiles():
    """Two modes:
       - JSON {name, url}        → background download
       - multipart file upload   → synchronous save (small files)"""
    if request.is_json:
        data = request.get_json(force=True, silent=True) or {}
        name = (data.get('name') or '').strip()
        url = (data.get('url') or '').strip()
        if not name or not url:
            return jsonify({'error': 'name and url required'}), 400
        try:
            target = _mbtiles_path(name)
        except ValueError:
            return jsonify({'error': 'name must be alphanumeric / dash / underscore'}), 400
        if os.path.exists(target):
            return jsonify({'error': 'name already exists'}), 400
        job_id = uuid.uuid4().hex[:12]
        job = {'id': job_id, 'name': name, 'url': url, 'status': 'queued',
               'done': 0, 'total': 0, 'cancel': False}
        with _import_jobs_lock:
            _import_jobs[job_id] = job
        threading.Thread(target=_import_worker, args=(job_id, url), daemon=True).start()
        return jsonify(job)

    # multipart upload
    name = (request.form.get('name') or '').strip()
    f = request.files.get('file')
    if not name or not f:
        return jsonify({'error': 'name and file required'}), 400
    try:
        target = _mbtiles_path(name)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if os.path.exists(target):
        return jsonify({'error': 'name already exists'}), 400
    tmp = target + '.part'
    try:
        f.save(tmp)
        size = os.path.getsize(tmp)
        if size > IMPORT_MAX_BYTES:
            os.remove(tmp)
            return jsonify({'error': f'file too large ({size} bytes; max {IMPORT_MAX_BYTES})'}), 413
        err = _validate_mbtiles_file(tmp)
        if err:
            os.remove(tmp)
            return jsonify({'error': err}), 400
        os.replace(tmp, target)
    except OSError as e:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
        return jsonify({'error': f'I/O error: {e}'}), 500
    logger.info(f"uploaded '{name}' ({size} bytes)")
    return jsonify({'ok': True, 'name': name, 'size_bytes': os.path.getsize(target)})

@app.route('/api/import_jobs/<job_id>', methods=['GET'])
def api_import_job(job_id):
    with _import_jobs_lock:
        job = _import_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify(job)

@app.route('/api/import_jobs/<job_id>/cancel', methods=['POST'])
def api_import_job_cancel(job_id):
    with _import_jobs_lock:
        job = _import_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    job['cancel'] = True
    return jsonify({'ok': True})


@app.route('/api/cache_jobs/<job_id>/cancel', methods=['POST'])
def api_cache_job_cancel(job_id):
    with CACHE_JOBS_LOCK:
        job = CACHE_JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    job['cancel'] = True
    _save_cache_jobs()
    return jsonify({'ok': True})

@app.route('/api/cache_jobs/<job_id>/resume', methods=['POST'])
def api_cache_job_resume(job_id):
    """Re-spawn the worker for a paused/error/cancelled job. Already-cached tiles
    are skipped automatically thanks to the SQLite primary key."""
    with CACHE_JOBS_LOCK:
        job = CACHE_JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    if job['status'] == 'running':
        return jsonify({'error': 'already running'}), 400
    job['cancel'] = False
    job['status'] = 'queued'
    job['pause_reason'] = ''
    job['consec_errors'] = 0
    _save_cache_jobs()
    threading.Thread(target=_cache_worker, args=(job_id,), daemon=True).start()
    return jsonify(job)

@app.route('/api/cache_jobs/<job_id>', methods=['DELETE'])
def api_cache_job_delete(job_id):
    """Forget a finished/cancelled/paused job. Doesn't delete the underlying mbtiles."""
    with CACHE_JOBS_LOCK:
        job = CACHE_JOBS.pop(job_id, None)
    if not job:
        return jsonify({'error': 'not found'}), 404
    if job.get('status') == 'running':
        # put it back; can't delete a running job — cancel first
        with CACHE_JOBS_LOCK:
            CACHE_JOBS[job_id] = job
        return jsonify({'error': 'cancel first'}), 400
    _save_cache_jobs()
    return jsonify({'ok': True})


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
  <title>Select USB Serial Ports</title>
  <link href="/static/fonts/orbitron.css" rel="stylesheet">
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
      image-rendering: auto;
      will-change: transform;
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
  <h1>SETUP · Mesh Mapper</h1>
  <div style="max-width:560px; margin:0 auto; text-align:left; padding:0 16px;">
    <p style="font-size:13px; color:#88ddff; text-align:center;">All settings save to disk and are mirrored in the in-UI Settings panel. Skip any section and configure later.</p>

    <form method="POST" action="/select_ports" style="display:block; text-align:left;">
      <!-- ============ USB · DRONE RECEIVERS ============ -->
      <fieldset style="border:1px solid lime; padding:8px 12px; margin-bottom:14px; border-radius:6px;">
        <legend style="color:#aaffaa; padding:0 6px; font-size:14px;">USB · DRONE RECEIVERS</legend>
        <div style="font-size:12px; color:#779977; margin-bottom:6px;">ESP32 boards forwarding Remote-ID detections over serial. Up to 3.</div>
        <label style="font-size:14px;">Port 1:</label><br>
        <select id="port1" name="port1" style="width:100%;"><option value="">--None--</option>{% for port in ports %}<option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>{% endfor %}</select><br>
        <label style="font-size:14px;">Port 2:</label><br>
        <select id="port2" name="port2" style="width:100%;"><option value="">--None--</option>{% for port in ports %}<option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>{% endfor %}</select><br>
        <label style="font-size:14px;">Port 3:</label><br>
        <select id="port3" name="port3" style="width:100%;"><option value="">--None--</option>{% for port in ports %}<option value="{{ port.device }}">{{ port.device }} - {{ port.description }}</option>{% endfor %}</select>
      </fieldset>

      <!-- ============ ADS-B · AIR TRAFFIC ============ -->
      <fieldset style="border:1px solid #00aaff; padding:8px 12px; margin-bottom:14px; border-radius:6px;">
        <legend style="color:#aaccff; padding:0 6px; font-size:14px;">ADS-B · AIR TRAFFIC</legend>
        <div style="font-size:12px; color:#5588aa; margin-bottom:6px;">Network feed or local SDR (HackRF / RTL-SDR / AirSpy via dump1090, readsb, or Beast).</div>
        <label style="display:flex; align-items:center; gap:8px; font-size:14px; color:#aaccff;">
          <input type="checkbox" name="adsb_enabled" id="adsbEnabledChk" value="1">
          <span>Enable ADS-B layer at startup</span>
        </label>
        <div style="margin-top:8px;">
          <label style="font-size:13px; color:#88aaff;">Mode:</label>
          <div style="display:flex; gap:0; margin-top:4px; border:1px solid #00aaff; border-radius:3px; overflow:hidden;">
            <label style="flex:1; text-align:center; padding:5px; cursor:pointer; background:#001a2a;"><input type="radio" name="adsb_mode" value="online" checked style="display:none;" onchange="onAdsbModeChange()"><span id="modeLabelOnline">⌒ ONLINE</span></label>
            <label style="flex:1; text-align:center; padding:5px; cursor:pointer; background:#001a2a; border-left:1px solid #00aaff;"><input type="radio" name="adsb_mode" value="local" style="display:none;" onchange="onAdsbModeChange()"><span id="modeLabelLocal">⎘ LOCAL SDR</span></label>
          </div>
        </div>
        <div id="setupOnlineFields" style="margin-top:8px;">
          <label style="font-size:13px; color:#88aaff;">Network source:</label>
          <select name="adsb_online_source" id="setupOnlineSource" style="width:100%;">
            <option value="adsblol">adsb.lol (free, no key)</option>
            <option value="adsbfi">adsb.fi (free, no key)</option>
            <option value="airplaneslive">airplanes.live (free, no key)</option>
            <option value="opensky">OpenSky Network</option>
            <option value="adsbexchange">ADS-B Exchange (RapidAPI key)</option>
          </select>
        </div>
        <div id="setupLocalFields" style="display:none; margin-top:8px;">
          <label style="font-size:13px; color:#88aaff;">Local mode:</label>
          <select name="adsb_local_mode" id="setupLocalMode" style="width:100%;" onchange="onSetupLocalModeChange()">
            <option value="dump1090">dump1090 / readsb / tar1090 / PiAware (HTTP JSON)</option>
            <option value="beast">Beast TCP raw feed (pyModeS)</option>
          </select>
          <div id="setupDump1090Fields" style="margin-top:6px;">
            <label style="font-size:13px; color:#88aaff;">JSON URL:</label>
            <input type="text" name="adsb_dump1090_url" id="setupDump1090Url" value="http://localhost:8080/data/aircraft.json" style="width:100%; background:#222; color:#aaeeff; border:1px solid #00aaff; padding:3px;">
          </div>
          <div id="setupBeastFields" style="display:none; margin-top:6px;">
            <div style="display:flex; gap:6px;">
              <div style="flex:2;">
                <label style="font-size:13px; color:#88aaff;">Host:</label>
                <input type="text" name="adsb_beast_host" id="setupBeastHost" value="localhost" style="width:100%; box-sizing:border-box; background:#222; color:#aaeeff; border:1px solid #00aaff; padding:3px;">
              </div>
              <div style="flex:1;">
                <label style="font-size:13px; color:#88aaff;">Port:</label>
                <input type="number" name="adsb_beast_port" id="setupBeastPort" value="30005" min="1" max="65535" style="width:100%; box-sizing:border-box; background:#222; color:#aaeeff; border:1px solid #00aaff; padding:3px;">
              </div>
            </div>
          </div>
        </div>
      </fieldset>

      <!-- ============ MAP LAYER ============ -->
      <fieldset style="border:1px solid #aaffaa; padding:8px 12px; margin-bottom:14px; border-radius:6px;">
        <legend style="color:#aaffaa; padding:0 6px; font-size:14px;">MAP LAYER</legend>
        <div style="font-size:12px; color:#779977; margin-bottom:6px;">Default basemap on next page load. Offline-cached layers appear here once you've saved any.</div>
        <label style="font-size:13px;">Default basemap:</label>
        <select name="default_basemap" id="setupDefaultBasemap" style="width:100%;">
          <option value="esriWorldImagery">Esri World Imagery (satellite)</option>
          <option value="osmStandard">OSM Standard</option>
          <option value="osmHumanitarian">OSM Humanitarian</option>
          <option value="cartoDarkMatter">CartoDB Dark Matter</option>
          <option value="cartoPositron">CartoDB Positron</option>
          <option value="esriWorldTopo">Esri World TopoMap</option>
          <option value="esriDarkGray">Esri Dark Gray Canvas</option>
          <option value="openTopoMap">OpenTopoMap</option>
        </select>
      </fieldset>

      <!-- ============ WEBHOOK ============ -->
      <fieldset style="border:1px solid #ff66ff; padding:8px 12px; margin-bottom:14px; border-radius:6px;">
        <legend style="color:#ff99ff; padding:0 6px; font-size:14px;">WEBHOOK · ALERTS</legend>
        <div style="font-size:12px; color:#aa77aa; margin-bottom:6px;">Optional. Drone detections + geofence alerts POST to this URL.</div>
        <input type="text" name="webhook_url" id="webhookUrl" placeholder="https://example.com/webhook" style="width:100%; background:#222; color:#ff99ff; border:1px solid #ff66ff; padding:4px;">
      </fieldset>

      <div style="display:flex; gap:8px; margin-top:14px; margin-bottom:8px;">
        <a href="/" style="flex:1; text-align:center; padding:10px; border:1px solid #888; color:#888; background:#222; text-decoration:none; border-radius:6px; font-family:'Orbitron', monospace;">SKIP — TO MAP</a>
        <button type="submit" id="beginMapping" style="flex:2; padding:10px; border:1px solid lime; background:#333; color:lime; font-family:'Orbitron',monospace; font-size:1.05em; cursor:pointer; border-radius:6px; box-shadow:0 0 8px #f0f, 0 0 16px #0ff;">SAVE &amp; BEGIN MAPPING</button>
      </div>
    </form>
  </div>
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

    function onAdsbModeChange() {
      const m = document.querySelector('input[name="adsb_mode"]:checked').value;
      document.getElementById('setupOnlineFields').style.display = (m === 'online') ? 'block' : 'none';
      document.getElementById('setupLocalFields').style.display  = (m === 'local')  ? 'block' : 'none';
      // Highlight selected mode pill
      document.getElementById('modeLabelOnline').style.color = (m === 'online') ? '#fff' : '#aaeeff';
      document.getElementById('modeLabelLocal').style.color  = (m === 'local')  ? '#fff' : '#aaeeff';
      document.getElementById('modeLabelOnline').parentElement.style.background = (m === 'online') ? '#003355' : '#001a2a';
      document.getElementById('modeLabelLocal').parentElement.style.background  = (m === 'local')  ? '#003355' : '#001a2a';
    }
    function onSetupLocalModeChange() {
      const m = document.getElementById('setupLocalMode').value;
      document.getElementById('setupDump1090Fields').style.display = (m === 'dump1090') ? 'block' : 'none';
      document.getElementById('setupBeastFields').style.display    = (m === 'beast')    ? 'block' : 'none';
    }
    function loadAdsbDefaults() {
      fetch('/api/adsb/config').then(r => r.json()).then(cfg => {
        document.getElementById('adsbEnabledChk').checked = !!cfg.enabled;
        if (cfg.source === 'beast' || cfg.source === 'dump1090') {
          document.querySelector('input[name="adsb_mode"][value="local"]').checked = true;
          document.getElementById('setupLocalMode').value = cfg.source;
          onSetupLocalModeChange();
        } else if (cfg.source) {
          document.querySelector('input[name="adsb_mode"][value="online"]').checked = true;
          document.getElementById('setupOnlineSource').value = cfg.source;
        }
        if (cfg.dump1090_url) document.getElementById('setupDump1090Url').value = cfg.dump1090_url;
        if (cfg.beast_host)   document.getElementById('setupBeastHost').value   = cfg.beast_host;
        if (cfg.beast_port)   document.getElementById('setupBeastPort').value   = cfg.beast_port;
        onAdsbModeChange();
      }).catch(() => onAdsbModeChange());
    }
    function loadDefaultBasemap() {
      // Read from localStorage if the user has set one in-UI; falls back to esriWorldImagery
      const v = localStorage.getItem('basemap');
      if (v && document.getElementById('setupDefaultBasemap').querySelector('option[value="' + v + '"]')) {
        document.getElementById('setupDefaultBasemap').value = v;
      }
    }
    function loadWebhook() {
      fetch('/api/get_webhook_url').then(r => r.json()).then(d => {
        if (d && d.webhook_url) document.getElementById('webhookUrl').value = d.webhook_url;
      }).catch(() => {});
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
      loadAdsbDefaults();
      loadDefaultBasemap();
      loadWebhook();
    }
    // Persist the chosen default basemap to localStorage on submit so the map page
    // picks it up via its existing localStorage-backed selector
    document.querySelector('form').addEventListener('submit', () => {
      const v = document.getElementById('setupDefaultBasemap').value;
      try { localStorage.setItem('basemap', v); } catch(e){}
    });
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
  <!-- Add Socket.IO client script for real-time updates -->
  <script src="/static/socketio/socket.io.min.js"></script>
  <link rel="stylesheet" href="/static/leaflet/leaflet.css"/>
  <link href="/static/fonts/orbitron.css" rel="stylesheet">
  <script src="/static/leaflet/leaflet.js"></script>
  <!-- MapLibre GL + Leaflet plugin: lets us render OpenMapTiles vector layers as a Leaflet layer -->
  <link rel="stylesheet" href="/static/maplibre/maplibre-gl.css"/>
  <script src="/static/maplibre/maplibre-gl.js"></script>
  <script src="/static/maplibre/leaflet-maplibre-gl.js"></script>
  <!-- Leaflet.draw for geofence drawing/editing -->
  <link rel="stylesheet" href="/static/leaflet-draw/leaflet.draw.css"/>
  <script src="/static/leaflet-draw/leaflet.draw.js"></script>
  <style>
    /* Hide tile seams on all map layers + buttery-smooth scaling between
       zoom steps. Bilinear interpolation (the browser default) keeps tiles
       sharp at native zoom and lerps cleanly during animated zoom; the older
       `crisp-edges` rule chunked into nearest-neighbor pixels and looked
       cheap during fractional zoom. */
    .leaflet-tile {
      border: none !important;
      box-shadow: none !important;
      background-color: transparent !important;
      image-rendering: auto;
      will-change: transform;
      backface-visibility: hidden;
    }
    /* GPU-composited panes — keeps pan/zoom on the compositor thread instead
       of triggering full layout/paint passes. */
    .leaflet-pane,
    .leaflet-tile-container,
    .leaflet-marker-pane,
    .leaflet-shadow-pane,
    .leaflet-overlay-pane {
      will-change: transform;
    }
    .leaflet-container {
      background-color: black !important;
    }
    /* ───── Header toggle ovals (Drones panel ON/OFF, ADS-B panel ON/OFF) ─────
       Modern iOS-style: thin outlined oval, accent fill when ON, muted when
       OFF. Each panel sets its own accent via a CSS variable so the drone
       toggle reads lime and the ADS-B toggle reads cyan. */
    .switch {
      position: relative;
      display: inline-block;
      vertical-align: middle;
      width: 40px;
      height: 20px;
      --switch-accent: #88ff99;       /* default — drone lime */
      --switch-outline: rgba(255, 255, 255, 0.18);
    }
    .switch input { opacity: 0; width: 0; height: 0; }
    .slider {
      position: absolute;
      cursor: pointer;
      inset: 0;
      background: rgba(255, 255, 255, 0.06);
      border: 1.5px solid var(--switch-outline);
      transition: background .2s, border-color .2s;
      border-radius: 20px;
    }
    .slider:before {
      position: absolute;
      content: "";
      height: 14px;
      width: 14px;
      left: 2px;
      top: 50%;
      background-color: #ffffff;
      border: 0;
      transition: transform .2s, background-color .2s;
      border-radius: 50%;
      transform: translateY(-50%);
      box-shadow: 0 1px 2px rgba(0,0,0,0.4);
    }
    .switch input:checked + .slider {
      background: var(--switch-accent);
      border-color: var(--switch-accent);
    }
    .switch input:checked + .slider:before {
      transform: translateX(20px) translateY(-50%);
      background-color: #fff;
    }
    /* Per-panel accent overrides — applied by panel scope so the same .switch
       class shows lime in the drones panel and cyan in the AIR TRAFFIC panel. */
    #adsbBox .switch          { --switch-accent: #88c8ff; --switch-outline: rgba(136, 200, 255, 0.35); }
    #filterBox .switch        { --switch-accent: #88ff99; --switch-outline: rgba(136, 255, 153, 0.35); }
    #geofenceFloatBox .switch { --switch-accent: #ff7a99; --switch-outline: rgba(255, 122, 153, 0.35); }
    #settingsFloatBox .switch { --switch-accent: #88ff99; --switch-outline: rgba(136, 255, 153, 0.35); }
    #mapLayerFloatBox .switch { --switch-accent: #88ff99; --switch-outline: rgba(136, 255, 153, 0.35); }
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
          width: 280px;
          max-width: calc(100vw - 20px);
          border: 1px solid lime;
          border-radius: 10px;
          color: lime;
          font-family: monospace;
          /* No outer scroll — content sizes to fit. Active/Inactive lists
             have their own compact caps so the whole panel stays short. */
          overflow: visible;
          z-index: 1000;
        }
        /* Active / Inactive drone lists — bounded so the whole panel always
           fits without an outer scroll. Overflow stays internal to each list. */
        #activePlaceholder, #inactivePlaceholder {
          max-height: 28vh !important;
          min-height: 90px !important;
          flex: 0 0 auto;
          box-sizing: border-box;
        }
        /* Filter content is a vertical flex stack so each section reserves
           its own space and they never visually overlap. */
        #filterContent {
          display: flex;
          flex-direction: column;
          gap: 0;
        }
        /* Each child of filterContent stays in place — no negative margins
           or absolute positioning leaking into siblings. */
        #filterContent > * { flex: 0 0 auto; }
        /* Settings/Exports outer wrapper inside the drones panel */
        #filterContent > div[style*="margin:8px 8px 0 8px"][style*="border:1px solid lime"] {
          margin: 8px 0 0 0 !important;
          flex: 0 0 auto;
        }
        /* Narrow viewport / tablet portrait — kicks in well before phones to
           avoid the "panel hangs over the edge" look on iPad / split-window. */
        @media (max-width: 900px) {
          #filterBox {
            width: 240px;
            max-width: calc(100vw - 20px);
            top: 8px;
            right: 8px;
            padding: 6px;
          }
          #adsbBox {
            top: 8px !important;
            left: 8px !important;
            max-width: calc(100vw - 16px) !important;
          }
          #adsbBoxContent {
            width: 240px !important;
            max-width: calc(100vw - 20px) !important;
          }
          #mapLayerFloatBox {
            right: 8px !important;
            bottom: 8px !important;
          }
          #mapLayerFloatContent {
            width: auto !important;
            max-width: calc(100vw - 16px) !important;
            max-height: 40vh !important;
          }
          #geofenceFloatBox {
            left: 8px !important;
            bottom: 8px !important;
          }
          #geofenceFloatContent, #settingsFloatContent {
            width: auto !important;
            max-width: calc(100vw - 20px) !important;
          }
          #activePlaceholder, #inactivePlaceholder {
            max-height: 24vh !important;
          }
        }
        /* Phones: panels go edge-to-edge with vertical separation. */
        @media (max-width: 600px) {
          #filterBox {
            width: auto;
            left: 6px;
            right: 6px;
            max-width: none;
            max-height: 42vh;
          }
          #mapLayerFloatBox {
            left: 6px !important;
            right: 6px !important;
            width: auto !important;
          }
          #mapLayerFloatContent {
            width: auto !important;
            max-width: none !important;
            max-height: 50vh !important;
          }
          #adsbBox {
            max-width: calc(100vw - 20px) !important;
          }
          #adsbBoxContent {
            width: auto !important;
            max-width: calc(100vw - 22px) !important;
          }
        }
        /* Inputs/selects/sliders inside the sidebar respect their inline width.
           The legacy 'width: auto !important' rule was causing the staleout slider
           to stop 3/5 of the way and breaking every full-width control. */
        #filterBox input[type="range"] {
          width: 100%;
          box-sizing: border-box;
          margin: 0;
        }
    #filterBox.collapsed #filterContent {
      display: none;
    }
    /* When the right sidebar is collapsed, hide every direct child of #filterBox
       except the header and anything explicitly marked .alwaysVisible (USB status). */
    #filterBox.collapsed > *:not(#filterHeader):not(.alwaysVisible) {
      display: none;
    }
    /* Tighten header when collapsed */
    #filterBox.collapsed {
      padding: 6px 14px;
      width: auto;
      min-width: 0;
    }
    /* When collapsed: title + USB pill + [+] toggle all on the SAME row.
       The USB element gets pulled into #filterHeader at runtime to make this work. */
    #filterBox.collapsed #filterHeader {
      padding: 0;
      display: flex;
      align-items: center;
      gap: 12px;
      width: 100%;
      white-space: nowrap;
    }
    #filterBox.collapsed #filterHeader h3 {
      display: inline-block;
      flex: none;
      width: auto;
      margin: 0;
      color: #FF00FF;
      font-size: 0.95em;
    }
    #filterBox.collapsed #filterHeader .alwaysVisible {
      /* USB pill compact + sandwiched between title and [+] */
      margin: 0;
      padding: 2px 8px !important;
      max-width: none;
      flex: 0 0 auto;
    }
    #filterBox.collapsed #filterHeader #filterToggle {
      margin-left: auto;       /* push the [+] to the far right edge */
    }
    #filterBox:not(.collapsed) #filterHeader h3 {
      display: none;
    }
    #filterHeader {
      display: flex;
      align-items: center;
      gap: 10px;            /* breathing room between count/ON badge/toggle/[-] */
    }
    #filterBox:not(.collapsed) #filterHeader {
      justify-content: flex-end;
      gap: 10px;
      padding-right: 2px;
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
    
    /* USB status styling - now integrated in filter window */
    #serialStatus div { margin-bottom: 2px; }
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
    /* #activePlaceholder .drone-item.no-gps:hover::after {
      content: "no gps lock";
      position: absolute;
      bottom: 100%;
      left: 50%;
      transform: translateX(-50%);
      background-color: black;
      color: #FF00FF;
      padding: 4px 6px;
      border: 1px solid #FF00FF;
      border-radius: 2px;
      white-space: nowrap;
      font-family: monospace;
      font-size: 0.75em;
      z-index: 2000;
    } */
    /* Highlight recently seen drones (but not no-GPS drones) */
    .drone-item.recent:not(.no-gps) {
      box-shadow: 0 0 0 1px lime;
    }
    .placeholder {
      border: 1px solid rgba(136, 255, 153, 0.18);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.02);
      /* Roomier caps so several drone tags fit side-by-side without an
         immediate inner scroll. Still bounded so the whole panel doesn't
         overflow the viewport. */
      min-height: 90px;
      max-height: 28vh;
      margin-top: 4px;
      margin-bottom: 4px;
      padding: 8px;
      overflow-y: auto;
      overflow-x: hidden;
      box-sizing: border-box;
      transition: border-color .15s, background .15s;
      flex: 0 0 auto;
      display: flex;
      flex-wrap: wrap;
      align-content: flex-start;
      gap: 4px;
    }
    /* Preserve the centered "none" placeholder when a list is empty. */
    .placeholder:empty { display: block; }
    /* Inside the active/inactive boxes, let `gap` handle spacing. */
    .placeholder .drone-item { margin: 0; }
    .placeholder:hover {
      border-color: rgba(136, 255, 153, 0.32);
    }
    .placeholder:empty::before {
      content: 'none';
      display: block;
      text-align: center;
      color: rgba(255, 255, 255, 0.18);
      font-size: 0.78em;
      letter-spacing: 1px;
      padding: 14px 0;
      font-style: italic;
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
    /* ───────── ADS-B aircraft popup — clean dark card, no neon border ───────── */
    .leaflet-popup.adsb-popup > .leaflet-popup-content-wrapper {
      background: linear-gradient(180deg, #11161c 0%, #0a0d12 100%) !important;
      color: #dde6ee !important;
      border: 1px solid rgba(136, 200, 255, 0.25) !important;
      border-radius: 8px !important;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6),
                  0 0 0 1px rgba(0, 0, 0, 0.4) inset,
                  0 0 12px rgba(136, 200, 255, 0.08) !important;
      width: auto !important;
      max-width: 280px !important;
      zoom: 1 !important;
    }
    .leaflet-popup.adsb-popup .leaflet-popup-content {
      font-size: 13px !important;
      line-height: 1.4 !important;
      margin: 12px 14px !important;
    }
    .leaflet-popup.adsb-popup .leaflet-popup-tip {
      background: #11161c !important;
      border: 1px solid rgba(136, 200, 255, 0.25) !important;
    }
    .leaflet-popup.adsb-popup .leaflet-popup-close-button {
      color: #6b7a8a !important;
      font-size: 18px !important;
      padding: 4px 6px !important;
      transition: color 0.15s;
    }
    .leaflet-popup.adsb-popup .leaflet-popup-close-button:hover {
      color: #88c8ff !important;
    }
    .leaflet-popup.adsb-popup button:hover {
      filter: brightness(1.15);
    }
    /* ───── DRONE popup — same card vocabulary, lime accent ───── */
    .leaflet-popup.drone-popup > .leaflet-popup-content-wrapper {
      background: linear-gradient(180deg, #11161c 0%, #0a0d12 100%) !important;
      color: #dde6ee !important;
      border: 1px solid rgba(136, 255, 153, 0.28) !important;
      border-radius: 8px !important;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6),
                  0 0 0 1px rgba(0, 0, 0, 0.4) inset,
                  0 0 12px rgba(136, 255, 153, 0.08) !important;
      width: auto !important;
      max-width: 300px !important;
      zoom: 1 !important;
    }
    .leaflet-popup.drone-popup .leaflet-popup-content {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-size: 13px !important;
      line-height: 1.4 !important;
      margin: 12px 14px !important;
      color: #dde6ee !important;
    }
    .leaflet-popup.drone-popup .leaflet-popup-tip {
      background: #11161c !important;
      border: 1px solid rgba(136, 255, 153, 0.28) !important;
    }
    .leaflet-popup.drone-popup .leaflet-popup-close-button {
      color: #6b7a8a !important;
      font-size: 18px !important;
      padding: 4px 6px !important;
      transition: color 0.15s;
    }
    .leaflet-popup.drone-popup .leaflet-popup-close-button:hover {
      color: #88ff99 !important;
    }
    .leaflet-popup.drone-popup button:hover {
      filter: brightness(1.15);
    }
    .leaflet-popup.drone-popup a:hover {
      background: rgba(136, 255, 153, 0.08);
    }
    /* ───── Pure-CSS popup toggles — animate from input:checked, no JS re-render needed ─────
       The popup re-render is suppressed while the popup is open (so live snapshot
       updates don't wipe DOM the user is interacting with). That meant inline-style
       toggles couldn't update their thumb position on click. These rules use sibling
       selectors so the track tints + knob slides purely from the checkbox state. */
    .pop-toggle {
      position: relative;
      display: inline-block;
      width: 34px;
      height: 18px;
      flex-shrink: 0;
    }
    .pop-toggle input {
      opacity: 0;
      width: 100%;
      height: 100%;
      margin: 0;
      cursor: pointer;
      position: absolute;
      inset: 0;
      z-index: 2;
    }
    .pop-toggle .pop-track {
      position: absolute;
      inset: 0;
      background: #2a2f36;
      border-radius: 18px;
      transition: background .18s ease;
      pointer-events: none;
    }
    .pop-toggle .pop-knob {
      position: absolute;
      top: 2px; left: 2px;
      width: 14px; height: 14px;
      background: #fff;
      border-radius: 50%;
      transition: left .18s ease, transform .18s ease;
      box-shadow: 0 1px 2px rgba(0,0,0,0.4);
      pointer-events: none;
    }
    /* Cyan accent (aircraft popup) */
    .leaflet-popup.adsb-popup .pop-toggle input:checked ~ .pop-track { background: #88c8ff; }
    .leaflet-popup.adsb-popup .pop-toggle input:checked ~ .pop-knob  { left: 18px; }
    /* Lime accent (drone popup) */
    .leaflet-popup.drone-popup .pop-toggle input:checked ~ .pop-track { background: #88ff99; }
    .leaflet-popup.drone-popup .pop-toggle input:checked ~ .pop-knob  { left: 18px; }
    /* Hover affordance */
    .pop-toggle:hover .pop-track { filter: brightness(1.2); }
    /* ───── Thin custom scrollbars on every floating panel ─────
       Replaces the default Win95-looking chunky bar with a 4px translucent
       track that only ghosts in when actively scrolling/hovered. */
    #adsbAircraftList,
    #adsbBoxContent,
    #geofenceFloatContent,
    #settingsFloatContent,
    #mapLayerFloatContent,
    #filterContent {
      scrollbar-width: thin;
      scrollbar-color: rgba(255,255,255,0.18) transparent;
    }
    #adsbAircraftList::-webkit-scrollbar,
    #adsbBoxContent::-webkit-scrollbar,
    #geofenceFloatContent::-webkit-scrollbar,
    #settingsFloatContent::-webkit-scrollbar,
    #mapLayerFloatContent::-webkit-scrollbar,
    #filterContent::-webkit-scrollbar {
      width: 6px;
      height: 6px;
      background: transparent;
    }
    #adsbAircraftList::-webkit-scrollbar-track,
    #adsbBoxContent::-webkit-scrollbar-track,
    #geofenceFloatContent::-webkit-scrollbar-track,
    #settingsFloatContent::-webkit-scrollbar-track,
    #mapLayerFloatContent::-webkit-scrollbar-track,
    #filterContent::-webkit-scrollbar-track {
      background: transparent;
      border: none;
    }
    #adsbAircraftList::-webkit-scrollbar-thumb,
    #adsbBoxContent::-webkit-scrollbar-thumb,
    #geofenceFloatContent::-webkit-scrollbar-thumb,
    #settingsFloatContent::-webkit-scrollbar-thumb,
    #mapLayerFloatContent::-webkit-scrollbar-thumb,
    #filterContent::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,0.14);
      border-radius: 3px;
      transition: background 0.15s;
    }
    #adsbAircraftList::-webkit-scrollbar-thumb:hover,
    #adsbBoxContent::-webkit-scrollbar-thumb:hover,
    #geofenceFloatContent::-webkit-scrollbar-thumb:hover,
    #settingsFloatContent::-webkit-scrollbar-thumb:hover,
    #mapLayerFloatContent::-webkit-scrollbar-thumb:hover,
    #filterContent::-webkit-scrollbar-thumb:hover {
      background: rgba(136, 200, 255, 0.4);
    }
    /* Kill the corner artifact between vertical + horizontal bars */
    #adsbAircraftList::-webkit-scrollbar-corner,
    #adsbBoxContent::-webkit-scrollbar-corner,
    #geofenceFloatContent::-webkit-scrollbar-corner,
    #settingsFloatContent::-webkit-scrollbar-corner,
    #mapLayerFloatContent::-webkit-scrollbar-corner,
    #filterContent::-webkit-scrollbar-corner {
      background: transparent;
    }

    /* ───────── Unified collapsible-panel skin ─────────
       Gives every floating panel + every collapsible sub-section the same
       look as the new ADS-B / drone popup cards: dark gradient surface,
       thin translucent border, system UI font, comfortable padding. The
       individual panel borders override the accent color (cyan / lime /
       pink) so the user can still tell them apart at a glance. */
    #filterBox {
      background: linear-gradient(180deg, #11161c 0%, #0a0d12 100%) !important;
      border: 1px solid rgba(136, 255, 153, 0.28) !important;
      border-radius: 8px !important;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6),
                  0 0 12px rgba(136, 255, 153, 0.06) !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      color: #dde6ee !important;
      padding: 10px !important;
    }
    #filterHeader h3 {
      color: #88ff99 !important;
      letter-spacing: 2px;
      font-weight: 700;
      font-size: 0.85em !important;
    }
    /* Sub-section headers inside #filterContent (Active / Inactive Drones) */
    #filterContent > h3 {
      color: #a5b8a8 !important;
      font-size: 0.72em !important;
      letter-spacing: 1.5px !important;
      font-weight: 700 !important;
      margin: 10px 0 4px 0 !important;
      padding: 6px 8px !important;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 5px;
      transition: background .15s, border-color .15s;
    }
    #filterContent > h3:hover {
      background: rgba(136, 255, 153, 0.06);
      border-color: rgba(136, 255, 153, 0.25);
    }
    /* Sub-cards (staleout slider, basemap select wrappers, geofence panel,
       settings panel) — match the popup vocabulary. */
    #filterBox > div,
    #filterContent > div {
      border-radius: 6px;
    }
    #filterContent > div[style*="border:1px solid lime"],
    #filterBox > div[style*="border:1px solid lime"] {
      border: 1px solid rgba(136, 255, 153, 0.18) !important;
      background: rgba(255,255,255,0.025) !important;
    }
    /* The drone-item pills (each drone in the list) */
    .drone-item {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      border-radius: 5px !important;
      padding: 4px 8px !important;
      letter-spacing: 0.3px;
      transition: filter .15s, background .15s;
    }
    .drone-item:hover {
      filter: brightness(1.2);
      background: rgba(255, 255, 255, 0.04) !important;
    }
    /* Headings inside the geofence + settings sub-panels (those ▼ collapsibles) */
    #geofenceToggle, #settingsToggle, #adsbToggle {
      color: #a5b8a8 !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-size: 0.78em !important;
      letter-spacing: 1.5px !important;
      padding: 6px 8px !important;
      background: rgba(255,255,255,0.03);
      border: 1px solid rgba(255,255,255,0.06);
      border-radius: 5px;
      transition: background .15s, border-color .15s;
    }
    #geofenceToggle:hover, #settingsToggle:hover, #adsbToggle:hover {
      background: rgba(136, 255, 153, 0.06);
      border-color: rgba(136, 255, 153, 0.25);
    }
    /* Make the [-] / [+] toggle in the drones panel header a clean glyph */
    #filterToggle {
      color: #88ff99 !important;
      font-size: 16px !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-weight: 700;
    }
    /* USB-status pill inside the header — keep it readable on the new dark bg */
    #filterHeader .alwaysVisible {
      background: rgba(255, 255, 255, 0.04) !important;
      border-color: rgba(136, 200, 255, 0.25) !important;
      border-radius: 5px;
      color: #aac8d6 !important;
      font-size: 0.7em !important;
    }
    /* Active/inactive drone count: push to the LEFT of the header so it doesn't
       crowd the ON badge. Larger + accent-colored. */
    #dronesHeaderCount {
      flex: 1 1 auto;
      text-align: left;
      font-weight: 700;
      letter-spacing: 1px;
      font-size: 0.78em !important;
      color: #88ff99 !important;
      padding-left: 4px;
      order: 0;
    }
    /* Push the ON badge + switch + [-] toggle to the right so the count owns
       the left side of the collapsed header. */
    #filterHeader #dronesLayerStateLabel { margin-left: auto; }
    /* Inline lime/yellow accent borders inside the drones panel — replaced */
    #filterContent div[style*="border:1px solid lime"],
    #filterContent div[style*="border: 1px solid lime"] {
      border: 1px solid rgba(136, 255, 153, 0.18) !important;
      background: rgba(255, 255, 255, 0.025) !important;
      border-radius: 6px !important;
    }
    /* Section headings inside the drones panel sub-cards */
    #filterContent div[style*="color:#aaffaa"] {
      color: #a5b8a8 !important;
      letter-spacing: 1.5px !important;
      font-weight: 700 !important;
      font-size: 0.78em !important;
    }
    /* "DRONE EXPORTS" header pill */
    #filterContent .alwaysVisible-export-header { display: none; }
    /* Staleout slider styling */
    #staleoutSlider {
      accent-color: #88ff99 !important;
    }
    #staleoutValue {
      color: #88ff99 !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-size: 0.78em !important;
      letter-spacing: 1px;
      font-weight: 700;
    }
    /* Basemap select inside #filterContent */
    #layerSelect {
      color: #88c8ff !important;
      letter-spacing: 0.5px;
    }
    /* Active / Inactive drone section headers — modern subtle pills */
    #activeHeader, #inactiveHeader {
      color: #a5b8a8 !important;
      font-size: 0.72em !important;
      letter-spacing: 1.5px !important;
      font-weight: 700 !important;
      padding: 6px 10px !important;
      background: rgba(255, 255, 255, 0.03) !important;
      border: 1px solid rgba(255, 255, 255, 0.06) !important;
      border-radius: 6px !important;
      margin: 10px 0 0 0 !important;
      transition: background .15s, border-color .15s;
    }
    #activeHeader:hover, #inactiveHeader:hover {
      background: rgba(136, 255, 153, 0.06) !important;
      border-color: rgba(136, 255, 153, 0.25) !important;
    }
    /* Tiny chevron arrow next to the section heading */
    #activeArrow, #inactiveArrow {
      color: rgba(255, 255, 255, 0.5) !important;
    }
    /* Export button pills — pill-style instead of cyberpunk neon */
    #downloadCsv, #downloadKml, #downloadAliases,
    #downloadCumulativeCsv, #downloadCumulativeKml {
      background: rgba(255, 255, 255, 0.04) !important;
      color: #88c8ff !important;
      border: 1px solid rgba(136, 200, 255, 0.25) !important;
      border-radius: 5px !important;
      padding: 5px 0 !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-size: 0.72em !important;
      letter-spacing: 1px !important;
      font-weight: 700 !important;
      transition: background .15s, border-color .15s, filter .15s;
    }
    #downloadCsv:hover, #downloadKml:hover, #downloadAliases:hover,
    #downloadCumulativeCsv:hover, #downloadCumulativeKml:hover {
      background: rgba(136, 200, 255, 0.10) !important;
      border-color: rgba(136, 200, 255, 0.45) !important;
      filter: brightness(1.1);
    }
    /* AIR TRAFFIC + GEOFENCING + SETTINGS + MAP LAYER + DRONE PANEL all share
       the same outer card vocabulary. Per-panel border-color is left alone
       (set inline) so colors stay distinct. */
    #adsbBox, #geofenceFloatBox, #settingsFloatBox, #mapLayerFloatBox {
      background: linear-gradient(180deg, #11161c 0%, #0a0d12 100%) !important;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6),
                  0 0 12px rgba(0,0,0,0.4) !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
    }
    /* AIR TRAFFIC panel — sizes to fit content, never scrolls itself.
       The aircraft LIST inside has its own short cap so the panel doesn't
       grow taller than the screen. */
    #adsbBox { overflow: visible; }
    #adsbBoxHeader, #geofenceFloatHeader, #settingsFloatHeader, #mapLayerFloatHeader {
      background: rgba(255, 255, 255, 0.03) !important;
      transition: background .15s;
    }
    #adsbBoxHeader:hover, #geofenceFloatHeader:hover, #settingsFloatHeader:hover, #mapLayerFloatHeader:hover {
      background: rgba(255, 255, 255, 0.06) !important;
    }
    #adsbBoxHeader h3, #geofenceFloatHeader h3, #settingsFloatHeader h3, #mapLayerFloatHeader h3 {
      letter-spacing: 2px !important;
      font-weight: 700 !important;
      font-size: 0.78em !important;
    }
    /* Inputs / selects across all panels — same dark glass treatment */
    #adsbBox input[type="text"], #adsbBox input[type="number"], #adsbBox input[type="password"], #adsbBox select,
    #filterBox input[type="text"], #filterBox input[type="number"], #filterBox select,
    #geofenceFloatBox input[type="text"], #geofenceFloatBox input[type="number"], #geofenceFloatBox select,
    #settingsFloatBox input[type="text"], #settingsFloatBox input[type="number"], #settingsFloatBox select,
    #mapLayerFloatBox select {
      background: rgba(255,255,255,0.04) !important;
      border: 1px solid rgba(255,255,255,0.10) !important;
      border-radius: 4px !important;
      color: #dde6ee !important;
      font-family: inherit !important;
      transition: border-color .15s;
    }
    #adsbBox input:focus, #filterBox input:focus, #geofenceFloatBox input:focus,
    #settingsFloatBox input:focus, #mapLayerFloatBox select:focus,
    #adsbBox select:focus, #filterBox select:focus, #geofenceFloatBox select:focus, #settingsFloatBox select:focus {
      outline: none;
      border-color: rgba(136, 200, 255, 0.55) !important;
    }
    /* Buttons across panels */
    #adsbBox button, #filterBox button, #geofenceFloatBox button,
    #settingsFloatBox button, #mapLayerFloatBox button {
      font-family: inherit !important;
      border-radius: 4px !important;
      letter-spacing: 0.5px;
      transition: filter .15s;
      cursor: pointer;
    }
    #adsbBox button:hover, #filterBox button:hover, #geofenceFloatBox button:hover,
    #settingsFloatBox button:hover, #mapLayerFloatBox button:hover {
      filter: brightness(1.18);
    }
    /* ───── AIR TRAFFIC panel — refined sub-elements ───── */
    /* Initial state-label inline style: lime → cyan baseline so the very first
       paint matches the ADS-B family before _adsbSetUiEnabled runs. */
    #adsbBoxStateLabel {
      color: #586978 !important;
      border-color: rgba(255, 255, 255, 0.10) !important;
      background: transparent !important;
    }
    /* "▸ SETTINGS" sub-section header inside the AIR TRAFFIC panel */
    #adsbBoxSettingsToggle {
      color: #aac8d6 !important;
      letter-spacing: 1.5px !important;
      font-weight: 700 !important;
      font-size: 0.85em !important;
      padding: 5px 8px !important;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 5px;
      transition: background .15s, border-color .15s, color .15s;
    }
    #adsbBoxSettingsToggle:hover {
      background: rgba(136, 200, 255, 0.08);
      border-color: rgba(136, 200, 255, 0.30);
      color: #88c8ff !important;
    }
    /* OSINT filter chips — outline-style off, soft-tinted bg + dark text on. */
    #adsbBoxFilterChips > span {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-size: 0.7em !important;
      letter-spacing: 0.5px !important;
      font-weight: 700 !important;
      padding: 3px 7px !important;
      border-radius: 9px !important;
      transition: filter .15s, background .15s !important;
    }
    #adsbBoxFilterChips > span:hover { filter: brightness(1.18); }
    /* Count + source line ("N in view · adsblol") — tighter typography */
    #adsbCount {
      color: #6b8294 !important;
      font-size: 0.72em !important;
      letter-spacing: 1.2px !important;
      font-weight: 600 !important;
      padding: 4px 0 !important;
      text-transform: uppercase;
    }
    /* Aircraft list rows — softer hover, no row jitter */
    #adsbAircraftList .adsbRow {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      transition: background .12s, filter .12s;
    }
    #adsbAircraftList .adsbRow:hover {
      background: rgba(136, 200, 255, 0.08) !important;
    }
    /* Soft fade-out at the bottom (and top when scrolled) so rows don't
       hard-cut under the panel's edge — they dissolve into the dark
       background instead. Uses CSS mask-image which the scrollbar respects. */
    #adsbAircraftList {
      -webkit-mask-image: linear-gradient(to bottom,
        transparent 0,
        black 14px,
        black calc(100% - 18px),
        transparent 100%);
      mask-image: linear-gradient(to bottom,
        transparent 0,
        black 14px,
        black calc(100% - 18px),
        transparent 100%);
      padding-bottom: 4px;
    }
    /* THE ACTUAL BUG: the dronCircle / pilotCircle panes use a canvas
       renderer (preferCanvas: true on the map), and that canvas spans the
       entire map at z-index 650 — sitting on top of the marker pane (z 600)
       where plane icons live. The canvas was intercepting every hover and
       click before they reached the planes. Drone circles are purely
       decorative (no bindPopup, no .on('click')) so we can safely tell the
       canvas to let mouse events pass through. */
    .leaflet-pane.leaflet-droneCircle-pane,
    .leaflet-pane.leaflet-pilotCircle-pane,
    .leaflet-droneCircle-pane canvas,
    .leaflet-pilotCircle-pane canvas {
      pointer-events: none !important;
    }
    /* Aircraft map marker — pointer cursor on EVERY descendant so the entire
       32×32 hit box flips to the one-finger icon on hover, just like drones. */
    .leaflet-marker-pane .leaflet-marker-icon.adsb-icon,
    .leaflet-marker-pane .leaflet-marker-icon.adsb-icon *,
    .leaflet-marker-pane .leaflet-marker-icon.adsb-icon svg,
    .leaflet-marker-pane .leaflet-marker-icon.adsb-icon svg * {
      pointer-events: auto !important;
      cursor: pointer !important;
    }
    /* And prevent the plane SVG from being draggable (which would block clicks
       AND trick the map into thinking the user is dragging the map). */
    .leaflet-marker-pane .leaflet-marker-icon.adsb-icon svg {
      -webkit-user-drag: none;
      user-select: none;
      -webkit-user-select: none;
    }
    /* Live in-view aircraft count in panel header — bare number,
       right-aligned, cyan accent, full visibility (never truncated). */
    #adsbBoxStatus {
      color: #88c8ff !important;
      font-weight: 700 !important;
      letter-spacing: 0.3px;
      text-transform: none !important;
      font-size: 0.85em !important;
      text-align: right;
      overflow: visible !important;
      text-overflow: clip !important;
    }
    /* ───── MAP LAYER panel — refined sub-elements ───── */
    /* Offline mapping wrapper (the lime-bordered cyberpunk card inside the
       MAP LAYER panel). Replaced with the dark glass treatment. */
    #offlineMappingPanel {
      border: 1px solid rgba(136, 255, 153, 0.18) !important;
      background: rgba(255, 255, 255, 0.025) !important;
      border-radius: 6px !important;
      padding: 8px !important;
      margin: 0 !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      color: #dde6ee !important;
      font-size: 12px !important;
    }
    /* "▼ OFFLINE MAPPING" sub-toggle inside the panel */
    #cacheToggle {
      color: #a5b8a8 !important;
      letter-spacing: 1.5px !important;
      font-weight: 700 !important;
      font-size: 0.85em !important;
      padding: 4px 6px !important;
      border-radius: 4px;
      transition: background .15s, color .15s;
    }
    #cacheToggle:hover {
      background: rgba(136, 255, 153, 0.06);
      color: #88ff99 !important;
    }
    #cacheToggle > span:first-child { color: #a5b8a8 !important; }
    #cacheToggleArrow { color: rgba(255, 255, 255, 0.5) !important; }
    /* Place search / cache controls inside cachePanel */
    #cachePanel input[type="text"],
    #cachePanel input[type="number"],
    #cachePanel input[type="search"],
    #cachePanel select {
      background: rgba(255, 255, 255, 0.04) !important;
      border: 1px solid rgba(255, 255, 255, 0.10) !important;
      border-radius: 4px !important;
      color: #dde6ee !important;
      font-family: inherit !important;
      padding: 5px 7px !important;
      box-sizing: border-box;
    }
    #cachePanel input:focus,
    #cachePanel select:focus {
      outline: none;
      border-color: rgba(136, 255, 153, 0.55) !important;
    }
    #cachePanel button {
      background: rgba(255, 255, 255, 0.04) !important;
      color: #88ff99 !important;
      border: 1px solid rgba(136, 255, 153, 0.25) !important;
      border-radius: 4px !important;
      font-family: inherit !important;
      letter-spacing: 0.5px;
      font-weight: 600;
      padding: 5px 8px !important;
      transition: background .15s, border-color .15s, filter .15s;
      cursor: pointer;
    }
    #cachePanel button:hover {
      background: rgba(136, 255, 153, 0.10) !important;
      border-color: rgba(136, 255, 153, 0.45) !important;
      filter: brightness(1.1);
    }
    /* Sub-section labels inside cache panel ("PLACE SEARCH", "CACHE THIS AREA"
       etc.) — clean uppercase letterspaced data labels. */
    #cachePanel div[style*="background: linear-gradient"],
    #cachePanel div[style*="background:linear-gradient"] {
      background: none !important;
      -webkit-background-clip: initial !important;
      -webkit-text-fill-color: initial !important;
      color: #a5b8a8 !important;
      letter-spacing: 1.5px !important;
      font-weight: 700 !important;
      font-size: 0.78em !important;
      text-transform: uppercase;
    }
    /* ───── Offline mapping flyout — opens to the LEFT of the Map Layer panel ─────
       When toggled open, cachePanel pops out as a horizontal panel anchored
       to the left edge of #offlineMappingPanel (which lives inside the Map
       Layer panel). It floats outside the Map Layer panel's right anchor so
       it doesn't push the Map Layer taller — purely horizontal expansion. */
    /* While the flyout is open, let it escape the box. !important + :has()
       beat the inline overflow:hidden / overflow-y:auto on these elements;
       scoped to flyout-open so normal scroll + rounded-corner clipping stay
       intact when it's closed. */
    #mapLayerFloatBox:has(#offlineMappingPanel.flyout-open) { overflow: visible !important; }
    #mapLayerFloatContent:has(#offlineMappingPanel.flyout-open) { overflow: visible !important; }
    #offlineMappingPanel.flyout-open #cachePanel {
      position: absolute;
      right: calc(100% + 8px);
      bottom: 0;
      width: 340px;
      max-width: calc(100vw - 420px);
      max-height: calc(100vh - 80px);
      overflow-y: auto;
      margin-top: 0;
      padding: 10px 12px;
      background: linear-gradient(180deg, #11161c 0%, #0a0d12 100%);
      border: 1px solid rgba(136, 255, 153, 0.28);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6),
                  0 0 12px rgba(136, 255, 153, 0.06);
      z-index: 1001;
      animation: flyoutSlideLeft .18s ease-out;
    }
    @keyframes flyoutSlideLeft {
      from { opacity: 0; transform: translateX(8px); }
      to   { opacity: 1; transform: translateX(0); }
    }
    /* On mobile, fall back to vertical expansion (no horizontal room) */
    @media (max-width: 700px) {
      #offlineMappingPanel.flyout-open #cachePanel {
        position: static;
        width: auto;
        max-width: none;
        max-height: 50vh;
        margin-top: 6px;
        animation: none;
      }
    }
    /* ───────── Leaflet zoom control — matches panel/popup card vocabulary ───────── */
    .leaflet-control-zoom {
      border: 1px solid rgba(136, 200, 255, 0.28) !important;
      border-radius: 8px !important;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6),
                  0 0 12px rgba(0,0,0,0.4) !important;
      overflow: hidden;
      background: linear-gradient(180deg, #11161c 0%, #0a0d12 100%);
    }
    .leaflet-control-zoom a {
      background: transparent !important;
      color: #dde6ee !important;
      border: 0 !important;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
      font-weight: 600 !important;
      font-size: 16px !important;
      width: 32px !important;
      height: 32px !important;
      line-height: 32px !important;
      transition: background .12s, color .12s;
    }
    .leaflet-control-zoom a:hover {
      background: rgba(136, 200, 255, 0.12) !important;
      color: #88c8ff !important;
    }
    .leaflet-control-zoom a:active {
      background: rgba(136, 200, 255, 0.22) !important;
    }
    .leaflet-control-zoom a:first-child {
      border-bottom: 1px solid rgba(255,255,255,0.07) !important;
    }
    .leaflet-control-zoom a.leaflet-disabled {
      color: #4a5560 !important;
      cursor: not-allowed;
    }
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
    /* Tile rendering: smooth bilinear scaling during animated zoom, GPU
       compositing hint for pan/zoom, no fighting with the fade-in anim. */
    .leaflet-tile {
      display: block;
      margin: 0;
      padding: 0;
      image-rendering: auto;
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
    /* (Old cyberpunk magenta heading style — neutralized; the new pill-style
       overrides further down own this now.) */
    #filterContent > h3:nth-of-type(1),
    #filterContent > h3:nth-of-type(2) {
      text-align: left;
      font-size: inherit;
    }
    /* Lime-green hacky dashes around filter headers */
    #filterContent > h3 {
      display: block;
      width: 100%;
      text-align: center;
      margin: 0.5em 0;
    }
    /* Old lime "---" hacky dashes — neutralized; the new pill headers don't need them. */
    #filterContent > h3::before,
    #filterContent > h3::after {
      content: '';
      margin: 0;
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
    /* Staleout slider styling – match popup sliders */
    #staleoutSlider {
      -webkit-appearance: none;
      width: 100%;
      height: 4px;
      background: transparent;
      border: none;
      outline: none;
    }
    #staleoutSlider::-webkit-slider-runnable-track {
      width: 100%;
      height: 4px;
      background: rgba(255, 255, 255, 0.10);
      border: none;
      border-radius: 4px;
    }
    #staleoutSlider::-webkit-slider-thumb {
      -webkit-appearance: none;
      height: 14px;
      width: 14px;
      background: #fff;
      border: 0;
      margin-top: -5px;
      border-radius: 50%;
      cursor: pointer;
      box-shadow: 0 0 0 2px rgba(136, 255, 153, 0.6),
                  0 1px 3px rgba(0, 0, 0, 0.4);
      transition: box-shadow .15s, transform .15s;
    }
    #staleoutSlider::-webkit-slider-thumb:hover { transform: scale(1.1); }
    /* Firefox */
    #staleoutSlider::-moz-range-track {
      width: 100%;
      height: 4px;
      background: rgba(255, 255, 255, 0.10);
      border: none;
      border-radius: 4px;
    }
    #staleoutSlider::-moz-range-thumb {
      height: 14px;
      width: 14px;
      background: #fff;
      border: 0;
      margin-top: -5px;
      border-radius: 50%;
      cursor: pointer;
      box-shadow: 0 0 0 2px rgba(136, 255, 153, 0.6),
                  0 1px 3px rgba(0, 0, 0, 0.4);
    }
    /* IE */
    #staleoutSlider::-ms-fill-lower,
    #staleoutSlider::-ms-fill-upper {
      background: rgba(255, 255, 255, 0.10);
      border: none;
      border-radius: 4px;
    }
    #staleoutSlider::-ms-thumb {
      height: 14px;
      width: 14px;
      background: #fff;
      border: 0;
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
    /* Cumulative download buttons styling to match regular download buttons */
    #downloadCumulativeButtons button {
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
    #downloadCumulativeButtons button:focus {
      outline: none;
      caret-color: transparent;
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

<!-- BOTTOM-LEFT: GEOFENCING float box (expands UP). Sits just to the right of
     Leaflet's bottom-left zoom controls. Width = content when collapsed, fixed
     when expanded. Same card aesthetic as the new ADS-B/drone popups. -->
<div id="geofenceFloatBox" style="position:absolute; bottom:10px; left:60px; z-index:1000; width:fit-content; border:1px solid rgba(255,102,136,0.35); border-radius:8px; background:linear-gradient(180deg,#181216 0%,#0c0a0c 100%); font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; color:#e8d8de; box-shadow:0 8px 24px rgba(0,0,0,0.6), 0 0 12px rgba(255,102,136,0.08); overflow:hidden;">
  <div id="geofenceFloatContent" style="display:none; padding:10px 12px; width:320px; max-height:60vh; overflow-y:auto; box-sizing:border-box;">
    <!-- populated at startup from #geofencePanel relocation -->
  </div>
  <div id="geofenceFloatHeader" style="display:flex; justify-content:space-between; align-items:center; padding:8px 14px; cursor:pointer; background:rgba(40,18,24,0.6); border-top:1px solid rgba(255,102,136,0.35); gap:12px; white-space:nowrap;">
    <h3 style="margin:0; font-size:0.78em; color:#ff99aa; letter-spacing:2px; font-weight:700;">GEOFENCING</h3>
    <span id="geofenceFloatStatus" style="color:#a87f87; font-size:0.7em; letter-spacing:0.5px; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;"></span>
    <span id="geofenceFloatToggle" style="font-size:16px; color:#ff99aa; font-weight:bold;">[+]</span>
  </div>
</div>

<!-- Bottom-CENTER: SETTINGS float box (expands UP). Width = content (collapses tight). -->
<div id="settingsFloatBox" style="position:absolute; bottom:10px; left:50%; transform:translateX(-50%); z-index:1000; width:fit-content; border:1px solid lime; border-radius:8px; background:rgba(0,0,0,0.88); font-family:monospace; color:lime; box-shadow:0 0 10px #002200; overflow:hidden;">
  <div id="settingsFloatContent" style="display:none; padding:8px 12px; width:340px; max-height:60vh; overflow-y:auto; box-sizing:border-box;">
    <!-- populated at startup -->
  </div>
  <div id="settingsFloatHeader" style="display:flex; justify-content:space-between; align-items:center; padding:6px 14px; cursor:pointer; background:rgba(0,30,0,0.7); border-top:1px solid lime; gap:14px; white-space:nowrap;">
    <h3 style="margin:0; font-size:0.85em; color:#aaffaa; letter-spacing:1px;">SETTINGS</h3>
    <span id="settingsFloatStatus" style="color:#88aa88; font-size:0.75em;">general</span>
    <span id="settingsFloatToggle" style="font-size:18px; color:#aaffaa;">[+]</span>
  </div>
</div>

<!-- Bottom-RIGHT: MAP LAYER float box. Anchored bottom-right; expands UP and LEFT.
     Width grows when content opens (BASEMAP + OFFLINE MAPPING). Height capped
     so it never reaches the Drones panel above. Mobile rules below override. -->
<div id="mapLayerFloatBox" style="position:absolute; bottom:10px; right:10px; z-index:1000; width:fit-content; border:1px solid rgba(136,255,153,0.28); border-radius:8px; background:linear-gradient(180deg,#11161c 0%,#0a0d12 100%); font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; color:#dde6ee; box-shadow:0 8px 24px rgba(0,0,0,0.6), 0 0 12px rgba(136,255,153,0.06); overflow:hidden;">
  <div id="mapLayerFloatContent" style="display:none; padding:10px 12px; width:380px; max-width:calc(100vw - 20px); max-height:calc(100vh - 380px); min-height:0; overflow-y:auto; box-sizing:border-box;">
    <!-- populated at startup with BASEMAP + OFFLINE MAPPING -->
  </div>
  <div id="mapLayerFloatHeader" style="display:flex; justify-content:space-between; align-items:center; padding:8px 14px; cursor:pointer; background:rgba(255,255,255,0.03); border-top:1px solid rgba(136,255,153,0.28); gap:12px; white-space:nowrap;">
    <h3 style="margin:0; font-size:0.78em; color:#88ff99; letter-spacing:2px; font-weight:700;">MAP LAYER</h3>
    <span id="mapLayerFloatStatus" style="color:#7e9486; font-size:0.7em; letter-spacing:0.5px; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis;"></span>
    <span id="mapLayerFloatToggle" style="font-size:16px; color:#88ff99; font-weight:700;">[+]</span>
  </div>
</div>

<!-- Top-left AIR TRAFFIC panel: full ADS-B settings + live aircraft list (in current view), collapsible.
     [+]/[-] is on the LEFT of the header (because the box itself is on the left of the screen). -->
<div id="adsbBox" style="position:absolute; top:10px; left:10px; z-index:1000; width:300px; border:1px solid rgba(136,200,255,0.28); border-radius:8px; background:linear-gradient(180deg,#11161c 0%,#0a0d12 100%); font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; color:#dde6ee; box-shadow:0 8px 24px rgba(0,0,0,0.6), 0 0 12px rgba(136,200,255,0.06); overflow:hidden;">
  <div id="adsbBoxHeader" style="display:flex; justify-content:flex-start; align-items:center; padding:8px 12px; cursor:pointer; background:rgba(255,255,255,0.03); border-bottom:1px solid rgba(136,200,255,0.18); gap:8px; white-space:nowrap;">
    <span id="adsbBoxToggle" style="font-size:15px; color:#88c8ff; font-weight:bold; flex-shrink:0;">[-]</span>
    <h3 style="margin:0; font-size:0.72em; color:#88c8ff; letter-spacing:1.5px; font-weight:700; flex-shrink:0;">AIR TRAFFIC</h3>
    <span id="adsbBoxStatus" style="color:#88c8ff; font-size:0.78em; font-weight:700; letter-spacing:0.3px; flex:1; min-width:0; text-align:right; overflow:visible;"></span>
    <span id="adsbBoxStateLabel" style="color:#586978; font-size:0.65em; font-weight:700; letter-spacing:0.5px; padding:2px 6px; border:1px solid rgba(255,255,255,0.10); border-radius:8px; background:transparent; flex-shrink:0;">OFF</span>
    <label class="switch" style="margin:0; flex-shrink:0;" title="Enable ADS-B layer">
      <input type="checkbox" id="adsbBoxEnableToggle">
      <span class="slider"></span>
    </label>
  </div>
  <div id="adsbBoxContent" style="padding:0; width:300px; box-sizing:border-box;">
    <!-- Settings sub-section (collapsible inside the panel) -->
    <div style="padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.05); font-size:0.78em;">
      <div id="adsbBoxSettingsToggle" style="cursor:pointer; user-select:none;">▸ SETTINGS</div>
      <div id="adsbBoxSettings" style="display:none; margin-top:4px;">
        <label style="display:block;">Source:
          <select id="adsbBoxSource" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;">
            <optgroup label="── Network ──">
              <option value="adsblol">adsb.lol</option>
              <option value="adsbfi">adsb.fi</option>
              <option value="airplaneslive">airplanes.live</option>
              <option value="opensky">OpenSky</option>
              <option value="adsbexchange">ADS-B Exchange (key)</option>
            </optgroup>
            <optgroup label="── Local SDR ──">
              <option value="dump1090">Local dump1090/readsb</option>
              <option value="beast">Beast TCP raw (pyModeS)</option>
            </optgroup>
          </select>
        </label>
        <label style="display:block; margin-top:3px;">Refresh (s):
          <input id="adsbBoxInterval" type="number" min="2" max="120" value="8"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
        </label>
        <label style="display:flex; align-items:center; gap:6px; margin-top:3px;">
          <input type="checkbox" id="adsbBoxBboxOnly" checked style="accent-color:#00aaff;"/>
          <span>Only fetch around current view</span>
        </label>
        <div id="adsbBoxDump1090Box" style="display:none; margin-top:4px;">
          <input id="adsbBoxDump1090Url" type="text" placeholder="http://localhost:8080/data/aircraft.json"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
        </div>
        <div id="adsbBoxBeastBox" style="display:none; margin-top:4px; display:flex; gap:4px;">
          <input id="adsbBoxBeastHost" type="text" placeholder="host"
                 style="flex:2; min-width:0; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
          <input id="adsbBoxBeastPort" type="number" placeholder="30005" min="1" max="65535"
                 style="flex:1; min-width:0; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
        </div>
        <div id="adsbBoxOpenskyBox" style="display:none; margin-top:4px;">
          <input id="adsbBoxOpenskyUser" type="text" placeholder="opensky user"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
          <input id="adsbBoxOpenskyPass" type="password" placeholder="pass"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px; margin-top:2px;"/>
        </div>
        <div id="adsbBoxExchangeBox" style="display:none; margin-top:4px;">
          <input id="adsbBoxExchangeKey" type="password" placeholder="RapidAPI key"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
        </div>
        <button id="adsbBoxSaveBtn" style="margin-top:4px; width:100%; padding:3px; background:#001a2a; border:1px solid #00aaff; color:#aaeeff; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">SAVE CONFIG</button>
      </div>
    </div>
    <!-- OSINT filter chips -->
    <!-- OSINT filter chips — what aircraft types to show on the map + list. -->
    <div style="padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.05);">
      <div style="font-size:0.65em; color:#7a8b9a; letter-spacing:1.5px; font-weight:700; margin-bottom:5px;">FILTER</div>
      <div id="adsbBoxFilterChips" style="display:flex; flex-wrap:wrap; gap:4px; font-size:0.72em;"></div>
    </div>
    <!-- Flight paths are PER-PLANE only: click a plane, toggle its path in the popup.
         The per-type chips + ALL IN VIEW were removed — they lit up thousands of full
         traces at once (unreadable spaghetti) and the bulk trace-fetch hammered the
         upstream hosts into rate-limiting us. CLEAR ALL wipes whatever's shown. -->
    <div style="padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.05);">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <span style="font-size:0.65em; color:#7a8b9a; letter-spacing:1.5px; font-weight:700;">FLIGHT PATHS</span>
        <button id="adsbPathsClearBtn" title="Hide all flight paths" style="padding:2px 6px; background:#2a0010; border:1px solid #ff5577; color:#ffccd5; font-family:monospace; font-size:0.72em; border-radius:3px; cursor:pointer;">CLEAR ALL</button>
      </div>
      <div style="font-size:0.6em; color:#586978; letter-spacing:0.5px; margin-top:4px;">click a plane, then toggle its path in the popup</div>
    </div>
    <!-- Live count + aircraft list. Zero right-padding so the scrollbar sits
         flush with the panel border; rows have their own internal right
         padding so content never touches the scrollbar. max-height pairs
         with #adsbBox max-height so it can never grow into GEOFENCING below. -->
    <div style="padding:6px 0 6px 10px;">
      <div id="adsbCount" style="text-align:center; margin-bottom:6px; padding-right:10px;">— OFF —</div>
      <div id="adsbAircraftList" style="overflow-y:auto; max-height:180px; min-height:60px; padding-right:6px;"></div>
    </div>
  </div>
</div>

<div id="filterBox">
  <div id="filterHeader">
    <h3>Drones</h3>
    <span id="dronesHeaderCount" style="color:#00ff88; font-size:0.75em; font-weight:bold; letter-spacing:0.5px; min-width:0; flex-shrink:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"></span>
    <span id="dronesLayerStateLabel" style="color:#00ff88; font-size:0.7em; font-weight:bold; letter-spacing:1px; padding:2px 6px; border:1px solid #00ff88; border-radius:3px; background:rgba(0,255,136,0.1);">ON</span>
    <label class="switch" style="margin:0; flex:0 0 auto;" title="Show/hide drone & pilot markers">
      <input type="checkbox" id="dronesLayerToggle" checked onclick="event.stopPropagation();">
      <span class="slider" style="background-color:#222;"></span>
    </label>
    <span id="filterToggle" style="cursor: pointer; font-size: 20px;">[-]</span>
  </div>
  <div id="filterContent">
    <h3 id="activeHeader" style="cursor:pointer; user-select:none; display:flex; justify-content:center; align-items:center; gap:6px;">
      <span id="activeArrow" style="font-size:0.85em;">▼</span> Active Drones
    </h3>
    <div id="activePlaceholder" class="placeholder"></div>
    <h3 id="inactiveHeader" style="cursor:pointer; user-select:none; display:flex; justify-content:center; align-items:center; gap:6px;">
      <span id="inactiveArrow" style="font-size:0.85em;">▼</span> Inactive Drones
    </h3>
    <div id="inactivePlaceholder" class="placeholder"></div>
    <!-- Staleout Slider -->
    <div style="margin:10px 8px 0 8px; padding:6px; border:1px solid lime; background:rgba(0,0,0,0.5); border-radius:4px; box-sizing:border-box;">
      <div style="color:#aaffaa; font-family:monospace; font-size:0.75em; text-align:center; margin-bottom:4px; font-weight:bold;">STALEOUT TIME</div>
      <input type="range" id="staleoutSlider" min="1" max="5" step="1" value="1"
             style="width:100%; box-sizing:border-box; border:1px solid lime; margin:0;">
      <div id="staleoutValue" style="color:lime; font-family:monospace; font-size:0.85em; width:100%; text-align:center; margin-top:2px;">1 min</div>
    </div>
    <!-- Basemap + status -->
    <div style="margin:8px 8px 0 8px; padding:6px; border:1px solid lime; background:rgba(0,0,0,0.5); border-radius:4px; box-sizing:border-box;">
      <div style="color:#aaffaa; font-family:monospace; font-size:0.75em; text-align:center; margin-bottom:4px; font-weight:bold;">BASEMAP</div>
      <select id="layerSelect" style="width:100%; box-sizing:border-box; background-color:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; padding:3px; font-family:monospace; font-size:0.8em; text-align:center; text-align-last:center;">
        <optgroup label="── ONLINE ──" style="color:#FF00FF; font-style:normal;">
          <option value="osmStandard">OSM Standard</option>
          <option value="osmHumanitarian">OSM Humanitarian</option>
          <option value="cartoPositron">CartoDB Positron</option>
          <option value="cartoDarkMatter">CartoDB Dark Matter</option>
          <option value="esriWorldImagery" selected>Esri World Imagery</option>
          <option value="esriWorldTopo">Esri World TopoMap</option>
          <option value="esriDarkGray">Esri Dark Gray Canvas</option>
          <option value="openTopoMap">OpenTopoMap</option>
        </optgroup>
        <optgroup id="offlineLayerGroup" label="── OFFLINE ──" style="color:lime; font-style:normal;">
          <!-- populated from /api/offline_layers -->
        </optgroup>
      </select>
      <!-- Status pill — full-width bar, color reflects ONLINE / OFFLINE -->
      <div id="basemapStatus" style="width:100%; box-sizing:border-box; margin-top:4px; padding:3px 10px; border:1px solid #00ff00; color:#00ff00; background:black; font-family:monospace; font-size:0.7em; letter-spacing:1px; border-radius:3px; text-align:center;">
        <span style="display:inline-block; width:7px; height:7px; border-radius:50%; background:#00ff00; box-shadow:0 0 6px #00ff00; vertical-align:middle; margin-right:5px;"></span>ONLINE
      </div>
    </div>
    <!-- Legacy compact ADS-B toggle — hidden; the top-left adsbBox is now the primary
         control. Kept in DOM so #adsbMainToggle / #adsbMainStatus references don't break. -->
    <div style="display:none;">
      <span id="adsbMainStatus">off</span>
      <input type="checkbox" id="adsbMainToggle">
    </div>
      <!-- Cache This Area panel (relocated into MAP LAYER float on load).
           When the user opens this collapsible inside the Map Layer panel,
           the inner cachePanel flies OUT to the LEFT (horizontal flyout)
           instead of pushing the Map Layer panel taller. -->
      <div id="offlineMappingPanel" style="position:relative; margin:8px 8px 0 8px; border:1px solid lime; background:rgba(0,0,0,0.7); padding:6px; font-family:monospace; font-size:0.7em; color:lime;">
        <div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" id="cacheToggle">
          <span style="color:#aaffaa; font-weight:bold;">▼ OFFLINE MAPPING</span>
          <span id="cacheToggleArrow" style="color:lime;">+</span>
        </div>
        <div id="cachePanel" style="display:none; margin-top:6px;">
          <!-- Place search (Nominatim) -->
          <div style="margin-bottom:6px;">
            <div style="font-size:0.95em; background: linear-gradient(to right, #ff00ff, #00ffff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight:bold;">⌕ PLACE SEARCH</div>
            <div style="display:flex; gap:3px; margin-top:3px;">
              <input id="geoQuery" type="text" placeholder="e.g. Yosemite National Park"
                     style="flex:1; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid #00ffff; font-family:monospace; font-size:0.9em; padding:2px;"/>
              <button id="geoGoBtn" style="padding:2px 8px; background:#001a1a; border:1px solid #00ffff; color:#00ffff; font-family:monospace; cursor:pointer;">GO</button>
            </div>
            <div id="geoResults" style="margin-top:4px; max-height:120px; overflow-y:auto;"></div>
          </div>
          <!-- Region presets -->
          <div style="margin-bottom:6px;">
            <div style="font-size:0.95em; background: linear-gradient(to right, lime, #00ffff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight:bold;">▣ REGION PRESETS</div>
            <select id="regionPreset" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px; margin-top:3px;">
              <option value="">— pick a region —</option>
              <option value='{"bbox":[-125,32,-114,42],"name":"california"}'>California</option>
              <option value='{"bbox":[-125,42,-117,49],"name":"pacific_northwest"}'>Pacific Northwest (OR/WA)</option>
              <option value='{"bbox":[-120,37,-117,40],"name":"eastern_sierra"}'>Eastern Sierra</option>
              <option value='{"bbox":[-125,24.5,-66.9,49.4],"name":"continental_us"}'>Continental US</option>
              <option value='{"bbox":[-74,40,-66,47.5],"name":"new_england"}'>New England</option>
              <option value='{"bbox":[-85,30,-76,39],"name":"appalachian"}'>Appalachian Trail corridor</option>
              <option value='{"bbox":[-87.6,24.5,-80,31],"name":"florida"}'>Florida</option>
              <option value='{"bbox":[-106.6,25.8,-93.5,36.5],"name":"texas"}'>Texas</option>
              <option value='{"bbox":[-160.3,18.9,-154.8,22.3],"name":"hawaii"}'>Hawaii</option>
              <option value='{"bbox":[-170,54,-130,72],"name":"alaska"}'>Alaska</option>
              <option value='{"bbox":[-11,49.8,2,59],"name":"uk"}'>United Kingdom</option>
              <option value='{"bbox":[2.5,42.3,8,51.1],"name":"germany_west"}'>Germany (west)</option>
              <option value='{"bbox":[129.5,30.5,146,46],"name":"japan"}'>Japan</option>
            </select>
          </div>
          <label style="display:block; margin-top:4px;">Source:
            <select id="cacheSource" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;">
              <option value="esriWorldImagery" selected>Esri World Imagery</option>
              <option value="cartoDarkMatter">CartoDB Dark Matter</option>
              <option value="cartoPositron">CartoDB Positron</option>
              <option value="esriWorldTopo">Esri World TopoMap</option>
              <option value="esriDarkGray">Esri Dark Gray Canvas</option>
              <option value="openTopoMap">OpenTopoMap</option>
              <option value="osmHumanitarian">OSM Humanitarian</option>
              <option value="osmStandard">OSM Standard</option>
            </select>
          </label>
          <label style="display:block; margin-top:4px;">Name (a-z 0-9 _ -):
            <input id="cacheName" type="text" placeholder="op_zone" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;"/>
          </label>
          <div style="display:flex; gap:4px; margin-top:4px;">
            <label style="flex:1;">zMin
              <input id="cacheZmin" type="number" min="0" max="22" value="10" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;"/>
            </label>
            <label style="flex:1;">zMax
              <input id="cacheZmax" type="number" min="0" max="22" value="16" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;"/>
            </label>
          </div>
          <div id="cacheEstimate" style="margin-top:4px; font-size:0.95em; color:#00ffff;">— tiles in current view</div>
          <button id="cacheStartBtn" style="margin-top:6px; width:100%; padding:4px; background:#003300; border:1px solid lime; color:lime; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">▶ START CACHE</button>
          <!-- World baseline presets — quick globe-wide overview at low zoom -->
          <div style="margin-top:8px; padding-top:6px; border-top:1px dashed #225522;">
            <div style="font-size:0.95em; background: linear-gradient(to right, #00ffff, lime); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight:bold; margin-bottom:3px;">◉ WORLD BASELINE</div>
            <div style="font-size:0.85em; color:#aaffaa; margin-bottom:4px;">Globe-wide overview of the selected source. Auto-names <code>world_&lt;source&gt;</code>.</div>
            <div style="display:flex; gap:4px;">
              <button id="worldZ6Btn" title="~5,500 tiles · ~80 MB · continents → cities"
                      style="flex:1; padding:4px; background:#001a1a; border:1px solid #00ffff; color:#00ffff; font-family:monospace; font-size:0.9em; border-radius:3px; cursor:pointer;">z0–6 (~80 MB)</button>
              <button id="worldZ8Btn" title="~88,000 tiles · ~1.3 GB · road networks visible"
                      style="flex:1; padding:4px; background:#001a1a; border:1px solid #00ffff; color:#00ffff; font-family:monospace; font-size:0.9em; border-radius:3px; cursor:pointer;">z0–8 (~1.3 GB)</button>
            </div>
          </div>
          <!-- Import existing MBTiles (URL or file upload) -->
          <div style="margin-top:8px; padding-top:6px; border-top:1px dashed #225522;">
            <div style="font-size:0.95em; background: linear-gradient(to right, #ff00ff, lime); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight:bold; margin-bottom:3px;">📥 IMPORT MBTILES</div>
            <div style="font-size:0.85em; color:#aaffaa; margin-bottom:4px;">Drop in a prebuilt file (raster or vector). Validates it's a real MBTiles before saving.</div>
            <label style="display:block;">Name (a-z 0-9 _ -):
              <input id="importName" type="text" placeholder="my_region"
                     style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid #ff00ff; font-family:monospace; font-size:0.9em; padding:2px;"/>
            </label>
            <label style="display:block; margin-top:3px;">URL (download):
              <input id="importUrl" type="text" placeholder="https://example.com/area.mbtiles"
                     style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid #ff00ff; font-family:monospace; font-size:0.9em; padding:2px;"/>
            </label>
            <button id="importUrlBtn" style="margin-top:4px; width:100%; padding:4px; background:#1a001a; border:1px solid #ff00ff; color:#ff00ff; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">⇩ DOWNLOAD URL</button>
            <div style="text-align:center; color:#aaffaa; margin:4px 0; font-size:0.85em;">— or —</div>
            <label style="display:block;">Upload file:
              <input id="importFile" type="file" accept=".mbtiles"
                     style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid #ff00ff; font-family:monospace; font-size:0.9em; padding:2px;"/>
            </label>
            <button id="importFileBtn" style="margin-top:4px; width:100%; padding:4px; background:#1a001a; border:1px solid #ff00ff; color:#ff00ff; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">↑ UPLOAD FILE</button>
            <div id="importJobs" style="margin-top:6px;"></div>
          </div>
          <div id="cacheJobs" style="margin-top:6px;"></div>
          <div id="cacheLayerList" style="margin-top:6px;"></div>
        </div>
      </div>
    </div>
    <!-- ADS-B AIR TRAFFIC (legacy panel, hidden — settings now live in the top-left adsbBox).
         Kept in the DOM so existing JS that drives `adsbEnabled`, `adsbSource`, etc. still
         finds its inputs; the top-left UI mirrors values into them via change events. -->
    <div id="adsbPanelOuter" style="display:none;">
      <div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" id="adsbToggle">
        <span style="color:#aaccff; font-weight:bold;">▼ AIR TRAFFIC (ADS-B)</span>
        <span id="adsbToggleArrow" style="color:#aaccff;">+</span>
      </div>
      <div id="adsbPanel" style="display:none; margin-top:6px;">
        <!-- Flight paths master toggles (drone/pilot/aircraft trail visibility).
             "ALL" is a meta-switch: ON enables every kind, OFF disables every
             kind. Per-kind switches stay independently usable below it. -->
        <div style="margin-bottom:6px; padding:6px; border:1px dashed #ff66cc; border-radius:3px;">
          <div style="color:#ffaaff; font-weight:bold; letter-spacing:1px; text-align:center; margin-bottom:4px; font-size:0.95em;">FLIGHT PATHS</div>
          <div style="display:flex; align-items:center; justify-content:space-between; gap:6px; padding-bottom:4px; border-bottom:1px dashed rgba(255,102,204,0.3);">
            <span style="font-size:0.95em; font-weight:bold; color:#ffccee;">ALL</span>
            <label class="switch" style="margin:0;"><input type="checkbox" id="pathsAllToggle"><span class="slider"></span></label>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; gap:6px; margin-top:4px;">
            <span style="font-size:0.95em;">Drone</span>
            <label class="switch" style="margin:0;"><input type="checkbox" id="pathsDroneToggle"><span class="slider"></span></label>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; gap:6px; margin-top:3px;">
            <span style="font-size:0.95em;">Pilot</span>
            <label class="switch" style="margin:0;"><input type="checkbox" id="pathsPilotToggle"><span class="slider"></span></label>
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; gap:6px; margin-top:3px;">
            <span style="font-size:0.95em;">Aircraft</span>
            <label class="switch" style="margin:0;"><input type="checkbox" id="pathsAircraftToggle"><span class="slider"></span></label>
          </div>
        </div>
        <label style="display:flex; align-items:center; gap:6px; margin-top:3px;">
          <input type="checkbox" id="adsbEnabled" style="accent-color:#00aaff;"/>
          <span>Enable ADS-B layer</span>
        </label>
        <label style="display:block; margin-top:4px;">Source:
          <select id="adsbSource" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;">
            <optgroup label="── Network (no setup) ──">
              <option value="adsblol">adsb.lol (free, no key)</option>
              <option value="adsbfi">adsb.fi (free, no key)</option>
              <option value="airplaneslive">airplanes.live (free, no key)</option>
              <option value="opensky">OpenSky Network (free)</option>
              <option value="adsbexchange">ADS-B Exchange (RapidAPI key)</option>
            </optgroup>
            <optgroup label="── Local SDR receivers ──">
              <option value="dump1090">Local SDR · HackRF / RTL-SDR / AirSpy / SDRplay</option>
              <option value="beast">Beast TCP raw feed (advanced)</option>
            </optgroup>
          </select>
        </label>
        <label style="display:block; margin-top:4px;">Refresh (sec):
          <input id="adsbInterval" type="number" min="2" max="120" value="8"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
        </label>
        <label style="display:flex; align-items:center; gap:6px; margin-top:4px;">
          <input type="checkbox" id="adsbBboxOnly" style="accent-color:#00aaff;"/>
          <span>Only fetch around current map view</span>
        </label>
        <!-- OSINT filter chips: which categories to render -->
        <div style="margin-top:6px;">
          <div style="font-size:0.95em; color:#aaccff; margin-bottom:3px;">Show:</div>
          <div id="adsbFilterChips" style="display:flex; flex-wrap:wrap; gap:3px;">
            <!-- chips populated by JS so colors match the marker palette -->
          </div>
        </div>
        <div id="adsbDump1090Box" style="display:none; margin-top:4px;">
          <div style="font-size:0.95em; color:#88aaff;">Works with any SDR fed through dump1090 / readsb / tar1090 / PiAware:</div>
          <label style="display:block; margin-top:2px;">Preset:
            <select id="adsbDump1090Preset" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;">
              <!-- populated from /api/adsb/sources -->
            </select>
          </label>
          <label style="display:block; margin-top:2px;">JSON URL:
            <input id="adsbDump1090Url" type="text" placeholder="http://localhost:8080/data/aircraft.json"
                   style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
          </label>
        </div>
        <div id="adsbOpenskyBox" style="display:none; margin-top:4px;">
          <div style="font-size:0.95em; color:#88aaff;">Optional auth (boosts rate limit):</div>
          <input id="adsbOpenskyUser" type="text" placeholder="opensky username"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px; margin-top:2px;"/>
          <input id="adsbOpenskyPass" type="password" placeholder="opensky password"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px; margin-top:2px;"/>
        </div>
        <div id="adsbExchangeBox" style="display:none; margin-top:4px;">
          <div style="font-size:0.95em; color:#88aaff;">RapidAPI key for ADS-B Exchange (paid tier):</div>
          <input id="adsbExchangeKey" type="password" placeholder="RapidAPI key"
                 style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px; margin-top:2px;"/>
          <div style="font-size:0.85em; color:#ffaa44; margin-top:2px;">Note: ADS-B Exchange requires the bbox toggle on.</div>
        </div>
        <div id="adsbBeastBox" style="display:none; margin-top:4px;">
          <div style="font-size:0.95em; color:#88aaff;">Direct connect to any ADS-B receiver speaking Beast (port 30005). Works with HackRF / RTL-SDR / AirSpy / SDRplay via dump1090, readsb, modesmixer2, or any FlightAware/PiAware feeder.</div>
          <div style="display:flex; gap:4px; margin-top:4px;">
            <label style="flex:2;">Host
              <input id="adsbBeastHost" type="text" placeholder="localhost"
                     style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
            </label>
            <label style="flex:1;">Port
              <input id="adsbBeastPort" type="number" placeholder="30005" min="1" max="65535"
                     style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>
            </label>
          </div>
          <div style="font-size:0.85em; color:#ffaa44; margin-top:2px;">
            Requires: <code>pip install pyModeS</code> (Mode-S/CPR decoder by junzis).
          </div>
        </div>
        <button id="adsbSaveBtn" style="margin-top:6px; width:100%; padding:4px; background:#001a2a; border:1px solid #00aaff; color:#aaeeff; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">SAVE CONFIG</button>
        <div style="font-size:0.85em; color:#88aaff; margin-top:2px; text-align:center;">
          (enable/disable handled by the toggle — this saves source &amp; credentials)
        </div>
        <div id="adsbStatus" style="margin-top:4px; font-size:0.9em; color:#88aaff; text-align:center;">— off —</div>
      </div>
    </div>

    <!-- GEOFENCING toolkit (draw, list, edit, delete + alerts) -->
    <div style="margin:8px 8px 0 8px; padding:6px; border:1px solid #ff5577; background:rgba(0,0,0,0.5); border-radius:4px; box-sizing:border-box; font-family:monospace; font-size:0.75em; color:#ffaaaa;">
      <div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" id="geofenceToggle">
        <span style="color:#ff8888; font-weight:bold; letter-spacing:1px;">▼ GEOFENCING</span>
        <span id="geofenceToggleArrow" style="color:#ff8888;">+</span>
      </div>
      <div id="geofencePanel" style="display:none; margin-top:6px;">
        <div style="display:flex; gap:4px;">
          <button id="drawPolygonBtn"   style="flex:1; padding:5px 4px; background:#1a000a; border:1px solid #ff5577; color:#ffaaaa; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">▱ POLY</button>
          <button id="drawCircleBtn"    style="flex:1; padding:5px 4px; background:#1a000a; border:1px solid #ff5577; color:#ffaaaa; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">○ CIRCLE</button>
          <button id="drawRectangleBtn" style="flex:1; padding:5px 4px; background:#1a000a; border:1px solid #ff5577; color:#ffaaaa; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">▭ RECT</button>
        </div>
        <!-- Inline form shown after a shape is drawn (replaces window.prompt) -->
        <div id="geofenceCreateForm" style="display:none; margin-top:6px; padding:6px; background:rgba(60,0,20,0.4); border:1px dashed #ff5577; border-radius:3px; font-size:0.95em;">
          <input id="gfFormName" type="text" placeholder="Fence name" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#ffcccc; border:1px solid #ff5577; font-family:monospace; padding:3px;"/>
          <div style="display:flex; align-items:center; gap:6px; margin-top:4px;">
            <span style="font-size:0.95em;">Color:</span>
            <div id="gfFormColorSwatches" style="display:flex; gap:3px; flex-wrap:wrap; flex:1;"></div>
            <input id="gfFormColor" type="color" value="#ff5577" style="width:28px; height:22px; padding:0; border:1px solid #ff5577; background:transparent; cursor:pointer;"/>
          </div>
          <div style="margin-top:6px;">
            <div style="font-size:0.78em; color:#a87f87; letter-spacing:1.5px; margin-bottom:3px;">WATCH</div>
            <div style="display:flex; gap:4px;">
              <label style="flex:1; display:flex; align-items:center; justify-content:center; gap:4px; padding:4px; border:1px solid rgba(255,102,136,0.35); border-radius:4px; cursor:pointer; font-size:0.85em;">
                <input id="gfFormTargetDrone" type="radio" name="gfFormTarget" value="drone" checked style="accent-color:#ff5577;">DRONES</label>
              <label style="flex:1; display:flex; align-items:center; justify-content:center; gap:4px; padding:4px; border:1px solid rgba(255,102,136,0.35); border-radius:4px; cursor:pointer; font-size:0.85em;">
                <input type="radio" name="gfFormTarget" value="aircraft" style="accent-color:#ff5577;">AIRCRAFT</label>
              <label style="flex:1; display:flex; align-items:center; justify-content:center; gap:4px; padding:4px; border:1px solid rgba(255,102,136,0.35); border-radius:4px; cursor:pointer; font-size:0.85em;">
                <input type="radio" name="gfFormTarget" value="both" style="accent-color:#ff5577;">BOTH</label>
            </div>
          </div>
          <div id="gfFormDroneTagsRow" style="margin-top:6px;">
            <div style="font-size:0.78em; color:#a87f87; letter-spacing:1.5px; margin-bottom:3px;">DRONE TAGS <span style="color:#7a5a60; font-weight:normal; letter-spacing:0; font-size:0.95em;">(blank = any)</span></div>
            <div id="gfFormTagChips" style="display:flex; flex-wrap:wrap; gap:3px;"></div>
          </div>
          <div id="gfFormAircraftTagsRow" style="display:none; margin-top:6px;">
            <div style="font-size:0.78em; color:#a87f87; letter-spacing:1.5px; margin-bottom:3px;">AIRCRAFT TAGS <span style="color:#7a5a60; font-weight:normal; letter-spacing:0; font-size:0.95em;">(blank = any)</span></div>
            <div id="gfFormAircraftChips" style="display:flex; flex-wrap:wrap; gap:3px;"></div>
          </div>
          <div style="margin-top:6px;">
            <div style="font-size:0.78em; color:#a87f87; letter-spacing:1.5px; margin-bottom:3px;">PER-FENCE WEBHOOK <span style="color:#7a5a60; font-weight:normal; letter-spacing:0; font-size:0.95em;">(blank = use global)</span></div>
            <input id="gfFormWebhook" type="text" placeholder="https://example.com/hook" style="width:100%; box-sizing:border-box; background:rgba(255,255,255,0.04); color:#ffcccc; border:1px solid rgba(255,102,136,0.35); border-radius:4px; font-family:inherit; padding:5px 7px; font-size:0.85em;"/>
          </div>
          <div style="display:flex; gap:6px; margin-top:8px; font-size:0.85em;">
            <label style="flex:1; display:flex; align-items:center; gap:4px;"><input id="gfFormEnter" type="checkbox" checked style="accent-color:#ff5577;">Alert on enter</label>
            <label style="flex:1; display:flex; align-items:center; gap:4px;"><input id="gfFormExit" type="checkbox" checked style="accent-color:#ff5577;">Alert on exit</label>
          </div>
          <div style="display:flex; gap:4px; margin-top:8px;">
            <button id="gfFormCancel" style="flex:1; padding:6px; background:transparent; border:1px solid #6b4248; color:#ffaaaa; font-family:inherit; font-size:0.78em; letter-spacing:1px; font-weight:600; border-radius:5px; cursor:pointer;">CANCEL</button>
            <button id="gfFormSave"   style="flex:2; padding:6px; background:#ff5577; border:0; color:#1a0000; font-family:inherit; font-size:0.78em; letter-spacing:1px; font-weight:700; border-radius:5px; cursor:pointer;">SAVE FENCE</button>
          </div>
        </div>
        <div id="geofenceList" style="margin-top:6px; max-height:240px; overflow-y:auto;"></div>
        <div style="margin-top:6px; padding-top:4px; border-top:1px dashed #552222;">
          <div id="geofenceAlertHeader" style="display:flex; justify-content:space-between; align-items:center; cursor:pointer; color:#ffaaaa; font-weight:bold; margin-bottom:3px;">
            <span>Recent alerts <span id="geofenceAlertCount" style="color:#a87f87; font-weight:normal;"></span></span>
            <span id="geofenceAlertArrow" style="color:#ff8888;">−</span>
          </div>
          <div id="geofenceAlertList" style="max-height:220px; overflow-y:auto; font-size:0.95em; color:#ffcccc;">— none —</div>
        </div>
      </div>
    </div>
    <!-- SETTINGS / EXPORTS expansion -->
    <div style="margin:8px 8px 0 8px; border:1px solid lime; background:rgba(0,0,0,0.7); padding:6px; font-family:monospace; font-size:0.7em; color:lime;">
      <div style="display:flex; justify-content:space-between; align-items:center; cursor:pointer;" id="settingsToggle">
        <span style="color:#aaffaa; font-weight:bold;">▼ SETTINGS &amp; EXPORTS</span>
        <span id="settingsToggleArrow" style="color:lime;">+</span>
      </div>
      <div id="settingsPanel" style="display:none; margin-top:6px;">
        <!-- Archive / Export buttons -->
        <div style="margin-bottom:6px;">
          <div style="font-size:0.95em; color:#aaffaa; margin-bottom:3px;">Session Archives</div>
          <div style="display:flex; gap:4px;">
            <button id="downloadCsv" style="flex:1;">CSV</button>
            <button id="downloadKml" style="flex:1;">KML</button>
            <button id="downloadAliases" style="flex:1;">Aliases</button>
          </div>
          <div style="font-size:0.95em; color:#aaffaa; margin:6px 0 3px 0;">Cumulative History</div>
          <div style="display:flex; gap:4px;">
            <button id="downloadCumulativeCsv" style="flex:1;">Cumulative CSV</button>
            <button id="downloadCumulativeKml" style="flex:1;">Cumulative KML</button>
          </div>
        </div>
        <!-- Port / hardware config (inline, no navigation) -->
        <div style="padding-top:6px; border-top:1px dashed #225522;">
          <div style="font-size:0.95em; color:#aaffaa; margin-bottom:3px; font-weight:bold; letter-spacing:1px;">USB PORTS · DRONE RECEIVERS</div>
          <div style="font-size:0.85em; color:#779977; margin-bottom:3px;">ESP32 boards forwarding Remote-ID detections over serial.</div>
          <div id="portRows" style="display:flex; flex-direction:column; gap:3px;">
            <select class="portSel" data-idx="1" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;"></select>
            <select class="portSel" data-idx="2" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;"></select>
            <select class="portSel" data-idx="3" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.9em; padding:2px;"></select>
          </div>
          <div style="display:flex; gap:4px; margin-top:4px;">
            <button id="portRefreshBtn" style="flex:1; padding:4px; border:1px solid lime; background-color:#001a00; color:lime; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">REFRESH</button>
            <button id="portApplyBtn" style="flex:2; padding:4px; border:1px solid lime; background-color:#003300; color:lime; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">APPLY</button>
          </div>
          <div id="portStatus" style="margin-top:4px; font-size:0.9em; color:#88aaff; text-align:center;"></div>
        </div>
        <!-- Webhooks (collapsible). Detection webhook fires on every drone
             ingest; geofence webhook fires on enter/exit alerts. If geofence
             URL is blank, geofence events fall back to the detection URL. -->
        <div style="margin-top:8px; padding-top:6px; border-top:1px dashed #225522;">
          <div style="cursor:pointer; user-select:none; color:#aaffaa; font-weight:bold; letter-spacing:1px; display:flex; justify-content:space-between; align-items:center;" id="webhooksToggle">
            <span><span id="webhooksArrow">&#9656;</span> WEBHOOKS</span>
            <span id="webhooksStatus" style="color:#779977; font-weight:normal; font-size:0.95em;"></span>
          </div>
          <div id="webhooksPanel" style="display:none; margin-top:6px;">
            <div style="font-size:0.95em; color:#aaffaa; margin:4px 0 2px 0;">Detection (drones)</div>
            <input id="webhookUrlMain" type="text" placeholder="https://example.com/hook"
                   style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#FF00FF; border:1px solid lime; font-family:monospace; font-size:0.95em; padding:3px;"/>
            <div style="font-size:0.95em; color:#aaffaa; margin:6px 0 2px 0;">Geofence alerts</div>
            <input id="webhookUrlGeofence" type="text" placeholder="(optional — falls back to detection URL)"
                   style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#ff66ff; border:1px solid #ff66ff; font-family:monospace; font-size:0.95em; padding:3px;"/>
            <div style="display:flex; gap:4px; margin-top:6px;">
              <button id="webhookSaveBtn" style="flex:2; padding:4px; border:1px solid lime; background:#003300; color:lime; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">SAVE</button>
              <button id="webhookTestBtn" style="flex:1; padding:4px; border:1px solid #00aaff; background:#001a2a; color:#00aaff; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;" title="POST a sample geofence payload to the geofence URL (or detection URL if blank)">TEST</button>
            </div>
            <div id="webhookSaveResult" style="margin-top:4px; font-size:0.9em; text-align:center;"></div>
          </div>
        </div>
        <!-- Holders for the offline-mapping + adsb panels (moved here from main UI on load) -->
        <div id="offlineMappingHolder" style="margin-top:8px;"></div>
        <div id="adsbHolder" style="margin-top:8px;"></div>
      </div>
    </div>
    <!-- USB Status display: kept always visible by CSS rule (.alwaysVisible) below -->
    <div class="alwaysVisible" style="margin-top:8px; width:fit-content; max-width:calc(100% - 16px); margin:8px auto 0 auto; border: 1px solid purple; background: black; padding:4px 8px; display:flex; justify-content:center; align-items:center;">
      <div id="serialStatus" style="font-family:monospace; font-size:0.7em; text-align:center; line-height:1.2em;">
        <!-- USB port statuses will be injected here via WebSocket -->
      </div>
    </div>
</div><!-- /#filterBox -->
<script>
  // Do not clear trackedPairs; persist across reloads
  // Track drones already alerted for no GPS
  const alertedNoGpsDrones = new Set();
  // Round positions to integer pixels ONLY for tile container elements — this
  // prevents the sub-pixel seam between tiles without making marker/polyline
  // pan motion stepwise. Modern Leaflet uses translate3d which the GPU
  // interpolates smoothly when we leave non-tile elements alone.
  L.DomUtil.setPosition = (function() {
    var original = L.DomUtil.setPosition;
    return function(el, point) {
      if (el && el.classList && (
            el.classList.contains('leaflet-tile') ||
            el.classList.contains('leaflet-tile-container'))) {
        original.call(this, el, L.point(Math.round(point.x), Math.round(point.y)));
      } else {
        original.call(this, el, point);
      }
    };
  })();

// --- Socket.IO real-time updates ---
const socket = io();
// Hoisted shared state — declared up-front so socket.on handlers (which can fire
// the moment they're attached) don't hit TDZ accessing them.
let persistentMACs = [];

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
  // Re-sync the single-line layout now that .collapsed reflects the persisted state
  if (typeof _syncDronesCollapsedLayout === 'function') _syncDronesCollapsedLayout(wasCollapsed);
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
  // Adaptive polling: PAUSE detection updates entirely during a pan/zoom gesture.
  // Running updateData mid-gesture moves markers, rebuilds trails and re-renders
  // the list — all of which fight the map's pan/zoom transform and cause hitching.
  // We stop the loop on gesture start and resume it (with one immediate refresh so
  // drones snap to current data the instant the gesture ends) on gesture end.
  map.on('zoomstart dragstart', () => {
    if (updateDataInterval) { clearInterval(updateDataInterval); updateDataInterval = null; }
  });
  map.on('zoomend dragend', () => {
    if (updateDataInterval) clearInterval(updateDataInterval);
    updateData();   // snap to current data immediately when the gesture ends
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
  _syncDronesCollapsedLayout(isCollapsed);
});

// USB lives in the Settings float now; collapse layout is just title + [+]
function _syncDronesCollapsedLayout(_isCollapsed) { /* no-op kept for backward calls */ }

// ---------- Drones layer toggle (mirrors the ADS-B layer toggle UX) ----------
// Hides every drone/pilot marker, circle, ring, and trail while keeping the
// detection pipeline running (so the active list still updates and re-enabling
// the layer instantly redraws everything from current state).
// Wiring is deferred via setTimeout because `const map` and the path layer
// groups are declared further down in the script — referencing them at
// script-eval time would be TDZ-error.
function _setDronesLayerVisible(visible) {
  if (typeof map === 'undefined') return;
  const panes = ['droneIconPane','pilotIconPane','droneCirclePane','pilotCirclePane'];
  panes.forEach(p => {
    const pane = map.getPane(p);
    if (pane) pane.style.display = visible ? '' : 'none';
  });
  // Update the ON/OFF badge so the toggle state is unmistakeable at a glance
  const lbl = document.getElementById('dronesLayerStateLabel');
  if (lbl) {
    lbl.textContent = visible ? 'ON' : 'OFF';
    // Lime accent matches the drone-popup/drone-panel family.
    lbl.style.color = visible ? '#88ff99' : '#586978';
    lbl.style.borderColor = visible ? 'rgba(136,255,153,0.55)' : 'rgba(255,255,255,0.10)';
    lbl.style.background = visible ? 'rgba(136,255,153,0.10)' : 'transparent';
  }
  // Path / trail layers respect the per-kind master toggles when re-shown
  if (typeof dronePathLayer !== 'undefined' && typeof pilotPathLayer !== 'undefined') {
    if (visible) {
      if (_pathsMasters.drone && !map.hasLayer(dronePathLayer)) dronePathLayer.addTo(map);
      if (_pathsMasters.pilot && !map.hasLayer(pilotPathLayer)) pilotPathLayer.addTo(map);
    } else {
      if (map.hasLayer(dronePathLayer)) map.removeLayer(dronePathLayer);
      if (map.hasLayer(pilotPathLayer)) map.removeLayer(pilotPathLayer);
    }
  }
  localStorage.setItem('dronesLayerOn', visible ? '1' : '0');
}
setTimeout(() => {
  const cb = document.getElementById('dronesLayerToggle');
  if (!cb) return;
  const persisted = localStorage.getItem('dronesLayerOn');
  const initial = persisted === null ? true : (persisted === '1');
  cb.checked = initial;
  _setDronesLayerVisible(initial);
  cb.addEventListener('change', (ev) => {
    ev.stopPropagation();
    _setDronesLayerVisible(cb.checked);
  });
}, 0);
// Configure tile loading for smooth zoom transitions
L.Map.prototype.options.fadeAnimation = true;
L.Map.prototype.options.zoomAnimation = true;
// ─────────── Tile loading tuned for "pro cartography app" feel ───────────
// Request new tiles WHILE panning (not just on settle), keep a generous ring
// of off-screen tiles so panning never reveals a blank seam, let the layer
// update mid-zoom, and lean on parallel HTTP/2 connections via subdomains.
//
// Note: we deliberately DO NOT set crossOrigin='anonymous'. Some providers
// (Esri, OpenTopoMap) return tiles without CORS headers, and turning this on
// makes those tiles fail to render — the dreaded "black box" symptom. Tiles
// don't need crossOrigin unless we're sampling pixels via canvas.
L.TileLayer.prototype.options.updateWhenZooming = true;
L.TileLayer.prototype.options.updateWhenIdle = false;   // load while panning
L.TileLayer.prototype.options.updateInterval  = 80;     // gentler refresh cadence
// Use default tileSize for crisp rendering on every device pixel ratio.
L.TileLayer.prototype.options.detectRetina = false;
// Off-screen ring of pre-rendered tiles (in tile widths). Bumped to 8 so a
// fast flick-pan still always reveals already-loaded tiles instead of black
// holes. The cost is some memory; the win is "always smooth, no blanks".
L.TileLayer.prototype.options.keepBuffer = 8;
L.GridLayer.prototype.options.keepBuffer = 8;
// Slight fade-in mask on tile load so any pop-in feels intentional.
L.TileLayer.prototype.options.fadeAnimation = true;
// On window load, restore persisted detection data (trackedPairs) and re-add markers.
window.onload = function() {
  let stored = localStorage.getItem("trackedPairs");
  if (stored) {
    try {
      let storedPairs = JSON.parse(stored);
      window.tracked_pairs = storedPairs;
      const nowSec = Date.now() / 1000;
      for (const mac in storedPairs) {
        let det = storedPairs[mac];
        let color = get_color_for_mac(mac);
        // Compute staleness for the RESTORED data. On a hard refresh, the
        // marker may be hours old — render it dimmed so the user knows it's
        // a stale snapshot, not a live ping. Once a fresh detection lands,
        // updateData() flips opacity back to 1.0.
        const isStale = !det.last_update || (nowSec - det.last_update) > STALE_THRESHOLD;
        const restoreOpacity = isStale ? 0.35 : 1.0;
        // Restore drone marker if valid coordinates exist.
        if (det.drone_lat && det.drone_long && det.drone_lat != 0 && det.drone_long != 0) {
          if (!droneMarkers[mac]) {
            droneMarkers[mac] = L.marker([det.drone_lat, det.drone_long], {icon: createDroneIcon(color), pane: 'droneIconPane', bubblingMouseEvents: false, opacity: restoreOpacity})
                                  .bindPopup(generatePopupContent(det, 'drone'), {className: 'drone-popup', maxWidth: 300, minWidth: 240, closeButton: true})
                                  .addTo(map);
          }
        }
        // Restore pilot marker if valid coordinates exist.
        if (det.pilot_lat && det.pilot_long && det.pilot_lat != 0 && det.pilot_long != 0) {
          if (!pilotMarkers[mac]) {
            pilotMarkers[mac] = L.marker([det.pilot_lat, det.pilot_long], {icon: createPilotIcon(color), pane: 'pilotIconPane', bubblingMouseEvents: false, opacity: restoreOpacity})
                                  .bindPopup(generatePopupContent(det, 'pilot'), {className: 'drone-popup', maxWidth: 300, minWidth: 240, closeButton: true})
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
    <strong>Observer Location</strong><br>
    <div style="display:flex; gap:4px; justify-content:center; margin-top:6px;">
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
    observerMarker.setIcon(createObserverIcon('blue'));
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
  // Drone popup — same visual language as the ADS-B aircraft popup (dark
  // gradient card, system UI, label/value rows, pill buttons, iOS toggles)
  // but with a lime accent so it reads as "drone" at a glance. Every existing
  // feature kept: alias, FAA RemoteID + lookup, OSINT tag, lock-on follow,
  // path toggles, color slider, Google Maps links, raw key/value telemetry.
  const mac = detection.mac;
  const accent = '#88ff99';   // lime accent (drone vocabulary)
  const muted  = '#7e9486';
  const aliasText = aliases[mac] ? aliases[mac] : '';
  const curTag = (window.droneTags && droneTags[mac.toLowerCase()]) || 'unknown';
  const tagColor = DRONE_TAG_COLORS[curTag] || '#888';
  const safeMac = mac.replace(/[^A-Za-z0-9]/g, '');

  // ── stat row helper (same shape as the ADS-B popup's `stat`) ──
  const stat = (label, value, color) =>
      '<div style="display:flex; justify-content:space-between; padding:2px 0; gap:8px;">'
    + '<span style="color:' + muted + ';">' + label + '</span>'
    + '<span style="color:' + (color || '#dde6ee') + '; font-weight:600; text-align:right; word-break:break-word;">'
    + value + '</span></div>';

  const pillBtn = (id, label, onclick, active) => {
    const fg = active ? '#001028' : accent;
    const bg = active ? accent : 'transparent';
    return '<button id="' + id + '" onclick="event.stopPropagation(); ' + onclick + '" '
      + 'style="flex:1; padding:6px 0; border:' + (active ? '0' : '1px solid ' + accent) + '; '
      + 'border-radius:5px; cursor:pointer; background:' + bg + '; color:' + fg + '; '
      + 'font-family:inherit; font-weight:600; letter-spacing:1px; font-size:0.75em;">'
      + label + '</button>';
  };

  // ── Pure-CSS toggle (path show/hide). Animates via input:checked siblings,
  //    so it stays responsive even while the popup is open and HTML re-render
  //    is suppressed.
  const toggleSwitch = (id, label, on, onchange) =>
      '<label style="display:flex; align-items:center; justify-content:space-between; '
    + 'padding:5px 2px; cursor:pointer; flex:1; min-width:0;" onclick="event.stopPropagation();">'
    + '<span style="color:' + muted + '; font-size:0.78em; letter-spacing:0.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">'
    + label + '</span>'
    + '<span class="pop-toggle">'
    +   '<input id="' + id + '" type="checkbox"' + (on ? ' checked' : '')
    +   ' onchange="event.stopPropagation(); ' + onchange + '">'
    +   '<span class="pop-track"></span>'
    +   '<span class="pop-knob"></span>'
    + '</span></label>';

  let html = '';

  // ── Header: alias (or MAC) + tag chip ──
  const headTitle = aliasText || mac;
  html += '<div style="font-size:1.05em; font-weight:700; color:#fff; letter-spacing:0.3px; '
       +  'overflow:hidden; text-overflow:ellipsis;">' + headTitle + '</div>';
  html += '<div style="display:flex; align-items:center; gap:6px; margin-top:1px;">'
       +    '<span style="color:' + muted + '; font-size:0.72em; letter-spacing:0.5px;">' + mac + '</span>'
       +    '<span id="droneTagPill_' + safeMac + '" style="margin-left:auto; font-size:0.7em; padding:1px 6px; border-radius:9px; '
       +      'background:' + tagColor + '22; color:' + tagColor + '; border:1px solid ' + tagColor + '55; letter-spacing:1px; font-weight:600;">'
       +      curTag.toUpperCase() + '</span>'
       + '</div>';

  // ── FAA RemoteID + lookup ──
  if (detection.basic_id || detection.faa_data) {
    html += '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>';
    html += '<div style="font-size:0.72em; color:' + muted + '; letter-spacing:1.5px; margin-bottom:4px;">FAA REMOTE-ID</div>';
    if (detection.basic_id) {
      html += stat('SERIAL', detection.basic_id, '#fff');
      html += '<button onclick="event.stopPropagation(); queryFaaAPI(\\'' + mac + '\\', \\'' + detection.basic_id + '\\')" '
           +  'id="queryFaaButton_' + mac + '" '
           +  'style="width:100%; margin-top:6px; padding:5px 0; border:1px solid ' + accent + '; border-radius:5px; '
           +  'background:transparent; color:' + accent + '; font-family:inherit; font-weight:600; letter-spacing:1px; '
           +  'font-size:0.72em; cursor:pointer;">QUERY FAA</button>';
    }
    html += '<div id="faaResult_' + mac + '" style="margin-top:6px;">';
    if (detection.faa_data) {
      let item = null;
      const fd = detection.faa_data;
      if (fd && fd.data && fd.data.items && fd.data.items.length > 0) item = fd.data.items[0];
      if (item) {
        const fields = ['makeName', 'modelName', 'series', 'trackingNumber', 'complianceCategories', 'updatedAt'];
        html += '<div style="font-size:0.85em; line-height:1.45; padding:6px 8px; background:rgba(136,255,153,0.05); border:1px solid rgba(136,255,153,0.18); border-radius:4px;">';
        fields.forEach(f => {
          if (item[f] !== undefined && item[f] !== '') html += stat(f, String(item[f]), '#dde6ee');
        });
        html += '</div>';
      } else {
        html += '<div style="font-size:0.78em; color:' + muted + '; font-style:italic; padding:4px 0;">No FAA data available</div>';
      }
    }
    html += '</div>';
  }

  // ── Telemetry (key/value, smart-filtered) ──
  // Hide internal/dev fields the user doesn't care about (_simulated comes
  // from the demo flight injector; lockTime/userLocked are UI bookkeeping).
  const skip = new Set(['mac','basic_id','last_update','userLocked','lockTime','faa_data','_simulated']);
  const telemetryKeys = Object.keys(detection).filter(k => !skip.has(k) && detection[k] !== '' && detection[k] !== null && detection[k] !== undefined);
  if (telemetryKeys.length > 0) {
    html += '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>';
    html += '<div style="font-size:0.72em; color:' + muted + '; letter-spacing:1.5px; margin-bottom:4px;">TELEMETRY</div>';
    // Field-specific decimal precision so coords don't look like scientific
    // notation. Lat/lon → 5 dp (≈1 m on the ground). Altitude/speed/heading/
    // distance → integer. Anything else → 2 dp max.
    const _fmtNumber = (key, v) => {
      const k = key.toLowerCase();
      if (k.endsWith('lat') || k.endsWith('lng') || k.endsWith('long') || k.endsWith('lon')) {
        return v.toFixed(5);
      }
      if (k.includes('alt') || k.includes('speed') || k.includes('heading')
          || k.includes('hdg')  || k.includes('rssi')  || k.includes('vel')
          || k.includes('rate') || k.includes('dist')  || k.includes('range')
          || k.includes('count')|| k.includes('time')) {
        return Math.round(v).toString();
      }
      if (Math.abs(v) < 1 && v !== 0) return v.toFixed(3);
      // General case: 2 decimals, trim trailing zeros
      return v.toFixed(2).replace(/\\.?0+$/, '');
    };
    html += '<div style="font-size:0.85em; line-height:1.45;">';
    telemetryKeys.forEach(k => {
      let v = detection[k];
      if (typeof v === 'number') v = _fmtNumber(k, v);
      html += stat(k, String(v));
    });
    html += '</div>';
  }

  // ── Google Maps links ──
  const validDrone = detection.drone_lat && detection.drone_long && detection.drone_lat !== 0 && detection.drone_long !== 0;
  const validPilot = detection.pilot_lat && detection.pilot_long && detection.pilot_lat !== 0 && detection.pilot_long !== 0;
  if (validDrone || validPilot) {
    html += '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>';
    html += '<div style="display:flex; gap:6px;">';
    if (validDrone) {
      html += '<a target="_blank" rel="noopener" '
           +  'href="https://www.google.com/maps/search/?api=1&query=' + detection.drone_lat + ',' + detection.drone_long + '" '
           +  'style="flex:1; text-align:center; padding:5px 0; border:1px solid rgba(136,255,153,0.4); border-radius:5px; '
           +  'color:' + accent + '; text-decoration:none; font-size:0.72em; font-weight:600; letter-spacing:1px;">GMAPS · DRONE</a>';
    }
    if (validPilot) {
      html += '<a target="_blank" rel="noopener" '
           +  'href="https://www.google.com/maps/search/?api=1&query=' + detection.pilot_lat + ',' + detection.pilot_long + '" '
           +  'style="flex:1; text-align:center; padding:5px 0; border:1px solid rgba(136,255,153,0.4); border-radius:5px; '
           +  'color:' + accent + '; text-decoration:none; font-size:0.72em; font-weight:600; letter-spacing:1px;">GMAPS · PILOT</a>';
    }
    html += '</div>';
  }

  // ── Alias + OSINT tag ──
  html += '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>';
  html += '<div style="font-size:0.72em; color:' + muted + '; letter-spacing:1.5px; margin-bottom:4px;">IDENTITY</div>';
  html += '<input type="text" id="aliasInput" placeholder="Alias…" '
       +  'onclick="event.stopPropagation();" ontouchstart="event.stopPropagation();" '
       +  'value="' + (aliasText || '').replace(/"/g, '&quot;') + '" '
       +  'style="width:100%; box-sizing:border-box; background:rgba(255,255,255,0.04); color:#fff; '
       +  'border:1px solid rgba(255,255,255,0.12); border-radius:4px; padding:6px 8px; '
       +  'font-family:inherit; font-size:0.85em; outline:none;">';
  html += '<div style="display:flex; gap:6px; margin-top:6px;">';
  html += pillBtn('saveAliasBtn_' + safeMac, 'SAVE ALIAS',
                  'saveAlias(\\'' + mac + '\\');', false);
  html += pillBtn('clearAliasBtn_' + safeMac, 'CLEAR',
                  'clearAlias(\\'' + mac + '\\');', false);
  html += '</div>';
  html += '<div style="display:flex; align-items:center; gap:8px; margin-top:8px;">';
  html += '<span style="color:' + muted + '; font-size:0.72em; letter-spacing:1.5px; min-width:32px;">TAG</span>';
  html += '<select id="droneTagSelect_' + safeMac + '" '
       +  'onclick="event.stopPropagation();" '
       +  'onchange="saveDroneTag(\\'' + mac + '\\', this.value)" '
       +  'style="flex:1; background:rgba(255,255,255,0.04); color:' + tagColor + '; '
       +  'border:1px solid ' + tagColor + '55; border-radius:4px; padding:5px 6px; '
       +  'font-family:inherit; font-size:0.85em; font-weight:600; outline:none;">';
  DRONE_TAG_VALUES.forEach(v => {
    html += '<option value="' + v + '"' + (v === curTag ? ' selected' : '') + '>' + v.toUpperCase() + '</option>';
  });
  html += '</select></div>';

  // ── Tracking + paths ──
  html += '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>';
  const isDroneLocked = (followLock.enabled && followLock.type === 'drone' && followLock.id === mac);
  const isPilotLocked = (followLock.enabled && followLock.type === 'pilot' && followLock.id === mac);
  html += '<div style="display:flex; gap:6px;">';
  html += pillBtn('lock-drone-' + mac,
                  isDroneLocked ? 'TRACKING DRONE' : 'TRACK DRONE',
                  (isDroneLocked
                    ? 'unlockMarker(\\'drone\\', \\'' + mac + '\\');'
                    : 'lockMarker(\\'drone\\', \\'' + mac + '\\');'),
                  isDroneLocked);
  if (validPilot) {
    html += pillBtn('lock-pilot-' + mac,
                    isPilotLocked ? 'TRACKING PILOT' : 'TRACK PILOT',
                    (isPilotLocked
                      ? 'unlockMarker(\\'pilot\\', \\'' + mac + '\\');'
                      : 'lockMarker(\\'pilot\\', \\'' + mac + '\\');'),
                    isPilotLocked);
  }
  html += '</div>';

  // Path toggles — drone always, pilot only if we ever saw it
  const dronePathOn = !hiddenPaths.has('drone:' + mac);
  const pilotPathOn = !hiddenPaths.has('pilot:' + mac);
  html += '<div style="display:flex; gap:8px; margin-top:2px;">';
  html += toggleSwitch('drone-path-' + safeMac, 'DRONE PATH', dronePathOn,
                       'setPathHidden(\\'drone:' + mac + '\\', !this.checked);');
  if (validPilot) {
    html += toggleSwitch('pilot-path-' + safeMac, 'PILOT PATH', pilotPathOn,
                         'setPathHidden(\\'pilot:' + mac + '\\', !this.checked);');
  }
  html += '</div>';

  // ── Color hue slider ──
  let defaultHue = colorOverrides[mac] !== undefined ? colorOverrides[mac] : (function(){
    let hash = 0;
    for (let i = 0; i < mac.length; i++) hash = mac.charCodeAt(i) + ((hash << 5) - hash);
    return Math.abs(hash) % 360;
  })();
  html += '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>';
  html += '<div style="display:flex; align-items:center; gap:8px;">';
  html += '<span style="color:' + muted + '; font-size:0.72em; letter-spacing:1.5px; min-width:38px;">COLOR</span>';
  html += '<input type="range" id="colorSlider_' + mac + '" min="0" max="360" value="' + defaultHue + '" '
       +  'onclick="event.stopPropagation();" '
       +  'onchange="updateColor(\\'' + mac + '\\', this.value)" '
       +  'style="flex:1; accent-color:' + accent + ';">';
  html += '</div>';

  return html;
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
  const prevId = followLock.id;
  followLock = { type: markerType, id: id, enabled: true };
  localStorage.setItem('followLock', JSON.stringify(followLock));
  _refreshDronePopup(id);
  if (prevId && prevId !== id) _refreshDronePopup(prevId);
}

function unlockMarker(markerType, id) {
  if (followLock.enabled && followLock.type === markerType && followLock.id === id) {
    followLock = { type: null, id: null, enabled: false };
    localStorage.setItem('followLock', JSON.stringify(followLock));
    _refreshDronePopup(id);
  }
}

// Force-rebuild a drone/pilot popup's HTML from the latest tracked_pairs entry.
// Used when an intentional state change (lock/unlock, alias save, tag pick)
// needs the popup to visually reflect new data — the per-snapshot re-render
// is suppressed while the popup is open, so we trigger this explicitly.
function _refreshDronePopup(mac) {
  const det = (window.tracked_pairs && window.tracked_pairs[mac]) || {mac};
  if (droneMarkers[mac]) droneMarkers[mac].setPopupContent(generatePopupContent(det, 'drone'));
  if (pilotMarkers[mac]) pilotMarkers[mac].setPopupContent(generatePopupContent(det, 'pilot'));
}
window._refreshDronePopup = _refreshDronePopup;

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

// ---------- Drone OSINT tags ----------
const DRONE_TAG_VALUES = ['unknown','civilian','police','government','military','commercial','known'];
const DRONE_TAG_COLORS = {
  unknown:    '#888888',
  civilian:   '#88ddff',
  known:      '#88ff88',
  police:     '#33aaff',
  government: '#ffcc00',
  military:   '#ff8800',
  commercial: '#88ff88',
};
window.droneTags = {};

async function loadDroneTags() {
  try {
    const r = await fetch('/api/drone_tags');
    const d = await r.json();
    window.droneTags = d.tags || {};
  } catch (e) { console.debug('drone tags load failed:', e); }
}
loadDroneTags();
socket.on('drone_tags', (msg) => {
  if (msg && msg.tags) window.droneTags = msg.tags;
});

async function saveDroneTag(mac, tag) {
  try {
    const r = await fetch('/api/drone_tags/' + encodeURIComponent(mac), {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({tag}),
    });
    const j = await r.json();
    if (!r.ok) { alert(j.error || 'failed'); return; }
    window.droneTags[mac.toLowerCase()] = (tag && tag !== 'unknown') ? tag : undefined;
    if (!window.droneTags[mac.toLowerCase()]) delete window.droneTags[mac.toLowerCase()];
    // Refresh the popup so the badge color updates
    if (droneMarkers[mac]) {
      droneMarkers[mac].setPopupContent(generatePopupContent(tracked_pairs[mac], 'drone'));
    }
  } catch (e) { alert('save failed: ' + e); }
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

// Each layer specifies subdomains where applicable so the browser opens
// parallel HTTP/2 connections for tile fetches — that's the difference between
// "tiles trickle in" and "tiles arrive in waves".
const osmStandard = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  subdomains: 'abc',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const osmHumanitarian = L.tileLayer('https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', {
  attribution: '© Humanitarian OpenStreetMap Team',
  subdomains: 'abc',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const cartoPositron = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap contributors, © CARTO',
  subdomains: 'abcd',
  maxNativeZoom: 19,
  maxZoom: 22,
});
const cartoDarkMatter = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '© OpenStreetMap contributors, © CARTO',
  subdomains: 'abcd',
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
  subdomains: 'abc',
  maxNativeZoom: 17,
  maxZoom: 17,
});

  // Load persisted basemap selection or default to satellite imagery.
  // Offline layers (offline:<name>) are applied later once /api/offline_layers responds.
  var persistedBasemap = localStorage.getItem('basemap') || 'esriWorldImagery';
  if (persistedBasemap.indexOf('offline:') !== 0) {
    document.getElementById('layerSelect').value = persistedBasemap;
  }
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
  zoomControl: false,                  // re-added bottom-left below
  // closePopupOnClick stays at the default (true) so clicking empty map area
  // closes any open popup — the user expects that. Marker clicks themselves
  // are isolated from this via `bubblingMouseEvents: false` on the marker
  // creation: a click on a marker does NOT bubble to the map, so the map's
  // close-on-click logic doesn't fire and the popup stays open.
  // Hard zoom limits — let the user zoom out all the way to the world.
  // Per-layer maxZoom is enforced separately in applyBasemap so high-zoom layers
  // (Carto z20, OpenTopoMap z17) clamp correctly when active.
  minZoom: 0,
  maxZoom: 22,
  worldCopyJump: true,
  // ───────── Smooth pan/zoom feel ─────────
  // Fractional zoom — wheel and pinch zoom land at any decimal level instead
  // of snapping to whole integers. Combined with zoomDelta = 0.5 the buttons
  // step in halves, so a single wheel notch feels like a glide, not a clunk.
  zoomSnap: 0.25,
  zoomDelta: 0.5,
  // Wheel zoom feel: more pixels per level = less twitchy; shorter debounce
  // = the map starts moving the moment you scroll, not after a pause.
  wheelDebounceTime: 30,
  wheelPxPerZoomLevel: 90,
  // Bounce when you hit min/max zoom — gives the user feedback they've hit
  // the limit instead of silently doing nothing.
  bounceAtZoomLimits: true,
  // Zoom animation thresholds. Default is 4; raising it lets longer animated
  // zooms still play out smoothly instead of snapping.
  zoomAnimationThreshold: 6,
  // Inertia / kinetic panning — flick the map and it keeps gliding. Tuned
  // for a "professional cartography app" feel: more deceleration = less
  // overshoot, faster max so big flicks still travel.
  inertia: true,
  inertiaDeceleration: 3500,
  inertiaMaxSpeed: 4000,
  easeLinearity: 0.25,
  // Fade animation on tile load — masks the pop-in.
  fadeAnimation: true,
  // Do NOT animate marker DOM nodes through a zoom. Every aircraft/drone/pilot is
  // a divIcon (an SVG DOM element); tweening hundreds-to-thousands of them on each
  // zoom is THE biggest cause of janky zooming. With this off, Leaflet repositions
  // markers once at zoomend — they sit at their true lat/lon the entire time, only
  // the in-between tween is skipped — and the 100ms dead-reckoning ticker re-anchors
  // aircraft immediately after. Canvas shapes (circles/trails via preferCanvas) keep
  // zooming smoothly. Net: the gesture stays smooth no matter how many contacts are
  // on the map — which is the whole point.
  markerZoomAnimation: false,
  // Render circles/polylines on a single canvas instead of one SVG element
  // per shape. With hundreds of aircraft + trails this is night-and-day for
  // pan/zoom smoothness.
  preferCanvas: true,
});
L.control.zoom({ position: 'bottomleft' }).addTo(map);
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
// Rescale drone/pilot marker icons + circle radii to the new zoom. Each setIcon
// rebuilds a divIcon DOM node, so on a continuous wheel zoom (zoomSnap 0.25 emits
// several settles) this could rebuild every marker many times in a row. Debounced
// so it runs once, ~130ms after the zoom settles — invisible to the user, and it
// keeps the rebuild burst off the gesture entirely.
map.on('zoomend', debounce(function() {
  // Scale circle and ring radii based on current zoom
  const zoomLevel = map.getZoom();
  const size = Math.max(12, Math.min(zoomLevel * 1.5, 24));
  const circleRadius = size * 0.45;
  Object.keys(droneMarkers).forEach(mac => {
    const color = get_color_for_mac(mac);
    droneMarkers[mac].setIcon(createDroneIcon(color));
  });
  Object.keys(pilotMarkers).forEach(mac => {
    const color = get_color_for_mac(mac);
    pilotMarkers[mac].setIcon(createPilotIcon(color));
  });
  // Update circle marker sizes
  Object.values(droneCircles).forEach(circle => circle.setRadius(circleRadius));
  Object.values(pilotCircles).forEach(circle => circle.setRadius(circleRadius));
  // Update broadcast ring sizes
  Object.values(droneBroadcastRings).forEach(ring => ring.setRadius(size * 0.34));
  // Update observer icon size based on zoom level
  if (observerMarker) {
    const storedObserverEmoji = localStorage.getItem('observerEmoji') || "😎";
    observerMarker.setIcon(createObserverIcon('blue'));
  }
}, 130));

// ---------- Tiny UI utilities ----------
// Prevent double-submits on async actions: locks the button while the promise runs.
async function withButtonLock(btn, label, asyncFn) {
  if (!btn || btn.dataset.locked === '1') return;
  const prev = btn.textContent;
  btn.dataset.locked = '1';
  btn.disabled = true;
  btn.style.opacity = '0.5';
  if (label) btn.textContent = label;
  try { return await asyncFn(); }
  finally {
    btn.dataset.locked = '';
    btn.disabled = false;
    btn.style.opacity = '1';
    btn.textContent = prev;
  }
}

// Debounce — useful for search-as-you-type without hammering Nominatim
function debounce(fn, ms) {
  let t = null;
  return function(...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

// Offline layers registry: name -> L.tileLayer
const offlineLayers = {};
// Bounds metadata per offline layer: name -> [w, s, e, n] or null
const offlineLayerBounds = {};

function setBasemapStatus(mode, label) {
  // mode: 'online' | 'offline'
  // online -> lime green (bright); offline -> forest green (camo, deeper)
  const el = document.getElementById('basemapStatus');
  if (!el) return;
  const isOff = (mode === 'offline');
  const color = isOff ? '#228B22' : '#00ff00';
  el.style.color = color;
  el.style.borderColor = color;
  el.innerHTML = '<span style="display:inline-block; width:7px; height:7px; border-radius:50%; background:'
    + color + '; box-shadow:0 0 6px ' + color + '; vertical-align:middle; margin-right:5px;"></span>'
    + (label || (isOff ? 'OFFLINE' : 'ONLINE'));
}

function resolveLayer(value) {
  if (value && value.indexOf('offline:') === 0) {
    return offlineLayers[value.substring(8)] || null;
  }
  switch (value) {
    case 'osmStandard': return osmStandard;
    case 'osmHumanitarian': return osmHumanitarian;
    case 'cartoPositron': return cartoPositron;
    case 'cartoDarkMatter': return cartoDarkMatter;
    case 'esriWorldImagery': return esriWorldImagery;
    case 'esriWorldTopo': return esriWorldTopo;
    case 'esriDarkGray': return esriDarkGray;
    case 'openTopoMap': return openTopoMap;
  }
  return null;
}

function applyBasemap(value) {
  const newLayer = resolveLayer(value);
  if (!newLayer) return;
  map.eachLayer(function(layer) {
    if (layer.options && layer.options.attribution) { map.removeLayer(layer); }
  });
  newLayer.addTo(map);
  if (typeof newLayer.redraw === 'function') newLayer.redraw();
  const maxAllowed = newLayer.options && newLayer.options.maxZoom ? newLayer.options.maxZoom : 19;
  // Clamp UP only — never raise minZoom, otherwise switching to a layer with
  // a high minzoom (e.g. cached AOI at z10+) would lock the user out of zooming
  // out to find context. Tile gaps are fine; lockouts aren't.
  if (map.getZoom() > maxAllowed) map.setZoom(maxAllowed);
  map.options.maxZoom = maxAllowed;
  map.options.minZoom = 0;
  localStorage.setItem('basemap', value);
  // Field-usability fix: when switching to an OFFLINE layer, snap the view to
  // its cached bounds. Otherwise the user is parked over a region with no tiles
  // and just sees black. We only fitBounds if the current map center isn't
  // already inside the layer's bounds — preserves zoom when the user is already
  // working inside the cached area.
  if (value.indexOf('offline:') === 0) {
    const name = value.substring(8);
    const b = offlineLayerBounds[name];   // [w, s, e, n] or null
    if (b && b.length === 4) {
      const [w, s, e, n] = b;
      const c = map.getCenter();
      const inside = (c.lat >= s && c.lat <= n && c.lng >= w && c.lng <= e);
      if (!inside) {
        map.fitBounds([[s, w], [n, e]], { padding: [20, 20], maxZoom: maxAllowed });
      } else {
        // Already inside — clamp zoom DOWN if above the layer max (avoid black tiles).
        // We never clamp UP: the user must always be free to zoom out for context.
        if (map.getZoom() > maxAllowed) map.setZoom(maxAllowed);
      }
    }
  }
  setBasemapStatus(value.indexOf('offline:') === 0 ? 'offline' : 'online',
                   value.indexOf('offline:') === 0 ? ('OFFLINE · ' + value.substring(8)) : 'ONLINE');
}

document.getElementById("layerSelect").addEventListener("change", function() {
  applyBasemap(this.value);
  this.style.backgroundColor = "rgba(0,0,0,0.8)";
  this.style.color = "#FF00FF";
});

// ---------- Offline layer discovery & registration ----------
async function refreshOfflineLayers() {
  let data;
  try {
    const r = await fetch('/api/offline_layers');
    data = await r.json();
  } catch (e) { return; }
  const group = document.getElementById('offlineLayerGroup');
  const list = document.getElementById('cacheLayerList');
  // wipe & re-register
  group.innerHTML = '';
  if (list) list.innerHTML = '';
  for (const k of Object.keys(offlineLayers)) {
    try { map.removeLayer(offlineLayers[k]); } catch (e) {}
    delete offlineLayers[k];
  }
  if (!data.layers || data.layers.length === 0) {
    const opt = document.createElement('option');
    opt.disabled = true; opt.textContent = '(none cached)';
    group.appendChild(opt);
    return;
  }
  for (const lyr of data.layers) {
    let tl;
    if (lyr.kind === 'vector') {
      // MapLibre GL vector layer — needs the leaflet-maplibre-gl plugin (loaded in <head>)
      if (typeof L.maplibreGL !== 'function') {
        console.warn('vector layer requires MapLibre plugin; skipping ' + lyr.name);
        continue;
      }
      tl = L.maplibreGL({
        style: '/styles/' + lyr.name + '.json',
        attribution: lyr.attribution + ' (offline · vector)',
      });
      // L.maplibreGL doesn't expose maxZoom on options; fake it for our zoom-clamp logic
      tl.options = tl.options || {};
      tl.options.maxZoom = lyr.maxzoom;
      tl.options.minZoom = lyr.minzoom;
      tl.options.attribution = lyr.attribution + ' (offline · vector)';
    } else {
      const ext = (lyr.format === 'jpg' || lyr.format === 'jpeg') ? 'jpg'
                : (lyr.format === 'webp') ? 'webp' : 'png';
      tl = L.tileLayer('/tiles/' + lyr.name + '/{z}/{x}/{y}.' + ext, {
        attribution: lyr.attribution + ' (offline)',
        minZoom: lyr.minzoom,
        maxZoom: lyr.maxzoom,
        maxNativeZoom: lyr.maxzoom,
      });
    }
    offlineLayers[lyr.name] = tl;
    offlineLayerBounds[lyr.name] = lyr.bounds || null;
    const tag = lyr.kind === 'vector' ? '[V]' : '[R]';
    const opt = document.createElement('option');
    opt.value = 'offline:' + lyr.name;
    opt.textContent = '◉ ' + tag + ' ' + lyr.label + ' (' + (lyr.size_bytes / 1048576).toFixed(1) + ' MB)';
    group.appendChild(opt);
    if (list) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:2px 0; border-top:1px dashed #225522;';
      const tagColor = lyr.kind === 'vector' ? '#00ffff' : 'lime';
      row.innerHTML = '<span style="color:' + tagColor + ';">◉ ' + tag + ' ' + lyr.label
                    + ' · ' + lyr.tile_count + ' tiles · ' + (lyr.size_bytes/1048576).toFixed(1) + ' MB</span>'
                    + '<button data-name="' + lyr.name + '" class="cacheDelBtn" style="background:#330000; border:1px solid #ff4444; color:#ff8888; font-family:monospace; font-size:0.85em; padding:1px 6px; cursor:pointer;">DEL</button>';
      list.appendChild(row);
    }
  }
  document.querySelectorAll('.cacheDelBtn').forEach(b => {
    b.addEventListener('click', async (e) => {
      const n = e.target.getAttribute('data-name');
      if (!confirm('Delete cached layer "' + n + '"? This cannot be undone.')) return;
      await fetch('/api/offline_layers/' + encodeURIComponent(n), {method: 'DELETE'});
      // if currently selected, fall back to default
      if (document.getElementById('layerSelect').value === 'offline:' + n) {
        document.getElementById('layerSelect').value = 'esriWorldImagery';
        applyBasemap('esriWorldImagery');
      }
      refreshOfflineLayers();
    });
  });
}

// ---------- Cache This Area panel ----------
// Toggles the cachePanel and adds .flyout-open to the parent so the CSS rule
// horizontal-flies it out to the LEFT of the Map Layer panel (vertical fall-
// back on mobile). Closing reverts to the static layout.
document.getElementById('cacheToggle').addEventListener('click', (ev) => {
  ev.stopPropagation();
  const p = document.getElementById('cachePanel');
  const a = document.getElementById('cacheToggleArrow');
  const wrap = document.getElementById('offlineMappingPanel');
  const open = p.style.display === 'none';
  p.style.display = open ? 'block' : 'none';
  a.textContent = open ? '−' : '+';
  if (wrap) wrap.classList.toggle('flyout-open', open);
  if (open) { refreshOfflineLayers(); updateCacheEstimate(); }
});
// Click outside the flyout to close it (so the user isn't stuck with it open).
document.addEventListener('click', (e) => {
  const wrap = document.getElementById('offlineMappingPanel');
  if (!wrap || !wrap.classList.contains('flyout-open')) return;
  if (wrap.contains(e.target)) return;
  // Don't close when clicking the parent Map Layer header or its content
  const mapBox = document.getElementById('mapLayerFloatBox');
  if (mapBox && mapBox.contains(e.target)) return;
  wrap.classList.remove('flyout-open');
  const p = document.getElementById('cachePanel');
  const a = document.getElementById('cacheToggleArrow');
  if (p) p.style.display = 'none';
  if (a) a.textContent = '+';
});

// Active / Inactive drone list toggles — collapsible, state persisted across reloads
function _wireDroneListToggle(headerId, placeholderId, arrowId, storageKey) {
  const header = document.getElementById(headerId);
  const ph = document.getElementById(placeholderId);
  const arrow = document.getElementById(arrowId);
  if (!header || !ph || !arrow) return;
  const collapsed = localStorage.getItem(storageKey) === '1';
  if (collapsed) { ph.style.display = 'none'; arrow.textContent = '▶'; }
  header.addEventListener('click', () => {
    const isCollapsed = ph.style.display === 'none';
    ph.style.display = isCollapsed ? '' : 'none';
    arrow.textContent = isCollapsed ? '▼' : '▶';
    localStorage.setItem(storageKey, isCollapsed ? '0' : '1');
  });
}
_wireDroneListToggle('activeHeader',  'activePlaceholder',  'activeArrow',  'droneListActiveCollapsed');
_wireDroneListToggle('inactiveHeader','inactivePlaceholder','inactiveArrow','droneListInactiveCollapsed');

// Wire flight-path master toggles to DOM checkboxes — actual wiring lives at the bottom
// of the script (after all layer-group + master-state declarations are initialized).
// We just expose the function name here for clarity; the calls are deferred.
function _wirePathsToggle(checkboxId, kind) {
  const cb = document.getElementById(checkboxId);
  if (!cb) return;
  cb.checked = _pathsMasters[kind];
  cb.addEventListener('change', () => {
    _pathsMasters[kind] = cb.checked;
    localStorage.setItem('pathsShow' + kind.charAt(0).toUpperCase() + kind.slice(1), cb.checked ? '1' : '0');
    _applyPathsMaster(kind);
  });
  _applyPathsMaster(kind);  // initial enforcement
}
// (calls moved to a DOMContentLoaded-style deferred block at the very end of the script)

// ============================================================
// GEOFENCING — draw polygons/circles, persist, alert on transitions
// ============================================================
const geofenceLayer = L.layerGroup().addTo(map);
const geofenceShapes = {};   // id -> L.Polygon | L.Circle  (rendered shape)
const geofences = {};         // id -> fence dict (server canonical)

// Toast container — drops down from TOP CENTER.
(function ensureGeofenceToastDiv() {
  if (document.getElementById('geofenceToasts')) return;
  const d = document.createElement('div');
  d.id = 'geofenceToasts';
  d.style.cssText = 'position:fixed; top:0; left:50%; transform:translateX(-50%); z-index:10000; '
    + 'display:flex; flex-direction:column; align-items:center; gap:6px; max-width:360px; '
    + 'padding-top:10px; pointer-events:none;';
  document.body.appendChild(d);
  // Drop-in animation for each toast (slides down from above + fades in).
  const s = document.createElement('style');
  s.textContent = '@keyframes gfToastDrop { from { opacity:0; transform:translateY(-28px); } '
    + 'to { opacity:1; transform:translateY(0); } }';
  document.head.appendChild(s);
})();

function showGeofenceToast(payload) {
  const t = document.createElement('div');
  const accent = payload.fence_color || '#ff3333';
  t.style.cssText =
    'pointer-events:auto; padding:8px 10px; border:2px solid ' + accent + ';'
    + 'background:rgba(0,0,0,0.92); color:#ffaaaa; font-family:monospace; font-size:0.85em;'
    + 'border-radius:4px; box-shadow:0 0 12px ' + accent + ';'
    + 'animation: gfToastDrop 0.35s ease-out;';
  const dt = new Date((payload.ts || 0) * 1000);
  const tagColor = (DRONE_TAG_COLORS[payload.drone_tag] || '#888');
  const tagPill = '<span style="display:inline-block; padding:1px 5px; border:1px solid '
    + tagColor + '; color:' + tagColor + '; border-radius:3px; font-size:0.85em;">'
    + (payload.drone_tag || 'unknown').toUpperCase() + '</span>';
  t.innerHTML =
    '<div style="font-weight:bold;">'
    + '<span style="color:' + (payload.transition === 'enter' ? '#4ade80' : '#ff6b6b') + ';">'
    + (payload.transition === 'enter' ? '▶ ENTERED' : '◀ LEFT') + '</span>'
    + ' <span style="color:' + accent + ';">· ' + payload.fence_name + '</span></div>'
    + '<div style="margin-top:3px;">' + (payload.alias || payload.mac) + ' ' + tagPill + '</div>'
    + '<div style="font-size:0.85em; color:#888; margin-top:2px;">' + dt.toLocaleTimeString() + '</div>';
  document.getElementById('geofenceToasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity 0.5s'; }, 8000);
  setTimeout(() => t.remove(), 8500);
}

function renderGeofenceShape(fence) {
  // Remove any existing rendering of this fence
  if (geofenceShapes[fence.id]) {
    geofenceLayer.removeLayer(geofenceShapes[fence.id]);
    delete geofenceShapes[fence.id];
  }
  let shape;
  const color = fence.color || '#ff3333';
  const style = { color, weight: 2, opacity: 0.85, fillColor: color, fillOpacity: 0.12 };
  if (fence.type === 'polygon') {
    const pts = (fence.geometry && fence.geometry.points) || [];
    shape = L.polygon(pts, style);
  } else if (fence.type === 'circle') {
    const c = fence.geometry && fence.geometry.center;
    const r = fence.geometry && fence.geometry.radius_m;
    if (!c || !r) return;
    shape = L.circle(c, { radius: r, ...style });
  } else { return; }
  shape.bindPopup(geofencePopup(fence));
  shape.addTo(geofenceLayer);
  geofenceShapes[fence.id] = shape;
}

// Human-readable description of what a fence actually watches — honors
// target_kind (drone/aircraft/both) AND both tag filters, so a 'both' fence no
// longer mislabels itself as "all drones".
function _gfWatchLabel(f) {
  const tk = f.target_kind || 'drone';
  const dt = (f.alert_tags && f.alert_tags.length) ? f.alert_tags.join(', ') : 'all';
  const at = (f.aircraft_tags && f.aircraft_tags.length) ? f.aircraft_tags.join(', ') : 'all';
  if (tk === 'aircraft') return 'aircraft (' + at + ')';
  if (tk === 'both')     return 'drones (' + dt + ') + aircraft (' + at + ')';
  return 'drones (' + dt + ')';
}

function geofencePopup(f) {
  const watch = _gfWatchLabel(f);
  return '<div style="font-family:monospace; color:#ffaaaa; min-width:200px;">'
    + '<div style="color:' + (f.color || '#ff3333') + '; font-weight:bold;">' + f.name + '</div>'
    + '<div style="font-size:0.85em; color:#aaa; margin-top:2px;">' + f.type.toUpperCase() + ' · ' + (f.enabled ? 'enabled' : 'disabled') + '</div>'
    + '<div style="margin-top:4px;">enter alert: ' + (f.alert_on_enter ? 'on' : 'off')
    + ' · exit alert: ' + (f.alert_on_exit ? 'on' : 'off') + '</div>'
    + '<div>watching: <span style="color:#ffcc88;">' + watch + '</span></div>'
    + '<div style="display:flex; gap:4px; margin-top:6px;">'
    + '<button onclick="event.stopPropagation(); editGeofence(\\'' + f.id + '\\')" style="flex:1; padding:3px; background:#001a2a; border:1px solid #00aaff; color:#aaeeff; font-family:monospace; cursor:pointer;">EDIT</button>'
    + '<button onclick="event.stopPropagation(); toggleGeofenceEnabled(\\'' + f.id + '\\')" style="flex:1; padding:3px; background:#1a1a00; border:1px solid #ffaa00; color:#ffcc66; font-family:monospace; cursor:pointer;">' + (f.enabled ? 'DISABLE' : 'ENABLE') + '</button>'
    + '<button onclick="event.stopPropagation(); deleteGeofence(\\'' + f.id + '\\')" style="flex:1; padding:3px; background:#330000; border:1px solid #ff5555; color:#ff8888; font-family:monospace; cursor:pointer;">DEL</button>'
    + '</div></div>';
}
window.editGeofence = editGeofence;
window.toggleGeofenceEnabled = toggleGeofenceEnabled;
window.deleteGeofence = deleteGeofence;

async function refreshGeofences() {
  try {
    const r = await fetch('/api/geofences');
    const d = await r.json();
    // Clear stale
    Object.keys(geofences).forEach(id => {
      if (!d.fences.find(f => f.id === id)) {
        if (geofenceShapes[id]) { geofenceLayer.removeLayer(geofenceShapes[id]); delete geofenceShapes[id]; }
        delete geofences[id];
      }
    });
    (d.fences || []).forEach(f => {
      geofences[f.id] = f;
      renderGeofenceShape(f);
    });
    renderGeofenceList();
  } catch (e) { console.debug('refreshGeofences failed:', e); }
}

function renderGeofenceList() {
  const c = document.getElementById('geofenceList');
  if (!c) return;
  c.innerHTML = '';
  const ids = Object.keys(geofences);
  if (ids.length === 0) {
    c.innerHTML = '<div style="color:#aa6666; font-style:italic; text-align:center; padding:4px;">no fences yet</div>';
    return;
  }
  ids.forEach(id => {
    const f = geofences[id];
    const watch = _gfWatchLabel(f);
    const row = document.createElement('div');
    row.style.cssText =
      'margin-top:3px; border-left:4px solid ' + (f.enabled ? f.color : '#444')
      + '; background:rgba(0,0,0,0.5); opacity:' + (f.enabled ? '1' : '0.55') + ';';
    // Header row — click to fly-to + toggle expansion
    const head = document.createElement('div');
    head.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:4px 6px; cursor:pointer; gap:6px;';
    head.innerHTML =
      '<span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:' + f.color + '; flex:0 0 auto;"></span>'
      + '<span style="color:' + f.color + '; font-weight:bold; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' + f.name + '</span>'
      + '<span style="color:#888; font-size:0.85em;">' + f.type + '</span>'
      + '<span class="gfRowToggle" style="color:#aa8888; font-size:0.85em; padding-left:4px;">▸</span>';
    // Detail block — hidden by default, shows tag/alert info + actions
    const detail = document.createElement('div');
    detail.style.cssText = 'display:none; padding:4px 8px 6px 8px; font-size:0.85em; color:#ddd; border-top:1px dashed #553333;';
    detail.innerHTML =
      '<div>watching: <span style="color:#ffcc88;">' + watch + '</span></div>'
      + '<div>alerts: ' + (f.alert_on_enter ? '<span style="color:#88ff88;">enter</span>' : 'enter:off')
      + ' · ' + (f.alert_on_exit ? '<span style="color:#88ff88;">exit</span>' : 'exit:off') + '</div>'
      + '<div style="display:flex; gap:4px; margin-top:4px;">'
      + '  <button data-action="fly" style="flex:1; padding:3px; background:#001a2a; border:1px solid #00aaff; color:#aaeeff; font-family:monospace; font-size:0.85em; cursor:pointer;">FLY TO</button>'
      + '  <button data-action="edit" style="flex:1; padding:3px; background:#001a2a; border:1px solid #00aaff; color:#aaeeff; font-family:monospace; font-size:0.85em; cursor:pointer;">EDIT</button>'
      + '  <button data-action="toggle" style="flex:1; padding:3px; background:#1a1a00; border:1px solid #ffaa00; color:#ffcc66; font-family:monospace; font-size:0.85em; cursor:pointer;">' + (f.enabled ? 'DISABLE' : 'ENABLE') + '</button>'
      + '  <button data-action="del" style="flex:0 0 auto; padding:3px 6px; background:#330000; border:1px solid #ff5555; color:#ff8888; font-family:monospace; font-size:0.85em; cursor:pointer;">DEL</button>'
      + '</div>';
    head.addEventListener('click', () => {
      const open = detail.style.display !== 'none';
      detail.style.display = open ? 'none' : 'block';
      head.querySelector('.gfRowToggle').textContent = open ? '▸' : '▾';
    });
    detail.querySelector('[data-action="fly"]').addEventListener('click', (e) => {
      e.stopPropagation();
      const shape = geofenceShapes[id];
      if (!shape) return;
      if (shape.getBounds) map.fitBounds(shape.getBounds(), { padding: [40, 40] });
      else if (shape.getLatLng) map.setView(shape.getLatLng(), 14);
      shape.openPopup();
    });
    detail.querySelector('[data-action="edit"]').addEventListener('click', (e) => {
      e.stopPropagation(); editGeofence(id);
    });
    detail.querySelector('[data-action="toggle"]').addEventListener('click', (e) => {
      e.stopPropagation(); toggleGeofenceEnabled(id);
    });
    detail.querySelector('[data-action="del"]').addEventListener('click', (e) => {
      e.stopPropagation(); deleteGeofence(id);
    });
    row.appendChild(head);
    row.appendChild(detail);
    c.appendChild(row);
  });
}

// Draw mode: opens a Leaflet.Draw handler, then on completion swaps the layer
// for the inline create form. Rectangle is just a 4-point polygon to the API.
const FENCE_PALETTE = ['#ff3333','#ff8800','#ffcc00','#88ff88','#33aaff','#cc66ff','#ff66cc','#aaffff'];
let _pendingDrawing = null;   // {kind, layer, geometry}
let _gfEditId = null;         // when set, the fence form is editing this fence (PUT), not creating (POST)

// Custom CENTER-OUT circle draw: click to drop the center, move the mouse and the
// circle grows from that center, click again to lock the radius. (Leaflet.draw's
// built-in circle is press-and-drag, which the user didn't want.) Esc cancels.
let _circleDrawCleanup = null;
function _startCircleDraw() {
  if (_circleDrawCleanup) _circleDrawCleanup();   // cancel any in-progress placement
  document.getElementById('geofenceCreateForm').style.display = 'none';
  const style = { color: '#ff3333', weight: 2, fillColor: '#ff3333', fillOpacity: 0.10 };
  let center = null, preview = null;
  map.getContainer().style.cursor = 'crosshair';
  const radiusTo = (ll) => Math.max(1, map.distance(center, ll));
  const onMove = (ev) => {
    if (!center || !preview) return;
    preview.setRadius(radiusTo(ev.latlng));
  };
  const onClick = (ev) => {
    if (!center) {
      // first click: drop center, start a zero-radius preview that follows the mouse
      center = ev.latlng;
      preview = L.circle(center, { radius: 1, ...style }).addTo(map);
    } else {
      // second click: lock the radius and hand off to the create form
      const r = radiusTo(ev.latlng);
      preview.setRadius(r);
      const layer = preview;
      cleanup();
      _pendingDrawing = { kind: 'circle', layer: layer, type: 'circle',
                          geometry: { center: [center.lat, center.lng], radius_m: r } };
      _showFenceCreateForm();
    }
  };
  const onKey = (e) => { if (e.key === 'Escape') { if (preview) map.removeLayer(preview); cleanup(); } };
  function cleanup() {
    map.off('mousemove', onMove);
    map.off('click', onClick);
    document.removeEventListener('keydown', onKey);
    map.getContainer().style.cursor = '';
    _circleDrawCleanup = null;
  }
  _circleDrawCleanup = () => { if (preview && !center) map.removeLayer(preview); cleanup(); };
  map.on('mousemove', onMove);
  map.on('click', onClick);
  document.addEventListener('keydown', onKey);
}

function _startDraw(kind) {
  // Hide any open form first
  document.getElementById('geofenceCreateForm').style.display = 'none';
  // Circle uses the custom center-out, two-click flow above (click center, move to
  // size, click to lock) instead of Leaflet.draw's press-and-drag.
  if (kind === 'circle') { _startCircleDraw(); return; }
  const opts = { showArea: false, shapeOptions: { color: '#ff3333', weight: 2 } };
  let handler;
  if (kind === 'polygon')   handler = new L.Draw.Polygon(map, opts);
  else if (kind === 'rectangle') handler = new L.Draw.Rectangle(map, opts);
  else return;
  handler.enable();
  const onDone = (e) => {
    map.off(L.Draw.Event.CREATED, onDone);
    let geometry, type;
    if (kind === 'polygon' || kind === 'rectangle') {
      type = 'polygon';
      geometry = { points: e.layer.getLatLngs()[0].map(p => [p.lat, p.lng]) };
    } else {
      type = 'circle';
      const c = e.layer.getLatLng();
      geometry = { center: [c.lat, c.lng], radius_m: e.layer.getRadius() };
    }
    _pendingDrawing = { kind, layer: e.layer, type, geometry };
    // Add the temporary preview to the map so the user sees what they drew
    e.layer.addTo(map);
    _showFenceCreateForm();
  };
  map.on(L.Draw.Event.CREATED, onDone);
}

function _showFenceCreateForm() {
  _gfEditId = null;   // drawing a new fence = create mode (overridden by editGeofence)
  document.getElementById('gfFormName').value = '';
  document.getElementById('gfFormColor').value = '#ff5577';
  document.getElementById('gfFormEnter').checked = true;
  document.getElementById('gfFormExit').checked = true;
  // Color palette swatches
  const sw = document.getElementById('gfFormColorSwatches');
  sw.innerHTML = '';
  FENCE_PALETTE.forEach(c => {
    const s = document.createElement('span');
    s.style.cssText = 'width:18px; height:18px; border-radius:50%; background:' + c + '; cursor:pointer; border:2px solid transparent;';
    s.title = c;
    s.addEventListener('click', () => {
      document.getElementById('gfFormColor').value = c;
      sw.querySelectorAll('span').forEach(el => el.style.border = '2px solid transparent');
      s.style.border = '2px solid #fff';
      // Live update preview color on the temp layer
      if (_pendingDrawing && _pendingDrawing.layer) {
        try { _pendingDrawing.layer.setStyle({ color: c, fillColor: c }); } catch(e){}
      }
    });
    sw.appendChild(s);
  });
  // Tag chips for alert filter
  const tagsBox = document.getElementById('gfFormTagChips');
  tagsBox.innerHTML = '';
  DRONE_TAG_VALUES.forEach(t => {
    const meta = {label: t.toUpperCase(), color: DRONE_TAG_COLORS[t] || '#888'};
    const chip = document.createElement('span');
    chip.dataset.tag = t;
    chip.dataset.on = '0';
    chip.style.cssText = 'padding:2px 6px; cursor:pointer; user-select:none; border:1px solid '
      + meta.color + '; color:' + meta.color
      + '; background:transparent; font-size:0.85em; letter-spacing:0.5px; border-radius:3px;';
    chip.textContent = meta.label;
    chip.addEventListener('click', () => {
      const on = chip.dataset.on === '1';
      chip.dataset.on = on ? '0' : '1';
      chip.style.background = on ? 'transparent' : meta.color;
      chip.style.color = on ? meta.color : '#000';
    });
    tagsBox.appendChild(chip);
  });
  // Aircraft tag chips (shown when target = aircraft or both)
  const acBox = document.getElementById('gfFormAircraftChips');
  acBox.innerHTML = '';
  // ADSB_TAGS is defined elsewhere; fall back to a sensible static list if missing
  const _aircraftTags = (typeof ADSB_TAGS !== 'undefined' && ADSB_TAGS.map)
    ? ADSB_TAGS.map(t => ({id: t.id, label: t.label, color: t.color}))
    : [{id:'military',label:'MIL',color:'#ff8800'},{id:'government',label:'GOV',color:'#ffcc00'},
       {id:'police',label:'LEO',color:'#33aaff'},{id:'rotorcraft',label:'ROTOR',color:'#cc66ff'},
       {id:'commercial',label:'COMM',color:'#88ff88'},{id:'private',label:'PVT',color:'#aaa'},
       {id:'emergency',label:'EMERGENCY',color:'#ff3333'},{id:'hijack',label:'HIJACK',color:'#ff66ff'}];
  _aircraftTags.forEach(t => {
    const chip = document.createElement('span');
    chip.dataset.tag = t.id;
    chip.dataset.on = '0';
    chip.style.cssText = 'padding:2px 6px; cursor:pointer; user-select:none; border:1px solid '
      + t.color + '; color:' + t.color
      + '; background:transparent; font-size:0.85em; letter-spacing:0.5px; border-radius:3px;';
    chip.textContent = t.label;
    chip.addEventListener('click', () => {
      const on = chip.dataset.on === '1';
      chip.dataset.on = on ? '0' : '1';
      chip.style.background = on ? 'transparent' : t.color;
      chip.style.color = on ? t.color : '#000';
    });
    acBox.appendChild(chip);
  });
  // Reset target kind + webhook
  document.getElementById('gfFormTargetDrone').checked = true;
  document.getElementById('gfFormDroneTagsRow').style.display = 'block';
  document.getElementById('gfFormAircraftTagsRow').style.display = 'none';
  document.getElementById('gfFormWebhook').value = '';
  // Show/hide tag rows when target kind changes
  document.querySelectorAll('input[name="gfFormTarget"]').forEach(r => {
    r.onchange = () => {
      const v = document.querySelector('input[name="gfFormTarget"]:checked').value;
      document.getElementById('gfFormDroneTagsRow').style.display = (v === 'aircraft') ? 'none' : 'block';
      document.getElementById('gfFormAircraftTagsRow').style.display = (v === 'drone') ? 'none' : 'block';
    };
  });
  document.getElementById('geofenceCreateForm').style.display = 'block';
  document.getElementById('gfFormName').focus();
}

document.getElementById('gfFormCancel').addEventListener('click', () => {
  if (_pendingDrawing && _pendingDrawing.layer) {
    try { map.removeLayer(_pendingDrawing.layer); } catch(e){}
  }
  _pendingDrawing = null;
  _gfEditId = null;
  document.getElementById('geofenceCreateForm').style.display = 'none';
});

document.getElementById('gfFormSave').addEventListener('click', async () => {
  const editing = _gfEditId;            // editing an existing fence vs creating a new one
  if (!editing && !_pendingDrawing) return;
  const name = document.getElementById('gfFormName').value.trim();
  if (!name) { alert('Name required'); return; }
  const color = document.getElementById('gfFormColor').value || '#ff5577';
  const alert_on_enter = document.getElementById('gfFormEnter').checked;
  const alert_on_exit = document.getElementById('gfFormExit').checked;
  const alert_tags = [...document.getElementById('gfFormTagChips').querySelectorAll('span')]
    .filter(c => c.dataset.on === '1')
    .map(c => c.dataset.tag);
  const aircraft_tags = [...document.getElementById('gfFormAircraftChips').querySelectorAll('span')]
    .filter(c => c.dataset.on === '1')
    .map(c => c.dataset.tag);
  const target_kind = (document.querySelector('input[name="gfFormTarget"]:checked')?.value) || 'drone';
  const webhook_url = document.getElementById('gfFormWebhook').value.trim();
  if (webhook_url && !/^https?:\\/\\//.test(webhook_url)) {
    alert('Webhook URL must start with http:// or https://');
    return;
  }
  const body = {
    name, color, alert_on_enter, alert_on_exit,
    alert_tags, aircraft_tags, target_kind, webhook_url,
  };
  // Create POSTs with the freshly-drawn geometry; edit PUTs the fields and keeps
  // the existing geometry (we don't re-draw on edit).
  if (!editing) {
    body.type = _pendingDrawing.type;
    body.geometry = _pendingDrawing.geometry;
  }
  const url = editing ? ('/api/geofences/' + encodeURIComponent(editing)) : '/api/geofences';
  const method = editing ? 'PUT' : 'POST';
  let r, j;
  try {
    r = await fetch(url, {
      method, headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    j = await r.json();
  } catch (e) { alert('failed: ' + e); return; }
  if (!r.ok) { alert(j.error || 'failed'); return; }
  // Clean up temp drawing layer if creating (the canonical fence is drawn by refreshGeofences)
  if (_pendingDrawing) { try { map.removeLayer(_pendingDrawing.layer); } catch(e){} _pendingDrawing = null; }
  _gfEditId = null;
  document.getElementById('geofenceCreateForm').style.display = 'none';
  refreshGeofences();
});

async function deleteGeofence(id) {
  const f = geofences[id];
  if (!f) return;
  if (!confirm('Delete fence "' + f.name + '"? This cannot be undone.')) return;
  try {
    await fetch('/api/geofences/' + encodeURIComponent(id), { method: 'DELETE' });
  } catch (e) { alert('failed: ' + e); return; }
  refreshGeofences();
}

async function toggleGeofenceEnabled(id) {
  const f = geofences[id];
  if (!f) return;
  try {
    await fetch('/api/geofences/' + encodeURIComponent(id), {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ enabled: !f.enabled }),
    });
  } catch (e) { alert('failed: ' + e); return; }
  refreshGeofences();
}

// Edit opens the SAME full form as create, pre-filled — so you can change the
// target (drone/aircraft/both), tags, enter/exit, color, and webhook, not just
// the name. Geometry is left as-is (we don't re-draw on edit). Saving PUTs.
function editGeofence(id) {
  const f = geofences[id];
  if (!f) return;
  _showFenceCreateForm();     // builds chips + resets to defaults (sets _gfEditId=null)
  _gfEditId = id;             // ...then switch to edit mode
  document.getElementById('gfFormName').value = f.name || '';
  if (f.color) document.getElementById('gfFormColor').value = f.color;
  document.getElementById('gfFormEnter').checked = f.alert_on_enter !== false;
  document.getElementById('gfFormExit').checked = f.alert_on_exit !== false;
  document.getElementById('gfFormWebhook').value = f.webhook_url || '';
  const tk = f.target_kind || 'drone';
  const radio = document.querySelector('input[name="gfFormTarget"][value="' + tk + '"]');
  if (radio) radio.checked = true;
  document.getElementById('gfFormDroneTagsRow').style.display = (tk === 'aircraft') ? 'none' : 'block';
  document.getElementById('gfFormAircraftTagsRow').style.display = (tk === 'drone') ? 'none' : 'block';
  // Light up the saved tag chips by reusing each chip's own click handler (so
  // the on-styling matches exactly).
  const onDrone = new Set(f.alert_tags || []);
  document.getElementById('gfFormTagChips').querySelectorAll('span').forEach(c => {
    if (onDrone.has(c.dataset.tag) && c.dataset.on !== '1') c.click();
  });
  const onAc = new Set(f.aircraft_tags || []);
  document.getElementById('gfFormAircraftChips').querySelectorAll('span').forEach(c => {
    if (onAc.has(c.dataset.tag) && c.dataset.on !== '1') c.click();
  });
  document.getElementById('gfFormName').focus();
}

// Wire panel toggle, draw buttons, socket alerts, alert log
document.getElementById('geofenceToggle').addEventListener('click', () => {
  const p = document.getElementById('geofencePanel');
  const a = document.getElementById('geofenceToggleArrow');
  const open = p.style.display === 'none';
  p.style.display = open ? 'block' : 'none';
  a.textContent = open ? '−' : '+';
  if (open) { refreshGeofences(); refreshGeofenceAlerts(); }
});
document.getElementById('drawPolygonBtn').addEventListener('click', () => _startDraw('polygon'));
document.getElementById('drawCircleBtn').addEventListener('click',  () => _startDraw('circle'));
const rectBtn = document.getElementById('drawRectangleBtn');
if (rectBtn) rectBtn.addEventListener('click', () => _startDraw('rectangle'));
// Collapsible "Recent alerts" — expands/collapses the alert history list.
const gfAlertHeader = document.getElementById('geofenceAlertHeader');
if (gfAlertHeader) gfAlertHeader.addEventListener('click', () => {
  const list = document.getElementById('geofenceAlertList');
  const arr = document.getElementById('geofenceAlertArrow');
  if (!list) return;
  const open = list.style.display === 'none';
  list.style.display = open ? 'block' : 'none';
  if (arr) arr.textContent = open ? '−' : '+';
});

socket.on('geofences', (msg) => {
  if (msg && msg.fences) {
    Object.keys(geofences).forEach(id => delete geofences[id]);
    Object.keys(geofenceShapes).forEach(id => {
      geofenceLayer.removeLayer(geofenceShapes[id]);
      delete geofenceShapes[id];
    });
    msg.fences.forEach(f => { geofences[f.id] = f; renderGeofenceShape(f); });
    renderGeofenceList();
  }
});

async function refreshGeofenceAlerts() {
  try {
    const r = await fetch('/api/geofence_alerts?limit=200');
    const d = await r.json();
    renderGeofenceAlertList(d.alerts || []);
  } catch (e) {}
}

function renderGeofenceAlertList(alerts) {
  const c = document.getElementById('geofenceAlertList');
  if (!c) return;
  const cnt = document.getElementById('geofenceAlertCount');
  if (cnt) cnt.textContent = alerts.length ? '(' + alerts.length + ')' : '';
  if (!alerts.length) { c.innerHTML = '— none —'; return; }
  c.innerHTML = '';
  alerts.slice().reverse().forEach(p => {
    const dt = new Date((p.ts || 0) * 1000);
    const tagColor = DRONE_TAG_COLORS[p.drone_tag] || '#888';
    const div = document.createElement('div');
    div.style.cssText = 'padding:3px; margin-top:2px; border-left:2px solid ' + (p.fence_color || '#ff3333') + '; padding-left:5px; background:rgba(40,0,0,0.3);';
    div.innerHTML =
      '<div style="font-weight:bold;">'
      + '<span style="color:' + (p.transition === 'enter' ? '#4ade80' : '#ff6b6b') + ';">'
      + (p.transition === 'enter' ? '▶ ENTER' : '◀ EXIT') + '</span>'
      + ' <span style="color:' + (p.fence_color || '#ff3333') + ';">· ' + p.fence_name + '</span></div>'
      + '<div style="color:#ccc;">' + (p.alias || p.mac) + ' '
      + '<span style="color:' + tagColor + ';">[' + (p.drone_tag || 'unknown') + ']</span></div>'
      + '<div style="color:#666; font-size:0.85em;">' + dt.toLocaleTimeString() + '</div>';
    c.appendChild(div);
  });
}

// Reliable alert delivery via POLLING. Geofence alerts are emitted with
// socketio.emit() from the ADS-B poller's BACKGROUND thread, which Flask-SocketIO
// does not deliver reliably in this setup (the same reason the ADS-B feed itself
// is client-polled). With only the socket handler, toasts never fired. So poll
// the server's alert ring buffer and fire a toast for anything new — the poll,
// not the socket, is the source of truth.
let _gfAlertWatermark = 0;
let _gfAlertsPrimed = false;
let _gfPollInflight = false;
async function _pollGeofenceAlerts() {
  if (_gfPollInflight) return;            // single-flight so socket+timer can't double-toast
  _gfPollInflight = true;
  try {
    const r = await fetch('/api/geofence_alerts?limit=200');
    const d = await r.json();
    const alerts = d.alerts || [];
    if (!_gfAlertsPrimed) {
      // First poll after load: set the watermark to the newest existing alert so
      // we don't replay the whole backlog as toasts (the list still shows them).
      _gfAlertsPrimed = true;
      _gfAlertWatermark = alerts.reduce((m, a) => Math.max(m, a.ts || 0), 0);
    } else {
      const fresh = alerts.filter(a => (a.ts || 0) > _gfAlertWatermark)
                          .sort((a, b) => (a.ts || 0) - (b.ts || 0));
      fresh.forEach(showGeofenceToast);
      if (fresh.length) _gfAlertWatermark = fresh.reduce((m, a) => Math.max(m, a.ts || 0), _gfAlertWatermark);
    }
    // Always render. The alert list was relocated into the bottom-left float, which
    // left #geofencePanel an empty, permanently display:none leftover — so gating on
    // its visibility meant the poll NEVER repopulated the list and recent alerts
    // never showed. Rendering into a (possibly collapsed) container is cheap and
    // keeps it current the instant the user expands the panel.
    renderGeofenceAlertList(alerts);
  } catch (e) {
  } finally {
    _gfPollInflight = false;
  }
}
setInterval(_pollGeofenceAlerts, 2000);
_pollGeofenceAlerts();   // prime the watermark + initial list

// Socket path still helps when Flask-SocketIO does deliver it: route it through
// the same poll so a real-time hit shows instantly, deduped by the watermark.
socket.on('geofence_alert', () => { _pollGeofenceAlerts(); });

// Initial fence load (so they show on map even if user never opens the panel)
refreshGeofences();

// Deferred wiring — runs after the entire script has finished evaluating, so all
// `const` declarations (path layers, _pathsMasters, etc.) have fully initialized.
// This dodges TDZ errors when the wiring needs values declared further down.
setTimeout(() => {
  try {
    _wirePathsToggle('pathsDroneToggle',    'drone');
    _wirePathsToggle('pathsPilotToggle',    'pilot');
    _wirePathsToggle('pathsAircraftToggle', 'aircraft');
    // (Removed: adsbBoxPathsToggle mirroring — that AIR TRAFFIC-panel master
    // toggle no longer exists. Flight paths are strictly per-aircraft now.)
    // ALL master: flips every per-kind toggle in lock-step. Reads as ON only
    // when all three are on, OFF when any is off — so it both summarizes and
    // commands the state.
    const allCb = document.getElementById('pathsAllToggle');
    const droneCb = document.getElementById('pathsDroneToggle');
    const pilotCb = document.getElementById('pathsPilotToggle');
    const aircraftCb = document.getElementById('pathsAircraftToggle');
    function _syncAllPathsToggle() {
      if (!allCb) return;
      allCb.checked = !!(droneCb?.checked && pilotCb?.checked && aircraftCb?.checked);
    }
    if (allCb) {
      _syncAllPathsToggle();
      allCb.addEventListener('change', () => {
        const want = allCb.checked;
        [droneCb, pilotCb, aircraftCb].forEach(cb => {
          if (cb && cb.checked !== want) {
            cb.checked = want;
            cb.dispatchEvent(new Event('change'));
          }
        });
      });
      [droneCb, pilotCb, aircraftCb].forEach(cb => {
        cb && cb.addEventListener('change', _syncAllPathsToggle);
      });
    }
  } catch (e) { console.warn('paths wiring failed:', e); }
}, 0);

// Settings & Exports panel toggle (also lazy-loads port list when opened)
document.getElementById('settingsToggle').addEventListener('click', () => {
  const p = document.getElementById('settingsPanel');
  const a = document.getElementById('settingsToggleArrow');
  const open = p.style.display === 'none';
  p.style.display = open ? 'block' : 'none';
  a.textContent = open ? '−' : '+';
  if (open) { refreshPorts(); loadWebhooks(); }
});

// ---------- Webhooks (detection + geofence) ----------
async function loadWebhooks() {
  try {
    const r = await fetch('/api/get_webhook_url');
    const d = await r.json();
    const main = document.getElementById('webhookUrlMain');
    const geo  = document.getElementById('webhookUrlGeofence');
    const status = document.getElementById('webhooksStatus');
    if (main) main.value = d.webhook_url || '';
    if (geo)  geo.value  = d.geofence_webhook_url || '';
    if (status) {
      const m = !!d.webhook_url, g = !!d.geofence_webhook_url;
      if (g && m)      { status.textContent = 'detection + geofence'; status.style.color = '#88ff88'; }
      else if (m)      { status.textContent = 'detection only';       status.style.color = '#88ff88'; }
      else if (g)      { status.textContent = 'geofence only';        status.style.color = '#88ff88'; }
      else             { status.textContent = 'not set';              status.style.color = '#996666'; }
    }
  } catch (e) { console.warn('loadWebhooks failed:', e); }
}
// Collapsible header
document.getElementById('webhooksToggle')?.addEventListener('click', () => {
  const p = document.getElementById('webhooksPanel');
  const a = document.getElementById('webhooksArrow');
  const open = p.style.display === 'none';
  p.style.display = open ? 'block' : 'none';
  if (a) a.innerHTML = open ? '&#9662;' : '&#9656;';
  if (open) loadWebhooks();
});
// SAVE — persists both URLs in one click. Empty string clears.
document.getElementById('webhookSaveBtn')?.addEventListener('click', async () => {
  const result = document.getElementById('webhookSaveResult');
  const main = (document.getElementById('webhookUrlMain')?.value || '').trim();
  const geo  = (document.getElementById('webhookUrlGeofence')?.value || '').trim();
  const validate = (u) => !u || u.startsWith('http://') || u.startsWith('https://');
  if (!validate(main) || !validate(geo)) {
    if (result) { result.textContent = 'URLs must start with http:// or https://'; result.style.color = '#ff8888'; }
    return;
  }
  try {
    const r1 = await fetch('/api/set_webhook_url', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ webhook_url: main })
    });
    const j1 = await r1.json();
    if (j1.status !== 'ok') throw new Error(j1.message || 'detection save failed');
    const r2 = await fetch('/api/set_geofence_webhook_url', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ geofence_webhook_url: geo })
    });
    const j2 = await r2.json();
    if (j2.status !== 'ok') throw new Error(j2.message || 'geofence save failed');
    if (result) { result.textContent = 'saved'; result.style.color = '#88ff88'; }
    loadWebhooks();
    setTimeout(() => { if (result) result.textContent = ''; }, 2000);
  } catch (e) {
    if (result) { result.textContent = 'save failed: ' + e.message; result.style.color = '#ff8888'; }
  }
});
// TEST — send a sample geofence payload through the webhook pipeline.
document.getElementById('webhookTestBtn')?.addEventListener('click', async () => {
  const result = document.getElementById('webhookSaveResult');
  const geo  = (document.getElementById('webhookUrlGeofence')?.value || '').trim();
  const main = (document.getElementById('webhookUrlMain')?.value || '').trim();
  const target = geo || main;
  if (!target) {
    if (result) { result.textContent = 'no URL set'; result.style.color = '#ffaa44'; }
    return;
  }
  if (result) { result.textContent = 'POSTing test payload...'; result.style.color = '#aaccff'; }
  try {
    const r = await fetch('/api/webhook_popup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        webhook_url: target,
        payload: {
          event: 'geofence',
          ts: Date.now()/1000,
          fence_name: 'TEST FENCE',
          transition: 'enter',
          mac: 'aa:bb:cc:dd:ee:ff',
          drone_tag: 'civilian',
          lat: 0, lon: 0,
          note: 'mesh-mapper webhook test',
        }
      })
    });
    const j = await r.json();
    if (j.status === 'ok') {
      if (result) { result.textContent = 'test delivered (HTTP ' + (j.response || '?') + ')'; result.style.color = '#88ff88'; }
    } else {
      if (result) { result.textContent = 'test failed: ' + (j.message || 'unknown'); result.style.color = '#ff8888'; }
    }
  } catch (e) {
    if (result) { result.textContent = 'test failed: ' + e.message; result.style.color = '#ff8888'; }
  }
});
// Initial fetch so the badge in the SETTINGS header reflects state even when
// the panel hasn't been opened yet.
loadWebhooks();

// ---------- Inline USB port configuration ----------
async function refreshPorts() {
  let avail = [], selectedMap = {};
  try {
    const d = await (await fetch('/api/ports')).json();
    avail = (d.ports || []).map(p => p.device).filter(Boolean);
  } catch (e) { console.debug('port list fetch failed:', e); }
  try {
    const d = await (await fetch('/api/selected_ports')).json();
    selectedMap = d.selected_ports || {};
  } catch (e) {}
  const sels = document.querySelectorAll('.portSel');
  sels.forEach(sel => {
    const idx = sel.getAttribute('data-idx');
    const cur = selectedMap['port' + idx] || '';
    sel.innerHTML = '<option value="">(none)</option>'
      + avail.map(p => `<option value="${p}"${p === cur ? ' selected' : ''}>${p}</option>`).join('');
    if (cur && !avail.includes(cur)) {
      // Selected port no longer present (unplugged) — show it greyed but still selected
      const opt = document.createElement('option');
      opt.value = cur;
      opt.textContent = cur + ' (unplugged)';
      opt.selected = true;
      opt.style.color = '#888';
      sel.appendChild(opt);
    }
  });
}
document.getElementById('portRefreshBtn').addEventListener('click', (ev) =>
  withButtonLock(ev.currentTarget, '...', refreshPorts));
document.getElementById('portApplyBtn').addEventListener('click', async (ev) => {
  await withButtonLock(ev.currentTarget, 'APPLYING...', async () => {
    const status = document.getElementById('portStatus');
    const ports = [...document.querySelectorAll('.portSel')]
      .map(s => s.value).filter(v => v);  // skip empty
    let r, j;
    try {
      r = await fetch('/api/select_ports', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ ports }),
      });
      j = await r.json();
    } catch (e) { status.textContent = 'failed: ' + e; status.style.color = '#ff5555'; return; }
    if (!r.ok) {
      status.textContent = j.error || 'failed';
      status.style.color = '#ff5555';
      return;
    }
    status.textContent = 'applied · ' + (Object.keys(j.selected || {}).length) + ' port(s) active';
    status.style.color = '#00ff88';
    setTimeout(refreshPorts, 800);   // pick up new connection state
  });
});

function tileCountForBbox(west, south, east, north, zmin, zmax) {
  function deg2num(lat, lon, z) {
    const n = 1 << z;
    const x = Math.floor((lon + 180) / 360 * n);
    const lat_rad = lat * Math.PI / 180;
    const y = Math.floor((1 - Math.log(Math.tan(lat_rad) + 1/Math.cos(lat_rad)) / Math.PI) / 2 * n);
    return [Math.max(0, Math.min(n-1, x)), Math.max(0, Math.min(n-1, y))];
  }
  let total = 0;
  for (let z = zmin; z <= zmax; z++) {
    const [x0, y1] = deg2num(north, west, z);
    const [x1, y0] = deg2num(south, east, z);
    total += (Math.abs(x1 - x0) + 1) * (Math.abs(y1 - y0) + 1);
  }
  return total;
}

function updateCacheEstimate() {
  const b = map.getBounds();
  const zmin = parseInt(document.getElementById('cacheZmin').value, 10) || 0;
  const zmax = parseInt(document.getElementById('cacheZmax').value, 10) || zmin;
  const n = tileCountForBbox(b.getWest(), b.getSouth(), b.getEast(), b.getNorth(), zmin, zmax);
  const mb = (n * 0.015).toFixed(1); // ~15 KB / tile rough estimate
  const el = document.getElementById('cacheEstimate');
  el.textContent = '≈ ' + n.toLocaleString() + ' tiles (~' + mb + ' MB) in current view';
  el.style.color = (n > 2000000) ? '#ff5555' : '#00ffff';
}
['cacheZmin', 'cacheZmax'].forEach(id => {
  document.getElementById(id).addEventListener('input', updateCacheEstimate);
});
const _debouncedEstimate = debounce(() => {
  if (document.getElementById('cachePanel').style.display !== 'none') updateCacheEstimate();
}, 200);
map.on('moveend zoomend', _debouncedEstimate);

// Global job state — survives panel collapse, gets rebuilt on page load.
const activeJobs = {};
let _jobPollerStarted = false;
let _jobRefreshInflight = false;

async function refreshAllJobs() {
  if (_jobRefreshInflight) return;            // race-guard against concurrent fetches
  _jobRefreshInflight = true;
  try {
    const r = await fetch('/api/cache_jobs');
    if (!r.ok) return;
    const d = await r.json();
    const ids = new Set((d.jobs || []).map(j => j.id));
    // Drop jobs that no longer exist server-side
    for (const k of Object.keys(activeJobs)) if (!ids.has(k)) delete activeJobs[k];
    // Upsert current state
    (d.jobs || []).forEach(j => { activeJobs[j.id] = j; });
    renderJobs();
  } catch (e) {
    console.debug('refreshAllJobs failed:', e);
  } finally {
    _jobRefreshInflight = false;
  }
}

function startJobPoller() {
  if (_jobPollerStarted) return;
  _jobPollerStarted = true;
  // Always-on tick: refresh active job list every second while ANY job is live,
  // every 5 seconds otherwise. Runs forever, regardless of panel state.
  async function tick() {
    const anyLive = Object.values(activeJobs).some(j => j.status === 'running' || j.status === 'queued');
    await refreshAllJobs();
    setTimeout(tick, anyLive ? 800 : 5000);
  }
  tick();
}

function renderJobs() {
  const container = document.getElementById('cacheJobs');
  if (!container) return;
  container.innerHTML = '';
  // Stable order: running → queued → paused → error → cancelled → done
  const order = {running:0, queued:1, paused:2, error:3, cancelled:4, done:5};
  const jobs = Object.values(activeJobs).sort((a, b) =>
    (order[a.status] ?? 9) - (order[b.status] ?? 9));
  jobs.forEach(j => {
    const pct = j.total > 0 ? (100 * j.done / j.total) : 0;
    const statusColor = ({
      running:'lime', queued:'#00ffff', done:'#00ff88',
      paused:'#ffaa00', cancelled:'#ff8800', error:'#ff5555'
    })[j.status] || 'lime';
    const reason = j.pause_reason ? '<div style="font-size:0.85em; color:#ffaa00; margin-top:2px;">⚠ ' + j.pause_reason + '</div>' : '';
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-top:4px; padding:4px; border:1px solid ' + statusColor + '; background:rgba(0,0,0,0.5);';
    let buttons = '';
    if (j.status === 'running' || j.status === 'queued') {
      buttons = '<button data-id="' + j.id + '" class="cacheCancelBtn" style="margin-top:4px; margin-right:3px; background:#330000; border:1px solid #ff8888; color:#ff8888; font-family:monospace; font-size:0.85em; padding:1px 6px; cursor:pointer;">CANCEL</button>';
    } else if (j.status === 'paused' || j.status === 'error' || j.status === 'cancelled') {
      buttons = '<button data-id="' + j.id + '" class="cacheResumeBtn" style="margin-top:4px; margin-right:3px; background:#003300; border:1px solid lime; color:lime; font-family:monospace; font-size:0.85em; padding:1px 8px; cursor:pointer;">▶ RESUME</button>'
              + '<button data-id="' + j.id + '" class="cacheForgetBtn" style="margin-top:4px; background:#330000; border:1px solid #ff8888; color:#ff8888; font-family:monospace; font-size:0.85em; padding:1px 6px; cursor:pointer;">FORGET</button>';
    } else if (j.status === 'done') {
      buttons = '<button data-id="' + j.id + '" class="cacheForgetBtn" style="margin-top:4px; background:#330000; border:1px solid #ff8888; color:#ff8888; font-family:monospace; font-size:0.85em; padding:1px 6px; cursor:pointer;">DISMISS</button>';
    }
    wrap.innerHTML =
      '<div style="display:flex; justify-content:space-between; color:' + statusColor + ';">'
      + '<span>' + j.name + ' · ' + j.source + '</span>'
      + '<span>' + j.status.toUpperCase() + '</span></div>'
      + '<div style="height:6px; border:1px solid ' + statusColor + '; margin-top:3px; background:#000; position:relative;">'
        + '<div style="position:absolute; left:0; top:0; bottom:0; width:' + pct.toFixed(1) + '%; background:' + statusColor + '; box-shadow:0 0 6px ' + statusColor + ';"></div>'
      + '</div>'
      + '<div style="font-size:0.9em; color:#aaffaa; margin-top:2px;">'
        + j.done + '/' + j.total + ' · fetched ' + j.fetched + ' · skipped ' + j.skipped + ' · errors ' + j.errors
      + '</div>'
      + reason
      + buttons;
    container.appendChild(wrap);
  });
  document.querySelectorAll('.cacheCancelBtn').forEach(b => {
    b.addEventListener('click', async (e) => {
      const id = e.target.getAttribute('data-id');
      await fetch('/api/cache_jobs/' + id + '/cancel', {method: 'POST'});
      refreshAllJobs();
    });
  });
  document.querySelectorAll('.cacheResumeBtn').forEach(b => {
    b.addEventListener('click', async (e) => {
      const id = e.target.getAttribute('data-id');
      await fetch('/api/cache_jobs/' + id + '/resume', {method: 'POST'});
      refreshAllJobs();
    });
  });
  document.querySelectorAll('.cacheForgetBtn').forEach(b => {
    b.addEventListener('click', async (e) => {
      const id = e.target.getAttribute('data-id');
      await fetch('/api/cache_jobs/' + id, {method: 'DELETE'});
      refreshAllJobs();
      refreshOfflineLayers();
    });
  });
}

// Compatibility shim — old call sites used pollJob(id); now everything uses the
// always-on poller. Just trigger an immediate refresh.
function pollJob(_id) { refreshAllJobs(); }

document.getElementById('cacheStartBtn').addEventListener('click', async (ev) => {
  await withButtonLock(ev.currentTarget, 'STARTING...', async () => {
    const name = (document.getElementById('cacheName').value || '').trim();
    const source = document.getElementById('cacheSource').value;
    const zmin = parseInt(document.getElementById('cacheZmin').value, 10);
    const zmax = parseInt(document.getElementById('cacheZmax').value, 10);
    if (!name) { alert('Pick a name (a-z 0-9 _ -)'); return; }
    if (!Number.isFinite(zmin) || !Number.isFinite(zmax)) { alert('zoom values must be integers'); return; }
    if (zmax < zmin) { alert('zMax must be >= zMin'); return; }
    const b = map.getBounds();
    const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
    let r, j;
    try {
      r = await fetch('/api/cache_tiles', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, source, bbox, zmin, zmax}),
      });
      j = await r.json();
    } catch (e) { alert('failed to start: ' + e); return; }
    if (!r.ok) { alert(j.error || 'failed to start'); return; }
    activeJobs[j.id] = j;
    renderJobs();
    refreshAllJobs();  // pick up server-side state immediately
  });
});

// World baseline preset buttons — globe-wide download at the chosen zMax
async function startWorldBaseline(zmax, sizeLabel) {
  const source = document.getElementById('cacheSource').value;
  const name = 'world_' + source;
  const tiles = ((1 << (2 * (zmax + 1))) - 1) / 3 | 0; // sum 4^z for z in [0..zmax]
  const ok = confirm(
    'Download globe-wide ' + source + ' baseline?\\n\\n' +
    '  zooms: 0-' + zmax + '\\n' +
    '  tiles: ~' + tiles.toLocaleString() + '\\n' +
    '  size:  ' + sizeLabel + '\\n\\n' +
    'This writes to ' + name + '.mbtiles and is resumable.');
  if (!ok) return;
  let r, j;
  try {
    r = await fetch('/api/cache_tiles', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, source, bbox: [-180, -85, 180, 85], zmin: 0, zmax}),
    });
    j = await r.json();
  } catch (e) { alert('failed to start: ' + e); return; }
  if (!r.ok) { alert(j.error || 'failed to start'); return; }
  activeJobs[j.id] = j;
  renderJobs();
  pollJob(j.id);
}
document.getElementById('worldZ6Btn').addEventListener('click', (ev) =>
  withButtonLock(ev.currentTarget, 'STARTING...', () => startWorldBaseline(6, '~80 MB')));
document.getElementById('worldZ8Btn').addEventListener('click', (ev) =>
  withButtonLock(ev.currentTarget, 'STARTING...', () => startWorldBaseline(8, '~1.3 GB')));

// ---------- Place search (Nominatim via /api/geocode) ----------
function applyBboxToMap(bbox, autoName) {
  // bbox is [w, s, e, n]; fit and optionally seed the cache name field
  map.fitBounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]]);
  if (autoName) {
    const safe = autoName.toLowerCase().replace(/[^a-z0-9_-]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 32);
    if (safe) document.getElementById('cacheName').value = safe;
  }
  setTimeout(updateCacheEstimate, 200);
}
async function runGeocode() {
  const q = document.getElementById('geoQuery').value.trim();
  const box = document.getElementById('geoResults');
  if (q.length < 2) { box.innerHTML = ''; return; }
  box.innerHTML = '<div style="color:#00ffff; font-size:0.85em;">searching...</div>';
  try {
    const r = await fetch('/api/geocode?q=' + encodeURIComponent(q));
    const d = await r.json();
    if (!d.results || d.results.length === 0) {
      box.innerHTML = '<div style="color:#ff8888; font-size:0.85em;">no results</div>';
      return;
    }
    box.innerHTML = '';
    d.results.forEach(res => {
      const row = document.createElement('div');
      row.style.cssText = 'padding:3px; margin-top:2px; border:1px solid #003355; cursor:pointer; font-size:0.85em; color:#aaffff; background:rgba(0,20,30,0.5);';
      row.innerHTML = '◉ ' + res.name.split(',').slice(0, 3).join(',')
                    + '<div style="font-size:0.85em; color:#88aaaa;">' + res.category + '/' + res.type + '</div>';
      row.addEventListener('mouseover', () => { row.style.background = 'rgba(0,40,60,0.7)'; });
      row.addEventListener('mouseout',  () => { row.style.background = 'rgba(0,20,30,0.5)'; });
      row.addEventListener('click', () => {
        applyBboxToMap(res.bbox, res.name.split(',')[0]);
        box.innerHTML = '<div style="color:lime; font-size:0.85em;">▸ ' + res.name.split(',')[0] + ' — bbox set</div>';
      });
      box.appendChild(row);
    });
  } catch (e) {
    box.innerHTML = '<div style="color:#ff5555; font-size:0.85em;">geocode failed: ' + e + '</div>';
  }
}
document.getElementById('geoGoBtn').addEventListener('click', (ev) =>
  withButtonLock(ev.currentTarget, '...', runGeocode));
document.getElementById('geoQuery').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); runGeocode(); }
});
// Debounced live search as the user types — respects Nominatim's 1 req/sec.
document.getElementById('geoQuery').addEventListener('input', debounce(() => {
  const q = document.getElementById('geoQuery').value.trim();
  if (q.length >= 3) runGeocode();
}, 600));

// ---------- Region presets ----------
document.getElementById('regionPreset').addEventListener('change', function() {
  if (!this.value) return;
  let p;
  try { p = JSON.parse(this.value); } catch (e) { return; }
  applyBboxToMap(p.bbox, p.name);
  this.value = '';
});

// ---------- Import MBTiles ----------
const importJobs = {};
function renderImportJobs() {
  const c = document.getElementById('importJobs');
  c.innerHTML = '';
  Object.values(importJobs).forEach(j => {
    const pct = j.total > 0 ? (100 * j.done / j.total) : 0;
    const sc = ({running:'#ff00ff', queued:'#00ffff', done:'#00ff88', cancelled:'#ff8800', error:'#ff5555'})[j.status] || '#ff00ff';
    const div = document.createElement('div');
    div.style.cssText = 'margin-top:4px; padding:4px; border:1px solid ' + sc + '; background:rgba(0,0,0,0.5);';
    const mb = (j.done/1048576).toFixed(1);
    const totalMb = j.total > 0 ? (j.total/1048576).toFixed(1) : '?';
    div.innerHTML =
      '<div style="display:flex; justify-content:space-between; color:' + sc + ';">'
      + '<span>' + j.name + '</span><span>' + j.status.toUpperCase() + '</span></div>'
      + '<div style="height:6px; border:1px solid ' + sc + '; margin-top:3px; background:#000; position:relative;">'
        + '<div style="position:absolute; left:0; top:0; bottom:0; width:' + pct.toFixed(1) + '%; background:' + sc + '; box-shadow:0 0 6px ' + sc + ';"></div>'
      + '</div>'
      + '<div style="font-size:0.9em; color:#aaffaa; margin-top:2px;">' + mb + ' / ' + totalMb + ' MB'
      + (j.error_msg ? ' · <span style="color:#ff5555;">' + j.error_msg + '</span>' : '')
      + '</div>';
    c.appendChild(div);
  });
}
async function pollImportJob(id) {
  try {
    const r = await fetch('/api/import_jobs/' + id);
    if (!r.ok) return;
    const j = await r.json();
    importJobs[id] = j;
    renderImportJobs();
    if (j.status === 'queued' || j.status === 'running') {
      setTimeout(() => pollImportJob(id), 800);
    } else {
      refreshOfflineLayers();
    }
  } catch (e) {}
}
document.getElementById('importUrlBtn').addEventListener('click', async (ev) => {
  await withButtonLock(ev.currentTarget, 'STARTING...', async () => {
    const name = document.getElementById('importName').value.trim();
    const url  = document.getElementById('importUrl').value.trim();
    if (!name || !url) { alert('name and URL required'); return; }
    let r, j;
    try {
      r = await fetch('/api/import_mbtiles', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, url}),
      });
      j = await r.json();
    } catch (e) { alert('failed: ' + e); return; }
    if (!r.ok) { alert(j.error || 'failed'); return; }
    importJobs[j.id] = j;
    renderImportJobs();
    pollImportJob(j.id);
  });
});
document.getElementById('importFileBtn').addEventListener('click', async (ev) => {
  await withButtonLock(ev.currentTarget, 'UPLOADING...', async () => {
    const name = document.getElementById('importName').value.trim();
    const f = document.getElementById('importFile').files[0];
    if (!name || !f) { alert('name and file required'); return; }
    const fd = new FormData();
    fd.append('name', name);
    fd.append('file', f);
    let r, j;
    try {
      r = await fetch('/api/import_mbtiles', {method: 'POST', body: fd});
      j = await r.json();
    } catch (e) { alert('failed: ' + e); return; }
    if (!r.ok) { alert(j.error || 'failed'); return; }
    alert('Imported "' + j.name + '" (' + (j.size_bytes/1048576).toFixed(1) + ' MB)');
    refreshOfflineLayers();
  });
});

// ============================================================
// ADS-B air traffic
// ============================================================
const adsbMarkers = {};         // icao -> L.marker
const _adsbLastSeenMs = {};     // icao -> Date.now() when last present in an applied snapshot
// Grace window before a vanished aircraft is removed from the map. ADS-B feeds
// jitter: an aircraft can be absent from one poll then back the next, a single
// upstream fetch can fail or rate-limit (returning an empty/partial set), and
// planes skirting the viewport edge drop out of the bbox for a cycle. Reaping a
// marker the instant it misses ONE snapshot is what made planes "randomly
// disappear" and the count crater to 0. We instead keep a marker until it has
// been missing for longer than this window — the dead-reckoning ticker carries
// it smoothly in the meantime. 30s comfortably spans several missed 2-4s polls
// while staying well under the server's own 180s stale-eviction.
const _ADSB_REAP_GRACE_MS = 30000;
const adsbTrails = {};          // icao -> L.polyline
const adsbHistory = {};         // icao -> [[lat, lon], ...]  (bounded)
const ADSB_TRAIL_MAX_POINTS = 1500;   // high so a loaded full-flight trace (decimated to ~400) plus live extension persists; was 60 (which slid the path off over seconds)
const adsbLayer = L.layerGroup().addTo(map);
const adsbTrailLayer = L.layerGroup().addTo(map);
let adsbLastBboxSent = null;
// Hoist hiddenPaths so adsbApply / trail creation / popup builders can read it
// without hitting TDZ. The drone-pipeline declaration further down was making
// every ADS-B socket frame that arrived during initial script eval crash —
// which is why the AIR TRAFFIC list emptied out and no markers rendered.
window.hiddenPaths = window.hiddenPaths
  || new Set(JSON.parse(localStorage.getItem('hiddenPaths') || '[]'));
window._persistHiddenPaths = window._persistHiddenPaths || function() {
  try { localStorage.setItem('hiddenPaths', JSON.stringify([...window.hiddenPaths])); }
  catch (e) {}
};
var hiddenPaths = window.hiddenPaths;
var _persistHiddenPaths = window._persistHiddenPaths;
// Aircraft trails are OPT-IN (default hidden to keep the map clean). hiddenPaths
// (a hide-list, default-visible) can't model that without re-hiding planes on
// every reap/reload, which is why toggles "disappeared". Track the planes the
// user explicitly turned ON instead — survives reaps, reloads, and supports any
// number of simultaneous paths.
window.shownAircraftPaths = window.shownAircraftPaths
  || new Set(JSON.parse(localStorage.getItem('shownAircraftPaths') || '[]'));
window._persistShownAircraftPaths = window._persistShownAircraftPaths || function() {
  try { localStorage.setItem('shownAircraftPaths', JSON.stringify([...window.shownAircraftPaths])); }
  catch (e) {}
};
var shownAircraftPaths = window.shownAircraftPaths;
var _persistShownAircraftPaths = window._persistShownAircraftPaths;
// Per-type / ALL-IN-VIEW bulk path toggling was removed; paths are per-plane now.
// Oversized residue from the old bulk feature would re-draw as spaghetti AND re-fetch
// thousands of traces on load (rate-limit risk) — so a large set can only be stale
// bulk leftovers. Drop it for a clean per-plane start.
if (shownAircraftPaths.size > 50) {
  shownAircraftPaths.clear();
  _persistShownAircraftPaths();
}
// Per-aircraft custom trail color, stored as a hue (0-359). Absent = use the
// OSINT tag color. Persisted so a chosen color survives reaps/reloads.
window.pathColors = window.pathColors
  || JSON.parse(localStorage.getItem('pathColors') || '{}');
window._persistPathColors = window._persistPathColors || function() {
  try { localStorage.setItem('pathColors', JSON.stringify(window.pathColors)); } catch (e) {}
};
var pathColors = window.pathColors;
var _persistPathColors = window._persistPathColors;

// Altitude colors (feet) — quick visual band
function adsbAltColor(altFt) {
  if (altFt === null || altFt === undefined || altFt === 'ground') return '#666';
  if (altFt < 1000)   return '#ff5555';
  if (altFt < 5000)   return '#ff9933';
  if (altFt < 15000)  return '#ffcc33';
  if (altFt < 25000)  return '#33ddff';
  if (altFt < 35000)  return '#3399ff';
  return '#aa66ff';
}

// ---------- Lock indicator rings (aircraft + drone) ----------
// Visible cyan dashed ring that follows the locked target. Pulses subtly.
// CSS keyframes + per-target Leaflet CircleMarker. Two separate rings because
// aircraft and drones can be locked independently.
(function _injectLockRingCss(){
  const css = '@keyframes lockRingPulse { 0% { stroke-opacity: 0.95; stroke-width: 3; } 50% { stroke-opacity: 0.55; stroke-width: 5; } 100% { stroke-opacity: 0.95; stroke-width: 3; } }'
            + '.lock-ring-svg { animation: lockRingPulse 1.4s ease-in-out infinite; }';
  const s = document.createElement('style');
  s.textContent = css;
  document.head.appendChild(s);
})();

let _aircraftLockRing = null;
let _droneLockRing = null;

function _ensureLockRing(existing, latlng, color) {
  if (!existing) {
    const ring = L.circleMarker(latlng, {
      radius: 24,
      color, weight: 3, opacity: 0.95,
      fill: false, dashArray: '6, 6',
      className: 'lock-ring-svg',
      interactive: false,
      pane: 'overlayPane',
    }).addTo(map);
    return ring;
  }
  existing.setLatLng(latlng);
  if (!map.hasLayer(existing)) existing.addTo(map);
  return existing;
}

function _clearLockRing(ring) {
  if (ring && map.hasLayer(ring)) map.removeLayer(ring);
  return null;
}

function _refreshLockRings() {
  // Pause while the map pans/zooms so the ring rides Leaflet's pane transform
  // (stays glued to its target) instead of being repositioned mid-animation.
  if (_adsbMapMoving) return;
  // Aircraft ring follows lockedAircraft (uses dead-reckoned position)
  if (lockedAircraft && _lastAdsbSnapshot[lockedAircraft]) {
    const ll = (typeof _drCurrentLatLon === 'function') ? _drCurrentLatLon(lockedAircraft) : null;
    const fallback = _lastAdsbSnapshot[lockedAircraft];
    const at = ll || (fallback ? [fallback.lat, fallback.lon] : null);
    if (at) _aircraftLockRing = _ensureLockRing(_aircraftLockRing, at, '#00ffff');
  } else {
    _aircraftLockRing = _clearLockRing(_aircraftLockRing);
  }
  // Drone ring follows followLock when type=drone or pilot
  if (followLock && followLock.enabled && (followLock.type === 'drone' || followLock.type === 'pilot')) {
    const id = followLock.id;
    const det = window.tracked_pairs && window.tracked_pairs[id];
    if (det) {
      const lat = (followLock.type === 'drone') ? det.drone_lat : det.pilot_lat;
      const lon = (followLock.type === 'drone') ? det.drone_long : det.pilot_long;
      if (lat && lon) {
        const color = (followLock.type === 'drone') ? '#ff66ff' : '#ffaa44';
        _droneLockRing = _ensureLockRing(_droneLockRing, [lat, lon], color);
      }
    }
  } else {
    _droneLockRing = _clearLockRing(_droneLockRing);
  }
}
// Refresh rings on the same 100ms tick used for aircraft motion
setInterval(_refreshLockRings, 100);

// Aircraft lock-on / tracking — the map follows whichever ICAO is locked.
// Per-flight lock means panning to follow without zoom changes; user can pan
// off and the lock auto-releases (so we never fight them mid-look).
let lockedAircraft = null;
let _lockPanUntil = 0;   // performance.now() gate so lock-follow re-centers don't stack
function lockAircraft(icao) {
  lockedAircraft = (icao || '').toLowerCase();
  const a = (icao && _lastAdsbSnapshot[icao]) || null;
  if (a && a.lat != null && a.lon != null) {
    map.setView([a.lat, a.lon], Math.max(map.getZoom(), 11));
  }
  if (adsbMarkers[icao]) adsbMarkers[icao].setPopupContent(adsbPopup(a || {icao}));
}
function unlockAircraft() {
  const prev = lockedAircraft;
  lockedAircraft = null;
  if (prev && adsbMarkers[prev]) {
    const a = _lastAdsbSnapshot[prev];
    if (a) adsbMarkers[prev].setPopupContent(adsbPopup(a));
  }
}
// Released on any user-initiated pan/zoom (drag, scroll wheel, pinch)
map.on('dragstart', () => { if (lockedAircraft) unlockAircraft(); });
window.lockAircraft = lockAircraft;     // expose to popup onclick
window.unlockAircraft = unlockAircraft;

// OSINT tag styling — tags drive both the filter chips AND the marker outline
const ADSB_TAGS = [
  // ordered by priority for primary-tag selection
  {id: 'hijack',      label: 'HIJACK',     color: '#ff0033'},
  {id: 'emergency',   label: 'EMERGENCY',  color: '#ff5555'},
  {id: 'military',    label: 'MIL',        color: '#ff8800'},
  {id: 'government',  label: 'GOV',        color: '#ffcc00'},
  {id: 'police',      label: 'LEO',        color: '#33aaff'},
  {id: 'rotorcraft',  label: 'ROTOR',      color: '#aa88ff'},
  {id: 'uav',         label: 'UAV',        color: '#ff44ff'},
  {id: 'commercial',  label: 'COMM',       color: '#88ff88'},
  {id: 'private',     label: 'PVT',        color: '#cccccc'},
  {id: 'unknown',     label: 'UNK',        color: '#888888'},
];
const adsbTagById = Object.fromEntries(ADSB_TAGS.map(t => [t.id, t]));
// Filter set: which tags are currently visible. Default = all on.
const adsbVisible = new Set(ADSB_TAGS.map(t => t.id));

function primaryTag(tags) {
  if (!tags || !tags.length) return 'unknown';
  for (const t of ADSB_TAGS) if (tags.indexOf(t.id) !== -1) return t.id;
  return tags[0];
}

function aircraftPassesFilter(a) {
  const tags = a.tags || ['unknown'];
  // pass if ANY of its tags is in the visible set
  for (const t of tags) if (adsbVisible.has(t)) return true;
  return false;
}

// Aircraft-shape SVG paths keyed by ADS-B emitter category. Each shape points
// along its heading (0deg = up). 22x22 viewBox, centered at 11,11.
//   A1 light, A2 small, A3 large, A5 heavy, A6 high-perf, A7 rotorcraft
//   B1 glider, B2 LTA, B4 ultralight, B6 UAV, B7 spacecraft
const ADSB_SHAPES = {
  // Heavy/high-performance: bigger swept-wing arrow
  heavy:  '<path d="M11 1 L20 20 L11 16 L2 20 Z" />',
  jet:    '<path d="M11 1 L18 20 L11 15 L4 20 Z" />',     // standard airliner
  light:  '<path d="M11 3 L16 19 L11 15 L6 19 Z" />',     // small triangle
  rotor:  '<g><circle cx="11" cy="11" r="5" /><path d="M11 4 L11 18 M4 11 L18 11" stroke-width="1.4" /></g>',
  glider: '<path d="M11 1 L13 17 L11 13 L9 17 Z" />',     // long narrow
  uav:    '<rect x="6" y="6" width="10" height="10" />',  // square (UAV)
  space:  '<path d="M11 1 L15 12 L13 20 L9 20 L7 12 Z" />', // rocket
  unknown:'<path d="M11 1 L18 20 L11 15 L4 20 Z" />',     // default to jet
};

function shapeForCategory(cat) {
  if (!cat) return 'jet';
  const c = cat.toUpperCase();
  if (c === 'A7') return 'rotor';
  if (c === 'A5' || c === 'A4') return 'heavy';
  if (c === 'A6') return 'jet';
  if (c === 'A1' || c === 'A2') return 'light';
  if (c === 'A3') return 'jet';
  if (c === 'B1') return 'glider';
  if (c === 'B6') return 'uav';
  if (c === 'B7') return 'space';
  return 'jet';
}

// Stable click handler — referenced by name so .off()/.on() can dedupe it on
// every snapshot. Opens the marker's popup. Don't stopPropagation: Leaflet's
// own bindPopup click-to-open listener also lives on the marker, and adding
// stopImmediatePropagation could starve it; this just guarantees `openPopup`
// runs even if some other path is misbehaving.
function _adsbMarkerClick(ev) {
  if (ev && ev.originalEvent && typeof L !== 'undefined' && L.DomEvent) {
    try { L.DomEvent.stopPropagation(ev.originalEvent); } catch (e) {}
  }
  if (this && typeof this.openPopup === 'function') this.openPopup();
}
// Companion mousedown handler — fires BEFORE click. Some browsers (Brave with
// strict shields, Safari with privacy mode) can swallow click events on
// dynamically-created marker DOM but still pass through mousedown. This is
// the belt-and-suspenders fallback so the popup opens no matter what.
function _adsbMarkerMouseDown(ev) {
  if (ev && ev.originalEvent && typeof L !== 'undefined' && L.DomEvent) {
    try { L.DomEvent.stopPropagation(ev.originalEvent); } catch (e) {}
  }
  if (this && typeof this.openPopup === 'function') this.openPopup();
}

// Native DOM click listener attached directly to the marker's icon element.
// This bypasses Leaflet's internal click-detection heuristics entirely —
// Leaflet considers a click "valid" only if mousedown and mouseup happen at
// the SAME pixel. Any mouse movement between them (even 1px on a trackpad)
// gets reclassified as a drag and the click event is suppressed. The native
// 'click' event the browser fires has no such restriction; this listener
// catches every actual click on the icon.
function _adsbAttachNativeClick(marker) {
  if (!marker || !marker.getElement) return;
  const el = marker.getElement();
  if (!el || el.__adsbNativeClickBound) return;
  el.__adsbNativeClickBound = true;
  const openHandler = function(e) {
    e.stopPropagation();
    try { marker.openPopup(); } catch (_) {}
  };
  el.addEventListener('click', openHandler);
  el.addEventListener('mouseup', openHandler);
  // CRITICAL — kill mousedown propagation. If we let mousedown bubble to the
  // map, Leaflet starts a "map drag" with the cursor pinned. User sees the
  // map sticking to the cursor instead of getting a popup. Stop the event
  // here so the map never knows mousedown happened on the icon.
  el.addEventListener('mousedown', function(e) {
    e.stopPropagation();
    e.preventDefault();   // also blocks the browser's drag-image behavior
  });
  el.addEventListener('dragstart', function(e) { e.preventDefault(); });
  // Force inline pointer-events:auto + cursor:pointer so the cascade can't
  // sabotage hit detection on Brave with shields or any privacy plugin.
  el.style.pointerEvents = 'auto';
  el.style.cursor = 'pointer';
}

// MAP-LEVEL "nearest plane" click handler. Clicks anywhere on the map look
// for the closest aircraft icon within 28 pixels and open ITS popup. This
// dodges every hit-test edge case: even if the user clicks 20px off the
// plane icon, they get the popup they intended. Drones already work because
// emoji icons paint the full glyph; this gives planes the same generosity.
function _adsbOpenNearestPlane(ev) {
  if (!ev || !ev.containerPoint) return false;
  const cp = ev.containerPoint;
  let bestDist = 30 * 30;   // 30px squared
  let best = null;
  for (const icao in adsbMarkers) {
    const m = adsbMarkers[icao];
    if (!m || !m.getLatLng) continue;
    let mp;
    try { mp = map.latLngToContainerPoint(m.getLatLng()); } catch (e) { continue; }
    const dx = mp.x - cp.x, dy = mp.y - cp.y;
    const d = dx*dx + dy*dy;
    if (d < bestDist) { bestDist = d; best = m; }
  }
  if (best) {
    try { best.openPopup(); } catch (_) {}
    return true;
  }
  return false;
}
setTimeout(() => {
  if (typeof map === 'undefined') return;
  map.on('click', (ev) => {
    // If the click was on a marker (handled by marker), Leaflet sets ev.layer.
    // Otherwise this is a "click in empty space" — try to grab the nearest
    // plane. If nothing's nearby, the map's normal closePopupOnClick still
    // dismisses any open popup.
    if (!ev.layer) _adsbOpenNearestPlane(ev);
  });
}, 200);

// Map-level delegated click listener — last-resort safety net. If any layer
// of Leaflet's marker click pipeline is misbehaving, this still routes the
// click to the right popup by walking up the DOM, finding the `.adsb-icon`
// the user clicked on, looking up the corresponding marker, and opening its
// popup directly. Wired after `map` is initialized.
setTimeout(() => {
  if (typeof map === 'undefined') return;
  const container = map.getContainer();
  if (!container || container.__adsbClickWired) return;
  container.__adsbClickWired = true;
  container.addEventListener('click', (e) => {
    const iconEl = e.target && e.target.closest && e.target.closest('.adsb-icon');
    if (!iconEl) return;
    // Find which marker owns this DOM element
    for (const icao in adsbMarkers) {
      const m = adsbMarkers[icao];
      if (m && m.getElement && m.getElement() === iconEl) {
        // Stop the click from bubbling further so the map's own click
        // handler can't close-then-reopen anything.
        e.stopPropagation();
        try { m.openPopup(); } catch (err) {}
        return;
      }
    }
  }, true);   // capture phase so we beat Leaflet's own delegate handlers
}, 100);

function adsbIcon(heading, fillColor, tagColor, category) {
  // Shape rotated along heading (0 deg = north, clockwise).
  // fillColor = altitude band; tagColor = OSINT category outline (or black).
  const h = (heading == null) ? 0 : heading;
  const stroke = tagColor || '#000';
  const sw = tagColor ? 1.5 : 0.8;
  const shapeKey = shapeForCategory(category);
  const shape = ADSB_SHAPES[shapeKey] || ADSB_SHAPES.jet;
  // Rotor shapes don't rotate (helicopters point all directions); everything else does
  const rot = (shapeKey === 'rotor' || shapeKey === 'uav') ? 0 : h;
  // STUDIED THE DRONE PATH AND COPIED IT EXACTLY:
  // Drones (createIcon) use a single <div> with line-height/font-size set to
  // the icon size, and the emoji as text content. The text glyph paints the
  // entire icon area → clicks register anywhere → no `visiblePainted` misses.
  //
  // Same approach for planes: ONE div, with an inline SVG inside. Critically,
  // the div itself has a SOLID-PAINTED background (very low alpha but real
  // paint, not opacity:0) so the bounding box always counts as "painted" for
  // hit detection. The SVG is centered inside via flex so it stays visible.
  const size = 32;
  return L.divIcon({
    className: 'adsb-icon',
    iconSize: [size, size],
    iconAnchor: [size/2, size/2],
    // SVG is decorative-only (pointer-events:none) so hover always lands on
    // the outer DIV. The DIV is the one with cursor:pointer + the click hit
    // target. This guarantees the one-finger cursor appears on hover anywhere
    // in the 32×32 box, regardless of whether you're over the plane silhouette
    // or the empty corner.
    html: '<div style="'
        +   'width:' + size + 'px; height:' + size + 'px; '
        +   'display:flex; align-items:center; justify-content:center; '
        +   'background-color:rgba(0,0,0,0.01); '   // solid paint (1% alpha)
        +   'border-radius:50%; cursor:pointer;'
        + '">'
        +   '<svg viewBox="0 0 22 22" width="22" height="22"'
        +     ' style="transform:rotate(' + rot + 'deg); transform-origin:50% 50%; '
        +     ' pointer-events:none; cursor:pointer;"'
        +     ' fill="' + fillColor + '" stroke="' + stroke + '"'
        +     ' stroke-width="' + sw + '" stroke-linejoin="round">'
        +     shape
        +   '</svg>'
        + '</div>',
  });
}

function adsbPopup(a) {
  const cs = a.callsign || '—';
  const alt = (a.alt_baro === 'ground') ? 'GND'
            : (a.alt_baro != null ? Math.round(a.alt_baro).toLocaleString() + ' ft' : '—');
  const vel = (a.velocity != null ? Math.round(a.velocity) + ' kt' : '—');
  const hdg = (a.heading != null ? Math.round(a.heading) + '°' : '—');
  const vr  = (a.vert_rate != null ? (a.vert_rate > 0 ? '+' : '') + Math.round(a.vert_rate) + ' fpm' : '—');
  const sq  = (a.squawk || '').toString();
  const emergSq = (sq === '7500' || sq === '7600' || sq === '7700');
  // Single-color tag (primary tag only) keeps the header restrained.
  const pTag = primaryTag(a.tags);
  const tagMeta = adsbTagById[pTag] || {label: pTag.toUpperCase(), color: '#88aaff'};
  const accent = '#88c8ff';   // unified popup accent (matches AIR TRAFFIC panel)
  const muted  = '#7a8b9a';
  const icao = (a.icao || '').toLowerCase();
  const isLocked = (lockedAircraft === icao);

  // Compact stat row builder — label / value pairs render uniformly.
  const stat = (label, value, color) =>
      '<div style="display:flex; justify-content:space-between; padding:2px 0;">'
    + '<span style="color:' + muted + ';">' + label + '</span>'
    + '<span style="color:' + (color || '#dde6ee') + '; font-weight:600;">' + value + '</span>'
    + '</div>';

  // TRACK — single pill button. Active state inverts (filled accent, dark text)
  // so it reads as "engaged" without neon glow gymnastics.
  const trackBtn = isLocked
    ? '<button onclick="event.stopPropagation(); unlockAircraft();" '
      + 'style="flex:1; padding:7px 0; border:0; border-radius:5px; cursor:pointer; '
      + 'background:' + accent + '; color:#001828; font-family:inherit; font-weight:700; letter-spacing:1.5px; font-size:0.85em;">'
      + 'TRACKING · TAP TO RELEASE</button>'
    : '<button onclick="event.stopPropagation(); lockAircraft(\\'' + icao + '\\');" '
      + 'style="flex:1; padding:7px 0; border:1px solid ' + accent + '; border-radius:5px; cursor:pointer; '
      + 'background:transparent; color:' + accent + '; font-family:inherit; font-weight:600; letter-spacing:1.5px; font-size:0.85em;">'
      + 'TRACK</button>';

  // PATH — pure-CSS toggle (animates via input:checked sibling selectors).
  // Works while the popup is open even though the popup HTML never re-renders.
  const pathOn = shownAircraftPaths.has(icao);
  const pathSwitch =
      '<label style="display:flex; align-items:center; justify-content:space-between; '
    + 'padding:6px 2px; cursor:pointer;" onclick="event.stopPropagation();">'
    + '<span style="color:' + muted + '; font-size:0.85em; letter-spacing:1px;">FLIGHT PATH</span>'
    + '<span class="pop-toggle">'
    +   '<input type="checkbox"' + (pathOn ? ' checked' : '')
    +   ' onchange="event.stopPropagation(); _adsbTogglePath(\\'' + icao + '\\', this.checked);">'
    +   '<span class="pop-track"></span>'
    +   '<span class="pop-knob"></span>'
    + '</span>'
    + '</label>';

  // Color slider — sits under the toggle; drag to recolor THIS plane's trail.
  const pathHue = _adsbPathHue(icao);
  const colorSlider =
      '<div style="display:flex; align-items:center; gap:8px; padding:0 2px 4px 2px;" onclick="event.stopPropagation();">'
    + '<span style="color:' + muted + '; font-size:0.72em; letter-spacing:1px;">COLOR</span>'
    + '<input type="range" min="0" max="359" value="' + pathHue + '"'
    +   ' oninput="event.stopPropagation(); _adsbSetPathColor(\\'' + icao + '\\', this.value);"'
    +   ' onmousedown="event.stopPropagation();" onpointerdown="event.stopPropagation();"'
    +   ' ontouchstart="event.stopPropagation();"'
    +   ' style="flex:1; height:10px; -webkit-appearance:none; appearance:none; border-radius:5px; cursor:pointer;'
    +   ' background:linear-gradient(to right, hsl(0,85%,55%), hsl(60,85%,55%), hsl(120,85%,55%), hsl(180,85%,55%), hsl(240,85%,55%), hsl(300,85%,55%), hsl(360,85%,55%));">'
    + '</div>';

  return ''
    // Outer card — single dark surface, single subtle border, comfortable padding
    + '<div style="font-family:\\'Inter\\',-apple-system,BlinkMacSystemFont,\\'Segoe UI\\',sans-serif; '
    +   'min-width:220px; color:#dde6ee; padding:2px 2px 2px 2px;">'
    // Header — callsign large, ICAO + tag chip on a quiet second line
    + '<div style="font-size:1.1em; font-weight:700; color:#fff; letter-spacing:0.5px;">' + cs + '</div>'
    + '<div style="display:flex; align-items:center; gap:6px; margin-top:1px;">'
    +   '<span style="color:' + muted + '; font-size:0.78em; letter-spacing:1px;">'
    +     (a.icao || '?').toUpperCase() + '</span>'
    +   '<span style="font-size:0.7em; padding:1px 6px; border-radius:9px; '
    +     'background:' + tagMeta.color + '22; color:' + tagMeta.color + '; '
    +     'border:1px solid ' + tagMeta.color + '55; letter-spacing:1px; font-weight:600;">'
    +     tagMeta.label + '</span>'
    +   (emergSq
        ? '<span style="margin-left:auto; font-size:0.7em; padding:1px 6px; border-radius:9px; '
          + 'background:#ff334422; color:#ff6677; border:1px solid #ff556688; letter-spacing:1px; font-weight:700;">SQ ' + sq + '</span>'
        : '')
    + '</div>'
    // Divider
    + '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0;"></div>'
    // Stats grid
    + '<div style="font-size:0.85em; line-height:1.45;">'
    +   stat('ALT',  alt, adsbAltColor(a.alt_baro))
    +   stat('SPD',  vel)
    +   stat('HDG',  hdg)
    +   stat('V/S',  vr, (a.vert_rate > 64 ? '#88dd88' : a.vert_rate < -64 ? '#dd8888' : '#dde6ee'))
    +   (sq && !emergSq ? stat('SQUAWK', sq) : '')
    + '</div>'
    // Divider
    + '<div style="height:1px; background:rgba(255,255,255,0.07); margin:8px 0 4px 0;"></div>'
    // Actions
    + '<div style="display:flex; gap:6px;">' + trackBtn + '</div>'
    + pathSwitch
    + colorSlider
    + (isLocked
        ? '<div style="font-size:0.72em; color:' + muted + '; text-align:center; margin-top:2px; letter-spacing:0.5px;">drag map to release</div>'
        : '')
    + '</div>';
}

// Per-aircraft path toggle. Pure individual control — there's no global
// aircraft-paths master to gate against. The trail layer is always on the
// map; whether each aircraft's polyline is in it is decided here.
//
// When the user flips a path ON, we ALSO request the full historical trace
// from the upstream readsb feeds (adsb.lol/adsb.fi/airplanes.live). This
// gives them the entire recorded flight — takeoff to now — instead of only
// the chunk we've observed since the page loaded. SDR sources don't have
// that endpoint; the polyline gracefully falls back to our local history.
const _adsbTraceFetched = new Set();    // succeeded — don't refetch
const _adsbTraceInflight = new Set();   // in-flight — dedupe concurrent toggles
async function _adsbFetchAndDrawTrace(icao) {
  if (!icao) return;
  // Only SUCCESS marks an icao fetched. A failed/empty fetch must stay retryable —
  // otherwise one slow miss froze the plane on its dead-reckoned straight line for
  // the rest of the session.
  if (_adsbTraceFetched.has(icao) || _adsbTraceInflight.has(icao)) return;
  _adsbTraceInflight.add(icao);
  try {
    const r = await fetch('/api/adsb/trace/' + encodeURIComponent(icao));
    const d = await r.json();
    if (!d || !d.ok || !Array.isArray(d.points) || d.points.length < 2) return;
    // Replace whatever local history we had with the full upstream trace, then
    // append our most-recent observed point so live updates keep extending it.
    const localHist = adsbHistory[icao] || [];
    const tail = localHist.length ? localHist[localHist.length - 1] : null;
    const fullPts = d.points.slice();
    if (tail) {
      const [lastLat, lastLon] = fullPts[fullPts.length - 1];
      if (Math.abs(lastLat - tail[0]) > 1e-5 || Math.abs(lastLon - tail[1]) > 1e-5) {
        fullPts.push(tail);
      }
    }
    adsbHistory[icao] = fullPts;
    // Draw/refresh immediately — the polyline may not exist yet if the plane
    // hadn't moved when the path was toggled on. Reconcile creates it if shown.
    _adsbReconcileTrail(icao, true);
    _adsbTraceFetched.add(icao);   // mark done ONLY after a real trace landed
  } catch (e) {
    console.debug('trace fetch failed for', icao, e);
  } finally {
    _adsbTraceInflight.delete(icao);
  }
}
function _adsbTogglePath(icao, on) {
  setPathHidden('aircraft:' + icao, !on);
  if (on) _adsbFetchAndDrawTrace(icao);
}
window._adsbTogglePath = _adsbTogglePath;

// Throttled batch trace loader for the BULK path controls. The local trail is
// only ~6s of dead-reckoning (~1km) — invisible zoomed out. So, like the single
// popup toggle, bulk-shown planes pull their FULL upstream flight trace (a long,
// visible path). Queue with low concurrency + a cap so 100s of planes don't
// hammer the server all at once; paths fill in progressively.
const _adsbTraceQueued = new Set();    // icaos queued or in-flight (dedupe)
const _adsbBatchQueue = [];            // pending batches (each an icao array)
let _adsbBatchActive = 0;
const _ADSB_TRACE_BATCH = 50;          // icaos per batch request
const _ADSB_BATCH_CONCURRENCY = 2;     // concurrent batch requests — gentle; the trace host hard-blocks on volume
const _ADSB_TRACE_BULK_CAP = 150;      // max icaos per bulk action — the trace host hard-blocks the IP on high volume, so keep bulk modest
function _adsbQueueTraces(icaos) {
  const fresh = [];
  for (const icao of icaos) {
    if (fresh.length >= _ADSB_TRACE_BULK_CAP) break;
    if (!icao || _adsbTraceFetched.has(icao) || _adsbTraceQueued.has(icao)) continue;
    _adsbTraceQueued.add(icao);
    fresh.push(icao);
  }
  for (let i = 0; i < fresh.length; i += _ADSB_TRACE_BATCH) {
    _adsbBatchQueue.push(fresh.slice(i, i + _ADSB_TRACE_BATCH));
  }
  _adsbBatchDrain();
}
// One POST fetches a whole batch of traces (server fetches them in parallel),
// so hundreds of paths load in a few round trips instead of hundreds — the
// browser's ~6-per-host request cap was the bottleneck.
function _adsbBatchDrain() {
  while (_adsbBatchActive < _ADSB_BATCH_CONCURRENCY && _adsbBatchQueue.length) {
    const batch = _adsbBatchQueue.shift();
    _adsbBatchActive++;
    fetch('/api/adsb/traces', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ icaos: batch }),
    }).then(r => r.json()).then(d => {
      const traces = (d && d.traces) || {};
      batch.forEach(icao => {
        _adsbTraceQueued.delete(icao);
        const pts = traces[icao];
        if (pts && pts.length >= 2) {
          adsbHistory[icao] = pts;
          _adsbReconcileTrail(icao, true);
          _adsbTraceFetched.add(icao);   // mark done ONLY on a real trace; misses stay retryable
        }
      });
    }).catch(() => {
      batch.forEach(icao => _adsbTraceQueued.delete(icao));   // allow a later retry
    }).finally(() => {
      _adsbBatchActive--;
      _adsbBatchDrain();
    });
  }
}

// Per-drone (or per-pilot) path toggle. Symmetrical to _adsbTogglePath:
// flipping ON for a single entity activates the matching global master so the
// trail actually renders even if the user left the kind's global switch off.
function _droneTogglePath(kind, mac, on) {
  setPathHidden(kind + ':' + mac, !on);
  if (on && !_pathsMasters[kind]) {
    _pathsMasters[kind] = true;
    const cap = kind.charAt(0).toUpperCase() + kind.slice(1);
    localStorage.setItem('pathsShow' + cap, '1');
    if (typeof _applyPathsMaster === 'function') _applyPathsMaster(kind);
    const cb = document.getElementById('paths' + cap + 'Toggle');
    if (cb) cb.checked = true;
    // Keep the meta "ALL" switch in sync if it exists
    const allCb = document.getElementById('pathsAllToggle');
    if (allCb) allCb.checked = !!(
      document.getElementById('pathsDroneToggle')?.checked &&
      document.getElementById('pathsPilotToggle')?.checked &&
      document.getElementById('pathsAircraftToggle')?.checked
    );
  }
}
window._droneTogglePath = _droneTogglePath;

// Latest snapshot keyed by ICAO — used by the lock-on tracking handler
const _lastAdsbSnapshot = {};
// Per-ICAO dead-reckoning state: anchor lat/lon + heading + velocity + timestamp.
// Between server polls (every 4s), we extrapolate the aircraft position so the
// markers visibly travel across the map instead of teleporting on each update.
const _adsbDR = {};

// Dead-reckoning is a light tracking filter, not a raw passthrough. We keep a
// smooth model state (position + velocity, anchored at time t) and, on each new
// fix, gently correct toward it instead of snapping. This is the "source of
// truth": straight-line extrapolation between fixes, low-pass-corrected so
// per-fix GPS / velocity noise (measured displacement/velocity ratio swings
// 0.55–1.55) doesn't make the marker wobble along its path.
function _drStorePosition(a) {
  if (!a.icao || a.lat == null || a.lon == null) return;
  const prev = _adsbDR[a.icao];
  // Stale-repeat guard: pollers fire faster than the source refreshes, so the
  // same RAW fix returns across several polls. Don't re-filter a repeat — keep
  // extrapolating from the existing anchor (refresh heading/ground only).
  if (prev && prev.rawLat === a.lat && prev.rawLon === a.lon) {
    prev.heading = a.heading;
    prev.onGround = !!a.on_ground;
    return;
  }
  const now = performance.now();
  // Project the raw fix forward by its OWN age so it represents where the plane
  // is *now*. Fixes arrive a few seconds stale (median ~3s, jittery 0.6–137s),
  // so anchoring at receive-time placed the marker behind and snapped it backward
  // every poll. a.seen is source epoch seconds; Date.now()/1000 is the client
  // epoch (equal on localhost).
  let ageSec = 0;
  if (a.seen != null) {
    const g = (Date.now() / 1000) - a.seen;
    if (g > 0 && g < 300) ageSec = g;
  }
  const fixNow = _drProject(a.lat, a.lon, a.velocity, a.heading, !!a.on_ground, ageSec);
  let lat = fixNow[0], lon = fixNow[1], vel = a.velocity;
  if (prev) {
    const predNow = _drCurrentLatLon(a.icao);   // where our track says it is now
    if (predNow) {
      const dLat = fixNow[0] - predNow[0], dLon = fixNow[1] - predNow[1];
      if (Math.abs(dLat) > 0.05 || Math.abs(dLon) > 0.05) {
        // Big divergence (sharp maneuver, teleport, very stale) — trust the fix.
        lat = fixNow[0]; lon = fixNow[1];
      } else {
        // Gentle alpha correction toward the fix; the rest stays on the smooth
        // predicted track. Velocity is filtered too so prediction speed (hence
        // longitudinal motion) doesn't jitter.
        const ALPHA_POS = 0.35, ALPHA_VEL = 0.30;
        lat = predNow[0] + dLat * ALPHA_POS;
        lon = predNow[1] + dLon * ALPHA_POS;
        if (a.velocity != null && prev.velocity != null) {
          vel = prev.velocity + (a.velocity - prev.velocity) * ALPHA_VEL;
        }
      }
    }
  }
  _adsbDR[a.icao] = {
    lat: lat, lon: lon,             // filtered estimate, valid as of `t`
    rawLat: a.lat, rawLon: a.lon,   // raw fix — for stale-repeat detection
    heading: a.heading,
    velocity: vel,                  // knots (filtered)
    onGround: !!a.on_ground,
    t: now,
  };
}

// Project a position forward along its track. 1 NM = 1/60° lat; lon scales by
// 1/cos(lat). On-ground / no-velocity / no-heading / out-of-window (>60s) → no
// motion (we don't trust extrapolation beyond a minute).
function _drProject(lat, lon, velocity, heading, onGround, dtSec) {
  if (onGround || !velocity || heading == null || dtSec <= 0 || dtSec > 60) return [lat, lon];
  const distDeg = (velocity * dtSec / 3600) / 60;
  const hRad = heading * Math.PI / 180;
  const dLat = distDeg * Math.cos(hRad);
  const cosLat = Math.max(0.05, Math.cos(lat * Math.PI / 180));
  const dLon = (distDeg * Math.sin(hRad)) / cosLat;
  return [lat + dLat, lon + dLon];
}

function _drCurrentLatLon(icao) {
  const d = _adsbDR[icao];
  if (!d || d.lat == null) return null;
  const dt = (performance.now() - d.t) / 1000;
  if (dt < 0) return [d.lat, d.lon];
  return _drProject(d.lat, d.lon, d.velocity, d.heading, d.onGround, dt);
}

// Animation tick — runs ~10x/sec, walks every aircraft marker forward along its
// last-known heading at last-known velocity. Cheap because we only call setLatLng
// (no DOM rebuild) and skip aircraft that are on the ground or have no velocity.
// ---- Per-aircraft trail color ----
// Resolve color: custom hue (popup slider) if set, else the OSINT tag color.
function _adsbTagColorFor(icao) {
  const a = _lastAdsbSnapshot[icao];
  return a ? (adsbTagById[primaryTag(a.tags)] || {color: '#888'}).color : '#888';
}
function _adsbPathColorFor(icao) {
  const h = pathColors[icao];
  return (h != null) ? ('hsl(' + h + ', 85%, 55%)') : _adsbTagColorFor(icao);
}
// hex -> hue (0-359), to seed the slider at the plane's current tag color.
function _hexToHue(hex) {
  if (!hex || hex[0] !== '#') return 200;
  let r, g, b;
  if (hex.length === 4) { r = parseInt(hex[1]+hex[1],16); g = parseInt(hex[2]+hex[2],16); b = parseInt(hex[3]+hex[3],16); }
  else { r = parseInt(hex.slice(1,3),16); g = parseInt(hex.slice(3,5),16); b = parseInt(hex.slice(5,7),16); }
  r/=255; g/=255; b/=255;
  const mx = Math.max(r,g,b), mn = Math.min(r,g,b), d = mx-mn;
  if (d === 0) return 0;
  let h;
  if (mx === r) h = ((g-b)/d) % 6;
  else if (mx === g) h = (b-r)/d + 2;
  else h = (r-g)/d + 4;
  h = Math.round(h*60); if (h<0) h+=360;
  return h;
}
// Slider value for a plane: its custom hue if set, else its tag color's hue.
function _adsbPathHue(icao) {
  return (pathColors[icao] != null) ? pathColors[icao] : _hexToHue(_adsbTagColorFor(icao));
}
// Live color change from the popup slider.
function _adsbSetPathColor(icao, hue) {
  pathColors[icao] = +hue;
  _persistPathColors();
  const tr = adsbTrails[icao];
  if (tr) tr.setStyle({color: _adsbPathColorFor(icao)});
  else _adsbReconcileTrail(icao, true);
}
window._adsbSetPathColor = _adsbSetPathColor;

// ---- Bulk path controls (AIR TRAFFIC panel) ----
// Aircraft whose markers are currently within the map viewport.
function _adsbInViewIcaos() {
  if (typeof map === 'undefined') return [];
  const b = map.getBounds();
  const out = [];
  for (const icao in adsbMarkers) {
    const m = adsbMarkers[icao];
    if (m && m.getLatLng && b.contains(m.getLatLng())) out.push(icao);
  }
  return out;
}
// Keep any OPEN popup's toggle/slider in sync after a bulk change.
function _adsbSyncOpenPopups() {
  for (const icao in adsbMarkers) {
    const m = adsbMarkers[icao];
    if (m && m.isPopupOpen && m.isPopupOpen()) {
      m.setPopupContent(adsbPopup(_lastAdsbSnapshot[icao] || {icao: icao}));
    }
  }
}
function _adsbShowAllPathsInView() {
  const v = _adsbInViewIcaos();
  v.forEach(icao => shownAircraftPaths.add(icao));
  _persistShownAircraftPaths();
  _adsbQueueTraces(v);   // pull full flight traces (capped) so paths are visible
  v.forEach(icao => _adsbReconcileTrail(icao, true));
  _adsbSyncOpenPopups();
  if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();
}
function _adsbClearAllPaths() {
  const all = [...shownAircraftPaths];
  shownAircraftPaths.clear();
  _persistShownAircraftPaths();
  all.forEach(icao => _adsbReconcileTrail(icao, true));   // reconcile removes them
  _adsbSyncOpenPopups();
  if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();
}
// Toggle paths for every in-view aircraft carrying a given OSINT tag (e.g. all
// 'military' in view). If they're all already shown, this turns them OFF.
function _adsbToggleTagPathsInView(tagId) {
  const inView = _adsbInViewIcaos().filter(icao => {
    const a = _lastAdsbSnapshot[icao];
    // Match on PRIMARY tag only (the category the plane is colored as on the
    // map) so a multi-tagged plane belongs to exactly one chip — clicking MIL
    // never also flips GOV/etc. Also require the plane to pass the visibility
    // filter: a type that's been filtered out shouldn't be path-toggled.
    return a && aircraftPassesFilter(a) && primaryTag(a.tags) === tagId;
  });
  if (!inView.length) return;
  const allShown = inView.every(icao => shownAircraftPaths.has(icao));
  inView.forEach(icao => { if (allShown) shownAircraftPaths.delete(icao); else shownAircraftPaths.add(icao); });
  _persistShownAircraftPaths();
  if (!allShown) _adsbQueueTraces(inView);   // turning ON → pull full flight traces so paths are visible
  inView.forEach(icao => _adsbReconcileTrail(icao, true));
  _adsbSyncOpenPopups();
  if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();
}
// Render the per-category path chips (one per OSINT tag). Each chip reflects
// state: FILLED when every in-view aircraft of that type has its path shown,
// outlined otherwise, dimmed when none of that type are in view. Shows a count.
function renderAdsbPathTagChips() {
  const c = document.getElementById('adsbPathTagChips');
  if (!c) return;
  const inView = (typeof _adsbInViewIcaos === 'function') ? _adsbInViewIcaos() : [];
  c.innerHTML = '';
  ADSB_TAGS.forEach(t => {
    const matching = inView.filter(icao => {
      const a = _lastAdsbSnapshot[icao];
      // primary classification only, and only planes that pass the visibility
      // filter (so a filtered-out type shows 0 and its chip dims, instead of
      // offering a path toggle that can't draw anything).
      return a && aircraftPassesFilter(a) && primaryTag(a.tags) === t.id;
    });
    const active = matching.length > 0 && matching.every(icao => shownAircraftPaths.has(icao));
    const chip = document.createElement('span');
    chip.textContent = t.label + (matching.length ? ' ' + matching.length : '');
    chip.title = matching.length
      ? ('Toggle flight paths for ' + matching.length + ' ' + t.label + ' in view')
      : ('No ' + t.label + ' in view');
    chip.style.cssText = 'display:inline-block; padding:2px 6px; cursor:pointer; user-select:none; '
      + 'border-radius:3px; border:1px solid ' + t.color + '; letter-spacing:0.5px;'
      + (active ? ('background:' + t.color + '; color:#000; font-weight:700;')
                : ('background:transparent; color:' + t.color + ';'))
      + (matching.length ? '' : ' opacity:0.35;');
    chip.addEventListener('click', () => { _adsbToggleTagPathsInView(t.id); renderAdsbPathTagChips(); });
    c.appendChild(chip);
  });
}

// Apply the OSINT filter to markers ALREADY on the map — instant show/hide, no
// refetch, no wipe-all. Called by the filter chips so toggling a type doesn't
// flash the whole fleet off and back on.
function _adsbReapplyFilter() {
  for (const icao in adsbMarkers) {
    const a = _lastAdsbSnapshot[icao];
    const pass = a ? aircraftPassesFilter(a) : true;
    const m = adsbMarkers[icao];
    if (!m) continue;
    if (pass) { if (!adsbLayer.hasLayer(m)) m.addTo(adsbLayer); }
    else if (adsbLayer.hasLayer(m)) adsbLayer.removeLayer(m);
    _adsbReconcileTrail(icao, false);
  }
}

// Reconcile ONE aircraft's trail polyline with the opt-in shownAircraftPaths
// set. Called every tick (so a toggle takes effect within 100ms even if the
// plane isn't moving) and immediately on toggle. Creates the polyline lazily
// once there are ≥2 points, adds/removes it from the layer to match the set,
// and (on movement) refreshes its coordinates. This is the ONLY place aircraft
// trails are shown/hidden — no reliance on the polyline pre-existing, which is
// what made the toggle unreliable.
function _adsbReconcileTrail(icao, moved) {
  // A trail shows only if the user opted it in AND the aircraft passes the OSINT
  // filter — so filtering a type out hides its paths too.
  const _a = _lastAdsbSnapshot[icao];
  const want = shownAircraftPaths.has(icao) && (!_a || aircraftPassesFilter(_a));
  let tr = adsbTrails[icao];
  if (want) {
    const hist = adsbHistory[icao];
    if (!hist || hist.length < 2) return;          // nothing to draw yet
    if (!tr) {
      tr = adsbTrails[icao] = L.polyline(hist, {color: _adsbPathColorFor(icao), weight: 2.5, opacity: 0.8});
    } else if (moved) {
      tr.setLatLngs(hist);
    }
    if (!adsbTrailLayer.hasLayer(tr)) tr.addTo(adsbTrailLayer);
  } else if (tr && adsbTrailLayer.hasLayer(tr)) {
    adsbTrailLayer.removeLayer(tr);
  }
}

let _drTickerStarted = false;
function _startDRTicker() {
  if (_drTickerStarted) return;
  _drTickerStarted = true;
  setInterval(() => {
    // While the map is mid pan/zoom, Leaflet transforms the marker + SVG panes
    // as one unit — that's what keeps every plane and trail glued to the map and
    // smooth through the gesture. Calling setLatLng mid-zoom fights that
    // transform and makes markers "swim" out of alignment, then snap on zoomend.
    // So pause repositioning during the gesture; the moveend/zoomend handler
    // clears this flag and the very next tick re-anchors everyone to their
    // current dead-reckoned position (correctly projected at the new zoom).
    if (_adsbMapMoving) return;
    Object.keys(adsbMarkers).forEach(icao => {
      const m = adsbMarkers[icao];
      // The dead-reckoned position IS the smoothed source of truth (the tracking
      // filter in _drStorePosition low-passes per-fix noise), so render it
      // directly — no extra easing, no added lag.
      const ll = _drCurrentLatLon(icao);
      if (!ll || !m) return;
      m.setLatLng(ll);
      // Append to the trail history when the marker actually moved, then let the
      // reconcile helper create / show / hide / update the polyline per the
      // opt-in shownAircraftPaths set (single source of truth for visibility).
      const hist = adsbHistory[icao] = adsbHistory[icao] || [];
      const last = hist[hist.length - 1];
      const moved = !last || Math.abs(last[0] - ll[0]) > 1e-6 || Math.abs(last[1] - ll[1]) > 1e-6;
      if (moved) {
        hist.push(ll);
        if (hist.length > ADSB_TRAIL_MAX_POINTS) hist.shift();
      }
      _adsbReconcileTrail(icao, moved);
    });
    // If the user has locked an aircraft, follow its dead-reckoned position.
    // Re-center only when it drifts past a threshold, with a single short glide
    // gated by a timestamp cooldown. The old code panned every 100ms with an
    // animated panTo: the eased animations stacked AND each one fired a moveend
    // that re-ran _adsbRefreshInView (full snapshot walk + list re-render) 10x/
    // sec — which stuttered the entire map. Now we pan at most ~2x/sec and only
    // when needed, so the locked plane stays near center and everything stays smooth.
    if (lockedAircraft) {
      const ll = _drCurrentLatLon(lockedAircraft);
      if (ll && performance.now() >= _lockPanUntil) {
        const size = map.getSize();
        const pp = map.latLngToContainerPoint(ll);
        if (Math.abs(pp.x - size.x / 2) > 60 || Math.abs(pp.y - size.y / 2) > 60) {
          _lockPanUntil = performance.now() + 450;   // let the 0.4s glide finish first
          map.panTo(ll, { animate: true, duration: 0.4, easeLinearity: 0.5 });
        }
      }
    }
  }, 100);
}
_startDRTicker();

// Live-update an OPEN drone popup's TELEMETRY section without wiping any
// in-progress alias edit, tag dropdown selection, lock-button state, etc.
function _droneUpdateOpenPopupStats(mac, det) {
  const m = droneMarkers[mac];
  if (!m || !m.isPopupOpen()) return;
  const root = m.getPopup() && m.getPopup().getElement();
  if (!root) return;
  // The drone popup's TELEMETRY section uses stat() rows shaped as
  // <div><span>LABEL</span><span>VALUE</span></div>. Walk each row, match by
  // label, replace value only.
  const fmt = (k, v) => {
    if (v == null || v === '') return null;
    const lk = String(k).toLowerCase();
    if (typeof v !== 'number') return String(v);
    if (lk.endsWith('lat') || lk.endsWith('long') || lk.endsWith('lon')) return v.toFixed(5);
    if (lk.includes('alt') || lk.includes('speed') || lk.includes('heading') ||
        lk.includes('hdg') || lk.includes('rssi')  || lk.includes('vel') ||
        lk.includes('rate')|| lk.includes('count') || lk.includes('time')) {
      return String(Math.round(v));
    }
    if (Math.abs(v) < 1 && v !== 0) return v.toFixed(3);
    return v.toFixed(2).replace(/\\.?0+$/, '');
  };
  const rows = root.querySelectorAll('.leaflet-popup-content div > div');
  rows.forEach(row => {
    const cells = row.children;
    if (cells.length !== 2) return;
    const label = cells[0].textContent.trim();
    const want = fmt(label, det[label]);
    if (want != null && cells[1].textContent !== want) cells[1].textContent = want;
  });
}

// Cache last-rendered icon params per ICAO so we can skip the expensive
// L.divIcon() rebuild + setIcon() call when nothing visually changed.
const _adsbIconCache = {};
// True while the user is actively dragging/zooming. We defer the expensive
// per-aircraft DOM updates until motion settles — keeps the map ENTIRELY
// snappy even with thousands of aircraft loaded, regardless of zoom level.
let _adsbMapMoving = false;
let _adsbDeferredSnapshot = null;
setTimeout(() => {
  if (typeof map === 'undefined') return;
  map.on('movestart zoomstart', () => { _adsbMapMoving = true; });
  map.on('moveend zoomend',     () => {
    _adsbMapMoving = false;
    // If a snapshot landed while we were moving, apply it now.
    if (_adsbDeferredSnapshot) {
      const s = _adsbDeferredSnapshot;
      _adsbDeferredSnapshot = null;
      requestAnimationFrame(() => adsbApply(s));
    }
  });
}, 0);

// Live-update an OPEN popup's stat values (alt / spd / hdg / vs) without
// re-rendering the popup HTML. This keeps toggle states, mid-click button
// presses, alias text, etc. intact while still showing live telemetry.
function _adsbUpdateOpenPopupStats(a) {
  const m = adsbMarkers[a.icao];
  if (!m || !m.isPopupOpen()) return;
  const popup = m.getPopup();
  if (!popup) return;
  const root = popup.getElement && popup.getElement();
  if (!root) return;
  // Find each stat row by its label cell text and update the value cell.
  // The popup builder renders rows as: <div><span>LABEL</span><span>VALUE</span></div>
  const rows = root.querySelectorAll('.leaflet-popup-content div > div');
  rows.forEach(row => {
    const cells = row.children;
    if (cells.length !== 2) return;
    const label = cells[0].textContent.trim();
    const valEl = cells[1];
    if (label === 'ALT') {
      const alt = (a.alt_baro === 'ground') ? 'GND'
                : (a.alt_baro != null ? Math.round(a.alt_baro).toLocaleString() + ' ft' : '—');
      if (valEl.textContent !== alt) valEl.textContent = alt;
    } else if (label === 'SPD') {
      const v = (a.velocity != null ? Math.round(a.velocity) + ' kt' : '—');
      if (valEl.textContent !== v) valEl.textContent = v;
    } else if (label === 'HDG') {
      const v = (a.heading != null ? Math.round(a.heading) + '°' : '—');
      if (valEl.textContent !== v) valEl.textContent = v;
    } else if (label === 'V/S') {
      const v = (a.vert_rate != null
                  ? (a.vert_rate > 0 ? '+' : '') + Math.round(a.vert_rate) + ' fpm'
                  : '—');
      if (valEl.textContent !== v) valEl.textContent = v;
    }
  });
}

// Chunked apply state — when snapshot.length is huge we slice the work across
// rAF frames so we never block the main thread > 16ms. Coalesces overlapping
// applies: if a new snapshot arrives while one is being chunked, the new one
// replaces the queued one (we always render the most recent).
let _adsbApplyQueued = null;
let _adsbApplyRunning = false;
function adsbApply(snapshot) {
  // Hard gate: if ADS-B is toggled OFF, never render aircraft — no matter which
  // path tried to apply them (filter-chip refetch, a late socket frame, an
  // in-flight poll resolving after disable). This is why switching ADS-B off and
  // then toggling a FILTER chip brought every plane back: the chip handler
  // refetched the still-cached aircraft and applied them. setAdsbEnabled() syncs
  // the toggles BEFORE pulling its snapshot, so the enable path is unaffected.
  if (typeof _adsbIsEnabled === 'function' && !_adsbIsEnabled()) return;
  // If the map is actively moving, defer all marker churn — we'll apply the
  // most recent snapshot on moveend. Map drag/zoom stays smooth no matter how
  // many aircraft are loaded.
  if (_adsbMapMoving) {
    _adsbDeferredSnapshot = snapshot;
    return;
  }
  // Snapshots over CHUNK_THRESHOLD aircraft get processed in rAF slabs so a
  // single huge poll never freezes the UI. Smaller snapshots go through the
  // fast sync path.
  const CHUNK_THRESHOLD = 400;
  if (snapshot.length > CHUNK_THRESHOLD) {
    _adsbApplyQueued = snapshot;
    if (!_adsbApplyRunning) {
      _adsbApplyRunning = true;
      requestAnimationFrame(_adsbApplyChunked);
    }
    return;
  }
  _adsbApplyDirect(snapshot);
}

function _adsbApplyChunked() {
  // Always work on the MOST RECENT queued snapshot. If a new one arrived during
  // chunking, abandon the partial pass and start fresh — we want the user to
  // see the latest data, not stale data we've half-applied.
  if (!_adsbApplyQueued) { _adsbApplyRunning = false; return; }
  const snapshot = _adsbApplyQueued;
  _adsbApplyQueued = null;
  const SLAB = 300;             // aircraft per rAF frame
  let i = 0;
  const seen = new Set();
  // Seed _lastAdsbSnapshot up front for ALL of them so the filter / lookup
  // helpers see the latest data even before the markers are placed.
  for (const a of snapshot) {
    if (a && a.icao && a.lat != null && a.lon != null) {
      _lastAdsbSnapshot[a.icao] = a;
      _drStorePosition(a);
    }
  }
  function step() {
    // Mid-chunk: if a newer snapshot arrived, abort this pass and let the
    // next rAF run the fresh data.
    if (_adsbApplyQueued) {
      requestAnimationFrame(_adsbApplyChunked);
      return;
    }
    const end = Math.min(i + SLAB, snapshot.length);
    for (; i < end; i++) {
      _adsbApplyOne(snapshot[i], seen);
    }
    if (i < snapshot.length) {
      requestAnimationFrame(step);
    } else {
      _adsbApplyReap(seen);
      _adsbApplyFollowLocked();
      _adsbApplyUpdateStatus(snapshot.length);
      _adsbApplyRunning = false;
      // If a newer snapshot landed during the last slab, kick it now.
      if (_adsbApplyQueued) {
        _adsbApplyRunning = true;
        requestAnimationFrame(_adsbApplyChunked);
      }
    }
  }
  step();
}

function _adsbApplyDirect(snapshot) {
  const seen = new Set();
  snapshot.forEach(a => {
    if (!a.icao || a.lat == null || a.lon == null) return;
    _lastAdsbSnapshot[a.icao] = a;
    // Anchor a fresh dead-reckoning state for this aircraft
    _drStorePosition(a);
    _adsbApplyOne(a, seen);
  });
  _adsbApplyReap(seen);
  _adsbApplyFollowLocked();
  _adsbApplyUpdateStatus(snapshot.length);
}

function _adsbApplyOne(a, seen) {
  // Per-aircraft work extracted so it can be shared between the chunked path
  // (rAF slabs) and the direct path (small snapshots). Pre-validated by caller
  // that a.icao / a.lat / a.lon exist.
  if (!a || !a.icao || a.lat == null || a.lon == null) return;
  {
    // The OSINT filter is a SHOW/HIDE, not a skip. We always create + track the
    // marker (so reap never deletes a merely-filtered plane), then add/remove it
    // from the layer below. This makes filter toggles instant in BOTH directions
    // and avoids the wipe-all churn the old refetch path caused.
    seen.add(a.icao);
    const _pass = aircraftPassesFilter(a);
    // Aircraft fill matches the legend chip color (OSINT tag), so
    // a glance at the map matches a glance at the AIR TRAFFIC panel chips.
    const pTag = primaryTag(a.tags);
    const tagMeta = adsbTagById[pTag] || {color: '#888'};
    const fillColor = tagMeta.color;
    const tagColor = '#000';
    // Heading rounded to nearest 5° so 1° jitter doesn't force constant icon
    // rebuilds — visually identical at our pixel scale, much cheaper.
    const headRounded = (a.heading == null) ? 0 : Math.round(a.heading / 5) * 5;
    const iconKey = headRounded + '|' + fillColor + '|' + (a.category || '');
    const ll = [a.lat, a.lon];
    if (adsbMarkers[a.icao]) {
      const m = adsbMarkers[a.icao];
      // Do NOT snap the marker to the raw polled [lat,lon] here. The 100ms
      // dead-reckoning ticker is the single source of truth for marker motion —
      // it interpolates from the DR anchor that _drStorePosition just refreshed.
      // Snapping to the poll point on every apply fought the ticker and yanked
      // the plane backward each poll, which is what made motion stutter.
      // Only rebuild + apply the icon when its visual parameters actually
      // changed. Saves ~1500 divIcon SVG generations per poll on US-wide view.
      if (_adsbIconCache[a.icao] !== iconKey) {
        m.setIcon(adsbIcon(headRounded, fillColor, tagColor, a.category));
        _adsbIconCache[a.icao] = iconKey;
        // setIcon() replaces the marker's DOM element, which DROPS the native click
        // listener bound to the old element — so a plane that had turned became
        // unclickable (click "track" failed, no popup). Re-bind to the new element.
        _adsbAttachNativeClick(m);
      }
      // Live-update the popup stat values if it's open — keeps alt/spd/hdg/vs
      // ticking in real time without wiping toggle / button state.
      _adsbUpdateOpenPopupStats(a);
      const popup = m.getPopup();
      const isOpen = m.isPopupOpen();
      if (!popup || !popup.options || popup.options.className !== 'adsb-popup') {
        if (!isOpen) {
          m.unbindPopup();
          m.bindPopup(adsbPopup(a),
            {className: 'adsb-popup', maxWidth: 280, minWidth: 240, closeButton: true, autoPan: false});
        }
      } else if (!isOpen) {
        m.setPopupContent(adsbPopup(a));
      }
      // Click handlers + DOM-level override only need to be attached ONCE per
      // marker. The hot path (1000+ aircraft × poll) was wasting time off/on'ing
      // the same handlers every poll — this short-circuits all of that.
      if (!m.__adsbHandlersAttached) {
        m.__adsbHandlersAttached = true;
        m.on('click', _adsbMarkerClick);
        m.on('mousedown', _adsbMarkerMouseDown);
        m.on('touchstart', _adsbMarkerMouseDown);
        _adsbAttachNativeClick(m);
        const el = m.getElement && m.getElement();
        if (el && !el.__adsbClickBound) {
          el.__adsbClickBound = true;
          el.style.pointerEvents = 'auto';
          el.style.cursor = 'pointer';
          el.addEventListener('click', (e) => {
            e.stopPropagation();
            try { m.openPopup(); } catch (_) {}
          }, true);
        }
      }
    } else {
      // bubblingMouseEvents:false → marker clicks don't bubble to the map,
      // so map's closePopupOnClick=true doesn't close our popup right after
      // it opens. Map clicks on empty space STILL close popups (user wants
      // click-off-to-close behavior, just not from clicking the marker).
      const icon = adsbIcon(headRounded, fillColor, tagColor, a.category);
      _adsbIconCache[a.icao] = iconKey;
      const m = L.marker(ll, {
        icon,
        riseOnHover: true,
        keyboard: false,
        bubblingMouseEvents: false,
      }).bindPopup(adsbPopup(a),
                    {className: 'adsb-popup', maxWidth: 280, minWidth: 240, closeButton: true, autoPan: false});
      m.on('click', _adsbMarkerClick);
      m.on('mousedown', _adsbMarkerMouseDown);
      m.on('touchstart', _adsbMarkerMouseDown);
      m.addTo(adsbLayer);
      adsbMarkers[a.icao] = m;
      m.__adsbHandlersAttached = true;
      // Native DOM listener — bypasses Leaflet's click-detection heuristics
      // (which can decide a click was a drag if the cursor moves even one
      // pixel between mousedown and mouseup, eating the click silently).
      // This is the listener that ALWAYS fires when the user actually clicks.
      _adsbAttachNativeClick(m);
      // Inline-style override: Leaflet stamps `pointer-events: visiblePainted`
      // on `.leaflet-marker-icon`, which means transparent regions of the SVG
      // never register clicks. Inline styles always win over class CSS — this
      // forces the entire 32×32 hit box to capture clicks regardless of fill.
      // Plus a direct DOM-level click listener as a third safety net.
      const el = m.getElement && m.getElement();
      if (el && !el.__adsbClickBound) {
        el.__adsbClickBound = true;
        el.style.pointerEvents = 'auto';
        el.style.cursor = 'pointer';
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          try { m.openPopup(); } catch (_) {}
        }, true);
      }
    }
    // Apply the OSINT filter as show/hide on the (always-created) marker. Newly
    // created markers are addTo'd above so getElement() works for click binding;
    // here we pull a filtered-out one back off the layer. Toggling a filter just
    // flips this — instant, no refetch, no wipe-all flash.
    const _fm = adsbMarkers[a.icao];
    if (_fm) {
      if (_pass) {
        if (!adsbLayer.hasLayer(_fm)) { _fm.addTo(adsbLayer); _adsbAttachNativeClick(_fm); }  // re-add = new element → re-bind click
      }
      else if (adsbLayer.hasLayer(_fm)) adsbLayer.removeLayer(_fm);
    }
    // Trail visibility is opt-IN via shownAircraftPaths (default hidden), so we
    // do NOT touch any per-aircraft state here. The old code re-hid the path on
    // every sighting whose history had been reaped — which silently dropped the
    // user's "show path" choice. Nothing to do now; the ticker's reconcile shows
    // the trail iff the user opted this aircraft in.
    // Trails are owned EXCLUSIVELY by the dead-reckoning ticker, which appends
    // the marker's actual on-screen (extrapolated) position every 100ms. We must
    // NOT also push the raw poll point here: the ticker's extrapolated points run
    // AHEAD of the last real fix, so appending the (older, behind) poll point into
    // the same adsbHistory array made the polyline zigzag forward/back on every
    // poll — the "paths resetting in a loop" symptom. One writer = one clean trail
    // that matches the marker. (Color stays as set at trail creation; tags rarely
    // change, and the ticker uses the same per-tag color.)
  }
}

function _adsbApplyReap(seen) {
  // Grace-based reap. Every aircraft present in THIS snapshot is stamped as
  // freshly seen; an aircraft is only actually removed once it has been ABSENT
  // for longer than _ADSB_REAP_GRACE_MS. This is the core fix for "planes
  // randomly disappear and the count vanishes": a single empty/partial/failed
  // poll no longer wipes the map — markers ride out the gap on dead reckoning
  // and only leave when they're genuinely gone.
  const now = Date.now();
  seen.forEach(icao => { _adsbLastSeenMs[icao] = now; });
  Object.keys(adsbMarkers).forEach(icao => {
    if (seen.has(icao)) return;                                  // still present — keep
    if ((now - (_adsbLastSeenMs[icao] || 0)) < _ADSB_REAP_GRACE_MS) return;  // within grace — keep
    // Missing beyond the grace window — really gone. Remove the marker. If the
    // user opted this aircraft's PATH in, KEEP the trail + history frozen on the
    // map (a shown flight path persists until they clear or toggle it off).
    adsbLayer.removeLayer(adsbMarkers[icao]);
    delete adsbMarkers[icao];
    delete _adsbIconCache[icao];
    delete _adsbLastSeenMs[icao];
    // Drop it from the count's backing store too, so the in-view tally stops
    // counting an aircraft we've stopped tracking.
    delete _lastAdsbSnapshot[icao];
    // Let a re-appeared plane re-fetch its real flight trace instead of being
    // stuck with a fresh dead-reckoned straight line.
    _adsbTraceFetched.delete(icao);
    _adsbTraceQueued.delete(icao);
    if (shownAircraftPaths.has(icao)) return;   // keep the opted-in path frozen on the map
    if (adsbTrails[icao]) {
      adsbTrailLayer.removeLayer(adsbTrails[icao]);
      delete adsbTrails[icao];
    }
    delete adsbHistory[icao];
    delete _adsbDR[icao];
  });
}

function _adsbApplyFollowLocked() {
  if (lockedAircraft && _lastAdsbSnapshot[lockedAircraft]) {
    const a = _lastAdsbSnapshot[lockedAircraft];
    if (a.lat != null && a.lon != null) {
      // panTo (no zoom change) for smooth follow
      map.panTo([a.lat, a.lon], { animate: true, duration: 0.4, noMoveStart: true });
    }
  }
}

function _adsbApplyUpdateStatus(count) {
  const status = document.getElementById('adsbStatus');
  if (status) status.textContent = count + ' aircraft';
}

// Socket push from server — also reflects fetched count + last error in UI
let _lastAdsbUpdateMs = 0;
socket.on('adsb', (msg) => {
  if (!msg) return;
  _lastAdsbUpdateMs = Date.now();
  _lastAdsbSourceId = msg.source || _lastAdsbSourceId || '?';
  if (msg.aircraft) adsbApply(msg.aircraft);
  const main = document.getElementById('adsbMainStatus');
  const detail = document.getElementById('adsbStatus');
  if (main) main.textContent = (msg.count != null) ? (msg.count + ' ac') : 'on';
  if (detail) {
    if (msg.error) detail.textContent = '⚠ ' + msg.error;
    else detail.textContent = (msg.fetched || 0) + ' fetched · ' + (msg.count || 0) + ' tracked · ' + (msg.source || '?');
  }
  // Realtime aircraft-list + counts (counts reflect what's in current map view).
  // Render from the retained snapshot, NOT the raw push payload, so a transient
  // empty/partial frame can't blank the list or drop the count to 0.
  renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));
});
let _lastAdsbSourceId = '?';

// In-view counter helpers — recompute whenever the map view changes so the
// AIR TRAFFIC header + list header always match what the user is looking at.
function _adsbVisibleSnapshot() {
  const b = map.getBounds();
  const out = [];
  for (const icao in _lastAdsbSnapshot) {
    const a = _lastAdsbSnapshot[icao];
    if (!a || a.lat == null || a.lon == null) continue;
    if (!aircraftPassesFilter(a)) continue;
    if (b.contains([a.lat, a.lon])) out.push(a);
  }
  return out;
}
function _adsbRefreshInView() {
  const visible = _adsbVisibleSnapshot();
  const enabled = document.getElementById('adsbBoxEnableToggle')?.checked;
  const status = document.getElementById('adsbBoxStatus');
  if (status) {
    if (!enabled) status.textContent = '';
    else status.textContent = visible.length.toLocaleString();
  }
  const cnt = document.getElementById('adsbCount');
  if (cnt) {
    cnt.textContent = enabled
      ? (visible.length + ' in view · ' + (_lastAdsbSourceId || '?'))
      : '— off —';
  }
  // Re-render the list to match (cheap; capped at 50 rows)
  renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));
}
// Wire the listeners after `map` exists.
setTimeout(() => {
  if (typeof map === 'undefined') return;
  map.on('moveend', _adsbRefreshInView);
  map.on('zoomend', _adsbRefreshInView);
}, 0);

// (Removed flashing live-age indicator — too noisy. Aircraft motion itself
// is the proof of liveness now, courtesy of the dead-reckoning loop below.)

// ---------- Top-left AIR TRAFFIC panel (live aircraft list, current view only) ----------
function renderAdsbAircraftList(snapshot) {
  const c = document.getElementById('adsbAircraftList');
  if (!c) return;
  // Restrict to current map view bounds — top-left list shows only what's on screen
  const b = map.getBounds();
  const visible = (snapshot || []).filter(a => {
    if (!aircraftPassesFilter(a)) return false;
    if (a.lat == null || a.lon == null) return false;
    return b.contains([a.lat, a.lon]);
  });
  // Sync the panel header + count line with the in-view total
  const enabled = document.getElementById('adsbBoxEnableToggle')?.checked;
  const status = document.getElementById('adsbBoxStatus');
  if (status) status.textContent = enabled ? visible.length.toLocaleString() : '';
  const cnt = document.getElementById('adsbCount');
  if (cnt) cnt.textContent = enabled
    ? (visible.length + ' in view · ' + (_lastAdsbSourceId || '?'))
    : '— off —';
  if (visible.length === 0) {
    c.innerHTML = '<div style="color:#446666; font-style:italic; text-align:center; padding:6px;">no aircraft</div>';
    return;
  }
  // Sort: emergencies first, then by alt desc; cap to keep DOM light
  const order = {hijack:0, emergency:1, military:2, government:3, police:4, rotorcraft:5, uav:6, commercial:7, private:8, unknown:9};
  visible.sort((a, b) => {
    const ta = primaryTag(a.tags), tb = primaryTag(b.tags);
    const oa = order[ta] ?? 99, ob = order[tb] ?? 99;
    if (oa !== ob) return oa - ob;
    const aa = (a.alt_baro === 'ground' || a.alt_baro == null) ? -1 : a.alt_baro;
    const bb = (b.alt_baro === 'ground' || b.alt_baro == null) ? -1 : b.alt_baro;
    return bb - aa;
  });
  const max = 50;     // never render more than this — keeps the panel responsive
  const truncated = visible.length > max;
  // Compass conversion: 8-point cardinal abbreviation for headings
  const _compass = (h) => {
    if (h == null) return '';
    const dirs = ['N','NE','E','SE','S','SW','W','NW'];
    return dirs[Math.round(((h % 360) / 45)) % 8];
  };
  const rows = visible.slice(0, max).map(a => {
    const cs = a.callsign || '(no cs)';
    const onGnd = (a.alt_baro === 'ground') || a.on_ground;
    const alt = onGnd ? 'GND'
              : (a.alt_baro != null ? Math.round(a.alt_baro).toLocaleString() + 'ft' : '—');
    const vel = (a.velocity != null ? Math.round(a.velocity) + 'kt' : '');
    const hdg = (a.heading != null ? Math.round(a.heading) + '°' : '');
    const card = _compass(a.heading);
    // Vertical-rate glyph: climb/descend triangle if non-zero, else hairspace
    let vrGlyph = '';
    if (a.vert_rate != null && Math.abs(a.vert_rate) >= 64) {
      vrGlyph = a.vert_rate > 0
        ? '<span style="color:#88ff88;">▲</span>'
        : '<span style="color:#ff8888;">▼</span>';
    }
    // Emergency / squawk highlight (7500 hijack, 7600 nordo, 7700 emergency)
    const sq = (a.squawk || '').toString();
    const emergSq = (sq === '7500' || sq === '7600' || sq === '7700');
    const sqCell = sq
      ? ('<span style="color:' + (emergSq ? '#ff4444' : '#669988') + (emergSq ? '; font-weight:bold' : '') + ';">sq ' + sq + '</span>')
      : '';
    const pTag = primaryTag(a.tags);
    const tagMeta = adsbTagById[pTag] || {label: pTag.toUpperCase(), color:'#888'};
    const isLocked = (lockedAircraft === a.icao);
    const bg = isLocked ? 'rgba(0,80,120,0.5)' : 'rgba(0,20,30,0.4)';
    // Heading rotation arrow — small SVG that spins to match the aircraft
    const arrow = (a.heading != null)
      ? '<span style="display:inline-block; transform:rotate(' + Math.round(a.heading) + 'deg); color:' + tagMeta.color + '; font-weight:bold;">▲</span>'
      : '';
    // Compact 2-line row: callsign + tag chip on top, ICAO/squawk + alt/velocity
    // condensed below. Heading & vert-rate fold into one short line of glyphs
    // when they're notable (climbing/descending plane or emergency squawk).
    const tagChip = '<span style="color:' + tagMeta.color
                  + '; font-weight:600; letter-spacing:0.5px;">' + tagMeta.label + '</span>';
    const altText = '<span style="color:' + adsbAltColor(a.alt_baro)
                  + '; font-weight:600;">' + alt + '</span>';
    const velText = vel ? '<span style="color:#7a8b9a;">· ' + vel + '</span>' : '';
    const sqText = sqCell ? '<span> · ' + sqCell + '</span>' : '';
    const dirText = (hdg && a.heading != null)
      ? ' · <span style="color:#7a8b9a;">' + arrow + ' ' + hdg + '</span>'
      : (onGnd ? ' · <span style="color:#776644;">GND</span>' : '');
    // Compact row — 20% smaller fonts/padding than the previous pass to fit
    // ~25% more aircraft per panel without losing legibility.
    return '<div data-icao="' + a.icao + '" class="adsbRow"'
      + ' style="display:flex; justify-content:space-between; align-items:center;'
      +   ' padding:2px 7px 2px 6px; margin-bottom:1px;'
      +   ' border-left:2px solid ' + tagMeta.color + '; border-radius:0 3px 3px 0;'
      +   ' background:' + bg + '; cursor:pointer; line-height:1.2;'
      +   (emergSq ? ' box-shadow: inset 0 0 0 1px #ff4444;' : '') + '">'
      + '<div style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; flex:1;">'
        + '<div style="display:flex; gap:5px; align-items:baseline;">'
          + '<span style="color:#dde6ee; font-weight:600; font-size:0.78em; letter-spacing:0.2px; overflow:hidden; text-overflow:ellipsis;">' + cs + '</span>'
          + '<span style="color:' + tagMeta.color + '; font-weight:600; font-size:0.62em; letter-spacing:0.5px;">' + tagMeta.label + '</span>'
        + '</div>'
        + '<div style="color:#7a8b9a; font-size:0.62em; letter-spacing:0.2px; overflow:hidden; text-overflow:ellipsis;">'
          + (a.icao || '?').toUpperCase()
          + sqText
          + dirText
        + '</div>'
      + '</div>'
      + '<div style="text-align:right; flex-shrink:0; padding-left:6px; font-size:0.62em; line-height:1.2;">'
        + altText + ' ' + velText
        + (vrGlyph ? '<div style="font-size:0.85em;">' + vrGlyph + '</div>' : '')
      + '</div>'
      + '</div>';
  });
  c.innerHTML = rows.join('') + (truncated ? '<div style="color:#666; text-align:center; padding:4px;">+ ' + (visible.length - max) + ' more</div>' : '');
  // Click row → pan to aircraft AND open its popup so the user gets the same
  // info card as clicking the icon on the map. Shift-click locks/tracks without
  // popping the popup. Click an already-locked row to unlock.
  c.querySelectorAll('.adsbRow').forEach(row => {
    row.addEventListener('click', (ev) => {
      const icao = row.getAttribute('data-icao');
      const a = _lastAdsbSnapshot[icao];
      if (ev.shiftKey) {
        // Shift-click: tracking-only toggle, no popup
        if (lockedAircraft === icao) unlockAircraft();
        else lockAircraft(icao);
      } else {
        // Plain click: pan to it, then open the popup. Always re-pan so the
        // user can scan the list and "snap to" any aircraft regardless of
        // current map position.
        if (a && a.lat != null && a.lon != null) {
          const z = Math.max(map.getZoom(), 11);
          map.setView([a.lat, a.lon], z, {animate: true});
        }
        // Open the marker popup. Defer one tick so the pan animation kicks
        // off before the popup auto-aligns.
        if (adsbMarkers[icao]) {
          setTimeout(() => {
            try { adsbMarkers[icao].openPopup(); } catch (e) {}
          }, 50);
        }
      }
      renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));   // refresh highlight + counts
    });
  });
}

// Top-left panel collapse toggle (clicking the box header collapses the whole panel,
// but NOT when the user clicks the on/off toggle inside the header)
document.getElementById('adsbBoxHeader').addEventListener('click', (e) => {
  if (e.target.closest('label.switch') || e.target.id === 'adsbBoxEnableToggle') return;
  const content = document.getElementById('adsbBoxContent');
  const toggle = document.getElementById('adsbBoxToggle');
  const open = content.style.display !== 'none';
  content.style.display = open ? 'none' : 'block';
  toggle.textContent = open ? '[+]' : '[-]';
  localStorage.setItem('adsbBoxCollapsed', open ? '1' : '0');
});
if (localStorage.getItem('adsbBoxCollapsed') === '1') {
  document.getElementById('adsbBoxContent').style.display = 'none';
  document.getElementById('adsbBoxToggle').textContent = '[+]';
}

// Settings sub-section collapse
document.getElementById('adsbBoxSettingsToggle').addEventListener('click', () => {
  const s = document.getElementById('adsbBoxSettings');
  const t = document.getElementById('adsbBoxSettingsToggle');
  const open = s.style.display !== 'none';
  s.style.display = open ? 'none' : 'block';
  t.textContent = (open ? '▸' : '▾') + ' SETTINGS';
});

// Bidirectional enable: top-left toggle <-> setAdsbEnabled (which also syncs the hidden #adsbEnabled)
document.getElementById('adsbBoxEnableToggle').addEventListener('change', function() {
  setAdsbEnabled(this.checked);
});
// Make sure _adsbSetUiEnabled also reflects state into the new top-left toggle
const _origAdsbSetUiEnabled = _adsbSetUiEnabled;
_adsbSetUiEnabled = function(enabled) {
  _origAdsbSetUiEnabled(enabled);
  const t = document.getElementById('adsbBoxEnableToggle');
  if (t) t.checked = enabled;
};

// Source-conditional credentials boxes inside the top-left panel
function _adsbBoxSyncConditionals() {
  const src = document.getElementById('adsbBoxSource').value;
  document.getElementById('adsbBoxDump1090Box').style.display = (src === 'dump1090') ? 'block' : 'none';
  document.getElementById('adsbBoxBeastBox').style.display    = (src === 'beast')    ? 'flex'  : 'none';
  document.getElementById('adsbBoxOpenskyBox').style.display  = (src === 'opensky')  ? 'block' : 'none';
  document.getElementById('adsbBoxExchangeBox').style.display = (src === 'adsbexchange') ? 'block' : 'none';
}
document.getElementById('adsbBoxSource').addEventListener('change', _adsbBoxSyncConditionals);

// Pull current config into the top-left panel on load
async function loadAdsbBoxConfig() {
  try {
    const cfg = await (await fetch('/api/adsb/config')).json();
    document.getElementById('adsbBoxEnableToggle').checked = !!cfg.enabled;
    document.getElementById('adsbBoxSource').value = cfg.source || 'adsblol';
    document.getElementById('adsbBoxInterval').value = cfg.interval || 8;
    document.getElementById('adsbBoxBboxOnly').checked = !!cfg.bbox;
    document.getElementById('adsbBoxDump1090Url').value = cfg.dump1090_url || '';
    document.getElementById('adsbBoxBeastHost').value = cfg.beast_host || 'localhost';
    document.getElementById('adsbBoxBeastPort').value = cfg.beast_port || 30005;
    document.getElementById('adsbBoxOpenskyUser').value = cfg.opensky_user || '';
    document.getElementById('adsbBoxOpenskyPass').value = '';
    document.getElementById('adsbBoxExchangeKey').value = '';
    _adsbBoxSyncConditionals();
    // Sync the legacy toggle + state label so _adsbIsEnabled() returns true
    // on first poll without waiting for the user to click anything. Then
    // post the current map bbox + kick an immediate snapshot pull so the
    // map populates on PAGE LOAD, no hard reload required.
    if (cfg.enabled) {
      if (typeof _adsbSetUiEnabled === 'function') _adsbSetUiEnabled(true);
      try {
        const b = map.getBounds();
        await fetch('/api/adsb/config', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            bbox: [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]
          }),
        });
      } catch (_) {}
      // Wait briefly for the server's kick fetch to populate, then pull.
      setTimeout(() => { if (typeof _adsbPullSnapshot === 'function') _adsbPullSnapshot(); }, 1200);
    }
  } catch (e) { console.debug('adsbBox config load failed:', e); }
}
loadAdsbBoxConfig();

// Save handler for the top-left settings
document.getElementById('adsbBoxSaveBtn').addEventListener('click', async (ev) => {
  await withButtonLock(ev.currentTarget, 'SAVING...', async () => {
    const source   = document.getElementById('adsbBoxSource').value;
    const interval = parseInt(document.getElementById('adsbBoxInterval').value, 10) || 8;
    const bboxOnly = document.getElementById('adsbBoxBboxOnly').checked;
    let bbox = null;
    if (bboxOnly) {
      const b = map.getBounds();
      bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
    }
    const body = { source, interval, bbox };
    if (source === 'dump1090') body.dump1090_url = document.getElementById('adsbBoxDump1090Url').value.trim();
    if (source === 'opensky')  {
      body.opensky_user = document.getElementById('adsbBoxOpenskyUser').value.trim();
      const pw = document.getElementById('adsbBoxOpenskyPass').value;
      if (pw) body.opensky_pass = pw;
    }
    if (source === 'adsbexchange') {
      const k = document.getElementById('adsbBoxExchangeKey').value.trim();
      if (k) body.adsbx_key = k;
    }
    if (source === 'beast') {
      body.beast_host = document.getElementById('adsbBoxBeastHost').value.trim() || 'localhost';
      body.beast_port = parseInt(document.getElementById('adsbBoxBeastPort').value, 10) || 30005;
    }
    let r, j;
    try {
      r = await fetch('/api/adsb/config', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body),
      });
      j = await r.json();
    } catch (e) { alert('save failed: ' + e); return; }
    if (!r.ok) { alert(j.error || 'save failed'); return; }
  });
});

// Filter chips inside the top-left panel — same set + behaviour as the old chips
function renderAdsbBoxFilterChips() {
  const c = document.getElementById('adsbBoxFilterChips');
  if (!c) return;
  c.innerHTML = '';
  ADSB_TAGS.forEach(t => {
    const on = adsbVisible.has(t.id);
    const chip = document.createElement('span');
    chip.style.cssText =
      'display:inline-block; padding:1px 5px; cursor:pointer; user-select:none;'
      + 'border:1px solid ' + t.color + ';'
      + 'color:' + (on ? '#000' : t.color) + ';'
      + 'background:' + (on ? t.color : 'transparent') + ';'
      + 'font-size:0.85em; letter-spacing:0.5px; border-radius:3px;';
    chip.textContent = t.label;
    chip.addEventListener('click', () => {
      if (adsbVisible.has(t.id)) adsbVisible.delete(t.id);
      else adsbVisible.add(t.id);
      renderAdsbBoxFilterChips();
      renderAdsbFilterChips();   // keep the other chip copy in sync
      if (typeof _adsbIsEnabled === 'function' && !_adsbIsEnabled()) return;
      // Apply the filter to markers ALREADY on the map, in place — show/hide each
      // and reconcile its trail. The old path DELETED every marker + trail and
      // refetched the whole feed on each click, which flashed the fleet AND wiped
      // shown flight paths (so per-type path filtering looked broken). This keeps
      // type filtering — markers AND paths — instant and stable.
      _adsbReapplyFilter();
      renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));   // refresh list + count
      if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();
    });
    c.appendChild(chip);
  });
}
renderAdsbBoxFilterChips();

// ---------- Layout relocation: move existing right-sidebar content into the
// new top-center GEOFENCING float and bottom-center SETTINGS float ----------
(function relocateLayout() {
  // 1. Move all geofence content from the right sidebar into the top-center float
  const geoSrc = document.getElementById('geofencePanel');
  const geoDst = document.getElementById('geofenceFloatContent');
  if (geoSrc && geoDst) {
    // Clone each child into the float so existing IDs (geofenceList, drawPolygonBtn,
    // geofenceAlertList) move with their wired handlers
    while (geoSrc.firstChild) geoDst.appendChild(geoSrc.firstChild);
    // Hide the now-empty geofence panel wrapper in the right sidebar
    const wrap = document.getElementById('geofenceToggle');
    if (wrap && wrap.parentElement) wrap.parentElement.style.display = 'none';
  }

  // 2. Move BASEMAP + OFFLINE MAPPING into the new bottom-RIGHT MAP LAYER float
  //    (decoupled from general SETTINGS; map config has its own dedicated panel)
  const mapDst = document.getElementById('mapLayerFloatContent');
  if (mapDst) {
    // BASEMAP: walk up from the layerSelect to the bordered container
    const basemapSelect = document.getElementById('layerSelect');
    if (basemapSelect) {
      let bm = basemapSelect.parentElement;
      while (bm && !(bm.style && bm.style.border && /lime/.test(bm.style.border))) {
        if (!bm.parentElement) break;
        bm = bm.parentElement;
      }
      if (bm) {
        bm.style.margin = '0 0 8px 0';
        bm.style.borderColor = '#225522';
        mapDst.appendChild(bm);
      }
    }
    // OFFLINE MAPPING (cache panel)
    const offPanel = document.getElementById('offlineMappingPanel');
    if (offPanel) {
      offPanel.style.margin = '0';
      offPanel.style.border = '1px dashed #225522';
      mapDst.appendChild(offPanel);
    }
  }

  // 3. Move USB port config into the bottom-CENTER SETTINGS float (general settings).
  //    Also relocate the USB-connection status pill from the Drones panel to here so
  //    the connection live-state lives next to the port selector that controls it.
  const setDst = document.getElementById('settingsFloatContent');
  if (setDst) {
    // Move the always-visible USB status pill (originally inside #filterBox) up here
    const filterBox = document.getElementById('filterBox');
    const usbPill = filterBox && filterBox.querySelector('.alwaysVisible');
    if (usbPill) {
      // Tag it so we can hide it from filterHeader CSS that pulled it inline
      usbPill.classList.remove('alwaysVisible');
      usbPill.style.margin = '0 0 6px 0';
      usbPill.style.maxWidth = '100%';
      setDst.appendChild(usbPill);
    }
    // USB port config block
    const portBtn = document.getElementById('portRefreshBtn');
    if (portBtn) {
      const section = portBtn.closest('div[style*="border-top:1px dashed"]');
      if (section) {
        section.style.borderTop = 'none';
        section.style.paddingTop = '0';
        section.style.marginTop = '0';
        setDst.appendChild(section);
      }
    }
    // Auto-populate the port dropdowns now (they're normally lazy-loaded on Settings open;
    // do it eagerly so the connected port shows the moment the user expands the panel)
    if (typeof refreshPorts === 'function') refreshPorts();

    // ---- Add a parallel SDR section for ADS-B receivers ----
    const sdrSection = document.createElement('div');
    sdrSection.id = 'sdrConfigSection';
    sdrSection.style.cssText = 'margin-top:10px; padding-top:8px; border-top:1px dashed #225522;';
    sdrSection.innerHTML = (
      '<div style="font-size:0.95em; color:#aaeeff; margin-bottom:3px; font-weight:bold; letter-spacing:1px;">SDR · ADS-B RECEIVERS</div>'
      + '<div style="font-size:0.85em; color:#5588aa; margin-bottom:6px;">Network feed or local HackRF/RTL-SDR/AirSpy via dump1090, readsb, or Beast.</div>'
      // Big binary mode toggle: ONLINE (network sources) vs LOCAL SDR. Sets default
      // network source to adsb.lol when toggled to ONLINE.
      + '<div style="display:flex; gap:0; margin-bottom:6px; border:1px solid #00aaff; border-radius:3px; overflow:hidden;">'
      + '  <button data-mode="online" id="sdrModeOnline" style="flex:1; padding:5px; background:#001a2a; border:none; color:#aaeeff; font-family:monospace; font-size:0.95em; cursor:pointer;">⌒ ONLINE</button>'
      + '  <button data-mode="local"  id="sdrModeLocal"  style="flex:1; padding:5px; background:#001a2a; border:none; color:#aaeeff; font-family:monospace; font-size:0.95em; cursor:pointer; border-left:1px solid #00aaff;">⎘ LOCAL SDR</button>'
      + '</div>'
      + '<label id="sdrOnlineSourceLabel" style="display:none; font-size:0.85em; color:#88aaff;">Network source:'
      + '  <select id="sdrOnlineSourceSelect" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;">'
      + '    <option value="adsblol">adsb.lol (free, no key)</option>'
      + '    <option value="adsbfi">adsb.fi (free, no key)</option>'
      + '    <option value="airplaneslive">airplanes.live (free, no key)</option>'
      + '    <option value="opensky">OpenSky (free, optional auth)</option>'
      + '    <option value="adsbexchange">ADS-B Exchange (RapidAPI key)</option>'
      + '  </select>'
      + '</label>'
      + '<label id="sdrModeSelectLabel" style="display:none; font-size:0.85em; color:#88aaff; margin-top:4px;">Local mode:'
      + '  <select id="sdrModeSelect" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;">'
      + '    <option value="dump1090">Local SDR · dump1090 / readsb / tar1090 / PiAware</option>'
      + '    <option value="beast">Beast TCP raw feed (pyModeS)</option>'
      + '  </select>'
      + '</label>'
      + '<div id="sdrDump1090Fields" style="margin-top:4px;">'
      + '  <label style="display:block; font-size:0.85em; color:#88aaff;">Preset:'
      + '    <select id="sdrDump1090Preset" style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"></select>'
      + '  </label>'
      + '  <label style="display:block; font-size:0.85em; color:#88aaff; margin-top:3px;">JSON URL:'
      + '    <input id="sdrDump1090Url" type="text" placeholder="http://localhost:8080/data/aircraft.json"'
      + '           style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>'
      + '  </label>'
      + '</div>'
      + '<div id="sdrBeastFields" style="display:none; margin-top:4px;">'
      + '  <div style="display:flex; gap:4px;">'
      + '    <label style="flex:2; min-width:0; font-size:0.85em; color:#88aaff;">Host'
      + '      <input id="sdrBeastHost" type="text" placeholder="localhost"'
      + '             style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>'
      + '    </label>'
      + '    <label style="flex:1; min-width:0; font-size:0.85em; color:#88aaff;">Port'
      + '      <input id="sdrBeastPort" type="number" placeholder="30005" min="1" max="65535"'
      + '             style="width:100%; box-sizing:border-box; background:rgba(51,51,51,0.7); color:#aaeeff; border:1px solid #00aaff; font-family:monospace; font-size:0.95em; padding:2px;"/>'
      + '    </label>'
      + '  </div>'
      + '  <div style="font-size:0.85em; color:#ffaa44; margin-top:2px;">Requires <code>pip install pyModeS</code>.</div>'
      + '</div>'
      + '<div id="sdrStatus" style="margin-top:4px; font-size:0.85em; color:#88aaff; text-align:center; min-height:1.2em;"></div>'
      + '<button id="sdrApplyBtn" style="margin-top:4px; width:100%; padding:4px; background:#001a2a; border:1px solid #00aaff; color:#aaeeff; font-family:monospace; font-size:0.95em; border-radius:3px; cursor:pointer;">APPLY SDR CONFIG</button>'
    );
    setDst.appendChild(sdrSection);

    // Wire SDR section
    const sdrModeSelect = document.getElementById('sdrModeSelect');
    const sdrDump1090Fields = document.getElementById('sdrDump1090Fields');
    const sdrBeastFields = document.getElementById('sdrBeastFields');
    const NETWORK_SOURCES = ['adsblol','adsbfi','airplaneslive','opensky','adsbexchange'];
    let _sdrModeKind = 'online';   // 'online' | 'local'
    function _sdrSetModeKind(kind) {
      _sdrModeKind = kind;
      const onlineBtn = document.getElementById('sdrModeOnline');
      const localBtn  = document.getElementById('sdrModeLocal');
      onlineBtn.style.background = (kind === 'online') ? '#003355' : '#001a2a';
      onlineBtn.style.color      = (kind === 'online') ? '#fff'    : '#aaeeff';
      localBtn.style.background  = (kind === 'local')  ? '#003355' : '#001a2a';
      localBtn.style.color       = (kind === 'local')  ? '#fff'    : '#aaeeff';
      document.getElementById('sdrOnlineSourceLabel').style.display = (kind === 'online') ? 'block' : 'none';
      document.getElementById('sdrModeSelectLabel').style.display   = (kind === 'local')  ? 'block' : 'none';
      _sdrSyncFields();
    }
    function _sdrSyncFields() {
      if (_sdrModeKind !== 'local') {
        sdrDump1090Fields.style.display = 'none';
        sdrBeastFields.style.display    = 'none';
        return;
      }
      const m = sdrModeSelect.value;
      sdrDump1090Fields.style.display = (m === 'dump1090') ? 'block' : 'none';
      sdrBeastFields.style.display    = (m === 'beast')    ? 'block' : 'none';
    }
    sdrModeSelect.addEventListener('change', _sdrSyncFields);
    document.getElementById('sdrModeOnline').addEventListener('click', () => _sdrSetModeKind('online'));
    document.getElementById('sdrModeLocal').addEventListener('click',  () => _sdrSetModeKind('local'));

    // Populate dump1090 presets + load current config
    fetch('/api/adsb/sources').then(r => r.json()).then(d => {
      const sel = document.getElementById('sdrDump1090Preset');
      if (sel && d.dump1090_presets) {
        sel.innerHTML = d.dump1090_presets.map(p => '<option value="' + p.url + '">' + p.label + '</option>').join('');
      }
    }).catch(() => {});
    fetch('/api/adsb/config').then(r => r.json()).then(cfg => {
      // Mode kind: online if source is a network feed, local if dump1090/beast
      if (cfg.source === 'beast' || cfg.source === 'dump1090') {
        _sdrSetModeKind('local');
        sdrModeSelect.value = cfg.source;
      } else {
        _sdrSetModeKind('online');
        if (NETWORK_SOURCES.includes(cfg.source)) {
          document.getElementById('sdrOnlineSourceSelect').value = cfg.source;
        }
      }
      document.getElementById('sdrDump1090Url').value = cfg.dump1090_url || 'http://localhost:8080/data/aircraft.json';
      document.getElementById('sdrBeastHost').value   = cfg.beast_host   || 'localhost';
      document.getElementById('sdrBeastPort').value   = cfg.beast_port   || 30005;
    }).catch(() => {});

    // Preset change → fill URL field
    document.addEventListener('change', (e) => {
      if (e.target && e.target.id === 'sdrDump1090Preset') {
        document.getElementById('sdrDump1090Url').value = e.target.value;
      }
    });

    document.getElementById('sdrApplyBtn').addEventListener('click', async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true; btn.textContent = 'SAVING...';
      try {
        let body;
        if (_sdrModeKind === 'online') {
          body = { source: document.getElementById('sdrOnlineSourceSelect').value };
        } else {
          const mode = sdrModeSelect.value;
          body = { source: mode };
          if (mode === 'dump1090') {
            body.dump1090_url = document.getElementById('sdrDump1090Url').value.trim();
          } else {
            body.beast_host = document.getElementById('sdrBeastHost').value.trim() || 'localhost';
            body.beast_port = parseInt(document.getElementById('sdrBeastPort').value, 10) || 30005;
          }
        }
        // If user sets a source while ADS-B is already enabled, the server picks it up
        // on the next poll; we also kick a fresh fetch so the map updates immediately.
        const r = await fetch('/api/adsb/config', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body),
        });
        const j = await r.json();
        const st = document.getElementById('sdrStatus');
        if (!r.ok) { st.textContent = '⚠ ' + (j.error || 'failed'); st.style.color = '#ff8888'; }
        else { st.textContent = 'saved · ' + body.source; st.style.color = '#88ff88'; }
      } catch (e) {
        document.getElementById('sdrStatus').textContent = '⚠ ' + e;
      } finally {
        btn.disabled = false; btn.textContent = 'APPLY SDR CONFIG';
      }
    });
    // Hide the now-empty Settings expansion in the right sidebar
    const setToggle = document.getElementById('settingsToggle');
    if (setToggle && setToggle.parentElement) setToggle.parentElement.style.display = 'none';
  }

  // 4. Move DOWNLOAD LOGS (drone exports) into the right Drones sidebar so they
  //    live inside #filterContent (collapse with the panel) — drone-only exports
  //    belong with drone data
  const downloadCsvBtn = document.getElementById('downloadCsv');
  const filterContent = document.getElementById('filterContent');
  if (downloadCsvBtn && filterContent) {
    // The download section is the parent that contains both the Session and Cumulative blocks
    let dlSection = downloadCsvBtn.parentElement;
    while (dlSection && !dlSection.querySelector('#downloadCumulativeKml')) {
      dlSection = dlSection.parentElement;
    }
    if (dlSection) {
      // Wrap with a header so it has its own visual section
      const wrap = document.createElement('div');
      wrap.style.cssText = 'margin:10px 8px 0 8px; padding:6px; border:1px solid lime; background:rgba(0,0,0,0.5); border-radius:4px; box-sizing:border-box; font-family:monospace; font-size:0.75em; color:lime;';
      wrap.innerHTML = '<div style="color:#aaffaa; font-weight:bold; letter-spacing:1px; text-align:center; margin-bottom:4px;">DRONE EXPORTS</div>';
      filterContent.appendChild(wrap);
      wrap.appendChild(dlSection);
      dlSection.style.background = 'transparent';
      dlSection.style.padding = '0';
      dlSection.style.border = 'none';
      dlSection.style.margin = '0';
    }
  }
})();

// Wire the float-box toggle behavior + restore collapse state from localStorage
function _wireFloat(headerId, contentId, toggleId, storageKey, openSym, closedSym) {
  const h = document.getElementById(headerId);
  const c = document.getElementById(contentId);
  const t = document.getElementById(toggleId);
  if (!h || !c || !t) return;
  const wasOpen = localStorage.getItem(storageKey) === '1';
  c.style.display = wasOpen ? 'block' : 'none';
  t.textContent = wasOpen ? openSym : closedSym;
  h.addEventListener('click', () => {
    const open = c.style.display !== 'none';
    c.style.display = open ? 'none' : 'block';
    t.textContent = open ? closedSym : openSym;
    localStorage.setItem(storageKey, open ? '0' : '1');
  });
}
_wireFloat('geofenceFloatHeader', 'geofenceFloatContent', 'geofenceFloatToggle', 'geofenceFloatOpen', '[-]', '[+]');
_wireFloat('settingsFloatHeader', 'settingsFloatContent', 'settingsFloatToggle', 'settingsFloatOpen', '[-]', '[+]');
_wireFloat('mapLayerFloatHeader', 'mapLayerFloatContent', 'mapLayerFloatToggle', 'mapLayerFloatOpen', '[-]', '[+]');

// ---------- Header status descriptors — give every collapsed panel a glanceable summary ----------
function updatePanelDescriptions() {
  // ADS-B — always show in-view count (consistent with the AIR TRAFFIC list).
  // Don't overwrite with a total here; that would fight with the moveend/render
  // path that keeps the badge accurate for the current viewport.
  const adsbStatus = document.getElementById('adsbBoxStatus');
  if (adsbStatus) {
    const enabled = document.getElementById('adsbBoxEnableToggle')?.checked;
    if (!enabled) {
      adsbStatus.textContent = '';
    } else if (typeof _adsbVisibleSnapshot === 'function') {
      const inView = _adsbVisibleSnapshot().length;
      const total  = Object.keys(_lastAdsbSnapshot || {}).length;
      adsbStatus.textContent = total === 0 ? '...' : inView.toLocaleString();
    }
  }
  // GEOFENCING
  const geoStatus = document.getElementById('geofenceFloatStatus');
  if (geoStatus) {
    const fenceCount = Object.keys(geofences || {}).length;
    const alertCount = (typeof GEOFENCE_ALERTS_LEN === 'number') ? GEOFENCE_ALERTS_LEN : 0;
    geoStatus.textContent = fenceCount + ' fences';
  }
  // SETTINGS — show USB connection state in the descriptor since USB now lives here
  const setStatus = document.getElementById('settingsFloatStatus');
  if (setStatus) {
    fetch('/api/serial_status').then(r => r.json()).then(d => {
      const statuses = d.statuses || {};
      const connected = Object.values(statuses).filter(Boolean).length;
      const total = Object.keys(statuses).length;
      if (total === 0) {
        setStatus.textContent = 'no usb';
        setStatus.style.color = '#888';
      } else {
        setStatus.textContent = connected + '/' + total + ' usb';
        setStatus.style.color = (connected > 0) ? '#88ff88' : '#ff8888';
      }
    }).catch(() => {});
  }
  // MAP LAYER
  const mapStatus = document.getElementById('mapLayerFloatStatus');
  if (mapStatus) {
    const sel = document.getElementById('layerSelect');
    const v = sel ? sel.value : '';
    let label = 'OSM';
    if (sel && sel.selectedOptions[0]) label = sel.selectedOptions[0].textContent.trim();
    if (label.length > 24) label = label.slice(0, 22) + '..';
    const offline = v.indexOf('offline:') === 0 ? 'OFFLINE' : 'ONLINE';
    mapStatus.textContent = label + ' · ' + offline;
  }
}
// Refresh descriptions every 2s + on key events
setInterval(updatePanelDescriptions, 2000);
document.getElementById('adsbBoxEnableToggle')?.addEventListener('change', updatePanelDescriptions);
document.getElementById('layerSelect')?.addEventListener('change', updatePanelDescriptions);
updatePanelDescriptions();

// ---------- Bulletproof bidirectional ADS-B toggle ----------
// Two checkboxes: #adsbMainToggle (compact, main UI) and #adsbEnabled (in Settings).
// Both call setAdsbEnabled() which: syncs both, posts to server, handles failures by
// reverting visuals, and queues the latest state if a save is already in flight so
// rapid toggles always settle on the user's final intent.
let _adsbToggleInflight = false;
let _adsbToggleQueued = null;     // null = nothing queued; bool = next desired state

function _adsbSetUiEnabled(enabled) {
  const main = document.getElementById('adsbMainToggle');
  const settings = document.getElementById('adsbEnabled');
  const mainStatus = document.getElementById('adsbMainStatus');
  const detailStatus = document.getElementById('adsbStatus');
  const stateLbl = document.getElementById('adsbBoxStateLabel');
  if (main) main.checked = enabled;
  if (settings) settings.checked = enabled;
  if (mainStatus) {
    mainStatus.textContent = enabled ? 'on' : 'off';
    mainStatus.style.color = enabled ? '#00ffaa' : '#88aaff';
  }
  if (detailStatus) detailStatus.textContent = enabled ? '— enabled, polling —' : '— off —';
  if (stateLbl) {
    stateLbl.textContent = enabled ? 'ON' : 'OFF';
    // Cyan accent for ADS-B (matches the AIR TRAFFIC panel border + popup card).
    stateLbl.style.color = enabled ? '#88c8ff' : '#586978';
    stateLbl.style.borderColor = enabled ? 'rgba(136,200,255,0.55)' : 'rgba(255,255,255,0.10)';
    stateLbl.style.background = enabled ? 'rgba(136,200,255,0.10)' : 'transparent';
  }
}

async function setAdsbEnabled(enabled) {
  // Always apply visual state immediately so the user gets feedback
  _adsbSetUiEnabled(enabled);

  // If an in-flight save exists, queue this state and return — when the inflight
  // save resolves, it will pick up the latest queued value.
  if (_adsbToggleInflight) {
    _adsbToggleQueued = enabled;
    return;
  }
  _adsbToggleInflight = true;
  try {
    // When turning ON, bbox-restrict to the user's current map view by default —
    // saves bandwidth, respects providers, and gives instant local results.
    // Setting bbox to null when turning OFF is unnecessary; we leave config alone.
    const body = { enabled };
    if (enabled) {
      const b = map.getBounds();
      body.bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
      // Mirror to the in-settings 'bbox-only' checkbox so the UI tells the truth
      const bboxOnly = document.getElementById('adsbBboxOnly');
      if (bboxOnly) bboxOnly.checked = true;
    }
    const r = await fetch('/api/adsb/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      let msg = 'save failed';
      try { msg = (await r.json()).error || msg; } catch(e){}
      throw new Error(msg);
    }
    if (!enabled) {
      // Clear all rendered aircraft + trails immediately + the in-memory
      // snapshot, so panel counts / lists / dead-reckoning all flip to empty.
      Object.keys(adsbMarkers).forEach(icao => {
        adsbLayer.removeLayer(adsbMarkers[icao]); delete adsbMarkers[icao];
      });
      Object.keys(adsbTrails).forEach(icao => {
        adsbTrailLayer.removeLayer(adsbTrails[icao]); delete adsbTrails[icao];
      });
      Object.keys(adsbHistory).forEach(icao => delete adsbHistory[icao]);
      Object.keys(_lastAdsbSnapshot).forEach(k => delete _lastAdsbSnapshot[k]);
      // Refresh the panel header count + in-view list to show 0 / "off"
      try { renderAdsbAircraftList([]); } catch (_) {}
      const status = document.getElementById('adsbBoxStatus');
      if (status) status.textContent = '';
      const cnt = document.getElementById('adsbCount');
      if (cnt) cnt.textContent = '— OFF —';
    } else {
      // Pull initial snapshot so the user sees aircraft within the next poll cycle
      try {
        const snap = await (await fetch('/api/adsb/aircraft')).json();
        if (snap && snap.aircraft) adsbApply(snap.aircraft);
      } catch(e) {}
    }
  } catch (e) {
    // Revert visuals — server didn't accept the toggle
    _adsbSetUiEnabled(!enabled);
    const detailStatus = document.getElementById('adsbStatus');
    if (detailStatus) detailStatus.textContent = '— save failed: ' + e.message + ' —';
    console.warn('ADS-B toggle save failed:', e);
  } finally {
    _adsbToggleInflight = false;
    // If the user clicked again while we were saving, run the final desired state.
    if (_adsbToggleQueued !== null) {
      const next = _adsbToggleQueued;
      _adsbToggleQueued = null;
      // Only refire if it differs from what we just persisted
      if (next !== enabled) setAdsbEnabled(next);
    }
  }
}

// Wire both checkboxes to the same handler. Use 'change' so keyboard-tab + space works too.
document.getElementById('adsbMainToggle').addEventListener('change', function() {
  setAdsbEnabled(this.checked);
});
// (the in-settings checkbox is wired below in the existing handler block — we re-bind to the new flow)
document.getElementById('adsbEnabled').addEventListener('change', function() {
  setAdsbEnabled(this.checked);
});

// Follow-map: re-post the current bbox on EVERY pan/zoom so the server-side
// poller refetches the new viewport's aircraft immediately (the POST handler
// kicks a fresh fetch + socket emit). Checks either the new top-left panel
// toggle OR the legacy main toggle so it works regardless of which UI the
// user is interacting with. Bbox-only gate dropped — server happily accepts
// a bbox even when world-wide mode was previously set, and on a fresh viewport
// the user almost certainly wants the new area's aircraft.
const _adsbFollowMap = debounce(async () => {
  const mainEnabled = (document.getElementById('adsbMainToggle')?.checked) ||
                      (document.getElementById('adsbBoxEnableToggle')?.checked) ||
                      (document.getElementById('adsbEnabled')?.checked);
  if (!mainEnabled) return;
  if (_adsbToggleInflight) return;  // toggle handler will refresh anyway
  const b = map.getBounds();
  // Pad slightly outward, then clamp lat to the Web-Mercator-safe band and
  // wrap lon into [-180, 180]. This makes ADS-B work *anywhere on Earth*:
  // pan to Tokyo, the Pacific, the Arctic — server gets a normalized bbox
  // every time and the upstream API converts it to lat/lon/radius.
  const lonSpan = b.getEast() - b.getWest();
  const latSpan = b.getNorth() - b.getSouth();
  const padW = lonSpan * 0.05, padH = latSpan * 0.05;
  const clampLat = (x) => Math.max(-85.06, Math.min(85.06, x));
  const wrapLon = (x) => {
    while (x > 180)  x -= 360;
    while (x < -180) x += 360;
    return x;
  };
  const bbox = [
    wrapLon(b.getWest() - padW),
    clampLat(b.getSouth() - padH),
    wrapLon(b.getEast() + padW),
    clampLat(b.getNorth() + padH),
  ];
  try {
    await fetch('/api/adsb/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ bbox }),
    });
    // The server's config-POST handler kicks an async fetch, but socketio
    // emits from background threads are unreliable in some Flask-SocketIO
    // setups (the regular poller emit also fails to reach the client past
    // the initial connection). So we drive the snapshot client-side: wait
    // a moment for the kick-fetch to populate the server cache, then GET
    // /api/adsb/aircraft and apply it. Reliable, viewport-aware, no socket
    // delivery quirks.
    setTimeout(_adsbPullSnapshot, 1200);
  } catch (e) { console.debug('adsb follow-map failed:', e); }
}, 250);
map.on('moveend zoomend', _adsbFollowMap);
// Helper: ADS-B enabled regardless of which UI toggle the user clicked.
function _adsbIsEnabled() {
  return (document.getElementById('adsbMainToggle')?.checked) ||
         (document.getElementById('adsbBoxEnableToggle')?.checked) ||
         (document.getElementById('adsbEnabled')?.checked) || false;
}
// ── ADS-B snapshot puller ──
// Centralized, bullet-proof against bog:
//   1. Asks the server to filter by the current viewport bbox (so a US-wide
//      pan with 3500 aircraft returns only the ~200 you can see).
//   2. Sends `fields=mini` to strip ~half the JSON wire size.
//   3. Returns EVERY aircraft in the viewport — no cap.
//   4. Single-flight: if a fetch is in flight, skip — don't queue up overlapping
//      requests that all race to update the map.
//   5. Re-checks enabled state both before and after the network round trip.
let _adsbPullInflight = false;
async function _adsbPullSnapshot() {
  if (!_adsbIsEnabled()) return;
  if (_adsbPullInflight) return;
  _adsbPullInflight = true;
  try {
    const b = map.getBounds();
    const bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].join(',');
    // limit=0 → no cap. Server returns every aircraft in the viewport bbox.
    const url = `/api/adsb/aircraft?bbox=${encodeURIComponent(bbox)}&fields=mini&limit=0`;
    const snap = await (await fetch(url)).json();
    if (!_adsbIsEnabled()) return;
    if (snap && snap.aircraft) {
      adsbApply(snap.aircraft);
      // Render from the retained snapshot (post-apply/grace), not the raw poll
      // payload — a single empty/partial response must not blank the count.
      renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));
      _lastAdsbSourceId = snap.source || _lastAdsbSourceId;
      _lastAdsbUpdateMs = Date.now();
    }
  } catch (_) {
  } finally {
    _adsbPullInflight = false;
  }
}
// Periodic refresh — tight cadence for realtime feel. Chunked-rAF marker
// placement (in adsbApply) and defer-during-motion keep the map smooth even
// at 3-4s polls with thousands of aircraft.
//   - Zoom 0-5  = world  → 4s (massive viewport, fetch latency dominates)
//   - Zoom 6-8  = region → 3s
//   - Zoom 9+   = city   → 2s (few aircraft, snap fast)
let _adsbPollTimer = null;
function _adsbScheduleNextPoll() {
  if (_adsbPollTimer) { clearTimeout(_adsbPollTimer); _adsbPollTimer = null; }
  const zoom = (typeof map !== 'undefined') ? map.getZoom() : 8;
  const delay = zoom < 6 ? 4000 : (zoom < 9 ? 3000 : 2000);
  _adsbPollTimer = setTimeout(async () => {
    await _adsbPullSnapshot();
    _adsbScheduleNextPoll();
  }, delay);
}
_adsbScheduleNextPoll();
// Top-left aircraft list re-renders on every pan/zoom so it stays restricted
// to whatever's currently in the map viewport.
map.on('moveend zoomend', () => {
  // Render from the in-memory snapshot — no network call, just reflows the list
  renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));
  // The in-view set changed, so the per-category path chips' counts + active
  // (all-shown) state need refreshing too.
  if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();
});

// Initial load + setup
async function loadAdsbConfig() {
  try {
    const cfg = await (await fetch('/api/adsb/config')).json();
    _adsbSetUiEnabled(!!cfg.enabled);
    document.getElementById('adsbSource').value = cfg.source || 'adsblol';
    document.getElementById('adsbInterval').value = cfg.interval || 8;
    document.getElementById('adsbBboxOnly').checked = !!cfg.bbox;
    document.getElementById('adsbDump1090Url').value = cfg.dump1090_url || '';
    document.getElementById('adsbOpenskyUser').value = cfg.opensky_user || '';
    document.getElementById('adsbOpenskyPass').value = '';   // never preload masked
    document.getElementById('adsbExchangeKey').value = '';
    document.getElementById('adsbBeastHost').value = cfg.beast_host || 'localhost';
    document.getElementById('adsbBeastPort').value = cfg.beast_port || 30005;
    syncAdsbConditionalBoxes();
    // Initial snapshot for late-joining clients
    const snap = await (await fetch('/api/adsb/aircraft')).json();
    if (snap && snap.aircraft) {
      adsbApply(snap.aircraft);
      renderAdsbAircraftList(snap.aircraft);   // populate top-left panel immediately
      const cnt = document.getElementById('adsbCount');
      if (cnt) cnt.textContent = (snap.count || 0) + ' tracked · ' + (snap.source || '?');
    }
  } catch (e) { console.debug('adsb config load failed:', e); }
}

// Eagerly load config on page boot so the main toggle reflects truth (in case the
// user enabled ADS-B previously and just refreshed the page).
loadAdsbConfig();

function renderAdsbFilterChips() {
  const c = document.getElementById('adsbFilterChips');
  if (!c) return;
  c.innerHTML = '';
  ADSB_TAGS.forEach(t => {
    const on = adsbVisible.has(t.id);
    const chip = document.createElement('span');
    chip.dataset.tag = t.id;
    chip.style.cssText =
      'display:inline-block; padding:2px 6px; cursor:pointer; user-select:none;'
      + 'border:1px solid ' + t.color + ';'
      + 'color:' + (on ? '#000' : t.color) + ';'
      + 'background:' + (on ? t.color : 'transparent') + ';'
      + 'font-size:0.85em; letter-spacing:0.5px; border-radius:3px;';
    chip.textContent = t.label;
    chip.addEventListener('click', () => {
      if (adsbVisible.has(t.id)) adsbVisible.delete(t.id);
      else adsbVisible.add(t.id);
      renderAdsbFilterChips();
      // If ADS-B is OFF, just remember the filter choice — don't redraw.
      if (typeof _adsbIsEnabled === 'function' && !_adsbIsEnabled()) return;
      // Apply the filter to the markers ALREADY on the map, in place. The old
      // path fetched the whole feed and DELETED every marker before re-adding —
      // so each filter click flashed all 5,000+ planes off and back on (the
      // "wonky" churn). Now we just show/hide what's already rendered: instant,
      // no flash, no refetch. The normal poll keeps the set fresh.
      _adsbReapplyFilter();
      renderAdsbAircraftList(Object.values(_lastAdsbSnapshot));   // refresh list + count
      if (typeof renderAdsbBoxFilterChips === 'function') renderAdsbBoxFilterChips();  // keep the panel copy in sync
      if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();      // path chips honor the filter now
    });
    c.appendChild(chip);
  });
}

async function loadAdsbPresets() {
  try {
    const d = await (await fetch('/api/adsb/sources')).json();
    const sel = document.getElementById('adsbDump1090Preset');
    sel.innerHTML = '';
    (d.dump1090_presets || []).forEach(p => {
      const o = document.createElement('option');
      o.value = p.url;
      o.textContent = p.label;
      sel.appendChild(o);
    });
  } catch (e) {}
}

function syncAdsbConditionalBoxes() {
  const src = document.getElementById('adsbSource').value;
  document.getElementById('adsbDump1090Box').style.display = (src === 'dump1090') ? 'block' : 'none';
  document.getElementById('adsbOpenskyBox').style.display  = (src === 'opensky')  ? 'block' : 'none';
  document.getElementById('adsbExchangeBox').style.display = (src === 'adsbexchange') ? 'block' : 'none';
  document.getElementById('adsbBeastBox').style.display    = (src === 'beast')    ? 'block' : 'none';
}

document.getElementById('adsbToggle').addEventListener('click', () => {
  const p = document.getElementById('adsbPanel');
  const a = document.getElementById('adsbToggleArrow');
  const open = p.style.display === 'none';
  p.style.display = open ? 'block' : 'none';
  a.textContent = open ? '−' : '+';
  if (open) { loadAdsbConfig(); loadAdsbPresets(); renderAdsbFilterChips(); }
});

// Render the chips on first load too so they exist before panel is opened
renderAdsbFilterChips();
// Flight-path bulk controls (AIR TRAFFIC panel)
renderAdsbPathTagChips();
(function _wireAdsbPathBulkBtns() {
  const clrBtn = document.getElementById('adsbPathsClearBtn');
  if (clrBtn) clrBtn.addEventListener('click', (e) => { e.stopPropagation(); _adsbClearAllPaths(); });
})();
document.getElementById('adsbSource').addEventListener('change', syncAdsbConditionalBoxes);
document.getElementById('adsbDump1090Preset').addEventListener('change', function() {
  document.getElementById('adsbDump1090Url').value = this.value;
});

document.getElementById('adsbSaveBtn').addEventListener('click', async (ev) => {
  // The 'enabled' state is owned by the toggle (instant save), so SAVE CONFIG
  // explicitly omits it — saving credentials should never accidentally flip the layer.
  await withButtonLock(ev.currentTarget, 'SAVING...', async () => {
    const source = document.getElementById('adsbSource').value;
    const interval = parseInt(document.getElementById('adsbInterval').value, 10) || 8;
    const bboxOnly = document.getElementById('adsbBboxOnly').checked;
    let bbox = null;
    if (bboxOnly) {
      const b = map.getBounds();
      bbox = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()];
    }
    const body = { source, interval, bbox };
    if (source === 'dump1090') body.dump1090_url = document.getElementById('adsbDump1090Url').value.trim();
    if (source === 'opensky') {
      body.opensky_user = document.getElementById('adsbOpenskyUser').value.trim();
      const pw = document.getElementById('adsbOpenskyPass').value;
      if (pw) body.opensky_pass = pw;
    }
    if (source === 'adsbexchange') {
      const k = document.getElementById('adsbExchangeKey').value.trim();
      if (k) body.adsbx_key = k;
    }
    if (source === 'beast') {
      body.beast_host = document.getElementById('adsbBeastHost').value.trim() || 'localhost';
      body.beast_port = parseInt(document.getElementById('adsbBeastPort').value, 10) || 30005;
    }
    let r, j;
    try {
      r = await fetch('/api/adsb/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      j = await r.json();
    } catch (e) { alert('save failed: ' + e); return; }
    if (!r.ok) { alert(j.error || 'save failed'); return; }
    const detailStatus = document.getElementById('adsbStatus');
    if (detailStatus) detailStatus.textContent = '— config saved —';
  });
});

// Initial offline layer load + persisted offline layer activation
// Plus the always-on cache-job poller, so in-flight / paused jobs survive
// page reloads, panel collapses, and basemap switches.
startJobPoller();
(async function initOffline() {
  await refreshOfflineLayers();
  const persisted = localStorage.getItem('basemap') || '';
  if (persisted.indexOf('offline:') === 0) {
    const sel = document.getElementById('layerSelect');
    // Verify the option exists; if so, switch to it.
    if ([...sel.options].some(o => o.value === persisted)) {
      sel.value = persisted;
      applyBasemap(persisted);
    }
  } else {
    setBasemapStatus('online', 'ONLINE');
  }
})();

// persistentMACs hoisted earlier — see top of <script> block
const droneMarkers = {};
const pilotMarkers = {};
const droneCircles = {};
const pilotCircles = {};
const dronePolylines = {};
const pilotPolylines = {};

// Update a polyline in place instead of destroying and recreating it every tick.
// Recreating the layer on each update caused visible flashing/choppiness and let
// the path get continuously re-added, fighting the staleout removal.
// The `visible` flag honors the hiddenPaths set so a user-hidden trail stays off
// the layer without losing the polyline object (and re-attaches cleanly on unhide).
function upsertPolyline(store, mac, coords, options, layer, visible) {
  if (store[mac]) {
    store[mac].setLatLngs(coords);
    if (options && options.color) { store[mac].setStyle({ color: options.color }); }
    if (visible) {
      if (!layer.hasLayer(store[mac])) store[mac].addTo(layer);
    } else {
      if (layer.hasLayer(store[mac])) layer.removeLayer(store[mac]);
    }
  } else {
    store[mac] = L.polyline(coords, options);
    if (visible) store[mac].addTo(layer);
  }
  return store[mac];
}
// Path layer groups so we can toggle visibility wholesale.
// Aircraft trails already live on adsbTrailLayer (created earlier).
const dronePathLayer = L.layerGroup().addTo(map);
const pilotPathLayer = L.layerGroup().addTo(map);
// Per-entity hide list — keys are mac (drone/pilot) or icao (aircraft).
// (hoisted to the ADS-B section above so adsbApply and trail creation can
// reach it without TDZ. The function below re-declares for backward compat.)
function _persistHiddenPaths_legacy() {
  localStorage.setItem('hiddenPaths', JSON.stringify([...hiddenPaths]));
}

// Master path-visibility toggles (Drone / Pilot / Aircraft). State persisted.
// ALL three default OFF — keeps the map clean on first load. Users opt in
// per-kind via the toggles below, individually per drone/aircraft via the
// popup toggles, or wholesale via the "ALL PATHS" master switch.
const _pathsMasters = {
  drone:    localStorage.getItem('pathsShowDrone')    === '1',
  pilot:    localStorage.getItem('pathsShowPilot')    === '1',
  aircraft: localStorage.getItem('pathsShowAircraft') === '1',
};
function _applyPathsMaster(kind) {
  // No more master gates — drone, pilot, and aircraft path layers all stay on
  // the map permanently. Individual polylines are added/removed based on the
  // per-entity popup toggles (tracked via hiddenPaths). This is what the user
  // means by "individuals at this point" — one switch per entity, no global.
  const layer = (kind === 'drone') ? dronePathLayer
              : (kind === 'pilot') ? pilotPathLayer
              : adsbTrailLayer;
  if (!map.hasLayer(layer)) layer.addTo(map);
}
// Per-entity show/hide helper — flips the hiddenPaths set + adds/removes the
// individual polyline. Works for drones (key 'drone:<mac>'), pilots ('pilot:<mac>'),
// and aircraft ('aircraft:<icao>').
function setPathHidden(key, hidden) {
  const [kind, id] = key.split(':');
  if (kind === 'aircraft') {
    // Opt-in model: track the planes the user explicitly turned ON. Reconcile
    // applies it immediately (creating the polyline if needed) so the toggle is
    // reliable even before the plane has moved; the ticker keeps it in sync.
    if (hidden) shownAircraftPaths.delete(id); else shownAircraftPaths.add(id);
    _persistShownAircraftPaths();
    _adsbReconcileTrail(id, true);
    if (adsbMarkers[id]) {
      adsbMarkers[id].setPopupContent(adsbPopup(_lastAdsbSnapshot[id] || {icao: id}));
    }
    if (typeof renderAdsbPathTagChips === 'function') renderAdsbPathTagChips();
    return;
  }
  // Drones / pilots: opt-out via hiddenPaths (default visible).
  if (hidden) hiddenPaths.add(key); else hiddenPaths.delete(key);
  _persistHiddenPaths();
  if (kind === 'drone' && dronePolylines[id]) {
    if (hidden) dronePathLayer.removeLayer(dronePolylines[id]);
    else        dronePolylines[id].addTo(dronePathLayer);
  } else if (kind === 'pilot' && pilotPolylines[id]) {
    if (hidden) pilotPathLayer.removeLayer(pilotPolylines[id]);
    else        pilotPolylines[id].addTo(pilotPathLayer);
  }
}
window.setPathHidden = setPathHidden;
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
    const observerIcon = createObserverIcon('blue');
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
      icon: createDroneIcon(color),
      pane: 'droneIconPane',
      bubblingMouseEvents: false
    })
                           .bindPopup(generatePopupContent(detection, 'drone'), {className: 'drone-popup', maxWidth: 300, minWidth: 240, closeButton: true})
                           .addTo(map)
                           .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
  } else {
    droneMarkers[mac].setLatLng([detection.drone_lat, detection.drone_long]);
    // Only refresh the popup HTML when the popup is closed — re-rendering it
    // while open destroys the DOM the user is interacting with (kills focus,
    // resets toggle states mid-click). When tracking a drone, this matters
    // every poll. The popup will re-render automatically the next time the
    // user closes & re-opens it.
    if (!droneMarkers[mac].isPopupOpen()) {
      droneMarkers[mac].setPopupContent(generatePopupContent(detection, 'drone'));
    }
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
  upsertPolyline(dronePolylines, mac, dronePathCoords[mac], { renderer: canvasRenderer, color: color }, dronePathLayer, !hiddenPaths.has('drone:' + mac));
  if (detection.pilot_lat && detection.pilot_long && detection.pilot_lat != 0 && detection.pilot_long != 0) {
    if (!pilotMarkers[mac]) {
      pilotMarkers[mac] = L.marker([detection.pilot_lat, detection.pilot_long], {
        icon: createPilotIcon(color),
        pane: 'pilotIconPane',
        bubblingMouseEvents: false
      })
                             .bindPopup(generatePopupContent(detection, 'pilot'), {className: 'drone-popup', maxWidth: 300, minWidth: 240, closeButton: true})
                             .addTo(map)
                             .on('click', function(){ map.setView(this.getLatLng(), map.getZoom()); });
    } else {
      pilotMarkers[mac].setLatLng([detection.pilot_lat, detection.pilot_long]);
      if (!pilotMarkers[mac].isPopupOpen()) {
        pilotMarkers[mac].setPopupContent(generatePopupContent(detection, 'pilot'));
      }
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
    upsertPolyline(pilotPolylines, mac, pilotPathCoords[mac], { renderer: canvasRenderer, color: color, dashArray: '5,5' }, pilotPathLayer, !hiddenPaths.has('pilot:' + mac));
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
         if (historicalDrones[mac]) {
             // UNLOCK: drone was historic-locked. Remove the lock state and clean up
             // anything tied to the lock — but only if the drone isn't currently active.
             delete historicalDrones[mac];
             localStorage.setItem('historicalDrones', JSON.stringify(historicalDrones));
             const liveDet = (window.tracked_pairs || {})[mac];
             const stillActive = liveDet && liveDet.last_update && ((Date.now()/1000 - liveDet.last_update) <= STALE_THRESHOLD);
             if (!stillActive) {
               // Tear down icons AND trails immediately so a second dblclick on an
               // inactive drone visibly "goes away" without waiting for the slow
               // restorePaths reconcile.
               if (droneMarkers[mac]) { map.removeLayer(droneMarkers[mac]); delete droneMarkers[mac]; }
               if (pilotMarkers[mac]) { map.removeLayer(pilotMarkers[mac]); delete pilotMarkers[mac]; }
               if (dronePolylines[mac]) { dronePathLayer.removeLayer(dronePolylines[mac]); delete dronePolylines[mac]; }
               if (pilotPolylines[mac]) { pilotPathLayer.removeLayer(pilotPolylines[mac]); delete pilotPolylines[mac]; }
               delete dronePathCoords[mac];
               delete pilotPathCoords[mac];
             }
             // For an active drone we leave the icons/trail alone — updateData
             // is still rendering it live.
             item.classList.remove("selected");
             map.closePopup();
         } else {
             // LOCK: dblclick on an inactive drone restores its icons, drone path,
             // and pilot path from the server's full history.
             historicalDrones[mac] = Object.assign({}, detection, { userLocked: true, lockTime: Date.now()/1000 });
             localStorage.setItem('historicalDrones', JSON.stringify(historicalDrones));
             showHistoricalDrone(mac, historicalDrones[mac]);
             // Now that the markers exist for this locked drone, pull its full trail
             // back from the server. restorePaths gates on marker presence, so it has
             // to run AFTER showHistoricalDrone — not before.
             restorePaths();
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
    
    // Mark items seen in the last 5 seconds
    const isRecent = detection && ((currentTime - detection.last_update) <= 5);
    item.classList.toggle('recent', isRecent);
    if (isActive) {
      if (item.parentNode !== activePlaceholder) { activePlaceholder.appendChild(item); }
    } else {
      if (item.parentNode !== inactivePlaceholder) { inactivePlaceholder.appendChild(item); }
    }
  });
  _refreshDronesHeaderCount();
}

// Drones count: TOTAL active drones (not view-filtered). Drones are typically
// few and the user wants to know how many are out there overall, regardless
// of where the map is looking. Counts every drone whose last_update is within
// the staleness threshold.
function _refreshDronesHeaderCount() {
  const el = document.getElementById('dronesHeaderCount');
  if (!el) return;
  const data = window.tracked_pairs || {};
  const now = Date.now() / 1000;
  let totalActive = 0;
  for (const mac in data) {
    const d = data[mac];
    if (!d || !d.last_update) continue;
    if ((now - d.last_update) > STALE_THRESHOLD) continue;
    totalActive++;
  }
  el.textContent = totalActive + ' active';
  el.style.color = totalActive > 0 ? '#00ff88' : '#666';
}
// Hook the map move/zoom so counts (and any other view-dependent state) update
// instantly without waiting for the next 1s detection poll.
setTimeout(() => {
  if (typeof map === 'undefined') return;
  map.on('moveend', _refreshDronesHeaderCount);
  map.on('zoomend', _refreshDronesHeaderCount);
}, 0);

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
        if (dronePolylines[mac]) { dronePathLayer.removeLayer(dronePolylines[mac]); delete dronePolylines[mac]; }
        if (pilotPolylines[mac]) { pilotPathLayer.removeLayer(pilotPolylines[mac]); delete pilotPolylines[mac]; }
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

      // Stale visual: fade drone/pilot markers + their broadcast/circle ring
      // when the entry is past STALE_THRESHOLD. Keeps the markers on the map
      // (so the user can see where the drone WAS) but dims them so live
      // activity reads clearly. Restored to full opacity the moment a new
      // detection lands and `activeNow` flips back to true.
      const staleOpacity = activeNow ? 1.0 : 0.35;
      try {
        if (droneMarkers[mac] && droneMarkers[mac].setOpacity) droneMarkers[mac].setOpacity(staleOpacity);
        if (pilotMarkers[mac] && pilotMarkers[mac].setOpacity) pilotMarkers[mac].setOpacity(staleOpacity);
        if (droneCircles[mac]) droneCircles[mac].setStyle({opacity: staleOpacity, fillOpacity: 0.7 * staleOpacity});
        if (pilotCircles[mac]) pilotCircles[mac].setStyle({opacity: staleOpacity, fillOpacity: 0.7 * staleOpacity});
        if (dronePolylines[mac]) dronePolylines[mac].setStyle({opacity: 0.5 * staleOpacity + 0.4});
        if (pilotPolylines[mac]) pilotPolylines[mac].setStyle({opacity: 0.5 * staleOpacity + 0.4});
      } catch (e) {}

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
          if (!droneMarkers[mac].isPopupOpen()) {
            droneMarkers[mac].setPopupContent(generatePopupContent(det, 'drone'));
          } else {
            // Live-update telemetry inside the open popup without wiping
            // alias input / tag dropdown / track buttons.
            _droneUpdateOpenPopupStats(mac, det);
          }
        } else {
          droneMarkers[mac] = L.marker([droneLat, droneLng], {
            icon: createDroneIcon(color),
            pane: 'droneIconPane'
          })
                                .bindPopup(generatePopupContent(det, 'drone'), {className: 'drone-popup', maxWidth: 300, minWidth: 240, closeButton: true})
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
        upsertPolyline(dronePolylines, mac, dronePathCoords[mac], {color: color}, dronePathLayer, !hiddenPaths.has('drone:' + mac));
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
            icon: createPilotIcon(color),
            pane: 'pilotIconPane',
            bubblingMouseEvents: false
          })
                                .bindPopup(generatePopupContent(det, 'pilot'), {className: 'drone-popup', maxWidth: 300, minWidth: 240, closeButton: true})
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
        upsertPolyline(pilotPolylines, mac, pilotPathCoords[mac], {color: color, dashArray: '5,5'}, pilotPathLayer, !hiddenPaths.has('pilot:' + mac));
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

// Top-down quadcopter UAV marker (SVG, not an emoji) for drones — 4 rotor rings on
// an X-frame with a solid central body, tinted to the drone's color.
function createDroneIcon(color) {
  const size = Math.round(getDynamicSize());
  const half = Math.round(size / 2);
  const svg =
    '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" style="display:block;" '
    + 'fill="none" stroke="' + color + '" stroke-width="1.6" stroke-linecap="round">'
    + '<line x1="6.5" y1="6.5" x2="17.5" y2="17.5"/>'
    + '<line x1="17.5" y1="6.5" x2="6.5" y2="17.5"/>'
    + '<circle cx="6" cy="6" r="3.2"/><circle cx="18" cy="6" r="3.2"/>'
    + '<circle cx="6" cy="18" r="3.2"/><circle cx="18" cy="18" r="3.2"/>'
    + '<rect x="9.3" y="9.3" width="5.4" height="5.4" rx="1.2" fill="' + color + '" stroke="none"/>'
    + '</svg>';
  return L.divIcon({
    html: '<div style="width:' + size + 'px; height:' + size + 'px;">' + svg + '</div>',
    className: '',
    iconSize: [size, size],
    iconAnchor: [half, half]
  });
}

// Pilot marker — a clean person glyph (head + shoulders), tinted to the pair color.
// Slightly smaller than the drone so the drone reads as the primary contact.
function createPilotIcon(color) {
  const size = Math.round(getDynamicSize() * 0.85);
  const half = Math.round(size / 2);
  const svg =
    '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" style="display:block;" fill="' + color + '">'
    + '<circle cx="12" cy="7.8" r="3.4"/>'
    + '<path d="M5.5 19.5 C5.5 14, 18.5 14, 18.5 19.5 Z"/>'
    + '</svg>';
  return L.divIcon({
    html: '<div style="width:' + size + 'px; height:' + size + 'px;">' + svg + '</div>',
    className: '', iconSize: [size, size], iconAnchor: [half, half]
  });
}

// Observer marker — a crosshair/target "you are here" glyph. Distinct from the
// quadcopter (drone) and the person (pilot). Replaces the old emoji picker.
function createObserverIcon(color) {
  const size = Math.round(getDynamicSize());
  const half = Math.round(size / 2);
  const svg =
    '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size + '" style="display:block;" '
    + 'fill="none" stroke="' + color + '" stroke-width="1.7" stroke-linecap="round">'
    + '<circle cx="12" cy="12" r="6"/>'
    + '<circle cx="12" cy="12" r="1.9" fill="' + color + '" stroke="none"/>'
    + '<line x1="12" y1="1.5" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22.5"/>'
    + '<line x1="1.5" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22.5" y2="12"/>'
    + '</svg>';
  return L.divIcon({
    html: '<div style="width:' + size + 'px; height:' + size + 'px;">' + svg + '</div>',
    className: '', iconSize: [size, size], iconAnchor: [half, half]
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
    // A trail should only exist while its marker is on the map. updateData owns the
    // marker lifecycle (creates on detection, removes at staleout), so tie paths to
    // marker presence: if the drone/pilot marker is gone, drop its trail instead of
    // re-adding it from server history. This is what kept staled-out paths sticking
    // around after the drone and pilot markers had already disappeared.
    for (const mac in data.dronePaths) {
      if (!droneMarkers[mac]) {
        if (dronePolylines[mac]) { dronePathLayer.removeLayer(dronePolylines[mac]); delete dronePolylines[mac]; }
        continue;
      }
      dronePathCoords[mac] = data.dronePaths[mac];
      upsertPolyline(dronePolylines, mac, dronePathCoords[mac], {color: get_color_for_mac(mac)}, dronePathLayer, !hiddenPaths.has('drone:' + mac));
    }
    for (const mac in data.pilotPaths) {
      if (!pilotMarkers[mac]) {
        if (pilotPolylines[mac]) { pilotPathLayer.removeLayer(pilotPolylines[mac]); delete pilotPolylines[mac]; }
        continue;
      }
      pilotPathCoords[mac] = data.pilotPaths[mac];
      upsertPolyline(pilotPolylines, mac, pilotPathCoords[mac], {color: get_color_for_mac(mac), dashArray: '5,5'}, pilotPathLayer, !hiddenPaths.has('pilot:' + mac));
    }
  } catch (error) { console.error("Error restoring paths:", error); }
}
// restorePaths reconciles trails against the server's full history. updateData
// already maintains live paths every tick, so this only needs to run occasionally
// (page load + a slow self-heal). Running it at 200ms recreated every polyline
// 5x/second, which caused the flashing and kept resurrecting expired paths.
setInterval(restorePaths, 15000);
restorePaths();

function updateColor(mac, hue) {
  hue = parseInt(hue);
  colorOverrides[mac] = hue;
  localStorage.setItem('colorOverrides', JSON.stringify(colorOverrides));
  var newColor = "hsl(" + hue + ", 70%, 50%)";
  if (droneMarkers[mac]) { droneMarkers[mac].setIcon(createDroneIcon(newColor)); droneMarkers[mac].setPopupContent(generatePopupContent(tracked_pairs[mac], 'drone')); }
  if (pilotMarkers[mac]) { pilotMarkers[mac].setIcon(createPilotIcon(newColor)); pilotMarkers[mac].setPopupContent(generatePopupContent(tracked_pairs[mac], 'pilot')); }
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

    # ---- ADS-B configuration from the onboarding form ----
    try:
        adsb_enabled = bool(request.form.get('adsb_enabled'))
        adsb_mode = (request.form.get('adsb_mode') or 'online').strip()
        if adsb_mode == 'local':
            local_mode = (request.form.get('adsb_local_mode') or 'dump1090').strip()
            if local_mode == 'beast':
                ADSB_CONFIG['source'] = 'beast'
                bh = (request.form.get('adsb_beast_host') or 'localhost').strip()
                try: bp = max(1, min(65535, int(request.form.get('adsb_beast_port', 30005))))
                except (TypeError, ValueError): bp = 30005
                ADSB_CONFIG['beast_host'] = bh
                ADSB_CONFIG['beast_port'] = bp
            else:
                ADSB_CONFIG['source'] = 'dump1090'
                u = (request.form.get('adsb_dump1090_url') or 'http://localhost:8080/data/aircraft.json').strip()
                ADSB_CONFIG['dump1090_url'] = u
        else:
            online_src = (request.form.get('adsb_online_source') or 'adsblol').strip()
            if online_src in ADSB_SOURCES:
                ADSB_CONFIG['source'] = online_src
        ADSB_CONFIG['enabled'] = adsb_enabled
        _adsb_save_config()
        if adsb_enabled:
            _start_adsb_poller()
            threading.Thread(target=_adsb_kick_fetch, daemon=True, name='adsb-kick-onboarding').start()
        logger.info(f"Onboarding saved ADS-B config: source={ADSB_CONFIG.get('source')} enabled={adsb_enabled}")
    except Exception as e:
        logger.warning(f"Onboarding ADS-B save failed: {e}")

    # Default basemap is persisted via localStorage on the client (the form does it
    # in JS just before submit). Nothing for us to do server-side.

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

# Cache-buster — bumped on every server start. Stamped into the served HTML
# and into the no-store headers so every reload after a server restart pulls
# fresh JS/CSS, no manual hard-reload needed.
APP_BUILD_ID = str(int(time.time()))
APP_BUILD_LABEL = "ADSB-ALL-PLANES-" + APP_BUILD_ID[-4:]

@app.route('/')
def index():
    """Render the map UI. First-boot only (no selected_ports.json yet) redirects
    to the guided /select_ports onboarding screen — once setup is saved (or the
    user explicitly skips it) future visits land directly on the map and never
    bounce again, even on board disconnect."""
    first_boot = not os.path.exists(PORTS_FILE)
    load_selected_ports()
    # Best-effort auto-connect in the background, but never block or redirect on it.
    if SELECTED_PORTS and not any(serial_connected_status.get(p, False) for p in SELECTED_PORTS.values()):
        try:
            auto_connect_to_saved_ports()
        except Exception as e:
            logger.debug(f"auto-connect attempt failed (continuing anyway): {e}")
    # First-boot guided onboarding — only on the very first visit. The user can
    # also click SKIP on that page to skip past it (we still create the empty
    # selected_ports.json on POST so we don't redirect twice).
    if first_boot:
        return redirect(url_for('select_ports_get'))
    # Stamp the build ID into a meta tag and force no-store so a server restart
    # always serves fresh content even if the browser tried to cache. Also log
    # the build label to the console on every load so the user can verify they
    # have fresh code (look for "[mesh-mapper] BUILD: ..." in DevTools).
    log_script = (
        '<script>'
        f'window.__BUILD_ID__="{APP_BUILD_ID}";'
        f'window.__BUILD_LABEL__="{APP_BUILD_LABEL}";'
        f'console.log("%c[mesh-mapper] BUILD:","color:#88c8ff;font-weight:bold","{APP_BUILD_LABEL}");'
        '</script>'
    )
    body = HTML_PAGE.replace(
        '</head>',
        f'<meta name="build-id" content="{APP_BUILD_ID}">\n'
        f'{log_script}\n'
        '</head>',
        1,
    )
    resp = make_response(body)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0, private'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['X-Build-Id'] = APP_BUILD_ID
    return resp

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
@app.route('/api/geofences', methods=['GET'])
def api_geofences_list():
    with GEOFENCE_LOCK:
        return jsonify({'fences': [{'id': fid, **f} for fid, f in GEOFENCES.items()]})


@app.route('/api/geofences', methods=['POST'])
def api_geofences_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        fence = _validate_fence(data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    fid = uuid.uuid4().hex[:12]
    fence['id'] = fid
    fence['created'] = time.time()
    with GEOFENCE_LOCK:
        GEOFENCES[fid] = fence
        save_geofences()
    try: socketio.emit('geofences', {'fences': list(GEOFENCES.values())})
    except Exception: pass
    return jsonify(fence)


@app.route('/api/geofences/<fid>', methods=['PUT'])
def api_geofences_update(fid):
    with GEOFENCE_LOCK:
        if fid not in GEOFENCES:
            return jsonify({'error': 'not found'}), 404
    data = request.get_json(force=True, silent=True) or {}
    # Allow partial updates: only send the keys the user wants to change
    cur = dict(GEOFENCES[fid])
    cur.update({k: v for k, v in data.items() if k != 'id'})
    try:
        fence = _validate_fence(cur)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    fence['id'] = fid
    fence['created'] = GEOFENCES[fid].get('created', time.time())
    with GEOFENCE_LOCK:
        GEOFENCES[fid] = fence
        save_geofences()
    try: socketio.emit('geofences', {'fences': list(GEOFENCES.values())})
    except Exception: pass
    return jsonify(fence)


@app.route('/api/geofences/<fid>', methods=['DELETE'])
def api_geofences_delete(fid):
    with GEOFENCE_LOCK:
        if fid not in GEOFENCES:
            return jsonify({'error': 'not found'}), 404
        GEOFENCES.pop(fid, None)
        GEOFENCE_STATE.pop(fid, None)
        save_geofences()
    try: socketio.emit('geofences', {'fences': list(GEOFENCES.values())})
    except Exception: pass
    return jsonify({'ok': True})


@app.route('/api/geofence_alerts', methods=['GET'])
def api_geofence_alerts():
    """Return recent geofence alerts (newest last). Cap with ?limit=N."""
    try: limit = max(1, min(GEOFENCE_ALERTS_MAX, int(request.args.get('limit', 50))))
    except ValueError: limit = 50
    return jsonify({'alerts': GEOFENCE_ALERTS[-limit:]})


@app.route('/api/drone_tags', methods=['GET'])
def api_drone_tags():
    """Return the full drone-tag map. Lightweight; UI loads once + tracks updates."""
    return jsonify({'tags': DRONE_TAGS, 'values': list(DRONE_TAG_VALUES)})


@app.route('/api/drone_tags/<mac>', methods=['POST'])
def api_drone_tag_set(mac):
    """Set or clear the OSINT tag for a single drone MAC."""
    mac = (mac or '').strip().lower()
    if not mac:
        return jsonify({'error': 'mac required'}), 400
    data = request.get_json(force=True, silent=True) or {}
    tag = (data.get('tag') or '').strip().lower()
    if tag and tag not in DRONE_TAG_VALUES:
        return jsonify({'error': f'tag must be one of {list(DRONE_TAG_VALUES)}'}), 400
    if tag in ('', 'unknown'):
        DRONE_TAGS.pop(mac, None)
    else:
        DRONE_TAGS[mac] = tag
    save_drone_tags()
    try:
        socketio.emit('drone_tags', {'tags': DRONE_TAGS})
    except Exception:
        pass
    return jsonify({'ok': True, 'mac': mac, 'tag': tag or 'unknown'})


@app.route('/api/select_ports', methods=['POST'])
def api_select_ports():
    """JSON-based port selection so the UI can configure USB inline without a
    page navigation. Body: {"ports": ["/dev/cu.usbmodem311101", ...]} (up to 3)."""
    global SELECTED_PORTS
    data = request.get_json(force=True, silent=True) or {}
    ports = data.get('ports') or []
    if not isinstance(ports, list) or len(ports) > 3:
        return jsonify({'error': 'ports must be a list of up to 3 device paths'}), 400

    # Verify each requested port exists right now (avoid silent failures)
    available = {p.device for p in serial.tools.list_ports.comports()}
    bad = [p for p in ports if p and p not in available]
    if bad:
        return jsonify({'error': f'unknown port(s): {bad}', 'available': sorted(available)}), 400

    new_selected = {}
    for i, p in enumerate(ports[:3], start=1):
        if p:
            new_selected[f'port{i}'] = p

    # Close connections to ports no longer selected
    with serial_objs_lock:
        for port_key, port_device in list(SELECTED_PORTS.items()):
            if port_key not in new_selected or new_selected[port_key] != port_device:
                if port_device in serial_objs:
                    try:
                        ser = serial_objs[port_device]
                        if ser and ser.is_open:
                            ser.close()
                            logger.info(f"Closed serial connection to {port_device}")
                    except Exception as e:
                        logger.error(f"Error closing {port_device}: {e}")
                    finally:
                        serial_objs.pop(port_device, None)
                        serial_connected_status[port_device] = False

    SELECTED_PORTS = new_selected
    save_selected_ports()

    for port in SELECTED_PORTS.values():
        if not serial_connected_status.get(port, False):
            serial_connected_status[port] = False
            start_serial_thread(port)
            logger.info(f"Started serial thread for {port}")

    return jsonify({'ok': True, 'selected': new_selected})


@app.route('/api/ports', methods=['GET'])
def api_ports():
    ports = list(serial.tools.list_ports.comports())
    return jsonify({
        'ports': [{'device': p.device, 'description': p.description} for p in ports]
    })

# Updated status endpoint: returns a dict of statuses for each selected USB.
@app.route('/api/serial_status', methods=['GET'])
def api_serial_status():
    return jsonify({"statuses": combined_connection_status()})

# Heartbeat endpoint for non-serial receivers (e.g. tools/ds110_bridge.py).
# They show up alongside USB ports in the connection status UI.
@app.route('/api/receiver_status', methods=['POST'])
def api_receiver_status():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"status": "error", "reason": "no receiver name"}), 400
    receiver_status[name] = {"last_seen": time.time(), "stats": data.get("stats", {})}
    emit_serial_status()
    return jsonify({"status": "ok"})

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

def open_serial_no_reset(port, baudrate=None, timeout=1):
    """Open a serial port WITHOUT rebooting the board on the other end.

    pyserial asserts both DTR and RTS when it opens a port. On an ESP32-S3
    talking over its native USB (the XIAO node boards), those lines are wired
    straight into the USB-Serial/JTAG peripheral's reset logic - asserting RTS
    is exactly the "chip reset" step of esptool's own reset sequence. So every
    plain serial.Serial(port, ...) here rebooted the node, and the reader
    thread's reconnect loop rebooted it again on every retry. Boards showed
    rst:0x15 (USB_UART_CHIP_RESET) and looked like they were watchdog-resetting
    in a loop; they were being reset by us.

    Setting dtr/rts False before open() stores the desired line state, which
    open() then applies - leaving the chip out of reset and running.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baudrate if baudrate is not None else BAUD_RATE
    ser.timeout = timeout
    try:
        ser.dtr = False
        ser.rts = False
    except Exception as e:
        # Some platforms/drivers refuse line-state changes before open; the
        # port is still usable, it may just reset the board on connect.
        logger.debug(f"Could not pre-clear DTR/RTS for {port}: {e}")
    ser.open()
    return ser


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
                ser = open_serial_no_reset(port)
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
                    
                    # Heartbeats and command acks are normal traffic, not
                    # detections - drop them before the MAC logic below, which
                    # would otherwise log a WARNING for every one of them.
                    if 'heartbeat' in detection:
                        logger.debug(f"Skipping heartbeat from {port}")
                        continue

                    # MAC tracking logic...
                    if 'mac' in detection:
                        last_mac_by_port[port] = detection['mac']
                        logger.debug(f"Found MAC in detection: {detection['mac']}")
                    elif port in last_mac_by_port:
                        detection['mac'] = last_mac_by_port[port]
                        logger.debug(f"Using cached MAC for {port}: {detection['mac']}")
                    else:
                        logger.warning(f"No MAC found in detection from {port}: {detection}")
                    
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
    
    with SERIAL_THREADS_LOCK:
        if SERIAL_THREADS.get(port) is threading.current_thread():
            SERIAL_THREADS.pop(port, None)

    logger.info(f"Serial reader thread for {port} shutting down. Total data packets received: {data_received_count}")

# One serial reader thread per port, and only one. start_serial_thread() is
# called from four places - startup auto-connect, the port-monitor thread, the
# port-selection form and the saved-port restore - none of which knew about the
# others. Every extra call spawned another reader on the SAME port, and those
# readers then stole bytes from each other: JSON arrived with characters
# missing, pyserial raised "device reports readiness to read but returned no
# data (device disconnected or multiple access on port?)", each reader closed
# and reopened, and the UI showed the node connecting and disconnecting
# forever. Registering live threads here makes duplicate starts harmless.
SERIAL_THREADS = {}                      # port -> Thread
SERIAL_THREADS_LOCK = threading.Lock()


def start_serial_thread(port):
    """Start the reader thread for `port`, unless one is already running."""
    with SERIAL_THREADS_LOCK:
        existing = SERIAL_THREADS.get(port)
        if existing is not None and existing.is_alive():
            logger.info(f"Serial reader already running for {port} - not starting a second one")
            return existing
        thread = threading.Thread(target=serial_reader, args=(port,),
                                  daemon=True, name=f"serial-reader:{port}")
        SERIAL_THREADS[port] = thread
        thread.start()
        return thread

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

    # Reload any cache jobs from previous runs (any 'running' becomes 'paused')
    _load_cache_jobs()

    # Load ADS-B config and spin up the poller if it was enabled before
    _adsb_load_config()
    if ADSB_CONFIG.get('enabled'):
        _start_adsb_poller()

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
            # flask-socketio >= 5.3 refuses to serve under Werkzeug unless we
            # say so explicitly. Without this it raises RuntimeError, which the
            # old `except KeyboardInterrupt` did not catch - the finally block
            # below then called signal_handler(), which calls sys.exit(0), so
            # the real error was swallowed and the process exited 0 having
            # printed "Access the interface at ...". The web UI simply never
            # came up and nothing said why. Werkzeug is the right server for a
            # LAN tool like this; the warning is about public deployment.
            try:
                socketio.run(app, host='0.0.0.0', port=args.web_port,
                             debug=False, allow_unsafe_werkzeug=True)
            except TypeError:
                # flask-socketio < 5.3 has no such argument.
                socketio.run(app, host='0.0.0.0', port=args.web_port, debug=False)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        except OSError as e:
            # Almost always "address already in use".
            logger.error(f"Could not start the web interface on port {args.web_port}: {e}")
            logger.error(f"Another process is using it. Try: python mesh-mapper.py --web-port {args.web_port + 1}")
        except Exception as e:
            logger.exception(f"Web interface failed to start: {e}")
        finally:
            signal_handler(signal.SIGTERM, None)


@app.route('/api/diagnostics', methods=['GET'])
def api_diagnostics():
    """Provide detailed diagnostic information for troubleshooting"""
    diagnostics = {
        "timestamp": datetime.now().isoformat(),
        "selected_ports": SELECTED_PORTS,
        "serial_status": combined_connection_status(),
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
        socketio.emit('serial_status', combined_connection_status(), )
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
    """Get both webhook URLs. Older clients only read webhook_url and still work."""
    try:
        return jsonify({
            "status": "ok",
            "webhook_url": WEBHOOK_URL or "",
            "geofence_webhook_url": GEOFENCE_WEBHOOK_URL or "",
        })
    except Exception as e:
        logger.error(f"Error getting webhook URLs: {e}")
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500

@app.route('/api/webhook_url', methods=['GET'])
def api_webhook_url():
    return jsonify({
        "webhook_url": WEBHOOK_URL or "",
        "geofence_webhook_url": GEOFENCE_WEBHOOK_URL or "",
    })

@app.route('/api/set_geofence_webhook_url', methods=['POST'])
def api_set_geofence_webhook_url():
    """Set the dedicated geofence-alert webhook URL. Empty string clears it
    (falls back to the main WEBHOOK_URL for geofence events)."""
    try:
        if not request.is_json:
            return jsonify({"status": "error", "message": "Request must be JSON"}), 400
        data = request.get_json() or {}
        url = (data.get('geofence_webhook_url') or '').strip()
        if url and not url.startswith(('http://', 'https://')):
            return jsonify({"status": "error",
                            "message": "Invalid URL — must start with http:// or https://"}), 400
        if url and 'localhost' in url and not url.startswith('http://localhost'):
            return jsonify({"status": "error",
                            "message": "For localhost URLs, please use http://localhost"}), 400
        set_geofence_webhook_url(url or None)
        logger.info(f"Geofence webhook URL {'updated to: ' + url if url else 'cleared'}")
        return jsonify({"status": "ok", "geofence_webhook_url": GEOFENCE_WEBHOOK_URL or ""})
    except Exception as e:
        logger.error(f"Error setting geofence webhook URL: {e}")
        return jsonify({"status": "error", "message": f"Server error: {str(e)}"}), 500

# --- Webhook URL Persistence ---
WEBHOOK_URL_FILE = os.path.join(BASE_DIR, "webhook_url.json")

def save_webhook_url():
    """Save both webhook URLs (detection + geofence) to disk."""
    global WEBHOOK_URL, GEOFENCE_WEBHOOK_URL
    try:
        with open(WEBHOOK_URL_FILE, "w") as f:
            json.dump({
                "webhook_url": WEBHOOK_URL,
                "geofence_webhook_url": GEOFENCE_WEBHOOK_URL,
            }, f)
        logger.debug(f"Webhook URLs saved to {WEBHOOK_URL_FILE}")
    except Exception as e:
        logger.error(f"Error saving webhook URLs: {e}")

def load_webhook_url():
    """Load both webhook URLs from disk on startup. Backward-compat: old files
    that only have `webhook_url` keep working, the geofence URL just defaults
    to None (which means: fall back to WEBHOOK_URL for geofence alerts)."""
    global WEBHOOK_URL, GEOFENCE_WEBHOOK_URL
    if os.path.exists(WEBHOOK_URL_FILE):
        try:
            with open(WEBHOOK_URL_FILE, "r") as f:
                data = json.load(f)
                WEBHOOK_URL = data.get("webhook_url") or None
                GEOFENCE_WEBHOOK_URL = data.get("geofence_webhook_url") or None
                if WEBHOOK_URL:
                    logger.info(f"Loaded detection webhook: {WEBHOOK_URL}")
                if GEOFENCE_WEBHOOK_URL:
                    logger.info(f"Loaded geofence webhook: {GEOFENCE_WEBHOOK_URL}")
                if not WEBHOOK_URL and not GEOFENCE_WEBHOOK_URL:
                    logger.info("No webhook URLs configured")
        except Exception as e:
            logger.error(f"Error loading webhook URLs: {e}")
            WEBHOOK_URL = None
            GEOFENCE_WEBHOOK_URL = None
    else:
        logger.info("No saved webhook URL file found")
        WEBHOOK_URL = None
        GEOFENCE_WEBHOOK_URL = None

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
