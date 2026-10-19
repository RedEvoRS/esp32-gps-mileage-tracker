// Fault-injected journal tests use only standard C++: no Arduino or real media.
#include "event_log.h"
#include <cassert>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
using namespace diagnostics;
static const char* boot1 = "00000000000000000000000000000001";
static const char* boot2 = "00000000000000000000000000000002";

struct MemoryFs : EventLogFs {
    std::map<std::string, std::string> files;
    bool mounted = true, readOnly = false, failOpen = false, failRename = false;
    bool failRemove = false, failRead = false, failList = false, failNotice = false, failClose = false;
    size_t capacity = SIZE_MAX, tear = SIZE_MAX;
    std::string tearPath;
    size_t writes = 0, calls = 0, lists = 0, reads = 0;
    size_t allocationUnit = 1, peakAllocated = 0;
    bool begin() override { ++calls; return mounted; }
    Result list(size_t index, Entry& entry) override {
        ++calls; ++lists;
        if (!mounted || failList) return Result::Error;
        if (index >= files.size()) return Result::End;
        auto it = files.begin(); std::advance(it, index);
        std::snprintf(entry.name, sizeof(entry.name), "%s", it->first.c_str() + 6);
        entry.size = static_cast<uint32_t>(it->second.size()); return Result::Ok;
    }
    bool read(const char* path, uint32_t offset, void* data, size_t size) override {
        ++calls; ++reads;
        auto it = files.find(path);
        if (!mounted || failRead || it == files.end() || offset + size > it->second.size()) return false;
        std::memcpy(data, it->second.data() + offset, size); return true;
    }
    size_t used() const { size_t bytes = 0; for (const auto& file : files) bytes += file.second.size(); return bytes; }
    size_t allocated() const {
        size_t bytes = 0;
        for (const auto& file : files) bytes += (file.second.size() + allocationUnit - 1) / allocationUnit * allocationUnit;
        return bytes;
    }
    bool write(const char* path, const void* data, size_t size, bool append) override {
        ++calls; ++writes;
        if (!mounted || readOnly || failOpen) return false;
        std::string value(static_cast<const char*>(data), size);
        if (failNotice && value.find("\"code\":4009,") != std::string::npos) return false;
        size_t n = size;
        bool torn = tear != SIZE_MAX && (tearPath.empty() || std::string(path).find(tearPath) != std::string::npos);
        if (torn) n = std::min(n, tear);
        size_t old = files.count(path) ? files[path].size() : 0;
        size_t base = used() - (append ? 0 : old);
        n = std::min(n, capacity > base ? capacity - base : 0);
        if (!append) files[path].clear();
        files[path].append(value.data(), n);
        peakAllocated = std::max(peakAllocated, allocated());
        if (torn) { tear = SIZE_MAX; tearPath.clear(); }
        if (failClose && std::string(path).find("seg-") != std::string::npos) { failClose = false; return false; }
        return n == size;
    }
    bool rename(const char* from, const char* to) override {
        ++calls;
        if (!mounted || readOnly || failRename || !files.count(from) || files.count(to)) return false;
        files[to] = files[from]; files.erase(from); return true;
    }
    bool remove(const char* path) override {
        ++calls;
        if (!mounted || readOnly || failRemove) return false;
        return files.erase(path) == 1;
    }
    std::vector<std::string> segments() const {
        std::vector<std::string> result;
        for (const auto& pair : files) if (pair.first.find("/diag/seg-") == 0) result.push_back(pair.first);
        return result;
    }
};
static std::unique_ptr<EventLog> logger(MemoryFs* sd, MemoryFs* flash = nullptr,
                                      const char* boot = boot1, uint32_t quota = kSdJournalQuota,
                                      uint32_t segment = kJournalSegmentBytes) {
    EventLog::Config config; config.sdQuota = quota; config.segmentBytes = segment;
    config.sdCluster = 1; // byte-granular synthetic quotas; physical-media tests use defaults
    auto result = std::unique_ptr<EventLog>(new EventLog(sd, flash, config));
    assert(result->begin(boot, 1, "test-1")); return result;
}
static EventInput notice() { EventInput input; input.code = 1000; return input; }
static void add(EventLog& log, size_t n = 1) {
    for (size_t i = 0; i < n; ++i) { assert(log.submit(notice()) == EventLog::Submit::Accepted); log.service(1); }
}
static EventLog::Batch batch;
static size_t read(EventLog& log, size_t limit = 64) { assert(log.nextBatch(batch, limit)); return batch.count; }
static void ack(EventLog& log) { assert(log.acknowledge(batch, batch.ids, batch.count)); }
static void metadataNumber(std::string& data, size_t offset, uint64_t value, size_t bytes = 8) {
    for (size_t i = 0; i < bytes; ++i) data[offset + i] = static_cast<char>(value >> (8 * i));
    uint32_t crc = EventLog::crc32(data.data(), 76);
    for (size_t i = 0; i < 4; ++i) data[76 + i] = static_cast<char>(crc >> (8 * i));
}

static void protocolInputs() {
    // Python sends authoritative catalog cases; export REAL nextBatch JSON.
    MemoryFs sd; auto log = logger(&sd);
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        int code, mode, type; std::string key, value;
        in >> code >> mode >> key >> type >> value;
        EventInput input; input.code = static_cast<uint16_t>(code);
        input.transition = mode ? EventTransition::Raise : EventTransition::Notice;
        if (key != "-") {
            input.contextCount = 1;
            input.context[0] = type == 0 ? EventContext::integer(key.c_str(), std::stoll(value)) :
                               type == 1 ? EventContext::boolean(key.c_str(), value == "true") :
                                           EventContext::enumeration(key.c_str(), value.c_str());
        }
        assert(log->submit(input) == EventLog::Submit::Accepted); log->service(1);
        assert(read(*log) == 1); std::cout << batch.events[0] << '\n'; ack(*log);
    }
}
static void basicAndIdentity() {
    MemoryFs sd; auto log = logger(&sd, nullptr, boot1, kSdJournalQuota, 700);
    assert(EventLog::crc32("123456789", 9) == 0xcbf43926);
    size_t calls = sd.calls;
    EventIdentity id;
    assert(log->submit(notice(), &id) == EventLog::Submit::Accepted);
    assert(sd.calls == calls && id.sequence == 1 && !std::strcmp(id.bootId, boot1));
    log->service(); assert(read(*log) == 1);
    std::string first = batch.events[0];
    assert(read(*log) == 1 && first == batch.events[0]);
    add(*log, 3); assert(sd.segments().size() >= 2);
    auto reboot = logger(&sd, nullptr, boot2);
    assert(read(*reboot) == 4 && first == batch.events[0]);
    assert(!std::strcmp(batch.ids[0].bootId, boot1) && batch.ids[0].sequence == 1);
    ack(*reboot);
    auto again = logger(&sd, nullptr, boot2); assert(read(*again) == 0);
    add(*again); assert(read(*again) == 1 && !std::strcmp(batch.ids[0].bootId, boot2));
    // A clean .open is reused across boot boundaries, no per-boot directory.
    const auto& shared = sd.files[sd.segments().back()];
    assert(shared.find(boot1) != std::string::npos && shared.find(boot2) != std::string::npos);
    assert(std::strstr(batch.events[0], "\"utc_ms\":null,\"time_source\":\"unknown\",\"sync_age_ms\":null"));
}
static void cachedAppendCost() {
    MemoryFs sd; auto log = logger(&sd); add(*log);
    for (size_t i = 0; i < 200; ++i) {
        size_t calls = sd.calls, lists = sd.lists, reads = sd.reads;
        add(*log);
        assert(sd.calls - calls == 1 && sd.lists == lists && sd.reads == reads);
    }
    assert(sd.segments().size() == 1 && sd.files[sd.segments()[0]].size() > 50 * 1024);
    // A failed close can leave a complete frame. Invalidate both inventory and
    // tail validation; retry scans it and retains the same event identity.
    sd.failClose = true; sd.failList = true;
    add(*log); assert(log->stats().queued == 1);
    size_t calls = sd.calls, lists = sd.lists, reads = sd.reads;
    sd.failList = false; log->service(1);
    assert(log->stats().queued == 0 && sd.calls - calls > 200);
    assert(sd.lists > lists && sd.reads - reads >= 202);
    calls = sd.calls; add(*log); assert(sd.calls - calls == 1);

    // A torn prefix must be sealed after recovery, never reused for appending.
    std::string old = sd.segments().back();
    sd.tear = 20; sd.tearPath = "seg-"; sd.failList = true;
    add(*log); std::string damaged = sd.files[old];
    sd.failList = false; log->service(1);
    std::string sealed = old.substr(0, old.size() - 4) + "ready";
    assert(!sd.files.count(old) && sd.files[sealed] == damaged);
    assert(sd.segments().size() == 2);
    calls = sd.calls; add(*log); assert(sd.calls - calls == 1);
    std::cout << "EventLog steady append: 1 fs call (write), early and >50 KiB tail; recovery rescans\n";
}
static void tornAndDamagedFrames() {
    MemoryFs sd; auto log = logger(&sd, nullptr, boot1, kSdJournalQuota, 700);
    add(*log); sd.tear = 80; sd.tearPath = "seg-";
    assert(log->submit(notice()) == EventLog::Submit::Accepted); log->service(1);
    assert(log->stats().queued == 1);
    log->service(1); add(*log, 3);
    assert(read(*log) == 5); // damaged tail did not hide subsequent .ready files
    assert(batch.ids[0].sequence == 1 && batch.ids[1].sequence == 2);
    ack(*log); assert(log->stats().dropped == 1); // explicit corruption loss

    MemoryFs middle; auto other = logger(&middle, nullptr, boot1, kSdJournalQuota, 1100);
    add(*other, 8);
    auto paths = middle.segments(); auto& first = middle.files[paths[0]];
    size_t second = first.find('\n') + 1; assert(second < first.size()); first[second + 10] ^= 1;
    auto reboot = logger(&middle, nullptr, boot2);
    assert(read(*reboot) == 7 && batch.ids[1].sequence == 3 && batch.ids[6].sequence == 8);
    ack(*reboot); assert(reboot->stats().dropped == 1);
    // Oversize garbage, including plausible suffixes, cannot turn into a frame.
    MemoryFs garbage; garbage.files["/diag/seg-00000001.ready"] = std::string(4096, 'x') + "\n";
    auto later = logger(&garbage); add(*later); assert(read(*later) == 1);
}
static void ackRecovery() {
    MemoryFs sd; auto log = logger(&sd); add(*log, 3);
    assert(read(*log, 1) == 1); ack(*log);
    assert(read(*log, 1) == 1 && batch.ids[0].sequence == 2);
    sd.tear = 31; sd.tearPath = "ack-";
    assert(!log->acknowledge(batch, batch.ids, batch.count));
    size_t lists = sd.lists;
    assert(read(*log) == 2 && sd.lists > lists); // failed metadata write reloads inventory/slots
    auto reboot = logger(&sd, nullptr, boot2);
    assert(read(*reboot) == 2 && batch.ids[0].sequence == 2); ack(*reboot);
    auto saved = sd;
    for (auto& item : sd.files) if (item.first.find("ack-") != std::string::npos) item.second[40] ^= 1;
    auto corrupt = logger(&sd, nullptr, boot2); assert(read(*corrupt) == 3);
    // Response verification: missing/extra/unexpected/duplicate IDs retain all.
    assert(!corrupt->acknowledge(batch, batch.ids, 2));
    EventIdentity accepted[3]; std::copy(batch.ids, batch.ids + 3, accepted);
    accepted[1] = accepted[0]; assert(!corrupt->acknowledge(batch, accepted, 3));
    accepted[1] = batch.ids[1]; accepted[2].sequence = 999;
    assert(!corrupt->acknowledge(batch, accepted, 3));
    std::copy(batch.ids, batch.ids + 3, accepted); std::swap(accepted[0], accepted[2]);
    assert(corrupt->acknowledge(batch, accepted, 3));
    auto restored = logger(&saved, nullptr, boot2); assert(read(*restored) == 0);
    // Every torn prefix of the next slot recovers the older committed cursor.
    for (size_t prefix = 0; prefix < 80; ++prefix) {
        MemoryFs torn; auto writer = logger(&torn); add(*writer, 3);
        assert(read(*writer, 1) == 1); ack(*writer);
        assert(read(*writer, 1) == 1);
        torn.tear = prefix; torn.tearPath = "ack-";
        assert(!writer->acknowledge(batch, batch.ids, batch.count));
        auto recovered = logger(&torn, nullptr, boot2);
        assert(read(*recovered) == 2 && batch.ids[0].sequence == 2);
    }
}
static void overflowAndFailure() {
    MemoryFs sd; sd.readOnly = true; auto log = logger(&sd);
    for (size_t i = 0; i < kEventQueueSize; ++i) assert(log->submit(notice()) == EventLog::Submit::Accepted);
    for (size_t i = 0; i < 7; ++i) assert(log->submit(notice()) == EventLog::Submit::Full);
    assert(log->stats().dropped == 7); log->service();
    assert(log->stats().queued == kEventQueueSize && sd.segments().empty());
    assert(read(*log) == kEventQueueSize); // RAM can still upload
    sd.readOnly = false; sd.failNotice = true; log->service(16);
    assert(log->stats().queued == 0 && log->stats().dropped == 7 && log->stats().pendingLoss == 7);
    size_t files = sd.files.size();
    for (size_t i = 0; i < 10; ++i) log->service();
    assert(log->stats().dropped == 7 && log->stats().pendingLoss == 7 && sd.files.size() == files);
    sd.failNotice = false; log->service();
    assert(log->stats().pendingLoss == 0 && read(*log) == kEventQueueSize + 1);
    size_t notices = 0;
    for (size_t i = 0; i < batch.count; ++i) if (std::strstr(batch.events[i], "\"code\":4009,")) {
        ++notices; assert(std::strstr(batch.events[i], "\"count\":7"));
    }
    assert(notices == 1 && batch.ids[batch.count - 1].sequence == kEventQueueSize + 1);
    ack(*log); log->service(); assert(read(*log) == 0);
    auto reboot = logger(&sd, nullptr, boot2); read(*reboot); assert(reboot->stats().dropped == 7);

    MemoryFs full; full.capacity = 160; auto bounded = logger(&full);
    add(*bounded, 1); assert(bounded->stats().queued == 1);
    full.capacity = SIZE_MAX; bounded->service(); assert(read(*bounded) == 1);
    // An ambiguous durable close is retried under the original event ID.
    MemoryFs close; close.failClose = true; auto uncertain = logger(&close);
    add(*uncertain); assert(uncertain->stats().queued == 1);
    uncertain->service(); assert(read(*uncertain) == 1 && batch.ids[0].sequence == 1); ack(*uncertain);
    assert(read(*uncertain) == 0);
}
static void fallbackAndFsFailures() {
    MemoryFs sd, flash; sd.mounted = false;
    auto log = logger(&sd, &flash); add(*log); assert(log->stats().backend == EventLog::Backend::LittleFs);
    flash.readOnly = true; add(*log); assert(log->stats().backend == EventLog::Backend::Ram);
    assert(log->stats().queued == 1);
    sd.mounted = true; log->service(); assert(log->stats().backend == EventLog::Backend::Sd);
    assert(read(*log) == 1 && batch.ids[0].sequence == 2); ack(*log);
    assert(read(*log) == 1 && batch.ids[0].sequence == 1);
    assert(!log->acknowledge(batch, batch.ids, batch.count));
    flash.readOnly = false; ack(*log);
    assert(log->acknowledge(batch, batch.ids, batch.count) == false); // consumed ticket

    MemoryFs broken, reserve; broken.failOpen = true;
    auto fails = logger(&broken, &reserve); add(*fails); assert(reserve.segments().size() == 1);
    MemoryFs rename; auto rolls = logger(&rename, nullptr, boot1, kSdJournalQuota, 512);
    add(*rolls); rename.failRename = true;
    size_t lists = rename.lists; add(*rolls);
    assert(rolls->stats().queued == 1 && rename.lists > lists);
    rename.failRename = false; rolls->service(); assert(read(*rolls) == 2);
    rename.failRead = true; assert(!rolls->nextBatch(batch)); rename.failRead = false;
    rename.failList = true; rolls->service(); rename.failList = false;
    assert(rolls->stats().storageFailures > 0);
}
static void quotaAndRetention() {
    MemoryFs sd; auto log = logger(&sd, nullptr, boot1, 1600, 700);
    add(*log, 20);
    assert(sd.used() <= 1600 && log->stats().dropped > 0);
    assert(read(*log) > 0);
    uint64_t prior = 0;
    for (size_t i = 0; i < batch.count; ++i) {
        assert(batch.ids[i].sequence > prior); prior = batch.ids[i].sequence;
    }
    assert(batch.ids[0].sequence > 1); // oldest unacknowledged segments evicted first
    ack(*log);
    auto reboot = logger(&sd, nullptr, boot2, 1600, 700); read(*reboot);
    assert(reboot->stats().dropped > 0);
    // An acked segment is normally reclaimed, with no loss notice.
    MemoryFs retained; auto normal = logger(&retained, nullptr, boot1, 1800, 700);
    add(*normal, 2); assert(read(*normal) == 2); ack(*normal); add(*normal, 3);
    assert(normal->stats().dropped == 0 && retained.used() <= 1800);
    assert(!retained.files.count("/diag/seg-00000001.ready"));
    // Failed removal after loss metadata commit survives reboot; no double count.
    MemoryFs removal; auto failed = logger(&removal, nullptr, boot1, 1000, 512);
    add(*failed, 2); removal.failRemove = true;
    size_t lists = removal.lists; add(*failed); assert(removal.lists > lists);
    uint64_t lost = failed->stats().dropped; assert(lost > 0);
    removal.failRemove = false;
    auto recovered = logger(&removal, nullptr, boot2, 1000, 512); recovered->service(0);
    read(*recovered); assert(recovered->stats().dropped == lost);
    // The fallback includes both ack slots and 4 KiB rounding, even during writes.
    MemoryFs unavailable, small; unavailable.mounted = false;
    small.allocationUnit = 4096;
    auto flash = logger(&unavailable, &small);
    for (size_t i = 0; i < 400; ++i) {
        add(*flash);
        assert(small.allocated() <= kLittleFsJournalReserve && small.peakAllocated <= kLittleFsJournalReserve);
    }
    assert(small.files.count("/diag/ack-0") && small.files.count("/diag/ack-1"));
    assert(flash->stats().dropped > 0);
    // Existing unknown and oversized metadata files also consume whole blocks.
    MemoryFs rounded; rounded.allocationUnit = 4096;
    rounded.files["/diag/unknown"] = "x";
    rounded.files["/diag/ack-0"] = std::string(4097, 'x');
    auto recover = logger(nullptr, &rounded);
    for (size_t i = 0; i < 400; ++i) add(*recover);
    assert(rounded.files["/diag/unknown"] == "x" && rounded.peakAllocated <= kLittleFsJournalReserve);
    // Exercise the default SD cluster accounting under a deliberately small cap.
    MemoryFs clusters; clusters.allocationUnit = 32768;
    EventLog::Config config; config.sdQuota = 4 * 32768;
    EventLog physical(&clusters, nullptr, config); assert(physical.begin(boot1, 1, "test-1"));
    for (size_t i = 0; i < 400; ++i) add(physical);
    assert(clusters.peakAllocated <= config.sdQuota && physical.stats().dropped > 0);
    config.littleFsBlock = 0; EventLog invalid(nullptr, nullptr, config);
    assert(!invalid.begin(boot1, 1, "test-1"));
}
static void countersAndRevisions() {
    MemoryFs sd; auto log = logger(&sd);
    uint64_t revision = 123;
    assert(!log->allocateStatusSequence(revision) && revision == 123);
    assert(log->restoreStatusSequence(100));
    size_t writes = sd.writes;
    assert(log->allocateStatusSequence(revision) && revision == 101);
    assert(sd.writes == writes + 1); // one 80-byte slot write per new revision
    auto reboot = logger(&sd, nullptr, boot2);
    assert(reboot->allocateStatusSequence(revision) && revision == 102);
    sd.tear = 20; sd.tearPath = "ack-";
    assert(!reboot->allocateStatusSequence(revision) && revision == 102);
    auto retry = logger(&sd, nullptr, boot2); assert(retry->allocateStatusSequence(revision) && revision == 103);
    assert(!retry->restoreStatusSequence(1));
    assert(retry->restoreStatusSequence(INT64_MAX)); assert(!retry->allocateStatusSequence(revision));
    // Last numeric filename is usable once; the following reservation fails.
    MemoryFs last; auto seed = logger(&last); add(*seed);
    for (auto& item : last.files) if (item.first.find("ack-") != std::string::npos)
        metadataNumber(item.second, 24, 99999999, 4);
    last.files["/diag/seg-00000001.ready"] = last.files["/diag/seg-00000001.open"];
    last.files.erase("/diag/seg-00000001.open");
    auto exhausted = logger(&last, nullptr, boot2, kSdJournalQuota, 512);
    add(*exhausted, 2); assert(exhausted->stats().queued == 1);
    assert(last.files.count("/diag/seg-99999999.ready"));
    assert(!last.files.count("/diag/seg-00000000.open"));
    char path[40]; EventLog::segmentPath(path, 1, true); assert(!std::strcmp(path, "/diag/seg-00000001.ready"));
}
static void validationAndConcurrency() {
    MemoryFs sd; auto log = logger(&sd);
    EventInput input = notice(); input.code = 5001; assert(log->submit(input) == EventLog::Submit::Invalid);
    input.code = 7001; assert(log->submit(input) == EventLog::Submit::Invalid);
    input.code = 4999; assert(log->submit(input) == EventLog::Submit::Accepted);
    input.contextCount = 1; input.context[0] = EventContext::integer("free_bytes", 1);
    assert(log->submit(input) == EventLog::Submit::Invalid);
    input = notice(); input.contextCount = 1; input.context[0] = EventContext::enumeration("reset_reason", "arbitrary");
    assert(log->submit(input) == EventLog::Submit::Invalid);
    input.context[0] = EventContext::enumeration("reset_reason", "power_on");
    input.contextCount = 2; input.context[1] = input.context[0]; assert(log->submit(input) == EventLog::Submit::Invalid);
    input = notice(); input.code = 2002; input.transition = EventTransition::Raise;
    input.contextCount = 1; input.context[0] = EventContext::integer("battery_mv", 6001);
    assert(log->submit(input) == EventLog::Submit::Invalid);
    input.context[0] = EventContext::integer("battery_mv", -1); assert(log->submit(input) == EventLog::Submit::Invalid);
    input.context[0] = EventContext::boolean("battery_mv", false); assert(log->submit(input) == EventLog::Submit::Invalid);
    input.contextCount = 0; EventIdentity raised;
    assert(log->submit(input, &raised) == EventLog::Submit::Accepted);
    input.transition = EventTransition::Repeat; input.episodeSequence = raised.sequence;
    assert(log->submit(input) == EventLog::Submit::Accepted);
    input.transition = EventTransition::Clear; assert(log->submit(input) == EventLog::Submit::Accepted);
    input.episodeSequence = INT64_MAX; assert(log->submit(input) == EventLog::Submit::Invalid);
    input = notice(); input.utcMs = 1; assert(log->submit(input) == EventLog::Submit::Invalid);
    input.timeSource = EventTimeSource::Gps; input.syncAgeMs = 1;
    assert(log->submit(input) == EventLog::Submit::Accepted);
    input.utcMs = 253402300800000LL; assert(log->submit(input) == EventLog::Submit::Invalid);
    input = notice(); input.uptimeMs = UINT64_MAX; assert(log->submit(input) == EventLog::Submit::Invalid);
    EventLog oversize(&sd, nullptr); assert(oversize.begin(boot1, INT64_MAX, "test-123456789012345678901234567"));
    input = notice(); input.code = 2007; input.uptimeMs = INT64_MAX; input.timeSource = EventTimeSource::Sntp;
    input.utcMs = 253402300799999LL; input.syncAgeMs = INT64_MAX; input.contextCount = 5;
    input.context[0] = EventContext::integer("clock_count", 27); input.context[1] = EventContext::integer("attempt_count", 3);
    input.context[2] = EventContext::enumeration("recovery_state", "not_needed");
    input.context[3] = EventContext::boolean("pmu_ready", true); input.context[4] = EventContext::boolean("wire_end_failed", true);
    assert(oversize.submit(input) == EventLog::Submit::Accepted);
    assert(read(oversize) == 1 && std::strlen(batch.events[0]) <= kEventBytes);
    input.contextCount = 9; assert(oversize.submit(input) == EventLog::Submit::Invalid);

    auto concurrent = logger(nullptr); std::atomic<size_t> accepted{0};
    auto produce = [&]() { for (size_t i = 0; i < 1000; ++i)
        if (concurrent->submit(notice()) == EventLog::Submit::Accepted) ++accepted; };
    std::thread a(produce), b(produce), c(produce); a.join(); b.join(); c.join();
    assert(accepted == kEventQueueSize && concurrent->stats().dropped == 3000 - kEventQueueSize);
    assert(read(*concurrent) == kEventQueueSize); ack(*concurrent); assert(read(*concurrent) == 0);
}
int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--protocol") { protocolInputs(); return 0; }
    std::cout << "EventLog sizeof: " << sizeof(EventLog) << "; Batch sizeof: " << sizeof(EventLog::Batch) << " bytes\n";
    std::cerr << "identity\n"; basicAndIdentity();
    std::cerr << "cached append\n"; cachedAppendCost();
    std::cerr << "frames\n"; tornAndDamagedFrames();
    std::cerr << "acks\n"; ackRecovery();
    std::cerr << "overflow\n"; overflowAndFailure();
    std::cerr << "fallback\n"; fallbackAndFsFailures();
    std::cerr << "quota\n"; quotaAndRetention();
    std::cerr << "counters\n"; countersAndRevisions();
    std::cerr << "validation\n"; validationAndConcurrency();
    std::cout << "EventLog: identity, recovery, faults, quota, fallback, protocol and concurrency passed\n";
}
