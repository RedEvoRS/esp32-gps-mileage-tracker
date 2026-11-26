// Runtime debug measurements. Core 1 owns this state and all I2C.
#pragma once
#include <stdint.h>

namespace debug_temperature {
struct Power {
    uint32_t sampledMs = 0;
    bool sampled = false;
    int status1 = -1, status2 = -1, percent = -1, millivolts = -1;
};
template<class Pmu> Power samplePower(Pmu& pmu, uint32_t now) {
    Power p;
    p.sampled = true; p.sampledMs = now;
    // One checked snapshot per Temp: line. Unlike bool/unsigned getters,
    // failed reads remain unknown in the serial log.
    p.status2 = pmu.readRegister(0x01);
    p.status1 = pmu.readRegister(0x00);
    if (p.status1 >= 0 && (p.status1 & 0x08)) {
        p.percent = pmu.readRegister(0xA4);
        if (p.percent > 100) p.percent = -1;
        int high = pmu.readRegister(0x34);
        int low = pmu.readRegister(0x35);
        if (high >= 0 && low >= 0) {
            // V1.4 REG34 lists [13:8], but Table 6-5 ends at 0x1FFF.
            // Match XPowersLib 0.3.3 getBattVoltage()'s H5L8 mask where
            // those descriptions conflict.
            p.millivolts = ((high & 0x1F) << 8) | low;
        }
    }
    return p;
}
inline bool isCharging(int status) {
    // V1.4 REG01[6:5] is current direction; bit 7 defaults to zero with
    // no description. Match XPowersLib 0.3.3's full upper-bit comparison.
    return status >= 0 && (status >> 5) == 0x01;
}
inline const char* phase(int status) {
    if (status < 0) return "--";
    static const char* const phases[] = {"trickle", "pre", "cc", "cv", "done", "stop", "--", "--"};
    return phases[status & 7];
}
}
