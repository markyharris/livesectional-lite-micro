#!/usr/bin/env bash
# flash_and_upload_esp32.sh - one-shot setup for a classic ESP32-WROOM-32
# dev board (e.g. HW-463), for bench-testing metar-map-micro before the
# ESP32-S3 boards arrive. Same flow as flash_and_upload.sh, adjusted for
# the original ESP32: different chip flag, firmware goes at 0x1000, and
# these boards use a USB-UART bridge (no native USB).
#
# Usage:
#   ./flash_and_upload_esp32.sh /dev/ttyUSB0 ~/Downloads/ESP32_GENERIC-*.bin
#
# Get the firmware from https://micropython.org/download/ESP32_GENERIC/
# (the standard build - these boards have no PSRAM, so skip the SPIRAM
# and OTA variants).
# Port: Linux /dev/ttyUSB0; macOS /dev/cu.usbserial* or
# /dev/cu.wchusbserial*; Windows - use Thonny instead (Tools > Options
# > Interpreter > Install MicroPython, ESP32_GENERIC build), then
# upload the six files from Thonny's file panel.
# If esptool cannot connect ("Failed to connect"), hold the board's
# BOOT button while it tries.
#
# Requires: python3 with esptool and mpremote (pip install esptool mpremote).
set -euo pipefail

PORT="${1:?usage: flash_and_upload_esp32.sh <serial-port> <firmware.bin>}"
FW="${2:?usage: flash_and_upload_esp32.sh <serial-port> <firmware.bin>}"
cd "$(dirname "$0")"

echo "== erasing flash on $PORT =="
esptool.py --chip esp32 --port "$PORT" erase_flash

echo "== writing MicroPython firmware: $FW =="
esptool.py --chip esp32 --port "$PORT" --baud 460800 write_flash -z 0x1000 "$FW"

echo "== waiting for the board to come back =="
sleep 3

echo "== uploading metar-map-micro files =="
mpremote connect "$PORT" cp boot.py main.py metar_micro.py scanner.py airports config.json :

echo "== resetting into the app (serial console follows; Ctrl-] to exit) =="
mpremote connect "$PORT" reset
mpremote connect "$PORT" repl
