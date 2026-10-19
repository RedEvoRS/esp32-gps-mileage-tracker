// Real TinyGPSPlus GSV parsing plus hardware-independent screen formatting.
#include "arduino_stub.h"
#include "debug_status.h"
#include "debug_gsv.h"
#include <cassert>
#include <climits>

uint32_t g_millis = 0;

static void feed(TinyGPSPlus& gps, DebugGsv& gsv, const std::string& body, bool corrupt = false) {
    uint8_t sum = 0;
    for (char c : body) sum ^= c;
    char tail[8];
    snprintf(tail, sizeof tail, "*%02X\r\n", corrupt ? sum ^ 1 : sum);
    for (char c : "$" + body + tail)
        if (gps.encode(c)) gsv.update(g_millis);
}

static void bounded(const DebugStatus& s) {
    char lines[8][22];
    memset(lines, 0x7f, sizeof lines);
    formatDebugStatus(s, lines);
    for (const auto& line : lines) {
        assert(strlen(line) <= 21);
        for (const char* p = line; *p; ++p) assert(*p >= 32 && *p <= 126);
    }
}

int main() {
    TinyGPSPlus gps;
    DebugGsv gsv(gps);
    assert(gsv.inView(0) == -1);
    feed(gps, gsv, "GPGSV,3,1,12");
    assert(gsv.inView(0) == 12);  // timestamp zero is a valid observation
    feed(gps, gsv, "GPGSV,3,2,12");
    feed(gps, gsv, "GPGSV,3,3,12");
    assert(gsv.inView(0) == 12);  // multipart total is counted only once
    feed(gps, gsv, "GLGSV,2,1,08");
    feed(gps, gsv, "GAGSV,2,1,06");
    feed(gps, gsv, "GBGSV,2,1,09");
    feed(gps, gsv, "GQGSV,1,1,03");
    assert(gsv.inView(0) == 38);
    feed(gps, gsv, "BDGSV,2,1,09");
    assert(gsv.inView(0) == 38);  // BD aliases GB, no double counting
    feed(gps, gsv, "GPGSV,1,1,02", true);
    assert(gsv.inView(0) == 38);  // bad checksums cannot replace good counts
    feed(gps, gsv, "GPGSV,1,1,");
    feed(gps, gsv, "GPGSV,1,1,xx");
    feed(gps, gsv, "GPGSV,1,1,100");
    assert(gsv.inView(0) == 38);
    g_millis = 10000;
    feed(gps, gsv, "GAGSV,1,1,00");
    assert(gsv.inView(15000) == 32);
    assert(gsv.inView(15001) == 0);  // other talkers expired independently
    assert(gsv.inView(25001) == -1);
    // Isolate rollover from the earlier observations at timestamp zero.
    TinyGPSPlus rolloverGps;
    DebugGsv rolloverGsv(rolloverGps);
    g_millis = UINT32_MAX - 1000;
    feed(rolloverGps, rolloverGsv, "GPGSV,1,1,04");
    assert(rolloverGsv.inView(1000) == 4);   // millis rollover
    assert(rolloverGsv.inView(13999) == 4);  // exactly 15000 ms old
    assert(rolloverGsv.inView(14000) == -1); // expires after 15000 ms

    DebugStatus s;
    s.state = "ACTIVE"; s.now = 754000; s.inView = 32; s.used = 12;
    s.fix = true; s.hdop = 0.9f; s.fixAgeMs = 300; s.mph = 42.3f;
    s.trip = true; s.tripMs = 720000; s.tripMiles = 8.4;
    s.store = "SD"; s.net.pending = 3; s.net.uploadCode = 200; s.net.uploadMs = 734000;
    s.batteryPct = 85; s.batteryV = 4.02f; s.usb = true; s.charging = true;
    char lines[8][22];
    formatDebugStatus(s, lines);
    const char* expected[] = {"ACTIVE 0h12m", "SATS V:32 U:12", "FIX OK H0.9 0s",
        "Speed 42.3 mph", "Trip 12m 008.4mi", "Store SD Q:3", "Net OFF OK 20s",
        "B85% 4.02V U1 C1"};
    for (unsigned i = 0; i < 8; ++i) {
        assert(strcmp(lines[i], expected[i]) == 0);
        puts(lines[i]);
    }
    bounded(s);
    bounded(DebugStatus{});
    for (const char* state : {"IDLE", "ARMING", "ACTIVE", "COOLDOWN", "FINALIZING"}) {
        for (DebugWifi wifi : {DebugWifi::Off, DebugWifi::Scan, DebugWifi::Join, DebugWifi::On}) {
            for (int code : {0, -1, 200, 400, 503, INT_MAX}) {
                s.state = state; s.net.wifi = wifi; s.net.uploadCode = code;
                s.now = s.tripMs = UINT32_MAX; s.fixAgeMs = UINT32_MAX - 1;
                s.inView = s.used = s.net.pending = INT_MAX;
                s.hdop = s.mph = s.batteryV = 1e9f; s.tripMiles = 1e9;
                bounded(s);
            }
        }
    }
    s.hdop = s.mph = s.batteryV = NAN; s.tripMiles = NAN;
    bounded(s);
    puts("Debug status/GSV tests passed.");
}
