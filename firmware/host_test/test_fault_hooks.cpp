// Reuse the real-parser drive fixtures and fake devices, exercising production
// sketch paths. The legacy regression main still runs separately in run.sh.
#define main gpsRegressionMain
#include "test_gps.cpp"
#undef main
#include <cassert>
using namespace diagnostics;
static EventLog::Batch faultBatch;
static std::vector<std::string> exported;
static bool exportJson = false;

static void faultWorld() {
    resetWorld();
    g_millis = 1000;
    g_sdDisk.mounted = gSdMounted = SD.cardPresent = true;
    gTripFS = &SD; SD.mkdir(TRIP_DIR);
    gJournal.~EventLog(); new (&gJournal) EventLog(nullptr, nullptr);
    assert(gJournal.begin("00000000000000000000000000000001", 1, "test-hooks"));
    gJournalReady = true;
    for (auto& s : gLoopConditions) { auto code = s.code; s = {}; s.code = code; }
    for (auto& s : gNetConditions) { auto code = s.code; s = {}; s.code = code; }
    gLoopFaults.~RuntimeFaults(); new (&gLoopFaults) RuntimeFaults(gJournal, gLoopConditions, sizeof(gLoopConditions)/sizeof(gLoopConditions[0]));
    gNetFaults.~RuntimeFaults(); new (&gNetFaults) RuntimeFaults(gJournal, gNetConditions, sizeof(gNetConditions)/sizeof(gNetConditions[0]));
    gBatteryDiagChecked = false; gStorageDiagMs = g_millis;
    gChargeApplied = gChargePaused = false; gChargeCheckT0 = 0;
    gJoinFailures = gTransportFailures = 0;
    gClockSet = true; gNetAbort = gNetHold = gNetBusy = false;
    gPmuIrqFlag = false; PMU = XPowersAXP2101(); WiFi = WiFiStub();
    g_httpCode = 200; g_httpHook = nullptr; g_stackHighWaterBytes = 4096;
    gGpsDiagStartMs = gLastNmeaMs = gIdleFixWaitMs = g_millis;
    gHaveLastFix = false; gLastFixMonoMs = 0;
}
static std::vector<std::string> take() {
    std::vector<std::string> rows;
    assert(gJournal.nextBatch(faultBatch));
    for (size_t i = 0; i < faultBatch.count; ++i) {
        rows.emplace_back(faultBatch.events[i]);
        exported.push_back(rows.back());
    }
    if (faultBatch.count) assert(gJournal.acknowledge(faultBatch, faultBatch.ids, faultBatch.count));
    return rows;
}
static std::vector<std::string> codeRows(const std::vector<std::string>& rows, int code) {
    std::vector<std::string> out;
    std::string key = "\"code\":" + std::to_string(code) + ",";
    for (const auto& row : rows) if (row.find(key) != std::string::npos) out.push_back(row);
    return out;
}
static std::string episode(const std::string& row) {
    auto start = row.find("\"episode_id\":") + 13;
    return row.substr(start, row.find(',', start) - start);
}
static void has(const std::string& row, const std::string& text) {
    if (row.find(text) == std::string::npos) { std::fprintf(stderr, "Missing %s in %s\n", text.c_str(), row.c_str()); std::abort(); }
}
static std::string one(int code, const char* transition) {
    auto all = take();
    auto rows = codeRows(all, code);
    if (rows.size() != 1) {
        std::fprintf(stderr, "Expected one E%d %s, got %zu (%zu total)\n", code, transition, rows.size(), all.size());
        for (const auto& row : all) std::fprintf(stderr, "%s\n", row.c_str());
    }
    assert(rows.size() == 1);
    has(rows[0], std::string("\"transition\":\"") + transition + "\"");
    return rows[0];
}
// Each condition is exercised through its real hook: first raise, hot-loop
// suppression, one bounded repeat, same-episode clear, healthy silence.
static void cycle(int code, const std::function<void(bool)>& path, const char* field = nullptr) {
    faultWorld();
    path(false); assert(take().empty());
    path(true); auto raised = one(code, "raise");
    if (field) has(raised, field);
    for (unsigned i = 0; i < 10; ++i) path(true);
    assert(take().empty());
    g_millis += RuntimeFaults::kRepeatMs;
    path(true); auto repeated = one(code, "repeat");
    assert(episode(raised) == episode(repeated));
    path(false); auto cleared = one(code, "clear");
    assert(episode(raised) == episode(cleared));
    path(false); assert(take().empty());
}
static void runtimeHealthy() {
    gLastNmeaMs = gGpsDiagStartMs = gIdleFixWaitMs = g_millis;
    gLastFixMonoMs = g_millis;
    gStorageDiagMs = g_millis;
}
static void feed(const std::string& bytes) { for (uint8_t c : bytes) receiveGpsByte(c); }

// Reuse the real GPS harness's wire builders and placeholder position. A fresh
// NAV-PVT precedes each status-A RMC so accuracy, rather than stale data, gates it.
static void diagnosticFix(uint32_t at, uint32_t hAcc, float mps = 0, bool withPvt = true) {
    g_millis = at;
    serviceGpsSpeedFallback();
    if (withPvt) feed(navPvt(at, 100, hAcc, 3, 1, int32_t(mps * 1000)));
    auto ss = sentences({int(at / 1000), gLat, gLon, mps, true});
    feed(ss[0] + ss[2]); // RMC plus GGA (measured satellites)
    serviceRuntimeDiagnostics();
}

static RuntimeFaults::Observation antennaObservation() {
    RuntimeFaults::Observation observations[64]; size_t count = 0;
    assert(gLoopFaults.snapshot(observations, 64, count));
    for (size_t i = 0; i < count; ++i) if (observations[i].code == 3009) return observations[i];
    assert(false); return {};
}

// Wire-level MON-RF AGC uses the same integer percentage as the receiver path.
// Four heard satellites cover the measured unplugged C/N0 range, 15-20.
static void antennaInterval(int agc = 79, bool strong = false, bool fresh = true) {
    if (agc >= 0) {
        std::vector<uint8_t> p(28, 0); p[1] = 1;
        unsigned raw = (unsigned(agc) * 8191 + 99) / 100;
        p[18] = raw & 255; p[19] = raw >> 8;
        feed(frame(0x0A, 0x38, p));
    }
    g_millis = gDebugRfPollMs + 30000;
    if (fresh) feed(nmea(std::string("GPGSV,1,1,04,01,40,083,15,02,17,308,16,03,07,344,18,04,22,228,") +
                         (strong ? "30,1" : "20,1")));
    runtimeHealthy();
    serviceDebugRf(); serviceRuntimeDiagnostics();
    assert(!gDebugRf.valid); // prior reply consumed and next poll sent
}

static void gpsAntennaRegressions() {
    for (bool strongRecovery : {false, true}) {
        faultWorld();
        gGpsConfig.state = GPS_WAIT_ACK;
        serviceDebugRf(); assert(GPSSerial.tx.empty());
        gGpsConfig.state = GPS_CONFIG_DONE;
        serviceDebugRf(); // first poll, with no evidence yet
        serviceRuntimeDiagnostics();
        assert(!antennaObservation().observed && take().empty());
        antennaInterval(-1, false, false);
        assert(!antennaObservation().observed && take().empty());
        // Suspect hold alone must not publish an observed healthy condition.
        for (int i = 0; i < 4; ++i) {
            antennaInterval(); assert(!antennaObservation().observed && take().empty());
        }
        // Missing reply breaks the run, and stale GSV breaks the next run.
        antennaInterval(-1); assert(!antennaObservation().observed && take().empty());
        antennaInterval(); assert(take().empty());
        antennaInterval(79, false, false);
        assert(!antennaObservation().observed && take().empty());
        for (int i = 0; i < 4; ++i) { antennaInterval(); assert(take().empty()); }
        uint32_t firstSuspect = gDebugRfPollMs - 90000;
        // Neither another hot-loop call nor 119999 ms can raise early.
        g_millis = firstSuspect + 119999; runtimeHealthy();
        serviceDebugRf(); serviceRuntimeDiagnostics();
        assert(!antennaObservation().observed && take().empty());
        antennaInterval(); auto raised = one(3009, "raise");
        assert(g_millis - firstSuspect == 120000);
        has(raised, "\"agc_pct\":79"); has(raised, "\"max_cno\":20");
        assert(antennaObservation().observed && antennaObservation().episode);
        for (int i = 0; i < 10; ++i) serviceRuntimeDiagnostics();
        assert(take().empty());
        // No evidence keeps the episode active, including at repeat time.
        antennaInterval(-1); assert(take().empty() && antennaObservation().episode);
        antennaInterval(79, false, false); assert(take().empty());
        antennaInterval(60); assert(take().empty());
        g_millis = firstSuspect + 120000 + RuntimeFaults::kRepeatMs;
        runtimeHealthy(); serviceDebugRf(); serviceRuntimeDiagnostics();
        auto repeated = one(3009, "repeat");
        has(repeated, "\"context\":{}"); // no fabricated RF/GSV measurements
        assert(episode(raised) == episode(repeated));
        // Low gain with zero strong satellites (indoors), or one strong
        // satellite at high gain, clears immediately in the same episode.
        antennaInterval(strongRecovery ? 79 : 29, strongRecovery);
        auto cleared = one(3009, "clear");
        assert(episode(raised) == episode(cleared));
        has(cleared, strongRecovery ? "\"agc_pct\":79" : "\"agc_pct\":29");
        assert(antennaObservation().observed && !antennaObservation().episode);
        antennaInterval(-1, false, false); assert(take().empty());
    }
    if (!exportJson) std::printf("GPS antenna integration: 2 recovery episodes passed (DISPLAY_DEBUG=%d).\n", DISPLAY_DEBUG);
}

static void gpsFixGapRegressions() {
    for (bool hadTrustedFix : {false, true}) {
        faultWorld();
        uint32_t anchor = gGpsDiagStartMs;
        if (hadTrustedFix) {
            diagnosticFix(anchor + 5000, 2000);
            assert(gHaveLastFix && gLatestSpeedTrusted);
            anchor = gLastFix.monoMs;
        }
        assert(take().empty());
        // 6 m passes the trip limit but fails the stricter 5 m start limit.
        for (uint32_t gap = 1000; gap < 120000; gap += 1000) {
            diagnosticFix(anchor + gap, 6000);
            assert(gState == IDLE && !gSpeedFallback && !gLatestSpeedTrusted);
            assert(gHaveLastFix == hadTrustedFix && gLastFixMonoMs == anchor + gap);
            if (hadTrustedFix) assert(gLastFix.monoMs == anchor);
            assert(take().empty());
        }
        diagnosticFix(anchor + 119999, 6000); assert(take().empty());
        diagnosticFix(anchor + 120000, 6000);
        auto rows = codeRows(take(), 3002);
        if (rows.size() != 1)
            std::fprintf(stderr, "E3002 idle regression: prior trusted fix=%d, expected one raise at 120000 ms, got %zu\n", hadTrustedFix, rows.size());
        assert((rows.size() == 1 && "E3002 must raise 120 s after the last trusted fix, including after a first trusted fix"));
        has(rows[0], "\"transition\":\"raise\"");
        has(rows[0], "\"elapsed_ms\":120000");
        has(rows[0], "\"satellites\":9");
        auto raised = rows[0];
        diagnosticFix(anchor + 120001, 2000);
        assert(gHaveLastFix && gLatestSpeedTrusted && gState == IDLE);
        assert(episode(one(3002, "clear")) == episode(raised));
        if (!exportJson) std::printf("GPS diagnostic regression passed: untrusted idle fixes %s first trusted fix; trusted recovery.\n",
                                    hadTrustedFix ? "after" : "before");
    }

    faultWorld(); run(15, 40, true, false, 100);
    assert(gState == ACTIVE && gHaveLastFix); assert(take().empty());
    const uint32_t trusted = gLastFix.monoMs;
    // Status-A fixes too inaccurate even for a trip must still prevent E3005
    // and allow the parked cooldown to finish before the five-minute watchdog.
    for (uint32_t gap = 1000; gap <= COOL_MS + 1000; gap += 1000) {
        diagnosticFix(trusted + gap, 10001);
        assert(!gSpeedFallback && !gLatestSpeedTrusted && gLastFix.monoMs == trusted);
        assert(gLastFixMonoMs == trusted + gap);
        assert(gState == (gap <= COOL_MS ? COOLDOWN : IDLE));
        assert(take().empty());
    }
    if (!exportJson) std::printf("GPS diagnostic regression passed: untrusted trip fixes prevent E3005 through cooldown.\n");

    faultWorld(); run(15, 40, true, false, 100);
    assert(gState == ACTIVE); assert(take().empty());
    const uint32_t lastRmc = gLastFixMonoMs;
    for (uint32_t gap : {29999u, 30000u}) {
        g_millis = lastRmc + gap;
        // GPS traffic continues, but no RMC positions arrive.
        feed(navPvt(g_millis, 100, 10001) + nmea("GNTXT,01,01,02,ready"));
        serviceRuntimeDiagnostics();
        assert(gState == ACTIVE);
        if (gap < 30000) assert(take().empty());
    }
    auto raised = one(3005, "raise"); has(raised, "\"elapsed_ms\":30000");
    diagnosticFix(lastRmc + 30001, 10001);
    assert(!gLatestSpeedTrusted && gState == COOLDOWN);
    assert(episode(one(3005, "clear")) == episode(raised));
    if (!exportJson) std::printf("GPS diagnostic regression passed: missing RMC raises E3005 at 30 s; untrusted RMC clears it.\n");

    faultWorld();
    const uint32_t start = gGpsDiagStartMs;
    for (uint32_t gap = 1000; gap <= 120000; gap += 1000) {
        diagnosticFix(start + gap, 0, 0, false);
        assert(!gNavPvt.seen && gSpeedFallback && gLatestSpeedTrusted && gState == IDLE);
        assert(gHaveLastFix && gLastFix.monoMs == start + gap);
        assert(take().empty());
    }
    const uint32_t fallbackFix = gLastFix.monoMs;
    for (uint32_t gap : {119999u, 120000u}) {
        g_millis = fallbackFix + gap;
        feed(nmea("GNTXT,01,01,02,ready")); serviceRuntimeDiagnostics();
        if (gap < 120000) assert(take().empty());
    }
    raised = one(3002, "raise"); has(raised, "\"elapsed_ms\":120000");
    diagnosticFix(fallbackFix + 120001, 0, 0, false);
    assert(gSpeedFallback && gLatestSpeedTrusted);
    assert(episode(one(3002, "clear")) == episode(raised));
    if (!exportJson) std::printf("GPS diagnostic regression passed: RMC-only fallback retains E3002 timing and recovery.\n");
}

#ifndef FAULT_HOOKS_HELPERS_ONLY
int main(int argc, char**) {
    exportJson = argc > 1;
    gpsAntennaRegressions();
    gpsFixGapRegressions();
    cycle(1007, [](bool bad) { g_stackHighWaterBytes = bad ? 2047 : 2048; reportNetworkStack("test"); }, "\"free_bytes\":2047");
    for (int c : {2002, 2003}) cycle(c, [c](bool bad) {
        PMU.batteryPercent = bad ? (c == 2002 ? 15 : 5) : 85;
        PMU.batteryMv = bad ? 3500 : 4020; gBatteryDiagChecked = false;
        serviceBatteryDiagnostics();
    }, "\"battery_mv\":3500");
    for (int c : {2004, 2005, 2006}) cycle(c, [c](bool bad) {
        PMU.dieC = !bad ? 30.0f : c == 2004 ? CHG_HOT_PAUSE_C : c == 2005 ? CHG_COLD_PAUSE_C : NAN;
        gChargeCheckT0 = g_millis - CHG_CHECK_MS; serviceChargeTemp();
    }, c == 2004 ? "\"temperature_mc\":54000" : c == 2005 ? "\"temperature_mc\":9000" : "\"context\":{}");
    // Guard hysteresis and changing reasons without charger resume.
    faultWorld(); PMU.dieC = CHG_HOT_PAUSE_C; serviceChargeTemp(); one(2004, "raise");
    PMU.dieC = CHG_HOT_RESUME_C + 1; g_millis += CHG_CHECK_MS; serviceChargeTemp(); assert(take().empty());
    PMU.dieC = NAN; g_millis += CHG_CHECK_MS; serviceChargeTemp(); one(2006, "raise");
    PMU.dieC = CHG_COLD_PAUSE_C; g_millis += CHG_CHECK_MS; serviceChargeTemp();
    auto changed = take(); assert(codeRows(changed, 2004).size() == 1 && codeRows(changed, 2005).size() == 1 && codeRows(changed, 2006).size() == 1);
    PMU.dieC = CHG_COLD_RESUME_C; g_millis += CHG_CHECK_MS; serviceChargeTemp(); one(2005, "clear");
    assert(PMU.chargeEnabled);
    // Thresholds come from the PMU configuration, not guessed voltage cutoffs.
    faultWorld(); PMU.lowWarnNibble = 15; PMU.criticalPercent = 8;
    PMU.batteryPercent = 20; serviceBatteryDiagnostics(); one(2002, "raise");
    PMU.batteryConnected = false; PMU.batteryPercent = 90;
    gBatteryDiagChecked = false; serviceBatteryDiagnostics(); assert(take().empty());
    PMU.batteryConnected = true; gBatteryDiagChecked = false;
    serviceBatteryDiagnostics(); one(2002, "clear");
    faultWorld(); PMU.lowIrq = true; gPmuIrqFlag = true; pollPmu(); one(2002, "raise");
    faultWorld(); PMU.criticalIrq = true; gPmuIrqFlag = true;
    // USB means shutdown returns; the critical IRQ must still be recorded.
    pollPmu(); one(2003, "raise");

    cycle(3001, [](bool bad) {
        runtimeHealthy();
        if (bad) gLastNmeaMs = g_millis - 10000;
        else feed(nmea("GNTXT,01,01,02,ready"));
        serviceRuntimeDiagnostics();
    }, "\"elapsed_ms\":10000");
    cycle(3002, [](bool bad) {
        runtimeHealthy(); gHaveLastFix = false;
        if (bad) gGpsDiagStartMs = gIdleFixWaitMs = g_millis - 120000;
        serviceRuntimeDiagnostics();
    }, "\"elapsed_ms\":120000");
    cycle(3005, [](bool bad) {
        runtimeHealthy();
        if (bad) { gState = ACTIVE; gHaveLastFix = true; gLastFixMonoMs = g_millis - 30000; }
        else { gState = IDLE; feed(rmc("143000.00", "140426")); }
        serviceRuntimeDiagnostics();
    }, "\"elapsed_ms\":30000");
    cycle(3006, [](bool bad) {
        runtimeHealthy(); gClockSet = !bad; gGpsDiagStartMs = g_millis - 120000;
        serviceRuntimeDiagnostics();
    }, "\"context\":{}");
    faultWorld(); gClockSet = false; serviceRuntimeDiagnostics(); assert(take().empty());
    g_millis += 9999; serviceRuntimeDiagnostics(); assert(take().empty());
    g_millis++; serviceRuntimeDiagnostics(); one(3001, "raise");
    feed("$GNTXT,01,01,02,ready*00\r\n"); serviceRuntimeDiagnostics(); assert(take().empty());
    feed(nmea("GNTXT,01,01,02,ready")); serviceRuntimeDiagnostics(); one(3001, "clear");
    // Deadline arithmetic crosses the 32-bit millis rollover safely.
    faultWorld(); g_millis = UINT32_MAX - 5000; runtimeHealthy();
    serviceRuntimeDiagnostics(); assert(take().empty());
    g_millis += 10000; gGpsDiagStartMs = gIdleFixWaitMs = g_millis;
    serviceRuntimeDiagnostics(); one(3001, "raise");
    cycle(4001, [](bool bad) { runtimeHealthy(); gSdMounted = !bad; serviceRuntimeDiagnostics(); }, "\"context\":{}");
    for (int c : {4004, 4005}) cycle(c, [c](bool bad) {
        // Production admission/stop paths report the exact guarded free space.
        g_sdDisk.capacity = g_sdDisk.used() + (bad ? (c == 4004 ? FS_START_FREE - 1 : FS_STOP_FREE - 1) : FS_START_FREE);
        roomForNewTrip(g_millis);
        gFsCheckT0 = g_millis - FS_CHECK_MS; tripStorageLow(g_millis);
    }, "\"backend\":\"sd\"");
    cycle(4006, [](bool bad) {
        Fix fx = {}; fx.hasUtc = true;
        // Empty fake media per attempt; changing it does no journal work.
        g_sdDisk.files.clear(); g_sdDisk.failNextOpens = bad ? 1 : 0;
        beginTrip(fx);
    }, "\"operation\":\"open\"");
#if DISPLAY_DEBUG
    cycle(6001, [](bool bad) {
        runtimeHealthy();
        if (bad && !gDebugDisplayFailed) { Wire.failBegins = 1; beginDebugDisplay(); }
        if (!bad) { gDebugDisplayFailed = false; beginDebugDisplay(); }
        serviceRuntimeDiagnostics();
    }, "\"context\":{}");
#endif
    cycle(5002, [](bool bad) { WiFi.connectionStatus = bad ? 0 : WL_CONNECTED; connectWifi(); }, "\"attempts\":1");
    const int statuses[] = {-1, 401, 413, 400, 409, 503, 404};
    for (int i = 0; i < 7; ++i) cycle(5003 + i, [i, &statuses](bool bad) {
        g_sdDisk.files["/trips/upload.gpx"] = std::string(GPX_PROLOGUE) + GPX_EPILOGUE;
        g_httpCode = bad ? statuses[i] : 200; uploadFile(SD, "/trips/upload.gpx");
    }, i == 0 ? "\"attempts\":1" : "\"target\":\"trip\"");
    // Error classes stay active until success, not merely a different failure.
    faultWorld(); uploadDiagnostics(401); one(5004, "raise");
    uploadDiagnostics(503); one(5008, "raise"); uploadDiagnostics(200);
    auto clears = take(); assert(codeRows(clears, 5004).size() == 1 && codeRows(clears, 5008).size() == 1);
    gNetAbort = true; uploadDiagnostics(-1); assert(take().empty());
    faultWorld();
    for (int i = 0; i < 1000; ++i) assert(!homeApInRange());
    assert(gNetFaults.homeApMisses() == 1000 && take().empty());
    WiFi.scanError = -1; assert(!homeApInRange()); assert(gNetFaults.homeApMisses() == 1000);
    WiFi.scanError = 0; WiFi.scanSsids = {WIFI_SSID}; assert(homeApInRange());
    assert(gNetFaults.homeApMisses() == 1000 && take().empty());
    faultWorld(); for (int i = 0; i < 300; ++i) { WiFi.connectionStatus = 0; connectWifi(); take(); }
    assert(gJoinFailures == 255);

    // Actual checksum-checked VALSET NAK and terminal timeout paths.
    faultWorld(); configureGps(); feed(nmea("GNTXT,01,01,02,ready")); serviceGpsConfiguration();
    feed(ack(0x06, 0x8A, false)); serviceGpsConfiguration();
    auto nak = one(3003, "notice"); has(nak, "\"message_class\":6"); has(nak, "\"message_id\":138");
    serviceGpsConfiguration(); assert(take().empty());
    faultWorld(); configureGps(); feed(nmea("GNTXT,01,01,02,ready"));
    while (gGpsConfig.state != GPS_CONFIG_DONE) { g_millis += 8000; serviceGpsConfiguration(); }
    auto timeout = one(3004, "notice"); has(timeout, "\"message_id\":138");
    for (int i = 0; i < 10; ++i) serviceGpsConfiguration(); assert(take().empty());
    faultWorld(); configureGps(); // no NMEA at all: readiness give-up also reports
    while (gGpsConfig.state != GPS_CONFIG_DONE) { g_millis += 8000; serviceGpsConfiguration(); }
    one(3004, "notice");

    // Boot fsck: no event on complete files; actual repair counts points;
    // read/open, write and rename failures preserve recoverable trip data.
    for (const char* reason : {"read", "write", "rename"}) {
        faultWorld(); g_sdDisk.files["/trips/broken.gpx"] = std::string(GPX_PROLOGUE) + "<trkpt></trkpt>\npartial";
        if (!std::strcmp(reason, "read")) g_sdDisk.failNextOpens = 1;
        if (!std::strcmp(reason, "write")) g_sdDisk.failFlushes = 1;
        if (!std::strcmp(reason, "rename")) g_sdDisk.failRenames = 1;
        fsckAllTrips(SD); auto failed = one(4007, "notice");
        has(failed, std::string("\"reason\":\"") + reason + "\"");
        g_sdDisk.failNextOpens = g_sdDisk.failFlushes = g_sdDisk.failRenames = 0;
        fsckAllTrips(SD); one(4010, "notice");
        fsckAllTrips(SD); assert(take().empty());
    }
    faultWorld(); g_sdDisk.files["/trips/broken.gpx"] = std::string(GPX_PROLOGUE) + "<trkpt></trkpt>\n<trkpt></trkpt>\npartial";
    fsckAllTrips(SD); auto repaired = one(4010, "notice"); has(repaired, "\"points\":2");
    faultWorld(); g_sdDisk.files["/trips/orphan.gpx.fix"] = std::string(GPX_PROLOGUE) + "<trkpt></trkpt>\n" + GPX_EPILOGUE;
    fsckAllTrips(SD); one(4010, "notice"); fsckAllTrips(SD); assert(take().empty());
    faultWorld(); g_sdDisk.files["/trips/ambiguous.gpx.fix"] = "unrecognized repair";
    fsckAllTrips(SD); has(one(4007, "notice"), "\"reason\":\"parse\"");

    // Write and finalize failures reach E4006 as well as failed creates.
    faultWorld(); Fix fx = {}; fx.hasUtc = true; assert(beginTrip(fx)); assert(take().empty());
    g_sdDisk.failFlushes = 1; assert(!writeOrBufferTrkpt(fx));
    has(one(4006, "raise"), "\"operation\":\"write\"");
    assert(writeOrBufferTrkpt(fx)); one(4006, "clear");
    faultWorld(); assert(beginTrip(fx)); g_sdDisk.failFlushes = 1;
    endTrip(false); auto finalize = codeRows(take(), 4006);
    assert(finalize.size() == 2);
    has(finalize[0], "\"transition\":\"raise\""); has(finalize[0], "\"operation\":\"close\"");
    has(finalize[1], "\"transition\":\"clear\"");

    // Five-minute finalization must not manufacture a fix recovery.
    faultWorld(); run(15, 40); assert(gState == ACTIVE); take();
    g_millis = gLastFixMonoMs + FIX_LOSS_END_MS + 1; runtimeHealthy();
    gLastFixMonoMs = g_millis - FIX_LOSS_END_MS - 1;
    loop(); one(3005, "raise"); assert(gState == IDLE);
    loop(); assert(take().empty()); feed(rmc("143100.00", "140426")); one(3005, "clear");

    // Queue overflow: no invented episode after rejected raise, bounded retry,
    // and failed clear retains the original identity until it can be queued.
    faultWorld(); for (unsigned i = 0; i < kEventQueueSize; ++i) assert(gJournal.submit(EventInput()) == EventLog::Submit::Accepted);
    reportNetworkStack("healthy"); g_stackHighWaterBytes = 1000; reportNetworkStack("full");
    auto drops = gJournal.stats().dropped;
    for (int i = 0; i < 1000; ++i) reportNetworkStack("full");
    assert(gJournal.stats().dropped == drops); take();
    g_millis += RuntimeFaults::kRetryMs; reportNetworkStack("retry"); auto raise = one(1007, "raise");
    for (unsigned i = 0; i < kEventQueueSize; ++i) assert(gJournal.submit(EventInput()) == EventLog::Submit::Accepted);
    g_stackHighWaterBytes = 4096; reportNetworkStack("clear full"); drops = gJournal.stats().dropped;
    for (int i = 0; i < 1000; ++i) reportNetworkStack("clear full");
    assert(gJournal.stats().dropped == drops); take();
    g_millis += RuntimeFaults::kRetryMs; reportNetworkStack("clear retry");
    assert(episode(one(1007, "clear")) == episode(raise));
    faultWorld(); g_stackHighWaterBytes = 1000; reportNetworkStack("raise"); raise = one(1007, "raise");
    for (unsigned i = 0; i < kEventQueueSize; ++i) assert(gJournal.submit(EventInput()) == EventLog::Submit::Accepted);
    g_stackHighWaterBytes = 4096; reportNetworkStack("clear full"); take();
    g_stackHighWaterBytes = 1000; reportNetworkStack("relapse after dropped clear");
    assert(take().empty()); // no premature repeat or invented second episode
    g_millis += RuntimeFaults::kRepeatMs; reportNetworkStack("repeat");
    assert(episode(one(1007, "repeat")) == episode(raise));
    g_stackHighWaterBytes = 4096; reportNetworkStack("clear"); one(1007, "clear");

    // Normal boot + real-parser drive + parked cooldown + actual HTTP body
    // upload: only the boot notice, with no fault events.
    faultWorld(); PmuStartupResult clean; clean.ready = true; clean.attempts = 1;
    submitJournalBoot(gJournal, ESP_RST_POWERON, clean, millis());
    run(15, 40); run(0, 185); assert(gState == IDLE);
    assert(uploadPending(SD)); auto happy = take();
    assert(happy.size() == 1); has(happy[0], "\"code\":1000,");
    assert(gJournal.stats().dropped == 0 && g_lockDepth == 0);
    if (exportJson) for (const auto& row : exported) std::printf("EVENT %s\n", row.c_str());
    std::printf("Runtime hook transitions, fields, counter, overflow and happy drive passed (DISPLAY_DEBUG=%d).\n", DISPLAY_DEBUG);
}
#endif
