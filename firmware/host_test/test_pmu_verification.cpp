// Real sketch paths and diagnostic journal, with register-level PMU failures.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"
using pmu_read::ChargeState;
static unsigned cases = 0;
static void pmuWorld() {
    faultWorld();
    gDegraded = startup::DegradedPolicy();
    gVbusOffPending = false;
    gJournalSealRequested = gJournalSealDone = false;
    g_lockDepth = 0;
}
static void failRead(uint8_t reg) {
    PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg] + 1;
}
static unsigned calls(const char* operation) {
    return std::count(PMU.operations.begin(), PMU.operations.end(), operation);
}
static void chargeTick() { g_millis += CHG_CHECK_MS; serviceChargeTemp(); }

int main() {
    auto bootRows = []() { FS_LOCK(); auto rows = take(); FS_UNLOCK(); return rows; };
    // A failed voltage write and a failed ADC read recover on the next attempt.
    pmuWorld();
    PMU.failAldo4VoltageWrites = 1;
    PMU.adcChannels = 0x01;
    std::vector<uint32_t> gpsAttempts, adcAttempts;
    PMU.onRegisterRead = [&](uint8_t reg) {
        if (reg == XPOWERS_AXP2101_LDO_VOL3_CTRL) gpsAttempts.push_back(millis());
        if (reg == XPOWERS_AXP2101_ADC_CHANNEL_CTRL && calls("enableTemperatureMeasure")) {
            if (adcAttempts.empty()) PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg];
            adcAttempts.push_back(millis());
        }
    };
    g_delayHook = [](uint32_t ms) { g_millis += ms; };
    setup();
    PMU.onRegisterRead = nullptr;
    g_delayHook = nullptr;
    assert(gpsAttempts.size() == 3 && gpsAttempts[1] - gpsAttempts[0] == PMU_PERIPHERAL_RETRY_MS);
    assert(gpsAttempts[2] == gpsAttempts[1]);
    assert(adcAttempts.size() == 2 && adcAttempts[1] - adcAttempts[0] == PMU_PERIPHERAL_RETRY_MS);
    assert(calls("setALDO4Voltage") == 2 && calls("enableALDO4") == 1 && PMU.aldo4 && PMU.aldo4Mv == 3300);
    assert(calls("enableTemperatureMeasure") == 2 && calls("enableVbusVoltageMeasure") == 2 &&
           calls("enableSystemVoltageMeasure") == 2 && PMU.adcChannels == 0x1D);
    assert(codeRows(bootRows(), 2010).empty() && !logCount("setup unconfirmed after "));
    ++cases;

    // Redundant writes can fail while the requested settings are retained.
    pmuWorld();
    PMU.aldo4Mv = 3300;
    PMU.failAldo4VoltageWrites = PMU_PERIPHERAL_ATTEMPTS;
    setup();
    assert(calls("setALDO4Voltage") == 1 && calls("enableALDO4") == 1);
    assert(PMU.aldo4 && logCount("GPS rail (ALDO4) verified on at 3.3 V") == 1);
    assert(codeRows(bootRows(), 2010).empty());
    ++cases;

    pmuWorld();
    PMU.failPeripheralAdcWrites = true;
    setup();
    assert(calls("enableTemperatureMeasure") == 1 && calls("enableVbusVoltageMeasure") == 1 &&
           calls("enableSystemVoltageMeasure") == 1 && PMU.adcChannels == 0x1D);
    assert(!logCount("measurement setup unconfirmed") && codeRows(bootRows(), 2010).empty());
    ++cases;

    // Reject a cleared enable, wrong voltage, unreadable register, or any
    // single missing ADC bit, even when the methods claim the writes succeeded.
    for (auto [reg, value] : std::initializer_list<std::pair<uint8_t, int>>{
            {XPOWERS_AXP2101_LDO_ONOFF_CTRL0, 0x01},
            {XPOWERS_AXP2101_LDO_VOL3_CTRL, 27},
            {XPOWERS_AXP2101_LDO_VOL3_CTRL, -1},
            {XPOWERS_AXP2101_ADC_CHANNEL_CTRL, 0x0D},
            {XPOWERS_AXP2101_ADC_CHANNEL_CTRL, 0x15},
            {XPOWERS_AXP2101_ADC_CHANNEL_CTRL, 0x19},
            {XPOWERS_AXP2101_ADC_CHANNEL_CTRL, -1}}) {
        pmuWorld();
        if (reg == XPOWERS_AXP2101_LDO_ONOFF_CTRL0) {
            // Only the GPS read-back loses its enable bit. The shared control
            // register must still report BLDO1 enabled when SD is checked.
            PMU.onRegisterRead = [&](uint8_t readReg) {
                if (readReg != reg) return;
                if (PMU.aldo4 && !PMU.bldo1) PMU.registerReadOverride[reg] = value;
                else PMU.registerReadOverride.erase(reg);
            };
        } else {
            PMU.registerReadOverride[reg] = value;
        }
        setup();
        PMU.onRegisterRead = nullptr;
        const bool adc = reg == XPOWERS_AXP2101_ADC_CHANNEL_CTRL;
        assert(calls("setALDO4Voltage") == (adc ? 1u : PMU_PERIPHERAL_ATTEMPTS));
        if (reg == XPOWERS_AXP2101_LDO_VOL3_CTRL)
            assert(calls("enableALDO4") == 0 && !PMU.aldo4);
        assert(calls("enableTemperatureMeasure") == (adc ? PMU_PERIPHERAL_ATTEMPTS : 1u));
        assert(logCount(adc ? "measurement setup unconfirmed after " :
                              "ALDO4) 3.3 V/on setup unconfirmed after ") == 1);
        assert(gDegraded.mode() == startup::Mode::Normal && tripStoreAvailable(SD));
        auto rows = bootRows();
        auto warning = codeRows(rows, 2010);
        assert(warning.size() == 1 && codeRows(rows, 2001).empty());
        has(warning[0], "\"transition\":\"raise\"");
        ++cases;
    }

    for (int raw = 0; raw <= 0xFF; ++raw) {
        assert(pmu_read::batteryDetectionEnabled(raw) == bool(raw & 1));
        assert(pmu_read::batteryVoltageMeasureEnabled(raw) == bool(raw & 1));
    }
    assert(!pmu_read::batteryDetectionEnabled(-1));
    assert(!pmu_read::batteryVoltageMeasureEnabled(-1));
    ++cases;

    enum class BatterySetupFault { TransientWrite, PersistentWrite, Unreadable };
    for (bool detection : {true, false}) {
        const auto reg = detection ? XPOWERS_AXP2101_BAT_DET_CTRL : XPOWERS_AXP2101_ADC_CHANNEL_CTRL;
        const auto operation = detection ? "enableBattDetection" : "enableBattVoltageMeasure";
        const auto warning = detection ? "PMU battery detection enable unconfirmed" :
                                         "PMU battery voltage measurement enable unconfirmed";
        for (auto [fault, attempts] : {std::pair{BatterySetupFault::TransientWrite, 2u},
                                       std::pair{BatterySetupFault::PersistentWrite, PMU_PERIPHERAL_ATTEMPTS},
                                       std::pair{BatterySetupFault::Unreadable, PMU_PERIPHERAL_ATTEMPTS}}) {
            pmuWorld();
            PMU.batteryDetection = false;
            PMU.adcChannels &= ~0x01;
            auto& failedWrites = detection ? PMU.failBatteryDetectionWrites : PMU.failBatteryVoltageWrites;
            failedWrites = fault == BatterySetupFault::TransientWrite ? 1 :
                           fault == BatterySetupFault::PersistentWrite ? attempts : 0;
            std::vector<uint32_t> readTimes;
            PMU.onRegisterRead = [&](uint8_t readReg) {
                if (readReg != reg || readTimes.size() >= calls(operation)) return;
                readTimes.push_back(millis());
                if (fault == BatterySetupFault::Unreadable)
                    PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg];
            };
            g_delayHook = [](uint32_t ms) { g_millis += ms; };
            setup();
            PMU.onRegisterRead = nullptr;
            g_delayHook = nullptr;
            assert(calls(operation) == attempts && readTimes.size() == attempts);
            assert(calls(detection ? "enableBattVoltageMeasure" : "enableBattDetection") == 1);
            for (unsigned i = 1; i < attempts; ++i)
                assert(readTimes[i] - readTimes[i - 1] == PMU_PERIPHERAL_RETRY_MS);
            assert((detection ? PMU.batteryDetection : bool(PMU.adcChannels & 1)) ==
                   (fault != BatterySetupFault::PersistentWrite));
            const bool confirmed = fault == BatterySetupFault::TransientWrite;
            assert(logCount(warning) == int(!confirmed));
            assert(gDegraded.mode() == startup::Mode::Normal && tripStoreAvailable(SD));
            auto warnings = codeRows(bootRows(), 2010);
            assert(warnings.size() == unsigned(!confirmed));
            if (!confirmed) has(warnings[0], R"("transition":"raise")");
            ++cases;
        }
    }

    constexpr auto tsReg = XPOWERS_AXP2101_TS_PIN_CTRL;
    constexpr auto adcReg = XPOWERS_AXP2101_ADC_CHANNEL_CTRL;
    for (int raw = 0; raw <= 0xFF; ++raw) {
        assert(pmu_read::tsFixedInputReady(raw, 0x1D) == ((raw & 0x1F) == 0x10));
        assert(pmu_read::tsFixedInputReady(0xF0, raw) == ((raw & 0x02) == 0));
    }
    assert(!pmu_read::tsFixedInputReady(-1, 0x1D));
    assert(!pmu_read::tsFixedInputReady(0x10, -1));
    ++cases;

    enum class TsFault { TransientWrite, PersistentWrite, Unreadable };
    for (auto reg : {tsReg, adcReg}) {
        for (auto [fault, writes] : {std::pair{TsFault::TransientWrite, 2u},
                                     std::pair{TsFault::PersistentWrite, PMU_PERIPHERAL_ATTEMPTS},
                                     std::pair{TsFault::Unreadable, 0u}}) {
            pmuWorld();
            // Fail one library TS write. begin() still reports success.
            PMU.tsControl = 0xB5;
            PMU.adcChannels = 0x9F;
            PMU.failRegisterWriteAt[reg] = 1;
            std::vector<uint32_t> writeTimes;
            PMU.onRegisterWrite = [&](uint8_t writeReg, uint8_t value) {
                if (writeReg != tsReg && writeReg != adcReg) return;
                assert(value == (writeReg == tsReg ? 0xB0 : 0x9D));
                if (PMU.registerWriteCalls[writeReg] == 1) {
                    assert(PMU.chargeEnabled && calls("disableCellbatteryCharge") == 0);
                    return;
                }
                assert(!PMU.chargeEnabled && gEarlyChargeDisabled);
                if (writeReg != reg) return;
                writeTimes.push_back(millis());
                if (fault == TsFault::PersistentWrite || writeTimes.size() == 1)
                    PMU.failRegisterWriteAt[reg] = PMU.registerWriteCalls[reg];
            };
            if (fault == TsFault::Unreadable) {
                PMU.onRegisterRead = [&](uint8_t readReg) {
                    if (readReg == reg && PMU.registerReadCalls[tsReg] > 1 && !calls("enableTemperatureMeasure"))
                        PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg];
                };
            }
            g_delayHook = [](uint32_t ms) { g_millis += ms; };
            setup();
            g_delayHook = nullptr;
            PMU.onRegisterRead = nullptr;
            PMU.onRegisterWrite = nullptr;
            assert(writeTimes.size() == writes && PMU.registerWriteCalls[reg] == writes + 1);
            for (unsigned i = 1; i < writes; ++i)
                assert(writeTimes[i] - writeTimes[i - 1] == PMU_PERIPHERAL_RETRY_MS);
            if (fault == TsFault::Unreadable) {
                assert(PMU.registerReadCalls[tsReg] == 1 + PMU_PERIPHERAL_ATTEMPTS);
                assert(PMU.registerWriteCalls[tsReg] == 1 && PMU.registerWriteCalls[adcReg] == 1);
            }
            const bool confirmed = fault == TsFault::TransientWrite;
            assert(pmu_read::tsFixedInputReady(PMU.tsControl, PMU.adcChannels) == confirmed);
            assert(logCount("TS fixed-input setup unconfirmed after ") == int(!confirmed));
            assert(gDegraded.mode() == startup::Mode::Normal && tripStoreAvailable(SD));
            auto rows = bootRows();
            auto warnings = codeRows(rows, 2010);
            assert(warnings.size() == unsigned(!confirmed) && codeRows(rows, 2008).empty());
            if (!confirmed) has(warnings[0], R"("transition":"raise")");
            ++cases;
        }
    }

    assert(pmu_read::bldo1Off(0x00) && pmu_read::bldo1Off(0x09));
    assert(!pmu_read::bldo1Off(0x10) && !pmu_read::bldo1Off(0x19));
    assert(!pmu_read::bldo1Off(-1));

    for (int control = 0; control <= 0xFF; ++control)
        assert(pmu_read::vbackupOff(control) == ((control & 0x04) == 0));
    assert(!pmu_read::vbackupOff(-1));
    ++cases;

    // Boot reports VBACKUP only after USB serial has had time to come up.
    enum class VbackupFault { TransientWrite, PersistentWrite, Unreadable };
    constexpr auto chargeReg = XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL;
    for (auto [fault, attempts] : {std::pair{VbackupFault::TransientWrite, 2u},
                                   std::pair{VbackupFault::PersistentWrite, PMU_PERIPHERAL_ATTEMPTS},
                                   std::pair{VbackupFault::Unreadable, PMU_PERIPHERAL_ATTEMPTS}}) {
        pmuWorld();
        PMU.failVbackupDisableWrites = fault == VbackupFault::TransientWrite ? 1 :
                                       fault == VbackupFault::PersistentWrite ? PMU_PERIPHERAL_ATTEMPTS : 0;
        std::vector<uint32_t> readTimes;
        PMU.onRegisterRead = [&](uint8_t reg) {
            if (reg != chargeReg || readTimes.size() == calls("disableButtonBatteryCharge")) return;
            readTimes.push_back(millis());
            if (fault == VbackupFault::Unreadable)
                PMU.failRegisterReadAt[reg] = PMU.registerReadCalls[reg];
        };
        g_delayHook = [](uint32_t ms) { g_millis += ms; };
        setup();
        g_delayHook = nullptr;
        PMU.onRegisterRead = nullptr;
        assert(calls("disableButtonBatteryCharge") == attempts && readTimes.size() == attempts);
        for (unsigned i = 1; i < attempts; ++i)
            assert(readTimes[i] - readTimes[i - 1] == PMU_PERIPHERAL_RETRY_MS);
        assert(PMU.vbackup == (fault == VbackupFault::PersistentWrite));
        const bool confirmed = fault == VbackupFault::TransientWrite;
        assert(logCount("VBACKUP verified off") == int(confirmed));
        assert(logCount("VBACKUP off unconfirmed") == int(!confirmed));
        for (unsigned i = 0; i < Serial.log.size(); ++i) {
            if (Serial.log[i].find("VBACKUP") == std::string::npos) continue;
            assert(Serial.logMs[i] - Serial.beginMs >= 1000);
            assert((Serial.log[i].find("WARNING: ") == 0) == !confirmed);
        }
        assert(gDegraded.mode() == startup::Mode::Normal && tripStoreAvailable(SD));
        ++cases;
    }

    // A transient voltage or enable write failure must not cost this boot its card.
    for (bool voltageFault : {true, false}) {
        pmuWorld();
        PMU.failSdVoltage = voltageFault;
        PMU.failSdEnable = !voltageFault;
        g_delayHook = [](uint32_t ms) {
            if (ms == PMU_PERIPHERAL_RETRY_MS && calls("setBLDO1Voltage") == 1) {
                assert(!PMU.bldo1 && SD.begins == 0);
                assert(calls("enableBLDO1") == (PMU.failSdVoltage ? 0u : 1u));
                PMU.failSdVoltage = PMU.failSdEnable = false;
            }
            g_millis += ms;
        };
        setup();
        g_delayHook = nullptr;
        assert(calls("setBLDO1Voltage") == 2);
        assert(calls("enableBLDO1") == (voltageFault ? 1u : 2u));
        assert(SD.begins == 1 && PMU.bldo1 && gTripFS == &SD);
        assert(!logCount("microSD card power rail (BLDO1) could not be verified"));
        assert(codeRows(bootRows(), 4001).empty());
        ++cases;
    }

    // The disable command can fail while the final settings are verifiable.
    pmuWorld();
    PMU.failSdDisable = true;
    setup();
    assert(calls("setBLDO1Voltage") == 1 && calls("enableBLDO1") == 1);
    assert(SD.begins == 1 && PMU.bldo1 && gTripFS == &SD);
    assert(codeRows(bootRows(), 4001).empty());
    ++cases;

    // Post-enable mismatch or failed read repeats the power sequence.
    // SPI starts only once the rail is verified.
    for (int badRead : {-1, 27}) for (bool persistent : {false, true}) {
        pmuWorld();
        constexpr auto sdVoltageReg = XPOWERS_AXP2101_LDO_VOL4_CTRL;
        std::vector<uint32_t> readTimes;
        PMU.onRegisterRead = [&](uint8_t readReg) {
            if (readReg != sdVoltageReg || !calls("setBLDO1Voltage")) return;
            if (!PMU.bldo1) {
                PMU.registerReadOverride.erase(readReg);
                return;
            }
            assert(SD.begins == 0);
            if (persistent || readTimes.empty()) PMU.registerReadOverride[readReg] = badRead;
            else PMU.registerReadOverride.erase(readReg);
            readTimes.push_back(millis());
        };
        g_delayHook = [](uint32_t ms) { g_millis += ms; };
        setup();
        g_delayHook = nullptr;
        PMU.onRegisterRead = nullptr;
        const unsigned attempts = persistent ? PMU_PERIPHERAL_ATTEMPTS : 2;
        assert(readTimes.size() == attempts);
        for (unsigned i = 1; i < attempts; ++i)
            assert(readTimes[i] - readTimes[i - 1] == PMU_PERIPHERAL_RETRY_MS);
        assert(calls("setBLDO1Voltage") == attempts && calls("enableBLDO1") == attempts);
        assert(SD.begins == (persistent ? 0 : 1) && PMU.bldo1 == !persistent);
        assert(gTripFS == (persistent ? static_cast<fs::FS*>(&LittleFS) : static_cast<fs::FS*>(&SD)));
        assert(logCount("microSD card power rail (BLDO1) could not be verified") == int(persistent));
        assert(codeRows(bootRows(), 4001).size() == unsigned(persistent));
        ++cases;
    }

    // Exercise real degraded SD cleanup with consecutive PMU faults.
    enum class SdOffFault { None, TransientWrite, PersistentWrite, Unreadable };
    constexpr auto reg = XPOWERS_AXP2101_LDO_ONOFF_CTRL0;
    for (auto [fault, attempts] : {std::pair{SdOffFault::None, 1u},
                                   std::pair{SdOffFault::TransientWrite, 2u},
                                   std::pair{SdOffFault::PersistentWrite, PMU_PERIPHERAL_ATTEMPTS},
                                   std::pair{SdOffFault::Unreadable, PMU_PERIPHERAL_ATTEMPTS}}) {
        pmuWorld(); // mounted SD with BLDO1 enabled
        PMU.bldo1Mv = 3300;
        const auto reads = PMU.registerReadCalls[reg];
        const auto disables = calls("disableBLDO1");
        PMU.failSdDisable = fault == SdOffFault::TransientWrite || fault == SdOffFault::PersistentWrite;
        if (fault == SdOffFault::TransientWrite) {
            PMU.onRegisterRead = [](uint8_t readReg) {
                if (readReg == reg && PMU.bldo1) PMU.failSdDisable = false;
            };
        }
        if (fault == SdOffFault::Unreadable) PMU.registerReadOverride[reg] = -1;
        g_delayHook = [](uint32_t ms) { g_millis += ms; };
        const auto start = millis();
        unmountDegradedSd();
        g_delayHook = nullptr;
        assert(!gSdMounted && !g_sdDisk.mounted);
        assert(PMU.bldo1 == (fault == SdOffFault::PersistentWrite));
        assert(calls("disableBLDO1") == disables + attempts);
        assert(PMU.registerReadCalls[reg] == reads + attempts);
        assert(millis() - start == SD_POWER_OFF_SETTLE_MS + (attempts - 1) * PMU_PERIPHERAL_RETRY_MS);
        assert(logCount("microSD card power rail (BLDO1) off unconfirmed after ") ==
               (attempts == PMU_PERIPHERAL_ATTEMPTS ? 1 : 0));
        ++cases;
    }

    // With neither store mounted, the E2010 raise joins the storage faults in RAM.
    pmuWorld();
    SD.cardPresent = false;
    g_disk.failMount = true;
    PMU.registerReadOverride[XPOWERS_AXP2101_LDO_VOL3_CTRL] = -1;
    setup();
    assert(gDegraded.mode() == startup::Mode::NoStorage && !gSdMounted && !gFlashMounted);
    assert(gJournal.stats().backend == EventLog::Backend::Ram);
    auto noStorageRows = bootRows();
    for (int code : {2010, 4001, 4002, 4003}) {
        auto warning = codeRows(noStorageRows, code);
        assert(warning.size() == 1);
        has(warning[0], R"("transition":"raise")");
    }
    ++cases;

    for (float value : {NAN, INFINITY, -INFINITY, -40.01f, 125.01f, 385.7f})
        assert(!charge_temperature::plausible(value));
    for (float value : {-40.0f, 30.0f, 60.0f, 125.0f})
        assert(charge_temperature::plausible(value));
    // A real sketch read/command/read-back on each entry path. A failed first
    // conversion never becomes a hot/cold observation or a debug measurement.
    for (bool degraded : {false, true}) {
        pmuWorld(); PMU.dieC = 385.7f;
        if (degraded) enterDegraded(startup::Mode::NoStorage);
        else setup();
        assert(!PMU.chargeEnabled && gChargeState == ChargeState::Paused);
        assert(std::isnan(gDebugTempDie));
        auto rows = bootRows();
        for (int code : {2004, 2005, 2006}) assert(codeRows(rows, code).empty());
        for (const auto& line : Serial.log) assert(line.find("too hot") == std::string::npos);
        auto n = calls("getTemperature");
        g_millis += CHG_FIRST_RETRY_MS - 1; serviceChargeTemp();
        assert(calls("getTemperature") == n);
        PMU.dieC = 33; ++g_millis;
        if (degraded) degradedLoop(); else serviceChargeTemp();
        assert(PMU.chargeEnabled && gChargeState == ChargeState::Charging);
        // The retried reading is still this boot's first decision.
        assert(logCount("Charge guard: die 33.0 C") == 1);
        assert(!logCount("Charging resumed") && !logCount("Charging paused") && !logCount("no plausible"));
        rows = bootRows();
        for (int code : {2004, 2005, 2006}) assert(codeRows(rows, code).empty());
        ++cases;
    }
    pmuWorld(); PMU.dieC = 385.7f; serviceChargeTemp();
    for (uint32_t elapsed = CHG_FIRST_RETRY_MS; elapsed < CHG_FIRST_GRACE_MS; elapsed += CHG_FIRST_RETRY_MS) {
        g_millis += CHG_FIRST_RETRY_MS; serviceChargeTemp();
        assert(take().empty() && !PMU.chargeEnabled);
    }
    g_millis += CHG_FIRST_RETRY_MS; serviceChargeTemp();
    auto unreadable = one(2006, "raise");
    assert(unreadable.find("temperature_mc") == std::string::npos);
    // The pause was commanded silently during the retries; say so once now.
    assert(logCount("no plausible temperature in the first 10 s; charging stays off") == 1);
    assert(!logCount("Charging paused") && !logCount("Charge guard: die"));
    auto n = calls("getTemperature");
    g_millis += CHG_CHECK_MS - 1; serviceChargeTemp(); assert(calls("getTemperature") == n);
    PMU.dieC = 33; ++g_millis; serviceChargeTemp(); one(2006, "clear");
    assert(PMU.chargeEnabled && logCount("Charging resumed (die 33.0 C") == 1 && logCount("no plausible") == 1); ++cases;
    // A dead sensor can outlive millis() rollover. Exhausted startup grace
    // must never reopen even if elapsed time again resembles the first second.
    pmuWorld(); PMU.dieC = NAN; serviceChargeTemp();
    g_millis += CHG_FIRST_GRACE_MS; serviceChargeTemp(); one(2006, "raise");
    assert(gChargeFirstResolved);
    g_millis = gChargeFirstMs + 1; serviceChargeTemp(); // one full wrap later
    auto afterWrapReads = calls("getTemperature");
    g_millis += CHG_FIRST_RETRY_MS; serviceChargeTemp();
    assert(calls("getTemperature") == afterWrapReads && !PMU.chargeEnabled); ++cases;
    // Runtime garbage preserves an existing hot episode until a plausible
    // sample can clear it; no false temperature is attached to E2006.
    pmuWorld(); PMU.dieC = 60; serviceChargeTemp(); one(2004, "raise");
    assert(!PMU.chargeEnabled);
    PMU.dieC = 385.7f; chargeTick(); unreadable = one(2006, "raise");
    assert(unreadable.find("temperature_mc") == std::string::npos && std::isnan(gDebugTempDie));
    PMU.dieC = 33; chargeTick(); auto recovery = take();
    assert(codeRows(recovery, 2004).size() == 1 && codeRows(recovery, 2006).size() == 1);
    assert(PMU.chargeEnabled); ++cases;
    pmuWorld(); serviceChargeTemp(); assert(PMU.chargeEnabled && calls("enableCellbatteryCharge") == 1); ++cases;
    pmuWorld(); g_millis = UINT32_MAX - 100; PMU.dieC = NAN; serviceChargeTemp();
    g_millis += CHG_FIRST_GRACE_MS; serviceChargeTemp(); one(2006, "raise"); ++cases;
    // Every byte/mask, including the production-only short key, follows XPowersLib's
    // INTSTS byte order and configured enable mask. Unknown suppresses ALL bits.
    for (int byte = 0; byte <= 255; ++byte) {
        auto batch = pmu_read::irqBatch(byte, byte, byte, PMU_ENABLED_IRQS);
        assert(batch.known);
        for (unsigned bit = 0; bit < 24; ++bit)
            assert(batch.has(1u << bit) == (bool(unsigned(byte) & (1u << (bit % 8))) &&
                   bool(PMU_ENABLED_IRQS & (1u << bit))));
        for (auto unknown : {pmu_read::irqBatch(-1, byte, byte, 0xffffff),
                             pmu_read::irqBatch(byte, -1, byte, 0xffffff),
                             pmu_read::irqBatch(byte, byte, -1, 0xffffff)}) {
            assert(!unknown.known && !unknown.has(0xffffff));
            assert(!unknown.has(XPOWERS_AXP2101_PKEY_SHORT_IRQ));
        }
        assert(pmu_read::chargeState(byte) == ((byte & 2) ? ChargeState::Charging : ChargeState::Paused));
    }
    assert(pmu_read::chargeState(-1) == ChargeState::Unconfirmed); ++cases;

    for (uint8_t reg : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3}) {
        pmuWorld(); PMU.vbusIn = false;
        PMU.longPressIrq = PMU.shortPressIrq = PMU.lowIrq = PMU.criticalIrq = true;
        PMU.removeIrq = PMU.insertIrq = true;
        gPmuIrqFlag = true; failRead(reg); pollPmu();
        assert(take().empty() && !PMU.shutdownCalls && !gVbusOffPending && !gVbusLost);
        assert(calls("clearIrqStatus") == 0 && calls("getIrqStatus") == 0);
        assert(gPmuIrqRetry && PMU.longPressIrq && PMU.criticalIrq);
        auto reads = PMU.registerReadCalls;
        for (unsigned i = 0; i < 100; ++i) { gPmuIrqFlag = true; pollPmu(); }
        assert(PMU.registerReadCalls == reads);
        g_millis += PMU_IRQ_RETRY_MS - 1; pollPmu(); assert(PMU.registerReadCalls == reads);
        ++g_millis; failRead(reg); pollPmu();
        assert(take().empty() && !PMU.shutdownCalls && calls("clearIrqStatus") == 0);
        // No new falling edge: the retained batch still gets another attempt.
        PMU.longPressIrq = PMU.criticalIrq = false;
        gPmuIrqFlag = false; g_millis += PMU_IRQ_RETRY_MS; pollPmu();
        one(2002, "raise");
        assert(gVbusOffPending && !gPmuIrqRetry && calls("clearIrqStatus") == 0);
        assert(PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS1] == 1 &&
               PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS2] == 1 &&
               PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS3] == 0);
        pollPmu(); assert(take().empty() && PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS1] == 1); ++cases;

        // Real long key/critical IRQ survives each possible failed byte and
        // still reaches the battery-only shutdown path on a successful retry.
        for (bool critical : {false, true}) {
            pmuWorld(); PMU.vbusIn = false;
            PMU.longPressIrq = !critical; PMU.criticalIrq = critical;
            gPmuIrqFlag = true; failRead(reg); pollPmu();
            assert(!PMU.shutdownCalls && take().empty());
            g_millis += PMU_IRQ_RETRY_MS;
            bool off = false;
            try { pollPmu(); } catch (const PoweredOff&) { off = true; }
            assert(off && PMU.shutdownCalls == 1);
            if (critical) one(2003, "raise");
            ++cases;
        }
    }
    pmuWorld(); g_millis = UINT32_MAX - 49; PMU.shortPressIrq = true; gPmuIrqFlag = true;
    failRead(XPOWERS_AXP2101_INTSTS2); pollPmu();
    auto reads = PMU.registerReadCalls;
    g_millis += 99; pollPmu(); assert(PMU.registerReadCalls == reads);
    ++g_millis; pollPmu();
    assert(!gPmuIrqRetry && calls("clearIrqStatus") == 0 && PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS2] == 1); ++cases;

    // An edge arriving during a read must survive consumption of the old edge.
    pmuWorld(); PMU.shortPressIrq = true; gPmuIrqFlag = true;
    PMU.onRegisterRead = [](uint8_t reg) { if (reg == XPOWERS_AXP2101_INTSTS2) onPmuIrq(); };
    pollPmu(); assert(gPmuIrqFlag && PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS2] == 1);
    PMU.onRegisterRead = nullptr; PMU.shortPressIrq = true; pollPmu();
    assert(!gPmuIrqFlag && PMU.registerWriteCalls[XPOWERS_AXP2101_INTSTS2] == 2); ++cases;

    // No false raise or false clear while the threshold register is unreadable.
    for (bool active : {false, true}) {
        pmuWorld();
        if (active) { batteryCondition(diagnostics::Code::BATTERY_LOW, true, g_millis); batteryCondition(diagnostics::Code::BATTERY_CRITICAL, true, g_millis); take(); }
        PMU.batteryPercent = active ? 100 : 0;
        failRead(XPOWERS_AXP2101_LOW_BAT_WARN_SET); serviceBatteryDiagnostics();
        assert(gBatteryThresholds < 0 && take().empty());
        g_millis += 30000; serviceBatteryDiagnostics();
        auto rows = take();
        for (int code : {2002, 2003}) {
            auto r = codeRows(rows, code); assert(r.size() == 1);
            has(r[0], active ? "\"transition\":\"clear\"" : "\"transition\":\"raise\"");
        }
        auto n = PMU.registerReadCalls[XPOWERS_AXP2101_LOW_BAT_WARN_SET];
        failRead(XPOWERS_AXP2101_LOW_BAT_WARN_SET);
        g_millis += 30000; serviceBatteryDiagnostics();
        assert(PMU.registerReadCalls[XPOWERS_AXP2101_LOW_BAT_WARN_SET] == n && take().empty()); ++cases;
    }
    // Successful decoding over every nibble pair; test inclusive boundaries.
    for (int raw = 0; raw <= 255; ++raw)
        for (int code : {2002, 2003}) {
            pmuWorld(); PMU.lowWarnNibble = raw >> 4; PMU.criticalPercent = raw & 15;
            PMU.batteryPercent = code == 2002 ? (raw >> 4) + 5 : raw & 15;
            serviceBatteryDiagnostics(); one(code, "raise");
            PMU.batteryPercent++; g_millis += 30000; serviceBatteryDiagnostics(); one(code, "clear");
        }
    ++cases;

    // Disable write failure, read failure, stale enabled read-back, and failed
    // resume: none can confirm the intended state. Three bounded failures raise
    // E2008; a later matching read-back clears that same episode.
    for (unsigned failure = 0; failure < 4; ++failure) {
        pmuWorld(); bool resume = failure == 3;
        if (resume) { PMU.dieC = 54.0f; serviceChargeTemp(); one(2004, "raise"); }
        PMU.dieC = resume ? 30.0f : 54.0f;
        PMU.failChargeDisable = failure == 0;
        PMU.failChargeRead = failure == 1;
        if (failure == 2) PMU.registerReadOverride[XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL] = 2;
        PMU.failChargeEnable = resume;
        const char* command = resume ? "enableCellbatteryCharge" : "disableCellbatteryCharge";
        for (unsigned attempt = 1; attempt <= 3; ++attempt) {
            if (attempt == 1 && !resume) serviceChargeTemp(); else chargeTick();
            assert(gChargeState == ChargeState::Unconfirmed && gChargeFailures == attempt);
            auto rows = take();
            if (attempt < 3) assert(codeRows(rows, 2008).empty());
            else { auto r = codeRows(rows, 2008); assert(r.size() == 1); has(r[0], "\"transition\":\"raise\""); }
            unsigned n = calls(command);
            for (unsigned spin = 0; spin < 50; ++spin) serviceChargeTemp();
            assert(calls(command) == n && take().empty());
        }
        assert(calls(command) == 3);
        assert(std::none_of(Serial.log.begin(), Serial.log.end(), [resume](const auto& line) {
            return line.find(resume ? "Charging resumed" : "Charging paused") != std::string::npos;
        }));
        PMU.failChargeDisable = PMU.failChargeRead = PMU.failChargeEnable = false;
        PMU.registerReadOverride.clear(); chargeTick(); one(2008, "clear");
        assert(gChargeState == (resume ? ChargeState::Charging : ChargeState::Paused));
        assert(gChargeFailures == 0 && PMU.chargeEnabled == resume);
        auto n = calls(command); chargeTick(); assert(calls(command) == n && take().empty()); ++cases;
    }
    // Failed pause retains hysteresis; a cooler-but-not-yet-safe reading must
    // retry disable, never enable. Unreadable temperature does the same.
    pmuWorld(); PMU.dieC = 54.0f; PMU.failChargeDisable = true;
    serviceChargeTemp(); one(2004, "raise");
    PMU.dieC = 48.0f; chargeTick();
    assert(calls("disableCellbatteryCharge") == 2 && calls("enableCellbatteryCharge") == 0);
    PMU.dieC = INFINITY; chargeTick(); auto rows = take();
    assert(codeRows(rows, 2006).size() == 1 && codeRows(rows, 2008).size() == 1);
    assert(gChargeState == ChargeState::Unconfirmed && gChargePauseRequested); ++cases;
    // Retry timer remains bounded through tick rollover, including first-command failure.
    pmuWorld(); g_millis = UINT32_MAX - 100; PMU.failChargeRead = true; serviceChargeTemp();
    g_millis += CHG_CHECK_MS - 1; serviceChargeTemp(); assert(calls("enableCellbatteryCharge") == 1);
    ++g_millis; serviceChargeTemp(); assert(calls("enableCellbatteryCharge") == 2); ++cases;

    // --- Issue #790 regressions: checked bit-selective IRQ clear, pin-LOW retry, periodic VBUS ---
    for (int raw = 0; raw <= 255; ++raw)
        assert(pmu_read::chargeTargetMatches(raw, XPOWERS_AXP2101_CHG_VOL_4V1) == ((raw & 7) == 2));
    assert(!pmu_read::chargeTargetMatches(-1, XPOWERS_AXP2101_CHG_VOL_4V1));

    // A failed INTSTS clear write sets gPmuIrqRetry so recovery runs without a new falling edge.
    for (uint8_t reg : {XPOWERS_AXP2101_INTSTS1, XPOWERS_AXP2101_INTSTS2, XPOWERS_AXP2101_INTSTS3}) {
        pmuWorld();
        PMU.registerReadOverride[reg] = (reg == XPOWERS_AXP2101_INTSTS1) ? 0x40 :
                                        (reg == XPOWERS_AXP2101_INTSTS2) ? 0x08 : 0x01;
        PMU.failRegisterWriteAt[reg] = 1;
        gPmuIrqFlag = true; pollPmu();
        assert(calls("clearIrqStatus") == 0 && gPmuIrqRetry);
        assert(PMU.registerReadOverride[reg] != 0); // bit stayed latched after failed clear
        auto writes = PMU.registerWriteCalls[reg];
        g_millis += PMU_IRQ_RETRY_MS - 1; pollPmu();
        assert(PMU.registerWriteCalls[reg] == writes); // rate-limited retry
        ++g_millis; pollPmu();
        assert(!gPmuIrqRetry && PMU.registerReadOverride[reg] == 0);
        assert(PMU.registerWriteCalls[reg] == writes + 1); ++cases;
    }
    // setupPmuIrq() uses checked writes and sets gPmuIrqRetry if clearing fails.
    pmuWorld(); PMU.failRegisterWriteAt[XPOWERS_AXP2101_INTSTS2] = 1;
    setupPmuIrq();
    assert(calls("clearIrqStatus") == 0 && gPmuIrqRetry); ++cases;

    // Issue #790: failed IRQ clear must not replay the already-handled action on retry,
    // while still dispatching any newly latched bits on that same retry.
    {
        pmuWorld(); gDebugMode.store(false);
        g_digitalReadHook = [](int pin) { return pin == DEBUG_BOOT_PIN ? LOW : HIGH; };
        gBootButton = runtime_debug::BootButton();
        gBootButton.sample(g_millis, true);
        g_millis += runtime_debug::BootButton::kDebounceMs;
        gBootButton.sample(g_millis, true);
        assert(gBootButton.held(g_millis));
        PMU.shortPressIrq = true;
        PMU.failRegisterWriteAt[XPOWERS_AXP2101_INTSTS2] = 1;
        gPmuIrqFlag = true; pollPmu();
        assert(debugMode() && gPmuIrqRetry && PMU.shortPressIrq);
        // Retry clears the stuck PKEY_SHORT bit without toggling debug back off,
        // while dispatching a newly latched VBUS_REMOVE bit in the same register.
        PMU.vbusIn = false; PMU.removeIrq = true;
        g_millis += PMU_IRQ_RETRY_MS; pollPmu();
        assert(debugMode() && !gPmuIrqRetry && !PMU.shortPressIrq && !PMU.removeIrq);
        assert(gVbusOffPending);
        // A genuine second tap after clear succeeded toggles debug back off.
        PMU.shortPressIrq = true; gPmuIrqFlag = true; pollPmu();
        assert(!debugMode() && !PMU.shortPressIrq);
        g_digitalReadHook = nullptr; ++cases;
    }
    // Issue #790: failed INTSTS2 clear on a long-press does not re-run shutdown prep on retry.
    {
        pmuWorld(); PMU.vbusIn = true; PMU.longPressIrq = true;
        PMU.failRegisterWriteAt[XPOWERS_AXP2101_INTSTS2] = 1;
        gPmuIrqFlag = true; pollPmu();
        assert(gDisplayUsbPowerRequest && gPmuIrqRetry && PMU.longPressIrq);
        gDisplayUsbPowerRequest = false;
        g_millis += PMU_IRQ_RETRY_MS; pollPmu();
        assert(!gDisplayUsbPowerRequest && !gPmuIrqRetry && !PMU.longPressIrq); ++cases;
    }
    // Issue #790: failed enableIRQ() in setupPmuIrq() is retried rate-limited until it succeeds.
    {
        pmuWorld(); PMU.failEnableIrq = true; PMU.enabledIrqs = 0;
        setupPmuIrq();
        assert(PMU.enabledIrqs == 0 && calls("enableIRQ") == 1);
        g_millis += PMU_IRQ_RETRY_MS - 1; pollPmu();
        assert(calls("enableIRQ") == 1); // rate-limited
        ++g_millis; pollPmu();
        assert(calls("enableIRQ") == 2 && PMU.enabledIrqs == 0); // retried while fault still active
        g_millis += PMU_IRQ_RETRY_MS - 1; pollPmu();
        assert(calls("enableIRQ") == 2);
        PMU.failEnableIrq = false;
        ++g_millis; pollPmu();
        assert(calls("enableIRQ") == 3 && PMU.enabledIrqs == PMU_ENABLED_IRQS);
        g_millis += PMU_IRQ_RETRY_MS; pollPmu();
        assert(calls("enableIRQ") == 3); // stops retrying once enabled
        ++cases;
    }

    // Clear only the read bits BEFORE powerOffForVbus() so flags latching during
    // a cancelled shutdown survive instead of being wiped by an all-ones clear.
    pmuWorld(); PMU.vbusIn = false; PMU.longPressIrq = true;
    g_sdDisk.onUnmount = [] {
        // USB returns (cancelling shutdown) while critical & remove flags latch mid-prep.
        PMU.vbusIn = true;
        PMU.criticalIrq = true;
        PMU.removeIrq = true;
    };
    gPmuIrqFlag = true; pollPmu();
    g_sdDisk.onUnmount = nullptr;
    assert(calls("clearIrqStatus") == 0 && !gPmuIrqRetry);
    assert(!PMU.longPressIrq); // original batch bit cleared before shutdown prep
    assert(PMU.criticalIrq && PMU.removeIrq); // late-latched flags survived cancelled shutdown
    assert(PMU.registerWrites.size() == 1 &&
           PMU.registerWrites[0].first == XPOWERS_AXP2101_INTSTS2 &&
           PMU.registerWrites[0].second == 0x04); ++cases;

    // Steady active-low PMU_IRQ_PIN triggers rate-limited poll without a new falling edge.
    pmuWorld(); PMU.vbusIn = false; PMU.removeIrq = true;
    gPmuIrqFlag = false; gPmuIrqRetry = false; gPmuIrqReadMs = g_millis;
    g_digitalReadHook = [](int pin) { return pin == PMU_IRQ_PIN ? LOW : HIGH; };
    g_millis += PMU_IRQ_RETRY_MS - 1; pollPmu();
    assert(!gVbusOffPending && PMU.removeIrq);
    ++g_millis; pollPmu();
    assert(gVbusOffPending && !PMU.removeIrq && !gPmuIrqRetry);
    auto pinReads = PMU.registerReadCalls;
    for (unsigned i = 0; i < 10; ++i) pollPmu();
    assert(PMU.registerReadCalls == pinReads); // stuck-low pin stays rate-limited
    g_digitalReadHook = nullptr; ++cases;

    // Periodic VBUS poll catches a missed VBUS edge (both loss and return).
    pmuWorld(); PMU.vbusIn = true; seedVbusState();
    assert(!gVbusLost && !gVbusOffPending);
    PMU.vbusIn = false; // no IRQ edge or pin low
    g_millis += VBUS_POLL_MS - 1; serviceVbusRecheck();
    assert(!gVbusOffPending && !gVbusLost);
    ++g_millis; serviceVbusRecheck();
    assert(gVbusOffPending && !gVbusLost);
    g_millis += VBUS_OFF_CONFIRM_MS; confirmVbusOff();
    assert(gVbusLost && !gVbusOffPending);
    PMU.vbusIn = true; // return without IRQ
    g_millis += VBUS_POLL_MS; serviceVbusRecheck();
    assert(!gVbusLost && !gVbusOffPending); ++cases;

    // --- Issue #792 regressions: early charger disable retry & verified 4.1 V target ---
    // Early disable retries on read-back failure and confirms charging off before storage mount.
    {
        pmuWorld(); PMU.chargeEnabled = true; PMU.failChargeDisable = true;
        static bool sawStorageMount; sawStorageMount = false;
        PMU.onRegisterRead = [](uint8_t reg) {
            if (reg == XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL &&
                PMU.registerReadCalls[reg] >= 2) PMU.failChargeDisable = false;
        };
        g_sdDisk.onMount = [] {
            sawStorageMount = true;
            assert(!PMU.chargeEnabled);
            assert(calls("disableCellbatteryCharge") == 3);
        };
        setup();
        PMU.onRegisterRead = nullptr; g_sdDisk.onMount = nullptr;
        assert(sawStorageMount && !logCount("early charger disable unconfirmed"));
        ++cases;
    }
    // Transient disable failure that recovers during the bounded 5 s retry window
    // confirms charging off before storage mount without raising E2008.
    {
        pmuWorld(); PMU.chargeEnabled = true; PMU.failChargeDisable = true;
        static uint32_t startMs; startMs = g_millis;
        PMU.tsControl = 0x05;
        PMU.failRegisterWriteAt[tsReg] = 1;
        static bool mountedAfterRecovery; mountedAfterRecovery = false;
        PMU.onRegisterRead = [](uint8_t reg) {
            if (reg == XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL &&
                uint32_t(g_millis - startMs) >= 500) PMU.failChargeDisable = false;
        };
        g_sdDisk.onMount = [] {
            mountedAfterRecovery = true;
            assert(!PMU.chargeEnabled && PMU.tsControl == 0x10);
            assert(PMU.registerWriteCalls[XPOWERS_AXP2101_TS_PIN_CTRL] == 2);
            assert(uint32_t(g_millis - startMs) >= 500 && uint32_t(g_millis - startMs) < 5000);
        };
        setup();
        PMU.onRegisterRead = nullptr; g_sdDisk.onMount = nullptr;
        assert(mountedAfterRecovery && PMU.chargeEnabled); // guard enabled charging after mount
        FS_LOCK(); auto ev = take(); FS_UNLOCK();
        assert(codeRows(ev, 2008).empty());
        ++cases;
    }
    // Persistent early disable failure retries for a bounded ~5 s window feeding
    // the startup watchdog before storage mount, keeps fault active through mount,
    // raises E2008, continues startup, and clears E2008 only when disable recovers.
    {
        pmuWorld(); PMU.chargeEnabled = true; PMU.failChargeDisable = true;
        static uint32_t startMs; startMs = g_millis;
        PMU.tsControl = 0x05;
        PMU.failRegisterWriteAt[tsReg] = 1;
        static bool checkedAtMount; checkedAtMount = false;
        g_sdDisk.onMount = [] {
            checkedAtMount = true;
            assert(PMU.failChargeDisable); // fault stays active through mount
            assert(PMU.tsControl == 0x05);
            assert(PMU.registerWriteCalls[XPOWERS_AXP2101_TS_PIN_CTRL] == 1 &&
                   PMU.registerWriteCalls[XPOWERS_AXP2101_ADC_CHANNEL_CTRL] == 1);
            assert(uint32_t(g_millis - startMs) >= 5000);
            assert(calls("disableCellbatteryCharge") >= 50);
            assert(g_watchdog.feeds >= 10);
        };
        setup();
        g_sdDisk.onMount = nullptr;
        assert(checkedAtMount && gDegraded.mode() == startup::Mode::Normal);
        assert(gChargeState == ChargeState::Unconfirmed);
        assert(logCount("early charger disable unconfirmed") == 1 &&
               logCount("charger disable still unconfirmed") == 1);
        FS_LOCK(); auto ev = take(); FS_UNLOCK();
        assert(logCount("TS fixed-input setup skipped because charger pause is unconfirmed") == 1);
        auto tsWarnings = codeRows(ev, 2010);
        assert(tsWarnings.size() == 1); has(tsWarnings[0], R"("transition":"raise")");
        auto ctrl = codeRows(ev, 2008);
        assert(ctrl.size() == 1); has(ctrl[0], "\"transition\":\"raise\"");
        // Recovering charger disable clears E2008 on the next guard tick.
        PMU.failChargeDisable = false;
        chargeTick();
        assert(gChargeState == ChargeState::Charging && PMU.chargeEnabled);
        FS_LOCK(); auto rec = take(); FS_UNLOCK();
        auto cleared = codeRows(rec, 2008);
        assert(cleared.size() == 1); has(cleared[0], "\"transition\":\"clear\"");
        ++cases;
    }
    // Issue #792: if early charger disable times out for the full 5 s window
    // and then recovers during storage mount, the first guard pass after the
    // journal is ready must still raise E2008 once (independent of gChargeFailures),
    // followed by a single clear on the next confirmed guard pass.
    {
        pmuWorld(); PMU.chargeEnabled = true; PMU.failChargeDisable = true;
        static uint32_t startMs; startMs = g_millis;
        static bool recoveredAtMount; recoveredAtMount = false;
        g_sdDisk.onMount = [] {
            recoveredAtMount = true;
            assert(PMU.failChargeDisable);
            assert(uint32_t(g_millis - startMs) >= 5000);
            PMU.failChargeDisable = false; // recovers during storage setup
        };
        setup();
        g_sdDisk.onMount = nullptr;
        assert(recoveredAtMount && gDegraded.mode() == startup::Mode::Normal);
        assert(gEarlyChargeDisabled && !gEarlyChargeTimeoutPending);
        assert(gChargeState == ChargeState::Charging && PMU.chargeEnabled && gChargeFailures == 0);
        FS_LOCK(); auto bootEv = take(); FS_UNLOCK();
        auto raised = codeRows(bootEv, 2008);
        assert(raised.size() == 1); has(raised[0], "\"transition\":\"raise\"");
        // Next confirmed guard pass clears that exact E2008 episode once.
        chargeTick();
        FS_LOCK(); auto rec = take(); FS_UNLOCK();
        auto cleared = codeRows(rec, 2008);
        assert(cleared.size() == 1); has(cleared[0], "\"transition\":\"clear\"");
        assert(episode(raised[0]) == episode(cleared[0]));
        // Subsequent confirmed passes stay silent.
        chargeTick();
        FS_LOCK(); auto quiet = take(); FS_UNLOCK();
        assert(codeRows(quiet, 2008).empty());
        ++cases;
    }
    // Unverified 4.1 V charge target (write error, stuck at 4.2 V, or read error)
    // blocks charging even at safe temperature and raises E2008; later recovery clears E2008.
    for (unsigned mode = 0; mode < 3; ++mode) {
        pmuWorld(); PMU.dieC = 30.0f; PMU.chgVol = 3; // 4.2 V default
        PMU.failChargeTargetWrite = (mode == 0);
        PMU.chargeTargetStuck = (mode == 1);
        PMU.failChargeTargetRead = (mode == 2);
        setup();
        assert(!gChargeTargetVerified && !PMU.chargeEnabled);
        assert(calls("enableCellbatteryCharge") == 0);
        assert(logCount("NOT 4.1 V") == 1 && logCount("4.1 V charge target unconfirmed") >= 1);
        FS_LOCK(); auto bootEv = take(); FS_UNLOCK();
        auto ctrl = codeRows(bootEv, 2008);
        assert(ctrl.size() == 1); has(ctrl[0], "\"transition\":\"raise\"");
        chargeTick();
        assert(!gChargeTargetVerified && !PMU.chargeEnabled && calls("enableCellbatteryCharge") == 0);
        // Recover 4.1 V setting on next tick: charging enables and E2008 clears.
        PMU.failChargeTargetWrite = PMU.chargeTargetStuck = PMU.failChargeTargetRead = false;
        chargeTick();
        assert(gChargeTargetVerified && PMU.chgVol == XPOWERS_AXP2101_CHG_VOL_4V1);
        assert(PMU.chargeEnabled && calls("enableCellbatteryCharge") == 1);
        FS_LOCK(); auto rec = take(); FS_UNLOCK();
        auto cleared = codeRows(rec, 2008);
        assert(cleared.size() == 1); has(cleared[0], "\"transition\":\"clear\"");
        ++cases;
    }
    // Runtime target drift away from 4.1 V pauses charging and raises E2008 after 3 failures.
    pmuWorld(); PMU.dieC = 30.0f; setup();
    FS_LOCK(); take(); FS_UNLOCK();
    assert(gChargeTargetVerified && PMU.chargeEnabled);
    PMU.chgVol = 3; PMU.chargeTargetStuck = true;
    for (unsigned attempt = 1; attempt <= 3; ++attempt) {
        chargeTick();
        assert(!gChargeTargetVerified && !PMU.chargeEnabled);
        FS_LOCK(); auto ev = take(); FS_UNLOCK();
        if (attempt < 3) assert(codeRows(ev, 2008).empty());
        else { auto r = codeRows(ev, 2008); assert(r.size() == 1); has(r[0], "\"transition\":\"raise\""); }
    }
    PMU.chargeTargetStuck = false; chargeTick();
    assert(gChargeTargetVerified && PMU.chargeEnabled);
    FS_LOCK(); auto driftClear = take(); FS_UNLOCK();
    assert(codeRows(driftClear, 2008).size() == 1); has(codeRows(driftClear, 2008)[0], "\"transition\":\"clear\""); ++cases;

    // --- Issue #804 regressions: unreadable samples never create hot/cold history ---
    // (1) After first-reading grace expires, a valid reading in hot or cold deadband
    //     must resume charging without raising E2004/E2005.
    for (float deadband : {50.0f, 11.0f}) {
        pmuWorld(); PMU.dieC = 385.7f; serviceChargeTemp();
        g_millis += CHG_FIRST_GRACE_MS; serviceChargeTemp();
        one(2006, "raise");
        assert(!PMU.chargeEnabled && !gChargePauseRequested);
        PMU.dieC = deadband; chargeTick();
        auto rows = take();
        assert(codeRows(rows, 2006).size() == 1); has(codeRows(rows, 2006)[0], "\"transition\":\"clear\"");
        assert(codeRows(rows, 2004).empty() && codeRows(rows, 2005).empty());
        assert(PMU.chargeEnabled && !gChargePauseRequested); ++cases;
    }
    // (2) Transient runtime unreadable reading pauses charging fail-safe, then
    //     a deadband sample resumes without false E2004/E2005.
    for (float deadband : {50.0f, 11.0f}) {
        pmuWorld(); PMU.dieC = 30.0f; serviceChargeTemp();
        assert(PMU.chargeEnabled && !gChargePauseRequested && take().empty());
        PMU.dieC = NAN; chargeTick(); one(2006, "raise");
        assert(!PMU.chargeEnabled && !gChargePauseRequested);
        PMU.dieC = deadband; chargeTick();
        auto rows = take();
        assert(codeRows(rows, 2006).size() == 1); has(codeRows(rows, 2006)[0], "\"transition\":\"clear\"");
        assert(codeRows(rows, 2004).empty() && codeRows(rows, 2005).empty());
        assert(PMU.chargeEnabled && !gChargePauseRequested); ++cases;
    }
    // (3) Existing cold pause history is still preserved across a transient unreadable reading.
    pmuWorld(); PMU.dieC = 9.0f; serviceChargeTemp(); one(2005, "raise");
    PMU.dieC = NAN; chargeTick(); one(2006, "raise");
    assert(!PMU.chargeEnabled && gChargePauseRequested);
    PMU.dieC = 11.0f; chargeTick(); // still below the 14 C die resume threshold
    auto coldRows = take();
    assert(codeRows(coldRows, 2006).size() == 1 && codeRows(coldRows, 2005).empty());
    assert(!PMU.chargeEnabled && gChargePauseRequested); ++cases;

    std::printf("PMU verification: %u scenarios plus exhaustive IRQ/charge/threshold bytes passed (runtime debug=%d).\n", cases, debugMode());
}
