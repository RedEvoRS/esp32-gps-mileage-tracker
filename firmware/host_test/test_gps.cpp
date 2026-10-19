// Host harness for 04_trip_logger.ino's GPS input path (audit F2). Built
// against the REAL TinyGPSPlus: it feeds the sketch's loop() the NMEA a
// NEO-M8N sends at its defaults (RMC, VTG, GGA, GSA, 3x GSV, GLL every
// second), one sentence per loop() call as the UART would deliver it, and
// checks one trackpoint is logged per epoch. Also checks the UBX config
// frames, cold-start readiness, and nonblocking ACK/retry handling.
#include "arduino_stub.h"

uint32_t g_millis = 0;
uint64_t g_epochBaseMs = 0;
SerialStub Serial;
FakeDisk g_disk;
LittleFSStub LittleFS;
int g_lockDepth = 0;
TwoWire Wire, Wire1;
WiFiStub WiFi;

#include "04_trip_logger.ino"

#include <fstream>
#include <new>

static int g_fail = 0;
static std::string g_outDir;
#define CHECK(cond, msg) do { if (cond) printf("  PASS %s\n", msg); \
    else { printf("  FAIL %s  (line %d)\n", msg, __LINE__); g_fail++; } } while (0)

// ---- NMEA generation ------------------------------------------------------
static std::string nmea(const std::string& body) {
    uint8_t c = 0;
    for (char ch : body) c ^= (uint8_t)ch;
    char tail[8];
    snprintf(tail, sizeof tail, "*%02X\r\n", c);
    return "$" + body + tail;
}
static std::string coord(double v, bool isLat) {
    double a = fabs(v);
    int deg = (int)a;
    char b[32];
    snprintf(b, sizeof b, isLat ? "%02d%08.5f,%c" : "%03d%08.5f,%c", deg, (a - deg) * 60.0,
             isLat ? (v < 0 ? 'S' : 'N') : (v < 0 ? 'W' : 'E'));
    return b;
}

struct Epoch { int t; double lat, lon; float mps; bool fix; };

// One second of default M8N output. ggaFirst swaps the RMC/GGA order.
static std::vector<std::string> sentences(const Epoch& e, bool ggaFirst = false) {
    char hms[16], b[200];
    int s = 30 * 60 + e.t;   // epoch 0 = 14:30:00
    snprintf(hms, sizeof hms, "14%02d%02d.00", s / 60, s % 60);
    std::string ll = coord(e.lat, true) + "," + coord(e.lon, false);
    double kn = e.mps / 0.514444444;
    std::string rmc, gga;
    if (e.fix) {
        snprintf(b, sizeof b, "GNRMC,%s,A,%s,%.3f,0.00,140426,,,A", hms, ll.c_str(), kn); rmc = b;
        snprintf(b, sizeof b, "GNGGA,%s,%s,1,09,0.90,12.5,M,-30.0,M,,", hms, ll.c_str()); gga = b;
    } else {
        snprintf(b, sizeof b, "GNRMC,%s,V,,,,,,,140426,,,N", hms); rmc = b;
        snprintf(b, sizeof b, "GNGGA,%s,,,,,0,00,99.99,,,,,,", hms); gga = b;
    }
    snprintf(b, sizeof b, "GNVTG,0.00,T,,M,%.3f,N,%.3f,K,A", kn, e.mps * 3.6);
    std::string vtg = b;
    std::vector<std::string> out;
    if (ggaFirst) { out.push_back(nmea(gga)); out.push_back(nmea(rmc)); }
    else          { out.push_back(nmea(rmc)); out.push_back(nmea(vtg)); out.push_back(nmea(gga)); }
    out.push_back(nmea("GNGSA,A,3,02,05,12,13,15,18,20,25,29,,,,1.50,0.90,1.20,1"));
    out.push_back(nmea("GPGSV,3,1,11,02,40,083,46,05,17,308,41,12,07,344,39,13,22,228,45,1"));
    out.push_back(nmea("GPGSV,3,2,11,15,62,175,48,18,33,051,44,20,14,121,40,25,54,267,47,1"));
    out.push_back(nmea("GPGSV,3,3,11,29,09,028,35,31,05,196,,32,11,152,,1"));
    if (e.fix) { snprintf(b, sizeof b, "GNGLL,%s,%s,A,A", ll.c_str(), hms); out.push_back(nmea(b)); }
    if (ggaFirst) out.push_back(nmea(vtg));
    return out;
}

static double gLat = 37.0, gLon = -122.0;
static int gT = 0;
// Reuse the harness's invented position; clock fields are independent of it.
static std::string rmc(const std::string& time, const std::string& date,
                       const std::string& talker = "GNRMC") {
    return nmea(talker + "," + time + ",A," + coord(gLat, true) + "," +
                coord(gLon, false) + ",29.158,0.00," + date + ",,,A");
}
static std::string navPvt(uint32_t tow, uint32_t sAcc, uint32_t hAcc = 2000,
                          uint8_t fixType = 3, uint8_t flags = 1, int32_t speed = 15000);

// Deliver each sentence as the UART would: ~70 ms apart, loop() between.
static void run(float mps, int seconds, bool fix = true, bool ggaFirst = false,
                int pvtSAcc = -1, bool pvtAfter = false, uint8_t fixType = 3, uint8_t flags = 1,
                uint32_t pvtHAcc = 2000) {
    for (int i = 0; i < seconds; ++i, ++gT) {
        if (fix) gLat += mps / 111320.0;
        auto ss = sentences({gT, gLat, gLon, mps, fix}, ggaFirst);
        for (size_t k = 0; k < ss.size(); ++k) {
            uint32_t at = (uint32_t)gT * 1000 + (uint32_t)k * 70;
            if (g_millis < at) g_millis = at;
            if (pvtSAcc >= 0 && k == (pvtAfter ? ss.size() - 1 : 0))
                GPSSerial.rx += navPvt(gT * 1000, pvtSAcc, pvtHAcc, fixType, flags, int32_t(mps * 1000));
            GPSSerial.rx += ss[k];
            loop();
        }
    }
}

static void resetWorld() {
    g_disk = FakeDisk();
    g_sdDisk = FakeDisk(); g_sdDisk.mounted = false; g_sdDisk.renameOverwrites = false;
    SD.dirs.clear(); SD.cardPresent = false; SD.begins = SD.ends = 0;
    gTripFS = &LittleFS; gSdMounted = false; gFlashMounted = true;
    g_powerEvents.clear();
    LittleFS.dirs.clear();
    LittleFS.mkdir(TRIP_DIR);
    prefs.kv.clear();
    Serial.log.clear();
    gps = TinyGPSPlus();                      // fresh parser, empty listener list
    gRmcStatusGN.begin(gps, "GNRMC", 2);      // re-register in place
    gRmcStatusGP.begin(gps, "GPRMC", 2);
    gRmcTimeGN.begin(gps, "GNRMC", 1);
    gRmcDateGN.begin(gps, "GNRMC", 9);
    gRmcTimeGP.begin(gps, "GPRMC", 1);
    gRmcDateGP.begin(gps, "GPRMC", 9);
#if DISPLAY_DEBUG
    gDebugGsv = DebugGsv();
    gDebugRf = DebugRf(); gDebugRfPollMs = 0; gDebugRfStarted = false;
    gDebugRmcValid = false;
    gDebugDisplayReady = gDebugDisplayFailed = false;
    gDebugNet = DebugNetwork{};
#endif
    GPSSerial = HardwareSerial(1);
    gGpsConfig = GpsConfig(); gGpsUbxRx = GpsUbxRx();
    gNavPvt = GpsNavPvt(); gSpeedFallback = false; gLatestSpeedTrusted = false;
    // Legacy regressions model completed config and a >10 s wait for PVT.
    // Startup/freshness tests below explicitly restore the real boot deadline.
    gGpsConfig.state = GPS_CONFIG_DONE;
    gNavPvtWaitMs = uint32_t(0 - NAV_PVT_FALLBACK_MS - 1);
    gDistanceContinuous = true;
    gState = IDLE; gResumeFixes = 0; gTripPath[0] = 0; gTripWriteFailed = false;
    gPendCount = gPendHead = 0; armBufReset(); gHaveLastFix = false;
    gClockSet = false; gVbusLost = false;
    gGpsClockSet = false; gLastGpsSyncMs = 0;
    g_sntpStatus = SNTP_SYNC_STATUS_RESET;
    g_sntpCompleteAfterPolls = -1; g_sntpStopCalls = g_sntpPolls = 0;
    g_sntpPollHook = nullptr;
    g_delayHook = nullptr;
    g_millis = 0; gT = 0; gLat = 37.0;
    g_epochBaseMs = 0;
}

static std::vector<std::string> tripFiles() {
    std::vector<std::string> v;
    for (auto& kv : g_disk.files) if (kv.first.rfind("/trips/", 0) == 0) v.push_back(kv.first);
    return v;
}
static std::vector<std::string> trkpts(const std::string& d) {
    std::vector<std::string> v;
    for (size_t p = d.find("<trkpt "); p != std::string::npos; p = d.find("<trkpt ", p + 1))
        v.push_back(d.substr(p, d.find("</trkpt>", p) - p));
    return v;
}
static std::string field(const std::string& pt, const char* tag) {
    std::string open = std::string("<") + tag + ">", close = std::string("</") + tag + ">";
    size_t a = pt.find(open); if (a == std::string::npos) return "";
    a += open.size(); return pt.substr(a, pt.find(close, a) - a);
}
static void dump(const char* test) {
    int i = 0;
    for (auto& f : tripFiles()) {
        std::ofstream o(g_outDir + "/" + test + "_" + std::to_string(i++) + ".gpx", std::ios::binary);
        o << g_disk.files[f];
    }
}

// Expected UBX bytes, computed independently of the sketch's encoder.
static std::string frame(uint8_t cls, uint8_t id, std::vector<uint8_t> p) {
    std::vector<uint8_t> body = { cls, id, (uint8_t)p.size(), (uint8_t)(p.size() >> 8) };
    body.insert(body.end(), p.begin(), p.end());
    unsigned a = 0, b = 0;
    for (uint8_t x : body) { a = (a + x) & 0xFF; b = (b + a) & 0xFF; }
    std::string s = "\xB5\x62";
    for (uint8_t x : body) s.push_back((char)x);
    s.push_back((char)a); s.push_back((char)b);
    return s;
}
static std::string ack(uint8_t cls, uint8_t id, bool ok = true) {
    return frame(0x05, ok ? 0x01 : 0x00, { cls, id });
}

static std::vector<uint8_t> navPayload(uint32_t tow, uint32_t sAcc, uint32_t hAcc,
                                      uint8_t fixType, uint8_t flags, int32_t speed) {
    std::vector<uint8_t> p(92, 0);
    auto put4 = [&p](size_t off, uint32_t value) {
        for (unsigned i = 0; i < 4; ++i) p[off + i] = uint8_t(value >> (8 * i));
    };
    put4(0, tow); p[11] = 7; p[20] = fixType; p[21] = flags; p[23] = 12;
    // Binary sentinels: NAV-PVT position bytes must never enter a buffer,
    // serial/OLED diagnostics or GPX; only RMC supplies recorded positions.
    put4(24, 0x51525354); put4(28, 0x61626364);
    put4(40, hAcc); put4(60, uint32_t(speed)); put4(68, sAcc);
    return p;
}
static std::string navPvt(uint32_t tow, uint32_t sAcc, uint32_t hAcc,
                          uint8_t fixType, uint8_t flags, int32_t speed) {
    return frame(0x01, 0x07, navPayload(tow, sAcc, hAcc, fixType, flags, speed));
}

// Only respond once the entire command (including checksum) is on the
// wire. Preloading ACKs would incorrectly allow a reply before its send.
static void respondToGps(std::function<std::string(uint8_t, uint8_t)> reply) {
    GPSSerial.onWrite = [parsed = GPSSerial.tx.size(), reply](HardwareSerial& port) mutable {
        if (port.tx.size() - parsed < 6) return;
        const auto* p = (const uint8_t*)port.tx.data() + parsed;
        size_t len = p[4] | (p[5] << 8);
        if (port.tx.size() - parsed < len + 8) return;
        uint8_t cls = p[2], id = p[3];
        parsed += len + 8;
        port.rx += reply(cls, id);
    };
}
static void ackGps() {
    respondToGps([](uint8_t cls, uint8_t id) { return ack(cls, id); });
}
static void gpsLoops(int count = 8) {
    for (int i = 0; i < count; ++i) loop();
}
static int logCount(const char* fragment) {
    int count = 0;
    for (const auto& line : Serial.log) if (line.find(fragment) != std::string::npos) count++;
    return count;
}

int main(int argc, char** argv) {
    g_outDir = argc > 1 ? argv[1] : ".";

    printf("G1 UBX config frames + ACKs\n");
    {
        resetWorld();
        ackGps(); configureGps();
        CHECK(GPSSerial.tx.empty(), "no command before checksum-valid NMEA");
        GPSSerial.rx = nmea("GNTXT,01,01,02,receiver booting");
        gpsLoops();
        CHECK(gGpsConfig.state == GPS_CONFIG_DONE && gGpsConfig.command == 6,
              "all six messages are ACKed without needing a position fix");
        std::vector<uint8_t> nav5(36, 0); nav5[0] = 0x01; nav5[2] = 4;
        std::string want = frame(0x06, 0x01, { 0xF0, 0x01, 0 }) + frame(0x06, 0x01, { 0xF0, 0x02, 0 }) +
                           frame(0x06, 0x01, { 0xF0, 0x03, DISPLAY_DEBUG ? 5 : 0 }) + frame(0x06, 0x01, { 0xF0, 0x05, 0 }) +
                           frame(0x06, 0x24, nav5) + frame(0x06, 0x01, {0x01, 0x07, 1});
        if (DISPLAY_DEBUG) want += frame(0x0A, 0x38, {});
        CHECK(GPSSerial.tx == want, "sends GLL/GSA/GSV/VTG rates + NAV5 automotive + current-port NAV-PVT rate 1");
        const char gll[] = "\xB5\x62\x06\x01\x03\x00\xF0\x01\x00\xFB\x11";
        CHECK(GPSSerial.tx.compare(0, 11, std::string(gll, 11)) == 0,
              "GLL-off frame matches the published u-blox bytes");
        CHECK(g_millis <= 8 && logCount("GPS configured on attempt 1 (") == 1 &&
              logCount("ms after config start") == 1,
              "loop does not wait for ACKs and success is logged once");
    }

    printf("G2 glitch, invalid checksum and incomplete NMEA cannot prove readiness\n");
    {
        resetWorld(); ackGps(); configureGps();
        GPSSerial.rx = std::string(1, '\xFF');
        loop();
        CHECK(GPSSerial.tx.empty(), "UART glitch does not trigger a command");
        std::string bad = nmea("GNTXT,01,01,02,not ready");
        bad[bad.size() - 4] = bad[bad.size() - 4] == '0' ? '1' : '0';
        GPSSerial.rx += bad;
        loop();
        CHECK(GPSSerial.tx.empty(), "a bad NMEA checksum does not trigger a command");
        std::string ready = nmea("GNRMC,143000.00,V,,,,,,,140426,,,N");
        GPSSerial.rx += ready.substr(0, ready.size() - 2);
        g_millis = 999; loop();
        CHECK(GPSSerial.tx.empty(), "a sentence without its terminator is not ready");
        GPSSerial.rx += "\r\n";
        loop();
        CHECK(!GPSSerial.tx.empty() && gGpsConfig.state == GPS_WAIT_ACK,
              "configuration starts as soon as a complete valid sentence arrives");
        gpsLoops();
        CHECK(logCount("GPS ready:") == 1 && logCount("GPS configured on attempt 1 (") == 1,
              "readiness and first-attempt success each log once");
    }

    printf("G3 a NAK schedules another complete attempt\n");
    {
        resetWorld(); configureGps();
        respondToGps([](uint8_t cls, uint8_t id) { return ack(cls, id, id != 0x24); });
        GPSSerial.rx = nmea("GNTXT,01,01,02,ready");
        gpsLoops();
        CHECK(gGpsConfig.state == GPS_RETRY && gGpsConfig.attempts == 1,
              "NAV5 NAK leaves the first attempt pending for retry");
        CHECK(GPSSerial.tx.size() == 4 * 11 + 44, "a NAK stops this attempt without repeating the rejected command");
        ackGps();
        uint32_t retryAt = gGpsConfig.since + GPS_CONFIG_RETRY_MS;
        g_millis = retryAt - 2; loop();
        CHECK(gGpsConfig.attempts == 1, "no retry before the eight-second spacing");
        g_millis = retryAt; gpsLoops();
        CHECK(gGpsConfig.command == 6 && gGpsConfig.attempts == 2 && GPSSerial.tx.size() == 88 + 99 + (DISPLAY_DEBUG ? 8 : 0),
              "second attempt resends the full sequence including NAV-PVT and succeeds");
        CHECK(logCount("retrying in background") == 1 && logCount("configured on attempt 2 (") == 1 &&
              logCount("ms after config start") == 1,
              "pending and eventual success each log once");
    }

    printf("G4 default M8N output -> one point per second (F2)\n");
    {
        resetWorld();
        run(0, 5); run(15, 60); run(0, 200);
        auto v = tripFiles();
        CHECK(v.size() == 1 && gState == IDLE, "one finalized trip");
        if (v.size() == 1) {
            auto pts = trkpts(g_disk.files[v[0]]);
            CHECK(pts.size() == 60 + 181, "60 moving + 181 cooldown points, no duplicates");
            CHECK(!pts.empty() && field(pts[0], "time") == "2026-04-14T14:30:05Z",
                  "trip starts at the first moving second (ARMING buffer intact)");
            bool distinct = true;
            for (size_t i = 1; i < pts.size(); ++i)
                if (field(pts[i], "time") == field(pts[i - 1], "time")) distinct = false;
            CHECK(distinct, "every point has its own timestamp");
            CHECK(pts.size() > 60 && field(pts[60], "speed") == "0.00",
                  "first stopped point carries this second's speed, not the last one's");
            CHECK(pts.size() > 0 && field(pts[0], "hdop") == "0.9",
                  "HDOP still comes through from GGA");
        }
        dump("G4");
#if DISPLAY_DEBUG
        CHECK(gDebugGsv.signal(g_millis).heard == 9,
              "live GSV excludes empty C/N0 fields while the same drive logs normally");
        g_disk.files["/trips/incomplete.gpx"] = "<gpx>";
        g_disk.files["/trips/rejected.gpx.bad"] = "</gpx>";
        refreshDebugPending();
        CHECK(gDebugNet.pending == 1 && g_lockDepth == 0,
              "cached queue counts complete trips only and releases every FS lock");
        gNetHold.store(true); gDebugNet.pending = 7;
        refreshDebugPending();
        CHECK(gDebugNet.pending == 7,
              "power-off interruption preserves the previous cache instead of publishing a partial scan");
#endif
    }

    printf("G5 GGA before RMC\n");
    {
        resetWorld();
        run(0, 5, true, true); run(15, 60, true, true); run(0, 200, true, true);
        auto v = tripFiles();
        CHECK(v.size() == 1 && trkpts(g_disk.files[v[0]]).size() == 60 + 181, "still one point per second");
    }

    printf("G6 no fix\n");
    {
        resetWorld();
        run(15, 30, false);
        CHECK(tripFiles().empty() && gState == IDLE, "nothing processed without a fix");
        run(15, 20);            // fix acquired: trip starts
        run(15, 30, false);     // fix lost mid-drive
        auto v = tripFiles();
        CHECK(v.size() == 1 && trkpts(g_disk.files[v[0]]).size() == 20,
              "no points logged while RMC says no fix");
    }

    printf("G7 carried RTC does not prove SNTP synchronization\n");
    {
        resetWorld();
        g_epochBaseMs = 1776177000000ULL; // plausible retained date, NTP unreachable
        g_sntpStatus = SNTP_SYNC_STATUS_COMPLETED; // stale earlier attempt
        trySntp();
        CHECK(!gClockSet && g_millis == 5000, "waits bounded five seconds; carried date and stale completion are untrusted");
        CHECK(g_sntpStopCalls == 2, "stops background SNTP after timeout");
        GPSSerial.rx = sentences({0, 37, -122, 0, true})[0];
        loop();
        CHECK(gClockSet && nowEpochMs() == 1776177000001ULL,
              "fresh GPS corrects retained RTC even after unsuccessful NTP");
    }

    printf("G8 actual SNTP completion, then GPS correction\n");
    {
        resetWorld();
        g_sntpCompleteAfterPolls = 4;
        g_sntpEpochMs = 1776177045000ULL; // NTP clock is 45 seconds ahead of GPS fixture
        trySntp();
        CHECK(gClockSet && g_millis == 300 && nowEpochMs() == g_sntpEpochMs,
              "trusts a new completed SNTP synchronization");
        CHECK(g_sntpStopCalls == 2, "stops SNTP after successful attempt");
        GPSSerial.rx = sentences({0, 37, -122, 0, true})[0];
        loop();
        CHECK(gGpsClockSet && nowEpochMs() == 1776177000001ULL,
              "first fresh GPS corrects time even when SNTP already set it");
        uint32_t lastSync = gLastGpsSyncMs;
        g_epochBaseMs += 20000; // inject RTC drift
        g_millis += 1000;
        GPSSerial.rx += sentences({1, 37, -122, 0, true})[0]; loop();
        CHECK(gLastGpsSyncMs == lastSync, "does not step the clock for every fix");
        g_millis = lastSync + GPS_CLOCK_SYNC_MS;
        GPSSerial.rx += sentences({60, 37, -122, 0, true})[0]; loop();
        CHECK(nowEpochMs() == 1776177060001ULL, "fresh GPS removes drift at the next minute");
    }

    printf("G9 stale GPS and midnight sentence ordering\n");
    {
        resetWorld();
        auto oldRmc = nmea("GNRMC,235959.00,A,3700.0,N,12200.0,W,0.0,0.0,140426,,,A");
        for (char c : oldRmc) gps.encode(c);
        g_millis = 3000;
        setClockFromGps();
        CHECK(!gClockSet, "cached date/time older than two seconds cannot set UTC");
        GPSSerial.rx = nmea("GNGGA,000000.00,3700.0,N,12200.0,W,1,09,0.90,12.5,M,-30.0,M,,");
        loop();
        CHECK(!gClockSet, "fresh GGA time cannot reuse yesterday's RMC date");
        GPSSerial.rx += nmea("GNRMC,000000.25,A,3700.0,N,12200.0,W,0.0,0.0,150426,,,A");
        loop();
        CHECK(gClockSet && nowEpochMs() == 1776211200251ULL,
              "new RMC sets the matching next-day date and fractional seconds");
    }

    printf("G10 empty RMC date/time cannot renew cached UTC\n");
    for (const char* talker : {"GNRMC", "GPRMC"}) {
        resetWorld();
        GPSSerial.rx = nmea(std::string(talker) + ",235959.00,A,3700.0,N,12200.0,W,0.0,0.0,140426,,,A");
        loop();
        CHECK(gGpsClockSet, "valid RMC establishes cached date/time");
        uint32_t lastSync = gLastGpsSyncMs;
        g_millis = GPS_CLOCK_SYNC_MS;
        GPSSerial.rx += nmea(std::string(talker) + ",000100.00,A,3700.0,N,12200.0,W,0.0,0.0,,,,A");
        loop();
        CHECK(gLastGpsSyncMs == lastSync, "empty date cannot reuse yesterday at midnight");
        GPSSerial.rx += nmea(std::string(talker) + ",,A,3700.0,N,12200.0,W,0.0,0.0,150426,,,A");
        loop();
        CHECK(gLastGpsSyncMs == lastSync, "empty time cannot refresh a cached time");
        GPSSerial.rx += nmea(std::string(talker) + ",000100.25,A,3700.0,N,12200.0,W,0.0,0.0,150426,,,A");
        loop();
        CHECK(nowEpochMs() == 1776211260251ULL, "next complete RMC corrects the clock");
    }

    printf("G11 GPS arrives during an SNTP wait\n");
    {
        resetWorld();
        g_sntpPollHook = []() {
            GPSSerial.rx = sentences({0, 37, -122, 0, true})[0];
            loop();
            g_sntpPollHook = nullptr;
        };
        trySntp();
        CHECK(gGpsClockSet && gClockSet && nowEpochMs() == 1776177000101ULL,
              "GPS source wins without waiting for an NTP timeout");
        CHECK(g_sntpStopCalls == 2 && g_sntpPolls == 1, "network task stops its SNTP attempt after GPS success");
    }

    printf("G12 the first fix after a GPS gap starts its own segment (#99)\n");
    {
        resetWorld(); run(15, 30);
        gT += (SEG_GAP_MS / 1000) + 1;
        run(15, 10); endTrip(false);
        const std::string& d = g_disk.files.begin()->second;
        size_t split = d.find("</trkseg><trkseg>");
        CHECK(trkpts(d).size() == 40 && split != std::string::npos &&
              d.find("</trkseg><trkseg>", split + 1) == std::string::npos &&
              trkpts(d.substr(0, split)).size() == 30,
              "real NMEA/loop path marks only the first post-gap fix");
    }

    printf("G13 silent first configuration retries during an active trip\n");
    {
        resetWorld(); gGpsConfig.state = GPS_CONFIG_OFF;
        run(15, 20, true, false, 100);
        CHECK(gState == ACTIVE, "trip is recording before configuration is requested");
        configureGps();
        respondToGps([](uint8_t cls, uint8_t id) {
            return gGpsConfig.attempts > 1 ? ack(cls, id) : std::string();
        });
        g_delayHook = [](uint32_t) { CHECK(false, "configuration must not delay loop"); };
        run(15, 20, true, false, 100);
        g_delayHook = nullptr;
        CHECK(gGpsConfig.command == 6 && gGpsConfig.attempts == 2,
              "no-answer first attempt succeeds on a later loop attempt");
        std::string first = frame(0x06, 0x01, {0xF0, 0x01, 0});
        CHECK(GPSSerial.tx.substr(0, first.size() * 3) == first + first + first &&
              GPSSerial.tx.size() == 3 * 11 + 99 + (DISPLAY_DEBUG ? 8 : 0),
              "silent attempt sends only the first command three times, then retries the entire set");
        CHECK(logCount("retrying in background") == 1 && logCount("configured on attempt 2 (") == 1,
              "no per-send or per-attempt log spam");
        endTrip(false);
        auto files = tripFiles();
        CHECK(files.size() == 1, "the active trip remains intact");
        if (files.size() == 1) {
            auto pts = trkpts(g_disk.files[files[0]]);
            bool intact = pts.size() == 40;
            for (size_t i = 0; i < pts.size(); ++i) {
                char want[32];
                snprintf(want, sizeof want, "2026-04-14T14:30:%02uZ", (unsigned)i);
                // The first RMC precedes the first GGA in this fixture.
                intact &= field(pts[i], "time") == want &&
                          field(pts[i], "hdop") == (i == 0 ? "99.9" : "0.9");
            }
            CHECK(intact, "all 40 consecutive RMC fixes and GGA HDOP survive the silent ACK deadlines and retry");
        }
    }

    printf("G14 ACK framing/checksum and interleaved NMEA\n");
    {
        resetWorld(); configureGps();
        GPSSerial.rx = nmea("GNTXT,01,01,02,ready"); loop();
        std::string corrupt = ack(0x06, 0x01); corrupt.back() ^= 1;
        GPSSerial.rx += ack(0x06, 0x24) + ack(0x07, 0x01) + corrupt;
        loop();
        CHECK(gGpsConfig.command == 0 && gGpsConfig.sends == 1,
              "wrong command/class and bad ACK checksum cannot acknowledge a setting");
        std::string rmc = sentences({0, 37, -122, 15, true})[0];
        size_t split = rmc.size() / 2;
        GPSSerial.rx += rmc.substr(0, split) + ack(0x06, 0x01).substr(0, 9);
        loop();
        CHECK(gArmCount == 0 && gGpsConfig.command == 0,
              "partial NMEA and an ACK missing its checksum byte remain pending");
        GPSSerial.rx += ack(0x06, 0x01).substr(9) + navPvt(0, 100) + rmc.substr(split);
        loop();
        CHECK(gArmCount == 1 && gGpsConfig.command == 1 && gps.passedChecksum() == 2,
              "binary ACK is removed and the split RMC resumes with its checksum and fix intact");
        // Ignore binary payload, even one containing complete-looking NMEA.
        std::string fakeNmea = nmea("GNTXT,01,01,02,binary payload");
        std::vector<uint8_t> payload(fakeNmea.begin(), fakeNmea.end());
        GPSSerial.rx += frame(0x0A, 0x04, payload);
        GPSSerial.rx += sentences({1, 37.001, -122, 15, true})[0];
        GPSSerial.rx += sentences({2, 37.002, -122, 15, true})[0];
        loop();
        CHECK(gArmCount == 3 && gps.passedChecksum() == 4,
              "UBX payload is never NMEA; two RMC epochs in one drain each reach the trip FSM");
    }

    printf("G15 retries and readiness are bounded, with one final failure log\n");
    for (int response : {0, 1, 2}) {  // no NMEA, NMEA + silence, NMEA + immediate NAK
        resetWorld(); configureGps();
        uint32_t lastSendMs = 0;
        if (response) {
            GPSSerial.rx = nmea("GNTXT,01,01,02,ready");
            respondToGps([response, &lastSendMs](uint8_t cls, uint8_t id) {
                lastSendMs = g_millis;
                return response == 2 ? ack(cls, id, false) : std::string();
            });
        }
        bool prematureFallback = false;
        for (uint32_t ms = 0; ms <= 60000; ms += 100) {
            g_millis = ms; loop();
            if (gGpsConfig.state != GPS_CONFIG_DONE)
                prematureFallback |= gSpeedFallback || gpsSpeedTrusted(true) || logCount("NAV-PVT unavailable");
            else break;
        }
        CHECK(gGpsConfig.state == GPS_CONFIG_DONE && gGpsConfig.attempts == 5,
              "stops after five attempts, including a receiver that never emits valid NMEA");
        CHECK(!prematureFallback && !gSpeedFallback && !gpsSpeedTrusted(true) &&
              logCount("NAV-PVT unavailable") == 0 && gNavPvtWaitMs == gGpsConfig.since,
              "all give-up paths keep speed untrusted and start the PVT wait at config completion");
        g_millis = gNavPvtWaitMs + NAV_PVT_FALLBACK_MS;
        serviceGpsSpeedFallback();
        CHECK(!gSpeedFallback && logCount("NAV-PVT unavailable") == 0,
              "giving up still allows the full ten-second PVT wait");
        ++g_millis; serviceGpsSpeedFallback(); gpsLoops();
        CHECK(gSpeedFallback && gpsSpeedTrusted(true) && logCount("NAV-PVT unavailable") == 1,
              "more than ten seconds after giving up enters fallback with exactly one warning");
        CHECK(GPSSerial.tx.size() == (response == 0 ? 0 : response == 1 ? 15 * 11 : 5 * 11) + (DISPLAY_DEBUG ? 8 : 0),
              "config sends remain bounded; debug RF polling begins after giving up");
        if (response) CHECK(lastSendMs - gGpsConfig.started >= 32000,
                            "even immediate NAKs keep attempts available at least 32 seconds after startup");
        size_t sent = GPSSerial.tx.size();
        g_millis += 60000; gpsLoops();
        CHECK(GPSSerial.tx.size() == sent + (DISPLAY_DEBUG ? 8 : 0) && logCount("not configured after 5 attempts") == 1 &&
              logCount("retrying in background") == 1 && logCount("GPS configured") == 0 &&
              logCount("NAV-PVT unavailable") == 1,
              "config stays stopped and logs failure once while debug polling continues");
    }

    printf("G16 readiness can arrive after the first three-second timeout\n");
    {
        resetWorld(); configureGps();
        g_millis = GPS_READY_MS - 2; loop();
        CHECK(gGpsConfig.state == GPS_WAIT_READY && GPSSerial.tx.empty(),
              "readiness timeout does not expire early");
        g_millis = GPS_READY_MS; loop();
        CHECK(gGpsConfig.state == GPS_RETRY && GPSSerial.tx.empty(),
              "three seconds without valid NMEA schedules a retry without sending UBX");
        uint32_t retryAt = gGpsConfig.since + GPS_CONFIG_RETRY_MS;
        GPSSerial.rx = navPvt(4000, 100) + sentences({4, 37, -122, 15, true})[0]; loop();
        CHECK(gArmCount == 1 && GPSSerial.tx.empty(),
              "new fixes still reach the FSM while waiting for the scheduled retry");
        ackGps(); g_millis = retryAt; gpsLoops();
        CHECK(gGpsConfig.command == 6 && gGpsConfig.attempts == 2,
              "late readiness configures successfully on the next loop attempt");
    }

    printf("G17 configuration deadlines survive millis rollover\n");
    {
        resetWorld(); g_millis = UINT32_MAX - 1000; configureGps();
        GPSSerial.rx = nmea("GNTXT,01,01,02,ready"); loop();
        for (int send = 0; send < 3; ++send) {
            g_millis = gGpsConfig.since + GPS_ACK_MS; loop();
        }
        CHECK(gGpsConfig.state == GPS_RETRY, "ACK timeouts reach the scheduled retry");
        uint32_t retryAt = gGpsConfig.since + GPS_CONFIG_RETRY_MS;
        g_millis = retryAt - 2; loop();
        CHECK(gGpsConfig.attempts == 1, "retry spacing is respected across wrap");
        ackGps(); g_millis = retryAt; gpsLoops();
        CHECK(gGpsConfig.command == 6 && gGpsConfig.attempts == 2,
              "retry succeeds after unsigned deadline wraps to a small millis value");
    }

    printf("G18 receiver emits NMEA but ignores UBX for the first 30 seconds\n");
    {
        resetWorld(); configureGps();
        respondToGps([](uint8_t cls, uint8_t id) {
            return g_millis - gGpsConfig.started >= 30000 ? ack(cls, id) : std::string();
        });
        bool prematureTrust = false;
        while (gGpsConfig.state != GPS_CONFIG_DONE && gT < 60) {
            run(15, 1);
            prematureTrust |= gSpeedFallback || gpsSpeedTrusted(true) || gLatestSpeedTrusted ||
                              gState != IDLE || gArmCount || gPendCount || !tripFiles().empty() ||
                              logCount("NAV-PVT unavailable");
        }
        CHECK(!prematureTrust && gNavPvtWaitMs >= 30000 && gNavPvtWaitMs <= g_millis,
              "slow cold-start config never trusts raw RMC, arms, records or warns before completion");
        CHECK(gGpsConfig.command == 6 && gGpsConfig.attempts == 5 &&
              gGpsConfig.since - gGpsConfig.started >= 32000,
              "last attempt configures a receiver that stays unresponsive well after valid NMEA starts");
        CHECK(logCount("configured on attempt 5 (") == 1 && logCount("ms after config start") == 1 &&
              logCount("retrying in background") == 1 && logCount("not configured after") == 0,
              "reports the successful late attempt and elapsed time once");
        g_millis = gNavPvtWaitMs + NAV_PVT_FALLBACK_MS;
        serviceGpsSpeedFallback();
        CHECK(!gSpeedFallback && !gpsSpeedTrusted(true) && logCount("NAV-PVT unavailable") == 0,
              "late successful config starts its own full ten-second PVT wait");
        ++g_millis; serviceGpsSpeedFallback(); gpsLoops();
        CHECK(gSpeedFallback && gpsSpeedTrusted(true) && logCount("NAV-PVT unavailable") == 1,
              "late successful config without PVT enters fallback once after its deadline");
    }

    printf("G19 debug RF polling and receive demux\n");
    {
        resetWorld();
        for (GpsConfigState state : {GPS_CONFIG_OFF, GPS_WAIT_READY, GPS_WAIT_ACK, GPS_RETRY}) {
            gGpsConfig.state = state;
            gGpsConfig.since = g_millis = 60000;
            loop();
        }
        CHECK(GPSSerial.tx.empty() && !logCount("GPS RF:"), "no RF traffic during boot config or ACK waits");
        gGpsConfig.state = GPS_CONFIG_DONE;
        loop();
#if DISPLAY_DEBUG
        CHECK(GPSSerial.tx == frame(0x0A, 0x38, {}) && !logCount("GPS RF:"),
              "first empty MON-RF poll starts immediately after config, without waiting");
        uint32_t firstPoll = gDebugRfPollMs;
        std::vector<uint8_t> payload(28, 0);
        payload[1] = 1; payload[5] = 1; payload[6] = 1; payload[7] = 2;
        payload[16] = 85; payload[19] = 10; payload[20] = 12;
        std::string packet = frame(0x0A, 0x38, payload);
        std::string rmc = sentences({0, 37, -122, 15, true})[0];
        size_t split = rmc.size() / 2;
        GPSSerial.rx += rmc.substr(0, split) + packet.substr(0, packet.size() - 1);
        loop();
        CHECK(!gDebugRf.valid && gArmCount == 0, "MON-RF and RMC stay pending until checksums complete");
        GPSSerial.rx += packet.back() + rmc.substr(split);
        loop();
        CHECK(gDebugRf.valid && gDebugRf.agcPct == 31 && gDebugRf.cw == 12 && gArmCount == 1,
              "MON-RF payload is retained and split NMEA resumes without changing the fix");
        g_millis = firstPoll + 29998; loop();
        CHECK(GPSSerial.tx.size() == 8 && !logCount("GPS RF:"), "no early RF poll or serial line");
        g_millis = firstPoll + 30000;
        GPSSerial.rx += sentences({30, 37, -122, 15, false})[0];
        loop();
        CHECK(GPSSerial.tx == frame(0x0A, 0x38, {}) + frame(0x0A, 0x38, {}) &&
              logCount("GPS RF: jam ok cw 12 noise 85 agc 31% ant DONTKNOW pwr DONTKNOW") == 1 &&
              logCount("fix V") == 1 && !gDebugRf.valid,
              "one coordinate-free line uses the previous reply, then starts a fresh interval");
        std::string bad = packet; bad.back() ^= 1;
        GPSSerial.rx += bad + frame(0x0A, 0x38, {0, 1, 0, 0}) +
                        frame(0x0A, 0x38, std::vector<uint8_t>(257, 0));
        loop();
        CHECK(!gDebugRf.valid, "bad checksum, wrong length and oversized packets cannot publish RF data");
        g_millis = gDebugRfPollMs + 30000; loop();
        CHECK(logCount("GPS RF:") == 2 && logCount("GPS RF: rf --") == 1,
              "a missing valid reply prints rf -- instead of reusing an old sample");
        GPSSerial.rx += packet.substr(0, 12); loop();
        g_millis += 501;
        GPSSerial.rx += sentences({61, 37, -122, 15, true})[0]; loop();
        CHECK(gArmCount == 1 && !gDebugRf.valid, "truncated MON-RF times out and later NMEA still parses");
        payload.resize(52); payload[1] = 2; payload[29] = 3;
        GPSSerial.rx += frame(0x0A, 0x38, payload); loop();
        CHECK(gDebugRf.valid && gDebugRf.blocks == 2 && gDebugRf.jam == 1,
              "multiple RF blocks validate their full length and retain the first block");
        g_millis = UINT32_MAX - 1000; gDebugRfPollMs = g_millis;
        g_millis += 29999; serviceDebugRf();
        CHECK(logCount("GPS RF:") == 2, "RF cadence does not fire early across millis rollover");
        ++g_millis; serviceDebugRf();
        CHECK(logCount("GPS RF:") == 3 && logCount("blocks 2") == 1,
              "RF cadence survives rollover and reports an unexpected block count");
#else
        g_millis += 60000; loop();
        CHECK(GPSSerial.tx.empty() && !logCount("GPS RF:"), "DISPLAY_DEBUG 0 sends and logs no RF diagnostics");
#endif
    }

    printf("G20 NAV-PVT decoding, framing and privacy\n");
    {
        resetWorld();
        const auto packet = navPvt(123456, 456, 12345, 4, 1, -1234);
        GPSSerial.rx = packet.substr(0, 48); loop();
        bool erased = true;
        for (size_t i = 24; i < 32; ++i) erased &= gGpsUbxRx.payload[i] == 0;
        CHECK(!gNavPvt.seen && erased, "partial PVT is not published and its position bytes are never retained");
        g_millis += 50;
        GPSSerial.rx += packet.substr(48, packet.size() - 49); loop();
        CHECK(!gNavPvt.seen, "split packet waits for both checksum bytes");
        GPSSerial.rx += packet.back(); loop();
        CHECK(gNavPvt.seen && gNavPvt.iTow == 123456 && gNavPvt.valid == 7 &&
              gNavPvt.fixType == 4 && gNavPvt.flags == 1 && gNavPvt.numSv == 12 &&
              gNavPvt.hAccMm == 12345 && gNavPvt.gSpeedMmps == -1234 && gNavPvt.sAccMmps == 456,
              "92-byte PVT decodes verified little-endian offsets, units and signed ground speed");
        auto received = gNavPvt.receivedMs;
        auto bad = navPvt(999, 999); bad.back() ^= 1;
        auto badA = navPvt(999, 999); badA[badA.size() - 2] ^= 1;
        GPSSerial.rx += bad + badA;
        for (size_t len : {size_t(0), size_t(91), size_t(93), size_t(256)})
            GPSSerial.rx += frame(0x01, 0x07, std::vector<uint8_t>(len, 0));
        g_millis += 100; loop();
        CHECK(gNavPvt.iTow == 123456 && gNavPvt.receivedMs == received,
              "bad checksum or wrong payload length cannot replace or refresh PVT");
        GPSSerial.rx += packet.substr(0, 40); loop();
        g_millis += 501; run(15, 1);
        CHECK(gState == IDLE && gps.passedChecksum() > 0,
              "truncated PVT does not swallow NMEA or replace the previous excessive hAcc");
        CHECK(logCount("QSRT") == 0 && logCount("abcd") == 0 &&
              logCount("37.000") == 0 && logCount("-122") == 0 && logCount("lat=") == 0 && logCount("lon=") == 0,
              "serial output never contains NAV-PVT sentinels or NMEA coordinates");
    }

    printf("G21 trust thresholds, fix flags, freshness and rollover\n");
    {
        resetWorld(); GPSSerial.rx = navPvt(0, 500); loop();
        CHECK(gpsSpeedTrusted(true) && gpsSpeedTrusted(false), "arming accepts exactly 0.5 m/s sAcc");
        GPSSerial.rx += navPvt(1000, 501); loop();
        CHECK(!gpsSpeedTrusted(true) && gpsSpeedTrusted(false), "0.501 m/s fails arming but passes the trip threshold");
        GPSSerial.rx += navPvt(2000, 1000); loop();
        CHECK(gpsSpeedTrusted(false), "trip accepts exactly 1.0 m/s sAcc");
        GPSSerial.rx += navPvt(3000, 1001); loop();
        CHECK(!gpsSpeedTrusted(false), "trip rejects sAcc above 1.0 m/s");
        for (uint8_t type : {0, 1, 2, 3, 4, 5}) {
            GPSSerial.rx += navPvt(4000, 100, 1000, type); loop();
            CHECK(gpsSpeedTrusted(true) == (type == 3 || type == 4), "only 3D or combined fixes are trusted");
        }
        GPSSerial.rx += navPvt(5000, 100, 1000, 3, 0); loop();
        CHECK(!gpsSpeedTrusted(true), "gnssFixOK is required even for 3D with small sAcc");
        g_millis = UINT32_MAX - 1000;
        GPSSerial.rx += navPvt(6000, 100); loop();
        auto received = gNavPvt.receivedMs;
        g_millis = received + NAV_PVT_FRESH_MS;
        CHECK(gpsSpeedTrusted(true), "1500 ms arrival window includes its boundary across millis wrap");
        ++g_millis;
        CHECK(!gpsSpeedTrusted(true) && !gSpeedFallback, "1501 ms is stale; it cannot silently trust RMC yet");
        g_millis = received + NAV_PVT_FALLBACK_MS; serviceGpsSpeedFallback();
        CHECK(!gSpeedFallback, "fallback waits for more than ten seconds across millis wrap");
        ++g_millis; serviceGpsSpeedFallback();
        CHECK(gSpeedFallback && gpsSpeedTrusted(true), "fallback starts after the ten-second deadline across wrap");
    }

    printf("G22 indoor noise cannot arm; a reliable drive records normally\n");
    {
        resetWorld(); gNavPvtWaitMs = 0;
        run(4.65f, 40, true, false, 2500); // 10.4 mph: bench's worst false speed
        CHECK(gState == IDLE && !gArmCount && tripFiles().empty(), "sustained indoor-like false speed never enters the arming buffer");
        run(15, 5, true, false, 500);
        CHECK(gState == ARMING && gArmCount == 5, "reliable speeds start the normal arming window");
        run(15, 1, true, false, 501);
        CHECK(gState == IDLE && !gArmCount, "untrusted arming fix resets the whole window");
        run(15, 60, true, false, 500); run(0, 200, true, false, 1000);
        auto files = tripFiles();
        CHECK(files.size() == 1 && gState == IDLE && gTripMeters > 850 && gTripMeters < 910,
              "small-sAcc drive arms, accumulates its usual distance and finalizes");
        if (files.size() == 1) CHECK(trkpts(g_disk.files[files[0]]).size() == 241,
                                    "all 60 moving and 181 trusted cooldown points are preserved");
        CHECK(logCount("fallback") == 0, "continuous PVT prevents fallback throughout the drive");
        dump("G22");
    }

    printf("G23 untrusted mid-trip fixes end via cooldown without recording noise\n");
    {
        resetWorld(); run(15, 60, true, false, 100);
        const auto path = tripFiles()[0];
        const double meters = gTripMeters;
        const auto lastAccepted = gLastFix.monoMs;
        run(4.65f, 1, true, false, 2500);
        CHECK(gState == COOLDOWN && trkpts(g_disk.files[path]).size() == 60 && gTripMeters == meters,
              "first untrusted moving fix enters cooldown without writing or counting it");
        run(4.65f, 179, true, false, 2500);
        CHECK(gState == COOLDOWN && gTripMeters == meters && gLastFix.monoMs == lastAccepted &&
              gLastFixMonoMs > lastAccepted,
              "rejected fixes advance fix receipt time but never the last accepted position");
        run(4.65f, 1, true, false, 2500);
        CHECK(gState == IDLE && trkpts(g_disk.files[path]).size() == 60 &&
              g_disk.files[path].find("</gpx>") != std::string::npos,
              "three-minute untrusted stretch finalizes the trip through normal cooldown");
        dump("G23");
    }

    printf("G24 rejected stretches preserve segment gaps and break distance continuity\n");
    {
        resetWorld(); run(15, 60, true, false, 100);
        double meters = gTripMeters;
        run(20, 2, true, false, 2500); run(15, 3, true, false, 100);
        CHECK(gState == ACTIVE && gTripMeters == meters,
              "three fixes confirm resumption without bridging the rejected stretch or cooldown");
        run(20, 7, true, false, 2500); run(15, 4, true, false, 100); endTrip(false);
        const auto& d = g_disk.files.begin()->second;
        size_t split = d.find("</trkseg><trkseg>");
        CHECK(trkpts(d).size() == 67 && split != std::string::npos &&
              trkpts(d.substr(0, split)).size() == 63 &&
              d.find("</trkseg><trkseg>", split + 1) == std::string::npos,
              "only trusted points enter GPX and the first one after >5 s skipped starts a segment");
        CHECK(gTripMeters > meters + 14 && gTripMeters < meters + 16,
              "only the consecutive trusted step after resumption adds distance");
        dump("G24");
    }

    printf("G25 disappearing fixes retain the five-minute watchdog\n");
    {
        resetWorld(); run(15, 60, true, false, 100);
        run(4.65f, 1, true, false, 2500);
        const uint32_t last = gLastFixMonoMs;
        // The serial stub advances millis once on the final available().
        g_millis = last + FIX_LOSS_END_MS - 1; loop();
        CHECK(gState == COOLDOWN, "watchdog measures from the last RMC, including a rejected speed");
        ++g_millis; loop();
        CHECK(gState == IDLE && logCount("No GPS fix for 5 min") == 1,
              "complete fix loss still finalizes with no further GPS bytes");
    }

    printf("G26 fallback transitions and old RMC-only behavior\n");
    {
        resetWorld(); g_millis = 5000; ackGps(); configureGps();
        GPSSerial.rx = nmea("GNTXT,01,01,02,ready"); gpsLoops();
        CHECK(gGpsConfig.state == GPS_CONFIG_DONE && gNavPvtWaitMs >= 5000 && gNavPvtWaitMs <= g_millis,
              "successful config records a nonzero PVT wait start");
        g_millis = gNavPvtWaitMs + NAV_PVT_FALLBACK_MS;
        serviceGpsSpeedFallback();
        CHECK(!gSpeedFallback && !gpsSpeedTrusted(true) && !logCount("fallback"),
              "successful config gives NAV-PVT the full ten seconds after config end");
        ++g_millis; serviceGpsSpeedFallback(); gpsLoops();
        CHECK(gSpeedFallback && logCount("WARNING: GPS NAV-PVT unavailable") == 1,
              "missing NAV-PVT logs exactly one warning on fallback entry");
        gT = 16; run(15, 60); run(0, 200);
        auto files = tripFiles();
        CHECK(files.size() == 1 && trkpts(g_disk.files[files[0]]).size() == 241 && gState == IDLE &&
              logCount("WARNING: GPS NAV-PVT unavailable") == 1,
              "RMC-only fallback preserves the legacy trip and every point without warning spam");
        GPSSerial.rx += navPvt(999, 2500); loop(); gpsLoops();
        CHECK(!gSpeedFallback && !gpsSpeedTrusted(true) && logCount("NAV-PVT returned") == 1,
              "PVT return logs once and immediately restores gating, even for an inaccurate fix");
        g_millis = gNavPvt.receivedMs + NAV_PVT_FALLBACK_MS + 1; loop(); gpsLoops();
        CHECK(gSpeedFallback && logCount("WARNING: GPS NAV-PVT unavailable") == 2,
              "another outage emits one new warning for the new transition");
        dump("G26");
    }

    printf("G27 PVT after RMC and stale-but-not-fallback samples\n");
    {
        resetWorld(); gNavPvtWaitMs = 0;
        run(15, 60, true, false, 100, true);
        CHECK(gState == ACTIVE && gTripMeters > 850,
              "PVT after RMC supports normal arming using the bounded previous-epoch sample");
        size_t count = trkpts(g_disk.files.begin()->second).size();
        g_millis = gNavPvt.receivedMs + NAV_PVT_FRESH_MS + 1;
        GPSSerial.rx += sentences({61, gLat, gLon, 15, true})[0]; loop();
        CHECK(gState == COOLDOWN && trkpts(g_disk.files.begin()->second).size() == count,
              "stale accuracy skips the whole point and treats speed as zero before fallback");
    }

    printf("G28 NAV-PVT config uses the same NAK and timeout retry machinery\n");
    for (bool nak : {false, true}) {
        resetWorld(); configureGps();
        respondToGps([nak](uint8_t cls, uint8_t id) {
            if (gGpsConfig.command == 5 && gGpsConfig.attempts == 1)
                return nak ? ack(cls, id, false) : std::string();
            return ack(cls, id);
        });
        GPSSerial.rx = nmea("GNTXT,01,01,02,ready"); gpsLoops();
        if (!nak) {
            for (int i = 0; i < 3; ++i) { g_millis = gGpsConfig.since + GPS_ACK_MS; loop(); }
        }
        CHECK(gGpsConfig.state == GPS_RETRY && GPSSerial.tx.size() == 88 + (nak ? 11 : 33),
              "PVT NAK stops the attempt; missing ACK retries PVT only up to the existing send limit");
        g_millis = gGpsConfig.since + GPS_CONFIG_RETRY_MS; gpsLoops();
        CHECK(gGpsConfig.command == 6 && gGpsConfig.attempts == 2 && gGpsConfig.state == GPS_CONFIG_DONE,
              "next complete attempt reconfigures and ACKs NAV-PVT successfully");
    }

#if DISPLAY_DEBUG
    printf("G29 coordinate-free accuracy diagnostics and rejected speed marker\n");
    {
        resetWorld(); gGpsConfig.state = GPS_CONFIG_DONE;
        run(4.65f, 31, true, false, 2500, false, 3, 1, 12345);
        CHECK(logCount("sAcc 2.50 m/s hAcc 12.3 m") == 1 && logCount("37.000") == 0 && logCount("-122") == 0,
              "30 s GPS RF line includes only speed/horizontal accuracy, never position");
        DebugStatus s; s.mph = 3.3; s.speedTrusted = gLatestSpeedTrusted;
        char lines[8][22]; formatDebugStatus(s, lines);
        CHECK(!strcmp(lines[3], "Speed 3.3? mph"), "untrusted OLED speed has a question mark");
        s.mph = 1e9; formatDebugStatus(s, lines);
        CHECK(!strcmp(lines[3], "Speed 999.9? mph"), "worst-case untrusted speed remains bounded");
        for (const auto& line : lines) CHECK(strlen(line) <= 21, "every OLED line fits 21 characters");
        g_millis = gDebugRfPollMs + 30000; loop();
        CHECK(logCount("sAcc -- m/s hAcc -- m") == 1, "missing/stale PVT reports unknown accuracy");
    }
#endif

    printf("G30 horizontal accuracy boundaries and indoor noise\n");
    {
        resetWorld(); gNavPvtWaitMs = 0;
        for (uint32_t hAcc : {7500u, 9900u, 11400u, 21000u, 21400u})
            run(4.65f, 20, true, false, 290, false, 3, 1, hAcc);
        CHECK(gState == IDLE && !gArmCount && tripFiles().empty(),
              "indoor hAcc cannot arm despite good sAcc and sustained false speeds above 5 mph");
        GPSSerial.rx += navPvt(0, 500, 5000); loop();
        CHECK(gpsSpeedTrusted(true), "arming accepts exactly 5.0 m hAcc and 0.5 m/s sAcc");
        GPSSerial.rx += navPvt(0, 100, 5001); loop();
        CHECK(!gpsSpeedTrusted(true) && gpsSpeedTrusted(false), "5.001 m fails only the arming hAcc gate");
        GPSSerial.rx += navPvt(0, 1000, 10000); loop();
        CHECK(gpsSpeedTrusted(false), "trip accepts exactly 10.0 m hAcc and 1.0 m/s sAcc");
        GPSSerial.rx += navPvt(0, 100, 10001); loop();
        CHECK(!gpsSpeedTrusted(false), "trip rejects 10.001 m hAcc even with good sAcc");
        run(15, 5, true, false, 100);
        CHECK(gState == ARMING, "open-sky hAcc starts arming");
        run(15, 1, true, false, 100, false, 3, 1, 5001);
        CHECK(gState == IDLE && !gArmCount, "hAcc rejection resets an existing arming window");
    }

    printf("G31 garage noise cannot extend cooldown or add distance/duration\n");
    {
        resetWorld(); run(15, 60, true, false, 100);
        const auto path = std::string(gTripPath);
        const auto lastMove = gLastMoveMs;
        const double meters = gTripMeters;
        run(0, 1, true, false, 100);
        const auto coolT0 = gCoolT0;
        // Two trusted noisy fixes every ten seconds never confirm movement.
        for (int i = 0; i < 18; ++i) {
            run(1.4f, 2, true, false, 400, false, 3, 1, 9000);
            run(0, 8, true, false, 400, false, 3, 1, 9000);
        }
        CHECK(gState == IDLE && gLastFixMonoMs - coolT0 == COOL_MS &&
              gCoolT0 == coolT0 && gTripMeters == meters && gLastMoveMs == lastMove,
              "garage noise finalizes at the original three-minute deadline with unchanged distance/duration");
        CHECK(trkpts(g_disk.files[path]).size() == 241 && g_disk.files[path].find("</gpx>") != std::string::npos,
              "all trusted cooldown fixes including noisy ones are still written");
        dump("G31");
    }

    printf("G32 exactly three consecutive moving fixes resume with a fresh baseline\n");
    {
        resetWorld(); run(15, 60, true, false, 100); run(0, 1, true, false, 100);
        const auto coolT0 = gCoolT0, lastMove = gLastMoveMs;
        const double meters = gTripMeters;
        run(15, 2, true, false, 100);
        CHECK(gState == COOLDOWN && gResumeFixes == 2 && gTripMeters == meters && gLastMoveMs == lastMove,
              "two moving fixes leave cooldown, distance and duration unchanged");
        run(15, 1, true, false, 100, false, 3, 1, 10001);
        CHECK(gResumeFixes == 0 && gCoolT0 == coolT0, "untrusted moving fix resets count but never deadline");
        run(15, 2, true, false, 100); run(0, 1, true, false, 100);
        CHECK(gResumeFixes == 0 && gCoolT0 == coolT0, "trusted stopped fix also resets count");
        run(15, 2, true, false, 100);
        CHECK(gState == COOLDOWN, "a new pair is still insufficient");
        run(15, 1, true, false, 100);
        CHECK(gState == ACTIVE && gResumeFixes == RESUME_FIXES && gTripMeters == meters &&
              gLastMoveMs == gLastFixMonoMs, "exactly the third fix resumes and sets last moving time without a distance bridge");
        run(15, 1, true, false, 100);
        CHECK(gTripMeters > meters + 14 && gTripMeters < meters + 16,
              "distance resumes only on the step after the confirming fix");
        endTrip(false); dump("G32");
    }

    printf("G33 RMC epochs survive variable processing delays and clock drift\n");
    {
        resetWorld(); run(15, 12);
        g_epochBaseMs += 20000; // drift, with GPS clock sync still rate limited
        const uint32_t arrivals[] = {12000, 14050, 14100, 15000, 17050, 17100};
        for (int i = 0; i < 6; ++i) {
            auto ss = sentences({12 + i, gLat, gLon, 15, true});
            // A GSV burst ahead of RMC models delayed processing at 9600 baud.
            g_millis = arrivals[i];
            GPSSerial.rx += ss[4] + ss[5] + ss[6] + ss[0]; loop();
        }
        endTrip(false);
        auto pts = trkpts(g_disk.files.begin()->second);
        bool exact = pts.size() == 18;
        for (size_t i = 0; i < pts.size(); ++i) {
            char want[32]; snprintf(want, sizeof want, "2026-04-14T14:30:%02uZ", unsigned(i));
            exact &= field(pts[i], "time") == want;
        }
        CHECK(exact, "every 1 Hz point has exact 1 s epoch steps despite a delayed GSV burst and RTC drift");
        dump("G33");
    }

    printf("G34 same-sentence dates, midnight rollover and fractional formatting\n");
    for (const char* talker : {"GNRMC", "GPRMC"}) {
        resetWorld(); run(15, 12);
        g_millis += 1000; GPSSerial.rx += rmc("235959.99", "140426", talker); loop();
        // A new GGA time must not mix with the previous RMC date.
        g_millis += 1000;
        GPSSerial.rx += nmea("GNGGA,000000.55," + coord(gLat, true) + "," + coord(gLon, false) +
                            ",1,09,0.90,12.5,M,-30.0,M,,"); loop();
        GPSSerial.rx += rmc("000000.00", "150426", talker); loop();
        for (const char* t : {"000001.01", "000002.10", "000003.25", "000004.99", "000005"}) {
            g_millis += 1000; GPSSerial.rx += rmc(t, "150426", talker); loop();
        }
        endTrip(false);
        auto pts = trkpts(g_disk.files.begin()->second);
        const char* expected[] = {"2026-04-14T23:59:59.990Z", "2026-04-15T00:00:00Z",
            "2026-04-15T00:00:01.010Z", "2026-04-15T00:00:02.100Z",
            "2026-04-15T00:00:03.250Z", "2026-04-15T00:00:04.990Z", "2026-04-15T00:00:05Z"};
        bool exact = pts.size() == 19;
        for (size_t i = 0; i < 7 && 12 + i < pts.size(); ++i)
            exact &= field(pts[12 + i], "time") == expected[i];
        CHECK(exact, "GN/GP RMC uses its matching date, whole seconds or three fractional digits including leading zeros");
        dump(talker);
    }

    printf("G35 invalid RMC date/time stays untimed even with a synchronized clock\n");
    for (const char* talker : {"GNRMC", "GPRMC"}) {
        resetWorld(); run(15, 12);
        const std::pair<const char*, const char*> invalid[] = {
            {"143012.00", ""}, {"", "140426"}, {"", ""}, {"bad", "140426"},
            {"250000.00", "140426"}, {"143015.00", "000026"},
            {"143016.", "140426"}, {"143016.xy", "140426"}};
        for (const auto& fields : invalid) {
            g_millis += 1000;
            GPSSerial.rx += rmc(fields.first, fields.second, talker); loop();
            CHECK(gClockSet && !gLastFix.hasUtc, "cached date/time or system UTC cannot supply an invalid RMC epoch");
        }
        // Flushing on the next valid epoch must not retroactively time them.
        g_millis += 1000; GPSSerial.rx += rmc("143020.50", "140426", talker); loop();
        g_millis += 1000; GPSSerial.rx += rmc("", "", talker); loop();
        endTrip(false); // flush the remaining untimed tail with system UTC set
        auto pts = trkpts(g_disk.files.begin()->second);
        bool exact = pts.size() == 22;
        for (size_t i = 12; i < pts.size(); ++i)
            exact &= field(pts[i], "time") == (i == 20 ? "2026-04-14T14:30:20.500Z" : "");
        CHECK(exact, "both timed-fix flush and trip-end flush preserve missing times and point order");
        dump((std::string("G35_") + talker).c_str());
    }

    printf(g_fail ? "\n%d FAILED\n" : "\nALL PASS\n", g_fail);
    return g_fail ? 1 : 0;
}
