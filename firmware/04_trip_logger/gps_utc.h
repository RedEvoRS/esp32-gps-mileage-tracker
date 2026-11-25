#pragma once
#include <cstdint>
#include <ctime>
#include "rtc_pcf8563.h"

namespace gps_utc {

inline bool validRmcCalendar(const struct tm& t) {
    return t.tm_year >= 124 && t.tm_mon >= 0 && t.tm_mon <= 11 &&
           t.tm_mday >= 1 &&
           unsigned(t.tm_mday) <= diagnostics::rtc::monthDays(unsigned(t.tm_year) + 1900, t.tm_mon + 1) &&
           t.tm_hour >= 0 && t.tm_hour <= 23 && t.tm_min >= 0 && t.tm_min <= 59 &&
           t.tm_sec >= 0 && t.tm_sec <= 59;
}

// Howard Hinnant's days-from-civil conversion, independent of the local
// timezone and of the width of long on the firmware target.
inline int64_t timegmUtc(const struct tm& t) {
    int64_t year = int64_t(t.tm_year) + 1900;
    const int64_t month = int64_t(t.tm_mon) + 1;
    year -= (month <= 2);
    const int64_t era = (year >= 0 ? year : year - 399) / 400;
    const int64_t yearOfEra = year - era * 400;
    const int64_t dayOfYear = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + t.tm_mday - 1;
    const int64_t dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear;
    const int64_t days = era * 146097 + dayOfEra - 719468;
    return days * 86400LL + t.tm_hour * 3600LL + t.tm_min * 60LL + t.tm_sec;
}

} // namespace gps_utc
