#pragma once
#include "arduino_stub.h"
enum sntp_sync_mode_t { SNTP_SYNC_MODE_IMMED };
enum sntp_sync_status_t { SNTP_SYNC_STATUS_RESET, SNTP_SYNC_STATUS_COMPLETED };
inline sntp_sync_status_t g_sntpStatus = SNTP_SYNC_STATUS_RESET;
inline int g_sntpCompleteAfterPolls = -1, g_sntpStopCalls = 0, g_sntpPolls = 0;
inline uint64_t g_sntpEpochMs = 0;
inline void (*g_sntpPollHook)() = nullptr;
inline void esp_sntp_stop() { ++g_sntpStopCalls; }
inline void esp_sntp_set_sync_mode(sntp_sync_mode_t) {}
inline void esp_sntp_set_sync_status(sntp_sync_status_t status) { g_sntpStatus = status; }
inline sntp_sync_status_t esp_sntp_get_sync_status() {
    ++g_sntpPolls;
    if (g_sntpPollHook) g_sntpPollHook();
    if (g_sntpCompleteAfterPolls > 0 && --g_sntpCompleteAfterPolls == 0) {
        timeval tv = { (long long)(g_sntpEpochMs / 1000), (long)(g_sntpEpochMs % 1000) * 1000 };
        settimeofday(&tv, nullptr);
        g_sntpStatus = SNTP_SYNC_STATUS_COMPLETED;
    }
    auto result = g_sntpStatus;
    g_sntpStatus = SNTP_SYNC_STATUS_RESET;
    return result;
}
