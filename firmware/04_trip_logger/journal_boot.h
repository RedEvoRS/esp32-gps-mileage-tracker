#pragma once
#include <Preferences.h>
#include <esp_system.h>
#include <cstring>
#include "event_log.h"
#include "pmu_startup_event.h"

namespace diagnostics {
struct JournalBoot {
    char id[33] = {};
    uint64_t counter = 0;
    bool counterPersisted = false;
};
inline JournalBoot newJournalBoot() {
    JournalBoot boot;
    uint8_t random[16]; esp_fill_random(random, sizeof(random));
    const char* hex = "0123456789abcdef";
    for (size_t i = 0; i < sizeof(random); ++i) {
        boot.id[2*i] = hex[random[i] >> 4]; boot.id[2*i+1] = hex[random[i] & 15];
    }
    Preferences counter;
    if (counter.begin("diag", false)) {
        uint64_t previous = counter.isKey("boots") ? counter.getULong64("boots", UINT64_MAX) : 0;
        if (previous < kMaxEventInteger) {
            boot.counter = previous + 1;
            boot.counterPersisted = counter.putULong64("boots", boot.counter) == sizeof(uint64_t) &&
                                    counter.getULong64("boots", UINT64_MAX) == boot.counter;
        }
        counter.end();
    }
    // A failed NVS write is never represented as a durable increment. Identity
    // remains fresh; zero is explicitly non-authoritative (no snapshot ordering).
    if (!boot.counterPersisted) boot.counter = 0;
    return boot;
}
inline const char* journalResetReason(esp_reset_reason_t reason) {
    switch (reason) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_SW: return "software";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_PANIC: return "panic";
    default: return "other";
    }
}
inline void submitJournalBoot(EventLog& log, esp_reset_reason_t reason,
                              const PmuStartupResult& pmu, uint64_t uptime) {
    EventInput event; event.uptimeMs = uptime;
    log.stamp(event);
    event.code = static_cast<uint16_t>(Code::BOOT);
    event.contextCount = 1;
    const char* reset = journalResetReason(reason);
    event.context[0] = EventContext::enumeration("reset_reason", reset);
    log.submit(event);
    // All boots retain E1000's reason. Error reset notices also carry their
    // existing severity-specific history code, with no active episode.
    event = EventInput(); event.uptimeMs = uptime;
    log.stamp(event);
    if (!std::strcmp(reset, "watchdog")) event.code = static_cast<uint16_t>(Code::WATCHDOG_RESET);
    else if (reason == ESP_RST_BROWNOUT) event.code = static_cast<uint16_t>(Code::BROWNOUT_RESET);
    else if (reason == ESP_RST_PANIC) event.code = static_cast<uint16_t>(Code::PANIC_RESET);
    else event.code = 0;
    if (event.code) log.submit(event);
    PmuStartupEvent startup;
    if (!makePmuStartupEvent(pmu, startup)) return;
    event = EventInput(); event.code = static_cast<uint16_t>(startup.code); event.uptimeMs = uptime;
    log.stamp(event);
    event.contextCount = 5;
    event.context[0] = EventContext::integer("clock_count", startup.clockCount);
    event.context[1] = EventContext::integer("attempt_count", startup.attemptCount);
    event.context[2] = EventContext::enumeration("recovery_state", startup.recoveryState);
    event.context[3] = EventContext::boolean("pmu_ready", startup.pmuReady);
    event.context[4] = EventContext::boolean("wire_end_failed", startup.wireEndFailed);
    log.submit(event);
}
} // namespace diagnostics
