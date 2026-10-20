#include "../04_trip_logger/degraded_policy.h"
#include <cassert>
#include <cstdio>
#include <cstring>
#include <initializer_list>
using namespace startup;
int main() {
    unsigned cases = 0;
    assert(std::strcmp(modeText(Mode::Normal), "normal") == 0);
    assert(std::strcmp(modeText(Mode::PmuMissing), "power chip not responding") == 0);
    assert(std::strcmp(modeText(Mode::PmuUntrusted), "power chip state untrusted") == 0);
    assert(std::strcmp(modeText(Mode::NoStorage), "no usable trip storage") == 0);
    cases += 4;
    for (bool pmu : {false, true}) for (bool radio : {false, true}) for (bool store : {false, true}) {
        assert(selectMode(pmu, radio, store) == (!pmu ? Mode::PmuMissing : !radio ?
            Mode::PmuUntrusted : !store ? Mode::NoStorage : Mode::Normal));
        ++cases;
    }
    for (auto mode : {Mode::PmuMissing, Mode::PmuUntrusted, Mode::NoStorage}) {
        for (uint32_t start : {0u, UINT32_MAX - 30000u}) {
            DegradedPolicy p; p.begin(mode, start);
            assert(p.mode() == mode);
            uint32_t now = start;
            for (uint32_t wait : {60000u, 120000u, 240000u, 480000u, 600000u, 600000u}) {
                assert(p.waitMs() == wait);
                assert(!p.retryDue(now + wait - 1));
                assert(!p.retryResult(now + wait - 1, true));
                now += wait;
                assert(p.retryDue(now));
                assert(!p.retryResult(now, false));
                assert(!p.retryDue(now));
                ++cases;
            }
            now += p.waitMs();
            assert(p.retryResult(now, true));
            assert(!p.retryDue(now)); // a failed restart cannot cause a hot loop
            ++cases;
        }
    }
    DegradedPolicy p;
    assert(!p.retryDue(UINT32_MAX) && !p.retryResult(UINT32_MAX, true));
    std::printf("test_degraded_policy: %u cases passed\n", cases + 1);
}
