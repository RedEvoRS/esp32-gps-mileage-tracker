// Model the installed core's enrollment and automatic pre-loop feed separately
// from sketch progress feeds. Expiry/reset itself still needs a real board.
#pragma once
#include "arduino_stub.h"
#include "esp_err.h"
#define CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0 1
#define CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1 0
struct esp_task_wdt_config_t {
    uint32_t timeout_ms, idle_core_mask;
    bool trigger_panic;
};
struct WatchdogStub {
    esp_task_wdt_config_t config{5000, 1, true};
    bool initialized = true, failConfigure = false, failAdd = false;
    unsigned reconfigures = 0, initializes = 0, enrollments = 0, feeds = 0;
    std::function<void()> onFeed;
};
inline WatchdogStub g_watchdog;
inline bool loopTaskWDTEnabled = false;
inline esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t* config) {
    ++g_watchdog.reconfigures;
    if (!g_watchdog.initialized) return ESP_ERR_INVALID_STATE;
    if (g_watchdog.failConfigure) return ESP_FAIL;
    g_watchdog.config = *config; return ESP_OK;
}
inline esp_err_t esp_task_wdt_init(const esp_task_wdt_config_t* config) {
    ++g_watchdog.initializes;
    if (g_watchdog.failConfigure) return ESP_FAIL;
    g_watchdog.initialized = true; g_watchdog.config = *config; return ESP_OK;
}
inline void enableLoopWDT() {
    assert(g_currentCore == 1);
    ++g_watchdog.enrollments;
    loopTaskWDTEnabled = !g_watchdog.failAdd;
}
inline esp_err_t esp_task_wdt_status(void*) {
    return !g_watchdog.initialized ? ESP_ERR_INVALID_STATE :
           g_currentCore == 1 && loopTaskWDTEnabled ? ESP_OK : ESP_ERR_NOT_FOUND;
}
inline void feedLoopWDT() {
    assert(g_currentCore == 1 && loopTaskWDTEnabled);
    ++g_watchdog.feeds;
    if (g_watchdog.onFeed) g_watchdog.onFeed();
}
