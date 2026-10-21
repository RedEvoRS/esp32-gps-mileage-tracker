// Real Stage 4 plumbing with the trip harness's fake disks. No hardware access.
#include "arduino_stub.h"
#include <cassert>
#include <new>
uint32_t g_millis = 0;
uint64_t g_epochBaseMs = 0;
SerialStub Serial;
FakeDisk g_disk;
LittleFSStub LittleFS;
int g_lockDepth = 0;
TwoWire Wire, Wire1;
WiFiStub WiFi;
#include "04_trip_logger.ino"
using namespace diagnostics;
static EventLog::Batch batch; // also static, as required on target

static void world(bool sd, bool flash, EventLog::Config config = EventLog::Config()) {
    gDebugMode.store(hostDebugMode());
    g_disk = FakeDisk(); g_sdDisk = FakeDisk();
    g_disk.mounted = flash; g_sdDisk.mounted = sd;
    SD.cardPresent = sd;
    gSdMounted = sd; gFlashMounted = flash;
    gTripFS = sd ? static_cast<fs::FS*>(&SD) : static_cast<fs::FS*>(&LittleFS);
    SD.dirs.clear(); LittleFS.dirs.clear();
    g_lockDepth = 0; gFsDemand = 0; gNetHold = gNetBusy = gNetAbort = false;
    gJournalSealRequested = gJournalSealDone = gJournalSealOk = false;
    gPowerOffEventPending = false;
    gJournalStarted = gJournalSealing = gJournalBootReported = false;
    gJournal.~EventLog(); new (&gJournal) EventLog(sd ? &gSdJournalFs : nullptr, flash ? &gFlashJournalFs : nullptr, config);
    gJournalBoot = newJournalBoot();
    gJournalReady = gJournal.begin(gJournalBoot.id, gJournalBoot.counter, "test-1");
    assert(gJournalReady);
    PMU = XPowersAXP2101(); gTripPath[0] = 0;
    Serial.log.clear(); g_taskDelayHook = nullptr;
}
static void submit(unsigned count = 1) {
    for (unsigned i = 0; i < count; ++i) assert(gJournal.submit(EventInput()) == EventLog::Submit::Accepted);
}
static uint64_t allocation(FakeDisk& disk, uint32_t unit) {
    uint64_t bytes = 0;
    for (const auto& file : disk.files) if (!file.first.rfind("/diag/", 0))
        bytes += ((file.second.size() + unit - 1) / unit) * unit;
    return bytes;
}
int main() {
    Preferences::durable64.clear();
    auto first = newJournalBoot(), second = newJournalBoot();
    assert(first.counterPersisted && first.counter == 1 && second.counter == 2);
    assert(std::strcmp(first.id, second.id));
    Preferences reopened; assert(reopened.getULong64("boots", 0) == 2);
    Preferences::failWrite64 = true;
    auto failed = newJournalBoot(); assert(!failed.counterPersisted && failed.counter == 0);
    Preferences::failWrite64 = false;
    assert(newJournalBoot().counter == 3);
    Preferences::failBegin = true;
    assert(!newJournalBoot().counterPersisted);
    Preferences::failBegin = false;
    auto savedCounter = Preferences::durable64;
    Preferences::failRead64 = true;
    assert(!newJournalBoot().counterPersisted && Preferences::durable64 == savedCounter);
    Preferences::failRead64 = false;
    Preferences::durable64["boots"] = kMaxEventInteger;
    assert(!newJournalBoot().counterPersisted);
    Preferences::durable64.clear();
    std::puts("Journal NVS increment, reopen, failure, saturation and fresh identities passed");

    world(false, false);
    gJournalReady = false;
    beginJournal();
    assert(gJournalReady);
    submit(); assert(gJournal.nextBatch(batch));
    assert(std::strstr(batch.events[0],
        (std::string("\"firmware_version\":\"") + firmware::kVersion + "\"").c_str()));
    std::puts("Journal production begin uses the build version passed");

    const esp_reset_reason_t reasons[] = { ESP_RST_POWERON, ESP_RST_DEEPSLEEP, ESP_RST_SW,
        ESP_RST_INT_WDT, ESP_RST_TASK_WDT, ESP_RST_WDT, ESP_RST_BROWNOUT, ESP_RST_PANIC,
        ESP_RST_UNKNOWN, static_cast<esp_reset_reason_t>(255) };
    for (auto reason : reasons) {
        world(false, false);
        PmuStartupResult clean; clean.ready = true; clean.attempts = 1;
        submitJournalBoot(gJournal, reason, clean, 123);
        assert(gJournal.nextBatch(batch));
        bool errorReset = reason == ESP_RST_INT_WDT || reason == ESP_RST_TASK_WDT ||
            reason == ESP_RST_WDT || reason == ESP_RST_BROWNOUT || reason == ESP_RST_PANIC;
        assert(batch.count == (errorReset ? 2 : 1));
        assert(std::strstr(batch.events[0], "\"code\":1000,"));
        assert(std::strstr(batch.events[0], journalResetReason(reason)));
        if (errorReset) {
            const char* code = reason == ESP_RST_BROWNOUT ? "\"code\":1004," :
                               reason == ESP_RST_PANIC ? "\"code\":1005," : "\"code\":1003,";
            assert(std::strstr(batch.events[1], code));
        }
    }
    world(false, false);
    PmuStartupResult recovered; recovered.ready = true; recovered.attempts = 2;
    recovered.recovery[0].state = I2cRecoveryState::Recovered; recovered.recovery[0].clocks = 9;
    submitJournalBoot(gJournal, ESP_RST_POWERON, recovered, 321);
    assert(gJournal.nextBatch(batch) && batch.count == 2);
    assert(std::strstr(batch.events[1], "\"code\":2007,"));
    assert(std::strstr(batch.events[1], "\"clock_count\":9"));
    assert(std::strstr(batch.events[1], "\"attempt_count\":2"));
    for (unsigned mode = 0; mode < 3; ++mode) {
        world(false, false);
        PmuStartupResult startup;
        startup.ready = mode != 2;
        startup.attempts = mode == 1 ? 1 : 2;
        if (mode == 1) startup.recovery[0].state = I2cRecoveryState::Recovered;
        submitJournalBoot(gJournal, ESP_RST_POWERON, startup, 400);
        assert(gJournal.nextBatch(batch) && batch.count == 2);
        assert(std::strstr(batch.events[1], "\"code\":2007,"));
    }
    std::puts("Journal boot/reset codes (including unknown) and conditional E2007 passed");

    for (bool sd : {true, false}) {
        world(sd, true); submit(); serviceJournal();
        assert(g_lockDepth == 0 && gFsDemand == 0);
        assert(gJournal.stats().backend == (sd ? EventLog::Backend::Sd : EventLog::Backend::LittleFs));
        assert(gJournal.stats().queued == 0);
        xSemaphoreTake(gFsMutex, 0);
        assert(gJournal.nextBatch(batch) && batch.count == 1);
        auto identity = batch.ids[0]; std::string json = batch.events[0];
        gJournal.invalidateStorage();
        assert(gJournal.nextBatch(batch) && json == batch.events[0]);
        assert(identity.sequence == batch.ids[0].sequence && !std::strcmp(identity.bootId, batch.ids[0].bootId));
        assert(gJournal.acknowledge(batch, batch.ids, batch.count));
        assert(gJournal.backlog().records == 0);
        xSemaphoreGive(gFsMutex);
        assert(!g_disk.formatRequested && !g_sdDisk.formatRequested);
    }
    std::puts("Journal SD and LittleFS adapter round trips, retry identity and durable ack passed");

    world(false, true);
    assert(fsFreeBytes() == g_disk.capacity - kLittleFsJournalReserve);
    submit(); serviceJournal();
    uint32_t allocated = allocation(g_disk, 4096);
    assert(allocated > 0 && allocated < kLittleFsJournalReserve);
    assert(fsFreeBytes() == g_disk.capacity - g_disk.used() - (kLittleFsJournalReserve - allocated));
    g_disk.capacity = g_disk.used() + (kLittleFsJournalReserve - allocated) + FS_START_FREE - 1;
    assert(!roomForNewTrip(millis()));
    ++g_disk.capacity; assert(roomForNewTrip(millis()));
    // Leave trip data untouched while rolling/evicting diagnostic history.
    g_disk.capacity = 4 * 1024 * 1024;
    g_disk.files["/trips/untouched.gpx"] = "trip-data";
    for (unsigned i = 0; i < 400; ++i) { submit(); serviceJournal(); }
    assert(allocation(g_disk, 4096) <= kLittleFsJournalReserve);
    assert(g_disk.files["/trips/untouched.gpx"] == "trip-data" && gJournal.stats().dropped > 0);
    std::puts("Journal LittleFS quota, unused reserve, trip admission boundary and trip preservation passed");

    EventLog::Config tiny; tiny.sdQuota = 3 * 32768; tiny.segmentBytes = 1024;
    world(true, true, tiny);
    for (unsigned i = 0; i < 80; ++i) { submit(); serviceJournal(); }
    assert(allocation(g_sdDisk, 32768) <= tiny.sdQuota);
    assert(gJournal.stats().dropped > 0);

    world(true, true); submit();
    FS_LOCK(); serviceJournal(); assert(gJournal.stats().queued == 1); FS_UNLOCK();
    serviceJournal(); assert(gJournal.stats().queued == 0);
    // Force recovery, then simulate core 1 requesting the lock at the first
    // frame read. Further adapter operations stop; the queue stays retryable.
    submit(); gJournal.invalidateStorage();
    g_sdDisk.onRead = [] { assert(g_lockDepth == 1); gFsDemand.store(1); };
    serviceJournal(); assert(g_lockDepth == 0 && gJournal.stats().queued == 1);
    g_sdDisk.onRead = nullptr;
    // The waiting trip now obtains the mutex and writes before maintenance.
    xSemaphoreTake(gFsMutex, portMAX_DELAY);
    auto trip = SD.open("/trips/concurrent.gpx", FILE_WRITE);
    assert(trip.write(reinterpret_cast<const uint8_t*>("trip"), 4) == 4); trip.close();
    xSemaphoreGive(gFsMutex); gFsDemand.store(0);
    serviceJournal(); assert(gJournal.stats().queued == 0);
    assert(g_sdDisk.files["/trips/concurrent.gpx"] == "trip");
    // Producer enqueue is independent of an already-held filesystem lock.
    FS_LOCK(); submit(); assert(gJournal.stats().queued == 1); FS_UNLOCK();
    serviceJournal();
    xSemaphoreTake(gFsMutex, 0);
    assert(gJournal.restoreStatusSequence(12));
    assert(gJournal.restoreStatusSequence(13)); // newest slot is read second
    gJournal.invalidateStorage();
    xSemaphoreGive(gFsMutex);
    g_sdDisk.onRead = [] { gFsDemand.store(1); };
    serviceJournal(); // empty queue, cancellation during metadata-only load
    g_sdDisk.onRead = nullptr; gFsDemand.store(0);
    xSemaphoreTake(gFsMutex, 0);
    uint64_t revision = 0;
    assert(gJournal.allocateStatusSequence(revision) && revision == 14);
    xSemaphoreGive(gFsMutex);
    std::puts("Journal SD allocation quota and service yielding to trip FS_LOCK demand passed");

    world(true, true); g_sdDisk.failMount = true; submit(); serviceJournal();
    assert(gJournal.stats().backend == EventLog::Backend::LittleFs);
    world(true, true); g_sdDisk.failMount = g_disk.failMount = true; submit(); serviceJournal();
    assert(gJournal.stats().backend == EventLog::Backend::Ram && gJournal.stats().queued == 1);
    for (unsigned i = 1; i < kEventQueueSize; ++i) submit();
    assert(gJournal.submit(EventInput()) == EventLog::Submit::Full);
    assert(!g_disk.formatRequested && !g_sdDisk.formatRequested);
    std::puts("Journal failed-media LittleFS and bounded RAM fallback passed");

    world(true, false); submit(); serviceJournal();
    g_sdDisk.failRenames = 1;
    xSemaphoreTake(gFsMutex, 0); assert(!gJournal.seal()); xSemaphoreGive(gFsMutex);
    assert(g_sdDisk.files.count("/diag/seg-00000001.open"));
    xSemaphoreTake(gFsMutex, 0); assert(gJournal.seal()); xSemaphoreGive(gFsMutex);
    assert(g_sdDisk.files.count("/diag/seg-00000001.ready"));
    std::puts("Journal failed shutdown seal stays retryable and preserves history passed");

    world(true, true); submit(3); serviceJournal();
    PMU.vbusIn = false;
    g_taskDelayHook = [](uint32_t) { if (!g_lockDepth) serviceJournal(); };
    g_sdDisk.onUnmount = [] {
        assert(gJournalSealDone && gJournalSealOk && gJournal.stats().queued == 0);
        for (const auto& file : g_sdDisk.files) assert(file.first.find(".open") == std::string::npos);
    };
    try { powerOffForVbus(); assert(false); } catch (const PoweredOff&) {}
    assert(g_lockDepth == 1 && !g_sdDisk.mounted && !g_disk.mounted);
    std::puts("Journal core-0 shutdown handoff drains and seals before unmount passed");

    world(true, true); submit(2); serviceJournal();
    std::string bootId = gJournalBoot.id;
    PMU.vbusIn = false; PMU.failShutdown = true;
    g_taskDelayHook = [](uint32_t) { if (!g_lockDepth) serviceJournal(); };
    powerOffForVbus();
    assert(!gNetHold && g_disk.mounted && g_sdDisk.mounted && g_lockDepth == 0);
    assert(!gJournalStarted && bootId == gJournalBoot.id);
    submit(); serviceJournal();
    xSemaphoreTake(gFsMutex, 0);
    assert(gJournal.nextBatch(batch) && batch.count == 4);
    for (size_t i = 0; i < batch.count; ++i) assert(bootId == batch.ids[i].bootId);
    xSemaphoreGive(gFsMutex);
    if (debugMode()) {
        size_t bootLines = 0;
        for (const auto& line : Serial.log) if (line.rfind("Journal: sd,", 0) == 0) ++bootLines;
        assert(bootLines == 1);
    }

    std::puts("Journal failed power-off remount reloads caches, preserves identity and reports boot once passed");
    return 0;
}
