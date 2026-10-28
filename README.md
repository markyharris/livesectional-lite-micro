# LS Lite Micro

The microcontroller sibling of **LiveSectional Lite**: same map, same
behavior, same data source — running on an ESP32-S3 under MicroPython
instead of a Raspberry Pi. METARs only, one LED per airport, driven from
a single bulk download.

## How it works

Every 5 minutes the board downloads the Aviation Weather Center cache
file over HTTPS:

```
https://aviationweather.gov/data/cache/metars.cache.xml.gz
```

That's ~360 KB gzipped / ~3.8 MB of XML — far too much to hold in RAM,
so it is inflated and scanned **as a stream** (`scanner.py`): each
`<METAR>` block is extracted as it flows past, only the newest report
for each station in `airports` is kept, and everything else is
discarded. Peak memory is one network chunk plus one METAR block. If a
fetch fails, the map keeps showing the previous frame — it never blanks
and never crashes (a 5-minute watchdog also reboots a wedged board).

Colors are the standard LiveSectional scheme: VFR green, MVFR blue, IFR
red, LIFR magenta, and dim white ("silver") for a station that isn't
reporting. If a report has no `flight_category`, it is computed from
visibility and ceiling (lowest BKN/OVC layer) using the standard FAA
thresholds. Legend (`LGND`) LEDs mirror the Pi version exactly: a
legend position can name its role — `LGND:VFR`, `LGND:MVFR`,
`LGND:IFR`, `LGND:LIFR`, `LGND:NODATA`, `LGND:WIND`, `LGND:TS` — and a
bare `LGND` takes the next role in that order. The WIND legend blinks
VFR green like a windy airport; the TS legend sits on IFR red with
bright-yellow flashes (steady yellow if lightning is off).

## Settings

Edited on the board's web page, stored in `config.json`, picked up on
the next cycle. The page opens with a live status box (map on/off, last
fetch, stations reporting, per-category counts, board IP) that
refreshes itself every 30 seconds, then a **Stations** section —
every airport with its category color and its raw METAR on hover/tap,
with a filter box — then the settings form, then an **airports-file
upload** (same validation as the Pi page: an original LiveSectional
airports backup uploads as-is; the replaced list is kept as
`airports.bak` and the board reboots to load the new list). The page
also has a map on/off button — off darkens every LED (legend included)
while data keeps fetching, so turning it back on is instant — and a
**Reboot board** button (a microcontroller has no shutdown; for fully
off, use the map-off button or cut its power).

1. **Map name** — optional; shown in the page title and heading (handy
   with several maps).
2. **Brightness** — 0–100% applied to every LED.
3. **Night mode** — between the configured times (default 22:00–06:00
   local) brightness scales to the night level (`night_brightness`,
   default 25% of normal; 0 = map completely off overnight). Local
   time = UTC + `tz_offset_min`
   (default −420 = Arizona, which has no DST; adjust if you travel the
   map or live elsewhere).
4. **High winds** — stations at/above 15 kt (wind or gust) blink their
   flight-category color on a ~1 s cycle.
5. **Lightning** — stations reporting TS/LTNG keep their category color
   as the baseline with a double bright-yellow flash (a 1.5 s cycle).
6. **Use wipe** — on (default), each update blanks the string for
   0.5 s, then a 1.5 s wipe sweeps the new colors in; off, updates land
   as an instant cut.

## What you need

- An **ESP32-S3 dev board with PSRAM** (e.g. ESP32-S3-DevKitC-1,
  8 MB flash / 8 MB PSRAM). A plain ESP32 (WROOM) will very likely work
  too — the streaming design keeps RAM use low — but the S3 + PSRAM is
  the configuration this was written for and removes all doubt.
- MicroPython firmware for ESP32-S3, **v1.22 or newer** (the code needs
  the `deflate` module for streaming gzip, plus `uasyncio` and
  `neopixel`, all in the official generic ESP32-S3 build).
- Your WS2812 LED strip/map, wired as below.

## Wiring

- Strip **5 V** and **GND** from a proper 5 V supply (do not power 183
  LEDs from the dev board's regulator).
- **Power the board separately** — its own USB power brick is ideal.
  Feeding the board from the strip's shared 5 V rail browned it out
  during the Wi-Fi join (learned the hard way, Oct 2026): the map kept
  its last frame but never joined the network.
- **Common ground** between the supply and the board.
- Strip **DATA** ← board **GPIO 5** (default; change `led_pin` in
  config or on the web page).
- The ESP32 outputs 3.3 V logic; most WS2812 strips accept it on short
  runs. If you see flicker or wrong colors on the first LEDs, add a
  74AHCT125 level shifter on the data line and a ~330 Ω series resistor.

## Install

1. Flash MicroPython (one time). With the board in download mode:

   ```bash
   pip install esptool mpremote
   esptool.py --chip esp32s3 --port /dev/ttyACM0 erase_flash
   esptool.py --chip esp32s3 --port /dev/ttyACM0 write_flash -z 0 \
       GENERIC_S3-SPIRAM-OCT-*.bin     # firmware from micropython.org
   ```

   (Thonny can also install the firmware from its interpreter dialog —
   use the ESP32-S3 build with SPIRAM if your board has octal PSRAM.)

2. Edit `config.json`: set `wifi_ssid` and `wifi_password`.

3. Upload all six files to the board:

   ```bash
   mpremote connect /dev/ttyACM0 cp boot.py main.py metar_micro.py \
       scanner.py airports config.json :
   ```

4. Reset the board. `boot.py` joins Wi-Fi and syncs the clock by NTP;
   `main.py` starts the map and the settings page. Watch progress on
   the serial console (`mpremote connect /dev/ttyACM0 repl`).

5. Open `http://<board-ip>/` (the IP is printed at boot) to change
   settings. Display settings apply on the next cycle; Wi-Fi and GPIO
   changes apply after a reboot.

No LEDs attached? The code falls back to printing each frame as a line
of category letters on the serial console, so you can smoke-test the
whole data path before wiring anything.

## Differences from the Pi version

- One processor does everything: the settings page can pause briefly
  during the 5-minute fetch (the download + inflate monopolizes the
  board for some seconds). The Pi version runs its web app separately.
- Local time comes from NTP + a fixed UTC offset instead of the OS
  timezone; there is no DST handling (a non-issue in Arizona).
- Status lives in memory instead of a `status.json` file; the web page
  shows the same data and polls the same JSON at `/api/status`.
- No Map Builder — that stays Pi-only.
- No git on the board, so updates work differently from the Pi's pull:
  the admin page checks this repo hourly, and **Update now** downloads
  the changed code files (`boot.py`, `main.py`, `metar_micro.py`,
  `scanner.py`) over HTTPS, verifies each against the git blob SHA
  GitHub publishes for it, swaps it in (old copy kept as `<file>.bak`)
  and reboots. `config.json` and `airports` are never touched. The
  airports list also uploads from the web page, just like on the Pi.

## Testing status

**Running on real hardware since Oct 28, 2026** (ESP32-S3 board #1):
MicroPython v1.29.0 flashed by hand with esptool, Wi-Fi join, NTP,
live HTTPS fetch ("fetch ok: 162 of 166 stations reporting"), NeoPixel
output on a real strip, the watchdog, and the settings page all proven
in daily use. If red and green come up swapped on your strip, set
`"color_order": "RGB"` in `config.json` (or on the web page) — some
strips are wired RGB instead of the WS2812-standard GRB.

The Oct 28, 2026 feature-parity port (tagged legends, silver no-data,
category-first lightning, blank-then-wipe + Use Wipe, status box with
30 s auto-refresh, Stations section, airports upload) and the GitHub
self-update (hourly check, SHA-verified download-and-reboot; the blob
SHA implementation is byte-checked against `git hash-object`, and the
apply and corrupt-download paths are exercised end to end against the
real repo listing) are bench-tested under CPython — the scanner and
frame logic are shared pure Python:

- 38 unit checks: the scanner against the live cache file (162
  stations, raw METAR text now included), compute_frame scenarios for
  every legend role and both animation phases, the settings/config
  toggles, page rendering, and the upload validation + multipart
  parsing.
- 17 end-to-end checks against the running app (page, `/api/status`,
  settings save, airports upload good and bad). The download itself is
  stubbed locally for that run — the dev sandbox's HTTPS proxy can't
  be used by raw sockets — while the live TLS/deflate fetch is the
  hardware-proven path above.
- The HTTP chunked-transfer decoder and the CPython zlib stand-in for
  `deflate.DeflateIO` both round-trip test data byte-for-byte.
