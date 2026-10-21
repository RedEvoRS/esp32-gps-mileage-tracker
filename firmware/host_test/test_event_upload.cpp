// Exercise the production worker and netCycle with fake WiFi/HTTP/disks only.
#define DIAGNOSTICS_ENROLLMENT_ID 1ULL
#define DIAGNOSTICS_STATUS_HIGH_WATER 0ULL
#define main journalRegressionMain
#include "test_journal.cpp"
#undef main
#include <string>
#include <vector>
static std::vector<std::string> requests;
static void collect() {
    for (size_t i = 0; i < g_httpBodies.size(); ++i)
        if (g_httpUrls[i].find("/v1/device-events") != std::string::npos &&
            !g_httpBodies[i].empty() && g_httpBodies[i].back() == '}') requests.push_back(g_httpBodies[i]);
    g_httpBodies.clear(); g_httpUrls.clear(); g_httpHeaders.clear();
}
static void resetFaults() {
    for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
    for (auto& s : gNetConditions) { auto code = s.code; s = {}; s.code = code; }
    for (auto& s : gEventConditions) { auto code = s.code; s = {}; s.code = code; }
    gLoopFaults.~RuntimeFaults(); new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
    gNetFaults.~RuntimeFaults(); new (&gNetFaults) RuntimeFaults(gJournal, gNetConditions, sizeof(gNetConditions)/sizeof(gNetConditions[0]));
    gEventFaults.~RuntimeFaults(); new (&gEventFaults) RuntimeFaults(gJournal, gEventConditions, sizeof(gEventConditions)/sizeof(gEventConditions[0]));
}
static bool enrollAt(uint64_t revision) {
    assert(eventLock()); bool ok = gJournal.restoreStatusSequence(revision); eventUnlock(); return ok;
}
static EventLog::Backlog backlog() {
    assert(eventLock()); auto result = gJournal.backlog(); eventUnlock(); return result;
}
static bool readBatch() {
    assert(eventLock()); bool ok = gJournal.nextBatch(batch); eventUnlock(); return ok;
}
static void uploadWorld(bool enroll = true) {
    collect(); world(true, true, uploadJournalConfig()); g_millis = 1000;
    LittleFS.mkdir(TRIP_DIR); SD.mkdir(TRIP_DIR);
    resetFaults(); gEventRetry = {}; gEnrollmentChecked = false;
    gContactTick = 0; gContactUptime = gLastContact = 0; gContactKnown = false;
    gNetRetryMs = gNetLastAttemptMs = 0; gNetRetryNow = false; gClockSet = true;
    g_httpCode = 200; g_httpHook = nullptr; g_httpResponseHook = nullptr;
    g_httpResponse.clear(); g_httpAutoAck = true; g_httpDeclaredSize = -2;
    g_httpConnectMs = g_httpWriteMs = g_httpHeaderMs = g_httpByteMs = 0;
    g_httpConnectTimeouts.clear(); g_httpReadTimeouts.clear(); g_httpRequests = 0;
    gEventTransportFailures = 0; gEventRevisionBlocked = false; gHaveEventSnapshot = false;
    for (unsigned i = 0; i < 3; ++i) gEventCapturedVersions[i] = gEventSentVersions[i] = 0;
    WiFi = WiFiStub(); WiFi.scanSsids = {WIFI_SSID}; WiFi.connectionStatus = WL_CONNECTED;
    serviceJournal(); // geometry and mounted metadata
    if (enroll) assert(enrollAt(100));
    Preferences::durable64["enroll"] = enroll ? 1 : 0;
}
static void records(unsigned count) {
    for (unsigned i = 0; i < count; ++i) { submit(); assert(eventLock()); gJournal.service(1); eventUnlock(); }
}
static void trip(bool complete = true, const char* name = "/trips/pending.gpx") {
    g_sdDisk.files[name] = complete ? "<gpx></gpx>" : "<gpx>";
}
static RuntimeFaults::Condition& eventCondition(unsigned code) {
    for (auto& slot : gEventConditions) if (slot.code == code) return slot;
    std::abort();
}
static size_t eventCount(const std::string& body) {
    size_t count = 0, pos = body.find("\"events\":[") + 10;
    while ((pos = body.find("\"schema_version\":1", pos)) != std::string::npos) { ++count; ++pos; }
    return count;
}
static size_t logLines(const char* text) {
    return std::count_if(Serial.log.begin(), Serial.log.end(), [text](const std::string& line) {
        return line.find(text) != std::string::npos;
    });
}
static void allocationFailure(bool missing) {
    uploadWorld();
    g_psramAvailable = !missing; g_psramAllocationFails = !missing;
    initEventUpload();
    assert(!gEventBuffers && gEventAllocationAttempted && g_psramAllocations == 1);
    assert(logLines("WARNING: diagnostics upload disabled:") == 1);
    // A later heap recovery must not retry or produce a warning storm.
    g_psramAvailable = true; g_psramAllocationFails = false;
    initEventUpload(); initEventUpload();
    assert(g_psramAllocations == 1 && !gEventBuffers);
    records(3);
    assert(backlog().records == 3 && !eventsDue() && !eventSnapshot());
    unsigned scans = WiFi.scans;
    for (unsigned i = 0; i < 10; ++i) { drainEvents(); netCycle(); }
    assert(g_httpRequests == 0 && WiFi.scans == scans && backlog().records == 3);
    assert(logLines("WARNING: diagnostics upload disabled:") == 1);
    trip(); netCycle();
    assert(!hasPending() && g_httpRequests == 1);
    assert(g_httpUrls[0].find("/v1/device-events") == std::string::npos);
    assert(backlog().records >= 3 && g_psramAllocations == 1);
    // Journal service still accepts and persists new events after GPX upload.
    records(1); assert(readBatch() && batch.ids[0].sequence == 1);
    assert(logLines("WARNING: diagnostics upload disabled:") == 1);
    std::puts("PSRAM failure: one allocation/warning, local journal retained, diagnostics/radio disabled, GPX uploads passed");
}
int main(int argc, char** argv) {
    if (argc > 1 && (!std::strcmp(argv[1], "--no-psram") || !std::strcmp(argv[1], "--alloc-failure"))) {
        allocationFailure(!std::strcmp(argv[1], "--no-psram")); return 0;
    }
    initEventUpload();
    assert(gEventBuffers && g_psramAllocations == 1);
    assert(g_psramAllocationBytes == sizeof(EventUploadBuffers));
    assert(g_psramAllocationCaps == (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    initEventUpload(); assert(g_psramAllocations == 1);
#if DISPLAY_DEBUG
    reportEventHeap();
    assert(logLines("Heap: internal 123456 free (largest 65536), PSRAM 8123456 free") == 1);
#endif
    uploadWorld(); records(130); drainEvents();
#if DISPLAY_DEBUG
    assert(gEventHeapReported && logLines("Heap:") == 1);
#endif
    assert(g_httpBodies.size() == 3 && eventCount(g_httpBodies[0]) == 64 &&
           eventCount(g_httpBodies[1]) == 64 && eventCount(g_httpBodies[2]) == 2);
    assert(backlog().records == 0);
    assert(g_httpHeaders[0].at("X-Device-Id") == DEVICE_ID);
    assert(g_httpHeaders[0].at("Content-Type") == "application/json");
    assert(g_httpHeaders[0].at("Authorization") == std::string("Bearer ") + AUTH_TOKEN);
    for (const auto& body : g_httpBodies) assert(body.size() <= kRequestBytes);
    std::puts("Event upload: 64-record batches, complete byte cap, headers, exact acks passed");

    // Worst-case full-size events plus every possible active/observed field.
    uploadWorld(); records(80);
    for (size_t i = 0; i < 14; ++i) gLoopFaults.event(gLoopConditions[i].code, millis()).condition(true);
    for (unsigned i = 0; i < 5; ++i) gNetFaults.homeApAbsent();
    gNetFaults.event(5008, millis()).enumeration("target", "trip").integer("http_status", 503).condition(true);
    gEventFaults.event(5008, millis()).enumeration("target", "events").integer("http_status", 503).condition(true);
    assert(eventSnapshot());
    assert(std::strstr(gEventBuffers->snapshot,
        (std::string("\"firmware_version\":\"") + firmware::kVersion + "\"").c_str()));
    assert(std::strstr(gEventBuffers->snapshot, "\"5001\":5"));
    assert(std::strstr(gEventBuffers->snapshot, "\"active\":[{"));
    assert(std::strstr(gEventBuffers->snapshot, "\"observed\":["));
    assert(eventLock()); gJournal.service(16); eventUnlock(); drainEvents();
    assert(g_httpBodies[0].find("\"code\":5008") != std::string::npos);
    std::puts("Event upload: atomic task snapshots, active/observed merge and E5001 counter passed");

    // Bound the snapshot for all 64 slots, maximum numeric widths, and the
    // longest allowed version. Synthetic future codes need not be catalogued:
    // already accepted episodes are published without submitting new events.
    uploadWorld();
    RuntimeFaults::Condition maximum[64] = {};
    gLoopFaults.~RuntimeFaults(); new (&gLoopFaults) RuntimeFaults(gJournal, maximum, 64);
    for (unsigned i = 0; i < 64; ++i) {
        auto& slot = maximum[i];
        slot.code = 60000 + i; slot.observed = slot.desired = true;
        slot.episode = kMaxEventInteger; slot.acceptedMs = millis();
        gLoopFaults.event(slot.code, millis()).condition(true);
    }
    gJournalBoot.counter = gContactUptime = kMaxEventInteger;
    assert(enrollAt(kMaxEventInteger - 1));
    assert(eventSnapshot() && gEventObservationCount == 64);
    // dropped_events and E5001 are currently zero (one digit); budget 19
    // digits for each, and the longer "littlefs" backend instead of "sd".
    size_t worstSnapshot = std::strlen(gEventBuffers->snapshot) +
        32 - std::strlen(firmware::kVersion) + 2 * 18 + 6;
    assert(worstSnapshot < sizeof(gEventBuffers->snapshot));
    assert(eventBody(0) && eventBody(0) <= kRequestBytes);
    std::printf("Event upload: 64-slot snapshot with maximum version <= %zu/6144 bytes passed\n", worstSnapshot);

    // Exercise the complete-envelope boundary independently of today's short
    // catalog records. Whitespace padding is valid JSON and canonicalizes back
    // to the actual journal record; all successful bodies go through Python.
    uploadWorld(); records(64);
    for (auto& slot : gLoopConditions) gLoopFaults.event(slot.code, millis()).condition(true);
    assert(eventSnapshot()); assert(eventLock());
    assert(gJournal.nextBatch(gEventBuffers->batch)); eventUnlock();
    for (size_t i = 0; i < gEventBuffers->batch.count; ++i) {
        size_t size = std::strlen(gEventBuffers->batch.events[i]);
        std::memset(gEventBuffers->batch.events[i] + size, ' ', kEventBytes - size);
        gEventBuffers->batch.events[i][kEventBytes] = 0;
    }
    assert(eventBody(64) == 0); // <=64 events alone does not guarantee 32 KiB
    size_t fit = 64; while (!eventBody(fit)) --fit;
    assert(fit < 64 && eventBody(fit) <= kRequestBytes);
    requests.emplace_back(gEventBuffers->request);
    uploadWorld(); records(20); gEventRetry.eventBytes = 1024; drainEvents();
    assert(eventCount(g_httpBodies[0]) < 20 && backlog().records == 0);
    std::puts("Event upload: full 502-byte records, envelope overflow refusal and byte-limited batching passed");

    // Old persisted boot plus today's snapshot; retries preserve old IDs/JSON.
    uploadWorld(); records(3); std::string oldBoot = gJournalBoot.id;
    gJournal.~EventLog(); new (&gJournal) EventLog(&gSdJournalFs, &gFlashJournalFs, uploadJournalConfig());
    gJournalBoot = newJournalBoot();
    assert(gJournal.begin(gJournalBoot.id, gJournalBoot.counter, "test-new"));
    resetFaults(); drainEvents();
    assert(g_httpBodies[0].find(oldBoot) != std::string::npos && oldBoot != gJournalBoot.id);
    assert(g_httpBodies[0].find(gJournalBoot.id) != std::string::npos);
    std::puts("Event upload: old journal boot with new boot snapshot passed");

    // A newly finalized trip must win before the next diagnostics batch.
    uploadWorld(); records(130);
    g_httpResponseHook = [] { trip(); };
    drainEvents(); assert(g_httpRequests == 1 && hasPending());
    assert(backlog().records == 66);
    for (bool hold : {true, false}) {
        uploadWorld(); records(130);
        g_httpResponseHook = [hold] { if (hold) gNetHold = true; else gNetAbort = true; };
        drainEvents(); assert(g_httpRequests == 1);
        gNetHold = gNetAbort = false;
        assert(readBatch() && batch.ids[0].sequence == 1); // unvalidated reply retained
    }
    uploadWorld(); records(2); trip(false); drainEvents(); assert(g_httpRequests == 1); // #56
    uploadWorld(); records(2); trip(); drainEvents(); assert(g_httpRequests == 0);
    std::strcpy(gTripPath, "/trips/pending.gpx"); drainEvents(); assert(g_httpRequests == 1);
    std::puts("Event upload: mid-drain trip, hold/abort and eligible-before-counting stops passed");

    for (unsigned slow = 0; slow < 4; ++slow) {
        uploadWorld(); records(130);
        if (slow == 0) g_httpConnectMs = 20000;
        if (slow == 1) g_httpWriteMs = 20000;
        if (slow == 2) g_httpHeaderMs = 9000;
        if (slow == 3) g_httpByteMs = 100;
        uint32_t start = millis(); drainEvents();
        // The envelope exists even when connect times out before the stub can
        // record a wire request. Validate that complete generated body too.
        if (slow == 0) requests.emplace_back(gEventBuffers->request);
        assert(millis() - start <= kDrainMs);
        assert(backlog().records >= 66);
        assert(g_httpConnectTimeouts[0] <= kDrainMs);
        if (slow == 2) assert(g_httpConnectTimeouts.size() == 2 && g_httpConnectTimeouts[1] <= 1000);
        assert(gEventRetry.delay && gNetRetryMs == 0);
    }
    // Deadline wrap, a slow trickle, and reads that terminate exactly at budget.
    uploadWorld(); records(2); g_millis = UINT32_MAX - 4999; g_httpByteMs = 1000;
    uint32_t start = millis(); drainEvents(); assert(uint32_t(millis() - start) <= kDrainMs);
    assert(backlog().records >= 2 && readBatch() && batch.ids[0].sequence == 1);
    std::puts("Event upload: connect/write/header/trickle deadlines, remaining budget and rollover passed");

    for (int status : {-1, 401, 413, 400, 409, 503, 302}) {
        uploadWorld(); records(10); g_httpCode = status;
        gNetRetryMs = 240000; gNetLastAttemptMs = 123;
        drainEvents();
        unsigned code = status < 0 ? 5003 : status == 401 ? 5004 : status == 413 ? 5005 :
                        status == 400 ? 5006 : status == 409 ? 5007 : status == 503 ? 5008 : 5009;
        assert(eventCondition(code).episode);
        assert(gEventRetry.delay >= (status == 401 ? 3600000u : 60000u));
        assert(gNetRetryMs == 240000 && gNetLastAttemptMs == 123);
        assert(readBatch() && batch.ids[0].sequence == 1);
        assert(std::strstr(batch.events[0], "\"code\":1000"));
        unsigned requestsBefore = g_httpRequests;
        drainEvents(); assert(g_httpRequests == requestsBefore); // no recursive storm
        // GPX is allowed immediately despite diagnostic failure/backoff.
        trip(); g_httpCode = 200; gNetRetryMs = 0;
        netCycle(); assert(!hasPending() && gNetRetryMs == NET_TICK_MS);
        assert(gEventRetry.delay); // GPX did not reset diagnostic backoff
        g_millis += gEventRetry.delay;
        drainEvents(); assert(!gEventRetry.delay && !eventCondition(code).episode);
        if (status == 413) assert(gEventRetry.maxEvents == 32 && gEventRetry.eventBytes == 15 * 1024);
    }
    std::puts("Event upload: every HTTP failure class, bounded storms and independent trip/event retries passed");

    const char* invalid[] = {"broken", "{\"accepted\":[]}",
        "{\"accepted\":[{\"boot_id\":\"00000000000000000000000000000000\",\"sequence\":1}]}",
        "{\"accepted\":[],\"accepted\":[]}", "{\"accepted\":[]}junk"};
    for (auto reply : invalid) {
        uploadWorld(); records(2); g_httpAutoAck = false; g_httpResponse = reply;
        drainEvents(); assert(eventCondition(5010).episode);
        assert(readBatch() && batch.ids[0].sequence == 1 && batch.count >= 2);
        g_millis += gEventRetry.delay; g_httpAutoAck = true; drainEvents();
        assert(!eventCondition(5010).episode && backlog().records == 0);
    }
    uploadWorld(); records(2); g_httpResponseHook = [] {
        g_httpAutoAck = false;
        g_httpResponse = "{\"accepted\":[{\"boot_id\":\"" + std::string(gEventBuffers->batch.ids[0].bootId) +
            "\",\"sequence\":1},{\"boot_id\":\"" + std::string(gEventBuffers->batch.ids[0].bootId) + "\",\"sequence\":1}]}";
    };
    drainEvents(); assert(eventCondition(5010).episode);
    for (int size : {-1, 8193}) {
        uploadWorld(); records(1); g_httpDeclaredSize = size; drainEvents();
        assert(eventCondition(5010).episode && backlog().records >= 1);
    }
    // A valid 2xx may be 201; order of fields and IDs is not semantically significant.
    uploadWorld(); records(2); g_httpCode = 201;
    g_httpResponseHook = [] {
        g_httpAutoAck = false;
        std::string boot = gEventBuffers->batch.ids[0].bootId;
        g_httpResponse = "{ \"accepted\" : [{\"sequence\":2,\"boot_id\":\"" + boot +
            "\"},{\"sequence\":1,\"boot_id\":\"" + boot + "\"}] }";
    };
    drainEvents(); assert(backlog().records == 0);
    std::puts("Event upload: malformed/missing/extra/duplicate acks, full retry and 2xx/order semantics passed");

    for (int rejected : {400, 409}) {
        uploadWorld(); records(3); gEventRetry.maxEvents = 1;
        g_httpResponseHook = [rejected] { g_httpCode = eventCount(g_httpLastBody) ? rejected : 200; };
        drainEvents(); assert(g_httpRequests == 2);
        assert(g_sdDisk.files.count("/diag/rejected"));
        gJournal.~EventLog(); new (&gJournal) EventLog(&gSdJournalFs, &gFlashJournalFs, uploadJournalConfig());
        assert(gJournal.begin(gJournalBoot.id, 1, "test-reopen"));
        assert(readBatch() && batch.ids[0].sequence == 2);
        assert(gJournal.stats().dropped == 1 && gJournal.stats().pendingLoss == 1);
        // A snapshot conflict is not evidence against an event; no isolation.
        uploadWorld(); records(3); gEventRetry.maxEvents = 1; g_httpCode = rejected;
        drainEvents(); assert(!g_sdDisk.files.count("/diag/rejected"));
        assert(readBatch() && batch.ids[0].sequence == 1);
        if (rejected == 409) {
            uint64_t revision = 0;
            assert(eventLock()); assert(!gJournal.allocateStatusSequence(revision)); eventUnlock();
        }
    }
    std::puts("Event upload: durable singleton isolation only after empty snapshot probe; loss survives reopen passed");

    uploadWorld();
    // Prime the same observations a successful connection publishes.
    observeNetworkStack(4096); eventHttpFault(200); assert(eventSnapshot());
    eventSnapshotAccepted(); serverContact(); netCycle(); assert(g_httpRequests == 0);
    g_millis += kHeartbeatMs - 1; netCycle(); assert(g_httpRequests == 0);
    ++g_millis; netCycle(); assert(g_httpRequests == 1 && eventCount(g_httpBodies[0]) == 0);
    netCycle(); assert(g_httpRequests == 1);
    // A recovery must refresh the frozen snapshot on the next awake tick even
    // after its clear event was already drained with the previous snapshot.
    uploadWorld(); records(1); g_httpCode = 503; drainEvents();
    g_millis += gEventRetry.delay; g_httpCode = 200; drainEvents();
    assert(backlog().records == 0 && eventSnapshotChanged());
    unsigned recoveryRequests = g_httpRequests; netCycle();
    assert(g_httpRequests == recoveryRequests + 1 && !eventSnapshotChanged());
    uploadWorld(); g_httpCode = 503; netCycle(); assert(gEventRetry.delay);
    unsigned scans = WiFi.scans; netCycle(); assert(WiFi.scans == scans);
    uploadWorld(); records(2); WiFi.scanSsids.clear(); netCycle();
    assert(gEventRetry.delay && gNetRetryMs == 0);
    scans = WiFi.scans; for (int i = 0; i < 10; ++i) netCycle(); assert(WiFi.scans == scans);
    assert(readBatch() && batch.ids[0].sequence == 1);
    std::puts("Event upload: 24 h heartbeat, success contact refresh and absent-server radio backoff passed");

    // Explicit one-use enrollment; never silently reset after losing the card.
    uploadWorld(false); records(1); drainEvents(); assert(g_httpRequests == 1);
    assert(Preferences::durable64["enroll"] == 1);
    g_sdDisk.files.erase("/diag/ack-0"); g_sdDisk.files.erase("/diag/ack-1");
    gJournal.~EventLog(); new (&gJournal) EventLog(&gSdJournalFs, &gFlashJournalFs, uploadJournalConfig());
    assert(gJournal.begin(gJournalBoot.id, 1, "test-recovery"));
    gEnrollmentChecked = false; gEventRetry = {}; unsigned before = g_httpRequests;
    drainEvents(); assert(g_httpRequests == before && gEventRetry.delay);
    trip(); gNetRetryMs = 0; netCycle(); assert(!hasPending());
    std::puts("Event upload: explicit revision enrollment, rollback fail-closed and GPX independence passed");
    collect();
#if DISPLAY_DEBUG
    assert(gEventHeapReported && logLines("Heap:") == 0); // no repeated post-attempt reports
#endif
    assert(g_psramAllocations == 1); // all drains/retries reused the boot allocation
    if (argc > 1) for (const auto& body : requests) std::printf("REQUEST %s\n", body.c_str());
    std::printf("Event upload: %zu complete firmware requests; PSRAM Batch=%zu request=%zu snapshot=%zu ack=%zu bytes\n",
        requests.size(), sizeof(gEventBuffers->batch), sizeof(gEventBuffers->request), sizeof(gEventBuffers->snapshot), sizeof(gEventBuffers->ack));
    std::printf("PSRAM observations=%zu copy=%zu workspace=%zu bytes (host ABI)\n",
        sizeof(gEventBuffers->observations), sizeof(gEventBuffers->copy), sizeof(EventUploadBuffers));
}
