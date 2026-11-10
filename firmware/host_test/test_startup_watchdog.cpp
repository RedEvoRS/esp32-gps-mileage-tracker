#include "../04_trip_logger/startup_watchdog.h"
#include <cassert>
#include <cstdio>
using namespace startup_watchdog;
int main() {
    Policy p;
    for (uint32_t s = 1; s < uint32_t(Stage::Count); ++s) {
        p.boot({}, true, false);
        assert(valid(p.record()) && p.hangs() == 0 && !p.skipCard());
        for (uint32_t n = 1; n <= STARTUP_HANG_LIMIT; ++n) {
            p.stage(Stage(s)); p.boot(p.record(), false, true);
            assert(p.hungStage() == Stage(s) && p.hangs() == n);
            if (n < STARTUP_HANG_LIMIT) assert(!p.skipCard());
        }
        assert(p.fallback() == (cardOnly(Stage(s)) ? Fallback::SkipCard : Fallback::NoStorage));
        auto saved = p.record();
        p.complete(); p.boot(p.record(), false, true);
        assert(p.hungStage() == Stage::None && p.hangs() == 0 && p.skipCard()); // loop WDT
        p.complete(); p.boot(p.record(), false, false);
        assert(p.hangs() == 0 && p.skipCard()); // software/brownout/etc retain latch
        p.boot(saved, true, false); assert(!p.skipCard() && p.hangs() == 0);
        p.boot(saved, true, true); assert(!p.skipCard() && p.hangs() == 0);
        for (unsigned word = 0; word < 5; ++word) {
            Record bad = saved;
            switch (word) {
            case 0: bad.magic ^= 1; break;
            case 1: bad.stage ^= 1; break;
            case 2: bad.hangs ^= 1; break;
            case 3: bad.fallback ^= 1; break;
            default: bad.checksum ^= 1; break;
            }
            p.boot(bad, false, true); assert(p.hangs() == 0 && !p.skipCard());
        }
    }
    p.boot({}, true, false); p.stage(Stage::CardMount);
    p.boot(p.record(), false, true); assert(p.hangs() == 1);
    p.boot(p.record(), false, false); assert(p.hangs() == 0); // breaks streak
    p.stage(Stage::CardSpace); p.boot(p.record(), false, true);
    p.stage(Stage::CardRepair); p.boot(p.record(), false, true);
    assert(p.fallback() == Fallback::SkipCard);
    p.stage(Stage::FlashMount); p.boot(p.record(), false, true);
    assert(p.noStorage()); // one more hang with card already skipped escalates
    p.complete(); assert(valid(p.record()) && !p.active() && p.noStorage());
    Record bad = p.record(); bad.stage = uint32_t(Stage::Count); bad.checksum = checksum(bad);
    p.boot(bad, false, true); assert(!p.skipCard());
    std::puts("Startup watchdog policy: all stages, streaks, escalation, reset latches, cold power-on and corrupt records passed");
}
