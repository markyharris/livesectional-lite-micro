#!/usr/bin/env bash
# flash_and_upload.sh - one-shot setup for a fresh ESP32-S3-DevKitC-1-N8R8.
#
# Usage:
#   ./flash_and_upload.sh /dev/ttyACM0 ~/Downloads/ESP32_GENERIC_S3-SPIRAM-OCT-*.bin
#
# Get the firmware from https://micropython.org/download/ESP32_GENERIC_S3/
# (the SPIRAM-OCT build - "OCT" matches this board's octal PSRAM).
# On macOS the port is usually /dev/cu.usbmodem*; on Windows use Thonny
# instead (Tools > Options > Interpreter > Install MicroPython), then
# upload the six files from Thonny's file panel.
#
# Requires: python3 with esptool and mpremote (pip install esptool mpremote).
set -euo pipefail

PORT="${1:?usage: flash_and_upload.sh <serial-port> <firmware.bin>}"
FW="${2:?usage: flash_and_upload.sh <serial-port> <firmware.bin>}"
cd "$(dirname "$0")"

echo "== erasing flash on $PORT =="
esptool.py --chip esp32s3 --port "$PORT" erase_flash

echo "== writing MicroPython firmware: $FW =="
esptool.py --chip esp32s3 --port "$PORT" --baud 460800 write_flash -z 0 "$FW"

echo "== waiting for the board to come back =="
sleep 3

echo "== uploading metar-map-micro files =="
mpremote connect "$PORT" cp boot.py main.py metar_micro.py scanner.py airports config.json :

echo "== resetting into the app (serial console follows; Ctrl-] to exit) =="
mpremote connect "$PORT" reset
mpremote connect "$PORT" repl
