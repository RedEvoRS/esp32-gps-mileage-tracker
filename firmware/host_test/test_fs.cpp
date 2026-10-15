// Host harness for 04_trip_logger.ino's file-writing paths (audit F3/F4).
// Compiles the real sketch against stubs and drives the trip FSM with
// synthetic fixes, injecting open failures, flash write errors, and a full
// filesystem. Every surviving trip file is dumped to OUT_DIR so the real
// ingest parser can check it.
#include "arduino_stub.h"

uint32_t g_millis = 0;
uint64_t g_epochMs = 1776177000000ULL;   // 2026-04-14T14:30:00Z; fix times
uint64_t g_epochBaseMs = g_epochMs;      // system clock (stub)
SerialStub Serial;
FakeDisk g_disk;
LittleFSStub LittleFS;
int g_lockDepth = 0;
TwoWire Wire;
WiFiStub WiFi;

#include "04_trip_logger.ino"

#include <filesystem>
#include <fstream>

static int g_fail = 0;
static std::string g_outDir;
#define CHECK(cond, msg) do { if (cond) printf("  PASS %s\n", msg); \
    else { printf("  FAIL %s  (line %d)\n", msg, __LINE__); g_fail++; } } while (0)

static double gLat = 37.0, gLon = -122.0;

static void resetWorld() {
    g_disk = FakeDisk();
    LittleFS.dirs.clear();
    LittleFS.mkdir(TRIP_DIR);
    prefs.kv.clear();
    Serial.log.clear();
    gState = IDLE; gTripPath[0] = 0; gTripWriteFailed = false; gNeedNewSeg = false;
    gPendCount = gPendHead = 0; armBufReset(); gHaveLastFix = false;
    gClockSet = true;
    PMU = XPowersAXP2101(); gPmuIrqFlag = false;
    gVbusLost = false; gVbusOffPending = false;
    g_httpCode = 200; g_httpRequests = 0; g_httpLastBody.clear(); g_httpHook = nullptr;
    gChargePaused = false; gChargeApplied = false; gChargeCheckT0 = 0;
    gNetBusy = false; gNetHold = false; gNetAbort = false;
    gIdleSince = g_millis; g_deepSleeps = 0; g_lockDepth = 0;
    g_delayHook = nullptr;
}

// Run loop() every 100 ms for `ms`, stopping at deep sleep (the stub returns
// from it, but the real board never runs loop() again).
static void loopFor(uint32_t ms) {
    uint32_t end = g_millis + ms;
    while ((int32_t)(g_millis - end) < 0 && !g_deepSleeps) { g_millis += 100; loop(); }
}

// Advance the clock a second at a time with the die at `c`, servicing the
// charge guard as loop() does.
static void dieFor(float c, int seconds) {
    for (int i = 0; i < seconds; ++i) {
        PMU.dieC = c;
        g_millis += 1000;
        serviceChargeTemp();
    }
}

static int logCount(const char* frag) {
    int n = 0;
    for (auto& l : Serial.log) if (l.find(frag) != std::string::npos) n++;
    return n;
}

// One fix per second at `mps`, heading north.
static void drive(float mps, int seconds) {
    for (int i = 0; i < seconds; ++i) {
        g_millis += 1000; g_epochMs += 1000;
        gLat += mps / 111320.0;
        Fix fx{};
        fx.lat = gLat; fx.lon = gLon; fx.speedMps = mps; fx.hdop = 0.9f;
        fx.monoMs = g_millis; fx.hasUtc = true; fx.utcMs = g_epochMs;
        tripFsmOnFix(fx);
        pollPmu();          // as loop() does after each fix
        confirmVbusOff();
    }
}

// USB power changes as the PMU reports them: level + latched interrupt.
static void vbus(bool on) {
    PMU.vbusIn = on;
    (on ? PMU.insertIrq : PMU.removeIrq) = true;
    gPmuIrqFlag = true;
}

static std::vector<std::string> tripFiles() {
    std::vector<std::string> v;
    for (auto& kv : g_disk.files) if (kv.first.rfind("/trips/", 0) == 0) v.push_back(kv.first);
    return v;
}
static int count(const std::string& s, const std::string& tok) {
    int n = 0; for (size_t p = s.find(tok); p != std::string::npos; p = s.find(tok, p + 1)) n++; return n;
}
static bool logHas(const char* frag) {
    for (auto& l : Serial.log) if (l.find(frag) != std::string::npos) return true; return false;
}
static bool wellFormedShape(const std::string& d) {
    // prologue at start, epilogue at end, every trkpt line complete
    if (d.rfind("<?xml", 0) != 0) return false;
    if (d.size() < 22 || d.compare(d.size() - 22, 22, "</trkseg></trk></gpx>\n") != 0) return false;
    return count(d, "<trkpt ") == count(d, "</trkpt>");
}
// setup()'s halt loops never return: a delay() hook throws this to stop one.
struct Halted {};
static int  g_haltSecs = 0;
static bool g_chargeAtMount = false, g_chargeWhileHot = false, g_chargeWhenCool = false;

static void dump(const char* test) {
    int i = 0;
    for (auto& f : tripFiles()) {
        std::ofstream o(g_outDir + "/" + test + "_" + std::to_string(i++) + ".gpx", std::ios::binary);
        o << g_disk.files[f];
    }
}

int main(int argc, char** argv) {
    g_outDir = argc > 1 ? argv[1] : ".";

    printf("T0 trkpt format (GPX 1.0, slim)\n");
    {
        resetWorld();
        Fix fx{}; fx.lat = -33.868820; fx.lon = 151.209296; fx.speedMps = 27.123f;
        fx.hdop = 12.3f; fx.hasUtc = true; fx.utcMs = g_epochMs;
        const std::string pt = "<trkpt lat=\"-33.868820\" lon=\"151.209296\">"
            "<time>2026-04-14T14:30:00Z</time><speed>27.12</speed><hdop>12.3</hdop></trkpt>\n";
        for (int seg = 0; seg < 2; ++seg) {
            g_disk.files["/t.gpx"] = "";
            File f = LittleFS.open("/t.gpx", FILE_APPEND);
            FileWriter w(f); gNeedNewSeg = seg; appendTrkpt(w, fx);
            CHECK(w.commit(), "commit ok");
            f.close();
            CHECK(g_disk.files["/t.gpx"] == (seg ? "</trkseg><trkseg>\n" + pt : pt),
                  seg ? "exact line (with segment break)" : "exact line");
            CHECK(!gNeedNewSeg, "segment flag consumed");
        }
        // A typical point, against the F1 budget.
        fx.lat = 37.123456; fx.lon = -122.123456; fx.speedMps = 15; fx.hdop = 0.9f;
        g_disk.files["/t.gpx"] = "";
        File f = LittleFS.open("/t.gpx", FILE_APPEND); FileWriter w(f); appendTrkpt(w, fx); w.commit(); f.close();
        size_t n = g_disk.files["/t.gpx"].size();
        printf("  (typical point: %zu bytes, was 281)\n", n);
        CHECK(n <= 120, "typical point fits the ~118 B budget");
        // A garbage speed value must not overflow the line buffer and be
        // mistaken for a write failure (which would end the trip).
        fx.speedMps = 1e30f;
        g_disk.files["/t.gpx"] = "";
        f = LittleFS.open("/t.gpx", FILE_APPEND); FileWriter w2(f); appendTrkpt(w2, fx);
        CHECK(w2.commit(), "absurd speed value still writes");
        f.close();
    }

    printf("T1 normal trip\n");
    {
        resetWorld();
        drive(0, 5); drive(15, 75); drive(0, 200);
        auto v = tripFiles();
        CHECK(gState == IDLE && v.size() == 1, "one finalized trip, FSM idle");
        if (v.size() == 1) {
            auto& d = g_disk.files[v[0]];
            CHECK(wellFormedShape(d), "complete, well-formed shape");
            CHECK(count(d, "<trkpt ") == 75 + 181, "all fixes recorded (ARMING, driving, 181 cooldown)");
        }
        dump("T1");
    }

    printf("T2 trip file can't be opened (F3)\n");
    {
        resetWorld();
        g_disk.failNextOpens = 1;
        drive(15, 11);
        CHECK(gState != ACTIVE && gTripPath[0] == 0, "stays out of ACTIVE, no active path");
        CHECK(tripFiles().empty(), "no file left behind");
        CHECK(logHas("could not create trip file"), "logged loudly");
        drive(15, 40); drive(0, 200);
        auto v = tripFiles();
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "re-arms and records a valid trip");
        dump("T2");
    }

    printf("T3 prologue write doesn't land\n");
    {
        resetWorld();
        g_disk.failFlushes = 1;
        drive(15, 11);
        CHECK(gState != ACTIVE && gTripPath[0] == 0, "stays out of ACTIVE");
        CHECK(tripFiles().empty(), "half-written file removed");
    }

    printf("T4 flash write error mid-trip (F4)\n");
    {
        resetWorld();
        drive(15, 40);
        CHECK(gState == ACTIVE, "trip running");
        g_disk.failFlushes = 1;
        drive(15, 1);
        CHECK(gState == IDLE && gTripPath[0] == 0, "trip ended on the failed write");
        CHECK(logHas("write to /trips/t000001.gpx failed"), "logged loudly");
        auto v = tripFiles();
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "file trimmed and closed: valid");
        CHECK(!g_disk.files.count("/trips/t000001.gpx.fix"), "no .fix left over");
        drive(15, 40); drive(0, 200);
        CHECK(tripFiles().size() == 2, "next trip starts normally");
        dump("T4");
    }

    printf("T5 filesystem fills mid-trip\n");
    {
        resetWorld();
        drive(15, 30);
        g_disk.capacity = g_disk.used() + 300;   // room for ~2.5 more points;
                                                  // under the guard's 30 s check
        drive(15, 3);
        CHECK(gTripPath[0] == 0 && gState != ACTIVE && gState != COOLDOWN, "trip ended when the filesystem filled");
        auto v = tripFiles();
        const std::string& d = g_disk.files[v[0]];
        CHECK(v.size() == 1 && d.find("</gpx>") == std::string::npos, "left incomplete (no room to repair)");
        CHECK(count(d, "<trkpt ") > count(d, "</trkpt>"), "a partial point sits at the tail (as on real flash)");
        CHECK(!isComplete(v[0].c_str()), "incomplete file is not offered for upload");
        CHECK(logHas("left for boot-time repair"), "logged loudly");
        drive(15, 20);
        CHECK(tripFiles().size() == 1 && gTripPath[0] == 0, "no new trip while full, nothing left behind");
        g_disk.capacity = 1 << 22;                // uploads freed space
        fsckAllTrips();
        CHECK(wellFormedShape(g_disk.files[v[0]]), "boot fsck repairs it once there is room");
        dump("T5");
    }

    printf("T6 epilogue write doesn't land\n");
    {
        resetWorld();
        drive(15, 40); drive(0, 180);
        CHECK(gState == COOLDOWN, "about to finalize");
        g_disk.flushesBeforeFail = 1;            // the last point lands...
        g_disk.failFlushes = 1;                  // ...the epilogue doesn't
        drive(0, 1);
        auto v = tripFiles();
        CHECK(gState == IDLE && v.size() == 1, "finalized");
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "repaired to a valid file");
        dump("T6");
    }

    printf("T7 trip file vanishes mid-trip\n");
    {
        resetWorld();
        drive(15, 40);
        g_disk.files.erase(gTripPath);
        drive(15, 1);
        CHECK(gState == IDLE, "trip ended");
        CHECK(tripFiles().empty(), "no epilogue-only or empty file created");
    }

    printf("T8 micro-trip still discarded\n");
    {
        resetWorld();
        drive(3, 12); drive(0, 200);
        CHECK(tripFiles().empty() && logHas("Discarding micro-trip"), "discarded");
    }

    printf("T9 repair copy fails -> original kept\n");
    {
        resetWorld();
        std::string partial = std::string(GPX_PROLOGUE) + "<trkpt lat=\"1\" lon=\"2\"></trkpt>\n<trkpt lat=";
        g_disk.files["/trips/t000009.gpx"] = partial;
        g_disk.failFlushes = 1;
        CHECK(!repairIfNeeded("/trips/t000009.gpx"), "reports failure");
        CHECK(g_disk.files["/trips/t000009.gpx"] == partial, "original untouched");
        CHECK(!g_disk.files.count("/trips/t000009.gpx.fix"), "bad copy removed");
    }

    printf("T10 free-space guard ends a trip before the partition fills (F1)\n");
    {
        resetWorld();
        drive(15, 40);
        CHECK(gState == ACTIVE, "trip running");
        g_disk.capacity = g_disk.used() + 30 * 1024;   // 30 KB left
        drive(15, 120);
        auto v = tripFiles();
        CHECK(gTripPath[0] == 0 && v.size() == 1, "trip ended by the guard");
        CHECK(logHas("storage nearly full"), "logged loudly");
        CHECK(!logHas("failed"), "no write ever failed");
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "file complete");
        CHECK(v.size() == 1 && isComplete(v[0].c_str()), "offered for upload");
        CHECK(g_disk.capacity - g_disk.used() >= 20 * 1024, "stopped with the reserve intact");
        dump("T10");
    }

    printf("T11 no trip starts while storage is full (F1)\n");
    {
        resetWorld();
        g_disk.capacity = 60 * 1024;                   // under the 64 KB start threshold
        drive(15, 180);
        int warns = 0;
        for (auto& l : Serial.log) if (l.find("trip storage full") != std::string::npos) warns++;
        CHECK(tripFiles().empty() && gTripPath[0] == 0, "nothing recorded, no file created");
        CHECK(warns >= 2 && warns <= 4, "warns about once a minute, not on every fix");
        g_disk.capacity = 1 << 22;                     // uploads freed space
        drive(15, 40); drive(0, 200);
        auto v = tripFiles();
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "records again once there's room");
    }

    printf("T12 USB dips while driving (cranking, start-stop) don't split the trip (F10)\n");
    {
        resetWorld();
        drive(15, 40);
        vbus(false); drive(15, 2); vbus(true);      // 2 s brown-out
        drive(15, 30);
        vbus(false); drive(0, 1); vbus(true);       // start-stop restart at a light
        drive(15, 30); drive(0, 200);
        auto v = tripFiles();
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "one trip, not three");
        CHECK(logCount("dipped and came back") == 2 && !logHas("car off"), "both dips ignored");
        CHECK(!gVbusLost, "still on USB power");
    }

    printf("T13 car switched off: trip finalized after 10 s (F10)\n");
    {
        resetWorld();
        drive(15, 40); drive(0, 5);
        vbus(false);
        drive(0, 9);
        CHECK(gTripPath[0] != 0, "not yet at 9 s");
        drive(0, 2);
        auto v = tripFiles();
        CHECK(gTripPath[0] == 0 && gVbusLost, "finalized at 10 s, on battery");
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "file complete");
        CHECK(logHas("car off"), "logged");
    }

    printf("T14 missed INSERT interrupt: level re-read before acting (F10)\n");
    {
        resetWorld();
        drive(15, 40);
        vbus(false); drive(15, 3);
        PMU.vbusIn = true;                          // power back, no interrupt seen
        drive(15, 15);
        CHECK(gState == ACTIVE && gTripPath[0] != 0 && !gVbusLost, "trip continues");
    }

    printf("T15 REMOVE and INSERT latched together (F10)\n");
    {
        resetWorld();
        drive(15, 40);
        PMU.vbusIn = true; PMU.removeIrq = PMU.insertIrq = true; gPmuIrqFlag = true;
        drive(15, 15);
        CHECK(gState == ACTIVE && !gVbusLost, "power is on: nothing to do");
        PMU.vbusIn = false; PMU.removeIrq = PMU.insertIrq = true; gPmuIrqFlag = true;
        drive(0, 11);
        CHECK(gTripPath[0] == 0 && gVbusLost, "power is off: confirmed and finalized");
    }

    printf("T16 rejected uploads are set aside, not retried forever (F6)\n");
    {
        resetWorld();
        drive(15, 40); drive(0, 200);
        auto v = tripFiles();
        std::string path = v.empty() ? "" : v[0], content = g_disk.files[path];
        g_httpCode = 500; uploadPending();
        CHECK(g_httpRequests == 1 && g_httpLastBody == content, "the whole file is sent");
        CHECK(g_disk.files.count(path) && hasPending(), "500: kept and retried");
        g_httpCode = -1; uploadPending();
        CHECK(g_disk.files.count(path) && hasPending(), "network error: kept and retried");
        g_httpCode = 401; uploadPending();
        CHECK(g_disk.files.count(path) && hasPending(), "401 (token mismatch): kept and retried");
        g_httpCode = 400; uploadPending();
        CHECK(!g_disk.files.count(path) && g_disk.files.count(path + ".bad"), "400: set aside as .bad");
        CHECK(g_disk.files[path + ".bad"] == content, ".bad file intact for inspection");
        CHECK(logHas("server rejected the file"), "logged loudly");
        int before = g_httpRequests;
        CHECK(!hasPending(), "nothing left to upload");
        uploadPending();
        CHECK(g_httpRequests == before, "never resent");
        fsckAllTrips();
        CHECK(g_disk.files[path + ".bad"] == content, "fsck leaves it alone");

        drive(15, 40); drive(0, 200);              // next trip: 413
        g_httpCode = 413; uploadPending();
        CHECK(tripFiles().size() == 2 && !hasPending(), "413: set aside as .bad too");

        drive(15, 40); drive(0, 200);              // next trip: accepted
        g_httpCode = 200; uploadPending();
        CHECK(tripFiles().size() == 2 && !hasPending(), "200: uploaded and deleted");
    }

    printf("T17 hot car: charging pauses at once, resumes as the AC cools it (N4)\n");
    {
        resetWorld();
        PMU.dieC = 65;                               // cabin ~56 C at start-up
        serviceChargeTemp();                         // setup()'s first check
        CHECK(!PMU.chargeEnabled && logHas("too hot"), "paused at boot, before any charging");
        float resumedAt = -1;
        for (float c = 65; c >= 35; c -= 0.5f) {     // AC: ~1 C a minute
            dieFor(c, 30);
            if (resumedAt < 0 && PMU.chargeEnabled) resumedAt = c;
        }
        printf("  (resumed at die %.1f C ~ battery %.1f C)\n", resumedAt, resumedAt - DIE_OFFSET_C);
        CHECK(resumedAt <= CHG_HOT_RESUME_C && resumedAt > CHG_HOT_RESUME_C - 1, "resumed on its own near 47 C");
        CHECK(PMU.chargeEnabled, "charging at the end");
    }

    printf("T18 charger self-heating can't make it toggle (N4)\n");
    {
        resetWorld();
        // Die = cabin-driven 51 C, +4 C while charging (measured ~2-5 C).
        int toggles = 0; bool last = true;
        PMU.chargeEnabled = true;
        for (int i = 0; i < 60 * 30; ++i) {          // 30 minutes
            dieFor(PMU.chargeEnabled ? 55.0f : 51.0f, 1);
            if (PMU.chargeEnabled != last) { toggles++; last = PMU.chargeEnabled; }
        }
        CHECK(toggles == 1 && !PMU.chargeEnabled, "pauses once and stays paused");
        dieFor(50, 120);                             // still above the resume line
        CHECK(!PMU.chargeEnabled, "no resume between 47 and 54 C");
    }

    printf("T19 a pause left over from before a reset doesn't stick (N4)\n");
    {
        resetWorld();
        PMU.chargeEnabled = false;                   // PMU state survives an ESP32 reset
        PMU.dieC = 35;
        serviceChargeTemp();
        CHECK(PMU.chargeEnabled, "re-enabled on the first check");
    }

    printf("T20 cold, unreadable sensor, check interval (N4)\n");
    {
        resetWorld();
        PMU.dieC = 5; serviceChargeTemp();
        CHECK(!PMU.chargeEnabled && logHas("too cold"), "paused below ~0 C battery");
        dieFor(12, 60);
        CHECK(!PMU.chargeEnabled, "still paused at ~3 C battery");
        dieFor(15, 60);
        CHECK(PMU.chargeEnabled, "resumed above ~5 C battery");
        dieFor(NAN, 60);
        CHECK(!PMU.chargeEnabled && logHas("unreadable"), "unreadable sensor: fail safe");
        dieFor(35, 60);
        CHECK(PMU.chargeEnabled, "recovers when the sensor reads again");
        dieFor(60, 10);
        CHECK(PMU.chargeEnabled, "checked every 30 s, not every loop");
        dieFor(60, 30);
        CHECK(!PMU.chargeEnabled, "acted on at the next check");
    }

    printf("T21 repair rename failure preserves the original for a later boot\n");
    {
        resetWorld();
        const std::string path = "/trips/t000021.gpx";
        const std::string partial = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>\n<trkpt lat=";
        g_disk.files[path] = partial;
        g_disk.failRenames = 1;
        CHECK(!repairIfNeeded(path.c_str()), "rename failure is reported");
        CHECK(g_disk.files.count(path) && g_disk.files[path] == partial,
              "original survives failed replacement byte for byte");
        CHECK(g_disk.removeCount == 0, "no delete-before-rename power-loss window");
        CHECK(g_disk.files.count(path + ".fix"), "complete repair copy also retained");
        CHECK(!isComplete(path.c_str()), "partial original cannot upload");
        fsckAllTrips();
        CHECK(g_disk.files.count(path) && wellFormedShape(g_disk.files[path]),
              "next boot repairs and publishes the queued trip");
        CHECK(!g_disk.files.count(path + ".fix"), "successful rename consumes temporary copy");
    }

    printf("T22 reset or stale NVS counter cannot overwrite queued trips\n");
    {
        resetWorld();
        drive(15, 40); endTrip(false);
        const std::string first = g_disk.files["/trips/t000001.gpx"];
        prefs.kv.clear();                         // NVS reset, LittleFS retained
        g_disk.files["/trips/t000002.gpx.bad"] = "quarantined evidence";
        g_disk.files["/trips/t000003.gpx.fix"] = "repair evidence";
        drive(15, 40); endTrip(false);
        CHECK(g_disk.files["/trips/t000001.gpx"] == first, "first trip is not truncated");
        CHECK(g_disk.files.count("/trips/t000004.gpx") &&
              wellFormedShape(g_disk.files["/trips/t000004.gpx"]), "new trip gets a free filename");
        CHECK(g_disk.files["/trips/t000002.gpx.bad"] == "quarantined evidence" &&
              g_disk.files["/trips/t000003.gpx.fix"] == "repair evidence",
              "quarantined and repair names remain reserved");
        prefs.kv.clear();                         // another failed persistence/reset
        drive(15, 40); endTrip(false);
        CHECK(g_disk.files.count("/trips/t000005.gpx"), "repeated stale counter still selects a free name");
        CHECK(g_disk.files["/trips/t000001.gpx"] == first, "oldest queued trip remains intact");
    }

    printf("T23 mount failure must not autoformat queued mileage\n");
    {
        resetWorld();
        g_disk.files["/trips/t000023.gpx"] = "irreplaceable trip";
        g_disk.failMount = true;
        CHECK(!mountTripStorage(), "mount error stops initialization");
        CHECK(!g_disk.formatRequested, "format-on-failure is disabled");
        CHECK(g_disk.files["/trips/t000023.gpx"] == "irreplaceable trip", "queued bytes preserved");
        g_disk.failMount = false;
        CHECK(mountTripStorage(), "a later successful mount recovers normally");
        CHECK(g_disk.files["/trips/t000023.gpx"] == "irreplaceable trip", "successful mount also preserves data");
    }

    printf("T24 boot repair snapshots the directory before mutating it\n");
    {
        resetWorld();
        g_disk.invalidateDirectoryOnMutation = true;
        const std::string partial = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>\n<trkpt lat=";
        g_disk.files["/trips/a.gpx"] = "empty trip";
        g_disk.files["/trips/b.gpx"] = partial;
        g_disk.files["/trips/c.gpx"] = partial;
        g_disk.files["/trips/d.gpx"] = partial;
        g_disk.files["/trips/e.gpx.bad"] = "quarantined evidence";
        fsckAllTrips();
        CHECK(!g_disk.files.count("/trips/a.gpx"), "garbage entry removed");
        for (const char* path : {"/trips/b.gpx", "/trips/c.gpx", "/trips/d.gpx"})
            CHECK(wellFormedShape(g_disk.files[path]), "every snapshotted trip repaired");
        CHECK(g_disk.files["/trips/e.gpx.bad"] == "quarantined evidence", "other files untouched");
        CHECK(g_disk.mutationsWithDirectoryOpen == 0, "no mutation while directory is open");
        CHECK(g_disk.openDirectories == 0 && g_lockDepth == 0, "directory and lock released");
    }

    printf("T25 network stack diagnostics report bytes and flag low headroom\n");
    {
        resetWorld();
        g_stackHighWaterBytes = 4096;
        reportNetworkStack("test");
        CHECK(logHas("minimum free 4096 / 12288 bytes"), "watermark uses ESP-IDF bytes without word conversion");
        CHECK(!logHas("WARNING"), "adequate simulated headroom has no warning");
        g_stackHighWaterBytes = 1024;
        reportNetworkStack("test");
        CHECK(logHas("WARNING: low headroom"), "low simulated watermark requests attention");
        g_stackHighWaterBytes = 4096;
    }

    printf("T26 stale repair copies are reclaimed only after comparing their bytes\n");
    {
        const std::string path = "/trips/t000026.gpx";
        const std::string points = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>";
        const std::string complete = points + "\n" + GPX_EPILOGUE;
        for (const std::string& stale : {std::string(), points.substr(0, 80), complete}) {
            resetWorld();
            g_disk.files[path] = complete;
            g_disk.files[path + ".fix"] = stale;
            fsckAllTrips();
            CHECK(g_disk.files[path] == complete, "complete original kept byte for byte");
            CHECK(!g_disk.files.count(path + ".fix"), "redundant repair bytes reclaimed");
            int removes = g_disk.removeCount;
            fsckAllTrips();
            CHECK(g_disk.removeCount == removes, "second boot makes no further changes");
        }
        resetWorld();
        g_disk.files[path] = points + "\n<trkpt lat=";
        g_disk.files[path + ".fix"] = complete;
        g_disk.capacity = g_disk.used();
        fsckAllTrips();
        CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
              "verified finished repair publishes without extra free space");
        resetWorld();
        g_disk.files[path] = points + "\n<trkpt lat=";
        g_disk.files[path + ".fix"] = points + "\n</trkseg>";
        fsckAllTrips();
        CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
              "interrupted repair epilogue is safely regenerated");
        resetWorld();
        const std::string partial = points + "\n<trkpt lat=";
        g_disk.files[path] = partial;
        g_disk.files[path + ".fix"] = complete;
        g_disk.failRenames = 1;
        fsckAllTrips();
        CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == complete,
              "failed publish of existing repair leaves both copies intact");
        fsckAllTrips();
        CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
              "existing repair rename retries on next boot");
    }

    printf("T27 orphan repair copies recover without discarding the only copy\n");
    {
        const std::string path = "/trips/t000027.gpx";
        const std::string points = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>";
        const std::string complete = points + "\n" + GPX_EPILOGUE;
        for (const std::string& orphan : {complete, points + "\n<trkpt lat="}) {
            resetWorld();
            g_disk.files[path + ".fix"] = orphan;
            fsckAllTrips();
            CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
                  "complete points from orphan become a queued GPX");
            fsckAllTrips();
            CHECK(g_disk.files[path] == complete, "recovered trip survives repeated boot");
        }
        for (const std::string& unknown : {std::string("unrecognized evidence"), std::string(GPX_PROLOGUE)}) {
            resetWorld();
            g_disk.files[path + ".fix"] = unknown;
            fsckAllTrips();
            CHECK(g_disk.files[path + ".fix"] == unknown && !g_disk.files.count(path),
                  "unrecoverable sole copy retained without publishing");
        }
        resetWorld();
        g_disk.files[path + ".fix"] = complete;
        g_disk.failRenames = 1;
        fsckAllTrips();
        CHECK(g_disk.files[path + ".fix"] == complete && !g_disk.files.count(path),
              "failed orphan rename preserves sole copy");
        fsckAllTrips();
        CHECK(g_disk.files[path] == complete, "orphan rename retried next boot");

        for (bool full : {false, true}) {
            resetWorld();
            const std::string partial = points + "\n<trkpt lat=";
            g_disk.files[path + ".fix"] = partial;
            if (full) g_disk.capacity = g_disk.used();
            else g_disk.failFlushes = 2;
            fsckAllTrips();
            CHECK(g_disk.files[path] == partial, "failed orphan repair preserves renamed original");
            CHECK(!isComplete(path.c_str()), "failed repair is not uploaded");
            g_disk.capacity = 1 << 22;
            fsckAllTrips();
            CHECK(g_disk.files[path] == complete, "repair retries after storage recovers");
        }
    }

    printf("T28 ambiguous repair collisions and IO failures preserve both copies\n");
    {
        const std::string path = "/trips/t000028.gpx";
        const std::string partial = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>\n<trkpt lat=";
        const std::string other = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"3\" lon=\"4\"></trkpt>\n" + GPX_EPILOGUE;
        resetWorld();
        g_disk.files[path] = partial;
        g_disk.files[path + ".fix"] = other;
        fsckAllTrips();
        CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == other,
              "unequal original and repair copy both retained");
        CHECK(g_disk.flushCount == 0 && g_disk.removeCount == 0, "ambiguous copy is never truncated");
        resetWorld();
        g_disk.files[path + ".bad"] = "quarantined evidence";
        g_disk.files[path + ".fix"] = other;
        fsckAllTrips();
        CHECK(g_disk.files[path + ".fix"] == other && !g_disk.files.count(path),
              "quarantined trip name is not republished");
        for (int fault = 0; fault < 5; ++fault) {
            resetWorld();
            const std::string stale = partial.substr(0, 80);
            g_disk.files[path] = partial;
            g_disk.files[path + ".fix"] = stale;
            if (fault == 0) g_disk.failOpenPath = path;
            if (fault == 1) g_disk.failOpenPath = path + ".fix";
            if (fault == 2) g_disk.failReadPath = path;
            if (fault == 3) g_disk.failReadPath = path + ".fix";
            if (fault == 4) g_disk.failRemoves = 1;
            fsckAllTrips();
            CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == stale,
                  "open/read/cleanup failure leaves original and temporary unchanged");
            g_disk.failOpenPath.clear(); g_disk.failReadPath.clear();
            fsckAllTrips();
            CHECK(wellFormedShape(g_disk.files[path]) && !g_disk.files.count(path + ".fix"),
                  "next boot completes after IO failure clears");
        }
    }

    printf("T29 incomplete reads cannot authorize recovery or removal\n");
    {
        const std::string path = "/trips/t000029.gpx";
        const std::string first = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>";
        const std::string partial = first + std::string(600, ' ') +
            "<trkpt lat=\"3\" lon=\"4\"></trkpt>\n<trkpt lat=";
        const std::string shorter = first + "\n" + GPX_EPILOGUE;
        resetWorld();
        g_disk.files[path] = partial;
        g_disk.files[path + ".fix"] = shorter;
        g_disk.failReadPath = path;
        g_disk.failReadAfter = 512;
        fsckAllTrips();
        CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == shorter,
              "read failure after first point cannot publish shorter repair");
        resetWorld();
        g_disk.files[path + ".fix"] = partial;
        g_disk.failReadPath = path + ".fix";
        g_disk.failReadAfter = 512;
        fsckAllTrips();
        CHECK(g_disk.files[path + ".fix"] == partial && !g_disk.files.count(path),
              "partially unreadable orphan stays intact and unpublished");
        resetWorld();
        g_disk.files[path] = partial;
        g_disk.failReadPath = path;
        fsckAllTrips();
        CHECK(g_disk.files[path] == partial && g_disk.removeCount == 0,
              "unreadable original is not classified as garbage");
    }

    // Issue #3. gNetBusy = true stands in for core 0 mid-upload; these
    // check the handshake, not real multicore timing.
    printf("T30 idle sleep lets a running upload finish, then sleeps (#3)\n");
    {
        resetWorld();
        drive(15, 40); drive(0, 5);
        vbus(false); drive(0, 11);                   // car off: trip finalized
        CHECK(gVbusLost && gState == IDLE && hasPending(), "trip queued, on battery");
        gIdleSince = g_millis;                       // as loop() last saw the trip
        gNetBusy = true;                             // uploading it
        loopFor(IDLE_BATT_SLEEP_MS + 1000);
        CHECK(g_deepSleeps == 0 && gNetHold, "10 min: no sleep yet, network task told to stop");
        CHECK(logHas("letting the upload finish"), "logged");
        loopFor(60000);
        CHECK(g_deepSleeps == 0, "keeps waiting while the upload runs");
        gNetBusy = false;                            // its file is done
        loopFor(1000);
        CHECK(g_deepSleeps == 1 && !gNetAbort, "sleeps once it's idle, nothing cut off");
        CHECK(!g_disk.mounted && g_lockDepth == 1, "storage unmounted, lock kept into sleep");

        resetWorld();
        gVbusLost = true; gNetBusy = true;
        loopFor(IDLE_BATT_SLEEP_MS + 1000);
        CHECK(g_deepSleeps == 0 && gNetHold, "waiting on the upload");
        vbus(true); loopFor(1000);
        CHECK(!gVbusLost && !gNetHold && g_deepSleeps == 0, "car back on: wait cancelled, uploads resume");
    }

    printf("T31 an upload that won't finish is cut off, and storage stays locked (#3)\n");
    {
        resetWorld();
        gVbusLost = true; gNetBusy = true;           // and it never finishes
        loopFor(IDLE_BATT_SLEEP_MS + NET_DRAIN_MS - 1000);
        CHECK(g_deepSleeps == 0 && !gNetAbort, "waits out the drain limit");
        loopFor(2000 + NET_ABORT_MS);
        CHECK(g_deepSleeps == 1 && gNetAbort, "then cuts the upload off and sleeps");
        CHECK(logHas("sleeping anyway") && g_lockDepth == 1, "a stuck task still can't reach storage");

        resetWorld();                                // critical battery / power key
        drive(15, 40); drive(0, 5);
        gNetBusy = true;
        uint32_t t0 = g_millis;
        deepSleepForVbus();                          // as pollPmu() calls it
        CHECK(gNetAbort && g_millis - t0 <= NET_ABORT_MS + 100, "upload cut off without waiting for it");
        auto v = tripFiles();
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "trip finalized first");
        CHECK(g_deepSleeps == 1 && g_lockDepth == 1, "asleep with storage locked");
    }

    printf("T32 network task: stops between files, ends a cut-off body, starts nothing when held (#3)\n");
    {
        resetWorld();
        drive(15, 40); drive(0, 200);
        drive(15, 40); drive(0, 200);
        CHECK(tripFiles().size() == 2, "two trips queued");
        g_httpHook = [] { gNetHold = true; };        // sleep asked for mid-upload
        uploadPending();
        CHECK(g_httpRequests == 1 && tripFiles().size() == 1, "current file finished, next not started");

        auto left = tripFiles();
        std::string content = left.empty() ? "" : g_disk.files[left[0]];
        gNetHold = false; g_httpRequests = 0;
        g_httpHook = [] { gNetHold = true; gNetAbort = true; };   // cut off at once
        uploadPending();
        CHECK(g_httpRequests == 1 && g_httpLastBody.empty(), "body stops at the abort");
        CHECK(!left.empty() && g_disk.files[left[0]] == content && logHas("will retry"),
              "trip kept for next time");

        g_httpHook = nullptr; gNetAbort = false;
        Serial.log.clear();
        gNetHold = true; netCycle();
        CHECK(!logHas("after network cycle") && !gNetBusy, "held: no cycle starts");
        gNetHold = false; netCycle();
        CHECK(logHas("after network cycle") && !gNetBusy, "released: cycle runs and clears busy");
    }

    printf("T33 storage that won't mount: the charge guard keeps running (#55)\n");
    {
        resetWorld();
        g_disk.files["/trips/t000033.gpx"] = "irreplaceable trip";
        g_disk.failMount = true;
        PMU.chargeEnabled = true;                    // left on by the last boot
        PMU.dieC = 60;                               // parked in the sun
        g_disk.onMount = [] { g_chargeAtMount = PMU.chargeEnabled; };
        g_haltSecs = 0;
        g_delayHook = [](uint32_t ms) {
            g_millis += ms;
            if (!logHas("LittleFS mount failed")) return;   // setup()'s start-up delay
            ++g_haltSecs;
            if (g_haltSecs == 60)  { g_chargeWhileHot = PMU.chargeEnabled; PMU.dieC = 35; }   // AC on
            if (g_haltSecs == 120) { g_chargeWhenCool = PMU.chargeEnabled; PMU.dieC = 60; }  // parked again
            if (g_haltSecs == 180) throw Halted{};
        };
        try { setup(); } catch (const Halted&) {}
        g_delayHook = nullptr;
        CHECK(g_haltSecs == 180, "setup() halts on the mount error");
        CHECK(!g_chargeAtMount, "charging off before storage is touched");
        CHECK(!g_chargeWhileHot, "hot: charging stays off while halted");
        CHECK(g_chargeWhenCool, "cooled: the halted guard turns charging back on");
        CHECK(!PMU.chargeEnabled && logCount("too hot") == 2, "hot again: paused again");
        CHECK(!g_disk.formatRequested && g_disk.files["/trips/t000033.gpx"] == "irreplaceable trip",
              "queued trip neither formatted nor erased");

        resetWorld();                                // normal boot, cool car
        PMU.chargeEnabled = true;
        PMU.dieC = 35;
        g_chargeAtMount = true;
        g_disk.onMount = [] { g_chargeAtMount = PMU.chargeEnabled; };
        setup();
        CHECK(!g_chargeAtMount, "normal boot: charging off while storage starts");
        CHECK(PMU.chargeEnabled && logHas("Setup complete"), "then the first check turns it on");
    }

    printf(g_fail ? "\n%d FAILED\n" : "\nALL PASS\n", g_fail);
    return g_fail ? 1 : 0;
}
