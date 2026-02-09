/*
 * =============================================================================
 * Drone Mesh Mapper - Remote ID to Mesh (XIAO ESP32-C5)
 * colonelpanichacks
 *
 * WiFi 6 dual-band (2.4GHz + 5GHz) + BLE 5 Remote ID detection
 * Single-core RISC-V port of remoteid-mesh-dualcore (S3/C6)
 *
 * Sends:
 *   - Google Maps links over UART to Heltec V3 Meshtastic (human-readable)
 *   - JSON over USB Serial for mesh-mapper.py
 *
 * Pin mapping (mesh-detect PCB, same physical D4/D5 pads as S3):
 *   D4 (GPIO23) TX -> Heltec RX
 *   D5 (GPIO24) RX <- Heltec TX
 * =============================================================================
 */

#if !defined(ARDUINO_ARCH_ESP32)
  #error "This program requires an ESP32-C5"
#endif

#include <Arduino.h>
#include <HardwareSerial.h>
#include <NimBLEDevice.h>
#include <NimBLEScan.h>
#include <NimBLEAdvertisedDevice.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_event.h>
#include <nvs_flash.h>
#include <esp_mac.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>

// =============================================================================
// Pin Definitions -- XIAO ESP32-C5 on mesh-detect PCB
// =============================================================================
static const int SERIAL1_TX_PIN = 23;  // D4 = GPIO23 -> Heltec RX
static const int SERIAL1_RX_PIN = 24;  // D5 = GPIO24 <- Heltec TX

// =============================================================================
// WiFi Channel Hopping -- Dual-band 2.4GHz + 5GHz
// =============================================================================
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
#define HOP_INTERVAL_MS 200

static void hopChannel() {
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
// UAV Tracking
// =============================================================================
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
};

#define MAX_UAVS 8
static id_data uavs[MAX_UAVS] = {0};
static NimBLEScan* pBLEScan = nullptr;
static ODID_UAS_Data UAS_data;
static unsigned long last_status = 0;

static QueueHandle_t printQueue;

// Forward declarations
void wifiCallback(void *, wifi_promiscuous_pkt_type_t);

// =============================================================================
// UAV Slot Management
// =============================================================================
static id_data* next_uav(uint8_t* mac) {
    for (int i = 0; i < MAX_UAVS; i++) {
        if (memcmp(uavs[i].mac, mac, 6) == 0)
            return &uavs[i];
    }
    for (int i = 0; i < MAX_UAVS; i++) {
        if (uavs[i].mac[0] == 0)
            return &uavs[i];
    }
    return &uavs[0];
}

// =============================================================================
// BLE Callback - Open Drone ID over BLE (NimBLE)
// =============================================================================
class MyAdvertisedDeviceCallbacks : public NimBLEScanCallbacks {
public:
    void onResult(const NimBLEAdvertisedDevice* device) override {
        if (!device->haveServiceData()) return;
        std::string svcData = device->getServiceData(NimBLEUUID((uint16_t)0xFFFA));
        if (svcData.empty() || svcData.length() < 3) return;

        uint8_t mac[6];
        NimBLEAddress addr = device->getAddress();
        const uint8_t* addrVal = addr.getVal();
        memcpy(mac, addrVal, 6);

        id_data* UAV = next_uav(mac);
        UAV->last_seen = millis();
        UAV->rssi = device->getRSSI();
        memcpy(UAV->mac, mac, 6);

        const uint8_t* odid = (const uint8_t*)svcData.data() + 1;  // skip counter
        int msgLen = svcData.length() - 1;
        if (msgLen < 1) return;

        switch (odid[0] & 0xF0) {
            case 0x00: {
                ODID_BasicID_data basic;
                if (msgLen >= (int)sizeof(ODID_BasicID_encoded))
                    decodeBasicIDMessage(&basic, (ODID_BasicID_encoded*)odid);
                strncpy(UAV->uav_id, (char*)basic.UASID, ODID_ID_SIZE);
                break;
            }
            case 0x10: {
                ODID_Location_data loc;
                if (msgLen >= (int)sizeof(ODID_Location_encoded))
                    decodeLocationMessage(&loc, (ODID_Location_encoded*)odid);
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
                    decodeSystemMessage(&sys, (ODID_System_encoded*)odid);
                UAV->base_lat_d = sys.OperatorLatitude;
                UAV->base_long_d = sys.OperatorLongitude;
                break;
            }
            case 0x50: {
                ODID_OperatorID_data op;
                if (msgLen >= (int)sizeof(ODID_OperatorID_encoded))
                    decodeOperatorIDMessage(&op, (ODID_OperatorID_encoded*)odid);
                strncpy(UAV->op_id, (char*)op.OperatorId, ODID_ID_SIZE);
                break;
            }
        }
        UAV->flag = 1;
        id_data tmp = *UAV;
        xQueueSend(printQueue, &tmp, 0);
    }
};

// =============================================================================
// JSON output (USB Serial, fast)
// =============================================================================
static void send_json_fast(const id_data *UAV) {
    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
             UAV->mac[0], UAV->mac[1], UAV->mac[2],
             UAV->mac[3], UAV->mac[4], UAV->mac[5]);
    char json_msg[256];
    snprintf(json_msg, sizeof(json_msg),
        "{\"mac\":\"%s\",\"rssi\":%d,\"drone_lat\":%.6f,\"drone_long\":%.6f,"
        "\"drone_altitude\":%d,\"pilot_lat\":%.6f,\"pilot_long\":%.6f,\"basic_id\":\"%s\"}",
        mac_str, UAV->rssi, UAV->lat_d, UAV->long_d, UAV->altitude_msl,
        UAV->base_lat_d, UAV->base_long_d, UAV->uav_id);
    Serial.println(json_msg);
}

// =============================================================================
// Google Maps links over UART to Meshtastic (human-readable mesh messages)
// =============================================================================
static void print_compact_message(const id_data *UAV) {
    static unsigned long lastSendTime = 0;
    const unsigned long sendInterval = 5000;  // 5s throttle for mesh
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
                        "Drone: %s RSSI:%d", mac_str, UAV->rssi);
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

// =============================================================================
// WiFi Promiscuous Callback - NAN + Beacon
// =============================================================================
void wifiCallback(void *buffer, wifi_promiscuous_pkt_type_t type) {
    if (type != WIFI_PKT_MGMT) return;

    wifi_promiscuous_pkt_t *packet = (wifi_promiscuous_pkt_t *)buffer;
    uint8_t *payload = packet->payload;
    int length = packet->rx_ctrl.sig_len;

    static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
    if (memcmp(nan_dest, &payload[4], 6) == 0) {
        if (odid_wifi_receive_message_pack_nan_action_frame(&UAS_data, nullptr, payload, length) == 0) {
            id_data UAV;
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

            id_data* stored = next_uav(UAV.mac);
            *stored = UAV;
            stored->flag = 1;

            id_data tmp = *stored;
            BaseType_t woken = pdFALSE;
            xQueueSendFromISR(printQueue, &tmp, &woken);
            if (woken) portYIELD_FROM_ISR();
        }
        return;
    }

    // Beacon with ODID vendor IE
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

                    id_data UAV;
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

                    id_data* stored = next_uav(UAV.mac);
                    *stored = UAV;
                    stored->flag = 1;

                    id_data tmp = *stored;
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
// FreeRTOS Tasks (single core)
// =============================================================================
static void bleScanTask(void *param) {
    for (;;) {
        pBLEScan->start(1, false);
        pBLEScan->clearResults();
        delay(100);
    }
}

static void printerTask(void *param) {
    id_data UAV;
    for (;;) {
        if (xQueueReceive(printQueue, &UAV, portMAX_DELAY)) {
            send_json_fast(&UAV);
            print_compact_message(&UAV);
        }
    }
}

// =============================================================================
// Setup
// =============================================================================
void setup() {
    delay(3000);

    Serial.begin(115200);
    Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

    Serial.println();
    Serial.println("================================================");
    Serial.println("  DRONE MESH MAPPER - XIAO ESP32-C5");
    Serial.println("  WiFi 6 (2.4+5GHz) + BLE 5 Remote ID");
    Serial.println("  Google Maps links -> Meshtastic Mesh");
    Serial.printf("  UART: TX=GPIO%d  RX=GPIO%d\n", SERIAL1_TX_PIN, SERIAL1_RX_PIN);
    Serial.println("================================================");

    nvs_flash_init();

    // WiFi promiscuous mode
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_promiscuous_rx_cb(&wifiCallback);
    esp_wifi_set_channel(6, WIFI_SECOND_CHAN_NONE);
    Serial.println("[+] WiFi promiscuous active (dual-band hopping)");

    // BLE scanner (NimBLE)
    NimBLEDevice::init("");
    pBLEScan = NimBLEDevice::getScan();
    pBLEScan->setScanCallbacks(new MyAdvertisedDeviceCallbacks());
    pBLEScan->setActiveScan(true);
    pBLEScan->setInterval(100);
    pBLEScan->setWindow(99);
    Serial.println("[+] BLE scanner active (NimBLE)");

    printQueue = xQueueCreate(MAX_UAVS * 2, sizeof(id_data));
    memset(uavs, 0, sizeof(uavs));

    // Single-core tasks
    xTaskCreate(bleScanTask, "BLE",   8192, NULL, 1, NULL);
    xTaskCreate(printerTask, "Print", 8192, NULL, 2, NULL);

    Serial.println("[+] Scanning for drones on 2.4GHz + 5GHz...\n");
}

// =============================================================================
// Loop
// =============================================================================
void loop() {
    unsigned long now = millis();

    // Dual-band channel hopping
    if (now - lastHop >= HOP_INTERVAL_MS) {
        hopChannel();
        lastHop = now;
    }

    // Heartbeat
    if (now - last_status > 60000UL) {
        Serial.println("{\"heartbeat\":\"Device is active and scanning.\"}");
        last_status = now;
    }

    delay(10);
}
