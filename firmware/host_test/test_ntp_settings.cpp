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
    assert(!first);
#elif defined(TEST_NTP_OVERRIDES)
    assert(!std::strcmp(first, "clock.example"));
#else
    assert(!std::strcmp(first, "pool.ntp.org"));
#endif
#if defined(TEST_NTP_EMPTY_SECONDARY) || defined(TEST_NTP_EMPTY_BOTH)
    assert(!second);
#elif defined(TEST_NTP_OVERRIDES)
    assert(!std::strcmp(second, "192.0.2.123"));
#else
    assert(!std::strcmp(second, "time.nist.gov"));
#endif
    std::cout << "NTP settings: static server pointers and empty-slot policy passed\n";
}
