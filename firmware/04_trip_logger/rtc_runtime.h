// Core-1-only I2C/NVS adapter. Producers only read DiagnosticClock publications.
#pragma once
#include <Wire.h>
#include <Preferences.h>
#include "rtc_pcf8563.h"
#include "diagnostic_clock.h"

namespace diagnostics {
class RtcRuntime {
    bool begun_ = false, checked_ = false, done_ = false, waiting_ = false;
    uint8_t attempts_ = 0, control_ = 0;
    uint32_t attemptedMs_ = 0;
    int64_t offset_ = 0;
    bool hadValid_ = false;
    bool repair_ = false;
    bool unavailable_ = false, invalid_ = false;
    rtc::Status invalidReason_ = rtc::Status::ReadFailed;
    rtc::Status status_ = rtc::Status::Unavailable;
    rtc::Status bootStatus_ = rtc::Status::Unavailable;
    uint32_t bootSeconds_ = 0;
    int64_t bootAgeMs_ = -1;
    void observe(rtc::Status status) {
        status_ = status;
        // Loss of communication cannot clear an earlier invalid-time episode,
        // nor can a readable but invalid chip clear an unavailable episode.
        if (status == rtc::Status::Unavailable) unavailable_ = true;
        else if (status != rtc::Status::Valid) { invalid_ = true; invalidReason_ = status; }
    }
    bool ack(TwoWire& wire) {
        wire.beginTransmission(0x51); return wire.endTransmission() == 0;
    }
    bool readRegisters(TwoWire& wire, uint8_t reg, uint8_t* out, size_t count) {
        wire.beginTransmission(0x51); wire.write(reg);
        if (wire.endTransmission(false) != 0 || wire.requestFrom(uint8_t(0x51), count) != count) return false;
        for (size_t i = 0; i < count; ++i) { int v = wire.read(); if (v < 0) return false; out[i] = uint8_t(v); }
        return true;
    }
    rtc::Status read(TwoWire& wire, uint32_t& seconds, bool boot) {
        if (!ack(wire)) return rtc::Status::Unavailable;
        uint8_t registers[7];
        if (!readRegisters(wire, 0, &control_, 1) || !readRegisters(wire, 2, registers, 7))
            return boot || !ack(wire) ? rtc::Status::Unavailable : rtc::Status::ReadFailed;
        return rtc::validate(control_, registers, seconds);
    }
    bool writeRegisters(TwoWire& wire, uint8_t reg, const uint8_t* bytes, size_t count) {
        wire.beginTransmission(0x51);
        return wire.write(reg) == 1 && wire.write(bytes, count) == count && wire.endTransmission() == 0;
    }
    void failure(TwoWire& wire, DiagnosticClock& clock) {
        attemptedMs_ = millis(); // full backoff after failure, including alignment wait
        observe(ack(wire) ? rtc::Status::ReadFailed : rtc::Status::Unavailable);
        repair_ = true;
        clock.invalidateRtc(); waiting_ = false; checked_ = false;
    }
public:
    rtc::Status status() const { return status_; }
    bool unavailable() const { return unavailable_; }
    bool invalid() const { return invalid_; }
    rtc::Status invalidReason() const { return invalidReason_; }
    void begin(TwoWire& wire, DiagnosticClock& clock) {
        if (xPortGetCoreID() != 1 || begun_) return;
        begun_ = true;
        uint32_t seconds = 0;
        observe(read(wire, seconds, true));
        bootStatus_ = status_; bootSeconds_ = seconds;
        repair_ = status_ != rtc::Status::Valid;
        uint32_t now = millis(); clock.tick(now);
        if (status_ == rtc::Status::Valid) {
            int64_t age = -1;
            Preferences settings;
            if (settings.begin("diag", true)) {
                uint64_t saved = settings.getULong64("rtc_set", 0);
                if (saved >= rtc::kFirstUtc && saved <= seconds) age = int64_t(seconds - saved) * 1000;
                settings.end();
            }
            clock.rtcAnchor(seconds, now, age);
            bootAgeMs_ = age;
        } else {
            clock.invalidateRtc();
        }
    }
    // Keep the early boot read, but report it after USB has had time to attach.
    void printBootStatus() const {
        if (!begun_) return;
        if (bootStatus_ == rtc::Status::Valid) {
            auto d = rtc::fromUtc(bootSeconds_);
            Serial.printf("RTC: valid %04u-%02u-%02uT%02u:%02u:%02uZ", d.year, d.month, d.day, d.hour, d.minute, d.second);
            if (bootAgeMs_ >= 0) Serial.printf(" (last set %.1f h ago)", double(bootAgeMs_) / 3600000);
            else Serial.printf(" (last set unknown)");
            Serial.println();
        } else {
            if (bootStatus_ == rtc::Status::Unavailable) Serial.println("RTC: not responding");
            else Serial.printf("RTC: invalid (%s), will set from GPS/SNTP\n",
                               bootStatus_ == rtc::Status::Stopped ? "stopped/VL" : rtc::reason(bootStatus_));
        }
    }
    bool begun() const { return begun_; }
    // Called from loop only. A failed transaction consumes an attempt, even if
    // it failed before writing. At most three attempts, >=60 s apart, per boot.
    void service(TwoWire& wire, DiagnosticClock& clock, bool trusted, uint64_t systemMs, EventTimeSource source) {
        if (xPortGetCoreID() != 1 || !begun_ || !trusted || done_ || (attempts_ >= 3 && !waiting_)) return;
        uint32_t now = millis();
        if (!waiting_ && attempts_ && uint32_t(now - attemptedMs_) < 60000) return;
        if (systemMs / 1000 < rtc::kFirstUtc || systemMs / 1000 > rtc::kLastUtc) return;
        if (!checked_) {
            ++attempts_; attemptedMs_ = now;
            uint32_t seconds = 0;
            observe(read(wire, seconds, false));
            if (status_ == rtc::Status::Unavailable || status_ == rtc::Status::ReadFailed) {
                attemptedMs_ = millis();
                repair_ = true;
                clock.invalidateRtc(); return;
            }
            // Pair the fresh RTC read with current system UTC, rather than the
            // pre-transaction sample which may precede a whole-second boundary.
            timeval observed; gettimeofday(&observed, nullptr);
            systemMs = uint64_t(observed.tv_sec) * 1000 + observed.tv_usec / 1000;
            hadValid_ = status_ == rtc::Status::Valid;
            if (!hadValid_) clock.invalidateRtc();
            offset_ = int64_t(seconds) - int64_t(systemMs / 1000);
            checked_ = true;
            if (!rtc::needsWrite(hadValid_ && !repair_, seconds, uint32_t(systemMs / 1000))) {
                done_ = true; return;
            }
            waiting_ = true;
        }
        // No delay: wait for a later loop pass inside the first 100 ms.
        timeval tv; gettimeofday(&tv, nullptr);
        systemMs = uint64_t(tv.tv_sec) * 1000 + tv.tv_usec / 1000;
        now = millis();
        if (!rtc::aligned(systemMs)) return;
        uint32_t written = uint32_t(systemMs / 1000);
        uint8_t registers[7];
        // The fresh system sample can cross the supported calendar boundary
        // during the drift read. Never transmit an unencodable register image.
        if (!rtc::encode(written, registers)) return;
        uint8_t running = control_ & ~uint8_t(0x20); // preserve all other Control_1 bits
        if (!writeRegisters(wire, 2, registers, 7) || !writeRegisters(wire, 0, &running, 1)) {
            failure(wire, clock); return;
        }
        uint32_t verified = 0;
        status_ = read(wire, verified, false);
        if (status_ != rtc::Status::Valid || verified < written || verified - written > 1) {
            failure(wire, clock); return;
        }
        done_ = true; waiting_ = false; // exactly one verified write per boot
        unavailable_ = invalid_ = false;
        // The write was aligned to systemMs, not to the later verification read.
        clock.rtcAnchorMs(systemMs, now, int64_t(systemMs % 1000));
        Preferences settings;
        if (settings.begin("diag", false)) {
            bool saved = settings.putULong64("rtc_set", written) == sizeof(uint64_t) &&
                         settings.getULong64("rtc_set", 0) == written;
            settings.end();
            if (!saved) Serial.println("RTC: last-set age could not be saved");
        } else Serial.println("RTC: last-set age could not be saved");
        Serial.printf("RTC: set from %s, verified", source == EventTimeSource::Gps ? "gps" : "sntp");
        if (hadValid_) Serial.printf(" (was off by %lld s)", static_cast<long long>(offset_));
        Serial.println();
    }
};
} // namespace diagnostics
