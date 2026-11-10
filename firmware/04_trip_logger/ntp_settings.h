// Include after secrets.h. Existing private settings need no new defines.
#pragma once
#ifndef NTP_SERVER_1
#define NTP_SERVER_1 "pool.ntp.org"
#endif
#ifndef NTP_SERVER_2
#define NTP_SERVER_2 "time.nist.gov"
#endif

namespace network_time {
// lwIP retains these pointers. String settings become static arrays whose
// bytes live for the entire boot, never temporary buffers on the net stack.
constexpr char kServer1[] = NTP_SERVER_1;
constexpr char kServer2[] = NTP_SERVER_2;
constexpr const char* server1() { return kServer1[0] ? kServer1 : nullptr; }
constexpr const char* server2() { return kServer2[0] ? kServer2 : nullptr; }
} // namespace network_time
