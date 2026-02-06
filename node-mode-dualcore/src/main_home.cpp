/*
 * =============================================================================
 * HOME NODE - Mesh-to-USB Bridge with Multi-Node Deduplication
 * colonelpanichacks
 *
 * Receives Remote ID JSON from the Meshtastic mesh via a Heltec V3
 * connected over UART, deduplicates detections from multiple remote nodes,
 * and forwards clean data out USB serial to mesh-mapper.py.
 *
 * DEDUP STRATEGY:
 *   Multiple remote nodes may detect the same drone simultaneously.
 *   If 5 nodes see 1 drone, we don't want 5 duplicate detections.
 *   But drones MOVE - we need continuous position updates, not just one.
 *
 *   How it works:
 *   - Key on drone MAC address (extracted from incoming JSON)
 *   - First detection for a new MAC: forward IMMEDIATELY (zero latency)
 *   - Duplicates from other nodes within 500ms: suppress (same event)
 *   - After 500ms: next detection goes through (new position data)
 *   - Result: near real-time tracking with no multi-node spam
 *   - Stale entries auto-cleared after 30s of no activity
 *
 * NO WiFi scanning. NO BLE scanning. NO detection.
 * This node is purely a smart bridge: Heltec V3 UART -> dedup -> USB Serial.
 *
 * Wiring (XIAO ESP32S3 <-> Heltec V3):
 *   GPIO5 (TX) -> Heltec RX
 *   GPIO6 (RX) <- Heltec TX
 *   GND        -- GND
 *
 * Build:  pio run -e home_node
 * Flash:  pio run -e home_node -t upload
 * =============================================================================
 */

#include <Arduino.h>
#include <HardwareSerial.h>

// =============================================================================
// Pin Definitions
// =============================================================================
static const int SERIAL1_TX_PIN = 5;   // GPIO5 -> Heltec RX
static const int SERIAL1_RX_PIN = 6;   // GPIO6 <- Heltec TX

// LED on XIAO ESP32S3 (active LOW / inverted logic)
#define LED_PIN 21

// =============================================================================
// Configuration
// =============================================================================
#define UART_BAUD          115200
#define LINE_BUF_SIZE      512      // Max line length from Heltec
#define HEARTBEAT_MS       30000    // Heartbeat interval (30s)
#define LED_FLASH_MS       50       // LED on-time per forwarded message
#define STATS_INTERVAL     60000    // Print stats every 60s

// Dedup tuning
// Remote nodes fire as fast as they detect - no rate limiting.
// Multi-node duplicates for the same detection event arrive within a few
// hundred ms of each other over mesh. 500ms window catches the burst of
// copies while letting every new position update through near-instantly.
#define DEDUP_MAX_DRONES   16       // Max simultaneous tracked drone MACs
#define DEDUP_WINDOW_MS    500      // 500ms - tight dedup, near real-time updates
#define DEDUP_STALE_MS     30000    // Clear entry after 30s of no activity

// =============================================================================
// Lightweight JSON Field Extractor
// Pulls string/int values from flat JSON without a full parser library.
// =============================================================================

// Extract a string value for a given key. Returns length, or 0 if not found.
// Example: extractJsonString(line, "mac", out, 18) -> "aa:bb:cc:dd:ee:ff"
static int extractJsonString(const char* json, const char* key, char* out, int outSize) {
  // Build search pattern: "key":"
  char pattern[48];
  snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);

  const char* p = strstr(json, pattern);
  if (!p) return 0;

  p += strlen(pattern);  // skip past opening quote
  int i = 0;
  while (*p && *p != '"' && i < outSize - 1) {
    out[i++] = *p++;
  }
  out[i] = '\0';
  return i;
}

// =============================================================================
// Deduplication Engine
// =============================================================================
struct dedup_entry {
  char     mac[18];             // Drone MAC address (key) "xx:xx:xx:xx:xx:xx"
  uint32_t windowStart;         // When the dedup window opened (ms)
  uint32_t lastSeen;            // Last time this MAC was seen (ms)
  bool     active;              // Slot in use
  char     firstNodeId[8];      // node_id that won (first in)
  uint8_t  dupsBlocked;         // How many duplicates were blocked this window
};

static dedup_entry dedupTable[DEDUP_MAX_DRONES];

// Initialize the dedup table
static void dedupInit() {
  memset(dedupTable, 0, sizeof(dedupTable));
}

// Find or allocate a dedup slot for a drone MAC
static dedup_entry* dedupFind(const char* mac) {
  // Search for existing entry
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    if (dedupTable[i].active && strcmp(dedupTable[i].mac, mac) == 0) {
      return &dedupTable[i];
    }
  }
  return nullptr;
}

static dedup_entry* dedupAlloc(const char* mac) {
  // Find empty slot
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    if (!dedupTable[i].active) {
      memset(&dedupTable[i], 0, sizeof(dedup_entry));
      strncpy(dedupTable[i].mac, mac, sizeof(dedupTable[i].mac) - 1);
      dedupTable[i].active = true;
      return &dedupTable[i];
    }
  }
  // Table full - evict oldest entry
  uint32_t oldestTime = UINT32_MAX;
  int oldestIdx = 0;
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    if (dedupTable[i].lastSeen < oldestTime) {
      oldestTime = dedupTable[i].lastSeen;
      oldestIdx = i;
    }
  }
  memset(&dedupTable[oldestIdx], 0, sizeof(dedup_entry));
  strncpy(dedupTable[oldestIdx].mac, mac, sizeof(dedupTable[oldestIdx].mac) - 1);
  dedupTable[oldestIdx].active = true;
  return &dedupTable[oldestIdx];
}

// Clean stale entries periodically
static void dedupCleanStale(uint32_t now) {
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    if (dedupTable[i].active && (now - dedupTable[i].lastSeen > DEDUP_STALE_MS)) {
      Serial.printf("[DEDUP] Cleared stale drone %s (no activity %lus)\n",
                    dedupTable[i].mac, (now - dedupTable[i].lastSeen) / 1000);
      dedupTable[i].active = false;
    }
  }
}

// =============================================================================
// State
// =============================================================================
static char lineBuf[LINE_BUF_SIZE];
static int  linePos = 0;

static unsigned long lastHeartbeat  = 0;
static unsigned long lastStats      = 0;
static unsigned long lastCleanup    = 0;
static unsigned long ledOffAt       = 0;
static bool          ledActive      = false;

// Stats
static uint32_t msgReceived   = 0;   // Total JSON messages from mesh
static uint32_t msgForwarded  = 0;   // Messages forwarded to USB (after dedup)
static uint32_t msgSuppressed = 0;   // Duplicates suppressed
static uint32_t msgNonJson    = 0;   // Non-JSON lines
static uint32_t totalBytes    = 0;   // Total bytes received from UART

// =============================================================================
// JSON Validation
// =============================================================================
static bool looksLikeJSON(const char* line, int len) {
  if (len < 2) return false;
  int start = 0;
  while (start < len && (line[start] == ' ' || line[start] == '\t')) start++;
  if (start >= len) return false;
  int end = len - 1;
  while (end > start && (line[end] == ' ' || line[end] == '\t')) end--;
  return (line[start] == '{' && line[end] == '}');
}

// =============================================================================
// LED Helpers
// =============================================================================
static inline void ledFlash() {
  digitalWrite(LED_PIN, LOW);   // ON (inverted)
  ledActive = true;
  ledOffAt = millis() + LED_FLASH_MS;
}

static inline void ledUpdate() {
  if (ledActive && millis() >= ledOffAt) {
    digitalWrite(LED_PIN, HIGH);  // OFF (inverted)
    ledActive = false;
  }
}

// =============================================================================
// Process a complete JSON line through the dedup engine
//
// SIMPLE RULE: First detection in wins. Everything else within the dedup
// window is dropped. No RSSI comparison, no "better data" updates.
// The drone's lat/long comes from Remote ID broadcast and is the same
// regardless of which node picks it up. First in = mapped. Done.
// =============================================================================
static void processJsonLine(const char* line, int len) {
  uint32_t now = millis();

  // Extract drone MAC (dedup key)
  char droneMac[18] = {0};
  char nodeIdBuf[8] = {0};

  if (extractJsonString(line, "mac", droneMac, sizeof(droneMac)) == 0) {
    // No MAC field - not a drone detection JSON, forward as-is
    Serial.println(line);
    msgForwarded++;
    ledFlash();
    return;
  }

  extractJsonString(line, "node_id", nodeIdBuf, sizeof(nodeIdBuf));
  msgReceived++;

  // Look up this drone in the dedup table
  dedup_entry* entry = dedupFind(droneMac);

  if (!entry) {
    // *** NEW DRONE - never seen before ***
    // Forward immediately, zero delay
    entry = dedupAlloc(droneMac);
    entry->windowStart = now;
    entry->lastSeen = now;
    entry->dupsBlocked = 0;
    strncpy(entry->firstNodeId, nodeIdBuf, sizeof(entry->firstNodeId) - 1);

    Serial.println(line);
    msgForwarded++;
    ledFlash();
    return;
  }

  // *** KNOWN DRONE ***
  entry->lastSeen = now;

  // Has the dedup window expired? First in for the new window wins.
  if (now - entry->windowStart >= DEDUP_WINDOW_MS) {
    entry->windowStart = now;
    entry->dupsBlocked = 0;
    strncpy(entry->firstNodeId, nodeIdBuf, sizeof(entry->firstNodeId) - 1);

    Serial.println(line);
    msgForwarded++;
    ledFlash();
    return;
  }

  // *** WITHIN DEDUP WINDOW - DROP IT ***
  entry->dupsBlocked++;
  msgSuppressed++;
}

// =============================================================================
// Process a complete line from Heltec V3
// =============================================================================
static void processLine(const char* line, int len) {
  if (len == 0) return;

  if (looksLikeJSON(line, len)) {
    // JSON line -> run through dedup engine
    processJsonLine(line, len);
  } else {
    // Not JSON (Meshtastic debug output, status messages, etc.)
    Serial.print("[MESH] ");
    Serial.println(line);
    msgNonJson++;
  }
}

// =============================================================================
// Arduino Setup
// =============================================================================
void setup() {
  // Boot delay to let Heltec V3 / Meshtastic initialize
  delay(3000);

  // USB Serial -> computer (mesh-mapper.py)
  Serial.begin(UART_BAUD);

  // UART -> Heltec V3 (Meshtastic)
  Serial1.begin(UART_BAUD, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

  // LED
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);  // OFF (inverted)

  // Initialize dedup engine
  dedupInit();

  Serial.println();
  Serial.println("================================================");
  Serial.println("  DRONE MESH MAPPER - HOME NODE");
  Serial.println("  Mesh-to-USB Bridge + Multi-Node Dedup");
  Serial.println("  Heltec V3 UART -> Dedup -> USB Serial");
  Serial.println("================================================");
  Serial.println();
  Serial.printf("[HOME] Dedup: %dms window, %d max drones tracked\n",
                DEDUP_WINDOW_MS, DEDUP_MAX_DRONES);
  Serial.printf("[HOME] UART pins: TX=GPIO%d  RX=GPIO%d  Baud=%d\n",
                SERIAL1_TX_PIN, SERIAL1_RX_PIN, UART_BAUD);
  Serial.println("[HOME] Listening for mesh data...\n");

  lastHeartbeat = millis();
  lastStats = millis();
  lastCleanup = millis();

  // Quick LED triple-blink to show we're alive
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, LOW);
    delay(80);
    digitalWrite(LED_PIN, HIGH);
    delay(80);
  }
}

// =============================================================================
// Arduino Loop
// =============================================================================
void loop() {
  unsigned long now = millis();

  // ----- Read from Heltec V3 UART, buffer lines -----
  while (Serial1.available()) {
    char c = Serial1.read();
    totalBytes++;

    if (c == '\n' || c == '\r') {
      if (linePos > 0) {
        lineBuf[linePos] = '\0';
        processLine(lineBuf, linePos);
        linePos = 0;
      }
    } else if (linePos < LINE_BUF_SIZE - 1) {
      lineBuf[linePos++] = c;
    } else {
      // Buffer overflow - line too long, reset
      linePos = 0;
    }
  }

  // ----- Forward USB Serial -> Heltec UART (bidirectional) -----
  // Allows mesh-mapper.py or user to send commands to the Heltec V3
  while (Serial.available()) {
    char c = Serial.read();
    Serial1.write(c);
  }

  // ----- LED update -----
  ledUpdate();

  // ----- Dedup stale entry cleanup (every 10s) -----
  if (now - lastCleanup >= 10000) {
    dedupCleanStale(now);
    lastCleanup = now;
  }

  // ----- Heartbeat -----
  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    // Count active tracked drones
    int activeDrones = 0;
    for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
      if (dedupTable[i].active) activeDrones++;
    }
    Serial.printf("{\"heartbeat\":\"home_node active\",\"tracked_drones\":%d}\n", activeDrones);
    lastHeartbeat = now;
  }

  // ----- Stats -----
  if (now - lastStats >= STATS_INTERVAL) {
    Serial.printf("[HOME] Stats: %u received, %u forwarded, %u suppressed, %u non-json, %u bytes\n",
                  msgReceived, msgForwarded, msgSuppressed, msgNonJson, totalBytes);

    // Show active dedup entries
    for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
      if (dedupTable[i].active) {
        Serial.printf("[HOME]   Drone %s: first node %s, %u dups blocked, age %lus\n",
                      dedupTable[i].mac, dedupTable[i].firstNodeId,
                      dedupTable[i].dupsBlocked, (now - dedupTable[i].lastSeen) / 1000);
      }
    }

    lastStats = now;
  }

  // Small yield to prevent watchdog
  delay(1);
}
