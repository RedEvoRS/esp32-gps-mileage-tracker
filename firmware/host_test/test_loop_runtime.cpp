// The real loop, parser and peripherals with time advancing through its pause.
#define FAULT_HOOKS_HELPERS_ONLY
#include "test_fault_hooks.cpp"

static unsigned pauses = 0;
static void idleWorld() {
    faultWorld();
    gDegraded = startup::DegradedPolicy();
    gRtc = diagnostics::RtcRuntime();
    gDebugMode = false;
    g_millis = 1000;
    pauses = 0;
    g_taskDelayHook = [](uint32_t ms) {
        assert(ms == LOOP_IDLE_MS && GPSSerial.available() == 0);
        ++pauses;
    };
}

int main() {
    idleWorld();
    uint32_t start = millis(); loop();
    assert(pauses == 1 && uint32_t(millis() - start) == LOOP_IDLE_MS);
    // A whole burst is drained before sleeping, including its final NAV-PVT.
    GPSSerial.rx = nmea("GNTXT,01,01,02,ready") + navPvt(0, 100);
    start = millis(); loop();
    assert(GPSSerial.available() == 0 && gNavPvt.seen && gNavPvt.receivedMs == start && pauses == 2);
    // Bytes arriving during later services suppress the pause on that pass.
    idleWorld();
    gPmuIrqFlag = true;
    PMU.onRegisterRead = [](uint8_t reg) {
        if (reg == XPOWERS_AXP2101_INTSTS1) GPSSerial.rx += navPvt(1000, 100);
    };
    start = millis(); loop();
    assert(GPSSerial.available() > 0 && pauses == 0 && millis() == start);
    PMU.onRegisterRead = nullptr; loop();
    assert(GPSSerial.available() == 0 && gNavPvt.seen && pauses == 1);

    // An IRQ arriving during sleep remains latched until the next pass.
    idleWorld();
    g_taskDelayHook = [](uint32_t ms) {
        assert(ms == LOOP_IDLE_MS);
        PMU.vbusIn = false; PMU.removeIrq = true; onPmuIrq();
    };
    loop(); assert(gPmuIrqFlag && !gVbusOffPending);
    g_taskDelayHook = nullptr; loop();
    assert(!gPmuIrqFlag && gVbusOffPending);

    // The 40 ms BOOT debounce is time based, with eight 5 ms idle passes.
    idleWorld();
    g_digitalReadHook = [](int pin) { return pin == DEBUG_BOOT_PIN ? LOW : HIGH; };
    for (unsigned i = 0; i < 7; ++i) loop();
    assert(!gBootButton.held(millis()));
    loop(); assert(gBootButton.held(millis()));
    PMU.shortPressIrq = true; gPmuIrqFlag = true; loop();
    assert(debugMode());
    g_digitalReadHook = nullptr;

    // An ACK read just before its deadline wins; no idle pause delays reading.
    idleWorld(); configureGps();
    GPSSerial.rx = nmea("GNTXT,01,01,02,ready"); loop();
    assert(gGpsConfig.state == GPS_WAIT_ACK);
    g_millis = gGpsConfig.since + GPS_ACK_MS - LOOP_IDLE_MS;
    GPSSerial.rx += frame(0x05, 0x01, {0x06, 0x8a}); loop();
    assert(gGpsConfig.state == GPS_CONFIG_DONE);
    idleWorld(); configureGps();
    GPSSerial.rx = nmea("GNTXT,01,01,02,ready"); loop();
    uint32_t deadline = gGpsConfig.since + GPS_ACK_MS;
    while (int32_t(millis() - deadline) < 0) loop();
    assert(gGpsConfig.sends == 1 && millis() == deadline);
    loop(); assert(gGpsConfig.sends == 2); // unchanged two-second timeout

    // The 1500 ms NAV-PVT boundary is unchanged with the real idle cadence.
    idleWorld(); GPSSerial.rx = navPvt(0, 100); loop();
    while (uint32_t(millis() - gNavPvt.receivedMs) < NAV_PVT_FRESH_MS) loop();
    assert(gpsSpeedTrusted(true));
    loop(); assert(!gpsSpeedTrusted(true));

    // Debug display still draws once per second, rather than once per pass.
    idleWorld(); beginProductionDisplay(); gDebugMode = true; serviceDisplay();
    start = gDebugDrawMs;
    while (uint32_t(millis() - start) < 1000) loop();
    assert(gDebugDrawMs == start);
    loop(); assert(gDebugDrawMs == start + 1000);
    assert(pauses == 201);
    g_taskDelayHook = nullptr;
    // Observe drain cadence even on an empty UART, with debug off. Enabling
    // debug reports only new stalls; suppressed reports still count.
    idleWorld(); g_taskDelayHook = nullptr; Serial.setTxTimeoutMs(0);
    loop();
    g_millis += 1501; loop();
    assert(gGpsDrainMonitor.count() == 1 && logCount("GPS UART drain gap:") == 0);
    gDebugMode = true;
    g_millis += 1600; GPSSerial.rx += navPvt(0, 100); loop();
    assert(gGpsDrainMonitor.count() == 2 && logCount("GPS UART drain gap:") == 1);
    assert(GPSSerial.available() == 0 && gNavPvt.seen);
    g_millis += 1600; loop();
    assert(gGpsDrainMonitor.count() == 3 && logCount("GPS UART drain gap:") == 1);
    g_millis += 60000; loop();
    assert(gGpsDrainMonitor.count() == 4 && logCount("GPS UART drain gap:") == 2);
    assert(gGpsDrainMonitor.maximumMs() == 60000 + LOOP_IDLE_MS);
    std::puts("Loop runtime: UART drain/recheck, 5 ms idle, IRQ, debounce, ACK, NAV-PVT and redraw passed");
    std::puts("Loop stalls: quiet counting, debug reporting, rate limit and continued UART drain passed");
}
