#pragma once
#include <Preferences.h>
#include "event_log.h"

namespace diagnostics {
// Core-0 consumer only, under FS_LOCK. No NVS writes on scans/events or on
// ordinary mirrored allocations. This is a recovery witness, not a revision.
class NvsRevisionState : public StatusRevisionState {
public:
    bool read(uint64_t& state) override {
        Preferences settings;
        if (!settings.begin("diag", false)) return false;
        state = settings.isKey("revstate") ? settings.getULong64("revstate", UINT64_MAX) : 0;
        settings.end();
        return state <= kMaxEventInteger;
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
