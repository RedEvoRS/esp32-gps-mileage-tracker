// Real sketch, real GPS parser and accepted diagnostic episode publication.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
static unsigned cases = 0;
static bool bootLow = false;
static void shortTap() {
    PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x08;
    gPmuIrqFlag = true; pollPmu();
    PMU.registerReadOverride.erase(XPOWERS_AXP2101_INTSTS2);
}
static void holdBoot() {
    bootLow = true; pollPmu();
    g_millis += runtime_debug::BootButton::kDebounceMs;
}
static void toggleRegressions() {
    faultWorld(); gDebugMode = false; beginProductionDisplay();
    gState = ACTIVE;
    g_digitalReadHook = [](int pin) { assert(pin == DEBUG_BOOT_PIN); return bootLow ? LOW : HIGH; };
    // BOOT alone and a bouncing low never toggle. POWER alone is a card.
    bootLow = true; pollPmu(); ++g_millis; bootLow = false; pollPmu();
    bootLow = true; pollPmu(); g_millis += 39; shortTap();
    assert(!debugMode() && gDisplayUserRequest);
    bootLow = false; serviceDisplay();
    assert(gProductionLastOutput.powered);
    holdBoot(); shortTap();
    assert(debugMode() && gModeFeedback && !gDisplayUserRequest);
    assert(gDisplayOwner == DisplayOwner::Feedback && logCount("Debug mode: on") == 1);
    assert(gProductionOled.text.size() >= 2 && gProductionOled.text[0].value == "Debug ON");
    std::string shownVersion;
    for (size_t i = 1; i < gProductionOled.text.size(); ++i)
        shownVersion += gProductionOled.text[i].value;
    assert(shownVersion == firmware::kVersion);
    assert(!gDebugDisplayBegun && gDebugTempEnabled);
    auto productionFrames = gProductionOled.frames;
    g_millis += DEBUG_FEEDBACK_MS - 1; serviceDisplay();
    assert(gDisplayOwner == DisplayOwner::Feedback && !gDebugDisplayBegun);
    ++g_millis; serviceDisplay();
    assert(gDisplayOwner == DisplayOwner::Debug && gDebugDisplayReady);
    assert(gDebugOled.frames == 1 && gProductionOled.frames == productionFrames);
    assert(Wire.begins == 1); // shared bus is never initialized by the second owner
    auto debugFrames = gDebugOled.frames;
    // A released BOOT must be read at this batch, even if the last sample was low.
    bootLow = false; shortTap();
    assert(debugMode() && gDisplayUserRequest);
    serviceDisplay();
    assert(gDisplayOwner == DisplayOwner::Production && gProductionLastOutput.powered);
    assert(gDebugOled.frames == debugFrames);
    g_millis += display::DisplayModel::kDurationMs; serviceDisplay();
    assert(gDisplayOwner == DisplayOwner::Debug && gDebugOled.frames == debugFrames + 1);
    // Observe error and trip-end episodes in debug, then toggle off promptly:
    // neither an old card nor a fault seen in the hidden model can replay.
    gLoopFaults.event(4003, g_millis).condition(true);
    gDisplayTripFinalized = true; serviceDisplay();
    holdBoot(); shortTap();
    assert(!debugMode() && gModeFeedback && logCount("Debug mode: off") == 1);
    assert(gProductionOled.text[0].value == "Debug OFF");
    debugFrames = gDebugOled.frames;
    g_millis += DEBUG_FEEDBACK_MS; serviceDisplay();
    assert(gDisplayOwner == DisplayOwner::Production && !gProductionLastOutput.powered);
    assert(gDebugOled.frames == debugFrames);
    auto reads = PMU.operations.size(); serviceDebugTemperature();
    assert(PMU.operations.size() == reads); // no debug measurement when off
    // A genuinely new episode while feedback is visible is still due afterward.
    holdBoot(); shortTap(); g_millis += DEBUG_FEEDBACK_MS; serviceDisplay();
    holdBoot(); shortTap();
    gLoopFaults.event(4002, g_millis).condition(true);
    g_millis += DEBUG_FEEDBACK_MS; serviceDisplay();
    assert(gProductionLastOutput.errorCode == 4002);
    g_digitalReadHook = nullptr; bootLow = false; ++cases;

    // Low debounce is rollover-safe, and high at the short batch cancels it.
    runtime_debug::BootButton button;
    button.sample(UINT32_MAX - 20, true);
    assert(!button.held(18) && button.held(19));
    button.sample(19, false); assert(!button.held(100)); ++cases;

    // A checked-batch read failure cannot toggle, even with BOOT held.
    for (uint8_t failed : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3}) {
        faultWorld(); gDebugMode = false;
        g_digitalReadHook = [](int) { return LOW; };
        pollPmu(); g_millis += 40;
        PMU.failRegisterReadAt[failed] = PMU.registerReadCalls[failed] + 1;
        shortTap();
        assert(gPmuIrqRetry && !debugMode() && !gModeFeedback && !gDisplayUserRequest);
        g_digitalReadHook = nullptr; ++cases;
    }

    // A handoff failure cannot be retried via the other adapter
    // during subsequent toggles. All transfers still belong to core 1/Wire.
    faultWorld(); gDebugMode = false; beginProductionDisplay();
    g_digitalReadHook = [](int) { return LOW; };
    pollPmu(); g_millis += 40; shortTap();
    unsigned attempts = 0;
    TwoWire::transaction = [&](TwoWire& bus, bool) -> uint8_t {
        assert(&bus == &Wire && g_currentCore == 1); ++attempts; return 5;
    };
    g_millis += DEBUG_FEEDBACK_MS; serviceDisplay();
    assert(gDebugDisplayFailed || gProductionDisplayFailed);
    unsigned failedAttempts = attempts;
    for (unsigned i = 0; i < 5; ++i) {
        shortTap(); g_millis += DEBUG_FEEDBACK_MS; serviceDisplay();
    }
    assert(attempts == failedAttempts);
    TwoWire::transaction = nullptr; g_digitalReadHook = nullptr; ++cases;

    // Degraded mode ignores even a fully debounced BOOT+POWER chord.
    for (auto mode : {startup::Mode::PmuMissing, startup::Mode::PmuUntrusted, startup::Mode::NoStorage}) {
        faultWorld(); gDebugMode = false; gDegraded.begin(mode, g_millis);
        g_digitalReadHook = [](int) { return LOW; };
        pollPmu(); g_millis += 40; shortTap();
        assert(!debugMode() && !gModeFeedback && !gDisplayUserRequest);
        g_digitalReadHook = nullptr; gDegraded = startup::DegradedPolicy(); ++cases;
    }
    // The BOOT modifier never changes a long press, in either runtime mode.
    for (bool debug : {false, true}) for (bool usb : {false, true}) {
        faultWorld(); gDebugMode = debug; PMU.vbusIn = usb;
        g_digitalReadHook = [](int) { return LOW; };
        pollPmu(); g_millis += 40;
        PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x04;
        gPmuIrqFlag = true; bool off = false;
        try { pollPmu(); } catch (const PoweredOff&) { off = true; }
        assert(off == !usb && debugMode() == debug && !gModeFeedback);
        g_lockDepth = 0; g_digitalReadHook = nullptr; ++cases;
    }
    // A new boot discards an enabled mode and initializes GPIO0 only as input.
    faultWorld(); gDebugMode = true; setup();
    assert(!debugMode() && g_pinModes.at(DEBUG_BOOT_PIN) == INPUT_PULLUP);
    assert(!g_pinLevels.count(DEBUG_BOOT_PIN) && !gDebugDisplayBegun && !gDebugTempEnabled); ++cases;
}
int main() {
    assert(!debugMode());
    faultWorld(); gDebugMode = false; setupPmuIrq();
    assert(bool(PMU.enabledIrqs & XPOWERS_AXP2101_PKEY_SHORT_IRQ) == true);
    assert(PMU.enabledIrqs == PMU_ENABLED_IRQS);
    // INTSTS2 bit 3 is the real short-key latch; no private library cache.
    PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x08;
    gPmuIrqFlag = true; pollPmu();
    assert(std::find(PMU.operations.begin(), PMU.operations.end(), "isPekeyShortPressIrq") == PMU.operations.end());
    assert(gDisplayUserRequest);
    gState = ACTIVE; serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::Blank); // adapter wasn't initialized
    beginProductionDisplay(); serviceProductionDisplay();
    gDisplayUserRequest = true; serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::StatusCard);
    ++cases;
    // Any unreadable INTSTS byte invalidates the entire synthetic all-flags
    // batch. No card, shutdown, power observation, journal event or clear.
    for (uint8_t failed : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3}) {
        faultWorld(); gDebugMode = false; setupPmuIrq(); PMU.operations.clear();
        for (uint8_t reg : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3})
            PMU.registerReadOverride[reg] = 0xff;
        PMU.failRegisterReadAt[failed] = PMU.registerReadCalls[failed] + 1;
        gPmuIrqFlag = true; pollPmu();
        assert(gPmuIrqRetry && !PMU.shutdownCalls && !gVbusOffPending && !gVbusLost && take().empty());
        assert(std::find(PMU.operations.begin(), PMU.operations.end(), "clearIrqStatus") == PMU.operations.end());
        assert(!gDisplayUserRequest);
        ++cases;
    }
    // A known short key must not request a production card while degraded.
    faultWorld(); gDebugMode = false; gDegraded.begin(startup::Mode::NoStorage, g_millis);
    PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x08;
    gPmuIrqFlag = true; pollPmu();
    assert(!gDisplayUserRequest);
    gDegraded = startup::DegradedPolicy(); ++cases;
    // Long-key INTSTS2 bit 2 keeps its USB-ignore/battery-shutdown meaning.
    for (bool usb : {true, false}) {
        faultWorld(); gDebugMode = false; PMU.vbusIn = usb;
        PMU.registerReadOverride[XPOWERS_AXP2101_INTSTS2] = 0x04;
        gPmuIrqFlag = true;
        bool off = false;
        try { pollPmu(); } catch (const PoweredOff&) { off = true; }
        assert(off == !usb && PMU.shutdownCalls == (usb ? 0 : 1));
        assert(!gDisplayUserRequest);
        ++cases;
    }
    // Successful battery shutdown leaves the filesystem mutex held forever;
    // the stub throws instead of removing power. Reset that simulated boot.
    g_lockDepth = 0;
    gJournalSealRequested = gJournalSealDone = false;
    // The indoor boot card wakes without a fix; fresh GSV and warning changes
    // repaint it while weak NAV-PVT accuracy still leaves motion Unknown.
    faultWorld(); gDebugMode = false; beginProductionDisplay(); serviceDisplay();
    assert(!gHaveLastFix && gProductionLastOutput.card == display::Card::StatusCard);
    assert(gProductionOled.text[0].value == "Sats: --");
    const uint32_t indoorEnd = gProductionLastOutput.until;
    diagnosticFix(g_millis + 1000, 7000);
    assert(!gLatestSpeedTrusted);
    feed(nmea("GPGSV,1,1,04,01,40,083,15,02,17,308,16,03,07,344,18,04,22,228,20,1"));
    gLoopFaults.event(3006, g_millis).condition(true);
    auto frames = gProductionOled.frames; serviceDisplay();
    assert(gProductionOled.frames == frames + 1 && gProductionOled.text[0].value == "Sats: 4");
    assert(gProductionLastOutput.codeCount == 1 && gProductionLastOutput.codes[0] == 3006);
    assert(gProductionLastOutput.until == indoorEnd);
    gLoopFaults.event(3006, g_millis).condition(false); serviceDisplay();
    assert(!gProductionLastOutput.codeCount && gProductionOled.text[1].value == "OK");
    assert(gProductionLastOutput.until == indoorEnd);
    g_millis = indoorEnd - 1; serviceDisplay(); assert(gProductionLastOutput.powered);
    ++g_millis; serviceDisplay(); assert(!gProductionLastOutput.powered);
    diagnosticFix(g_millis + 1000, 100);
    assert(gLatestSpeedTrusted); serviceDisplay();
    assert(!gProductionLastOutput.powered); ++cases;

    faultWorld(); gDebugMode = false; beginProductionDisplay();
    diagnosticFix(1000, 100, 0); serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::StatusCard);
    g_millis += display::DisplayModel::kDurationMs; serviceProductionDisplay(); assert(!gProductionLastOutput.powered);
    // Stale speed is Unknown, but a fresh model still shows its boot card.
    g_millis += 2501; // exceed speed freshness even with a short card setting
    gDisplayModel = display::DisplayModel(); serviceProductionDisplay();
    assert(gProductionLastOutput.card == display::Card::StatusCard);
    const uint32_t bootEnd = gProductionLastOutput.until;
    assert(bootEnd == g_millis + display::DisplayModel::kDurationMs);
    // VBUS absence is Parked even without a trusted/fresh fix.
    gVbusLost = true; serviceProductionDisplay();
    assert(gProductionLastOutput.powered && gProductionLastOutput.until == bootEnd);
    g_millis = bootEnd; serviceProductionDisplay(); assert(!gProductionLastOutput.powered); ++cases;

    faultWorld(); gDebugMode = false; beginProductionDisplay(); gState = ACTIVE;
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

    faultWorld(); gDebugMode = false;
    unsigned transfers = 0;
    TwoWire::transaction = [&](TwoWire& bus, bool) -> uint8_t { if (&bus == &Wire) { ++transfers; return 5; } return 0; };
    beginProductionDisplay(); assert(gProductionDisplayFailed && !gProductionDisplayReady);
    serviceRuntimeDiagnostics();
    auto rows = take(); assert(codeRows(rows, 6001).size() == 1);
    has(codeRows(rows, 6001)[0], "\"transition\":\"raise\"");
    for (int i = 0; i < 100; ++i) { beginProductionDisplay(); serviceProductionDisplay(); serviceRuntimeDiagnostics(); }
    assert(transfers == 1 && codeRows(take(), 6001).empty());
    // A disconnected display cannot stop a real drive or alter GPX writes.
    run(15, 40); run(0, kCooldownSeconds + 5);
    assert(gState == IDLE && !g_sdDisk.files.empty());
    bool completeTrip = false;
    for (const auto& file : g_sdDisk.files)
        completeTrip |= file.first.rfind("/trips/", 0) == 0 && file.second.find("</gpx>") != std::string::npos;
    assert(completeTrip);
    assert(transfers == 1 && !g_lockDepth);
    TwoWire::transaction = nullptr; ++cases;

    faultWorld(); gDebugMode = false; beginProductionDisplay(); gDisplayUserRequest = true; serviceProductionDisplay();
    assert(gProductionLastOutput.powered);
    PMU.vbusIn = false; gVbusLost = true;
    try { powerOffForVbus(); assert(false); } catch (const PoweredOff&) {}
    assert(!gProductionLastOutput.powered && gProductionOled.sleeps >= 2); ++cases;
    g_lockDepth = 0;
    toggleRegressions();
    std::printf("test_display_runtime: %u scenarios passed (runtime toggle and panel ownership)\n", cases);
}
