/*
 * RemoteID Mesh Detect — Dual-Band Edition
 *
 * Supports ESP32-C5 (dual-band 2.4GHz + 5GHz WiFi 6) and ESP32-S3 (2.4GHz only)
 * Detects drones broadcasting RemoteID via WiFi (NAN/Beacon) and Bluetooth LE
 *
 * Output:
 *   USB Serial  — JSON lines for mesh-mapper.py
 *   Serial1 UART (TX=GPIO5, RX=GPIO6) — compact messages for Heltec/Meshtastic relay
 *
 * For ESP32-C5: Dual-band scanning with fast channel hopping across 2.4+5GHz
 * For ESP32-S3: Single-band 2.4GHz scanning (original behavior)
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

// ============================================================================
// UART Pins — same wiring as remoteid-mesh-dualcore (Heltec LoRa V3)
// ============================================================================

const int SERIAL1_TX_PIN = 5;   // GPIO5 → Heltec RX
const int SERIAL1_RX_PIN = 6;   // GPIO6 → Heltec TX

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

// ============================================================================
// Dual-Band Channel Configuration
// ============================================================================

#define CHANNEL_2_4GHZ 6

// 5GHz RemoteID channels (UNII-3 band — commonly used for RemoteID)
static const uint8_t channels_5ghz[] = {149, 153, 157, 161, 165};
#define NUM_5GHZ_CHANNELS (sizeof(channels_5ghz) / sizeof(channels_5ghz[0]))

// Dwell time per channel (ms). Total cycle ≈ DWELL_TIME * (1 + NUM_5GHZ_CHANNELS) ≈ 300ms
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
void send_json_fast(const id_data *UAV);
void print_compact_message(const id_data *UAV);

// ============================================================================
// Global Variables
// ============================================================================

#define MAX_UAVS 8
id_data uavs[MAX_UAVS] = {0};
NimBLEScan* pBLEScan = nullptr;
ODID_UAS_Data UAS_data;
unsigned long last_status = 0;

// Current channel tracking (for dual-band)
volatile uint8_t current_channel = CHANNEL_2_4GHZ;
volatile WiFiBand current_band = BAND_2_4GHZ;
static portMUX_TYPE channelMux = portMUX_INITIALIZER_UNLOCKED;

static QueueHandle_t printQueue;

// ============================================================================
// UAV Tracking
// ============================================================================

id_data* next_uav(const uint8_t* mac) {
  for (int i = 0; i < MAX_UAVS; i++) {
    if (memcmp(uavs[i].mac, mac, 6) == 0)
      return &uavs[i];
  }
  for (int i = 0; i < MAX_UAVS; i++) {
    if (uavs[i].mac[0] == 0)
      return &uavs[i];
  }
  // Evict oldest entry
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

// ============================================================================
// BLE Scanning Callbacks (NimBLE 2.1.0)
// ============================================================================

class MyAdvertisedDeviceCallbacks : public NimBLEScanCallbacks {
public:
  void onResult(const NimBLEAdvertisedDevice* device) override {
    const std::vector<uint8_t>& payloadVec = device->getPayload();
    int len = (int)payloadVec.size();
    if (len <= 0) return;

    const uint8_t* payload = payloadVec.data();
    // RemoteID BLE advertisement: Service UUID 0xFFFA, type 0x0D
    if (len > 5 && payload[1] == 0x16 && payload[2] == 0xFA &&
        payload[3] == 0xFF && payload[4] == 0x0D) {
      const uint8_t* mac = device->getAddress().getBase()->val;
      id_data* UAV = next_uav(mac);
      UAV->last_seen = millis();
      UAV->rssi = device->getRSSI();
      memcpy(UAV->mac, (const uint8_t*)mac, 6);
      UAV->band = BAND_BLE;
      UAV->channel = 0;

      const uint8_t* odid = &payload[6];
      switch (odid[0] & 0xF0) {
        case 0x00: {
          ODID_BasicID_data basic;
          decodeBasicIDMessage(&basic, (ODID_BasicID_encoded*) odid);
          strncpy(UAV->uav_id, (char*) basic.UASID, ODID_ID_SIZE);
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
          break;
        }
      }
      UAV->flag = 1;
      {
        id_data tmp = *UAV;
        BaseType_t xHigherPriorityTaskWoken = pdFALSE;
        xQueueSendFromISR(printQueue, &tmp, &xHigherPriorityTaskWoken);
        if (xHigherPriorityTaskWoken) portYIELD_FROM_ISR();
      }
    }
  }
};

// ============================================================================
// JSON Output (USB Serial → mesh-mapper.py)
// ============================================================================

const char* bandToString(WiFiBand band) {
  switch (band) {
    case BAND_2_4GHZ: return "2.4GHz";
    case BAND_5GHZ:   return "5GHz";
    case BAND_BLE:    return "BLE";
    default:          return "unknown";
  }
}

void send_json_fast(const id_data *UAV) {
  char mac_str[18];
  snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
           UAV->mac[0], UAV->mac[1], UAV->mac[2],
           UAV->mac[3], UAV->mac[4], UAV->mac[5]);
  char json_msg[320];
  snprintf(json_msg, sizeof(json_msg),
    "{\"mac\":\"%s\",\"rssi\":%d,\"band\":\"%s\",\"channel\":%d,"
    "\"drone_lat\":%.6f,\"drone_long\":%.6f,\"drone_altitude\":%d,"
    "\"pilot_lat\":%.6f,\"pilot_long\":%.6f,\"basic_id\":\"%s\"}",
    mac_str, UAV->rssi, bandToString(UAV->band), UAV->channel,
    UAV->lat_d, UAV->long_d, UAV->altitude_msl,
    UAV->base_lat_d, UAV->base_long_d, UAV->uav_id);
  Serial.println(json_msg);
}

// ============================================================================
// Compact Message Output (Serial1 UART → Heltec/Meshtastic)
// ============================================================================

void print_compact_message(const id_data *UAV) {
  static unsigned long lastSendTime = 0;
  const unsigned long sendInterval = 5000;
  const int MAX_MESH_SIZE = 230;

  if (millis() - lastSendTime < sendInterval) return;
  lastSendTime = millis();

  char mac_str[18];
  snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
           UAV->mac[0], UAV->mac[1], UAV->mac[2],
           UAV->mac[3], UAV->mac[4], UAV->mac[5]);

  char mesh_msg[MAX_MESH_SIZE];
  int msg_len = 0;
  msg_len += snprintf(mesh_msg + msg_len, sizeof(mesh_msg) - msg_len,
                      "Drone[%s]: %s RSSI:%d",
                      bandToString(UAV->band), mac_str, UAV->rssi);
  if (msg_len < MAX_MESH_SIZE && UAV->lat_d != 0.0 && UAV->long_d != 0.0) {
    msg_len += snprintf(mesh_msg + msg_len, sizeof(mesh_msg) - msg_len,
                        " https://maps.google.com/?q=%.6f,%.6f",
                        UAV->lat_d, UAV->long_d);
  }
  if (Serial1.availableForWrite() >= msg_len) {
    Serial1.println(mesh_msg);
  }

  delay(1000);
  if (UAV->base_lat_d != 0.0 && UAV->base_long_d != 0.0) {
    char pilot_msg[MAX_MESH_SIZE];
    int pilot_len = snprintf(pilot_msg, sizeof(pilot_msg),
                             "Pilot: https://maps.google.com/?q=%.6f,%.6f",
                             UAV->base_lat_d, UAV->base_long_d);
    if (Serial1.availableForWrite() >= pilot_len) {
      Serial1.println(pilot_msg);
    }
  }
}

// ============================================================================
// Channel Hopping Task (C5 dual-band only)
// ============================================================================

#if DUAL_BAND_ENABLED
void channelHopTask(void *parameter) {
  uint8_t channel_index = 0;
  bool on_5ghz = false;

  Serial.println("[DUAL-BAND] Channel hopping active");
  Serial.printf("[DUAL-BAND] 2.4GHz ch%d + 5GHz ch", CHANNEL_2_4GHZ);
  for (int i = 0; i < (int)NUM_5GHZ_CHANNELS; i++) {
    Serial.printf("%d%s", channels_5ghz[i], (i < (int)NUM_5GHZ_CHANNELS - 1) ? "," : "\n");
  }

  for (;;) {
    uint8_t next_channel;
    WiFiBand next_band;

    if (!on_5ghz) {
      // Jump to first 5GHz channel
      next_channel = channels_5ghz[0];
      next_band = BAND_5GHZ;
      channel_index = 0;
      on_5ghz = true;
    } else {
      channel_index++;
      if (channel_index >= NUM_5GHZ_CHANNELS) {
        // Return to 2.4GHz
        next_channel = CHANNEL_2_4GHZ;
        next_band = BAND_2_4GHZ;
        on_5ghz = false;
      } else {
        next_channel = channels_5ghz[channel_index];
        next_band = BAND_5GHZ;
      }
    }

    portENTER_CRITICAL(&channelMux);
    current_channel = next_channel;
    current_band = next_band;
    portEXIT_CRITICAL(&channelMux);

    esp_wifi_set_channel(next_channel, WIFI_SECOND_CHAN_NONE);
    vTaskDelay(pdMS_TO_TICKS(DWELL_TIME_MS));
  }
}
#endif

// ============================================================================
// BLE Scan Task
// ============================================================================

void bleScanTask(void *parameter) {
  for (;;) {
    NimBLEScanResults foundDevices = pBLEScan->getResults(1000, false);
    pBLEScan->clearResults();
    delay(100);
  }
}

// ============================================================================
// WiFi Process Task (keeps promiscuous callback alive)
// ============================================================================

void wifiProcessTask(void *parameter) {
  for (;;) {
    delay(10);
  }
}

// ============================================================================
// WiFi Promiscuous Mode Callback
// ============================================================================

static void processODIDData(id_data* UAV) {
  if (UAS_data.BasicIDValid[0])
    strncpy(UAV->uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
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
  if (UAS_data.OperatorIDValid)
    strncpy(UAV->op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);
}

static void storeAndQueue(id_data* UAV) {
  id_data* storedUAV = next_uav(UAV->mac);
  *storedUAV = *UAV;
  storedUAV->flag = 1;
  {
    id_data tmp = *storedUAV;
    BaseType_t xHigherPriorityTaskWoken = pdFALSE;
    xQueueSendFromISR(printQueue, &tmp, &xHigherPriorityTaskWoken);
    if (xHigherPriorityTaskWoken) portYIELD_FROM_ISR();
  }
}

void callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;

  wifi_promiscuous_pkt_t *packet = (wifi_promiscuous_pkt_t *)buffer;
  uint8_t *payload = packet->payload;
  int length = packet->rx_ctrl.sig_len;

  // Get current channel/band info (thread-safe)
  uint8_t detect_channel;
  WiFiBand detect_band;
  portENTER_CRITICAL_ISR(&channelMux);
  detect_channel = current_channel;
  detect_band = current_band;
  portEXIT_CRITICAL_ISR(&channelMux);

  // NAN Action Frame (WiFi Aware RemoteID)
  static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
  if (memcmp(nan_dest, &payload[4], 6) == 0) {
    char nan_mac[6] = {0};
    if (odid_wifi_receive_message_pack_nan_action_frame(&UAS_data, nan_mac, payload, length) == 0) {
      id_data UAV;
      memset(&UAV, 0, sizeof(UAV));
      memcpy(UAV.mac, &payload[10], 6);
      UAV.rssi = packet->rx_ctrl.rssi;
      UAV.last_seen = millis();
      UAV.band = detect_band;
      UAV.channel = detect_channel;
      processODIDData(&UAV);
      storeAndQueue(&UAV);
    }
  }
  // Beacon Frame with RemoteID Vendor Specific IE
  else if (payload[0] == 0x80) {
    int offset = 36;
    while (offset < length) {
      int typ = payload[offset];
      int len = payload[offset + 1];

      if ((typ == 0xdd) &&
          (((payload[offset + 2] == 0x90 && payload[offset + 3] == 0x3a && payload[offset + 4] == 0xe6)) ||
           ((payload[offset + 2] == 0xfa && payload[offset + 3] == 0x0b && payload[offset + 4] == 0xbc)))) {
        int j = offset + 7;
        if (j < length) {
          memset(&UAS_data, 0, sizeof(UAS_data));
          odid_message_process_pack(&UAS_data, &payload[j], length - j);

          id_data UAV;
          memset(&UAV, 0, sizeof(UAV));
          memcpy(UAV.mac, &payload[10], 6);
          UAV.rssi = packet->rx_ctrl.rssi;
          UAV.last_seen = millis();
          UAV.band = detect_band;
          UAV.channel = detect_channel;
          processODIDData(&UAV);
          storeAndQueue(&UAV);
        }
      }
      offset += len + 2;
    }
  }
}

// ============================================================================
// Printer Task — outputs on both USB Serial and UART (mesh)
// ============================================================================

void printerTask(void *param) {
  id_data UAV;
  for (;;) {
    if (xQueueReceive(printQueue, &UAV, portMAX_DELAY)) {
      send_json_fast(&UAV);
      print_compact_message(&UAV);
    }
  }
}

// ============================================================================
// Initialization
// ============================================================================

void initializeSerial() {
  Serial.begin(115200);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  delay(100);

  Serial.println("\n========================================");
  Serial.println("    RemoteID Mesh Detect — Dual-Band");
  Serial.println("========================================");
  Serial.printf("Board: %s\n", BOARD_NAME);
#if DUAL_BAND_ENABLED
  Serial.println("Mode:  DUAL-BAND (2.4GHz + 5GHz WiFi)");
#else
  Serial.println("Mode:  SINGLE-BAND (2.4GHz WiFi only)");
#endif
  Serial.println("Proto: WiFi NAN, WiFi Beacon, BLE");
  Serial.printf("UART:  TX=GPIO%d, RX=GPIO%d → Heltec\n", SERIAL1_TX_PIN, SERIAL1_RX_PIN);
  Serial.println("========================================\n");
}

void setup() {
  setCpuFrequencyMhz(160);
  initializeSerial();

  nvs_flash_init();

  // WiFi promiscuous mode
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(&callback);
  esp_wifi_set_channel(CHANNEL_2_4GHZ, WIFI_SECOND_CHAN_NONE);

#if DUAL_BAND_ENABLED
  Serial.printf("WiFi promiscuous mode (starting 2.4GHz ch%d, hopping enabled)\n", CHANNEL_2_4GHZ);
#else
  Serial.printf("WiFi promiscuous mode (fixed ch%d)\n", CHANNEL_2_4GHZ);
#endif

  // BLE init (NimBLE 2.1.0)
  NimBLEDevice::init("DroneID");
  pBLEScan = NimBLEDevice::getScan();
  pBLEScan->setScanCallbacks(new MyAdvertisedDeviceCallbacks());
  pBLEScan->setActiveScan(true);
  Serial.println("BLE scanning initialized (NimBLE)");

  // Print queue
  printQueue = xQueueCreate(MAX_UAVS, sizeof(id_data));

  // FreeRTOS tasks — C5 is single-core, S3 is dual-core
#if SINGLE_CORE
  xTaskCreate(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL);
  xTaskCreate(wifiProcessTask, "WiFiProcessTask", 10000, NULL, 1, NULL);
  xTaskCreate(printerTask, "PrinterTask", 10000, NULL, 1, NULL);
  #if DUAL_BAND_ENABLED
  xTaskCreate(channelHopTask, "ChannelHopTask", 4096, NULL, 2, NULL);
  #endif
#else
  xTaskCreatePinnedToCore(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(wifiProcessTask, "WiFiProcessTask", 10000, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(printerTask, "PrinterTask", 10000, NULL, 1, NULL, 1);
#endif

  memset(uavs, 0, sizeof(uavs));

  Serial.println("\n[+] Scanning for drones...\n");
}

// ============================================================================
// Main Loop
// ============================================================================

void loop() {
  unsigned long current_millis = millis();

  if ((current_millis - last_status) > 60000UL) {
#if DUAL_BAND_ENABLED
    Serial.println("{\"status\":\"active\",\"mode\":\"dual-band\",\"bands\":[\"2.4GHz\",\"5GHz\",\"BLE\"]}");
#else
    Serial.println("{\"status\":\"active\",\"mode\":\"single-band\",\"bands\":[\"2.4GHz\",\"BLE\"]}");
#endif
    last_status = current_millis;
  }
}
