// Defaults, independent overrides and unused slots, without Arduino or DNS.
#include <cassert>
#include <cstring>
#include <iostream>
#if defined(TEST_NTP_OVERRIDES)
#define NTP_SERVER_1 "clock.example"
#define NTP_SERVER_2 "192.0.2.123"
#elif defined(TEST_NTP_EMPTY_PRIMARY)
#define NTP_SERVER_1 ""
#elif defined(TEST_NTP_EMPTY_SECONDARY)
#define NTP_SERVER_2 ""
#elif defined(TEST_NTP_EMPTY_BOTH)
#define NTP_SERVER_1 ""
#define NTP_SERVER_2 ""
#endif
#include "ntp_settings.h"

int main() {
    // A constant pointer into the setting arrays also checks static lifetime.
    constexpr const char* first = network_time::server1();
    constexpr const char* second = network_time::server2();
#if defined(TEST_NTP_EMPTY_PRIMARY) || defined(TEST_NTP_EMPTY_BOTH)
    static_assert(!network_time::kUseServer1, "Empty first server must be unused");
    static_assert(first == nullptr, "Empty first server must pass nullptr to configTime");
#elif defined(TEST_NTP_OVERRIDES)
    static_assert(network_time::kUseServer1, "First override must be used");
    assert(!std::strcmp(first, "clock.example"));
#else
    static_assert(network_time::kUseServer1, "First default must be used");
    assert(!std::strcmp(first, "pool.ntp.org"));
#endif
#if defined(TEST_NTP_EMPTY_SECONDARY) || defined(TEST_NTP_EMPTY_BOTH)
    static_assert(!network_time::kUseServer2, "Empty second server must be unused");
    static_assert(second == nullptr, "Empty second server must pass nullptr to configTime");
#elif defined(TEST_NTP_OVERRIDES)
    static_assert(network_time::kUseServer2, "Second override must be used");
    assert(!std::strcmp(second, "192.0.2.123"));
#else
    static_assert(network_time::kUseServer2, "Second default must be used");
    assert(!std::strcmp(second, "time.nist.gov"));
#endif
    std::cout << "NTP settings: static server pointers and empty-slot policy passed\n";
}
