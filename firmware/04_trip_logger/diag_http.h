// HTTPClient integration with a single wall-clock deadline across DNS, connect,
// writes, headers and body. Plain NetworkClient's per-operation timeout alone
// cannot bound a slow trickle or its internal select/write retry loop.
#pragma once
#ifndef JOURNAL_HOST_TEST
#include <lwip/dns.h>
#include <lwip/tcpip.h>
#include <lwip/sockets.h>
#include <cerrno>

namespace diagnostics {
// At most one pending query; callback data outlives both request and timeout.
// A timed-out lookup is reused on the next opportunity, never duplicated.
struct DnsLookup {
    std::atomic<unsigned> state{0}; // 0 idle, 1 pending, 2 complete
    char host[254] = {};
    ip_addr_t address;
    bool found = false;
    static void done(const char*, const ip_addr_t* address, void* arg) {
        auto& self = *static_cast<DnsLookup*>(arg);
        self.found = address && IP_IS_V4(address);
        if (self.found) self.address = *address;
        self.state.store(2, std::memory_order_release);
    }
    static void start(void* arg) {
        auto& self = *static_cast<DnsLookup*>(arg);
        err_t result = dns_gethostbyname(self.host, &self.address, done, arg);
        if (result != ERR_INPROGRESS) done(self.host, result == ERR_OK ? &self.address : nullptr, arg);
    }
};
static DnsLookup diagnosticDns;
#endif
#ifdef JOURNAL_HOST_TEST
namespace diagnostics {
#endif
class DeadlineClient : public WiFiClient {
    uint32_t started_;
public:
    explicit DeadlineClient(uint32_t started) : started_(started) {}
    uint32_t remaining() const {
        uint32_t elapsed = millis() - started_;
        return gNetHold.load() || gNetAbort.load() || elapsed >= kDrainMs ? 0 : kDrainMs - elapsed;
    }
    int connect(const char* host, uint16_t port, int32_t timeout) override {
#ifdef JOURNAL_HOST_TEST
        return remaining() ? WiFiClient::connect(host, port, min(remaining(), uint32_t(timeout))) : 0;
#else
        IPAddress ip;
        if (!ip.fromString(host)) {
            auto& dns = diagnosticDns;
            if (dns.state.load(std::memory_order_acquire) == 0) {
                if (std::strlen(host) >= sizeof(dns.host)) return 0;
                std::strcpy(dns.host, host); dns.state.store(1, std::memory_order_release);
                if (tcpip_try_callback(DnsLookup::start, &dns) != ERR_OK) {
                    dns.state.store(0, std::memory_order_release); return 0;
                }
            }
            while (dns.state.load(std::memory_order_acquire) == 1 && remaining()) vTaskDelay(pdMS_TO_TICKS(1));
            if (!remaining()) return 0;
            bool found = dns.found && !std::strcmp(host, dns.host);
            if (found) ip = IPAddress(ip4_addr_get_u32(ip_2_ip4(&dns.address)));
            dns.state.store(0, std::memory_order_release);
            if (!found) return 0;
        }
        uint32_t left = remaining();
        if (!left) return 0;
        return WiFiClient::connect(ip, port, int32_t(min(left, uint32_t(timeout))));
#endif
    }
    int available() override { if (!remaining()) { stop(); return 0; } return WiFiClient::available(); }
    uint8_t connected() override { if (!remaining()) { stop(); return 0; } return WiFiClient::connected(); }
    int read() override { return remaining() ? WiFiClient::read() : -1; }
    int read(uint8_t* data, size_t size) override { return remaining() ? WiFiClient::read(data, size) : -1; }
    size_t readBytes(char* data, size_t size) override {
        size_t n = 0;
        while (n < size && remaining()) {
            int r = read(reinterpret_cast<uint8_t*>(data) + n, size - n);
            if (r < 0) break;
            if (r) n += size_t(r); else vTaskDelay(pdMS_TO_TICKS(1));
        }
        return n;
    }
    size_t write(const uint8_t* data, size_t size) override {
#ifdef JOURNAL_HOST_TEST
        setTimeout(remaining());
        return remaining() ? WiFiClient::write(data, size) : 0;
#else
        size_t n = 0;
        while (n < size && remaining() && fd() >= 0) {
            int r = ::send(fd(), data + n, size - n, MSG_DONTWAIT);
            if (r > 0) n += size_t(r);
            else if (r < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) break;
            else vTaskDelay(pdMS_TO_TICKS(1));
        }
        return n;
#endif
    }
};
} // namespace diagnostics
