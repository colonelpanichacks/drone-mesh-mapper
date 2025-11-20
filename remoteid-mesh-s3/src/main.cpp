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
#include <esp_event.h>
#include <nvs_flash.h>
#include <nvs.h>
#include "opendroneid.h"
#include "odid_wifi.h"
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <ArduinoJson.h>

const int SERIAL1_RX_PIN = 6;
const int SERIAL1_TX_PIN = 5;

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

void callback(void *, wifi_promiscuous_pkt_type_t);
void send_json_fast(const id_data *UAV);
void print_compact_message(const id_data *UAV);
const char* get_alias_for_mac(const uint8_t* mac);
void save_aliases_to_nvs(const char* aliases_json);
void load_aliases_from_nvs();
void process_serial_commands();

#define MAX_UAVS 8
#define MAX_ALIAS_LEN 64
#define MAX_ALIASES 50
id_data uavs[MAX_UAVS] = {0};
BLEScan* pBLEScan = nullptr;
ODID_UAS_Data UAS_data;
unsigned long last_status = 0;

static QueueHandle_t printQueue;

// Alias storage: MAC address (as string) -> alias
struct alias_entry {
  char mac_str[18];  // "aa:bb:cc:dd:ee:ff"
  char alias[MAX_ALIAS_LEN];
};
alias_entry aliases[MAX_ALIASES] = {0};
int alias_count = 0;

id_data* next_uav(uint8_t* mac) {
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

class MyAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {
public:
  void onResult(BLEAdvertisedDevice device) override {
    int len = device.getPayloadLength();
    if (len <= 0) return;
      
    uint8_t* payload = device.getPayload();
    if (len > 5 && payload[1] == 0x16 && payload[2] == 0xFA && 
        payload[3] == 0xFF && payload[4] == 0x0D) {
      uint8_t* mac = (uint8_t*) device.getAddress().getNative();
      id_data* UAV = next_uav(mac);
      UAV->last_seen = millis();
      UAV->rssi = device.getRSSI();
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

// Convert MAC address to string
void mac_to_string(char* mac_str, const uint8_t* mac) {
  snprintf(mac_str, 18, "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

// Get alias for a MAC address
const char* get_alias_for_mac(const uint8_t* mac) {
  char mac_str[18];
  mac_to_string(mac_str, mac);
  
  for (int i = 0; i < alias_count; i++) {
    if (strcmp(aliases[i].mac_str, mac_str) == 0) {
      return aliases[i].alias;
    }
  }
  return "";  // Return empty string if no alias found
}

// Save aliases to NVS
void save_aliases_to_nvs(const char* aliases_json) {
  nvs_handle_t nvs_handle;
  esp_err_t err = nvs_open("aliases", NVS_READWRITE, &nvs_handle);
  if (err != ESP_OK) {
    Serial.println("{\"error\":\"Failed to open NVS for aliases\"}");
    return;
  }
  
  err = nvs_set_str(nvs_handle, "data", aliases_json);
  if (err != ESP_OK) {
    Serial.println("{\"error\":\"Failed to save aliases to NVS\"}");
    nvs_close(nvs_handle);
    return;
  }
  
  err = nvs_commit(nvs_handle);
  nvs_close(nvs_handle);
  
  if (err == ESP_OK) {
    Serial.println("{\"status\":\"aliases_saved\"}");
  } else {
    Serial.println("{\"error\":\"Failed to commit aliases to NVS\"}");
  }
}

// Load aliases from NVS
void load_aliases_from_nvs() {
  nvs_handle_t nvs_handle;
  esp_err_t err = nvs_open("aliases", NVS_READONLY, &nvs_handle);
  if (err != ESP_OK) {
    return;  // No aliases saved yet
  }
  
  size_t required_size = 0;
  err = nvs_get_str(nvs_handle, "data", NULL, &required_size);
  if (err != ESP_OK || required_size == 0) {
    nvs_close(nvs_handle);
    return;
  }
  
  char* aliases_json = (char*) malloc(required_size);
  if (!aliases_json) {
    nvs_close(nvs_handle);
    return;
  }
  
  err = nvs_get_str(nvs_handle, "data", aliases_json, &required_size);
  nvs_close(nvs_handle);
  
  if (err != ESP_OK) {
    free(aliases_json);
    return;
  }
  
  // Parse JSON and populate aliases array
  StaticJsonDocument<4096> doc;
  DeserializationError error = deserializeJson(doc, aliases_json);
  if (error) {
    free(aliases_json);
    return;
  }
  
  alias_count = 0;
  JsonObject root = doc.as<JsonObject>();
  for (JsonPair kv : root) {
    if (alias_count >= MAX_ALIASES) break;
    
    strncpy(aliases[alias_count].mac_str, kv.key().c_str(), 17);
    aliases[alias_count].mac_str[17] = '\0';
    const char* alias_val = kv.value().as<const char*>();
    if (alias_val) {
      strncpy(aliases[alias_count].alias, alias_val, MAX_ALIAS_LEN - 1);
      aliases[alias_count].alias[MAX_ALIAS_LEN - 1] = '\0';
    } else {
      aliases[alias_count].alias[0] = '\0';
    }
    alias_count++;
  }
  
  free(aliases_json);
  Serial.print("{\"status\":\"aliases_loaded\",\"count\":");
  Serial.print(alias_count);
  Serial.println("}");
}

// Process serial commands
void process_serial_commands() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    
    if (line.startsWith("UPLOAD_ALIASES:")) {
      String aliases_json = line.substring(15);  // Skip "UPLOAD_ALIASES:"
      
      // Parse and store aliases
      StaticJsonDocument<4096> doc;
      DeserializationError error = deserializeJson(doc, aliases_json);
      if (error) {
        Serial.println("{\"error\":\"Failed to parse aliases JSON\"}");
        return;
      }
      
      alias_count = 0;
      JsonObject root = doc.as<JsonObject>();
      for (JsonPair kv : root) {
        if (alias_count >= MAX_ALIASES) break;
        
        strncpy(aliases[alias_count].mac_str, kv.key().c_str(), 17);
        aliases[alias_count].mac_str[17] = '\0';
        const char* alias_val = kv.value().as<const char*>();
        if (alias_val) {
          strncpy(aliases[alias_count].alias, alias_val, MAX_ALIAS_LEN - 1);
          aliases[alias_count].alias[MAX_ALIAS_LEN - 1] = '\0';
        } else {
          aliases[alias_count].alias[0] = '\0';
        }
        alias_count++;
      }
      
      // Save to NVS
      save_aliases_to_nvs(aliases_json.c_str());
    }
  }
}

void send_json_fast(const id_data *UAV) {
  char mac_str[18];
  mac_to_string(mac_str, UAV->mac);
  const char* alias = get_alias_for_mac(UAV->mac);
  
  char json_msg[320];
  if (strlen(alias) > 0) {
    snprintf(json_msg, sizeof(json_msg),
      "{\"alias\":\"%s\",\"mac\":\"%s\",\"rssi\":%d,\"drone_lat\":%.6f,\"drone_long\":%.6f,\"drone_altitude\":%d,\"pilot_lat\":%.6f,\"pilot_long\":%.6f,\"basic_id\":\"%s\"}",
      alias, mac_str, UAV->rssi, UAV->lat_d, UAV->long_d, UAV->altitude_msl,
      UAV->base_lat_d, UAV->base_long_d, UAV->uav_id);
  } else {
    snprintf(json_msg, sizeof(json_msg),
      "{\"mac\":\"%s\",\"rssi\":%d,\"drone_lat\":%.6f,\"drone_long\":%.6f,\"drone_altitude\":%d,\"pilot_lat\":%.6f,\"pilot_long\":%.6f,\"basic_id\":\"%s\"}",
      mac_str, UAV->rssi, UAV->lat_d, UAV->long_d, UAV->altitude_msl,
      UAV->base_lat_d, UAV->base_long_d, UAV->uav_id);
  }
  Serial.println(json_msg);
}

void print_compact_message(const id_data *UAV) {
  static unsigned long lastSendTime = 0;
  const unsigned long sendInterval = 5000;
  const int MAX_MESH_SIZE = 230;
  
  if (millis() - lastSendTime < sendInterval) return;
  lastSendTime = millis();
  
  char mac_str[18];
  mac_to_string(mac_str, UAV->mac);
  const char* alias = get_alias_for_mac(UAV->mac);
  
  char mesh_msg[MAX_MESH_SIZE];
  int msg_len = 0;
  
  // Include alias first if available
  if (strlen(alias) > 0) {
    msg_len += snprintf(mesh_msg + msg_len, sizeof(mesh_msg) - msg_len,
                        "Drone: %s (%s) RSSI:%d", alias, mac_str, UAV->rssi);
  } else {
    msg_len += snprintf(mesh_msg + msg_len, sizeof(mesh_msg) - msg_len,
                        "Drone: %s RSSI:%d", mac_str, UAV->rssi);
  }
  
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

void bleScanTask(void *parameter) {
  for (;;) {
    BLEScanResults* foundDevices = pBLEScan->start(1, false);
    pBLEScan->clearResults();
    for (int i = 0; i < MAX_UAVS; i++) {
      if (uavs[i].flag) {
        // Removed send_json_fast and print_compact_message calls here
        uavs[i].flag = 0;
      }
    }
    delay(100);
  }
}

void wifiProcessTask(void *parameter) {
  for (;;) {
    // No-op: callback sets uavs[].flag and data, so nothing needed here
    delay(10);
  }
}

void callback(void *buffer, wifi_promiscuous_pkt_type_t type) {
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
      
      id_data* storedUAV = next_uav(UAV.mac);
      *storedUAV = UAV;
      storedUAV->flag = 1;
      {
        id_data tmp = *storedUAV;
        BaseType_t xHigherPriorityTaskWoken = pdFALSE;
        xQueueSendFromISR(printQueue, &tmp, &xHigherPriorityTaskWoken);
        if (xHigherPriorityTaskWoken) portYIELD_FROM_ISR();
      }
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
          
          id_data UAV;
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
          
          id_data* storedUAV = next_uav(UAV.mac);
          *storedUAV = UAV;
          storedUAV->flag = 1;
          {
            id_data tmp = *storedUAV;
            BaseType_t xHigherPriorityTaskWoken = pdFALSE;
            xQueueSendFromISR(printQueue, &tmp, &xHigherPriorityTaskWoken);
            if (xHigherPriorityTaskWoken) portYIELD_FROM_ISR();
          }
        }
      }
      offset += len + 2;
    }
  }
}

void printerTask(void *param) {
  id_data UAV;
  for (;;) {
    if (xQueueReceive(printQueue, &UAV, portMAX_DELAY)) {
      send_json_fast(&UAV);
      print_compact_message(&UAV);
      // no need to reset flag on copy
    }
  }
}

void serialCommandTask(void *parameter) {
  for (;;) {
    process_serial_commands();
    delay(50);  // Check for commands every 50ms
  }
}

void initializeSerial() {
  Serial.begin(115200);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
}

void setup() {
  setCpuFrequencyMhz(160);
  initializeSerial();
  nvs_flash_init();
  
  // Load aliases from NVS
  load_aliases_from_nvs();
  
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_promiscuous_rx_cb(&callback);
  esp_wifi_set_channel(6, WIFI_SECOND_CHAN_NONE);
  
  BLEDevice::init("DroneID");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new MyAdvertisedDeviceCallbacks());
  pBLEScan->setActiveScan(true);

  printQueue = xQueueCreate(MAX_UAVS, sizeof(id_data));
  
  xTaskCreatePinnedToCore(bleScanTask, "BLEScanTask", 10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(wifiProcessTask, "WiFiProcessTask", 10000, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(printerTask, "PrinterTask", 10000, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(serialCommandTask, "SerialCommandTask", 4096, NULL, 1, NULL, 0);
  
  memset(uavs, 0, sizeof(uavs));
}

void loop() {
  unsigned long current_millis = millis();
    if ((current_millis - last_status) > 60000UL) {
      Serial.println("{\"   [+] Device is active and scanning...\"}");
      last_status = current_millis;
    }
}
