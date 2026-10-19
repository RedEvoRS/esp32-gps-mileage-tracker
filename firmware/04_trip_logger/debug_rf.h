// Optional MON-RF decoding and coordinate-free serial formatting, core 1 only.
#pragma once
#include "debug_gsv.h"
#include "debug_status.h"

struct DebugRf {
    bool valid = false;
    uint8_t blocks = 0, jam = 0, cw = 0, antenna = 0, power = 0;
    uint16_t noise = 0;
    unsigned agcPct = 0;
};

static inline bool parseDebugMonRf(const uint8_t* payload, size_t len, DebugRf& out) {
    if (len < 4 || payload[0] != 0 || !payload[1] || len != 4 + 24u * payload[1])
        return false;
    const uint8_t* b = payload + 4;  // M10 has one RF block; report the first.
    unsigned agc = b[14] | (unsigned(b[15]) << 8);
    DebugRf rf;
    rf.valid = true; rf.blocks = payload[1];
    rf.jam = b[1] & 0x03; rf.antenna = b[2]; rf.power = b[3];
    rf.noise = b[12] | (uint16_t(b[13]) << 8); rf.cw = b[16];
    rf.agcPct = debugBound(agc, 8191) * 100UL / 8191;
    out = rf;
    return true;
}

static constexpr size_t DEBUG_RF_LINE_BYTES = 192;
static inline void formatDebugRf(const DebugRf& rf, const DebugSignal& signal,
                                 int usedCount, float dilution, char fix,
                                 char* out, size_t n) {
    static const char* kAnt[] = {"INIT", "DONTKNOW", "OK", "SHORT", "OPEN"};
    static const char* kPower[] = {"OFF", "ON", "DONTKNOW"};
    static const char* kJam[] = {"unknown", "ok", "warning", "critical"};
    char radio[128];
    if (!rf.valid) snprintf(radio, sizeof radio, "rf --");
    else {
        char blocks[12] = "";
        if (rf.blocks > 1) snprintf(blocks, sizeof blocks, " blocks %u", unsigned(rf.blocks));
        snprintf(radio, sizeof radio, "jam %s cw %u noise %u agc %u%% ant %s pwr %s%s",
                 kJam[rf.jam & 3], unsigned(rf.cw), unsigned(rf.noise),
                 debugBound(rf.agcPct, 100), rf.antenna < 5 ? kAnt[rf.antenna] : "?",
                 rf.power < 3 ? kPower[rf.power] : "?", blocks);
    }
    char heard[4], strong[4], maxCno[4], used[4], hdop[5];
    debugCount(signal.heard, heard, sizeof heard);
    debugCount(signal.strong, strong, sizeof strong);
    debugCount(signal.maxCno, maxCno, sizeof maxCno);
    debugCount(usedCount, used, sizeof used);
    if (!isfinite(dilution) || dilution < 0) snprintf(hdop, sizeof hdop, "--");
    else snprintf(hdop, sizeof hdop, "%.1f", dilution < 99.9f ? dilution : 99.9f);
    snprintf(out, n, "GPS RF: %s | heard %s (>=30: %s) max %s | used %s hdop %s fix %s",
             radio, heard, strong, maxCno, used, hdop, fix == 'A' ? "A" : fix == 'V' ? "V" : "--");
}
