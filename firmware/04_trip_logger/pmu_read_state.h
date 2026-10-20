#pragma once

// Raw XPowersLib register results retain -1 on I2C failure. Never interpret
// that sentinel as a cleared bit in a rail or power-source decision.
namespace pmu_read {
enum class Aldo3State { Off, On, Unknown };
enum class VbusState { Present, Absent, Unknown };
enum class ChargeState { Paused, Charging, Unconfirmed };

inline ChargeState chargeState(int control) {
    if (control < 0) return ChargeState::Unconfirmed;
    return (control & (1 << 1)) ? ChargeState::Charging : ChargeState::Paused;
}

// XPowersParams.hpp masks put INTSTS1 in bits 0..7, INTSTS2 in 8..15,
// INTSTS3 in 16..23 (unlike getIrqStatus()'s return-value byte order).
struct IrqBatch {
    bool known;
    unsigned flags;
    bool has(unsigned mask) const { return known && (flags & mask) != 0; }
};

inline IrqBatch irqBatch(int first, int second, int third, unsigned enabled) {
    if (first < 0 || second < 0 || third < 0) return {false, 0};
    return {true, (unsigned(first) | (unsigned(second) << 8) |
                   (unsigned(third) << 16)) & enabled};
}

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
