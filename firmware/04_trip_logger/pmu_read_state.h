#pragma once

// Raw XPowersLib register results retain -1 on I2C failure. Never interpret
// that sentinel as a cleared bit in a rail or power-source decision.
namespace pmu_read {
enum class Aldo3State { Off, On, Unknown };
enum class VbusState { Present, Absent, Unknown };

inline Aldo3State aldo3State(int control) {
    if (control < 0) return Aldo3State::Unknown;
    return (control & (1 << 2)) ? Aldo3State::On : Aldo3State::Off;
}

inline VbusState vbusState(int status1, int status2) {
    if (status1 < 0 || status2 < 0) return VbusState::Unknown;
    // XPowersLib 0.3.3 isVbusIn(): STATUS2 bit 3 clear AND STATUS1 bit 5 set.
    return !(status2 & (1 << 3)) && (status1 & (1 << 5))
        ? VbusState::Present : VbusState::Absent;
}
}
