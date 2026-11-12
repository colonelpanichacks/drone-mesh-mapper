/*
 * RemoteID Mesh Detect - Dual-Band Edition
 *
 * Supports ESP32-C5 (dual-band 2.4GHz + 5GHz WiFi 6) and ESP32-S3 (2.4GHz only)
 * Detects drones broadcasting RemoteID via WiFi (NAN/Beacon) and Bluetooth LE
 *
 * Output:
 *   USB Serial  - JSON lines for mesh-mapper.py
 *   Serial1 UART (TX=pad D4, RX=pad D5) - compact messages for Heltec/Meshtastic relay
 *
 * For ESP32-C5: Dual-band scanning with fast channel hopping across 2.4+5GHz
 * For ESP32-S3: Single-band 2.4GHz scanning (original behavior)
 *
 * Every line written to Serial1 is broadcast to the whole mesh by the Heltec
 * (Meshtastic serial module in TEXTMSG mode). Nothing else may write there.
 */

#if !defined(ARDUINO_ARCH_ESP32)
  #error "This program requires an ESP32"
#endif

#include <Arduino.h>
#include <HardwareSerial.h>
#include <NimBLEDevice.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_event.h>
#include <nvs_flash.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include <stdarg.h>

// ============================================================================
// UART Pins - same wiring as remoteid-mesh-dualcore (Heltec LoRa V3)
// ============================================================================

const int SERIAL1_TX_PIN = D4;  // pad D4 -> Heltec RX (GPIO23 on C5, GPIO5 on S3)
const int SERIAL1_RX_PIN = D5;  // pad D5 <- Heltec TX (GPIO24 on C5, GPIO6 on S3)

// ============================================================================
// Board-specific configuration
// ============================================================================

#if defined(CONFIG_IDF_TARGET_ESP32C5) || defined(ARDUINO_XIAO_ESP32C5)
  #define BOARD_IS_C5 1
  #define DUAL_BAND_ENABLED true
  #define BOARD_NAME "XIAO ESP32-C5 (Dual-Band)"
  // C5 is single-core RISC-V
  #define SINGLE_CORE 1
#else
  #define BOARD_IS_C5 0
  #define DUAL_BAND_ENABLED false
  #define BOARD_NAME "XIAO ESP32-S3 (2.4GHz)"
  #define SINGLE_CORE 0
#endif

#define FW_VERSION "1.2"

// ============================================================================
// Dual-Band Channel Configuration
// ============================================================================

#define CHANNEL_2_4GHZ 6

// 5GHz RemoteID channels (UNII-3). WiFi NAN RemoteID uses ch149 on 5GHz;
// beacon RemoteID can sit on any of these.
static const uint8_t channels_5ghz[] = {149, 153, 157, 161, 165};
#define NUM_5GHZ_CHANNELS (sizeof(channels_5ghz) / sizeof(channels_5ghz[0]))

// Dwell time per channel (ms). Total cycle = DWELL_TIME * (1 + NUM_5GHZ_CHANNELS) = 300ms
#define DWELL_TIME_MS 50

// ============================================================================
// WiFi Band Enum
// ============================================================================

enum WiFiBand {
  BAND_UNKNOWN = 0,
  BAND_2_4GHZ  = 1,
  BAND_5GHZ    = 2,
  BAND_BLE     = 3
};

static inline WiFiBand bandForChannel(uint8_t ch) {
  if (ch == 0) return BAND_UNKNOWN;
  return (ch <= 14) ? BAND_2_4GHZ : BAND_5GHZ;
}

// ============================================================================
// Data Structures
// ============================================================================

struct id_data {
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
  WiFiBand band;
  uint8_t  channel;
};

// ============================================================================
// Function Prototypes
// ============================================================================

void callback(void *, wifi_promiscuous_pkt_type_t);

// ============================================================================
// Global Variables
// ============================================================================

#define MAX_UAVS 32
id_data uavs[MAX_UAVS] = {0};
NimBLEScan* pBLEScan = nullptr;
ODID_UAS_Data UAS_data;
unsigned long last_status = 0;

static QueueHandle_t printQueue = nullptr;

// Health counters (reported in the 60s heartbeat)
static uint32_t queueDrops = 0;       // detections lost to a full printQueue
static uint32_t hopFailures = 0;      // esp_wifi_set_channel errors
static uint32_t frames24 = 0;         // RemoteID frames seen per band
static uint32_t frames5 = 0;
static uint32_t framesBle = 0;
static esp_err_t lastHopErr = ESP_OK;
static uint8_t lastHopErrChannel = 0;

// ============================================================================
// Non-blocking USB output ring
//
// On native USB (HWCDC) Serial.println blocks for up to ~2s when the host is
// not reading. Every USB write goes through this ring instead; txFlush() only
// pushes what the CDC buffer will take right now, so a stalled or absent host
// can never stall detection or mesh sending. When the ring fills, the OLDEST
// bytes are dropped: a fresh detection is worth more than a stale one.
// ============================================================================

#define TXQ_SIZE       8192
#define TXQ_MAX_DRAIN  1024

static uint8_t  txq[TXQ_SIZE];
static volatile size_t txHead = 0;
static volatile size_t txTail = 0;
static uint32_t txDroppedBytes = 0;
static SemaphoreHandle_t txqMutex = nullptr;

static inline size_t txUsed() {
  return (txHead >= txTail) ? (txHead - txTail) : (TXQ_SIZE - txTail + txHead);
}

static inline size_t txFree() {
  return TXQ_SIZE - txUsed() - 1;
}

static void txWrite(const char* data, size_t len) {
  if (len == 0 || !txqMutex) return;
  if (len > TXQ_SIZE - 1) {
    data += (len - (TXQ_SIZE - 1));
    len = TXQ_SIZE - 1;
  }
  if (xSemaphoreTake(txqMutex, pdMS_TO_TICKS(50)) != pdTRUE) return;
  while (txFree() < len && txUsed() > 0) {
    txTail = (txTail + 1) % TXQ_SIZE;
    txDroppedBytes++;
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

static void txFlush() {
  size_t budget = TXQ_MAX_DRAIN;
  if (!txqMutex) return;
  if (xSemaphoreTake(txqMutex, pdMS_TO_TICKS(50)) != pdTRUE) return;
  while (txUsed() > 0 && budget > 0) {
    int room = Serial.availableForWrite();
    if (room <= 0) break;
    size_t chunk = txUsed();
    if (chunk > (size_t)room) chunk = (size_t)room;
    if (chunk > budget) chunk = budget;
    if (txTail + chunk > TXQ_SIZE) chunk = TXQ_SIZE - txTail;
    size_t wrote = Serial.write(&txq[txTail], chunk);
    if (wrote == 0) break;
    txTail = (txTail + wrote) % TXQ_SIZE;
    budget -= wrote;
  }
  xSemaphoreGive(txqMutex);
}

// ============================================================================
// UAV Tracking
// ============================================================================

static const uint8_t ZERO_MAC[6] = {0};

id_data* next_uav(const uint8_t* mac) {
  for (int i = 0; i < MAX_UAVS; i++) {
    if (memcmp(uavs[i].mac, mac, 6) == 0)
      return &uavs[i];
  }
  for (int i = 0; i < MAX_UAVS; i++) {
    if (memcmp(uavs[i].mac, ZERO_MAC, 6) == 0)
      return &uavs[i];
  }
  // Evict the entry seen longest ago (rollover-safe age comparison)
  uint32_t now = millis();
  uint32_t oldest_age = 0;
  int oldest_idx = 0;
  for (int i = 0; i < MAX_UAVS; i++) {
    uint32_t age = now - uavs[i].last_seen;
    if (age >= oldest_age) {
      oldest_age = age;
      oldest_idx = i;
    }
  }
  memset(&uavs[oldest_idx], 0, sizeof(id_data));
  return &uavs[oldest_idx];
}

// Radio callbacks run in the WiFi and NimBLE host TASKS, not in an ISR, so
// the plain (non-FromISR) queue API with a zero timeout is the correct call.
static void queueDetection(const id_data* UAV) {
  if (!printQueue) return;
  if (xQueueSend(printQueue, UAV, 0) != pdTRUE) queueDrops++;
}

// ============================================================================
// BLE Scanning Callbacks (NimBLE 2.x)
// ============================================================================

#define ODID_MSG_LEN 25

class MyAdvertisedDeviceCallbacks : public NimBLEScanCallbacks {
public:
  void onResult(const NimBLEAdvertisedDevice* device) override {
    const std::vector<uint8_t>& payloadVec = device->getPayload();
    int len = (int)payloadVec.size();
    // AD header (len, 0x16, 0xFA, 0xFF), app code 0x0D, counter, then one
    // 25-byte ODID message. Anything shorter would read past the payload.
    if (len < 6 + ODID_MSG_LEN) return;

    const uint8_t* payload = payloadVec.data();
    if (!(payload[1] == 0x16 && payload[2] == 0xFA &&
          payload[3] == 0xFF && payload[4] == 0x0D)) return;

    const uint8_t* mac = device->getAddress().getBase()->val;
    id_data* UAV = next_uav(mac);
    UAV->last_seen = millis();
    UAV->rssi = device->getRSSI();
    memcpy(UAV->mac, mac, 6);
    UAV->band = BAND_BLE;
    UAV->channel = 0;

    const uint8_t* odid = &payload[6];
    switch (odid[0] & 0xF0) {
      case 0x00: {
        ODID_BasicID_data basic;
        decodeBasicIDMessage(&basic, (ODID_BasicID_encoded*) odid);
        strncpy(UAV->uav_id, (char*) basic.UASID, ODID_ID_SIZE);
        UAV->uav_id[ODID_ID_SIZE] = '\0';
        break;
      }
      case 0x10: {
        ODID_Location_data loc;
        decodeLocationMessage(&loc, (ODID_Location_encoded*) odid);
        UAV->lat_d = loc.Latitude;
        UAV->long_d = loc.Longitude;
        UAV->altitude_msl = (int) loc.AltitudeGeo;
        UAV->height_agl = (int) loc.Height;
        UAV->speed = (int) loc.SpeedHorizontal;
        UAV->heading = (int) loc.Direction;
        break;
      }
      case 0x40: {
        ODID_System_data sys;
        decodeSystemMessage(&sys, (ODID_System_encoded*) odid);
        UAV->base_lat_d = sys.OperatorLatitude;
        UAV->base_long_d = sys.OperatorLongitude;
        break;
      }
      case 0x50: {
        ODID_OperatorID_data op;
        decodeOperatorIDMessage(&op, (ODID_OperatorID_encoded*) odid);
        strncpy(UAV->op_id, (char*) op.OperatorId, ODID_ID_SIZE);
        UAV->op_id[ODID_ID_SIZE] = '\0';
        break;
      }
    }
    UAV->flag = 1;
    framesBle++;
    id_data tmp = *UAV;
    queueDetection(&tmp);
  }
};

// ============================================================================
// Output helpers
// ============================================================================

const char* bandToString(WiFiBand band) {
  switch (band) {
    case BAND_2_4GHZ: return "2.4GHz";
    case BAND_5GHZ:   return "5GHz";
    case BAND_BLE:    return "BLE";
    default:          return "unknown";
  }
}

// The UAS ID is attacker-controlled over-the-air data. Keep only benign
// characters so a hostile broadcast cannot break the JSON line, inject fields
// into mesh-mapper, or put arbitrary text on the mesh.
static void sanitizeId(char* out, size_t outSize, const char* in) {
  size_t o = 0;
  for (size_t i = 0; in[i] && i < ODID_ID_SIZE && o + 1 < outSize; i++) {
    char c = in[i];
    if (isalnum((unsigned char)c) || c == '.' || c == '_' || c == '-' || c == ':') {
      out[o++] = c;
    }
  }
  out[o] = '\0';
}

static void macToString(char* out, size_t outSize, const uint8_t* mac) {
  snprintf(out, outSize, "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

// ============================================================================
// JSON Output (USB Serial -> mesh-mapper.py)
// ============================================================================

void send_json_fast(const id_data *UAV) {
  char mac_str[18];
  macToString(mac_str, sizeof(mac_str), UAV->mac);
  char safe_id[ODID_ID_SIZE + 1];
  sanitizeId(safe_id, sizeof(safe_id), UAV->uav_id);

  char json_msg[320];
  snprintf(json_msg, sizeof(json_msg),
    "{\"mac\":\"%s\",\"rssi\":%d,\"band\":\"%s\",\"channel\":%d,"
    "\"drone_lat\":%.6f,\"drone_long\":%.6f,\"drone_altitude\":%d,"
    "\"pilot_lat\":%.6f,\"pilot_long\":%.6f,\"basic_id\":\"%s\"}",
    mac_str, UAV->rssi, bandToString(UAV->band), UAV->channel,
    UAV->lat_d, UAV->long_d, UAV->altitude_msl,
    UAV->base_lat_d, UAV->base_long_d, safe_id);
  txPrintln(json_msg);
}

// ============================================================================
// Compact Message Output (Serial1 UART -> Heltec/Meshtastic)
//
// Meshtastic's serial module frames its input with readBytes(237 bytes /
// 250ms timeout): writes closer together than that get coalesced into one
// packet and split into broken fragments. Messages sit in a small queue and
// go out at most one per MESH_SEND_INTERVAL_MS, without ever blocking the
// printer task (the old code slept 1s here, stalling USB output with it).
// ============================================================================

#define MESH_DETECTION_INTERVAL_MS 5000   // at most one drone report per 5s
#define MESH_SEND_INTERVAL_MS      1000   // gap between Drone and Pilot lines
#define MESH_QUEUE_DEPTH           4
#define MAX_MESH_SIZE              230

static char     meshPending[MESH_QUEUE_DEPTH][MAX_MESH_SIZE];
static int      meshPendHead = 0;
static int      meshPendCount = 0;
static uint32_t lastMeshSend = 0;
static uint32_t lastMeshDetection = 0;
static bool     meshDetectionSent = false;
static uint32_t meshDropped = 0;

// Only called from printerTask, so no locking is needed.
static void meshEnqueue(const char* msg) {
  int slot = (meshPendHead + meshPendCount) % MESH_QUEUE_DEPTH;
  if (meshPendCount == MESH_QUEUE_DEPTH) {
    meshPendHead = (meshPendHead + 1) % MESH_QUEUE_DEPTH;   // drop oldest
    meshPendCount--;
    meshDropped++;
    slot = (meshPendHead + meshPendCount) % MESH_QUEUE_DEPTH;
  }
  strncpy(meshPending[slot], msg, MAX_MESH_SIZE - 1);
  meshPending[slot][MAX_MESH_SIZE - 1] = '\0';
  meshPendCount++;
}

void print_compact_message(const id_data *UAV) {
  uint32_t now = millis();
  if (meshDetectionSent && (uint32_t)(now - lastMeshDetection) < MESH_DETECTION_INTERVAL_MS) return;
  lastMeshDetection = now;
  meshDetectionSent = true;

  char mac_str[18];
  macToString(mac_str, sizeof(mac_str), UAV->mac);
  char safe_id[ODID_ID_SIZE + 1];
  sanitizeId(safe_id, sizeof(safe_id), UAV->uav_id);

  char mesh_msg[MAX_MESH_SIZE];
  int n = snprintf(mesh_msg, sizeof(mesh_msg), "Drone[%s]: %s RSSI:%d",
                   bandToString(UAV->band), mac_str, UAV->rssi);
  if (safe_id[0] && n > 0 && n < MAX_MESH_SIZE) {
    n += snprintf(mesh_msg + n, sizeof(mesh_msg) - n, " ID:%s", safe_id);
  }
  if (UAV->lat_d != 0.0 && UAV->long_d != 0.0 && n > 0 && n < MAX_MESH_SIZE) {
    snprintf(mesh_msg + n, sizeof(mesh_msg) - n,
             " https://maps.google.com/?q=%.6f,%.6f", UAV->lat_d, UAV->long_d);
  }
  meshEnqueue(mesh_msg);

  if (UAV->base_lat_d != 0.0 && UAV->base_long_d != 0.0) {
    char pilot_msg[MAX_MESH_SIZE];
    snprintf(pilot_msg, sizeof(pilot_msg),
             "Pilot: https://maps.google.com/?q=%.6f,%.6f",
             UAV->base_lat_d, UAV->base_long_d);
    meshEnqueue(pilot_msg);
  }
}

static void meshQueueDrain() {
  if (meshPendCount == 0) return;
  uint32_t now = millis();
  if ((uint32_t)(now - lastMeshSend) < MESH_SEND_INTERVAL_MS) return;

  const char* msg = meshPending[meshPendHead];
  size_t len = strlen(msg);
  if ((size_t)Serial1.availableForWrite() >= len + 2) {   // +2 for \r\n
    Serial1.println(msg);
    lastMeshSend = now;
    meshPendHead = (meshPendHead + 1) % MESH_QUEUE_DEPTH;
    meshPendCount--;
  }
}

// ============================================================================
// Channel Hopping Task (C5 dual-band only)
// ============================================================================

#if DUAL_BAND_ENABLED
static void hopTo(uint8_t ch) {
  esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  if (err != ESP_OK) {
    hopFailures++;
    lastHopErr = err;
    lastHopErrChannel = ch;
  }
}

void channelHopTask(void *parameter) {
  for (;;) {
    hopTo(CHANNEL_2_4GHZ);
    vTaskDelay(pdMS_TO_TICKS(DWELL_TIME_MS));
    for (size_t i = 0; i < NUM_5GHZ_CHANNELS; i++) {
      hopTo(channels_5ghz[i]);
      vTaskDelay(pdMS_TO_TICKS(DWELL_TIME_MS));
    }
  }
}
#endif

// ============================================================================
// BLE Scan Task
// ============================================================================

void bleScanTask(void *parameter) {
  for (;;) {
    pBLEScan->getResults(1000, false);
    pBLEScan->clearResults();
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

// ============================================================================
// WiFi Promiscuous Mode Callback
// ============================================================================

static void processODIDData(id_data* UAV) {
  if (UAS_data.BasicIDValid[0]) {
    strncpy(UAV->uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
    UAV->uav_id[ODID_ID_SIZE] = '\0';
  }
  if (UAS_data.LocationValid) {
    UAV->lat_d = UAS_data.Location.Latitude;
    UAV->long_d = UAS_data.Location.Longitude;
    UAV->altitude_msl = (int)UAS_data.Location.AltitudeGeo;
    UAV->height_agl = (int)UAS_data.Location.Height;
    UAV->speed = (int)UAS_data.Location.SpeedHorizontal;
    UAV->heading = (int)UAS_data.Location.Direction;
  }
  if (UAS_data.SystemValid) {
    UAV->base_lat_d = UAS_data.System.OperatorLatitude;
    UAV->base_long_d = UAS_data.System.OperatorLongitude;
  }
  if (UAS_data.OperatorIDValid) {
    strncpy(UAV->op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);
    UAV->op_id[ODID_ID_SIZE] = '\0';
  }
}

static void storeAndQueue(id_data* UAV) {
  id_data* storedUAV = next_uav(UAV->mac);
  *storedUAV = *UAV;
  storedUAV->flag = 1;
  if (UAV->band == BAND_5GHZ) frames5++; else frames24++;
  id_data tmp = *storedUAV;
  queueDetection(&tmp);
}

static void fillFromPacket(id_data* UAV, const wifi_promiscuous_pkt_t* packet,
                           const uint8_t* payload) {
  memset(UAV, 0, sizeof(*UAV));
  memcpy(UAV->mac, &payload[10], 6);
  UAV->rssi = packet->rx_ctrl.rssi;
  UAV->last_seen = millis();
  // Tag with the channel the frame actually arrived on (8-bit field on the
  // C5), not the hop task's current target, which races the hop.
  UAV->channel = packet->rx_ctrl.channel;
  UAV->band = bandForChannel(UAV->channel);
}

void callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;

  wifi_promiscuous_pkt_t *packet = (wifi_promiscuous_pkt_t *)buffer;
  uint8_t *payload = packet->payload;
  int length = packet->rx_ctrl.sig_len;
  if (length < 24) return;   // shorter than a management header

  // NAN Action Frame (WiFi Aware RemoteID)
  static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
  if (memcmp(nan_dest, &payload[4], 6) == 0) {
    // A NAN frame missing a message type must not inherit stale fields.
    memset(&UAS_data, 0, sizeof(UAS_data));
    char nan_mac[6] = {0};
    if (odid_wifi_receive_message_pack_nan_action_frame(&UAS_data, nan_mac, payload, length) == 0) {
      id_data UAV;
      fillFromPacket(&UAV, packet, payload);
      processODIDData(&UAV);
      storeAndQueue(&UAV);
    }
  }
  // Beacon Frame with RemoteID Vendor Specific IE
  else if (payload[0] == 0x80) {
    int offset = 36;
    while (offset + 1 < length) {
      int typ = payload[offset];
      int len = payload[offset + 1];
      if (offset + 2 + len > length) break;   // truncated IE

      if (typ == 0xdd && len >= 5 &&
          ((payload[offset + 2] == 0x90 && payload[offset + 3] == 0x3a && payload[offset + 4] == 0xe6) ||
           (payload[offset + 2] == 0xfa && payload[offset + 3] == 0x0b && payload[offset + 4] == 0xbc))) {
        int j = offset + 7;
        int end = offset + 2 + len;
        if (j < end) {
          memset(&UAS_data, 0, sizeof(UAS_data));
          odid_message_process_pack(&UAS_data, &payload[j], end - j);

          id_data UAV;
          fillFromPacket(&UAV, packet, payload);
          processODIDData(&UAV);
          storeAndQueue(&UAV);
        }
      }
      offset += len + 2;
    }
  }
}

// ============================================================================
// Printer Task - outputs on both USB Serial and UART (mesh)
// ============================================================================

void printerTask(void *param) {
  id_data UAV;
  for (;;) {
    // Bounded wait so queued mesh lines and USB bytes keep draining even when
    // no new detections arrive.
    if (xQueueReceive(printQueue, &UAV, pdMS_TO_TICKS(100))) {
      send_json_fast(&UAV);
      print_compact_message(&UAV);
    }
    meshQueueDrain();
    txFlush();
  }
}

// ============================================================================
// Initialization
// ============================================================================

void initializeSerial() {
  Serial.begin(115200);
  txqMutex = xSemaphoreCreateMutex();   // before the first txPrintln

  // Without a TX ring buffer, availableForWrite() tops out at the 128-byte
  // hardware FIFO and long mesh lines would never pass the send gate.
  // Must be set before begin().
  Serial1.setTxBufferSize(1024);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

  txPrintln("");
  txPrintln("========================================");
  txPrintln("    RemoteID Mesh Detect - Dual-Band");
  txPrintln("========================================");
  txPrintf("Board: %s  fw %s\n", BOARD_NAME, FW_VERSION);
#if DUAL_BAND_ENABLED
  txPrintln("Mode:  DUAL-BAND (2.4GHz + 5GHz WiFi)");
#else
  txPrintln("Mode:  SINGLE-BAND (2.4GHz WiFi only)");
#endif
  txPrintln("Proto: WiFi NAN, WiFi Beacon, BLE");
  txPrintf("UART:  TX=GPIO%d, RX=GPIO%d -> Heltec\n", SERIAL1_TX_PIN, SERIAL1_RX_PIN);
  txPrintln("========================================");
}

void setup() {
#if !BOARD_IS_C5
  // S3 only: on the C5 a clock change here, before USB serial starts, left
  // the app silent and deaf to USB resets.
  setCpuFrequencyMhz(160);
#endif
  initializeSerial();
  // Give the USB host a moment to attach so the boot log is not lost.
  for (int i = 0; i < 15 && !Serial; i++) delay(100);
  txFlush();

  txFlush();
  nvs_flash_init();

  // Everything the radio callbacks touch must exist BEFORE any radio is
  // armed: the promiscuous callback starts firing immediately, and a frame
  // landing before the queue existed hit a null handle at boot.
  memset(uavs, 0, sizeof(uavs));
  printQueue = xQueueCreate(MAX_UAVS * 2, sizeof(id_data));

  // WiFi promiscuous mode
  txPrintln("[boot] wifi start");
  txFlush();
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

#if DUAL_BAND_ENABLED
  // The Arduino core sets AUTO on WiFi start, but older cores did not and
  // without it every 5GHz hop fails. Set it explicitly and report the result.
  esp_err_t bm = esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);
  txPrintf("Band mode AUTO (2.4+5GHz): %s\n", esp_err_to_name(bm));
#endif

  esp_wifi_set_promiscuous_rx_cb(&callback);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(CHANNEL_2_4GHZ, WIFI_SECOND_CHAN_NONE);

#if DUAL_BAND_ENABLED
  // Probe every 5GHz channel once at boot so a regulatory or band-mode
  // problem shows up on the console instead of silently never hopping.
  for (size_t i = 0; i < NUM_5GHZ_CHANNELS; i++) {
    esp_err_t err = esp_wifi_set_channel(channels_5ghz[i], WIFI_SECOND_CHAN_NONE);
    txPrintf("  5GHz ch%d: %s\n", channels_5ghz[i], esp_err_to_name(err));
  }
  esp_wifi_set_channel(CHANNEL_2_4GHZ, WIFI_SECOND_CHAN_NONE);
  txPrintf("WiFi promiscuous: 2.4GHz ch%d + 5GHz ch149-165, %dms dwell\n",
           CHANNEL_2_4GHZ, DWELL_TIME_MS);
#else
  txPrintf("WiFi promiscuous mode (fixed ch%d)\n", CHANNEL_2_4GHZ);
#endif

  txPrintln("[boot] ble start");
  txFlush();
  // BLE init (NimBLE 2.x)
  NimBLEDevice::init("DroneID");
  pBLEScan = NimBLEDevice::getScan();
  pBLEScan->setScanCallbacks(new MyAdvertisedDeviceCallbacks());
  pBLEScan->setActiveScan(true);
  txPrintln("BLE scanning initialized (NimBLE)");
  txFlush();

  // FreeRTOS tasks - C5 is single-core, S3 is dual-core
#if SINGLE_CORE
  xTaskCreate(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL);
  xTaskCreate(printerTask, "PrinterTask", 10000, NULL, 1, NULL);
  #if DUAL_BAND_ENABLED
  xTaskCreate(channelHopTask, "ChannelHopTask", 4096, NULL, 2, NULL);
  #endif
#else
  xTaskCreatePinnedToCore(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(printerTask, "PrinterTask", 10000, NULL, 1, NULL, 1);
#endif

  txPrintln("[+] Scanning for drones...");
  txFlush();
}

// ============================================================================
// Main Loop - 60s heartbeat
// ============================================================================

void loop() {
  unsigned long current_millis = millis();

  if ((current_millis - last_status) > 60000UL) {
    last_status = current_millis;
#if DUAL_BAND_ENABLED
    txPrintf("{\"status\":\"active\",\"mode\":\"dual-band\",\"bands\":[\"2.4GHz\",\"5GHz\",\"BLE\"],"
             "\"fw\":\"%s\",\"frames_24\":%lu,\"frames_5\":%lu,\"frames_ble\":%lu,"
             "\"hop_fail\":%lu,\"hop_err\":\"%s\",\"hop_err_ch\":%u,"
             "\"queue_drops\":%lu,\"mesh_drops\":%lu,\"usb_drops\":%lu}\n",
             FW_VERSION, (unsigned long)frames24, (unsigned long)frames5, (unsigned long)framesBle,
             (unsigned long)hopFailures, esp_err_to_name(lastHopErr), lastHopErrChannel,
             (unsigned long)queueDrops, (unsigned long)meshDropped, (unsigned long)txDroppedBytes);
#else
    txPrintf("{\"status\":\"active\",\"mode\":\"single-band\",\"bands\":[\"2.4GHz\",\"BLE\"],"
             "\"fw\":\"%s\",\"frames_24\":%lu,\"frames_ble\":%lu,"
             "\"queue_drops\":%lu,\"mesh_drops\":%lu,\"usb_drops\":%lu}\n",
             FW_VERSION, (unsigned long)frames24, (unsigned long)framesBle,
             (unsigned long)queueDrops, (unsigned long)meshDropped, (unsigned long)txDroppedBytes);
#endif
  }
  // Also drain USB from here, so console output never depends on a single
  // task staying alive (txFlush is mutex-guarded and non-blocking).
  txFlush();
  delay(100);
}
