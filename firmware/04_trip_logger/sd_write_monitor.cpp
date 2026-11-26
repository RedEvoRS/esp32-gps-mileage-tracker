#include <SD.h>
#include <esp_arduino_version.h>
#include <ff.h>
#include <diskio_impl.h>
#include "sd_write_check.h"

static_assert(ESP_ARDUINO_VERSION == ESP_ARDUINO_VERSION_VAL(3, 3, 11),
              "Review Stage 4's SD disk callbacks and drive allocation before changing the ESP32 core");

// These C++ symbols are defined by the pinned Arduino SD library in
// sd_diskio.cpp. ESP-IDF offers no getter for its registered callback table.
DSTATUS ff_sd_initialize(uint8_t drive);
DSTATUS ff_sd_status(uint8_t drive);
DRESULT ff_sd_read(uint8_t drive, uint8_t* data, DWORD sector, UINT count);
DRESULT ff_sd_write(uint8_t drive, const uint8_t* data, DWORD sector, UINT count);
DRESULT ff_sd_ioctl(uint8_t drive, uint8_t cmd, void* data);

static DRESULT monitoredWrite(uint8_t drive, const uint8_t* data, DWORD sector, UINT count) {
    const DRESULT result = ff_sd_write(drive, data, sector, count);
    if (result != RES_OK) ++sdWriteFailureCount();
    return result;
}

bool beginMonitoredSd(uint8_t pin, SPIClass& spi, uint32_t frequency) {
    // Stage 4 mounts SD only from an unmounted state with no competing
    // mount. sdcard_init takes this same first free drive inside SD.begin.
    BYTE drive;
    if (ff_diskio_get_drive(&drive) != ESP_OK ||
        !SD.begin(pin, spi, frequency, "/sd", 10, false)) return false;

    static const ff_diskio_impl_t driver{
        ff_sd_initialize, ff_sd_status, ff_sd_read, monitoredWrite, ff_sd_ioctl
    };
    // SD.end unregisters this table through the normal Arduino teardown.
    ff_diskio_register(drive, &driver);
    return true;
}
