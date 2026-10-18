// =================================================================
// Stage 1: Supreme Hello World / Toolchain Test (no PMU access)
// =================================================================
// Purpose: Check USB serial, the ESP32-S3 build options, and reset cause.
// What to expect: chip/memory details once, then a heartbeat every 2 s.
// Open Serial Monitor at 115200 baud. No MAC address is printed.
//
// Supreme upload settings (for the owner when the board arrives):
//   Before ANY power, attach the 915 MHz LoRa antenna: factory firmware
//   may transmit. Back up the factory flash before the first upload.
//   Arduino IDE: ESP32S3 Dev Module, USB CDC On Boot Enabled,
//   USB Mode Hardware CDC and JTAG, Flash Size 8 MB, PSRAM QSPI PSRAM.
//   CLI FQBN (ESP32 core 3.3.11):
//   esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
//   Select the Supreme's port, then Upload. If native USB disappears,
//   hold BOOT, tap RST, release BOOT; select the new port and retry.
//   PSRAM is UNCONFIRMED on this board. Start with QSPI (enabled), from
//   LilyGO's boards/t-beams3-supreme.json memory_type=qio_qspi. Check the
//   printed PSRAM size; if zero, try OPI PSRAM (PSRAM=opi) and check again.
//   Wrong PSRAM settings may prevent boot; use BOOT/RST to recover.
//
// This stage deliberately leaves every PMU rail as it was at boot.
// Source: Xinyuan-LilyGO/LilyGo-LoRa-Series, commit
// 1991b1ed70915d4f246b841f99f6027166ea7330 (bench verification pending).
// =================================================================

#include <esp_system.h>

static const char* resetReasonText(esp_reset_reason_t reason) {
    switch (reason) {
        case ESP_RST_POWERON:    return "power on";
        case ESP_RST_EXT:        return "external reset";
        case ESP_RST_SW:         return "software reset";
        case ESP_RST_PANIC:      return "panic/exception";
        case ESP_RST_INT_WDT:    return "interrupt watchdog";
        case ESP_RST_TASK_WDT:   return "task watchdog";
        case ESP_RST_WDT:        return "other watchdog";
        case ESP_RST_DEEPSLEEP:  return "deep sleep wake";
        case ESP_RST_BROWNOUT:   return "brownout";
        case ESP_RST_SDIO:       return "SDIO reset";
        case ESP_RST_USB:        return "USB reset";
        case ESP_RST_JTAG:       return "JTAG reset";
        case ESP_RST_EFUSE:      return "eFuse error";
        case ESP_RST_PWR_GLITCH: return "power glitch";
        case ESP_RST_CPU_LOCKUP: return "CPU lockup";
        default:                return "unknown";
    }
}

void setup() {
    Serial.begin(115200);
    // Wait briefly for native USB, but still run with no PC attached.
    uint32_t started = millis();
    while (!Serial && millis() - started < 3000) delay(10);

    Serial.println("\nT-Beam Supreme is alive! Stage 1: Hello World");
    Serial.printf("Chip: %s, revision %u, cores %u, CPU %u MHz\n",
                  ESP.getChipModel(), ESP.getChipRevision(),
                  ESP.getChipCores(), (unsigned)ESP.getCpuFreqMHz());
    Serial.printf("Flash: %lu bytes | PSRAM: %lu bytes\n",
                  (unsigned long)ESP.getFlashChipSize(),
                  (unsigned long)ESP.getPsramSize());
    esp_reset_reason_t reason = esp_reset_reason();
    Serial.printf("Reset reason: %s (%d)\n", resetReasonText(reason), (int)reason);
    Serial.println("PSRAM zero? Try the other PSRAM option described above.");
    Serial.println("No PMU access in this sketch; boot rail states are unchanged.");
}

void loop() {
    Serial.printf("Heartbeat - Supreme running for %lu seconds\n",
                  (unsigned long)(millis() / 1000));
    delay(2000);
}
