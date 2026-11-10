// Real sketch, parser, boot builders and both tasks' fault paths; fake I2C only.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
static uint8_t rtcRegisters[16];
static bool present, failRead, failWrite, corruptVerify;
static unsigned writes, requests, transactions;
static uint32_t fakeEpoch;
static void rtcWorld(bool valid = true) {
    faultWorld();
    gClockSet = false; g_currentCore = 1;
    gDiagnosticClock.~DiagnosticClock(); new (&gDiagnosticClock) DiagnosticClock;
    gRtc = RtcRuntime();
    gJournal.setTimeProvider(stampDiagnosticTime);
    Preferences::durable64.clear(); Preferences::failBegin = Preferences::failRead64 = Preferences::failWrite64 = false;
    present = true; failRead = failWrite = corruptVerify = false;
    writes = requests = transactions = 0;
    memset(rtcRegisters, 0, sizeof rtcRegisters);
    fakeEpoch = rtc::toUtc({2026,4,14,16,30,0});
    rtc::encode(fakeEpoch, rtcRegisters + 2);
    if (!valid) rtcRegisters[2] |= 0x80;
    TwoWire::transaction = [](TwoWire& wire, bool stop) -> uint8_t {
        if (&wire == &Wire && wire.address == 0x3c) return 0; // unrelated debug OLED
        assert(&wire == &Wire1 && wire.address == 0x51 && g_currentCore == 1);
        ++transactions;
        if (!present) return 2;
        if (wire.tx.empty()) return 0;
        wire.cursor = wire.tx[0];
        if (wire.tx.size() > 1) {
            assert(stop);
            if (wire.cursor == 2) {
                ++writes; assert(wire.tx.size() == 8); // one complete seven-register burst
                assert(!(wire.tx[1] & 0x80) && !(wire.tx[6] & 0x80)); // VL clear, 20xx
            }
            if (failWrite) return 4;
            for (size_t i = 1; i < wire.tx.size(); ++i) rtcRegisters[wire.cursor++] = wire.tx[i];
            if (corruptVerify && wire.tx[0] == 2) rtcRegisters[2] = 0x60;
        } else assert(!stop); // repeated START for register reads
        return 0;
    };
    TwoWire::request = [](TwoWire& wire, size_t count) -> size_t {
        assert(g_currentCore == 1); ++requests;
        if (failRead) return 0;
        assert((wire.cursor == 0 && count == 1) || (wire.cursor == 2 && count == 7));
        for (size_t i = 0; i < count; ++i) wire.rx.push_back(rtcRegisters[wire.cursor++]);
        return count;
    };
}
static void beginRtc() {
    size_t before = Serial.log.size();
    gRtc.begin(Wire1,gDiagnosticClock);
    assert(Serial.log.size() == before);
    gRtc.printBootStatus();
    rtcConditions();
}
static void expectBootStatus(const std::string& expected) {
    Serial.log.clear();
    gRtc.printBootStatus(); assert(Serial.log.empty()); // no read, no message
    gRtc.begin(Wire1,gDiagnosticClock); assert(Serial.log.empty());
    unsigned before = transactions;
    gRtc.printBootStatus();
    assert(transactions == before); // deferred printing uses the saved boot read
    std::string actual;
    for (const auto& part : Serial.log) actual += part;
    assert(actual == expected); // println's stub stores text without its newline
}
static void setTrusted(uint32_t seconds, unsigned fraction = 0) {
    timeval tv{seconds, long(fraction)*1000}; settimeofday(&tv,nullptr);
    gDiagnosticClock.trusted(EventTimeSource::Gps,millis()); gClockSet = true;
}
static bool rtcLog(const char* needle) {
    for (const auto& line : Serial.log) if (line.find(needle) != std::string::npos) return true;
    return false;
}
int main(int argc, char**) {
    exportJson = argc > 1;
    rtcWorld();
    expectBootStatus("RTC: valid 2026-04-14T16:30:00Z (last set unknown)");
    rtcWorld(); Preferences::durable64["rtc_set"] = fakeEpoch - 3600;
    expectBootStatus("RTC: valid 2026-04-14T16:30:00Z (last set 1.0 h ago)");
    for (bool vl : {true,false}) {
        rtcWorld();
        if (vl) rtcRegisters[2] |= 0x80;
        else rtcRegisters[0] = 0x20;
        expectBootStatus("RTC: invalid (stopped/VL), will set from GPS/SNTP\n");
    }
    for (bool missing : {true,false}) {
        rtcWorld(); present = !missing; failRead = !missing;
        expectBootStatus("RTC: not responding");
    }
    rtcWorld(); Preferences::durable64["rtc_set"] = fakeEpoch - 3600;
    // The actual sketch setup includes PMU recovery, boot read and boot event.
    setup();
    assert(!Serial.log.empty() && Serial.log[0] == "\n=== Stage 4: Trip Logger ===");
    // Setup selected mounted journal adapters; the real consumer uses FS_LOCK.
    assert(xSemaphoreTake(gFsMutex,0));
    auto boot = take(); auto rows = codeRows(boot,1000);
    xSemaphoreGive(gFsMutex);
    assert(rows.size() == 1); has(rows[0],"\"time_source\":\"rtc\"");
    has(rows[0],"\"utc_ms\":" + std::to_string(uint64_t(fakeEpoch)*1000));
    has(rows[0],"\"sync_age_ms\":3600000");
    assert(!gClockSet && !gGpsClockSet && writes == 0 && rtcLog("RTC: valid") && rtcLog("1.0 h ago"));
    // Error resets and the retained five-field PMU notice also get boot RTC time.
    PmuStartupResult recovered{}; recovered.ready = true; recovered.attempts = 2;
    recovered.recovery[0] = {I2cRecoveryState::Recovered,3};
    submitJournalBoot(gJournal,ESP_RST_TASK_WDT,recovered,millis());
    assert(xSemaphoreTake(gFsMutex,0)); rows = take(); xSemaphoreGive(gFsMutex); assert(rows.size() == 3);
    for (auto& row : rows) has(row,"\"time_source\":\"rtc\"");
    // The internal loss notice bypasses RuntimeFaults, but uses the provider too.
    for (unsigned i = 0; i < kEventQueueSize + 1; ++i) gLoopFaults.event(3003,millis()).notice();
    assert(xSemaphoreTake(gFsMutex,0)); gJournal.service(kEventQueueSize); rows = take(); xSemaphoreGive(gFsMutex);
    auto losses = codeRows(rows,4009); assert(losses.size() == 1);
    has(losses[0],"\"time_source\":\"rtc\"");
    // RTC does not suppress the existing trusted-system-clock diagnostic.
    g_millis = gGpsDiagStartMs + 120000; gLastNmeaMs = g_millis;
    serviceRuntimeDiagnostics(); assert(xSemaphoreTake(gFsMutex,0)); rows = codeRows(take(),3006); xSemaphoreGive(gFsMutex);
    assert(rows.size() == 1); has(rows[0],"\"time_source\":\"rtc\"");
    g_millis = 1000; // new fixture below provides a fresh monotonic boot
    rtcWorld(); beginRtc(); take();
    // Feed a complete real RMC through the production GPS setter.
    auto ss = sentences({0,gLat,gLon,0,true}); feed(ss[0]);
    assert(gClockSet && gGpsClockSet);
    gLoopFaults.event(3003,millis()).integer("message_class",6).integer("message_id",138).notice();
    rows = take(); assert(rows.size() == 1);
    has(rows[0],"\"time_source\":\"gps\""); has(rows[0],"\"sync_age_ms\":0");
    // Both core-0 fault builders use the same provider, without I2C or NVS.
    unsigned before = transactions; g_currentCore = 0;
    g_stackHighWaterBytes = 1000; reportNetworkStack("test");
    gEventFaults.event(5010,millis()).condition(true);
    trySntp(); serviceRtc(); // defensive guard also rejects accidental core-0 service
    assert(transactions == before && writes == 0);
    rows = take(); assert(rows.size() == 2);
    for (auto& row : rows) has(row,"\"time_source\":\"gps\"");
    g_currentCore = 1;
    // Unknown age, VL -> raise -> aligned, verified write -> same-episode clear.
    rtcWorld(false); beginRtc(); auto raised = one(3008,"raise");
    has(raised,"\"reason\":\"stopped\""); has(raised,"\"time_source\":\"unknown\"");
    assert(rtcLog("invalid (stopped/VL)")); setTrusted(fakeEpoch,101);
    serviceRtc(); assert(writes == 0); assert(take().empty());
    g_millis += 899; serviceRtc(); auto cleared = one(3008,"clear");
    assert(episode(raised) == episode(cleared)); has(cleared,"\"time_source\":\"gps\"");
    assert(writes == 1 && !(rtcRegisters[2] & 0x80) && !(rtcRegisters[0] & 0x20));
    assert(Preferences::durable64["rtc_set"] == fakeEpoch + 1 && rtcLog("set from gps, verified"));
    for (unsigned i = 0; i < 500; ++i) { g_millis += 1000; serviceRtc(); }
    assert(writes == 1 && take().empty());
    gClockSet = false;
    gLoopFaults.event(3003,millis()).notice(); rows = take();
    has(rows[0],"\"time_source\":\"rtc\""); has(rows[0],"\"sync_age_ms\":500000");
    // No ACK, and a boot register failure, are E3007 rather than read_failed.
    for (bool missing : {true,false}) {
        rtcWorld(); present = !missing; failRead = !missing; beginRtc();
        one(3007,"raise"); assert(writes == 0);
    }
    // Device returns after boot; success clears the unavailable episode.
    present = true; failRead = false; setTrusted(fakeEpoch); serviceRtc();
    one(3007,"clear"); assert(writes == 1);
    // STOP and malformed/range failures are all classified by the real adapter.
    for (auto status : {rtc::Status::Stopped,rtc::Status::InvalidDate,rtc::Status::OutOfRange}) {
        rtcWorld();
        if (status == rtc::Status::Stopped) rtcRegisters[0] = 0x20;
        if (status == rtc::Status::InvalidDate) rtcRegisters[3] = 0x6a;
        if (status == rtc::Status::OutOfRange) rtcRegisters[7] |= 0x80;
        beginRtc(); has(one(3008,"raise"),std::string("\"reason\":\"")+rtc::reason(status)+"\"");
    }
    // Exactly two seconds causes one correction; <2 s never writes.
    for (int drift : {-2,-1,0,1,2}) {
        rtcWorld(); beginRtc(); take(); setTrusted(fakeEpoch + drift); serviceRtc();
        assert(writes == (std::abs(drift) >= 2 ? 1u : 0u));
        g_millis += 60000; serviceRtc(); assert(writes <= 1); take();
    }
    // A later register read and a corrupt verification reserve read_failed.
    rtcWorld(); beginRtc(); take(); failRead = true; setTrusted(fakeEpoch); serviceRtc();
    has(one(3008,"raise"),"\"reason\":\"read_failed\"");
    assert(writes == 0);
    rtcWorld(false); beginRtc(); take(); corruptVerify = true; setTrusted(fakeEpoch);
    serviceRtc(); assert(gRtc.status() == rtc::Status::ReadFailed && writes == 1); take();
    // A changed reason within the same episode uses normal repeat suppression.
    g_millis += RuntimeFaults::kRepeatMs; rtcConditions();
    has(one(3008,"repeat"),"\"reason\":\"read_failed\"");
    // Failed bursts: no hot-loop retries, >=60 s backoff and three total.
    rtcWorld(false); beginRtc(); take(); failWrite = true; setTrusted(fakeEpoch,101);
    serviceRtc(); assert(writes == 0); g_millis += 899;
    serviceRtc(); assert(writes == 1); take(); before = transactions;
    for (unsigned i = 0; i < 100; ++i) serviceRtc();
    assert(writes == 1);
    g_millis += 59999; serviceRtc(); assert(writes == 1 && transactions == before);
    ++g_millis; serviceRtc(); assert(writes == 2);
    g_millis += 60000; serviceRtc(); assert(writes == 3);
    g_millis += 60000; serviceRtc(); assert(writes == 3); take();
    // An ACK loss is not recovery from a stopped clock. Only verified setting
    // may clear either raised condition, including when both were observed.
    rtcWorld(false); beginRtc(); raised = one(3008,"raise");
    present = false; setTrusted(fakeEpoch); serviceRtc(); auto unavailable = one(3007,"raise");
    assert(gRtc.invalid() && gRtc.unavailable());
    present = true; g_millis += 60000; serviceRtc(); rows = take();
    auto invalidClears = codeRows(rows,3008), unavailableClears = codeRows(rows,3007);
    assert(invalidClears.size() == 1 && unavailableClears.size() == 1 && writes == 1);
    has(invalidClears[0],"\"transition\":\"clear\""); has(unavailableClears[0],"\"transition\":\"clear\"");
    assert(episode(invalidClears[0]) == episode(raised) && episode(unavailableClears[0]) == episode(unavailable));
    // SNTP publication from core 0 does no RTC work; core 1 later repairs it.
    rtcWorld(false); beginRtc(); take(); before = transactions; g_currentCore = 0;
    g_sntpCompleteAfterPolls = 1; g_sntpEpochMs = uint64_t(fakeEpoch)*1000;
    trySntp(); assert(gClockSet && transactions == before && writes == 0);
    gNetFaults.event(1007,millis()).integer("free_bytes",1000).condition(true);
    has(one(1007,"raise"),"\"time_source\":\"sntp\"");
    g_currentCore = 1; serviceRtc(); one(3008,"clear"); assert(writes == 1 && rtcLog("set from sntp, verified"));
    // A drift read crossing into 2100 must not send an uninitialized calendar.
    rtcWorld(false); beginRtc(); take(); setTrusted(rtc::kLastUtc);
    auto request = TwoWire::request;
    TwoWire::request = [request](TwoWire& wire, size_t count) {
        size_t n = request(wire,count);
        if (count == 7) g_epochBaseMs += 1000;
        return n;
    };
    serviceRtc(); assert(writes == 0 && !Preferences::durable64.count("rtc_set")); take();
    // Compare against post-read system UTC at the two-second drift threshold.
    rtcWorld(); rtc::encode(fakeEpoch+2,rtcRegisters+2); beginRtc(); take(); setTrusted(fakeEpoch);
    request = TwoWire::request;
    TwoWire::request = [request](TwoWire& wire, size_t count) {
        size_t n = request(wire,count);
        if (count == 7) g_epochBaseMs += 1000;
        return n;
    };
    serviceRtc(); assert(writes == 0 && gRtc.status() == rtc::Status::Valid); take();
    // Missing/future NVS ages remain null; valid old age grows with uptime.
    for (uint64_t saved : {uint64_t(0),uint64_t(fakeEpoch+1)}) {
        rtcWorld(); Preferences::durable64["rtc_set"] = saved; beginRtc();
        gLoopFaults.event(3003,millis()).notice(); has(take()[0],"\"sync_age_ms\":null");
    }
    // Advance only via the production loop's 5 ms pause. Its service must
    // still catch the first 100 ms of a later second without spending retries.
    rtcWorld(false); beginRtc(); take(); setTrusted(fakeEpoch, 101);
    uint32_t alignedStart = millis();
    while (!writes && uint32_t(millis() - alignedStart) < 1000) loop();
    assert(writes == 1 && uint32_t(millis() - alignedStart) <= 905);
    assert((g_epochBaseMs + millis() - LOOP_IDLE_MS) % 1000 < 100);
    if (exportJson) for (auto& row : exported) std::printf("EVENT %s\n",row.c_str());
    std::printf("RTC runtime: 21 scenario groups passed (runtime debug=%d)\n",debugMode());
}
