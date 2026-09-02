#!/usr/bin/env python3
"""
ds110_bridge.py — feed DroneScout Bridge ds110 relayed Remote ID detections
into mesh-mapper.

The DroneScout Bridge receives drone Remote ID (2.4/5/5.8 GHz) and
re-broadcasts it as Bluetooth 4 Legacy Advertising ODID frames (ASTM F3411,
service-data UUID 0xFFFA, app code 0x0D) so phones can receive them. This
script listens for those advertisements directly, decodes them, and POSTs
detections to mesh-mapper's /api/detections endpoint using the same JSON
schema the ESP32 firmware nodes emit.

Why not just let the mesh nodes hear the relay? They do — but they tag each
detection with the *advertiser's* MAC, so every drone relayed by the bridge
collapses into a single tracked entry. This script keys relayed drones by
their original BasicID instead and synthesizes a stable MAC per drone.

Usage:
    python3 ds110_bridge.py --list                     # print heard ODID ads, no POST
    python3 ds110_bridge.py                            # feed mapper at default URL
    python3 ds110_bridge.py --url http://10.41.0.60:5000

Note: macOS will prompt for Bluetooth permission for the terminal/Python on
first scan. On macOS bleak reports a CoreBluetooth UUID instead of the real
BLE MAC — only used as a fallback key when no BasicID has been decoded.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import struct
import time

import requests
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

ODID_UUID = "0000fffa-0000-1000-8000-00805f9b34fb"
ODID_APP_CODE = 0x0D
MSG_LEN = 25

# Message types (upper nibble of byte 0)
MSG_BASIC_ID = 0x0
MSG_LOCATION = 0x1
MSG_SYSTEM = 0x4

# Bridge idle self-advertisement; seen as both "DroneScout Bridge" (raw BLE
# payload) and "DroneScoutBridge" (mesh-node firmware strips the space).
def is_placeholder(basic_id: str) -> bool:
    return basic_id.replace(" ", "").lower() == "dronescoutbridge"

# The mapper is run with `--web-port 5001` because loopback:5000 on this
# machine is owned by an unrelated app. Override with --url if needed.
DEFAULT_URL = "http://localhost:5001"

POST_INTERVAL_S = 1.0     # max POST rate per drone
DRONE_EXPIRY_S = 60.0     # drop drones not heard for this long
HEARTBEAT_INTERVAL_S = 15.0  # receiver-status heartbeat; mapper times us out after 45s
RECEIVER_NAME = "DroneScout Bridge (BLE)"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ds110_bridge")


# ---------------------------------------------------------------------------
# ODID message decoding (layouts match opendroneid.h in the firmware)
# ---------------------------------------------------------------------------

def decode_basic_id(msg: bytes) -> str:
    # Bytes 2-21: UASID, null-padded ASCII
    return msg[2:22].split(b"\x00")[0].decode("ascii", errors="replace").strip()


def decode_location(msg: bytes) -> dict:
    # Byte 1: [Status:4][Reserved:1][HeightType:1][EWDirection:1][SpeedMult:1]
    flags = msg[1]
    speed_mult = flags & 0x01
    ew_dir = (flags >> 1) & 0x01

    raw_dir = msg[2]
    direction = raw_dir * 0.5 + (180.0 if ew_dir else 0.0)
    if direction >= 360.0:
        direction = None  # invalid sentinel per spec

    raw_speed = msg[3]
    if raw_speed == 255:
        speed = None
    elif speed_mult:
        speed = 0.75 * raw_speed + 63.5
    else:
        speed = 0.25 * raw_speed

    lat_raw, lon_raw = struct.unpack_from("<ii", msg, 5)
    lat = lat_raw / 1e7
    lon = lon_raw / 1e7

    alt_baro_raw, alt_geo_raw, height_raw = struct.unpack_from("<HHH", msg, 13)
    alt_geo = (alt_geo_raw - 1000) * 0.5 if alt_geo_raw else -1000.0
    height = (height_raw - 1000) * 0.5 if height_raw else -1000.0

    return {
        "lat": lat, "lon": lon, "alt_geo": alt_geo, "height": height,
        "speed": speed, "direction": direction,
    }


def decode_system(msg: bytes) -> dict:
    lat_raw, lon_raw = struct.unpack_from("<ii", msg, 2)
    return {"pilot_lat": lat_raw / 1e7, "pilot_lon": lon_raw / 1e7}


# ---------------------------------------------------------------------------
# Per-drone state
# ---------------------------------------------------------------------------

class DroneState:
    def __init__(self, key: str):
        self.key = key              # tracked key: synthesized MAC or advertiser addr
        self.basic_id = None
        self.rssi = None
        self.lat = 0.0
        self.lon = 0.0
        self.alt_geo = 0.0
        self.speed = None
        self.direction = None
        self.pilot_lat = 0.0
        self.pilot_lon = 0.0
        self.last_seen = time.monotonic()
        self.last_posted = 0.0

    def to_payload(self) -> dict:
        p = {
            "mac": self.key,
            "rssi": self.rssi if self.rssi is not None else 0,
            "drone_lat": self.lat,
            "drone_long": self.lon,
            "drone_altitude": int(self.alt_geo),
            "pilot_lat": self.pilot_lat,
            "pilot_long": self.pilot_lon,
            "source_port": "ds110-ble",
        }
        if self.basic_id:
            p["basic_id"] = self.basic_id
        return p


def synth_mac(basic_id: str) -> str:
    """Stable locally-administered MAC derived from the drone's BasicID, so
    each relayed drone gets its own tracked_pairs entry in mesh-mapper."""
    h = hashlib.sha1(basic_id.encode()).digest()
    return "02:%02x:%02x:%02x:%02x:%02x" % (h[0], h[1], h[2], h[3], h[4])


class Bridge:
    def __init__(self, url: str | None, list_only: bool):
        self.url = url
        self.list_only = list_only
        self.ads_seen = 0
        self.last_heartbeat = 0.0
        self.last_hb_warning = 0.0
        # advertiser address -> most recent non-placeholder basic_id from it
        self.last_id_by_advertiser: dict[str, str] = {}
        # tracked key -> DroneState
        self.drones: dict[str, DroneState] = {}
        # advertiser address -> basic_id currently being merged (relay cycles
        # through drones; location/system ads attach to the latest BasicID)
        self.advertiser_current: dict[str, str] = {}

    # -- BLE callback --------------------------------------------------------

    def on_advertisement(self, device: BLEDevice, adv: AdvertisementData):
        data = adv.service_data.get(ODID_UUID)
        if not data or len(data) < 2 + MSG_LEN:
            return
        if data[0] != ODID_APP_CODE:
            return
        self.ads_seen += 1
        msg = data[2:2 + MSG_LEN]
        msg_type = (msg[0] & 0xF0) >> 4
        advertiser = device.address

        if msg_type == MSG_BASIC_ID:
            basic_id = decode_basic_id(msg)
            if not basic_id:
                return
            if is_placeholder(basic_id):
                # bridge's idle self-advertisement: never track, but show it
                # in --list mode so you can confirm the bridge is on the air
                if self.list_only:
                    logger.info("[adv] %s type=%d id=%s (placeholder, filtered from map)",
                                advertiser, msg_type, basic_id)
                return
            self.advertiser_current[advertiser] = basic_id
            key = synth_mac(basic_id)
            drone = self.drones.get(key)
            if drone is None:
                drone = DroneState(key)
                self.drones[key] = drone
                logger.info("New drone via BLE: basic_id=%s key=%s", basic_id, key)
            drone.basic_id = basic_id
            drone.rssi = adv.rssi
            drone.last_seen = time.monotonic()

        elif msg_type in (MSG_LOCATION, MSG_SYSTEM):
            basic_id = self.advertiser_current.get(advertiser)
            key = synth_mac(basic_id) if basic_id else advertiser
            drone = self.drones.get(key)
            if drone is None:
                drone = DroneState(key)
                if basic_id:
                    drone.basic_id = basic_id
                self.drones[key] = drone
            if msg_type == MSG_LOCATION:
                loc = decode_location(msg)
                drone.lat, drone.lon = loc["lat"], loc["lon"]
                drone.alt_geo = loc["alt_geo"]
                drone.speed, drone.direction = loc["speed"], loc["direction"]
            else:
                op = decode_system(msg)
                drone.pilot_lat, drone.pilot_lon = op["pilot_lat"], op["pilot_lon"]
            drone.rssi = adv.rssi
            drone.last_seen = time.monotonic()

        if self.list_only:
            d = self.drones.get(key) if msg_type != MSG_BASIC_ID else self.drones.get(synth_mac(decode_basic_id(msg)))
            if d:
                logger.info("[adv] %s type=%d id=%s rssi=%s lat=%.6f lon=%.6f pilot=(%.6f,%.6f)",
                            advertiser, msg_type, d.basic_id, adv.rssi, d.lat, d.lon,
                            d.pilot_lat, d.pilot_lon)

    # -- POST loop -----------------------------------------------------------

    async def post_loop(self):
        while True:
            now = time.monotonic()
            # Heartbeat so the mapper UI shows this receiver as Connected,
            # even while idle (no drones around).
            if not self.list_only and self.url and now - self.last_heartbeat >= HEARTBEAT_INTERVAL_S:
                try:
                    requests.post(self.url.rstrip("/") + "/api/receiver_status",
                                  json={"name": RECEIVER_NAME,
                                        "stats": {"ads_seen": self.ads_seen,
                                                  "drones": len(self.drones)}},
                                  timeout=5)
                    self.last_heartbeat = now
                except requests.RequestException as e:
                    if now - self.last_hb_warning > 60:
                        logger.warning("heartbeat to %s failed: %s", self.url, e)
                        self.last_hb_warning = now
            for key in list(self.drones):
                drone = self.drones[key]
                if now - drone.last_seen > DRONE_EXPIRY_S:
                    logger.info("Expiring drone %s (basic_id=%s)", key, drone.basic_id)
                    del self.drones[key]
                    continue
                if self.list_only or not self.url:
                    continue
                if now - drone.last_posted < POST_INTERVAL_S:
                    continue
                # nothing useful yet (no position and no id): skip
                if not drone.basic_id and drone.lat == 0 and drone.lon == 0:
                    continue
                try:
                    r = requests.post(self.url.rstrip("/") + "/api/detections",
                                      json=drone.to_payload(), timeout=5)
                    if r.status_code != 200:
                        logger.warning("POST %s -> HTTP %s", key, r.status_code)
                    else:
                        drone.last_posted = now
                except requests.RequestException as e:
                    logger.warning("POST failed (%s): %s", self.url, e)
            await asyncio.sleep(0.25)

    async def run(self):
        # BlueZ (Linux/Raspberry Pi) defaults to DuplicateData=False, i.e. it
        # suppresses repeats of byte-identical advertisement data. A hovering
        # drone re-broadcasts the same Location message unchanged, so those
        # repeats would never reach us, last_seen would stop advancing and the
        # drone would expire off the map while still in the air. CoreBluetooth
        # delivers every advertisement, which is why this only bites on Linux.
        # The kwarg is a plain TypedDict and is ignored by other backends.
        scanner = BleakScanner(detection_callback=self.on_advertisement,
                               bluez={"filters": {"DuplicateData": True}})
        await scanner.start()
        logger.info("BLE scan started%s",
                    " (list-only, no POSTs)" if self.list_only else f", posting to {self.url}")
        try:
            await self.post_loop()
        finally:
            await scanner.stop()


def main():
    ap = argparse.ArgumentParser(description="Feed DroneScout Bridge ds110 BLE relay into mesh-mapper")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"mesh-mapper base URL (default: {DEFAULT_URL})")
    ap.add_argument("--list", action="store_true",
                    help="print heard ODID advertisements, don't POST")
    args = ap.parse_args()

    bridge = Bridge(url=None if args.list else args.url, list_only=args.list)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
