#include "../04_trip_logger/gps_antenna.h"
#include <cassert>
#include <cstdio>
#include <initializer_list>

static DebugSignal weak{15, 0, 20}, stale;

static GpsAntenna raised(uint32_t first = 30000) {
    GpsAntenna antenna;
    antenna.update(first, true, 79, {13, 0, 15});
    assert(!antenna.observed() && !antenna.active());
    for (uint32_t gap : {30000u, 60000u, 90000u, 119999u}) {
        antenna.update(first + gap, true, 79, weak);
        assert(!antenna.observed() && !antenna.active());
    }
    antenna.update(first + 120000, true, 79, weak);
    assert(antenna.observed() && antenna.active());
    return antenna;
}

int main() {
    raised(); // exactly 120 s from the first suspect, never before
    {
        GpsAntenna a;
        for (uint32_t at : {0u, 30000u, 60000u}) {
            a.update(at, true, 29, weak);
            assert(a.observed() && !a.active());
        }
        for (uint32_t at : {90000u, 120000u, 150000u, 180000u, 209999u}) {
            a.update(at, true, 79, weak); assert(!a.active());
        }
        a.update(210000, true, 79, weak); assert(a.active());
    }
    {
        GpsAntenna indoors, outdoors;
        for (uint32_t at = 0; at <= 1800000; at += 30000) {
            indoors.update(at, true, at % 60000 ? 6 : 18, {25, int((at / 30000) % 4), 33});
            outdoors.update(at, true, 29, {26, 22, 50});
            assert(indoors.observed() && !indoors.active());
            assert(outdoors.observed() && !outdoors.active());
        }
    }
    // Each no-evidence input restarts the hold and cannot clear a warning.
    for (unsigned kind = 0; kind < 3; ++kind) {
        GpsAntenna a;
        a.update(0, true, 79, weak); a.update(90000, true, 79, weak);
        a.update(120000, kind != 0, kind == 2 ? 60 : 79, kind == 1 ? stale : weak);
        assert(!a.observed() && !a.active());
        a.update(150000, true, 79, weak); a.update(269999, true, 79, weak);
        assert(!a.observed() && !a.active());
        a.update(270000, true, 79, weak); assert(a.active());
        a.update(300000, kind != 0, kind == 2 ? 60 : 79, kind == 1 ? stale : weak);
        assert(a.observed() && a.active());
    }
    {
        GpsAntenna a;
        a.update(0, false, 29, stale); a.update(300000, true, 79, stale);
        a.update(600000, true, 60, weak);
        assert(!a.observed() && !a.active());
    }
    for (unsigned agc : {6u, 18u, 29u, 50u}) {
        auto a = raised();
        a.update(180000, true, agc, weak); assert(a.observed() && !a.active());
    }
    for (bool rfValid : {false, true}) {
        auto a = raised();
        a.update(180000, rfValid, 79, {20, 1, 30}); assert(!a.active());
    }
    {
        auto a = raised();
        a.update(180000, true, 50, stale); assert(!a.active());
        a.update(210000, true, 70, weak); a.update(329999, true, 70, weak); assert(!a.active());
        a.update(330000, true, 70, weak); assert(a.active());
    }
    raised(UINT32_MAX - 60000); // hold survives millis rollover
    {
        GpsAntenna a;
        a.update(0, true, 79, weak); a.update(90000, true, 79, weak);
        a.update(120000, true, 29, weak); // healthy breaks the suspect run
        a.update(150000, true, 79, weak); a.update(269999, true, 79, weak); assert(!a.active());
        a.update(270000, true, 79, weak); assert(a.active());
    }
    std::puts("GPS antenna: 16 scenarios passed (hold, bench signals, no evidence, recovery, rollover).");
}
