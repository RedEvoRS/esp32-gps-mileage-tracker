#include "gps_drain_monitor.h"
#include <cassert>
#include <cstdio>

int main() {
    GpsDrainMonitor monitor;
    assert(!monitor.observe(0)); // zero is a valid first timestamp
    assert(!monitor.observe(1500));
    assert(monitor.count() == 0 && monitor.maximumMs() == 1500);
    assert(monitor.observe(3001));
    assert(monitor.count() == 1 && monitor.maximumMs() == 1501);
    assert(monitor.lastGapMs() == 1501 && monitor.reportDue(3001));
    assert(monitor.observe(9001));
    assert(monitor.count() == 2 && monitor.maximumMs() == 6000);
    assert(!monitor.reportDue(63000));
    assert(monitor.reportDue(63001)); // exact report boundary
    assert(!monitor.observe(9002) && monitor.maximumMs() == 6000);

    GpsDrainMonitor wrap;
    assert(!wrap.observe(UINT32_MAX - 1000));
    assert(!wrap.observe(499)); // exactly 1500 ms across wrap
    assert(wrap.observe(2000));
    assert(wrap.count() == 1 && wrap.maximumMs() == 1501);
    GpsDrainMonitor reporting;
    assert(reporting.reportDue(UINT32_MAX - 1000));
    assert(!reporting.reportDue(58998));
    assert(reporting.reportDue(58999));
    std::puts("GPS drain monitor: 3 scenarios passed (threshold/max, drain wrap, report wrap)");
}
