// Exercise the production sketch with synthetic media and fixes, no hardware.
#define main fsRegressionMain
#include "test_fs.cpp"
#undef main
#include <cassert>
using namespace diagnostics;
static EventLog::Batch sdRuntimeBatch;

static void sdWorld(bool flash = true, bool corruptFat = false, bool flashTripsFile = false,
                    bool cardPresent = true) {
    resetWorld();
    gSdUnavailable = false;
    SD.capacityQueries = 0;
    Preferences::durable64.clear();
    for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
    for (auto& s : gNetConditions) { auto code = s.code; s = {}; s.code = code; }
    for (auto& s : gEventConditions) { auto code = s.code; s = {}; s.code = code; }
    gLoopFaults.~RuntimeFaults(); new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
    gNetFaults.~RuntimeFaults(); new (&gNetFaults) RuntimeFaults(gJournal, gNetConditions, sizeof(gNetConditions)/sizeof(gNetConditions[0]));
    gEventFaults.~RuntimeFaults(); new (&gEventFaults) RuntimeFaults(gJournal, gEventConditions, sizeof(gEventConditions)/sizeof(gEventConditions[0]));
    gEventRetry = {}; gEventRevisionBlocked = gHaveEventSnapshot = false;
    gEnrollmentChecked = false;
    gContactTick = 0; gContactUptime = gLastContact = 0; gContactKnown = false;
    g_millis = 1000; SD.cardPresent = cardPresent;
    g_disk.failMount = !flash;
    if (flashTripsFile) {
        LittleFS.dirs.erase(TRIP_DIR);
        g_disk.files[TRIP_DIR] = "preserved flash file";
    }
    SD.corruptFat = corruptFat;
    setup();
    assert(gFlashMounted == flash && gSdMounted == (cardPresent && !corruptFat));
    assert(gTripFS == (gSdMounted ? static_cast<fs::FS*>(&SD) : static_cast<fs::FS*>(&LittleFS)));
    serviceJournal();
}

static std::vector<std::string> events() {
    std::vector<std::string> rows;
    assert(eventLock());
    assert(gJournal.nextBatch(sdRuntimeBatch));
    for (size_t i = 0; i < sdRuntimeBatch.count; ++i) rows.emplace_back(sdRuntimeBatch.events[i]);
    if (sdRuntimeBatch.count) assert(gJournal.acknowledge(sdRuntimeBatch, sdRuntimeBatch.ids, sdRuntimeBatch.count));
    eventUnlock();
    return rows;
}
static std::vector<std::string> codeRows(const std::vector<std::string>& rows, unsigned code) {
    std::vector<std::string> out;
    for (const auto& row : rows)
        if (row.find("\"code\":" + std::to_string(code) + ",") != std::string::npos) out.push_back(row);
    return out;
}
static void noSpaceFaults(const std::vector<std::string>& rows) {
    assert(codeRows(rows, 4004).empty() && codeRows(rows, 4005).empty());
}
static void checkStorage() {
    g_millis = gStorageDiagMs + FS_CHECK_MS;
    serviceRuntimeDiagnostics();
}

int main() {
    // Command success alone cannot justify starting the card's SPI bus.
    for (auto [reg, value] : std::initializer_list<std::pair<uint8_t, int>>{
            {XPOWERS_AXP2101_LDO_ONOFF_CTRL0, 0x09},
            {XPOWERS_AXP2101_LDO_VOL4_CTRL, 13},
            {XPOWERS_AXP2101_LDO_ONOFF_CTRL0, -1},
            {XPOWERS_AXP2101_LDO_VOL4_CTRL, -1}}) {
        resetWorld();
        SD.cardPresent = true;
        PMU.registerReadOverride[reg] = value;
        assert(!mountSdStorage());
        assert(SD.begins == 0 && !gSdSpi.started && !PMU.bldo1);
        assert(g_powerEvents == (std::vector<std::string>{
            "BLDO1.off", "BLDO1.voltage", "BLDO1.on", "BLDO1.off"
        }));
        assert(logHas("microSD card power rail (BLDO1) could not be verified"));
    }
    resetWorld();
    SD.cardPresent = true;
    assert(mountSdStorage() && SD.begins == 1 && PMU.bldo1 && PMU.bldo1Mv == 3300);
    SD.end();
    gSdSpi.end();
    std::puts("PASS BLDO1 read-back: four bad reads prevent mounting; healthy card mounts");

    // A successful begin requires a powered settle after end, even when
    // rejecting FAT or a directory the card reports missing after mkdir.
    for (int scenario = 0; scenario < 3; ++scenario) {
        resetWorld();
        SD.cardPresent = scenario != 0;
        SD.corruptFat = scenario == 1;
        if (scenario == 2) g_sdDisk.failOpenPath = TRIP_DIR;
        g_delayHook = [](uint32_t ms) { g_millis += ms; };
        g_taskDelayHook = [](uint32_t ms) {
            assert(ms == SD_POWER_OFF_SETTLE_MS);
            assert(SD.ends == 1 && !g_sdDisk.mounted && PMU.bldo1);
        };
        const uint32_t startedAt = millis();
        assert(!mountSdStorage());
        assert(SD.ends == 1 && !PMU.bldo1);
        assert(SD.endedAtMs == startedAt + SD_SETTLE_MS);
        assert(PMU.bldo1OffMs - SD.endedAtMs == (scenario == 0 ? 0 : SD_POWER_OFF_SETTLE_MS));
        assert(g_sdDisk.mkdirCalls == (scenario == 2 ? 1 : 0));
    }
    std::puts("SD runtime: mounted FAT and directory rejections settle 500 ms; never-mounted cut stays immediate");

    sdWorld(true, true);
    for (unsigned i = 0; i < kEventQueueSize && gJournal.stats().queued; ++i) serviceJournal();
    assert(!gJournal.stats().queued);
    auto rejected = events();
    auto damage = codeRows(rejected, 4011);
    assert(damage.size() == 1 && damage[0].find("\"reason\":\"fat_reserved\"") != std::string::npos);
    assert(damage[0].find("\"transition\":\"raise\"") != std::string::npos);
    assert(codeRows(rejected, 4001).size() == 1 && codeRows(rejected, 4008).empty());
    assert(gJournal.stats().backend == EventLog::Backend::LittleFs);
    assert(SD.ends == 1 && !gSdSpi.started && !PMU.bldo1);
    assert(logHas("microSD filesystem check failed (fat_reserved)"));
    assert(!g_sdDisk.mkdirCalls && !g_sdDisk.writeCalls && g_sdDisk.files.empty() && SD.dirs.empty());

    sdWorld();
    for (unsigned i = 0; i < kEventQueueSize && gJournal.stats().queued; ++i) serviceJournal();
    assert(!gJournal.stats().queued);
    auto healthy = events();
    assert(codeRows(healthy, 4011).empty() && codeRows(healthy, 4001).empty());
    assert(gJournal.stats().backend == EventLog::Backend::Sd);
    assert(g_sdDisk.mkdirCalls > 0);
    RuntimeFaults::Observation observations[64];
    size_t count = 0;
    assert(gLoopFaults.snapshot(observations, 64, count));
    const auto* verified = std::find_if(observations, observations + count, [](const auto& entry) {
        return entry.code == static_cast<uint16_t>(Code::SD_FILESYSTEM_DAMAGED);
    });
    assert(verified != observations + count && verified->observed && !verified->episode);
    std::puts("PASS SD mount guard: rejected card untouched, fallback events on flash, healthy SD selected");

    // Nonzero total with zero free means genuinely full, even at SD sizes
    // beyond ESP32 size_t. A failed capacity query means unavailable instead.
    sdWorld(false); events();
    g_sdDisk.capacity = uint64_t(32) * 1024 * 1024 * 1024;
    assert(fsFreeBytes() > UINT32_MAX && roomForNewTrip(millis()));
    g_sdDisk.capacity = g_sdDisk.used(); // readable but exactly full
    assert(!roomForNewTrip(millis()) && !gSdUnavailable && gTripFS == &SD);
    auto full = events();
    assert(codeRows(full, 4004).size() == 1 && codeRows(full, 4005).size() == 1);
    assert(codeRows(full, 4001).empty());
    SD.cardPresent = false;
    assert(SD.cardType() == CARD_SDHC); // removal does not update cached type
    checkStorage();
    auto removed = events();
    assert(gSdUnavailable && codeRows(removed, 4001).size() == 1);
    auto noStore = codeRows(removed, 4003);
    assert(noStore.size() == 1 && noStore[0].find("\"transition\":\"raise\"") != std::string::npos);
    for (unsigned code : {4004, 4005}) {
        auto rows = codeRows(removed, code);
        assert(rows.size() == 1 && rows[0].find("\"transition\":\"clear\"") != std::string::npos);
        assert(rows[0].find("free_bytes") == std::string::npos);
    }
    g_millis += 60000; // admission messages retain the existing minute throttle
    assert(!roomForNewTrip(millis()) && logHas("trip storage unavailable"));
    events();
    int queries = SD.capacityQueries;
    g_millis += RuntimeFaults::kRepeatMs;
    storageConditions(fsFreeBytes(), millis());
    auto repeated = events();
    auto sdRepeat = codeRows(repeated, 4001);
    assert(sdRepeat.size() == 1 && sdRepeat[0].find("\"transition\":\"repeat\"") != std::string::npos);
    auto noStoreRepeat = codeRows(repeated, 4003);
    assert(noStoreRepeat.size() == 1 && noStoreRepeat[0].find("\"transition\":\"repeat\"") != std::string::npos);
    noSpaceFaults(repeated);
    assert(SD.capacityQueries == queries && gTripFS == &SD);
    std::puts("SD runtime: full readable card vs failed query, E4001/E4003 repeat, no false space faults passed");

    // Both physical removal and a present but unreadable card switch only to
    // the already-mounted flash queue, without formatting or remounting SD.
    for (bool removedCard : {false, true}) {
        sdWorld(); events();
        int mounts = SD.begins;
        if (removedCard) SD.cardPresent = false;
        else g_sdDisk.readsFail = true;
        checkStorage();
        auto rows = events();
        assert(gSdUnavailable && gTripFS == &LittleFS && codeRows(rows, 4001).size() == 1);
        noSpaceFaults(rows);
        assert(roomForNewTrip(millis()));
        drive(15, 40); assert(gState == ACTIVE && gTripFS == &LittleFS);
        std::string path = gTripPath; endTrip(false);
        assert(wellFormedShape(g_disk.files.at(path)));
        g_sdDisk.files["/trips/retained.gpx"] = "<gpx></gpx>";
        auto retained = g_sdDisk.files;
        assert(hasPending() && !hasPending(SD) && !uploadPending(SD));
        assert(uploadPending() && g_httpRequests == 1 && !g_disk.files.count(path));
        assert(g_sdDisk.files == retained && SD.begins == mounts && SD.ends == 0);
        SD.cardPresent = true; g_sdDisk.readsFail = false;
        assert(roomForNewTrip(millis()) && gTripFS == &LittleFS && gSdUnavailable);
        assert(!g_disk.formatRequested && !g_sdDisk.formatRequested && g_lockDepth == 0);
    }
    std::puts("SD runtime: idle removal/unresponsive card -> flash trips/uploads, retained SD skipped until reboot passed");

    // An active file remains bound to SD, including its untimed RAM tail.
    // The next attempted point uses E4006 and cannot spill onto flash.
    sdWorld(); events(); drive(15, 40); events();
    std::string activePath = gTripPath;
    auto savedSd = g_sdDisk.files;
    SD.cardPresent = false;
    checkStorage();
    assert(gTripFS == &SD && gState == ACTIVE && activePath == gTripPath);
    auto activeRows = events();
    assert(codeRows(activeRows, 4001).size() == 1); noSpaceFaults(activeRows);
    // Healthy flash is the fallback for the next trip: no critical E4003.
    assert(codeRows(activeRows, 4003).empty());
    gClockSet = false; drive(15, 1);
    assert(gState == IDLE && !gTripPath[0] && gTripFS == &SD);
    auto failedWrite = events();
    auto writes = codeRows(failedWrite, 4006);
    assert(writes.size() == 1 && writes[0].find("\"backend\":\"sd\"") != std::string::npos);
    assert(codeRows(failedWrite, 4007).empty() && g_sdDisk.files == savedSd);
    assert(codeRows(failedWrite, 4003).empty());
    for (const auto& file : g_disk.files) assert(file.first.rfind("/trips/", 0) != 0);
    gClockSet = true; drive(15, 40);
    assert(gState == ACTIVE && gTripFS == &LittleFS);
    endTrip(false);
    auto flashRows = events();
    noSpaceFaults(flashRows); assert(codeRows(flashRows, 4003).empty());
    std::puts("SD runtime: active SD trip kept on SD, E4006 finalization, next trip on flash, no E4003 passed");

    sdWorld(true, false, true);
    assert(gFlashMounted && !tripStoreAvailable(LittleFS));
    assert(!hasPending(LittleFS) && !uploadPending(LittleFS));
    events(); drive(15, 40); events();
    assert(gState == ACTIVE && gTripFS == &SD);
    SD.cardPresent = false;
    checkStorage();
    auto blocked = events();
    auto sdLost = std::find_if(blocked.begin(), blocked.end(), [](const std::string& row) {
        return row.find("\"code\":4001,") != std::string::npos;
    });
    auto noRecording = std::find_if(sdLost, blocked.end(), [](const std::string& row) {
        return row.find("\"code\":4003,") != std::string::npos;
    });
    assert(sdLost != blocked.end() && noRecording != blocked.end());
    assert(noRecording->find("\"transition\":\"raise\"") != std::string::npos);
    gClockSet = false; drive(15, 1);
    assert(gState == IDLE && !gTripPath[0] && gTripFS == &SD);
    assert(!roomForNewTrip(millis()) && gTripFS == &SD);
    serviceJournal();
    assert(gFlashMounted && gJournal.stats().backend == EventLog::Backend::LittleFs);
    assert(g_disk.files.at(TRIP_DIR) == "preserved flash file" && !LittleFS.dirs.count(TRIP_DIR));
    for (const auto& file : g_disk.files) assert(file.first.rfind("/trips/", 0) != 0);
    std::puts("SD runtime: flash /trips file preserved, E4001 then E4003 mid-trip, diagnostics remain on flash");

    sdWorld(true, false, true, false);
    assert(gDegraded.mode() == startup::Mode::NoStorage && !tripStoreAvailable(LittleFS));
    assert(gJournal.stats().backend == EventLog::Backend::LittleFs);
    g_millis += gDegraded.waitMs();
    loop();
    assert(g_restarts == 0 && gDegraded.waitMs() == 120000);
    assert(gFlashMounted && g_disk.mounted && !tripStoreAvailable(LittleFS));
    assert(g_disk.files.at(TRIP_DIR) == "preserved flash file");
    std::puts("SD runtime: degraded trip-store retry preserves a diagnostic-only flash mount");

    sdWorld(); events(); drive(15, 12); events();
    activePath = gTripPath;
    SD.cardPresent = false;
    storageConditions(fsFreeBytes(), millis()); events();
    endTrip(true); // no next point: even a micro-trip cannot claim deletion
    assert(gState == IDLE && g_sdDisk.files.count(activePath));
    assert(codeRows(events(), 4006).size() == 1);
    std::puts("SD runtime: unavailable micro-trip finalization reports E4006 and preserves the SD file passed");

    // Flash still obeys its original start/stop guards and journal reserve.
    sdWorld(); events();
    g_disk.capacity = g_disk.used() + kLittleFsJournalReserve + FS_STOP_FREE - 1;
    SD.cardPresent = false;
    assert(!roomForNewTrip(millis()) && gTripFS == &LittleFS);
    auto flashFull = events();
    for (unsigned code : {4004, 4005}) {
        auto rows = codeRows(flashFull, code);
        assert(rows.size() == 1 && rows[0].find("\"backend\":\"littlefs\"") != std::string::npos);
    }
    std::puts("SD runtime: insufficient flash capacity still refuses recording with LittleFS space faults passed");

    // Use the real journal adapter, revision allocator, snapshot and HTTP
    // worker. SD remains mounted/cached, but its gate must prevent reuse.
    sdWorld();
    assert(eventLock()); assert(gJournal.restoreStatusSequence(100)); eventUnlock();
    g_sdDisk.files["/trips/old.gpx"] = "<gpx></gpx>";
    SD.cardPresent = false;
    checkStorage();
    for (unsigned i = 0; i < kEventQueueSize; ++i) serviceJournal();
    auto journalSd = g_sdDisk.files;
    g_httpAutoAck = true; g_httpRequests = 0; g_httpBodies.clear();
    drainEvents(true);
    assert(!g_httpBodies.empty());
    const auto& body = g_httpBodies.front();
    assert(body.find("\"storage_backend\":\"littlefs\"") != std::string::npos);
    auto active = body.substr(body.find("\"active\":["), body.find("\"observed\":[") - body.find("\"active\":["));
    assert(active.find("\"code\":4001,") != std::string::npos);
    assert(body.find("\"code\":4004,") == std::string::npos && body.find("\"code\":4005,") == std::string::npos);
    assert(Preferences::durable64["revstate"] & 2); // SD stale, flash current
    assert(!(Preferences::durable64["revstate"] & 5));
    assert(gJournal.stats().queued == 0 && g_sdDisk.files == journalSd && g_lockDepth == 0);
    std::puts("SD runtime: E4001 uploaded through LittleFS, cached SD authority gated, NVS SD stale bit persisted passed");

    // Issue #812: boot-time SD capacity failure with LittleFS unavailable must enter
    // NoStorage degraded mode, leave SD mounted/powered with gSdUnavailable latched
    // (no immediate BLDO1 cut without settle), raise E4003, and skip fsck on SD.
    {
        resetWorld();
        gSdUnavailable = false;
        SD.capacityQueries = 0;
        Preferences::durable64.clear();
        for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
        gLoopFaults.~RuntimeFaults();
        new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
        g_millis = 1000; SD.cardPresent = true; g_disk.failMount = true;
        const std::string torn = std::string(GPX_PROLOGUE) + "<trkpt></trkpt>\n<trkpt";
        g_sdDisk.files["/trips/torn.gpx"] = torn;
        g_sdDisk.onMount = [] { g_sdDisk.readsFail = false; };
        SD.onCapacity = [] { g_sdDisk.readsFail = true; };
        setup();
        assert(gDegraded.mode() == startup::Mode::NoStorage);
        assert(gSdMounted && !gFlashMounted && PMU.bldo1 && SD.ends == 0 && gSdUnavailable);
        assert(g_sdDisk.files["/trips/torn.gpx"] == torn); // fsck did not touch failed SD
        for (unsigned i = 0; i < kEventQueueSize && gJournal.stats().queued; ++i) serviceJournal();
        auto rows = events();
        auto e4003 = codeRows(rows, 4003);
        assert(e4003.size() == 1 && e4003[0].find("\"transition\":\"raise\"") != std::string::npos);
        assert(codeRows(rows, 4001).size() == 1 && codeRows(rows, 4002).size() == 1);

        // NoStorage retry probe must repeat the SD capacity query instead of
        // restarting forever every 60 s on a card that mounts but fails capacity.
        const int queriesBeforeRetry = SD.capacityQueries;
        g_millis += gDegraded.waitMs();
        loop();
        assert(g_restarts == 0 && gDegraded.waitMs() == 120000);
        assert(SD.capacityQueries == queriesBeforeRetry + 1);
        assert(!gSdMounted && !gFlashMounted);
        // Second retry with the same fault still backs off without restarting.
        g_millis += gDegraded.waitMs();
        loop();
        assert(g_restarts == 0 && gDegraded.waitMs() == 240000);
        assert(SD.capacityQueries == queriesBeforeRetry + 2);
        // Once the card's capacity query succeeds, the next retry restarts cleanly.
        SD.onCapacity = nullptr;
        g_millis += gDegraded.waitMs();
        bool restarted = false;
        try { loop(); } catch (const Restarted&) { restarted = true; }
        assert(restarted && g_restarts == 1);
        g_sdDisk.onMount = nullptr;
    }
    // Boot-time SD capacity failure with LittleFS mounted leaves SD mounted with
    // gSdUnavailable latched, falls back to LittleFS, skips fsck on SD, repairs
    // LittleFS, raises E4001, and clears E4003.
    {
        resetWorld();
        gSdUnavailable = false;
        SD.capacityQueries = 0;
        Preferences::durable64.clear();
        for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
        gLoopFaults.~RuntimeFaults();
        new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
        g_millis = 1000; SD.cardPresent = true; g_disk.failMount = false;
        const std::string torn = std::string(GPX_PROLOGUE) + "<trkpt></trkpt>\n<trkpt";
        g_sdDisk.files["/trips/torn.gpx"] = torn;
        g_disk.files["/trips/flash_torn.gpx"] = torn;
        SD.onCapacity = [] { g_sdDisk.readsFail = true; };
        setup();
        SD.onCapacity = nullptr;
        assert(gDegraded.mode() == startup::Mode::Normal);
        assert(gSdMounted && gFlashMounted && gTripFS == &LittleFS && PMU.bldo1 && SD.ends == 0 && gSdUnavailable);
        assert(g_sdDisk.files["/trips/torn.gpx"] == torn); // fsck skipped failed SD
        assert(g_disk.files["/trips/flash_torn.gpx"] ==
               std::string(GPX_PROLOGUE) + "<trkpt></trkpt>\n" + GPX_EPILOGUE); // flash still repaired
        for (unsigned i = 0; i < kEventQueueSize && gJournal.stats().queued; ++i) serviceJournal();
        auto rows = events();
        assert(codeRows(rows, 4001).size() == 1);
        assert(codeRows(rows, 4003).empty());
        RuntimeFaults::Observation obs[64]; size_t count = 0;
        assert(gLoopFaults.snapshot(obs, 64, count));
        const auto* noStorageObs = std::find_if(obs, obs + count, [](const auto& e) {
            return e.code == static_cast<uint16_t>(Code::NO_RECORDING_STORAGE);
        });
        assert(noStorageObs != obs + count && noStorageObs->observed && !noStorageObs->episode);
    }
    // Issue #812: setup() uses one authoritative startup capacity query rather
    // than re-querying after the mode decision (which could latch gSdUnavailable
    // and raise E4003 while remaining in Normal mode with no usable store).
    {
        resetWorld();
        gSdUnavailable = false;
        SD.capacityQueries = 0;
        Preferences::durable64.clear();
        for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
        gLoopFaults.~RuntimeFaults();
        new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
        g_millis = 1000; SD.cardPresent = true; g_disk.failMount = true;
        SD.onCapacity = [] { if (SD.capacityQueries >= 2) g_sdDisk.readsFail = true; };
        setup();
        assert(SD.capacityQueries == 1);
        assert(gDegraded.mode() == startup::Mode::Normal);
        assert(tripStoreAvailable(*gTripFS) && !gSdUnavailable);
        for (unsigned i = 0; i < kEventQueueSize && gJournal.stats().queued; ++i) serviceJournal();
        auto rows = events();
        assert(codeRows(rows, 4003).empty());
        SD.onCapacity = nullptr;
    }
    // Issue #812: quarantined SD card (boot capacity failure) + healthy LittleFS +
    // cancelled shutdown must NOT reboot the tracker, must keep SD out (never
    // silently re-enabled even if the card starts responding), and must continue
    // recording trips on LittleFS.
    for (bool cardRecovers : {false, true}) {
        resetWorld();
        gSdUnavailable = false;
        SD.capacityQueries = 0;
        g_restarts = 0;
        Preferences::durable64.clear();
        for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
        gLoopFaults.~RuntimeFaults();
        new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
        g_millis = 1000; SD.cardPresent = true; g_disk.failMount = false;
        SD.onCapacity = [] { g_sdDisk.readsFail = true; };
        setup();
        SD.onCapacity = nullptr;
        assert(gDegraded.mode() == startup::Mode::Normal);
        assert(gSdMounted && gFlashMounted && gTripFS == &LittleFS && gSdUnavailable);
        events();
        const int sdBeginsBefore = SD.begins;
        if (cardRecovers) g_sdDisk.readsFail = false; // card would mount if retried
        else SD.cardPresent = false;                  // remount would fail if retried
        PMU.vbusIn = false;
        g_disk.onUnmount = [] { PMU.vbusIn = true; }; // cancel shutdown after unmount
        powerOffForVbus("idle_battery");
        g_disk.onUnmount = nullptr;
        assert(g_restarts == 0 && PMU.shutdownCalls == 0);
        assert(!gSdMounted && gSdUnavailable && !tripStoreAvailable(SD) && !PMU.bldo1);
        assert(SD.begins == sdBeginsBefore); // quarantined card never remounted
        assert(gFlashMounted && gTripFS == &LittleFS && tripStoreAvailable(LittleFS));
        assert(g_lockDepth == 0 && !gNetHold && !gNetAbort);
        assert(roomForNewTrip(millis()));
        drive(15, 40);
        assert(gState == ACTIVE && gTripFS == &LittleFS);
        std::string path = gTripPath;
        endTrip(false);
        assert(gState == IDLE && wellFormedShape(g_disk.files.at(path)));
        assert(gSdUnavailable && !gSdMounted);
    }
    std::puts("SD runtime: boot capacity failure, no-storage probe, single boot query, cancelled shutdown (#812) passed");

    sdWorld();
    PMU.vbusIn = false;
    g_disk.onUnmount = [] { PMU.vbusIn = true; SD.corruptFat = true; };
    g_taskDelayHook = [](uint32_t ms) {
        if (ms == 20) serviceJournal();
        if (ms == SD_POWER_OFF_SETTLE_MS)
            assert(gNetHold && g_lockDepth > 0 && !g_sdDisk.mounted && PMU.bldo1);
    };
    bool restarted = false;
    try { powerOffForVbus("idle_battery"); } catch (const Restarted&) { restarted = true; }
    assert(restarted && PMU.shutdownCalls == 0 && SD.begins == 2 && SD.ends == 2);
    assert(!PMU.bldo1 && PMU.bldo1OffMs - SD.endedAtMs == SD_POWER_OFF_SETTLE_MS);
    std::puts("SD runtime: rejected cancelled-shutdown remount settles with filesystem locked and network held");
}
