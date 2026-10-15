// Host-side stand-ins for the Arduino/ESP32 APIs 04_trip_logger.ino uses.
// Only the FS is modeled faithfully (stdio buffering, capacity, fault
// injection); everything else exists just so the sketch compiles.
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
#include <sys/time.h>

// ---- time / misc ------------------------------------------------------
extern uint32_t g_millis;
inline uint32_t millis() { return g_millis; }
inline void delay(uint32_t) {}
inline double radians(double d) { return d * M_PI / 180.0; }
using std::min;
#define IRAM_ATTR
#define INPUT 0
#define FALLING 0
inline void pinMode(int, int) {}
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
    void begin(int) {}
    void println(const char* m = "") { log.push_back(m); if (!quiet) puts(m); }
    void printf(const char* fmt, ...) {
        char b[512]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof b, fmt, ap); va_end(ap);
        log.push_back(b); if (!quiet) fputs(b, stdout);
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
    int removeCount = 0;
    bool invalidateDirectoryOnMutation = false;
    unsigned directoryRevision = 0;
    int openDirectories = 0;
    int mutationsWithDirectoryOpen = 0;
    void mutatedDirectory() {
        directoryRevision++;
        if (openDirectories) mutationsWithDirectoryOpen++;
    }
    bool failMount = false;
    bool formatRequested = false;
    bool mounted = true;
    size_t used() const { size_t u = 0; for (auto& kv : files) u += kv.second.size(); return u; }
};
extern FakeDisk g_disk;

#define FILE_READ "r"
#define FILE_WRITE "w"
#define FILE_APPEND "a"

struct FileImpl {
    std::string path;
    bool isDir = false, writable = false;
    std::string pending;              // stdio buffer
    size_t pos = 0;                   // read position
    std::vector<std::string> dirNames;
    size_t dirIdx = 0;
    unsigned directoryRevision = 0;
    ~FileImpl() { if (isDir) g_disk.openDirectories--; }

    bool commitPending() {
        if (pending.empty()) return true;
        g_disk.flushCount++;
        if (g_disk.flushesBeforeFail > 0) g_disk.flushesBeforeFail--;
        else if (g_disk.failFlushes > 0) { g_disk.failFlushes--; return false; }
        auto it = g_disk.files.find(path);
        if (it == g_disk.files.end()) return false;
        size_t room = g_disk.capacity > g_disk.used() ? g_disk.capacity - g_disk.used() : 0;
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
        static std::string n; n = p->path.substr(p->path.rfind('/') + 1); return n.c_str();
    }
    File openNextFile() {
        if (!p || !p->isDir || p->dirIdx >= p->dirNames.size()) return File();
        if (g_disk.invalidateDirectoryOnMutation &&
            p->directoryRevision != g_disk.directoryRevision) return File();
        auto f = std::make_shared<FileImpl>();
        f->path = p->dirNames[p->dirIdx++];
        return File(f);
    }
    const std::string& data() const { static std::string e; auto it = g_disk.files.find(p->path); return it == g_disk.files.end() ? e : it->second; }
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
        if (p->path == g_disk.failReadPath && p->pos >= g_disk.failReadAfter) return 0;
        size_t k = std::min(n, size() - std::min(size(), p->pos));
        memcpy(b, data().data() + p->pos, k); p->pos += k; return (int)k;
    }
    size_t readBytes(char* b, size_t n) override { return read((uint8_t*)b, n); }
    int peek() override { return p->pos < size() ? (uint8_t)data()[p->pos] : -1; }
};

struct LittleFSStub {
    bool begin(bool formatOnFail = false) {
        g_disk.formatRequested = formatOnFail;
        if (!g_disk.failMount) return true;
        if (!formatOnFail) return false;
        g_disk.files.clear();
        return true;
    }
    void end() { g_disk.mounted = false; }
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
        if (dirs.count(ps)) {
            auto d = std::make_shared<FileImpl>(); d->path = ps; d->isDir = true;
            g_disk.openDirectories++;
            d->directoryRevision = g_disk.directoryRevision;
            std::string pre = ps + "/";
            for (auto& kv : g_disk.files) if (kv.first.rfind(pre, 0) == 0) d->dirNames.push_back(kv.first);
            return File(d);
        }
        auto f = std::make_shared<FileImpl>(); f->path = ps;
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
        auto it = g_disk.files.find(a); if (it == g_disk.files.end()) return false;
        g_disk.mutatedDirectory();
        g_disk.files[b] = it->second; g_disk.files.erase(a); return true;
    }
    size_t usedBytes() { return g_disk.used(); }
    size_t totalBytes() { return g_disk.capacity; }
    std::set<std::string> dirs;
};
extern LittleFSStub LittleFS;

// ---- Preferences ----------------------------------------------------------
struct Preferences {
    std::map<std::string, uint32_t> kv;
    bool begin(const char*, bool) { return true; }
    uint32_t getUInt(const char* k, uint32_t d) { auto it = kv.find(k); return it == kv.end() ? d : it->second; }
    void putUInt(const char* k, uint32_t v) { kv[k] = v; }
};

// ---- FreeRTOS -------------------------------------------------------------
typedef void* SemaphoreHandle_t;
#define portMAX_DELAY 0xffffffff
inline SemaphoreHandle_t xSemaphoreCreateMutex() { return (void*)1; }
extern int g_lockDepth;
inline int xSemaphoreTake(SemaphoreHandle_t, uint32_t) {
    if (g_lockDepth != 0) { fprintf(stderr, "FATAL: re-entrant FS_LOCK (would deadlock)\n"); exit(3); }
    g_lockDepth++; return 1;
}
inline int xSemaphoreGive(SemaphoreHandle_t) { g_lockDepth--; return 1; }
constexpr int pdPASS = 1;
inline int xTaskCreatePinnedToCore(void (*)(void*), const char*, int, void*, int, void*, int) { return pdPASS; }
inline unsigned g_stackHighWaterBytes = 4096;
inline unsigned uxTaskGetStackHighWaterMark(void*) { return g_stackHighWaterBytes; }
inline void vTaskDelay(uint32_t ms) { g_millis += ms; }
#define pdMS_TO_TICKS(x) (x)

// ---- hardware odds and ends (compile-only) ----------------------------------
struct TwoWire { void begin(int, int) {} };
extern TwoWire Wire;
#define AXP2101_SLAVE_ADDRESS 0x34
#define XPOWERS_AXP2101_ALL_IRQ 0xffffffff
#define XPOWERS_AXP2101_VBUS_REMOVE_IRQ 1
#define XPOWERS_AXP2101_VBUS_INSERT_IRQ 2
#define XPOWERS_AXP2101_PKEY_LONG_IRQ 4
#define XPOWERS_AXP2101_PKEY_SHORT_IRQ 8
#define XPOWERS_AXP2101_WARNING_LEVEL2_IRQ 16
#define XPOWERS_AXP2101_CHG_VOL_4V1 2
// PMU: tests set vbusIn (the USB power level) and the latched VBUS
// interrupt flags, then raise gPmuIrqFlag as the IRQ pin would.
struct XPowersAXP2101 {
    bool vbusIn = true, removeIrq = false, insertIrq = false;
    bool begin(TwoWire&, int, int, int) { return true; }
    void setALDO3Voltage(int) {} void enableALDO3() {} void disableALDO3() {}
    void setALDO4Voltage(int) {} void enableALDO4() {} void disableALDO4() {}
    void setButtonBatteryChargeVoltage(int) {} void enableButtonBatteryCharge() {}
    bool isEnableButtonBatteryCharge() { return true; }
    uint8_t chgVol = 3;
    bool setChargeTargetVoltage(uint8_t v) { chgVol = v; return true; }
    uint8_t getChargeTargetVoltage() { return chgVol; }
    float dieC = 30.0f; bool chargeEnabled = true;   // tests set dieC
    void enableTemperatureMeasure() {}
    float getTemperature(uint16_t* = nullptr) { return dieC; }
    void enableCellbatteryCharge() { chargeEnabled = true; }
    void disableCellbatteryCharge() { chargeEnabled = false; }
    void enableBattDetection() {} void enableBattVoltageMeasure() {}
    void enableVbusVoltageMeasure() {} void enableSystemVoltageMeasure() {}
    void disableIRQ(uint32_t) {} void enableIRQ(uint32_t) {}
    void clearIrqStatus() { removeIrq = insertIrq = false; }
    uint32_t getIrqStatus() { return 0; }
    bool isVbusRemoveIrq() { return removeIrq; } bool isVbusInsertIrq() { return insertIrq; }
    bool isPekeyLongPressIrq() { return false; } bool isDropWarningLevel2Irq() { return false; }
    bool isVbusIn() { return vbusIn; }
};
// GPS UART: tests append NMEA/UBX bytes to rx and inspect what the sketch
// sends in tx. Polling an empty port advances the clock 1 ms, so timeout
// loops in the sketch terminate instead of spinning.
struct HardwareSerial {
    std::string rx, tx;
    size_t rxPos = 0;
    HardwareSerial(int) {}
    void setRxBufferSize(int) {} void begin(int, int, int, int) {}
    int available() {
        if (rxPos >= rx.size()) { g_millis += 1; return 0; }
        return (int)(rx.size() - rxPos);
    }
    int read() { return rxPos < rx.size() ? (uint8_t)rx[rxPos++] : -1; }
    size_t write(uint8_t c) { tx.push_back((char)c); return 1; }
    size_t write(const uint8_t* b, size_t n) { tx.append((const char*)b, n); return n; }
};
#define SERIAL_8N1 0
#define WL_CONNECTED 3
#define WIFI_STA 1
#define WIFI_OFF 0
struct WiFiStub {
    int scanNetworks(bool, bool, bool, int) { return 0; }
    String SSID(int) { return String(""); }
    const uint8_t* BSSID(int) { static uint8_t b[6]; return b; }
    void mode(int) {} void setAutoReconnect(bool) {}
    void begin(const char*, const char*, int = 0, const uint8_t* = nullptr, bool = false) {}
    int status() { return 0; } void disconnect(bool) {}
};
extern WiFiStub WiFi;
struct WiFiClient {};
// HTTP: the response code is set by the test; each request's body is read
// through the sketch's stream exactly as HTTPClient would, and recorded.
// g_httpHook, if set, runs once the request starts, standing in for the
// other core acting mid-upload. As in the real client, a stream that ends
// early (available() < 0) fails the request with SEND_PAYLOAD_FAILED.
inline int g_httpCode = 200;
inline int g_httpRequests = 0;
inline std::string g_httpLastBody;
inline void (*g_httpHook)() = nullptr;
struct HTTPClient {
    void begin(WiFiClient&, const String&) {} void addHeader(const char*, const String&) {}
    int sendRequest(const char*, Stream* s, size_t size) {
        g_httpRequests++;
        g_httpLastBody.clear();
        if (g_httpHook) g_httpHook();
        char buf[1436];
        while (g_httpLastBody.size() < size && s->available() > 0) {
            size_t want = std::min(sizeof buf, size - g_httpLastBody.size());
            size_t n = s->readBytes(buf, want);
            if (n == 0) break;
            g_httpLastBody.append(buf, n);
        }
        if (g_httpLastBody.size() != size) return -3;   // HTTPC_ERROR_SEND_PAYLOAD_FAILED
        return g_httpCode;
    }
    void end() {}
};
#define ESP_EXT1_WAKEUP_ALL_LOW 0
inline void esp_sleep_enable_ext1_wakeup(uint64_t, int) {}
// Returns, unlike the real call, so tests can count it and look around.
inline int g_deepSleeps = 0;
inline void esp_deep_sleep_start() { g_deepSleeps++; }
