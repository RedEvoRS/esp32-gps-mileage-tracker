// Pure PCF8563 calendar/decision helpers. No Arduino, timezone or time_t limits.
#pragma once
#include <cstdint>

namespace diagnostics {
namespace rtc {
enum class Status { Valid, Unavailable, Stopped, InvalidDate, OutOfRange, ReadFailed };
struct Date { unsigned year, month, day, hour, minute, second; };
inline bool leap(unsigned y) { return y % 4 == 0 && (y % 100 != 0 || y % 400 == 0); }
inline unsigned monthDays(unsigned y, unsigned m) {
    const unsigned days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    return m >= 1 && m <= 12 ? days[m-1] + (m == 2 && leap(y)) : 0;
}
inline bool decodeBcd(uint8_t v, unsigned& out) {
    if ((v & 15) > 9 || (v >> 4) > 9) return false;
    out = (v >> 4) * 10 + (v & 15); return true;
}
inline uint8_t encodeBcd(unsigned v) { return uint8_t((v / 10) * 16 + v % 10); }
inline uint32_t toUtc(const Date& d) {
    uint32_t days = 0;
    for (unsigned y = 1970; y < d.year; ++y) days += leap(y) ? 366 : 365;
    for (unsigned m = 1; m < d.month; ++m) days += monthDays(d.year, m);
    return (days + d.day - 1) * 86400u + d.hour * 3600u + d.minute * 60u + d.second;
}
inline Date fromUtc(uint32_t seconds) {
    Date d{1970, 1, 1, 0, 0, 0};
    uint32_t days = seconds / 86400;
    d.second = seconds % 60; d.minute = seconds / 60 % 60; d.hour = seconds / 3600 % 24;
    while (days >= (leap(d.year) ? 366u : 365u)) { days -= leap(d.year) ? 366 : 365; ++d.year; }
    while (days >= monthDays(d.year, d.month)) { days -= monthDays(d.year, d.month); ++d.month; }
    d.day += days; return d;
}
constexpr uint32_t kFirstUtc = 1767225600u, kLastUtc = 4102444799u;
inline Status validate(uint8_t control, const uint8_t* r, uint32_t& utc) {
    if ((control & 0x20) || (r[0] & 0x80)) return Status::Stopped;
    Date d{}; unsigned year;
    if (!decodeBcd(r[0] & 0x7f, d.second) || !decodeBcd(r[1] & 0x7f, d.minute) ||
        !decodeBcd(r[2] & 0x3f, d.hour) || !decodeBcd(r[3] & 0x3f, d.day) ||
        !decodeBcd(r[5] & 0x1f, d.month) || !decodeBcd(r[6], year)) return Status::InvalidDate;
    d.year = (r[5] & 0x80 ? 1900 : 2000) + year; // C=0 is this project's 20xx convention
    if (d.second > 59 || d.minute > 59 || d.hour > 23 || d.month < 1 || d.month > 12 ||
        d.day < 1 || d.day > monthDays(d.year, d.month)) return Status::InvalidDate;
    if (d.year < 2026 || d.year > 2099) return Status::OutOfRange;
    utc = toUtc(d); return Status::Valid;
}
inline bool encode(uint32_t utc, uint8_t* r) {
    if (utc < kFirstUtc || utc > kLastUtc) return false;
    Date d = fromUtc(utc);
    r[0] = encodeBcd(d.second); r[1] = encodeBcd(d.minute); r[2] = encodeBcd(d.hour);
    r[3] = encodeBcd(d.day); r[4] = uint8_t((utc / 86400 + 4) % 7);
    r[5] = encodeBcd(d.month); r[6] = encodeBcd(d.year % 100); return true;
}
inline bool aligned(uint64_t utcMs) { return utcMs % 1000 <= 100; }
inline bool needsWrite(bool valid, uint32_t rtcUtc, uint32_t trustedUtc) {
    return !valid || (rtcUtc > trustedUtc ? rtcUtc - trustedUtc : trustedUtc - rtcUtc) >= 2;
}
inline const char* reason(Status s) {
    return s == Status::Stopped ? "stopped" : s == Status::InvalidDate ? "invalid_date" :
           s == Status::OutOfRange ? "out_of_range" : "read_failed";
}
} // namespace rtc
} // namespace diagnostics
