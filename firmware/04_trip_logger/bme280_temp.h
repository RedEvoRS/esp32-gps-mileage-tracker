#pragma once
#include <stdint.h>
#include <stddef.h>

// Bosch BME280 datasheet, sections 4.2.2/4.2.3 and 8.1. No pressure or
// humidity compensation/state is needed for this temperature-only probe.
namespace bme280_temp {
struct Calibration { uint16_t t1; int16_t t2, t3; };

inline int16_t signedWord(uint16_t word) {
    return word < 0x8000 ? int16_t(word) : int16_t(int32_t(word) - 65536);
}
inline Calibration calibration(const uint8_t* p) {
    return {uint16_t(p[0] | (uint16_t(p[1]) << 8)),
            signedWord(uint16_t(p[2] | (uint16_t(p[3]) << 8))),
            signedWord(uint16_t(p[4] | (uint16_t(p[5]) << 8)))};
}

// Result in hundredths of a degree C. Widen Bosch's integer intermediates:
// signed calibration extremes and the full 20-bit ADC range must not overflow.
inline int32_t compensate(uint32_t raw, Calibration c) {
    int64_t v1 = ((int64_t(raw >> 3) - int64_t(c.t1) * 2) * c.t2) >> 11;
    int64_t delta = int64_t(raw >> 4) - c.t1;
    int64_t v2 = (((delta * delta) >> 12) * c.t3) >> 14;
    return int32_t(((v1 + v2) * 5 + 128) >> 8);
}

// Wire is supplied by the caller, which owns its core, pins and timeout.
// Never begin/end/recover the shared OLED bus here. Abort on the first failed
// transaction; a missing sensor costs at most one existing bus timeout.
class Sensor {
    Calibration cal_{};
    bool ready_ = false, pending_ = false;
    uint32_t triggered_ = 0;
    static constexpr uint8_t address = 0x77;
    template<class Bus> static bool read(Bus& bus, uint8_t reg, uint8_t* p, size_t n) {
        bus.beginTransmission(address);
        if (bus.write(reg) != 1) { bus.endTransmission(); return false; }
        if (bus.endTransmission() != 0 || bus.requestFrom(address, n) != n) return false;
        for (size_t i = 0; i < n; ++i) {
            int value = bus.read();
            if (value < 0) return false;
            p[i] = uint8_t(value);
        }
        return true;
    }
    template<class Bus> static bool write(Bus& bus, uint8_t reg, uint8_t value) {
        bus.beginTransmission(address);
        bool ok = bus.write(reg) == 1;
        if (ok) ok = bus.write(value) == 1;
        return bus.endTransmission() == 0 && ok;
    }
    bool failed() { ready_ = pending_ = false; return false; }
public:
    // Only called once per logging interval, including retries after absence.
    template<class Bus> bool trigger(Bus& bus, uint32_t now) {
        pending_ = false;
        uint8_t status;
        if (!ready_) {
            uint8_t id, bytes[6];
            if (!read(bus, 0xD0, &id, 1) || id != 0x60 ||
                !read(bus, 0xF3, &status, 1) || (status & 0x09) ||
                !read(bus, 0x88, bytes, sizeof bytes)) return failed();
            cal_ = calibration(bytes);
            if (cal_.t1 == 0 || cal_.t1 == 0xffff) return failed();
            // Sleep before changing configuration, including after an ESP reset.
            if (!write(bus, 0xF4, 0x00) || !write(bus, 0xF2, 0x00) ||
                !write(bus, 0xF5, 0x00)) return failed(); // humidity skipped, filter off
            ready_ = true;
        }
        // osrs_t=x1, osrs_p=skipped, forced mode. Returns to sleep by itself.
        if (!write(bus, 0xF4, 0x21)) return failed();
        pending_ = true; triggered_ = now;
        return true;
    }
    bool waiting(uint32_t now) const {
        // Datasheet maximum: 1.25 + 2.3 * osrs_t = 3.55 ms; round up.
        return pending_ && uint32_t(now - triggered_) < 5;
    }
    template<class Bus> bool finish(Bus& bus, uint32_t now, int32_t& centiC) {
        if (!pending_ || waiting(now)) return false;
        pending_ = false;
        uint8_t status, bytes[3];
        // No polling loop: still busy, NVM reload, short read, or skipped value
        // all mean unavailable for this interval, never the previous sample.
        if (!read(bus, 0xF3, &status, 1) || (status & 0x09) ||
            !read(bus, 0xFA, bytes, sizeof bytes)) return failed();
        uint32_t raw = (uint32_t(bytes[0]) << 12) | (uint32_t(bytes[1]) << 4) | (bytes[2] >> 4);
        if (raw == 0x80000) return failed();
        centiC = compensate(raw, cal_);
        return true;
    }
};
}
