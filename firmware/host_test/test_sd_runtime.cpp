// Exercise the production sketch with synthetic media and fixes, no hardware.
#define main fsRegressionMain
#include "test_fs.cpp"
#undef main
#include <cassert>
using namespace diagnostics;
static EventLog::Batch sdRuntimeBatch;

static void sdWorld(bool flash = true) {
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
    g_millis = 1000; SD.cardPresent = true;
    g_disk.failMount = !flash;
    setup();
    assert(gTripFS == &SD && gSdMounted && gFlashMounted == flash);
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
    noSpaceFaults(repeated);
    assert(SD.capacityQueries == queries && gTripFS == &SD);
    std::puts("SD runtime: full readable card vs failed query, E4001 repeat, no false space faults passed");

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
    gClockSet = false; drive(15, 1);
    assert(gState == IDLE && !gTripPath[0] && gTripFS == &SD);
    auto failedWrite = events();
    auto writes = codeRows(failedWrite, 4006);
    assert(writes.size() == 1 && writes[0].find("\"backend\":\"sd\"") != std::string::npos);
    assert(codeRows(failedWrite, 4007).empty() && g_sdDisk.files == savedSd);
    for (const auto& file : g_disk.files) assert(file.first.rfind("/trips/", 0) != 0);
    gClockSet = true; drive(15, 40);
    assert(gState == ACTIVE && gTripFS == &LittleFS);
    endTrip(false); noSpaceFaults(events());
    std::puts("SD runtime: active SD trip kept on SD, E4006 finalization, next trip on flash passed");

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
}
