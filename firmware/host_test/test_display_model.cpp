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
static void usbPowerRegressions() {
    DisplayModel model; DisplayModel::Input in;
    in.motion = Motion::Moving;
    in.now = UINT32_MAX - DisplayModel::kDurationMs / 2;
    in.usbPowerRequest = in.userRequest = true;
    assert(model.update(in).card == Card::UsbPowerCard);
    const uint32_t until = in.now + DisplayModel::kDurationMs;
    in.usbPowerRequest = in.userRequest = false;
    in.now = until - 1;
    assert(model.update(in).card == Card::UsbPowerCard && model.update(in).until == until);
    in.now = until; assert(!model.update(in).powered); ++cases;

    // Short taps still select status, and cannot leave a refusal to replay.
    in.usbPowerRequest = true; model.update(in); in.usbPowerRequest = false;
    ++in.now; in.userRequest = true;
    assert(model.update(in).card == Card::StatusCard);
    in.userRequest = false; in.now += DisplayModel::kDurationMs;
    assert(!model.update(in).powered); ++cases;

    // Refusal feedback retains the interrupted error and the remaining queue.
    model = DisplayModel{}; in = {}; in.motion = Motion::Moving;
    Condition errors[] = {{4003, 1, 0}, {4002, 2, 0}};
    in.conditions = errors; in.count = 2;
    assert(model.update(in).errorCode == 4003);
    ++in.now; in.usbPowerRequest = true;
    assert(model.update(in).card == Card::UsbPowerCard);
    ++in.now; assert(model.update(in).card == Card::UsbPowerCard); // repeated press
    in.usbPowerRequest = false;
    in.now += DisplayModel::kDurationMs;
    assert(model.update(in).errorCode == 4003);
    in.now += DisplayModel::kDurationMs;
    assert(model.update(in).errorCode == 4002);
    in.now += DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;

    // Debug accepts explicit feedback, consumes hidden faults, and expires it.
    model = DisplayModel{}; in = {}; in.suspended = true; in.usbPowerRequest = true;
    assert(model.update(in).card == Card::UsbPowerCard);
    in.usbPowerRequest = false; in.conditions = errors; in.count = 2;
    ++in.now; assert(model.update(in).card == Card::UsbPowerCard);
    in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered);
    in.suspended = false; assert(!model.update(in).powered); ++cases;

    in.usbPowerRequest = true; model.update(in); in.usbPowerRequest = false;
    model.dismiss(); assert(!model.update(in).powered); // no stale mode-switch card
    in.usbPowerRequest = true; model.update(in); in.usbPowerRequest = false;
    in.shutdown = true; assert(!model.update(in).powered); ++cases;
}
int main() {
    {
        DisplayModel model; DisplayModel::Input in;
        in.switchingOff = in.suspended = in.usbPowerRequest = true;
        assert(model.update(in).card == Card::SwitchingOffCard);
        assert(model.update(in).powered);
        in.shutdown = true; assert(!model.update(in).powered);
        ++cases;
    }
    usbPowerRegressions();
    DisplayModel model; DisplayModel::Input in;
    // The runtime's first update implies boot, without a trusted GPS fix.
    in.now = 100;
    assert(model.update(in).card == Card::StatusCard);
    assert(model.update(in).heard == -1);
    const uint32_t bootEnd = in.now + DisplayModel::kDurationMs;
    assert(model.update(in).until == bootEnd); ++cases;
    Condition live[] = {{3002, 1, 0}, {3006, 2, 0}};
    in.now += 100; in.heard = 7; in.conditions = live; in.count = 2;
    assert(model.update(in).heard == 7 && model.update(in).codeCount == 2);
    assert(model.update(in).codes[0] == 3002 && model.update(in).codes[1] == 3006);
    assert(model.update(in).until == bootEnd);
    live[0].episode = 0; in.heard = 12; ++in.now;
    assert(model.update(in).heard == 12 && model.update(in).codeCount == 1);
    assert(model.update(in).codes[0] == 3006 && model.update(in).until == bootEnd);
    in.count = 0; in.heard = -1; ++in.now;
    assert(model.update(in).heard == -1 && model.update(in).codeCount == 0);
    assert(model.update(in).until == bootEnd); ++cases;
    in.now = bootEnd - 1; assert(model.update(in).powered);
    ++in.now; assert(!model.update(in).powered);
    in.motion = Motion::Parked; assert(!model.update(in).powered);
    in.now += DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;

    model = DisplayModel{}; in = {}; in.boot = true;
    assert(model.update(in).card == Card::StatusCard);
    in.boot = false; in.now = 100; in.motion = Motion::Moving;
    assert(!model.update(in).powered);
    in.motion = Motion::Unknown; assert(!model.update(in).powered);
    in.motion = Motion::Parked; assert(!model.update(in).powered); ++cases;

    // Becoming Parked during the boot interval does not restart its timer.
    model = DisplayModel{}; in = {};
    model.update(in); in.now = 100; in.motion = Motion::Parked;
    assert(model.update(in).until == DisplayModel::kDurationMs);
    in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered); ++cases;

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
    in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered);
    in.motion = Motion::Unknown; assert(!model.update(in).powered);
    in.motion = Motion::Parked; assert(model.update(in).card == Card::StatusCard);
    in.now += DisplayModel::kDurationMs; assert(!model.update(in).powered);
    // Repeated parked samples do not repeat an unresolved warning.
    ++in.now; assert(!model.update(in).powered); ++cases;

    // Boot does not consume the unresolved-fault reminder at the next Parked.
    model = DisplayModel{}; in = {}; in.conditions = warning; in.count = 1;
    assert(model.update(in).card == Card::StatusCard);
    in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered);
    in.motion = Motion::Parked; assert(model.update(in).codes[0] == 3002); ++cases;

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
    const uint32_t tripEnd = in.now + DisplayModel::kDurationMs;
    in.tripFinalized = false; in.motion = Motion::Unknown; ++in.now; in.heard = 4;
    assert(model.update(in).powered && model.update(in).heard == 4);
    assert(model.update(in).until == tripEnd); ++cases;
    in.tripFinalized = false; in.motion = Motion::Moving;
    assert(!model.update(in).powered); ++cases;
    in.userRequest = true; assert(model.update(in).card == Card::StatusCard);
    const uint32_t userEnd = in.now + DisplayModel::kDurationMs;
    in.userRequest = false;
    in.heard = 9; in.conditions = warning; in.count = 1;
    assert(model.update(in).heard == 9 && model.update(in).codes[0] == 3002);
    assert(model.update(in).until == userEnd);
    in.userRequest = false; in.now += DisplayModel::kDurationMs - 1; assert(model.update(in).powered);
    ++in.now; assert(!model.update(in).powered); ++cases;
    in.userRequest = true; model.update(in); in.shutdown = true;
    assert(!model.update(in).powered && model.update(in).card == Card::Blank); ++cases;
    in.shutdown = in.userRequest = false;
    assert(!model.update(in).powered); ++cases;

    // Only boot bypasses Parked: finalization waits through Unknown/Moving.
    model = DisplayModel{}; in = {}; model.update(in);
    in.now = DisplayModel::kDurationMs; model.update(in);
    in.tripFinalized = true; assert(!model.update(in).powered);
    in.tripFinalized = false; in.motion = Motion::Moving; assert(!model.update(in).powered);
    in.motion = Motion::Unknown; assert(!model.update(in).powered);
    in.motion = Motion::Parked; assert(model.update(in).card == Card::StatusCard); ++cases;

    // Debug consumes boot, trip and error triggers; only a user overlay shows.
    model = DisplayModel{}; in = {}; in.suspended = in.boot = in.tripFinalized = true;
    in.conditions = critical; in.count = 1;
    assert(!model.update(in).powered);
    in.boot = in.tripFinalized = in.suspended = false;
    assert(!model.update(in).powered);
    in.motion = Motion::Parked; assert(model.update(in).card == Card::StatusCard);
    in.suspended = true; assert(!model.update(in).powered);
    in.userRequest = true; assert(model.update(in).card == Card::StatusCard);
    in.userRequest = false; in.motion = Motion::Moving; in.now = 100; in.heard = 5;
    assert(model.update(in).heard == 5 && model.update(in).until == DisplayModel::kDurationMs);
    in.now = DisplayModel::kDurationMs; assert(!model.update(in).powered);
    in.suspended = false; assert(!model.update(in).powered); ++cases;

    // Dismiss also consumes an unshown boot and preserves episode history.
    model = DisplayModel{}; in = {}; model.dismiss();
    assert(!model.update(in).powered);
    in.conditions = critical; in.count = 1; assert(model.update(in).card == Card::ErrorCard);
    model.dismiss(); assert(!model.update(in).powered);
    in.tripFinalized = true; in.motion = Motion::Unknown; model.update(in);
    in.tripFinalized = false; model.dismiss();
    in.motion = Motion::Parked; assert(model.update(in).card == Card::StatusCard);
    model.dismiss(); assert(!model.update(in).powered); ++cases;

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
