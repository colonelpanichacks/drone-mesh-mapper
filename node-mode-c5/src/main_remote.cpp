/*
 * =============================================================================
 * MESH-DETECT NODE MODE - Drone Remote ID Detector + Mesh Sender
 * colonelpanichacks
 *
 * XIAO ESP32-C5 (RISC-V, single-core, WiFi 6 dual-band 2.4+5GHz, BLE 5)
 *
 * Scans for Open Drone ID broadcasts:
 *   - WiFi: NAN Action Frames + Beacon vendor-specific IEs (2.4 + 5 GHz)
 *   - BLE:  ODID BLE advertisements (service data 0xFFFA)
 *
 * Detected drone JSON sent to:
 *   - USB Serial (local monitoring / direct mesh-mapper.py connection)
 *   - UART on D4/D5 pads -> Heltec V3 running Meshtastic (mesh relay)
 *
 * JSON format:
 *   {"mac":"xx:xx:xx:xx:xx:xx","rssi":-50,"drone_lat":0.0,"drone_long":0.0,
 *    "drone_altitude":0,"pilot_lat":0.0,"pilot_long":0.0,"basic_id":"...",
 *    "node_id":"A1B2"}
 *
 * Pin mapping (mesh-detect PCB, same physical pads as S3/C3 versions):
 *   D4 (GPIO23) TX -> Heltec RX
 *   D5 (GPIO24) RX <- Heltec TX
 * =============================================================================
 */

#include <Arduino.h>
#include <HardwareSerial.h>
#include <NimBLEDevice.h>
#include <NimBLEScan.h>
#include <NimBLEAdvertisedDevice.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <nvs_flash.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include <esp_timer.h>
#include <esp_mac.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>

// =============================================================================
// Pin Definitions -- XIAO ESP32-C5 on mesh-detect PCB
// =============================================================================
// UART to Heltec V3 (Meshtastic) -- same physical D4/D5 pads as S3
static const int SERIAL1_TX_PIN = 23;  // D4 = GPIO23 on C5 -> Heltec RX
static const int SERIAL1_RX_PIN = 24;  // D5 = GPIO24 on C5 <- Heltec TX

// LED on XIAO ESP32-C5 (active HIGH, NOT inverted like S3)
#define LED_PIN          27
#define LED_ON           HIGH
#define LED_OFF          LOW

// =============================================================================
// Unique Node ID (derived from ESP32 MAC at boot)
// =============================================================================
static char nodeId[5] = "0000";

static void generateNodeId() {
    uint8_t mac[6];
    esp_efuse_mac_get_default(mac);
    snprintf(nodeId, sizeof(nodeId), "%02X%02X", mac[4], mac[5]);
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

#define MAX_UAVS 8
static uav_data uavs[MAX_UAVS] = {0};
static NimBLEScan* pBLEScan = nullptr;
static ODID_UAS_Data UAS_data;
static unsigned long last_status = 0;

// Thread-safe print queue (BLE callback + WiFi ISR -> printer task)
static QueueHandle_t printQueue;

// Forward declarations
void wifiCallback(void *, wifi_promiscuous_pkt_type_t);

// =============================================================================
// WiFi Channel Hopping -- Dual-band 2.4GHz + 5GHz
// =============================================================================
// 2.4GHz channels 1-13 + 5GHz channels commonly used for Remote ID
static const uint8_t channels_24g[] = { 1, 6, 11, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13 };
static const uint8_t channels_5g[]  = { 36, 40, 44, 48, 52, 56, 60, 64,
                                        100, 104, 108, 112, 116, 120, 124, 128,
                                        132, 136, 140, 144, 149, 153, 157, 161, 165 };
#define NUM_24G_CH  (sizeof(channels_24g) / sizeof(channels_24g[0]))
#define NUM_5G_CH   (sizeof(channels_5g) / sizeof(channels_5g[0]))

static uint8_t chanIdx24 = 0;
static uint8_t chanIdx5  = 0;
static bool    on5GHz    = false;
static unsigned long lastHop = 0;
#define HOP_INTERVAL_MS 100  // 100ms dwell -- halves full sweep time to ~7.5s

static void hopChannel() {
    // Alternate between 2.4GHz and 5GHz bands
    if (on5GHz) {
        esp_wifi_set_channel(channels_5g[chanIdx5], WIFI_SECOND_CHAN_NONE);
        chanIdx5 = (chanIdx5 + 1) % NUM_5G_CH;
    } else {
        esp_wifi_set_channel(channels_24g[chanIdx24], WIFI_SECOND_CHAN_NONE);
        chanIdx24 = (chanIdx24 + 1) % NUM_24G_CH;
    }
    on5GHz = !on5GHz;
}

// =============================================================================
// UAV Slot Management
// =============================================================================
static uav_data* next_uav(uint8_t* mac) {
    for (int i = 0; i < MAX_UAVS; i++) {
        if (memcmp(uavs[i].mac, mac, 6) == 0)
            return &uavs[i];
    }
    for (int i = 0; i < MAX_UAVS; i++) {
        if (uavs[i].mac[0] == 0)
            return &uavs[i];
    }
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
// BLE Advertisement Callback - Open Drone ID over BLE (NimBLE)
// =============================================================================
class DroneIDCallback : public NimBLEScanCallbacks {
public:
    void onResult(const NimBLEAdvertisedDevice* device) override {
        // Check for ODID service data: UUID 0xFFFA
        if (!device->haveServiceData()) return;
        std::string svcData = device->getServiceData(NimBLEUUID((uint16_t)0xFFFA));
        if (svcData.empty() || svcData.length() < 2) return;

        const uint8_t* odid = (const uint8_t*)svcData.data();
        // First byte after counter (0x0D) is the ODID message
        if (svcData.length() < 3) return;

        uint8_t mac[6];
        // Extract MAC bytes from address
        NimBLEAddress addr = device->getAddress();
        const uint8_t* addrVal = addr.getVal();
        memcpy(mac, addrVal, 6);

        uav_data* UAV = next_uav(mac);
        UAV->last_seen = millis();
        UAV->rssi = device->getRSSI();
        UAV->flag = 1;
        memcpy(UAV->mac, mac, 6);

        // Skip counter byte (0x0D), parse ODID message
        const uint8_t* msg = odid + 1;
        int msgLen = svcData.length() - 1;
        if (msgLen < 1) return;

        switch (msg[0] & 0xF0) {
            case 0x00: {
                ODID_BasicID_data basic;
                if (msgLen >= (int)sizeof(ODID_BasicID_encoded))
                    decodeBasicIDMessage(&basic, (ODID_BasicID_encoded*)msg);
                strncpy(UAV->uav_id, (char*)basic.UASID, ODID_ID_SIZE);
                break;
            }
            case 0x10: {
                ODID_Location_data loc;
                if (msgLen >= (int)sizeof(ODID_Location_encoded))
                    decodeLocationMessage(&loc, (ODID_Location_encoded*)msg);
                UAV->lat_d = loc.Latitude;
                UAV->long_d = loc.Longitude;
                UAV->altitude_msl = (int)loc.AltitudeGeo;
                UAV->height_agl = (int)loc.Height;
                UAV->speed = (int)loc.SpeedHorizontal;
                UAV->heading = (int)loc.Direction;
                break;
            }
            case 0x40: {
                ODID_System_data sys;
                if (msgLen >= (int)sizeof(ODID_System_encoded))
                    decodeSystemMessage(&sys, (ODID_System_encoded*)msg);
                UAV->base_lat_d = sys.OperatorLatitude;
                UAV->base_long_d = sys.OperatorLongitude;
                break;
            }
            case 0x50: {
                ODID_OperatorID_data op;
                if (msgLen >= (int)sizeof(ODID_OperatorID_encoded))
                    decodeOperatorIDMessage(&op, (ODID_OperatorID_encoded*)msg);
                strncpy(UAV->op_id, (char*)op.OperatorId, ODID_ID_SIZE);
                break;
            }
        }

        // Queue for output
        uav_data tmp = *UAV;
        xQueueSend(printQueue, &tmp, 0);
    }
};

// =============================================================================
// WiFi Promiscuous Callback - Open Drone ID over WiFi (NAN + Beacon)
// =============================================================================
void wifiCallback(void *buffer, wifi_promiscuous_pkt_type_t type) {
    if (type != WIFI_PKT_MGMT) return;

    wifi_promiscuous_pkt_t *packet = (wifi_promiscuous_pkt_t *)buffer;
    uint8_t *payload = packet->payload;
    int length = packet->rx_ctrl.sig_len;

    // --- NAN Action Frame ---
    static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
    if (memcmp(nan_dest, &payload[4], 6) == 0) {
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
            BaseType_t woken = pdFALSE;
            xQueueSendFromISR(printQueue, &tmp, &woken);
            if (woken) portYIELD_FROM_ISR();
        }
        return;
    }

    // --- Beacon Frame with ODID vendor-specific IE ---
    if (payload[0] == 0x80) {
        int offset = 36;
        while (offset < length) {
            int typ = payload[offset];
            int len = payload[offset + 1];
            if (offset + len + 2 > length) break;

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
                    BaseType_t woken = pdFALSE;
                    xQueueSendFromISR(printQueue, &tmp, &woken);
                    if (woken) portYIELD_FROM_ISR();
                }
            }
            offset += len + 2;
        }
    }
}

// =============================================================================
// JSON Builder
// =============================================================================
static int buildJson(char *buf, size_t bufSize, const uav_data *UAV) {
    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
             UAV->mac[0], UAV->mac[1], UAV->mac[2],
             UAV->mac[3], UAV->mac[4], UAV->mac[5]);

    return snprintf(buf, bufSize,
        "{\"mac\":\"%s\",\"rssi\":%d,\"drone_lat\":%.6f,\"drone_long\":%.6f,"
        "\"drone_altitude\":%d,\"pilot_lat\":%.6f,\"pilot_long\":%.6f,"
        "\"basic_id\":\"%s\",\"node_id\":\"%s\"}",
        mac_str, UAV->rssi, UAV->lat_d, UAV->long_d, UAV->altitude_msl,
        UAV->base_lat_d, UAV->base_long_d, UAV->uav_id, nodeId);
}

// =============================================================================
// Output: USB Serial + UART to Heltec mesh
// =============================================================================
static void send_json(const uav_data *UAV) {
    char json[300];
    buildJson(json, sizeof(json), UAV);
    Serial.println(json);
    digitalWrite(LED_PIN, LED_ON);
}

static void send_to_mesh(const uav_data *UAV) {
    char json[300];
    int len = buildJson(json, sizeof(json), UAV);
    if (Serial1.availableForWrite() >= len) {
        Serial1.println(json);
    }
}

// =============================================================================
// FreeRTOS Tasks (all on single core for C5)
// =============================================================================

// Printer task: dequeues UAV data and outputs JSON
static void printerTask(void *param) {
    uav_data UAV;
    for (;;) {
        if (xQueueReceive(printQueue, &UAV, portMAX_DELAY)) {
            send_json(&UAV);
            send_to_mesh(&UAV);
        }
    }
}

// BLE scan task
static void bleScanTask(void *param) {
    for (;;) {
        pBLEScan->start(1, false);
        pBLEScan->clearResults();
        delay(100);
    }
}

// UART forward task: anything the Heltec sends back -> USB serial
static void uartForwardTask(void *param) {
    static char lineBuf[512];
    static int linePos = 0;

    for (;;) {
        while (Serial1.available()) {
            char c = Serial1.read();
            if (c == '\n' || c == '\r') {
                if (linePos > 0) {
                    lineBuf[linePos] = '\0';
                    Serial.println(lineBuf);
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
// Arduino Setup
// =============================================================================
void setup() {
    delay(3000);  // Boot delay (Meshtastic serial init timing)

    generateNodeId();

    // Serial init
    Serial.begin(115200);
    Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

    // LED init
    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LED_OFF);

    Serial.println();
    Serial.println("================================================");
    Serial.println("  MESH-DETECT NODE - XIAO ESP32-C5");
    Serial.printf("  Node ID: %s\n", nodeId);
    Serial.println("  WiFi 6 Dual-Band + BLE 5 Remote ID Detection");
    Serial.println("  UART D4/D5 -> Heltec V3 Meshtastic Mesh");
    Serial.printf("  TX=GPIO%d  RX=GPIO%d\n", SERIAL1_TX_PIN, SERIAL1_RX_PIN);
    Serial.println("================================================");

    nvs_flash_init();

    // WiFi promiscuous mode for ODID NAN/Beacon frames
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_promiscuous_rx_cb(&wifiCallback);
    esp_wifi_set_channel(6, WIFI_SECOND_CHAN_NONE);
    Serial.println("[NODE] WiFi promiscuous mode active (dual-band hopping)");

    // Print channel list
    Serial.printf("[NODE] 2.4GHz channels: ");
    for (int i = 0; i < (int)NUM_24G_CH; i++) Serial.printf("%d ", channels_24g[i]);
    Serial.println();
    Serial.printf("[NODE] 5GHz channels: ");
    for (int i = 0; i < (int)NUM_5G_CH; i++) Serial.printf("%d ", channels_5g[i]);
    Serial.println();

    // BLE scanner for ODID BLE advertisements (NimBLE)
    NimBLEDevice::init("");
    pBLEScan = NimBLEDevice::getScan();
    pBLEScan->setScanCallbacks(new DroneIDCallback());
    pBLEScan->setActiveScan(true);
    pBLEScan->setInterval(100);
    pBLEScan->setWindow(99);
    Serial.println("[NODE] BLE scanner active (NimBLE)");

    // Print queue
    printQueue = xQueueCreate(MAX_UAVS * 2, sizeof(uav_data));

    // Clear tracking array
    memset(uavs, 0, sizeof(uavs));

    // Launch tasks (single core, priority-based scheduling)
    xTaskCreate(bleScanTask,     "BLE",     8192, NULL, 1, NULL);
    xTaskCreate(printerTask,     "Print",   8192, NULL, 2, NULL);
    xTaskCreate(uartForwardTask, "UART_FW", 4096, NULL, 1, NULL);

    Serial.println("[NODE] All tasks launched - scanning for drones...\n");
}

// =============================================================================
// Arduino Loop
// =============================================================================
void loop() {
    unsigned long now = millis();

    // Channel hopping (dual-band)
    if (now - lastHop >= HOP_INTERVAL_MS) {
        hopChannel();
        lastHop = now;
    }

    // Heartbeat every 60 seconds
    if (now - last_status > 60000UL) {
        int active = 0;
        for (int i = 0; i < MAX_UAVS; i++) {
            if (uavs[i].mac[0] != 0 && (now - uavs[i].last_seen) < 120000)
                active++;
        }
        Serial.printf("{\"heartbeat\":\"node_active\",\"node_id\":\"%s\",\"tracked\":%d}\n",
                      nodeId, active);
        last_status = now;
    }

    // LED off after brief flash
    static unsigned long ledOffTime = 0;
    static bool ledOn = false;
    if (digitalRead(LED_PIN) == LED_ON) {
        if (!ledOn) { ledOn = true; ledOffTime = now; }
        if (now - ledOffTime > 80) {
            digitalWrite(LED_PIN, LED_OFF);
            ledOn = false;
        }
    }

    delay(10);
}
