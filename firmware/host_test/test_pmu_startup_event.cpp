// Boot-history policy uses the saved result without touching hardware or Serial.
#include "arduino_stub.h"
uint32_t g_millis = 0;
SerialStub Serial;
#include "pmu_startup_event.h"
#include <cassert>
#include <cstring>

int main() {
    PmuStartupResult result;
    PmuStartupEvent event;
    assert(!makePmuStartupEvent(result, event));  // startup has not run

    result.attempts = 1;
    result.ready = true;
    assert(!makePmuStartupEvent(result, event));  // clean boot

    for (uint8_t clocks = 1; clocks <= 9; ++clocks) {
        result.recovery[0] = {I2cRecoveryState::Recovered, clocks};
        assert(makePmuStartupEvent(result, event));
        assert(event.code == diagnostics::Code::PMU_STARTUP_RECOVERY);
        assert(event.clockCount == clocks && event.attemptCount == 1 && event.pmuReady);
        assert(std::strcmp(event.recoveryState, "recovered") == 0 && !event.wireEndFailed);
    }

    result = PmuStartupResult();
    result.ready = true;
    for (uint8_t attempts = 2; attempts <= PMU_STARTUP_ATTEMPTS; ++attempts) {
        result.attempts = attempts;
        assert(makePmuStartupEvent(result, event));  // retries on an idle bus still count
        assert(event.clockCount == 0 && event.attemptCount == attempts && event.pmuReady);
        assert(std::strcmp(event.recoveryState, "not_needed") == 0);
    }

    result.recovery[0] = {I2cRecoveryState::SdaStuck, 9};
    result.recovery[1] = {I2cRecoveryState::Recovered, 4};
    assert(makePmuStartupEvent(result, event));
    assert(event.clockCount == 13 && event.attemptCount == 3 && event.pmuReady);
    assert(std::strcmp(event.recoveryState, "not_needed") == 0);  // last attempt, not worst

    result.ready = false;
    for (auto& recovery : result.recovery) recovery = {I2cRecoveryState::SdaStuck, 9};
    assert(makePmuStartupEvent(result, event));
    assert(event.clockCount == 27 && !event.pmuReady && !event.wireEndFailed);
    assert(std::strcmp(event.recoveryState, "sda_stuck") == 0);

    for (auto& recovery : result.recovery) recovery = {I2cRecoveryState::SclStuck, 0};
    assert(makePmuStartupEvent(result, event));
    assert(event.clockCount == 0 && std::strcmp(event.recoveryState, "scl_stuck") == 0);

    result = PmuStartupResult();
    result.attempts = 3;
    assert(makePmuStartupEvent(result, event));  // PMU probes can fail with an idle bus
    assert(event.attemptCount == 3 && !event.pmuReady);
    assert(std::strcmp(event.recoveryState, "not_needed") == 0);

    result.attempts = 1;
    result.wireEndFailed = true;
    assert(makePmuStartupEvent(result, event));  // aborted before the next recovery
    assert(event.attemptCount == 1 && event.wireEndFailed && !event.pmuReady);

    result = PmuStartupResult();
    result.attempts = 1;
    result.ready = true;
    assert(!makePmuStartupEvent(result, event));  // no stale event on output reuse
    assert(event.clockCount == 0 && event.attemptCount == 0 && !event.wireEndFailed);
    assert(Serial.log.empty() && g_millis == 0 && g_micros == 0 && g_pinModes.empty());
    puts("PMU startup event: all checks passed");
}
