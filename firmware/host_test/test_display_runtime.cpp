// Real sketch, real GPS parser and accepted diagnostic episode publication.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
static unsigned cases = 0;
int main() {
    faultWorld(); setupPmuIrq();
    assert(bool(PMU.enabledIrqs & XPOWERS_AXP2101_PKEY_SHORT_IRQ) == !DISPLAY_DEBUG);
    assert(PMU.enabledIrqs == PMU_ENABLED_IRQS);
    // INTSTS2 bit 3 is the real short-key latch; no private library cache.
    PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x08;
    gPmuIrqFlag = true; pollPmu();
    assert(std::find(PMU.operations.begin(), PMU.operations.end(), "isPekeyShortPressIrq") == PMU.operations.end());
#if !DISPLAY_DEBUG
    assert(gDisplayUserRequest);
    gState = ACTIVE; serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::Blank); // adapter wasn't initialized
    beginProductionDisplay(); serviceProductionDisplay();
    gDisplayUserRequest = true; serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::StatusCard);
#endif
    ++cases;
    // Any unreadable INTSTS byte invalidates the entire synthetic all-flags
    // batch. No card, shutdown, power observation, journal event or clear.
    for (uint8_t failed : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3}) {
        faultWorld(); setupPmuIrq(); PMU.operations.clear();
        for (uint8_t reg : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3})
            PMU.registerReadOverride[reg] = 0xff;
        PMU.failRegisterReadAt[failed] = PMU.registerReadCalls[failed] + 1;
        gPmuIrqFlag = true; pollPmu();
        assert(gPmuIrqRetry && !PMU.shutdownCalls && !gVbusOffPending && !gVbusLost && take().empty());
        assert(std::find(PMU.operations.begin(), PMU.operations.end(), "clearIrqStatus") == PMU.operations.end());
#if !DISPLAY_DEBUG
        assert(!gDisplayUserRequest);
#endif
        ++cases;
    }
    // A known short key must not request a production card while degraded.
    faultWorld(); gDegraded.begin(startup::Mode::NoStorage, g_millis);
    PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x08;
    gPmuIrqFlag = true; pollPmu();
#if !DISPLAY_DEBUG
    assert(!gDisplayUserRequest);
#endif
    gDegraded = startup::DegradedPolicy(); ++cases;
    // Long-key INTSTS2 bit 2 keeps its USB-ignore/battery-shutdown meaning.
    for (bool usb : {true, false}) {
        faultWorld(); PMU.vbusIn = usb;
        PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x04;
        gPmuIrqFlag = true;
        bool off = false;
        try { pollPmu(); } catch (const PoweredOff&) { off = true; }
        assert(off == !usb && PMU.shutdownCalls == (usb ? 0 : 1));
#if !DISPLAY_DEBUG
        assert(!gDisplayUserRequest);
#endif
        ++cases;
    }
    // Successful battery shutdown leaves the filesystem mutex held forever;
    // the stub throws instead of removing power. Reset that simulated boot.
    g_lockDepth = 0;
    gJournalSealRequested = gJournalSealDone = false;
#if !DISPLAY_DEBUG
    faultWorld(); beginProductionDisplay();
    diagnosticFix(1000, 100, 0); serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::StatusCard);
    g_millis += display::DisplayModel::kDurationMs; serviceProductionDisplay(); assert(!gProductionLastOutput.powered);
    // Stale speed is Unknown and cannot consume a newly queued boot status.
    g_millis += 2501; // exceed speed freshness even with a short card setting
    gDisplayModel = display::DisplayModel(); serviceProductionDisplay();
    assert(!gProductionLastOutput.powered);
    // VBUS absence is Parked even without a trusted/fresh fix.
    gVbusLost = true; serviceProductionDisplay(); assert(gProductionLastOutput.powered); ++cases;

    faultWorld(); beginProductionDisplay(); gState = ACTIVE;
    gLoopFaults.event(4003, g_millis).condition(true);
    gNetFaults.event(5004, g_millis).enumeration("target", "trip").integer("http_status", 401).condition(true);
    serviceProductionDisplay();
    assert(gProductionLastOutput.errorCode == 4003);
    g_millis += display::DisplayModel::kDurationMs; serviceProductionDisplay();
    assert(gProductionLastOutput.errorCode == 5004);
    g_millis += display::DisplayModel::kDurationMs; serviceProductionDisplay(); assert(!gProductionLastOutput.powered);
    // The trip-network snapshot is read on core 1 as well as core-1 faults.
    assert(gDisplayConditionCounts[1] && gDisplayConditions[1][3].episode);
    ++cases;

    faultWorld();
    unsigned transfers = 0;
    TwoWire::transaction = [&](TwoWire& bus, bool) -> uint8_t { if (&bus == &Wire) { ++transfers; return 5; } return 0; };
    beginProductionDisplay(); assert(gProductionDisplayFailed && !gProductionDisplayReady);
    serviceRuntimeDiagnostics();
    auto rows = take(); assert(codeRows(rows, 6001).size() == 1);
    has(codeRows(rows, 6001)[0], "\"transition\":\"raise\"");
    for (int i = 0; i < 100; ++i) { beginProductionDisplay(); serviceProductionDisplay(); serviceRuntimeDiagnostics(); }
    assert(transfers == 1 && codeRows(take(), 6001).empty());
    // A disconnected display cannot stop a real drive or alter GPX writes.
    run(15, 40); run(0, 185);
    assert(gState == IDLE && !g_sdDisk.files.empty());
    bool completeTrip = false;
    for (const auto& file : g_sdDisk.files)
        completeTrip |= file.first.rfind("/trips/", 0) == 0 && file.second.find("</gpx>") != std::string::npos;
    assert(completeTrip);
    assert(transfers == 1 && !g_lockDepth);
    TwoWire::transaction = nullptr; ++cases;

    faultWorld(); beginProductionDisplay(); gDisplayUserRequest = true; serviceProductionDisplay();
    assert(gProductionLastOutput.powered);
    PMU.vbusIn = false; gVbusLost = true;
    try { powerOffForVbus(); assert(false); } catch (const PoweredOff&) {}
    assert(!gProductionLastOutput.powered && gProductionOled.sleeps >= 2); ++cases;
#endif
    std::printf("test_display_runtime: %u scenarios passed (DISPLAY_DEBUG=%d)\n", cases, DISPLAY_DEBUG);
}
