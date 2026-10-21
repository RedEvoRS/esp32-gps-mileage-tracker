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
TwoWire Wire, Wire1;
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
    gDebugMode.store(hostDebugMode());
    gProductionDisplayReady = gProductionDisplayFailed = gProductionDisplayBegun = false;
    gProductionHaveOutput = gProductionWireReady = false;
    gModeFeedback = false; gDisplayOwner = DisplayOwner::Production;
    gDisplayModel = display::DisplayModel();
    gDegraded = startup::DegradedPolicy();
    gFsDemand = 0; gJournalReady = gJournalStarted = false;
    gJournalSealing = gJournalBootReported = false; gJournalSealRequested = gJournalSealDone = gJournalSealOk = false;
    gJournal.~EventLog(); new (&gJournal) diagnostics::EventLog(&gSdJournalFs, &gFlashJournalFs);
    g_disk = FakeDisk();
    g_sdDisk = FakeDisk(); g_sdDisk.mounted = false; g_sdDisk.renameOverwrites = false;
    SD.dirs.clear(); SD.cardPresent = false; SD.begins = SD.ends = 0;
    gTripFS = &LittleFS; gSdMounted = false; gFlashMounted = true;
    g_powerEvents.clear();
    g_partition = PartitionStub();
    LittleFS.dirs.clear();
    LittleFS.mkdir(TRIP_DIR);
    prefs.kv.clear();
    Serial.log.clear();
    GPSSerial = HardwareSerial(1);
    gGpsConfig = GpsConfig(); gGpsUbxRx = GpsUbxRx();
    gState = IDLE; gTripPath[0] = 0; gTripWriteFailed = false;
    gTripBytes = 0;
    gPendCount = gPendHead = 0; armBufReset(); gHaveLastFix = false;
    gClockSet = true;
    PMU = XPowersAXP2101(); gPmuIrqFlag = false;
    Wire = TwoWire(); Wire1 = TwoWire(); g_pinModes.clear(); g_pinLevels.clear();
    gVbusLost = false; gVbusOffPending = false;
    gVbusRetry = VbusRetry::None; gVbusRetryMs = gVbusOffReadMs = 0;
    gPmuIrqRetry = false; gPmuIrqReadMs = 0; gBatteryThresholds = -1;
    g_httpCode = 200; g_httpRequests = 0; g_httpLastBody.clear(); g_httpHook = nullptr;
    g_httpBodies.clear();
    gChargeState = pmu_read::ChargeState::Unconfirmed; gChargeChecked = gChargePauseRequested = false; gChargeFailures = 0; gChargeCheckT0 = 0;
    gNetBusy = false; gNetHold = false; gNetAbort = false;
    gNetLastAttemptMs = 0; gNetRetryMs = 0; gNetRetryNow = false; WiFi = WiFiStub();
    g_failMutexCreate = false; g_taskCreates = 0; g_restarts = 0;
    gIdleSince = g_millis; g_lockDepth = 0;
    g_delayHook = nullptr; g_taskDelayHook = nullptr;
}

// Run loop() every 100 ms for `ms`, stopping when simulated PMU power-off
// ends the boot. A deliberately failed shutdown returns for recovery tests.
static void loopFor(uint32_t ms) {
    uint32_t end = g_millis + ms;
    try {
        while ((int32_t)(g_millis - end) < 0) { g_millis += 100; loop(); }
    } catch (const PoweredOff&) {}
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

// One synthetic fix per second at `mps`, heading north. In this FSM-only
// harness gClockSet models whether the RMC supplied a valid epoch; the real
// parser's independence from the system clock is checked by test_gps.
static void drive(float mps, int seconds) {
    for (int i = 0; i < seconds; ++i) {
        g_millis += 1000; g_epochMs += 1000;
        gLat += mps / 111320.0;
        Fix fx{};
        fx.lat = gLat; fx.lon = gLon; fx.speedMps = mps; fx.hdop = 0.9f;
        fx.monoMs = g_millis; fx.hasUtc = gClockSet; fx.utcMs = fx.hasUtc ? g_epochMs : 0;
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
static std::vector<std::string> times(const std::string& d) {
    std::vector<std::string> v;
    for (size_t p = d.find("<time>"); p != std::string::npos; p = d.find("<time>", p + 1))
        v.push_back(d.substr(p + 6, d.find("</time>", p) - p - 6));
    return v;
}
static std::string iso(uint64_t ms) { char b[25]; isoFromEpochMs(ms, b, sizeof b); return b; }
static bool logHas(const char* frag) {
    for (auto& l : Serial.log) if (l.find(frag) != std::string::npos) return true; return false;
}
static bool wellFormedShape(const std::string& d) {
    // prologue at start, epilogue at end, every trkpt line complete
    if (d.rfind("<?xml", 0) != 0) return false;
    if (d.size() < 22 || d.compare(d.size() - 22, 22, "</trkseg></trk></gpx>\n") != 0) return false;
    return count(d, "<trkpt ") == count(d, "</trkpt>");
}

// XML whitespace grows a real SD trip near the boundary without simulating
// twenty hours of driving. Keep the byte counter in sync with the file.
static void padTripTo(size_t bytes) {
    auto& data = g_sdDisk.files.at(gTripPath);
    data.append(bytes - data.size(), ' ');
    gTripBytes = data.size();
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
            FileWriter w(f); fx.newSegment = seg; appendTrkpt(w, fx);
            CHECK(w.commit(), "commit ok");
            f.close();
            CHECK(g_disk.files["/t.gpx"] == (seg ? "</trkseg><trkseg>\n" + pt : pt),
                  seg ? "exact line (with segment break)" : "exact line");
        }
        // A typical point, against the F1 budget.
        fx.newSegment = false;
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
        CHECK(!isComplete(LittleFS, v[0].c_str()), "incomplete file is not offered for upload");
        CHECK(logHas("left for boot-time repair"), "logged loudly");
        drive(15, 20);
        CHECK(tripFiles().size() == 1 && gTripPath[0] == 0, "no new trip while full, nothing left behind");
        g_disk.capacity = 1 << 22;                // uploads freed space
        fsckAllTrips(LittleFS);
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
        CHECK(!repairIfNeeded(LittleFS, "/trips/t000009.gpx"), "reports failure");
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
        CHECK(v.size() == 1 && isComplete(LittleFS, v[0].c_str()), "offered for upload");
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
        fsckAllTrips(LittleFS);
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
        CHECK(!repairIfNeeded(LittleFS, path.c_str()), "rename failure is reported");
        CHECK(g_disk.files.count(path) && g_disk.files[path] == partial,
              "original survives failed replacement byte for byte");
        CHECK(g_disk.removeCount == 0, "no delete-before-rename power-loss window");
        CHECK(g_disk.files.count(path + ".fix"), "complete repair copy also retained");
        CHECK(!isComplete(LittleFS, path.c_str()), "partial original cannot upload");
        fsckAllTrips(LittleFS);
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
        CHECK(g_disk.formatAttempts == 0, "non-erased partition is never formatted");
        CHECK(g_disk.files["/trips/t000023.gpx"] == "irreplaceable trip", "queued bytes preserved");
        g_disk.failMount = false;
        CHECK(mountTripStorage(), "a later successful mount recovers normally");
        CHECK(g_disk.files["/trips/t000023.gpx"] == "irreplaceable trip", "successful mount also preserves data");
    }

    printf("T23a only a fully erased partition can be provisioned (#76)\n");
    {
        resetWorld();
        g_disk.files["/trips/queued.gpx"] = "queued trip";
        CHECK(mountTripStorage(), "existing filesystem mounts normally");
        CHECK(g_partition.finds == 0 && g_disk.formatAttempts == 0,
              "successful mount skips the raw scan and format");

        resetWorld();
        g_disk.failMount = true;
        g_partition.partition.size = 0x260000;
        g_partition.bytes.assign(g_partition.partition.size, 0xFF);
        setup();
        CHECK(logHas("Setup complete") && g_taskCreates == 1,
              "first flash on fully erased storage completes setup and starts worker");
        CHECK(g_disk.formatAttempts == 1 && g_disk.mountAttempts == 2 && !g_disk.formatRequested,
              "provisions once then remounts with format-on-failure disabled");
        CHECK(g_partition.readBytes == g_partition.partition.size && !g_partition.wrongLookup &&
              g_disk.partitionLabel == "spiffs", "reads every byte of the same labelled data partition");
        g_disk.files["/trips/queued.gpx"] = "queued trip";
        CHECK(mountTripStorage() && g_disk.formatAttempts == 1 &&
              g_disk.files["/trips/queued.gpx"] == "queued trip",
              "next mount preserves trips without provisioning again");

        // Exercise the first byte, both sides of a read boundary, and the
        // last byte of a short final chunk. Sampling must never suffice.
        for (size_t dirtyAt : {0U, 1023U, 1024U, 2500U}) {
            resetWorld();
            g_disk.failMount = true;
            g_partition.partition.size = 2501;
            g_partition.bytes.assign(2501, 0xFF);
            g_partition.bytes[dirtyAt] = 0xFE;
            const auto before = g_partition.bytes;
            CHECK(!mountTripStorage() && g_disk.formatAttempts == 0 &&
                  g_partition.bytes == before && logHas("contains non-erased data"),
                  "even one programmed bit prevents formatting and explains the halt");
        }

        resetWorld();
        g_disk.failMount = true;
        g_partition.partition.size = 2501;
        g_partition.bytes.assign(2501, 0xFF);
        CHECK(mountTripStorage() && g_partition.readBytes == 2501 &&
              g_partition.readOffsets == std::vector<size_t>({0, 1024, 2048}),
              "erased short final chunk is read in full and provisions safely");

        for (size_t failAt : {0U, 1024U, 2048U}) {
            resetWorld();
            g_disk.failMount = true;
            g_partition.bytes.assign(8192, 0xFF);
            g_partition.failReadAt = failAt;
            CHECK(!mountTripStorage() && g_disk.formatAttempts == 0 &&
                  logHas(("read failed at offset " + std::to_string(failAt)).c_str()),
                  "a raw read error at any point prevents formatting and reports its offset");
        }
        for (bool missing : {false, true}) {
            resetWorld();
            g_disk.failMount = true;
            g_partition.missing = missing;
            g_partition.partition.size = 0;
            CHECK(!mountTripStorage() && g_disk.formatAttempts == 0 &&
                  g_partition.readOffsets.empty() && logHas("check the flashed partitions.csv"),
                  "missing or zero-sized partition cannot authorize formatting and explains the halt");
        }
        for (bool failFormat : {false, true}) {
            resetWorld();
            g_disk.failMount = true;
            g_partition.bytes.assign(8192, 0xFF);
            g_disk.failFormat = failFormat;
            g_disk.failRemount = !failFormat;
            CHECK(!mountTripStorage() && g_disk.formatAttempts == 1 &&
                  !g_disk.formatRequested && logHas("provisioning failed"),
                  "format or remount failure halts without an unconditional format retry");
        }
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
        fsckAllTrips(LittleFS);
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
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files[path] == complete, "complete original kept byte for byte");
            CHECK(!g_disk.files.count(path + ".fix"), "redundant repair bytes reclaimed");
            int removes = g_disk.removeCount;
            fsckAllTrips(LittleFS);
            CHECK(g_disk.removeCount == removes, "second boot makes no further changes");
        }
        resetWorld();
        g_disk.files[path] = points + "\n<trkpt lat=";
        g_disk.files[path + ".fix"] = complete;
        g_disk.capacity = g_disk.used();
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
              "verified finished repair publishes without extra free space");
        resetWorld();
        g_disk.files[path] = points + "\n<trkpt lat=";
        g_disk.files[path + ".fix"] = points + "\n</trkseg>";
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
              "interrupted repair epilogue is safely regenerated");
        resetWorld();
        const std::string partial = points + "\n<trkpt lat=";
        g_disk.files[path] = partial;
        g_disk.files[path + ".fix"] = complete;
        g_disk.failRenames = 1;
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == complete,
              "failed publish of existing repair leaves both copies intact");
        fsckAllTrips(LittleFS);
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
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files[path] == complete && !g_disk.files.count(path + ".fix"),
                  "complete points from orphan become a queued GPX");
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files[path] == complete, "recovered trip survives repeated boot");
        }
        for (const std::string& unknown : {std::string("unrecognized evidence"), std::string(GPX_PROLOGUE)}) {
            resetWorld();
            g_disk.files[path + ".fix"] = unknown;
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files[path + ".fix"] == unknown && !g_disk.files.count(path),
                  "unrecoverable sole copy retained without publishing");
        }
        resetWorld();
        g_disk.files[path + ".fix"] = complete;
        g_disk.failRenames = 1;
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path + ".fix"] == complete && !g_disk.files.count(path),
              "failed orphan rename preserves sole copy");
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == complete, "orphan rename retried next boot");

        for (bool full : {false, true}) {
            resetWorld();
            const std::string partial = points + "\n<trkpt lat=";
            g_disk.files[path + ".fix"] = partial;
            if (full) g_disk.capacity = g_disk.used();
            else g_disk.failFlushes = 2;
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files[path] == partial, "failed orphan repair preserves renamed original");
            CHECK(!isComplete(LittleFS, path.c_str()), "failed repair is not uploaded");
            g_disk.capacity = 1 << 22;
            fsckAllTrips(LittleFS);
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
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == other,
              "unequal original and repair copy both retained");
        CHECK(g_disk.flushCount == 0 && g_disk.removeCount == 0, "ambiguous copy is never truncated");
        resetWorld();
        g_disk.files[path + ".bad"] = "quarantined evidence";
        g_disk.files[path + ".fix"] = other;
        fsckAllTrips(LittleFS);
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
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == stale,
                  "open/read/cleanup failure leaves original and temporary unchanged");
            g_disk.failOpenPath.clear(); g_disk.failReadPath.clear();
            fsckAllTrips(LittleFS);
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
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == partial && g_disk.files[path + ".fix"] == shorter,
              "read failure after first point cannot publish shorter repair");
        resetWorld();
        g_disk.files[path + ".fix"] = partial;
        g_disk.failReadPath = path + ".fix";
        g_disk.failReadAfter = 512;
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path + ".fix"] == partial && !g_disk.files.count(path),
              "partially unreadable orphan stays intact and unpublished");
        resetWorld();
        g_disk.files[path] = partial;
        g_disk.failReadPath = path;
        fsckAllTrips(LittleFS);
        CHECK(g_disk.files[path] == partial && g_disk.removeCount == 0,
              "unreadable original is not classified as garbage");
    }

    // Issue #3. gNetBusy = true stands in for core 0 mid-upload; these
    // check the handshake, not real multicore timing.
    printf("T30 idle power-off lets a running upload finish (#3, #106)\n");
    {
        resetWorld();
        drive(15, 40); drive(0, 5);
        vbus(false); drive(0, 11);                   // car off: trip finalized
        CHECK(gVbusLost && gState == IDLE && hasPending(), "trip queued, on battery");
        gIdleSince = g_millis;                       // as loop() last saw the trip
        gNetBusy = true;                             // uploading it
        loopFor(IDLE_BATT_SLEEP_MS + 1000);
        CHECK(PMU.shutdownCalls == 0 && gNetHold, "10 min: no power-off yet, network task told to stop");
        CHECK(logHas("letting the upload finish"), "logged");
        loopFor(60000);
        CHECK(PMU.shutdownCalls == 0, "keeps waiting while the upload runs");
        gNetBusy = false;                            // its file is done
        loopFor(1000);
        CHECK(PMU.shutdownCalls == 1 && !gNetAbort, "powers off once it's idle, nothing cut off");
        CHECK(!g_disk.mounted && g_lockDepth == 1, "storage unmounted, lock kept through power-off");

        resetWorld();
        PMU.vbusIn = false;
        gVbusLost = true; gNetBusy = true;
        loopFor(IDLE_BATT_SLEEP_MS + 1000);
        CHECK(PMU.shutdownCalls == 0 && gNetHold, "waiting on the upload");
        vbus(true); loopFor(1000);
        CHECK(!gVbusLost && !gNetHold && PMU.shutdownCalls == 0, "car back on: wait cancelled, uploads resume");
    }

    printf("T31 an upload that won't finish is cut off, and storage stays locked (#3)\n");
    {
        resetWorld();
        PMU.vbusIn = false;
        gVbusLost = true; gNetBusy = true;           // and it never finishes
        loopFor(IDLE_BATT_SLEEP_MS + NET_DRAIN_MS - 1000);
        CHECK(PMU.shutdownCalls == 0 && !gNetAbort, "waits out the drain limit");
        loopFor(2000 + NET_ABORT_MS);
        CHECK(PMU.shutdownCalls == 1 && gNetAbort, "then cuts the upload off and powers off");
        CHECK(logHas("powering off with storage locked") && g_lockDepth == 1, "a stuck task still can't reach storage");

        resetWorld();                                // critical battery / power key
        PMU.vbusIn = false;
        drive(15, 40); drive(0, 5);
        gNetBusy = true;
        uint32_t t0 = g_millis;
        try { powerOffForVbus(); } catch (const PoweredOff&) {} // as pollPmu() calls it
        CHECK(gNetAbort && g_millis - t0 <= NET_ABORT_MS + 100, "upload cut off without waiting for it");
        auto v = tripFiles();
        CHECK(v.size() == 1 && wellFormedShape(g_disk.files[v[0]]), "trip finalized first");
        CHECK(PMU.shutdownCalls == 1 && g_lockDepth == 1, "powered off with storage locked");
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
        g_delayHook = [](uint32_t ms) { g_millis += ms; };
        setup();
        CHECK(gDegraded.mode() == startup::Mode::NoStorage, "storage failure enters degraded loop");
        loopFor(60000); g_chargeWhileHot = PMU.chargeEnabled;
        PMU.dieC = 35; loopFor(60000); g_chargeWhenCool = PMU.chargeEnabled;
        PMU.dieC = 60; loopFor(60000);
        g_delayHook = nullptr;
        CHECK(!g_chargeAtMount, "charging off before storage is touched");
        CHECK(!g_chargeWhileHot, "hot: charging stays off in degraded service");
        CHECK(g_chargeWhenCool, "cooled: the degraded guard turns charging back on");
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

    printf("T34 a trip closed before UTC is trusted gets no made-up times (#57)\n");
    {
        const uint64_t clock0 = g_epochBaseMs;
        // A fresh clock reads 1970. One carried over deep sleep reads a
        // plausible date, drifted. Neither was synchronized this boot.
        for (uint64_t base : {uint64_t(0), g_epochMs - g_millis - 90000}) {
            resetWorld();
            gClockSet = false;
            g_epochBaseMs = base;
            drive(15, 40);
            endTrip(false);                          // car off, still no UTC
            auto v = tripFiles();
            std::string d = v.size() == 1 ? g_disk.files[v[0]] : "";
            CHECK(v.size() == 1 && isComplete(LittleFS, v[0].c_str()), "trip kept, complete");
            CHECK(count(d, "<trkpt ") == 40, "every point kept");
            CHECK(times(d).empty(), base ? "carried-over date not written as UTC"
                                         : "1970 not written as UTC");
            dump("T34");
        }

        // Valid RMC epochs mid-trip cannot back-date earlier untimed points.
        resetWorld();
        gClockSet = false;
        g_epochBaseMs = 0;
        uint64_t first = g_epochMs + 1000;           // the trip's first fix
        drive(15, 20);
        g_epochBaseMs = g_epochMs - g_millis;        // GPS sets the clock...
        gClockSet = true;                            // ...and says so
        drive(15, 20);
        endTrip(false);
        auto v = tripFiles();
        auto t = v.size() == 1 ? times(g_disk.files[v[0]]) : std::vector<std::string>();
        bool exact = t.size() == 20;
        for (size_t i = 0; exact && i < t.size(); ++i) exact = t[i] == iso(first + (i + 20) * 1000);
        CHECK(exact, "RMC UTC mid-trip: only fixes carrying their own epoch get times");

        // NTP syncs the clock (core 0), and the trip ends before another fix.
        resetWorld();
        gClockSet = false;
        g_epochBaseMs = 0;
        first = g_epochMs + 1000;
        drive(15, 20);
        g_epochBaseMs = g_epochMs - g_millis;
        gClockSet = true;
        endTrip(false);
        v = tripFiles();
        t = v.size() == 1 ? times(g_disk.files[v[0]]) : std::vector<std::string>();
        CHECK(t.empty(), "NTP before the trip closes cannot invent epochs for buffered points");
        g_epochBaseMs = clock0;
    }

    printf("T35 the parked cooldown doesn't count toward the 30 s minimum (#58)\n");
    {
        resetWorld();
        drive(15, 20); drive(0, 181);                // 19 s moving, ~285 m
        CHECK(gState == IDLE && tripFiles().empty() && logHas("Discarding micro-trip"),
              "20 s drive, then the cooldown: discarded");

        resetWorld();
        drive(15, 31); drive(0, 181);                // 30 s, first to last moving fix
        CHECK(tripFiles().size() == 1, "30 s drive: kept");

        resetWorld();                                // 11 s, a 60 s stop, 12 s more
        drive(15, 12); drive(0, 60); drive(15, 12); drive(0, 181);
        CHECK(tripFiles().size() == 1, "a stop mid-trip still counts toward the 30 s");

        resetWorld();
        drive(15, 20); drive(0, 5);
        endTrip(false);                              // car off: a real short drive
        CHECK(tripFiles().size() == 1, "power-loss finalize keeps a short drive");

        resetWorld();                                // GPS lost right after a short drive
        drive(15, 20);
        loopFor(FIX_LOSS_END_MS + 1000);
        CHECK(logHas("No GPS fix for 5 min") && tripFiles().empty(),
              "nor does the wait before a fix-loss finalize");
    }

    printf("T36 WiFi scan results are freed on every exit (#16)\n");
    {
        resetWorld();
        for (int error : {0, -1, -2}) {
            WiFi.scanError = error;
            CHECK(!homeApInRange(), "empty or failed scan is a miss");
            CHECK(WiFi.scanDeletes == WiFi.scans && !WiFi.scanResultsAvailable,
                  "empty/failed results released");
        }
        WiFi.scanError = 0;
        WiFi.scanSsids = {"other-network"};
        CHECK(!homeApInRange(), "unrelated AP is a miss");
        CHECK(WiFi.scanDeletes == WiFi.scans, "unmatched results released");
        WiFi.scanSsids = {"other-network", WIFI_SSID};
        CHECK(homeApInRange(), "home AP found after an unrelated result");
        CHECK(WiFi.scanDeletes == WiFi.scans, "matched results released");
        WiFi.scanSsids = {WIFI_SSID, "other-network"};
        CHECK(homeApInRange() && !WiFi.scanResultsAvailable, "early match also releases results");
        CHECK(WiFi.readsAfterDelete == 0, "scan data read only before release");
    }

    printf("T37 failed network attempts back off, recover, and survive millis wrap (#16)\n");
    {
        resetWorld();
        drive(15, 40); endTrip(false);
        const std::string complete = g_disk.files.begin()->second;
        WiFi.scanDurationMs = 2000;
        // Debug queue counting yields 1 ms per pending file before the due check.
        for (uint32_t wait : {60000U, 120000U, 240000U, 300000U, 300000U}) {
            int before = WiFi.scans;
            netCycle();
            CHECK(WiFi.scans == before + 1 && gNetRetryMs == wait,
                  "AP miss doubles the wait up to five minutes");
            CHECK(gNetLastAttemptMs == g_millis && !gNetBusy &&
                  WiFi.currentMode == WIFI_OFF && WiFi.scanDeletes == WiFi.scans,
                  "wait begins after the attempt; radio and busy flag cleared");
            g_millis = gNetLastAttemptMs + wait - 1 - (debugMode() ? 1 : 0);
            netCycle();
            CHECK(WiFi.scans == before + 1, "no scan before retry is due");
            ++g_millis;
        }
        CHECK(logHas("Network: home WiFi not found \"" WIFI_SSID "\" -- next try in 60 s.") &&
              logHas("Network: home WiFi not found \"" WIFI_SSID "\" -- next try in 300 s."),
              "each miss logs why and when the next try is");
        int scans = WiFi.scans;
        gNetHold = true; netCycle();
        CHECK(WiFi.scans == scans && !gNetBusy, "sleep hold suppresses even an overdue retry");
        gNetHold = false;
        WiFi.scanSsids = {WIFI_SSID};
        uint32_t joinStart = g_millis;
        netCycle();
        CHECK(WiFi.joins == 1 && g_millis - joinStart == 17000 + (debugMode() ? 1 : 0) && gNetRetryMs == NET_RETRY_MAX_MS,
              "seeing the AP but failing to join keeps the backoff");
        CHECK(gNetLastAttemptMs == g_millis, "join timeout does not consume the next retry wait");
        CHECK(logHas("Network: couldn't join home WiFi \"" WIFI_SSID "\" (status 0) -- next try in 300 s."),
              "join failure logged with SSID and status");

        WiFi.connectionStatus = WL_CONNECTED;
        g_httpCode = 503;
        g_millis = gNetLastAttemptMs + gNetRetryMs;
        netCycle();
        CHECK(g_httpRequests == 1 && gNetRetryMs == NET_RETRY_MAX_MS && hasPending(),
              "server failure stays queued and keeps the backoff");
        CHECK(logHas("Network: no trip uploaded -- next try in 300 s."), "server failure logged");
        g_disk.files["/trips/t999999.gpx"] = complete;
        g_httpHook = [] { g_httpCode = g_httpRequests == 2 ? 200 : 503; };
        g_millis = gNetLastAttemptMs + gNetRetryMs;
        int misses = logCount("Network: ");
        netCycle();
        CHECK(g_httpRequests == 3 && tripFiles().size() == 1 && gNetRetryMs == NET_TICK_MS,
              "one successful upload resets the wait even if a later file fails");
        CHECK(logCount("Network: ") == misses, "a cycle that uploaded anything logs no miss");
        g_httpHook = nullptr;
        scans = WiFi.scans;
        g_millis = gNetLastAttemptMs + NET_TICK_MS - 1 - (debugMode() ? 1 : 0); netCycle();
        CHECK(WiFi.scans == scans, "success still waits the normal 30 s before another attempt");
        ++g_millis; netCycle();
        CHECK(g_httpRequests == 4 && gNetRetryMs == 60000, "a new failure grows from the reset wait");

        g_disk.files.clear();
        scans = WiFi.scans;
        netCycle();
        CHECK(gNetRetryMs == 0 && WiFi.scans == scans, "empty queue clears backoff without a scan");
        g_disk.files["/trips/t999999.gpx"] = complete;
        g_httpCode = 200;
        netCycle();
        CHECK(g_httpRequests == 5 && !hasPending(), "new queued trip can upload immediately");

        resetWorld();
        g_disk.files["/trips/t999999.gpx"] = complete;
        g_millis = UINT32_MAX - 1000;
        netCycle();
        g_millis = gNetLastAttemptMs + gNetRetryMs - 1 - (debugMode() ? 1 : 0); netCycle();
        CHECK(WiFi.scans == 1, "wrapped clock: no early retry");
        ++g_millis; netCycle();
        CHECK(WiFi.scans == 2 && gNetRetryMs == 120000, "wrapped clock: retry when due");
    }

    printf("T38 every trip walk accepts basenames and full paths (#17)\n");
    {
        for (bool fullPaths : {false, true}) {
            resetWorld();
            g_disk.fullPathNames = fullPaths;
            drive(15, 40); endTrip(false);
            const std::string complete = g_disk.files.begin()->second;
            g_disk.files.clear();
            g_disk.files["/trips/t000038.gpx.fix"] = complete;  // orphan boot repair
            fsckAllTrips(LittleFS);
            CHECK(g_disk.files.count("/trips/t000038.gpx") == 1 &&
                  !g_disk.files.count("/trips/t000038.gpx.fix"), "fsck resolves the actual trip path");
            strcpy(gTripPath, "/trips/t000038.gpx");
            CHECK(!hasPending(), "active trip excluded from pending lookup");
            CHECK(!uploadPending() && g_httpRequests == 0, "active trip excluded from uploads");
            gTripPath[0] = 0;
            CHECK(hasPending(), "finished trip found by pending lookup");
            CHECK(uploadPending() && g_httpRequests == 1 && g_httpLastBody == complete,
                  "upload reads the actual path and exact trip bytes");
            CHECK(tripFiles().empty() && !hasPending(), "accepted trip deleted from the actual path");
        }
    }

    printf("T39 failed mutex allocation restarts before storage or task startup (#18)\n");
    {
        resetWorld();
        g_disk.files["/trips/t000039.gpx"] = "irreplaceable trip";
        g_failMutexCreate = true;
        try { setup(); } catch (const Restarted&) {}
        CHECK(g_restarts == 1 && !gFsMutex && logHas("LittleFS mutex allocation failed"),
              "specific boot error and restart on allocation failure");
        CHECK(g_disk.mountAttempts == 0 && g_lockDepth == 0 && g_taskCreates == 0 && prefs.kv.empty(),
              "no storage access, lock acquisition, or worker startup");
        CHECK(g_disk.files["/trips/t000039.gpx"] == "irreplaceable trip" && !logHas("Setup complete"),
              "queued trip preserved and setup does not continue");
        resetWorld();
        setup();
        CHECK(gFsMutex && g_restarts == 0 && g_disk.mountAttempts == 1 && g_taskCreates == 1,
              "successful allocation still mounts storage and starts the network task");
    }

    printf("T40 a trip ending clears the backoff, so arriving home retries at once (#16)\n");
    {
        for (bool atHome : {true, false}) {
            // The drive to work stays queued all day, and by the drive home
            // every miss has pushed the wait to the cap.
            resetWorld();
            drive(15, 40); endTrip(false);
            WiFi.scanDurationMs = 2000;
            netCycle();
            for (int i = 0; i < 4; ++i) { g_millis = gNetLastAttemptMs + gNetRetryMs; netCycle(); }
            CHECK(WiFi.scans == 5 && gNetRetryMs == NET_RETRY_MAX_MS, "away from home the wait reaches the cap");
            drive(15, 40);
            netCycle();
            CHECK(gTripPath[0] && WiFi.scans == 5, "starting the next trip doesn't cut the wait short");

            if (atHome) { WiFi.scanSsids = {WIFI_SSID}; WiFi.connectionStatus = WL_CONNECTED; }
            vbus(false);
            drive(0, 12);   // parked; USB off confirmed after 10 s
            CHECK(!gTripPath[0] && tripFiles().size() == 2, "car off finalizes the trip; both trips queued");

            if (atHome) {
                netCycle();
                CHECK(WiFi.scans == 6 && g_httpRequests == 2 && tripFiles().empty(),
                      "first tick after parking uploads both trips despite the 5 min wait");
            } else {
                // Tick as the network task does until the idle sleep is due.
                uint32_t parked = g_millis;
                while (g_millis - parked < IDLE_BATT_SLEEP_MS) { netCycle(); g_millis += NET_TICK_MS; }
                CHECK(WiFi.scans == 5 + 4, "four tries (~0, 1, 3, 7 min) before the 10 min idle sleep");
                CHECK(gNetRetryMs == NET_RETRY_MAX_MS, "misses after parking back off again");
            }
        }
    }

    printf("T41 incomplete files cannot fill the upload batch (#56)\n");
    {
        for (bool fullPaths : {false, true}) {
            resetWorld();
            g_disk.fullPathNames = fullPaths;
            drive(15, 40); endTrip(false);
            const std::string complete = g_disk.files.begin()->second;
            const std::string incomplete = complete.substr(0, complete.find(GPX_EPILOGUE));
            g_disk.files.clear();
            for (int i = 0; i < 36; ++i) {
                char path[40]; snprintf(path, sizeof(path), "/trips/t%06d.gpx", i);
                g_disk.files[path] = i < 16 ? incomplete : complete;
            }
            g_disk.capacity = g_disk.used();
            fsckAllTrips(LittleFS);
            CHECK(tripFiles().size() == 36 && g_disk.used() == g_disk.capacity,
                  "full flash cannot repair the 16 incomplete originals");
            CHECK(uploadPending() && g_httpRequests == 16,
                  "first batch reaches and uploads 16 complete files after the incomplete prefix");
            CHECK(g_disk.used() < g_disk.capacity && tripFiles().size() == 20,
                  "successful uploads free flash for later recovery");
            CHECK(uploadPending() && g_httpRequests == 20 && tripFiles().size() == 16,
                  "second batch uploads the remaining complete trips");
            CHECK(!uploadPending() && g_httpRequests == 20, "incomplete files are never sent");
            bool preserved = tripFiles().size() == 16;
            for (auto& kv : g_disk.files) preserved &= kv.second == incomplete;
            CHECK(preserved, "all incomplete originals kept byte for byte across repeated cycles");
            CHECK(g_lockDepth == 0, "filesystem lock released after queue walks and uploads");
            CHECK(g_disk.mutationsWithDirectoryOpen == 0, "uploads mutate only after directory handles close");
        }
    }

    printf("T42 long trips keep every fix without trusted UTC (#60)\n");
    {
        for (int seconds : {90, 91, 300, 1200}) {
            resetWorld();
            gClockSet = false;
            const double startLat = gLat;
            drive(15, seconds);
            CHECK(gPendCount == 90 && count(g_disk.files[gTripPath], "<trkpt ") == seconds - 90,
                  "only the latest 90 fixes stay buffered; older fixes reach flash");
            endTrip(false);
            const std::string& d = g_disk.files.begin()->second;
            CHECK(wellFormedShape(d) && count(d, "<trkpt ") == seconds && times(d).empty(),
                  "every point retained with no invented timestamps");
            double lat = startLat;
            size_t pos = 0;
            bool ordered = true;
            for (int i = 0; i < seconds; ++i) {
                lat += 15.0 / 111320.0;
                char point[80]; snprintf(point, sizeof(point), "<trkpt lat=\"%.6f\"", lat);
                size_t next = d.find(point, pos);
                if (next == std::string::npos) { ordered = false; break; }
                pos = next + strlen(point);
            }
            CHECK(ordered, "fixes remain in order across repeated ring wrap");
            dump(("T42_" + std::to_string(seconds)).c_str());
        }

        resetWorld();
        gClockSet = false;
        drive(15, 300);
        const uint64_t firstTimed = g_epochMs + 1000;
        g_epochBaseMs = g_epochMs - g_millis;
        gClockSet = true;
        drive(15, 30); endTrip(false);
        const std::string& d = g_disk.files.begin()->second;
        const auto ts = times(d);
        bool exact = ts.size() == 30;
        for (size_t i = 0; exact && i < ts.size(); ++i) exact = ts[i] == iso(firstTimed + i * 1000);
        CHECK(wellFormedShape(d) && count(d, "<trkpt ") == 330 && exact,
              "late RMC UTC times only new fixes, preserving every earlier untimed point");
        CHECK(count(d.substr(0, d.find("<time>")), "<trkpt ") == 301,
              "the first 300 points stay untimed and precede the timed tail");
        dump("T42_late_utc");

        resetWorld();
        gClockSet = false;
        drive(15, 100); drive(0, 200);
        CHECK(gState == IDLE && tripFiles().size() == 1 &&
              count(g_disk.files.begin()->second, "<trkpt ") == 281,
              "untimed spills also preserve cooldown fixes until normal finalize");

        for (bool failOpen : {false, true}) {
            resetWorld();
            gClockSet = false;
            drive(15, 100);
            if (failOpen) g_disk.failNextOpens = 1;
            else g_disk.failFlushes = 2;  // both commit() and close() fail
            drive(15, 1);
            CHECK(gState == IDLE && !gTripPath[0] && logHas("write to /trips/t000001.gpx failed"),
                  "an untimed spill reports storage failure and ends the trip");
            CHECK(tripFiles().size() == 1 && wellFormedShape(g_disk.files.begin()->second) &&
                  count(g_disk.files.begin()->second, "<trkpt ") == 10,
                  "repair preserves the previously committed untimed prefix");
        }
    }

    printf("T43 WiFi failures identify the SSID and pre-disconnect status (#77)\n");
    {
        resetWorld();
        netCycle();
        CHECK(logCount("Network: ") == 0 && WiFi.scans == 0, "empty queue is quiet");
        drive(15, 40); endTrip(false);
        netCycle();
        CHECK(logCount("Network: ") == 1 &&
              logHas("home WiFi not found \"" WIFI_SSID "\" -- next try in 60 s."),
              "AP miss identifies the configured SSID and next retry in one line");
        netCycle();
        CHECK(logCount("Network: ") == 1, "backoff does not repeat the diagnostic");
        WiFi.scanSsids = {WIFI_SSID};
        for (int status : {1, 4, 6}) {  // NO_SSID_AVAIL, CONNECT_FAILED, DISCONNECTED
            WiFi.disconnectStatus = 6;
            WiFi.connectionStatus = status;
            g_millis = gNetLastAttemptMs + gNetRetryMs;
            int before = logCount("Network: ");
            netCycle();
            const std::string expected = "couldn't join home WiFi \"" WIFI_SSID "\" (status " +
                                         std::to_string(status) + ")";
            CHECK(logCount("Network: ") == before + 1 && logHas(expected.c_str()),
                  "one join failure line includes the status captured before disconnect");
        }
        CHECK(!logHas(WIFI_PASSWORD) && !logHas(AUTH_TOKEN), "diagnostics never print credentials");
    }

    printf("T44 segment boundaries stay with buffered fixes (#99)\n");
    {
        for (int prefix : {20, 100}) for (bool lateUtc : {false, true}) {
            resetWorld();
            gClockSet = false;
            drive(15, prefix);
            auto gapFix = [] {
                g_millis += SEG_GAP_MS + 1000;
                g_epochMs += SEG_GAP_MS + 1000;
                gLat += 15.0 / 111320.0;
                Fix fx{};
                fx.lat = gLat; fx.lon = gLon; fx.speedMps = 15; fx.hdop = .9f;
                fx.monoMs = g_millis; fx.newSegment = true;
                tripFsmOnFix(fx);
            };
            gapFix(); drive(15, 30); gapFix(); drive(15, 120);
            if (lateUtc) {
                g_epochBaseMs = g_epochMs - g_millis;
                gClockSet = true;
                drive(15, 10);
            }
            endTrip(false);
            const std::string& d = g_disk.files.begin()->second;
            size_t first = d.find("</trkseg><trkseg>");
            size_t second = first == std::string::npos ? first : d.find("</trkseg><trkseg>", first + 1);
            CHECK(wellFormedShape(d) && count(d, "<trkpt ") == prefix + 152 + (lateUtc ? 10 : 0),
                  "multiple gaps preserve every pre-UTC and later fix");
            CHECK(count(d, "</trkseg><trkseg>") == 2 &&
                  count(d.substr(0, first), "<trkpt ") == prefix &&
                  count(d.substr(0, second), "<trkpt ") == prefix + 31,
                  "segment starts appear at the first post-gap fixes after ring wrap");
            CHECK(lateUtc ? times(d).size() == 10 : times(d).empty(),
                  "late RMC UTC times only new fixes without moving segment boundaries");
        }
        // UTC arriving while the marked fix is still buffered uses flushPending,
        // rather than the spill path exercised above.
        resetWorld(); gClockSet = false;
        drive(15, 20);
        Fix fx = gLastFix; fx.monoMs = ++g_millis; fx.newSegment = true;
        tripFsmOnFix(fx);
        g_epochBaseMs = g_epochMs - g_millis; gClockSet = true;
        drive(15, 10); endTrip(false);
        const std::string& d = g_disk.files.begin()->second;
        CHECK(count(d, "<trkpt ") == 31 && times(d).size() == 10 &&
              count(d.substr(0, d.find("</trkseg><trkseg>")), "<trkpt ") == 20,
              "late-UTC flush places a still-buffered segment marker on its own fix");
    }

    printf("T45 incomplete-only queues leave the radio off (#101)\n");
    {
        for (bool fullPaths : {false, true}) {
            resetWorld(); g_disk.fullPathNames = fullPaths;
            drive(15, 40); endTrip(false);
            const std::string complete = g_disk.files.begin()->second;
            const std::string incomplete = complete.substr(0, complete.find(GPX_EPILOGUE));
            g_disk.files.clear();
            for (int i = 0; i < 16; ++i) {
                char path[40]; snprintf(path, sizeof(path), "/trips/t%06d.gpx", i);
                g_disk.files[path] = incomplete;
            }
            g_disk.capacity = g_disk.used(); fsckAllTrips(LittleFS);
            auto originals = g_disk.files;
            WiFi.scanSsids = {WIFI_SSID}; WiFi.connectionStatus = WL_CONNECTED;
            for (int i = 0; i < 3; ++i) { netCycle(); g_millis += NET_RETRY_MAX_MS; }
            CHECK(!hasPending() && WiFi.scans == 0 && WiFi.joins == 0 && g_httpRequests == 0 &&
                  logCount("Network: ") == 0 && gNetRetryMs == 0,
                  "incomplete-only queue causes no radio work or repeated failure logs");
            CHECK(g_disk.files == originals && g_lockDepth == 0 && g_disk.openDirectories == 0,
                  "preserved originals and lookup handles remain intact");
            g_disk.files["/trips/t999999.gpx"] = complete;
            g_disk.capacity += complete.size();
            gNetHold = true; netCycle();
            CHECK(WiFi.scans == 0 && g_httpRequests == 0, "sleep hold suppresses uploadable work");
            gNetHold = false; gNetAbort = true; netCycle();
            CHECK(WiFi.scans == 0 && g_httpRequests == 0, "abort suppresses uploadable work");
            gNetAbort = false;
            netCycle();
            CHECK(WiFi.scans == 1 && WiFi.joins == 1 && g_httpRequests == 1 &&
                  g_httpLastBody == complete && g_disk.files == originals,
                  "a complete trip beyond 16 incomplete files still uploads immediately");
            CHECK(g_disk.mutationsWithDirectoryOpen == 0, "all lookup and upload directory handles close before mutation");
        }
    }

    printf("T46 interruption during queue lookup preserves retry backoff\n");
    {
        for (bool abort : {false, true}) {
            resetWorld(); drive(15, 40); endTrip(false);
            const std::string complete = g_disk.files.begin()->second;
            g_disk.files.clear();
            g_disk.files["/trips/t000000.gpx"] = complete.substr(0, complete.find(GPX_EPILOGUE));
            g_disk.files["/trips/t999999.gpx"] = complete;
            const auto originals = g_disk.files;
            gNetRetryNow = false;
            gNetRetryMs = NET_RETRY_MAX_MS; gNetLastAttemptMs = g_millis;
            if (abort) g_disk.onRead = [] { g_disk.onRead = nullptr; gNetAbort = true; };
            else g_disk.onRead = [] { g_disk.onRead = nullptr; gNetHold = true; };
            netCycle();
            CHECK(gNetRetryMs == NET_RETRY_MAX_MS && !gNetBusy &&
                  WiFi.scans == 0 && g_httpRequests == 0,
                  "hold/abort arriving inside the filesystem read leaves the retry delay intact");
            CHECK(g_disk.files == originals && g_lockDepth == 0 && g_disk.openDirectories == 0,
                  "interrupted lookup preserves all files and releases every handle and lock");
            gNetHold = false; gNetAbort = false;
            netCycle();
            CHECK(WiFi.scans == 0, "releasing the interruption does not start an early retry");
            WiFi.scanSsids = {WIFI_SSID}; WiFi.connectionStatus = WL_CONNECTED;
            g_millis = gNetLastAttemptMs + gNetRetryMs;
            netCycle();
            CHECK(g_httpRequests == 1 && !g_disk.files.count("/trips/t999999.gpx") &&
                  g_disk.files.count("/trips/t000000.gpx"),
                  "the due retry still uploads complete work and preserves the incomplete original");
        }
    }

    printf("T47 Supreme boot disables LoRa after bounded PMU startup, before other setup (#107)\n");
    {
        for (bool failMutex : {false, true}) {
            resetWorld();
            g_failMutexCreate = failMutex;
            g_delayHook = [](uint32_t) {
                CHECK(!PMU.aldo3 && !PMU.bldo1 && !PMU.vbackup,
                      "radio, SD and VBACKUP are off before a startup delay");
                g_delayHook = nullptr;
            };
            try { setup(); } catch (const Restarted&) {}
            CHECK(PMU.bus == &Wire1 && Wire1.sda == 42 && Wire1.scl == 41 &&
                  Wire.begins == 1,
                  "PMU uses only Wire1 on SDA 42 / SCL 41");
            CHECK(std::vector<std::string>(PMU.operations.begin(), PMU.operations.begin() + 3) ==
                  std::vector<std::string>({"begin", "disableALDO3", "readRegister"}),
                  "radio disable and readback are the first operations after PMU.begin");
            CHECK(logHas("power-on source 0x04, power-off source 0x02"), "boot logs PMU power sources");
            if (!failMutex) {
                CHECK(PMU.aldo4 && PMU.aldo4Mv == 3300 &&
                      g_pinModes[7] == OUTPUT && g_pinLevels[7] == HIGH,
                      "GPS is on ALDO4 at 3.3 V with GPS_EN HIGH");
                CHECK(GPSSerial.baud == 9600 && GPSSerial.rxPin == 9 && GPSSerial.txPin == 8,
                      "MAX-M10S UART uses RX 9 / TX 8 at 9600 baud");
            }
        }
        for (bool writeFailed : {false, true}) {
            resetWorld();
            PMU.failRadioDisable = writeFailed;
            PMU.radioStuckOn = !writeFailed;
            setup();
            CHECK(logHas("ALDO3) could not be verified off") &&
                  gDegraded.mode() == startup::Mode::PmuUntrusted &&
                  g_disk.mountAttempts == 1 && g_taskCreates == 0 && !PMU.aldo4 && !PMU.chargeEnabled,
                  "failed radio write or readback enters journal-only degraded mode with charging disabled");
        }
        resetWorld();
        PMU.failBegins = 2;
        g_delayHook = [](uint32_t ms) {
            if (ms == 10) {
                CHECK(Serial.log.empty() && !PMU.aldo4 && g_disk.mountAttempts == 0 && g_taskCreates == 0,
                      "PMU retry waits precede Serial, GPS, storage and worker startup");
            } else {
                CHECK(!PMU.aldo3, "successful PMU retry disables LoRa before later setup delays");
            }
        };
        setup(); g_delayHook = nullptr;
        CHECK(PMU.beginCalls == 3 && Wire1.ends == 2 && !PMU.aldo3 && logHas("retry 3/3"),
              "Stage 4 uses bounded retries and prints the saved startup report");

        resetWorld();
        PMU.failBegins = 3;
        setup();
        CHECK(PMU.beginCalls == 3 && Wire1.ends == 2 && logHas("attempt 3/3 failed") &&
              logHas("power chip not responding") && !PMU.aldo4 &&
              gDegraded.mode() == startup::Mode::PmuMissing &&
              g_disk.mountAttempts == 1 && g_taskCreates == 0,
              "exhausted PMU retries enter flash-journal-only degraded mode");
    }

    printf("T48 power-off is cancelled on USB, including a late return without an IRQ (#106)\n");
    {
        for (bool critical : {false, true}) {
            resetWorld(); drive(15, 40);
            const std::string path = gTripPath;
            PMU.criticalIrq = critical; PMU.longPressIrq = !critical; gPmuIrqFlag = true;
            pollPmu();
            CHECK(PMU.shutdownCalls == 0 && g_disk.mounted && g_lockDepth == 0 &&
                  gState == ACTIVE && path == gTripPath,
                  "power-key and critical-battery IRQs on USB keep the current trip running");
        }
        resetWorld(); drive(15, 40);
        PMU.vbusIn = false; gVbusLost = true; PMU.aldo4 = true;
        g_disk.onUnmount = [] { PMU.vbusIn = true; };  // no INSERT flag delivered
        powerOffForVbus();
        CHECK(PMU.shutdownCalls == 0 && logHas("power-off cancelled"),
              "final level read catches USB returning after storage closes");
        CHECK(!gVbusLost && !gVbusOffPending && !gNetHold && !gNetAbort &&
              g_disk.mounted && g_lockDepth == 0 && PMU.aldo4,
              "late return restores storage and network flags, leaving GPS powered");
        CHECK(tripFiles().size() == 1 && wellFormedShape(g_disk.files.begin()->second),
              "the finalized trip remains queued through cancellation");
        drive(15, 40); endTrip(false);
        CHECK(tripFiles().size() == 2, "logging resumes in a distinct trip file");
        WiFi.scanSsids = {WIFI_SSID}; WiFi.connectionStatus = WL_CONNECTED; netCycle();
        CHECK(g_httpRequests == 2 && tripFiles().empty(), "queued trips can upload after cancellation");

        resetWorld(); PMU.vbusIn = false; gVbusLost = true; gNetBusy = true;
        g_taskDelayHook = [](uint32_t) { PMU.vbusIn = true; gNetBusy = false; };
        powerOffForVbus(); g_taskDelayHook = nullptr;
        CHECK(PMU.shutdownCalls == 0 && !gNetHold && !gNetAbort && !gVbusLost && g_lockDepth == 0,
              "USB returning during upload abort cancels power-off and resumes networking");
    }

    printf("T49 failed PMU power-off restores logging and handles recovery failures (#106)\n");
    {
        resetWorld(); drive(15, 40);
        PMU.vbusIn = false; PMU.failShutdown = true;
        PMU.onShutdown = [] {
            CHECK(!PMU.vbusIn && !g_disk.mounted && g_lockDepth == 1 && gNetHold,
                  "shutdown is requested only on battery with storage closed and locked");
            CHECK(gState == IDLE && wellFormedShape(g_disk.files.begin()->second),
                  "trip is complete before the PMU shutdown request");
        };
        uint32_t t0 = g_millis;
        powerOffForVbus();
        CHECK(PMU.shutdownCalls == 1 && g_millis - t0 >= 1000 && logHas("did not power off"),
              "a returning shutdown gets time for rail collapse, then reports failure");
        CHECK(gVbusLost && g_disk.mounted && !gNetHold && !gNetAbort && g_lockDepth == 0,
              "failed shutdown stays up with storage and logging usable");
        loopFor(1000);
        CHECK(PMU.shutdownCalls == 1, "failed shutdown does not create a tight retry loop");
        drive(15, 40); endTrip(false);
        CHECK(tripFiles().size() == 2, "a new trip can record after failed power-off");

        for (bool failedShutdown : {false, true}) {
            resetWorld(); PMU.vbusIn = false; gNetBusy = true;
            PMU.failShutdown = failedShutdown;
            if (!failedShutdown) g_disk.onUnmount = [] { PMU.vbusIn = true; };
            try { powerOffForVbus(); } catch (const Restarted&) {}
            CHECK(g_restarts == 1 && gNetHold && gNetAbort && logHas("upload task still busy") &&
                  !g_disk.mounted && g_lockDepth == 1 && g_disk.mountAttempts == 0 &&
                  PMU.shutdownCalls == (failedShutdown ? 1 : 0),
                  "a stuck upload restarts with storage locked, without remounting under its old file handle");
        }

        resetWorld(); drive(15, 40); PMU.vbusIn = false;
        g_disk.onUnmount = [] { PMU.vbusIn = true; g_disk.failMount = true; };
        try { powerOffForVbus(); } catch (const Restarted&) {}
        CHECK(g_restarts == 1 && gNetHold && g_lockDepth == 1 &&
              !g_disk.formatRequested && g_disk.formatAttempts == 0 &&
              wellFormedShape(g_disk.files.begin()->second),
              "failed recovery remount preserves the trip and restarts without formatting or releasing storage");
    }

    printf("T50 PMU power-off uses a full boot with filesystem/NVS persistence (#106)\n");
    {
        resetWorld(); drive(15, 40); PMU.vbusIn = false;
        try { powerOffForVbus(); } catch (const PoweredOff&) {}
        CHECK(PMU.shutdownCalls == 1 && !g_disk.mounted && g_lockDepth == 1,
              "successful power-off ends this boot with storage locked");
        const auto savedFiles = g_disk.files;
        const auto savedPrefs = prefs.kv;
        resetWorld();
        g_disk.files = savedFiles; prefs.kv = savedPrefs;
        gClockSet = false; gGpsClockSet = false; g_epochBaseMs = 0;
        setup();
        CHECK(g_disk.files == savedFiles && hasPending() && !gClockSet &&
              gState == IDLE && logHas("Setup complete"),
              "ordinary boot finds the queued trip without any retained RTC clock or trip state");
        char nextPath[48]; makeTripPath(nextPath, sizeof(nextPath));
        CHECK(!g_disk.files.count(nextPath) && prefs.kv["tripno"] == savedPrefs.at("tripno") + 1,
              "Preferences retains the counter and the next trip cannot overwrite the previous one");
    }

    printf("T51 SD boot selects the card, with a separate flash backlog (#109)\n");
    {
        resetWorld(); SD.cardPresent = true;
        g_sdDisk.capacity = 32ULL * 1024 * 1024 * 1024;
        g_sdDisk.onMount = [] {
            CHECK(PMU.bldo1 && PMU.bldo1Mv == 3300 && g_pinLevels[34] == HIGH,
                  "SD mount sees BLDO1 enabled at 3.3 V and the IMU deselected");
        };
        g_delayHook = [](uint32_t ms) {
            if (ms == SD_SETTLE_MS)
                CHECK(PMU.bldo1 && PMU.bldo1Mv == 3300 && SD.begins == 0,
                      "card supply settles before the first mount");
        };
        setup(); g_delayHook = nullptr; g_sdDisk.onMount = nullptr;
        CHECK(gTripFS == &SD && gSdMounted && gFlashMounted && PMU.bldo1 && !PMU.aldo3,
              "SD is selected for recording and flash remains mounted for draining");
        CHECK(SD.spi == &gSdSpi && gSdSpi.host == HSPI && gSdSpi.sck == 36 &&
              gSdSpi.mosi == 35 && gSdSpi.miso == 37 && SD.cs == 47 && SD.hz == 4000000,
              "dedicated HSPI uses LilyGO pins at 4 MHz");
        auto voltage = std::find(g_powerEvents.begin(), g_powerEvents.end(), "BLDO1.voltage");
        auto enabled = std::find(g_powerEvents.begin(), g_powerEvents.end(), "BLDO1.on");
        auto mounted = std::find(g_powerEvents.begin(), g_powerEvents.end(), "SD.begin");
        CHECK(voltage < enabled && enabled < mounted && g_powerEvents.front() == "BLDO1.off",
              "early rail off precedes voltage programming, enabling and mounting");
        CHECK(!g_sdDisk.formatRequested && !g_sdDisk.formatAttempts && logHas("Trip storage: microSD"),
              "boot identifies SD and never asks to format it");
        CHECK(fsFreeBytes() == 32ULL * 1024 * 1024 * 1024,
              "SD free space remains 64 bit above 4 GiB");
        drive(15, 40); endTrip(false);
        CHECK(g_disk.files.empty() && g_sdDisk.files.size() == 1 &&
              wellFormedShape(g_sdDisk.files.begin()->second) && g_sdDisk.openHandles == 0,
              "a complete trip is written only to SD and every batch handle closes");
        g_disk.capacity = 0;
        CHECK(roomForNewTrip(g_millis), "full flash cannot stop recording to an SD card with space");
        g_sdDisk.capacity = g_sdDisk.used() + FS_START_FREE - 1;
        CHECK(!roomForNewTrip(g_millis), "SD free-space guard uses the card, not flash");
    }

    printf("T52 missing, unmountable or unpowered SD falls back without erasing (#109)\n");
    {
        for (int fault = 0; fault < 5; ++fault) {
            resetWorld(); SD.cardPresent = fault != 0;
            g_sdDisk.failMount = fault == 1;
            PMU.failSdVoltage = fault == 2; PMU.failSdEnable = fault == 3;
            PMU.failSdDisable = fault == 4;
            g_sdDisk.files["/keep.txt"] = "card evidence";
            setup();
            CHECK(gTripFS == &LittleFS && !gSdMounted && gFlashMounted &&
                  logHas("microSD unavailable -- using internal flash"),
                  "missing/mount/power failure logs the internal-flash fallback");
            CHECK(!g_sdDisk.mounted && !g_sdDisk.formatRequested && !g_sdDisk.formatAttempts &&
                  g_sdDisk.files.at("/keep.txt") == "card evidence",
                  "fallback preserves the card and never formats it");
            if (fault != 4) CHECK(!PMU.bldo1, "unused card supply is off after fallback");
            if (fault >= 2) CHECK(SD.begins == 0, "power setup failure prevents any mount attempt");
            drive(15, 40); endTrip(false);
            CHECK(g_disk.files.size() == 1 && wellFormedShape(g_disk.files.begin()->second),
                  "fallback still records a complete trip to flash");
        }
        resetWorld(); SD.cardPresent = true; g_disk.failMount = true;
        g_disk.files["/trips/keep.gpx"] = "flash evidence";
        setup();
        CHECK(gTripFS == &SD && gSdMounted && !gFlashMounted && logHas("Setup complete") &&
              logHas("unavailable -- preserved for recovery") && g_disk.formatAttempts == 0,
              "damaged optional flash is preserved while usable SD keeps recording");
    }

    printf("T53 both queues drain with filesystem identity, even for matching names (#109)\n");
    {
        resetWorld(); SD.cardPresent = true; setup();
        drive(15, 40);
        std::string active = gTripPath;
        const std::string flashTrip = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>\n" + GPX_EPILOGUE;
        g_disk.files[active] = flashTrip;
        CHECK(hasPending(), "flash backlog is pending even when its name matches the active SD trip");
        CHECK(uploadPending() && g_httpRequests == 1 && g_disk.files.empty() &&
              g_sdDisk.files.count(active) && gState == ACTIVE,
              "drains matching flash name while preserving the active SD trip");
        endTrip(false);
        g_disk.files[active] = flashTrip;
        const std::string sdTrip = g_sdDisk.files.at(active);
        int before = g_httpRequests;
        CHECK(uploadPending() && g_httpRequests == before + 2 &&
              g_disk.files.empty() && g_sdDisk.files.empty() && !hasPending(),
              "both finalized queues drain and delete from their own medium");
        CHECK(g_httpBodies.size() == 3 && g_httpBodies[1] == sdTrip && g_httpBodies[2] == flashTrip,
              "SD uploads first, flash second, each from the correct medium");
        // A per-store batch prevents a large SD queue starving flash.
        for (int i = 0; i < 20; ++i)
            g_sdDisk.files["/trips/queued" + std::to_string(i) + ".gpx"] = flashTrip;
        g_disk.files["/trips/old.gpx"] = flashTrip;
        before = g_httpRequests;
        CHECK(uploadPending() && g_httpRequests == before + 17 &&
              g_sdDisk.files.size() == 4 && g_disk.files.empty(),
              "one cycle sends 16 SD trips and still drains the old flash backlog");
        CHECK(g_sdDisk.openHandles == 0 && g_disk.openHandles == 0,
              "queue scans and uploads close all file and directory handles");
    }

    printf("T54 SD is closed and unmounted before PMU shutdown (#109)\n");
    {
        resetWorld(); SD.cardPresent = true; setup(); drive(15, 40); PMU.vbusIn = false;
        PMU.onShutdown = [] {
            CHECK(g_sdDisk.openHandles == 0 && g_disk.openHandles == 0 &&
                  !g_sdDisk.mounted && !g_disk.mounted && g_lockDepth == 1 && gNetHold,
                  "shutdown sees no open handles, both stores unmounted and mutex/hold retained");
            CHECK(wellFormedShape(g_sdDisk.files.begin()->second),
                  "SD trip finalized before shutdown cuts power");
            CHECK(PMU.bldo1, "BLDO1 stays enabled until PMU shutdown cuts the rails");
        };
        try { powerOffForVbus(); } catch (const PoweredOff&) {}
        auto ended = std::find(g_powerEvents.begin(), g_powerEvents.end(), "SD.end");
        auto shutdown = std::find(g_powerEvents.begin(), g_powerEvents.end(), "PMU.shutdown");
        CHECK(ended < shutdown && PMU.shutdownCalls == 1, "SD.end precedes PMU.shutdown");
    }

    printf("T55 cancelled/failed SD shutdown remounts the same stores (#109)\n");
    {
        for (bool failedShutdown : {false, true}) {
            resetWorld(); SD.cardPresent = true; setup(); drive(15, 40); PMU.vbusIn = false;
            g_disk.files["/trips/old.gpx"] = "preserved flash backlog";
            PMU.failShutdown = failedShutdown;
            if (!failedShutdown) g_sdDisk.onUnmount = [] { PMU.vbusIn = true; };
            int mounts = SD.begins;
            powerOffForVbus();
            CHECK(SD.begins == mounts + 1 && gTripFS == &SD && gSdMounted && gFlashMounted &&
                  g_sdDisk.mounted && g_disk.mounted && g_lockDepth == 0 && !gNetHold && !gNetAbort,
                  "recovery remounts SD and flash, then releases storage and networking");
            CHECK(!g_sdDisk.formatRequested && !g_disk.formatRequested &&
                  g_disk.files.at("/trips/old.gpx") == "preserved flash backlog",
                  "recovery does not format or discard either queue");
            CHECK(PMU.shutdownCalls == (failedShutdown ? 1 : 0),
                  "late VBUS return cancels shutdown; a returned shutdown call recovers");
            PMU.vbusIn = true; gVbusLost = false;
            drive(15, 40); endTrip(false);
            CHECK(g_sdDisk.files.size() == 2 && g_sdDisk.openHandles == 0,
                  "the next trip still records to SD after recovery");
        }
        for (bool failFlash : {false, true}) {
            resetWorld(); SD.cardPresent = true; setup(); drive(15, 40); PMU.vbusIn = false;
            g_sdDisk.onUnmount = [] { PMU.vbusIn = true; };
            if (failFlash) g_disk.failMount = true;
            else SD.cardPresent = false;
            try { powerOffForVbus(); } catch (const Restarted&) {}
            CHECK(g_restarts == 1 && g_lockDepth == 1 && gNetHold && gTripFS == &SD &&
                  !g_sdDisk.formatRequested && !g_disk.formatRequested,
                  "failed remount restarts with mutex held and never switches stores underneath logging");
        }
    }

    printf("T56 FAT repair, interrupted replacement and both boot-time queues (#109)\n");
    {
        const std::string path = "/trips/repair.gpx";
        const std::string partial = std::string(GPX_PROLOGUE) +
            "<trkpt lat=\"1\" lon=\"2\"></trkpt>\n<trkpt lat=";
        resetWorld(); SD.cardPresent = true;
        g_sdDisk.files[path] = partial; g_disk.files[path] = partial;
        setup();
        CHECK(wellFormedShape(g_sdDisk.files.at(path)) && wellFormedShape(g_disk.files.at(path)) &&
              !g_sdDisk.files.count(path + ".fix"),
              "boot repairs SD and flash, including FAT rename over an existing destination");
        g_sdDisk.files[path] = partial; g_sdDisk.failRenames = 1;
        fsckAllTrips(SD);
        CHECK(!g_sdDisk.files.count(path) && g_sdDisk.files.count(path + ".fix") &&
              wellFormedShape(g_sdDisk.files.at(path + ".fix")),
              "failure after original removal retains the complete closed repair copy");
        fsckAllTrips(SD);
        CHECK(wellFormedShape(g_sdDisk.files.at(path)) && !g_sdDisk.files.count(path + ".fix"),
              "the next boot recovers an orphan FAT repair without losing trackpoints");
        g_sdDisk.files[path] = partial; g_sdDisk.failRemoves = 1;
        fsckAllTrips(SD);
        CHECK(g_sdDisk.files.at(path) == partial && g_sdDisk.files.count(path + ".fix"),
              "failed FAT removal keeps the original and the verified repair");
        fsckAllTrips(SD);
        CHECK(wellFormedShape(g_sdDisk.files.at(path)), "a verified repair publishes on the next retry");
    }

    printf("T57 split decision reserves closing tags and buffered points\n");
    {
        for (size_t pending : {size_t(0), PENDING_CAPACITY}) {
            size_t reserve = (pending + 1) * (TRKPT_LINE_BYTES - 1) + strlen(GPX_EPILOGUE);
            CHECK(!tripShouldSplit(TRIP_SPLIT_BYTES - reserve - 1, pending),
                  "one byte below the reserved boundary can accept another fix");
            CHECK(tripShouldSplit(TRIP_SPLIT_BYTES - reserve, pending),
                  "exact boundary splits before the next fix can exceed the cap");
        }
    }

    printf("T58 SD split keeps the boundary fix, stats and upload queue\n");
    {
        resetWorld(); SD.cardPresent = true; g_sdDisk.capacity = 32 * 1024 * 1024;
        setup(); drive(15, 40);
        CHECK(gTripBytes == g_sdDisk.files.at(gTripPath).size(), "counts prologue and arm/active points");
        std::string first = gTripPath;
        padTripTo(TRIP_SPLIT_BYTES - (TRKPT_LINE_BYTES - 1) - strlen(GPX_EPILOGUE) - 1);
        drive(5, 1);
        std::string second = gTripPath;
        CHECK(gState == ACTIVE && first != second && g_sdDisk.files.size() == 2 && logHas("Trip split at"),
              "finalizes and starts a different file in the same fix, with a Serial split line");
        CHECK(wellFormedShape(g_sdDisk.files.at(first)) && g_sdDisk.files.at(first).size() < TRIP_SPLIT_BYTES,
              "closed first piece is valid and strictly under 8 MiB");
        CHECK(times(g_sdDisk.files.at(first)).back() == times(g_sdDisk.files.at(second)).front() &&
              count(g_sdDisk.files.at(second), "<trkpt ") == 1,
              "boundary point belongs to both pieces without an arming gap");
        CHECK(gTripMeters == 0 && gMaxSpeedMps == 5 && gTripT0 == gLastFix.monoMs &&
              gLastMoveMs == gTripT0 && gPendCount == 0 && gArmCount == 0 &&
              gTripBytes == g_sdDisk.files.at(second).size(),
              "new piece starts with fresh distance, speed, time and byte count");
        drive(5, 1);
        CHECK(gTripMeters > 4 && gTripMeters < 6 && count(g_sdDisk.files.at(second), "<trkpt ") == 2,
              "next fix adds one step from the shared boundary point");
        // Repeat at the same timestamp, as can happen when startup and a
        // split land within one clock second. Names must still be unique.
        padTripTo(TRIP_SPLIT_BYTES - (TRKPT_LINE_BYTES - 1) - strlen(GPX_EPILOGUE) - 1);
        tripFsmOnFix(gLastFix);
        CHECK(gState == ACTIVE && gTripPath != second && gTripPath != first && g_sdDisk.files.size() == 3,
              "persistent counter prevents same-second filename collisions");
        CHECK(uploadPending() && g_httpRequests == 2 && g_sdDisk.files.size() == 1 &&
              g_sdDisk.files.count(gTripPath), "each closed piece uploads while the new piece stays active");
    }

    printf("T59 split flushes pre-UTC points and preserves cooldown deadline\n");
    {
        resetWorld(); SD.cardPresent = true; g_sdDisk.capacity = 32 * 1024 * 1024;
        setup(); gClockSet = false; drive(15, 110);
        std::string first = gTripPath;
        CHECK(gPendCount == PENDING_CAPACITY, "pending ring is full before split");
        size_t reserve = (gPendCount + 1) * (TRKPT_LINE_BYTES - 1) + strlen(GPX_EPILOGUE);
        padTripTo(TRIP_SPLIT_BYTES - reserve - 1);
        drive(15, 1);
        CHECK(gState == ACTIVE && first != gTripPath && gPendCount == 1 &&
              wellFormedShape(g_sdDisk.files.at(first)) && g_sdDisk.files.at(first).size() < TRIP_SPLIT_BYTES &&
              count(g_sdDisk.files.at(first), "<trkpt ") == 111 && times(g_sdDisk.files.at(first)).empty(),
              "full pending tail closes under cap without dropping or inventing times");
        CHECK(gTripBytes == strlen(GPX_PROLOGUE), "new untimed boundary fix is pending, prologue is counted");
        gClockSet = true; drive(15, 1);
        CHECK(gPendCount == 0 && gTripBytes == g_sdDisk.files.at(gTripPath).size() &&
              count(g_sdDisk.files.at(gTripPath), "<trkpt ") == 2,
              "UTC arrival counts the flushed boundary point and current fix");

        drive(0, 1); uint32_t coolT0 = gCoolT0;
        drive(0, 100); first = gTripPath;
        padTripTo(TRIP_SPLIT_BYTES - (TRKPT_LINE_BYTES - 1) - strlen(GPX_EPILOGUE) - 1);
        drive(0, 1);
        CHECK(gState == COOLDOWN && first != gTripPath && gCoolT0 == coolT0 &&
              gTripMeters == 0 && gMaxSpeedMps == 0 && gTripT0 == gLastFix.monoMs,
              "stationary split keeps the original cooldown deadline and resets per-piece stats");
        drive(0, 79);
        CHECK(gState == IDLE && gTripPath[0] == 0,
              "original three-minute cooldown still ends the trip after a split");
    }

    printf("T60 failed split startup leaves the old piece safe and stops recording\n");
    {
        for (int fault = 0; fault < 3; ++fault) {
            resetWorld(); SD.cardPresent = true; g_sdDisk.capacity = 32 * 1024 * 1024;
            setup(); drive(15, 40);
            std::string first = gTripPath;
            padTripTo(TRIP_SPLIT_BYTES - (TRKPT_LINE_BYTES - 1) - strlen(GPX_EPILOGUE) - 1);
            if (fault == 0) g_sdDisk.capacity = g_sdDisk.used() + FS_START_FREE - 1;
            if (fault == 1) g_sdDisk.failOpenPath = "/trips/t000002.gpx";
            if (fault == 2) { g_sdDisk.flushesBeforeFail = 2; g_sdDisk.failFlushes = 1; }
            drive(15, 1);
            CHECK(gState == IDLE && gTripPath[0] == 0 && gPendCount == 0 && gArmCount == 0 &&
                  g_sdDisk.files.size() == 1 && wellFormedShape(g_sdDisk.files.at(first)) &&
                  g_sdDisk.files.at(first).size() < TRIP_SPLIT_BYTES,
                  "storage full, open failure or prologue failure leaves only a valid queued old piece");
            CHECK(logHas(fault == 0 ? "trip storage full" : "could not create trip file"),
                  "split startup failure uses existing error logging");
        }
    }

    printf(g_fail ? "\n%d FAILED\n" : "\nALL PASS\n", g_fail);
    return g_fail ? 1 : 0;
}
