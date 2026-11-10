// Real sketch startup/loop with the existing real-parser drive fixtures.
#define main gpsRegressionMain
#include "test_gps.cpp"
#undef main
#include <cassert>
using namespace diagnostics;
using startup::Mode;
static EventLog::Batch degradedBatch;
static unsigned cases = 0;
static std::vector<uint32_t> bootDelays;

static void world() {
    resetWorld();
    Serial.logMs.clear();
    gDegraded = startup::DegradedPolicy();
    PMU = XPowersAXP2101(); Wire = TwoWire(); Wire1 = TwoWire();
    g_pinModes.clear(); g_pinLevels.clear(); g_partition = PartitionStub();
    gSdMounted = gFlashMounted = g_disk.mounted = false;
    g_taskCreates = g_restarts = g_lockDepth = 0; gFsDemand = 0;
    g_failMutexCreate = false; gFsMutex = nullptr;
    gNetHold = gNetBusy = gNetAbort = false;
    gJournalReady = gJournalStarted = gJournalSealing = gJournalBootReported = false;
    gJournalSealRequested = gJournalSealDone = gJournalSealOk = false;
    gEventIoBudget = false; g_taskDelayHook = nullptr;
    gChargeState = pmu_read::ChargeState::Unconfirmed; gChargeChecked = gChargePauseRequested = false; gChargeFailures = 0;
    gChargeFirstResolved = false; gChargeFirstMs = 0;
    gBatteryDiagChecked = false;
    gVbusOffPending = false;
    gPmuIrqFlag = false;
    gVbusRetry = VbusRetry::None; gVbusRetryMs = gVbusOffReadMs = 0;
    gPmuIrqRetry = false; gPmuIrqReadMs = 0; gBatteryThresholds = -1;
    for (auto& slot : gLoopConditions) { auto code = slot.code; slot = {}; slot.code = code; }
    gLoopFaults.~RuntimeFaults();
    new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
    bootDelays.clear();
    g_delayHook = [](uint32_t ms) { bootDelays.push_back(ms); g_millis += ms; };
}
static std::vector<std::string> rows() {
    xSemaphoreTake(gFsMutex, portMAX_DELAY);
    bool ok = gJournal.nextBatch(degradedBatch);
    if (!ok) std::fprintf(stderr, "batch failure: scenario %u ready=%d queued=%u mode=%d\n", cases, gJournalReady, unsigned(gJournal.stats().queued), int(gDegraded.mode()));
    assert(ok);
    std::vector<std::string> out;
    for (size_t i = 0; i < degradedBatch.count; ++i) out.emplace_back(degradedBatch.events[i]);
    xSemaphoreGive(gFsMutex);
    return out;
}
static bool contains(const std::vector<std::string>& all, unsigned code) {
    auto key = "\"code\":" + std::to_string(code) + ",";
    for (const auto& row : all) if (row.find(key) != std::string::npos) return true;
    return false;
}
static RuntimeFaults::Observation observation(unsigned code) {
    RuntimeFaults::Observation out[64]; size_t count = 0;
    assert(gLoopFaults.snapshot(out, 64, count));
    for (size_t i = 0; i < count; ++i) if (out[i].code == code) return out[i];
    assert(false); return {};
}
static void noTrips() {
    assert(!gDebugTempEnabled);
    assert(std::find(PMU.operations.begin(), PMU.operations.end(), "writeRegister") == PMU.operations.end());
    assert(gState == IDLE && !gTripPath[0] && gArmCount == 0 && gPendCount == 0);
    for (auto* disk : {&g_disk, &g_sdDisk}) for (const auto& file : disk->files)
        assert(file.first.rfind("/trips/", 0) != 0);
    assert(g_taskCreates == 0 && GPSSerial.baud == 0 && GPSSerial.tx.empty());
}
static void restartOnLoop() {
    auto before = g_restarts;
    try { loop(); assert(false); } catch (const Restarted&) {}
    assert(g_restarts == before + 1);
}
static void bootMessagesAfterUsbWait() {
    assert(!Serial.log.empty() && Serial.log.size() == Serial.logMs.size());
    assert(Serial.log.front() == "\n=== Stage 4: Trip Logger ===");
    assert(Serial.log[1] == std::string("Firmware: ") + firmware::kVersion + "\n");
    assert(uint32_t(Serial.logMs.front() - Serial.beginMs) == 1000);
    for (auto at : Serial.logMs) assert(uint32_t(at - Serial.beginMs) >= 1000);
}
static void retryMessage(const char* cause) {
    const auto expected = std::string("DEGRADED (") + cause +
        "): retry failed -- not recording trips. Retrying in 120 s.\n";
    assert(std::count(Serial.log.begin(), Serial.log.end(), expected) == 1);
}
static void bootD() {
    world(); g_disk.failMount = true;
    setup(); assert(gDegraded.mode() == Mode::NoStorage);
    bootMessagesAfterUsbWait();
    assert(!gSdMounted && !gFlashMounted && gJournalReady);
    noTrips();
}
static void onlyPmuCalls(std::initializer_list<const char*> allowed) {
    for (const auto& op : PMU.operations) {
        bool found = false;
        for (const auto* name : allowed) found |= op == name;
        assert(found);
    }
}
static void failNextRead(uint8_t reg, unsigned offset = 1) {
    PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg] + offset;
}
static void assertAwakeRecovery() {
    assert(PMU.shutdownCalls == 0 && g_lockDepth == 0 && !gNetHold && !gNetAbort);
    assert(!gVbusOffPending && g_disk.mounted && gFlashMounted);
    assert(!gJournalSealRequested && !gJournalSealDone);
}
int main() {
    // Exhaust all successful byte values against XPowersLib's bit semantics.
    for (int raw = 0; raw <= 255; ++raw) {
        assert(pmu_read::aldo3State(raw) == ((raw & 4) ? Aldo3State::On : Aldo3State::Off));
        for (int second = 0; second <= 255; ++second) {
            assert(pmu_read::vbusState(raw, second) ==
                   (!(second & 8) && (raw & 32) ? VbusState::Present : VbusState::Absent));
            assert(pmu_read::aldo1Ready(raw, second) == bool((raw & 1) && (second & 31) == 28));
        }
        assert(!pmu_read::aldo1Ready(-1, raw));
        assert(!pmu_read::aldo1Ready(raw, -1));
        assert(pmu_read::vbusState(-1, raw) == VbusState::Unknown);
        assert(pmu_read::vbusState(raw, -1) == VbusState::Unknown);
    }
    assert(pmu_read::aldo3State(-1) == Aldo3State::Unknown);
    assert(pmu_read::vbusState(-1, -1) == VbusState::Unknown);
    assert(!pmu_read::aldo1Ready(-1, -1));

    // Cold boot changes 1.8 V to 3.3 V; a warm boot already at 3.3 V/on
    // needs no settle delay. The first display bus operation sees all PMU
    // writes and independent read-backs completed, after charging is paused.
    for (bool enabled : {false, true}) for (int mv : {1800, 3300}) {
        world(); PMU.aldo1 = enabled; PMU.aldo1Mv = mv;
        bool displayBegan = false;
        Wire.onBegin = [&] {
            displayBegan = true;
            const std::vector<std::string> expected = {
                "begin", "disableALDO3", "readRegister", "disableCellbatteryCharge",
                "enableBattDetection", "enableBattVoltageMeasure", "readRegister", "readRegister",
                "setALDO1Voltage", "enableALDO1", "readRegister", "readRegister"
            };
            assert(PMU.operations == expected);
            assert(!PMU.aldo3 && !PMU.chargeEnabled && PMU.aldo1 && PMU.aldo1Mv == 3300);
            assert(Serial.log.empty());
            assert(std::count(bootDelays.begin(), bootDelays.end(), 10) == (enabled && mv == 3300 ? 0 : 1));
            assert(PMU.registerReadCalls[XPOWERS_AXP2101_LDO_ONOFF_CTRL0] == 3);
            assert(PMU.registerReadCalls[XPOWERS_AXP2101_LDO_VOL0_CTRL] == 2);
        };
        setup(); Wire.onBegin = nullptr;
        assert(displayBegan && gProductionDisplayReady && g_taskCreates == 1);
        assert(gDegraded.mode() == Mode::Normal && !logCount("WARNING: ALDO1"));
        bootMessagesAfterUsbWait(); ++cases;
    }

    // Failed writes, lying writes and either unreadable verification must
    // warn once after USB is ready, but still start the display and logger.
    for (unsigned failure = 0; failure < 6; ++failure) {
        world(); PMU.aldo1 = false;
        PMU.failAldo1Voltage = failure == 0;
        PMU.failAldo1Enable = failure == 1;
        PMU.aldo1VoltageStuck = failure == 2;
        PMU.aldo1EnableStuck = failure == 3;
        if (failure == 4) failNextRead(XPOWERS_AXP2101_LDO_ONOFF_CTRL0, 3);
        if (failure == 5) failNextRead(XPOWERS_AXP2101_LDO_VOL0_CTRL, 2);
        setup();
        assert(gDegraded.mode() == Mode::Normal && gProductionDisplayReady && g_taskCreates == 1);
        assert(logCount("WARNING: ALDO1 3.3 V/on setup unconfirmed") == 1);
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "setALDO1Voltage") == 1);
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "enableALDO1") == 1);
        bootMessagesAfterUsbWait(); ++cases;
    }
    // Unknown initial state cannot skip settling; successful final reads can
    // still confirm the requested setting without producing a warning.
    for (bool control : {false, true}) {
        world(); PMU.aldo1Mv = 3300;
        failNextRead(control ? XPOWERS_AXP2101_LDO_ONOFF_CTRL0 : XPOWERS_AXP2101_LDO_VOL0_CTRL,
                     control ? 2 : 1);
        setup();
        assert(std::count(bootDelays.begin(), bootDelays.end(), 10) == 1);
        assert(!logCount("WARNING: ALDO1") && g_taskCreates == 1); ++cases;
    }

    // Disable succeeds, but neither boot nor a degraded retry may accept a
    // failed independent verification. A later real clear read can recover.
    world(); failNextRead(XPOWERS_AXP2101_LDO_ONOFF_CTRL0);
    setup();
    assert(!PMU.aldo3 && gDegraded.mode() == Mode::PmuUntrusted);
    onlyPmuCalls({"begin", "disableALDO3", "disableCellbatteryCharge", "readRegister"});
    assert(PMU.aldo1 && PMU.aldo1Mv == 1800);
    assert(!logCount("LoRa rail (ALDO3) verified off"));
    assert(PMU.operations[0] == "begin" && PMU.operations[1] == "disableALDO3" &&
           PMU.operations[2] == "readRegister");
    failNextRead(XPOWERS_AXP2101_LDO_ONOFF_CTRL0);
    g_millis += 60000; loop();
    assert(g_restarts == 0 && gDegraded.mode() == Mode::PmuUntrusted && gDegraded.waitMs() == 120000);
    assert(!logCount("initialization retry succeeded") && !logCount("LoRa rail (ALDO3) verified off"));
    g_millis += 120000; restartOnLoop(); ++cases;

    for (uint8_t reg : {XPOWERS_AXP2101_STATUS1, XPOWERS_AXP2101_STATUS2}) {
        static uint8_t injectedVbusReg;
        injectedVbusReg = reg;
        // Both entry and final checks reject unknown even if USB is really
        // present. The final case simulates USB returning without an IRQ.
        world(); setup();
        gVbusLost = gVbusOffPending = true; gNetHold = true;
        failNextRead(reg); powerOffForVbus();
        assertAwakeRecovery(); assert(!gVbusLost && gVbusRetry == VbusRetry::Seed);
        assert(logCount(VBUS_UNREADABLE) == 1); ++cases;

        world(); setup(); PMU.vbusIn = false; gVbusLost = true;
        // Fail the final VBUS read itself, after the new battery sample's
        // STATUS1 read. Do not depend on unrelated register-read counts.
        g_disk.onUnmount = [] {
            PMU.vbusIn = true;
            failNextRead(injectedVbusReg);
        };
        powerOffForVbus();
        assertAwakeRecovery(); assert(!gVbusLost && gVbusRetry == VbusRetry::None);
        assert(logCount(VBUS_UNREADABLE) == 1); ++cases;

        // Returning shutdown exercises recovery seed independently of the
        // successful absent entry and final reads.
        world(); setup(); PMU.vbusIn = false; PMU.failShutdown = true;
        PMU.onShutdown = [] { failNextRead(injectedVbusReg); };
        powerOffForVbus();
        assert(PMU.shutdownCalls == 1 && !gVbusLost && gVbusRetry == VbusRetry::Seed);
        assert(!gNetHold && !gNetAbort && !gVbusOffPending && g_disk.mounted && g_lockDepth == 0);
        auto reads = PMU.registerReadCalls[reg];
        serviceVbusRecheck(); assert(PMU.registerReadCalls[reg] == reads);
        g_millis += VBUS_RETRY_MS; serviceVbusRecheck();
        assert(gVbusLost && gVbusRetry == VbusRetry::None); ++cases;

        // Boot seed assumes USB on an unreadable result; a bounded loop
        // retry corrects battery-only startup, including no-storage mode.
        for (bool degraded : {false, true}) {
            world(); PMU.vbusIn = false; g_disk.failMount = degraded;
            PMU.onRegisterRead = [](uint8_t readReg) {
                if (readReg == 0xA4) failNextRead(injectedVbusReg); // after early battery snapshot
            };
            setup(); PMU.onRegisterRead = nullptr;
            assert(!gVbusLost && gVbusRetry == VbusRetry::Seed);
            reads = PMU.registerReadCalls[reg];
            for (unsigned i = 0; i < 20; ++i) serviceVbusRecheck();
            // No-storage startup has a one-second ADC delay after the seed;
            // service can already be due there, but never reads tightly.
            assert(PMU.registerReadCalls[reg] <= reads + 1);
            g_millis += VBUS_RETRY_MS; loop();
            assert(gVbusLost && gVbusRetry == VbusRetry::None);
            ++cases;
        }

        // An unreadable confirmation preserves a real in-progress trip.
        world(); setup(); ackGps(); run(15, 40, true, false, 100);
        assert(gTripPath[0]); auto path = std::string(gTripPath);
        PMU.vbusIn = false; PMU.removeIrq = true; gPmuIrqFlag = true; pollPmu();
        g_millis += VBUS_OFF_CONFIRM_MS; failNextRead(reg); confirmVbusOff();
        assert(gVbusOffPending && !gVbusLost && path == gTripPath);
        assert(!logCount("car off: finalizing"));
        reads = PMU.registerReadCalls[reg];
        for (unsigned i = 0; i < 20; ++i) confirmVbusOff();
        assert(PMU.registerReadCalls[reg] == reads);
        g_millis += VBUS_RETRY_MS; confirmVbusOff();
        assert(!gVbusOffPending && gVbusLost && !gTripPath[0]);
        assert(logCount("car off: finalizing") == 1); ++cases;

        // Lost IRQ-level observation is retried without inventing REMOVE.
        world(); setup(); PMU.vbusIn = false;
        PMU.removeIrq = true; gPmuIrqFlag = true; failNextRead(reg); pollPmu();
        assert(!gVbusLost && !gVbusOffPending && gVbusRetry == VbusRetry::Change);
        reads = PMU.registerReadCalls[reg]; serviceVbusRecheck();
        assert(PMU.registerReadCalls[reg] == reads);
        g_millis += VBUS_RETRY_MS; serviceVbusRecheck();
        assert(gVbusOffPending && !gVbusLost);
        g_millis += VBUS_OFF_CONFIRM_MS; confirmVbusOff(); assert(gVbusLost); ++cases;
    }

    // Retry arithmetic remains bounded across millis rollover, and repeated
    // failures keep the safe seed until a real absent observation succeeds.
    world(); setup(); PMU.vbusIn = false; g_millis = UINT32_MAX - 499;
    failNextRead(XPOWERS_AXP2101_STATUS1); seedVbusState();
    g_millis += 999; auto reads = PMU.registerReadCalls;
    serviceVbusRecheck(); assert(PMU.registerReadCalls == reads && !gVbusLost);
    ++g_millis; failNextRead(XPOWERS_AXP2101_STATUS1); serviceVbusRecheck();
    assert(!gVbusLost && gVbusRetry == VbusRetry::Seed);
    g_millis += 1000; serviceVbusRecheck(); assert(gVbusLost); ++cases;

    bootD();
    auto all = rows();
    assert(contains(all, 1000) && contains(all, 4001) && contains(all, 4002) && contains(all, 4003));
    assert(observation(4003).observed && observation(4003).episode);
    assert(!observation(2001).observed); // a degraded boot never publishes clear
    auto mounts = g_disk.mountAttempts;
    GPSSerial.rx = navPvt(0, 100);
    for (const auto& sentence : sentences({0, 37, -122, 15, true})) GPSSerial.rx += sentence;
    auto started = g_millis;
    for (int i = 0; i < 1000; ++i) loop();
    assert(g_millis - started == 20000 && g_disk.mountAttempts == mounts && GPSSerial.rxPos == 0);
    noTrips();
    assert(!gProductionHaveOutput); // degraded loop never invokes the model/renderer

    PMU.dieC = 60; g_millis += CHG_CHECK_MS; loop(); assert(!PMU.chargeEnabled);
    PMU.dieC = 35; g_millis += CHG_CHECK_MS; loop(); assert(PMU.chargeEnabled);
    assert(!g_disk.formatAttempts && !g_sdDisk.formatRequested);
    ++cases;

    for (unsigned reason = 0; reason < 4; ++reason) {
        bootD(); PMU.vbusIn = false;
        if (reason == 0) {
            PMU.removeIrq = true; gPmuIrqFlag = true; loop();
            assert(gVbusOffPending && !gVbusLost);
            g_millis += VBUS_OFF_CONFIRM_MS; loop(); assert(gVbusLost);
            g_millis += IDLE_BATT_SLEEP_MS;
        } else if (reason == 1) {
            PMU.longPressIrq = true; gPmuIrqFlag = true;
        } else if (reason == 2) {
            PMU.criticalIrq = true; gPmuIrqFlag = true;
        } else {
            gVbusLost = true; g_millis += IDLE_BATT_SLEEP_MS; PMU.failShutdown = true;
        }
        if (reason == 3) {
            loop();
            assert(g_lockDepth == 0 && !gNetHold && !gJournalSealRequested);
            assert(!gSdMounted && !gFlashMounted && gDegraded.mode() == Mode::NoStorage);
        } else {
            try { loop(); assert(false); } catch (const PoweredOff&) {}
            assert(g_lockDepth == 1 && gJournalSealDone);
        }
        assert(PMU.shutdownCalls == 1 && !g_disk.mounted && !g_sdDisk.mounted);
        assert(g_disk.mountAttempts == 1 + (reason == 3 ? 1 : 0));
        assert(!g_disk.formatAttempts); noTrips(); ++cases;
    }
    bootD(); PMU.longPressIrq = true; gPmuIrqFlag = true; loop();
    assert(PMU.shutdownCalls == 0 && !gVbusLost); ++cases;

    for (bool sd : {false, true}) {
        bootD();
        if (sd) SD.cardPresent = true; else g_disk.failMount = false;
        g_millis += 60000; restartOnLoop();
        assert(!gSdMounted && !gFlashMounted && !g_disk.mounted && !g_sdDisk.mounted);
        assert(observation(4003).episode && !observation(2001).observed);
        noTrips(); ++cases;
    }
    bootD(); g_partition.bytes.assign(8192, 0xff);
    g_millis += 60000; loop(); assert(gDegraded.waitMs() == 120000 && !g_disk.formatAttempts);
    retryMessage("no usable trip storage");
    auto attempts = g_disk.mountAttempts; loop(); assert(g_disk.mountAttempts == attempts); ++cases;

    for (bool flash : {false, true}) {
        world(); g_disk.failMount = !flash; PMU.failBegins = 100;
        setup(); assert(gDegraded.mode() == Mode::PmuMissing);
        bootMessagesAfterUsbWait();
        auto attempts = PMU.beginCalls;
        for (unsigned i = 0; i < 5; ++i) loop();
        assert(PMU.beginCalls == attempts);
        assert(gProductionDisplayReady && gProductionOled.sleeps >= 1 && !gProductionHaveOutput);

        onlyPmuCalls({"begin"}); noTrips();
        assert(PMU.aldo1 && PMU.aldo1Mv == 1800);
        auto all = rows(); assert(contains(all, 1000) && contains(all, 2001) && contains(all, 2007));
        assert(observation(2001).observed && observation(2001).episode && !observation(4003).observed);
        assert(gJournal.stats().backend == (flash ? EventLog::Backend::LittleFs : EventLog::Backend::Ram));
        assert(!g_disk.formatAttempts && !SD.begins && !g_disk.files.count("/trips"));
        g_millis += 60000; loop();
        assert(PMU.beginCalls == attempts + 3 && gDegraded.waitMs() == 120000);
        retryMessage("power chip not responding");
        attempts = PMU.beginCalls; loop(); assert(PMU.beginCalls == attempts);
        PMU.failBegins = 0; g_millis += 120000; restartOnLoop();
        assert(gProductionDisplayReady && gProductionOled.sleeps >= 1 && !gProductionHaveOutput);

        onlyPmuCalls({"begin"}); noTrips(); ++cases;
    }
    for (bool writeFails : {false, true}) for (unsigned chargeResult = 0; chargeResult < 3; ++chargeResult) {
        world(); PMU.failRadioDisable = writeFails; PMU.radioStuckOn = !writeFails;
        PMU.failChargeDisable = chargeResult == 1; PMU.failChargeRead = chargeResult == 2;
        setup(); assert(gDegraded.mode() == Mode::PmuUntrusted);
        bootMessagesAfterUsbWait();
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "readRegister") == (writeFails ? 1 : 2));
        const char* expected = chargeResult == 0 ? "read-back shows charging disabled" :
            chargeResult == 1 ? "read-back still shows charging enabled" : "charger read-back failed, state unknown";
        bool printed = false;
        for (const auto& line : Serial.log) printed |= line.find(expected) != std::string::npos;
        assert(printed);
        for (unsigned i = 0; i < 5; ++i) loop();
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "disableCellbatteryCharge") == 1);
        onlyPmuCalls({"begin", "disableALDO3", "isEnableALDO3", "disableCellbatteryCharge", "readRegister"});
        assert(PMU.aldo1 && PMU.aldo1Mv == 1800);
        assert(contains(rows(), 2001)); noTrips();
        g_millis += 60000; loop(); assert(gDegraded.waitMs() == 120000);
        retryMessage("power chip state untrusted");
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "disableCellbatteryCharge") == 1);
        PMU.failRadioDisable = PMU.radioStuckOn = false;
        g_millis += 120000; restartOnLoop();
        onlyPmuCalls({"begin", "disableALDO3", "isEnableALDO3", "disableCellbatteryCharge", "readRegister"});
        noTrips(); ++cases;
    }
    // Leave a failed-PMU boot on flash, then simulate a normal reboot with SD.
    world(); PMU.failBegins = 3; setup(); for (int i = 0; i < 5; ++i) loop();
    auto oldId = std::string(gJournalBoot.id); auto flashFiles = g_disk.files;
    world(); g_disk.files = flashFiles; LittleFS.dirs.insert("/diag"); SD.cardPresent = true;
    setup();
    assert(gDegraded.mode() == Mode::Normal && gSdMounted && gFlashMounted && g_taskCreates == 1);
    assert(observation(2001).observed && !observation(2001).episode);
    assert(observation(4003).observed && !observation(4003).episode);
    xSemaphoreTake(gFsMutex, portMAX_DELAY);
    assert(gJournal.nextBatch(degradedBatch) && degradedBatch.count >= 2);
    bool oldBoot = false;
    for (size_t i = 0; i < degradedBatch.count; ++i) oldBoot |= oldId == degradedBatch.ids[i].bootId;
    assert(oldBoot && gJournal.acknowledge(degradedBatch, degradedBatch.ids, degradedBatch.count));
    assert(gJournal.nextBatch(degradedBatch));
    for (size_t i = 0; i < degradedBatch.count; ++i) assert(oldId != degradedBatch.ids[i].bootId);
    xSemaphoreGive(gFsMutex); ++cases;
    std::printf("test_degraded: %u integration scenarios passed (runtime debug=%d)\n", cases, debugMode());
}
