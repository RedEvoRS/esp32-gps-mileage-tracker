// Real sketch startup/loop with the existing real-parser drive fixtures.
#define main gpsRegressionMain
#include "test_gps.cpp"
#undef main
#include <cassert>
using namespace diagnostics;
using startup::Mode;
static EventLog::Batch degradedBatch;
static unsigned cases = 0;

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
    gChargeApplied = gChargePaused = gBatteryDiagChecked = false;
    gVbusOffPending = gPmuIrqFlag = false;
    for (auto& slot : gLoopConditions) { auto code = slot.code; slot = {}; slot.code = code; }
    gLoopFaults.~RuntimeFaults();
    new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
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
int main() {
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
        onlyPmuCalls({"begin"}); noTrips();
        auto all = rows(); assert(contains(all, 1000) && contains(all, 2001) && contains(all, 2007));
        assert(observation(2001).observed && observation(2001).episode && !observation(4003).observed);
        assert(gJournal.stats().backend == (flash ? EventLog::Backend::LittleFs : EventLog::Backend::Ram));
        assert(!g_disk.formatAttempts && !SD.begins && !g_disk.files.count("/trips"));
        g_millis += 60000; loop();
        assert(PMU.beginCalls == attempts + 3 && gDegraded.waitMs() == 120000);
        retryMessage("power chip not responding");
        attempts = PMU.beginCalls; loop(); assert(PMU.beginCalls == attempts);
        PMU.failBegins = 0; g_millis += 120000; restartOnLoop();
        onlyPmuCalls({"begin"}); noTrips(); ++cases;
    }
    for (bool writeFails : {false, true}) for (unsigned chargeResult = 0; chargeResult < 3; ++chargeResult) {
        world(); PMU.failRadioDisable = writeFails; PMU.radioStuckOn = !writeFails;
        PMU.failChargeDisable = chargeResult == 1; PMU.failChargeRead = chargeResult == 2;
        setup(); assert(gDegraded.mode() == Mode::PmuUntrusted);
        bootMessagesAfterUsbWait();
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "readRegister") == 1);
        const char* expected = chargeResult == 0 ? "read-back shows charging disabled" :
            chargeResult == 1 ? "read-back still shows charging enabled" : "charger read-back failed, state unknown";
        bool printed = false;
        for (const auto& line : Serial.log) printed |= line.find(expected) != std::string::npos;
        assert(printed);
        for (unsigned i = 0; i < 5; ++i) loop();
        assert(std::count(PMU.operations.begin(), PMU.operations.end(), "disableCellbatteryCharge") == 1);
        onlyPmuCalls({"begin", "disableALDO3", "isEnableALDO3", "disableCellbatteryCharge", "readRegister"});
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
    std::printf("test_degraded: %u integration scenarios passed (DISPLAY_DEBUG=%d)\n", cases, DISPLAY_DEBUG);
}
