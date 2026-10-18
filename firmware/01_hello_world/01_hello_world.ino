// =================================================================
// Stage 1: Supreme Hello World / Toolchain Test (no PMU access)
// =================================================================
// Purpose: Check USB serial, the ESP32-S3 build options, and reset cause.
// What to expect: chip/memory details once, then a heartbeat every 2 s.
// Open Serial Monitor at 115200 baud. No MAC address is printed.
//
// Supreme upload settings (bench-confirmed 2026-10-18):
//   Before ANY power, attach the 915 MHz LoRa antenna: factory firmware
//   may transmit. Back up the factory flash before the first upload.
//   Arduino IDE: ESP32S3 Dev Module, USB CDC On Boot Enabled,
//   USB Mode Hardware CDC and JTAG, Flash Size 8 MB, PSRAM QSPI PSRAM.
//   CLI FQBN (ESP32 core 3.3.11):
//   esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=8M,PSRAM=enabled
//   Stage 1 printed flash 8388608 and PSRAM 8388608 bytes with QSPI.
//   Native USB-Serial/JTAG is VID:PID 303A:1001; the COM number varies.
//   With our sketches running, esptool auto-reset needs no buttons;
//   it failed under the factory Meshtastic firmware.
//   Manual download with an 18650 (USB unplug alone leaves it powered):
//   hold POWER ~6 s to switch off, hold BOOT, press POWER ~1 s, keep
//   BOOT held 2-3 s, then release. Without a battery: unplug USB, hold
//   BOOT, plug USB in, wait 2-3 s, then release BOOT.
//   After a manual-download flash, software/RTS reset returns to download
//   mode: the BOOT strap stays latched until a real reset. Press RESET
//   once to run the sketch.
//   Buttons (LilyGO photo, bench-confirmed): screen left, USB-C right.
//   RESET/POWER are a close pair near the middle, RESET on the left;
//   BOOT is on its own further right. Hold POWER ~6 s to switch off.
//
// This stage deliberately leaves every PMU rail as it was at boot.
// Source: Xinyuan-LilyGO/LilyGo-LoRa-Series, commit
// 1991b1ed70915d4f246b841f99f6027166ea7330. Build/USB confirmed above.
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
    Serial.println("Bench-confirmed: QSPI PSRAM, flash and PSRAM each 8388608 bytes.");
    Serial.println("No PMU access in this sketch; boot rail states are unchanged.");
}

void loop() {
    Serial.printf("Heartbeat - Supreme running for %lu seconds\n",
                  (unsigned long)(millis() / 1000));
    delay(2000);
}
