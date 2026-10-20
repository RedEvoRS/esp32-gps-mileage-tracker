#include "../04_trip_logger/debug_temperature.h"
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
    std::map<uint8_t,int> regs{{0x50,0x10},{0x30,0x1d},{0x00,0x28},{0x01,0x22},
                               {0xA4,85},{0x34,0x0f},{0x35,0xb4}};
    std::vector<std::pair<uint8_t,uint8_t>> writes;
    unsigned reads = 0, failRead = 0, failWrite = 0, ignoredWrite = 0;
    int readRegister(uint8_t reg) { return ++reads == failRead ? -1 : regs[reg]; }
    int writeRegister(uint8_t reg, uint8_t val) {
        writes.emplace_back(reg,val);
        if (writes.size() == failWrite) return -1;
        if (writes.size() != ignoredWrite) regs[reg] = val;
        return 0;
    }
};
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
    for (unsigned old = 0; old < 256; ++old) {
        Pmu p; p.regs[0x30] = old;
        auto result = debug_temperature::setupTs(p);
        REQUIRE(result.ready && result.control == 0x16 && result.adc == int(old | 2));
        REQUIRE(p.writes.size() == 2 && p.writes[0].first == 0x50 && p.writes[1].first == 0x30);
        REQUIRE((p.writes[0].second & 0x10) != 0);
    }
    for (unsigned fail=1;fail<=4;++fail) {
        Pmu p; p.failRead=fail;
        REQUIRE(!debug_temperature::setupTs(p).ready);
        REQUIRE(p.writes.size() == (fail==1 ? 0u : fail==4 ? 2u : 1u));
    }
    for (unsigned fail=1;fail<=2;++fail) {
        Pmu p; p.failWrite=fail; REQUIRE(!debug_temperature::setupTs(p).ready);
        Pmu q; q.ignoredWrite=fail; REQUIRE(!debug_temperature::setupTs(q).ready);
    }
    REQUIRE(debug_temperature::tsRaw(0x84,0) == 1024); // non-data high bits masked
    REQUIRE(std::abs(debug_temperature::tsVolts(1024) - 0.512) < 0.000001);
    REQUIRE(debug_temperature::tsVolts(0) == 0);
    REQUIRE(std::abs(debug_temperature::tsVolts(8191) - 4.0955) < 0.000001);
    REQUIRE(debug_temperature::tsRaw(-1,0) == -1 && debug_temperature::tsRaw(0,-1) == -1);
    REQUIRE(debug_temperature::tsRaw(0x20,0) == -1 && debug_temperature::tsRaw(0x3f,0xff) == -1);
    REQUIRE(debug_temperature::tsRaw(0xc4,0) == 1024); // both REG36 control bits excluded
    REQUIRE(debug_temperature::tsRaw(0xa0,0) == -1); // sentinel survives control-bit masking
    REQUIRE(std::isnan(debug_temperature::tsVolts(-1)) && std::isnan(debug_temperature::tsVolts(8192)));
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
    std::printf("PASS temperature: %u checks (compensation, bounded I2C, TS setup/read failures)\n", checks);
}
