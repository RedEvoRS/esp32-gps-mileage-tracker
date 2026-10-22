# The CLI checksum is CI's Linux x86-64 archive. Use linux/amd64 explicitly.
FROM debian:13.1-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 git bash passwd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 builder \
    && mkdir /opt/arduino /opt/libraries \
    && chown builder:builder /opt/arduino /opt/libraries
COPY pins.env /opt/builder/pins.env
RUN . /opt/builder/pins.env \
    && curl -fsSL --retry 3 -o /tmp/arduino.tar.gz \
       "https://github.com/arduino/arduino-cli/releases/download/v${ARDUINO_CLI_VERSION}/arduino-cli_${ARDUINO_CLI_VERSION}_Linux_64bit.tar.gz" \
    && printf '%s  %s\n' "$ARDUINO_CLI_SHA256" /tmp/arduino.tar.gz | sha256sum -c - \
    && tar -xzf /tmp/arduino.tar.gz -C /usr/local/bin arduino-cli \
    && rm /tmp/arduino.tar.gz \
    && printf 'directories:\n  data: /opt/arduino\n  user: /opt/libraries\n' > /opt/arduino-cli.yaml
ENV ARDUINO_CONFIG_FILE=/opt/arduino-cli.yaml
USER builder
RUN --mount=type=cache,id=mileage-arduino-downloads,target=/opt/arduino/staging,uid=1000,gid=1000 \
    . /opt/builder/pins.env \
    && arduino-cli core update-index --additional-urls "$ESP32_INDEX_URL" \
    && arduino-cli core install "esp32:esp32@$ESP32_CORE_VERSION" --additional-urls "$ESP32_INDEX_URL" \
    && arduino-cli lib update-index \
    && arduino-cli lib install "XPowersLib@$XPOWERS_VERSION" "TinyGPSPlus@$TINYGPS_VERSION" "U8g2@$U8G2_VERSION" \
    # Stage 4's S3 recipes need only Xtensa, S3 SDK libraries and elf2image.
    # Prune in this layer: a later RUN would retain the installed bytes.
    && for tool in /opt/arduino/packages/esp32/tools/*; do \
         case "${tool##*/}" in \
           esp-x32|esp32s3-libs|esptool_py) ;; \
           *) rm -rf -- "$tool" ;; \
         esac; \
       done \
    # DFU flashing is done on the PC, never by this offline compiler image.
    && rm -rf /opt/arduino/packages/arduino/tools/dfu-util \
    && rm -rf /opt/arduino/staging/*
COPY builder.py /opt/builder/builder.py
ENTRYPOINT ["python3", "/opt/builder/builder.py"]
