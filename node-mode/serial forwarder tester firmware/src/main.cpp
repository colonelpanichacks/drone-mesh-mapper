
#if !defined(ARDUINO_ARCH_ESP32)
#error "This program requires an ESP32"
#endif

#include <Arduino.h>
#include <HardwareSerial.h>

// Custom UART pin definitions for Serial1
const int SERIAL1_RX_PIN = 7;  // GPIO7
const int SERIAL1_TX_PIN = 6;  // GPIO6

void setup() {
  // Initialize USB serial and Serial1 for mesh/UART
  Serial.begin(115200);
  Serial1.begin(115200, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);
  Serial.println("Serial forwarder initialized.");
}

void loop() {
  // Forward from USB Serial to Serial1
  while (Serial.available()) {
    int c = Serial.read();
    Serial1.write(c);
  }
  // Forward from Serial1 to USB Serial
  while (Serial1.available()) {
    int c = Serial1.read();
    Serial.write(c);
  }
}
