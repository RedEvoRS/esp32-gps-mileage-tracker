// Real sketch + real parser fixtures. Explicit failures survive NDEBUG too.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
static unsigned checks = 0;
#define REQUIRE(c) do { ++checks; if (!(c)) { std::fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#c); std::exit(1); } } while (0)

static std::map<uint8_t,uint8_t> bmeRegs;
static unsigned bmeTransactions = 0;
static bool bmeMissing = false;
static void prepare() {
    faultWorld(); gDebugMode = true; Wire = TwoWire(); Wire1 = TwoWire();
    gDebugDrawMs = 0;
    bmeTransactions = 0; bmeMissing = false;
    bmeRegs = {{0xd0,0x60},{0xf3,0},{0x88,0x70},{0x89,0x6b},
               {0x8a,0x43},{0x8b,0x67},{0x8c,0x18},{0x8d,0xfc},
               {0xfa,0x7e},{0xfb,0xed},{0xfc,0x00}};
    TwoWire::transaction = [](TwoWire& bus, bool) -> uint8_t {
        REQUIRE(&bus == &Wire && g_currentCore == 1);
        if (bus.address != 0x77) return 0; // OLED remains independent
        ++bmeTransactions;
        if (bmeMissing) return 5;
        if (!bus.tx.empty()) bus.cursor = bus.tx[0];
        if (bus.tx.size() == 2) bmeRegs[bus.cursor] = bus.tx[1];
        return 0;
    };
    TwoWire::request = [](TwoWire& bus, size_t n) -> size_t {
        REQUIRE(&bus == &Wire && bus.address == 0x77 && g_currentCore == 1);
        for (size_t i = 0; i < n; ++i) bus.rx.push_back(bmeRegs[uint8_t(bus.cursor+i)]);
        return n;
    };
    beginDebugDisplay();
    serviceChargeTemp();
    beginDebugTemperature();
    PMU.registerReadOverride[0x01] = 0x22; // actual charging, constant current
    Serial.log.clear();
}
static std::string tempLine() {
    for (auto it = Serial.log.rbegin(); it != Serial.log.rend(); ++it)
        if (it->rfind("Temp:",0) == 0) return *it;
    return "";
}
static void tick() {
    g_millis = gDebugTempLogMs + 30000;
    serviceChargeTemp(); serviceDebugDisplay(); serviceDebugTemperature();
    g_millis += 5; serviceDebugTemperature();
}
int main() {
    prepare();
    REQUIRE(gDebugTempTs.ready && PMU.registerReadOverride[0x50] == 0x16 && PMU.registerReadOverride[0x30] == 0x1f);
    REQUIRE(Wire.timeout == 10 && Wire.sda == 17 && Wire.scl == 18);
    auto operations = PMU.operations.size();
    for (unsigned i=0; i<100; ++i) serviceDebugTemperature();
    REQUIRE(tempLine().empty() && bmeTransactions == 0 && PMU.operations.size() == operations);
    g_millis = gDebugTempLogMs + 29999; serviceDebugTemperature();
    REQUIRE(tempLine().empty() && bmeTransactions == 0);
    g_millis += 1; serviceChargeTemp(); serviceDebugDisplay();
    auto reads = PMU.registerReadCalls;
    unsigned dieCalls = std::count(PMU.operations.begin(),PMU.operations.end(),"getTemperature");
    serviceDebugTemperature(); REQUIRE(tempLine().empty());
    auto starts = bmeTransactions;
    g_millis += 4; serviceDebugTemperature(); REQUIRE(bmeTransactions == starts && tempLine().empty());
    ++g_millis; serviceDebugTemperature();
    REQUIRE(tempLine() == "Temp: die 30.0 C (0 s), guard_est 21.0 C, TS 0.512 V, TS_raw 1024, BME280 25.08 C, chg on, phase cc, batt 4.02 V, pct 85, VBUS 1\n");
    REQUIRE(unsigned(std::count(PMU.operations.begin(),PMU.operations.end(),"getTemperature")) == dieCalls);
    REQUIRE(PMU.registerReadCalls[0x00] == reads[0x00] + 1 && PMU.registerReadCalls[0xa4] == reads[0xa4] + 1);
    auto n = Serial.log.size();
    for (unsigned i=0;i<100;++i) serviceDebugTemperature();
    REQUIRE(Serial.log.size() == n);

    // Each sample is replaced on failure; neither TS nor battery state uses a
    // failed unsigned/bool getter, and no failed die sample reuses its old value.
    PMU.dieC = NAN;
    PMU.registerReadOverride[0x36] = -1;
    PMU.registerReadOverride[0x00] = -1;
    PMU.registerReadOverride[0x01] = -1;
    bmeMissing = true; tick();
    REQUIRE(tempLine() == "Temp: die -- C, guard_est -- C, TS -- V, TS_raw --, BME280 -- C, chg --, phase --, batt -- V, pct --, VBUS --\n");
    REQUIRE(gDebugDisplayReady && !gDebugDisplayFailed); // missing BME never latches OLED fault

    // Sensor recovers on the next 30 s attempt. OLED may already be disabled;
    // temperature logs continue using a bounded, once-per-line power snapshot.
    prepare(); gDebugDisplayReady = false;
    auto before = PMU.registerReadCalls[0x00]; tick();
    REQUIRE(tempLine().find("BME280 25.08 C") != std::string::npos);
    REQUIRE(PMU.registerReadCalls[0x00] == before + 1);
    // Disabled/uninitialized bus is never restarted to obtain a measurement.
    gDebugWireReady = false; starts = bmeTransactions; tick();
    REQUIRE(bmeTransactions == starts && tempLine().find("BME280 -- C") != std::string::npos);

    prepare(); gDebugTempTs.ready = false;
    before = PMU.registerReadCalls[0x36]; tick();
    REQUIRE(PMU.registerReadCalls[0x36] == before && tempLine().find("TS -- V") != std::string::npos);
    // Independent schedules: the logger loses another 100 ms per cycle.
    // Two hours of separate guard/log iterations grow the phase beyond 1 s
    // without ever causing a second die read or blanking the latest sample.
    prepare();
    for (unsigned cycle = 1; cycle <= 240; ++cycle) {
        g_millis = gChargeCheckT0 + CHG_CHECK_MS; serviceChargeTemp();
        dieCalls = std::count(PMU.operations.begin(),PMU.operations.end(),"getTemperature");
        g_millis = gDebugTempLogMs + 30100; serviceDebugTemperature();
        g_millis += 5; serviceDebugTemperature();
        auto age = (g_millis - gDebugTempDieMs) / 1000;
        REQUIRE(tempLine().find("die 30.0 C (" + std::to_string(age) + " s), guard_est 21.0 C") != std::string::npos);
        REQUIRE(unsigned(std::count(PMU.operations.begin(),PMU.operations.end(),"getTemperature")) == dieCalls);
    }
    REQUIRE(g_millis - gDebugTempDieMs > 1000);
    // Boundary, stale sample, and never-sampled guard. BME finishing time is
    // part of the freshness check, rather than the start of its conversion.
    for (uint32_t age : {35000u,35001u}) {
        prepare();
        g_millis = gDebugTempDieMs + age - 5; serviceDebugTemperature();
        g_millis += 5; serviceDebugTemperature();
        REQUIRE(tempLine().find(age == 35000 ? "die 30.0 C (35 s), guard_est 21.0 C" :
                                              "die -- C, guard_est -- C") != std::string::npos);
    }
    prepare(); gChargeChecked = false;
    g_millis = gDebugTempLogMs + 30000; serviceDebugTemperature();
    g_millis += 5; serviceDebugTemperature();
    REQUIRE(tempLine().find("die -- C, guard_est -- C") != std::string::npos);
    // Bit 7 must not turn the 101 upper-bit pattern into charging; reserved
    // direction 11 follows XPowersLib's successful-read "off" behavior.
    for (int status : {0x22,0xa2,0x62}) {
        prepare(); PMU.registerReadOverride[0x01] = status; tick();
        REQUIRE(tempLine().find(status == 0x22 ? "chg on, phase cc" : "chg off, phase cc") != std::string::npos);
    }
    prepare(); g_millis = UINT32_MAX - 100; gDebugTempLogMs = gChargeCheckT0 = g_millis; tick();
    REQUIRE(tempLine().find("die 30.0 C") != std::string::npos);
    // No manually advanced conversion clock: an idle pass starts the forced
    // conversion, sleeps 5 ms, and the next pass consumes it exactly once.
    prepare(); gRtc = diagnostics::RtcRuntime();
    g_millis = gDebugTempLogMs + 30000;
    loop(); REQUIRE(gDebugTempPending && tempLine().empty());
    loop(); REQUIRE(!gDebugTempPending && tempLine().find("BME280 25.08 C") != std::string::npos);
    prepare(); gDegraded.begin(startup::Mode::NoStorage,g_millis);
    operations = PMU.operations.size(); starts = bmeTransactions;
    g_millis += 30000; serviceDebugTemperature();
    REQUIRE(PMU.operations.size() == operations && bmeTransactions == starts && tempLine().empty());
    std::printf("PASS temperature runtime: %u checks (cadence, guard reuse, phase drift, stale/unknown samples, charge decode, degraded)\n",checks);
}
