#include "event_log.h"
#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>

namespace diagnostics {
namespace {
void copyToken(char* out, size_t capacity, const char* value) {
    size_t n = 0;
    if (value) while (n < capacity && value[n]) ++n;
    if (!value || n == capacity) { out[0] = 0; return; }
    std::memcpy(out, value, n + 1);
}
bool terminated(const char* value, size_t capacity) {
    return std::memchr(value, 0, capacity) != nullptr;
}
bool bootToken(const char* value) {
    if (!value || std::strlen(value) != 32) return false;
    for (size_t i = 0; i < 32; ++i)
        if (!((value[i] >= '0' && value[i] <= '9') || (value[i] >= 'a' && value[i] <= 'f'))) return false;
    return true;
}
bool versionToken(const char* value) {
    if (!value || !value[0] || std::strlen(value) > 32) return false;
    for (size_t i = 0; value[i]; ++i) {
        char c = value[i];
        bool alnum = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
        if (!alnum && (!i || !std::strchr("._+-", c))) return false;
    }
    return true;
}
// Context schemas are deliberately local to the journal, not changes to the
// catalog generator. The host fixture exercises every field against Python's
// authoritative catalog, so a future schema change needs an explicit review.
struct Field {
    uint16_t code; const char* key; EventContext::Type type;
    int64_t min, max; const char* values;
};
using Type = EventContext::Type;
#define NUMBER(code, key, lo, hi) {code, key, Type::Integer, lo, hi, ""}
#define ENUM(code, key, values) {code, key, Type::Enum, 0, 0, values}
#define BOOL(code, key) {code, key, Type::Boolean, 0, 1, ""}
#define BACKEND(code) ENUM(code, "backend", "sd|littlefs|ram|none")
#define HTTP(code) NUMBER(code, "http_status", 100, 599), ENUM(code, "target", "trip|events")
const Field fields[] = {
    ENUM(1000, "reset_reason", "power_on|deep_sleep|software|watchdog|brownout|panic|other"),
    NUMBER(1007, "free_bytes", 0, 1048576),
    NUMBER(2002, "battery_mv", 0, 6000), NUMBER(2003, "battery_mv", 0, 6000),
    NUMBER(2004, "temperature_mc", -50000, 150000), NUMBER(2005, "temperature_mc", -50000, 150000),
    NUMBER(2007, "clock_count", 0, 27), NUMBER(2007, "attempt_count", 1, 3),
    ENUM(2007, "recovery_state", "not_needed|recovered|sda_stuck|scl_stuck"),
    BOOL(2007, "pmu_ready"), BOOL(2007, "wire_end_failed"),
    NUMBER(3001, "elapsed_ms", 0, INT64_MAX), NUMBER(3002, "elapsed_ms", 0, INT64_MAX),
    NUMBER(3002, "satellites", 0, 255),
    NUMBER(3003, "message_class", 0, 255), NUMBER(3003, "message_id", 0, 255),
    NUMBER(3004, "message_class", 0, 255), NUMBER(3004, "message_id", 0, 255),
    NUMBER(3005, "elapsed_ms", 0, INT64_MAX),
    ENUM(3008, "reason", "stopped|invalid_date|out_of_range|read_failed"),
    NUMBER(3009, "agc_pct", 0, 100), NUMBER(3009, "max_cno", 0, 99),
    BACKEND(4004), NUMBER(4004, "free_bytes", 0, INT64_MAX),
    BACKEND(4005), NUMBER(4005, "free_bytes", 0, INT64_MAX),
    BACKEND(4006), ENUM(4006, "operation", "open|write|close|rename"),
    BACKEND(4007), ENUM(4007, "reason", "read|parse|write|rename"),
    BACKEND(4008), ENUM(4008, "operation", "open|write|close|rename|ack"),
    NUMBER(4009, "count", 0, INT32_MAX), BACKEND(4009),
    BACKEND(4010), NUMBER(4010, "points", 0, INT32_MAX),
    NUMBER(5002, "attempts", 0, 255), NUMBER(5003, "attempts", 0, 255),
    ENUM(5003, "target", "trip|events"),
    HTTP(5004), HTTP(5005), HTTP(5006), HTTP(5007), HTTP(5008), HTTP(5009),
    ENUM(5010, "reason", "parse|missing_ids|unexpected_ids|duplicate_ids")
};
#undef NUMBER
#undef ENUM
#undef BOOL
#undef BACKEND
#undef HTTP
bool validContext(uint16_t code, const EventContext& context) {
    if (!terminated(context.key, sizeof(context.key)) || !terminated(context.value, sizeof(context.value))) return false;
    for (const auto& field : fields) {
        if (code != field.code || std::strcmp(context.key, field.key)) continue;
        if (context.type != field.type) return false;
        if (field.type != Type::Enum) return context.number >= field.min && context.number <= field.max;
        const char* at = field.values;
        size_t length = std::strlen(context.value);
        while (*at) {
            const char* end = std::strchr(at, '|');
            size_t n = end ? static_cast<size_t>(end - at) : std::strlen(at);
            if (length == n && !std::strncmp(at, context.value, n)) return true;
            at += n + (end ? 1 : 0);
        }
        return false;
    }
    return false;
}
struct Writer {
    char* data; size_t used = 0; bool ok = true;
    explicit Writer(char* out) : data(out) {}
    void add(const char* format, ...) {
        if (!ok) return;
        va_list args; va_start(args, format);
        int n = std::vsnprintf(data + used, kEventBytes + 1 - used, format, args);
        va_end(args);
        if (n < 0 || static_cast<size_t>(n) > kEventBytes - used) { ok = false; return; }
        used += static_cast<size_t>(n);
    }
};
void put64(uint8_t* data, uint64_t value) {
    for (unsigned i = 0; i < 8; ++i) data[i] = static_cast<uint8_t>(value >> (8 * i));
}
uint64_t get64(const uint8_t* data) {
    uint64_t value = 0;
    for (unsigned i = 0; i < 8; ++i) value |= static_cast<uint64_t>(data[i]) << (8 * i);
    return value;
}
void put32(uint8_t* data, uint32_t value) {
    for (unsigned i = 0; i < 4; ++i) data[i] = static_cast<uint8_t>(value >> (8 * i));
}
uint32_t get32(const uint8_t* data) {
    uint32_t value = 0;
    for (unsigned i = 0; i < 4; ++i) value |= static_cast<uint32_t>(data[i]) << (8 * i);
    return value;
}
uint64_t sum(uint64_t a, uint64_t b) { return b > kMaxEventInteger - a ? kMaxEventInteger : a + b; }
bool sameId(const EventIdentity& a, const EventIdentity& b) {
    return a.sequence == b.sequence && !std::memcmp(a.bootId, b.bootId, sizeof(a.bootId));
}
const char* metaPath(uint64_t generation) { return generation % 2 ? "/diag/ack-1" : "/diag/ack-0"; }
} // namespace

EventContext EventContext::integer(const char* key, int64_t value) {
    EventContext context; copyToken(context.key, sizeof(context.key), key); context.number = value; return context;
}
EventContext EventContext::boolean(const char* key, bool value) {
    EventContext context = integer(key, value); context.type = Type::Boolean; return context;
}
EventContext EventContext::enumeration(const char* key, const char* value) {
    EventContext context; copyToken(context.key, sizeof(context.key), key);
    context.type = Type::Enum; copyToken(context.value, sizeof(context.value), value); return context;
}
EventLog::EventLog(EventLogFs* sd, EventLogFs* littleFs) : EventLog(sd, littleFs, Config()) {}
EventLog::EventLog(EventLogFs* sd, EventLogFs* littleFs, Config config) : config_(config) {
    stores_[0].fs = sd; stores_[0].quota = std::min(config.sdQuota, kSdJournalQuota);
    stores_[1].fs = littleFs; stores_[1].quota = std::min(config.littleFsQuota, kLittleFsJournalReserve);
    stores_[0].unit = config.sdCluster; stores_[1].unit = config.littleFsBlock;
}
bool EventLog::begin(const char* bootId, uint64_t bootCounter, const char* firmwareVersion) {
    if (begun_ || !bootToken(bootId) || !versionToken(firmwareVersion) || bootCounter > kMaxEventInteger ||
        config_.segmentBytes < kFrameBytes || config_.segmentBytes > kJournalSegmentBytes ||
        !config_.sdCluster || !config_.littleFsBlock) return false;
    std::strcpy(bootId_, bootId); std::strcpy(version_, firmwareVersion); bootCounter_ = bootCounter;
    begun_ = true; return true;
}
uint32_t EventLog::crc32(const void* bytes, size_t size) {
    const auto* data = static_cast<const uint8_t*>(bytes);
    uint32_t crc = 0xffffffff;
    for (size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
    }
    return ~crc;
}
bool EventLog::setSdAllocationUnit(uint32_t bytes) {
    if (stores_[0].loaded || stores_[1].loaded) return false;
    if (bytes) stores_[0].unit = bytes;
    else stores_[0].fs = nullptr;
    return true;
}
void EventLog::segmentPath(char* out, uint32_t id, bool ready) {
    std::snprintf(out, 40, "/diag/seg-%08lu.%s", static_cast<unsigned long>(id), ready ? "ready" : "open");
}
bool EventLog::encode(const EventInput& input, uint64_t sequence, Frame& frame) {
    auto transition = static_cast<unsigned>(input.transition), source = static_cast<unsigned>(input.timeSource);
    const auto* definition = find(input.code);
    if (!begun_ || !sequence || sequence > kMaxEventInteger || input.code < 1000 || input.code > 6999 ||
        input.contextCount > 8 || transition > 3 || source > 3 || input.uptimeMs > kMaxEventInteger ||
        input.utcMs < -1 || input.utcMs > 253402300799999LL || input.syncAgeMs < -1 ||
        ((source == 0) != (input.utcMs == -1)) || (!source && input.syncAgeMs != -1)) return false;
    if (definition && (definition->journal == Journal::Counter ||
        ((definition->journal == Journal::PerOccurrence) != (transition == 0)))) return false;
    if ((transition == 0 && input.episodeSequence) ||
        (transition == 1 && input.episodeSequence) ||
        (transition >= 2 && (!input.episodeSequence || input.episodeSequence >= sequence))) return false;
    for (size_t i = 0; i < input.contextCount; ++i) {
        if (!validContext(input.code, input.context[i])) return false;
        for (size_t j = 0; j < i; ++j)
            if (!std::strcmp(input.context[i].key, input.context[j].key)) return false;
    }
    Writer out(frame.data);
    out.add("{\"schema_version\":1,\"catalog_version\":%u,\"firmware_version\":\"%s\",\"boot_id\":\"%s\","
            "\"boot_counter\":%llu,\"sequence\":%llu,\"code\":%u,\"transition\":\"%s\",\"episode_id\":",
            kCatalogVersion, version_, bootId_, static_cast<unsigned long long>(bootCounter_),
            static_cast<unsigned long long>(sequence), input.code,
            transition == 0 ? "notice" : transition == 1 ? "raise" : transition == 2 ? "repeat" : "clear");
    if (!transition) out.add("null");
    else out.add("\"%s:%llu\"", bootId_, static_cast<unsigned long long>(transition == 1 ? sequence : input.episodeSequence));
    out.add(",\"uptime_ms\":%llu,\"utc_ms\":", static_cast<unsigned long long>(input.uptimeMs));
    if (input.utcMs == -1) out.add("null"); else out.add("%lld", static_cast<long long>(input.utcMs));
    out.add(",\"time_source\":\"%s\",\"sync_age_ms\":", source == 0 ? "unknown" : source == 1 ? "rtc" : source == 2 ? "gps" : "sntp");
    if (input.syncAgeMs == -1) out.add("null"); else out.add("%lld", static_cast<long long>(input.syncAgeMs));
    out.add(",\"context\":{");
    for (size_t i = 0; i < input.contextCount; ++i) {
        const auto& context = input.context[i];
        out.add("%s\"%s\":", i ? "," : "", context.key);
        if (context.type == Type::Integer) out.add("%lld", static_cast<long long>(context.number));
        else if (context.type == Type::Boolean) out.add("%s", context.number ? "true" : "false");
        else out.add("\"%s\"", context.value);
    }
    out.add("}}");
    if (!out.ok) return false;
    uint32_t crc = crc32(frame.data, out.used);
    std::snprintf(frame.data + out.used, 11, "\t%08lx\n", static_cast<unsigned long>(crc));
    frame.size = static_cast<uint16_t>(out.used + 10);
    frame.loss = input.code == static_cast<uint16_t>(Code::EVENTS_DROPPED);
    return true;
}
void EventLog::drop() {
    uint32_t old = queueDrops_.load(std::memory_order_relaxed);
    // Bounded atomic accounting, never a retry loop in a producer. Saturate
    // well before wrap; concurrent increments at the threshold remain safe.
    if (old < UINT32_MAX / 2) queueDrops_.fetch_add(1, std::memory_order_relaxed);
}
EventLog::Submit EventLog::submit(const EventInput& input, EventIdentity* id) {
    if (queueLock_.test_and_set(std::memory_order_acquire)) { drop(); return Submit::Busy; }
    Submit result;
    if (!begun_) result = Submit::Invalid;
    else if (count_ == kEventQueueSize) { drop(); result = Submit::Full; }
    else if (sequence_ == kMaxEventInteger) result = Submit::Exhausted;
    else if (!encode(input, sequence_ + 1, queue_[(head_ + count_) % kEventQueueSize])) result = Submit::Invalid;
    else {
        ++sequence_; ++count_;
        if (id) { std::strcpy(id->bootId, bootId_); id->sequence = sequence_; }
        result = Submit::Accepted;
    }
    queueLock_.clear(std::memory_order_release); return result;
}
void EventLog::fail() { if (failures_ != UINT32_MAX) ++failures_; } // no sink callback/recursive event

uint64_t EventLog::allocated(const Store& store, uint32_t bytes) const {
    return ((static_cast<uint64_t>(bytes) + store.unit - 1) / store.unit) * store.unit;
}
bool EventLog::inventory(Store& store) {
    store.count = 0;
    uint64_t reserved = 2 * allocated(store, kMetaBytes);
    if (config_.reserveRejected) reserved += allocated(store, kFrameBytes);
    if (reserved > UINT32_MAX) return false;
    store.used = static_cast<uint32_t>(reserved); // reserve both slots even when absent
    store.metaSizes[0] = store.metaSizes[1] = kMetaBytes;
    for (size_t i = 0; i <= 1024; ++i) {
        EventLogFs::Entry entry{};
        auto result = store.fs->list(i, entry);
        if (result == EventLogFs::Result::End) {
            std::sort(store.segments, store.segments + store.count,
                      [](const Segment& a, const Segment& b) { return a.id < b.id; });
            for (size_t j = 1; j < store.count; ++j)
                if (store.segments[j].id == store.segments[j - 1].id) return false;
            return true;
        }
        if (result == EventLogFs::Result::Error || i == 1024 || !terminated(entry.name, sizeof(entry.name))) return false;
        if (!std::strcmp(entry.name, "ack-0") || !std::strcmp(entry.name, "ack-1")) {
            unsigned slot = entry.name[4] - '0';
            store.metaSizes[slot] = std::max<uint32_t>(entry.size, kMetaBytes);
            uint64_t extra = allocated(store, store.metaSizes[slot]) - allocated(store, kMetaBytes);
            if (extra > UINT32_MAX - store.used) return false;
            store.used += static_cast<uint32_t>(extra);
            continue;
        }
        uint64_t bytes = allocated(store, entry.size);
        if (config_.reserveRejected && !std::strcmp(entry.name, "rejected"))
            bytes -= std::min(bytes, allocated(store, kFrameBytes));
        if (bytes > UINT32_MAX - store.used) return false;
        store.used += static_cast<uint32_t>(bytes);
        size_t length = std::strlen(entry.name);
        if (length != 17 && length != 18) continue;
        if (std::strncmp(entry.name, "seg-", 4)) continue;
        uint32_t id = 0;
        bool digits = true;
        for (size_t j = 4; j < 12; ++j) {
            if (entry.name[j] < '0' || entry.name[j] > '9') { digits = false; break; }
            id = id * 10 + entry.name[j] - '0';
        }
        bool ready = !std::strcmp(entry.name + 12, ".ready");
        if (!digits || !id || (!ready && std::strcmp(entry.name + 12, ".open"))) continue;
        if (store.count == kMaxSegments || entry.size > kJournalSegmentBytes) return false;
        store.segments[store.count++] = {id, entry.size, ready};
    }
    return false;
}
bool EventLog::load(Store& store) {
    if (store.loaded) return true;
    if (!store.fs || !store.fs->begin() || !inventory(store)) return false;
    Meta best;
    for (unsigned slot = 0; slot < 2; ++slot) {
        uint8_t data[kMetaBytes];
        if (!store.fs->read(metaPath(slot), 0, data, sizeof(data)) ||
            std::memcmp(data, "ELG1", 4) || crc32(data, 76) != get32(data + 76)) continue;
        Meta m;
        m.generation = get64(data + 8); m.ack = {get32(data + 16), get32(data + 20)};
        m.next = get32(data + 24); m.revision = get64(data + 32);
        m.dropped = get64(data + 40); m.pending = get64(data + 48);
        m.discarded = get32(data + 56); m.revisionReady = data[60] == 1;
        if (!m.generation || m.generation % 2 != slot || m.generation > kMaxEventInteger ||
            m.ack.segment > 99999999 || m.ack.offset > kJournalSegmentBytes ||
            !m.next || m.next > 100000000 || m.revision > kMaxEventInteger ||
            m.dropped > kMaxEventInteger || m.pending > m.dropped || m.discarded > 99999999 || data[60] > 1) continue;
        if (m.generation > best.generation) best = m;
    }
    store.meta = best;
    if (store.count) store.meta.next = std::max(store.meta.next, store.segments[store.count - 1].id + 1);
    store.loaded = true; return true;
}
bool EventLog::save(Store& store, Meta next) {
    if (!load(store)) return false;
    if (store.meta.generation == kMaxEventInteger || store.quota < 2 * allocated(store, kMetaBytes)) return false;
    next.generation = store.meta.generation + 1;
    uint8_t data[kMetaBytes] = {}, check[kMetaBytes];
    std::memcpy(data, "ELG1", 4); put64(data + 8, next.generation);
    put32(data + 16, next.ack.segment); put32(data + 20, next.ack.offset); put32(data + 24, next.next);
    put64(data + 32, next.revision); put64(data + 40, next.dropped); put64(data + 48, next.pending);
    put32(data + 56, next.discarded); data[60] = next.revisionReady ? 1 : 0;
    put32(data + 76, crc32(data, 76));
    if (!store.fs->write(metaPath(next.generation), data, sizeof(data), false) ||
        !store.fs->read(metaPath(next.generation), 0, check, sizeof(check)) || std::memcmp(data, check, sizeof(data))) {
        store.loaded = false; return false;
    }
    unsigned slot = next.generation % 2;
    store.used -= static_cast<uint32_t>(allocated(store, store.metaSizes[slot]) - allocated(store, kMetaBytes));
    store.metaSizes[slot] = kMetaBytes;
    store.meta = next; return true;
}
bool EventLog::acknowledged(const Store& store, const Segment& segment) const {
    return segment.id < store.meta.ack.segment ||
           (segment.id == store.meta.ack.segment && segment.size <= store.meta.ack.offset);
}
// Scan a physical frame boundary with a 512-byte buffer. Oversize damaged
// lines are consumed in bounded chunks until LF; none of their suffixes count
// as a frame. The enclosing segment scan is capped at 64 KiB.
bool EventLog::readFrame(Store& store, const Segment& segment, uint32_t& offset, Frame& frame) {
    frame.size = 0;
    if (offset >= segment.size) return true;
    char path[40]; segmentPath(path, segment.id, segment.ready);
    size_t used = 0; bool overflow = false, ended = false;
    while (offset < segment.size) {
        char block[kFrameBytes];
        size_t n = std::min<size_t>(sizeof(block), segment.size - offset);
        if (!store.fs->read(path, offset, block, n)) { store.loaded = false; return false; }
        size_t take = 0;
        for (; take < n; ++take) {
            char c = block[take];
            if (used < kFrameBytes) frame.data[used++] = c; else overflow = true;
            if (c == '\n') { ++take; ended = true; break; }
        }
        offset += static_cast<uint32_t>(take);
        if (ended) break;
    }
    if (!ended || overflow || used < 12 || frame.data[used - 10] != '\t') return true;
    uint32_t expected = 0;
    for (size_t i = used - 9; i < used - 1; ++i) {
        char c = frame.data[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return true;
        expected = expected * 16 + (c <= '9' ? c - '0' : c - 'a' + 10);
    }
    if (expected != crc32(frame.data, used - 10)) return true;
    frame.size = static_cast<uint16_t>(used); frame.data[used] = 0;
    frame.loss = std::strstr(frame.data, "\"code\":4009,") != nullptr;
    return true;
}
uint64_t EventLog::lostIn(Store& store, const Segment& segment) {
    uint32_t offset = segment.id == store.meta.ack.segment ? store.meta.ack.offset : 0;
    uint64_t lost = 0;
    while (offset < segment.size) {
        Frame frame;
        if (!readFrame(store, segment, offset, frame)) return UINT64_MAX;
        if (!frame.size || !frame.loss) ++lost;
    }
    return lost;
}
bool EventLog::makeRoom(Store& store, size_t bytes) {
    if (bytes + 2 * allocated(store, kMetaBytes) > store.quota) return false;
    // Reclaim only sealed, acknowledged data normally. A discarded segment was
    // explicitly loss-accounted BEFORE deleting, so a reset can finish deletion.
    for (size_t i = 0; i < store.count;) {
        const auto& s = store.segments[i];
        bool discarded = s.id <= store.meta.discarded;
        bool reclaim = s.ready && acknowledged(store, s);
        if (!discarded && !reclaim && static_cast<uint64_t>(store.used) + bytes <= store.quota) { ++i; continue; }
        if (!discarded && !reclaim) {
            // Never evict an actively appended .open file. Seal it first.
            if (!s.ready) return false;
            uint64_t lost = lostIn(store, s);
            if (lost == UINT64_MAX) return false;
            Meta next = store.meta; next.discarded = s.id;
            next.dropped = sum(next.dropped, lost); next.pending = sum(next.pending, lost);
            if (!save(store, next)) return false;
        }
        char path[40]; segmentPath(path, s.id, s.ready);
        if (!store.fs->remove(path)) { store.loaded = false; return false; }
        issued_ = 0; // a batch referencing removed storage must be read again
        store.used -= static_cast<uint32_t>(allocated(store, s.size));
        std::move(store.segments + i + 1, store.segments + store.count, store.segments + i);
        --store.count;
    }
    return static_cast<uint64_t>(store.used) + bytes <= store.quota;
}
bool EventLog::append(Store& store, const Frame& frame) {
    if (!load(store)) return false;
    // At boot or after a failed append, never append to an uncertain tail. A
    // complete valid tail may be reused across boots; a torn one is sealed.
    Segment* open = nullptr;
    for (size_t i = 0; i < store.count; ++i) {
        auto& segment = store.segments[i];
        if (segment.ready) continue;
        uint32_t offset = 0; bool clean = true;
        while (!segment.clean && offset < segment.size) {
            Frame existing;
            if (!readFrame(store, segment, offset, existing)) return false;
            if (!existing.size) clean = false;
        }
        segment.clean = clean;
        bool keep = clean && segment.id > store.meta.discarded &&
                    segment.size + frame.size <= config_.segmentBytes && i + 1 == store.count;
        if (keep) { open = &segment; continue; }
        char from[40], to[40]; segmentPath(from, segment.id, false); segmentPath(to, segment.id, true);
        if (!store.fs->rename(from, to)) { store.loaded = false; return false; }
        segment.ready = true;
    }
    // Under the 64 KiB flash quota the segment itself must leave room for both
    // metadata slots. Roll early instead of filling the entire reserve.
    uint32_t oldSize = open ? open->size : 0;
    uint64_t growth = allocated(store, oldSize + frame.size) - allocated(store, oldSize);
    if (open && (static_cast<uint64_t>(store.used) + growth > store.quota)) {
        char from[40], to[40]; segmentPath(from, open->id, false); segmentPath(to, open->id, true);
        if (!store.fs->rename(from, to)) { store.loaded = false; return false; }
        open->ready = true; open = nullptr;
        oldSize = 0; growth = allocated(store, frame.size);
    }
    uint32_t openId = open ? open->id : 0;
    if (!makeRoom(store, static_cast<size_t>(growth))) return false;
    if (!openId) {
        if (store.meta.next > 99999999 || store.count == kMaxSegments) return false;
        openId = store.meta.next;
        Meta next = store.meta; ++next.next;
        // Persist the filename reservation before creation, including after all
        // older segments have been reclaimed. Counter exhaustion never wraps.
        if (!save(store, next)) return false;
    }
    char path[40]; segmentPath(path, openId, false);
    if (!store.fs->write(path, frame.data, frame.size, true)) { store.loaded = false; return false; }
    store.used += static_cast<uint32_t>(growth);
    if (!open) store.segments[store.count++] = {openId, frame.size, false, true};
    else store.segments[store.count - 1].size = oldSize + frame.size;
    return true;
}

void EventLog::service(size_t maxRecords) {
    if (!begun_) return;
    uint32_t drops = queueDrops_.exchange(0, std::memory_order_relaxed);
    ramPending_ = sum(ramPending_, drops);
    for (size_t n = 0; n < std::min(maxRecords, kEventQueueSize); ++n) {
        Frame frame;
        if (queueLock_.test_and_set(std::memory_order_acquire)) break;
        bool hasFrame = count_ != 0;
        if (hasFrame) frame = queue_[head_];
        queueLock_.clear(std::memory_order_release);
        if (!hasFrame) break;
        if (!headStored_) {
            backend_ = Backend::Ram;
            for (unsigned i = 0; i < 2; ++i) {
                if (append(stores_[i], frame)) { backend_ = static_cast<Backend>(i); headStored_ = true; break; }
                if (stores_[i].fs) fail();
            }
        }
        if (!headStored_) break; // preserve bounded RAM, retry next service step
        if (issuedBackend_ == Backend::Ram) issued_ = 0;
        // Producers only append, so the copied head cannot have changed.
        // Never spin: a higher-priority service task could otherwise starve the
        // producer holding the flag on the same core. Defer removal, not writes.
        if (queueLock_.test_and_set(std::memory_order_acquire)) break;
        head_ = (head_ + 1) % kEventQueueSize; --count_;
        headStored_ = false;
        queueLock_.clear(std::memory_order_release);
    }
    // Persist queued drop accounting on the first writable backend. Never count
    // an unsuccessful loss notice as another lost event. Failed writes may leave
    // a valid duplicate with the SAME identity; the server dedupes it.
    for (unsigned i = 0; i < 2; ++i) {
        Store& store = stores_[i];
        if (!load(store)) continue;
        if (ramPending_) {
            Meta next = store.meta; next.dropped = sum(next.dropped, ramPending_);
            next.pending = sum(next.pending, ramPending_);
            if (!save(store, next)) { fail(); continue; }
            ramPending_ = 0;
        }
        if (!store.meta.pending) continue;
        if (!lossReported_[i]) {
            uint64_t reported = std::min<uint64_t>(store.meta.pending, INT32_MAX);
            EventInput input; input.code = static_cast<uint16_t>(Code::EVENTS_DROPPED);
            stamp(input);
            input.contextCount = 2;
            input.context[0] = EventContext::integer("count", static_cast<int64_t>(reported));
            input.context[1] = EventContext::enumeration("backend", i == 0 ? "sd" : "littlefs");
            if (queueLock_.test_and_set(std::memory_order_acquire)) break;
            bool encoded = sequence_ < kMaxEventInteger && encode(input, sequence_ + 1, lossNotice_[i]);
            if (encoded) { ++sequence_; lossReported_[i] = reported; }
            queueLock_.clear(std::memory_order_release);
            if (!encoded) { fail(); break; }
        }
        if (!append(store, lossNotice_[i])) { fail(); break; }
        Meta next = store.meta; next.pending -= lossReported_[i];
        if (!save(store, next)) fail();
        else lossReported_[i] = 0;
        backend_ = static_cast<Backend>(i);
        break; // one notice at most per service step
    }
}
bool EventLog::identity(const Frame& frame, EventIdentity& id) {
    const char* boot = std::strstr(frame.data, "\"boot_id\":\"");
    const char* seq = std::strstr(frame.data, "\"sequence\":");
    if (!boot || !seq) return false;
    boot += 11; seq += 11;
    if (std::strlen(boot) < 33 || boot[32] != '"') return false;
    std::memcpy(id.bootId, boot, 32); id.bootId[32] = 0;
    if (!bootToken(id.bootId) || *seq < '1' || *seq > '9') return false;
    uint64_t number = 0;
    while (*seq >= '0' && *seq <= '9') {
        unsigned digit = *seq++ - '0';
        if (number > (kMaxEventInteger - digit) / 10) return false;
        number = number * 10 + digit;
    }
    if (*seq != ',') return false;
    id.sequence = number; return true;
}
bool EventLog::nextBatch(Batch& batch, size_t maxEvents, size_t byteBudget) {
    batch.count = 0; batch.bytes = 2; batch.ticket = 0; issued_ = 0;
    maxEvents = std::min<size_t>(64, maxEvents); byteBudget = std::min<size_t>(30 * 1024, byteBudget);
    if (!begun_ || !maxEvents || byteBudget < 2 || ticket_ == kMaxEventInteger) return false;
    for (unsigned i = 0; i < 2; ++i) {
        Store& store = stores_[i];
        if (!load(store)) continue;
        uint64_t corrupt = 0;
        for (size_t j = 0; j < store.count; ++j) {
            const auto& segment = store.segments[j];
            if (segment.id <= store.meta.discarded || acknowledged(store, segment)) continue;
            uint32_t offset = segment.id == store.meta.ack.segment ? store.meta.ack.offset : 0;
            while (offset < segment.size) {
                Frame frame;
                if (!readFrame(store, segment, offset, frame)) { fail(); return false; }
                EventIdentity id;
                if (!frame.size || !identity(frame, id)) { ++corrupt; continue; }
                bool duplicate = false;
                for (size_t k = 0; k < batch.count; ++k) duplicate |= sameId(id, batch.ids[k]);
                if (duplicate) { issuedEnd_ = {segment.id, offset}; issuedCorrupt_ = corrupt; continue; }
                size_t bytes = frame.size - 10;
                if (batch.count == maxEvents || batch.bytes + bytes + (batch.count ? 1 : 0) > byteBudget) goto done;
                std::memcpy(batch.events[batch.count], frame.data, bytes); batch.events[batch.count][bytes] = 0;
                batch.ids[batch.count++] = id; batch.bytes += bytes + (batch.count > 1 ? 1 : 0);
                issuedEnd_ = {segment.id, offset};
                issuedCorrupt_ = corrupt;
            }
        }
done:
        if (batch.count) { issuedBackend_ = static_cast<Backend>(i); goto issue; }
    }
    // RAM-only upload is possible even with no filesystem. Ack removes only
    // these exact head identities; service migration invalidates the ticket.
    if (queueLock_.test_and_set(std::memory_order_acquire)) return false;
    for (size_t j = 0; j < count_ && batch.count < maxEvents; ++j) {
        const auto& frame = queue_[(head_ + j) % kEventQueueSize];
        size_t bytes = frame.size - 10;
        if (batch.bytes + bytes + (batch.count ? 1 : 0) > byteBudget) break;
        if (!identity(frame, batch.ids[batch.count])) break;
        std::memcpy(batch.events[batch.count], frame.data, bytes); batch.events[batch.count][bytes] = 0;
        ++batch.count; batch.bytes += bytes + (batch.count > 1 ? 1 : 0);
    }
    queueLock_.clear(std::memory_order_release); issuedBackend_ = Backend::Ram;
issue:
    if (!batch.count) return true;
    batch.ticket = issued_ = ++ticket_; issuedCount_ = batch.count;
    std::copy(batch.ids, batch.ids + batch.count, issuedIds_);
    return true;
}
bool EventLog::acknowledge(const Batch& batch, const EventIdentity* accepted, size_t count) {
    if (!issued_ || batch.ticket != issued_ || count != issuedCount_ || batch.count != issuedCount_ || !accepted) return false;
    for (size_t i = 0; i < count; ++i) {
        if (!sameId(batch.ids[i], issuedIds_[i])) return false;
        size_t matches = 0;
        for (size_t j = 0; j < count; ++j) matches += sameId(issuedIds_[i], accepted[j]) ? 1 : 0;
        if (matches != 1) return false;
    }
    if (issuedBackend_ == Backend::Ram) {
        if (queueLock_.test_and_set(std::memory_order_acquire)) return false;
        bool valid = count_ >= count;
        for (size_t i = 0; valid && i < count; ++i) {
            EventIdentity id;
            valid = identity(queue_[(head_ + i) % kEventQueueSize], id) && sameId(id, issuedIds_[i]);
        }
        if (valid) { head_ = (head_ + count) % kEventQueueSize; count_ -= count; headStored_ = false; }
        queueLock_.clear(std::memory_order_release);
        if (!valid) return false;
    } else {
        Store& store = stores_[static_cast<unsigned>(issuedBackend_)];
        if (!load(store)) { fail(); return false; }
        Meta next = store.meta; next.ack = issuedEnd_;
        // Corruption is explicit loss, never successful delivery. Persist the
        // loss with the cursor before allowing normal segment reclamation.
        next.dropped = sum(next.dropped, issuedCorrupt_);
        next.pending = sum(next.pending, issuedCorrupt_);
        if (!save(store, next)) { fail(); return false; }
    }
    issued_ = 0; return true;
}
bool EventLog::isolateRejected(const Batch& batch) {
    if (!issued_ || batch.ticket != issued_ || batch.count != 1 || issuedCount_ != 1 ||
        !sameId(batch.ids[0], issuedIds_[0])) return false;
    // A volatile head cannot atomically move with a persistent loss cursor.
    // Let normal service migrate it to media before narrowing/retrying again.
    if (issuedBackend_ == Backend::Ram) return false;
    Frame frame;
    size_t bytes = std::strlen(batch.events[0]);
    if (bytes > kEventBytes) return false;
    std::memcpy(frame.data, batch.events[0], bytes);
    std::snprintf(frame.data + bytes, sizeof(frame.data) - bytes, "\t%08x\n",
                  unsigned(crc32(frame.data, bytes)));
    frame.size = bytes + 10;
    bool retained = false;
    for (auto& store : stores_) {
        if (!load(store)) continue;
        // Stage 4 reserves this allocation even before the inspection file
        // exists. Standalone callers without that reserve need free space.
        if (uint64_t(store.used) + (config_.reserveRejected ? 0 : allocated(store, frame.size)) > store.quota) continue;
        if (!store.fs->write("/diag/rejected", frame.data, frame.size, false)) {
            store.loaded = false; continue;
        }
        Frame verify;
        retained = store.fs->read("/diag/rejected", 0, verify.data, frame.size) &&
                   !std::memcmp(frame.data, verify.data, frame.size);
        store.loaded = false;
        if (retained) break;
    }
    if (!retained) return false; // never discard without checked inspection copy
    // acknowledge() commits loss and cursor together, using its existing
    // read-back verification. This is explicit discarded delivery, not success.
    uint64_t corrupt = issuedCorrupt_;
    issuedCorrupt_ = sum(corrupt, 1);
    bool ok = acknowledge(batch, batch.ids, 1);
    if (!ok) issuedCorrupt_ = corrupt;
    return ok;
}
EventLog::Stats EventLog::stats() {
    Stats result; result.backend = backend_; result.storageFailures = failures_;
    result.dropped = ramPending_; result.pendingLoss = ramPending_;
    for (const auto& store : stores_) {
        result.dropped = sum(result.dropped, store.meta.dropped);
        result.pendingLoss = sum(result.pendingLoss, store.meta.pending);
    }
    result.dropped = sum(result.dropped, queueDrops_.load(std::memory_order_relaxed));
    if (!queueLock_.test_and_set(std::memory_order_acquire)) {
        result.queued = count_; queueLock_.clear(std::memory_order_release);
    }
    return result;
}
EventLog::Backlog EventLog::backlog() {
    Backlog result;
    for (auto& store : stores_) {
        if (!store.fs) continue;
        if (!load(store)) { result.complete = false; continue; }
        for (size_t i = 0; i < store.count; ++i) {
            auto& segment = store.segments[i];
            if (segment.id <= store.meta.discarded || acknowledged(store, segment)) continue;
            ++result.segments;
            uint32_t offset = segment.id == store.meta.ack.segment ? store.meta.ack.offset : 0;
            while (offset < segment.size) {
                Frame frame;
                if (!readFrame(store, segment, offset, frame)) { result.complete = false; break; }
                if (frame.size) ++result.records;
            }
        }
    }
    result.records += stats().queued;
    return result;
}
bool EventLog::seal() {
    service(kEventQueueSize);
    bool ok = stats().queued == 0;
    for (auto& store : stores_) {
        if (!store.fs) continue;
        if (!load(store)) { ok = false; continue; }
        for (size_t i = 0; i < store.count; ++i) {
            auto& segment = store.segments[i];
            if (segment.ready) continue;
            char from[40], to[40];
            segmentPath(from, segment.id, false); segmentPath(to, segment.id, true);
            if (!store.fs->rename(from, to)) { store.loaded = false; ok = false; break; }
            segment.ready = true;
        }
    }
    return ok;
}
void EventLog::invalidateStorage() {
    for (auto& store : stores_) store.loaded = false;
    issued_ = 0;
}
bool EventLog::restoreStatusSequence(uint64_t highWater) {
    auto& store = stores_[0];
    if (!begun_ || highWater > kMaxEventInteger || !load(store) || highWater < store.meta.revision) return false;
    Meta next = store.meta; next.revision = highWater; next.revisionReady = true; return save(store, next);
}
bool EventLog::allocateStatusSequence(uint64_t& sequence) {
    auto& store = stores_[0];
    if (!begun_ || !load(store) || !store.meta.revisionReady || store.meta.revision == kMaxEventInteger) return false;
    Meta next = store.meta; ++next.revision;
    if (!save(store, next)) return false;
    sequence = next.revision; return true;
}
bool EventLog::suspendStatusSequence() {
    auto& store = stores_[0];
    if (!begun_ || !load(store)) return false;
    Meta next = store.meta; next.revisionReady = false;
    return save(store, next);
}
} // namespace diagnostics
