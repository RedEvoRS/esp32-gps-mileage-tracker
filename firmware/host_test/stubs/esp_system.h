#pragma once
#include "arduino_stub.h"
enum esp_reset_reason_t { ESP_RST_UNKNOWN, ESP_RST_POWERON, ESP_RST_EXT, ESP_RST_SW,
    ESP_RST_PANIC, ESP_RST_INT_WDT, ESP_RST_TASK_WDT, ESP_RST_WDT, ESP_RST_DEEPSLEEP,
    ESP_RST_BROWNOUT, ESP_RST_SDIO };
inline esp_reset_reason_t g_resetReason = ESP_RST_POWERON;
inline esp_reset_reason_t esp_reset_reason() { return g_resetReason; }
inline void esp_fill_random(void* out, size_t bytes) {
    static uint8_t seed = 0;
    auto* p = static_cast<uint8_t*>(out);
    for (size_t i = 0; i < bytes; ++i) p[i] = ++seed;
}
