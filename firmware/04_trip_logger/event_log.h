#pragma once
#include "diag_catalog.h"
#include <atomic>
#include <cstddef>
#include <cstdint>

namespace diagnostics {
constexpr uint32_t kSdJournalQuota = 16 * 1024 * 1024;
constexpr uint32_t kLittleFsJournalReserve = 64 * 1024;
constexpr uint32_t kJournalSegmentBytes = 64 * 1024;
constexpr size_t kEventBytes = 502, kFrameBytes = 512, kEventQueueSize = 16;
constexpr uint64_t kMaxEventInteger = INT64_MAX;

// Implement on mounted media; never format. A successful write includes a
// durable close/sync, not just flush(). Failure may leave any written prefix.
// list() enumerates regular files in /diag (names, not full paths), returning
// End only after a complete listing. Missing media and I/O errors return Error.
// All methods, including reads and metadata operations, run under FS_LOCK.
// EventLog exclusively owns /diag while alive. Reconstruct it after a remount
// or external changes; successful operations update its cached inventory.
class EventLogFs {
public:
    enum class Operation : uint8_t { Open, Write, Close, Rename, Ack };
    enum class Result { Ok, End, Error };
    struct Entry { char name[40]; uint32_t size; };
    virtual ~EventLogFs() = default;
    virtual bool begin() = 0;  // ensure /diag exists; no formatting
    virtual Result list(size_t index, Entry& entry) = 0;
    virtual bool read(const char* path, uint32_t offset, void* data, size_t size) = 0;
    virtual bool write(const char* path, const void* data, size_t size, bool append) = 0;
    virtual bool rename(const char* from, const char* to) = 0; // never overwrite
    virtual bool remove(const char* path) = 0;
    // write() includes open/sync/close. Adapters can identify its first failure.
    virtual Operation writeFailure() const { return Operation::Write; }
    virtual bool deferred() const { return false; } // cooperative yield, not failed media
};

enum class EventTransition : uint8_t { Notice, Raise, Repeat, Clear };
enum class EventTimeSource : uint8_t { Unknown, Rtc, Gps, Sntp };
// Device-local recovery witness, independent of either removable/mountable
// journal. Checked durable writes; reads fail closed. Single consumer, FS_LOCK.
class StatusRevisionState {
public:
    virtual ~StatusRevisionState() = default;
    enum class Read { Ready, Missing, Error };
    virtual Read read(uint64_t& state) = 0;
    virtual bool write(uint64_t state) = 0;
};
struct EventContext {
    enum class Type : uint8_t { Integer, Boolean, Enum };
    char key[24] = {};
    Type type = Type::Integer;
    int64_t number = 0;
    char value[24] = {};
    static EventContext integer(const char* key, int64_t value);
    static EventContext boolean(const char* key, bool value);
    static EventContext enumeration(const char* key, const char* value);
};
struct EventInput {
    uint16_t code = 1000;
    EventTransition transition = EventTransition::Notice;
    // Raise uses its newly assigned sequence. Repeat/clear supply that earlier
    // raise's sequence in this boot. Notice requires zero (exports JSON null).
    uint64_t episodeSequence = 0, uptimeMs = 0;
    EventTimeSource timeSource = EventTimeSource::Unknown;
    int64_t utcMs = -1, syncAgeMs = -1; // -1 exports null
    EventContext context[8];
    size_t contextCount = 0;
};
struct EventIdentity { char bootId[33] = {}; uint64_t sequence = 0; };

class EventLog {
public:
    enum class Backend : uint8_t { Sd, LittleFs, Ram };
    enum class Submit { Accepted, Invalid, Full, Busy, Exhausted };
    struct Config {
        uint32_t sdQuota = kSdJournalQuota;
        uint32_t littleFsQuota = kLittleFsJournalReserve;
        uint32_t segmentBytes = kJournalSegmentBytes;
        uint32_t sdCluster = 32768, littleFsBlock = 4096; // actual allocation units
        bool reserveRejected = false; // transport's one-frame inspection slot
        StatusRevisionState* revisionState = nullptr; // required for snapshot authority
    };
    struct Batch {
        // Caller owns storage: static DRAM or PSRAM on ESP32-S3, never a task
        // stack (about 35 KiB). JSON array only; the worker adds check-in.
        char events[64][kEventBytes + 1] = {};
        EventIdentity ids[64];
        size_t count = 0, bytes = 2;
        uint64_t ticket = 0;
    };
    struct Stats {
        uint64_t dropped = 0, pendingLoss = 0;
        uint32_t storageFailures = 0;
        size_t queued = 0;
        Backend backend = Backend::Ram;
    };

    // The journal itself also needs static DRAM or PSRAM, never a task stack.
    // Host tests print sizeof(EventLog) and sizeof(Batch) for their ABI.
    EventLog(EventLogFs* sd, EventLogFs* littleFs);
    EventLog(EventLogFs* sd, EventLogFs* littleFs, Config config);
    // Before producers start. Caller supplies fresh random boot ID for EVERY
    // wake, NVS boot counter and public build label. Never reuse a boot ID with
    // a reset sequence; retry history is read directly from stored frames.
    bool begin(const char* bootId, uint64_t bootCounter, const char* firmwareVersion);
    // Integration sets actual mounted FAT geometry before the first consumer
    // call. Zero disables SD for this boot; queued producer records are kept.
    bool setSdAllocationUnit(uint32_t bytes);
    // Any task, NOT ISR/panic: one try-lock, bounded encoding, no filesystem or
    // waiting. On contention/full the drop counter advances. id is optional.
    Submit submit(const EventInput& input, EventIdentity* id = nullptr);
    // Installed before tasks start; immutable for this boot. Used at creation,
    // including internal loss notices, never when replaying journaled frames.
    void setTimeProvider(void (*provider)(EventInput&)) { timeProvider_ = provider; }
    void stamp(EventInput& input) const { if (timeProvider_) timeProvider_(input); }

    // Single consumer only, with FS_LOCK held (also nextBatch/ack/revision).
    // M=min(maxRecords,16) queue records plus one coalesced loss notice: at most
    // 2*M+1 append attempts, 2*M+3 store loads and three extra metadata saves.
    // #124: use maxRecords=1 on core 0, release the lock between calls and yield
    // to GPS trip writes. A normal append is one write, no reads/listing.
    // A roll adds one rename + one metadata write/read. Each append can reclaim
    // up to 512 segments; each unacked eviction scans <=64 KiB and saves metadata
    // before removal (<=513 saves/append including reservation). Each uncached
    // load adds one begin, <=1025 list calls, two metadata
    // reads and validation of <=512 open segments (<=64 KiB each). These recovery
    // bounds are work limits, NOT a time guarantee: schedule away from GPS writes
    // and measure adapter latency; do not leave GPS waiting through maintenance.
    void service(size_t maxRecords = 4);
    // Oldest retained records, normally SD first, then flash, then RAM. A failed
    // durable ack gives the other store boot-local priority until recovery.
    // The deferred store remains eligible when the preferred store is empty.
    // A 30 KiB byte budget leaves room for #125's current snapshot in the
    // 32 KiB request. Re-reads preserve record JSON/identity. One outstanding batch.
    bool nextBatch(Batch& batch, size_t maxEvents = 64, size_t byteBudget = 30 * 1024);
    // Pass the complete server accepted-ID list; exact set required. No partial
    // acceptance. False leaves data retryable; true means durable metadata.
    bool acknowledge(const Batch& batch, const EventIdentity* accepted, size_t count);
    // Only after a server-rejected singleton AND a successful identical empty
    // snapshot probe. Keep one checked frame per store for inspection; replacing
    // it is bounded loss, counted durably with the cursor (never server ack).
    bool isolateRejected(const Batch& singleton);
    Stats stats();
    // Single consumer, no I/O. Each failed store needs a successful append or
    // durable event ack; ack failures specifically need verified metadata/ack.
    // SD supplies context while both fail. No cross-store recovery.
    struct StorageFault {
        bool observed = false, active = false;
        Backend backend = Backend::Ram;
        const char* operation = "write";
    };
    StorageFault storageFault() const;
    // Single consumer, under FS_LOCK. Startup inventory counts physical valid
    // unacknowledged frames (duplicates after uncertain I/O may be counted).
    struct Backlog { uint64_t records = 0; size_t segments = 0; bool complete = true; };
    Backlog backlog();
    // Drain a bounded queue and seal open segments before unmount. False means
    // some RAM records or a failed seal remain; never claim them durable.
    bool seal();
    void invalidateStorage(); // after remount: keep boot/queue, reload all caches

    // Single consumer, FS_LOCK, unchanged core ownership. Mirror max(revision)+1
    // to every usable journal before exposing it. At least one existing current
    // authority must save; omitted/failed stores become stale in NVS before
    // transmission. Refusal never adds stale bits. No volatile/RAM authority.
    // The independent recovery witness blocks suspended or absent/stale stores
    // across boots. A missing witness adopts an agreed ready journal epoch;
    // explicit enrollment advances beyond every observed recovery epoch.
    // Store synchronization requires a current, nonsuspended authority.
    // Each allocation attempts one 80-byte slot per usable mounted store, including
    // wear-levelled LittleFS; no revision writes per producer scan/event.
    // Enrollment must supply >= server high-water AND minimumStatusSequence().
    uint64_t minimumStatusSequence(); // read-only, includes every loaded store
    bool restoreStatusSequence(uint64_t highWater);
    bool allocateStatusSequence(uint64_t& sequence);
    bool suspendStatusSequence(); // explicit recovery required after snapshot conflict

    static uint32_t crc32(const void* data, size_t size);
    static void segmentPath(char* out, uint32_t id, bool ready);

private:
    void (*timeProvider_)(EventInput&) = nullptr;
    static constexpr size_t kMaxSegments = 512, kMetaBytes = 80;
    struct Frame { char data[kFrameBytes + 1] = {}; uint16_t size = 0; bool loss = false; };
    struct Segment { uint32_t id = 0, size = 0; bool ready = false, clean = false; };
    struct Cursor { uint32_t segment = 0, offset = 0; };
    struct Meta {
        uint64_t generation = 0, revision = 0, dropped = 0, pending = 0;
        Cursor ack;
        uint32_t next = 1, discarded = 0;
        bool revisionReady = false, revisionSuspended = false;
        uint64_t revisionEpoch = 0;
    };
    struct Store {
        EventLogFs* fs = nullptr;
        uint32_t quota = 0, used = 0, unit = 1;
        uint32_t metaSizes[2] = {kMetaBytes, kMetaBytes};
        Segment segments[kMaxSegments];
        size_t count = 0;
        Meta meta;
        bool loaded = false;
        EventLogFs::Operation operation = EventLogFs::Operation::Open;
        EventLogFs::Operation failedOperation = EventLogFs::Operation::Open;
        EventLogFs::Operation recoveryOperation = EventLogFs::Operation::Write;
        bool observed = false, failed = false;
    };
    Store stores_[2];
    Config config_;
    char bootId_[33] = {}, version_[33] = {};
    uint64_t bootCounter_ = 0, sequence_ = 0;
    bool begun_ = false;
    uint64_t revisionFloor_ = 0;
    uint64_t revisionEpochFloor_ = 0;
    bool revisionBlocked_ = false; // also latch uncertain recovery-witness I/O
    std::atomic_flag queueLock_ = ATOMIC_FLAG_INIT;
    std::atomic<uint32_t> queueDrops_{0};
    Frame queue_[kEventQueueSize];
    size_t head_ = 0, count_ = 0;
    bool headStored_ = false;
    uint64_t ramPending_ = 0;
    uint32_t failures_ = 0;
    Backend backend_ = Backend::Ram;
    Backend recoveredBackend_ = Backend::Ram;
    uint64_t ticket_ = 0, issued_ = 0;
    Backend issuedBackend_ = Backend::Ram;
    Backend ackDeferredBackend_ = Backend::Ram; // cleared by its next durable ack
    Cursor issuedEnd_;
    EventIdentity issuedIds_[64];
    size_t issuedCount_ = 0;
    uint64_t issuedCorrupt_ = 0;
    Frame lossNotice_[2];
    uint64_t lossReported_[2] = {};

    bool encode(const EventInput& input, uint64_t sequence, Frame& frame);
    void drop();
    bool load(Store& store);
    bool inventory(Store& store);
    uint64_t allocated(const Store& store, uint32_t bytes) const;
    bool save(Store& store, Meta next);
    bool revisionStores(uint8_t& mounted, uint64_t& highest);
    bool readRevisionState(uint64_t& state, uint8_t loaded, bool adopt = true);
    bool writeRevisionState(uint64_t state);
    bool append(Store& store, const Frame& frame);
    bool makeRoom(Store& store, size_t bytes);
    bool readFrame(Store& store, const Segment& segment, uint32_t& offset, Frame& frame);
    bool identity(const Frame& frame, EventIdentity& id);
    bool acknowledged(const Store& store, const Segment& segment) const;
    uint64_t lostIn(Store& store, const Segment& segment);
    void fail();
    void fail(Store& store);
    void recovered(Store& store);
};
} // namespace diagnostics
