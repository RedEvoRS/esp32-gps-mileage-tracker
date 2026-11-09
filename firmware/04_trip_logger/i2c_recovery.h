// Sketch-local copy: keep identical in Stages 2, 3 and 4 so each builds alone.
#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <XPowersLib.h>

enum class I2cRecoveryState { NotNeeded, Recovered, SdaStuck, SclStuck };
struct I2cRecoveryResult {
    I2cRecoveryState state = I2cRecoveryState::NotNeeded;
    uint8_t clocks = 0;
};

// Allow up to 1 ms of clock stretching at each release; never wait forever.
static inline bool waitI2cClockHigh(int scl) {
    for (unsigned waited = 0; waited < 1000; waited += 5) {
        if (digitalRead(scl) == HIGH) return true;
        delayMicroseconds(5);
    }
    return digitalRead(scl) == HIGH;
}

static inline I2cRecoveryResult recoverI2cBus(int sda, int scl) {
    pinMode(sda, INPUT_PULLUP);
    pinMode(scl, INPUT_PULLUP);
    delayMicroseconds(5);  // Let the released lines rise before sampling.
    if (!waitI2cClockHigh(scl)) return {I2cRecoveryState::SclStuck, 0};
    if (digitalRead(sda) == HIGH) return {I2cRecoveryState::NotNeeded, 0};

    // Preload HIGH while still an input, then use open drain: HIGH releases,
    // never drives against a slave. 5 us low + 5 us high is at most 100 kHz.
    digitalWrite(scl, HIGH);
    pinMode(scl, OUTPUT_OPEN_DRAIN | PULLUP);
    uint8_t clocks = 0;
    while (clocks < 9 && digitalRead(sda) == LOW) {
        digitalWrite(scl, LOW);
        delayMicroseconds(5);
        digitalWrite(scl, HIGH);
        if (!waitI2cClockHigh(scl)) {
            pinMode(scl, INPUT_PULLUP);
            return {I2cRecoveryState::SclStuck, clocks};
        }
        delayMicroseconds(5);
        ++clocks;
    }

    // STOP: set SDA low with SCL low to avoid an unintended START, then
    // release SCL before SDA. This final SCL edge is STOP setup, not data.
    digitalWrite(scl, LOW);
    digitalWrite(sda, LOW);
    pinMode(sda, OUTPUT_OPEN_DRAIN);
    delayMicroseconds(5);
    digitalWrite(scl, HIGH);
    bool sclHigh = waitI2cClockHigh(scl);
    if (sclHigh) delayMicroseconds(5);
    pinMode(sda, INPUT_PULLUP);
    delayMicroseconds(5);
    pinMode(scl, INPUT_PULLUP);  // Wire.begin() takes over both released pins.
    if (!sclHigh) return {I2cRecoveryState::SclStuck, clocks};
    return {digitalRead(sda) == HIGH ? I2cRecoveryState::Recovered : I2cRecoveryState::SdaStuck,
            clocks};
}

static constexpr uint8_t PMU_STARTUP_ATTEMPTS = 3;
struct PmuStartupResult {
    I2cRecoveryResult recovery[PMU_STARTUP_ATTEMPTS];
    uint8_t attempts = 0;
    bool ready = false;
    bool wireEndFailed = false;
};

// Save results without Serial/allocation: Stage 4 must disable LoRa first.
static inline bool beginPmuWithRecovery(TwoWire& bus, XPowersAXP2101& pmu, int sda, int scl,
                                 PmuStartupResult& result) {
    result = PmuStartupResult();
    for (uint8_t attempt = 0; attempt < PMU_STARTUP_ATTEMPTS; ++attempt) {
        if (attempt) {
            // Core 3.x owns the pins until end() deinitializes the controller.
            if (!bus.end()) { result.wireEndFailed = true; return false; }
            delay(10);
        }
        result.attempts = attempt + 1;
        result.recovery[attempt] = recoverI2cBus(sda, scl);
        I2cRecoveryState state = result.recovery[attempt].state;
        if (state == I2cRecoveryState::SdaStuck || state == I2cRecoveryState::SclStuck) continue;
        if (!bus.begin(sda, scl)) continue;
        bus.setTimeOut(50);
        if (pmu.begin(bus, AXP2101_SLAVE_ADDRESS, sda, scl)) {
            result.ready = true;
            return true;
        }
    }
    return false;
}

static inline void printPmuStartup(const PmuStartupResult& result) {
    for (uint8_t i = 0; i < result.attempts; ++i) {
        if (i) Serial.printf("PMU startup retry %u/%u.\n", (unsigned)i + 1, (unsigned)PMU_STARTUP_ATTEMPTS);
        const I2cRecoveryResult& recovery = result.recovery[i];
        if (recovery.state != I2cRecoveryState::NotNeeded) {
            const char* status = recovery.state == I2cRecoveryState::Recovered ? "recovered" :
                                 recovery.state == I2cRecoveryState::SdaStuck ? "failed: SDA stuck low" :
                                 "failed: SCL stuck low";
            Serial.printf("I2C recovery: %s after %u clocks.\n", status, (unsigned)recovery.clocks);
        }
        if (!result.ready || i + 1 < result.attempts)
            Serial.printf("PMU startup attempt %u/%u failed.\n", (unsigned)i + 1, (unsigned)PMU_STARTUP_ATTEMPTS);
    }
    if (result.wireEndFailed) Serial.println("ERROR: Wire1.end() failed; GPIO recovery aborted.");
}
