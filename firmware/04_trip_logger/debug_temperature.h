// Included only with DISPLAY_DEBUG=1. Core 1 owns this state and all I2C.
#pragma once
#include <stdint.h>
#include <cmath>
#include "bme280_temp.h"

namespace debug_temperature {
// AXP2101 datasheet V1.4: REG50[4]=1 excludes TS from charger decisions.
// [3:2]=01 biases TS while its ADC channel is enabled; [1:0]=10 is 50 uA.
// Do NOT use XPowersLib 0.3.3 enableTSPinMeasure(): it clears [4].
// https://github.com/lewisxhe/XPowersLib/blob/ef40b49af66fe3b1907ab74ca76e10c0dbf69012/datasheet/AXP2101_Datasheet_V1.4_en.pdf
struct TsSetup {
    bool ready = false;
    int control = -1, adc = -1; // actual read-backs, retained for the boot log
};
template<class Pmu> TsSetup setupTs(Pmu& pmu) {
    TsSetup result;
    int before = pmu.readRegister(0x50);
    if (before < 0) return result;
    int written = pmu.writeRegister(0x50, uint8_t((before & 0xE0) | 0x16));
    result.control = pmu.readRegister(0x50);
    if (written != 0 || result.control < 0 || (result.control & 0x1F) != 0x16) return result;
    before = pmu.readRegister(0x30);
    if (before < 0) return result;
    written = pmu.writeRegister(0x30, uint8_t(before | 0x02));
    result.adc = pmu.readRegister(0x30);
    result.ready = written == 0 && result.adc >= 0 && result.adc == (before | 0x02);
    return result;
}
inline int tsRaw(int high, int low) {
    if (high < 0 || low < 0) return -1;
    int raw = ((high & 0x3F) << 8) | low;
    return raw < 0x2000 ? raw : -1; // 0x2000 is invalid, not a voltage
}
inline float tsVolts(int raw) {
    // Table 6-5: 0.5 mV/count; read high six bits (REG36) BEFORE low (REG37).
    return raw >= 0 && raw < 0x2000 ? raw * 0.0005f : NAN;
}
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
            // those descriptions conflict. TS keeps its separate H6L8 read.
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
