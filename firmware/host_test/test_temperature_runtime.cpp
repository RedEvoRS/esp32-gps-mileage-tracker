// Real sketch + real parser fixtures. Explicit failures survive NDEBUG too.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
static unsigned checks = 0;
#define REQUIRE(c) do { ++checks; if (!(c)) { std::fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#c); std::exit(1); } } while (0)

static std::map<uint8_t,uint8_t> bmeRegs;
static unsigned bmeTransactions = 0, bmeConversions = 0;
static uint32_t bmeTriggeredMs = 0;
static bool bmeMissing = false;

static void setAirRaw(uint32_t raw) {
    bmeRegs[0xfa] = uint8_t(raw >> 12);
    bmeRegs[0xfb] = uint8_t(raw >> 4);
    bmeRegs[0xfc] = uint8_t(raw << 4);
}

static void prepare() {
    faultWorld(); gDebugMode = false; Wire = TwoWire(); Wire1 = TwoWire();
    gDegraded = startup::DegradedPolicy();
    gDebugDrawMs = 0;
    bmeTransactions = bmeConversions = 0; bmeMissing = false;
    bmeRegs = {{0xd0,0x60},{0xf3,0},{0x88,0x70},{0x89,0x6b},
               {0x8a,0x43},{0x8b,0x67},{0x8c,0x18},{0x8d,0xfc}};
    setAirRaw(519888); // 25.08 C
    TwoWire::transaction = [](TwoWire& bus, bool) -> uint8_t {
        REQUIRE(&bus == &Wire && g_currentCore == 1);
        if (bus.address != 0x77) return 0;
        ++bmeTransactions;
        if (bmeMissing) return 5;
        if (!bus.tx.empty()) bus.cursor = bus.tx[0];
        if (bus.tx.size() == 2) {
            bmeRegs[bus.cursor] = bus.tx[1];
            if (bus.cursor == 0xf4 && bus.tx[1] == 0x21) {
                ++bmeConversions;
                bmeTriggeredMs = g_millis;
            }
        }
        return 0;
    };
    TwoWire::request = [](TwoWire& bus, size_t n) -> size_t {
        REQUIRE(&bus == &Wire && bus.address == 0x77 && g_currentCore == 1);
        if (bus.cursor == 0xfa) REQUIRE(uint32_t(g_millis - bmeTriggeredMs) >= 5);
        for (size_t i = 0; i < n; ++i) bus.rx.push_back(bmeRegs[uint8_t(bus.cursor+i)]);
        return n;
    };
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
    PMU.registerReadOverride[0x01] = 0x22; // actual charging, constant current
}

static std::string tempLine() {
    for (auto it = Serial.log.rbegin(); it != Serial.log.rend(); ++it)
        if (it->rfind("Temp:",0) == 0) return *it;
    return "";
}

static unsigned linesContaining(const char* text) {
    return std::count_if(Serial.log.begin(), Serial.log.end(), [&](const std::string& line) {
        return line.find(text) != std::string::npos;
    });
}

static void chargeTick() {
    g_millis = gChargeCheckT0 + CHG_CHECK_MS;
    serviceChargeTemp();
}

static void logTick() {
    g_millis = gDebugTempLogMs + 30000;
    serviceDebugTemperature();
}

static void testGuard() {
    prepare(); gChargeAirPowered = true;
    serviceChargeTemp(); // Wire has yet to start.
    REQUIRE(bmeTransactions == 0 && Wire.begins == 0 && std::isnan(gChargeAirC));
    REQUIRE(gChargeState == pmu_read::ChargeState::Charging);
    beginProductionDisplay();
    gChargeAirPowered = false;
    chargeTick(); // Wire is ready but the rail setup was unconfirmed.
    REQUIRE(bmeTransactions == 0 && std::isnan(gChargeAirC));
    REQUIRE(Wire.begins == 1 && Wire.ends == 0 && Wire.timeout == 10);
    REQUIRE(Wire.sda == 17 && Wire.scl == 18);

    gChargeAirPowered = true;
    gProductionDisplayReady = false; // Sensor can work independently of OLED.
    setAirRaw(415148); // below zero
    chargeTick(); // BME280 is sampled even with debug off.
    REQUIRE(gChargeAirC < 0 && gChargeState == pmu_read::ChargeState::Paused);
    auto rows = take();
    auto cold = codeRows(rows, 2005);
    REQUIRE(cold.size() == 1 && codeRows(rows, 2004).empty());
    has(cold.front(), "\"transition\":\"raise\"");
    has(cold.front(), "\"context\":{\"temperature_mc\":30000}");
    REQUIRE(linesContaining("Charging paused: board air too cold") == 1);
    REQUIRE(linesContaining(", board air -") == 1);
    REQUIRE(linesContaining("using both sensors") == 1 && bmeConversions == 1);

    setAirRaw(450000); // above zero, below resume threshold
    chargeTick();
    REQUIRE(gChargeAirC > 0 && gChargeAirC < 5 && gChargePauseRequested);
    REQUIRE(take().empty() && linesContaining("using both sensors") == 1);
    setAirRaw(519888);
    chargeTick();
    REQUIRE(!gChargePauseRequested && gChargeState == pmu_read::ChargeState::Charging);
    has(one(2005, "clear"), "\"temperature_mc\":30000");
    REQUIRE(linesContaining("Charging resumed (die 30.0 C, battery ~21 C, board air 25.1 C)") == 1);

    setAirRaw(600000); // above 45 C
    chargeTick();
    has(one(2004, "raise"), "\"temperature_mc\":30000");
    REQUIRE(linesContaining("Charging paused: board air too hot") == 1 && gChargePauseRequested);
    setAirRaw(415148);
    PMU.dieC = 54;
    chargeTick();
    one(2005, "raise"); // die hot and air cold are active together
    REQUIRE(gChargePauseRequested);

    PMU.dieC = 30;
    const auto starts = bmeTransactions;
    const auto conversions = bmeConversions;
    bmeMissing = true;
    chargeTick(); // Failed attempt must discard the prior sub-zero sample.
    REQUIRE(bmeTransactions == starts + 1 && bmeConversions == conversions);
    rows = take();
    REQUIRE(codeRows(rows, 2004).size() == 1 && codeRows(rows, 2005).size() == 1);
    has(codeRows(rows, 2004).front(), "\"transition\":\"clear\"");
    has(codeRows(rows, 2005).front(), "\"transition\":\"clear\"");
    REQUIRE(std::isnan(gChargeAirC) && !gChargePauseRequested);
    REQUIRE(linesContaining("using die only") == 1);
    chargeTick();
    REQUIRE(bmeTransactions == starts + 2 && linesContaining("using die only") == 1);

    bmeMissing = false;
    setAirRaw(0); // below the BME280's allowed range
    chargeTick();
    REQUIRE(std::isnan(gChargeAirC) && !gChargePauseRequested);
    REQUIRE(linesContaining("using die only") == 1 && linesContaining("using both sensors") == 1);
    setAirRaw(519888);
    chargeTick();
    REQUIRE(linesContaining("using both sensors") == 2);
    REQUIRE(Wire.begins == 1 && Wire.ends == 0);
}

static void testUnreadableDieWithAir() {
    for (bool cold : {true, false}) {
        const int code = cold ? 2005 : 2004;
        const int otherCode = cold ? 2004 : 2005;
        const auto paused = pmu_read::ChargeState::Paused;
        prepare(); beginProductionDisplay(); gChargeAirPowered = true;
        serviceChargeTemp();
        REQUIRE(gChargeState == pmu_read::ChargeState::Charging);
        take();

        PMU.dieC = 385.7f;
        setAirRaw(cold ? 415148 : 600000);
        chargeTick();
        REQUIRE(gChargeState == paused && gChargePauseRequested);
        const auto rows = take();
        const auto airFault = codeRows(rows, code);
        const auto unreadable = codeRows(rows, 2006);
        REQUIRE(airFault.size() == 1 && unreadable.size() == 1 && codeRows(rows, otherCode).empty());
        has(airFault.front(), "\"transition\":\"raise\"");
        has(unreadable.front(), "\"transition\":\"raise\"");
        REQUIRE(airFault.front().find("temperature_mc") == std::string::npos);
        REQUIRE(linesContaining("board air available, die unreadable") == 1);
        REQUIRE(linesContaining(cold ? "Charging paused: die unreadable, board air too cold" :
                                       "Charging paused: die unreadable, board air too hot") == 1);
        gDebugMode = true;
        gDebugTempLogMs = g_millis - 30000;
        logTick();
        char temperatures[80];
        std::snprintf(temperatures, sizeof temperatures,
                      "die -- C, guard_est -- C, BME280 %.2f C", gChargeAirC);
        REQUIRE(tempLine().find(temperatures) != std::string::npos);

        setAirRaw(519888); // In-range air cannot clear a pause without a die reading.
        chargeTick();
        REQUIRE(gChargeState == paused && gChargePauseRequested && take().empty());
        bmeMissing = true;
        chargeTick();
        REQUIRE(gChargeState == paused && gChargePauseRequested && take().empty());
        REQUIRE(linesContaining("no usable sensor readings") == 1);

        PMU.dieC = 30;
        bmeMissing = false;
        setAirRaw(cold ? 450000 : 575000); // Between pause and resume thresholds.
        chargeTick();
        REQUIRE(cold ? (gChargeAirC > 0 && gChargeAirC < 5) : (gChargeAirC > 38 && gChargeAirC < 45));
        REQUIRE(gChargeState == paused && gChargePauseRequested);
        one(2006, "clear");
        REQUIRE(linesContaining("using both sensors") == 2);

        setAirRaw(519888);
        chargeTick();
        REQUIRE(gChargeState == pmu_read::ChargeState::Charging && !gChargePauseRequested);
        has(one(code, "clear"), "\"temperature_mc\":30000");
    }
}

static void testUnreadableDieStartupWithAir() {
    prepare(); beginProductionDisplay(); gChargeAirPowered = true;
    PMU.dieC = 385.7f;
    setAirRaw(415148);
    serviceChargeTemp();
    REQUIRE(!gChargeFirstResolved && gChargePauseRequested);
    REQUIRE(gChargeState == pmu_read::ChargeState::Paused && take().empty());

    setAirRaw(450000); // Warmer air still needs the cold resume threshold.
    g_millis = gChargeCheckT0 + CHG_FIRST_RETRY_MS;
    serviceChargeTemp();
    REQUIRE(!gChargeFirstResolved && gChargePauseRequested && take().empty());

    g_millis = gChargeFirstMs + CHG_FIRST_GRACE_MS;
    serviceChargeTemp();
    REQUIRE(gChargeFirstResolved && gChargePauseRequested);
    const auto rows = take();
    REQUIRE(codeRows(rows, 2005).size() == 1 && codeRows(rows, 2006).size() == 1);
    REQUIRE(linesContaining("no plausible die temperature") == 1);
}

static void testDebugLog() {
    prepare();
    beginProductionDisplay(); gChargeAirPowered = true;
    serviceChargeTemp();
    REQUIRE(linesContaining("Charge guard: die 30.0 C (battery ~21 C, board air 25.1 C), charging on.") == 1);
    const auto writes = PMU.registerWriteCalls;
    toggleRuntimeDebug();
    REQUIRE(debugMode() && PMU.registerWriteCalls == writes && linesContaining("Temp setup:") == 0);
    const auto starts = bmeTransactions;
    g_millis = gDebugTempLogMs + 29999; serviceDebugTemperature();
    REQUIRE(tempLine().empty() && bmeTransactions == starts);
    // Change the sensor's registers after the check. Debug must use the cache.
    setAirRaw(415148);
    auto reads = PMU.registerReadCalls;
    const auto dieCalls = std::count(PMU.operations.begin(), PMU.operations.end(), "getTemperature");
    logTick();
    REQUIRE(tempLine() == "Temp: die 30.0 C (30 s), guard_est 21.0 C, BME280 25.08 C, chg on, phase cc, batt 4.02 V, pct 85, VBUS 1\n");
    REQUIRE(bmeTransactions == starts);
    REQUIRE(std::count(PMU.operations.begin(), PMU.operations.end(), "getTemperature") == dieCalls);
    REQUIRE(!PMU.registerReadCalls.count(0x36) && !PMU.registerReadCalls.count(0x37));
    REQUIRE(PMU.registerReadCalls[0x00] == reads[0x00] + 1 && PMU.registerReadCalls[0xa4] == reads[0xa4] + 1);
    for (unsigned i = 0; i < 100; ++i) serviceDebugTemperature();
    REQUIRE(linesContaining("Temp:") == 1);

    for (uint32_t age : {35000u, 35001u}) {
        g_millis = gDebugTempDieMs + age;
        gDebugTempLogMs = g_millis - 30000;
        serviceDebugTemperature();
        REQUIRE(tempLine().find(age == 35000 ? "die 30.0 C (35 s), guard_est 21.0 C, BME280 25.08 C" :
                                              "die -- C, guard_est -- C, BME280 -- C") != std::string::npos);
        REQUIRE(bmeTransactions == starts);
    }

    // Failed values replace both cached temperatures. Failed power reads
    // remain unknown instead of appearing as valid unsigned/boolean values.
    PMU.dieC = 385.7f;
    PMU.registerReadOverride[0x00] = -1;
    PMU.registerReadOverride[0x01] = -1;
    bmeMissing = true;
    g_millis += CHG_CHECK_MS;
    serviceChargeTemp();
    logTick();
    REQUIRE(tempLine() == "Temp: die -- C, guard_est -- C, BME280 -- C, chg --, phase --, batt -- V, pct --, VBUS --\n");
    REQUIRE(gChargeState == pmu_read::ChargeState::Paused && !gChargePauseRequested);
    one(2006, "raise");

    gDegraded.begin(startup::Mode::NoStorage, g_millis);
    const auto operations = PMU.operations.size();
    const auto line = tempLine();
    logTick();
    REQUIRE(PMU.operations.size() == operations && tempLine() == line);
    chargeTick();
    REQUIRE(bmeTransactions == starts + 2); // guard still samples in no-storage mode
}

static void testDebugTiming() {
    prepare(); gDebugMode = true;
    logTick(); // No guard sample has been taken.
    REQUIRE(tempLine().find("die -- C, guard_est -- C, BME280 -- C") != std::string::npos);
    REQUIRE(bmeTransactions == 0);

    prepare(); beginProductionDisplay(); gChargeAirPowered = true;
    g_millis = UINT32_MAX - 100;
    serviceChargeTemp();
    toggleRuntimeDebug();
    gRtc = diagnostics::RtcRuntime();
    g_millis = gDebugTempLogMs + 30000;
    loop(); // The rollover-safe guard check and debug log complete in this pass.
    REQUIRE(bmeConversions == 2);
    REQUIRE(tempLine().find("die 30.0 C (0 s), guard_est 21.0 C, BME280 25.08 C") != std::string::npos);
    for (int status : {0xa2, 0x62}) {
        PMU.registerReadOverride[0x01] = status; logTick();
        REQUIRE(tempLine().find("chg off, phase cc") != std::string::npos);
    }
    REQUIRE(bmeConversions == 2);
}

int main() {
    testGuard();
    testUnreadableDieWithAir();
    testUnreadableDieStartupWithAir();
    testDebugLog();
    testDebugTiming();
    std::printf("PASS temperature runtime: %u checks (guard ownership, fault transitions, fallback, debug reuse and TS untouched)\n", checks);
}
