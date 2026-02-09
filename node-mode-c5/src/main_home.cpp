/*
 * =============================================================================
 * MESH-DETECT HE MODE - Head-End / Home Receiver
 * colonelpanichacks
 *
 * XIAO ESP32-C5 mounted on mesh-detect PCB
 *
 * Receives Remote ID JSON from the Meshtastic mesh via a Heltec V3
 * connected over UART, deduplicates detections from multiple remote nodes,
 * and forwards clean data out USB serial to mesh-mapper.py (Google Maps).
 *
 * DEDUP STRATEGY:
 *   Multiple remote nodes may detect the same drone simultaneously.
 *   - Key on drone MAC address
 *   - First detection for a new MAC: forward IMMEDIATELY (zero latency)
 *   - Duplicates from other nodes within 500ms: suppress (same event)
 *   - After 500ms: next detection goes through (new position data)
 *   - Stale entries auto-cleared after 30s of no activity
 *
 * NO WiFi scanning. NO BLE scanning. NO detection.
 * Pure smart bridge: Heltec V3 UART -> dedup -> USB Serial -> mesh-mapper.py
 *
 * Pin mapping (mesh-detect PCB, same physical pads as S3/C3 versions):
 *   D4 (GPIO23) TX -> Heltec RX
 *   D5 (GPIO24) RX <- Heltec TX
 *   GND        -- GND
 *
 * Build:  pio run -e he
 * Flash:  pio run -e he -t upload
 * =============================================================================
 */

#include <Arduino.h>
#include <HardwareSerial.h>

// =============================================================================
// Pin Definitions -- XIAO ESP32-C5 on mesh-detect PCB
// =============================================================================
static const int SERIAL1_TX_PIN = 23;  // D4 = GPIO23 on C5 -> Heltec RX
static const int SERIAL1_RX_PIN = 24;  // D5 = GPIO24 on C5 <- Heltec TX

// LED on XIAO ESP32-C5 (active HIGH)
#define LED_PIN      27
#define LED_ON       HIGH
#define LED_OFF      LOW

// =============================================================================
// Configuration
// =============================================================================
#define UART_BAUD          115200
#define LINE_BUF_SIZE      512
#define HEARTBEAT_MS       30000
#define LED_FLASH_MS       50
#define STATS_INTERVAL     60000

// Dedup tuning
#define DEDUP_MAX_DRONES   16
#define DEDUP_WINDOW_MS    500
#define DEDUP_STALE_MS     30000

// =============================================================================
// Lightweight JSON Field Extractor
// =============================================================================
static int extractJsonString(const char* json, const char* key, char* out, int outSize) {
    char pattern[48];
    snprintf(pattern, sizeof(pattern), "\"%s\":\"", key);

    const char* p = strstr(json, pattern);
    if (!p) return 0;

    p += strlen(pattern);
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
    char     mac[18];
    uint32_t windowStart;
    uint32_t lastSeen;
    bool     active;
    char     firstNodeId[8];
    uint8_t  dupsBlocked;
};

static dedup_entry dedupTable[DEDUP_MAX_DRONES];

static void dedupInit() {
    memset(dedupTable, 0, sizeof(dedupTable));
}

static dedup_entry* dedupFind(const char* mac) {
    for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
        if (dedupTable[i].active && strcmp(dedupTable[i].mac, mac) == 0)
            return &dedupTable[i];
    }
    return nullptr;
}

static dedup_entry* dedupAlloc(const char* mac) {
    for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
        if (!dedupTable[i].active) {
            memset(&dedupTable[i], 0, sizeof(dedup_entry));
            strncpy(dedupTable[i].mac, mac, sizeof(dedupTable[i].mac) - 1);
            dedupTable[i].active = true;
            return &dedupTable[i];
        }
    }
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
static uint32_t msgReceived   = 0;
static uint32_t msgForwarded  = 0;
static uint32_t msgSuppressed = 0;
static uint32_t msgNonJson    = 0;
static uint32_t totalBytes    = 0;

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
    digitalWrite(LED_PIN, LED_ON);
    ledActive = true;
    ledOffAt = millis() + LED_FLASH_MS;
}

static inline void ledUpdate() {
    if (ledActive && millis() >= ledOffAt) {
        digitalWrite(LED_PIN, LED_OFF);
        ledActive = false;
    }
}

// =============================================================================
// Dedup + Forward
// =============================================================================
static void processJsonLine(const char* line, int len) {
    uint32_t now = millis();

    char droneMac[18] = {0};
    char nodeIdBuf[8] = {0};

    if (extractJsonString(line, "mac", droneMac, sizeof(droneMac)) == 0) {
        Serial.println(line);
        msgForwarded++;
        ledFlash();
        return;
    }

    extractJsonString(line, "node_id", nodeIdBuf, sizeof(nodeIdBuf));
    msgReceived++;

    dedup_entry* entry = dedupFind(droneMac);

    if (!entry) {
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

    entry->lastSeen = now;

    if (now - entry->windowStart >= DEDUP_WINDOW_MS) {
        entry->windowStart = now;
        entry->dupsBlocked = 0;
        strncpy(entry->firstNodeId, nodeIdBuf, sizeof(entry->firstNodeId) - 1);

        Serial.println(line);
        msgForwarded++;
        ledFlash();
        return;
    }

    entry->dupsBlocked++;
    msgSuppressed++;
}

static void processLine(const char* line, int len) {
    if (len == 0) return;

    if (looksLikeJSON(line, len)) {
        processJsonLine(line, len);
    } else {
        Serial.print("[MESH] ");
        Serial.println(line);
        msgNonJson++;
    }
}

// =============================================================================
// Arduino Setup
// =============================================================================
void setup() {
    delay(3000);

    Serial.begin(UART_BAUD);
    Serial1.begin(UART_BAUD, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, LED_OFF);

    dedupInit();

    Serial.println();
    Serial.println("================================================");
    Serial.println("  MESH-DETECT HE - XIAO ESP32-C5");
    Serial.println("  Head-End: Mesh-to-USB Bridge + Dedup");
    Serial.println("  Heltec V3 UART -> Dedup -> USB -> mesh-mapper");
    Serial.printf("  TX=GPIO%d  RX=GPIO%d  Baud=%d\n",
                  SERIAL1_TX_PIN, SERIAL1_RX_PIN, UART_BAUD);
    Serial.println("================================================");
    Serial.println();
    Serial.printf("[HE] Dedup: %dms window, %d max drones tracked\n",
                  DEDUP_WINDOW_MS, DEDUP_MAX_DRONES);
    Serial.println("[HE] Listening for mesh data...\n");

    lastHeartbeat = millis();
    lastStats = millis();
    lastCleanup = millis();

    // Triple-blink to show we're alive
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_PIN, LED_ON);
        delay(80);
        digitalWrite(LED_PIN, LED_OFF);
        delay(80);
    }
}

// =============================================================================
// Arduino Loop
// =============================================================================
void loop() {
    unsigned long now = millis();

    // Read from Heltec V3 UART
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
            linePos = 0;
        }
    }

    // Forward USB -> Heltec (bidirectional)
    while (Serial.available()) {
        char c = Serial.read();
        Serial1.write(c);
    }

    ledUpdate();

    // Stale entry cleanup
    if (now - lastCleanup >= 10000) {
        dedupCleanStale(now);
        lastCleanup = now;
    }

    // Heartbeat
    if (now - lastHeartbeat >= HEARTBEAT_MS) {
        int activeDrones = 0;
        for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
            if (dedupTable[i].active) activeDrones++;
        }
        Serial.printf("{\"heartbeat\":\"he_active\",\"tracked_drones\":%d}\n", activeDrones);
        lastHeartbeat = now;
    }

    // Stats
    if (now - lastStats >= STATS_INTERVAL) {
        Serial.printf("[HE] Stats: %u received, %u forwarded, %u suppressed, %u non-json, %u bytes\n",
                      msgReceived, msgForwarded, msgSuppressed, msgNonJson, totalBytes);

        for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
            if (dedupTable[i].active) {
                Serial.printf("[HE]   Drone %s: first node %s, %u dups blocked, age %lus\n",
                              dedupTable[i].mac, dedupTable[i].firstNodeId,
                              dedupTable[i].dupsBlocked, (now - dedupTable[i].lastSeen) / 1000);
            }
        }

        lastStats = now;
    }

    delay(1);
}
