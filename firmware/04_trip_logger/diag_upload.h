// Bounded protocol helpers. All large owners must live in static DRAM/PSRAM.
#pragma once
#include "event_log.h"
#include <cstdarg>
#include <cstdio>
#include <cstring>

namespace diagnostics {
inline EventLog::Config uploadJournalConfig() {
    EventLog::Config config; config.reserveRejected = true; return config;
}
constexpr size_t kRequestBytes = 32 * 1024, kAckBytes = 8192;
constexpr uint32_t kDrainMs = 10000, kHeartbeatMs = 24 * 60 * 60 * 1000;
struct JsonWriter {
    char* data; size_t capacity, size = 0; bool ok = true;
    void add(const char* format, ...) {
        if (!ok) return;
        va_list args; va_start(args, format);
        int n = std::vsnprintf(data + size, capacity - size, format, args);
        va_end(args);
        if (n < 0 || size_t(n) >= capacity - size) { ok = false; return; }
        size += size_t(n);
    }
};

// Strict bounded acknowledgment parser: no general JSON tree, dynamic storage,
// raw reply logging, or integer conversion that can overflow. Key order and ID
// order may vary. Unknown/duplicate keys, IDs and trailing bytes are rejected.
class AckParser {
    const char* p_; const char* end_;
    bool token(char c) { ws(); if (p_ == end_ || *p_ != c) return false; ++p_; return true; }
    void ws() { while (p_ != end_ && (*p_ == ' ' || *p_ == '\n' || *p_ == '\r' || *p_ == '\t')) ++p_; }
    bool text(char* out, size_t capacity) {
        if (!token('"')) return false;
        size_t n = 0;
        while (p_ != end_ && *p_ != '"') {
            unsigned char c = *p_++;
            if (c < 32 || c > 126 || c == '\\' || n + 1 >= capacity) return false;
            out[n++] = char(c);
        }
        out[n] = 0; return token('"');
    }
    bool integer(uint64_t& n) {
        ws(); if (p_ == end_ || *p_ < '1' || *p_ > '9') return false;
        n = 0;
        while (p_ != end_ && *p_ >= '0' && *p_ <= '9') {
            unsigned d = *p_++ - '0';
            if (n > (kMaxEventInteger - d) / 10) return false;
            n = n * 10 + d;
        }
        return true;
    }
public:
    AckParser(const char* data, size_t size) : p_(data), end_(data + size) {}
    const char* parse(const EventLog::Batch& batch, size_t expectedCount) {
        char key[16]; bool seen[64] = {}; size_t count = 0;
        if (!token('{') || !text(key, sizeof(key)) || std::strcmp(key, "accepted") ||
            !token(':') || !token('[')) return "parse";
        ws();
        if (p_ != end_ && *p_ != ']') for (;;) {
            EventIdentity id; bool boot = false, seq = false;
            if (!token('{')) return "parse";
            for (unsigned field = 0; field < 2; ++field) {
                if (field && !token(',')) return "parse";
                if (!text(key, sizeof(key)) || !token(':')) return "parse";
                if (!std::strcmp(key, "boot_id") && !boot) {
                    if (!text(id.bootId, sizeof(id.bootId)) || std::strlen(id.bootId) != 32) return "parse";
                    for (unsigned i = 0; i < 32; ++i)
                        if (!((id.bootId[i] >= '0' && id.bootId[i] <= '9') ||
                              (id.bootId[i] >= 'a' && id.bootId[i] <= 'f'))) return "parse";
                    boot = true;
                } else if (!std::strcmp(key, "sequence") && !seq) {
                    if (!integer(id.sequence)) return "parse";
                    seq = true;
                } else return "parse";
            }
            if (!boot || !seq || !token('}')) return "parse";
            size_t index = 0;
            for (; index < expectedCount; ++index)
                if (id.sequence == batch.ids[index].sequence && !std::strcmp(id.bootId, batch.ids[index].bootId)) break;
            if (index == expectedCount) return "unexpected_ids";
            if (seen[index]) return "duplicate_ids";
            seen[index] = true; ++count;
            ws(); if (p_ == end_ || *p_ != ',') break;
            ++p_;
        }
        if (!token(']') || !token('}')) return "parse";
        ws(); if (p_ != end_) return "parse";
        return count == expectedCount ? nullptr : "missing_ids";
    }
};

struct UploadRetry {
    uint32_t lastAttempt = 0, delay = 0;
    size_t maxEvents = 64, eventBytes = 30 * 1024;
    bool ready(uint32_t now) const { return !delay || uint32_t(now - lastAttempt) >= delay; }
    void failed(uint32_t now, int status) {
        lastAttempt = now;
        uint32_t floor = status == 401 ? 60 * 60 * 1000u : 60000u;
        delay = delay ? (delay >= 1800000u ? 3600000u : delay * 2) : floor;
        if (delay < floor) delay = floor;
        if (status == 413) {
            maxEvents = maxEvents > 1 ? maxEvents / 2 : 1;
            eventBytes = eventBytes > 1024 ? eventBytes / 2 : 1024;
        }
        // Narrow first; never discard a rejected multi-record batch.
        if (status == 400 || status == 409) maxEvents = maxEvents > 1 ? maxEvents / 2 : 1;
    }
    void succeeded() { delay = 0; }
};
} // namespace diagnostics
