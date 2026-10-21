// Pure, bounded production display policy. No Arduino, bus, or task APIs.
#pragma once
#include <cstdint>
#include <cstddef>
#include "diag_catalog.h"

// 30 s for early road tests; planned to return to 10 s afterward.
#ifndef DISPLAY_CARD_MS
#define DISPLAY_CARD_MS 30000
#endif

namespace display {
enum class Motion { Unknown, Parked, Moving };
// VBUS absence takes precedence, even during a trip's finalization.
inline Motion motion(bool vbusLost, bool idle, bool tripMoving,
                     bool trustedFreshSpeed, float speed, float endSpeed) {
    if (vbusLost) return Motion::Parked;
    if (tripMoving || (trustedFreshSpeed && speed >= endSpeed)) return Motion::Moving;
    if (idle && trustedFreshSpeed && speed < endSpeed) return Motion::Parked;
    return Motion::Unknown;
}
enum class Card { Blank, StatusCard, ErrorCard };
struct Condition {
    uint16_t code = 0;
    uint64_t episode = 0; // accepted raise sequence; zero means inactive/unknown
    uint8_t source = 0;   // independent trip/events upload producers can share codes
};
struct Output {
    Card card = Card::Blank;
    bool powered = false;
    uint32_t until = 0;
    int heard = -1;
    uint16_t codes[64] = {}; // unique, severity-ordered active warnings and above
    size_t codeCount = 0;
    uint16_t errorCode = 0;
};
inline unsigned rank(uint16_t code) {
    auto entry = diagnostics::find(code);
    // Unknown active codes remain visible, with no invented catalog text.
    return entry ? unsigned(entry->severity) : unsigned(diagnostics::Severity::Warning);
}
inline Output errorCard(uint16_t code) {
    Output out; out.card = Card::ErrorCard; out.powered = true;
    out.errorCode = code;
    return out;
}
inline Output statusCard(int heard, const Condition* conditions, size_t count) {
    Output out; out.card = Card::StatusCard; out.powered = true;
    out.heard = heard;
    auto& codes = out.codes;
    auto& used = out.codeCount;
    for (size_t i = 0; i < count && used < 64; ++i) {
        if (!conditions[i].episode || rank(conditions[i].code) < unsigned(diagnostics::Severity::Warning)) continue;
        bool duplicate = false;
        for (size_t j = 0; j < used; ++j) duplicate |= codes[j] == conditions[i].code;
        if (!duplicate) codes[used++] = conditions[i].code;
    }
    // Stable severity order, code order breaks ties independently of producer.
    for (size_t i = 1; i < used; ++i) {
        uint16_t code = codes[i]; size_t j = i;
        while (j && (rank(code) > rank(codes[j-1]) ||
                     (rank(code) == rank(codes[j-1]) && code < codes[j-1]))) {
            codes[j] = codes[j-1]; --j;
        }
        codes[j] = code;
    }
    return out;
}

class DisplayModel {
public:
    static_assert(DISPLAY_CARD_MS >= 1000 && DISPLAY_CARD_MS <= 120000,
                  "DISPLAY_CARD_MS must be between 1000 and 120000 ms");
    static constexpr uint32_t kDurationMs = DISPLAY_CARD_MS;
    static constexpr size_t kCapacity = 64;
    struct Input {
        uint32_t now = 0;
        Motion motion = Motion::Unknown;
        bool boot = false, tripFinalized = false, userRequest = false, shutdown = false;
        bool suspended = false; // debug owns panel; still consume observed episodes
        int heard = -1;
        const Condition* conditions = nullptr;
        size_t count = 0;
    };
    const Output& update(const Input& in) {
        if (in.shutdown) { out_ = {}; return out_; }
        if (in.boot || in.tripFinalized) pendingStatus_ = true;
        bool unresolved = false;
        for (size_t i = 0; i < in.count; ++i) {
            const auto& c = in.conditions[i];
            if (!c.episode) continue;
            unresolved |= rank(c.code) >= unsigned(diagnostics::Severity::Warning);
            size_t slot = 0;
            while (slot < seenCount_ && (seen_[slot].code != c.code || seen_[slot].source != c.source)) ++slot;
            if (slot == seenCount_) {
                if (seenCount_ == kCapacity) continue;
                seen_[seenCount_++] = {c.code, 0, c.source};
            }
            if (c.episode <= seen_[slot].episode) continue;
            seen_[slot].episode = c.episode; // also consume overflow; never replay it later
            auto entry = diagnostics::find(c.code);
            if (entry && entry->severity >= diagnostics::Severity::Error && queued_ < kCapacity) {
                queue_[(head_ + queued_) % kCapacity] = c.code; ++queued_;
            }
        }
        if (in.motion == Motion::Parked && previousMotion_ != Motion::Parked && unresolved) pendingStatus_ = true;
        previousMotion_ = in.motion;
        if (in.suspended) {
            pendingStatus_ = false;
            head_ = queued_ = 0;
            if (!userCard_) out_ = {};
        }
        if (out_.powered && uint32_t(in.now - started_) >= kDurationMs) out_ = {};
        // A routine status card must not remain visible after driving resumes.
        if (out_.card == Card::StatusCard && !userCard_ && in.motion != Motion::Parked) out_ = {};
        if (in.userRequest) {
            show(statusCard(in.heard, in.conditions, in.count), in.now); userCard_ = true;
            if (in.motion == Motion::Parked) pendingStatus_ = false;
        } else if (queued_ && out_.card != Card::ErrorCard && !(out_.powered && userCard_)) {
            show(errorCard(queue_[head_]), in.now);
            head_ = (head_ + 1) % kCapacity; --queued_; userCard_ = false;
        } else if (pendingStatus_ && in.motion == Motion::Parked &&
                   (!out_.powered || (in.tripFinalized && out_.card == Card::StatusCard))) {
            show(statusCard(in.heard, in.conditions, in.count), in.now);
            pendingStatus_ = false; userCard_ = false;
        }
        return out_;
    }
    // Preserve episode/motion history across ownership changes, but never
    // replay a card or pending trigger from the previous mode.
    void dismiss() {
        out_ = {}; pendingStatus_ = userCard_ = false;
        head_ = queued_ = 0;
    }
private:
    void show(Output card, uint32_t now) { out_ = card; started_ = now; out_.until = now + kDurationMs; }
    Output out_;
    uint32_t started_ = 0;
    Motion previousMotion_ = Motion::Unknown;
    bool pendingStatus_ = true, userCard_ = false;
    Condition seen_[kCapacity] = {};
    size_t seenCount_ = 0, head_ = 0, queued_ = 0;
    uint16_t queue_[kCapacity] = {};
};
} // namespace display
