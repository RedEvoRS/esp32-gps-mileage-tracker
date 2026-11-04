// Exercise the real journal accounting and bounded producer with fake stores.
#define main eventLogRegressionMain
#include "test_event_log.cpp"
#undef main
#include "runtime_faults.h"

struct FaultFs : MemoryFs {
    Operation error = Operation::Write;
    const char* reject = nullptr;
    bool deferredFailure = false;
    bool write(const char* path, const void* data, size_t size, bool append) override {
        if (reject && std::strstr(path, reject)) { ++writes; return false; }
        return MemoryFs::write(path, data, size, append);
    }
    Operation writeFailure() const override { return error; }
    bool deferred() const override { return deferredFailure; }
};
static void observe(EventLog& log, RuntimeFaults& faults, uint32_t now) {
    auto state = log.storageFault();
    if (state.observed)
        faults.event(4008, now).enumeration("operation", state.operation)
            .enumeration("backend", state.backend == EventLog::Backend::Sd ? "sd" : "littlefs")
            .condition(state.active);
}
static std::string events(EventLog& log) {
    std::string json;
    while (read(log)) {
        for (size_t i = 0; i < batch.count; ++i) json += batch.events[i];
        ack(log);
    }
    return json;
}
static void operations() {
    for (auto op : {EventLogFs::Operation::Open, EventLogFs::Operation::Write,
                    EventLogFs::Operation::Close, EventLogFs::Operation::Rename,
                    EventLogFs::Operation::Ack}) {
        FaultFs sd, flash;
        auto log = logger(&sd, &flash, boot1, kSdJournalQuota, 512);
        RuntimeFaults::Condition slot{4008}; RuntimeFaults faults(*log, &slot, 1);
        add(*log); // good initial segment; next frame must roll
        events(*log);
        if (op == EventLogFs::Operation::Rename) sd.failRename = true;
        else { sd.reject = op == EventLogFs::Operation::Ack ? "/diag/ack-" : "seg-"; sd.error = op; }
        add(*log);
        auto state = log->storageFault();
        static const char* names[] = {"open", "write", "close", "rename", "ack"};
        assert(state.observed && state.active && state.backend == EventLog::Backend::Sd);
        assert(!std::strcmp(state.operation, names[static_cast<unsigned>(op)]));
        observe(*log, faults, 1000); assert(slot.episode);
        const auto episode = slot.episode;
        log->service(1); // E4008 itself uses ordinary SD -> LittleFS fallback
        assert(log->stats().queued == 0 && log->storageFault().active);
        auto json = events(*log); // ack on the healthy store must not clear SD
        assert(json.find("\"code\":4008,\"transition\":\"raise\"") != std::string::npos);
        assert(json.find(std::string("\"operation\":\"") + names[static_cast<unsigned>(op)] + "\",\"backend\":\"sd\"") != std::string::npos);
        assert(log->storageFault().active);
        sd.reject = nullptr; sd.failRename = false;
        assert(log->restoreStatusSequence(100)); // metadata recovers only ack failure
        assert(log->storageFault().active == (op != EventLogFs::Operation::Ack));
        add(*log); observe(*log, faults, 2000); log->service(1);
        assert(!log->storageFault().active && !slot.episode);
        json = events(*log);
        assert(json.find("\"code\":4008,\"transition\":\"clear\"") != std::string::npos);
        assert(json.find(std::string("\"episode_id\":\"") + boot1 + ":" + std::to_string(episode) + "\"") != std::string::npos);
    }
    std::puts("E4008: open/write/close/rename/ack raises, fallback, same-store append/metadata recovery passed");
}
static void faultAckRecovery() {
    FaultFs sd, flash;
    auto log = logger(&sd, &flash); add(*log);
    RuntimeFaults::Condition slot{4008}; RuntimeFaults faults(*log, &slot, 1);
    assert(read(*log) == 1);
    sd.reject = "/diag/ack-";
    assert(!log->acknowledge(batch, batch.ids, batch.count));
    assert(log->storageFault().active && !std::strcmp(log->storageFault().operation, "ack"));
    observe(*log, faults, 1000);
    add(*log); // successful existing-segment append cannot recover broken ack metadata
    observe(*log, faults, 1001);
    assert(log->storageFault().active && slot.episode);
    assert(read(*log)); // refresh the ticket after appending
    std::string raised;
    for (size_t i = 0; i < batch.count; ++i) raised += batch.events[i];
    assert(raised.find("\"transition\":\"raise\"") != std::string::npos);
    sd.reject = nullptr;
    assert(log->acknowledge(batch, batch.ids, batch.count));
    observe(*log, faults, 1001);
    assert(!slot.episode && !log->storageFault().active);
    auto json = events(*log);
    assert(json.find("\"transition\":\"clear\"") != std::string::npos);
    std::puts("E4008: durable event ack recovery passed");
}
static void boundedFailure() {
    FaultFs sd, flash;
    sd.readOnly = flash.readOnly = true;
    auto log = logger(&sd, &flash);
    RuntimeFaults::Condition slot{4008}; RuntimeFaults faults(*log, &slot, 1);
    assert(log->submit(notice()) == EventLog::Submit::Accepted);
    log->service(1); observe(*log, faults, 1);
    assert(log->stats().queued == 2 && slot.episode);
    const auto episode = slot.episode;
    size_t writes = sd.writes + flash.writes;
    for (uint32_t now = 2; now < 10002; ++now) { log->service(1); observe(*log, faults, now); }
    assert(log->stats().queued == 2 && slot.episode == episode);
    assert(sd.writes + flash.writes == writes + 20000); // no recursive I/O
    observe(*log, faults, RuntimeFaults::kRepeatMs); assert(log->stats().queued == 2);
    observe(*log, faults, RuntimeFaults::kRepeatMs + 1); assert(log->stats().queued == 3);
    // Recovering just SD leaves the LittleFS failure active, without a new raise.
    sd.readOnly = false; log->service(1); observe(*log, faults, RuntimeFaults::kRepeatMs + 2);
    assert(log->storageFault().active && log->storageFault().backend == EventLog::Backend::LittleFs);
    assert(slot.episode == episode);
    sd.readOnly = true;
    for (uint32_t i = 2; i < 40; ++i) {
        log->service(1); observe(*log, faults, i * RuntimeFaults::kRepeatMs + 1);
        assert(log->stats().queued <= kEventQueueSize && slot.episode == episode);
    }
    assert(log->stats().queued == kEventQueueSize);
    FaultFs yielded; yielded.readOnly = yielded.deferredFailure = true;
    auto cancelled = logger(&yielded); add(*cancelled);
    assert(!cancelled->storageFault().observed);
    std::puts("E4008: 10000 failing service calls, 15-minute repeats, both-store aggregation, bounded overflow and cooperative yield passed");
}
int main() { operations(); faultAckRecovery(); boundedFailure(); }
