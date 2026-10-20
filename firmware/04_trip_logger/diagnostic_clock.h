// Independent single-writer publications: core 1 owns ticks/RTC/GPS, core 0 SNTP.
// Readers do bounded seqlock copies with native 32-bit atomics only; never spin.
#pragma once
#include "event_log.h"

namespace diagnostics {
class DiagnosticClock {
    // Xtensa reports ATOMIC_INT_LOCK_FREE=1 because arbitrary alignment is not
    // guaranteed. Aligned 32-bit atomic loads/stores emit l32i/s32i + memw (no
    // library call or lock). Only these operations are used by this provider.
    static_assert(sizeof(std::atomic<uint32_t>) == 4 && alignof(std::atomic<uint32_t>) >= 4,
                  "diagnostic timestamps require aligned 32-bit atomics");
    struct Value { uint64_t a = 0, b = 0, c = 0; uint32_t tick = 0, valid = 0; };
    struct Publication {
        std::atomic<uint32_t> revision{0}, words[8]{};
        void put(const Value& v) {
            uint32_t next = revision.load() + 1; // exactly one writer per slot
            revision.store(next);
            const uint32_t w[] = {uint32_t(v.a), uint32_t(v.a >> 32), uint32_t(v.b), uint32_t(v.b >> 32),
                                  uint32_t(v.c), uint32_t(v.c >> 32), v.tick, v.valid};
            for (unsigned i = 0; i < 8; ++i) words[i].store(w[i]);
            revision.store(next + 1);
        }
        bool get(Value& v) const {
            // Sequential consistency also orders the payload loads against both
            // revision checks. A racing writer yields unknown time for this event.
            for (unsigned attempt = 0; attempt < 2; ++attempt) {
                uint32_t before = revision.load(), w[8];
                if (before & 1) continue;
                for (unsigned i = 0; i < 8; ++i) w[i] = words[i].load();
                if (before != revision.load()) continue;
                v = {uint64_t(w[0]) | uint64_t(w[1]) << 32, uint64_t(w[2]) | uint64_t(w[3]) << 32,
                     uint64_t(w[4]) | uint64_t(w[5]) << 32, w[6], w[7]}; return true;
            }
            return false;
        }
    } rtc_, gps_, sntp_;
    uint32_t lastTick_ = 0; // core 1 only; service at least once per millis period
    uint64_t uptime_ = 0;
    std::atomic<uint32_t> halfPeriods_{0};
    std::atomic<uint32_t> latest_{0};
    uint64_t uptime(uint32_t now) const {
        // Publish the half-period index in ONE atomic word. A caller's sampled
        // tick can straddle its publication in either direction. Reconstruct the
        // adjacent half-period using its low bits (loop runs far more often than
        // every 2^30 ms). No seqlock collision can lose a trusted sync publication.
        uint32_t period = halfPeriods_.load();
        if ((now >> 31) != (period & 1)) period += (now & 0x7fffffff) < 0x40000000 ? 1 : uint32_t(-1);
        return (uint64_t(period) << 31) | (now & 0x7fffffff);
    }
public:
    EventTimeSource latestSource() const { return EventTimeSource(latest_.load()); }
    void tick(uint32_t now) {
        uptime_ += uint32_t(now - lastTick_); lastTick_ = now;
        halfPeriods_.store(uint32_t(uptime_ >> 31));
    }
    void trusted(EventTimeSource source, uint32_t now) {
        uint64_t at = uptime(now);
        (source == EventTimeSource::Gps ? gps_ : sntp_).put({at, 0, 0, now, 1});
        latest_.store(uint32_t(source));
    }
    void rtcAnchor(uint32_t seconds, uint32_t now, int64_t ageMs) {
        rtcAnchorMs(uint64_t(seconds) * 1000, now, ageMs);
    }
    void rtcAnchorMs(uint64_t utcMs, uint32_t now, int64_t ageMs) {
        uint64_t at = uptime(now);
        rtc_.put({utcMs, at, ageMs < 0 ? 0 : uint64_t(ageMs), now, ageMs < 0 ? 1u : 2u});
    }
    void invalidateRtc() { rtc_.put({}); }
    void fill(EventInput& event, uint32_t now, bool clockSet, uint64_t systemMs) const {
        event.timeSource = EventTimeSource::Unknown; event.utcMs = event.syncAgeMs = -1;
        uint64_t at = uptime(now);
        Value g, s;
        if (clockSet) {
            if (!gps_.get(g) || !sntp_.get(s)) return;
            if (!g.valid && !s.valid) return;
            bool useGps = g.valid && (!s.valid || g.a > s.a ||
                          (g.a == s.a && latest_.load() == uint32_t(EventTimeSource::Gps)));
            event.timeSource = useGps ? EventTimeSource::Gps : EventTimeSource::Sntp;
            uint64_t synced = useGps ? g.a : s.a;
            event.utcMs = int64_t(systemMs); event.syncAgeMs = at >= synced ? int64_t(at - synced) : 0;
        } else {
            Value r; if (!rtc_.get(r) || !r.valid) return;
            uint64_t elapsed = at >= r.b ? at - r.b : 0;
            event.timeSource = EventTimeSource::Rtc; event.utcMs = int64_t(r.a + elapsed);
            if (r.valid == 2) event.syncAgeMs = int64_t(r.c + elapsed);
        }
    }
};
} // namespace diagnostics
