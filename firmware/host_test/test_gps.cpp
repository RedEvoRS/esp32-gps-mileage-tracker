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

// Deliver each sentence as the UART would: ~70 ms apart, loop() between.
static void run(float mps, int seconds, bool fix = true, bool ggaFirst = false) {
    for (int i = 0; i < seconds; ++i, ++gT) {
        if (fix) gLat += mps / 111320.0;
        auto ss = sentences({gT, gLat, gLon, mps, fix}, ggaFirst);
        for (size_t k = 0; k < ss.size(); ++k) {
            uint32_t at = (uint32_t)gT * 1000 + (uint32_t)k * 70;
            if (g_millis < at) g_millis = at;
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
    GPSSerial = HardwareSerial(1);
    gGpsConfig = GpsConfig(); gGpsUbxRx = GpsUbxRx();
    gState = IDLE; gTripPath[0] = 0; gTripWriteFailed = false;
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
        CHECK(gGpsConfig.state == GPS_CONFIG_DONE && gGpsConfig.command == 5,
              "all five messages are ACKed without needing a position fix");
        std::vector<uint8_t> nav5(36, 0); nav5[0] = 0x01; nav5[2] = 4;
        std::string want = frame(0x06, 0x01, { 0xF0, 0x01, 0 }) + frame(0x06, 0x01, { 0xF0, 0x02, 0 }) +
                           frame(0x06, 0x01, { 0xF0, 0x03, 0 }) + frame(0x06, 0x01, { 0xF0, 0x05, 0 }) +
                           frame(0x06, 0x24, nav5);
        CHECK(GPSSerial.tx == want, "sends exactly GLL/GSA/GSV/VTG off + NAV5 automotive");
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
        CHECK(gGpsConfig.command == 5 && gGpsConfig.attempts == 2 && GPSSerial.tx.size() == 2 * 88,
              "second attempt resends all five unchanged messages and succeeds");
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
        resetWorld(); run(15, 20);
        CHECK(gState == ACTIVE, "trip is recording before configuration is requested");
        configureGps();
        respondToGps([](uint8_t cls, uint8_t id) {
            return gGpsConfig.attempts > 1 ? ack(cls, id) : std::string();
        });
        g_delayHook = [](uint32_t) { CHECK(false, "configuration must not delay loop"); };
        run(15, 20);
        g_delayHook = nullptr;
        CHECK(gGpsConfig.command == 5 && gGpsConfig.attempts == 2,
              "no-answer first attempt succeeds on a later loop attempt");
        std::string first = frame(0x06, 0x01, {0xF0, 0x01, 0});
        CHECK(GPSSerial.tx.substr(0, first.size() * 3) == first + first + first &&
              GPSSerial.tx.size() == 3 * 11 + 88,
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
        GPSSerial.rx += ack(0x06, 0x01).substr(9) + rmc.substr(split);
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
        for (uint32_t ms = 0; ms <= 60000; ms += 100) { g_millis = ms; loop(); }
        CHECK(gGpsConfig.state == GPS_CONFIG_DONE && gGpsConfig.attempts == 5,
              "stops after five attempts, including a receiver that never emits valid NMEA");
        CHECK(GPSSerial.tx.size() == (response == 0 ? 0 : response == 1 ? 15 * 11 : 5 * 11),
              "only a ready receiver receives commands, each silence is limited to three sends");
        if (response) CHECK(lastSendMs - gGpsConfig.started >= 32000,
                            "even immediate NAKs keep attempts available at least 32 seconds after startup");
        size_t sent = GPSSerial.tx.size();
        g_millis += 60000; gpsLoops();
        CHECK(GPSSerial.tx.size() == sent && logCount("not configured after 5 attempts") == 1 &&
              logCount("retrying in background") == 1 && logCount("GPS configured") == 0,
              "stays stopped and logs pending/final failure only once");
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
        GPSSerial.rx = sentences({4, 37, -122, 15, true})[0]; loop();
        CHECK(gArmCount == 1 && GPSSerial.tx.empty(),
              "new fixes still reach the FSM while waiting for the scheduled retry");
        ackGps(); g_millis = retryAt; gpsLoops();
        CHECK(gGpsConfig.command == 5 && gGpsConfig.attempts == 2,
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
        CHECK(gGpsConfig.command == 5 && gGpsConfig.attempts == 2,
              "retry succeeds after unsigned deadline wraps to a small millis value");
    }

    printf("G18 receiver emits NMEA but ignores UBX for the first 30 seconds\n");
    {
        resetWorld(); configureGps();
        respondToGps([](uint8_t cls, uint8_t id) {
            return g_millis - gGpsConfig.started >= 30000 ? ack(cls, id) : std::string();
        });
        run(15, 45);
        CHECK(gGpsConfig.command == 5 && gGpsConfig.attempts == 5 &&
              gGpsConfig.since - gGpsConfig.started >= 32000,
              "last attempt configures a receiver that stays unresponsive well after valid NMEA starts");
        CHECK(logCount("configured on attempt 5 (") == 1 && logCount("ms after config start") == 1 &&
              logCount("retrying in background") == 1 && logCount("not configured after") == 0,
              "reports the successful late attempt and elapsed time once");
        endTrip(false);
        auto files = tripFiles();
        CHECK(files.size() == 1 && trkpts(g_disk.files[files[0]]).size() == 45,
              "all 45 fixes survive the full cold-start retry window");
    }

    printf(g_fail ? "\n%d FAILED\n" : "\nALL PASS\n", g_fail);
    return g_fail ? 1 : 0;
}
