// =================================================================
// Stage 4: Trip Logger (GPX logging + WiFi upload)
// =================================================================
// This is the real firmware. It ties together everything proven in
// Stages 1-3 (toolchain, AXP2101 PMU + GPS power, NMEA parsing) and
// adds the four pieces that make it a mileage tracker:
//
//   1. A trip state machine that auto-detects start (>5 mph for 10 s)
//      and end (<2 mph for 5 min), with hysteresis so stop-and-go
//      traffic doesn't split one drive into many.
//   2. GPX logging to microSD (LittleFS fallback) -- one line per timed fix,
//      file.close() after every write to limit power-cut data loss.
//      The latest 90 untimed points are buffered in RAM; older ones spill
//      to the selected trip store without times. Boot-time "fsck" attempts
//      repair; a maximum loss bound still needs hardware validation.
//   3. A FreeRTOS networking task pinned to core 0 that, when parked on
//      home WiFi, uploads finished .gpx files to the ingest server and
//      deletes them locally on success. GPS parsing stays on core 1 and
//      shares storage only for individual reads/writes.
//   4. AXP2101 VBUS-loss handling: when the car turns off (USB power
//      goes away), finalize the current trip cleanly and keep the file
//      for the next time we're home.
//
// Trackpoint UTC comes from each position's own RMC date/time, including
// centiseconds; missing RMC epochs stay untimed even after a clock sync.
// Fresh GPS time independently corrects the system clock every minute.
//
// Required libraries (U8g2 is needed in both display modes):
//   XPowersLib (lewisxhe), TinyGPSPlus (Mikal Hart). WiFi, HTTPClient,
//   SD, SPI, LittleFS, and Preferences ship with the ESP32 Arduino core.
//   U8g2 2.36.18 (olikraus) drives the optional test-drive OLED page.
//
// PARTITIONS: partitions.csv is unchanged (no OTA, 2.375 MB LittleFS
// fallback, about 5-6 driving hours). SD requires FAT32; never auto-format.
// New trips use SD when it mounts at boot. Both mounted stores are repaired
// and drained, SD first, so flash trips from a previous boot are not stranded.
//
// NOTE: targets ESP32 Arduino core 3.3.11 (XPowersLib 0.3.3,
// TinyGPSPlus 1.0.3). Recorded hardware checks are in the README.
// 2026-06-09 review fixes: per-chunk FS locking during upload, battery
// idle power-off, ARMING-phase point capture, stationary-jitter distance
// gate, fix-loss trip finalization. See ../../docs/history/CODE_REVIEW_2026-06-09.md.
// =================================================================

#include <Wire.h>
#include <XPowersLib.h>
#include <TinyGPSPlus.h>
#include <LittleFS.h>
#include <SD.h>
#include <SPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <time.h>
#include <sys/time.h>
#include <vector>
#include <esp_sntp.h>
#include <esp_partition.h>
#include <esp_task_wdt.h>
#include <esp_attr.h>
#include <atomic>
#include <new>
#include <cmath>

#include "secrets.h"
#include "pmu_startup_event.h"
#include "pmu_read_state.h"
#include "charge_temperature.h"
#include "journal_fs.h"
#include "journal_boot.h"
#include "revision_nvs.h"
#include "degraded_policy.h"
#include "startup_watchdog.h"
#include "journal_sd_geometry.h"
#include "diag_upload.h"
#include "journal_platform.h"
#include "runtime_faults.h"
#include "rtc_runtime.h"

#include "runtime_debug.h"
// Core 1 is the only writer. Core 0 reads the mode, never the button or OLED.
static std::atomic<bool> gDebugMode{false};
static bool debugMode();
static constexpr int DEBUG_BOOT_PIN = 0;
static runtime_debug::BootButton gBootButton;
static void toggleRuntimeDebug();
static void serviceDisplay();
enum class DisplayOwner { Production, Debug, Feedback };
static DisplayOwner gDisplayOwner = DisplayOwner::Production;
static bool gModeFeedback = false, gDebugNeedsWake = true;
static uint32_t gModeFeedbackMs = 0;
static constexpr uint32_t DEBUG_FEEDBACK_MS = 2000;
#if __has_include("firmware_version.h")
#include "firmware_version.h"
#endif
#include "build_version.h"
#include "debug_display.h"
static uint32_t gDebugDrawMs = 0; // loop-owned redraw cadence, not renderer state
#include "debug_temperature.h"
static float gDebugTempDie = NAN;
static uint32_t gDebugTempDieMs = 0, gDebugTempLogMs = 0;
static bool gDebugTempEnabled = false, gDebugTempPending = false;
static debug_temperature::TsSetup gDebugTempTs;
static bme280_temp::Sensor gDebugTempBme;
#include "production_display.h"
static display::DisplayModel gDisplayModel;
static bool gDisplayTripFinalized = false, gDisplayUserRequest = false;
static bool gDisplayUsbPowerRequest = false; // core-1 power handler -> renderer
#include "debug_gsv.h"
#include "debug_rf.h"
#include "gps_antenna.h"

// -----------------------------------------------------------------
// LilyGO T-Beam Supreme (ESP32-S3 + MAX-M10S), measured 2026-10-18
// -----------------------------------------------------------------
#define I2C_SDA      42   // Wire1: PMU + PCF8563 RTC
#define I2C_SCL      41
#define GPS_RX_PIN   9
#define GPS_TX_PIN   8
#define GPS_EN_PIN   7
#define GPS_BAUD     9600
#define PMU_IRQ_PIN  40   // active-low; not an RTC GPIO on the S3
// LilyGO utilities.h / LoRaBoards.cpp at 1991b1ed70915d4f246b841f99f6027166ea7330.
// SD wiring checked by Stage 2 d, 2026-10-18. Board uses QSPI, not octal PSRAM.
static constexpr int SD_MOSI = 35, SD_SCK = 36, SD_MISO = 37, SD_CS = 47, IMU_CS = 34;
static constexpr uint32_t SD_SPI_HZ = 4000000; // core default; ample for ~119 B/s GPX
static constexpr uint32_t SD_SETTLE_MS = 250;

// -----------------------------------------------------------------
// Trip state machine thresholds and timing
// -----------------------------------------------------------------
static constexpr float    START_MPS       = 2.235f;          // 5 mph
static constexpr float    END_MPS         = 0.894f;          // 2 mph
static constexpr uint32_t ARM_MS          = 10UL  * 1000;    // sustain to start
static constexpr uint32_t COOL_MS         = 300UL * 1000;    // sustain to end
// First guesses: calibrate from drive/indoor logs; err low on mileage.
static constexpr float    SPEED_ACC_ARM_MPS = 0.5f;
static constexpr float    SPEED_ACC_TRIP_MPS = 1.0f;
static constexpr float    HACC_ARM_M      = 5.0f;
static constexpr float    HACC_TRIP_M     = 10.0f;
static constexpr unsigned RESUME_FIXES    = 3; // consecutive trusted moving fixes
// Arrival-time association, allowing either UBX/NMEA ordering at 1 Hz.
static constexpr uint32_t NAV_PVT_FRESH_MS = 1500;
static constexpr uint32_t NAV_PVT_FALLBACK_MS = 10000;
// Let core 1 idle between bursts. At 9600 baud, 5 ms is about five bytes,
// far below the 2048-byte RX buffer; check the UART again just before resting.
static constexpr uint32_t LOOP_IDLE_MS = 5;
// Normal passes take milliseconds; shutdown has two bounded 15 s handoffs.
// Allow 60 s for slow card/driver calls without masking a stuck loop task.
// Large boot scans run before enrollment; runtime repair feeds on byte progress.
static constexpr uint32_t LOOP_WDT_TIMEOUT_MS = 60000;
static constexpr float    MIN_TRIP_METERS = 160.0f;          // ~0.1 mi
static constexpr uint32_t MIN_TRIP_MS     = 30UL  * 1000;
static constexpr size_t   TRIP_SPLIT_BYTES = 8 * 1024 * 1024; // half the server cap
static constexpr size_t   TRKPT_LINE_BYTES = 256;            // includes terminating NUL

// Fix-loss heuristic: gap this long between valid fixes starts a new
// <trkseg> so viewers don't draw a straight line across a tunnel.
static constexpr uint32_t SEG_GAP_MS      = 5000;
// An unconfirmed start gets one arming window plus the normal gap margin.
// No file exists yet; release ARMING so battery idle power-off can run.
static constexpr uint32_t ARM_FIX_LOSS_MS = ARM_MS + SEG_GAP_MS;

// If a trip is ACTIVE/COOLDOWN and we go this long without ANY valid fix
// (parked in a garage, GPS failure), finalize the trip rather than leave
// it open until a power event.
static constexpr uint32_t FIX_LOSS_END_MS = 300UL * 1000;   // 5 min

// On battery (VBUS lost) with no trip in progress, power off after this
// long. Ample time for the post-drive upload (a trip ending clears the
// retry backoff, so tries come at ~0, 1, 3 and 7 min after parking), and
// it stops the GPS rail + WiFi scans from draining the 18650 for days.
// A VBUS insert edge (car back on) powers the PMU on for a full boot.
static constexpr uint32_t IDLE_BATT_SLEEP_MS = 10UL * 60 * 1000;

// Power-off never unmounts storage under an upload (issue #3). When the idle
// timer runs out mid-upload, the file gets up to NET_DRAIN_MS to finish:
// the server's 900 s worker timeout bounds one upload, plus time to scan
// and connect. After that, or at once for a critical battery or a held
// power key, the upload is cut off and the network task gets NET_ABORT_MS
// to close the file and switch WiFi off. A cut-off trip stays queued.
static constexpr uint32_t NET_DRAIN_MS = 16UL * 60 * 1000;
static constexpr uint32_t NET_ABORT_MS = 15UL * 1000;
static constexpr uint32_t TRIP_CONNECT_TIMEOUT_MS = 5000;
// An 8 MiB trip parses in about 0.4 s on a desktop. Allow slower CPUs and
// durable disk writes 10 s to reply, while leaving margin in the 15 s
// shutdown handshake: gNetAbort cannot interrupt HTTPClient's reply wait.
static constexpr uint16_t TRIP_REPLY_TIMEOUT_MS = 10000;
static_assert(TRIP_REPLY_TIMEOUT_MS < NET_ABORT_MS, "trip reply must fit the shutdown abort window");

// How often the networking task wakes to look for pending uploads.
static constexpr uint32_t NET_TICK_MS     = 30000;
static constexpr uint32_t NET_RETRY_MAX_MS = 5UL * 60 * 1000;
// 12 KiB network stack; real-upload free-stack measurements are recorded in #6.
// ESP-IDF task stack sizes and high-water marks are in bytes, not words.
static constexpr uint32_t NET_STACK_BYTES = 12288;
// #6: 2 KiB leaves warning room before HTTP/journal calls exhaust the stack.
static constexpr uint32_t NET_STACK_WARN_BYTES = 2048;

// Storage layout.
static const char* TRIP_DIR = "/trips";
static constexpr const char* TRIP_PARTITION_LABEL = "spiffs";

// Free-space guard. A full partition silently loses points, and LittleFS
// misbehaves at 100% (joltwallet/esp_littlefs#183). Don't start a trip
// below FS_START_FREE (~9 min of driving at ~118 B/s); end the active one
// cleanly, so it stays uploadable, once free space falls below
// FS_STOP_FREE. The gap between the two stops a trip that was just ended
// from immediately restarting. Uploads at home free the space again.
static constexpr size_t   FS_START_FREE = 64 * 1024;
static constexpr size_t   FS_STOP_FREE  = 24 * 1024;
static constexpr uint32_t FS_CHECK_MS   = 30UL * 1000;  // usedBytes() walks the FS

// -----------------------------------------------------------------
// Globals
// -----------------------------------------------------------------
XPowersAXP2101 PMU;
// Journal #124 emits a notice from this saved result via makePmuStartupEvent.
PmuStartupResult gPmuStartup;
TinyGPSPlus    gps;
HardwareSerial GPSSerial(1);
Preferences    prefs;

// RMC status field ('A' = valid fix). Committed only when an RMC sentence
// passes its checksum, so isUpdated() means "an RMC just arrived" -- the
// once-per-epoch signal loop() processes fixes on. "GN" is the talker ID
// when the receiver uses more than one constellation, "GP" when
// it uses GPS alone.
TinyGPSCustom  gRmcStatusGN(gps, "GNRMC", 2);
TinyGPSCustom  gRmcStatusGP(gps, "GPRMC", 2);
TinyGPSCustom  gRmcTimeGN(gps, "GNRMC", 1);
TinyGPSCustom  gRmcDateGN(gps, "GNRMC", 9);
TinyGPSCustom  gRmcTimeGP(gps, "GPRMC", 1);
TinyGPSCustom  gRmcDateGP(gps, "GPRMC", 9);

static DebugGsv gDebugGsv;
static DebugRf gDebugRf;
static uint32_t gDebugRfPollMs = 0;
static bool gDebugRfStarted = false;
static GpsAntenna gGpsAntenna;
// Last completed interval, retained for runtime hook retries/repeats after
// the next poll invalidates gDebugRf. Unmeasured context stays omitted.
static DebugRf gAntennaRf;
static DebugSignal gAntennaSignal;
static bool gDebugRmcValid = false;
// Core 0 publishes a coherent network snapshot; core 1 only copies it.
// Critical sections contain just scalar copies, never I/O or formatting.
static portMUX_TYPE gDebugNetMux = portMUX_INITIALIZER_UNLOCKED;
static DebugNetwork gDebugNet;

static startup::DegradedPolicy gDegraded;

SemaphoreHandle_t gFsMutex;     // serializes ALL filesystem access
SPIClass          gSdSpi(HSPI); // dedicated bus instance; radio is never initialized
fs::FS*           gTripFS = &LittleFS;
// Mount membership stays fixed for shutdown. A failed SD is latched out until
// reboot; core 1 may select the mounted flash queue only between trips.
bool              gSdMounted = false, gFlashMounted = false;
static std::atomic<bool> gSdUnavailable{false};
volatile bool     gVbusLost   = false;
volatile bool     gPmuIrqFlag = false;
// Shared by core 0 (SNTP) and core 1 (GPS/fix construction).
std::atomic<bool> gClockSet{false};  // a source has synchronized UTC this boot
static diagnostics::DiagnosticClock gDiagnosticClock;
static diagnostics::RtcRuntime gRtc;
// Power-off/upload handshake (issue #3). Core 0 raises gNetBusy before it reads
// gNetHold; core 1 sets gNetHold before it reads gNetBusy. With seq_cst
// atomics at least one side sees the other, so once core 1 reads gNetBusy
// false the network task can't start a new cycle. gNetAbort cuts off an
// upload in progress (LockedFileStream ends the body).
std::atomic<bool> gNetBusy{false};   // a network cycle is running
std::atomic<bool> gNetHold{false};   // don't start a cycle or another file
std::atomic<bool> gNetAbort{false};  // stop the current upload now
// Set by core 1 when a trip ends: the car just parked, maybe at home, and
// on battery it powers off in IDLE_BATT_SLEEP_MS. Core 0 clears the backoff.
std::atomic<bool> gNetRetryNow{false};
// Owned by core 0. Zero wait means the next queued trip can try immediately.
uint32_t          gNetLastAttemptMs = 0;
uint32_t          gNetRetryMs = 0;
// GPS correction cadence belongs only to core 1; SNTP never suppresses it.
bool              gGpsClockSet = false;
uint32_t          gLastGpsSyncMs = 0;
static constexpr uint32_t GPS_CLOCK_SYNC_MS = 60UL * 1000;

// Core 1 owns receiver configuration. UART receive and ACK deadlines are
// serviced by loop(), never by a blocking ACK wait during a drive.
static constexpr uint32_t GPS_READY_MS = 3000;
// NMEA can start before UBX input is ready. Five attempts spaced 8 s after
// each failure put the last attempt at least 32 s after configuration starts.
static constexpr uint32_t GPS_CONFIG_RETRY_MS = 8000;
// 9600 baud, 8N1 = 960 bytes/s. A ~1 KB GSV burst alone takes ~1.1 s;
// allow 2 s for that backlog plus default NMEA, NAV-PVT (~100 B/s) and ACK.
// Do not resend while that window is open (bench backlog described in #177).
static constexpr uint32_t GPS_ACK_MS = 2000;
static constexpr uint8_t GPS_CONFIG_ATTEMPTS = 5;
static constexpr uint8_t GPS_COMMAND_SENDS = 3;
enum GpsConfigState { GPS_CONFIG_OFF, GPS_WAIT_READY, GPS_WAIT_ACK, GPS_RETRY, GPS_CONFIG_DONE };
struct GpsConfig {
    GpsConfigState state = GPS_CONFIG_OFF;
    uint32_t since = 0, started = 0;
    uint8_t attempts = 0, sends = 0, sendGeneration = 0;
    int8_t ack = -1;
    bool readyLogged = false, retryLogged = false;
};
static GpsConfig gGpsConfig;

// Demultiplex UBX before TinyGPSPlus. Both builds retain NAV-PVT and ACKs;
// the same bounded buffer also accommodates MON-RF for the antenna check.
struct GpsUbxRx {
    uint32_t pos = 0, since = 0;
    uint16_t len = 0;
    uint8_t cls = 0, id = 0, a = 0, b = 0;
    uint8_t configGeneration = 0;
    uint8_t payload[256] = {};
    bool checksumA = false;
};
static GpsUbxRx gGpsUbxRx;

// Coordinate-free subset of NAV-PVT (SPG 5.10, UBX-21035062 R03, 3.15.11).
// valid describes UTC, not speed quality: speed uses fixType/gnssFixOK/sAcc.
struct GpsNavPvt {
    bool seen = false;
    uint32_t receivedMs = 0, iTow = 0, hAccMm = 0, sAccMmps = 0;
    int32_t gSpeedMmps = 0;
    uint8_t valid = 0, fixType = 0, flags = 0, numSv = 0;
};
static GpsNavPvt gNavPvt;
static uint32_t gNavPvtWaitMs = 0;
static bool gSpeedFallback = false;
static bool gLatestSpeedTrusted = false;

// Announce demand BEFORE waiting: journal recovery checks it between VFS
// operations, then releases the mutex. Trip/upload callers keep their existing
// blocking lock semantics; the diagnostic consumer never waits for this lock.
static std::atomic<uint32_t> gFsDemand{0};
#define FS_LOCK()   do { gFsDemand.fetch_add(1); xSemaphoreTake(gFsMutex, portMAX_DELAY); } while (0)
#define FS_UNLOCK() do { xSemaphoreGive(gFsMutex); gFsDemand.fetch_sub(1); } while (0)

static std::atomic<bool> gJournalSealRequested{false}, gJournalSealDone{false}, gJournalSealOk{false};
// Core 1 fills the immutable shutdown notice before publishing the seal request.
// Core 0 drains a queue slot, submits it once, then seals. Static task storage.
static diagnostics::EventInput gPowerOffEvent;
static std::atomic<bool> gPowerOffEventPending{false};
static diagnostics::BatterySample gBootBattery;
static bool gJournalSealing = false; // core-0 only, while holding the mutex
static bool gEventIoBudget = false; // core-0 only, cooperative VFS deadline
static uint32_t gEventIoStarted = 0;
static bool journalMayRun();
static bool sdJournalMayRun();
static JournalPlatformIo gSdJournalIo("/sd"), gFlashJournalIo("/littlefs");
static diagnostics::MountedJournalFs<JournalPlatformIo> gSdJournalFs(gSdJournalIo, gSdMounted, sdJournalMayRun);
static diagnostics::MountedJournalFs<JournalPlatformIo> gFlashJournalFs(gFlashJournalIo, gFlashMounted, journalMayRun);
// Static storage, never the 12 KiB network task stack.
static diagnostics::NvsRevisionState gRevisionState;
static diagnostics::EventLog gJournal(nullptr, nullptr, diagnostics::uploadJournalConfig(&gRevisionState));
static diagnostics::JournalBoot gJournalBoot;
static bool gJournalReady = false; // set once on core 1 before task creation
static bool gJournalStarted = false; // under FS_LOCK; core-0 consumer only
static bool gJournalBootReported = false;
// Single writer per array/builder. Only EventLog's try-lock crosses cores.
static diagnostics::RuntimeFaults::Condition gLoopConditions[] = {
    {2001}, {2002}, {2003}, {2004}, {2005}, {2006}, {2008}, {3001}, {3002}, {3005}, {3006}, {3007}, {3008}, {3009},
    {4001}, {4002}, {4003}, {4004}, {4005}, {4006}, {4008}, {6001}
};
static diagnostics::RuntimeFaults::Condition gNetConditions[] = {
    {1007}, {5002}, {5003}, {5004}, {5005}, {5006}, {5007}, {5008}, {5009}
};
static diagnostics::RuntimeFaults gLoopFaults(gJournal, gLoopConditions, sizeof(gLoopConditions) / sizeof(gLoopConditions[0]));
static diagnostics::RuntimeFaults gNetFaults(gJournal, gNetConditions, sizeof(gNetConditions) / sizeof(gNetConditions[0]));
static diagnostics::RuntimeFaults::Condition gEventConditions[] = {
    {4008}, {5003}, {5004}, {5005}, {5006}, {5007}, {5008}, {5009}, {5010}
};
static diagnostics::RuntimeFaults gEventFaults(gJournal, gEventConditions, sizeof(gEventConditions) / sizeof(gEventConditions[0]));
static uint32_t gContactTick = 0;
static uint64_t gContactUptime = 0, gLastContact = 0;
static bool gContactKnown = false;
static uint32_t gGpsDiagStartMs = 0, gLastNmeaMs = 0, gIdleFixWaitMs = 0;
static uint32_t gStorageDiagMs = 0, gBatteryDiagMs = 0;
static bool gBatteryDiagChecked = false;
static uint8_t gJoinFailures = 0, gTransportFailures = 0;

struct TripRecovery {
    const char* reason = "read";
    bool repaired = false, pointsKnown = false;
    uint32_t points = 0;
};

// A single GPS fix, with monotonic receipt time and its own RMC UTC epoch.
struct Fix {
    double   lat, lon;
    float    speedMps, hdop;
    bool     hasUtc;
    bool     newSegment = false;  // travels with this fix through both buffers
    bool     speedTrusted = true;
    uint64_t utcMs;
    uint32_t monoMs;
};

// Batches writes to one open file and confirms they actually reached flash.
// File::write() only copies into a 4 KB stdio buffer, so it reports success
// even when the flash write later fails -- and close() swallows that error.
// commit() flushes (fflush + fsync) and checks the on-disk size grew by
// exactly the bytes queued, the only reliable signal the Arduino FS API
// offers. One commit per batch keeps it to a single sync per point.
// (Defined up here, above every function, because the Arduino builder
// inserts its generated prototypes before the first function.)
struct FileWriter {
    File&  f;
    size_t start;
    size_t queued = 0;
    bool   ok = true;

    explicit FileWriter(File& file) : f(file), start(file.size()) {}

    void put(const char* s, size_t n) {
        if (!ok) return;
        if (f.write((const uint8_t*)s, n) == n) queued += n;
        else ok = false;
    }
    void put(const char* s) { put(s, strlen(s)); }

    bool commit() {
        f.flush();
        return ok && f.size() == start + queued;
    }
};

// Ring buffer of fixes captured before UTC was available. One fix per
// 1 Hz epoch (see loop()), so this covers ~90 s -- a cold start. Once
// full, spill the oldest fix to flash without a time before adding more.
static constexpr size_t PENDING_CAPACITY = 90;
static Fix    gPend[PENDING_CAPACITY];
static size_t gPendHead = 0, gPendCount = 0;

// -----------------------------------------------------------------
// Trip FSM state
// -----------------------------------------------------------------
enum TripState { IDLE, ARMING, ACTIVE, COOLDOWN, FINALIZING };
static TripState gState = IDLE;
static uint32_t  gArmT0 = 0, gCoolT0 = 0, gTripT0 = 0;
static unsigned  gResumeFixes = 0;
static uint32_t  gLastMoveMs = 0;       // the active trip's last moving fix
static uint32_t  gFsCheckT0 = 0;        // last free-space check during a trip
static uint32_t  gFullWarnT0 = 0;       // last "storage full" log line
static bool      gFullWarned = false;
static uint32_t  gIdleSince = 0;        // last time we were NOT (on battery + IDLE)
static double    gTripMeters = 0;
static size_t    gTripBytes = 0;        // committed prologue + trackpoint bytes
static bool      gTripContinuation = false; // piece started by a size split

// Fixes seen while ARMING, so the first ~10 s of a trip isn't lost.
// One fix per 1 Hz epoch makes a 10 s arm window 10-11 fixes; 16 slots is
// comfortable headroom. Ring: oldest overwritten on overflow.
static Fix    gArmBuf[16];
static size_t gArmHead = 0, gArmCount = 0;

static uint64_t contactUptime() {
    uint32_t now = millis();
    gContactUptime += uint32_t(now - gContactTick); gContactTick = now;
    return gContactUptime;
}
static bool debugMode() { return gDebugMode.load(); }

static bool gLoopWatchdogArmed = false; // loop task only, never core 0
static uint32_t gLoopWatchdogTimeoutMs = 0;
static RTC_NOINIT_ATTR volatile startup_watchdog::Record gStartupWatchdogRecord;
static startup_watchdog::Policy gStartupWatchdog;
static void saveStartupWatchdog() {
    auto r = gStartupWatchdog.record();
    // Invalidate first, publish magic last. A reset during these stores
    // leaves either a complete record or invalid history, never a false latch.
    gStartupWatchdogRecord.magic = 0;
    gStartupWatchdogRecord.stage = r.stage;
    gStartupWatchdogRecord.hangs = r.hangs;
    gStartupWatchdogRecord.fallback = r.fallback;
    gStartupWatchdogRecord.checksum = r.checksum;
    gStartupWatchdogRecord.magic = r.magic;
}
static void armLoopWatchdog(uint32_t timeoutMs = LOOP_WDT_TIMEOUT_MS, bool reportError = true) {
    if (gLoopWatchdogArmed && gLoopWatchdogTimeoutMs == timeoutMs) return;
    uint32_t idleMask = 0;
#if CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0
    idleMask |= 1U;
#endif
#if CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1
    idleMask |= 2U;
#endif
    // Keep the core's idle-task subscriptions. The installed S3 core starts
    // TWDT at 5 s with panic enabled; explicitly retain panic (reset), not
    // just a warning, with the longer startup/runtime limits.
    // IDF 5.5.5 reconfigure stops/restarts the timer under its spinlock and
    // retains task subscriptions. Never unenroll at the startup hand-over.
    if (gLoopWatchdogArmed) feedLoopWDT(); // previous stage has returned
    const esp_task_wdt_config_t config = {timeoutMs, idleMask, true};
    esp_err_t error = esp_task_wdt_reconfigure(&config);
    if (error == ESP_ERR_INVALID_STATE) error = esp_task_wdt_init(&config);
    if (error == ESP_OK) {
        if (!gLoopWatchdogArmed) enableLoopWDT(); // core feeds before loop()
        if (esp_task_wdt_status(nullptr) == ESP_OK) {
            gLoopWatchdogArmed = true;
            gLoopWatchdogTimeoutMs = timeoutMs;
            feedLoopWDT();
            return;
        }
    }
    // Carry on without it. Recording trips matters more than the safety net,
    // and restarting here would loop forever if arming can never succeed.
    if (reportError) Serial.println("ERROR: loop watchdog could not be armed/configured; carrying on.");
}

static void feedLoopWatchdogProgress() {
    // Some file helpers are shared. Never feed or enroll the core-0 network
    // task, and never feed while waiting to acquire FS_LOCK or for another task.
    if (xPortGetCoreID() == 1 && gLoopWatchdogArmed) feedLoopWDT();
}

static void startupStage(startup_watchdog::Stage stage) {
    if (!gStartupWatchdog.active()) return; // runtime mounts are not startup
    feedLoopWatchdogProgress(); // only after the previous stage returned
    gStartupWatchdog.stage(stage);
    saveStartupWatchdog();
}
static void finishStartupWatchdog() {
    // Switch to the loop limit. If the early arm failed, this is one more
    // try; a second failure prints an error and logging carries on unwatched.
    armLoopWatchdog();
    gStartupWatchdog.complete();
    saveStartupWatchdog();
}
static void printStartupWatchdog() {
    using startup_watchdog::Stage;
    if (!gLoopWatchdogArmed) Serial.println("ERROR: startup watchdog could not be armed; startup is not watched.");
    if (gStartupWatchdog.hungStage() != Stage::None)
        Serial.printf("STARTUP WATCHDOG: previous hang in %s; consecutive startup hangs %lu.\n",
            startup_watchdog::stageText(gStartupWatchdog.hungStage()), (unsigned long)gStartupWatchdog.hangs());
    if (gStartupWatchdog.skipCard()) Serial.println(gStartupWatchdog.noStorage() ?
        "STARTUP WATCHDOG: skipping all storage and recovery probes until next power-on; no trip recording." :
        "STARTUP WATCHDOG: skipping card until next power-on; using internal flash for trips.");
}

static void serverContact() { gLastContact = contactUptime(); gContactKnown = true; }
static bool heartbeatDue() {
    return !gContactKnown || contactUptime() - gLastContact >= diagnostics::kHeartbeatMs;
}

static const char* tripBackend(fs::FS& storage) { return &storage == &SD ? "sd" : "littlefs"; }

static bool tripStoreAvailable(fs::FS& storage) {
    return &storage == &SD ? gSdMounted && !gSdUnavailable.load() : gFlashMounted;
}

static void tripWriteResult(fs::FS& storage, bool ok, const char* operation) {
    if (gJournalReady) gLoopFaults.event(4006, millis()).enumeration("backend", tripBackend(storage))
        .enumeration("operation", operation).condition(!ok);
}

static void batteryCondition(uint16_t code, bool active, uint32_t now) {
    gLoopFaults.event(code, now);
    unsigned mv = PMU.getBattVoltage();
    if (mv <= 6000) gLoopFaults.integer("battery_mv", mv);
    gLoopFaults.condition(active);
}

static void storageConditions(uint64_t freeB, uint32_t now) {
    if (!gJournalReady) return;
    gLoopFaults.event(4001, now).condition(!gSdMounted || gSdUnavailable.load());
    bool available = tripStoreAvailable(*gTripFS);
    gLoopFaults.event(4004, now).enumeration("backend", tripBackend(*gTripFS));
    if (available) gLoopFaults.integer("free_bytes", freeB);
    gLoopFaults.condition(available && freeB < FS_START_FREE);
    gLoopFaults.event(4005, now).enumeration("backend", tripBackend(*gTripFS));
    if (available) gLoopFaults.integer("free_bytes", freeB);
    gLoopFaults.condition(available && freeB < FS_STOP_FREE);
}

static bool journalMayRun() {
    return gFsDemand.load() == 0 && (gJournalSealing || !gNetHold.load()) &&
           (!gEventIoBudget || (!gNetAbort.load() && uint32_t(millis() - gEventIoStarted) < diagnostics::kDrainMs));
}

static bool sdJournalMayRun() {
    return !gSdUnavailable.load() && journalMayRun();
}

static void journalStorageFaults() {
    // Consumer only, never called by fail() or submit(). Normal boots use core
    // 0's existing builder; degraded boots have only the core-1 loop consumer.
    auto fault = gJournal.storageFault();
    if (!fault.observed) return;
    auto& faults = gDegraded.mode() == startup::Mode::Normal ? gEventFaults : gLoopFaults;
    faults.event(4008, millis()).enumeration("operation", fault.operation)
        .enumeration("backend", fault.backend == diagnostics::EventLog::Backend::Sd ? "sd" : "littlefs")
        .condition(fault.active);
}

static void serviceJournal() {
    if (!gJournalReady) return;
    bool sealing = gJournalSealRequested.load() && !gJournalSealDone.load();
    if ((!sealing && !journalMayRun()) || xSemaphoreTake(gFsMutex, 0) != pdPASS) return;
    gJournalSealing = sealing;
    if (!journalMayRun()) { gJournalSealing = false; xSemaphoreGive(gFsMutex); return; }
    if (!gJournalStarted) {
        uint32_t cluster = tripStoreAvailable(SD) ? diagnostics::journalSdCluster(SD, journalMayRun) : 0;
        // A trip arriving during geometry probing gets the lock next. Retry
        // initialization later instead of permanently disabling a healthy SD.
        if (!journalMayRun()) { gJournalSealing = false; xSemaphoreGive(gFsMutex); return; }
        gJournal.setSdAllocationUnit(cluster);
        gJournalStarted = true;
    }
    // Boot recovery and later maintenance each service exactly one record.
    gJournal.service(1);
    journalStorageFaults();
    if (debugMode() && !gJournalBootReported) {
        auto backlog = gJournal.backlog();
        backlog.complete = backlog.complete && journalMayRun();
        auto stats = gJournal.stats();
        const char* backend = stats.backend == diagnostics::EventLog::Backend::Sd ? "sd" :
                              stats.backend == diagnostics::EventLog::Backend::LittleFs ? "littlefs" : "ram";
        Serial.printf("Journal: %s, %llu unacknowledged records, %u segments%s; boot counter %s.\n",
                      backend, (unsigned long long)backlog.records, (unsigned)backlog.segments,
                      backlog.complete ? "" : " (inventory incomplete)",
                      gJournalBoot.counterPersisted ? "persisted" : "unavailable");
        gJournalBootReported = true;
    }
    if (sealing && gPowerOffEventPending.load()) {
        auto result = gJournal.submit(gPowerOffEvent);
        if (result == diagnostics::EventLog::Submit::Accepted) gPowerOffEventPending.store(false);
        // Full/busy stays pending for the next service tick, within the same
        // bounded shutdown deadline. Never submit a second accepted notice.
    }
    if (sealing && !gPowerOffEventPending.load()) {
        bool sealed = gJournal.seal();
        journalStorageFaults();
        gJournalSealOk.store(sealed && gJournal.stats().queued == 0);
        gJournalSealDone.store(true);
    }
    // A cancelled metadata read must not remain cached as "missing metadata".
    // EventLog's bool read API also uses false for an absent/torn slot. Reload
    // both stores after any cooperative cancellation before later appends.
    if (!journalMayRun()) gJournal.invalidateStorage();
    gJournalSealing = false;
    xSemaphoreGive(gFsMutex);
}

static void serviceGpsSpeedFallback() {
    // NAV-PVT is not expected until boot configuration finishes (or gives up).
    if (gGpsConfig.state != GPS_CONFIG_DONE) return;
    bool fallback = uint32_t(millis() - (gNavPvt.seen ? gNavPvt.receivedMs : gNavPvtWaitMs)) >
                    NAV_PVT_FALLBACK_MS;
    if (fallback != gSpeedFallback) {
        Serial.println(fallback ?
            "WARNING: GPS NAV-PVT unavailable for 10 s -- trusting RMC speed (fallback)." :
            "GPS NAV-PVT returned -- speed accuracy gating restored.");
        gSpeedFallback = fallback;
    }
}

static bool gpsSpeedTrusted(bool starting) {
    if (gSpeedFallback) return true;
    float limit = starting ? SPEED_ACC_ARM_MPS : SPEED_ACC_TRIP_MPS;
    float hAccLimit = starting ? HACC_ARM_M : HACC_TRIP_M;
    return gNavPvt.seen && uint32_t(millis() - gNavPvt.receivedMs) <= NAV_PVT_FRESH_MS &&
           (gNavPvt.flags & 1) && (gNavPvt.fixType == 3 || gNavPvt.fixType == 4) &&
           gNavPvt.sAccMmps <= limit * 1000.0f && gNavPvt.hAccMm <= hAccLimit * 1000.0f;
}

static void armBufReset() { gArmHead = gArmCount = 0; }

static void armBufPush(const Fix& fx) {
    gArmBuf[(gArmHead + gArmCount) % 16] = fx;
    if (gArmCount < 16) gArmCount++;
    else gArmHead = (gArmHead + 1) % 16;
}
static char      gTripPath[48] = {0};   // active file, "" when none
static bool      gTripWriteFailed = false;  // a write to gTripPath didn't land
static bool      gHaveLastFix = false;
static Fix       gLastFix;
static bool      gDistanceContinuous = true; // never bridge across a rejected point
// Every valid RMC, including rejected speed: separate from last accepted point.
static uint32_t  gLastFixMonoMs = 0;

// Keep scalar history current even with debug off, so runtime entry cannot
// show stale WiFi/upload state. Expensive scans and serial output stay gated.
static void debugWifi(DebugWifi wifi) {
    portENTER_CRITICAL(&gDebugNetMux);
    gDebugNet.wifi = wifi;
    portEXIT_CRITICAL(&gDebugNetMux);
}

static void debugUpload(int code) {
    uint32_t now = millis();
    portENTER_CRITICAL(&gDebugNetMux);
    gDebugNet.uploadCode = code ? code : -1;
    gDebugNet.uploadMs = now;
    portEXIT_CRITICAL(&gDebugNetMux);
}

// =================================================================
// Time helpers
// =================================================================
static uint64_t nowEpochMs() {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
}

static void stampDiagnosticTime(diagnostics::EventInput& event) {
    uint32_t now = millis();
    event.uptimeMs = now;
    bool trusted = gClockSet.load();
    gDiagnosticClock.fill(event, now, trusted, trusted ? nowEpochMs() : 0);
}

// Core 1 only. Both cores' event builders read atomic publications, with no I2C.
static void rtcConditions() {
    if (!gJournalReady || !gRtc.begun()) return;
    gLoopFaults.event(3007, millis()).condition(gRtc.unavailable());
    bool invalid = gRtc.invalid();
    gLoopFaults.event(3008, millis());
    if (invalid) gLoopFaults.enumeration("reason", diagnostics::rtc::reason(gRtc.invalidReason()));
    gLoopFaults.condition(invalid);
}

static void serviceRtc() {
    if (xPortGetCoreID() != 1) return;
    gDiagnosticClock.tick(millis());
    gRtc.service(Wire1, gDiagnosticClock, gClockSet.load(), nowEpochMs(), gDiagnosticClock.latestSource());
    rtcConditions();
}

// ISO 8601 UTC: whole seconds stay compact; retain nonzero milliseconds.
static void isoFromEpochMs(uint64_t ms, char* out, size_t n) {
    time_t secs = (time_t)(ms / 1000);
    struct tm tmv;
    gmtime_r(&secs, &tmv);
    char seconds[20];
    if (strftime(seconds, sizeof(seconds), "%Y-%m-%dT%H:%M:%S", &tmv) == 0) {
        if (n) out[0] = 0;
        return;
    }
    unsigned fraction = ms % 1000;
    int written = fraction ? snprintf(out, n, "%s.%03uZ", seconds, fraction) :
                             snprintf(out, n, "%sZ", seconds);
    if (n && (written < 0 || size_t(written) >= n)) out[0] = 0;
}

// Convert a UTC struct tm to a Unix epoch. The newlib that ships with
// the ESP32 core doesn't provide timegm() (and mktime() would wrongly
// apply a local-timezone offset), so we do it directly with Howard
// Hinnant's days-from-civil algorithm. GPS time is already UTC.
static time_t timegmUtc(const struct tm* t) {
    int y = t->tm_year + 1900;
    int m = t->tm_mon + 1;
    int d = t->tm_mday;
    y -= (m <= 2);
    long era = (y >= 0 ? y : y - 399) / 400;
    unsigned yoe = (unsigned)(y - era * 400);
    unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    long days = era * 146097 + (long)doe - 719468;
    return (time_t)(days * 86400L +
                    t->tm_hour * 3600L + t->tm_min * 60L + t->tm_sec);
}

// Validate the raw fields because TinyGPSPlus retains parsed values for empty
// RMC fields while still marking them newly committed.
static bool validRmcClockFields(TinyGPSCustom& time, TinyGPSCustom& date) {
    bool updated = time.isUpdated() && date.isUpdated();
    const char* rawTime = time.value();
    const char* rawDate = date.value();
    if (!updated || strlen(rawTime) < 6 || strlen(rawDate) != 6) return false;
    for (unsigned i = 0; i < 6; ++i) {
        if (rawTime[i] < '0' || rawTime[i] > '9' ||
            rawDate[i] < '0' || rawDate[i] > '9') return false;
    }
    if (rawTime[6] == 0) return true;
    if (rawTime[6] != '.' || rawTime[7] == 0) return false;
    for (const char* p = rawTime + 7; *p; ++p)
        if (*p < '0' || *p > '9') return false;
    return true;
}

// Called at each checksum-valid sentence, before another sentence can mix a
// new GGA time with an older RMC date (especially at midnight). Reading year()
// consumes date.isUpdated(), even while correction is rate limited.
// Also capture this sentence's epoch before the rate-limited clock sync
// consumes date/time's updated flags. Never add receipt age to a trackpoint.
static void setClockFromGps(uint64_t* rmcUtcMs = nullptr) {
    if (rmcUtcMs) *rmcUtcMs = 0;
    if (!gps.date.isUpdated()) return;
    // TinyGPSPlus renews cached date/time even when an RMC field is empty.
    // Check the sentence's raw fields before trusting its parsed values.
    bool rawGN = validRmcClockFields(gRmcTimeGN, gRmcDateGN);
    bool rawGP = validRmcClockFields(gRmcTimeGP, gRmcDateGP);
    bool fresh = (rawGN || rawGP) && gps.date.isValid() && gps.time.isValid() &&
                 gps.time.isUpdated() && gps.date.age() < 2000 && gps.time.age() < 2000;
    struct tm tmv = {};
    tmv.tm_year = gps.date.year() - 1900;
    tmv.tm_mon  = gps.date.month() - 1;
    tmv.tm_mday = gps.date.day();
    tmv.tm_hour = gps.time.hour();
    tmv.tm_min  = gps.time.minute();
    tmv.tm_sec  = gps.time.second();
    unsigned rmcFractionMs = gps.time.centisecond() * 10;
    unsigned subsecondMs = rmcFractionMs + gps.time.age();
    if (!fresh || tmv.tm_year < 124 || tmv.tm_mon < 0 || tmv.tm_mon > 11 ||
        tmv.tm_mday < 1 || tmv.tm_mday > 31 || tmv.tm_hour > 23 ||
        tmv.tm_min > 59 || tmv.tm_sec > 59) return;
    time_t t = timegmUtc(&tmv);
    if (rmcUtcMs) *rmcUtcMs = uint64_t(t) * 1000 + rmcFractionMs;
    uint32_t now = millis();
    if (gGpsClockSet && now - gLastGpsSyncMs < GPS_CLOCK_SYNC_MS) return;
    struct timeval tv = { .tv_sec = t + subsecondMs / 1000,
                          .tv_usec = (long)(subsecondMs % 1000) * 1000 };
    if (settimeofday(&tv, nullptr) != 0) return;
    gDiagnosticClock.trusted(diagnostics::EventTimeSource::Gps, now);
    gClockSet.store(true);
    gGpsClockSet = true;
    gLastGpsSyncMs = now;
    Serial.println("System clock set from GPS UTC.");
}

static double haversineM(double la1, double lo1, double la2, double lo2) {
    const double R = 6371000.0;
    double p1 = radians(la1), p2 = radians(la2);
    double dphi = radians(la2 - la1), dlmb = radians(lo2 - lo1);
    double a = sin(dphi / 2) * sin(dphi / 2) +
               cos(p1) * cos(p2) * sin(dlmb / 2) * sin(dlmb / 2);
    return 2 * R * asin(sqrt(a));
}

// =================================================================
// GPX file writing (all callers must already hold the FS mutex, or be
// in setup() before the network task starts)
// =================================================================
// GPX 1.0, not 1.1: 1.0 allows <speed> (m/s) and <hdop> directly on a
// trkpt, where 1.1 needs ~95 bytes of Garmin extension wrapper per point
// for the speed alone. Flash is the binding limit (audit F1), and the
// ingest server reads either version.
static const char* GPX_PROLOGUE =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<gpx version=\"1.0\" creator=\"TBeamMileage/1.1\""
    " xmlns=\"http://www.topografix.com/GPX/1/0\">\n"
    "<trk><name>Trip</name><trkseg>\n";

static const char* GPX_EPILOGUE = "</trkseg></trk></gpx>\n";

// Choose a unique local filename for a new trip using a persistent
// counter. The server renames trips by their GPX timestamps on receipt,
// so the local name only needs to be unique on the device.
static void makeTripPath(char* out, size_t n) {
    fs::FS& storage = *gTripFS;
    uint32_t c = prefs.getUInt("tripno", 0);
    // NVS may be reset independently of LittleFS, or its last counter write
    // may have failed. Never let FILE_WRITE truncate a queued trip. The
    // caller holds the FS mutex while selecting and creating this path.
    do {
        ++c;
        snprintf(out, n, "%s/t%06u.gpx", TRIP_DIR, (unsigned)c);
    } while (storage.exists(out) ||
             storage.exists((String(out) + ".bad").c_str()) ||
             storage.exists((String(out) + ".fix").c_str()));
    prefs.putUInt("tripno", c);
}

// One trkpt per line. Closing each point on its own line means a power
// cut leaves at most a single truncated final line, which the boot-time
// fsck trims cleanly. The line is formatted whole and queued as a single
// write so the caller's commit() can tell whether it landed.
//
// ~118 bytes a point (was 281). Kept: position (6 decimals ~ 11 cm, far
// below GPS error), time, speed (the Doppler speed the server needs to
// tell driving from stationary jitter) and HDOP (the only per-point
// quality signal, for filtering bad fixes). Dropped: elevation, course and
// satellite count, which nothing reads. Element order follows the GPX 1.0
// schema (time, speed, hdop).
static void appendTrkpt(FileWriter& w, const Fix& fx) {
    char timeEl[40] = "";
    if (fx.hasUtc) {
        char iso[25];
        isoFromEpochMs(fx.utcMs, iso, sizeof(iso));
        snprintf(timeEl, sizeof(timeEl), "<time>%s</time>", iso);
    }

    char line[TRKPT_LINE_BYTES]; // ~150 max for sane values; headroom for garbage floats
    int n = snprintf(line, sizeof(line),
        "%s<trkpt lat=\"%.6f\" lon=\"%.6f\">%s"
        "<speed>%.2f</speed><hdop>%.1f</hdop></trkpt>\n",
        fx.newSegment ? "</trkseg><trkseg>\n" : "",
        fx.lat, fx.lon, timeEl, fx.speedMps, fx.hdop);
    if (n <= 0 || n >= (int)sizeof(line)) { w.ok = false; return; }

    w.put(line, n);
}

// =================================================================
// Pending ring buffer (pre-UTC fixes)
// =================================================================
static void pushPending(const Fix& fx) {
    // Callers make room first; an active trip must never overwrite fixes.
    gPend[(gPendHead + gPendCount) % PENDING_CAPACITY] = fx;
    gPendCount++;
}

// Write buffered fixes in order, preserving their original timing status.
// A missing/invalid RMC epoch stays untimed even after GPS or NTP sets the
// system clock: receipt delays cannot reconstruct the position's epoch.
static void flushPending(FileWriter& w) {
    for (size_t i = 0; i < gPendCount; ++i) {
        const Fix& p = gPend[(gPendHead + i) % PENDING_CAPACITY];
        appendTrkpt(w, p);
    }
    gPendCount = gPendHead = 0;
}

// =================================================================
// Trip lifecycle
// =================================================================
// Reserve the pending tail, one more point (including a segment break),
// and the closing tags. A point is at most 255 bytes, including fractional
// UTC. Checking after each fix leaves the complete file below 8 MiB.
static bool tripShouldSplit(size_t bytes, size_t pendingCount) {
    return bytes + (pendingCount + 1) * (TRKPT_LINE_BYTES - 1) +
           strlen(GPX_EPILOGUE) >= TRIP_SPLIT_BYTES;
}

// Only called before workers start or with FS_LOCK held. SD capacity must
// stay 64 bit: size_t on the ESP32 cannot represent a 32 GB card.
static uint64_t storageFreeBytes(fs::FS& storage) {
    if (!tripStoreAvailable(storage)) return 0;
    uint64_t total = &storage == &SD ? SD.totalBytes() : LittleFS.totalBytes();
    // ESP32 Arduino 3.3.x SD.cpp returns 0 when f_getfree fails; cardType()
    // only returns the cached mount-time type. A full readable card still
    // has nonzero total capacity. Never turn a failed query into "full".
    if (&storage == &SD && total == 0) { gSdUnavailable.store(true); return 0; }
    uint64_t used = &storage == &SD ? SD.usedBytes() : LittleFS.usedBytes();
    uint64_t free = used < total ? total - used : 0;
    if (&storage == &LittleFS)
        free = diagnostics::tripFreeWithJournal(free, gFlashJournalFs.allocatedBytes(4096));
    return free;
}

// Takes the FS lock; callers must not hold it.
static uint64_t fsFreeBytes() {
    FS_LOCK();
    uint64_t free = storageFreeBytes(*gTripFS);
    if (gTripFS == &SD && gSdUnavailable.load() && !gTripPath[0] && gFlashMounted) {
        gTripFS = &LittleFS;
        free = storageFreeBytes(LittleFS);
        Serial.println("SD unavailable -- new trips use the internal flash queue until reboot.");
    }
    FS_UNLOCK();
    return free;
}

// True if there's room to start a trip. Logs (at most once a minute while
// driving) when there isn't, since otherwise the miles vanish silently.
static bool roomForNewTrip(uint32_t now) {
    uint64_t freeB = fsFreeBytes();
    storageConditions(freeB, now);
    if (freeB >= FS_START_FREE) return true;
    if (!gFullWarned || now - gFullWarnT0 >= 60000) {
        if (!tripStoreAvailable(*gTripFS))
            Serial.println("ERROR: trip storage unavailable -- NOT recording. Reboot with working storage.");
        else Serial.printf("ERROR: trip storage full (%llu bytes free) -- NOT recording. "
                      "Upload at home to free space.\n", (unsigned long long)freeB);
        gFullWarned = true;
        gFullWarnT0 = now;
    }
    return false;
}

// Periodic check during a trip: true once free space is below the stop
// reserve, at which point the trip is ended while it can still be closed.
static bool tripStorageLow(uint32_t now) {
    if (now - gFsCheckT0 < FS_CHECK_MS) return false;
    gFsCheckT0 = now;
    uint64_t freeB = fsFreeBytes();
    storageConditions(freeB, now);
    if (!tripStoreAvailable(*gTripFS)) return false; // E4006 owns failed trip writes
    if (freeB >= FS_STOP_FREE) return false;
    Serial.printf("ERROR: trip storage nearly full (%llu bytes free) -- ending trip.\n",
                  (unsigned long long)freeB);
    return true;
}

// Returns false -- and leaves no file and no active trip behind -- if the
// trip file can't be created and written. The caller must then stay IDLE:
// carrying on as ACTIVE would let FILE_APPEND create a file with no
// prologue, which uploads as invalid GPX and is rejected forever.
static bool beginTrip(const Fix& fx, bool continuation = false) {
    gTripContinuation = false;
    fs::FS& storage = *gTripFS;
    gTripMeters = 0;
    gTripBytes = 0;
    gTripWriteFailed = false;
    gTripT0 = gArmCount ? gArmBuf[gArmHead].monoMs : fx.monoMs;
    gLastMoveMs = fx.monoMs;

    FS_LOCK();
    makeTripPath(gTripPath, sizeof(gTripPath));
    bool ok = false;
    File f = storage.open(gTripPath, FILE_WRITE);
    const char* operation = f ? "write" : "open";
    if (f) {
        FileWriter w(f);
        w.put(GPX_PROLOGUE);
        // Write the fixes captured during ARMING (includes the current
        // fix, pushed by the FSM before calling us) so the first ~10 s
        // of the drive is in the file. Fixes without RMC UTC go to the
        // pending buffer and retain their untimed status when flushed.
        for (size_t i = 0; i < gArmCount; ++i) {
            const Fix& p = gArmBuf[(gArmHead + i) % 16];
            if (p.hasUtc) {
                // Keep file order chronological: if UTC arrived mid-arm,
                // flush the pre-UTC prefix before the first UTC point.
                if (gPendCount > 0) flushPending(w);
                appendTrkpt(w, p);
            } else {
                pushPending(p);
            }
        }
        ok = w.commit();
        if (ok) gTripBytes = w.queued;
        f.close();
        if (!ok) storage.remove(gTripPath);
    }
    if (!ok) {
        Serial.printf("ERROR: could not create trip file %s -- not recording.\n",
                      gTripPath);
        gTripPath[0] = 0;
    }
    FS_UNLOCK();

    tripWriteResult(storage, ok, operation);
    if (!ok) {
        gPendCount = gPendHead = 0;
        armBufReset();
        return false;
    }

    // Count the distance covered while ARMING into the trip total.
    for (size_t i = 0; i < gArmCount; ++i) {
        const Fix& b = gArmBuf[(gArmHead + i) % 16];
        if (i > 0) {
            const Fix& a = gArmBuf[(gArmHead + i - 1) % 16];
            gTripMeters += haversineM(a.lat, a.lon, b.lat, b.lon);
        }
    }
    armBufReset();
    gCoolT0 = fx.monoMs;
    gFsCheckT0 = fx.monoMs;
    gFullWarned = false;
    gTripContinuation = continuation;
    Serial.printf("Trip started: %s\n", gTripPath);
    return true;
}

// Append a point to the active trip, buffering instead if UTC isn't
// available yet. If the buffer is full, commit its oldest point untimed
// before accepting this one. A timed RMC flushes the buffered untimed tail
// first; points without their own RMC epoch always stay untimed.
// Returns false if the write didn't reach flash; the caller must end the
// trip, since a partial line may now sit at the tail of the file.
static bool writeOrBufferTrkpt(const Fix& fx) {
    fs::FS& storage = *gTripFS;
    if (gTripPath[0] == 0) return true;
    if (!fx.hasUtc && gPendCount < PENDING_CAPACITY && tripStoreAvailable(storage)) {
        pushPending(fx);
        return true;
    }
    FS_LOCK();
    bool ok = false;
    File f = tripStoreAvailable(storage) ? storage.open(gTripPath, FILE_APPEND) : File();
    const char* operation = f ? "write" : "open";
    // FILE_APPEND creates a missing file. A real trip file always holds at
    // least the prologue, so an empty one means ours is gone -- never write
    // points into a file with no <gpx> header.
    if (f && f.size() > 0) {
        FileWriter w(f);
        if (!fx.hasUtc) {
            appendTrkpt(w, gPend[gPendHead]);
        } else {
            if (gPendCount > 0) flushPending(w);
            appendTrkpt(w, fx);
        }
        ok = w.commit();
        if (ok) gTripBytes += w.queued;
    }
    if (f) f.close();   // close() as well as the commit's fsync
    FS_UNLOCK();

    tripWriteResult(storage, ok, operation);
    if (ok && !fx.hasUtc) {
        gPendHead = (gPendHead + 1) % PENDING_CAPACITY;
        gPendCount--;
        pushPending(fx);
    }
    if (!ok) {
        Serial.printf("ERROR: write to %s failed -- ending trip.\n", gTripPath);
        gTripWriteFailed = true;
    }
    return ok;
}

static bool repairIfNeeded(fs::FS& storage, const char* path);

// Close out the active trip. If it's too short/slow it's discarded --
// unless this is a power-loss finalize, where a genuine short drive that
// ended in a power cut is still data worth keeping.
// A size split keeps FINALIZING until its next piece has a valid prologue.
static void endTrip(bool discardIfMicro, TripState nextState = IDLE) {
    fs::FS& storage = *gTripFS;
    if (gTripPath[0] == 0) { gTripContinuation = false; gState = nextState; return; }

    // Duration runs to the last moving fix: the 5 min parked cooldown (or
    // the wait before a fix-loss finalize) that ends every trip would
    // otherwise pass MIN_TRIP_MS on its own. A stop mid-trip still counts.
    uint32_t tripMs = gLastMoveMs - gTripT0;
    bool micro = (tripMs < MIN_TRIP_MS) ||
                 (gTripMeters < MIN_TRIP_METERS);

    FS_LOCK();
    // A successful continuation starts with the shared boundary fix (pending
    // if untimed). Keep even a stopped tail, including a lone boundary point,
    // so per-piece thresholds never discard recorded points from a split trip.
    if (discardIfMicro && micro && !gTripContinuation && tripStoreAvailable(storage)) {
        Serial.printf("Discarding micro-trip (%.0f m, %lu ms)\n",
                      gTripMeters, (unsigned long)tripMs);
        storage.remove(gTripPath);
    } else {
        bool ok = false;
        if (!gTripWriteFailed && tripStoreAvailable(storage)) {
            File f = storage.open(gTripPath, FILE_APPEND);
            // Same guard as writeOrBufferTrkpt: never turn a missing trip
            // file into one holding only the epilogue.
            if (f && f.size() > 0) {
                FileWriter w(f);
                // Flush any still-buffered (never-got-UTC) points before
                // closing, so a trip that never got UTC isn't lost. They're
                // written without times regardless of the system clock.
                if (gPendCount > 0) flushPending(w);
                w.put(GPX_EPILOGUE);
                ok = w.commit();
            }
            if (f) f.close();
            tripWriteResult(storage, ok, "close");
        }
        // A failed write may have left a partial line at the tail. Don't
        // append after it: trim back to the last complete point and close
        // the document there. If even that fails (e.g. no room for the
        // repair copy) the file stays incomplete -- never uploaded -- and
        // the boot-time fsck retries it.
        if (!ok && tripStoreAvailable(storage)) {
            ok = repairIfNeeded(storage, gTripPath);
            tripWriteResult(storage, ok, "close");
        }
        if (!tripStoreAvailable(storage)) tripWriteResult(storage, false, "close");

        if (ok) {
            Serial.printf("Trip finalized: %s (%.2f mi)\n",
                          gTripPath, gTripMeters / 1609.344);
        } else {
            Serial.printf("ERROR: could not finalize %s -- left for boot-time repair.\n",
                          gTripPath);
        }
    }
    gTripPath[0] = 0;
    FS_UNLOCK();

    gPendCount = gPendHead = 0;
    gTripWriteFailed = false;
    gTripContinuation = false;
    gState = nextState;
    if (nextState == IDLE) gDisplayTripFinalized = true;
    gNetRetryNow.store(true);   // after the file is closed and no longer active
}

// The five-state machine, driven once per fix.
static void tripFsmOnFix(const Fix& fx) {
    const uint32_t now = fx.monoMs;
    const float speed = fx.speedTrusted ? fx.speedMps : 0.0f;

    // Only ACTIVE movement adds distance. COOLDOWN noise must neither add
    // meters nor extend the last moving time used by endTrip(). Resuming
    // establishes a new distance baseline at the confirming fix, erring low.
    if (gState == ACTIVE && speed > END_MPS) {
        if (gHaveLastFix && gDistanceContinuous)
            gTripMeters += haversineM(gLastFix.lat, gLastFix.lon, fx.lat, fx.lon);
        gLastMoveMs = now;
    }

    switch (gState) {
    case IDLE:
        if (speed >= START_MPS) {
            gState = ARMING; gArmT0 = now;
            armBufReset(); armBufPush(fx);
        }
        break;
    case ARMING:
        if (speed < START_MPS) {
            gState = IDLE;
            armBufReset();
        } else {
            armBufPush(fx);
            if (now - gArmT0 >= ARM_MS) {
                // Writes the buffered ARMING fixes too. If there's no room,
                // or the file can't be created, stay IDLE; still moving, we
                // re-arm and retry.
                if (roomForNewTrip(now) && beginTrip(fx)) {
                    gState = ACTIVE;
                } else {
                    gState = IDLE;
                    armBufReset();
                    gPendCount = gPendHead = 0;
                }
            }
        }
        break;
    case ACTIVE:
        if ((fx.speedTrusted && !writeOrBufferTrkpt(fx)) || tripStorageLow(now)) { gState = FINALIZING; break; }
        if (speed <= END_MPS) { gState = COOLDOWN; gCoolT0 = now; gResumeFixes = 0; }
        break;
    case COOLDOWN:
        if ((fx.speedTrusted && !writeOrBufferTrkpt(fx)) || tripStorageLow(now)) { gState = FINALIZING; break; }
        gResumeFixes = speed > END_MPS ? gResumeFixes + 1 : 0;
        // Keep the original deadline while collecting evidence of movement.
        if (now - gCoolT0 >= COOL_MS) {
            gState = FINALIZING;
        } else if (gResumeFixes >= RESUME_FIXES) {
            gState = ACTIVE;
            gLastMoveMs = now;
        }
        break;
    case FINALIZING:
        break;   // handled immediately below, not on the NEXT fix
    }

    TripState resumeState = gState;
    bool split = fx.speedTrusted && (gState == ACTIVE || gState == COOLDOWN) &&
                 tripShouldSplit(gTripBytes, gPendCount);
    if (split) {
        Serial.printf("Trip split at %u bytes: %s\n", (unsigned)gTripBytes, gTripPath);
        gState = FINALIZING;
    }

    // Finalize in the same pass that triggered it. Previously this only
    // ran on the next valid fix, so losing GPS right as the cooldown
    // expired (parked in a garage) left the trip open until power loss.
    if (gState == FINALIZING) {
        uint32_t coolT0 = gCoolT0;
        endTrip(/*discardIfMicro=*/!split, split ? FINALIZING : IDLE);
        if (split) {
            // Share the boundary fix so the join loses neither a point nor
            // a distance step. Reuse startup without the 10 s arming wait.
            armBufReset();
            armBufPush(fx);
            if (roomForNewTrip(now) && beginTrip(fx, /*continuation=*/true)) {
                gState = resumeState;
                if (resumeState == COOLDOWN) gCoolT0 = coolT0;
            } else {
                gState = IDLE;
                armBufReset();
                gPendCount = gPendHead = 0;
            }
        }
    }

    if (fx.speedTrusted) {
        gLastFix = fx;
        gHaveLastFix = true;
    }
    gDistanceContinuous = fx.speedTrusted && gState == ACTIVE;
    gLastFixMonoMs = now;
}

// =================================================================
// Boot-time fsck: repair any trip file left unclosed by a power cut,
// then leave it in place for the network task to upload.
// =================================================================
static long findLastTrkptEnd(File& f, uint32_t* points = nullptr) {
    if (points) *points = 0;
    const char* tok = "</trkpt>";
    const int   tl  = 8;
    uint8_t buf[256];
    char    region[272];
    int     carryLen = 0;
    long    pos = 0, last = -1;
    if (!f.seek(0)) return -2;
    const size_t size = f.size();
    while ((size_t)pos < size) {
        size_t want = min(sizeof(buf), size - (size_t)pos);
        int n = f.read(buf, want);
        // Distinguish unreadable bytes from a file with no complete points.
        // Publishing a repair after a partial scan could discard later points.
        if (n != (int)want) return -2;
        // region currently holds carryLen carry bytes at its start; append
        // the freshly read bytes after them so a token spanning the read
        // boundary is still found.
        memmove(region + carryLen, buf, n);
        int rl = carryLen + n;
        for (int i = 0; i + tl <= rl; ++i) {
            if (memcmp(region + i, tok, tl) == 0) {
                last = pos - carryLen + i + tl;
                if (points && *points < INT32_MAX) ++*points;
            }
        }
        carryLen = min(tl - 1, rl);
        memmove(region, region + rl - carryLen, carryLen);
        pos += n;
        feedLoopWatchdogProgress(); // another verified scan block
    }
    return last;
}

// Compare only verified bytes; a short read must never authorize cleanup.
static bool sameFilePrefix(File& a, File& b, size_t bytes, bool* readFailed = nullptr) {
    if (!a.seek(0) || !b.seek(0)) { if (readFailed) *readFailed = true; return false; }
    uint8_t ab[128], bb[128];
    while (bytes) {
        size_t n = min(bytes, sizeof(ab));
        if (a.read(ab, n) != (int)n || b.read(bb, n) != (int)n) {
            if (readFailed) *readFailed = true;
            return false;
        }
        if (memcmp(ab, bb, n) != 0) return false;
        bytes -= n;
        feedLoopWatchdogProgress(); // another verified matching block
    }
    return true;
}

static bool fileMatchesText(File& f, const char* text, size_t bytes, bool* readFailed = nullptr) {
    uint8_t buf[128];
    while (bytes) {
        size_t n = min(bytes, sizeof(buf));
        if (f.read(buf, n) != (int)n) { if (readFailed) *readFailed = true; return false; }
        if (memcmp(buf, text, n) != 0) return false;
        text += n;
        bytes -= n;
        feedLoopWatchdogProgress();
    }
    return true;
}

static bool publishRepair(fs::FS& storage, const char* tmp, const char* path) {
    // FatFs rename refuses an existing destination. Only remove the original
    // AFTER a complete, verified, closed repair exists. If power fails in
    // between, fsckAllTrips discovers the orphan .gpx.fix and recovers it.
    // FAT metadata itself is not power-loss atomic; the battery/clean
    // shutdown is still essential. LittleFS retains its atomic replacement.
    if (&storage == &SD && storage.exists(path) && !storage.remove(path)) return false;
    return storage.rename(tmp, path);
}

// Deal with previous repair attempts before FILE_WRITE can truncate them.
// Called under the same FS lock as repairIfNeeded. Ambiguous copies are
// retained for inspection; only proven redundant bytes may be discarded.
static bool recoverRepairCopy(fs::FS& storage, const char* path, TripRecovery& result) {
    String tmp = String(path) + ".fix";
    if (!storage.exists(tmp.c_str())) return true;
    if (storage.exists((String(path) + ".bad").c_str())) {
        Serial.printf("fsck: retained %s (quarantine collision)\n", tmp.c_str());
        result.reason = "parse";
        return false;
    }
    File copy = storage.open(tmp.c_str(), FILE_READ);
    if (!copy) return false;
    if (!storage.exists(path)) {
        // A historical interrupted replacement may have left this as the
        // only copy. Rename without copying, then use normal repair below.
        // An incomplete/unknown header or no complete point is not safe to
        // feed to the normal garbage-removal path, so preserve those bytes.
        bool readFailed = false;
        bool header = copy.size() >= strlen(GPX_PROLOGUE) &&
                      fileMatchesText(copy, GPX_PROLOGUE, strlen(GPX_PROLOGUE), &readFailed);
        long end = header ? findLastTrkptEnd(copy, &result.points) : -1;
        bool recoverable = header && end > (long)strlen(GPX_PROLOGUE);
        copy.close();
        if (recoverable && storage.rename(tmp.c_str(), path)) {
            Serial.printf("fsck: recovered orphan %s\n", path);
            result.repaired = result.pointsKnown = true;
            return true;
        }
        Serial.printf("fsck: retained orphan %s\n", tmp.c_str());
        result.reason = recoverable ? "rename" : readFailed || end == -2 ? "read" : "parse";
        return false;
    }

    File original = storage.open(path, FILE_READ);
    if (!original) { copy.close(); return false; }
    const size_t copySize = copy.size();
    bool readFailed = false;
    bool redundant = copySize <= original.size() && sameFilePrefix(original, copy, copySize, &readFailed);
    bool finishedRepair = false;
    if (!redundant) {
        long end = findLastTrkptEnd(original, &result.points);
        if (end == -2) readFailed = true;
        else result.pointsKnown = true;
        size_t suffixSize = 1 + strlen(GPX_EPILOGUE);
        if (end > 0 && copySize > (size_t)end && copySize <= (size_t)end + suffixSize &&
            sameFilePrefix(original, copy, end, &readFailed) && copy.read() == '\n' &&
            fileMatchesText(copy, GPX_EPILOGUE, copySize - end - 1, &readFailed)) {
            redundant = true;
            finishedRepair = copySize == (size_t)end + suffixSize;
        }
    }
    original.close();
    copy.close();
    if (!redundant) {
        Serial.printf("fsck: retained %s (differs from original)\n", tmp.c_str());
        result.reason = readFailed ? "read" : "parse";
        return false;
    }
    // A complete verified repair can be published even with no free space.
    // Failed publication retains at least the verified copy for a later boot.
    result.reason = finishedRepair ? "rename" : "write";
    bool ok = finishedRepair ? publishRepair(storage, tmp.c_str(), path) : storage.remove(tmp.c_str());
    if (ok && finishedRepair) result.repaired = true;
    return ok;
}

// Returns true if the file is complete afterwards (or was removed as empty),
// false if it had to be left as it was.
static bool repairTrip(fs::FS& storage, const char* path, TripRecovery& result) {
    if (!recoverRepairCopy(storage, path, result)) return false;
    result.reason = "read";
    File f = storage.open(path, FILE_READ);
    if (!f) return false;
    size_t sz = f.size();

    // Already complete? Look for </gpx> in the tail.
    bool complete = false;
    if (sz >= 6) {
        size_t back = sz > 16 ? 16 : sz;
        f.seek(sz - back);
        char tail[17];
        int n = f.read((uint8_t*)tail, back);
        if (n != (int)back) { f.close(); return false; }
        tail[n] = 0;
        if (strstr(tail, "</gpx>")) complete = true;
    }
    if (complete) { f.close(); return true; }

    long end = findLastTrkptEnd(f, &result.points);
    if (end == -2) { f.close(); return false; }
    if (end <= 0) {
        f.close();
        result.reason = "write";
        if (!storage.remove(path)) return false;
        Serial.printf("fsck: removed empty/garbage %s\n", path);
        return true;
    }

    String tmp = String(path) + ".fix";
    File out = storage.open(tmp.c_str(), FILE_WRITE);
    if (!out) { f.close(); result.reason = "write"; return false; }
    FileWriter w(out);
    f.seek(0);
    long remaining = end;
    uint8_t b[256];
    while (remaining > 0) {
        int want = remaining > 256 ? 256 : (int)remaining;
        int n = f.read(b, want);
        if (n <= 0) break;
        w.put((const char*)b, n);
        if (!w.ok) break;
        remaining -= n;
        feedLoopWatchdogProgress(); // copied bytes, never a stalled read/write
    }
    w.put("\n");
    w.put(GPX_EPILOGUE);
    // A short read or a write that didn't land means the copy is not a
    // faithful repair -- keep the original rather than replace it.
    bool ok = w.commit() && remaining == 0;
    out.close();
    f.close();
    if (!ok) {
        // The copy loop stops at the first failed write, with bytes still
        // unread, so check the writer before blaming the read.
        result.reason = (!w.ok || !remaining) ? "write" : "read";
        storage.remove(tmp.c_str());
        Serial.printf("fsck: could not repair %s (write failed)\n", path);
        return false;
    }
    // Publish only after both handles are closed and the repair is verified.
    // A surviving .fix is discovered on the next boot, including on FAT.
    if (!publishRepair(storage, tmp.c_str(), path)) {
        result.reason = "rename";
        Serial.printf("fsck: could not replace %s (rename failed)\n", path);
        return false;
    }
    Serial.printf("fsck: repaired %s\n", path);
    result.repaired = result.pointsKnown = true;
    return true;
}

static bool repairIfNeeded(fs::FS& storage, const char* path) {
    TripRecovery result;
    bool ok = repairTrip(storage, path, result);
    if (gJournalReady && (!ok || result.repaired)) {
        gLoopFaults.event(ok ? 4010 : 4007, millis()).enumeration("backend", tripBackend(storage));
        if (!ok) gLoopFaults.enumeration("reason", result.reason);
        else if (result.pointsKnown) gLoopFaults.integer("points", result.points);
        gLoopFaults.notice();
    }
    return ok;
}

static String tripEntryPath(const File& entry) {
    // ESP32 Arduino core 3.x File::name() is a basename; core 2.x returned
    // the full path. Keep that difference here, shared by every trip walk.
    const char* name = entry.name();
    return name[0] == '/' ? String(name) : String(TRIP_DIR) + "/" + name;
}

static void fsckAllTrips(fs::FS& storage) {
    FS_LOCK();
    std::vector<String> paths;
    File dir = storage.open(TRIP_DIR);
    if (!dir || !dir.isDirectory()) {
        if (gJournalReady) gLoopFaults.event(4007, millis()).enumeration("backend", tripBackend(storage))
            .enumeration("reason", "read").notice();
        if (dir) dir.close();
        FS_UNLOCK();
        return;
    }
    if (dir && dir.isDirectory()) {
        File e = dir.openNextFile();
        while (e) {
            String name = tripEntryPath(e);
            if (!e.isDirectory() && (name.endsWith(".gpx") || name.endsWith(".gpx.fix"))) {
                if (name.endsWith(".fix")) name = name.substring(0, name.length() - 4);
                bool seen = false;
                for (const String& path : paths) if (path == name) { seen = true; break; }
                if (!seen) paths.push_back(name);
            }
            e.close();
            feedLoopWatchdogProgress(); // another directory entry completed
            e = dir.openNextFile();
        }
    }
    // LittleFS directory cursors can change when repair creates, removes or
    // renames entries. Finish enumeration and close every handle first.
    dir.close();
    for (const String& path : paths) {
        repairIfNeeded(storage, path.c_str());
        feedLoopWatchdogProgress(); // another file checked, even if no repair
    }
    FS_UNLOCK();
}

// =================================================================
// AXP2101 power management / IRQ
// =================================================================
void IRAM_ATTR onPmuIrq() { gPmuIrqFlag = true; }

// One mask for both IRQ configuration and checked-batch interpretation.
static constexpr uint32_t PMU_ENABLED_IRQS =
    XPOWERS_AXP2101_VBUS_REMOVE_IRQ | XPOWERS_AXP2101_VBUS_INSERT_IRQ |
    XPOWERS_AXP2101_PKEY_SHORT_IRQ |
    XPOWERS_AXP2101_PKEY_LONG_IRQ | XPOWERS_AXP2101_WARNING_LEVEL1_IRQ |
    XPOWERS_AXP2101_WARNING_LEVEL2_IRQ;
static constexpr uint32_t PMU_IRQ_RETRY_MS = 100;
static bool gPmuIrqRetry = false;
static uint32_t gPmuIrqReadMs = 0;

static void setupPmuIrq() {
    pinMode(PMU_IRQ_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(PMU_IRQ_PIN), onPmuIrq, FALLING);
    PMU.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
    PMU.clearIrqStatus();
    PMU.enableIRQ(PMU_ENABLED_IRQS);
}

// Stop the network task before storage is unmounted (issue #3). It starts
// no new work once gNetHold is set; an upload still running is cut off and
// given NET_ABORT_MS to close its file and switch WiFi off. The trip stays
// queued: only an HTTP 200 deletes it.
static void stopNetworkTask() {
    gNetHold.store(true);
    if (!gNetBusy.load()) return;
    gNetAbort.store(true);
    Serial.println("Stopping the upload in progress -- the trip stays queued.");
    uint32_t t0 = millis();
    while (gNetBusy.load() && millis() - t0 < NET_ABORT_MS) vTaskDelay(pdMS_TO_TICKS(20));
    if (gNetBusy.load()) Serial.println("Network task still busy -- powering off with storage locked.");
}

// USB power must stay off this long before it counts as "car off". Engine
// cranking and start-stop restarts brown the USB port out for a second or
// two; acting on the first dip split one drive into several trips.
static constexpr uint32_t VBUS_OFF_CONFIRM_MS = 10UL * 1000;
static bool     gVbusOffPending = false;
static uint32_t gVbusOffT0 = 0;

using pmu_read::Aldo3State;
using pmu_read::VbusState;

static pmu_read::Aldo3State aldo3State() {
    return pmu_read::aldo3State(PMU.readRegister(XPOWERS_AXP2101_LDO_ONOFF_CTRL0));
}

static pmu_read::VbusState vbusState() {
    // Read both even when STATUS2 alone would short-circuit isVbusIn().
    // Either failed read makes the whole observation unknown.
    int status2 = PMU.readRegister(XPOWERS_AXP2101_STATUS2);
    int status1 = PMU.readRegister(XPOWERS_AXP2101_STATUS1);
    return pmu_read::vbusState(status1, status2);
}

static constexpr uint32_t VBUS_RETRY_MS = 1000;
enum class VbusRetry { None, Seed, Change };
static VbusRetry gVbusRetry = VbusRetry::None;
static uint32_t gVbusRetryMs = 0;
static uint32_t gVbusOffReadMs = 0;

static void seedVbusState() {
    VbusState state = vbusState();
    gVbusLost = state == VbusState::Absent;
    gVbusRetry = state == VbusState::Unknown ? VbusRetry::Seed : VbusRetry::None;
    gVbusRetryMs = millis();
}

static void observeVbusChange(pmu_read::VbusState state) {
    gVbusRetry = state == VbusState::Unknown ? VbusRetry::Change : VbusRetry::None;
    gVbusRetryMs = millis();
    if (state == VbusState::Present) {
        if (gVbusOffPending) Serial.println("VBUS dipped and came back -- ignored.");
        else if (gVbusLost) Serial.println("VBUS inserted -- back on USB power.");
        gVbusOffPending = false;
        gVbusLost = false;
    } else if (state == VbusState::Absent && !gVbusOffPending && !gVbusLost) {
        Serial.println("VBUS removed -- confirming before ending the trip.");
        gVbusOffPending = true;
        gVbusOffT0 = gVbusOffReadMs = millis();
    }
}

static void serviceVbusRecheck() {
    if (gVbusRetry == VbusRetry::None || uint32_t(millis() - gVbusRetryMs) < VBUS_RETRY_MS) return;
    if (gVbusRetry == VbusRetry::Seed) seedVbusState();
    else observeVbusChange(vbusState());
}

static const char* const VBUS_UNREADABLE =
    "VBUS state unreadable -- staying awake instead of powering off.";

static bool mountSdStorage();

static void powerOffForVbus(const char* reason = "idle_battery") {
    // AXP2101 powers on on a VBUS INSERT EDGE, not its steady level. Never
    // shut down on USB, even for a power-key or low-battery interrupt: the
    // board would stay off until USB was unplugged and replugged (#106).
    VbusState entryVbus = vbusState();
    if (entryVbus != VbusState::Absent) {
        if (entryVbus == VbusState::Present && strcmp(reason, "user_long_press") == 0)
            gDisplayUsbPowerRequest = true;
        Serial.println(entryVbus == VbusState::Unknown ? VBUS_UNREADABLE :
                       "VBUS present -- staying awake instead of powering off.");
        gVbusRetry = entryVbus == VbusState::Unknown ? VbusRetry::Seed : VbusRetry::None;
        gVbusRetryMs = millis();
        gVbusLost = false;
        gVbusOffPending = false;
        gIdleSince = millis();
        gNetHold.store(false);
        return;
    }

    Serial.println("Preparing PMU power-off (battery critical / idle).");
    if (gTripPath[0]) endTrip(/*discardIfMicro=*/false);
    stopNetworkTask();
    feedLoopWatchdogProgress(); // bounded abort handoff completed, <=15 s
    // The core-0 consumer closes each append itself; ask it to drain and seal
    // before unmount. A stuck network task cannot delay power safety forever.
    if (gJournalReady) {
        gPowerOffEvent = diagnostics::EventInput();
        gPowerOffEvent.code = static_cast<uint16_t>(diagnostics::Code::BATTERY_POWER_OFF);
        gPowerOffEvent.uptimeMs = millis();
        gJournal.stamp(gPowerOffEvent);
        diagnostics::addBatteryContext(gPowerOffEvent, diagnostics::readBatterySample(PMU));
        gPowerOffEvent.context[gPowerOffEvent.contextCount++] =
            diagnostics::EventContext::enumeration("reason", reason);
        gPowerOffEventPending.store(true);
        gJournalSealDone.store(false);
        gJournalSealRequested.store(true);
        uint32_t journalWait = millis();
        while (!gJournalSealDone.load() && millis() - journalWait < NET_ABORT_MS) {
            // Degraded startup has no core-0 task. It is the sole consumer.
            if (gDegraded.mode() == startup::Mode::NoStorage) serviceJournal();
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        if (!gJournalSealDone.load() || !gJournalSealOk.load())
            Serial.println("Journal: shutdown drain/seal incomplete; RAM records may be lost.");
    }
    feedLoopWatchdogProgress(); // bounded journal handoff completed, <=15 s
    // pollPmu() and both idle paths run on loop/core 1, the OLED owner.
    // Draw synchronously: no wait for another task or display acknowledgment.
    // The adapter latches the first bus failure (10 ms Wire timeout); a failed
    // draw skips the hold. Only a user request gets this bounded 1 s pause.
    if (strcmp(reason, "user_long_press") == 0 &&
        gDegraded.mode() == startup::Mode::Normal &&
        !gDebugDisplayFailed && !gProductionDisplayFailed &&
        gProductionDisplayReady && vbusState() == VbusState::Absent) {
        if (gDisplayOwner == DisplayOwner::Debug) debugDisplayPowerSave(true);
        if (!gDebugDisplayFailed) {
            gDisplayOwner = DisplayOwner::Production;
            gProductionHaveOutput = false; // redraw and wake even from debug
            display::DisplayModel::Input switchingOff; switchingOff.switchingOff = true;
            drawProductionDisplay(gDisplayModel.update(switchingOff));
            if (!gProductionDisplayFailed) vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }
    // Keep the lock through power-off, so even a network task
    // that never stopped can't reach the filesystem once it's unmounted.
    FS_LOCK();
    if (gSdMounted) SD.end();
    if (gFlashMounted) LittleFS.end();
    feedLoopWatchdogProgress(); // unmount returned; no feed while acquiring lock
    // Only the current owner sleeps the shared panel, before the final VBUS
    // read. A latched failure must not retry via the other adapter.
    display::DisplayModel::Input displayOff; displayOff.shutdown = true;
    gDisplayModel.update(displayOff);
    if (!gDebugDisplayFailed && !gProductionDisplayFailed) {
        if (gDisplayOwner == DisplayOwner::Debug) debugDisplayPowerSave(true);
        else {
            drawProductionDisplay(gDisplayModel.update(displayOff));
            productionDisplayPowerSave(true);
        }
    }
    gProductionHaveOutput = false;
    // PMU shutdown cuts all main rails, including SD BLDO1 and GPS ALDO4.
    // Leave rails and awake IRQs alone so a cancelled shutdown can resume.
    // GPS backup RAM is supplied directly by the 18650, not VBACKUP.
    Serial.println("PMU power-off: storage closed; checking VBUS before shutdown.");
    VbusState finalVbus = vbusState();
    if (finalVbus == VbusState::Absent) {
        // Last possible level check: no log, delay or other work before
        // shutdown. A VBUS edge between this read and the PMU's I2C
        // shutdown write remains a race; software cannot make them atomic.
        PMU.shutdown();
        // XPowersLib only writes the shutdown bit. Allow the rails to fall
        // before treating a returning call as failure; don't spin or leave
        // the logger alive with storage unmounted if power stays on.
        vTaskDelay(pdMS_TO_TICKS(1000));
        feedLoopWatchdogProgress(); // shutdown returned after the bounded hold
        Serial.println(gDegraded.mode() == startup::Mode::Normal ?
            "ERROR: PMU.shutdown() did not power off -- resuming logging." :
            "ERROR: PMU.shutdown() did not power off -- resuming degraded service.");
    } else {
        Serial.println(finalVbus == VbusState::Unknown ? VBUS_UNREADABLE :
                       "VBUS returned -- PMU power-off cancelled.");
        if (finalVbus == VbusState::Present && strcmp(reason, "user_long_press") == 0)
            gDisplayUsbPowerRequest = true;
    }

    // An upload that outlived the abort timeout may still own a File from
    // before unmount. Never release it against remounted storage: keep the
    // mutex/hold/abort in place and restart into an ordinary boot instead.
    if (gNetBusy.load()) {
        Serial.println("ERROR: upload task still busy after power-off recovery; restarting.");
        ESP.restart();
        return;
    }
    // Resume without formatting or changing the trip counter. The network
    // stays held until storage is mounted again.
    bool remounted = true;
    if (gSdMounted) remounted = mountSdStorage();
    if (gFlashMounted) remounted = LittleFS.begin(false, "/littlefs", 10, TRIP_PARTITION_LABEL) && remounted;
    if (!remounted) {
        Serial.println("ERROR: storage remount after cancelled/failed power-off failed; restarting.");
        ESP.restart();
        return;
    }
    feedLoopWatchdogProgress(); // completed remount, before journal recovery
    gJournal.invalidateStorage(); // same boot/queue, reload metadata after remount
    gJournalStarted = false; // recheck FAT allocation geometry on core 0
    gPowerOffEventPending.store(false); // consumer excluded by FS_LOCK
    gJournalSealRequested.store(false);
    gJournalSealDone.store(false);
    FS_UNLOCK();
    seedVbusState();   // also catches a missed INSERT interrupt; unknown stays awake
    // The owning display service redraws before waking after recovery.
    gProductionHaveOutput = false;
    gDebugDrawMs = millis() - 1000;
    gDebugNeedsWake = true;
    gVbusOffPending = false;
    gIdleSince = millis();        // failed power-off retries after the idle grace
    gNetAbort.store(false);
    gNetHold.store(false);
    Serial.println(gDegraded.mode() == startup::Mode::Normal ?
        "Power-off recovery complete; storage mounted, logging continues." :
        "Power-off recovery complete; degraded service continues, not recording trips.");
}

static void pollPmu() {
    gBootButton.sample(millis(), digitalRead(DEBUG_BOOT_PIN) == LOW);
    if (!gPmuIrqFlag && !gPmuIrqRetry) return;
    uint32_t now = millis();
    if (gPmuIrqRetry && uint32_t(now - gPmuIrqReadMs) < PMU_IRQ_RETRY_MS) return;
    // Consume only the pending edge before reading; an ISR during the reads
    // can set it again. Failed batches retain a separate pending retry even
    // though an asserted IRQ line cannot produce another falling edge.
    gPmuIrqFlag = false;
    gPmuIrqReadMs = now;
    int first = PMU.readRegister(XPOWERS_AXP2101_INTSTS1);
    int second = PMU.readRegister(XPOWERS_AXP2101_INTSTS2);
    int third = PMU.readRegister(XPOWERS_AXP2101_INTSTS3);
    const auto irq = pmu_read::irqBatch(first, second, third, PMU_ENABLED_IRQS);
    gPmuIrqRetry = !irq.known;
    if (!irq.known) return; // no actions or clears: real flags stay latched

    // Treat either VBUS interrupt as "power changed" and read the actual
    // level: a short dip can latch REMOVE and INSERT in the same batch,
    // and the flags don't say which came last.
    if (irq.has(XPOWERS_AXP2101_VBUS_REMOVE_IRQ) || irq.has(XPOWERS_AXP2101_VBUS_INSERT_IRQ)) {
        observeVbusChange(vbusState());
    }
    if (gDegraded.mode() == startup::Mode::Normal && irq.has(XPOWERS_AXP2101_PKEY_SHORT_IRQ)) {
        // Read again at batch handling: a release must not use a cached low.
        gBootButton.sample(millis(), digitalRead(DEBUG_BOOT_PIN) == LOW);
        if (gBootButton.held(millis())) toggleRuntimeDebug();
        else gDisplayUserRequest = true;
    }
    if (irq.has(XPOWERS_AXP2101_PKEY_LONG_IRQ)) powerOffForVbus("user_long_press");
    // Observe hardware's existing low/critical policy before shutdown. Never
    // submit from the ISR (it only latches gPmuIrqFlag).
    if (gJournalReady) {
        if (irq.has(XPOWERS_AXP2101_WARNING_LEVEL1_IRQ))
            batteryCondition(2002, true, millis());
        if (irq.has(XPOWERS_AXP2101_WARNING_LEVEL2_IRQ))
            batteryCondition(2003, true, millis());
    }
    if (irq.has(XPOWERS_AXP2101_WARNING_LEVEL2_IRQ)) powerOffForVbus("battery_critical");

    PMU.clearIrqStatus();   // write-1-to-clear, or the line never re-fires
}

// Stage 4 never writes LOW_BAT_WARN_SET. Cache only a successful read.
static int gBatteryThresholds = -1;
static void serviceBatteryDiagnostics() {
    uint32_t now = millis();
    if (!gJournalReady || (gBatteryDiagChecked && uint32_t(now - gBatteryDiagMs) < 30000)) return;
    gBatteryDiagChecked = true; gBatteryDiagMs = now;
    if (!PMU.isBatteryConnect()) return; // unavailable measurement is not recovery
    int percent = PMU.getBatteryPercent();
    if (percent < 0 || percent > 100) return;
    // The existing shutdown is AXP2101's SOC level-2 IRQ, not a firmware
    // voltage cutoff. XPowersLib 0.3.3 returns the raw level-1 nibble: +5%.
    if (gBatteryThresholds < 0)
        gBatteryThresholds = PMU.readRegister(XPOWERS_AXP2101_LOW_BAT_WARN_SET);
    if (gBatteryThresholds < 0) return; // unmeasured: neither raise nor clear
    unsigned low = ((gBatteryThresholds >> 4) & 0x0F) + 5;
    unsigned critical = gBatteryThresholds & 0x0F;
    batteryCondition(2002, unsigned(percent) <= low, now);
    batteryCondition(2003, unsigned(percent) <= critical, now);
}

// -----------------------------------------------------------------
// Battery charge temperature guard (audit N4)
// -----------------------------------------------------------------
// Li-ion must only be charged at roughly 0-45 C cell temperature, and a
// parked car's cabin passes 60 C in summer. TS is excluded from charging
// (the Supreme's Rt is unidentified), so the AXP2101's own die temperature
// stands in. Measured on the retired V1.2 2026-10-05: the die idles
// ~9 C above a 25.6 C room with nothing charging, and charge current heats
// it a few degrees more within seconds. Hence the offset, and a 7 C gap
// between pausing and resuming so the charger's own heat can't make it
// toggle. Pausing charging never affects logging: on USB the PMU powers
// the board directly; the battery just isn't topped up until it cools.
// Supreme debug-only Temp: logs collect recalibration evidence; no tuning yet.
static constexpr float    DIE_OFFSET_C      = 9.0f;
static constexpr float    CHG_HOT_PAUSE_C   = 45.0f + DIE_OFFSET_C;   // battery ~45 C
static constexpr float    CHG_HOT_RESUME_C  = 38.0f + DIE_OFFSET_C;   // battery ~38 C
static constexpr float    CHG_COLD_PAUSE_C  =  0.0f + DIE_OFFSET_C;   // battery ~0 C
static constexpr float    CHG_COLD_RESUME_C =  5.0f + DIE_OFFSET_C;   // battery ~5 C
static constexpr uint32_t CHG_CHECK_MS      = 30UL * 1000;
static constexpr uint32_t CHG_FIRST_RETRY_MS = 1000;
static constexpr uint32_t CHG_FIRST_GRACE_MS = 10000;
// The first temperature check must command/verify explicitly: the charger
// keeps its state across ESP32 resets. Only matching read-back confirms it.
static pmu_read::ChargeState gChargeState = pmu_read::ChargeState::Unconfirmed;
static bool     gChargeChecked = false;
static bool     gChargeFirstResolved = false; // plausible read OR grace exhausted; never reopens on rollover
static uint32_t gChargeFirstMs = 0;
static bool     gChargePauseRequested = false; // hysteresis follows intent, even on failure
static uint8_t  gChargeFailures = 0;
static constexpr uint8_t CHG_FAILURE_LIMIT = 3;
static uint32_t gChargeCheckT0 = 0;

static void serviceChargeTemp() {
    uint32_t now = millis();
    if (gChargeChecked && uint32_t(now - gChargeCheckT0) <
        (gChargeFirstResolved ? CHG_CHECK_MS : CHG_FIRST_RETRY_MS)) return;
    gChargeCheckT0 = now;
    if (!gChargeChecked) gChargeFirstMs = now;
    gChargeChecked = true;
    bool first = !gChargeFirstResolved; // no decision made yet this boot

    float die = PMU.getTemperature();
    // XPowersLib AXP2101: 22 + (7274 - raw) / 20. Raw zero is 385.7 C,
    // consistent with the cold-start ADC not having completed a conversion.
    // Treat implausible values exactly like a failed read, including debug.
    if (!charge_temperature::plausible(die)) die = NAN;
    if (std::isfinite(die) || uint32_t(now - gChargeFirstMs) >= CHG_FIRST_GRACE_MS)
        gChargeFirstResolved = true;
    bool firstGrace = !gChargeFirstResolved;
    // Reuse this exact sample, including NAN on failure. No second PMU read.
    gDebugTempDie = die;
    gDebugTempDieMs = now;
    bool pause;
    const char* why = "";
    if (!std::isfinite(die)) {
        pause = true;                        // can't tell: fail safe
        why = "temperature unreadable";
    } else if (gChargePauseRequested) {
        pause = !(die <= CHG_HOT_RESUME_C && die >= CHG_COLD_RESUME_C);
        why = die > CHG_HOT_RESUME_C ? "too hot" : "too cold";
    } else {
        pause = die >= CHG_HOT_PAUSE_C || die <= CHG_COLD_PAUSE_C;
        why = die >= CHG_HOT_PAUSE_C ? "too hot" : "too cold";
    }
    if (!firstGrace) gChargePauseRequested = pause;

    const auto intended = pause ? pmu_read::ChargeState::Paused : pmu_read::ChargeState::Charging;
    if (gChargeState != intended) {
        if (pause) PMU.disableCellbatteryCharge();
        else PMU.enableCellbatteryCharge();
        // The void XPowersLib commands discard their write result. A readable
        // matching bit is the only evidence that the intended state took effect.
        auto actual = pmu_read::chargeState(PMU.readRegister(XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL));
        gChargeState = actual == intended ? intended : pmu_read::ChargeState::Unconfirmed;
        if (gChargeState == pmu_read::ChargeState::Unconfirmed) {
            if (gChargeFailures < CHG_FAILURE_LIMIT) ++gChargeFailures;
            Serial.printf("Charge control unconfirmed; retrying in %lu s.\n",
                (unsigned long)((firstGrace ? CHG_FIRST_RETRY_MS : CHG_CHECK_MS) / 1000));
        } else {
            gChargeFailures = 0;
            if (pause && !firstGrace) {
                Serial.printf("Charging paused: %s (die %.1f C, battery ~%.0f C)\n",
                              why, die, die - DIE_OFFSET_C);
            } else if (!pause) {
                Serial.printf(first ? "Charge guard: die %.1f C (battery ~%.0f C), charging on.\n"
                                    : "Charging resumed (die %.1f C, battery ~%.0f C)\n",
                              die, die - DIE_OFFSET_C);
            }
        }
    }

    if (gJournalReady) {
        // E2004/E2005 describe a required pause, not a confirmed charger state.
        // Follow intent/hysteresis even if control is unconfirmed. Context is
        // measured die temperature, not an invented cell measurement.
        // Unreadable samples carry no temperature.
        bool unreadable = !std::isfinite(die);
        if (!unreadable) {
            for (uint16_t code : {2004, 2005}) {
                gLoopFaults.event(code, now);
                if (die >= -50 && die <= 150) gLoopFaults.integer("temperature_mc", int(die * 1000));
                gLoopFaults.condition(pause && (code == 2004 ? die > CHG_HOT_RESUME_C : die < CHG_COLD_RESUME_C));
            }
        }
        if (!firstGrace) gLoopFaults.event(2006, now).condition(unreadable);
        if (gChargeState != pmu_read::ChargeState::Unconfirmed || gChargeFailures >= CHG_FAILURE_LIMIT)
            gLoopFaults.event(2008, now).condition(gChargeFailures >= CHG_FAILURE_LIMIT);
    }
    // Charging was already commanded off during the retries, so the state
    // block above prints nothing when the window ends without a reading.
    if (first && !firstGrace && !std::isfinite(die))
        Serial.printf("Charge guard: no plausible temperature in the first %lu s; charging stays off.\n",
                      (unsigned long)(CHG_FIRST_GRACE_MS / 1000));
}

// Once USB power has been off for VBUS_OFF_CONFIRM_MS, the car is off:
// finalize the trip cleanly but keep running on battery -- we'll upload
// next time we're home. Re-reads the level in case an INSERT interrupt was
// missed. Keeps a genuine short trip (discardIfMicro=false).
static void confirmVbusOff() {
    if (!gVbusOffPending || millis() - gVbusOffT0 < VBUS_OFF_CONFIRM_MS ||
        uint32_t(millis() - gVbusOffReadMs) < VBUS_RETRY_MS) return;
    gVbusOffReadMs = millis();
    VbusState state = vbusState();
    if (state == VbusState::Unknown) return; // pending; one read attempt per second
    gVbusOffPending = false;
    if (state == VbusState::Present) return;
    Serial.println("VBUS off for 10 s -- car off: finalizing trip, staying awake.");
    gVbusLost = true;
    if (gTripPath[0]) endTrip(/*discardIfMicro=*/false);
    else if (gState == ARMING) {
        // A confirmed car-off also cancels a start that never opened a file.
        gState = IDLE;
        armBufReset();
        gPendCount = gPendHead = 0;
    }
}

// =================================================================
// Networking task (core 0): upload finished trips when parked at home
// =================================================================
static void observeNetworkStack(unsigned remaining) {
    if (gJournalReady) gNetFaults.event(1007, millis()).integer("free_bytes", min(remaining, 1048576u))
        .condition(remaining < NET_STACK_WARN_BYTES);
}

static void reportNetworkStack(const char* phase) {
    // The watermark remembers the deepest call, including HTTP internals
    // that have already returned. Call only from the network task itself.
    unsigned remaining = (unsigned)uxTaskGetStackHighWaterMark(nullptr);
    observeNetworkStack(remaining);
    Serial.printf("Net stack (%s): minimum free %u / %u bytes%s\n",
                  phase, remaining, (unsigned)NET_STACK_BYTES,
                  remaining < NET_STACK_WARN_BYTES ? " -- WARNING: low headroom" : "");
}

static bool homeApInRange() {
    int16_t n = WiFi.scanNetworks(false, false, false, 150);
    bool found = false;
    for (int i = 0; i < n; ++i) {
        if (WiFi.SSID(i) != WIFI_SSID) continue;
#if USE_BSSID_FILTER
        if (memcmp(WiFi.BSSID(i), WIFI_BSSID, 6) != 0) continue;
#endif
        found = true;
        break;
    }
    // Also release an empty/failed scan, and never keep pointers into its
    // results (including BSSID) after this call.
    WiFi.scanDelete();
    // A failed/aborted scan has not observed absence. No per-scan events.
    if (gJournalReady && !found && n >= 0 && !gNetAbort.load()) gNetFaults.homeApAbsent();
    return found;
}

static bool connectWifi() {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(false);
#if USE_BSSID_FILTER
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD, 0, WIFI_BSSID, true);
#else
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
#endif
    uint32_t t0 = millis();
    while (WiFi.status() != WL_CONNECTED && !gNetAbort.load() && millis() - t0 < 15000) {
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    bool joined = WiFi.status() == WL_CONNECTED;
    if (gJournalReady && !gNetAbort.load()) {
        gJoinFailures = joined ? 0 : min(unsigned(gJoinFailures) + 1, 255u);
        gNetFaults.event(5002, millis()).integer("attempts", gJoinFailures).condition(!joined);
    }
    return joined;
}

static void uploadDiagnostics(int status) {
    if (!gJournalReady || gNetAbort.load()) return; // planned shutdown is not a transport fault
    uint16_t code = status <= 0 ? 5003 : status == 401 ? 5004 : status == 413 ? 5005 :
                    status == 400 ? 5006 : status == 409 ? 5007 :
                    status >= 500 && status <= 599 ? 5008 : 5009;
    if (status <= 0) gTransportFailures = min(unsigned(gTransportFailures) + 1, 255u);
    if (status == 200) gTransportFailures = 0;
    for (uint16_t c = 5003; c <= 5009; ++c) {
        // A different error class is not evidence of recovery. Keep each
        // episode until an actual HTTP 200, with context from that observation.
        if (status != 200 && c != code) continue;
        gNetFaults.event(c, millis()).enumeration("target", "trip");
        if (c == 5003) gNetFaults.integer("attempts", gTransportFailures);
        else if (status >= 100 && status <= 599) gNetFaults.integer("http_status", status);
        // A non-HTTP positive library result has no valid status measurement.
        gNetFaults.condition(status != 200);
    }
}

// Opportunistic SNTP while already connected. A plausible RTC date is not
// evidence of synchronization: deep sleep carries that clock, including drift.
// Only an actual completion from this attempt makes its timestamps trusted.
static void trySntp() {
    if (gClockSet.load()) return;
    esp_sntp_stop();
    esp_sntp_set_sync_mode(SNTP_SYNC_MODE_IMMED);
    esp_sntp_set_sync_status(SNTP_SYNC_STATUS_RESET);
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    uint32_t start = millis();
    while (!gClockSet.load() && !gNetAbort.load() && millis() - start < 5000) {
        if (esp_sntp_get_sync_status() == SNTP_SYNC_STATUS_COMPLETED) {
            gDiagnosticClock.trusted(diagnostics::EventTimeSource::Sntp, millis());
            gClockSet.store(true);
            Serial.println("System clock set from NTP.");
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    // No background SNTP writes after the attempt, including if GPS won.
    esp_sntp_stop();
}

// Caller holds the FS mutex. Accept an already-open directory entry too,
// so queue selection can skip incomplete files without a recursive lock.
static bool isCompleteFile(File& f) {
    bool ok = false;
    if (f) {
        size_t sz = f.size();
        if (sz >= 6) {
            size_t back = sz > 16 ? 16 : sz;
            f.seek(sz - back);
            char tail[17];
            int n = f.read((uint8_t*)tail, back);
            tail[n] = 0;
            ok = strstr(tail, "</gpx>") != nullptr;
        }
    }
    return ok;
}

static bool isComplete(fs::FS& storage, const char* path) {
    FS_LOCK();
    if (!tripStoreAvailable(storage)) { FS_UNLOCK(); return false; }
    File f = storage.open(path, FILE_READ);
    bool ok = isCompleteFile(f);
    if (f) f.close();
    FS_UNLOCK();
    return ok;
}

// Stream proxy that takes the FS mutex only for the duration of each
// individual chunk read. The old code held the mutex across the whole
// http.sendRequest() (up to ~15 s): if a new trip started during an
// upload, core 1 blocked in writeOrBufferTrkpt() and the 2 KB GPS UART
// buffer (~2 s at 9600 baud) overflowed, dropping fixes. HTTPClient
// reads ~1.4 KB per chunk, so each lock hold is now milliseconds and
// the GPS task interleaves freely. The file being uploaded is always
// finalized and never the active trip, so per-chunk locking is safe.
// available() returns -1 once gNetAbort is set: HTTPClient then stops
// sending and fails the request, so a cut-off upload ends within a chunk.
class LockedFileStream : public Stream {
    File& _f;
    size_t _remaining;
    bool _replyStarted = false;
    uint32_t _replyStartMs = 0;
public:
    LockedFileStream(File& f, size_t size) : _f(f), _remaining(size) {}
    int available() override {
        // Core 3.3.11's HTTPClient checks available() again AFTER the final
        // socket write, before testing its remaining length and reading the
        // response. Recording EOF here includes the last socket write in
        // send time, unlike timing the final filesystem read.
        if (!_remaining && !_replyStarted) { _replyStarted = true; _replyStartMs = millis(); }
        if (gNetAbort.load()) return -1;
        FS_LOCK(); int n = _f.available(); FS_UNLOCK(); return n;
    }
    int read() override {
        FS_LOCK(); int c = _f.read(); FS_UNLOCK();
        if (c >= 0 && _remaining) --_remaining;
        return c;
    }
    size_t readBytes(char* buf, size_t len) override {
        FS_LOCK(); size_t n = _f.read((uint8_t*)buf, len); FS_UNLOCK();
        _remaining -= min(n, _remaining);
        return n;
    }
    int peek() override {
        FS_LOCK(); int c = _f.peek(); FS_UNLOCK(); return c;
    }
    size_t write(uint8_t) override { return 0; }   // read-only proxy
    uint32_t replyWaitMs(uint32_t ended) const { return _replyStarted ? uint32_t(ended - _replyStartMs) : 0; }
};

static bool uploadFile(fs::FS& storage, const char* path) {
    String url = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/upload";
    WiFiClient client;
    HTTPClient http;

    FS_LOCK();
    if (!tripStoreAvailable(storage)) { FS_UNLOCK(); return false; }
    File f = storage.open(path, FILE_READ);
    if (!f) {
        FS_UNLOCK();
        debugUpload(-1);
        return false;
    }
    size_t sz = f.size();
    FS_UNLOCK();

    LockedFileStream body(f, sz);
    http.begin(client, url);
    http.setConnectTimeout(TRIP_CONNECT_TIMEOUT_MS);
    http.setTimeout(TRIP_REPLY_TIMEOUT_MS);
    http.addHeader("Content-Type", "application/gpx+xml");
    http.addHeader("Authorization", String("Bearer ") + AUTH_TOKEN);
    http.addHeader("X-Device-Id", DEVICE_ID);
    // No X-Trip-Id, by design: the server names the trip by its GPX start
    // time. Don't send the local name -- its NVS counter can reset and
    // reuse a name, the server answers a reused id holding different GPX
    // with 409, and a 409 stays queued, retried indefinitely.

    // Size and time go in the log so a real upload shows the link speed:
    // the server's worker timeout (900 s) bounds how slow it can be.
    uint32_t t0 = millis();
    int code = http.sendRequest("POST", &body, sz);
    uint32_t ended = millis();
    uint32_t elapsed = ended - t0;
    uint32_t replyMs = body.replyWaitMs(ended);
    float sendSecs = (elapsed - replyMs) / 1000.0f;
    float replySecs = replyMs / 1000.0f;
    http.end();
    if (code == 200) serverContact();
    uploadDiagnostics(code);
    float secs = elapsed / 1000.0f;
    reportNetworkStack("after HTTP upload");

    FS_LOCK();
    f.close();
    bool ok = (code == 200);
    // 400 (malformed GPX) and 413 (too large) mean the server read this
    // file and rejected it: resending the same bytes can never succeed, and
    // retrying forever burns battery and radio time. Set it
    // aside as .bad -- kept for inspection, ignored by uploads and fsck.
    // Every other failure (401 token mismatch, 404 wrong host/port, 5xx,
    // network errors) is a fixable setup or server problem, not this
    // file's fault, so those stay queued.
    bool rejected = (code == 400 || code == 413);
    bool setAside = false;
    bool deleted = false;
    if (ok) {
        deleted = storage.remove(path);
    } else if (rejected) {
        String bad = String(path) + ".bad";
        setAside = storage.rename(path, bad.c_str());
    }
    FS_UNLOCK();

    debugUpload(code);
    if (ok && !deleted) {
        // HTTP success still clears upload backoff; the queued copy may retry.
        Serial.printf("Upload %s (%u B, %.1f s) -> HTTP 200, but deleting the local file failed "
                      "(will re-upload; the server ignores identical copies); send %.1f s, reply %.1f s\n",
                      path, (unsigned)sz, secs, sendSecs, replySecs);
    } else if (ok) {
        Serial.printf("Upload %s (%u B, %.1f s) -> HTTP 200 (deleted); send %.1f s, reply %.1f s\n",
                      path, (unsigned)sz, secs, sendSecs, replySecs);
    } else if (rejected) {
        Serial.printf("ERROR: upload %s (%u B, %.1f s) -> HTTP %d: server rejected the file -- %s; send %.1f s, reply %.1f s\n",
                      path, (unsigned)sz, secs, code,
                      setAside ? "kept as .bad, not retried" : "rename failed, will retry", sendSecs, replySecs);
    } else {
        Serial.printf("Upload %s (%u B, %.1f s) -> HTTP %d (will retry); send %.1f s, reply %.1f s\n",
                      path, (unsigned)sz, secs, code, sendSecs, replySecs);
    }
    return ok;
}

// Collect names of complete, non-active trip files, then upload each.
// Any successful upload resets the network retry delay, even if a later
// file fails or a sleep request stops the batch.
static bool uploadPending(fs::FS& storage) {
    String pending[16];
    int    count = 0;

    FS_LOCK();
    if (!tripStoreAvailable(storage)) { FS_UNLOCK(); return false; }
    File dir = storage.open(TRIP_DIR);
    if (dir && dir.isDirectory()) {
        File e = dir.openNextFile();
        while (e && count < 16 && !gNetHold.load() && !gNetAbort.load()) {
            String name = tripEntryPath(e);
            if (name.endsWith(".gpx") &&
                (&storage != gTripFS || gTripPath[0] == 0 || name != String(gTripPath)) &&
                isCompleteFile(e)) {
                pending[count++] = name;
            }
            e = dir.openNextFile();
        }
    }
    if (dir) dir.close();  // finish enumeration before uploads mutate the directory
    FS_UNLOCK();

    // A sleep request (gNetHold) lets the current file finish, then stops.
    bool uploaded = false;
    for (int i = 0; i < count && !gNetHold.load() && !gNetAbort.load() && tripStoreAvailable(storage); ++i) {
        if (isComplete(storage, pending[i].c_str())) {
            if (uploadFile(storage, pending[i].c_str())) uploaded = true;
        }
    }
    return uploaded;
}

static bool hasPending(fs::FS& storage) {
    // Preserved incomplete files need recovery, not radio work. Scan past
    // them so a complete trip later in directory order is still found.
    bool found = false;
    FS_LOCK();
    if (!tripStoreAvailable(storage)) { FS_UNLOCK(); return false; }
    File dir = storage.open(TRIP_DIR);
    if (dir && dir.isDirectory()) {
        File e = dir.openNextFile();
        while (e && !gNetHold.load() && !gNetAbort.load()) {
            String name = tripEntryPath(e);
            if (name.endsWith(".gpx") &&
                (&storage != gTripFS || gTripPath[0] == 0 || name != String(gTripPath)) &&
                isCompleteFile(e)) {
                found = true;
                break;
            }
            e = dir.openNextFile();
        }
    }
    if (dir) dir.close();
    FS_UNLOCK();
    return found;
}

static bool uploadPending() {
    // Separate batches prevent a large SD queue from starving old flash
    // trips. A filename can exist on both stores: every operation keeps its
    // filesystem identity, and only the recording store has an active file.
    bool uploaded = false;
    if (gSdMounted && !gSdUnavailable.load()) uploaded = uploadPending(SD);
    if (gFlashMounted) uploaded = uploadPending(LittleFS) || uploaded;
    return uploaded;
}

static bool hasPending() {
    return (gSdMounted && !gSdUnavailable.load() && hasPending(SD)) || (gFlashMounted && hasPending(LittleFS));
}

#include "diag_worker.h"

// Only the network task scans: once per ~30 s cycle, and again after an
// upload batch. gNetBusy protects unmount; per-entry FS locking lets GPS
// writes interleave even with a large SD backlog. Count complete files on
// BOTH stores, excluding .bad/.tmp and the still-incomplete current trip.
static unsigned debugPendingCount(fs::FS& storage) {
    unsigned count = 0;
    FS_LOCK();
    if (!tripStoreAvailable(storage)) { FS_UNLOCK(); return 0; }
    File dir = storage.open(TRIP_DIR);
    bool directory = dir && dir.isDirectory();
    FS_UNLOCK();
    if (directory) {
        while (debugMode() && !gNetHold.load() && !gNetAbort.load() && tripStoreAvailable(storage)) {
            FS_LOCK();
            File e = dir.openNextFile();
            bool end = !e;
            if (e) {
                if (tripEntryPath(e).endsWith(".gpx") && isCompleteFile(e)) ++count;
                e.close();
            }
            FS_UNLOCK();
            if (end || count >= 999) break;
            vTaskDelay(pdMS_TO_TICKS(1));
        }
    }
    FS_LOCK();
    if (dir) dir.close();
    FS_UNLOCK();
    return count;
}

static void refreshDebugPending() {
    unsigned count = 0;
    if (gSdMounted && !gSdUnavailable.load()) count += debugPendingCount(SD);
    if (gFlashMounted) count += debugPendingCount(LittleFS);
    if (!debugMode() || gNetHold.load() || gNetAbort.load()) return; // partial count is unknown
    portENTER_CRITICAL(&gDebugNetMux);
    gDebugNet.pending = (int)count;
    portEXIT_CRITICAL(&gDebugNetMux);
}

// One pass of the network task. gNetBusy covers everything that touches
// WiFi or the filesystem, and is raised before gNetHold is read (see the
// handshake note at the globals).
static void netCycle() {
    contactUptime(); // extend millis across wrap while the awake task ticks
    gNetBusy.store(true);
    if (!gNetHold.load()) {
        if (debugMode()) refreshDebugPending();
        // A trip just ended, so the car may be parked at home: try now,
        // whatever the backoff reached while driving or parked elsewhere.
        if (gNetRetryNow.exchange(false)) gNetRetryMs = 0;
        bool pending = hasPending();
        // A scan interrupted by sleep/abort has not proved the queue empty.
        if (gNetHold.load() || gNetAbort.load()) {
            gNetBusy.store(false);
            return;
        }
        if (!pending) gNetRetryMs = 0;
        bool tripDue = pending && (gNetRetryMs == 0 || millis() - gNetLastAttemptMs >= gNetRetryMs);
        bool diagnosticsDue = !pending && eventsDue();
        if (tripDue || diagnosticsDue) {
            bool uploaded = false;
            const char* miss = nullptr;
            int joinStatus = -1;
            debugWifi(DebugWifi::Scan);
            bool homeFound = homeApInRange();
            debugWifi(homeFound ? DebugWifi::Join : DebugWifi::Off);
            if (!homeFound) {
                miss = "home WiFi not found";
            } else if (!connectWifi()) {
                miss = "couldn't join home WiFi";
                joinStatus = (int)WiFi.status();  // capture before disconnect
            } else {
                debugWifi(DebugWifi::On);
                trySntp();
                if (tripDue) uploaded = uploadPending();
                drainEvents(tripDue);
                if (debugMode()) refreshDebugPending();
                if (tripDue && !uploaded) miss = "no trip uploaded";
            }
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
            debugWifi(DebugWifi::Off);
            // Back off AP misses, failed joins, and server failures alike.
            // Seeing the AP alone must not undo a server-failure backoff.
            if (diagnosticsDue && (!homeFound || joinStatus >= 0)) gEventRetry.failed(millis(), -1);
            if (tripDue) {
                gNetRetryMs = uploaded ? NET_TICK_MS :
                    min((gNetRetryMs ? gNetRetryMs : NET_TICK_MS) * 2, NET_RETRY_MAX_MS);
                gNetLastAttemptMs = millis(); // diagnostics never changes GPX retry
            }
            if (miss && tripDue) {
                if (joinStatus >= 0) {
                    Serial.printf("Network: %s \"%s\" (status %d) -- next try in %lu s.\n",
                                  miss, WIFI_SSID, joinStatus, (unsigned long)(gNetRetryMs / 1000));
                } else if (!homeFound) {
                    Serial.printf("Network: %s \"%s\" -- next try in %lu s.\n",
                                  miss, WIFI_SSID, (unsigned long)(gNetRetryMs / 1000));
                } else {
                    Serial.printf("Network: %s -- next try in %lu s.\n",
                                  miss, (unsigned long)(gNetRetryMs / 1000));
                }
            }
            reportNetworkStack("after network cycle");
        }
    }
    gNetBusy.store(false);
}

static void networkTask(void*) {
    reportNetworkStack("task start");
    for (;;) {
        serviceJournal(); // one record, zero-wait lock, releases before networking
        // Check after maintenance even with no trips/AP to trigger netCycle's
        // existing log points. No serial output or per-slice scan is needed.
        observeNetworkStack((unsigned)uxTaskGetStackHighWaterMark(nullptr));
        netCycle();
        // Preserve the network cadence while allowing shutdown handoff and
        // bounded journal slices during the old 30-second idle delay.
        uint32_t idleStart = millis();
        while (millis() - idleStart < NET_TICK_MS) {
            vTaskDelay(pdMS_TO_TICKS(100));
            serviceJournal();
        }
    }
}

// =================================================================
// GPS receiver configuration (u-blox UBX protocol, started at boot)
// =================================================================
// Apply one RAM-only VALSET at every boot rather than rely on retained
// settings. One ACK covers the whole configuration, so late CFG-MSG ACKs
// cannot be mistaken for replies to a different setting.

// Frame: B5 62 | class id | length (LE16) | payload | Fletcher-8 checksum
// over class..payload.
static void ubxSend(uint8_t cls, uint8_t id, const uint8_t* payload, uint16_t len) {
    uint8_t hdr[6] = { 0xB5, 0x62, cls, id, (uint8_t)(len & 0xFF), (uint8_t)(len >> 8) };
    uint8_t a = 0, b = 0;
    for (int i = 2; i < 6; ++i)    { a += hdr[i];     b += a; }
    for (uint16_t i = 0; i < len; ++i) { a += payload[i]; b += a; }
    GPSSerial.write(hdr, sizeof(hdr));
    GPSSerial.write(payload, len);
    GPSSerial.write(a);
    GPSSerial.write(b);
}

// Send one message and arm its deadline. Only one frame (52 bytes)
// is written per call; there is no flush or ACK wait.
static void sendGpsConfigCommand() {
    // u-blox M10 SPG 5.10 Interface description, UBX-21035062 R03:
    // 3.10.5.1: version 0, RAM layer only, two reserved zero bytes.
    // 4.9.11 / Table 19: UART1 message keys below, all U1 (one byte).
    // 4.9.12 / Tables 20, 23: DYNMODEL is E1 (one byte), AUTOMOT = 4.
    // 4.2 / 4.5: U1/E1 values occupy one byte, keys are little-endian U4,
    // no padding. No BBR/flash writes or navigation-rate changes.
    static const uint8_t p[] = {
        0x00, 0x01, 0x00, 0x00,
        0xCA, 0x00, 0x91, 0x20, 0,  // CFG-MSGOUT-NMEA_ID_GLL_UART1
        0xC0, 0x00, 0x91, 0x20, 0,  // CFG-MSGOUT-NMEA_ID_GSA_UART1
        0xB1, 0x00, 0x91, 0x20, 0,  // CFG-MSGOUT-NMEA_ID_VTG_UART1
        0xC5, 0x00, 0x91, 0x20,     // CFG-MSGOUT-NMEA_ID_GSV_UART1
        5,  // ~1 KB bursts every fifth epoch leave 9600-baud headroom
        0xAC, 0x00, 0x91, 0x20, 1,  // CFG-MSGOUT-NMEA_ID_RMC_UART1
        0xBB, 0x00, 0x91, 0x20, 1,  // CFG-MSGOUT-NMEA_ID_GGA_UART1
        0x07, 0x00, 0x91, 0x20, 1,  // CFG-MSGOUT-UBX_NAV_PVT_UART1
        0x21, 0x00, 0x11, 0x20, 4,  // CFG-NAVSPG-DYNMODEL
    };
    ubxSend(0x06, 0x8A, p, sizeof(p));
    gGpsConfig.sends++;
    gGpsConfig.sendGeneration++;
    gGpsConfig.ack = -1;
    gGpsConfig.since = millis();
    gGpsConfig.state = GPS_WAIT_ACK;
}

static void gpsConfigAttemptFailed() {
    gGpsConfig.since = millis();
    if (gGpsConfig.attempts >= GPS_CONFIG_ATTEMPTS) {
        // One give-up notice, not one per send/deadline. NAKs are separately
        // per-occurrence E3003; a final NAK is not also a timeout.
        if (gJournalReady && gGpsConfig.ack != 0)
            gLoopFaults.event(3004, millis()).integer("message_class", 0x06).integer("message_id", 0x8A).notice();
        gGpsConfig.state = GPS_CONFIG_DONE;
        gNavPvtWaitMs = millis();
        Serial.printf("WARNING: GPS not configured after %u attempts -- continuing with current receiver settings.\n",
                      (unsigned)GPS_CONFIG_ATTEMPTS);
    } else {
        gGpsConfig.state = GPS_RETRY;
        if (!gGpsConfig.retryLogged) {
            Serial.println("WARNING: GPS config incomplete -- retrying in background.");
            gGpsConfig.retryLogged = true;
        }
    }
}

static void configureGps() {
    gGpsConfig = GpsConfig();
    gGpsConfig.state = GPS_WAIT_READY;
    gGpsConfig.attempts = 1;
    gGpsConfig.since = millis();
    gGpsConfig.started = gGpsConfig.since;
    gGpsDiagStartMs = gLastNmeaMs = gIdleFixWaitMs = gGpsConfig.since;
    Serial.println("GPS: waiting for checksum-valid NMEA (up to 3 s).");
}

static void serviceGpsConfiguration() {
    if (gGpsConfig.state == GPS_RETRY) {
        if (millis() - gGpsConfig.since < GPS_CONFIG_RETRY_MS) return;
        gGpsConfig.attempts++;
        gGpsConfig.sends = 0;
        gGpsConfig.since = millis();
        gGpsConfig.state = GPS_WAIT_READY;
    }
    if (gGpsConfig.state == GPS_WAIT_READY) {
        // Any complete, checksum-valid NMEA sentence proves the receiver
        // is running; a position fix is unnecessary. A glitch byte cannot.
        if (gps.passedChecksum() > 0) {
            if (!gGpsConfig.readyLogged) {
                Serial.println("GPS ready: checksum-valid NMEA received.");
                gGpsConfig.readyLogged = true;
            }
            sendGpsConfigCommand();
        } else if (millis() - gGpsConfig.since >= GPS_READY_MS) {
            gpsConfigAttemptFailed();
        }
        return;
    }
    if (gGpsConfig.state != GPS_WAIT_ACK) return;
    if (gGpsConfig.ack == 1) {
        gGpsConfig.state = GPS_CONFIG_DONE;
        gNavPvtWaitMs = millis();
        Serial.printf("GPS configured on attempt %u (%lu ms after config start): "
                      "RMC+GGA+NAV-PVT, GSV every 5 epochs, automotive model.\n",
                      (unsigned)gGpsConfig.attempts,
                      (unsigned long)(millis() - gGpsConfig.started));
    } else if (gGpsConfig.ack == 0) {
        if (gJournalReady)
            gLoopFaults.event(3003, millis()).integer("message_class", 0x06).integer("message_id", 0x8A).notice();
        gpsConfigAttemptFailed();  // NAK: retry the same VALSET later
    } else if (millis() - gGpsConfig.since >= GPS_ACK_MS) {
        if (gGpsConfig.sends < GPS_COMMAND_SENDS) sendGpsConfigCommand();
        else gpsConfigAttemptFailed();
    }
}

// True when an RMC sentence carrying a valid fix has arrived since the last
// call. Reading value() clears the flag, so check both talker IDs each time.
static bool rmcFixArrived() {
    bool fix = false;
    if (gRmcStatusGN.isUpdated()) {
        bool valid = gRmcStatusGN.value()[0] == 'A';
        fix |= valid;
        gDebugRmcValid = valid;
        if (!valid) gLatestSpeedTrusted = false;
    }
    if (gRmcStatusGP.isUpdated()) {
        bool valid = gRmcStatusGP.value()[0] == 'A';
        fix |= valid;
        gDebugRmcValid = valid;
        if (!valid) gLatestSpeedTrusted = false;
    }
    return fix;
}

// =================================================================
// Build a Fix from the current TinyGPSPlus state
// =================================================================
static bool buildFix(Fix& fx, uint64_t rmcUtcMs) {
    if (!gps.location.isValid()) return false;
    fx.lat       = gps.location.lat();
    fx.lon       = gps.location.lng();
    fx.speedMps  = gps.speed.isValid() ? gps.speed.mps() : 0.0f;
    fx.speedTrusted = gpsSpeedTrusted(gState == IDLE || gState == ARMING);
    gLatestSpeedTrusted = fx.speedTrusted;
    fx.hdop      = gps.hdop.isValid() ? gps.hdop.hdop() : 99.9f;
    fx.monoMs    = millis();
    fx.hasUtc    = rmcUtcMs != 0;
    fx.utcMs     = rmcUtcMs;
    return true;
}

// Handle each RMC as soon as it commits, including multiple epochs in one
// UART drain. ACK handling never consumes bytes on a separate read path.
// RMC carries position and speed together; GGA supplies the latest HDOP
// but must not log a second point with the previous epoch's speed.
static void receiveGpsNmea(uint8_t c) {
    gDebugGsv.encode((char)c);
    if (!gps.encode((char)c)) return;
    gLastNmeaMs = millis(); // only checksum-valid NMEA, never UBX/noise bytes
    gDebugGsv.update(millis());
    uint64_t rmcUtcMs = 0;
    setClockFromGps(&rmcUtcMs);
    if (rmcFixArrived()) {
        Fix fx;
        if (buildFix(fx, rmcUtcMs)) {
            if (gJournalReady) {
                gLoopFaults.event(3005, fx.monoMs).integer("elapsed_ms", 0).condition(false);
            }
            if (gHaveLastFix && (fx.monoMs - gLastFix.monoMs) > SEG_GAP_MS) {
                if (gState == ACTIVE || gState == COOLDOWN) {
                    fx.newSegment = true;
                } else if (gState == ARMING) {
                    gState = IDLE;
                    armBufReset();
                }
            }
            tripFsmOnFix(fx);
        }
    }
}

static void serviceRuntimeDiagnostics() {
    if (!gJournalReady) return;
    uint32_t now = millis();
    // Ten seconds of silence is well beyond normal 1 Hz output.
    uint32_t nmeaGap = now - gLastNmeaMs;
    gLoopFaults.event(3001, now).integer("elapsed_ms", nmeaGap).condition(nmeaGap >= 10000);
    bool recording = gState == ACTIVE || gState == COOLDOWN;
    // E3002 means no fix accurate enough to start a trip: count from the last
    // trusted fix, or GPS config start, with a fresh idle acquisition grace.
    uint32_t trustedFixGap = now - (gHaveLastFix ? gLastFix.monoMs : gGpsDiagStartMs);
    if (gState != IDLE) gIdleFixWaitMs = now;
    uint32_t idleGap = min(trustedFixGap, uint32_t(now - gIdleFixWaitMs));
    gLoopFaults.event(3002, now).integer("elapsed_ms", idleGap);
    if (gps.satellites.isValid()) gLoopFaults.integer("satellites", min(gps.satellites.value(), uint32_t(255)));
    gLoopFaults.condition(gState == IDLE && idleGap >= 120000);
    // E3005 means positions stopped: any valid RMC counts, trusted or not,
    // so weak accuracy while parked in cooldown does not cause a trip alert.
    // Finalizing does not establish recovery; receiveGpsNmea() clears E3005
    // on a valid RMC, including after the five-minute watchdog.
    uint32_t rmcFixGap = now - gLastFixMonoMs;
    if (recording && rmcFixGap >= 30000)
        gLoopFaults.event(3005, now).integer("elapsed_ms", rmcFixGap).condition(true);
    // UTC_UNAVAILABLE describes the existing trusted system-clock state, with
    // a two-minute boot allowance for GPS/SNTP. Untimed GPX points are separate.
    gLoopFaults.event(3006, now).condition(!gClockSet.load() && uint32_t(now - gGpsDiagStartMs) >= 120000);
    // Never manufacture a healthy observation at boot or during the hold.
    // No-evidence samples retain the last measured verdict; RuntimeFaults
    // supplies bounded retries/repeats and the original episode identity.
    if (gGpsAntenna.observed()) {
        gLoopFaults.event(3009, now);
        if (gAntennaRf.valid) gLoopFaults.integer("agc_pct", gAntennaRf.agcPct);
        if (gAntennaSignal.heard >= 0) gLoopFaults.integer("max_cno", gAntennaSignal.maxCno);
        gLoopFaults.condition(gGpsAntenna.active());
    }
    gLoopFaults.event(4001, now).condition(!gSdMounted || gSdUnavailable.load());
    // Check idle storage too, so a successful core-0 upload can be observed as
    // recovery on core 1 without sharing producer state across tasks.
    if (uint32_t(now - gStorageDiagMs) >= FS_CHECK_MS) {
        gStorageDiagMs = now;
        storageConditions(fsFreeBytes(), now);
    }
    serviceBatteryDiagnostics();
    gLoopFaults.event(6001, now).condition(gDebugDisplayFailed || gProductionDisplayFailed);
}

static void receiveGpsByte(uint8_t c) {
    GpsUbxRx& rx = gGpsUbxRx;
    // Recover from a truncated or noise-induced UBX header. Expected ACKs
    // are 10 bytes; bound other packets to 256 payload bytes / 500 ms.
    if (rx.pos && millis() - rx.since >= 500) rx = GpsUbxRx();
    if (rx.pos == 0) {
        if (c == 0xB5) {
            rx.pos = 1; rx.since = millis();
            rx.configGeneration = gGpsConfig.state == GPS_WAIT_ACK ? gGpsConfig.sendGeneration : 0;
        }
        else receiveGpsNmea(c);
        return;
    }
    if (rx.pos == 1) {
        if (c == 0x62) rx.pos++;
        else {
            rx = GpsUbxRx();
            if (c == 0xB5) {
                rx.pos = 1; rx.since = millis();
                rx.configGeneration = gGpsConfig.state == GPS_WAIT_ACK ? gGpsConfig.sendGeneration : 0;
            }
            else receiveGpsNmea(c);
        }
        return;
    }
    if (rx.pos < 6 || rx.pos < (uint32_t)rx.len + 6) {
        rx.a += c; rx.b += rx.a;
        if (rx.pos == 2) rx.cls = c;
        else if (rx.pos == 3) rx.id = c;
        else if (rx.pos == 4) rx.len = c;
        else if (rx.pos == 5) rx.len |= (uint16_t)c << 8;
        else if (rx.pos - 6 < sizeof rx.payload) {
            size_t offset = rx.pos - 6;
            // Checksum the original bytes above, but never retain NAV-PVT
            // longitude/latitude (offsets 24..31), even in the receive buffer.
            bool coordinate = rx.cls == 0x01 && rx.id == 0x07 && offset >= 24 && offset < 32;
            rx.payload[offset] = coordinate ? 0 : c;
        }
        rx.pos++;
        if (rx.len > 256) rx = GpsUbxRx();
    } else if (rx.pos == (uint32_t)rx.len + 6) {
        rx.checksumA = (c == rx.a);
        rx.pos++;
    } else {
        // Ignore replies outside the current send window, including frames
        // started in backoff or before a resend. UBX ACK carries no attempt
        // ID: a whole delayed ACK arriving after a later identical send is
        // indistinguishable, but still acknowledges the same settings.
        if (rx.checksumA && c == rx.b && rx.cls == 0x05 && rx.len == 2 &&
            (rx.id == 0x01 || rx.id == 0x00) && rx.payload[0] == 0x06 &&
            rx.payload[1] == 0x8A && gGpsConfig.state == GPS_WAIT_ACK &&
            rx.configGeneration == gGpsConfig.sendGeneration &&
            millis() - gGpsConfig.since < GPS_ACK_MS &&
            gGpsConfig.ack < 0) {
            gGpsConfig.ack = rx.id == 0x01 ? 1 : 0;
        }
        if (rx.checksumA && c == rx.b && rx.cls == 0x01 && rx.id == 0x07 && rx.len == 92) {
            // Explicit little-endian reads avoid alignment and host-endian assumptions.
            const auto u4 = [](size_t off) -> uint32_t {
                return uint32_t(rx.payload[off]) | uint32_t(rx.payload[off + 1]) << 8 |
                       uint32_t(rx.payload[off + 2]) << 16 | uint32_t(rx.payload[off + 3]) << 24;
            };
            gNavPvt.seen = true; gNavPvt.receivedMs = millis();
            gNavPvt.iTow = u4(0); gNavPvt.valid = rx.payload[11];
            gNavPvt.fixType = rx.payload[20]; gNavPvt.flags = rx.payload[21];
            gNavPvt.numSv = rx.payload[23]; gNavPvt.hAccMm = u4(40);
            gNavPvt.gSpeedMmps = (int32_t)u4(60); gNavPvt.sAccMmps = u4(68);
            serviceGpsSpeedFallback();
        }
        if (rx.checksumA && c == rx.b && rx.cls == 0x0A && rx.id == 0x38 &&
            gDebugRfStarted && gGpsConfig.state == GPS_CONFIG_DONE)
            parseDebugMonRf(rx.payload, rx.len, gDebugRf);
        rx = GpsUbxRx();
    }
}

// =================================================================
// setup / loop
// =================================================================
static bool tripPartitionIsErased() {
    // Use the same data partition label as LittleFS, never a fixed address
    // or a sample of the partition: even its last byte could hold trip data.
    const esp_partition_t* partition = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, TRIP_PARTITION_LABEL);
    if (!partition || !partition->size) {
        Serial.println("LittleFS partition missing or empty; check the flashed partitions.csv.");
        return false;
    }

    uint8_t buf[1024];
    for (size_t offset = 0; offset < partition->size;) {
        size_t n = std::min(sizeof buf, (size_t)partition->size - offset);
        if (esp_partition_read(partition, offset, buf, n) != ESP_OK) {
            Serial.printf("LittleFS partition read failed at offset %u; storage preserved.\n",
                          (unsigned)offset);
            return false;
        }
        for (size_t i = 0; i < n; ++i) {
            if (buf[i] != 0xFF) {
                Serial.println("LittleFS partition contains non-erased data; storage preserved.");
                return false;
            }
        }
        offset += n;
        feedLoopWatchdogProgress(); // verified another erased block, not a wait
        delay(1);  // yield during the full-partition scan on a new board
    }
    return true;
}

static bool mountFlashStorage(bool allowProvision = true) {
    // Mount failures alone never authorize erasure. Only a successful read
    // of EVERY byte as 0xFF proves that provisioning cannot lose queued data.
    if (LittleFS.begin(false, "/littlefs", 10, TRIP_PARTITION_LABEL)) return true;
    if (allowProvision && tripPartitionIsErased()) {
        Serial.println("LittleFS partition is fully erased; provisioning trip storage.");
        if (LittleFS.format() &&
            LittleFS.begin(false, "/littlefs", 10, TRIP_PARTITION_LABEL)) return true;
        Serial.println("ERROR: LittleFS provisioning failed. "
                       "See firmware README for storage recovery.");
        return false;
    }
    Serial.println("ERROR: LittleFS mount failed. Storage preserved. "
                   "See firmware README for first-time provisioning/recovery.");
    return false;
}

static bool mountSdStorage() {
    if (gStartupWatchdog.skipCard()) return false;
    // Also used for cancelled/failed shutdown recovery, under FS_LOCK and
    // gNetHold, with no live handles. Never provision or format the card.
    if (!PMU.disableBLDO1() || !PMU.setBLDO1Voltage(3300) || !PMU.enableBLDO1()) {
        Serial.println("WARNING: microSD power setup failed (BLDO1 must be 3.3 V before enable).");
        PMU.disableBLDO1();
        return false;
    }
    pinMode(IMU_CS, OUTPUT);
    digitalWrite(IMU_CS, HIGH);
    pinMode(SD_CS, OUTPUT);
    digitalWrite(SD_CS, HIGH);
    delay(SD_SETTLE_MS);
    bool busReady = gSdSpi.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);
    if (busReady && SD.begin(SD_CS, gSdSpi, SD_SPI_HZ, "/sd", 10, false) && SD.cardType() != CARD_NONE) {
        if (SD.exists(TRIP_DIR) || SD.mkdir(TRIP_DIR)) {
            File dir = SD.open(TRIP_DIR);
            bool ready = dir && dir.isDirectory();
            dir.close();
            if (ready) return true;
        }
        Serial.println("WARNING: microSD trip directory unavailable; card preserved.");
    } else {
        // SD.begin discards card type on failure; there is no card-detect
        // pin. Be explicit that absence and bus/format faults look alike.
        Serial.println("WARNING: no microSD card or mount failed -- check power/wiring and FAT32 (exFAT unsupported); card never formatted.");
    }
    SD.end();
    gSdSpi.end();
    PMU.disableBLDO1();
    return false;
}

static bool mountTripStorage(bool allowProvision = true, bool reportFreeSpace = true) {
    if (gStartupWatchdog.noStorage()) return false;
    startupStage(startup_watchdog::Stage::CardMount);
#ifdef WATCHDOG_BENCH_STARTUP_HANG
    if (gStartupWatchdog.active() && !gStartupWatchdog.skipCard()) {
        Serial.println("WATCHDOG BENCH: blocking card mount until startup watchdog reset.");
        for (;;) vTaskDelay(pdMS_TO_TICKS(1000)); // deliberately never feed
    }
#endif
    gSdMounted = mountSdStorage();
    // Keep the existing preservation-first provisioning rule for flash.
    // If flash is damaged but SD works, record to SD and preserve flash
    // for recovery; degraded service handles both stores being unavailable.
    startupStage(startup_watchdog::Stage::FlashMount);
    gFlashMounted = mountFlashStorage(allowProvision);
    if (gFlashMounted && !LittleFS.exists(TRIP_DIR)) {
        gFlashMounted = LittleFS.mkdir(TRIP_DIR);
        if (!gFlashMounted) LittleFS.end();
    }
    gTripFS = gSdMounted ? static_cast<fs::FS*>(&SD) : static_cast<fs::FS*>(&LittleFS);
    // Degraded retry only probes mounts, then restarts through full setup.
    // Leave the potentially slow first FAT free-cluster scan to that startup,
    // under the longer startup limit, rather than repeat it on the loop.
    if (!reportFreeSpace) return gSdMounted || gFlashMounted;
    if (gSdMounted) {
        startupStage(startup_watchdog::Stage::CardSpace);
        Serial.printf("Trip storage: microSD (%.2f GiB free); internal flash queue %s.\n",
                      storageFreeBytes(SD) / (1024.0 * 1024 * 1024),
                      gFlashMounted ? "mounted" : "unavailable -- preserved for recovery");
    } else if (gFlashMounted) {
        startupStage(startup_watchdog::Stage::FlashSpace);
        Serial.printf("WARNING: microSD unavailable -- using internal flash (%.2f MiB free).\n",
                      storageFreeBytes(LittleFS) / (1024.0 * 1024));
    } else {
        Serial.println("ERROR: no usable trip storage; trips cannot be recorded.");
    }
    return gSdMounted || gFlashMounted;
}

static int debugSatellitesUsed() {
    return gps.satellites.isValid() && gps.satellites.age() <= 15000 ?
           (int)gps.satellites.value() : -1;
}

static float debugHdop() {
    return gps.hdop.isValid() && gps.hdop.age() <= 2500 ? gps.hdop.hdop() : -1;
}

static void serviceDebugRf() {
    if (gGpsConfig.state != GPS_CONFIG_DONE) return;
    uint32_t now = millis();
    if (gDebugRfStarted) {
        if (uint32_t(now - gDebugRfPollMs) < 30000) return;
        gAntennaRf = gDebugRf;
        gAntennaSignal = gDebugGsv.signal(now);
        gGpsAntenna.update(now, gAntennaRf.valid, gAntennaRf.agcPct, gAntennaSignal);
        if (debugMode()) {
            char fix = 0;
            if ((gRmcStatusGN.isValid() && gRmcStatusGN.age() <= 2500) ||
                (gRmcStatusGP.isValid() && gRmcStatusGP.age() <= 2500))
                fix = gDebugRmcValid ? 'A' : 'V';
            char line[DEBUG_RF_LINE_BYTES];
            formatDebugRf(gDebugRf, gDebugGsv.signal(now), debugSatellitesUsed(),
                          debugHdop(), fix, line, sizeof line);
            size_t n = strlen(line);
            if (gNavPvt.seen && uint32_t(now - gNavPvt.receivedMs) <= NAV_PVT_FRESH_MS)
                snprintf(line + n, sizeof line - n, " | sAcc %.2f m/s hAcc %.1f m",
                         gNavPvt.sAccMmps / 1000.0, gNavPvt.hAccMm / 1000.0);
            else snprintf(line + n, sizeof line - n, " | sAcc -- m/s hAcc -- m");
            Serial.println(line);
        }
    }
    // First poll starts after config succeeds OR gives up. Each later poll
    // evaluates the previous interval's reply, then invalidates it. Never wait.
    gDebugRfStarted = true; gDebugRfPollMs = now;
    gDebugRf.valid = false;
    ubxSend(0x0A, 0x38, nullptr, 0);
}

static void beginDebugTemperature() {
    // Only runtime debug entry reaches here, after LoRa-off verification, storage,
    // and the first guard check. PMU/Wire1 accesses (including RTC and IRQ
    // batches) all run serially on core 1; the ISR only sets a flag. There is
    // no PMU access from the networking task and no PMU mutex to acquire.
    gDebugTempTs = debug_temperature::setupTs(PMU);
    char control[11] = "--", adc[11] = "--"; // room for the API's full int range
    if (gDebugTempTs.control >= 0) snprintf(control, sizeof control, "0x%02X", gDebugTempTs.control);
    if (gDebugTempTs.adc >= 0) snprintf(adc, sizeof adc, "0x%02X", gDebugTempTs.adc);
    Serial.printf("Temp setup: TS %s, REG50 %s, REG30 %s (target external input, 50 uA).\n",
                  gDebugTempTs.ready ? "verified" : "unavailable", control, adc);
    // ALDO1 supplies BME280 + OLED + magnetometer. Do not change any rail:
    // Trusted startup sets ALDO1 to 3.3 V; an unpowered/missing sensor shows --.
    gDebugTempEnabled = true;
    gDebugTempPending = false;
    gDebugTempLogMs = millis();
}

static void serviceDebugTemperature() {
    if (!debugMode() || !gDebugTempEnabled || gDegraded.mode() != startup::Mode::Normal) return;
    uint32_t now = millis();
    if (!gDebugTempPending) {
        // Same rollover-safe 30 s scheduling pattern as serviceDebugRf().
        if (uint32_t(now - gDebugTempLogMs) < 30000) return;
        gDebugTempLogMs = now;
        gDebugTempPending = true;
        if (gDebugWireReady && gDebugTempBme.trigger(Wire, now)) return;
    }
    if (gDebugTempBme.waiting(now)) return;
    int32_t centiC = 0;
    bool bmeOk = gDebugWireReady && gDebugTempBme.finish(Wire, now, centiC);
    gDebugTempPending = false;

    int raw = -1;
    if (gDebugTempTs.ready) {
        int high = PMU.readRegister(0x36); // high first latches low (datasheet)
        int low = PMU.readRegister(0x37);
        raw = debug_temperature::tsRaw(high, low);
    }
    const auto p = debug_temperature::samplePower(PMU, now);
    char die[40] = "-- C", estimate[16] = "--", ts[16] = "--", tsRaw[8] = "--";
    char bme[16] = "--", volts[16] = "--", percent[12] = "--";
    // Independent 30 s schedules can drift. Reuse the latest guard sample
    // through one guard interval plus 5 s, and report its age explicitly.
    uint32_t dieAgeMs = uint32_t(now - gDebugTempDieMs);
    if (gChargeChecked && dieAgeMs <= CHG_CHECK_MS + 5000 && std::isfinite(gDebugTempDie)) {
        snprintf(die, sizeof die, "%.1f C (%lu s)", gDebugTempDie, (unsigned long)(dieAgeMs / 1000));
        snprintf(estimate, sizeof estimate, "%.1f", gDebugTempDie - DIE_OFFSET_C);
    }
    if (raw >= 0) {
        snprintf(ts, sizeof ts, "%.3f", debug_temperature::tsVolts(raw));
        snprintf(tsRaw, sizeof tsRaw, "%d", raw);
    }
    if (bmeOk) snprintf(bme, sizeof bme, "%.2f", centiC / 100.0);
    if (p.millivolts >= 0) snprintf(volts, sizeof volts, "%.2f", p.millivolts / 1000.0);
    if (p.percent >= 0) snprintf(percent, sizeof percent, "%d", p.percent);
    const char* charging = p.status2 < 0 ? "--" : debug_temperature::isCharging(p.status2) ? "on" : "off";
    auto vbus = pmu_read::vbusState(p.status1, p.status2);
    Serial.printf("Temp: die %s, guard_est %s C, TS %s V, TS_raw %s, BME280 %s C, "
                  "chg %s, phase %s, batt %s V, pct %s, VBUS %s\n",
                  die, estimate, ts, tsRaw, bme, charging, debug_temperature::phase(p.status2),
                  volts, percent, vbus == VbusState::Unknown ? "--" : vbus == VbusState::Present ? "1" : "0");
}

static void serviceDebugDisplay() {
    if (!debugMode()) return;
    uint32_t now = millis();
    if (!gDebugDisplayReady || now - gDebugDrawMs < 1000) return;
    gDebugDrawMs = now;
    DebugStatus s;
    static const char* states[] = {"IDLE", "ARMING", "ACTIVE", "COOLDOWN", "FINALIZING"};
    s.state = states[gState];
    s.version = firmware::kVersion;
    s.heard = gDebugGsv.signal(now).heard;
    s.used = debugSatellitesUsed();
    if (gDebugRf.valid) s.agcPct = gDebugRf.agcPct;
    s.fixAgeMs = gps.location.isValid() ? gps.location.age() : UINT32_MAX;
    s.fix = gDebugRmcValid && s.fixAgeMs <= 2500;
    s.hdop = debugHdop();
    if (s.fix && gps.speed.isValid() && gps.speed.age() <= 2500) s.mph = gps.speed.mph();
    s.speedTrusted = gLatestSpeedTrusted;
    s.trip = gTripPath[0] != 0;
    s.tripMs = now - gTripT0;
    s.tripMiles = gTripMeters / 1609.344;
    s.store = tripStoreAvailable(*gTripFS) ? (gTripFS == &SD ? "SD" : "FLASH") : "NONE";
    portENTER_CRITICAL(&gDebugNetMux);
    s.net = gDebugNet;
    portEXIT_CRITICAL(&gDebugNetMux);
    s.now = millis();  // snapshot upload time cannot be newer than this clock read
    s.batteryPct = PMU.getBatteryPercent();
    s.batteryV = PMU.getBattVoltage() / 1000.0f;
    s.usb = vbusState() == VbusState::Present; // unknown is display-only false
    s.charging = PMU.isCharging();
    drawDebugDisplay(s);
}

// Each source has its own accepted raise sequence and may share a code with
// another source. Cache only complete atomic snapshots; contention never clears
// a fault or invents another episode. These buffers belong solely to core 1.
static display::Condition gDisplayConditions[3][64];
static size_t gDisplayConditionCounts[3] = {};
static diagnostics::RuntimeFaults::Observation gDisplayCopy[64];
static display::Condition gDisplayMerged[64];
static const display::Output& serviceProductionDisplay(bool render = true) {
    diagnostics::RuntimeFaults* sources[] = {&gLoopFaults, &gNetFaults, &gEventFaults};
    size_t merged = 0;
    for (unsigned source = 0; source < 3; ++source) {
        size_t count = 0;
        if (sources[source]->snapshot(gDisplayCopy, 64, count)) {
            gDisplayConditionCounts[source] = count;
            for (size_t i = 0; i < count; ++i)
                gDisplayConditions[source][i] = {gDisplayCopy[i].code,
                    gDisplayCopy[i].observed ? gDisplayCopy[i].episode : 0, uint8_t(source)};
        }
        for (size_t i = 0; i < gDisplayConditionCounts[source] && merged < 64; ++i)
            gDisplayMerged[merged++] = gDisplayConditions[source][i];
    }
    uint32_t now = millis();
    // Match the debug page's 2.5 s freshness bound, plus the fix's actual
    // accuracy decision. Do not promote stale or invalid RMC speed at boot.
    bool fresh = gHaveLastFix && uint32_t(now - gLastFixMonoMs) <= 2500 &&
                 gLatestSpeedTrusted && gps.speed.isValid() && gps.speed.age() <= 2500 &&
                 gps.location.isValid() && gps.location.age() <= 2500;
    display::DisplayModel::Input input;
    input.now = now;
    input.suspended = debugMode();
    input.motion = display::motion(gVbusLost, gState == IDLE,
        gState == ARMING || gState == ACTIVE || gState == COOLDOWN,
        fresh, gps.speed.mps(), END_MPS);
    input.tripFinalized = gDisplayTripFinalized; gDisplayTripFinalized = false;
    input.userRequest = gDisplayUserRequest; gDisplayUserRequest = false;
    input.usbPowerRequest = gDisplayUsbPowerRequest; gDisplayUsbPowerRequest = false;
    input.heard = gDebugGsv.signal(now).heard;
    input.conditions = gDisplayMerged; input.count = merged;
    const auto& output = gDisplayModel.update(input);
    if (render) drawProductionDisplay(output);
    return output;
}

// These functions execute only on the loop task/core 1. Core 0 can only
// observe gDebugMode; it never initializes a sensor or transfers OLED pixels.
static void toggleRuntimeDebug() {
    if (gDegraded.mode() != startup::Mode::Normal) return;
    bool enabled = !debugMode();
    gDebugMode.store(enabled);
    gDisplayModel.dismiss();
    gDisplayUserRequest = gDisplayTripFinalized = false;
    gDisplayUsbPowerRequest = false;
    gModeFeedback = true; gModeFeedbackMs = millis();
    // Drop an unfinished BME conversion; the next entry starts a fresh cycle.
    gDebugTempPending = false;
    gDebugTempBme = bme280_temp::Sensor();
    if (enabled) {
        gDebugWireReady = gProductionWireReady;
        if (!gDebugTempEnabled) beginDebugTemperature();
        gDebugTempLogMs = millis();
        reportEventHeap(); // current heap at debug entry, rather than a boot-time fiction
    }
    Serial.println(enabled ? "Debug mode: on" : "Debug mode: off");
    // Paint feedback now, without waiting for a later loop iteration.
    if (!gDebugDisplayFailed && !gProductionDisplayFailed) {
        if (gDisplayOwner == DisplayOwner::Debug) debugDisplayPowerSave(true);
        else productionDisplayPowerSave(true);
    }
    gDisplayOwner = DisplayOwner::Feedback;
    gProductionHaveOutput = false;
    if (!gDebugDisplayFailed && !gProductionDisplayFailed) drawDebugModeFeedback(enabled);
}

static void serviceDisplay() {
    if (gDegraded.mode() != startup::Mode::Normal) return;
    const auto& out = serviceProductionDisplay(false);
    if (gModeFeedback && uint32_t(millis() - gModeFeedbackMs) >= DEBUG_FEEDBACK_MS)
        gModeFeedback = false;
    // Either adapter failing disables the shared panel for this boot.
    if (gDebugDisplayFailed || gProductionDisplayFailed) return;
    DisplayOwner owner = gModeFeedback ? DisplayOwner::Feedback :
        (debugMode() && !out.powered ? DisplayOwner::Debug : DisplayOwner::Production);
    if (owner != gDisplayOwner) {
        if (gDisplayOwner == DisplayOwner::Debug) debugDisplayPowerSave(true);
        else productionDisplayPowerSave(true);
        if (gDebugDisplayFailed || gProductionDisplayFailed) return;
        gProductionHaveOutput = false;
        gDisplayOwner = owner;
        if (owner == DisplayOwner::Debug) {
            beginDebugDisplay(gProductionWireReady);
            gDebugDrawMs = millis() - 1000; // first frame immediately
            gDebugNeedsWake = true;
        }
    }
    if (owner == DisplayOwner::Debug) {
        serviceDebugDisplay();
        if (gDebugNeedsWake) {
            debugDisplayPowerSave(false); // only after drawing
            gDebugNeedsWake = false;
        }
    } else if (owner == DisplayOwner::Production) drawProductionDisplay(out);
}

// Shared boot producers, including failed startup's retained E2007 notice.
static void beginJournal() {
    // EventLog::begin is RAM-only. Disk journal loading is deferred to the
    // service task; this shared startup stage includes the NVS boot counter.
    startupStage(startup_watchdog::Stage::Journal);
    gJournal.~EventLog();
    new (&gJournal) diagnostics::EventLog(gSdMounted ? &gSdJournalFs : nullptr,
                                         gFlashMounted ? &gFlashJournalFs : nullptr, diagnostics::uploadJournalConfig(&gRevisionState));
    gJournalBoot = diagnostics::newJournalBoot(!gStartupWatchdog.noStorage());
    gJournalReady = gJournal.begin(gJournalBoot.id, gJournalBoot.counter, firmware::kVersion);
    gJournal.setTimeProvider(stampDiagnosticTime);
    if (gJournalReady) diagnostics::submitJournalBoot(gJournal, esp_reset_reason(), gPmuStartup, millis(), gBootBattery);
}

static bool createFsMutex() {
    gFsMutex = xSemaphoreCreateMutex();
    if (gFsMutex) return true;
    Serial.println("ERROR: LittleFS mutex allocation failed. Restarting.");
    delay(1000);
    ESP.restart();
    return false;
}

static void enterDegraded(startup::Mode mode) {
    gDegraded.begin(mode, millis());
    // Setup initialized this independent Wire adapter asleep, even if the
    // PMU or storage failed. No display service runs in the degraded loop.
    productionDisplayPowerSave(true);
    if (mode == startup::Mode::NoStorage) {
        // No mounted store: boot and faults stay in the bounded RAM queue.
        beginJournal();
        gLoopFaults.event(4001, millis()).condition(true);
        gLoopFaults.event(4002, millis()).condition(true);
        gLoopFaults.event(4003, millis()).condition(true);
        Serial.println(gStartupWatchdog.noStorage() ?
            "DEGRADED: no trip storage; power management and charge guard active. Storage recovery disabled until next power-on." :
            "DEGRADED: no usable trip storage -- not recording trips. Power management and charge guard active. Retrying storage in 60 s.");
        // The loop retries an implausible first conversion without blocking.
        serviceChargeTemp();
    } else {
        if (mode == startup::Mode::PmuUntrusted) {
            // The only extra PMU write allowed with untrusted rail state.
            PMU.disableCellbatteryCharge();
            // Same register/bit used by XPowersLib's disable method. Read the
            // raw result so an I2C failure cannot look like a cleared bit.
            int charge = PMU.readRegister(XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL);
            Serial.printf("DEGRADED: LoRa rail (ALDO3) could not be verified off. Charge-disable attempted once; %s.\n",
                          charge < 0 ? "charger read-back failed, state unknown" :
                          (charge & 0x02) ? "read-back still shows charging enabled" :
                                            "read-back shows charging disabled");
        }
        if (!createFsMutex()) return;
        // Internal flash only, journal only. Never provision/format or use SD.
        startupStage(startup_watchdog::Stage::FlashMount);
        gFlashMounted = !gStartupWatchdog.noStorage() && LittleFS.begin(false, "/littlefs", 10, TRIP_PARTITION_LABEL);
        beginJournal();
        gLoopFaults.event(2001, millis()).condition(true);
        Serial.println(mode == startup::Mode::PmuMissing ?
            "DEGRADED: power chip not responding -- not recording; no charge or rail control. Retrying in 60 s." :
            "DEGRADED: power chip state untrusted -- not recording; no further charge or rail control. Retrying in 60 s.");
    }
    finishStartupWatchdog(); // degraded boots have a loop task too
}

static void degradedLoop() {
    using startup::Mode;
    const Mode mode = gDegraded.mode();
    if (mode == Mode::NoStorage) {
        pollPmu();
        serviceVbusRecheck();
        confirmVbusOff();
        serviceChargeTemp();
        serviceBatteryDiagnostics();
        if (!gVbusLost) gIdleSince = millis();
        else if (uint32_t(millis() - gIdleSince) >= IDLE_BATT_SLEEP_MS) powerOffForVbus();
        gLoopFaults.event(4001, millis()).condition(true);
        gLoopFaults.event(4002, millis()).condition(true);
        gLoopFaults.event(4003, millis()).condition(true);
    } else {
        gLoopFaults.event(2001, millis()).condition(true);
    }
    gLoopFaults.event(6001, millis()).condition(gProductionDisplayFailed);
    serviceJournal(); // single consumer; no network task in any degraded mode
    if (!(mode == Mode::NoStorage && gStartupWatchdog.noStorage()) && gDegraded.retryDue(millis())) {
        bool recovered = false;
        if (mode == Mode::NoStorage) {
            // No format, fsck, trip activity or journal migration on this probe.
            recovered = mountTripStorage(false, false);
            if (gSdMounted) { SD.end(); gSdSpi.end(); }
            if (gFlashMounted) LittleFS.end();
            gSdMounted = gFlashMounted = false;
        } else {
            PmuStartupResult retry;
            // Release Wire1 before GPIO bus recovery on a later cycle.
            if (Wire1.end()) {
                recovered = beginPmuWithRecovery(Wire1, PMU, I2C_SDA, I2C_SCL, retry);
                if (recovered && mode == Mode::PmuUntrusted)
                    recovered = PMU.disableALDO3() && aldo3State() == Aldo3State::Off;
            }
        }
        if (gDegraded.retryResult(millis(), recovered)) {
            Serial.println("DEGRADED: initialization retry succeeded; restarting into normal startup checks.");
            ESP.restart();
            return;
        }
        Serial.printf("DEGRADED (%s): retry failed -- not recording trips. Retrying in %lu s.\n",
                      startup::modeText(mode), (unsigned long)(gDegraded.waitMs() / 1000));
    }
    delay(20); // yield even without UART, display or network activity
}

void setup() {
    // VBUS-insert power-on brings the Supreme's LoRa rail up ON. Disable
    // and verify it as the first PMU operation after bounded bus recovery
    // and initialization, BEFORE Serial, storage or GPS setup can block.
    bool pmuReady = beginPmuWithRecovery(Wire1, PMU, I2C_SDA, I2C_SCL, gPmuStartup);
    bool radioOff = pmuReady && PMU.disableALDO3() && aldo3State() == Aldo3State::Off;
    // Bounded PMU/bus recovery and the radio-off safety command come first.
    // Enroll before display, USB waits, filesystems, NVS or any storage call.
    startup_watchdog::Record saved{gStartupWatchdogRecord.magic, gStartupWatchdogRecord.stage,
        gStartupWatchdogRecord.hangs, gStartupWatchdogRecord.fallback, gStartupWatchdogRecord.checksum};
    auto reason = esp_reset_reason();
    gStartupWatchdog.boot(saved, reason == ESP_RST_POWERON,
        reason == ESP_RST_TASK_WDT || reason == ESP_RST_INT_WDT || reason == ESP_RST_WDT);
    saveStartupWatchdog();
    armLoopWatchdog(startup_watchdog::STARTUP_WDT_TIMEOUT_MS, false);
    bool aldo1Warning = false;
    gBootBattery = diagnostics::BatterySample();
    if (radioOff) {
        // Stop retained charging before display/USB/storage waits. Only trusted
        // PMU startup may do this; degraded power modes retain their policy.
        PMU.disableCellbatteryCharge();
        PMU.enableBattDetection();
        PMU.enableBattVoltageMeasure();
        // ALDO1 supplies the OLED/BME280/magnetometer and Wire pull-ups.
        int aldo1Control = PMU.readRegister(XPOWERS_AXP2101_LDO_ONOFF_CTRL0);
        int aldo1Voltage = PMU.readRegister(XPOWERS_AXP2101_LDO_VOL0_CTRL);
        bool aldo1WasReady = pmu_read::aldo1Ready(aldo1Control, aldo1Voltage);
        bool voltageSet = PMU.setALDO1Voltage(3300);
        bool enabled = PMU.enableALDO1();
        aldo1Control = PMU.readRegister(XPOWERS_AXP2101_LDO_ONOFF_CTRL0);
        aldo1Voltage = PMU.readRegister(XPOWERS_AXP2101_LDO_VOL0_CTRL);
        aldo1Warning = !voltageSet || !enabled || !pmu_read::aldo1Ready(aldo1Control, aldo1Voltage);
        // No 250 ms off/on cycle: the reported failures were cold power-ons.
        // 10 ms is a conservative guess for the LDO ramp and local supply
        // capacitors to settle before I2C, not a measured board requirement.
        // Unknown initial state may also mean the rail changed or enabled.
        if (!aldo1WasReady) delay(10);
    }
    // SH1106 can retain its state across an ESP32 reset. Put it to sleep
    // before either degraded startup branch; never touch the PMU's Wire1.
    if (!gStartupWatchdog.noStorage()) beginProductionDisplay();
    gDebugMode.store(false); // boot-local, never restored from NVS
    pinMode(DEBUG_BOOT_PIN, INPUT_PULLUP); // ROM has already sampled the strap
    gBootButton = runtime_debug::BootButton();
    gBootButton.sample(millis(), digitalRead(DEBUG_BOOT_PIN) == LOW);
    startup::Mode powerMode = startup::selectMode(pmuReady, radioOff, true);
    if (powerMode != startup::Mode::Normal) {
        Serial.begin(115200);
        delay(1000); // Allow native USB to become ready before boot messages.
        Serial.println("\n=== Stage 4: Trip Logger ===");
        Serial.printf("Firmware: %s\n", firmware::kVersion);
        printStartupWatchdog();
        printPmuStartup(gPmuStartup);
        enterDegraded(powerMode);
        return;
    }
    // BLDO1 powers the SD slot but starts ON at 1.8 V. Disable it early;
    // mountSdStorage sets 3.3 V before enabling it, after safety setup.
    PMU.disableBLDO1();
    // Supreme GPS backup RAM comes straight from the 18650. LilyGO also
    // disables VBACKUP; a 60 s ALDO4-off test hot-started in 2.0 s with it off.
    PMU.disableButtonBatteryCharge();
    // ALDO1 is set to 3.3 V above. Leave ALDO2 and DCDC4/DCDC5 as found.
    // Peripheral loads and PMU/RTC bus pull-up dependencies need proof
    // before disabling them.
    // Never touch DCDC1 (ESP32).

    Serial.begin(115200);
    if (!gStartupWatchdog.noStorage()) gRtc.begin(Wire1, gDiagnosticClock);
    delay(1000);
    // ADC has had a conversion interval, charger still disabled. Retain this
    // early sample across storage startup; never re-read it after charge guard.
    gBootBattery = diagnostics::readBatterySample(PMU);
    Serial.println("\n=== Stage 4: Trip Logger ===");
    Serial.printf("Firmware: %s\n", firmware::kVersion);
    printStartupWatchdog();
    printPmuStartup(gPmuStartup);
    if (aldo1Warning)
        Serial.println("WARNING: ALDO1 3.3 V/on setup unconfirmed; OLED/BME280/magnetometer may be unavailable.");
    gRtc.printBootStatus();
    Serial.println("LoRa rail (ALDO3) verified off; SD rail (BLDO1) off; VBACKUP off.");
    Serial.printf("PMU power-on source 0x%02X, power-off source 0x%02X.\n",
                  (unsigned)PMU.getPowerOnSource(), (unsigned)PMU.getPowerOffSource());

    if (!createFsMutex()) return;

    // --- Supreme GPS: ALDO4 at 3.3 V, GPS_EN HIGH as in LilyGO's setup ---
    pinMode(GPS_EN_PIN, OUTPUT);
    digitalWrite(GPS_EN_PIN, HIGH);
    PMU.setALDO4Voltage(3300);
    PMU.enableALDO4();
    // Full-charge voltage 4.1 V instead of the 4.2 V default: ~10% less
    // capacity, much less stress on a cell that lives in a hot car (the
    // charge temperature guard, below, covers charging when it's too hot).
    PMU.setChargeTargetVoltage(XPOWERS_AXP2101_CHG_VOL_4V1);
    // No charging until the temperature guard's first check. The PMU keeps
    // the charger on across ESP32 resets, and storage setup (mount, fsck)
    // can halt or crash before that check runs.
    // Enable the measurements the battery/VBUS predicates and the charge
    // temperature guard rely on.
    PMU.enableTemperatureMeasure();
    PMU.enableVbusVoltageMeasure();
    PMU.enableSystemVoltageMeasure();
    setupPmuIrq();
    // Seed VBUS state from hardware: a battery-only boot (e.g. wake via
    // power key away from the car) must still hit the idle power-off timer.
    seedVbusState();
    gIdleSince = millis();
    Serial.printf("PMU ready, MAX-M10S GPS rail (ALDO4) on at 3.3 V, charge to %s. VBUS %s.\n",
                  PMU.getChargeTargetVoltage() == XPOWERS_AXP2101_CHG_VOL_4V1 ? "4.1 V" : "NOT 4.1 V",
                  gVbusRetry == VbusRetry::Seed ? "unknown (assuming USB present)" :
                  gVbusLost ? "absent (on battery)" : "present");

    // --- Storage + boot-time repair ---
    // A latched no-storage fallback returns false here before any mount, so
    // it takes the same degraded path, with the same PMU setup, as a failure.
    if (!mountTripStorage()) {
        enterDegraded(startup::Mode::NoStorage);
        return;
    }
    startupStage(startup_watchdog::Stage::Services);
    // --- GPS UART. setRxBufferSize MUST precede begin(). ---
    GPSSerial.setRxBufferSize(2048);
    GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);

    prefs.begin("mileage", false);
    beginJournal();
    rtcConditions();
    if (gJournalReady) {
        // Explicit healthy observations resolve prior degraded-boot alerts.
        gLoopFaults.event(2001, millis()).condition(false);
        gLoopFaults.event(4003, millis()).condition(false);
        gLoopFaults.event(4001, millis()).condition(!gSdMounted);
        startupStage(gSdMounted ? startup_watchdog::Stage::CardSpace : startup_watchdog::Stage::FlashSpace);
        storageConditions(storageFreeBytes(*gTripFS), millis());
    }
    if (gSdMounted) { startupStage(startup_watchdog::Stage::CardRepair); fsckAllTrips(SD); }
    if (gFlashMounted) { startupStage(startup_watchdog::Stage::FlashRepair); fsckAllTrips(LittleFS); }
    startupStage(startup_watchdog::Stage::Services);

    // Start a bounded readiness wait; loop() receives NMEA and configures
    // the receiver without blocking trip logging or power/charge checks.
    configureGps();

    // First charge-temperature check now, not 30 s in: charging has been off
    // since PMU setup. A cold-start ADC can still be unready: the loop retries
    // once a second for ten seconds, keeping charging off until a valid read.
    serviceChargeTemp();

    initEventUpload();
    // --- Networking task on core 0; GPS/FSM stay on core 1 (loop) ---
    if (xTaskCreatePinnedToCore(networkTask, "net", NET_STACK_BYTES,
                               nullptr, 1, nullptr, 0) != pdPASS) {
        Serial.println("ERROR: network task creation failed; trips remain queued for a later boot.");
    }

    Serial.println("Setup complete. Logging trips.");
    finishStartupWatchdog();
}

void loop() {
#ifdef WATCHDOG_BENCH_HANG
    // Deliberately no feed: the next watchdog-reset boot skips this hook,
    // runs ordinary fsck, records E1003 and continues instead of reset-looping.
    if (std::strcmp(diagnostics::journalResetReason(esp_reset_reason()), "watchdog") != 0) {
        Serial.println("WATCHDOG BENCH: blocking loop until watchdog reset.");
        for (;;) vTaskDelay(pdMS_TO_TICKS(1000));
    }
#endif
    if (gDegraded.mode() != startup::Mode::Normal) {
        degradedLoop();
        return;
    }
    serviceRtc();
    // Core 1: one UART reader routes NMEA/fixes and checksum-checked UBX
    // ACKs. Configuration deadlines never block receive or trip processing.
    serviceGpsSpeedFallback();
    while (GPSSerial.available() > 0) {
        receiveGpsByte((uint8_t)GPSSerial.read());
    }
    serviceGpsConfiguration();
    serviceDebugRf();
    serviceRuntimeDiagnostics(); // before fix-loss finalization and PMU shutdown

    // ARMING must also expire when no next fix arrives to drive the FSM.
    if (gState == ARMING && millis() - gLastFixMonoMs > ARM_FIX_LOSS_MS) {
        gState = IDLE;
        armBufReset();
        gPendCount = gPendHead = 0;
    }

    // Fix-loss watchdog: a recording trip with no valid fix at all for
    // FIX_LOSS_END_MS (parked indoors, antenna failure) is finalized so
    // it can upload, instead of staying open until a power event.
    if ((gState == ACTIVE || gState == COOLDOWN) && gHaveLastFix &&
        millis() - gLastFixMonoMs > FIX_LOSS_END_MS) {
        Serial.println("No GPS fix for 5 min during trip -- finalizing.");
        endTrip(/*discardIfMicro=*/true);
    }

    // Service power events (VBUS loss/insert, key press, low battery).
    pollPmu();
    serviceVbusRecheck();
    confirmVbusOff();
    serviceChargeTemp();

    // Battery idle power-off: on battery with no trip in progress for
    // IDLE_BATT_SLEEP_MS, PMU shutdown. The 10 min
    // grace gives the network task ample time to upload after a drive.
    // A VBUS insert edge powers us on when the car turns back on.
    if (!gVbusLost || gState != IDLE) {
        gIdleSince = millis();
        gNetHold.store(false);
    } else if (millis() - gIdleSince >= IDLE_BATT_SLEEP_MS) {
        // Not under a running upload (issue #3): the network task finishes
        // its current file and stops, and we power off once it's idle. Logging
        // carries on while it finishes, and power or a trip coming back
        // cancels the wait. A file still going after NET_DRAIN_MS is cut off.
        if (!gNetHold.exchange(true) && gNetBusy.load())
            Serial.println("Idle on battery -- letting the upload finish before powering off.");
        if (!gNetBusy.load() || millis() - gIdleSince >= IDLE_BATT_SLEEP_MS + NET_DRAIN_MS)
            powerOffForVbus();
    }
    serviceDisplay();
    if (debugMode()) serviceDebugTemperature();
    if (GPSSerial.available() == 0) vTaskDelay(pdMS_TO_TICKS(LOOP_IDLE_MS));
}
