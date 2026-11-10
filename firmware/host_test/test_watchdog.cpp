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
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
}
static void checkArmed() {
    assert(gLoopWatchdogArmed && loopTaskWDTEnabled);
    assert(g_watchdog.enrollments == 1 && g_watchdog.reconfigures == 2);
    assert(g_watchdog.config.timeout_ms == LOOP_WDT_TIMEOUT_MS && LOOP_WDT_TIMEOUT_MS == 60000);
    assert(g_watchdog.config.trigger_panic && g_watchdog.config.idle_core_mask == 1);
    assert((g_watchdog.timeouts == std::vector<uint32_t>{startup_watchdog::STARTUP_WDT_TIMEOUT_MS, LOOP_WDT_TIMEOUT_MS}));
    assert(g_watchdog.feeds > 0 && !gStartupWatchdog.active());
    auto feeds = g_watchdog.feeds;
    armLoopWatchdog(); assert(g_watchdog.enrollments == 1 && g_watchdog.reconfigures == 2 && g_watchdog.feeds == feeds);
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
}
static void arduinoPass() {
    // This is the installed main.cpp's ordering, separate from sketch feeds.
    if (loopTaskWDTEnabled) feedLoopWDT();
    loop();
}
#ifndef WATCHDOG_BENCH_HANG
static void nextBoot(startup_watchdog::Record saved, esp_reset_reason_t reason) {
    watchdogWorld();
    gStartupWatchdogRecord.magic = saved.magic;
    gStartupWatchdogRecord.stage = saved.stage;
    gStartupWatchdogRecord.hangs = saved.hangs;
    gStartupWatchdogRecord.fallback = saved.fallback;
    gStartupWatchdogRecord.checksum = saved.checksum;
    g_resetReason = reason;
}
#endif
#if !defined(WATCHDOG_BENCH_HANG) && !defined(WATCHDOG_BENCH_STARTUP_HANG)
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
    // Each operation exceeds the startup limit in total, but successful
    // block/copy progress feeds it. Individual waits never feed it.
    g_disk.failMount = true;
    g_partition.partition.size = 0x260000;
    g_partition.bytes.assign(g_partition.partition.size, 0xff);
    g_sdDisk.capacity = 32 * 1024 * 1024;
    g_sdDisk.files["/trips/interrupted.gpx"] = largeOpenTrip();
    g_sdDisk.onRead = []() {
        assert(gLoopWatchdogArmed && g_watchdog.config.timeout_ms == startup_watchdog::STARTUP_WDT_TIMEOUT_MS);
        assert(uint32_t(millis() - g_watchdog.lastResetMs) < 1000);
        g_millis += 100;
    };
    unsigned delayFeeds = 0;
    g_delayHook = [](uint32_t ms) {
        if (ms == 1) g_millis += 100; // erased block scan exceeds 120 s
        else g_millis += ms;
    };
    SD.onCapacity = []() { assert(gLoopWatchdogArmed && g_watchdog.feeds); };
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
    delayFeeds = g_watchdog.feeds;
    arduinoPass(); assert(g_watchdog.feeds == delayFeeds + 1);
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
        unsigned before = g_watchdog.feeds;
        arduinoPass(); assert(g_watchdog.feeds == before + 1); // core's feed
        if (mode == startup::Mode::NoStorage) {
            SD.cardPresent = true;
            SD.onCapacity = []() { assert(false); }; // defer first capacity scan to reboot
            g_millis += gDegraded.waitMs();
            try { arduinoPass(); assert(false); } catch (const Restarted&) {}
            assert(g_watchdog.enrollments == 1 && g_watchdog.feeds == before + 2);
            SD.onCapacity = nullptr;
        }
    }
}
struct StartupExpired {};
static void hangStage() {
    assert(gLoopWatchdogArmed && g_watchdog.config.timeout_ms == startup_watchdog::STARTUP_WDT_TIMEOUT_MS);
    unsigned feeds = g_watchdog.feeds;
    g_millis += startup_watchdog::STARTUP_WDT_TIMEOUT_MS;
    assert(g_watchdog.feeds == feeds);
    throw StartupExpired{};
}
static void startupFallback() {
    using namespace startup_watchdog;
    for (Stage target : {Stage::CardMount, Stage::CardSpace, Stage::CardRepair, Stage::FlashMount}) {
        Record saved{};
        for (uint32_t attempt = 0; attempt < STARTUP_HANG_LIMIT; ++attempt) {
            nextBoot(saved, attempt ? ESP_RST_TASK_WDT : ESP_RST_POWERON);
            if (target == Stage::CardMount) g_sdDisk.onMount = hangStage;
            if (target == Stage::FlashMount) g_disk.onMount = hangStage;
            if (target == Stage::CardSpace) SD.onCapacity = hangStage;
            if (target == Stage::CardRepair) {
                g_sdDisk.files["/trips/interrupted.gpx"] = largeOpenTrip();
                g_sdDisk.onRead = hangStage;
            }
            try { setup(); assert(false); } catch (const StartupExpired&) {}
            saved = gStartupWatchdog.record();
            assert(saved.stage == uint32_t(target) && saved.hangs == attempt && valid(saved));
            if (attempt) assert(logCount(stageText(target)) > 0 && logCount("consecutive startup hangs 1") == 1);
        }
        nextBoot(saved, ESP_RST_TASK_WDT);
        unsigned nvsBegins = Preferences::begins;
        setup(); checkArmed();
        assert(gStartupWatchdog.skipCard() && SD.begins == 0);
        assert(logCount("consecutive startup hangs 2") == 1 && logCount("until next power-on") >= 1);
        FS_LOCK(); auto rows = take(); FS_UNLOCK();
        assert(codeRows(rows, 1003).size() == 1 && codeRows(rows, 4001).size() == 1);
        if (cardOnly(target)) {
            assert(gDegraded.mode() == startup::Mode::Normal && gFlashMounted && gTripFS == &LittleFS);
            Fix fix{}; fix.monoMs = millis(); fix.speedMps = 10; fix.hdop = 1;
            assert(beginTrip(fix));
            assert(!g_disk.files.empty());
            // Completed fallback boots keep their latch through loop resets.
            saved = gStartupWatchdog.record();
            nextBoot(saved, ESP_RST_TASK_WDT); setup();
            assert(gStartupWatchdog.hangs() == 0 && gStartupWatchdog.hungStage() == Stage::None && !gSdMounted);
            // A later startup hang with the card already skipped escalates.
            gStartupWatchdog.stage(Stage::FlashMount); saved = gStartupWatchdog.record();
            nextBoot(saved, ESP_RST_TASK_WDT); setup();
            assert(gStartupWatchdog.noStorage() && gDegraded.mode() == startup::Mode::NoStorage);
        } else {
            assert(gStartupWatchdog.noStorage() && gDegraded.mode() == startup::Mode::NoStorage);
            assert(codeRows(rows, 4002).size() == 1 && codeRows(rows, 4003).size() == 1);
            assert(g_disk.mountAttempts == 0 && g_sdDisk.mountAttempts == 0);
            assert(!gJournalBoot.counterPersisted); // RAM-only boot: never revisit NVS
            assert(Preferences::begins == nvsBegins);
            g_millis += gDegraded.waitMs(); arduinoPass();
            assert(g_restarts == 0 && g_disk.mountAttempts == 0 && SD.begins == 0);
        }
        saved = gStartupWatchdog.record();
        nextBoot(saved, ESP_RST_POWERON); setup();
        assert(!gStartupWatchdog.skipCard() && gSdMounted && gDegraded.mode() == startup::Mode::Normal);
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
static void handoverNearDeadline() {
    watchdogWorld();
    gStartupWatchdog.boot({}, true, false); saveStartupWatchdog();
    armLoopWatchdog(startup_watchdog::STARTUP_WDT_TIMEOUT_MS);
    unsigned before = g_watchdog.feeds;
    g_millis += startup_watchdog::STARTUP_WDT_TIMEOUT_MS - 1;
    finishStartupWatchdog();
    assert(gLoopWatchdogArmed && g_watchdog.enrollments == 1);
    assert(g_watchdog.config.timeout_ms == LOOP_WDT_TIMEOUT_MS && g_watchdog.lastResetMs == millis());
    assert(g_watchdog.feeds == before + 2 && !gStartupWatchdog.active());
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
    unsigned before = g_watchdog.feeds;
    g_taskDelayHook = [](uint32_t ms) { assert(ms == 1000); if (millis() > LOOP_WDT_TIMEOUT_MS + 5000) throw Hung{}; };
    try { arduinoPass(); assert(false); } catch (const Hung&) {}
    assert(uint32_t(millis() - start) >= LOOP_WDT_TIMEOUT_MS && g_watchdog.feeds == before + 1);
    g_taskDelayHook = nullptr;
    for (auto reason : {ESP_RST_TASK_WDT, ESP_RST_INT_WDT, ESP_RST_WDT}) {
        g_resetReason = reason;
        arduinoPass();
    }
    assert(g_watchdog.enrollments == 1 && g_watchdog.feeds == before + 4);
    std::puts("Watchdog bench hook: intentional hang without feeds, watchdog-reset boots skip hang passed");
#elif defined(WATCHDOG_BENCH_STARTUP_HANG)
    startup_watchdog::Record saved{};
    struct Hung {};
    for (uint32_t attempt = 0; attempt < startup_watchdog::STARTUP_HANG_LIMIT; ++attempt) {
        nextBoot(saved, attempt ? ESP_RST_TASK_WDT : ESP_RST_POWERON);
        static unsigned before = 0;
        before = 0;
        g_taskDelayHook = [](uint32_t ms) {
            assert(ms == 1000 && gStartupWatchdog.record().stage == uint32_t(startup_watchdog::Stage::CardMount));
            if (!before) before = g_watchdog.feeds;
            assert(g_watchdog.feeds == before);
            if (uint32_t(millis() - g_watchdog.lastResetMs) >= startup_watchdog::STARTUP_WDT_TIMEOUT_MS) throw Hung{};
        };
        try { setup(); assert(false); } catch (const Hung&) {}
        assert(SD.begins == 0 && g_watchdog.feeds == before);
        saved = gStartupWatchdog.record();
    }
    nextBoot(saved, ESP_RST_TASK_WDT); setup(); checkArmed();
    assert(!gSdMounted && gFlashMounted && gTripFS == &LittleFS);
    assert(gDegraded.mode() == startup::Mode::Normal && logCount("skipping card until next power-on") == 1);
    Fix fix{}; fix.monoMs = millis(); fix.speedMps = 10; fix.hdop = 1;
    assert(beginTrip(fix));
    FS_LOCK(); auto rows = take(); FS_UNLOCK();
    assert(codeRows(rows, 4001).size() == 1 && codeRows(rows, 1003).size() == 1);
    unsigned before = g_watchdog.feeds; arduinoPass(); assert(g_watchdog.feeds == before + 1);
    std::puts("Startup watchdog bench hook: two un-fed 120 s hangs, retained count, skipped card, LittleFS trip and E1003/E4001 passed");
#else
    startupAndRecovery(); degradedCoverage(); startupFallback(); handoverNearDeadline(); repairProgress(); boundedShutdownAndBlockedLock();
    for (bool initialized : {false, true}) {
        watchdogWorld(); g_watchdog.initialized = initialized;
        setup(); checkArmed(); assert(g_watchdog.initializes == (initialized ? 0u : 1u));
    }
    for (bool addFailure : {false, true}) {
        // Arming failure must not restart: a persistent failure would reboot
        // forever and record nothing. Setup finishes and logging carries on
        // unwatched, with no progress feeds for a task that is not enrolled.
        watchdogWorld(); g_watchdog.failAdd = addFailure; g_watchdog.failConfigure = !addFailure;
        setup();
        assert(!gLoopWatchdogArmed && !g_watchdog.feeds && g_restarts == 0 && g_taskCreates == 1);
        loop(); feedLoopWatchdogProgress();
        assert(!gLoopWatchdogArmed && !g_watchdog.feeds && g_restarts == 0);
    }
    watchdogWorld(); g_failMutexCreate = true;
    try { setup(); assert(false); } catch (const Restarted&) {}
    assert(gLoopWatchdogArmed && g_watchdog.enrollments == 1 && g_watchdog.config.timeout_ms == startup_watchdog::STARTUP_WDT_TIMEOUT_MS);
    std::puts("Watchdog: staged startup, retained fallback, boot repair/E1003, degraded modes, hand-over, long progress, shutdown bounds, blocked lock and core-0 exclusion passed");
#endif
}
