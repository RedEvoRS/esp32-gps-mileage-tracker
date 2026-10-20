// Task-owned producer state only: no filesystem, waits, allocation or ISR use.
#pragma once
#include "event_log.h"

namespace diagnostics {
class RuntimeFaults {
public:
    static constexpr uint32_t kRepeatMs = 15 * 60 * 1000, kRetryMs = 60 * 1000;
    struct Condition {
        uint16_t code = 0;
        bool observed = false, desired = false, failed = false;
        uint64_t episode = 0;
        uint32_t attemptedMs = 0, acceptedMs = 0;
    };
    // One instance per task. The fixed input buffer belongs in static storage,
    // as do the condition slots; never construct EventInput on the net stack.
    RuntimeFaults(EventLog& log, Condition* slots, size_t count)
        : log_(log), slots_(slots), count_(count) {}

    RuntimeFaults& event(uint16_t code, uint32_t now) {
        input_.code = code; input_.uptimeMs = now;
        input_.contextCount = 0; input_.episodeSequence = 0;
        // Runtime hooks deliberately use unknown UTC until #128 supplies clock
        // confidence/age. They never substitute uptime or a stale RTC for UTC.
        return *this;
    }
    RuntimeFaults& integer(const char* key, int64_t n) {
        input_.context[input_.contextCount++] = EventContext::integer(key, n); return *this;
    }
    RuntimeFaults& enumeration(const char* key, const char* value) {
        input_.context[input_.contextCount++] = EventContext::enumeration(key, value); return *this;
    }
    void notice() {
        input_.transition = EventTransition::Notice;
        log_.submit(input_);
    }
    void condition(bool active) {
        Condition* slot = nullptr;
        for (size_t i = 0; i < count_; ++i) if (slots_[i].code == input_.code) { slot = &slots_[i]; break; }
        if (!slot) return;
        auto& s = *slot;
        bool changed = !s.observed || s.desired != active;
        s.observed = true; s.desired = active;
        uint32_t now = uint32_t(input_.uptimeMs);
        if (!active && !s.episode) return; // observed healthy is silent
        // A failed clear keeps its episode, and retries are bounded. A new
        // physical transition may attempt immediately, including fast recovery.
        if (!changed && s.failed && uint32_t(now - s.attemptedMs) < kRetryMs) return;
        // If a clear was dropped, renewed failure still belongs to the old
        // accepted episode: it cannot bypass that episode's repeat limit.
        if (active && s.episode && uint32_t(now - s.acceptedMs) < kRepeatMs) return;
        s.attemptedMs = now;
        input_.episodeSequence = s.episode;
        input_.transition = !active ? EventTransition::Clear :
                            s.episode ? EventTransition::Repeat : EventTransition::Raise;
        EventIdentity id;
        s.failed = log_.submit(input_, &id) != EventLog::Submit::Accepted;
        if (!s.failed) {
            s.episode = active ? (s.episode ? s.episode : id.sequence) : 0;
            s.acceptedMs = now;
        }
    }
    // #125 may copy these task-owned values at a coherent handoff for check-in.
    // Never journal E5001, including at overflow (saturate at protocol maximum).
    void homeApAbsent() { if (homeApMisses_ < kMaxEventInteger) ++homeApMisses_; }
    uint64_t homeApMisses() const { return homeApMisses_; }
private:
    EventLog& log_;
    Condition* slots_;
    size_t count_;
    EventInput input_;
    uint64_t homeApMisses_ = 0;
};
} // namespace diagnostics
