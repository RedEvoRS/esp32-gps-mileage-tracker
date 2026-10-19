#pragma once
#include "diag_catalog.h"
#include "i2c_recovery.h"

struct PmuStartupEvent {
    diagnostics::Code code = diagnostics::Code::PMU_STARTUP_RECOVERY;
    uint8_t clockCount = 0, attemptCount = 0;
    const char* recoveryState = "not_needed";
    bool pmuReady = false, wireEndFailed = false;
};

// Summarize a completed beginPmuWithRecovery result; false means no event.
static bool makePmuStartupEvent(const PmuStartupResult& result, PmuStartupEvent& event) {
    event = PmuStartupEvent();
    if (!result.attempts) return false;
    bool recoveryNeeded = false;
    for (uint8_t i = 0; i < result.attempts; ++i) {
        event.clockCount += result.recovery[i].clocks;
        recoveryNeeded |= result.recovery[i].state != I2cRecoveryState::NotNeeded;
    }
    if (result.ready && result.attempts == 1 && !recoveryNeeded && !result.wireEndFailed)
        return false;
    I2cRecoveryState state = result.recovery[result.attempts - 1].state;
    event.attemptCount = result.attempts;
    event.recoveryState = state == I2cRecoveryState::Recovered ? "recovered" :
                          state == I2cRecoveryState::SdaStuck ? "sda_stuck" :
                          state == I2cRecoveryState::SclStuck ? "scl_stuck" : "not_needed";
    event.pmuReady = result.ready;
    event.wireEndFailed = result.wireEndFailed;
    return true;
}
