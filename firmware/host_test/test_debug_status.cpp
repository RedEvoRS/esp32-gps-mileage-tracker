// Real TinyGPSPlus GSV parsing plus hardware-independent screen formatting.
#include "arduino_stub.h"
#include <TinyGPSPlus.h>
#include "debug_status.h"
#include "debug_gsv.h"
#include "debug_rf.h"
#include <cassert>
#include <climits>

uint32_t g_millis = 0;

static void feed(TinyGPSPlus& gps, DebugGsv& gsv, const std::string& body, bool corrupt = false) {
    uint8_t sum = 0;
    for (char c : body) sum ^= c;
    char tail[8];
    snprintf(tail, sizeof tail, "*%02X\r\n", corrupt ? sum ^ 1 : sum);
    for (char c : "$" + body + tail) {
        gsv.encode(c);
        if (gps.encode(c)) gsv.update(g_millis);
    }
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
    DebugGsv gsv;
    assert(gsv.signal(0).heard == -1);
    feed(gps, gsv, "GPGSV,2,1,05,01,40,083,47,02,17,308,,03,07,344,00,04,22,228,29,1");
    assert(gsv.signal(0).heard == -1);  // publish only complete groups
    feed(gps, gsv, "GPGSV,2,2,05,05,09,028,30,1");
    assert(gsv.signal(0).heard == 3);   // empty and zero C/N0 excluded
    assert(gsv.signal(0).strong == 2 && gsv.signal(0).maxCno == 47);
    // One satellite repeated on another signal: count once, strongest wins.
    feed(gps, gsv, "GPGSV,1,1,02,04,22,228,40,06,11,152,20,5");
    assert(gsv.signal(0).heard == 4 && gsv.signal(0).strong == 3);
    feed(gps, gsv, "GPGSV,1,1,01,06,11,152,20,5");
    assert(gsv.signal(0).heard == 4 && gsv.signal(0).strong == 2);
    feed(gps, gsv, "GLGSV,1,1,02,65,40,083,31,66,17,308,,1");
    feed(gps, gsv, "GAGSV,1,1,03,01,40,083,32,02,17,308,,03,10,123,,7");
    feed(gps, gsv, "GBGSV,1,1,01,201,40,083,33,1");
    feed(gps, gsv, "GQGSV,1,1,01,193,40,083,34,1");
    assert(gsv.signal(0).heard == 8);
    feed(gps, gsv, "BDGSV,1,1,01,201,40,083,33,1");
    assert(gsv.signal(0).heard == 8);  // BD aliases GB, no double counting
    feed(gps, gsv, "GPGSV,1,1,00,1", true);
    assert(gsv.signal(0).heard == 8);  // checksum failure preserves snapshots
    feed(gps, gsv, "GPGSV,1,1,");
    feed(gps, gsv, "GPGSV,1,1,xx");
    feed(gps, gsv, "GPGSV,1,1,100");
    feed(gps, gsv, "GPGSV,2,2,05,05,09,028,30,1"); // out of sequence
    assert(gsv.signal(0).heard == 8);
    g_millis = 10000;
    feed(gps, gsv, "GAGSV,1,1,00,7");
    assert(gsv.signal(15000).heard == 7);
    assert(gsv.signal(15001).heard == 0);  // talkers/signals expire independently
    assert(gsv.signal(15001).strong == 0 && gsv.signal(15001).maxCno == 0);
    assert(gsv.signal(25001).heard == -1);
    // Isolate rollover from the earlier observations at timestamp zero.
    TinyGPSPlus rolloverGps;
    DebugGsv rolloverGsv;
    g_millis = UINT32_MAX - 1000;
    feed(rolloverGps, rolloverGsv, "GPGSV,1,1,01,01,40,083,47"); // legacy, no signal ID
    assert(rolloverGsv.signal(1000).heard == 1);   // millis rollover
    assert(rolloverGsv.signal(13999).heard == 1);  // exactly 15000 ms old
    assert(rolloverGsv.signal(14000).heard == -1); // expires after 15000 ms

    // Partial last messages with one, two, three or four satellite blocks.
    for (unsigned blocks = 1; blocks <= 4; ++blocks) {
        DebugGsv partial;
        std::string body = "GPGSV,1,1,0" + std::to_string(blocks);
        for (unsigned i = 1; i <= blocks; ++i)
            body += ",0" + std::to_string(i) + ",40,083,31";
        feed(gps, partial, body + ",1");
        assert(partial.signal(g_millis).heard == int(blocks));
    }

    DebugStatus s;
    s.state = "ACTIVE"; s.now = 754000; s.heard = 19; s.used = 12; s.agcPct = 31;
    s.fix = true; s.hdop = 0.9f; s.fixAgeMs = 300; s.mph = 42.3f;
    s.trip = true; s.tripMs = 720000; s.tripMiles = 8.4;
    s.store = "SD"; s.net.pending = 3; s.net.uploadCode = 200; s.net.uploadMs = 734000;
    s.batteryPct = 85; s.batteryV = 4.02f; s.usb = true; s.charging = true;
    char lines[8][22];
    formatDebugStatus(s, lines);
    const char* expected[] = {"ACTIVE 0h12m", "SATS R:19 U:12 A31%", "FIX OK H0.9 0s",
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
                s.heard = s.used = s.net.pending = INT_MAX;
                s.hdop = s.mph = s.batteryV = 1e9f; s.tripMiles = 1e9;
                for (int agc : {-1, 0, 99, 100, INT_MAX}) {
                    s.agcPct = agc;
                    bounded(s);
                    formatDebugStatus(s, lines);
                    assert(lines[1][strlen(lines[1]) - 1] == '%');
                }
            }
        }
    }
    s.hdop = s.mph = s.batteryV = NAN; s.tripMiles = NAN;
    bounded(s);
    s.heard = s.used = 999; s.agcPct = 100;
    formatDebugStatus(s, lines);
    assert(!strcmp(lines[1], "SATS R:999 U:999A100%"));
    formatDebugStatus(DebugStatus{}, lines);
    assert(!strcmp(lines[1], "SATS R:-- U:-- A--%"));

    uint8_t payload[52] = {};
    payload[1] = 1;
    payload[5] = 0xFD; payload[6] = 1; payload[7] = 2;
    payload[16] = 85; payload[18] = 0; payload[19] = 10; payload[20] = 12;
    DebugRf rf;
    assert(parseDebugMonRf(payload, 28, rf));
    assert(rf.valid && rf.blocks == 1 && rf.jam == 1 && rf.noise == 85 &&
           rf.agcPct == 31 && rf.cw == 12 && rf.antenna == 1 && rf.power == 2);
    char serial[DEBUG_RF_LINE_BYTES];
    DebugSignal signal; signal.heard = 19; signal.strong = 12; signal.maxCno = 47;
    formatDebugRf(rf, signal, 12, 0.8f, 'A', serial, sizeof serial);
    assert(!strcmp(serial, "GPS RF: jam ok cw 12 noise 85 agc 31% ant DONTKNOW pwr DONTKNOW | heard 19 (>=30: 12) max 47 | used 12 hdop 0.8 fix A"));
    puts(serial);
    for (size_t len : {size_t(0), size_t(3), size_t(4), size_t(27), size_t(29), size_t(52)})
        assert(!parseDebugMonRf(payload, len, rf));
    payload[1] = 0; assert(!parseDebugMonRf(payload, 4, rf));
    payload[1] = 1; payload[0] = 1; assert(!parseDebugMonRf(payload, 28, rf));
    payload[0] = 0;
    assert(rf.valid && rf.cw == 12);  // invalid replies cannot replace a good one
    const char* jams[] = {"jam unknown", "jam ok", "jam warning", "jam critical"};
    const char* antennas[] = {"ant INIT", "ant DONTKNOW", "ant OK", "ant SHORT", "ant OPEN", "ant ?"};
    const char* powers[] = {"pwr OFF", "pwr ON", "pwr DONTKNOW", "pwr ?"};
    for (unsigned j = 0; j < 4; ++j) {
        for (unsigned a = 0; a < 6; ++a) {
            for (unsigned p = 0; p < 4; ++p) {
                payload[5] = 0xFC | j; payload[6] = a; payload[7] = p;
                assert(parseDebugMonRf(payload, 28, rf));
                formatDebugRf(rf, signal, 12, 0.8f, 'V', serial, sizeof serial);
                assert(strstr(serial, jams[j]) && strstr(serial, antennas[a]) && strstr(serial, powers[p]));
                assert(strstr(serial, "fix V"));
            }
        }
    }
    payload[1] = 2; payload[5] = 3; payload[6] = 1; payload[7] = 2;
    payload[16] = payload[17] = payload[18] = payload[19] = payload[20] = 255;
    assert(parseDebugMonRf(payload, sizeof payload, rf));
    assert(rf.blocks == 2 && rf.noise == 65535 && rf.agcPct == 100 && rf.cw == 255);
    signal.heard = signal.strong = signal.maxCno = INT_MAX;
    rf.blocks = 255; // worst-case field widths, even beyond the receive bound
    formatDebugRf(rf, signal, INT_MAX, 1e9f, 0, serial, sizeof serial);
    const char* worst = "GPS RF: jam critical cw 255 noise 65535 agc 100% ant DONTKNOW pwr DONTKNOW blocks 255 | heard 999 (>=30: 999) max 999 | used 999 hdop 99.9 fix --";
    assert(!strcmp(serial, worst));
    assert(strlen(serial) < sizeof serial);
    for (char c : std::string(serial)) assert(c >= 32 && c <= 126);
    formatDebugRf(DebugRf{}, DebugSignal{}, -1, NAN, 0, serial, sizeof serial);
    assert(!strcmp(serial, "GPS RF: rf -- | heard -- (>=30: --) max -- | used -- hdop -- fix --"));
    puts("Debug status/GSV tests passed.");
}
