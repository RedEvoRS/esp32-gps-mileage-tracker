// Enrollment/progress policy against the real sketch. Timer reset hardware is
// deliberately not simulated as proof: use the documented bench build for that.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"

static void watchdogWorld() {
    faultWorld();
    g_currentCore = 1;
    gDegraded = startup::DegradedPolicy();
    gRtc = diagnostics::RtcRuntime();
    Wire = TwoWire(); Wire1 = TwoWire();
    TwoWire::transaction = nullptr; TwoWire::request = nullptr;
    g_taskDelayHook = nullptr; g_semaphoreTakeHook = nullptr;
    gSdMounted = gFlashMounted = false;
    gJournalReady = gJournalStarted = false;
    gJournalSealing = false;
    gJournalSealRequested = gJournalSealDone = gJournalSealOk = false;
    gPowerOffEventPending = false;
    gFsDemand = 0; g_lockDepth = 0;
    g_taskCreates = g_restarts = 0;
    g_failMutexCreate = false;
    g_partition = PartitionStub();
    SD.cardPresent = true; SD.capacityQueries = 0; SD.onCapacity = nullptr;
    g_sdDisk.mounted = g_disk.mounted = false;
    g_sdDisk.capacity = 32 * 1024 * 1024;
    g_resetReason = ESP_RST_POWERON;
    g_delayHook = [](uint32_t ms) {
        assert(!gLoopWatchdogArmed && !g_watchdog.feeds);
        g_millis += ms;
    };
}
static void checkArmed() {
    assert(gLoopWatchdogArmed && loopTaskWDTEnabled);
    assert(g_watchdog.enrollments == 1 && g_watchdog.reconfigures == 1);
    assert(g_watchdog.config.timeout_ms == LOOP_WDT_TIMEOUT_MS && LOOP_WDT_TIMEOUT_MS == 60000);
    assert(g_watchdog.config.trigger_panic && g_watchdog.config.idle_core_mask == 1);
    assert(g_watchdog.feeds == 0);
    armLoopWatchdog(); assert(g_watchdog.enrollments == 1 && g_watchdog.reconfigures == 1);
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
}
static void arduinoPass() {
    // This is the installed main.cpp's ordering, separate from sketch feeds.
    if (loopTaskWDTEnabled) feedLoopWDT();
    loop();
}
#ifndef WATCHDOG_BENCH_HANG
static std::string largeOpenTrip() {
    std::string data = GPX_PROLOGUE;
    // Generate wholly synthetic points with the production serializer rather
    // than storing coordinate literals in this recovery-only test source.
    File pointFile = SD.open("/synthetic-point", FILE_WRITE);
    FileWriter writer(pointFile);
    Fix zero{}; zero.speedMps = 1; zero.hdop = 1;
    appendTrkpt(writer, zero); assert(writer.commit()); pointFile.close();
    const std::string point = g_sdDisk.files.at("/synthetic-point");
    SD.remove("/synthetic-point");
    while (data.size() + point.size() + 10 < TRIP_SPLIT_BYTES) data += point;
    return data + "<trkpt";
}
static void startupAndRecovery() {
    watchdogWorld();
    // Both long boot operations exceed the timeout in fake elapsed time.
    // Neither may enroll or feed before setup finishes.
    g_disk.failMount = true;
    g_partition.partition.size = 0x260000;
    g_partition.bytes.assign(g_partition.partition.size, 0xff);
    g_sdDisk.capacity = 32 * 1024 * 1024;
    g_sdDisk.files["/trips/interrupted.gpx"] = largeOpenTrip();
    g_sdDisk.onRead = []() {
        assert(!gLoopWatchdogArmed && !g_watchdog.feeds);
        g_millis += 100;
    };
    SD.onCapacity = []() { assert(!gLoopWatchdogArmed && !g_watchdog.feeds); };
    g_resetReason = ESP_RST_TASK_WDT;
    setup(); checkArmed();
    assert(g_partition.readBytes == g_partition.partition.size && g_disk.formatAttempts == 1);
    assert(SD.capacityQueries > 0 && g_taskCreates == 1 && gJournalReady && !gJournalStarted);
    assert(g_sdDisk.files.at("/trips/interrupted.gpx").ends_with(GPX_EPILOGUE));
    assert(millis() > LOOP_WDT_TIMEOUT_MS);
    g_sdDisk.onRead = nullptr; SD.onCapacity = nullptr;
    FS_LOCK(); auto rows = take(); FS_UNLOCK();
    assert(codeRows(rows, 1003).size() == 1);
    has(codeRows(rows, 1000)[0], "\"reset_reason\":\"watchdog\"");
    arduinoPass(); assert(g_watchdog.feeds == 1);
    // Network maintenance and uploads run on core 0, never enrolled/fed.
    g_currentCore = 0;
    unsigned feeds = g_watchdog.feeds;
    feedLoopWatchdogProgress(); serviceJournal(); netCycle();
    assert(g_watchdog.feeds == feeds && g_watchdog.enrollments == 1);
    g_currentCore = 1;
}
static void degradedCoverage() {
    for (auto mode : {startup::Mode::PmuMissing, startup::Mode::PmuUntrusted, startup::Mode::NoStorage}) {
        watchdogWorld();
        if (mode == startup::Mode::PmuMissing) PMU.failBegins = 3;
        if (mode == startup::Mode::PmuUntrusted) PMU.radioStuckOn = true;
        if (mode == startup::Mode::NoStorage) { SD.cardPresent = false; g_disk.failMount = true; }
        setup(); checkArmed();
        assert(gDegraded.mode() == mode && g_taskCreates == 0);
        arduinoPass(); assert(g_watchdog.feeds == 1); // existing 20 ms delay, core's feed
        if (mode == startup::Mode::NoStorage) {
            SD.cardPresent = true;
            SD.onCapacity = []() { assert(false); }; // defer first capacity scan to reboot
            g_millis += gDegraded.waitMs();
            try { arduinoPass(); assert(false); } catch (const Restarted&) {}
            assert(g_watchdog.enrollments == 1 && g_watchdog.feeds == 2);
            SD.onCapacity = nullptr;
        }
    }
}
static void repairProgress() {
    watchdogWorld(); setup(); checkArmed();
    strcpy(gTripPath, "/trips/runtime.gpx"); gTripWriteFailed = true; gState = ACTIVE;
    g_sdDisk.files[gTripPath] = largeOpenTrip();
    g_sdDisk.onRead = []() { g_millis += 100; };
    feedLoopWDT();
    uint32_t lastFeed = millis();
    g_watchdog.onFeed = [&]() { assert(uint32_t(millis() - lastFeed) <= 200); lastFeed = millis(); };
    uint32_t started = millis(); endTrip(false);
    assert(uint32_t(millis() - started) > LOOP_WDT_TIMEOUT_MS && g_watchdog.feeds > 10000);
    assert(g_sdDisk.files.at("/trips/runtime.gpx").ends_with(GPX_EPILOGUE));
    // Verify .fix prefix/suffix scan loops feed only after successful matches.
    g_sdDisk.files["/trips/original.gpx"] = g_sdDisk.files.at("/trips/runtime.gpx");
    g_sdDisk.files["/trips/original.gpx.fix"] = g_sdDisk.files["/trips/original.gpx"];
    FS_LOCK(); assert(repairIfNeeded(SD, "/trips/original.gpx")); FS_UNLOCK();
    assert(!g_sdDisk.files.count("/trips/original.gpx.fix"));
    g_watchdog.onFeed = nullptr; g_sdDisk.onRead = nullptr;
    unsigned before = g_watchdog.feeds;
    g_sdDisk.failReadPath = "/trips/runtime.gpx";
    g_sdDisk.failReadAfter = 0;
    File failed = SD.open("/trips/runtime.gpx"); assert(findLastTrkptEnd(failed) == -2);
    assert(g_watchdog.feeds == before); failed.close();
}
static void boundedShutdownAndBlockedLock() {
    watchdogWorld(); setup(); checkArmed();
    // Both handoffs consume their full budget, then the failed PMU shutdown
    // returns after both one-second holds. Never feed inside either wait.
    PMU.vbusIn = false; PMU.failShutdown = true;
    gNetBusy = true; gJournalReady = true;
    unsigned baseline = g_watchdog.feeds;
    unsigned slices = 0;
    g_taskDelayHook = [](uint32_t ms) { assert(ms == 20 || ms == 1000); };
    // Observe feeds only between stages; the first two feeds follow the two
    // full 15-second waits, never their 20 ms polling slices.
    uint32_t start = millis();
    g_watchdog.onFeed = [&]() {
        ++slices;
        uint32_t elapsed = millis() - start;
        if (slices == 1) assert(elapsed == NET_ABORT_MS);
        if (slices == 2) assert(elapsed == 2 * NET_ABORT_MS);
    };
    try { powerOffForVbus("user_long_press"); assert(false); } catch (const Restarted&) {}
    assert(uint32_t(millis() - start) == 2 * NET_ABORT_MS + 2000);
    assert(g_watchdog.feeds > baseline && g_restarts == 1);
    g_watchdog.onFeed = nullptr; g_taskDelayHook = nullptr;
    g_lockDepth = 0; gFsDemand = 0;
    struct Blocked {};
    unsigned feeds = g_watchdog.feeds;
    g_semaphoreTakeHook = [&](uint32_t wait) {
        assert(wait == portMAX_DELAY);
        g_millis += LOOP_WDT_TIMEOUT_MS + 1;
        assert(g_watchdog.feeds == feeds);
        throw Blocked{};
    };
    try { FS_LOCK(); assert(false); } catch (const Blocked&) {}
    assert(g_watchdog.feeds == feeds);
    g_semaphoreTakeHook = nullptr; gFsDemand = 0;
    // A returning PMU shutdown either completes remount or restarts promptly;
    // neither path disables the watchdog around driver calls.
    for (bool remountFails : {false, true}) {
        watchdogWorld(); setup(); checkArmed();
        PMU.vbusIn = false; PMU.failShutdown = true; gJournalReady = false;
        g_disk.failMount = remountFails;
        if (remountFails) {
            try { powerOffForVbus(); assert(false); } catch (const Restarted&) {}
            assert(g_restarts == 1);
        } else {
            powerOffForVbus(); assert(g_lockDepth == 0 && !gNetHold);
        }
        assert(gLoopWatchdogArmed && g_watchdog.feeds >= 4);
        g_lockDepth = 0; gFsDemand = 0;
    }
}
#endif
int main() {
#ifdef WATCHDOG_BENCH_HANG
    watchdogWorld(); setup(); checkArmed();
    struct Hung {};
    uint32_t start = millis();
    g_taskDelayHook = [](uint32_t ms) { assert(ms == 1000); if (millis() > LOOP_WDT_TIMEOUT_MS + 5000) throw Hung{}; };
    try { arduinoPass(); assert(false); } catch (const Hung&) {}
    assert(uint32_t(millis() - start) >= LOOP_WDT_TIMEOUT_MS && g_watchdog.feeds == 1);
    g_taskDelayHook = nullptr;
    for (auto reason : {ESP_RST_TASK_WDT, ESP_RST_INT_WDT, ESP_RST_WDT}) {
        g_resetReason = reason;
        arduinoPass();
    }
    assert(g_watchdog.enrollments == 1 && g_watchdog.feeds == 4);
    std::puts("Watchdog bench hook: intentional hang without feeds, watchdog-reset boots skip hang passed");
#else
    startupAndRecovery(); degradedCoverage(); repairProgress(); boundedShutdownAndBlockedLock();
    for (bool initialized : {false, true}) {
        watchdogWorld(); g_watchdog.initialized = initialized;
        setup(); checkArmed(); assert(g_watchdog.initializes == (initialized ? 0u : 1u));
    }
    for (bool addFailure : {false, true}) {
        watchdogWorld(); g_watchdog.failAdd = addFailure; g_watchdog.failConfigure = !addFailure;
        try { setup(); assert(false); } catch (const Restarted&) {}
        assert(!gLoopWatchdogArmed && !g_watchdog.feeds);
    }
    watchdogWorld(); g_failMutexCreate = true;
    try { setup(); assert(false); } catch (const Restarted&) {}
    assert(!gLoopWatchdogArmed && !g_watchdog.enrollments && !g_watchdog.feeds);
    std::puts("Watchdog: late enrollment, boot repair/E1003, degraded modes, long progress, shutdown bounds, blocked lock and core-0 exclusion passed");
#endif
}
