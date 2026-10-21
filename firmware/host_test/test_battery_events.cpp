// Production sketch, checked PMU failures and mounted journal shutdown ordering.
#define main journalRegressionMain
#include "test_journal.cpp"
#undef main

static std::string bootRecord() {
    xSemaphoreTake(gFsMutex, portMAX_DELAY);
    assert(gJournal.nextBatch(batch));
    for (size_t i = 0; i < batch.count; ++i)
        if (std::strstr(batch.events[i], "\"code\":1000,")) {
            std::string row = batch.events[i];
            xSemaphoreGive(gFsMutex);
            return row;
        }
    assert(false); return {};
}
static unsigned powerOffRecords(FakeDisk& disk) {
    unsigned count = 0;
    for (const auto& file : disk.files) {
        if (file.first.rfind("/diag/", 0)) continue;
        assert(file.first.find(".open") == std::string::npos);
        size_t at = 0;
        while ((at = file.second.find("\"code\":2009,", at)) != std::string::npos) { ++count; ++at; }
    }
    return count;
}
int main(int argc, char**) {
    PmuStartupResult clean; clean.ready = true; clean.attempts = 1;
    for (int reg : {-1, 0x00, 0x34, 0x35, 0xA4}) {
        world(false, false);
        if (reg >= 0) PMU.failRegisterReadAt[reg] = 1;
        auto sample = readBatterySample(PMU);
        submitJournalBoot(gJournal, ESP_RST_POWERON, clean, 100, sample);
        auto row = bootRecord();
        bool mvKnown = reg == -1 || reg == 0xA4;
        bool pctKnown = reg == -1 || reg == 0x34 || reg == 0x35;
        assert((row.find("\"battery_mv\":4020") != std::string::npos) == mvKnown);
        assert((row.find("\"battery_pct\":85") != std::string::npos) == pctKnown);
        if (argc > 1) std::printf("EVENT %s\n", row.c_str());
    }
    for (int percent : {0, 100, 101, 255, -1}) {
        world(false, false); PMU.batteryPercent = percent;
        auto sample = readBatterySample(PMU);
        assert(sample.percent == (percent >= 0 && percent <= 100 ? percent : -1));
    }
    for (int voltage : {0, 1, 4200, 6000, 6001, 8191}) {
        world(false, false); PMU.batteryMv = voltage;
        assert(readBatterySample(PMU).mv == (voltage > 0 && voltage <= 6000 ? voltage : -1));
    }
    world(false, false); PMU.batteryConnected = false;
    auto absent = readBatterySample(PMU);
    assert(absent.mv == -1 && absent.percent == -1);
    assert(PMU.registerReadCalls[0x34] == 0 && PMU.registerReadCalls[0xA4] == 0);
    world(false, false); PMU.registerReadOverride[0x34] = 0xE0 | (PMU.batteryMv >> 8);
    assert(readBatterySample(PMU).mv == 4020); // ignore reserved ADC bits

    // Early capture uses the enabled ADC after the USB wait and before charger
    // enable. Changing the hardware reading later cannot change the BOOT.
    world(true, true);
    g_millis = 0;
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
    bool captured = false;
    PMU.onRegisterRead = [&](uint8_t reg) {
        if (reg == 0x34 && !captured) {
            assert(!PMU.chargeEnabled && g_millis >= 1000);
            assert(std::find(PMU.operations.begin(), PMU.operations.end(), "enableBattVoltageMeasure") != PMU.operations.end());
            assert(std::find(PMU.operations.begin(), PMU.operations.end(), "enableCellbatteryCharge") == PMU.operations.end());
            captured = true;
        }
    };
    setup(); assert(captured);
    PMU.onRegisterRead = nullptr;
    PMU.batteryMv = 4300; PMU.batteryPercent = 100;
    auto boot = bootRecord();
    assert(boot.find("\"battery_mv\":4020") != std::string::npos);
    assert(boot.find("\"battery_pct\":85") != std::string::npos);
    if (argc > 1) std::printf("EVENT %s\n", boot.c_str());

    for (bool sd : {false, true}) for (const char* reason : {"idle_battery", "battery_critical", "user_long_press"})
    for (int reg : {-1, 0x00, 0x34, 0x35, 0xA4}) {
        world(sd, true);
        // Even a full queue must leave room for exactly one power-off sample.
        submit(kEventQueueSize); PMU.vbusIn = false;
        if (reg >= 0) PMU.failRegisterReadAt[reg] = reg == 0x00 ? 2 : 1; // after entry VBUS read
        g_taskDelayHook = [](uint32_t) { if (!g_lockDepth) serviceJournal(); };
        auto verify = [] {
            assert(gJournalSealDone && gJournalSealOk && !gPowerOffEventPending);
            assert(gJournal.stats().queued == 0);
            assert(powerOffRecords(gSdMounted ? g_sdDisk : g_disk) == 1);
        };
        if (sd) g_sdDisk.onUnmount = verify; else g_disk.onUnmount = verify;
        try { powerOffForVbus(reason); assert(false); } catch (const PoweredOff&) {}
        // Reopen journal media like a later boot; inspect and export the frame.
        g_lockDepth = 0; gFsDemand = 0; gNetHold = gNetAbort = false;
        g_sdDisk.mounted = sd; g_disk.mounted = true;
        xSemaphoreTake(gFsMutex, 0);
        gJournal.invalidateStorage();
        assert(gJournal.nextBatch(batch) && batch.count == kEventQueueSize + 1);
        const char* row = batch.events[batch.count - 1];
        assert(std::strstr(row, "\"code\":2009,"));
        assert(std::strstr(row, reason));
        bool mvKnown = reg == -1 || reg == 0xA4;
        bool pctKnown = reg == -1 || reg == 0x34 || reg == 0x35;
        assert(bool(std::strstr(row, "\"battery_mv\":4020")) == mvKnown);
        assert(bool(std::strstr(row, "\"battery_pct\":85")) == pctKnown);
        if (argc > 1) std::printf("EVENT %s\n", row);
        xSemaphoreGive(gFsMutex);
    }
    world(false, false); PMU.vbusIn = false;
    gDegraded.begin(startup::Mode::NoStorage, millis());
    try { powerOffForVbus(); assert(false); } catch (const PoweredOff&) {}
    assert(!gJournalSealOk); // RAM is never described as durable
    std::puts("Battery events: checked failures, early BOOT and full-queue durable power-off passed");
}
