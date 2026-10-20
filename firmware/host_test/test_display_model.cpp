#include "display_model.h"
#include <cassert>
#include <cstdio>
#include <cstring>
using namespace display;
static unsigned cases = 0;
static void bounded(const Output& out) {
    for (const auto& line : out.lines) {
        assert(std::strlen(line) <= 21);
        for (char ch : line) { if (!ch) break; assert(ch >= 32 && ch <= 126); }
    }
}
int main() {
    DisplayModel model; DisplayModel::Input in;
    in.boot = true;
    assert(!model.update(in).powered);
    in.boot = false; in.motion = Motion::Moving; in.now = 100;
    assert(!model.update(in).powered);
    in.motion = Motion::Parked;
    assert(model.update(in).card == Card::StatusCard); ++cases;
    in.now += 9999; assert(model.update(in).powered);
    ++in.now; assert(!model.update(in).powered);
    in.now += 10000; assert(!model.update(in).powered); ++cases;

    model = DisplayModel{}; in = {}; in.boot = true; in.motion = Motion::Parked;
    const auto& boot = model.update(in); assert(boot.powered && boot.until == 10000);
    in.boot = false; in.now = 10000; assert(!model.update(in).powered); ++cases;

    Condition critical[] = {{4003, 1, 0}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving;
    in.conditions = critical; in.count = 1;
    assert(model.update(in).card == Card::ErrorCard);
    assert(!std::strcmp(model.update(in).lines[0], "E4003"));
    in.now = 10000; assert(!model.update(in).powered);
    in.now = 900000; assert(!model.update(in).powered); ++cases;
    critical[0].episode = 2; assert(model.update(in).powered); ++cases;

    Condition warning[] = {{3002, 3, 0}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = warning; in.count = 1;
    assert(!model.update(in).powered);
    in.motion = Motion::Parked;
    assert(model.update(in).card == Card::StatusCard);
    assert(!std::strcmp(model.update(in).lines[1], "E3002")); ++cases;

    Condition errors[] = {{4003, 4, 0}, {4002, 5, 0}, {5004, 6, 1}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = errors; in.count = 3;
    for (unsigned i = 0; i < 3; ++i) {
        in.now = i * 10000;
        char code[22]; codeLine(code, errors[i].code);
        assert(!std::strcmp(model.update(in).lines[0], code));
    }
    in.now = 30000; assert(!model.update(in).powered); ++cases;
    // An explicit status request owns its full interval even with errors queued.
    model = DisplayModel{}; in.now = 0; model.update(in);
    in.now = 100; in.userRequest = true;
    assert(model.update(in).card == Card::StatusCard);
    in.userRequest = false; in.now = 10099;
    assert(model.update(in).card == Card::StatusCard);
    in.now = 10100; assert(!std::strcmp(model.update(in).lines[0], "E4002"));
    in.now = 20100; model.update(in); in.now = 30100; assert(!model.update(in).powered); ++cases;
    in.count = 0; model.update(in); // clear does not discard identity history
    in.count = 3; assert(!model.update(in).powered); ++cases;

    model = DisplayModel{}; in = {}; in.motion = Motion::Parked;
    model.update(in); in.now = 10000; model.update(in);
    in.tripFinalized = true; assert(model.update(in).card == Card::StatusCard); ++cases;
    in.tripFinalized = false; in.motion = Motion::Moving;
    assert(!model.update(in).powered); ++cases;
    in.userRequest = true; assert(model.update(in).card == Card::StatusCard);
    in.userRequest = false; in.now += 9999; assert(model.update(in).powered);
    ++in.now; assert(!model.update(in).powered); ++cases;
    in.userRequest = true; model.update(in); in.shutdown = true;
    assert(!model.update(in).powered && model.update(in).card == Card::Blank); ++cases;

    model = DisplayModel{}; in = {}; in.now = UINT32_MAX - 4999; in.motion = Motion::Parked;
    assert(model.update(in).until == 5000);
    in.now = 4999; assert(model.update(in).powered);
    in.now = 5000; assert(!model.update(in).powered); ++cases;
    assert(motion(true, false, true, false, 20, .894f) == Motion::Parked);
    assert(motion(false, true, false, false, 0, .894f) == Motion::Unknown);
    assert(motion(false, true, false, true, .894f, .894f) == Motion::Moving);
    assert(motion(false, true, false, true, 0, .894f) == Motion::Parked);
    assert(motion(false, false, true, false, 0, .894f) == Motion::Moving); ++cases;

    // Every generated line is copied exactly; unknown codes have no extra text.
    for (const auto& entry : diagnostics::kCatalog) {
        auto card = errorCard(uint16_t(entry.code)); bounded(card);
        for (unsigned i = 0; i < entry.oled_line_count; ++i)
            assert(!std::strcmp(card.lines[i+1], entry.oled[i]));
        Condition c{uint16_t(entry.code), 1, 0}; bounded(statusCard(255, &c, 1));
    }
    auto card = errorCard(6999); bounded(card);
    assert(!std::strcmp(card.lines[0], "E6999") && !card.lines[1][0]); ++cases;
    Condition many[64];
    for (unsigned i = 0; i < 64; ++i) many[i] = {uint16_t(6100+i), i+1, 0};
    card = statusCard(-1, many, 64); bounded(card);
    assert(!std::strcmp(card.lines[0], "Sats heard: --"));
    assert(std::strstr(card.lines[7], "+52 more")); ++cases;
    Condition sorted[] = {{3002, 1, 0}, {4003, 2, 0}, {4003, 3, 1}, {1000, 4, 0}};
    card = statusCard(7, sorted, 4); bounded(card);
    assert(!std::strcmp(card.lines[0], "Sats heard: 7"));
    assert(!std::strcmp(card.lines[1], "E4003"));
    assert(!std::strcmp(card.lines[4], "Also: E3002")); ++cases;

    // Queue exhaustion drops excess episodes once; unchanged snapshots cannot
    // create a never-ending carousel. Both sources' same-code episodes count.
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = critical; in.count = 1;
    for (unsigned i = 1; i <= 100; ++i) { critical[0].episode = i; model.update(in); }
    for (unsigned i = 1; i <= 65; ++i) { in.now = i * 10000; model.update(in); }
    assert(!model.update(in).powered); ++cases;
    Condition duplicate[] = {{5004, 101, 1}, {5004, 102, 2}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = duplicate; in.count = 2;
    assert(model.update(in).powered); in.now = 10000; assert(model.update(in).powered);
    in.now = 20000; assert(!model.update(in).powered); ++cases;
    std::printf("test_display_model: %u scenarios passed\n", cases);
}
