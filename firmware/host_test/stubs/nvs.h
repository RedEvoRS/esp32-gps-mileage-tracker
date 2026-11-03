#pragma once
#include "arduino_stub.h"
#include "esp_partition.h"
// ESP-IDF's typed lookup distinguishes an absent key from an I/O failure.
using nvs_handle_t = unsigned;
constexpr int ESP_ERR_NVS_NOT_FOUND = 1, ESP_FAIL = 2;
constexpr int NVS_READONLY = 0;
inline esp_err_t nvs_open(const char*, int, nvs_handle_t* handle) {
    if (Preferences::failBegin) return ESP_FAIL;
    *handle = 1; return ESP_OK;
}
inline esp_err_t nvs_get_u64(nvs_handle_t, const char* key, uint64_t* value) {
    if (Preferences::failRead64) return ESP_FAIL;
    auto found = Preferences::durable64.find(key);
    if (found == Preferences::durable64.end()) return ESP_ERR_NVS_NOT_FOUND;
    *value = found->second; return ESP_OK;
}
inline void nvs_close(nvs_handle_t) {}
