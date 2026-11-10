// Included after trip eligibility helpers. One core-0 consumer, with the same
// zero-wait FS discipline as journal maintenance. No network while holding FS.
#include "diag_http.h"
#include <esp_heap_caps.h>

// One boot-only allocation, exclusively owned by the core-0 worker after
// setup. Never fall back to internal RAM, allocate on a task stack, or retry
// after failure. Producers and their cross-core atomic publications stay in
// internal RAM. No ISR/cache-disabled callback may access this workspace.
struct EventUploadBuffers {
    diagnostics::EventLog::Batch batch;
    char request[diagnostics::kRequestBytes + 1];
    char snapshot[6144], ack[diagnostics::kAckBytes + 1];
    diagnostics::RuntimeFaults::Observation observations[64], copy[64];
};
static EventUploadBuffers* gEventBuffers = nullptr;
static bool gEventAllocationAttempted = false;
static void initEventUpload() {
    if (gEventAllocationAttempted) return;
    gEventAllocationAttempted = true;
    void* memory = heap_caps_malloc(sizeof(EventUploadBuffers), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (memory) gEventBuffers = new (memory) EventUploadBuffers{}; // lifetime is the whole boot
    else Serial.println("WARNING: diagnostics upload disabled: PSRAM allocation failed; journaling locally.");
    // No catalog code describes PSRAM/workspace allocation failure. Do not
    // misreport it as NET_TASK_FAILED or NET_STACK_LOW; the GPX task still runs.
}

static void reportEventHeap() {
    if (!debugMode()) return;
    constexpr uint32_t internal = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
    Serial.printf("Heap: internal %lu free (largest %lu), PSRAM %lu free\n",
                  (unsigned long)heap_caps_get_free_size(internal),
                  (unsigned long)heap_caps_get_largest_free_block(internal),
                  (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}
static bool gEventHeapReported = false; // worker-owned, one post-attempt line
static diagnostics::UploadRetry gEventRetry;
static size_t gEventObservationCount = 0;
static uint8_t gEventTransportFailures = 0;
static bool gEnrollmentChecked = false;
static bool gEventRevisionBlocked = false;
static bool gHaveEventSnapshot = false;
static unsigned gEventCapturedVersions[3] = {}, gEventSentVersions[3] = {};
static void eventSnapshotAccepted() {
    gHaveEventSnapshot = true;
    for (unsigned i = 0; i < 3; ++i) gEventSentVersions[i] = gEventCapturedVersions[i];
}
static bool eventSnapshotChanged() {
    return !gHaveEventSnapshot || gLoopFaults.publicationRevision() != gEventSentVersions[0] ||
        gNetFaults.publicationRevision() != gEventSentVersions[1] ||
        gEventFaults.publicationRevision() != gEventSentVersions[2];
}

static bool eventLock() {
    if (!gJournalReady || !journalMayRun() || gNetAbort.load() ||
        xSemaphoreTake(gFsMutex, 0) != pdPASS) return false;
    if (!journalMayRun()) { xSemaphoreGive(gFsMutex); return false; }
    return true;
}
static void eventUnlock() {
    journalStorageFaults();
    if (!journalMayRun()) gJournal.invalidateStorage();
    xSemaphoreGive(gFsMutex);
}
static void eventHttpFault(int status, const char* ackError = nullptr) {
    if (gNetAbort.load() || gNetHold.load()) return;
    bool success = status >= 200 && status < 300 && !ackError;
    diagnostics::Code code = status <= 0 ? diagnostics::Code::UPLOAD_TRANSPORT_FAILED : status == 401 ? diagnostics::Code::UPLOAD_AUTH_FAILED : status == 413 ? diagnostics::Code::UPLOAD_TOO_LARGE :
        status == 400 ? diagnostics::Code::UPLOAD_INVALID : status == 409 ? diagnostics::Code::UPLOAD_CONFLICT : status >= 500 && status <= 599 ? diagnostics::Code::UPLOAD_SERVER_FAILED : diagnostics::Code::UPLOAD_HTTP_OTHER;
    if (status <= 0) gEventTransportFailures = min(unsigned(gEventTransportFailures) + 1, 255u);
    if (success) gEventTransportFailures = 0;
    for (auto c : diagnostics::kHttpFaultCodes) {
        if (!success && (c != code || (status >= 200 && status < 300))) continue;
        gEventFaults.event(c, millis()).enumeration("target", "events");
        if (c == diagnostics::Code::UPLOAD_TRANSPORT_FAILED) gEventFaults.integer("attempts", gEventTransportFailures);
        else if (status >= 100 && status <= 599) gEventFaults.integer("http_status", status);
        gEventFaults.condition(!success);
    }
    if (ackError || success)
        gEventFaults.event(diagnostics::Code::EVENT_ACK_INVALID, millis()).enumeration("reason", ackError ? ackError : "parse").condition(bool(ackError));
}

static bool appendObservations(const diagnostics::RuntimeFaults& faults, unsigned& revision) {
    size_t count = 0;
    if (!faults.snapshot(gEventBuffers->copy, 64, count, &revision)) return false;
    for (size_t i = 0; i < count; ++i) {
        auto observation = gEventBuffers->copy[i];
        if (!observation.observed) continue;
        size_t j = 0;
        for (; j < gEventObservationCount; ++j) if (gEventBuffers->observations[j].code == observation.code) break;
        if (j == gEventObservationCount) {
            if (j == 64) return false;
            gEventBuffers->observations[gEventObservationCount++] = observation;
        } else if (observation.episode) gEventBuffers->observations[j] = observation;
    }
    return true;
}

// Enrollment is an explicit operator action, consumed in NVS BEFORE resetting
// the mirrored allocator. Reusing a firmware configuration after metadata loss must
// never silently enroll at zero again. A failed attempt consumes the token too.
static void enrollEventRevision() {
    if (gEnrollmentChecked) return;
    gEnrollmentChecked = true;
#if defined(DIAGNOSTICS_ENROLLMENT_ID) && defined(DIAGNOSTICS_STATUS_HIGH_WATER)
    Preferences settings;
    if (!settings.begin("diag", false)) return;
    uint64_t used = settings.isKey("enroll") ? settings.getULong64("enroll", UINT64_MAX) : 0;
    uint64_t token = DIAGNOSTICS_ENROLLMENT_ID;
    if (token > used && token <= diagnostics::kMaxEventInteger &&
        settings.putULong64("enroll", token) == sizeof(uint64_t) &&
        settings.getULong64("enroll", 0) == token) {
        if (!gJournal.restoreStatusSequence(DIAGNOSTICS_STATUS_HIGH_WATER))
            Serial.printf("Diagnostics enrollment refused: local minimum high-water %llu; "
                          "also read server high-water, then use a higher enrollment ID. "
                          "Check mounted journals and NVS if this minimum was met.\n",
                          (unsigned long long)gJournal.minimumStatusSequence());
    }
    settings.end();
#endif
}

static bool eventSnapshot() {
    if (!gEventBuffers || gEventRevisionBlocked) return false;
    gEventObservationCount = 0;
    if (!appendObservations(gLoopFaults, gEventCapturedVersions[0]) ||
        !appendObservations(gNetFaults, gEventCapturedVersions[1]) ||
        !appendObservations(gEventFaults, gEventCapturedVersions[2])) return false;
    if (!eventLock()) return false;
    enrollEventRevision();
    uint64_t revision = 0;
    bool allocated = gJournal.allocateStatusSequence(revision);
    auto stats = gJournal.stats();
    eventUnlock();
    if (!allocated) return false;
    diagnostics::JsonWriter json{gEventBuffers->snapshot, sizeof(gEventBuffers->snapshot)};
    json.add("{\"boot_id\":\"%s\",\"status_sequence\":%llu,\"uptime_ms\":%llu,"
             "\"boot_counter\":%llu,\"firmware_version\":\"%s\",\"storage_backend\":\"%s\",\"dropped_events\":%llu,\"active\":[",
             gJournalBoot.id, (unsigned long long)revision, (unsigned long long)contactUptime(),
             (unsigned long long)gJournalBoot.counter, firmware::kVersion,
             stats.backend == diagnostics::EventLog::Backend::Sd ? "sd" :
             stats.backend == diagnostics::EventLog::Backend::LittleFs ? "littlefs" : "ram",
             (unsigned long long)stats.dropped);
    bool comma = false;
    for (size_t i = 0; i < gEventObservationCount; ++i) {
        auto& s = gEventBuffers->observations[i];
        if (!s.episode) continue;
        json.add("%s{\"code\":%u,\"episode_id\":\"%s:%llu\"}", comma ? "," : "", s.code,
                 gJournalBoot.id, (unsigned long long)s.episode); comma = true;
    }
    json.add("],\"observed\":[");
    for (size_t i = 0; i < gEventObservationCount; ++i)
        json.add("%s%u", i ? "," : "", gEventBuffers->observations[i].code);
    json.add("],\"counters\":{\"5001\":%llu}}", (unsigned long long)gNetFaults.homeApMisses());
    return json.ok;
}
static size_t eventBody(size_t count) {
    diagnostics::JsonWriter json{gEventBuffers->request, sizeof(gEventBuffers->request)};
    json.add("{\"schema_version\":1,\"checkin\":%s,\"events\":[", gEventBuffers->snapshot);
    for (size_t i = 0; i < count; ++i) json.add("%s%s", i ? "," : "", gEventBuffers->batch.events[i]);
    json.add("]}");
    return json.ok && json.size <= diagnostics::kRequestBytes ? json.size : 0;
}
class EventBodyStream : public Stream {
    size_t pos_ = 0, size_; uint32_t started_;
public:
    EventBodyStream(size_t size, uint32_t started) : size_(size), started_(started) {}
    int available() override {
        if (gNetHold.load() || gNetAbort.load() || uint32_t(millis() - started_) >= diagnostics::kDrainMs) return -1;
        return int(size_ - pos_);
    }
    int read() override { return pos_ < size_ ? uint8_t(gEventBuffers->request[pos_++]) : -1; }
    int peek() override { return pos_ < size_ ? uint8_t(gEventBuffers->request[pos_]) : -1; }
    size_t readBytes(char* out, size_t size) override {
        size = min(size, size_ - pos_); std::memcpy(out, gEventBuffers->request + pos_, size); pos_ += size; return size;
    }
    size_t write(uint8_t) override { return 0; }
};
static bool eventTimeLeft(uint32_t started) {
    return !gNetHold.load() && !gNetAbort.load() && uint32_t(millis() - started) < diagnostics::kDrainMs;
}
static int postEvents(uint32_t started, size_t count, const char*& ackError) {
    size_t bytes = eventBody(count);
    if (!bytes || !eventTimeLeft(started)) return -1;
    diagnostics::DeadlineClient client(started);
    HTTPClient http;
    http.begin(client, String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/v1/device-events");
    if (!eventTimeLeft(started)) { http.end(); return -1; }
    http.setReuse(false);
    http.setConnectTimeout(diagnostics::kDrainMs - uint32_t(millis() - started));
    http.setTimeout(diagnostics::kDrainMs - uint32_t(millis() - started));
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", String("Bearer ") + AUTH_TOKEN);
    http.addHeader("X-Device-Id", DEVICE_ID);
    EventBodyStream body(bytes, started);
    int code = http.sendRequest("POST", &body, bytes);
    if (code >= 200 && code < 300) {
        // HTTPClient parses headers; consume only a bounded Content-Length body.
        // No getString(): it can grow or reset a timeout on a slow trickle.
        int size = http.getSize();
        if (size < 0 || size > int(diagnostics::kAckBytes)) ackError = "parse";
        else {
            size_t read = 0;
            auto* stream = http.getStreamPtr();
            while (stream && read < size_t(size) && eventTimeLeft(started)) {
                if (stream->available()) {
                    int c = stream->read(); if (c >= 0) gEventBuffers->ack[read++] = char(c);
                } else if (!stream->connected()) break;
                else vTaskDelay(pdMS_TO_TICKS(1));
            }
            ackError = read == size_t(size) && eventTimeLeft(started) ?
                diagnostics::AckParser(gEventBuffers->ack, read).parse(gEventBuffers->batch, count) : "parse";
        }
    }
    http.end();
    observeNetworkStack((unsigned)uxTaskGetStackHighWaterMark(nullptr));
    return code;
}

static void drainEvents(bool afterTrips = false) {
    if (!gEventBuffers || !gJournalReady || !gEventRetry.ready(millis())) return;
    uint32_t started = millis();
    struct IoBudget {
        explicit IoBudget(uint32_t now) { gEventIoStarted = now; gEventIoBudget = true; }
        ~IoBudget() { gEventIoBudget = false; }
    } ioBudget(started);
    bool sent = false;
    // Revisions are allocated once per drain (at most once per network tick).
    // All batches/probes in this <=10 s opportunity reuse identical snapshot
    // bytes. The next opportunity captures new observations and a new revision.
    if (!eventTimeLeft(started) || hasPending()) return;
    if (!eventSnapshot()) { gEventRetry.failed(millis(), -1); return; }
    while (eventTimeLeft(started) && !hasPending()) {
        if (!eventLock()) return;
        size_t snapshotBytes = std::strlen(gEventBuffers->snapshot) + 64;
        size_t budget = min(gEventRetry.eventBytes, diagnostics::kRequestBytes - snapshotBytes);
        bool ready = gJournal.nextBatch(gEventBuffers->batch, gEventRetry.maxEvents, budget);
        eventUnlock();
        if (!ready) { gEventRetry.failed(millis(), -1); return; }
        if (!gEventBuffers->batch.count && (sent || (!afterTrips && !heartbeatDue() && !eventSnapshotChanged()))) return;
        // Eligibility may have changed during journal I/O; never count a trip
        // before checking completeness and the active-path exclusion (#56).
        if (!eventTimeLeft(started) || hasPending()) return;
        const char* ackError = nullptr;
        int code = postEvents(started, gEventBuffers->batch.count, ackError);
        if (debugMode() && !gEventHeapReported) { gEventHeapReported = true; reportEventHeap(); }
        eventHttpFault(code, ackError);
        bool success = code >= 200 && code < 300 && !ackError;
        if (success) {
            serverContact();
            eventSnapshotAccepted();
            if (gEventBuffers->batch.count) {
                if (!eventLock()) return;
                bool acked = gJournal.acknowledge(gEventBuffers->batch, gEventBuffers->batch.ids, gEventBuffers->batch.count);
                eventUnlock();
                if (!acked) { gEventRetry.failed(millis(), -1); return; }
            }
            gEventRetry.succeeded(); sent = true;
            if (!gEventBuffers->batch.count) return;
        } else {
            // A valid empty request with these exact snapshot bytes proves a
            // singleton rejection belongs to the event, not its check-in.
            if ((code == 400 || code == 409) && gEventBuffers->batch.count == 1 &&
                eventTimeLeft(started) && !hasPending()) {
                const char* probeError = nullptr;
                int probe = postEvents(started, 0, probeError);
                if (probe >= 200 && probe < 300 && !probeError) {
                    serverContact();
                    eventSnapshotAccepted();
                    if (eventLock()) { gJournal.isolateRejected(gEventBuffers->batch); eventUnlock(); }
                } else {
                    eventHttpFault(probe, probeError);
                    if (probe == 409) {
                        gEventRevisionBlocked = true;
                        if (eventLock()) { gJournal.suspendStatusSequence(); eventUnlock(); }
                    }
                }
            }
            if (code == 409 && !gEventBuffers->batch.count) {
                gEventRevisionBlocked = true;
                if (eventLock()) { gJournal.suspendStatusSequence(); eventUnlock(); }
            }
            gEventRetry.failed(millis(), code);
            return; // faults cannot recursively drain themselves this cycle
        }
    }
}

// Peek only, no revision write or queue mutation. Radio retry is independent of
// GPX. Empty heartbeats use existing awake task ticks; no wake timer is added.
static bool eventsDue() {
    if (!gEventBuffers || !gJournalReady || !gEventRetry.ready(millis())) return false;
    if (heartbeatDue() || eventSnapshotChanged()) return true;
    if (!eventLock()) return false;
    bool ready = gJournal.nextBatch(gEventBuffers->batch, 1) && gEventBuffers->batch.count;
    eventUnlock();
    return ready;
}
