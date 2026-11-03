#pragma once
#include <Preferences.h>
#include <nvs.h>
#include "event_log.h"

namespace diagnostics {
// Core-0 consumer only, under FS_LOCK. No NVS writes on scans/events or on
// ordinary mirrored allocations. This is a recovery witness, not a revision.
class NvsRevisionState : public StatusRevisionState {
public:
    Read read(uint64_t& state) override {
        // Preferences::isKey() conflates missing keys with lookup errors.
        // Only an actual NVS NOT_FOUND permits recovery after erase-all.
        nvs_handle_t handle;
        esp_err_t result = nvs_open("diag", NVS_READONLY, &handle);
        if (result == ESP_ERR_NVS_NOT_FOUND) return Read::Missing;
        if (result != ESP_OK) return Read::Error;
        result = nvs_get_u64(handle, "revstate", &state);
        nvs_close(handle);
        if (result == ESP_ERR_NVS_NOT_FOUND) return Read::Missing;
        return result == ESP_OK && state <= kMaxEventInteger ? Read::Ready : Read::Error;
    }
    bool write(uint64_t state) override {
        Preferences settings;
        if (!settings.begin("diag", false)) return false;
        bool ok = settings.putULong64("revstate", state) == sizeof(state) &&
                  settings.getULong64("revstate", UINT64_MAX) == state;
        settings.end(); return ok;
    }
};
} // namespace diagnostics
