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
        // Publish only atomic values across cores. A reader never copies the
        // producer's mutable Condition or EventInput while it is being changed.
        struct Publish {
            RuntimeFaults& owner; Condition& slot; size_t index;
            ~Publish() {
                uint64_t value = 0;
                if (slot.observed && (!slot.desired || slot.episode))
                    value = (uint64_t(1) << 63) | (slot.desired ? slot.episode : 0);
                if (owner.published_[index].low.load(std::memory_order_relaxed) == uint32_t(value) &&
                    owner.published_[index].high.load(std::memory_order_relaxed) == uint32_t(value >> 32)) return;
                owner.revision_.fetch_add(1, std::memory_order_acq_rel);
                owner.published_[index].low.store(uint32_t(value), std::memory_order_release);
                owner.published_[index].high.store(uint32_t(value >> 32), std::memory_order_release);
                owner.revision_.fetch_add(1, std::memory_order_release);
            }
        } publish{*this, *slot, size_t(slot - slots_)};
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
    // The check-in worker reads this counter on its owning network task.
    // Never journal E5001, including at overflow (saturate at protocol maximum).
    void homeApAbsent() { if (homeApMisses_ < kMaxEventInteger) ++homeApMisses_; }
    uint64_t homeApMisses() const { return homeApMisses_; }
    struct Observation { uint16_t code; uint64_t episode; bool observed; };
    // One bounded attempt, no waits. Omit an active condition whose raise has
    // never been accepted: there is no valid episode identity to transmit yet.
    unsigned publicationRevision() const { return revision_.load(std::memory_order_acquire); }
    bool snapshot(Observation* out, size_t capacity, size_t& count, unsigned* capturedRevision = nullptr) const {
        if (count_ > capacity || count_ > 64) return false;
        unsigned revision = revision_.load(std::memory_order_acquire);
        if (revision & 1) return false;
        for (size_t i = 0; i < count_; ++i) {
            uint64_t value = published_[i].low.load(std::memory_order_acquire);
            value |= uint64_t(published_[i].high.load(std::memory_order_acquire)) << 32;
            out[i] = {slots_[i].code, value & kMaxEventInteger, bool(value >> 63)};
        }
        count = count_;
        if (revision != revision_.load(std::memory_order_acquire)) return false;
        if (capturedRevision) *capturedRevision = revision;
        return true;
    }
private:
    EventLog& log_;
    Condition* slots_;
    size_t count_;
    EventInput input_;
    uint64_t homeApMisses_ = 0;
    std::atomic<unsigned> revision_{0};
    // ESP32-S3 has native 32-bit atomics; avoid libatomic's 64-bit lock path.
    struct AtomicObservation { std::atomic<uint32_t> low{0}, high{0}; };
    AtomicObservation published_[64];
};
} // namespace diagnostics
