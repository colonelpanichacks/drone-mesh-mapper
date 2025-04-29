/* 
* node-mode with dual Wi-Fi and BLE support for ESP32S3
*/
#if !defined(ARDUINO_ARCH_ESP32)
#error "This program requires an ESP32"
#endif

#include <Arduino.h>
#include <HardwareSerial.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <nvs_flash.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include <esp_timer.h>

// UART pin definitions for Serial1 on esp32s3
const int SERIAL1_RX_PIN = 6;  // GPIO6
const int SERIAL1_TX_PIN = 5;  // GPIO5

// Structure to hold UAV detection data
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
uav_data uavs[MAX_UAVS] = {0};
BLEScan* pBLEScan = nullptr;
ODID_UAS_Data UAS_data;
unsigned long last_status = 0;

// Forward declarations
void callback(void *, wifi_promiscuous_pkt_type_t);
void send_json_fast(const uav_data *UAV);
void print_compact_message(const uav_data *UAV);

// Get next available UAV slot or reuse existing one
uav_data* next_uav(uint8_t* mac) {
  for (int i = 0; i < MAX_UAVS; i++) {
    if (memcmp(uavs[i].mac, mac, 6) == 0)
      return &uavs[i];
  }
  for (int i = 0; i < MAX_UAVS; i++) {
    if (uavs[i].mac[0] == 0)
      return &uavs[i];
  }
  return &uavs[0]; // Fallback to first slot if all are used
}

// BLE Advertisement callback handler
class MyAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {
public:
  void onResult(BLEAdvertisedDevice device) override {
    int len = device.getPayloadLength();
    if (len <= 0) return;
      
    uint8_t* payload = device.getPayload();
    if (len > 5 && payload[1] == 0x16 && payload[2] == 0xFA && 
        payload[3] == 0xFF && payload[4] == 0x0D) {
      uint8_t* mac = (uint8_t*) device.getAddress().getNative();
      uav_data* UAV = next_uav(mac);
      UAV->last_seen = millis();
      UAV->rssi = device.getRSSI();
      UAV->flag = 1;
      memcpy(UAV->mac, mac, 6);
      
      uint8_t* odid = &payload[6];
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
    }
  }
};

// Initialize USB Serial (for JSON output) and Serial1 (for mesh/UART)
void initializeSerial() {
  Serial.begin(115200);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  Serial.println("USB Serial (for JSON) and UART (Serial1) initialized.");
}

// Sends JSON payload as fast as possible over USB Serial (includes basic_id).
void send_json_fast(const uav_data *UAV) {
  char mac_str[18];
  snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
           UAV->mac[0], UAV->mac[1], UAV->mac[2],
           UAV->mac[3], UAV->mac[4], UAV->mac[5]);
  char json_msg[256];
  snprintf(json_msg, sizeof(json_msg),
    "{\"mac\":\"%s\",\"rssi\":%d,\"drone_lat\":%.6f,\"drone_long\":%.6f,"
    "\"drone_altitude\":%d,\"pilot_lat\":%.6f,\"pilot_long\":%.6f,"
    "\"basic_id\":\"%s\"}",
    mac_str, UAV->rssi, UAV->lat_d, UAV->long_d, UAV->altitude_msl,
    UAV->base_lat_d, UAV->base_long_d, UAV->uav_id);
  Serial.println(json_msg);
}

// Modified function: emits two JSON messages over Serial1
void print_compact_message(const uav_data *UAV) {
  static unsigned long lastSendTime = 0;
  const unsigned long sendInterval = 3000;  // 3-second interval for UART messages
  if (millis() - lastSendTime < sendInterval) return;
  lastSendTime = millis();

  // Format MAC address
  char mac_str[18];
  snprintf(mac_str, sizeof(mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
           UAV->mac[0], UAV->mac[1], UAV->mac[2],
           UAV->mac[3], UAV->mac[4], UAV->mac[5]);

  // First JSON: MAC address and drone coordinates
  char json_drone[128];
  int len_drone = snprintf(json_drone, sizeof(json_drone),
                           "{\"mac\":\"%s\",\"drone_lat\":%.6f,\"drone_long\":%.6f}",
                           mac_str, UAV->lat_d, UAV->long_d);
  if (Serial1.availableForWrite() >= len_drone) {
    Serial1.println(json_drone);
  }

  // Second JSON: remote ID and pilot coordinates
  char json_pilot[128];
  snprintf(json_pilot, sizeof(json_pilot),
           "{\"remote_id\":\"%s\",\"pilot_lat\":%.6f,\"pilot_long\":%.6f}",
           UAV->uav_id, UAV->base_lat_d, UAV->base_long_d);
  Serial1.println(json_pilot);
}

// Wi-Fi promiscuous packet callback
void callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_MGMT) return;
  
  wifi_promiscuous_pkt_t *packet = (wifi_promiscuous_pkt_t *)buffer;
  uint8_t *payload = packet->payload;
  int length = packet->rx_ctrl.sig_len;
  
  static const uint8_t nan_dest[6] = {0x51, 0x6f, 0x9a, 0x01, 0x00, 0x00};
  if (memcmp(nan_dest, &payload[4], 6) == 0) {
    if (odid_wifi_receive_message_pack_nan_action_frame(&UAS_data, nullptr, payload, length) == 0) {
      uav_data UAV;
      memset(&UAV, 0, sizeof(UAV));
      memcpy(UAV.mac, &payload[10], 6);
      UAV.rssi = packet->rx_ctrl.rssi;
      UAV.last_seen = millis();
      
      if (UAS_data.BasicIDValid[0]) {
        strncpy(UAV.uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
      }
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
      if (UAS_data.OperatorIDValid) {
        strncpy(UAV.op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);
      }
      
      uav_data* dbUAV = next_uav(UAV.mac);
      memcpy(dbUAV, &UAV, sizeof(UAV));
      dbUAV->flag = 1;
    }
  }
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
          
          uav_data UAV;
          memset(&UAV, 0, sizeof(UAV));
          memcpy(UAV.mac, &payload[10], 6);
          UAV.rssi = packet->rx_ctrl.rssi;
          UAV.last_seen = millis();
          
          if (UAS_data.BasicIDValid[0]) {
            strncpy(UAV.uav_id, (char *)UAS_data.BasicID[0].UASID, ODID_ID_SIZE);
          }
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
          if (UAS_data.OperatorIDValid) {
            strncpy(UAV.op_id, (char *)UAS_data.OperatorID.OperatorId, ODID_ID_SIZE);
          }
          
          uav_data* dbUAV = next_uav(UAV.mac);
          memcpy(dbUAV, &UAV, sizeof(UAV));
          dbUAV->flag = 1;
        }
      }
      offset += len + 2;
    }
  }
}

// BLE scanning task running on core 0
void bleScanTask(void *parameter) {
  for(;;) {
    BLEScanResults* foundDevices = pBLEScan->start(1, false);
    pBLEScan->clearResults();
    
    for (int i = 0; i < MAX_UAVS; i++) {
      if (uavs[i].flag) {
        send_json_fast(&uavs[i]);
        print_compact_message(&uavs[i]);
        uavs[i].flag = 0;
      }
    }
    
    unsigned long current_millis = millis();
    if ((current_millis - last_status) > 60000UL) {
      Serial.println("{\"heartbeat\":\"Device is active and running.\"}");
      last_status = current_millis;
    }
  
    delay(100);
  }
}

// Wi-Fi processing task running on core 1

void wifiProcessTask(void *parameter) {
  for(;;) {
    for (int i = 0; i < MAX_UAVS; i++) {
      if (uavs[i].flag) {
        send_json_fast(&uavs[i]);
        print_compact_message(&uavs[i]);
        uavs[i].flag = 0;
      }
    }
    delay(10);
  }
}

// Task to forward incoming JSON from Serial1 (UART) to USB Serial
void uartForwardTask(void *parameter) {
  for (;;) {
    while (Serial1.available()) {
      char c = Serial1.read();
      Serial.write(c);
    }
    delay(3000);  // 3-second polling interval for UART-to-USB echo
  }
}

void setup() {
  delay(6000);  // 6-second boot delay (necessary for xiao meshtastic)
  setCpuFrequencyMhz(160);
  nvs_flash_init();
  initializeSerial();
  
  // Initialize Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(&callback);
  esp_wifi_set_channel(6, WIFI_SECOND_CHAN_NONE);
  
  // Initialize BLE scanning
  BLEDevice::init("DroneID");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new MyAdvertisedDeviceCallbacks());
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);
  
  // Initialize UAV tracking array
  memset(uavs, 0, sizeof(uavs));
  
  // Create tasks for BLE scanning and Wi-Fi processing on separate cores
  xTaskCreatePinnedToCore(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(wifiProcessTask, "WiFiProcessTask", 10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(uartForwardTask, "UARTForwardTask", 4096, NULL, 1, NULL, 1);
}

void loop() {
  // Main tasks are handled by the FreeRTOS tasks on separate cores
}
