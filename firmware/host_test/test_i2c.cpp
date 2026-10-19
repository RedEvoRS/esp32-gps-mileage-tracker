// Exercise each sketch's real recovery/startup code against GPIO and PMU faults.
#include "arduino_stub.h"
uint32_t g_millis = 0;
SerialStub Serial;
#include "i2c_recovery.h"

static int g_fail = 0;
static unsigned g_retryDelays = 0;
#define CHECK(cond, msg) do { if (cond) printf("  PASS %s\n", msg); \
    else { printf("  FAIL %s (line %d)\n", msg, __LINE__); g_fail++; } } while (0)

static constexpr int SDA = 42, SCL = 41;
struct BusModel {
    unsigned clocks = 0, releaseAt = 0, stops = 0, stopSetups = 0, starts = 0;
    unsigned releases = 0, stickAtRelease = 0, stretchUs = 0;
    uint64_t stretchUntil = 0, fellAt = 0, roseAt = 0;
    bool sdaHeld = false, sclHeld = false, sclHigh = true;
    bool masterSclLow = false, masterSdaLow = false;
    bool openDrainOnly = true, clockPullup = true, timingOk = true;

    bool drivenLow(int pin) const {
        return (g_pinModes[pin] & ~PULLUP) == OUTPUT_OPEN_DRAIN && g_pinLevels[pin] == LOW;
    }
    void update() {
        bool sclLow = drivenLow(SCL), sdaLow = drivenLow(SDA);
        if (masterSclLow && !sclLow) {
            ++releases;
            stretchUntil = g_micros + stretchUs;
        }
        bool high = !sclLow && !sclHeld && g_micros >= stretchUntil &&
                    (!stickAtRelease || releases < stickAtRelease);
        if (sclHigh && !high) {
            timingOk &= g_micros - roseAt >= 5;
            fellAt = g_micros;
        }
        if (!sclHigh && high) {
            timingOk &= g_micros - fellAt >= 5;
            roseAt = g_micros;
            if (sdaLow) ++stopSetups;
            else {
                ++clocks;
                if (releaseAt && clocks >= releaseAt) sdaHeld = false;
            }
        }
        if (!masterSdaLow && sdaLow && high) ++starts;
        if (masterSdaLow && !sdaLow && high && !sdaHeld) {
            timingOk &= g_micros - roseAt >= 5;
            ++stops;
        }
        sclHigh = high; masterSclLow = sclLow; masterSdaLow = sdaLow;
        for (int pin : {SDA, SCL}) {
            int mode = g_pinModes[pin];
            openDrainOnly &= mode == INPUT || mode == INPUT_PULLUP ||
                             (mode & ~PULLUP) == OUTPUT_OPEN_DRAIN;
            if (pin == SCL && (mode & ~PULLUP) == OUTPUT_OPEN_DRAIN)
                clockPullup &= (mode & PULLUP) != 0;
        }
    }
    int read(int pin) {
        update();
        return pin == SCL ? sclHigh : !sdaHeld && !drivenLow(SDA);
    }
    bool released() const {
        return g_pinModes[SDA] == INPUT_PULLUP && g_pinModes[SCL] == INPUT_PULLUP;
    }
};
static BusModel model;

static void resetBus(unsigned releaseAt = 0, bool sdaHeld = false) {
    g_gpioHook = nullptr; g_digitalReadHook = nullptr; g_microDelayHook = nullptr;
    g_pinModes.clear(); g_pinLevels.clear(); g_micros = 0;
    model = BusModel(); model.releaseAt = releaseAt; model.sdaHeld = sdaHeld;
    g_gpioHook = [](int) { model.update(); };
    g_digitalReadHook = [](int pin) { return model.read(pin); };
    g_microDelayHook = [] { model.update(); };
    g_delayHook = nullptr;
    Serial.log.clear();
}

static bool logHas(const char* text) {
    for (const auto& line : Serial.log) if (line.find(text) != std::string::npos) return true;
    return false;
}

int main() {
    resetBus();
    auto result = recoverI2cBus(SDA, SCL);
    CHECK(result.state == I2cRecoveryState::NotNeeded && result.clocks == 0 && model.clocks == 0,
          "idle SDA needs no recovery and produces no clocks");
    CHECK(model.stops == 0 && model.released(), "idle bus stays released without STOP");

    for (unsigned k = 1; k <= 9; ++k) {
        resetBus(k, true);
        result = recoverI2cBus(SDA, SCL);
        CHECK(result.state == I2cRecoveryState::Recovered && result.clocks == k && model.clocks == k,
              "SDA release is detected after exactly k clocks (k=1..9)");
        CHECK(model.stops == 1 && model.stopSetups == 1 && model.starts == 0,
              "recovery generates one STOP with no unintended START");
        CHECK(model.openDrainOnly && model.clockPullup && model.timingOk && model.released(),
              "open drain with SCL pullup, at least 5 us phases, both pins released");
    }

    resetBus(0, true);
    result = recoverI2cBus(SDA, SCL);
    CHECK(result.state == I2cRecoveryState::SdaStuck && result.clocks == 9 && model.clocks == 9,
          "SDA permanently low fails after nine data clocks");
    CHECK(model.stopSetups == 1 && model.stops == 0 && model.released() && g_micros == 110,
          "stuck SDA gets a bounded best-effort STOP and released pins");

    for (bool sdaHeld : {false, true}) {
        resetBus(0, sdaHeld); model.sclHeld = true; model.sclHigh = false;
        result = recoverI2cBus(SDA, SCL);
        CHECK(result.state == I2cRecoveryState::SclStuck && result.clocks == 0 && model.clocks == 0,
              "SCL held low fails without clocks regardless of SDA");
        CHECK(g_micros == 1005 && model.released(), "initial SCL timeout is 1 ms plus settling");
    }

    resetBus(3, true); model.stretchUs = 15;
    result = recoverI2cBus(SDA, SCL);
    CHECK(result.state == I2cRecoveryState::Recovered && result.clocks == 3 && model.stops == 1 &&
          model.timingOk, "short clock stretching succeeds without shortening high phases");

    resetBus(5, true); model.stickAtRelease = 3;
    result = recoverI2cBus(SDA, SCL);
    CHECK(result.state == I2cRecoveryState::SclStuck && result.clocks == 2 &&
          model.clocks == 2 && model.released() && g_micros < 1200,
          "SCL stuck mid-recovery times out and reports completed clocks");

    resetBus(2, true); model.stickAtRelease = 3;
    result = recoverI2cBus(SDA, SCL);
    CHECK(result.state == I2cRecoveryState::SclStuck && result.clocks == 2 &&
          model.stops == 0 && model.released(), "SCL stuck during STOP fails with pins released");

    resetBus();
    TwoWire bus;
    XPowersAXP2101 pmu;
    PmuStartupResult startup;
    CHECK(beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && startup.attempts == 1 &&
          bus.begins == 1 && bus.ends == 0 && bus.timeout == 50 && pmu.beginCalls == 1,
          "normal startup succeeds once with a 50 ms Wire timeout");
    printPmuStartup(startup);
    CHECK(Serial.log.empty(), "normal startup does not log a recovery or retry");

    resetBus(); bus = TwoWire(); pmu = XPowersAXP2101(); pmu.failBegins = 2;
    std::vector<std::string> events;
    unsigned badOwnership = 0;
    g_retryDelays = 0;
    bus.onBegin = [&] { events.push_back("wire.begin"); };
    bus.onEnd = [&] { events.push_back("wire.end"); };
    pmu.onBegin = [&] {
        events.push_back("pmu.begin");
        if (pmu.beginCalls < 3) { model.sdaHeld = true; model.releaseAt = model.clocks + 2; }
    };
    g_gpioHook = [&](int) { if (bus.started) ++badOwnership; model.update(); };
    g_delayHook = [](uint32_t ms) { if (ms == 10) ++g_retryDelays; };
    CHECK(beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && startup.attempts == 3 &&
          bus.begins == 3 && bus.ends == 2 && pmu.beginCalls == 3 && g_retryDelays == 2,
          "two failed PMU probes retry with end/recovery/begin and 10 ms spacing");
    CHECK(events == std::vector<std::string>({"wire.begin", "pmu.begin", "wire.end",
          "wire.begin", "pmu.begin", "wire.end", "wire.begin", "pmu.begin"}) && !badOwnership,
          "Wire ends before GPIO recovery; each fresh bus precedes its PMU probe");
    CHECK(startup.recovery[1].clocks == 2 && startup.recovery[2].clocks == 2 && model.stops == 2,
          "a failed PMU transfer can leave SDA stuck and the next attempt clears it");
    CHECK(Serial.log.empty(), "startup saves results without printing before rail safety checks");
    printPmuStartup(startup);
    CHECK(logHas("retry 2/3") && logHas("retry 3/3") && logHas("recovered after 2 clocks"),
          "saved report prints retries and recovery clock counts");

    resetBus(); bus = TwoWire(); pmu = XPowersAXP2101(); pmu.failBegins = 9;
    CHECK(!beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && startup.attempts == 3 &&
          pmu.beginCalls == 3 && bus.ends == 2, "persistent PMU probe failure stops after three attempts");
    printPmuStartup(startup);
    CHECK(logHas("attempt 3/3 failed"), "final failed attempt is reported");

    resetBus(0, true); bus = TwoWire(); pmu = XPowersAXP2101();
    CHECK(!beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && startup.attempts == 3 &&
          bus.begins == 0 && pmu.beginCalls == 0 && model.clocks == 27 && model.released(),
          "persistent SDA fault retries recovery three times without starting Wire or PMU");
    printPmuStartup(startup);
    CHECK(logHas("SDA stuck low after 9 clocks"), "SDA failure includes completed clock count");

    resetBus(); model.sclHeld = true; model.sclHigh = false;
    bus = TwoWire(); pmu = XPowersAXP2101();
    CHECK(!beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && startup.attempts == 3 &&
          bus.begins == 0 && pmu.beginCalls == 0 && g_micros == 3015,
          "persistent SCL fault is bounded across all three attempts");
    printPmuStartup(startup);
    CHECK(logHas("SCL stuck low after 0 clocks"), "SCL failure is identified in Serial report");

    resetBus(); bus = TwoWire(); bus.failBegins = 2; pmu = XPowersAXP2101();
    CHECK(beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && bus.begins == 3 &&
          bus.ends == 2 && pmu.beginCalls == 1, "Wire begin failures retry without probing PMU");

    resetBus(); bus = TwoWire(); bus.failEnd = true;
    pmu = XPowersAXP2101(); pmu.failBegins = 1;
    CHECK(!beginPmuWithRecovery(bus, pmu, SDA, SCL, startup) && startup.wireEndFailed &&
          startup.attempts == 1 && bus.begins == 1 && pmu.beginCalls == 1,
          "Wire end failure aborts before touching controller-owned pins");
    printPmuStartup(startup);
    CHECK(logHas("Wire1.end() failed"), "controller release failure is reported");

    printf("test_i2c: %d failures\n", g_fail);
    return g_fail ? 1 : 0;
}
