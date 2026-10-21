#ifndef DISPLAY_CARD_MS
static constexpr unsigned kExpectedDurationMs = 30000;
#else
static constexpr unsigned kExpectedDurationMs = DISPLAY_CARD_MS;
#endif
#include "display_model.h"
#include <cassert>
#include <cstdio>
using namespace display;
static_assert(DisplayModel::kDurationMs == kExpectedDurationMs,
              "card duration must use the 30 s default or the build override");
static unsigned cases = 0;
int main() {
    DisplayModel model; DisplayModel::Input in;
    in.boot = true;
    assert(!model.update(in).powered);
    in.boot = false; in.motion = Motion::Moving; in.now = 100;
    assert(!model.update(in).powered);
    in.motion = Motion::Parked;
    assert(model.update(in).card == Card::StatusCard); ++cases;
    in.now += DisplayModel::kDurationMs - 1; assert(model.update(in).powered);
    ++in.now; assert(!model.update(in).powered);
    in.now += DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;

    model = DisplayModel{}; in = {}; in.boot = true; in.motion = Motion::Parked;
    const auto& boot = model.update(in); assert(boot.powered && boot.until == DisplayModel::kDurationMs);
    in.boot = false; in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;

    Condition critical[] = {{4003, 1, 0}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving;
    in.conditions = critical; in.count = 1;
    assert(model.update(in).card == Card::ErrorCard);
    assert(model.update(in).errorCode == 4003);
    in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered);
    in.now = 900000; assert(!model.update(in).powered); ++cases;
    critical[0].episode = 2; assert(model.update(in).powered); ++cases;

    Condition warning[] = {{3002, 3, 0}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = warning; in.count = 1;
    assert(!model.update(in).powered);
    in.motion = Motion::Parked;
    assert(model.update(in).card == Card::StatusCard);
    assert(model.update(in).codes[0] == 3002); ++cases;

    Condition errors[] = {{4003, 4, 0}, {4002, 5, 0}, {5004, 6, 1}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = errors; in.count = 3;
    for (unsigned i = 0; i < 3; ++i) {
        in.now = i * DisplayModel::kDurationMs;
        assert(model.update(in).errorCode == errors[i].code);
    }
    in.now = 3 * DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;
    // An explicit status request owns its full interval even with errors queued.
    model = DisplayModel{}; in.now = 0; model.update(in);
    in.now = 100; in.userRequest = true;
    assert(model.update(in).card == Card::StatusCard);
    in.userRequest = false; in.now = 100 + DisplayModel::kDurationMs - 1;
    assert(model.update(in).card == Card::StatusCard);
    ++in.now; assert(model.update(in).errorCode == 4002);
    in.now += DisplayModel::kDurationMs; model.update(in);
    in.now += DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;
    in.count = 0; model.update(in); // clear does not discard identity history
    in.count = 3; assert(!model.update(in).powered); ++cases;

    model = DisplayModel{}; in = {}; in.motion = Motion::Parked;
    model.update(in); in.now = DisplayModel::kDurationMs; model.update(in);
    in.tripFinalized = true; assert(model.update(in).card == Card::StatusCard); ++cases;
    in.tripFinalized = false; in.motion = Motion::Moving;
    assert(!model.update(in).powered); ++cases;
    in.userRequest = true; assert(model.update(in).card == Card::StatusCard);
    in.userRequest = false; in.now += DisplayModel::kDurationMs - 1; assert(model.update(in).powered);
    ++in.now; assert(!model.update(in).powered); ++cases;
    in.userRequest = true; model.update(in); in.shutdown = true;
    assert(!model.update(in).powered && model.update(in).card == Card::Blank); ++cases;

    model = DisplayModel{}; in = {}; in.now = UINT32_MAX - DisplayModel::kDurationMs / 2;
    in.motion = Motion::Parked;
    const uint32_t wrapEnd = in.now + DisplayModel::kDurationMs;
    assert(model.update(in).until == wrapEnd);
    in.now = wrapEnd - 1; assert(model.update(in).powered);
    in.now = wrapEnd; assert(!model.update(in).powered); ++cases;
    assert(motion(true, false, true, false, 20, .894f) == Motion::Parked);
    assert(motion(false, true, false, false, 0, .894f) == Motion::Unknown);
    assert(motion(false, true, false, true, .894f, .894f) == Motion::Moving);
    assert(motion(false, true, false, true, 0, .894f) == Motion::Parked);
    assert(motion(false, false, true, false, 0, .894f) == Motion::Moving); ++cases;

    // Structured content contains codes only, with no catalog OLED descriptions.
    for (const auto& entry : diagnostics::kCatalog) {
        auto card = errorCard(uint16_t(entry.code));
        assert(card.errorCode == uint16_t(entry.code) && !card.codeCount);
    }
    auto card = errorCard(6999);
    assert(card.errorCode == 6999 && !card.codeCount); ++cases;
    card = statusCard(12, nullptr, 0);
    assert(card.heard == 12 && !card.codeCount && !card.errorCode); ++cases;
    Condition many[64];
    for (unsigned i = 0; i < 64; ++i) many[i] = {uint16_t(6100+i), i+1, 0};
    card = statusCard(-1, many, 64);
    assert(card.heard == -1 && card.codeCount == 64);
    for (unsigned i = 0; i < 64; ++i) assert(card.codes[i] == 6100+i);
    ++cases;
    Condition sorted[] = {{3002, 1, 0}, {4003, 2, 0}, {4003, 3, 1}, {1000, 4, 0}};
    card = statusCard(7, sorted, 4);
    assert(card.heard == 7 && card.codeCount == 2);
    assert(card.codes[0] == 4003 && card.codes[1] == 3002); ++cases;

    // Queue exhaustion drops excess episodes once; unchanged snapshots cannot
    // create a never-ending carousel. Both sources' same-code episodes count.
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = critical; in.count = 1;
    for (unsigned i = 1; i <= 100; ++i) { critical[0].episode = i; model.update(in); }
    for (unsigned i = 1; i <= 65; ++i) { in.now = i * DisplayModel::kDurationMs; model.update(in); }
    assert(!model.update(in).powered); ++cases;
    Condition duplicate[] = {{5004, 101, 1}, {5004, 102, 2}};
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving; in.conditions = duplicate; in.count = 2;
    assert(model.update(in).powered); in.now = DisplayModel::kDurationMs; assert(model.update(in).powered);
    in.now += DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;
    std::printf("test_display_model: %u scenarios passed\n", cases);
}
