// Host-side stand-ins for the Arduino/ESP32 APIs 04_trip_logger.ino uses.
// Models FS buffering/faults, network retry inputs, and selected hardware
// events. The remaining APIs exist just so the sketch compiles.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
// Arduino exposes isnan globally; GCC's <cmath> only guarantees std::isnan.
using std::isnan;
#include <cstdarg>
#include <ctime>
#include <string>
#include <map>
#include <memory>
#include <vector>
#include <algorithm>
#include <set>
#include <functional>
#include <sys/time.h>

// ---- time / misc ------------------------------------------------------
extern uint32_t g_millis;
inline uint32_t millis() { return g_millis; }
// delay() takes no time unless a test sets g_delayHook. A test escapes one
// of setup()'s halt loops by throwing from the hook.
inline void (*g_delayHook)(uint32_t ms) = nullptr;
inline void delay(uint32_t ms) { if (g_delayHook) g_delayHook(ms); }
inline double radians(double d) { return d * M_PI / 180.0; }
using std::min;
#define IRAM_ATTR
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define OUTPUT_OPEN_DRAIN 3
#define PULLUP 4
#define LOW 0
#define HIGH 1
#define FALLING 0
inline std::map<int, int> g_pinModes, g_pinLevels;
inline std::function<void(int)> g_gpioHook;
inline std::function<int(int)> g_digitalReadHook;
inline std::function<void()> g_microDelayHook;
inline uint64_t g_micros = 0;
inline void delayMicroseconds(unsigned us) {
    g_micros += us;
    if (g_microDelayHook) g_microDelayHook();
}
inline void pinMode(int pin, int mode) {
    g_pinModes[pin] = mode;
    if (g_gpioHook) g_gpioHook(pin);
}
inline void digitalWrite(int pin, int level) {
    // ESP32 core 3.x ignores digitalWrite before pinMode registers a GPIO.
    if (g_pinModes.count(pin)) g_pinLevels[pin] = level;
    if (g_gpioHook) g_gpioHook(pin);
}
inline int digitalRead(int pin) { return g_digitalReadHook ? g_digitalReadHook(pin) : HIGH; }
inline int digitalPinToInterrupt(int p) { return p; }
inline void attachInterrupt(int, void (*)(), int) {}

struct timeval_stub { long long tv_sec; long tv_usec; };
#define timeval timeval_stub
// System clock: wall time advances with millis(), like the real RTC.
extern uint64_t g_epochBaseMs;   // epoch ms at millis() == 0
inline int gettimeofday_stub(timeval_stub* tv, void*) {
    uint64_t ms = g_epochBaseMs + g_millis;
    tv->tv_sec = (long long)(ms / 1000); tv->tv_usec = (long)(ms % 1000) * 1000; return 0;
}
inline int settimeofday_stub(const timeval_stub* tv, void*) {
    g_epochBaseMs = (uint64_t)tv->tv_sec * 1000 + tv->tv_usec / 1000 - g_millis; return 0;
}
#define gettimeofday gettimeofday_stub
#define settimeofday settimeofday_stub
inline struct tm* gmtime_r_stub(const time_t* t, struct tm* out) {
    // days-to-civil (Hinnant), UTC only
    long long s = *t; long long days = s / 86400; long long rem = s % 86400;
    if (rem < 0) { rem += 86400; days--; }
    days += 719468; long long era = (days >= 0 ? days : days - 146096) / 146097;
    unsigned doe = (unsigned)(days - era * 146097);
    unsigned yoe = (doe - doe/1460 + doe/36524 - doe/146096) / 365;
    long long y = yoe + era * 400; unsigned doy = doe - (365*yoe + yoe/4 - yoe/100);
    unsigned mp = (5*doy + 2)/153; unsigned d = doy - (153*mp+2)/5 + 1;
    unsigned m = mp < 10 ? mp+3 : mp-9; y += (m <= 2);
    memset(out, 0, sizeof *out);
    out->tm_year = (int)y - 1900; out->tm_mon = m - 1; out->tm_mday = d;
    out->tm_hour = (int)(rem / 3600); out->tm_min = (int)(rem % 3600 / 60); out->tm_sec = (int)(rem % 60);
    return out;
}
#define gmtime_r gmtime_r_stub
inline void configTime(int, int, const char*, const char*) {}
// Mirrors Arduino's plausibility test, including a retained RTC with drift.
// It deliberately does not prove that an SNTP response was received.
inline bool getLocalTime(struct tm* out, uint32_t) {
    time_t secs = (time_t)((g_epochBaseMs + g_millis) / 1000);
    gmtime_r(&secs, out);
    return out->tm_year > 116;
}

// ---- String -------------------------------------------------------------
class String {
public:
    std::string s;
    String() {}
    String(const char* c) : s(c ? c : "") {}
    String(const std::string& x) : s(x) {}
    String(int v) : s(std::to_string(v)) {}
    const char* c_str() const { return s.c_str(); }
    size_t length() const { return s.size(); }
    String substring(size_t start, size_t end) const { return String(s.substr(start, end - start)); }
    bool endsWith(const char* suf) const {
        size_t n = strlen(suf); return s.size() >= n && s.compare(s.size()-n, n, suf) == 0;
    }
    String operator+(const String& o) const { return String(s + o.s); }
    String operator+(const char* o) const { return String(s + o); }
    friend String operator+(const char* a, const String& b) { return String(std::string(a) + b.s); }
    bool operator==(const String& o) const { return s == o.s; }
    bool operator!=(const String& o) const { return s != o.s; }
    bool operator!=(const char* o) const { return s != o; }
};

// ---- Serial -------------------------------------------------------------
struct SerialStub {
    bool quiet = true;
    std::vector<std::string> log;
    uint32_t beginMs = 0;
    std::vector<uint32_t> logMs;
    void begin(int) { beginMs = millis(); }
    void println(const char* m = "") { logMs.push_back(millis()); log.push_back(m); if (!quiet) puts(m); }
    void printf(const char* fmt, ...) {
        char b[512]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof b, fmt, ap); va_end(ap);
        logMs.push_back(millis()); log.push_back(b); if (!quiet) fputs(b, stdout);
    }
};
extern SerialStub Serial;

// ---- Stream -------------------------------------------------------------
class Stream {
public:
    virtual ~Stream() {}
    virtual int available() = 0;
    virtual int read() = 0;
    virtual size_t readBytes(char*, size_t) { return 0; }
    virtual int peek() = 0;
    virtual size_t write(uint8_t) = 0;
};

// ---- Fake LittleFS --------------------------------------------------------
// Models what matters for write-verification: writes land in a 4 KB stdio
// buffer and "succeed"; flush() commits to the disk map only as far as
// capacity allows (a partial commit is possible); size() reports the
// committed size. Faults: failNextOpens, failFlushes (flash error), and a
// byte capacity (full filesystem).
struct FakeDisk {
    std::map<std::string, std::string> files;
    size_t capacity = 1 << 22;
    int failNextOpens = 0;
    int flushesBeforeFail = 0; // let this many flushes succeed first
    int failFlushes = 0;       // then fail the next N (flash error)
    int flushCount = 0;
    int failRenames = 0;
    int failRemoves = 0;
    std::string failOpenPath;
    std::string failReadPath;
    size_t failReadAfter = 0;
    void (*onRead)() = nullptr;  // simulate the other core interrupting a queue scan
    int removeCount = 0;
    bool invalidateDirectoryOnMutation = false;
    unsigned directoryRevision = 0;
    int openDirectories = 0;
    int openHandles = 0;
    bool renameOverwrites = true;
    int mutationsWithDirectoryOpen = 0;
    bool fullPathNames = false;  // core 2.x File::name() compatibility
    void mutatedDirectory() {
        directoryRevision++;
        if (openDirectories) mutationsWithDirectoryOpen++;
    }
    bool failMount = false;
    int mountAttempts = 0;
    void (*onMount)() = nullptr;   // runs at each mount attempt
    void (*onUnmount)() = nullptr; // power may return during shutdown preparation
    bool formatRequested = false;
    int formatAttempts = 0;
    bool failFormat = false;
    bool failRemount = false;
    std::string partitionLabel;
    bool mounted = true;
    bool writesFail = false, readsFail = false; // mounted but failing media
    size_t used() const { size_t u = 0; for (auto& kv : files) u += kv.second.size(); return u; }
};
extern FakeDisk g_disk;

#define FILE_READ "r"
#define FILE_WRITE "w"
#define FILE_APPEND "a"

struct FileImpl {
    FakeDisk* disk;
    explicit FileImpl(FakeDisk& d) : disk(&d) { disk->openHandles++; }
    std::string path;
    bool isDir = false, writable = false;
    std::string pending;              // stdio buffer
    size_t pos = 0;                   // read position
    std::vector<std::string> dirNames;
    size_t dirIdx = 0;
    unsigned directoryRevision = 0;
    ~FileImpl() { disk->openHandles--; if (isDir) disk->openDirectories--; }

    bool commitPending() {
        if (pending.empty()) return true;
        disk->flushCount++;
        if (disk->flushesBeforeFail > 0) disk->flushesBeforeFail--;
        else if (disk->failFlushes > 0) { disk->failFlushes--; return false; }
        auto it = disk->files.find(path);
        if (it == disk->files.end()) return false;
        size_t room = disk->capacity > disk->used() ? disk->capacity - disk->used() : 0;
        size_t n = std::min(room, pending.size());
        it->second.append(pending, 0, n);
        pending.erase(0, n);
        return pending.empty();
    }
};

class File : public Stream {
public:
    std::shared_ptr<FileImpl> p;
    File() {}
    explicit File(std::shared_ptr<FileImpl> i) : p(i) {}
    explicit operator bool() const { return (bool)p; }
    bool isDirectory() const { return p && p->isDir; }
    const char* name() const {
        if (p->disk->fullPathNames) return p->path.c_str();
        static std::string n; n = p->path.substr(p->path.rfind('/') + 1); return n.c_str();
    }
    File openNextFile() {
        if (!p || !p->isDir || p->dirIdx >= p->dirNames.size()) return File();
        if (p->disk->invalidateDirectoryOnMutation &&
            p->directoryRevision != p->disk->directoryRevision) return File();
        auto f = std::make_shared<FileImpl>(*p->disk);
        f->path = p->dirNames[p->dirIdx++];
        return File(f);
    }
    const std::string& data() const { static std::string e; auto it = p->disk->files.find(p->path); return it == p->disk->files.end() ? e : it->second; }
    size_t size() const { return data().size(); }
    size_t write(uint8_t c) override { return write(&c, 1); }
    size_t write(const uint8_t* b, size_t n) {
        if (!p || !p->writable) return 0;
        p->pending.append((const char*)b, n);
        if (p->pending.size() > 4096) p->commitPending();   // stdio auto-flush
        return n;                                            // fwrite into buffer "succeeds"
    }
    void flush() { if (p && p->writable) p->commitPending(); }
    void close() { if (p && p->writable) p->commitPending(); p.reset(); }
    bool seek(uint32_t pos) { p->pos = pos; return true; }
    int available() override { return (int)(size() - p->pos); }
    int read() override { return p->pos < size() ? (uint8_t)data()[p->pos++] : -1; }
    int read(uint8_t* b, size_t n) {
        if (p->disk->onRead) p->disk->onRead();
        if (p->path == p->disk->failReadPath && p->pos >= p->disk->failReadAfter) return 0;
        size_t k = std::min(n, size() - std::min(size(), p->pos));
        memcpy(b, data().data() + p->pos, k); p->pos += k; return (int)k;
    }
    size_t readBytes(char* b, size_t n) override { return read((uint8_t*)b, n); }
    int peek() override { return p->pos < size() ? (uint8_t)data()[p->pos] : -1; }
};

namespace fs {
struct FS {
    FakeDisk& g_disk;
    std::set<std::string> dirs;
    explicit FS(FakeDisk& disk) : g_disk(disk) {}
    bool exists(const char* path) {
        std::string pre = std::string(path) + "/";
        if (g_disk.files.count(path)) return true;
        for (auto& kv : g_disk.files) if (kv.first.rfind(pre, 0) == 0) return true;
        return dirs.count(path) > 0;
    }
    bool mkdir(const char* p) { dirs.insert(p); return true; }
    File open(const char* path, const char* mode = "r") {
        if (path == g_disk.failOpenPath) return File();
        if (g_disk.failNextOpens > 0) { g_disk.failNextOpens--; return File(); }
        std::string ps(path);
        if (dirs.count(ps) || std::any_of(g_disk.files.begin(), g_disk.files.end(),
                [&](const auto& entry) { return entry.first.rfind(ps + "/", 0) == 0; })) {
            auto d = std::make_shared<FileImpl>(g_disk); d->path = ps; d->isDir = true;
            g_disk.openDirectories++;
            d->directoryRevision = g_disk.directoryRevision;
            std::string pre = ps + "/";
            for (auto& kv : g_disk.files) if (kv.first.rfind(pre, 0) == 0) d->dirNames.push_back(kv.first);
            return File(d);
        }
        auto f = std::make_shared<FileImpl>(g_disk); f->path = ps;
        if (mode[0] == 'r') { if (!g_disk.files.count(ps)) return File(); }
        else {
            f->writable = true;
            if (!g_disk.files.count(ps)) g_disk.mutatedDirectory();
            if (mode[0] == 'w') g_disk.files[ps] = "";
            else if (!g_disk.files.count(ps)) g_disk.files[ps] = "";
        }
        return File(f);
    }
    File open(const String& p, const char* m = "r") { return open(p.c_str(), m); }
    bool remove(const char* p) {
        g_disk.removeCount++;
        if (g_disk.failRemoves > 0) { g_disk.failRemoves--; return false; }
        if (!g_disk.files.erase(p)) return false;
        g_disk.mutatedDirectory(); return true;
    }
    bool rename(const char* a, const char* b) {
        if (g_disk.failRenames > 0) { g_disk.failRenames--; return false; }
        if (!g_disk.renameOverwrites && g_disk.files.count(b)) return false;
        auto it = g_disk.files.find(a); if (it == g_disk.files.end()) return false;
        g_disk.mutatedDirectory();
        g_disk.files[b] = it->second; g_disk.files.erase(a); return true;
    }
};
}
struct LittleFSStub : fs::FS {
    LittleFSStub() : FS(::g_disk) {}
    bool begin(bool formatOnFail = false, const char* = "/littlefs", uint8_t = 10,
               const char* partitionLabel = "spiffs") {
        g_disk.mountAttempts++;
        g_disk.partitionLabel = partitionLabel;
        if (g_disk.onMount) g_disk.onMount();
        g_disk.formatRequested |= formatOnFail;
        if (!g_disk.failMount) { g_disk.mounted = true; return true; }
        if (!formatOnFail) return false;
        g_disk.files.clear();
        return true;
    }
    bool format() {
        g_disk.formatAttempts++;
        if (g_disk.failFormat) return false;
        g_disk.files.clear();
        dirs.clear();
        g_disk.failMount = g_disk.failRemount;
        return true;
    }
    void end() { g_disk.mounted = false; if (g_disk.onUnmount) g_disk.onUnmount(); }
    size_t usedBytes() { return g_disk.used(); }
    size_t totalBytes() { return g_disk.capacity; }
};
extern LittleFSStub LittleFS;

// Separate SD media, including FAT's refusal to rename over a destination.
#define HSPI 1
struct SPIClass {
    int host, sck = -1, miso = -1, mosi = -1, cs = -1;
    bool started = false;
    explicit SPIClass(int h) : host(h) {}
    bool begin(int s = -1, int i = -1, int o = -1, int c = -1) {
        started = true; sck = s; miso = i; mosi = o; cs = c; return true;
    }
    void end() { started = false; }
};
constexpr uint8_t CARD_NONE = 0, CARD_SD = 2, CARD_SDHC = 3;
inline FakeDisk g_sdDisk;
inline std::vector<std::string> g_powerEvents;
struct SDStub : fs::FS {
    bool cardPresent = false;
    int begins = 0, ends = 0;
    int capacityQueries = 0;
    std::function<void()> onCapacity;
    int cs = -1, maxFiles = 0;
    uint32_t hz = 0;
    SPIClass* spi = nullptr;
    SDStub() : FS(g_sdDisk) {}
    bool begin(uint8_t pin, SPIClass& bus, uint32_t frequency, const char*, uint8_t max_files, bool format) {
        begins++; g_disk.mountAttempts++; cs = pin; hz = frequency; spi = &bus; maxFiles = max_files;
        g_disk.formatRequested |= format;
        g_powerEvents.push_back("SD.begin");
        if (g_disk.onMount) g_disk.onMount();
        g_disk.mounted = cardPresent && !g_disk.failMount;
        return g_disk.mounted;
    }
    void end() {
        ends++; g_powerEvents.push_back("SD.end"); g_disk.mounted = false;
        if (g_disk.onUnmount) g_disk.onUnmount();
    }
    uint8_t cardType() { return g_disk.mounted ? CARD_SDHC : CARD_NONE; }
    // Core 3.3.x keeps cardType cached but returns zero on f_getfree error.
    uint64_t totalBytes() {
        ++capacityQueries;
        if (onCapacity) onCapacity();
        return cardPresent && !g_disk.readsFail ? g_disk.capacity : 0;
    }
    uint64_t usedBytes() { return cardPresent && !g_disk.readsFail ? g_disk.used() : 0; }
    bool readRAW(uint8_t* data, uint32_t sector) {
        if (sector || !g_disk.mounted) return false;
        memset(data, 0, 512); data[11] = 0; data[12] = 2; data[13] = 64;
        data[14] = 32; data[16] = 2; data[510] = 0x55; data[511] = 0xaa;
        return true;
    }
};
inline SDStub SD;

// ---- Preferences ----------------------------------------------------------
struct Preferences {
    std::map<std::string, uint32_t> kv;
    bool begin(const char*, bool) { return !failBegin; }
    uint32_t getUInt(const char* k, uint32_t d) { auto it = kv.find(k); return it == kv.end() ? d : it->second; }
    void putUInt(const char* k, uint32_t v) { kv[k] = v; }
    inline static std::map<std::string, uint64_t> durable64;
    inline static bool failWrite64 = false, failRead64 = false, failBegin = false;
    bool isKey(const char* k) { return durable64.count(k) || kv.count(k); }
    uint64_t getULong64(const char* k, uint64_t d) { return !failRead64 && durable64.count(k) ? durable64[k] : d; }
    size_t putULong64(const char* k, uint64_t v) {
        if (failWrite64) return 0;
        durable64[k] = v; return sizeof(v);
    }
    void end() {}
};

// ---- FreeRTOS -------------------------------------------------------------
typedef void* SemaphoreHandle_t;
// Display snapshots are exercised on one host thread; ESP32 uses a spinlock.
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
inline void portENTER_CRITICAL(portMUX_TYPE*) {}
inline void portEXIT_CRITICAL(portMUX_TYPE*) {}
#define portMAX_DELAY 0xffffffff
inline bool g_failMutexCreate = false;
inline SemaphoreHandle_t xSemaphoreCreateMutex() { return g_failMutexCreate ? nullptr : (void*)1; }
extern int g_lockDepth;
inline std::function<void(uint32_t)> g_semaphoreTakeHook;
inline int xSemaphoreTake(SemaphoreHandle_t, uint32_t wait) {
    if (g_semaphoreTakeHook) g_semaphoreTakeHook(wait);
    if (!wait && g_lockDepth) return 0;
    if (g_lockDepth != 0) { fprintf(stderr, "FATAL: re-entrant FS_LOCK (would deadlock)\n"); exit(3); }
    g_lockDepth++; return 1;
}
inline int xSemaphoreGive(SemaphoreHandle_t) { g_lockDepth--; return 1; }
constexpr int pdPASS = 1;
inline int g_taskCreates = 0;
inline int xTaskCreatePinnedToCore(void (*)(void*), const char*, int, void*, int, void*, int) {
    g_taskCreates++; return pdPASS;
}
inline unsigned g_stackHighWaterBytes = 4096;
inline unsigned uxTaskGetStackHighWaterMark(void*) { return g_stackHighWaterBytes; }
inline int g_currentCore = 1;
inline int xPortGetCoreID() { return g_currentCore; }
inline void (*g_taskDelayHook)(uint32_t ms) = nullptr;
inline void vTaskDelay(uint32_t ms) { g_millis += ms; if (g_taskDelayHook) g_taskDelayHook(ms); }
#define pdMS_TO_TICKS(x) (x)

// ---- hardware odds and ends (compile-only) ----------------------------------
struct TwoWire {
    uint8_t address = 0, cursor = 0;
    std::vector<uint8_t> tx, rx;
    size_t rxAt = 0;
    inline static std::function<uint8_t(TwoWire&, bool)> transaction;
    inline static std::function<size_t(TwoWire&, size_t)> request;
    int sda = -1, scl = -1, begins = 0, ends = 0, failBegins = 0;
    bool started = false, failEnd = false;
    uint16_t timeout = 0;
    std::function<void()> onBegin, onEnd;
    bool begin(int a, int b, uint32_t = 0) {
        sda = a; scl = b; begins++;
        if (onBegin) onBegin();
        if (failBegins) { failBegins--; return false; }
        started = true; return true;
    }
    bool end() {
        ends++;
        if (onEnd) onEnd();
        if (failEnd) return false;
        started = false; return true;
    }
    void setTimeOut(uint16_t ms) { timeout = ms; }
    void beginTransmission(uint8_t addr) { address = addr; tx.clear(); }
    size_t write(uint8_t value) { tx.push_back(value); return 1; }
    size_t write(const uint8_t* bytes, size_t n) { tx.insert(tx.end(), bytes, bytes + n); return n; }
    uint8_t endTransmission(bool stop = true) { return transaction ? transaction(*this, stop) : 0; }
    size_t requestFrom(uint8_t addr, size_t n) {
        address = addr; rx.clear(); rxAt = 0; return request ? request(*this, n) : 0;
    }
    int read() { return rxAt < rx.size() ? rx[rxAt++] : -1; }
};
extern TwoWire Wire, Wire1;
#define AXP2101_SLAVE_ADDRESS 0x34
#define XPOWERS_AXP2101_ALL_IRQ 0xffffffff
#define XPOWERS_AXP2101_VBUS_REMOVE_IRQ (1u << 14)
#define XPOWERS_AXP2101_VBUS_INSERT_IRQ (1u << 15)
#define XPOWERS_AXP2101_PKEY_LONG_IRQ (1u << 10)
#define XPOWERS_AXP2101_PKEY_SHORT_IRQ (1u << 11)
#define XPOWERS_AXP2101_WARNING_LEVEL2_IRQ (1u << 7)
#define XPOWERS_AXP2101_WARNING_LEVEL1_IRQ (1u << 6)
#define XPOWERS_AXP2101_CHG_VOL_4V1 2
// PMU: tests set vbusIn (the USB power level) and the latched VBUS
// interrupt flags, then raise gPmuIrqFlag as the IRQ pin would.
struct PoweredOff {};
constexpr uint8_t XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL = 0x18;
constexpr uint8_t XPOWERS_AXP2101_STATUS1 = 0x00;
constexpr uint8_t XPOWERS_AXP2101_STATUS2 = 0x01;
constexpr uint8_t XPOWERS_AXP2101_LDO_ONOFF_CTRL0 = 0x90;
constexpr uint8_t XPOWERS_AXP2101_LDO_VOL0_CTRL = 0x92;
constexpr uint8_t XPOWERS_AXP2101_INTSTS1 = 0x48;
constexpr uint8_t XPOWERS_AXP2101_INTSTS2 = 0x49;
constexpr uint8_t XPOWERS_AXP2101_INTSTS3 = 0x4A;
constexpr uint8_t XPOWERS_AXP2101_LOW_BAT_WARN_SET = 0x1A;
struct XPowersAXP2101 {
    // Per-register one-based call injection, independent of other PMU reads.
    std::map<uint8_t, unsigned> registerReadCalls;
    std::map<uint8_t, unsigned> failRegisterReadAt;
    std::map<uint8_t, int> registerReadOverride;
    std::function<void(uint8_t)> onRegisterRead;
    bool vbusNotIn = false; // STATUS2 bit 3, independently test library semantics
    bool failChargeDisable = false, failChargeEnable = false, failChargeRead = false;
    int writeRegister(uint8_t reg, uint8_t value) {
        operations.push_back("writeRegister");
        assert(reg == 0x50 || reg == 0x30); // debug TS setup only
        registerReadOverride[reg] = value;
        return 0;
    }
    int readRegister(uint8_t reg) {
        operations.push_back("readRegister");
        unsigned call = ++registerReadCalls[reg];
        if (onRegisterRead) onRegisterRead(reg);
        if (failRegisterReadAt.count(reg) && failRegisterReadAt[reg] == call) return -1;
        if (registerReadOverride.count(reg)) return registerReadOverride[reg];
        if (reg == 0x50) return 0x10; // XPowersLib init: external TS, bias off
        if (reg == 0x30) return 0x1d; // existing non-TS ADC channels
        if (reg == 0x36) return 0x04;
        if (reg == 0x37) return 0x00;
        if (reg == 0xa4) return batteryPercent;
        if (reg == 0x34) return batteryMv >> 8;
        if (reg == 0x35) return batteryMv & 0xff;
        if (reg == XPOWERS_AXP2101_STATUS1) return (vbusIn ? 0x20 : 0) | (batteryConnected ? 0x08 : 0);
        if (reg == XPOWERS_AXP2101_STATUS2) return vbusNotIn ? 0x08 : 0;
        if (reg == XPOWERS_AXP2101_LDO_ONOFF_CTRL0) return (aldo3 ? 0x04 : 0) | (aldo1 ? 0x01 : 0);
        if (reg == XPOWERS_AXP2101_LDO_VOL0_CTRL) return (aldo1Mv - 500) / 100;
        if (reg == XPOWERS_AXP2101_INTSTS1) return (lowIrq ? 0x40 : 0) | (criticalIrq ? 0x80 : 0);
        if (reg == XPOWERS_AXP2101_INTSTS2) return (removeIrq ? 0x40 : 0) | (insertIrq ? 0x80 : 0) |
            (longPressIrq ? 0x04 : 0) | (shortPressIrq ? 0x08 : 0);
        if (reg == XPOWERS_AXP2101_INTSTS3) return 0;
        if (reg == XPOWERS_AXP2101_LOW_BAT_WARN_SET) return (lowWarnNibble << 4) | criticalPercent;
        assert(reg == XPOWERS_AXP2101_CHARGE_GAUGE_WDT_CTRL);
        return failChargeRead ? -1 : chargeEnabled ? 0x02 : 0;
    }
    bool vbusIn = true, removeIrq = false, insertIrq = false;
    bool longPressIrq = false, shortPressIrq = false, criticalIrq = false, lowIrq = false;
    bool batteryConnected = true;
    int batteryPercent = 85;
    uint16_t batteryMv = 4020;
    uint8_t lowWarnNibble = 10, criticalPercent = 5;
    bool failShutdown = false;
    int shutdownCalls = 0;
    void (*onShutdown)() = nullptr;
    bool aldo3 = true, aldo4 = false, bldo1 = true, vbackup = true;
    bool aldo1 = true, failAldo1Voltage = false, failAldo1Enable = false;
    bool aldo1VoltageStuck = false, aldo1EnableStuck = false;
    int aldo1Mv = 1800;
    bool failRadioDisable = false, radioStuckOn = false;
    int aldo4Mv = 0, bldo1Mv = 1800;
    bool failSdVoltage = false, failSdEnable = false, failSdDisable = false;
    TwoWire* bus = nullptr;
    std::vector<std::string> operations;
    int beginCalls = 0, failBegins = 0;
    std::function<void()> onBegin;
    bool begin(TwoWire& wire, int, int, int) {
        bus = &wire; operations.push_back("begin"); beginCalls++;
        if (onBegin) onBegin();
        if (failBegins) { failBegins--; return false; }
        return true;
    }
    bool disableALDO3() {
        operations.push_back("disableALDO3");
        if (!failRadioDisable && !radioStuckOn) aldo3 = false;
        return !failRadioDisable;
    }
    bool isEnableALDO3() { operations.push_back("isEnableALDO3"); return aldo3; }
    bool setALDO1Voltage(uint16_t mv) {
        operations.push_back("setALDO1Voltage");
        if (failAldo1Voltage) return false;
        if (!aldo1VoltageStuck) aldo1Mv = mv;
        return true;
    }
    bool enableALDO1() {
        operations.push_back("enableALDO1");
        if (failAldo1Enable) return false;
        if (!aldo1EnableStuck) aldo1 = true;
        return true;
    }
    void setALDO4Voltage(int mv) { operations.push_back("setALDO4Voltage"); aldo4Mv = mv; }
    void enableALDO4() { operations.push_back("enableALDO4"); aldo4 = true; } void disableALDO4() { operations.push_back("disableALDO4"); aldo4 = false; }
    bool disableBLDO1() { operations.push_back("disableBLDO1");
        g_powerEvents.push_back("BLDO1.off");
        if (failSdDisable) return false;
        bldo1 = false; return true;
    }
    bool setBLDO1Voltage(int mv) { operations.push_back("setBLDO1Voltage");
        g_powerEvents.push_back("BLDO1.voltage");
        if (failSdVoltage) return false;
        bldo1Mv = mv; return true;
    }
    bool enableBLDO1() { operations.push_back("enableBLDO1");
        g_powerEvents.push_back("BLDO1.on");
        if (failSdEnable) return false;
        bldo1 = true; return true;
    }
    void disableButtonBatteryCharge() { operations.push_back("disableButtonBatteryCharge"); vbackup = false; }
    uint8_t getPowerOnSource() { operations.push_back("getPowerOnSource"); return 0x04; }
    uint8_t getPowerOffSource() { operations.push_back("getPowerOffSource"); return 0x02; }
    uint8_t chgVol = 3;
    bool setChargeTargetVoltage(uint8_t v) { operations.push_back("setChargeTargetVoltage"); chgVol = v; return true; }
    uint8_t getChargeTargetVoltage() { operations.push_back("getChargeTargetVoltage"); return chgVol; }
    float dieC = 30.0f; bool chargeEnabled = true;   // tests set dieC
    void enableTemperatureMeasure() { operations.push_back("enableTemperatureMeasure");}
    float getTemperature(uint16_t* = nullptr) { operations.push_back("getTemperature"); return dieC; }
    void enableCellbatteryCharge() { operations.push_back("enableCellbatteryCharge"); if (!failChargeEnable) chargeEnabled = true; }
    void disableCellbatteryCharge() { operations.push_back("disableCellbatteryCharge"); if (!failChargeDisable) chargeEnabled = false; }
    void enableBattDetection() { operations.push_back("enableBattDetection");} void enableBattVoltageMeasure() { operations.push_back("enableBattVoltageMeasure");}
    void enableVbusVoltageMeasure() { operations.push_back("enableVbusVoltageMeasure");} void enableSystemVoltageMeasure() { operations.push_back("enableSystemVoltageMeasure");}
    uint32_t enabledIrqs = 0;
    void disableIRQ(uint32_t) { operations.push_back("disableIRQ");} void enableIRQ(uint32_t mask) { enabledIrqs |= mask; operations.push_back("enableIRQ");}
    void clearIrqStatus() { operations.push_back("clearIrqStatus"); removeIrq = insertIrq = shortPressIrq = longPressIrq = criticalIrq = lowIrq = false; }
    uint32_t getIrqStatus() { operations.push_back("getIrqStatus"); return 0; }
    bool isVbusRemoveIrq() { operations.push_back("isVbusRemoveIrq"); return removeIrq; } bool isVbusInsertIrq() { operations.push_back("isVbusInsertIrq"); return insertIrq; }
    bool isPekeyShortPressIrq() { operations.push_back("isPekeyShortPressIrq"); return shortPressIrq; }
    bool isPekeyLongPressIrq() { operations.push_back("isPekeyLongPressIrq"); return longPressIrq; } bool isDropWarningLevel2Irq() { operations.push_back("isDropWarningLevel2Irq"); return criticalIrq; }
    bool isDropWarningLevel1Irq() { operations.push_back("isDropWarningLevel1Irq"); return lowIrq; }
    bool isBatteryConnect() { operations.push_back("isBatteryConnect"); return batteryConnected; }
    uint8_t getLowBatWarnThreshold() { operations.push_back("getLowBatWarnThreshold"); return lowWarnNibble; }
    uint8_t getLowBatShutdownThreshold() { operations.push_back("getLowBatShutdownThreshold"); return criticalPercent; }
    bool isVbusIn() { operations.push_back("isVbusIn"); return vbusIn; }
    int getBatteryPercent() { operations.push_back("getBatteryPercent"); return batteryPercent; }
    uint16_t getBattVoltage() { operations.push_back("getBattVoltage"); return batteryMv; }
    bool isCharging() { operations.push_back("isCharging"); return chargeEnabled && vbusIn; }
    void shutdown() { operations.push_back("shutdown");
        g_powerEvents.push_back("PMU.shutdown");
        shutdownCalls++;
        if (onShutdown) onShutdown();
        if (!failShutdown) throw PoweredOff{};  // successful hardware power-off never resumes
    }
};
// GPS UART: tests append NMEA/UBX bytes to rx and inspect what the sketch
// sends in tx. loop() advances time through its explicit vTaskDelay, like
// the scheduler; checking an empty UART itself takes no simulated time.
struct HardwareSerial {
    std::string rx, tx;
    size_t rxPos = 0;
    std::function<void(HardwareSerial&)> onWrite;
    HardwareSerial(int) {}
    int baud = 0, rxPin = -1, txPin = -1;
    void setRxBufferSize(int) {}
    void begin(int b, int, int rx, int tx) { baud = b; rxPin = rx; txPin = tx; }
    int available() {
        if (rxPos >= rx.size()) return 0;
        return (int)(rx.size() - rxPos);
    }
    int read() { return rxPos < rx.size() ? (uint8_t)rx[rxPos++] : -1; }
    size_t write(uint8_t c) {
        tx.push_back((char)c);
        if (onWrite) onWrite(*this);
        return 1;
    }
    size_t write(const uint8_t* b, size_t n) {
        tx.append((const char*)b, n);
        if (onWrite) onWrite(*this);
        return n;
    }
};
#define SERIAL_8N1 0
#define WL_CONNECTED 3
#define WIFI_STA 1
#define WIFI_OFF 0
struct WiFiStub {
    std::vector<std::string> scanSsids;
    int scanError = 0, scans = 0, scanDeletes = 0, joins = 0, disconnects = 0;
    int connectionStatus = 0, currentMode = WIFI_OFF, readsAfterDelete = 0;
    int disconnectStatus = -1;  // optionally change status when the radio is shut down
    uint32_t scanDurationMs = 0;
    bool scanResultsAvailable = false;
    int scanNetworks(bool, bool, bool, int) {
        scans++; g_millis += scanDurationMs; scanResultsAvailable = true;
        return scanError < 0 ? scanError : (int)scanSsids.size();
    }
    void scanDelete() { scanDeletes++; scanResultsAvailable = false; }
    String SSID(int i) {
        if (!scanResultsAvailable) readsAfterDelete++;
        return String(scanSsids.at(i));
    }
    const uint8_t* BSSID(int) {
        if (!scanResultsAvailable) readsAfterDelete++;
        static uint8_t b[6]; return b;
    }
    void mode(int m) { currentMode = m; } void setAutoReconnect(bool) {}
    void begin(const char*, const char*, int = 0, const uint8_t* = nullptr, bool = false) { joins++; }
    int status() { return connectionStatus; }
    void disconnect(bool) {
        disconnects++;
        if (disconnectStatus >= 0) connectionStatus = disconnectStatus;
    }
};
extern WiFiStub WiFi;
// A real restart never returns. Unwind setup() so a test can inspect what
// happened before the reset, without continuing into storage or tasks.
struct Restarted {};
inline int g_restarts = 0;
struct ESPStub { void restart() { g_restarts++; throw Restarted{}; } };
inline ESPStub ESP;
inline uint32_t g_httpConnectMs = 0, g_httpWriteMs = 0, g_httpHeaderMs = 0, g_httpByteMs = 0;
inline std::vector<uint32_t> g_httpConnectTimeouts, g_httpReadTimeouts;
struct WiFiClient : Stream {
    std::string response; size_t position = 0;
    uint32_t timeout = 5000, nextByte = 0; bool open = true;
    virtual int connect(const char*, uint16_t, int32_t budget) {
        g_httpConnectTimeouts.push_back(budget);
        g_millis += std::min(g_httpConnectMs, uint32_t(budget));
        return g_httpConnectMs < uint32_t(budget);
    }
    void setTimeout(uint32_t ms) { timeout = ms; }
    virtual uint8_t connected() { return open; }
    void stop() { open = false; }
    int available() override { return position < response.size() && int32_t(g_millis - nextByte) >= 0 ? 1 : 0; }
    int read() override {
        if (!available()) return -1;
        nextByte = g_millis + g_httpByteMs; return uint8_t(response[position++]);
    }
    virtual int read(uint8_t* out, size_t size) {
        if (!size || !available()) return 0;
        *out = read();
        return 1;
    }
    int peek() override { return available() ? uint8_t(response[position]) : -1; }
    size_t write(uint8_t) override { return 1; }
    virtual size_t write(const uint8_t*, size_t size) {
        g_millis += std::min(g_httpWriteMs, timeout);
        return g_httpWriteMs < timeout ? size : 0;
    }
};
// HTTP: the response code is set by the test; each request's body is read
// through the sketch's stream exactly as HTTPClient would, and recorded.
// g_httpHook, if set, runs once the request starts, standing in for the
// other core acting mid-upload. As in the real client, a stream that ends
// early (available() < 0) fails the request with SEND_PAYLOAD_FAILED.
inline int g_httpCode = 200;
inline int g_httpRequests = 0;
inline std::string g_httpLastBody;
inline std::vector<std::string> g_httpBodies;
inline void (*g_httpHook)() = nullptr;
inline std::function<void()> g_httpResponseHook;
inline std::string g_httpResponse;
inline bool g_httpAutoAck = true;
inline int g_httpDeclaredSize = -2;
inline std::vector<std::string> g_httpUrls;
inline std::vector<std::map<std::string, std::string>> g_httpHeaders;
struct HTTPClient {
    WiFiClient* client = nullptr; std::string url;
    uint32_t connectTimeout = 5000, readTimeout = 5000;
    std::map<std::string, std::string> headers;
    void begin(WiFiClient& c, const String& u) { client = &c; url = u.c_str(); }
    void addHeader(const char* key, const String& value) { headers[key] = value.c_str(); }
    void setReuse(bool) {}
    void setConnectTimeout(uint32_t ms) { connectTimeout = ms; }
    void setTimeout(uint16_t ms) { readTimeout = ms; g_httpReadTimeouts.push_back(ms); }
    int getSize() { return g_httpDeclaredSize == -2 ? int(client->response.size()) : g_httpDeclaredSize; }
    WiFiClient* getStreamPtr() { return client; }
    int sendRequest(const char*, Stream* s, size_t size) {
        g_httpRequests++;
        g_httpLastBody.clear();
        bool events = url.find("/v1/device-events") != std::string::npos;
        if (!client->connect("test.invalid", 8090, connectTimeout)) return -1;
        if (!events) client->setTimeout(readTimeout);
        if (g_httpHook) g_httpHook();
        char buf[1436];
        // Match core 3.3.11: the final available() follows the last socket
        // write even when the specified body length has been consumed.
        while (s->available() > -1 && g_httpLastBody.size() < size) {
            if (s->available() <= 0) break;
            size_t want = std::min(sizeof buf, size - g_httpLastBody.size());
            size_t n = s->readBytes(buf, want);
            if (n == 0) break;
            g_httpLastBody.append(buf, n);
            if (!events && client->write(reinterpret_cast<const uint8_t*>(buf), n) != n) return -3;
        }
        g_httpBodies.push_back(g_httpLastBody);
        g_httpUrls.push_back(url); g_httpHeaders.push_back(headers);
        if (g_httpLastBody.size() != size) return -3;   // HTTPC_ERROR_SEND_PAYLOAD_FAILED
        if (!events) {
            g_millis += std::min(g_httpHeaderMs, readTimeout);
            return g_httpHeaderMs > readTimeout ? -11 : g_httpCode;
        }
        if (events) {
            if (client->write(reinterpret_cast<const uint8_t*>(g_httpLastBody.data()), size) != size) return -3;
            uint32_t headerStart = g_millis;
            while (g_millis - headerStart < g_httpHeaderMs && client->connected()) g_millis++;
            if (!client->connected()) return -1;
            if (g_httpResponseHook) g_httpResponseHook();
            client->response = g_httpResponse;
            if (g_httpAutoAck) {
                client->response = "{\"accepted\":[";
                size_t pos = g_httpLastBody.find("\"events\":[") + 10; bool comma = false;
                while ((pos = g_httpLastBody.find("\"boot_id\":\"", pos)) != std::string::npos) {
                    std::string boot = g_httpLastBody.substr(pos + 11, 32);
                    size_t seq = g_httpLastBody.find("\"sequence\":", pos) + 11, end = seq;
                    while (g_httpLastBody[end] >= '0' && g_httpLastBody[end] <= '9') ++end;
                    client->response += std::string(comma ? "," : "") + "{\"boot_id\":\"" + boot +
                        "\",\"sequence\":" + g_httpLastBody.substr(seq, end - seq) + "}";
                    comma = true; pos = end;
                }
                client->response += "]}";
            }
            client->position = 0; client->nextByte = g_millis + g_httpByteMs;
        }
        return g_httpCode;
    }
    void end() {}
};

// Test-process runtime selection; never included in the device firmware.
inline bool hostDebugMode() { const char* mode = std::getenv("HOST_DEBUG"); return mode && std::strcmp(mode, "1") == 0; }
