#pragma once
#include <cstdint>

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

inline bool vbackupOff(int control) {
    // XPowersLib 0.3.3 disableButtonBatteryCharge() and isEnableButtonBatteryCharge()
    // use CHARGE_GAUGE_WDT_CTRL (0x18) bit 2.
    return control >= 0 && (control & (1 << 2)) == 0;
}

inline bool chargeTargetMatches(int cvReg, uint8_t target) {
    if (cvReg < 0) return false;
    return (cvReg & 0x07) == (target & 0x07);
}

// XPowersParams.hpp masks put INTSTS1 in bits 0..7, INTSTS2 in 8..15,
// INTSTS3 in 16..23 (unlike getIrqStatus()'s return-value byte order).
struct IrqBatch {
    bool known;
    unsigned flags;
    uint8_t raw[3];
    bool has(unsigned mask) const { return known && (flags & mask) != 0; }
};

inline IrqBatch irqBatch(int first, int second, int third, unsigned enabled) {
    if (first < 0 || second < 0 || third < 0) return {false, 0, {0, 0, 0}};
    return {true, (unsigned(first) | (unsigned(second) << 8) |
                   (unsigned(third) << 16)) & enabled,
            {uint8_t(first), uint8_t(second), uint8_t(third)}};
}

// Write-1-to-clear only the bits read in this batch (or 0xFF at init).
// Returns true only when every nonzero write succeeds (XPowersLib returns 0).
template <typename Pmu>
inline bool clearIrqBytes(Pmu& pmu, const uint8_t regs[3], const uint8_t masks[3]) {
    bool ok = true;
    for (int i = 0; i < 3; ++i) {
        if (masks[i] != 0 && pmu.writeRegister(regs[i], masks[i]) != 0) ok = false;
    }
    return ok;
}

// Compute newly latched flags (excluding bits already handled during a prior
// failed clear), write-1-to-clear all currently latched bits in INTSTS1..3,
// and retain only bits whose clear write failed in `handled`.
// Returns true only when every nonzero clear write succeeds.
template <typename Pmu>
inline bool clearIrqBatch(Pmu& pmu, const uint8_t regs[3],
                          const uint8_t raw[3], uint8_t handled[3],
                          uint8_t freshOut[3]) {
    bool ok = true;
    for (int i = 0; i < 3; ++i) {
        handled[i] &= raw[i];
        freshOut[i] = uint8_t(raw[i] & ~handled[i]);
        if (raw[i] != 0) {
            if (pmu.writeRegister(regs[i], raw[i]) == 0) {
                handled[i] = 0;
            } else {
                handled[i] = raw[i];
                ok = false;
            }
        }
    }
    return ok;
}

// Fresh falling edges run immediately when not backing off from a failed
// read/clear. Failed batches and a steady active-low IRQ line retry at retryMs.
inline bool shouldPollIrq(bool edgeFlag, bool retry, bool pinLow,
                          uint32_t now, uint32_t lastReadMs, uint32_t retryMs) {
    if (!edgeFlag && !retry && !pinLow) return false;
    if ((retry || (!edgeFlag && pinLow)) && uint32_t(now - lastReadMs) < retryMs) return false;
    return true;
}

inline Aldo3State aldo3State(int control) {
    if (control < 0) return Aldo3State::Unknown;
    return (control & (1 << 2)) ? Aldo3State::On : Aldo3State::Off;
}

inline bool aldo1Ready(int control, int voltage) {
    if (control < 0 || voltage < 0) return false;
    // XPowersLib 0.3.3: 0x90 bit 0 enables ALDO1; 0x92 bits 0..4
    // select 500 mV + 100 mV/step. Check raw reads, not its masked getter.
    return (control & 1) && 500 + (voltage & 0x1F) * 100 == 3300;
}

inline bool aldo4VoltageReady(int voltage) {
    // XPowersLib 0.3.3: 0x95 bits 0..4 select 500 mV + 100 mV/step.
    return voltage >= 0 && 500 + (voltage & 0x1F) * 100 == 3300;
}

inline bool aldo4Ready(int control, int voltage) {
    // LDO_ONOFF_CTRL0 (0x90) bit 3 enables ALDO4.
    return control >= 0 && (control & (1 << 3)) && aldo4VoltageReady(voltage);
}

inline bool bldo1Off(int control) {
    // LDO_ONOFF_CTRL0 (0x90) bit 4 enables BLDO1.
    return control >= 0 && (control & (1 << 4)) == 0;
}

inline bool bldo1VoltageReady(int voltage) {
    // XPowersLib 0.3.3: LDO_VOL4_CTRL (0x96) bits 0..4 select 500 mV + 100 mV/step.
    return voltage >= 0 && 500 + (voltage & 0x1F) * 100 == 3300;
}

inline bool bldo1Ready(int control, int voltage) {
    // LDO_ONOFF_CTRL0 (0x90) bit 4 enables BLDO1.
    return control >= 0 && (control & (1 << 4)) && bldo1VoltageReady(voltage);
}

inline bool batteryDetectionEnabled(int detectionCtrl) {
    // XPowersLib 0.3.3 enableBattDetection(): BAT_DET_CTRL (0x68) bit 0.
    return detectionCtrl >= 0 && (detectionCtrl & 1) != 0;
}

inline bool batteryVoltageMeasureEnabled(int adcCtrl) {
    // XPowersLib 0.3.3 enableBattVoltageMeasure(): ADC_CHANNEL_CTRL (0x30) bit 0.
    return adcCtrl >= 0 && (adcCtrl & 1) != 0;
}

// XPowersLib 0.3.3 disableTSPinMeasure(): TS_PIN_CTRL (0x50) applies
// "value &= 0xF0" then writes "value | 0x10" (bits 3:0 clear, bit 4 set).
// It also clears ADC_CHANNEL_CTRL (0x30) bit 1.
constexpr int tsModeMask = 0x1F;
constexpr int tsFixedInput = 0x10;
constexpr int tsAdcEnable = 1 << 1;

inline bool tsFixedInputReady(int tsCtrl, int adcCtrl) {
    return tsCtrl >= 0 && adcCtrl >= 0 &&
           (tsCtrl & tsModeMask) == tsFixedInput && (adcCtrl & tsAdcEnable) == 0;
}

inline bool pmuMeasurementsReady(int adcCtrl) {
    if (adcCtrl < 0) return false;
    // XPowersLib 0.3.3: 0x30 bits 4, 3 and 2 enable die temperature,
    // system voltage and VBUS voltage measurements respectively.
    constexpr int channels = 0x1C;
    return (adcCtrl & channels) == channels;
}

inline VbusState vbusState(int status1, int status2) {
    if (status1 < 0 || status2 < 0) return VbusState::Unknown;
    // XPowersLib 0.3.3 isVbusIn(): STATUS2 bit 3 clear AND STATUS1 bit 5 set.
    return !(status2 & (1 << 3)) && (status1 & (1 << 5))
        ? VbusState::Present : VbusState::Absent;
}
}
