/*
 * =============================================================================
 * REMOTE NODE - Drone Remote ID Detector + Mesh Sender
 * colonelpanichacks
 *
 * Dual-core ESP32S3 firmware:
 *   Core 0: WiFi promiscuous packet sniffing (Open Drone ID NAN/Beacon)
 *   Core 1: BLE scanning (Open Drone ID BLE advertisements)
 *
 * Detected drone JSON is sent to:
 *   - USB Serial (for local monitoring / direct mesh-mapper.py connection)
 *   - Serial1 UART (GPIO5 TX / GPIO6 RX -> Heltec V3 running Meshtastic)
 *
 * USB output NEVER blocks: on this board Serial is the native USB CDC
 * (HWCDC), whose write() waits on the TX ring when an attached host stops
 * draining (~2s per call worst case). Everything steady-state goes through a
 * non-blocking ring that drops oldest-first, so a stalled or absent host can
 * never back up printerTask and stall mesh sending. Same disease the home
 * node was hardened against; see main_home.cpp's header for the full story.
 *
 * JSON format (matches mesh-mapper.py API, includes node_id for dedup):
 *   {"mac":"xx:xx:xx:xx:xx:xx","rssi":-50,"drone_lat":0.0,"drone_long":0.0,
 *    "drone_altitude":0,"pilot_lat":0.0,"pilot_long":0.0,"basic_id":"...",
 *    "node_id":"A1B2"}
 * =============================================================================
 */

#if !defined(ARDUINO_ARCH_ESP32)
  #error "This program requires an ESP32S3"
#endif

#include <Arduino.h>
#include <HardwareSerial.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_mac.h>
#include <nvs_flash.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

// =============================================================================
// Pin Definitions
// =============================================================================
// UART to Heltec V3 (Meshtastic)
static const int SERIAL1_TX_PIN = 5;   // GPIO5 -> Heltec RX
static const int SERIAL1_RX_PIN = 6;   // GPIO6 <- Heltec TX

// LED on XIAO ESP32S3 (active LOW / inverted logic)
#define LED_PIN 21

// =============================================================================
// Unique Node ID (derived from ESP32 MAC at boot)
// Used by home node to deduplicate detections from multiple remote nodes
// =============================================================================
static char nodeId[5] = "0000";  // 4-char hex, e.g. "A1B2"

static void generateNodeId() {
  uint8_t mac[6];
  esp_efuse_mac_get_default(mac);
  // Use last 2 bytes of factory MAC -> unique 4-hex-char ID per board
  snprintf(nodeId, sizeof(nodeId), "%02X%02X", mac[4], mac[5]);
}

// =============================================================================
// Non-blocking USB output ring
//
// Two tasks write USB output here (printerTask: detections/heartbeat,
// uartForwardTask: Heltec echoes), so all ring access is mutex-guarded.
// Nothing steady-state calls Serial.print directly. txFlush() pushes queued
// bytes only as far as availableForWrite() permits, so a host that stops
// reading can never stall the node. When the queue fills, the OLDEST bytes
// are dropped: a fresh detection is worth more than a stale one.
// =============================================================================
#define TXQ_SIZE       8192
#define TXQ_MAX_DRAIN  1024    // Max bytes pushed to USB per flush

static uint8_t  txq[TXQ_SIZE];
static volatile size_t txHead = 0;   // write index
static volatile size_t txTail = 0;   // read index
static uint32_t txDroppedBytes = 0;
static SemaphoreHandle_t txqMutex = nullptr;

static inline size_t txUsed() {
  return (txHead >= txTail) ? (txHead - txTail) : (TXQ_SIZE - txTail + txHead);
}

static inline size_t txFree() {
  return TXQ_SIZE - txUsed() - 1;   // keep one slot free to distinguish states
}

static void txWrite(const char* data, size_t len) {
  if (len == 0) return;
  if (len > TXQ_SIZE - 1) {           // absurdly long, keep the tail of it
    data += (len - (TXQ_SIZE - 1));
    len = TXQ_SIZE - 1;
  }
  if (!txqMutex) return;
  if (xSemaphoreTake(txqMutex, pdMS_TO_TICKS(50)) != pdTRUE) return;  // drop rather than block
  if (txFree() < len) {
    // Drop whole bytes from the oldest end until `len` bytes fit.
    while (txFree() < len && txUsed() > 0) {
      txTail = (txTail + 1) % TXQ_SIZE;
      txDroppedBytes++;
    }
  }
  for (size_t i = 0; i < len; i++) {
    txq[txHead] = (uint8_t)data[i];
    txHead = (txHead + 1) % TXQ_SIZE;
  }
  xSemaphoreGive(txqMutex);
}

static void txPrintln(const char* s) {
  txWrite(s, strlen(s));
  txWrite("\n", 1);
}

static void txPrintf(const char* fmt, ...) {
  char buf[256];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof(buf), fmt, ap);
  va_end(ap);
  if (n > 0) txWrite(buf, (size_t)min((int)sizeof(buf) - 1, n));
}

// Push queued bytes to USB, strictly bounded and strictly non-blocking.
static void txFlush() {
  size_t budget = TXQ_MAX_DRAIN;
  if (!txqMutex) return;
  if (xSemaphoreTake(txqMutex, pdMS_TO_TICKS(50)) != pdTRUE) return;
  while (txUsed() > 0 && budget > 0) {
    // availableForWrite() is how much the CDC ring will take right now.
    // Writing only that much guarantees write() returns without waiting.
    int room = Serial.availableForWrite();
    if (room <= 0) break;             // host not draining - try again next pass

    size_t chunk = txUsed();
    if (chunk > (size_t)room) chunk = (size_t)room;
    if (chunk > budget) chunk = budget;
    // Do not wrap past the end of the ring in one write
    if (txTail + chunk > TXQ_SIZE) chunk = TXQ_SIZE - txTail;

    size_t wrote = Serial.write(&txq[txTail], chunk);
    if (wrote == 0) break;            // made no progress, do not spin
    txTail = (txTail + wrote) % TXQ_SIZE;
    budget -= wrote;
  }
  xSemaphoreGive(txqMutex);
}

// =============================================================================
// UAV Tracking
// =============================================================================
struct uav_data {
  uint8_t  mac[6];
  int      rssi;
  uint32_t last_seen;
  char     op_id[ODID_ID_SIZE + 1];
  char     uav_id[ODID_ID_SIZE + 1];
  double   lat_d;
  double   long_d;
  double   base_lat_d;
  double   base_long_d;
  int      altitude_msl;
  int      height_agl;
  int      speed;
  int      heading;
  int      flag;
};

#define MAX_UAVS 32
static uav_data uavs[MAX_UAVS] = {0};
static BLEScan* pBLEScan = nullptr;
static ODID_UAS_Data UAS_data;
static unsigned long last_status = 0;

// Thread-safe print queue (BLE callback + WiFi ISR -> printer task)
static QueueHandle_t printQueue;

// Forward declarations
void callback(void *, wifi_promiscuous_pkt_type_t);

// =============================================================================
// UAV Slot Management
// =============================================================================
static uav_data* next_uav(uint8_t* mac) {
  // First: find existing entry for this MAC
  for (int i = 0; i < MAX_UAVS; i++) {
    if (memcmp(uavs[i].mac, mac, 6) == 0)
      return &uavs[i];
  }
  // Second: find empty slot
  for (int i = 0; i < MAX_UAVS; i++) {
    if (uavs[i].mac[0] == 0)
      return &uavs[i];
  }
  // Fallback: evict oldest entry
  uint32_t oldest_time = UINT32_MAX;
  int oldest_idx = 0;
  for (int i = 0; i < MAX_UAVS; i++) {
    if (uavs[i].last_seen < oldest_time) {
      oldest_time = uavs[i].last_seen;
      oldest_idx = i;
    }
  }
  return &uavs[oldest_idx];
}

// =============================================================================
// BLE Advertisement Callback - Open Drone ID over BLE
// =============================================================================
class DroneIDCallback : public BLEAdvertisedDeviceCallbacks {
public:
  void onResult(BLEAdvertisedDevice device) override {
    int len = device.getPayloadLength();
    if (len <= 5) return;

    uint8_t* payload = device.getPayload();
    // Check for ODID BLE service data: type=0x16, UUID=0xFFFA, counter=0x0D
    if (payload[1] != 0x16 || payload[2] != 0xFA ||
        payload[3] != 0xFF || payload[4] != 0x0D) return;

    uint8_t* mac = (uint8_t*)device.getAddress().getNative();
    uav_data* UAV = next_uav(mac);
    UAV->last_seen = millis();
    UAV->rssi = device.getRSSI();
    UAV->flag = 1;
    memcpy(UAV->mac, mac, 6);

    uint8_t* odid = &payload[6];
    switch (odid[0] & 0xF0) {
      case 0x00: {  // Basic ID
        ODID_BasicID_data basic;
        decodeBasicIDMessage(&basic, (ODID_BasicID_encoded*)odid);
        strncpy(UAV->uav_id, (char*)basic.UASID, ODID_ID_SIZE);
        break;
      }
      case 0x10: {  // Location
        ODID_Location_data loc;
        decodeLocationMessage(&loc, (ODID_Location_encoded*)odid);
        UAV->lat_d = loc.Latitude;
        UAV->long_d = loc.Longitude;
        UAV->altitude_msl = (int)loc.AltitudeGeo;
        UAV->height_agl = (int)loc.Height;
        UAV->speed = (int)loc.SpeedHorizontal;
        UAV->heading = (int)loc.Direction;
        break;
      }
      case 0x40: {  // System (operator location)
        ODID_System_data sys;
        decodeSystemMessage(&sys, (ODID_System_encoded*)odid);
        UAV->base_lat_d = sys.OperatorLatitude;
        UAV->base_long_d = sys.OperatorLongitude;
        break;
      }
      case 0x50: {  // Operator ID
        ODID_OperatorID_data op;
        decodeOperatorIDMessage(&op, (ODID_OperatorID_encoded*)odid);
        strncpy(UAV->op_id, (char*)op.OperatorId, ODID_ID_SIZE);
        break;
      }
    }

    // Queue for printing (non-blocking, ISR-safe)
    uav_data tmp = *UAV;
    if (printQueue) {
      BaseType_t woken = pdFALSE;
      xQueueSendFromISR(printQueue, &tmp, &woken);
      if (woken) portYIELD_FROM_ISR();
    }
  }
};

// =============================================================================
// WiFi Promiscuous Callback - Open Drone ID over WiFi (NAN + Beacon)
// =============================================================================
void callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;

  wifi_promiscuous_pkt_t *packet = (wifi_promiscuous_pkt_t *)buffer;
  uint8_t *payload = packet->payload;
  int length = packet->rx_ctrl.sig_len;

  // --- NAN Action Frame (WiFi Aware / Neighbor Awareness Networking) ---
  static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
  if (memcmp(nan_dest, &payload[4], 6) == 0) {
    // Zero first, same as the beacon path below: a NAN frame missing a
    // message type must not inherit stale fields from the previous frame.
    memset(&UAS_data, 0, sizeof(UAS_data));
    if (odid_wifi_receive_message_pack_nan_action_frame(&UAS_data, nullptr, payload, length) == 0) {
      uav_data UAV;
      memset(&UAV, 0, sizeof(UAV));
      memcpy(UAV.mac, &payload[10], 6);
      UAV.rssi = packet->rx_ctrl.rssi;
      UAV.last_seen = millis();

      if (UAS_data.BasicIDValid[0])
        strncpy(UAV.uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
      if (UAS_data.LocationValid) {
        UAV.lat_d = UAS_data.Location.Latitude;
        UAV.long_d = UAS_data.Location.Longitude;
        UAV.altitude_msl = (int)UAS_data.Location.AltitudeGeo;
        UAV.height_agl = (int)UAS_data.Location.Height;
        UAV.speed = (int)UAS_data.Location.SpeedHorizontal;
        UAV.heading = (int)UAS_data.Location.Direction;
      }
      if (UAS_data.SystemValid) {
        UAV.base_lat_d = UAS_data.System.OperatorLatitude;
        UAV.base_long_d = UAS_data.System.OperatorLongitude;
      }
      if (UAS_data.OperatorIDValid)
        strncpy(UAV.op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);

      uav_data* stored = next_uav(UAV.mac);
      *stored = UAV;
      stored->flag = 1;

      uav_data tmp = *stored;
      if (printQueue) {
        BaseType_t woken = pdFALSE;
        xQueueSendFromISR(printQueue, &tmp, &woken);
        if (woken) portYIELD_FROM_ISR();
      }
    }
    return;
  }

  // --- Beacon Frame with ODID vendor-specific IE ---
  if (payload[0] == 0x80) {
    int offset = 36;
    while (offset < length) {
      int typ = payload[offset];
      int len = payload[offset + 1];
      if (offset + len + 2 > length) break;  // bounds check

      if ((typ == 0xdd) &&
          (((payload[offset + 2] == 0x90 && payload[offset + 3] == 0x3a && payload[offset + 4] == 0xe6)) ||
           ((payload[offset + 2] == 0xfa && payload[offset + 3] == 0x0b && payload[offset + 4] == 0xbc)))) {
        int j = offset + 7;
        if (j < length) {
          memset(&UAS_data, 0, sizeof(UAS_data));
          odid_message_process_pack(&UAS_data, &payload[j], length - j);

          uav_data UAV;
          memset(&UAV, 0, sizeof(UAV));
          memcpy(UAV.mac, &payload[10], 6);
          UAV.rssi = packet->rx_ctrl.rssi;
          UAV.last_seen = millis();

          if (UAS_data.BasicIDValid[0])
            strncpy(UAV.uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
          if (UAS_data.LocationValid) {
            UAV.lat_d = UAS_data.Location.Latitude;
            UAV.long_d = UAS_data.Location.Longitude;
            UAV.altitude_msl = (int)UAS_data.Location.AltitudeGeo;
            UAV.height_agl = (int)UAS_data.Location.Height;
            UAV.speed = (int)UAS_data.Location.SpeedHorizontal;
            UAV.heading = (int)UAS_data.Location.Direction;
          }
          if (UAS_data.SystemValid) {
            UAV.base_lat_d = UAS_data.System.OperatorLatitude;
            UAV.base_long_d = UAS_data.System.OperatorLongitude;
          }
          if (UAS_data.OperatorIDValid)
            strncpy(UAV.op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);

          uav_data* stored = next_uav(UAV.mac);
          *stored = UAV;
          stored->flag = 1;

          uav_data tmp = *stored;
          if (printQueue) {
            BaseType_t woken = pdFALSE;
            xQueueSendFromISR(printQueue, &tmp, &woken);
            if (woken) portYIELD_FROM_ISR();
          }
        }
      }
      offset += len + 2;
    }
  }
}

// =============================================================================
// JSON Builder (shared format for USB + mesh, includes node_id)
// =============================================================================
// Coordinate text at full 6-decimal precision - identical value to what this
// firmware has always sent - with only trailing zeros and a trailing dot
// removed. "34.050000" -> "34.05" is the same number in fewer bytes, so this
// is lossless: no rounding, no reduced precision.
static int fmtCoord(char* out, int outSize, double v) {
  int n = snprintf(out, outSize, "%.6f", v);
  if (n <= 0 || n >= outSize) return n;
  int e = n - 1;
  while (e > 0 && out[e] == '0') e--;
  if (e > 0 && out[e] == '.') e--;
  out[e + 1] = '\0';
  return e + 1;
}

static int buildJson(char *buf, size_t bufSize, const uav_data *UAV) {
  char mac_str[18];
  snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
           UAV->mac[0], UAV->mac[1], UAV->mac[2],
           UAV->mac[3], UAV->mac[4], UAV->mac[5]);

  // The UAS ID is attacker-controlled over-the-air data. Keep only benign
  // characters so a hostile broadcast cannot break the JSON line or inject
  // extra fields into mesh-mapper.
  char safe_id[ODID_ID_SIZE + 1];
  int si = 0;
  for (int i = 0; UAV->uav_id[i] && si < ODID_ID_SIZE; i++) {
    char c = UAV->uav_id[i];
    if (isalnum((unsigned char)c) || c == '.' || c == '_' || c == '-' || c == ':') {
      safe_id[si++] = c;
    }
  }
  safe_id[si] = '\0';

  // Every byte here is LoRa airtime: a 233-byte packet costs ~2s on air at
  // Meshtastic's default LONG_FAST. Both savings below are LOSSLESS - no
  // value is rounded and no populated field is dropped:
  //   - coordinates keep full 6-decimal precision, only trailing zeros go
  //   - a field is omitted only when it carries nothing (pilot position not
  //     broadcast, empty UAS ID); absent and zero mean the same thing here
  // mesh-mapper reads every field with .get() and gates only on one of
  // mac/drone_lat/pilot_lat/basic_id being present, so omission is safe.
  char dlat[16], dlon[16];
  fmtCoord(dlat, sizeof(dlat), UAV->lat_d);
  fmtCoord(dlon, sizeof(dlon), UAV->long_d);

  int n = snprintf(buf, bufSize,
    "{\"mac\":\"%s\",\"rssi\":%d,\"drone_lat\":%s,\"drone_long\":%s,\"drone_altitude\":%d",
    mac_str, UAV->rssi, dlat, dlon, UAV->altitude_msl);
  if (n < 0 || n >= bufSize) return n;

  // Pilot position is frequently absent - omitting it saves ~40 bytes.
  if (UAV->base_lat_d != 0.0 || UAV->base_long_d != 0.0) {
    char plat[16], plon[16];
    fmtCoord(plat, sizeof(plat), UAV->base_lat_d);
    fmtCoord(plon, sizeof(plon), UAV->base_long_d);
    int m = snprintf(buf + n, bufSize - n,
                     ",\"pilot_lat\":%s,\"pilot_long\":%s", plat, plon);
    if (m < 0 || m >= bufSize - n) return n;
    n += m;
  }
  if (safe_id[0]) {
    int m = snprintf(buf + n, bufSize - n, ",\"basic_id\":\"%s\"", safe_id);
    if (m < 0 || m >= bufSize - n) return n;
    n += m;
  }
  int m = snprintf(buf + n, bufSize - n, ",\"node_id\":\"%s\"}", nodeId);
  if (m < 0 || m >= bufSize - n) return n;
  return n + m;
}

// =============================================================================
// JSON Output - Sends to USB Serial + UART (Heltec V3 mesh)
// =============================================================================
static void send_json(const uav_data *UAV) {
  char json[300];
  buildJson(json, sizeof(json), UAV);

  // USB Serial (local monitoring / direct connection to mesh-mapper.py).
  // Queued, never blocking - a stalled host must not back up mesh sending.
  txPrintln(json);

  // LED flash on detection (quick blink)
  digitalWrite(LED_PIN, LOW);   // ON (inverted)
}

// Send to the Heltec V3, paced. Meshtastic's serial module frames its input
// with readBytes(237 bytes / 250ms timeout): writes closer together than that
// are coalesced into one packet (and the overflow splits into a broken
// fragment), so one message per window is the fastest reliable rate. LoRa
// airtime cannot move messages faster than this anyway. Pending detections sit
// in a small queue; when it overflows the OLDEST is dropped - the home node's
// dedup makes a fresher position strictly better than a stale one.
#define MESH_SEND_INTERVAL_MS 350
#define MESH_QUEUE_DEPTH      4

static char     meshPending[MESH_QUEUE_DEPTH][300];
static int      meshPendHead = 0;      // next slot to send
static int      meshPendCount = 0;
static uint32_t lastMeshSend = 0;
static uint32_t meshDropped = 0;

// Only ever called from printerTask, so no locking is needed.
static void send_to_mesh(const uav_data *UAV) {
  int slot = (meshPendHead + meshPendCount) % MESH_QUEUE_DEPTH;
  if (meshPendCount == MESH_QUEUE_DEPTH) {
    meshPendHead = (meshPendHead + 1) % MESH_QUEUE_DEPTH;   // drop oldest
    meshPendCount--;
    meshDropped++;
  }
  buildJson(meshPending[slot], sizeof(meshPending[slot]), UAV);
  meshPendCount++;
}

static void meshQueueDrain() {
  if (meshPendCount == 0) return;
  uint32_t now = millis();
  if ((uint32_t)(now - lastMeshSend) < MESH_SEND_INTERVAL_MS) return;

  const char* msg = meshPending[meshPendHead];
  int len = strlen(msg);
  if (Serial1.availableForWrite() >= len + 2) {   // +2 for println's \r\n
    Serial1.println(msg);
    lastMeshSend = now;
    meshPendHead = (meshPendHead + 1) % MESH_QUEUE_DEPTH;
    meshPendCount--;
  }
}

// =============================================================================
// FreeRTOS Tasks
// =============================================================================

// Printer task: dequeues UAV data and outputs JSON (runs on core 1)
static void printerTask(void *param) {
  uav_data UAV;
  for (;;) {
    // Bounded wait instead of portMAX_DELAY so queued mesh messages keep
    // draining even when no new detections arrive.
    if (xQueueReceive(printQueue, &UAV, pdMS_TO_TICKS(100))) {
      send_json(&UAV);
      send_to_mesh(&UAV);
    }
    meshQueueDrain();
    txFlush();   // runs at least every 100ms even with zero detections
  }
}

// BLE scan task (runs on core 1)
static void bleScanTask(void *param) {
  for (;;) {
    pBLEScan->start(1, false);
    pBLEScan->clearResults();
    delay(100);
  }
}

// WiFi processing task - just keeps the task alive (runs on core 0)
static void wifiProcessTask(void *param) {
  for (;;) {
    delay(10);
  }
}

// UART forward task: anything the Heltec sends back gets echoed to USB
// (mesh acknowledgments, Meshtastic debug output, etc.)
static void uartForwardTask(void *param) {
  static char lineBuf[512];
  static int linePos = 0;

  for (;;) {
    while (Serial1.available()) {
      char c = Serial1.read();
      if (c == '\n' || c == '\r') {
        if (linePos > 0) {
          lineBuf[linePos] = '\0';
          txPrintln(lineBuf);   // queued, never blocking
          linePos = 0;
        }
      } else if (linePos < (int)sizeof(lineBuf) - 1) {
        lineBuf[linePos++] = c;
      }
    }
    delay(10);
  }
}

// =============================================================================
// Arduino Entry Points
// =============================================================================
void setup() {
  delay(3000);  // Boot delay (Meshtastic serial init timing)
  setCpuFrequencyMhz(160);

  // Generate unique node ID from ESP32 factory MAC
  generateNodeId();

  // Serial init
  Serial.begin(115200);
  // Without a TX ring buffer, availableForWrite() tops out at the 128-byte
  // hardware FIFO. The detection JSON is ~200 bytes, so the send gate
  // (availableForWrite() >= len) could NEVER pass and the remote node never
  // transmitted a single detection. The buffer must be set before begin().
  Serial1.setTxBufferSize(1024);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

  // LED init
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);  // OFF (inverted logic on XIAO)

  txPrintln("");
  txPrintln("Mesh Detect - Node Mode / REMOTE");
  txPrintf("Node ID: %s   WiFi + BLE -> mesh\n", nodeId);

  nvs_flash_init();

  // Everything the radio callbacks touch must exist BEFORE any radio is armed.
  // esp_wifi_set_promiscuous_rx_cb() starts delivering frames immediately, and
  // the callback pushes to printQueue - creating the queue afterwards left a
  // window where a frame arriving on a busy channel hit a null handle and
  // panicked at boot.
  printQueue = xQueueCreate(MAX_UAVS * 2, sizeof(uav_data));
  txqMutex = xSemaphoreCreateMutex();
  memset(uavs, 0, sizeof(uavs));

  // WiFi promiscuous mode for ODID NAN/Beacon frames
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(&callback);
  esp_wifi_set_channel(6, WIFI_SECOND_CHAN_NONE);

  // BLE scanner for ODID BLE advertisements
  BLEDevice::init("DroneID");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new DroneIDCallback());
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);

  // Launch FreeRTOS tasks on separate cores
  xTaskCreatePinnedToCore(bleScanTask,     "BLE",     10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(wifiProcessTask, "WiFi",    10000, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(printerTask,     "Print",   10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(uartForwardTask, "UART_FW",  4096, NULL, 1, NULL, 1);

  txPrintln("Scanning.");
  txPrintln("");
}

void loop() {
  unsigned long now = millis();

  // Heartbeat every 60 seconds (queued, never blocking)
  if (now - last_status > 60000UL) {
    txPrintln("{\"heartbeat\":\"remote_node active\"}");
    last_status = now;
  }

  // LED off after brief flash (set ON by send_json)
  static unsigned long ledOffTime = 0;
  static bool ledOn = false;
  if (digitalRead(LED_PIN) == LOW) {
    if (!ledOn) { ledOn = true; ledOffTime = now; }
    if (now - ledOffTime > 80) {
      digitalWrite(LED_PIN, HIGH);  // OFF
      ledOn = false;
    }
  }

  delay(10);
}
