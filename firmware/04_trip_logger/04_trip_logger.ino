// =================================================================
// Stage 4: Trip Logger (GPX logging + WiFi upload)
// =================================================================
// This is the real firmware. It ties together everything proven in
// Stages 1-3 (toolchain, AXP2101 PMU + GPS power, NEO-M8N parsing) and
// adds the four pieces that make it a mileage tracker:
//
//   1. A trip state machine that auto-detects start (>5 mph for 10 s)
//      and end (<2 mph for 3 min), with hysteresis so stop-and-go
//      traffic doesn't split one drive into many.
//   2. Crash-safe GPX logging to LittleFS -- one trkpt line appended
//      per fix, file.close() after every write, so a yanked USB cable
//      loses at most the single in-flight point. A boot-time "fsck"
//      repairs any file left unclosed by a hard power cut.
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
// Required libraries (all used in earlier stages except none new):
//   XPowersLib (lewisxhe), TinyGPSPlus (Mikal Hart). WiFi, HTTPClient,
//   LittleFS, and Preferences ship with the ESP32 Arduino core.
//
// PARTITIONS: partitions.csv in this folder (no OTA, 2.375 MB of trip
// storage) is picked up automatically and overrides Tools -> Partition
// Scheme. Trips are ~118 B per second of driving, so that holds roughly
// 5-6 hours of driving between uploads.
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
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <time.h>
#include <sys/time.h>
#include <esp_sleep.h>
#include <vector>
#include <esp_sntp.h>
#include <atomic>

#include "secrets.h"

// -----------------------------------------------------------------
// Pin definitions for T-Beam V1.2 (identical to Stages 2-3)
// -----------------------------------------------------------------
#define I2C_SDA      21
#define I2C_SCL      22
#define GPS_RX_PIN   34
#define GPS_TX_PIN   12
#define GPS_BAUD     9600
#define PMU_IRQ_PIN  35   // AXP2101 IRQ, input-only, active-low

// -----------------------------------------------------------------
// Trip state machine tuning (from brief section 5)
// -----------------------------------------------------------------
static constexpr float    START_MPS       = 2.235f;          // 5 mph
static constexpr float    END_MPS         = 0.894f;          // 2 mph
static constexpr uint32_t ARM_MS          = 10UL  * 1000;    // sustain to start
static constexpr uint32_t COOL_MS         = 180UL * 1000;    // sustain to end
static constexpr float    MIN_TRIP_METERS = 160.0f;          // ~0.1 mi
static constexpr uint32_t MIN_TRIP_MS     = 30UL  * 1000;

// Fix-loss heuristic: gap this long between valid fixes starts a new
// <trkseg> so viewers don't draw a straight line across a tunnel.
static constexpr uint32_t SEG_GAP_MS      = 5000;

// If a trip is ACTIVE/COOLDOWN and we go this long without ANY valid fix
// (parked in a garage, GPS failure), finalize the trip rather than leave
// it open until a power event.
static constexpr uint32_t FIX_LOSS_END_MS = 300UL * 1000;   // 5 min

// On battery (VBUS lost) with no trip in progress, deep sleep after this
// long. Ample time for the post-drive upload (net task ticks every 30 s),
// and it stops the GPS rail + WiFi scans from draining the 18650 for days.
// VBUS insert (car back on) wakes us.
static constexpr uint32_t IDLE_BATT_SLEEP_MS = 10UL * 60 * 1000;

// How often the networking task wakes to look for pending uploads.
static constexpr uint32_t NET_TICK_MS     = 30000;
// Restore the brief's conservative allocation until measured on hardware.
// ESP-IDF task stack sizes and high-water marks are in bytes, not words.
static constexpr uint32_t NET_STACK_BYTES = 12288;
static constexpr uint32_t NET_STACK_WARN_BYTES = 1024;

// Storage layout.
static const char* TRIP_DIR = "/trips";

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
TinyGPSPlus    gps;
HardwareSerial GPSSerial(1);
Preferences    prefs;

// RMC status field ('A' = valid fix). Committed only when an RMC sentence
// passes its checksum, so isUpdated() means "an RMC just arrived" -- the
// once-per-epoch signal loop() processes fixes on. "GN" is the talker ID
// when the M8N uses more than one constellation (its default), "GP" when
// it uses GPS alone.
TinyGPSCustom  gRmcStatusGN(gps, "GNRMC", 2);
TinyGPSCustom  gRmcStatusGP(gps, "GPRMC", 2);
TinyGPSCustom  gRmcTimeGN(gps, "GNRMC", 1);
TinyGPSCustom  gRmcDateGN(gps, "GNRMC", 9);
TinyGPSCustom  gRmcTimeGP(gps, "GPRMC", 1);
TinyGPSCustom  gRmcDateGP(gps, "GPRMC", 9);

SemaphoreHandle_t gFsMutex;     // serializes ALL LittleFS access
volatile bool     gVbusLost   = false;
volatile bool     gPmuIrqFlag = false;
// Shared by core 0 (SNTP) and core 1 (GPS/fix construction).
std::atomic<bool> gClockSet{false};  // a source has synchronized UTC this boot
// GPS correction cadence belongs only to core 1; SNTP never suppresses it.
bool              gGpsClockSet = false;
uint32_t          gLastGpsSyncMs = 0;
static constexpr uint32_t GPS_CLOCK_SYNC_MS = 60UL * 1000;

#define FS_LOCK()   xSemaphoreTake(gFsMutex, portMAX_DELAY)
#define FS_UNLOCK() xSemaphoreGive(gFsMutex)

// A single GPS fix, with both a monotonic and (once known) a UTC stamp.
struct Fix {
    double   lat, lon;
    float    speedMps, hdop;
    bool     hasUtc;
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
// 1 Hz epoch (see loop()), so this covers ~90 s -- a cold start.
static Fix    gPend[90];
static size_t gPendHead = 0, gPendCount = 0;

// -----------------------------------------------------------------
// Trip FSM state
// -----------------------------------------------------------------
enum TripState { IDLE, ARMING, ACTIVE, COOLDOWN, FINALIZING };
static TripState gState = IDLE;
static uint32_t  gArmT0 = 0, gCoolT0 = 0, gTripT0 = 0;
static uint32_t  gFsCheckT0 = 0;        // last free-space check during a trip
static uint32_t  gFullWarnT0 = 0;       // last "storage full" log line
static bool      gFullWarned = false;
static uint32_t  gIdleSince = 0;        // last time we were NOT (on battery + IDLE)
static double    gTripMeters = 0;

// Fixes seen while ARMING, so the first ~10 s of a trip isn't lost.
// One fix per 1 Hz epoch makes a 10 s arm window 10-11 fixes; 16 slots is
// comfortable headroom. Ring: oldest overwritten on overflow.
static Fix    gArmBuf[16];
static size_t gArmHead = 0, gArmCount = 0;

static void armBufReset() { gArmHead = gArmCount = 0; }

static void armBufPush(const Fix& fx) {
    gArmBuf[(gArmHead + gArmCount) % 16] = fx;
    if (gArmCount < 16) gArmCount++;
    else gArmHead = (gArmHead + 1) % 16;
}
static float     gMaxSpeedMps = 0;
static char      gTripPath[48] = {0};   // active file, "" when none
static bool      gTripWriteFailed = false;  // a write to gTripPath didn't land
static bool      gNeedNewSeg = false;
static bool      gHaveLastFix = false;
static Fix       gLastFix;
static uint32_t  gLastFixMonoMs = 0;

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
    snprintf(out, n, "%04d-%02d-%02dT%02d:%02d:%02dZ",
             tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
             tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
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
    uint32_t c = prefs.getUInt("tripno", 0);
    // NVS may be reset independently of LittleFS, or its last counter write
    // may have failed. Never let FILE_WRITE truncate a queued trip. The
    // caller holds the FS mutex while selecting and creating this path.
    do {
        ++c;
        snprintf(out, n, "%s/t%06u.gpx", TRIP_DIR, c);
    } while (LittleFS.exists(out) ||
             LittleFS.exists((String(out) + ".bad").c_str()) ||
             LittleFS.exists((String(out) + ".fix").c_str()));
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

    char line[256];   // ~150 max for sane values; headroom for garbage floats
    int n = snprintf(line, sizeof(line),
        "%s<trkpt lat=\"%.6f\" lon=\"%.6f\">%s"
        "<speed>%.2f</speed><hdop>%.1f</hdop></trkpt>\n",
        gNeedNewSeg ? "</trkseg><trkseg>\n" : "",
        fx.lat, fx.lon, timeEl, fx.speedMps, fx.hdop);
    if (n <= 0 || n >= (int)sizeof(line)) { w.ok = false; return; }

    w.put(line, n);
    gNeedNewSeg = false;
}

// =================================================================
// Pending ring buffer (pre-UTC fixes)
// =================================================================
static void pushPending(const Fix& fx) {
    gPend[(gPendHead + gPendCount) % 90] = fx;
    if (gPendCount < 90) {
        gPendCount++;
    } else {
        gPendHead = (gPendHead + 1) % 90;   // overwrite oldest
    }
}

// Write all buffered fixes to the open trip file, back-dating each from
// the now-known UTC and its monotonic stamp. Called once, the first time
// UTC becomes available during an active trip.
static void flushPending(FileWriter& w) {
    uint64_t utcNow = nowEpochMs();
    uint32_t monoNow = millis();
    for (size_t i = 0; i < gPendCount; ++i) {
        Fix p = gPend[(gPendHead + i) % 90];
        p.hasUtc = true;
        p.utcMs  = utcNow - (uint64_t)(monoNow - p.monoMs);
        appendTrkpt(w, p);
    }
    gPendCount = gPendHead = 0;
}

// =================================================================
// Trip lifecycle
// =================================================================
// Free bytes on the trip partition. Takes the FS lock; callers must not
// hold it.
static size_t fsFreeBytes() {
    FS_LOCK();
    size_t total = LittleFS.totalBytes();
    size_t used  = LittleFS.usedBytes();
    FS_UNLOCK();
    return used < total ? total - used : 0;
}

// True if there's room to start a trip. Logs (at most once a minute while
// driving) when there isn't, since otherwise the miles vanish silently.
static bool roomForNewTrip(uint32_t now) {
    size_t freeB = fsFreeBytes();
    if (freeB >= FS_START_FREE) return true;
    if (!gFullWarned || now - gFullWarnT0 >= 60000) {
        Serial.printf("ERROR: trip storage full (%u bytes free) -- NOT recording. "
                      "Upload at home to free space.\n", (unsigned)freeB);
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
    size_t freeB = fsFreeBytes();
    if (freeB >= FS_STOP_FREE) return false;
    Serial.printf("ERROR: trip storage nearly full (%u bytes free) -- ending trip.\n",
                  (unsigned)freeB);
    return true;
}

// Returns false -- and leaves no file and no active trip behind -- if the
// trip file can't be created and written. The caller must then stay IDLE:
// carrying on as ACTIVE would let FILE_APPEND create a file with no
// prologue, which uploads as invalid GPX and is rejected forever.
static bool beginTrip(const Fix& fx) {
    gTripMeters = 0;
    gMaxSpeedMps = 0;
    gNeedNewSeg = false;
    gTripWriteFailed = false;
    gTripT0 = gArmCount ? gArmBuf[gArmHead].monoMs : fx.monoMs;

    FS_LOCK();
    makeTripPath(gTripPath, sizeof(gTripPath));
    bool ok = false;
    File f = LittleFS.open(gTripPath, FILE_WRITE);
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
        f.close();
        if (!ok) LittleFS.remove(gTripPath);
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
    for (size_t i = 1; i < gArmCount; ++i) {
        const Fix& a = gArmBuf[(gArmHead + i - 1) % 16];
        const Fix& b = gArmBuf[(gArmHead + i) % 16];
        gTripMeters += haversineM(a.lat, a.lon, b.lat, b.lon);
        if (b.speedMps > gMaxSpeedMps) gMaxSpeedMps = b.speedMps;
    }
    armBufReset();
    gFsCheckT0 = fx.monoMs;
    gFullWarned = false;
    Serial.printf("Trip started: %s\n", gTripPath);
    return true;
}

// Append a point to the active trip, buffering instead if UTC isn't
// available yet. Once UTC arrives mid-trip, flush the buffer first.
// Returns false if the write didn't reach flash; the caller must end the
// trip, since a partial line may now sit at the tail of the file.
static bool writeOrBufferTrkpt(const Fix& fx) {
    if (gTripPath[0] == 0) return true;
    if (!fx.hasUtc) {
        pushPending(fx);
        return true;
    }
    FS_LOCK();
    bool ok = false;
    File f = LittleFS.open(gTripPath, FILE_APPEND);
    // FILE_APPEND creates a missing file. A real trip file always holds at
    // least the prologue, so an empty one means ours is gone -- never write
    // points into a file with no <gpx> header.
    if (f && f.size() > 0) {
        FileWriter w(f);
        if (gPendCount > 0) flushPending(w);
        appendTrkpt(w, fx);
        ok = w.commit();
    }
    if (f) f.close();   // close() as well as the commit's fsync
    FS_UNLOCK();

    if (!ok) {
        Serial.printf("ERROR: write to %s failed -- ending trip.\n", gTripPath);
        gTripWriteFailed = true;
    }
    return ok;
}

static bool repairIfNeeded(const char* path);

// Close out the active trip. If it's too short/slow it's discarded --
// unless this is a power-loss finalize, where a genuine short drive that
// ended in a power cut is still data worth keeping.
static void endTrip(bool discardIfMicro) {
    if (gTripPath[0] == 0) { gState = IDLE; return; }

    bool micro = (millis() - gTripT0 < MIN_TRIP_MS) ||
                 (gTripMeters < MIN_TRIP_METERS);

    FS_LOCK();
    if (discardIfMicro && micro) {
        Serial.printf("Discarding micro-trip (%.0f m, %lu ms)\n",
                      gTripMeters, (unsigned long)(millis() - gTripT0));
        LittleFS.remove(gTripPath);
    } else {
        bool ok = false;
        if (!gTripWriteFailed) {
            File f = LittleFS.open(gTripPath, FILE_APPEND);
            // Same guard as writeOrBufferTrkpt: never turn a missing trip
            // file into one holding only the epilogue.
            if (f && f.size() > 0) {
                FileWriter w(f);
                // Flush any still-buffered (never-got-UTC) points before
                // closing, so a trip that never got UTC isn't lost.
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
        if (!ok) ok = repairIfNeeded(gTripPath);

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
    gState = IDLE;
}

// The five-state machine, driven once per fix.
static void tripFsmOnFix(const Fix& fx) {
    const uint32_t now = fx.monoMs;

    // Distance + max speed accumulate whenever we're recording. The
    // speed gate stops GPS jitter from adding phantom meters while
    // stationary (3 min parked in COOLDOWN scatters tens of meters);
    // erring low is the IRS-safe direction.
    if ((gState == ACTIVE || gState == COOLDOWN) && gHaveLastFix &&
        fx.speedMps > END_MPS) {
        gTripMeters += haversineM(gLastFix.lat, gLastFix.lon, fx.lat, fx.lon);
    }
    if (fx.speedMps > gMaxSpeedMps) gMaxSpeedMps = fx.speedMps;

    switch (gState) {
    case IDLE:
        if (fx.speedMps >= START_MPS) {
            gState = ARMING; gArmT0 = now;
            armBufReset(); armBufPush(fx);
        }
        break;
    case ARMING:
        if (fx.speedMps < START_MPS) {
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
        if (!writeOrBufferTrkpt(fx) || tripStorageLow(now)) { gState = FINALIZING; break; }
        if (fx.speedMps <= END_MPS) { gState = COOLDOWN; gCoolT0 = now; }
        break;
    case COOLDOWN:
        if (!writeOrBufferTrkpt(fx) || tripStorageLow(now)) { gState = FINALIZING; break; }
        if (fx.speedMps > END_MPS) {
            gState = ACTIVE;
        } else if (now - gCoolT0 >= COOL_MS) {
            gState = FINALIZING;
        }
        break;
    case FINALIZING:
        break;   // handled immediately below, not on the NEXT fix
    }

    // Finalize in the same pass that triggered it. Previously this only
    // ran on the next valid fix, so losing GPS right as the cooldown
    // expired (parked in a garage) left the trip open until power loss.
    if (gState == FINALIZING) {
        endTrip(/*discardIfMicro=*/true);
    }

    gLastFix = fx;
    gHaveLastFix = true;
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

// Deal with previous repair attempts before FILE_WRITE can truncate them.
// Called under the same FS lock as repairIfNeeded. Ambiguous copies are
// retained for inspection; only proven redundant bytes may be discarded.
static bool recoverRepairCopy(const char* path) {
    String tmp = String(path) + ".fix";
    if (!LittleFS.exists(tmp.c_str())) return true;
    if (LittleFS.exists((String(path) + ".bad").c_str())) {
        Serial.printf("fsck: retained %s (quarantine collision)\n", tmp.c_str());
        return false;
    }
    File copy = LittleFS.open(tmp.c_str(), FILE_READ);
    if (!copy) return false;
    if (!LittleFS.exists(path)) {
        // A historical interrupted replacement may have left this as the
        // only copy. Rename without copying, then use normal repair below.
        // An incomplete/unknown header or no complete point is not safe to
        // feed to the normal garbage-removal path, so preserve those bytes.
        bool recoverable = fileMatchesText(copy, GPX_PROLOGUE, strlen(GPX_PROLOGUE)) &&
                           findLastTrkptEnd(copy) > (long)strlen(GPX_PROLOGUE);
        copy.close();
        if (recoverable && LittleFS.rename(tmp.c_str(), path)) {
            Serial.printf("fsck: recovered orphan %s\n", path);
            return true;
        }
        Serial.printf("fsck: retained orphan %s\n", tmp.c_str());
        return false;
    }

    File original = LittleFS.open(path, FILE_READ);
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
    // Failed rename/remove leaves both copies for a later boot to retry.
    return finishedRepair ? LittleFS.rename(tmp.c_str(), path) : LittleFS.remove(tmp.c_str());
}

// Returns true if the file is complete afterwards (or was removed as empty),
// false if it had to be left as it was.
static bool repairIfNeeded(const char* path) {
    if (!recoverRepairCopy(path)) return false;
    File f = LittleFS.open(path, FILE_READ);
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
        LittleFS.remove(path);
        Serial.printf("fsck: removed empty/garbage %s\n", path);
        return true;
    }

    String tmp = String(path) + ".fix";
    File out = LittleFS.open(tmp.c_str(), FILE_WRITE);
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
        LittleFS.remove(tmp.c_str());
        Serial.printf("fsck: could not repair %s (write failed)\n", path);
        return false;
    }
    // LittleFS rename atomically replaces an existing file. Removing the
    // original first creates a power-loss window in which only an ignored
    // .fix remains. On failure keep both copies for the next boot to retry.
    if (!LittleFS.rename(tmp.c_str(), path)) {
        Serial.printf("fsck: could not replace %s (rename failed)\n", path);
        return false;
    }
    Serial.printf("fsck: repaired %s\n", path);
    return true;
}

static void fsckAllTrips() {
    FS_LOCK();
    std::vector<String> paths;
    File dir = LittleFS.open(TRIP_DIR);
    if (dir && dir.isDirectory()) {
        File e = dir.openNextFile();
        while (e) {
            String name = String(TRIP_DIR) + "/" + e.name();
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
    for (const String& path : paths) repairIfNeeded(path.c_str());
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

static void deepSleepForVbus() {
    Serial.println("Entering deep sleep (battery critical / idle).");
    if (gTripPath[0]) endTrip(/*discardIfMicro=*/false);
    FS_LOCK();
    LittleFS.end();
    FS_UNLOCK();
    // GPS main supply off. The receiver keeps its clock and satellite data
    // on the backup supply (enabled in setup), so the next fix is a hot
    // start.
    PMU.disableALDO3();
    PMU.disableALDO4();   // unused here; set by older builds
    // Disable before clearing: an interrupt landing between a clear and the
    // disable would hold the IRQ line low and wake us immediately.
    PMU.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
    PMU.clearIrqStatus();
    PMU.enableIRQ(XPOWERS_AXP2101_VBUS_INSERT_IRQ | XPOWERS_AXP2101_PKEY_SHORT_IRQ);
    esp_sleep_enable_ext1_wakeup(1ULL << PMU_IRQ_PIN, ESP_EXT1_WAKEUP_ALL_LOW);
    esp_deep_sleep_start();
}

// USB power must stay off this long before it counts as "car off". Engine
// cranking and start-stop restarts brown the USB port out for a second or
// two; acting on the first dip split one drive into several trips.
static constexpr uint32_t VBUS_OFF_CONFIRM_MS = 10UL * 1000;
static bool     gVbusOffPending = false;
static uint32_t gVbusOffT0 = 0;

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
    if (PMU.isPekeyLongPressIrq())    deepSleepForVbus();
    if (PMU.isDropWarningLevel2Irq()) deepSleepForVbus();

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
    if (n <= 0) return false;
    for (int i = 0; i < n; ++i) {
        if (WiFi.SSID(i) != WIFI_SSID) continue;
#if USE_BSSID_FILTER
        if (memcmp(WiFi.BSSID(i), WIFI_BSSID, 6) != 0) continue;
#endif
        return true;
    }
    return false;
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
    while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
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
    while (!gClockSet.load() && millis() - start < 5000) {
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

static bool isComplete(const char* path) {
    FS_LOCK();
    File f = LittleFS.open(path, FILE_READ);
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
        f.close();
    }
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
class LockedFileStream : public Stream {
    File& _f;
public:
    explicit LockedFileStream(File& f) : _f(f) {}
    int available() override {
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

static bool uploadFile(const char* path) {
    String url = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/upload";
    WiFiClient client;
    HTTPClient http;

    FS_LOCK();
    File f = LittleFS.open(path, FILE_READ);
    if (!f) { FS_UNLOCK(); return false; }
    size_t sz = f.size();
    FS_UNLOCK();

    LockedFileStream body(f);
    http.begin(client, url);
    http.addHeader("Content-Type", "application/gpx+xml");
    http.addHeader("Authorization", String("Bearer ") + AUTH_TOKEN);
    http.addHeader("X-Device-Id", DEVICE_ID);
    int code = http.sendRequest("POST", &body, sz);
    http.end();
    reportNetworkStack("after HTTP upload");

    FS_LOCK();
    f.close();
    bool ok = (code == 200);
    // 400 (malformed GPX) and 413 (too large) mean the server read this
    // file and rejected it: resending the same bytes can never succeed, and
    // retrying every 30 s forever burns battery and radio time. Set it
    // aside as .bad -- kept for inspection, ignored by uploads and fsck.
    // Every other failure (401 token mismatch, 404 wrong host/port, 5xx,
    // network errors) is a fixable setup or server problem, not this
    // file's fault, so those stay queued.
    bool rejected = (code == 400 || code == 413);
    bool setAside = false;
    if (ok) {
        LittleFS.remove(path);
    } else if (rejected) {
        String bad = String(path) + ".bad";
        setAside = LittleFS.rename(path, bad.c_str());
    }
    FS_UNLOCK();

    if (ok) {
        Serial.printf("Upload %s -> HTTP 200 (deleted)\n", path);
    } else if (rejected) {
        Serial.printf("ERROR: upload %s -> HTTP %d: server rejected the file -- %s\n",
                      path, code, setAside ? "kept as .bad, not retried" : "rename failed, will retry");
    } else {
        Serial.printf("Upload %s -> HTTP %d (will retry)\n", path, code);
    }
    return ok;
}

// Collect names of complete, non-active trip files, then upload each.
static void uploadPending() {
    String pending[16];
    int    count = 0;

    FS_LOCK();
    File dir = LittleFS.open(TRIP_DIR);
    if (dir && dir.isDirectory()) {
        File e = dir.openNextFile();
        while (e && count < 16) {
            String name = String(TRIP_DIR) + "/" + e.name();
            if (name.endsWith(".gpx") &&
                (gTripPath[0] == 0 || name != String(gTripPath))) {
                pending[count++] = name;
            }
            e = dir.openNextFile();
        }
    }
    FS_UNLOCK();

    for (int i = 0; i < count; ++i) {
        if (isComplete(pending[i].c_str())) {
            uploadFile(pending[i].c_str());
        }
    }
}

static bool hasPending() {
    bool found = false;
    FS_LOCK();
    File dir = LittleFS.open(TRIP_DIR);
    if (dir && dir.isDirectory()) {
        File e = dir.openNextFile();
        while (e) {
            String name = String(TRIP_DIR) + "/" + e.name();
            if (name.endsWith(".gpx") &&
                (gTripPath[0] == 0 || name != String(gTripPath))) {
                found = true;
                break;
            }
            e = dir.openNextFile();
        }
    }
    FS_UNLOCK();
    return found;
}

static void networkTask(void*) {
    reportNetworkStack("task start");
    for (;;) {
        if (hasPending()) {
            if (homeApInRange() && connectWifi()) {
                trySntp();
                uploadPending();
            }
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
            reportNetworkStack("after network cycle");
        }
        vTaskDelay(pdMS_TO_TICKS(NET_TICK_MS));
    }
}

// =================================================================
// GPS receiver configuration (u-blox UBX protocol, sent at boot)
// =================================================================
// The M8N's defaults work, but they send four NMEA sentences nothing here
// parses and use the "portable" motion model. The settings live in the
// receiver's RAM, so they're re-sent on every boot.

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

// Watch the UART for UBX-ACK-ACK (1) or ACK-NAK (0) answering (cls, id);
// -1 on timeout. NMEA arriving meanwhile is discarded -- setup() only.
static int ubxWaitAck(uint8_t cls, uint8_t id, uint32_t timeoutMs) {
    // B5 62 05 {01=ACK|00=NAK} 02 00 <cls> <id>
    uint8_t win[8] = {0};
    uint32_t t0 = millis();
    while (millis() - t0 < timeoutMs) {
        if (GPSSerial.available() <= 0) { delay(1); continue; }
        memmove(win, win + 1, 7);
        win[7] = (uint8_t)GPSSerial.read();
        if (win[0] == 0xB5 && win[1] == 0x62 && win[2] == 0x05 &&
            win[4] == 0x02 && win[5] == 0x00 && win[6] == cls && win[7] == id) {
            if (win[3] == 0x01) return 1;
            if (win[3] == 0x00) return 0;
        }
    }
    return -1;
}

// Send one configuration message, retrying while the receiver is silent.
static int ubxCommand(uint8_t cls, uint8_t id, const uint8_t* payload, uint16_t len) {
    for (int attempt = 0; attempt < 3; ++attempt) {
        ubxSend(cls, id, payload, len);
        int r = ubxWaitAck(cls, id, 300);
        if (r >= 0) return r;
    }
    return -1;
}

// Returns true only if every setting was acknowledged. Failure is logged
// and is not fatal: loop() already processes one fix per RMC sentence
// whatever the receiver sends, so the defaults still log correctly.
static bool configureGps() {
    // Give a freshly powered receiver up to 2 s to start talking.
    uint32_t t0 = millis();
    while (GPSSerial.available() <= 0 && millis() - t0 < 2000) delay(10);

    bool ok = true;
    // UBX-CFG-MSG (3-byte form: rate on the current port) = 0 for the NMEA
    // sentences TinyGPSPlus ignores: GLL, GSA, GSV, VTG. RMC and GGA stay --
    // GGA carries altitude, HDOP and satellite count.
    static const uint8_t kSilence[] = { 0x01, 0x02, 0x03, 0x05 };
    for (size_t i = 0; i < sizeof(kSilence); ++i) {
        uint8_t p[3] = { 0xF0, kSilence[i], 0 };
        int r = ubxCommand(0x06, 0x01, p, sizeof(p));
        if (r < 0 && i == 0) {
            // Nothing answered at all: the receiver isn't hearing us (wiring,
            // still booting). Don't spend seconds on the rest.
            Serial.println("WARNING: GPS did not acknowledge UBX config -- using its defaults.");
            return false;
        }
        ok &= (r == 1);
    }

    // UBX-CFG-NAV5: dynamic model "automotive" (4). Mask bit 0 applies only
    // the dynModel field; every other field in the payload is ignored.
    uint8_t nav5[36] = {0};
    nav5[0] = 0x01;
    nav5[2] = 4;
    ok &= (ubxCommand(0x06, 0x24, nav5, sizeof(nav5)) == 1);

    Serial.println(ok ? "GPS configured: RMC+GGA only, automotive model."
                      : "WARNING: GPS rejected part of the UBX config.");
    return ok;
}

// True when an RMC sentence carrying a valid fix has arrived since the last
// call. Reading value() clears the flag, so check both talker IDs each time.
static bool rmcFixArrived() {
    bool fix = false;
    if (gRmcStatusGN.isUpdated()) fix |= gRmcStatusGN.value()[0] == 'A';
    if (gRmcStatusGP.isUpdated()) fix |= gRmcStatusGP.value()[0] == 'A';
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
    fx.hdop      = gps.hdop.isValid() ? gps.hdop.hdop() : 99.9f;
    fx.monoMs    = millis();
    fx.hasUtc    = gClockSet.load();
    fx.utcMs     = fx.hasUtc ? nowEpochMs() : 0;
    return true;
}

// =================================================================
// setup / loop
// =================================================================
static bool mountTripStorage() {
    // A mount error is not proof that the partition is empty. Formatting
    // here would erase every queued trip after a recoverable mount error.
    // Provision a new device explicitly; see the firmware README.
    if (LittleFS.begin(false)) return true;
    Serial.println("ERROR: LittleFS mount failed. Storage preserved; halting. "
                   "See firmware README for first-time provisioning/recovery.");
    return false;
}

void setup() {
    Serial.begin(115200);
    delay(1000);
    Serial.println("\n=== Stage 4: Trip Logger ===");

    gFsMutex = xSemaphoreCreateMutex();

    // --- PMU + GPS power (Stages 2-3) ---
    Wire.begin(I2C_SDA, I2C_SCL);
    if (!PMU.begin(Wire, AXP2101_SLAVE_ADDRESS, I2C_SDA, I2C_SCL)) {
        Serial.println("ERROR: AXP2101 not found. Halting.");
        while (1) delay(1000);
    }
    // The NEO-M8N runs on ALDO3 -- measured on this board: with ALDO4 off
    // the GPS kept talking, with ALDO3 off it went silent. ALDO3 is on by
    // default at power-up, which is why earlier stages enabling ALDO4
    // appeared to work. Set it explicitly rather than rely on that.
    PMU.setALDO3Voltage(3300);
    PMU.enableALDO3();
    // GPS backup supply (VBACKUP): keeps the receiver's clock and satellite
    // data while ALDO3 is off in deep sleep, so a drive starts with a hot
    // start (~1 s) instead of a cold one (~30 s of unlogged driving). Off
    // by default on this board.
    PMU.setButtonBatteryChargeVoltage(3300);
    PMU.enableButtonBatteryCharge();
    // Full-charge voltage 4.1 V instead of the 4.2 V default: ~10% less
    // capacity, much less stress on a cell that lives in a hot car (the
    // charge temperature guard, below, covers charging when it's too hot).
    PMU.setChargeTargetVoltage(XPOWERS_AXP2101_CHG_VOL_4V1);
    // Enable the measurements the battery/VBUS predicates and the charge
    // temperature guard rely on.
    PMU.enableTemperatureMeasure();
    PMU.enableBattDetection();
    PMU.enableBattVoltageMeasure();
    PMU.enableVbusVoltageMeasure();
    PMU.enableSystemVoltageMeasure();
    setupPmuIrq();
    // Seed VBUS state from hardware: a battery-only boot (e.g. wake via
    // power key away from the car) must still hit the idle sleep timer.
    gVbusLost = !PMU.isVbusIn();
    gIdleSince = millis();
    Serial.printf("PMU ready, GPS rail (ALDO3) on, GPS backup %s, charge to %s. VBUS %s.\n",
                  PMU.isEnableButtonBatteryCharge() ? "on" : "OFF",
                  PMU.getChargeTargetVoltage() == XPOWERS_AXP2101_CHG_VOL_4V1 ? "4.1 V" : "NOT 4.1 V",
                  gVbusLost ? "absent (on battery)" : "present");

    // --- GPS UART. setRxBufferSize MUST precede begin(). ---
    GPSSerial.setRxBufferSize(2048);
    GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX_PIN, GPS_TX_PIN);

    // --- Storage + boot-time repair ---
    if (!mountTripStorage()) {
        while (1) delay(1000);
    }
    if (!LittleFS.exists(TRIP_DIR)) LittleFS.mkdir(TRIP_DIR);
    prefs.begin("mileage", false);
    fsckAllTrips();

    // After the fsck so the receiver has had time to boot.
    configureGps();

    // First charge-temperature check now, not 30 s in: a car that starts
    // hot must not charge in the meantime. (The temperature ADC was enabled
    // well over a conversion time ago, with the PMU.)
    serviceChargeTemp();

    // --- Networking task on core 0; GPS/FSM stay on core 1 (loop) ---
    if (xTaskCreatePinnedToCore(networkTask, "net", NET_STACK_BYTES,
                               nullptr, 1, nullptr, 0) != pdPASS) {
        Serial.println("ERROR: network task creation failed; trips remain queued for a later boot.");
    }

    Serial.println("Setup complete. Logging trips.");
}

void loop() {
    // Core 1: drain the GPS UART into the parser.
    while (GPSSerial.available() > 0) {
        if (gps.encode(GPSSerial.read())) setClockFromGps();
    }

    // Process one fix per epoch, on its RMC sentence. TinyGPSPlus commits
    // the location on both RMC and GGA, and the receiver sends both every
    // second, so gating on location.isUpdated() logged each position twice
    // -- the GGA copy carrying the previous epoch's speed. RMC holds the
    // position, speed and course together; altitude/HDOP/satellites come
    // from the latest GGA, at most one epoch old.
    if (rmcFixArrived()) {
        Fix fx;
        if (buildFix(fx)) {
            // Fix-loss detection: a long gap since the last good fix
            // means the next point should start a fresh segment. A gap
            // during ARMING invalidates the arm timer entirely (a stale
            // gArmT0 would otherwise start a trip on the very next fix).
            if (gHaveLastFix && (fx.monoMs - gLastFixMonoMs) > SEG_GAP_MS) {
                if (gState == ACTIVE || gState == COOLDOWN) {
                    gNeedNewSeg = true;
                } else if (gState == ARMING) {
                    gState = IDLE;
                    armBufReset();
                }
            }
            tripFsmOnFix(fx);
        }
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
    confirmVbusOff();
    serviceChargeTemp();

    // Battery idle sleep: on battery with no trip in progress for
    // IDLE_BATT_SLEEP_MS, deep sleep (GPS rail off, WiFi off). The 10 min
    // grace gives the network task ample time to upload after a drive.
    // VBUS insert wakes us when the car turns back on.
    if (!gVbusLost || gState != IDLE) {
        gIdleSince = millis();
    } else if (millis() - gIdleSince >= IDLE_BATT_SLEEP_MS) {
        deepSleepForVbus();
    }
}
