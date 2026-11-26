#include "../04_trip_logger/debug_temperature.h"
#include "../04_trip_logger/charge_temperature.h"
#include "../04_trip_logger/bme280_temp.h"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <vector>
#include <utility>
#include <string>

static unsigned checks = 0;
#define REQUIRE(c) do { ++checks; if (!(c)) { std::fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #c); std::exit(1); } } while (0)

// Independent transcription of Bosch datasheet section 8.1, degrees C.
static double reference(uint32_t raw, bme280_temp::Calibration c) {
    double var1 = (double(raw) / 16384.0 - double(c.t1) / 1024.0) * c.t2;
    double delta = double(raw) / 131072.0 - double(c.t1) / 8192.0;
    double var2 = delta * delta * c.t3;
    return (var1 + var2) / 5120.0;
}

struct Bus {
    std::map<unsigned, uint8_t> regs;
    std::vector<uint8_t> tx, rx;
    std::vector<std::pair<unsigned, unsigned>> writes;
    unsigned cursor = 0, at = 0, transactions = 0, failAt = 0;
    bool shortRead = false;
    void beginTransmission(uint8_t addr) { REQUIRE(addr == 0x77); tx.clear(); }
    size_t write(uint8_t v) { tx.push_back(v); return 1; }
    unsigned endTransmission() {
        if (++transactions == failAt) return 5;
        REQUIRE(!tx.empty()); cursor = tx[0];
        if (tx.size() == 2) { regs[cursor] = tx[1]; writes.emplace_back(cursor, tx[1]); }
        return 0;
    }
    size_t requestFrom(uint8_t addr, size_t n) {
        REQUIRE(addr == 0x77); rx.clear(); at = 0;
        for (size_t i = 0; i < n; ++i) rx.push_back(regs[cursor + i]);
        if (shortRead) rx.pop_back();
        return rx.size();
    }
    int read() { return at < rx.size() ? rx[at++] : -1; }
};
static Bus sensorBus() {
    Bus bus;
    bus.regs = {{0xD0,0x60},{0xF3,0},{0x88,0x70},{0x89,0x6b}, // T1=27504
                {0x8a,0x43},{0x8b,0x67},{0x8c,0x18},{0x8d,0xfc}, // T2=26435,T3=-1000
                {0xfa,0x7e},{0xfb,0xed},{0xfc,0x00}}; // adc_T=519888
    return bus;
}
struct Pmu {
    std::map<uint8_t,int> regs{{0x00,0x28},{0x01,0x22},
                               {0xA4,85},{0x34,0x0f},{0x35,0xb4}};
    int readRegister(uint8_t reg) { return regs[reg]; }
};

static void testChargeDecision() {
    auto evaluate = [](float die, float air, bool paused = false) {
        return charge_temperature::evaluate(die, air, paused, 45.0f, 38.0f, 0.0f, 5.0f, 9.0f);
    };
    auto dieHot = evaluate(54, 20), dieCold = evaluate(9, 20);
    auto airHot = evaluate(30, 45), airCold = evaluate(30, 0);
    REQUIRE(dieHot.pause && dieHot.hot() && dieHot.die.hot && !dieHot.air.hot && !dieHot.cold());
    REQUIRE(dieCold.pause && dieCold.cold() && dieCold.die.cold && !dieCold.air.cold && !dieCold.hot());
    REQUIRE(airHot.pause && airHot.hot() && airHot.air.hot && !airHot.die.hot && !airHot.cold());
    REQUIRE(airCold.pause && airCold.cold() && airCold.air.cold && !airCold.die.cold && !airCold.hot());
    auto split = evaluate(54, 0);
    REQUIRE(split.pause && split.hot() && split.cold() && split.die.hot && split.air.cold);
    REQUIRE(evaluate(30, -40).air.cold && evaluate(30, 85).air.hot);
    REQUIRE(!evaluate(53.99f, 44.99f).pause && !evaluate(9.01f, 0.01f).pause);

    for (float die : {14.0f, 47.0f}) for (float air : {5.0f, 38.0f}) {
        auto resumed = evaluate(die, air, true);
        REQUIRE(!resumed.pause && !resumed.nextPauseRequested && !resumed.hot() && !resumed.cold());
    }
    REQUIRE(evaluate(47.01f, 20, true).die.hot && evaluate(13.99f, 20, true).die.cold);
    REQUIRE(evaluate(30, 38.01f, true).air.hot && evaluate(30, 4.99f, true).air.cold);

    // NAN represents an absent, failed or stale sample. Compare fallbacks
    // with the original die-only thresholds, including each exact boundary.
    for (float air : {NAN, INFINITY, -INFINITY, -40.01f, 85.01f}) {
        for (float die : {-40.0f, 9.0f, 9.01f, 13.99f, 14.0f, 30.0f, 47.0f, 47.01f, 53.99f, 54.0f, 125.0f}) {
            for (bool paused : {false, true}) {
                bool hot = paused ? die > 47 : die >= 54;
                bool cold = paused ? die < 14 : die <= 9;
                auto result = evaluate(die, air, paused);
                REQUIRE(result.pause == (hot || cold) && result.nextPauseRequested == result.pause);
                REQUIRE(!result.unreadable && result.hot() == hot && result.cold() == cold);
                REQUIRE(!result.air.hot && !result.air.cold);
            }
        }
    }
    for (float die : {NAN, INFINITY, -INFINITY, -40.01f, 125.01f, 385.7f}) {
        for (float air : {NAN, -5.0f, 20.0f, 50.0f}) for (bool paused : {false, true}) {
            auto result = evaluate(die, air, paused);
            REQUIRE(result.pause && result.nextPauseRequested == paused && result.unreadable);
            REQUIRE(!result.hot() && !result.cold());
        }
    }
}
int main() {
    const bme280_temp::Calibration calibrations[] = {
        {27504,26435,-1000}, {27504,-26435,1000}, {32000,19000,-12000}, {24000,-19000,-12000}
    };
    const uint32_t raws[] = {0,1,16,415148,519888,600000,0xffff0,0xfffff};
    for (auto cal : calibrations) for (auto raw : raws) {
        double actual = bme280_temp::compensate(raw,cal) / 100.0;
        REQUIRE(std::abs(actual - reference(raw,cal)) < 0.01);
    }
    REQUIRE(bme280_temp::compensate(519888, calibrations[0]) == 2508);
    // Widened integer arithmetic remains defined even outside physical trim ranges.
    REQUIRE(bme280_temp::compensate(0, {65535,-32768,-32768}) == 1);
    auto bus = sensorBus();
    uint8_t words[6]; for (unsigned i=0;i<6;++i) words[i]=bus.regs[0x88+i];
    auto cal = bme280_temp::calibration(words);
    REQUIRE(cal.t1 == 27504 && cal.t2 == 26435 && cal.t3 == -1000);
    REQUIRE(bme280_temp::signedWord(0x8000) == -32768);
    REQUIRE(bme280_temp::signedWord(0xffff) == -1);

    bme280_temp::Sensor sensor;
    int32_t centi = -9999;
    REQUIRE(sensor.trigger(bus, 100));
    REQUIRE((bus.writes == std::vector<std::pair<unsigned,unsigned>>{{0xf4,0},{0xf2,0},{0xf5,0},{0xf4,0x21}}));
    unsigned before = bus.transactions;
    REQUIRE(sensor.waiting(104) && !sensor.finish(bus,104,centi));
    REQUIRE(bus.transactions == before && centi == -9999);
    REQUIRE(!sensor.waiting(105) && sensor.finish(bus,105,centi) && centi == 2508);
    REQUIRE(!sensor.finish(bus,106,centi));
    // Unsigned elapsed timing crosses millis rollover without a blocking wait.
    REQUIRE(sensor.trigger(bus, UINT32_MAX-2));
    REQUIRE(sensor.waiting(1) && !sensor.waiting(2));
    REQUIRE(sensor.finish(bus,2,centi));
    // First error aborts, with no further transactions during that interval.
    for (unsigned fail = 1; fail <= 7; ++fail) {
        auto broken = sensorBus(); broken.failAt = fail;
        bme280_temp::Sensor s;
        REQUIRE(!s.trigger(broken,0));
        REQUIRE(broken.transactions == fail);
        REQUIRE(!s.finish(broken,10,centi));
        broken.failAt = 0;
        REQUIRE(s.trigger(broken,30000) && s.finish(broken,30005,centi));
    }
    for (unsigned bad : {0u,0x58u,0xffu}) {
        auto missing = sensorBus(); missing.regs[0xd0] = uint8_t(bad);
        bme280_temp::Sensor s; REQUIRE(!s.trigger(missing,0));
        REQUIRE(missing.transactions == 1 && missing.writes.empty());
    }
    for (unsigned mode = 0; mode < 4; ++mode) {
        auto broken = sensorBus(); bme280_temp::Sensor s;
        REQUIRE(s.trigger(broken,0));
        if (mode == 0) broken.shortRead = true;
        if (mode == 1) broken.regs[0xf3] = 0x08;
        if (mode == 2) broken.regs[0xf3] = 0x01;
        if (mode == 3) { broken.regs[0xfa] = 0x80; broken.regs[0xfb] = broken.regs[0xfc] = 0; }
        centi = -9999;
        REQUIRE(!s.finish(broken,5,centi) && centi == -9999);
    }
    Pmu p;
    auto power = debug_temperature::samplePower(p,42);
    REQUIRE(power.sampled && power.sampledMs == 42 && power.millivolts == 4020 && power.percent == 85);
    for (int high : {0x0f,0x2f,0x4f,0x6f,0x8f,0xaf,0xcf,0xef}) {
        Pmu q; q.regs[0x34] = high;
        REQUIRE(debug_temperature::samplePower(q,43).millivolts == 4020);
    }
    for (int raw : {0,1,8191}) {
        Pmu q; q.regs[0x34] = raw >> 8; q.regs[0x35] = raw & 255;
        REQUIRE(debug_temperature::samplePower(q,43).millivolts == raw);
    }
    Pmu absent; absent.regs[0x00] = 0;
    REQUIRE(debug_temperature::samplePower(absent,43).millivolts == -1);
    const char* phases[] = {"trickle","pre","cc","cv","done","stop","--","--"};
    for (int status = 0; status < 256; ++status) {
        REQUIRE(debug_temperature::isCharging(status) == ((status >> 5) == 1));
        REQUIRE(std::string(debug_temperature::phase(status)) == phases[status & 7]);
    }
    REQUIRE(!debug_temperature::isCharging(-1));
    REQUIRE(std::string(debug_temperature::phase(-1)) == "--");
    for (uint8_t reg : {0x00,0x01,0xa4,0x34,0x35}) {
        Pmu q; q.regs[reg] = -1;
        auto bad = debug_temperature::samplePower(q,43);
        if (reg == 0x00 || reg == 0x34 || reg == 0x35) REQUIRE(bad.millivolts == -1);
        if (reg == 0x00 || reg == 0xa4) REQUIRE(bad.percent == -1);
        if (reg == 0x01) REQUIRE(bad.status2 == -1);
    }
    testChargeDecision();
    std::printf("PASS temperature: %u checks (compensation, bounded I2C, power reads, two-sensor hysteresis)\n", checks);
}
