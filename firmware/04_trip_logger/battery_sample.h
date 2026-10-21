#pragma once
#include "event_log.h"

namespace diagnostics {
struct BatterySample {
    int mv = -1, percent = -1;
};

// AXP2101/XPowersLib 0.3.3 register layout. Read raw signed results: the
// convenience voltage getter can turn an I2C failure into a plausible value.
template<class Pmu> BatterySample readBatterySample(Pmu& pmu) {
    BatterySample sample;
    int status = pmu.readRegister(0x00); // STATUS1: battery present, bit 3
    if (status < 0 || !(status & 0x08)) return sample;
    int high = pmu.readRegister(0x34), low = pmu.readRegister(0x35);
    int percent = pmu.readRegister(0xA4);
    if (high >= 0 && low >= 0) {
        int mv = ((high & 0x1F) << 8) | low;
        if (mv > 0 && mv <= 6000) sample.mv = mv;
    }
    if (percent >= 0 && percent <= 100) sample.percent = percent;
    return sample;
}

inline void addBatteryContext(EventInput& event, const BatterySample& sample) {
    if (sample.mv > 0 && sample.mv <= 6000)
        event.context[event.contextCount++] = EventContext::integer("battery_mv", sample.mv);
    if (sample.percent >= 0 && sample.percent <= 100)
        event.context[event.contextCount++] = EventContext::integer("battery_pct", sample.percent);
}
} // namespace diagnostics
