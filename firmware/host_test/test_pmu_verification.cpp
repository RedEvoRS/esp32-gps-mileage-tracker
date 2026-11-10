// Real sketch paths and diagnostic journal, with register-level PMU failures.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
using pmu_read::ChargeState;
static unsigned cases = 0;
static void pmuWorld() {
    faultWorld();
    gDegraded = startup::DegradedPolicy();
    gVbusOffPending = false;
    gJournalSealRequested = gJournalSealDone = false;
    g_lockDepth = 0;
}
static void failRead(uint8_t reg) {
    PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg] + 1;
}
static unsigned calls(const char* operation) {
    return std::count(PMU.operations.begin(), PMU.operations.end(), operation);
}
static void chargeTick() { g_millis += CHG_CHECK_MS; serviceChargeTemp(); }

int main() {
    for (float value : {NAN, INFINITY, -INFINITY, -40.01f, 125.01f, 385.7f})
        assert(!charge_temperature::plausible(value));
    for (float value : {-40.0f, 30.0f, 60.0f, 125.0f})
        assert(charge_temperature::plausible(value));
    // A real sketch read/command/read-back on each entry path. A failed first
    // conversion never becomes a hot/cold observation or a debug measurement.
    for (bool degraded : {false, true}) {
        pmuWorld(); PMU.dieC = 385.7f;
        auto bootRows = []() { FS_LOCK(); auto rows = take(); FS_UNLOCK(); return rows; };
        if (degraded) enterDegraded(startup::Mode::NoStorage);
        else setup();
        assert(!PMU.chargeEnabled && gChargeState == ChargeState::Paused);
        assert(std::isnan(gDebugTempDie));
        auto rows = bootRows();
        for (int code : {2004, 2005, 2006}) assert(codeRows(rows, code).empty());
        for (const auto& line : Serial.log) assert(line.find("too hot") == std::string::npos);
        auto n = calls("getTemperature");
        g_millis += CHG_FIRST_RETRY_MS - 1; serviceChargeTemp();
        assert(calls("getTemperature") == n);
        PMU.dieC = 33; ++g_millis;
        if (degraded) degradedLoop(); else serviceChargeTemp();
        assert(PMU.chargeEnabled && gChargeState == ChargeState::Charging);
        rows = bootRows();
        for (int code : {2004, 2005, 2006}) assert(codeRows(rows, code).empty());
        ++cases;
    }
    pmuWorld(); PMU.dieC = 385.7f; serviceChargeTemp();
    for (uint32_t elapsed = CHG_FIRST_RETRY_MS; elapsed < CHG_FIRST_GRACE_MS; elapsed += CHG_FIRST_RETRY_MS) {
        g_millis += CHG_FIRST_RETRY_MS; serviceChargeTemp();
        assert(take().empty() && !PMU.chargeEnabled);
    }
    g_millis += CHG_FIRST_RETRY_MS; serviceChargeTemp();
    auto unreadable = one(2006, "raise");
    assert(unreadable.find("temperature_mc") == std::string::npos);
    auto n = calls("getTemperature");
    g_millis += CHG_CHECK_MS - 1; serviceChargeTemp(); assert(calls("getTemperature") == n);
    PMU.dieC = 33; ++g_millis; serviceChargeTemp(); one(2006, "clear");
    assert(PMU.chargeEnabled); ++cases;
    // A dead sensor can outlive millis() rollover. Exhausted startup grace
    // must never reopen even if elapsed time again resembles the first second.
    pmuWorld(); PMU.dieC = NAN; serviceChargeTemp();
    g_millis += CHG_FIRST_GRACE_MS; serviceChargeTemp(); one(2006, "raise");
    assert(gChargeFirstResolved);
    g_millis = gChargeFirstMs + 1; serviceChargeTemp(); // one full wrap later
    auto afterWrapReads = calls("getTemperature");
    g_millis += CHG_FIRST_RETRY_MS; serviceChargeTemp();
    assert(calls("getTemperature") == afterWrapReads && !PMU.chargeEnabled); ++cases;
    // Runtime garbage preserves an existing hot episode until a plausible
    // sample can clear it; no false temperature is attached to E2006.
    pmuWorld(); PMU.dieC = 60; serviceChargeTemp(); one(2004, "raise");
    assert(!PMU.chargeEnabled);
    PMU.dieC = 385.7f; chargeTick(); unreadable = one(2006, "raise");
    assert(unreadable.find("temperature_mc") == std::string::npos && std::isnan(gDebugTempDie));
    PMU.dieC = 33; chargeTick(); auto recovery = take();
    assert(codeRows(recovery, 2004).size() == 1 && codeRows(recovery, 2006).size() == 1);
    assert(PMU.chargeEnabled); ++cases;
    pmuWorld(); serviceChargeTemp(); assert(PMU.chargeEnabled && calls("enableCellbatteryCharge") == 1); ++cases;
    pmuWorld(); g_millis = UINT32_MAX - 100; PMU.dieC = NAN; serviceChargeTemp();
    g_millis += CHG_FIRST_GRACE_MS; serviceChargeTemp(); one(2006, "raise"); ++cases;
    // Every byte/mask, including the production-only short key, follows XPowersLib's
    // INTSTS byte order and configured enable mask. Unknown suppresses ALL bits.
    for (int byte = 0; byte <= 255; ++byte) {
        auto batch = pmu_read::irqBatch(byte, byte, byte, PMU_ENABLED_IRQS);
        assert(batch.known);
        for (unsigned bit = 0; bit < 24; ++bit)
            assert(batch.has(1u << bit) == (bool(unsigned(byte) & (1u << (bit % 8))) &&
                   bool(PMU_ENABLED_IRQS & (1u << bit))));
        for (auto unknown : {pmu_read::irqBatch(-1, byte, byte, 0xffffff),
                             pmu_read::irqBatch(byte, -1, byte, 0xffffff),
                             pmu_read::irqBatch(byte, byte, -1, 0xffffff)}) {
            assert(!unknown.known && !unknown.has(0xffffff));
            assert(!unknown.has(XPOWERS_AXP2101_PKEY_SHORT_IRQ));
        }
        assert(pmu_read::chargeState(byte) == ((byte & 2) ? ChargeState::Charging : ChargeState::Paused));
    }
    assert(pmu_read::chargeState(-1) == ChargeState::Unconfirmed); ++cases;

    for (uint8_t reg : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3}) {
        pmuWorld(); PMU.vbusIn = false;
        PMU.longPressIrq = PMU.shortPressIrq = PMU.lowIrq = PMU.criticalIrq = true;
        PMU.removeIrq = PMU.insertIrq = true;
        gPmuIrqFlag = true; failRead(reg); pollPmu();
        assert(take().empty() && !PMU.shutdownCalls && !gVbusOffPending && !gVbusLost);
        assert(calls("clearIrqStatus") == 0 && calls("getIrqStatus") == 0);
        assert(gPmuIrqRetry && PMU.longPressIrq && PMU.criticalIrq);
        auto reads = PMU.registerReadCalls;
        for (unsigned i = 0; i < 100; ++i) { gPmuIrqFlag = true; pollPmu(); }
        assert(PMU.registerReadCalls == reads);
        g_millis += PMU_IRQ_RETRY_MS - 1; pollPmu(); assert(PMU.registerReadCalls == reads);
        ++g_millis; failRead(reg); pollPmu();
        assert(take().empty() && !PMU.shutdownCalls && calls("clearIrqStatus") == 0);
        // No new falling edge: the retained batch still gets another attempt.
        PMU.longPressIrq = PMU.criticalIrq = false;
        gPmuIrqFlag = false; g_millis += PMU_IRQ_RETRY_MS; pollPmu();
        one(2002, "raise");
        assert(gVbusOffPending && !gPmuIrqRetry && calls("clearIrqStatus") == 1);
        pollPmu(); assert(take().empty() && calls("clearIrqStatus") == 1); ++cases;

        // Real long key/critical IRQ survives each possible failed byte and
        // still reaches the battery-only shutdown path on a successful retry.
        for (bool critical : {false, true}) {
            pmuWorld(); PMU.vbusIn = false;
            PMU.longPressIrq = !critical; PMU.criticalIrq = critical;
            gPmuIrqFlag = true; failRead(reg); pollPmu();
            assert(!PMU.shutdownCalls && take().empty());
            g_millis += PMU_IRQ_RETRY_MS;
            bool off = false;
            try { pollPmu(); } catch (const PoweredOff&) { off = true; }
            assert(off && PMU.shutdownCalls == 1);
            if (critical) one(2003, "raise");
            ++cases;
        }
    }
    pmuWorld(); g_millis = UINT32_MAX - 49; gPmuIrqFlag = true;
    failRead(XPOWERS_AXP2101_INTSTS2); pollPmu();
    auto reads = PMU.registerReadCalls;
    g_millis += 99; pollPmu(); assert(PMU.registerReadCalls == reads);
    ++g_millis; pollPmu(); assert(!gPmuIrqRetry && calls("clearIrqStatus") == 1); ++cases;

    // An edge arriving during a read must survive consumption of the old edge.
    pmuWorld(); gPmuIrqFlag = true;
    PMU.onRegisterRead = [](uint8_t reg) { if (reg == XPOWERS_AXP2101_INTSTS2) onPmuIrq(); };
    pollPmu(); assert(gPmuIrqFlag);
    PMU.onRegisterRead = nullptr; pollPmu(); assert(!gPmuIrqFlag && calls("clearIrqStatus") == 2); ++cases;

    // No false raise or false clear while the threshold register is unreadable.
    for (bool active : {false, true}) {
        pmuWorld();
        if (active) { batteryCondition(2002, true, g_millis); batteryCondition(2003, true, g_millis); take(); }
        PMU.batteryPercent = active ? 100 : 0;
        failRead(XPOWERS_AXP2101_LOW_BAT_WARN_SET); serviceBatteryDiagnostics();
        assert(gBatteryThresholds < 0 && take().empty());
        g_millis += 30000; serviceBatteryDiagnostics();
        auto rows = take();
        for (int code : {2002, 2003}) {
            auto r = codeRows(rows, code); assert(r.size() == 1);
            has(r[0], active ? "\"transition\":\"clear\"" : "\"transition\":\"raise\"");
        }
        auto n = PMU.registerReadCalls[XPOWERS_AXP2101_LOW_BAT_WARN_SET];
        failRead(XPOWERS_AXP2101_LOW_BAT_WARN_SET);
        g_millis += 30000; serviceBatteryDiagnostics();
        assert(PMU.registerReadCalls[XPOWERS_AXP2101_LOW_BAT_WARN_SET] == n && take().empty()); ++cases;
    }
    // Successful decoding over every nibble pair; test inclusive boundaries.
    for (int raw = 0; raw <= 255; ++raw)
        for (int code : {2002, 2003}) {
            pmuWorld(); PMU.lowWarnNibble = raw >> 4; PMU.criticalPercent = raw & 15;
            PMU.batteryPercent = code == 2002 ? (raw >> 4) + 5 : raw & 15;
            serviceBatteryDiagnostics(); one(code, "raise");
            PMU.batteryPercent++; g_millis += 30000; serviceBatteryDiagnostics(); one(code, "clear");
        }
    ++cases;

    // Disable write failure, read failure, stale enabled read-back, and failed
    // resume: none can confirm the intended state. Three bounded failures raise
    // E2008; a later matching read-back clears that same episode.
    for (unsigned failure = 0; failure < 4; ++failure) {
        pmuWorld(); bool resume = failure == 3;
        if (resume) { PMU.dieC = CHG_HOT_PAUSE_C; serviceChargeTemp(); one(2004, "raise"); }
        PMU.dieC = resume ? 30 : CHG_HOT_PAUSE_C;
        PMU.failChargeDisable = failure == 0;
        PMU.failChargeRead = failure == 1;
        if (failure == 2) PMU.registerReadOverride[XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL] = 2;
        PMU.failChargeEnable = resume;
        const char* command = resume ? "enableCellbatteryCharge" : "disableCellbatteryCharge";
        for (unsigned attempt = 1; attempt <= 3; ++attempt) {
            if (attempt == 1 && !resume) serviceChargeTemp(); else chargeTick();
            assert(gChargeState == ChargeState::Unconfirmed && gChargeFailures == attempt);
            auto rows = take();
            if (attempt < 3) assert(codeRows(rows, 2008).empty());
            else { auto r = codeRows(rows, 2008); assert(r.size() == 1); has(r[0], "\"transition\":\"raise\""); }
            unsigned n = calls(command);
            for (unsigned spin = 0; spin < 50; ++spin) serviceChargeTemp();
            assert(calls(command) == n && take().empty());
        }
        assert(calls(command) == 3);
        assert(std::none_of(Serial.log.begin(), Serial.log.end(), [resume](const auto& line) {
            return line.find(resume ? "Charging resumed" : "Charging paused") != std::string::npos;
        }));
        PMU.failChargeDisable = PMU.failChargeRead = PMU.failChargeEnable = false;
        PMU.registerReadOverride.clear(); chargeTick(); one(2008, "clear");
        assert(gChargeState == (resume ? ChargeState::Charging : ChargeState::Paused));
        assert(gChargeFailures == 0 && PMU.chargeEnabled == resume);
        auto n = calls(command); chargeTick(); assert(calls(command) == n && take().empty()); ++cases;
    }
    // Failed pause retains hysteresis; a cooler-but-not-yet-safe reading must
    // retry disable, never enable. Unreadable temperature does the same.
    pmuWorld(); PMU.dieC = CHG_HOT_PAUSE_C; PMU.failChargeDisable = true;
    serviceChargeTemp(); one(2004, "raise");
    PMU.dieC = CHG_HOT_RESUME_C + 1; chargeTick();
    assert(calls("disableCellbatteryCharge") == 2 && calls("enableCellbatteryCharge") == 0);
    PMU.dieC = INFINITY; chargeTick(); auto rows = take();
    assert(codeRows(rows, 2006).size() == 1 && codeRows(rows, 2008).size() == 1);
    assert(gChargeState == ChargeState::Unconfirmed && gChargePauseRequested); ++cases;
    // Retry timer remains bounded through tick rollover, including first-command failure.
    pmuWorld(); g_millis = UINT32_MAX - 100; PMU.failChargeRead = true; serviceChargeTemp();
    g_millis += CHG_CHECK_MS - 1; serviceChargeTemp(); assert(calls("enableCellbatteryCharge") == 1);
    ++g_millis; serviceChargeTemp(); assert(calls("enableCellbatteryCharge") == 2); ++cases;
    std::printf("PMU verification: %u scenarios plus exhaustive IRQ/charge/threshold bytes passed (runtime debug=%d).\n", cases, debugMode());
}
