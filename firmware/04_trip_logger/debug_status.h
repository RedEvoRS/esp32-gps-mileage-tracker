// Debug-page data/formatting only: no buses, files or cross-core access.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <math.h>
#include <string.h>

enum class DebugWifi : uint8_t { Off, Scan, Join, On };
struct DebugNetwork {
    DebugWifi wifi = DebugWifi::Off;
    int uploadCode = 0;               // 0 = never; -1 = local/transport failure
    uint32_t uploadMs = 0;
    int pending = -1;                 // -1 = not counted yet
};

struct DebugStatus {
    const char* state = "IDLE";
    uint32_t now = 0;
    int heard = -1, used = -1, agcPct = -1;
    bool fix = false;
    bool speedTrusted = true;
    float hdop = -1, mph = -1;
    uint32_t fixAgeMs = UINT32_MAX;
    bool trip = false;
    uint32_t tripMs = 0;
    double tripMiles = 0;
    const char* store = "NONE";
    DebugNetwork net;
    int batteryPct = -1;
    float batteryV = 0;
    bool usb = false, charging = false;
};

static inline unsigned debugBound(unsigned value, unsigned max) {
    return value < max ? value : max;
}

static inline void debugCount(int value, char* out, size_t n) {
    if (value < 0) snprintf(out, n, "--");
    else snprintf(out, n, "%u", debugBound((unsigned)value, 999));
}

// Eight printable ASCII lines, always <=21 characters (including worst cases).
// Unknown/stale measurements use -- rather than a misleading cached zero.
static inline void formatDebugStatus(const DebugStatus& s, char lines[8][22]) {
    unsigned up = s.now / 1000;
    snprintf(lines[0], 22, "%.10s %uh%02um", s.state, up / 3600, up / 60 % 60);
    char heard[4], used[4], agc[4], age[5], hdop[5];
    debugCount(s.heard, heard, sizeof heard);
    debugCount(s.used, used, sizeof used);
    debugCount(s.agcPct < 0 ? -1 : (int)debugBound(s.agcPct, 100), agc, sizeof agc);
    // Keep the percent sign even with two 999 counts and full-scale AGC.
    const char* gap = strlen(heard) + strlen(used) + strlen(agc) > 8 ? "" : " ";
    snprintf(lines[1], 22, "SATS R:%s U:%s%sA%s%%", heard, used, gap, agc);
    if (s.fixAgeMs == UINT32_MAX) snprintf(age, sizeof age, "--");
    else snprintf(age, sizeof age, "%u", debugBound(s.fixAgeMs / 1000, 999));
    if (!isfinite(s.hdop) || s.hdop < 0) snprintf(hdop, sizeof hdop, "--");
    else snprintf(hdop, sizeof hdop, "%.1f", s.hdop < 99.9f ? s.hdop : 99.9f);
    snprintf(lines[2], 22, "FIX %s H%s %ss", s.fix ? "OK" : "NO", hdop, age);
    if (!isfinite(s.mph) || s.mph < 0) snprintf(lines[3], 22, "Speed -- mph");
    else snprintf(lines[3], 22, "Speed %.1f%s mph", s.mph < 999.9f ? s.mph : 999.9f,
                  s.speedTrusted ? "" : "?");
    if (s.trip) {
        unsigned minutes = debugBound(s.tripMs / 60000, 999);
        double miles = isfinite(s.tripMiles) && s.tripMiles > 0 ? s.tripMiles : 0;
        snprintf(lines[4], 22, "Trip %um %05.1fmi", minutes, miles < 999.9 ? miles : 999.9);
    } else snprintf(lines[4], 22, "Trip --");
    char pending[4];
    debugCount(s.net.pending, pending, sizeof pending);
    snprintf(lines[5], 22, "Store %.5s Q:%s", s.store, pending);
    const char* wifi = s.net.wifi == DebugWifi::Scan ? "SCAN" :
                       s.net.wifi == DebugWifi::Join ? "JOIN" :
                       s.net.wifi == DebugWifi::On ? "ON" : "OFF";
    char result[5];
    if (!s.net.uploadCode) snprintf(result, sizeof result, "--");
    else if (s.net.uploadCode == 200) snprintf(result, sizeof result, "OK");
    else if (s.net.uploadCode < 0) snprintf(result, sizeof result, "FAIL");
    else snprintf(result, sizeof result, "%u", debugBound((unsigned)s.net.uploadCode, 999));
    if (s.net.uploadCode)
        snprintf(lines[6], 22, "Net %s %s %us", wifi, result,
                 debugBound((s.now - s.net.uploadMs) / 1000, 999));
    else snprintf(lines[6], 22, "Net %s --", wifi);
    char pct[4];
    debugCount(s.batteryPct, pct, sizeof pct);
    float volts = isfinite(s.batteryV) && s.batteryV > 0 ? s.batteryV : 0;
    snprintf(lines[7], 22, "B%s%% %.2fV U%d C%d", pct,
             volts < 9.99f ? volts : 9.99f, s.usb, s.charging);
}
