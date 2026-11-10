#pragma once
#include <cstdint>
#include <initializer_list>

namespace startup_watchdog {
static constexpr uint32_t STARTUP_WDT_TIMEOUT_MS = 120000;
static constexpr uint32_t STARTUP_HANG_LIMIT = 2;
// Generous, UNMEASURED board limits. A card that always hangs costs two
// 120 s timeouts per power-on, plus reboot and healthy internal-flash setup,
// before flash trip recording. Progressing scans/repairs have no total limit.
enum class Stage : uint32_t { None, Peripherals, CardMount, FlashMount, CardSpace,
    FlashSpace, Journal, CardRepair, FlashRepair, Services, Count };
enum class Fallback : uint32_t { None, SkipCard, NoStorage };
// Plain words: no constructor or initialized data in RTC_NOINIT memory.
struct Record { uint32_t magic, stage, hangs, fallback, checksum; };
static constexpr uint32_t kMagic = 0x53574431;
inline uint32_t checksum(const Record& r) {
    // FNV-1a over explicit words; no padding bytes or uninitialized fields.
    uint32_t hash = 2166136261u;
    for (uint32_t word : {r.magic, r.stage, r.hangs, r.fallback}) {
        for (unsigned i = 0; i < 4; ++i) {
            hash = (hash ^ (word & 255u)) * 16777619u;
            word >>= 8;
        }
    }
    return hash;
}
inline bool valid(const Record& r) {
    return r.magic == kMagic && r.stage < uint32_t(Stage::Count) &&
        r.hangs <= STARTUP_HANG_LIMIT && r.fallback <= uint32_t(Fallback::NoStorage) &&
        r.checksum == checksum(r);
}
inline bool cardOnly(Stage s) {
    return s == Stage::CardMount || s == Stage::CardSpace || s == Stage::CardRepair;
}
inline const char* stageText(Stage s) {
    switch (s) {
    case Stage::Peripherals: return "peripheral setup";
    case Stage::CardMount: return "card mount";
    case Stage::FlashMount: return "internal flash mount/provision";
    case Stage::CardSpace: return "card free-space query";
    case Stage::FlashSpace: return "internal flash free-space query";
    case Stage::Journal: return "journal/NVS initialization";
    case Stage::CardRepair: return "card trip repair";
    case Stage::FlashRepair: return "internal flash trip repair";
    case Stage::Services: return "startup services";
    default: return "none";
    }
}
class Policy {
public:
    void boot(Record saved, bool coldPowerOn, bool watchdogReset) {
        record_ = valid(saved) && !coldPowerOn ? saved : Record{kMagic, 0, 0, 0, 0};
        hung_ = watchdogReset ? Stage(record_.stage) : Stage::None;
        if (hung_ != Stage::None) {
            if (record_.hangs < STARTUP_HANG_LIMIT) ++record_.hangs;
            if (fallback() == Fallback::SkipCard) record_.fallback = uint32_t(Fallback::NoStorage);
            else if (record_.hangs >= STARTUP_HANG_LIMIT)
                record_.fallback = uint32_t(cardOnly(hung_) ? Fallback::SkipCard : Fallback::NoStorage);
        } else record_.hangs = 0;
        stage(Stage::Peripherals);
    }
    void stage(Stage next) { record_.stage = uint32_t(next); seal(); }
    // A completed boot clears the hang streak, but never a fallback latch.
    // Only a real power-on retries a device known to hang. No flash writes.
    void complete() { record_.hangs = 0; stage(Stage::None); }
    bool active() const { return record_.stage != uint32_t(Stage::None); }
    bool skipCard() const { return fallback() != Fallback::None; }
    bool noStorage() const { return fallback() == Fallback::NoStorage; }
    Fallback fallback() const { return Fallback(record_.fallback); }
    Stage hungStage() const { return hung_; }
    uint32_t hangs() const { return record_.hangs; }
    Record record() const { return record_; }
private:
    void seal() { record_.checksum = checksum(record_); }
    Record record_{};
    Stage hung_ = Stage::None;
};
}
