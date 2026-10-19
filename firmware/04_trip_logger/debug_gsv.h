// Optional, core-1-only GSV counters. Does not change fix or GPX construction.
#pragma once
#include <TinyGPSPlus.h>
#include <stdint.h>

class DebugGsv {
    // M10 SPG 5.10 interface description, sections 2.4/2.5: GNSS-specific
    // GSV talkers, GB by default; QZSS is GP before NMEA 4.11, GQ after.
    // Accept legacy BD as an alias for GB, never a second constellation.
    TinyGPSCustom fields[6];
    uint32_t seen[5] = {};
    uint8_t count[5] = {};
    bool valid[5] = {};
public:
    explicit DebugGsv(TinyGPSPlus& gps) : fields{
        {gps, "GPGSV", 3}, {gps, "GLGSV", 3}, {gps, "GAGSV", 3},
        {gps, "GBGSV", 3}, {gps, "BDGSV", 3}, {gps, "GQGSV", 3}} {}

    // Call after EACH checksum-valid sentence, before another sentence can
    // overwrite its custom fields. Multipart/signal groups replace a talker
    // total, never add that total again for every sentence in the group.
    void update(uint32_t now) {
        for (unsigned i = 0; i < 6; ++i) {
            if (!fields[i].isUpdated()) continue;
            const char* text = fields[i].value();
            unsigned value = 0, digits = 0;
            for (; *text >= '0' && *text <= '9' && digits < 3; ++text, ++digits)
                value = value * 10 + (*text - '0');
            if (!digits || *text || value > 99) continue;
            unsigned slot = i == 4 ? 3 : (i == 5 ? 4 : i);
            count[slot] = value;
            seen[slot] = now;
            valid[slot] = true;
        }
    }

    int inView(uint32_t now) const {
        int total = 0;
        bool any = false;
        for (unsigned i = 0; i < 5; ++i) {
            if (!valid[i] || uint32_t(now - seen[i]) > 15000) continue;
            total += count[i];
            any = true;
        }
        return any ? total : -1;
    }
};
