/*
 * =============================================================================
 * HOME NODE - Mesh-to-USB Bridge with Multi-Node Deduplication
 * colonelpanichacks
 *
 * Receives Remote ID JSON from the Meshtastic mesh via a Heltec V3 connected
 * over UART, deduplicates detections from multiple remote nodes, and forwards
 * clean data out USB serial to mesh-mapper.py.
 *
 * DEDUP STRATEGY:
 *   Multiple remote nodes may detect the same drone simultaneously.
 *   If 5 nodes see 1 drone, we don't want 5 duplicate detections.
 *   But drones MOVE - we need continuous position updates, not just one.
 *
 *   - Key on drone MAC address (extracted from incoming JSON)
 *   - First detection for a new MAC: forward IMMEDIATELY (zero latency)
 *   - Duplicates from other nodes within 500ms: suppress (same event)
 *   - After 500ms: next detection goes through (new position data)
 *   - Stale entries auto-cleared after 30s of no activity
 *
 * NO WiFi scanning. NO BLE scanning. NO detection.
 * Purely a smart bridge: Heltec V3 UART -> dedup -> USB Serial.
 *
 * -----------------------------------------------------------------------------
 * WHY THIS NODE USED TO WEDGE / TRIP THE WATCHDOG
 *
 * On this board Serial is the native USB CDC (HWCDC), not a UART. HWCDC::write
 * waits on the TX ring with tx_timeout_ms (100ms) and gives up only after
 * max_consec_timeouts (20) - so ONE Serial.println can block for ~2 seconds
 * whenever the host stops draining the port. mesh-mapper.py not running, a
 * closed serial monitor, or a stalled reader is enough to trigger that.
 *
 * The old loop called that blocking println from inside
 *
 *     while (Serial1.available()) { ... processLine() ... }
 *
 * which has no bound at all. While mesh traffic keeps arriving, bytes keep
 * landing in the RX buffer, so the loop never exits and the "delay(1) to
 * prevent watchdog" at the bottom is never reached. The bridge stops feeding
 * anything, the UART RX buffer overflows, and detections are lost.
 *
 * Nothing recovered from that, either: the Arduino core leaves
 * loopTaskWDTEnabled = false, so loop() is not subscribed to the task
 * watchdog. A wedged loop just hangs forever, silently.
 *
 * There was also a fourth bug, the one users actually saw: this node used to
 * pass every byte from USB straight through to the Heltec. mesh-mapper.py
 * writes "WATCHDOG_RESET" to each port it opens, the Heltec's serial module in
 * TEXTMSG mode broadcasts every line it receives, so the whole mesh was
 * spammed with "WATCHDOG_RESET" texts on every mapper start or reconnect. Host
 * input is now parsed and answered locally; nothing is forwarded to the mesh.
 *
 * This version fixes all of it:
 *   1. Every read/forward path is BOUNDED - loop() always returns promptly.
 *   2. USB writes NEVER block: output goes through a ring buffer that is
 *      drained only as fast as availableForWrite() allows, and is dropped
 *      oldest-first when no host is reading.
 *   3. The loop task IS subscribed to the task watchdog, so if it ever does
 *      wedge the node reboots cleanly instead of hanging. The reset reason
 *      and a boot counter survive the reboot and are reported on startup.
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
#include <esp_task_wdt.h>
#include <esp_system.h>

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

// Watchdog. The loop task is subscribed and fed once per pass. Every stage of
// the loop is bounded well under this, so tripping it means something is
// genuinely stuck and a reboot is the correct recovery.
#define WDT_TIMEOUT_MS     10000

// Bounds that keep one loop pass short. At 115200 baud the UART delivers
// ~11.5 bytes/ms, so 512 bytes is ~45ms of wire time - we can never fall
// behind, and we always reach the bottom of loop().
#define UART_CHUNK_BYTES   512      // Max UART bytes drained per loop pass
#define USB_IN_CHUNK       128      // Max USB->UART bytes per loop pass
#define UART_RX_BUFFER     4096     // Deep RX buffer so bursts are not lost

// USB output ring. Detections are queued here and drained without ever
// blocking. Sized to hold a healthy burst while the host catches up.
#define TXQ_SIZE           8192
#define TXQ_MAX_DRAIN      1024     // Max bytes pushed to USB per loop pass

// Dedup tuning
// Remote nodes fire as fast as they detect - no rate limiting.
// Multi-node duplicates for the same detection event arrive within a few
// hundred ms of each other over mesh. 500ms window catches the burst of
// copies while letting every new position update through near-instantly.
#define DEDUP_MAX_DRONES   16       // Max simultaneous tracked drone MACs
#define DEDUP_WINDOW_MS    500      // 500ms - tight dedup, near real-time
#define DEDUP_STALE_MS     30000    // Clear entry after 30s of no activity

// Rollover-safe elapsed time. millis() wraps every ~49.7 days; a home node is
// expected to run far longer than that, and plain `a - b > x` on signed or
// mixed types breaks at the wrap. Unsigned subtraction is correct across it.
static inline uint32_t elapsed(uint32_t now, uint32_t then) {
  return (uint32_t)(now - then);
}

// =============================================================================
// Boot diagnostics - survives a reset so a wedge can be diagnosed after it
// =============================================================================
RTC_NOINIT_ATTR static uint32_t rtcBootCount;
RTC_NOINIT_ATTR static uint32_t rtcMagic;
RTC_NOINIT_ATTR static uint32_t rtcLastUptimeMs;
#define RTC_MAGIC 0xB0075EEDu

static const char* resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_EXT:       return "external pin";
    case ESP_RST_SW:        return "software restart";
    case ESP_RST_PANIC:     return "PANIC / exception";
    case ESP_RST_INT_WDT:   return "INTERRUPT WATCHDOG";
    case ESP_RST_TASK_WDT:  return "TASK WATCHDOG";
    case ESP_RST_WDT:       return "other watchdog";
    case ESP_RST_DEEPSLEEP: return "deep sleep wake";
    case ESP_RST_BROWNOUT:  return "BROWNOUT (power supply)";
    case ESP_RST_SDIO:      return "SDIO";
#if ESP_IDF_VERSION_MAJOR >= 5
    // A host opening the USB port drives the S3's USB-Serial/JTAG reset lines.
    // This is by far the most common reset here and is NOT a fault.
    case ESP_RST_USB:       return "USB peripheral (host opened the port)";
    case ESP_RST_JTAG:      return "JTAG";
    case ESP_RST_EFUSE:     return "efuse error";
    case ESP_RST_PWR_GLITCH:return "power glitch";
    case ESP_RST_CPU_LOCKUP:return "CPU lockup (double exception)";
#endif
    default:                return "unknown";
  }
}

// =============================================================================
// Non-blocking USB output ring
//
// Nothing in this file ever calls Serial.print directly. Everything is queued
// here and drained by txFlush() only as far as availableForWrite() permits, so
// a host that stops reading can never stall the bridge. When the queue fills,
// the OLDEST bytes are dropped: fresh detections matter more than stale ones.
// =============================================================================
static uint8_t  txq[TXQ_SIZE];
static volatile size_t txHead = 0;   // write index
static volatile size_t txTail = 0;   // read index
static uint32_t txDroppedBytes = 0;
static uint32_t txDropEvents = 0;

static inline size_t txUsed() {
  return (txHead >= txTail) ? (txHead - txTail) : (TXQ_SIZE - txTail + txHead);
}

static inline size_t txFree() {
  return TXQ_SIZE - txUsed() - 1;   // keep one slot free to distinguish states
}

// Drop whole bytes from the oldest end until `need` bytes fit.
static void txDropOldest(size_t need) {
  size_t freed = 0;
  while (txFree() < need && txUsed() > 0) {
    txTail = (txTail + 1) % TXQ_SIZE;
    freed++;
  }
  if (freed) {
    txDroppedBytes += freed;
    txDropEvents++;
  }
}

static void txWrite(const char* data, size_t len) {
  if (len == 0) return;
  if (len > TXQ_SIZE - 1) {           // absurdly long, keep the tail of it
    data += (len - (TXQ_SIZE - 1));
    len = TXQ_SIZE - 1;
  }
  if (txFree() < len) txDropOldest(len);
  for (size_t i = 0; i < len; i++) {
    txq[txHead] = (uint8_t)data[i];
    txHead = (txHead + 1) % TXQ_SIZE;
  }
}

static void txPrint(const char* s) { txWrite(s, strlen(s)); }

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
// Returns the number of bytes actually handed to the CDC ring so the loop can
// tell "making progress" from "host not draining".
static size_t txFlush() {
  size_t sent = 0;
  size_t budget = TXQ_MAX_DRAIN;
  while (txUsed() > 0 && budget > 0) {
    // availableForWrite() is how much the CDC ring will take right now.
    // Writing only that much guarantees write() returns without waiting.
    int room = Serial.availableForWrite();
    if (room <= 0) return sent;       // host not draining - try again next pass

    size_t chunk = txUsed();
    if (chunk > (size_t)room) chunk = (size_t)room;
    if (chunk > budget) chunk = budget;
    // Do not wrap past the end of the ring in one write
    if (txTail + chunk > TXQ_SIZE) chunk = TXQ_SIZE - txTail;

    size_t wrote = Serial.write(&txq[txTail], chunk);
    if (wrote == 0) return sent;      // made no progress, do not spin
    txTail = (txTail + wrote) % TXQ_SIZE;
    budget -= wrote;
    sent += wrote;
  }
  return sent;
}

// =============================================================================
// Lightweight JSON Field Extractor
// Pulls string values from flat JSON without a full parser library.
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
  char     mac[18];             // Drone MAC address (key) "xx:xx:xx:xx:xx:xx"
  uint32_t windowStart;         // When the dedup window opened (ms)
  uint32_t lastSeen;            // Last time this MAC was seen (ms)
  bool     active;              // Slot in use
  char     firstNodeId[8];      // node_id that won (first in)
  uint8_t  dupsBlocked;         // How many duplicates were blocked this window
};

static dedup_entry dedupTable[DEDUP_MAX_DRONES];

static void dedupInit() {
  memset(dedupTable, 0, sizeof(dedupTable));
}

static dedup_entry* dedupFind(const char* mac) {
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    if (dedupTable[i].active && strcmp(dedupTable[i].mac, mac) == 0) {
      return &dedupTable[i];
    }
  }
  return nullptr;
}

static dedup_entry* dedupAlloc(const char* mac, uint32_t now) {
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    if (!dedupTable[i].active) {
      memset(&dedupTable[i], 0, sizeof(dedup_entry));
      strncpy(dedupTable[i].mac, mac, sizeof(dedupTable[i].mac) - 1);
      dedupTable[i].active = true;
      return &dedupTable[i];
    }
  }
  // Table full - evict the entry with the greatest age. Comparing ages rather
  // than raw timestamps keeps this correct across a millis() rollover.
  uint32_t bestAge = 0;
  int oldestIdx = 0;
  for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
    uint32_t age = elapsed(now, dedupTable[i].lastSeen);
    if (age >= bestAge) {
      bestAge = age;
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
    if (dedupTable[i].active && elapsed(now, dedupTable[i].lastSeen) > DEDUP_STALE_MS) {
      txPrintf("[DEDUP] Cleared stale drone %s (no activity %us)\n",
               dedupTable[i].mac, elapsed(now, dedupTable[i].lastSeen) / 1000);
      dedupTable[i].active = false;
    }
  }
}

// =============================================================================
// State
// =============================================================================
static char lineBuf[LINE_BUF_SIZE];
static int  linePos = 0;
static bool lineOverflow = false;    // discarding an over-long line

static uint32_t lastHeartbeat  = 0;
static uint32_t lastStats      = 0;
static uint32_t lastCleanup    = 0;
static uint32_t ledOffAt       = 0;
static bool     ledActive      = false;

// Stats
static uint32_t msgReceived   = 0;   // Total JSON messages from mesh
static uint32_t msgForwarded  = 0;   // Messages forwarded to USB (after dedup)
static uint32_t msgSuppressed = 0;   // Duplicates suppressed
static uint32_t msgNonJson    = 0;   // Non-JSON lines
static uint32_t totalBytes    = 0;   // Total bytes received from UART
static uint32_t linesDropped  = 0;   // Over-long lines discarded
static uint32_t loopMaxUs     = 0;   // Longest loop pass since last stats

// =============================================================================
// LED Helpers
// =============================================================================
static inline void ledFlash() {
  digitalWrite(LED_PIN, LOW);   // ON (inverted)
  ledActive = true;
  ledOffAt = millis() + LED_FLASH_MS;
}

static inline void ledUpdate(uint32_t now) {
  if (ledActive && elapsed(now, ledOffAt) < 0x80000000u) {
    digitalWrite(LED_PIN, HIGH);  // OFF (inverted)
    ledActive = false;
  }
}

// =============================================================================
// Process a complete JSON line through the dedup engine
//
// SIMPLE RULE: First detection in wins. Everything else within the dedup
// window is dropped. The drone's lat/long comes from the Remote ID broadcast
// and is the same regardless of which node picks it up.
// =============================================================================
static void processJsonLine(const char* line, int len, uint32_t now) {
  char droneMac[18] = {0};
  char nodeIdBuf[8] = {0};

  if (extractJsonString(line, "mac", droneMac, sizeof(droneMac)) == 0) {
    // No MAC field - not a drone detection JSON, forward as-is
    txPrintln(line);
    msgForwarded++;
    ledFlash();
    return;
  }

  extractJsonString(line, "node_id", nodeIdBuf, sizeof(nodeIdBuf));
  msgReceived++;

  dedup_entry* entry = dedupFind(droneMac);

  if (!entry) {
    // *** NEW DRONE - never seen before *** forward immediately, zero delay
    entry = dedupAlloc(droneMac, now);
    entry->windowStart = now;
    entry->lastSeen = now;
    entry->dupsBlocked = 0;
    strncpy(entry->firstNodeId, nodeIdBuf, sizeof(entry->firstNodeId) - 1);

    txPrintln(line);
    msgForwarded++;
    ledFlash();
    return;
  }

  // *** KNOWN DRONE ***
  entry->lastSeen = now;

  // Has the dedup window expired? First in for the new window wins.
  if (elapsed(now, entry->windowStart) >= DEDUP_WINDOW_MS) {
    entry->windowStart = now;
    entry->dupsBlocked = 0;
    strncpy(entry->firstNodeId, nodeIdBuf, sizeof(entry->firstNodeId) - 1);

    txPrintln(line);
    msgForwarded++;
    ledFlash();
    return;
  }

  // *** WITHIN DEDUP WINDOW - DROP IT ***
  if (entry->dupsBlocked < 255) entry->dupsBlocked++;
  msgSuppressed++;
}

// =============================================================================
// Process a complete line from Heltec V3
//
// Meshtastic's serial module in TEXTMSG mode does not deliver the payload
// bare: every received message is printed as "<sender_short_name>: <payload>"
// (e.g. '77b4: {"mac":...}'). Expecting the line to START with '{' silently
// bypassed the dedup engine for every real detection, so locate the JSON
// inside the line instead and forward just that.
// =============================================================================
static void processLine(char* line, int len, uint32_t now) {
  if (len == 0) return;

  char* jstart = strchr(line, '{');
  char* jend = jstart ? strrchr(jstart, '}') : nullptr;

  if (jstart && jend && jend > jstart) {
    jend[1] = '\0';   // trim anything after the closing brace
    processJsonLine(jstart, (int)(jend - jstart + 1), now);
  } else {
    // No JSON in it (Meshtastic debug, status text, a fragment of a message
    // the sender's Heltec split mid-JSON, etc.)
    txPrint("[MESH] ");
    txPrintln(line);
    msgNonJson++;
  }
}

// =============================================================================
// Bounded UART drain
//
// Reads at most UART_CHUNK_BYTES per call so loop() always returns promptly,
// no matter how hard the mesh is talking. Anything still buffered is picked up
// on the next pass - the deep RX buffer plus this cadence keeps us ahead of
// the wire without ever monopolising the CPU.
// =============================================================================
static void drainUart(uint32_t now) {
  int budget = UART_CHUNK_BYTES;
  while (budget-- > 0 && Serial1.available()) {
    char c = (char)Serial1.read();
    totalBytes++;

    if (c == '\n' || c == '\r') {
      if (lineOverflow) {
        // End of the over-long line: drop it and resync cleanly on the next
        // one rather than emitting the tail as a bogus record.
        lineOverflow = false;
        linePos = 0;
        linesDropped++;
      } else if (linePos > 0) {
        lineBuf[linePos] = '\0';
        processLine(lineBuf, linePos, now);
        linePos = 0;
      }
    } else if (lineOverflow) {
      // still discarding
    } else if (linePos < LINE_BUF_SIZE - 1) {
      lineBuf[linePos++] = c;
    } else {
      lineOverflow = true;   // discard until the next newline
    }
  }
}

// =============================================================================
// Bounded USB command handler
//
// This used to be a raw USB -> UART pass-through, and that was the bug the
// field reports called "watchdog timer resets broadcast over the mesh":
// mesh-mapper.py writes the literal line "WATCHDOG_RESET" to every serial
// port it opens (startup, port selection, every reconnect). The pass-through
// pushed those bytes into the Heltec, whose serial module in TEXTMSG mode
// broadcasts every line it receives - so the whole mesh got spammed with
// "WATCHDOG_RESET" text messages. The node never actually reset.
//
// Host bytes are now parsed as commands and NOTHING is ever forwarded to the
// Heltec. Anything this node writes to Serial1 becomes a public mesh
// broadcast, so that path stays closed.
// =============================================================================
static char usbLineBuf[128];
static int  usbLinePos = 0;
static uint32_t hostCmdsHandled = 0;
static uint32_t hostLinesDropped = 0;

static void handleHostLine(const char* line) {
  if (strcmp(line, "WATCHDOG_RESET") == 0) {
    // mesh-mapper.py's liveness poke. Answer immediately on USB so the mapper
    // sees the port produce data right away.
    hostCmdsHandled++;
    txPrintf("{\"heartbeat\":\"home_node active\",\"ack\":\"WATCHDOG_RESET\",\"uptime_s\":%u}\n",
             millis() / 1000);
    return;
  }
  if (strcmp(line, "STATUS") == 0) {
    hostCmdsHandled++;
    txPrintf("{\"heartbeat\":\"home_node active\",\"uptime_s\":%u}\n", millis() / 1000);
    return;
  }
  // Unknown host input is dropped, never forwarded to the mesh.
  hostLinesDropped++;
}

static void drainUsbCommands() {
  int budget = USB_IN_CHUNK;
  while (budget-- > 0 && Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (usbLinePos > 0) {
        usbLineBuf[usbLinePos] = '\0';
        handleHostLine(usbLineBuf);
        usbLinePos = 0;
      }
    } else if (usbLinePos < (int)sizeof(usbLineBuf) - 1) {
      usbLineBuf[usbLinePos++] = c;
    } else {
      usbLinePos = 0;   // over-long junk, resync
    }
  }
}

// =============================================================================
// Watchdog setup
//
// The core leaves the loop task unsubscribed (loopTaskWDTEnabled = false), so
// a wedged loop() would hang forever with no recovery. Subscribing it turns
// the watchdog into exactly what this node needs: an automatic reboot if the
// bridge ever stops making progress.
// =============================================================================
static void watchdogSetup() {
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = WDT_TIMEOUT_MS;
  cfg.idle_core_mask = (1 << 0);     // keep watching IDLE0, the default
  cfg.trigger_panic = true;          // panic -> clean reboot -> reset reason
  // CONFIG_ESP_TASK_WDT_INIT means the IDF already started the TWDT during
  // boot, so reconfigure is the only correct call - calling init first would
  // log "E task_wdt: TWDT already initialized" on every healthy boot.
#if defined(CONFIG_ESP_TASK_WDT_INIT) && CONFIG_ESP_TASK_WDT_INIT
  esp_task_wdt_reconfigure(&cfg);
#else
  if (esp_task_wdt_init(&cfg) == ESP_ERR_INVALID_STATE) {
    esp_task_wdt_reconfigure(&cfg);
  }
#endif
#else
  esp_task_wdt_init(WDT_TIMEOUT_MS / 1000, true);
#endif
  esp_task_wdt_add(NULL);            // subscribe this (the loop) task
}

// =============================================================================
// Arduino Setup
// =============================================================================
void setup() {
  // USB Serial -> computer (mesh-mapper.py)
  Serial.begin(UART_BAUD);

  // Deep RX buffer must be set before begin() to take effect. Combined with
  // the bounded drain this absorbs mesh bursts without dropping bytes.
  Serial1.setRxBufferSize(UART_RX_BUFFER);
  Serial1.begin(UART_BAUD, SERIAL_8N1, SERIAL1_RX_PIN, SERIAL1_TX_PIN);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);  // OFF (inverted)

  dedupInit();

  // Boot diagnostics before the watchdog is armed
  esp_reset_reason_t reason = esp_reset_reason();
  bool rtcValid = (rtcMagic == RTC_MAGIC);
  if (!rtcValid) {
    rtcMagic = RTC_MAGIC;
    rtcBootCount = 0;
    rtcLastUptimeMs = 0;
  }
  rtcBootCount++;

  watchdogSetup();

  // Let the Heltec V3 / Meshtastic come up. Kept as a fed, bounded wait
  // instead of a bare delay(3000) so the freshly armed watchdog is happy.
  uint32_t bootWaitStart = millis();
  while (elapsed(millis(), bootWaitStart) < 3000) {
    esp_task_wdt_reset();
    delay(50);
  }

  // Quiet by default: identity only. Detail is available on request (send
  // STATUS over USB) and problems still announce themselves - an abnormal
  // reset is reported below, and the periodic stats line carries the rest.
  txPrintln("");
  txPrintln("Mesh Detect - Node Mode / HOME");
  txPrintln("Mesh -> dedup -> USB");

  if (reason == ESP_RST_TASK_WDT || reason == ESP_RST_INT_WDT || reason == ESP_RST_WDT) {
    txPrintf("[WARN] Boot #%u after a WATCHDOG reset - the bridge stalled and self-recovered\n",
             rtcBootCount);
  } else if (reason == ESP_RST_BROWNOUT) {
    txPrintf("[WARN] Boot #%u after a BROWNOUT - check the USB supply and cable\n", rtcBootCount);
  } else if (reason == ESP_RST_PANIC) {
    txPrintf("[WARN] Boot #%u after a PANIC / exception\n", rtcBootCount);
  }
  txPrintln("");

  uint32_t now = millis();
  lastHeartbeat = now;
  lastStats = now;
  lastCleanup = now;

  // Quick LED triple-blink to show we're alive
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, LOW);
    delay(80);
    digitalWrite(LED_PIN, HIGH);
    delay(80);
  }
  esp_task_wdt_reset();
}

// =============================================================================
// Arduino Loop
//
// Every stage below is bounded. One pass is worst-case a few milliseconds, so
// the watchdog is fed comfortably and the node cannot wedge on a stalled host.
// =============================================================================
void loop() {
  uint32_t passStart = micros();
  uint32_t now = millis();

  esp_task_wdt_reset();
  rtcLastUptimeMs = now;          // survives a reset, reported on next boot

  // ----- Mesh UART -> dedup -> USB queue (bounded) -----
  drainUart(now);

  // ----- USB host commands (bounded; never forwarded to the mesh) -----
  drainUsbCommands();

  // ----- Push queued output to USB (bounded, never blocks) -----
  size_t flushed = txFlush();

  // ----- LED update -----
  ledUpdate(now);

  // ----- Dedup stale entry cleanup (every 10s) -----
  if (elapsed(now, lastCleanup) >= 10000) {
    dedupCleanStale(now);
    lastCleanup = now;
  }

  // ----- Heartbeat -----
  if (elapsed(now, lastHeartbeat) >= HEARTBEAT_MS) {
    int activeDrones = 0;
    for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
      if (dedupTable[i].active) activeDrones++;
    }
    txPrintf("{\"heartbeat\":\"home_node active\",\"tracked_drones\":%d,\"uptime_s\":%u}\n",
             activeDrones, now / 1000);
    lastHeartbeat = now;
  }

  // ----- Stats -----
  if (elapsed(now, lastStats) >= STATS_INTERVAL) {
    txPrintf("[HOME] Stats: %u received, %u forwarded, %u suppressed, %u non-json, %u bytes\n",
             msgReceived, msgForwarded, msgSuppressed, msgNonJson, totalBytes);
    txPrintf("[HOME] Health: loop max %uus, txq %u/%u used, %u bytes dropped in %u events, %u long lines\n",
             loopMaxUs, (unsigned)txUsed(), (unsigned)TXQ_SIZE,
             txDroppedBytes, txDropEvents, linesDropped);
    txPrintf("[HOME] Host: %u commands handled, %u unknown lines dropped (nothing is ever forwarded to the mesh)\n",
             hostCmdsHandled, hostLinesDropped);
    if (txDropEvents > 0) {
      txPrintln("[HOME] NOTE: output was dropped - the USB host is not reading fast enough.");
    }

    for (int i = 0; i < DEDUP_MAX_DRONES; i++) {
      if (dedupTable[i].active) {
        txPrintf("[HOME]   Drone %s: first node %s, %u dups blocked, age %us\n",
                 dedupTable[i].mac, dedupTable[i].firstNodeId,
                 dedupTable[i].dupsBlocked, elapsed(now, dedupTable[i].lastSeen) / 1000);
      }
    }

    loopMaxUs = 0;
    lastStats = now;
  }

  uint32_t passUs = micros() - passStart;
  if (passUs > loopMaxUs) loopMaxUs = passUs;

  // Yield so the idle task runs. Skip the delay only while there is real work
  // making progress: buffered mesh data, or queued output actually moving.
  // Crucially, "queue non-empty but the host is not draining" must NOT skip
  // the delay, or a stalled reader would pin this core in a hot spin forever.
  if (!Serial1.available() && (txUsed() == 0 || flushed == 0)) {
    delay(1);
  }
}
