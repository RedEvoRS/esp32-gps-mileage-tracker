// =================================================================
// Stage 4: Trip Logger (GPX logging + WiFi upload)
// =================================================================
// This is the real firmware. It ties together everything proven in
// Stages 1-3 (toolchain, AXP2101 PMU + GPS power, NMEA parsing) and
// adds the four pieces that make it a mileage tracker:
//
//   1. A trip state machine that auto-detects start (>5 mph for 10 s)
//      and end (<2 mph for 3 min), with hysteresis so stop-and-go
//      traffic doesn't split one drive into many.
//   2. GPX logging to microSD (LittleFS fallback) -- one line per timed fix,
//      file.close() after every write to limit power-cut data loss.
//      The latest 90 untimed points are buffered in RAM; older ones spill
//      to flash without times. Boot-time "fsck" attempts
//      repair; a maximum loss bound still needs hardware validation.
//   3. A FreeRTOS networking task pinned to core 0 that, when parked on
//      home WiFi, uploads finished .gpx files to the ingest server and
//      deletes them locally on success. GPS parsing stays on core 1 and
//      is never blocked by networking.
//   4. AXP2101 VBUS-loss handling: when the car turns off (USB power
//      goes away), finalize the current trip cleanly and keep the file
//      for the next time we're home.
//
// Time comes from the GPS (UTC). There is no battery-backed wall clock,
// so until the first fix we buffer points by millis() and back-date them
// once UTC arrives. Fresh GPS time corrects the system clock every minute.
//
// Required libraries (U8g2 is only needed with DISPLAY_DEBUG):
//   XPowersLib (lewisxhe), TinyGPSPlus (Mikal Hart). WiFi, HTTPClient,
//   SD, SPI, LittleFS, and Preferences ship with the ESP32 Arduino core.
//   U8g2 2.36.18 (olikraus) drives the optional test-drive OLED page.
//
// PARTITIONS: partitions.csv is unchanged (no OTA, 2.375 MB LittleFS
// fallback, about 5-6 driving hours). SD requires FAT32; never auto-format.
// New trips use SD when it mounts at boot. Both mounted stores are repaired
// and drained, SD first, so flash trips from a previous boot are not stranded.
//
// NOTE: compiles on ESP32 Arduino core 3.3.11 (XPowersLib 0.3.3,
// TinyGPSPlus 1.0.3) but has not yet had its on-hardware shakedown --
// do that pass before trusting it in the car (see the README).
// 2026-06-09 review fixes: per-chunk FS locking during upload, battery
// idle deep-sleep, ARMING-phase point capture, stationary-jitter distance
// gate, fix-loss trip finalization. See CODE_REVIEW_2026-06-09.md.
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
#include <atomic>

#include "secrets.h"
#include "pmu_startup_event.h"

#ifndef DISPLAY_DEBUG
#define DISPLAY_DEBUG 1
#endif
#if DISPLAY_DEBUG
#include "debug_display.h"
#include "debug_gsv.h"
#include "debug_rf.h"
#endif

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
// SD wiring still needs Stage 2 d on the bench. Board uses QSPI, not octal PSRAM.
static constexpr int SD_MOSI = 35, SD_SCK = 36, SD_MISO = 37, SD_CS = 47, IMU_CS = 34;
static constexpr uint32_t SD_SPI_HZ = 4000000; // core default; ample for ~119 B/s GPX
static constexpr uint32_t SD_SETTLE_MS = 250;

// -----------------------------------------------------------------
// Trip state machine tuning (from brief section 5)
// -----------------------------------------------------------------
static constexpr float    START_MPS       = 2.235f;          // 5 mph
static constexpr float    END_MPS         = 0.894f;          // 2 mph
static constexpr uint32_t ARM_MS          = 10UL  * 1000;    // sustain to start
static constexpr uint32_t COOL_MS         = 180UL * 1000;    // sustain to end
// First guesses: calibrate from drive/indoor logs; err low on mileage.
static constexpr float    SPEED_ACC_ARM_MPS = 0.5f;
static constexpr float    SPEED_ACC_TRIP_MPS = 1.0f;
static constexpr float    HACC_ARM_M      = 5.0f;
static constexpr float    HACC_TRIP_M     = 10.0f;
static constexpr unsigned RESUME_FIXES    = 3; // consecutive trusted moving fixes
// Arrival-time association, allowing either UBX/NMEA ordering at 1 Hz.
static constexpr uint32_t NAV_PVT_FRESH_MS = 1500;
static constexpr uint32_t NAV_PVT_FALLBACK_MS = 10000;
static constexpr float    MIN_TRIP_METERS = 160.0f;          // ~0.1 mi
static constexpr uint32_t MIN_TRIP_MS     = 30UL  * 1000;
static constexpr size_t   TRIP_SPLIT_BYTES = 8 * 1024 * 1024; // half the server cap
static constexpr size_t   TRKPT_LINE_BYTES = 256;            // includes terminating NUL

// Fix-loss heuristic: gap this long between valid fixes starts a new
// <trkseg> so viewers don't draw a straight line across a tunnel.
static constexpr uint32_t SEG_GAP_MS      = 5000;

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

// How often the networking task wakes to look for pending uploads.
static constexpr uint32_t NET_TICK_MS     = 30000;
static constexpr uint32_t NET_RETRY_MAX_MS = 5UL * 60 * 1000;
// Restore the brief's conservative allocation until measured on hardware.
// ESP-IDF task stack sizes and high-water marks are in bytes, not words.
static constexpr uint32_t NET_STACK_BYTES = 12288;
static constexpr uint32_t NET_STACK_WARN_BYTES = 1024;

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

#if DISPLAY_DEBUG
static DebugGsv gDebugGsv;
static DebugRf gDebugRf;
static uint32_t gDebugRfPollMs = 0;
static bool gDebugRfStarted = false;
static bool gDebugRmcValid = false;
// Core 0 publishes a coherent network snapshot; core 1 only copies it.
// Critical sections contain just scalar copies, never I/O or formatting.
static portMUX_TYPE gDebugNetMux = portMUX_INITIALIZER_UNLOCKED;
static DebugNetwork gDebugNet;
#endif

SemaphoreHandle_t gFsMutex;     // serializes ALL filesystem access
SPIClass          gSdSpi(HSPI); // dedicated bus instance; radio is never initialized
fs::FS*           gTripFS = &LittleFS;
// Store membership is fixed for this boot, including recovery after unmount.
// Do not switch destinations mid-trip or remount an absent card at runtime.
bool              gSdMounted = false, gFlashMounted = false;
volatile bool     gVbusLost   = false;
volatile bool     gPmuIrqFlag = false;
// Shared by core 0 (SNTP) and core 1 (GPS/fix construction).
std::atomic<bool> gClockSet{false};  // a source has synchronized UTC this boot
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
static constexpr uint32_t GPS_ACK_MS = 300;
static constexpr uint8_t GPS_CONFIG_ATTEMPTS = 5;
static constexpr uint8_t GPS_COMMAND_SENDS = 3;
enum GpsConfigState { GPS_CONFIG_OFF, GPS_WAIT_READY, GPS_WAIT_ACK, GPS_RETRY, GPS_CONFIG_DONE };
struct GpsConfig {
    GpsConfigState state = GPS_CONFIG_OFF;
    uint32_t since = 0, started = 0;
    uint8_t attempts = 0, command = 0, sends = 0;
    int8_t ack = -1;
    bool readyLogged = false, retryLogged = false;
};
static GpsConfig gGpsConfig;

// Demultiplex UBX before TinyGPSPlus. Both builds retain NAV-PVT and ACKs;
// the same bounded buffer also accommodates the debug MON-RF path.
struct GpsUbxRx {
    uint32_t pos = 0, since = 0;
    uint16_t len = 0;
    uint8_t cls = 0, id = 0, a = 0, b = 0;
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

#define FS_LOCK()   xSemaphoreTake(gFsMutex, portMAX_DELAY)
#define FS_UNLOCK() xSemaphoreGive(gFsMutex)

// A single GPS fix, with both a monotonic and (once known) a UTC stamp.
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

// Fixes seen while ARMING, so the first ~10 s of a trip isn't lost.
// One fix per 1 Hz epoch makes a 10 s arm window 10-11 fixes; 16 slots is
// comfortable headroom. Ring: oldest overwritten on overflow.
static Fix    gArmBuf[16];
static size_t gArmHead = 0, gArmCount = 0;

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
static float     gMaxSpeedMps = 0;
static char      gTripPath[48] = {0};   // active file, "" when none
static bool      gTripWriteFailed = false;  // a write to gTripPath didn't land
static bool      gHaveLastFix = false;
static Fix       gLastFix;
static bool      gDistanceContinuous = true; // never bridge across a rejected point
// Every valid RMC, including rejected speed: separate from last accepted point.
static uint32_t  gLastFixMonoMs = 0;

#if DISPLAY_DEBUG
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
#endif

// =================================================================
// Time helpers
// =================================================================
static uint64_t nowEpochMs() {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
}

// Format epoch milliseconds as ISO 8601 UTC, e.g. 2026-04-14T14:30:15Z.
static void isoFromEpochMs(uint64_t ms, char* out, size_t n) {
    time_t secs = (time_t)(ms / 1000);
    struct tm tmv;
    gmtime_r(&secs, &tmv);
    if (n && strftime(out, n, "%Y-%m-%dT%H:%M:%SZ", &tmv) == 0) out[0] = 0;
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
static void setClockFromGps() {
    if (!gps.date.isUpdated()) return;
    // TinyGPSPlus renews cached date/time even when an RMC field is empty.
    // Check the sentence's raw fields before trusting its parsed values.
    bool rawGN = validRmcClockFields(gRmcTimeGN, gRmcDateGN);
    bool rawGP = validRmcClockFields(gRmcTimeGP, gRmcDateGP);
    bool fresh = (rawGN || rawGP) && gps.date.isValid() && gps.time.isValid() &&
                 gps.time.isUpdated() && gps.date.age() < 2000 && gps.time.age() < 2000;
    struct tm tmv = {0};
    tmv.tm_year = gps.date.year() - 1900;
    tmv.tm_mon  = gps.date.month() - 1;
    tmv.tm_mday = gps.date.day();
    tmv.tm_hour = gps.time.hour();
    tmv.tm_min  = gps.time.minute();
    tmv.tm_sec  = gps.time.second();
    unsigned subsecondMs = gps.time.centisecond() * 10 + gps.time.age();
    if (!fresh || tmv.tm_year < 124 || tmv.tm_mon < 0 || tmv.tm_mon > 11 ||
        tmv.tm_mday < 1 || tmv.tm_mday > 31 || tmv.tm_hour > 23 ||
        tmv.tm_min > 59 || tmv.tm_sec > 59) return;
    uint32_t now = millis();
    if (gGpsClockSet && now - gLastGpsSyncMs < GPS_CLOCK_SYNC_MS) return;
    time_t t = timegmUtc(&tmv);
    struct timeval tv = { .tv_sec = t + subsecondMs / 1000,
                          .tv_usec = (long)(subsecondMs % 1000) * 1000 };
    if (settimeofday(&tv, nullptr) != 0) return;
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
        char iso[24];
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

// Write all buffered fixes to the open trip file, back-dating each from
// the now-known UTC and its monotonic stamp. Called once, the first time
// UTC becomes available during an active trip -- or when a trip that never
// got UTC is closed. Those points get no time at all: until a source has
// synchronized it, the system clock reads 1970 or a date carried over deep
// sleep, and a made-up date can file the miles under the wrong day or year.
static void flushPending(FileWriter& w) {
    // Read the flag before the clock: core 0 sets the clock from NTP first,
    // then the flag.
    bool trusted = gClockSet.load();
    uint64_t utcNow = nowEpochMs();
    uint32_t monoNow = millis();
    for (size_t i = 0; i < gPendCount; ++i) {
        Fix p = gPend[(gPendHead + i) % PENDING_CAPACITY];
        p.hasUtc = trusted;
        p.utcMs  = trusted ? utcNow - (uint64_t)(monoNow - p.monoMs) : 0;
        appendTrkpt(w, p);
    }
    gPendCount = gPendHead = 0;
}

// =================================================================
// Trip lifecycle
// =================================================================
// Reserve the pending tail, one more point (including a segment break),
// and the closing tags. A point is at most 255 bytes, even when back-dated
// with UTC. Checking after each fix leaves the complete file below 8 MiB.
static bool tripShouldSplit(size_t bytes, size_t pendingCount) {
    return bytes + (pendingCount + 1) * (TRKPT_LINE_BYTES - 1) +
           strlen(GPX_EPILOGUE) >= TRIP_SPLIT_BYTES;
}

// Only called before workers start or with FS_LOCK held. SD capacity must
// stay 64 bit: size_t on the ESP32 cannot represent a 32 GB card.
static uint64_t storageFreeBytes(fs::FS& storage) {
    uint64_t total = &storage == &SD ? SD.totalBytes() : LittleFS.totalBytes();
    uint64_t used = &storage == &SD ? SD.usedBytes() : LittleFS.usedBytes();
    return used < total ? total - used : 0;
}

// Takes the FS lock; callers must not hold it.
static uint64_t fsFreeBytes() {
    FS_LOCK();
    uint64_t free = storageFreeBytes(*gTripFS);
    FS_UNLOCK();
    return free;
}

// True if there's room to start a trip. Logs (at most once a minute while
// driving) when there isn't, since otherwise the miles vanish silently.
static bool roomForNewTrip(uint32_t now) {
    uint64_t freeB = fsFreeBytes();
    if (freeB >= FS_START_FREE) return true;
    if (!gFullWarned || now - gFullWarnT0 >= 60000) {
        Serial.printf("ERROR: trip storage full (%llu bytes free) -- NOT recording. "
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
    if (freeB >= FS_STOP_FREE) return false;
    Serial.printf("ERROR: trip storage nearly full (%llu bytes free) -- ending trip.\n",
                  (unsigned long long)freeB);
    return true;
}

// Returns false -- and leaves no file and no active trip behind -- if the
// trip file can't be created and written. The caller must then stay IDLE:
// carrying on as ACTIVE would let FILE_APPEND create a file with no
// prologue, which uploads as invalid GPX and is rejected forever.
static bool beginTrip(const Fix& fx) {
    fs::FS& storage = *gTripFS;
    gTripMeters = 0;
    gMaxSpeedMps = fx.speedMps;
    gTripBytes = 0;
    gTripWriteFailed = false;
    gTripT0 = gArmCount ? gArmBuf[gArmHead].monoMs : fx.monoMs;
    gLastMoveMs = fx.monoMs;

    FS_LOCK();
    makeTripPath(gTripPath, sizeof(gTripPath));
    bool ok = false;
    File f = storage.open(gTripPath, FILE_WRITE);
    if (f) {
        FileWriter w(f);
        w.put(GPX_PROLOGUE);
        // Write the fixes captured during ARMING (includes the current
        // fix, pushed by the FSM before calling us) so the first ~10 s
        // of the drive is in the file. Pre-UTC fixes go to the pending
        // buffer and are back-dated when UTC arrives, same as any other.
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
        if (b.speedMps > gMaxSpeedMps) gMaxSpeedMps = b.speedMps;
    }
    armBufReset();
    gCoolT0 = fx.monoMs;
    gFsCheckT0 = fx.monoMs;
    gFullWarned = false;
    Serial.printf("Trip started: %s\n", gTripPath);
    return true;
}

// Append a point to the active trip, buffering instead if UTC isn't
// available yet. If the buffer is full, commit its oldest point untimed
// before accepting this one. Once UTC arrives, back-date the buffered tail;
// points already committed without times stay untimed.
// Returns false if the write didn't reach flash; the caller must end the
// trip, since a partial line may now sit at the tail of the file.
static bool writeOrBufferTrkpt(const Fix& fx) {
    fs::FS& storage = *gTripFS;
    if (gTripPath[0] == 0) return true;
    if (!fx.hasUtc && gPendCount < PENDING_CAPACITY) {
        pushPending(fx);
        return true;
    }
    FS_LOCK();
    bool ok = false;
    File f = storage.open(gTripPath, FILE_APPEND);
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
    if (gTripPath[0] == 0) { gState = nextState; return; }

    // Duration runs to the last moving fix: the 3 min parked cooldown (or
    // the wait before a fix-loss finalize) that ends every trip would
    // otherwise pass MIN_TRIP_MS on its own. A stop mid-trip still counts.
    uint32_t tripMs = gLastMoveMs - gTripT0;
    bool micro = (tripMs < MIN_TRIP_MS) ||
                 (gTripMeters < MIN_TRIP_METERS);

    FS_LOCK();
    if (discardIfMicro && micro) {
        Serial.printf("Discarding micro-trip (%.0f m, %lu ms)\n",
                      gTripMeters, (unsigned long)tripMs);
        storage.remove(gTripPath);
    } else {
        bool ok = false;
        if (!gTripWriteFailed) {
            File f = storage.open(gTripPath, FILE_APPEND);
            // Same guard as writeOrBufferTrkpt: never turn a missing trip
            // file into one holding only the epilogue.
            if (f && f.size() > 0) {
                FileWriter w(f);
                // Flush any still-buffered (never-got-UTC) points before
                // closing, so a trip that never got UTC isn't lost. They're
                // written without times unless the clock is now trusted.
                if (gPendCount > 0) flushPending(w);
                w.put(GPX_EPILOGUE);
                ok = w.commit();
            }
            if (f) f.close();
        }
        // A failed write may have left a partial line at the tail. Don't
        // append after it: trim back to the last complete point and close
        // the document there. If even that fails (e.g. no room for the
        // repair copy) the file stays incomplete -- never uploaded -- and
        // the boot-time fsck retries it.
        if (!ok) ok = repairIfNeeded(storage, gTripPath);

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
    gState = nextState;
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
    if (speed > gMaxSpeedMps) gMaxSpeedMps = speed;

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
            if (roomForNewTrip(now) && beginTrip(fx)) {
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
static long findLastTrkptEnd(File& f) {
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
            }
        }
        carryLen = min(tl - 1, rl);
        memmove(region, region + rl - carryLen, carryLen);
        pos += n;
    }
    return last;
}

// Compare only verified bytes; a short read must never authorize cleanup.
static bool sameFilePrefix(File& a, File& b, size_t bytes) {
    if (!a.seek(0) || !b.seek(0)) return false;
    uint8_t ab[128], bb[128];
    while (bytes) {
        size_t n = min(bytes, sizeof(ab));
        if (a.read(ab, n) != (int)n || b.read(bb, n) != (int)n ||
            memcmp(ab, bb, n) != 0) return false;
        bytes -= n;
    }
    return true;
}

static bool fileMatchesText(File& f, const char* text, size_t bytes) {
    uint8_t buf[128];
    while (bytes) {
        size_t n = min(bytes, sizeof(buf));
        if (f.read(buf, n) != (int)n || memcmp(buf, text, n) != 0) return false;
        text += n;
        bytes -= n;
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
static bool recoverRepairCopy(fs::FS& storage, const char* path) {
    String tmp = String(path) + ".fix";
    if (!storage.exists(tmp.c_str())) return true;
    if (storage.exists((String(path) + ".bad").c_str())) {
        Serial.printf("fsck: retained %s (quarantine collision)\n", tmp.c_str());
        return false;
    }
    File copy = storage.open(tmp.c_str(), FILE_READ);
    if (!copy) return false;
    if (!storage.exists(path)) {
        // A historical interrupted replacement may have left this as the
        // only copy. Rename without copying, then use normal repair below.
        // An incomplete/unknown header or no complete point is not safe to
        // feed to the normal garbage-removal path, so preserve those bytes.
        bool recoverable = fileMatchesText(copy, GPX_PROLOGUE, strlen(GPX_PROLOGUE)) &&
                           findLastTrkptEnd(copy) > (long)strlen(GPX_PROLOGUE);
        copy.close();
        if (recoverable && storage.rename(tmp.c_str(), path)) {
            Serial.printf("fsck: recovered orphan %s\n", path);
            return true;
        }
        Serial.printf("fsck: retained orphan %s\n", tmp.c_str());
        return false;
    }

    File original = storage.open(path, FILE_READ);
    if (!original) { copy.close(); return false; }
    const size_t copySize = copy.size();
    bool redundant = copySize <= original.size() && sameFilePrefix(original, copy, copySize);
    bool finishedRepair = false;
    if (!redundant) {
        long end = findLastTrkptEnd(original);
        size_t suffixSize = 1 + strlen(GPX_EPILOGUE);
        if (end > 0 && copySize > (size_t)end && copySize <= (size_t)end + suffixSize &&
            sameFilePrefix(original, copy, end) && copy.read() == '\n' &&
            fileMatchesText(copy, GPX_EPILOGUE, copySize - end - 1)) {
            redundant = true;
            finishedRepair = copySize == (size_t)end + suffixSize;
        }
    }
    original.close();
    copy.close();
    if (!redundant) {
        Serial.printf("fsck: retained %s (differs from original)\n", tmp.c_str());
        return false;
    }
    // A complete verified repair can be published even with no free space.
    // Failed publication retains at least the verified copy for a later boot.
    return finishedRepair ? publishRepair(storage, tmp.c_str(), path) : storage.remove(tmp.c_str());
}

// Returns true if the file is complete afterwards (or was removed as empty),
// false if it had to be left as it was.
static bool repairIfNeeded(fs::FS& storage, const char* path) {
    if (!recoverRepairCopy(storage, path)) return false;
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
        tail[n] = 0;
        if (strstr(tail, "</gpx>")) complete = true;
    }
    if (complete) { f.close(); return true; }

    long end = findLastTrkptEnd(f);
    if (end == -2) { f.close(); return false; }
    if (end <= 0) {
        f.close();
        storage.remove(path);
        Serial.printf("fsck: removed empty/garbage %s\n", path);
        return true;
    }

    String tmp = String(path) + ".fix";
    File out = storage.open(tmp.c_str(), FILE_WRITE);
    if (!out) { f.close(); return false; }
    FileWriter w(out);
    f.seek(0);
    long remaining = end;
    uint8_t b[256];
    while (remaining > 0) {
        int want = remaining > 256 ? 256 : (int)remaining;
        int n = f.read(b, want);
        if (n <= 0) break;
        w.put((const char*)b, n);
        remaining -= n;
    }
    w.put("\n");
    w.put(GPX_EPILOGUE);
    // A short read or a write that didn't land means the copy is not a
    // faithful repair -- keep the original rather than replace it.
    bool ok = w.commit() && remaining == 0;
    out.close();
    f.close();
    if (!ok) {
        storage.remove(tmp.c_str());
        Serial.printf("fsck: could not repair %s (write failed)\n", path);
        return false;
    }
    // Publish only after both handles are closed and the repair is verified.
    // A surviving .fix is discovered on the next boot, including on FAT.
    if (!publishRepair(storage, tmp.c_str(), path)) {
        Serial.printf("fsck: could not replace %s (rename failed)\n", path);
        return false;
    }
    Serial.printf("fsck: repaired %s\n", path);
    return true;
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
            e = dir.openNextFile();
        }
    }
    // LittleFS directory cursors can change when repair creates, removes or
    // renames entries. Finish enumeration and close every handle first.
    dir.close();
    for (const String& path : paths) repairIfNeeded(storage, path.c_str());
    FS_UNLOCK();
}

// =================================================================
// AXP2101 power management / IRQ
// =================================================================
void IRAM_ATTR onPmuIrq() { gPmuIrqFlag = true; }

static void setupPmuIrq() {
    pinMode(PMU_IRQ_PIN, INPUT);
    attachInterrupt(digitalPinToInterrupt(PMU_IRQ_PIN), onPmuIrq, FALLING);
    PMU.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
    PMU.clearIrqStatus();
    PMU.enableIRQ(XPOWERS_AXP2101_VBUS_REMOVE_IRQ |
                  XPOWERS_AXP2101_VBUS_INSERT_IRQ |
                  XPOWERS_AXP2101_PKEY_LONG_IRQ   |
                  XPOWERS_AXP2101_WARNING_LEVEL2_IRQ);
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

static bool mountSdStorage();

static void powerOffForVbus() {
    // AXP2101 powers on on a VBUS INSERT EDGE, not its steady level. Never
    // shut down on USB, even for a power-key or low-battery interrupt: the
    // board would stay off until USB was unplugged and replugged (#106).
    if (PMU.isVbusIn()) {
        Serial.println("VBUS present -- staying awake instead of powering off.");
        gVbusLost = false;
        gVbusOffPending = false;
        gIdleSince = millis();
        gNetHold.store(false);
        return;
    }

    Serial.println("Preparing PMU power-off (battery critical / idle).");
    if (gTripPath[0]) endTrip(/*discardIfMicro=*/false);
    stopNetworkTask();
    // Keep the lock through power-off, so even a network task
    // that never stopped can't reach the filesystem once it's unmounted.
    FS_LOCK();
    if (gSdMounted) SD.end();
    if (gFlashMounted) LittleFS.end();
#if DISPLAY_DEBUG
    // Before the final VBUS read, preserving its immediate shutdown write.
    debugDisplayPowerSave(true);
#endif
    // PMU shutdown cuts all main rails, including SD BLDO1 and GPS ALDO4.
    // Leave rails and awake IRQs alone so a cancelled shutdown can resume.
    // GPS backup RAM is supplied directly by the 18650, not VBACKUP.
    Serial.println("PMU power-off: storage closed; checking VBUS before shutdown.");
    if (!PMU.isVbusIn()) {
        // Last possible level check: no log, delay or other work before
        // shutdown. A VBUS edge between this read and the PMU's I2C
        // shutdown write remains a race; software cannot make them atomic.
        PMU.shutdown();
        // XPowersLib only writes the shutdown bit. Allow the rails to fall
        // before treating a returning call as failure; don't spin or leave
        // the logger alive with storage unmounted if power stays on.
        vTaskDelay(pdMS_TO_TICKS(1000));
        Serial.println("ERROR: PMU.shutdown() did not power off -- resuming logging.");
    } else {
        Serial.println("VBUS returned -- PMU power-off cancelled.");
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
    FS_UNLOCK();
    gVbusLost = !PMU.isVbusIn();   // also catches a missed INSERT interrupt
#if DISPLAY_DEBUG
    debugDisplayPowerSave(false);
#endif
    gVbusOffPending = false;
    gIdleSince = millis();        // failed power-off retries after the idle grace
    gNetAbort.store(false);
    gNetHold.store(false);
    Serial.println("Power-off recovery complete; storage mounted, logging continues.");
}

static void pollPmu() {
    if (!gPmuIrqFlag) return;
    gPmuIrqFlag = false;
    PMU.getIrqStatus();   // latch status registers before testing

    // Treat either VBUS interrupt as "power changed" and read the actual
    // level: a short dip can latch REMOVE and INSERT in the same batch,
    // and the flags don't say which came last.
    if (PMU.isVbusRemoveIrq() || PMU.isVbusInsertIrq()) {
        if (PMU.isVbusIn()) {
            if (gVbusOffPending) Serial.println("VBUS dipped and came back -- ignored.");
            else if (gVbusLost)  Serial.println("VBUS inserted -- back on USB power.");
            gVbusOffPending = false;
            gVbusLost = false;
        } else if (!gVbusOffPending && !gVbusLost) {
            Serial.println("VBUS removed -- confirming before ending the trip.");
            gVbusOffPending = true;
            gVbusOffT0 = millis();
        }
    }
    if (PMU.isPekeyLongPressIrq())    powerOffForVbus();
    if (PMU.isDropWarningLevel2Irq()) powerOffForVbus();

    PMU.clearIrqStatus();   // write-1-to-clear, or the line never re-fires
}

// -----------------------------------------------------------------
// Battery charge temperature guard (audit N4)
// -----------------------------------------------------------------
// Li-ion must only be charged at roughly 0-45 C cell temperature, and a
// parked car's cabin passes 60 C in summer. The T-Beam has no battery
// thermistor (XPowersLib disables the TS pin), so the AXP2101's own die
// temperature stands in. Measured on this board 2026-10-05: the die idles
// ~9 C above a 25.6 C room with nothing charging, and charge current heats
// it a few degrees more within seconds. Hence the offset, and a 7 C gap
// between pausing and resuming so the charger's own heat can't make it
// toggle. Pausing charging never affects logging: on USB the PMU powers
// the board directly; the battery just isn't topped up until it cools.
static constexpr float    DIE_OFFSET_C      = 9.0f;
static constexpr float    CHG_HOT_PAUSE_C   = 45.0f + DIE_OFFSET_C;   // battery ~45 C
static constexpr float    CHG_HOT_RESUME_C  = 38.0f + DIE_OFFSET_C;   // battery ~38 C
static constexpr float    CHG_COLD_PAUSE_C  =  0.0f + DIE_OFFSET_C;   // battery ~0 C
static constexpr float    CHG_COLD_RESUME_C =  5.0f + DIE_OFFSET_C;   // battery ~5 C
static constexpr uint32_t CHG_CHECK_MS      = 30UL * 1000;
static bool     gChargePaused  = false;
static bool     gChargeApplied = false;   // has the guard set the charger yet?
static uint32_t gChargeCheckT0 = 0;

static void serviceChargeTemp() {
    uint32_t now = millis();
    if (gChargeApplied && now - gChargeCheckT0 < CHG_CHECK_MS) return;
    gChargeCheckT0 = now;

    float die = PMU.getTemperature();
    bool pause;
    const char* why = "";
    if (isnan(die)) {
        pause = true;                        // can't tell: fail safe
        why = "temperature unreadable";
    } else if (gChargeApplied && gChargePaused) {
        pause = !(die <= CHG_HOT_RESUME_C && die >= CHG_COLD_RESUME_C);
    } else {
        pause = die >= CHG_HOT_PAUSE_C || die <= CHG_COLD_PAUSE_C;
        why = die >= CHG_HOT_PAUSE_C ? "too hot" : "too cold";
    }

    // The first check sets the charger explicitly either way: the PMU keeps
    // its state across ESP32 resets, so a pause from before a reset must
    // not stick.
    if (gChargeApplied && pause == gChargePaused) return;
    bool first = !gChargeApplied;
    gChargeApplied = true;
    gChargePaused = pause;
    if (pause) {
        PMU.disableCellbatteryCharge();
        Serial.printf("Charging paused: %s (die %.1f C, battery ~%.0f C)\n",
                      why, die, die - DIE_OFFSET_C);
    } else {
        PMU.enableCellbatteryCharge();
        Serial.printf(first ? "Charge guard: die %.1f C (battery ~%.0f C), charging on.\n"
                            : "Charging resumed (die %.1f C, battery ~%.0f C)\n",
                      die, die - DIE_OFFSET_C);
    }
}

// Once USB power has been off for VBUS_OFF_CONFIRM_MS, the car is off:
// finalize the trip cleanly but keep running on battery -- we'll upload
// next time we're home. Re-reads the level in case an INSERT interrupt was
// missed. Keeps a genuine short trip (discardIfMicro=false).
static void confirmVbusOff() {
    if (!gVbusOffPending || millis() - gVbusOffT0 < VBUS_OFF_CONFIRM_MS) return;
    gVbusOffPending = false;
    if (PMU.isVbusIn()) return;
    Serial.println("VBUS off for 10 s -- car off: finalizing trip, staying awake.");
    gVbusLost = true;
    if (gTripPath[0]) endTrip(/*discardIfMicro=*/false);
}

// =================================================================
// Networking task (core 0): upload finished trips when parked at home
// =================================================================
static void reportNetworkStack(const char* phase) {
    // The watermark remembers the deepest call, including HTTP internals
    // that have already returned. Call only from the network task itself.
    unsigned remaining = (unsigned)uxTaskGetStackHighWaterMark(nullptr);
    Serial.printf("Net stack (%s): minimum free %u / %u bytes%s\n",
                  phase, remaining, (unsigned)NET_STACK_BYTES,
                  remaining <= NET_STACK_WARN_BYTES ? " -- WARNING: low headroom" : "");
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
    return WiFi.status() == WL_CONNECTED;
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
public:
    explicit LockedFileStream(File& f) : _f(f) {}
    int available() override {
        if (gNetAbort.load()) return -1;
        FS_LOCK(); int n = _f.available(); FS_UNLOCK(); return n;
    }
    int read() override {
        FS_LOCK(); int c = _f.read(); FS_UNLOCK(); return c;
    }
    size_t readBytes(char* buf, size_t len) override {
        FS_LOCK(); size_t n = _f.read((uint8_t*)buf, len); FS_UNLOCK(); return n;
    }
    int peek() override {
        FS_LOCK(); int c = _f.peek(); FS_UNLOCK(); return c;
    }
    size_t write(uint8_t) override { return 0; }   // read-only proxy
};

static bool uploadFile(fs::FS& storage, const char* path) {
    String url = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/upload";
    WiFiClient client;
    HTTPClient http;

    FS_LOCK();
    File f = storage.open(path, FILE_READ);
    if (!f) {
        FS_UNLOCK();
#if DISPLAY_DEBUG
        debugUpload(-1);
#endif
        return false;
    }
    size_t sz = f.size();
    FS_UNLOCK();

    LockedFileStream body(f);
    http.begin(client, url);
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
    http.end();
    float secs = (millis() - t0) / 1000.0f;
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
    if (ok) {
        storage.remove(path);
    } else if (rejected) {
        String bad = String(path) + ".bad";
        setAside = storage.rename(path, bad.c_str());
    }
    FS_UNLOCK();

#if DISPLAY_DEBUG
    debugUpload(code);
#endif
    if (ok) {
        Serial.printf("Upload %s (%u B, %.1f s) -> HTTP 200 (deleted)\n",
                      path, (unsigned)sz, secs);
    } else if (rejected) {
        Serial.printf("ERROR: upload %s (%u B, %.1f s) -> HTTP %d: server rejected the file -- %s\n",
                      path, (unsigned)sz, secs, code,
                      setAside ? "kept as .bad, not retried" : "rename failed, will retry");
    } else {
        Serial.printf("Upload %s (%u B, %.1f s) -> HTTP %d (will retry)\n",
                      path, (unsigned)sz, secs, code);
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
    for (int i = 0; i < count && !gNetHold.load() && !gNetAbort.load(); ++i) {
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
    if (gSdMounted) uploaded = uploadPending(SD);
    if (gFlashMounted) uploaded = uploadPending(LittleFS) || uploaded;
    return uploaded;
}

static bool hasPending() {
    return (gSdMounted && hasPending(SD)) || (gFlashMounted && hasPending(LittleFS));
}

#if DISPLAY_DEBUG
// Only the network task scans: once per ~30 s cycle, and again after an
// upload batch. gNetBusy protects unmount; per-entry FS locking lets GPS
// writes interleave even with a large SD backlog. Count complete files on
// BOTH stores, excluding .bad/.tmp and the still-incomplete current trip.
static unsigned debugPendingCount(fs::FS& storage) {
    unsigned count = 0;
    FS_LOCK();
    File dir = storage.open(TRIP_DIR);
    bool directory = dir && dir.isDirectory();
    FS_UNLOCK();
    if (directory) {
        while (!gNetHold.load() && !gNetAbort.load()) {
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
    if (gSdMounted) count += debugPendingCount(SD);
    if (gFlashMounted) count += debugPendingCount(LittleFS);
    if (gNetHold.load() || gNetAbort.load()) return; // partial count is unknown
    portENTER_CRITICAL(&gDebugNetMux);
    gDebugNet.pending = (int)count;
    portEXIT_CRITICAL(&gDebugNetMux);
}
#endif

// One pass of the network task. gNetBusy covers everything that touches
// WiFi or the filesystem, and is raised before gNetHold is read (see the
// handshake note at the globals).
static void netCycle() {
    gNetBusy.store(true);
    if (!gNetHold.load()) {
#if DISPLAY_DEBUG
        refreshDebugPending();
#endif
        // A trip just ended, so the car may be parked at home: try now,
        // whatever the backoff reached while driving or parked elsewhere.
        if (gNetRetryNow.exchange(false)) gNetRetryMs = 0;
        bool pending = hasPending();
        // A scan interrupted by sleep/abort has not proved the queue empty.
        if (gNetHold.load() || gNetAbort.load()) {
            gNetBusy.store(false);
            return;
        }
        if (!pending) {
            gNetRetryMs = 0;
        } else if (gNetRetryMs == 0 || millis() - gNetLastAttemptMs >= gNetRetryMs) {
            bool uploaded = false;
            const char* miss = nullptr;
            int joinStatus = -1;
#if DISPLAY_DEBUG
            debugWifi(DebugWifi::Scan);
#endif
            bool homeFound = homeApInRange();
#if DISPLAY_DEBUG
            debugWifi(homeFound ? DebugWifi::Join : DebugWifi::Off);
#endif
            if (!homeFound) {
                miss = "home WiFi not found";
            } else if (!connectWifi()) {
                miss = "couldn't join home WiFi";
                joinStatus = (int)WiFi.status();  // capture before disconnect
            } else {
#if DISPLAY_DEBUG
                debugWifi(DebugWifi::On);
#endif
                trySntp();
                uploaded = uploadPending();
#if DISPLAY_DEBUG
                refreshDebugPending();
#endif
                if (!uploaded) miss = "no trip uploaded";
            }
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
#if DISPLAY_DEBUG
            debugWifi(DebugWifi::Off);
#endif
            // Back off AP misses, failed joins, and server failures alike.
            // Seeing the AP alone must not undo a server-failure backoff.
            gNetRetryMs = uploaded ? NET_TICK_MS :
                min((gNetRetryMs ? gNetRetryMs : NET_TICK_MS) * 2, NET_RETRY_MAX_MS);
            gNetLastAttemptMs = millis();  // wait after the attempt finishes
            if (miss) {
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
        netCycle();
        vTaskDelay(pdMS_TO_TICKS(NET_TICK_MS));
    }
}

// =================================================================
// GPS receiver configuration (u-blox UBX protocol, started at boot)
// =================================================================
// The MAX-M10S (SPG 5.10) ACKed these legacy messages on 2026-10-18.
// Keep the legacy ACK/retry protocol until #108 handles VALSET separately.
// Re-send at every boot rather than rely on retained receiver settings.

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

// Send one message and arm its deadline. Only one frame (at most 44 bytes)
// is written per call; there is no flush or ACK wait.
static void sendGpsConfigCommand() {
    // UBX-CFG-MSG (3-byte form: rate on the current port) = 0 for the NMEA
    // sentences TinyGPSPlus ignores: GLL, GSA, GSV, VTG. RMC and GGA stay --
    // GGA carries altitude, HDOP and satellite count.
    static const uint8_t kSilence[] = { 0x01, 0x02, 0x03, 0x05 };
    if (gGpsConfig.command < sizeof(kSilence)) {
        uint8_t p[3] = { 0xF0, kSilence[gGpsConfig.command], 0 };
#if DISPLAY_DEBUG
        // GSV every fifth 1 Hz epoch: multi-constellation bursts can be
        // ~1 KB; spreading them over 5 s leaves 9600-baud headroom for
        // RMC+GGA and ACKs. The UART buffer is 2048 B. No VALSET changes.
        if (p[1] == 0x03) p[2] = 5;
#endif
        ubxSend(0x06, 0x01, p, sizeof(p));
    } else if (gGpsConfig.command == 4) {
        // UBX-CFG-NAV5: apply only dynamic model "automotive" (4).
        uint8_t nav5[36] = {0};
        nav5[0] = 0x01;
        nav5[2] = 4;
        ubxSend(0x06, 0x24, nav5, sizeof(nav5));
    } else {
        // Legacy CFG-MSG current-port rate: NAV-PVT every 1 Hz epoch.
        const uint8_t p[3] = {0x01, 0x07, 1};
        ubxSend(0x06, 0x01, p, sizeof(p));
    }
    gGpsConfig.sends++;
    gGpsConfig.ack = -1;
    gGpsConfig.since = millis();
    gGpsConfig.state = GPS_WAIT_ACK;
}

static void gpsConfigAttemptFailed() {
    gGpsConfig.since = millis();
    if (gGpsConfig.attempts >= GPS_CONFIG_ATTEMPTS) {
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
    Serial.println("GPS: waiting for checksum-valid NMEA (up to 3 s).");
}

static void serviceGpsConfiguration() {
    if (gGpsConfig.state == GPS_RETRY) {
        if (millis() - gGpsConfig.since < GPS_CONFIG_RETRY_MS) return;
        gGpsConfig.attempts++;
        gGpsConfig.command = gGpsConfig.sends = 0;
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
        if (++gGpsConfig.command == 6) {
            gGpsConfig.state = GPS_CONFIG_DONE;
            gNavPvtWaitMs = millis();
            Serial.printf("GPS configured on attempt %u (%lu ms after config start): "
#if DISPLAY_DEBUG
                          "RMC+GGA+NAV-PVT, GSV every 5 epochs, automotive model.\n",
#else
                          "RMC+GGA+NAV-PVT, automotive model.\n",
#endif
                          (unsigned)gGpsConfig.attempts,
                          (unsigned long)(millis() - gGpsConfig.started));
        } else {
            gGpsConfig.sends = 0;
            sendGpsConfigCommand();
        }
    } else if (gGpsConfig.ack == 0) {
        gpsConfigAttemptFailed();  // NAK: retry the entire set later
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
#if DISPLAY_DEBUG
        gDebugRmcValid = valid;
#endif
    }
    if (gRmcStatusGP.isUpdated()) {
        bool valid = gRmcStatusGP.value()[0] == 'A';
        fix |= valid;
#if DISPLAY_DEBUG
        gDebugRmcValid = valid;
#endif
    }
    return fix;
}

// =================================================================
// Build a Fix from the current TinyGPSPlus state
// =================================================================
static bool buildFix(Fix& fx) {
    if (!gps.location.isValid()) return false;
    fx.lat       = gps.location.lat();
    fx.lon       = gps.location.lng();
    fx.speedMps  = gps.speed.isValid() ? gps.speed.mps() : 0.0f;
    fx.speedTrusted = gpsSpeedTrusted(gState == IDLE || gState == ARMING);
    gLatestSpeedTrusted = fx.speedTrusted;
    fx.hdop      = gps.hdop.isValid() ? gps.hdop.hdop() : 99.9f;
    fx.monoMs    = millis();
    fx.hasUtc    = gClockSet.load();
    fx.utcMs     = fx.hasUtc ? nowEpochMs() : 0;
    return true;
}

// Handle each RMC as soon as it commits, including multiple epochs in one
// UART drain. ACK handling never consumes bytes on a separate read path.
// RMC carries position and speed together; GGA supplies the latest HDOP
// but must not log a second point with the previous epoch's speed.
static void receiveGpsNmea(uint8_t c) {
#if DISPLAY_DEBUG
    gDebugGsv.encode((char)c);
#endif
    if (!gps.encode((char)c)) return;
#if DISPLAY_DEBUG
    gDebugGsv.update(millis());
#endif
    setClockFromGps();
    if (rmcFixArrived()) {
        Fix fx;
        if (buildFix(fx)) {
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

static void receiveGpsByte(uint8_t c) {
    GpsUbxRx& rx = gGpsUbxRx;
    // Recover from a truncated or noise-induced UBX header. Expected ACKs
    // are 10 bytes; bound other packets to 256 payload bytes / 500 ms.
    if (rx.pos && millis() - rx.since >= 500) rx = GpsUbxRx();
    if (rx.pos == 0) {
        if (c == 0xB5) { rx.pos = 1; rx.since = millis(); }
        else receiveGpsNmea(c);
        return;
    }
    if (rx.pos == 1) {
        if (c == 0x62) rx.pos++;
        else {
            rx = GpsUbxRx();
            if (c == 0xB5) { rx.pos = 1; rx.since = millis(); }
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
        uint8_t expectedId = gGpsConfig.command == 4 ? 0x24 : 0x01;
        if (rx.checksumA && c == rx.b && rx.cls == 0x05 && rx.len == 2 &&
            (rx.id == 0x01 || rx.id == 0x00) && rx.payload[0] == 0x06 &&
            rx.payload[1] == expectedId && gGpsConfig.state == GPS_WAIT_ACK &&
            gGpsConfig.ack < 0) {
            gGpsConfig.ack = rx.id == 0x01 ? 1 : 0;
        }
        if (rx.checksumA && c == rx.b && rx.cls == 0x01 && rx.id == 0x07 && rx.len == 92) {
            // Explicit little-endian reads avoid alignment and host-endian assumptions.
            const auto u4 = [&rx](size_t off) -> uint32_t {
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
#if DISPLAY_DEBUG
        if (rx.checksumA && c == rx.b && rx.cls == 0x0A && rx.id == 0x38 &&
            gDebugRfStarted && gGpsConfig.state == GPS_CONFIG_DONE)
            parseDebugMonRf(rx.payload, rx.len, gDebugRf);
#endif
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
        delay(1);  // yield during the full-partition scan on a new board
    }
    return true;
}

static bool mountFlashStorage() {
    // Mount failures alone never authorize erasure. Only a successful read
    // of EVERY byte as 0xFF proves that provisioning cannot lose queued data.
    if (LittleFS.begin(false, "/littlefs", 10, TRIP_PARTITION_LABEL)) return true;
    if (tripPartitionIsErased()) {
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

static bool mountTripStorage() {
    gSdMounted = mountSdStorage();
    // Keep the existing preservation-first provisioning rule for flash.
    // If flash is damaged but SD works, record to SD and preserve flash
    // for recovery; #126 owns degraded service when neither store works.
    gFlashMounted = mountFlashStorage();
    if (gFlashMounted && !LittleFS.exists(TRIP_DIR)) {
        gFlashMounted = LittleFS.mkdir(TRIP_DIR);
        if (!gFlashMounted) LittleFS.end();
    }
    gTripFS = gSdMounted ? static_cast<fs::FS*>(&SD) : static_cast<fs::FS*>(&LittleFS);
    if (gSdMounted) {
        Serial.printf("Trip storage: microSD (%.2f GiB free); internal flash queue %s.\n",
                      storageFreeBytes(SD) / (1024.0 * 1024 * 1024),
                      gFlashMounted ? "mounted" : "unavailable -- preserved for recovery");
    } else if (gFlashMounted) {
        Serial.printf("WARNING: microSD unavailable -- using internal flash (%.2f MiB free).\n",
                      storageFreeBytes(LittleFS) / (1024.0 * 1024));
    } else {
        Serial.println("ERROR: no usable trip storage; halting with charge guard active.");
    }
    return gSdMounted || gFlashMounted;
}

#if DISPLAY_DEBUG
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
    // First poll starts after config succeeds OR gives up. Each later poll
    // logs the previous interval's reply, then invalidates it. Never wait.
    gDebugRfStarted = true; gDebugRfPollMs = now;
    gDebugRf.valid = false;
    ubxSend(0x0A, 0x38, nullptr, 0);
}

static void serviceDebugDisplay() {
    uint32_t now = millis();
    if (!gDebugDisplayReady || now - gDebugDrawMs < 1000) return;
    gDebugDrawMs = now;
    DebugStatus s;
    static const char* states[] = {"IDLE", "ARMING", "ACTIVE", "COOLDOWN", "FINALIZING"};
    s.state = states[gState];
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
    s.store = gSdMounted ? "SD" : (gFlashMounted ? "FLASH" : "NONE");
    portENTER_CRITICAL(&gDebugNetMux);
    s.net = gDebugNet;
    portEXIT_CRITICAL(&gDebugNetMux);
    s.now = millis();  // snapshot upload time cannot be newer than this clock read
    s.batteryPct = PMU.getBatteryPercent();
    s.batteryV = PMU.getBattVoltage() / 1000.0f;
    s.usb = PMU.isVbusIn();
    s.charging = PMU.isCharging();
    drawDebugDisplay(s);
}
#endif

void setup() {
    // VBUS-insert power-on brings the Supreme's LoRa rail up ON. Disable
    // and verify it as the first PMU operation after bounded bus recovery
    // and initialization, BEFORE Serial, storage or GPS setup can block.
    if (!beginPmuWithRecovery(Wire1, PMU, I2C_SDA, I2C_SCL, gPmuStartup)) {
        Serial.begin(115200);
        printPmuStartup(gPmuStartup);
        Serial.println("ERROR: AXP2101 not found. Halting.");
        while (1) delay(1000);
    }
    if (!PMU.disableALDO3() || PMU.isEnableALDO3()) {
        Serial.begin(115200);
        printPmuStartup(gPmuStartup);
        Serial.println("ERROR: LoRa rail (ALDO3) could not be verified off. Halting.");
        while (1) delay(1000);
    }
    // BLDO1 powers the SD slot but starts ON at 1.8 V. Disable it early;
    // mountSdStorage sets 3.3 V before enabling it, after safety setup.
    PMU.disableBLDO1();
    // Supreme GPS backup RAM comes straight from the 18650. LilyGO also
    // disables VBACKUP; a 60 s ALDO4-off test hot-started in 2.0 s with it off.
    PMU.disableButtonBatteryCharge();
    // Leave ALDO1/ALDO2 and DCDC4/DCDC5 as found. Peripheral loads and
    // PMU/RTC bus pull-up dependencies need proof before disabling them;
    // #25 owns the measured current decision. Never touch DCDC1 (ESP32).

    Serial.begin(115200);
    delay(1000);
    Serial.println("\n=== Stage 4: Trip Logger ===");
    printPmuStartup(gPmuStartup);
    Serial.println("LoRa rail (ALDO3) verified off; SD rail (BLDO1) off; VBACKUP off.");
    Serial.printf("PMU power-on source 0x%02X, power-off source 0x%02X.\n",
                  (unsigned)PMU.getPowerOnSource(), (unsigned)PMU.getPowerOffSource());

    gFsMutex = xSemaphoreCreateMutex();
    if (!gFsMutex) {
        Serial.println("ERROR: LittleFS mutex allocation failed. Restarting.");
        delay(1000);
        ESP.restart();
        return;
    }

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
    PMU.disableCellbatteryCharge();
    // Enable the measurements the battery/VBUS predicates and the charge
    // temperature guard rely on.
    PMU.enableTemperatureMeasure();
    PMU.enableBattDetection();
    PMU.enableBattVoltageMeasure();
    PMU.enableVbusVoltageMeasure();
    PMU.enableSystemVoltageMeasure();
    setupPmuIrq();
    // Seed VBUS state from hardware: a battery-only boot (e.g. wake via
    // power key away from the car) must still hit the idle power-off timer.
    gVbusLost = !PMU.isVbusIn();
    gIdleSince = millis();
    Serial.printf("PMU ready, MAX-M10S GPS rail (ALDO4) on at 3.3 V, charge to %s. VBUS %s.\n",
                  PMU.getChargeTargetVoltage() == XPOWERS_AXP2101_CHG_VOL_4V1 ? "4.1 V" : "NOT 4.1 V",
                  gVbusLost ? "absent (on battery)" : "present");

    // --- GPS UART. setRxBufferSize MUST precede begin(). ---
    GPSSerial.setRxBufferSize(2048);
    GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);

    // --- Storage + boot-time repair ---
    if (!mountTripStorage()) {
        // Halted, but maybe still on USB in a hot car: keep guarding the
        // charger (off since the PMU setup). Delay first, so the temperature
        // ADC has converted before the first reading.
        while (1) { delay(1000); serviceChargeTemp(); }
    }
    prefs.begin("mileage", false);
    if (gSdMounted) fsckAllTrips(SD);
    if (gFlashMounted) fsckAllTrips(LittleFS);

    // Start a bounded readiness wait; loop() receives NMEA and configures
    // the receiver without blocking trip logging or power/charge checks.
    configureGps();

    // First charge-temperature check now, not 30 s in: charging has been off
    // since the PMU setup, and this turns it back on if it's safe. (The
    // temperature ADC was enabled well over a conversion time ago, with the
    // PMU.)
    serviceChargeTemp();

#if DISPLAY_DEBUG
    beginDebugDisplay();
#endif
    // --- Networking task on core 0; GPS/FSM stay on core 1 (loop) ---
    if (xTaskCreatePinnedToCore(networkTask, "net", NET_STACK_BYTES,
                               nullptr, 1, nullptr, 0) != pdPASS) {
        Serial.println("ERROR: network task creation failed; trips remain queued for a later boot.");
    }

    Serial.println("Setup complete. Logging trips.");
}

void loop() {
    // Core 1: one UART reader routes NMEA/fixes and checksum-checked UBX
    // ACKs. Configuration deadlines never block receive or trip processing.
    serviceGpsSpeedFallback();
    while (GPSSerial.available() > 0) {
        receiveGpsByte((uint8_t)GPSSerial.read());
    }
    serviceGpsConfiguration();
#if DISPLAY_DEBUG
    serviceDebugRf();
#endif

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
#if DISPLAY_DEBUG
    serviceDebugDisplay();
#endif
}
