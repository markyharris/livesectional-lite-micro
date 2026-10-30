# LS Lite Micro — Complete Setup Guide (ESP32-S3)

This guide takes you from a bare circuit board to a working LED METAR
map: an ESP32-S3 microcontroller that downloads live aviation weather
every 5 minutes and lights one LED per airport — green for VFR, blue
for MVFR, red for IFR, magenta for LIFR, silver when a station isn't
reporting. It is the microcontroller sibling of **LiveSectional Lite**
(the Raspberry Pi version); the map, the colors, the data source and
the admin page all behave the same way.

No programming experience is assumed. You will flash ready-made
firmware, copy six files onto the board, type your Wi-Fi name and
password into one settings file, and wire three wires. Budget an hour
for the first build, most of it downloads and reading.

## Why the ESP32-S3 — and this exact variant — suits this job

The workload has an unusual shape, and it's worth understanding
because it explains every hardware choice below:

- Every 5 minutes the board makes an **encrypted (HTTPS) download of
  a ~4 MB weather file**, inflates it, and scans it for your stations.
- In between, it **drives up to 2,500 NeoPixel LEDs** with the strict
  pulse timing those LEDs demand, and **serves a small web page** so
  you can run the map from your phone.

The ESP32-S3 fits that shape unusually well for a ~$10 board:

- **Memory is the deciding factor.** An HTTPS (TLS) session plus the
  streaming machinery for a 4 MB file needs real headroom. This
  board's **8 MB of octal PSRAM** — on top of its internal RAM — means
  nothing ever runs close to the edge. The smaller ESP8266 (~80 KB of
  RAM) cannot hold a TLS session for this job at all, and the original
  ESP32 (~320 KB) is uncomfortably tight. When you shop, the variant
  code matters: **N8R8** means 8 MB flash (N8) and 8 MB octal PSRAM
  (R8). Boards with no PSRAM, or with the older quad-PSRAM, need a
  different firmware build and give up the headroom.
- **Two 240 MHz cores.** One core largely runs the Wi-Fi stack while
  your code runs on the other, so downloads don't stutter the LED
  output.
- **A dedicated RMT peripheral** generates the NeoPixel pulse train
  in hardware. The CPU isn't bit-banging timing-critical signals, so
  frames stay accurate even mid-download.
- **Native USB plus a USB-to-serial bridge** (the DevKitC-1 has two
  USB-C ports, one of each) — flashing and debugging need nothing but
  a USB cable, no adapter dongles.
- **8 MB of flash**: the MicroPython firmware, this project's files,
  your settings, and room to spare for the backups the board keeps.
- **First-class MicroPython support.** The whole application is plain
  Python files you can read and edit — no compiling — and the S3 is
  one of the best-supported MicroPython targets there is.

In short: it's the cheapest board that does the encrypted download
*comfortably*, and comfort is what makes the map boringly reliable.

## What you need

**Hardware**

- **ESP32-S3-DevKitC-1-N8R8** development board (the variant code is
  usually printed in the listing title; "ESP32-S3-DevKitC-1" alone
  isn't enough — check for **N8R8**).
- A **WS2812B LED strip or string** (the usual "NeoPixel" kind) and
  your map. Any length up to 2,500 LEDs.
- A **5 V power supply for the LEDs**, sized for your map. Budget up
  to ~60 mA per LED worst case; in practice a 150–250 LED map runs
  well on a 5 V / 10 A brick. When in doubt, size up — an undersized
  supply causes the weirdest faults in this hobby. This same supply
  also powers the board, through its 5V pin (Step 7) on most maps;
  on some, the board also wants a USB power connection — Step 7
  explains how to tell.
- A **USB-C data cable** (not a charge-only cable — many bundled
  cables can't carry data; if the board never shows up on the
  computer, suspect the cable first).
- A computer (Windows, macOS, or Linux) for the one-time setup.
- Optional but recommended: a **220–470 Ω resistor** for the data
  line (Step 7), and a **normally-open momentary pushbutton** if you
  want a reset button on the map frame (Step 7).

**Software (all free)**

- **Thonny** — the beginner-friendly Python editor that talks to the
  board: https://thonny.org
- The **MicroPython firmware** for this board (Step 1).
- This project's files (the repository you're reading): `boot.py`,
  `main.py`, `metar_micro.py`, `scanner.py`, `config.json`,
  `airports`.

## Two things that trip everyone up (read first)

1. **The wrong firmware variant.** This board needs the MicroPython
   build with **SPIRAM-OCT** in its name (for its octal PSRAM). The
   plain build for generic ESP32-S3 boards will not boot properly
   here. Step 1 points at the exact file.
2. **Red and green swapped.** LED strips come in two color orders,
   GRB (the common default) and RGB. If your map shows VFR airports
   red and IFR airports green, don't rewire anything — there's a
   **Color order** setting on the admin page (Step 8). Flip it, done.

## Step 1 — Get the firmware

1. Go to the MicroPython download page for this board family:
   **https://micropython.org/download/ESP32_GENERIC_S3/**
2. Find the section titled **"Firmware (Support for Octal-SPIRAM)"**.
3. Under **Releases**, download the newest **stable** release (the
   file name looks like
   `ESP32_GENERIC_S3-SPIRAM-OCT-<date>-v1.29.0.bin`). Skip anything
   labeled "preview".
4. Note where the `.bin` file landed (usually your Downloads folder).
   You'll point the flashing tool at it in Step 3.

## Step 2 — Install Thonny

Download Thonny from https://thonny.org and install it like any other
app. Thonny is the workshop for everything that follows: it flashes
the firmware, copies files to the board, and shows the board's
console — the scrolling text log that tells you what the board is
doing. When something misbehaves later, that console is where the
answer lives.

Get the **current** version from the site (5.x as of this writing;
anything 4.1 or newer works). An old 3.x copy already on your
computer will install but cannot flash this board correctly —
Step 3 explains why.

## Step 3 — Flash MicroPython onto the board

"Flashing" writes the MicroPython firmware into the board's memory.
Out of the box the board has no Python on it; this step puts it
there. You only do it once.

First, plug the board into your computer with the USB-C **data**
cable. The DevKitC-1 has two USB-C ports; use the one labeled **COM**
(it's the USB-to-serial port — on some boards the labels are **UART**
and **USB** instead; if your computer doesn't notice the board, try
the other port). The board will appear as a serial port: `COM3`-style
on Windows, `/dev/ttyACM0` on Linux, `/dev/cu.usbmodem…` on macOS.

**On Windows and macOS, use Thonny's built-in installer.** One
version warning first, learned the hard way: the installer only
handles the ESP32-S3 correctly in **Thonny 4.1 and newer** (the
current release is 5.x, which is what Step 2 gets you). Thonny 3.x
writes this chip's firmware to the wrong address — the install
reports success, "Hash of data verified" and all, and the board
then boot-loops printing `invalid header: 0xffffffff`. If
**Help → About** shows a 3.x version, upgrade before going any
further.

1. In Thonny, open **Tools → Options → Interpreter**. Choose
   **MicroPython (ESP32)** and your board's port, then click
   **Install or update MicroPython (esptool)**.
2. In the installer window, set **MicroPython family** to
   **ESP32-S3**. That family choice is what tells Thonny where in
   flash the firmware belongs — and it's the field the old version
   didn't have.
3. Point it at the firmware file from Step 1: click the **☰**
   button in the installer's corner, choose **Select local
   MicroPython image**, and pick the
   `ESP32_GENERIC_S3-SPIRAM-OCT-....bin` file. Using the local file
   guarantees the Octal-SPIRAM build; the installer's online
   variant list doesn't always offer it.
4. Click **Install** and wait for **Done!**. The installer writes
   the firmware only — files already on the board, settings
   included, are left alone.
5. Close the options windows. Back in Thonny's console you should
   see a `>>>` prompt — that's MicroPython alive on your board.
   Pressing the board's **RST** button restarts it; you'll see the
   ESP32 boot text scroll by. (One scary-looking line is fine:
   `E (25) qio_mode: Failed to set QIE bit...` is a harmless
   warning some of these boards print; the boot continues past it.)

**Alternate — flash by hand with esptool.** The installer above is
a front end for this same tool. If it ever gives you trouble, these
commands are the known-good fallback — they verify what they
wrote, byte for byte:

1. Open Thonny. At the bottom right, click the interpreter name and
   choose **Local Python 3** (your computer's own Python). This frees
   the serial port for the flashing tool.
2. Open **Tools → Open system shell…**. A terminal window appears.
3. Install the flashing tool (once):
   `pip install esptool`
4. Erase the board's flash (replace `COM3` with your board's port):
   `python -m esptool --chip esp32s3 --port COM3 erase_flash`
   You'll see the board's chip identified (it prints its MAC address
   — write it down; you'll recognize the board by it in your router's
   device list later). If esptool can't connect, hold the board's
   **BOOT** button, tap **RST**, release BOOT, and run the command
   again.
5. Write the firmware (one line; note the quotes — needed if your
   file path contains spaces):
   `python -m esptool --chip esp32s3 --port COM3 --baud 460800 write_flash -z 0 "C:\path\to\ESP32_GENERIC_S3-SPIRAM-OCT-....bin"`
   It ends with **"Hash of data verified."** — that line matters; it
   means the image on the board is exactly the file you downloaded.
   (If a flash ever produces a board that reboots in a loop printing
   `invalid header: 0xffffffff`, the firmware didn't land where the
   chip looks for it — erase and write again with these commands.)
6. Now switch Thonny back to the board: click the interpreter name
   (bottom right) and choose **MicroPython (ESP32)** and your board's
   port. The console panel should show a `>>>` prompt — that's
   MicroPython alive on your board. Pressing the board's **RST**
   button restarts it; you'll see the ESP32 boot text scroll by.

**On Linux** you can use the one-shot script bundled with this
project instead (same esptool process, plus it uploads the project
files from Step 5 in one go):

```
pip install esptool mpremote
./flash_and_upload.sh /dev/ttyACM0 ~/Downloads/ESP32_GENERIC_S3-SPIRAM-OCT-*.bin
```

If you take the script path, skip ahead to Step 4 — no wait, your
`config.json` still needs your Wi-Fi credentials (Step 4), and the
script uploads whatever `config.json` sits next to it. Do Step 4's
edit first, then run the script, then continue at Step 6.

## Step 4 — Set your Wi-Fi and settings in `config.json`

`config.json` is the board's settings file — plain text, edited on
your computer before uploading. Open the copy from this project in
Thonny (or any text editor) and fill in, at minimum:

- **`wifi_ssid`** and **`wifi_password`** — your 2.4 GHz home
  network (the ESP32 doesn't do 5 GHz Wi-Fi). Type them carefully;
  this file is the only place your password lives, and it never
  leaves the board.
- **`tz_offset_min`** — your timezone's offset from UTC, in minutes.
  It only affects displayed times and the night-dimming window.
  US standard-time offsets: Pacific −480, Mountain −420, Central
  −360, Eastern −300. The board has no daylight-saving rules, so add
  60 during daylight time if you observe it (Arizona builders: stay
  at −420 all year).

The settings you can leave alone until the map is running (all of
them are also on the admin page later): `brightness` (percent),
`night_mode` with `night_start`/`night_end` (dim overnight;
`night_brightness` is a percent *of* the normal brightness, 0 = fully
off overnight), `high_wind` (blink windy airports), `lightning`
(flash airports reporting thunderstorms), `use_wipe` (the
blank-then-sweep transition when new data arrives), `color_order`
(GRB or RGB — trip-up #2), `led_pin` (the GPIO driving the strip,
default 5), `map_name` (an optional label shown on the admin page),
`map_on`, and `watchdog` (a hardware self-reset if the board ever
hangs — leave it `true`).

Save the file when you're done.

## Step 5 — Upload the project files

The board needs six files, all from this project:

| File | What it does |
| --- | --- |
| `boot.py` | Runs first at power-up: joins Wi-Fi, syncs the clock |
| `main.py` | Starts the application |
| `metar_micro.py` | The application: fetching, LED rendering, the admin page, updates |
| `scanner.py` | The streaming parser for the weather file |
| `config.json` | Your settings (Step 4) |
| `airports` | Your layout: one entry per LED, in order |

In Thonny:

1. Choose **View → Files**. The panel shows your computer's files on
   top and the board's ("MicroPython device") below.
2. Browse to the folder holding the project files.
3. Select all six files, right-click, and choose **Upload to /**.
4. The board's side of the panel should now list the same six files.

(If you used the Linux script in Step 3, this already happened.)

## Step 6 — Bench test (before wiring the map)

Prove the software before touching a soldering iron:

1. With the board still on USB power from your computer, press
   **RST** and watch Thonny's console. A healthy boot looks like:

   ```
   [boot] joining Wi-Fi 'YourNetwork'...
   [boot] Wi-Fi up: ('192.168.1.50', '255.255.255.0', ...)
   [boot] clock synced (UTC): (2026, ...)
   [metar-micro] settings page on http://<board-ip>/
   [metar-micro] fetch ok: 152 of 166 stations reporting
   [metar-micro] update check: code is current with GitHub
   ```

   - *"fetch ok"* means the board joined your network, reached the
     aviation weather service, and parsed real data. The two numbers
     are how many of your mapped stations are reporting right now —
     the second number comes from your `airports` file.
   - The Wi-Fi line prints the board's **IP address** (the first of
     the numbers in parentheses). Write it down — that's where the
     admin page lives.
2. Open that address in a browser on the same network
   (`http://192.168.1.50/` in the example). You should see the admin
   page: status box on top, sections below. The dashboard works
   fully even with no LEDs attached.

If the console instead loops errors or the Wi-Fi join fails, jump to
**Troubleshooting** — the console text is the diagnostic, so note
exactly what it says.

## Step 7 — Wire the board to the map

Power everything off while wiring.

1. **Data:** connect the strip's **DIN** (data in) to board pin
   **GPIO 5** (labeled `5` on the board's header; it's the
   `led_pin` setting, so it can move if you must). Put the
   220–470 Ω resistor in series in this wire if you have one — it
   cleans up the signal. Keep this wire as short as practical.
2. **Ground:** connect a board **GND** pin to the LED supply's
   negative **and** the strip's GND. Board, strip and supply all
   need this common ground — don't skip it.
3. **Strip power:** the strip's **+5V** goes to the LED supply's
   positive.
4. **Board power:** connect the board's **5V** pin to the LED
   supply's positive — the same 5 V brick that powers the strip
   powers the board through its pins, and on many maps that's all
   it needs. It is map-dependent, though: the Wi-Fi radio pulls
   power in bursts, and a marginal feed (long thin wires, a busy
   strip, a tired supply) shows up as resets, Wi-Fi that won't
   join, or an admin page that comes and goes. If yours does any
   of that, connect a USB power source to the board as well and
   see if it settles — one of the author's maps runs exactly that
   way, both feeds connected. Treat "add USB power" as the first
   test whenever a board acts flaky.

For long strips, feed power at both ends if your map design allows —
voltage sags along a long strip and shows up as dim, reddish far-end
LEDs.

**Optional — a reset button on the frame.** Once the board is
mounted inside a map, its own RST button is hard to reach. The
board's reset line is on the header as the **EN** pin (next to the
3V3 pin; some boards label it RST). Wire any normally-open momentary
pushbutton between **EN** and **GND** — pressing it resets the board,
exactly like the onboard button. Keep the wires short or twisted
together; EN is a sensitive line.

Power up the 5 V supply. Within a minute the map should light
with current conditions.

## Step 8 — Find the admin page and finish setup

Browse to the board's IP address (from the Step 6 console line, or
find the board in your router's connected-devices list — it shows as
an Espressif device with the MAC address shown when you flashed it
in Step 3).

On the page:

1. **Status box** (always visible): whether the map is on, when it
   last fetched, how many stations are reporting, and the per-category
   counts. It re-reads itself every 30 seconds — no refreshing.
2. **If red and green are swapped**, open **Settings**, set **Color
   order** to RGB, Save. (Trip-up #2, fixed in ten seconds.)
3. **Give the map a name** in Settings if you like — with several
   maps it's how you tell their pages (and browser tabs) apart. The
   tab icon helps too: this board's icon is the sectional-chart scrap
   with a **red** dot, where the Pi version's is green.
4. Leave the map running. Every 5 minutes it fetches fresh weather;
   once an hour it checks this project's GitHub repository for code
   updates (see **Updating later**).

## Laying out your `airports` file

The `airports` file is the map's wiring diagram in text: **one entry
per LED, in the order the LEDs are wired**. Line 1 drives the first
LED, line 2 the second, and so on. Each line is one of:

- A **station ID** exactly as it appears in the weather data —
  usually 3–5 characters: `KFLG`, `KPHX`, `PAN` style Alaska IDs, and
  so on.
- **`NULL`** — a position with no airport (a spacer, or a burned-out
  spot you route around). The LED stays dark, but the position is
  still counted.
- **`LGND`** — a legend LED: it lights a fixed color so it can sit
  inside your printed legend. A bare `LGND` takes the next role in
  the standard sequence (VFR, MVFR, IFR, LIFR, No data, Wind,
  Storms), or pin a role with a tag: `LGND:VFR`, `LGND:MVFR`,
  `LGND:IFR`, `LGND:LIFR`, `LGND:NODATA`, `LGND:WIND`, `LGND:TS`.
  The WIND legend blinks to demonstrate the wind effect; the TS
  legend sits on IFR red and double-flashes yellow, like a real
  thunderstorm report.

Rules: at most **2,500 entries**; entries are case-insensitive (the
board uppercases them) but must be plain station-ID characters. The
`airports` file included with the project is one builder's real list
— a fine template, but replace it with your own layout. Two ways to
change it once the board is running, both on the admin page:

- **Upload a whole file** (in the **Map Builder** section): handy if
  you keep the list on your computer. An airports file from an
  original LiveSectional map uploads as-is — the format is identical.
  The old list is kept on the board as `airports.bak`, and the board
  reboots to load the new one.
- **Edit one LED at a time with the Map Builder** (next section):
  the way to build a layout standing at the physical map.

## Using the admin page

The page is organized in collapsible sections (click a section title
to open or close it; your browser remembers which you left open). The
status box on top is always visible.

### The Map Builder (entering your airports without counting)

Building a layout by counting LEDs along a string is miserable. The
Map Builder lights **one LED at a time** so the map itself tells you
where each position is:

1. Open the **Map Builder** section and click **Light this LED**.
   The whole string goes dark except one LED, lit bright white at
   full brightness (whatever your brightness/night settings say).
   The section shows the position as **LED n**, its line number in
   the airports file, and what's currently saved there, with a few
   neighbors for orientation.
2. Look at the physical map: that lit LED sits on one airport. Type
   that airport's ID into the **Airport at LED n** box and click
   **Save airport**. The change lands immediately — the board keeps
   your old file as `airports.bak`.
3. **Next** / **Previous** (or **Go to LED** to jump) move the light
   along the string; moving lights the new LED automatically. Walk
   the whole map this way, saving as you go.
4. To make a position a legend LED instead, use the legend buttons
   (**VFR**, **MVFR**, **IFR**, **LIFR**, **No data**, **Wind**,
   **Storms**, **Plain LGND**) — they save the matching `LGND:…`
   entry for you. Typing `NULL` in the airport box marks a position
   unused.
5. Click **Turn builder light OFF** when you're done; the weather
   display resumes. Your place is saved in `builder.json` on the
   board, so even a reboot mid-build doesn't lose it.

The whole-file **Upload an airports file** form also lives at the
bottom of this section (see **Laying out your airports file**).

### Power

Map on/off (the weather keeps fetching while off, so turning it back
on is instant) and a **Reboot board** button.

### Settings

Every `config.json` value from Step 4, editable in place. Display
settings take effect on the next cycle; Wi-Fi and LED-pin changes
take effect after a reboot.

### Stations

Every airport on your map with its current category color. Hover or
tap an airport ID to see its raw METAR text, and use the filter box
to find one quickly. The list refreshes with the status box.

## How it works (30-second version)

Every 5 minutes the board downloads the Aviation Weather Center's
bulk METAR cache (one gzipped XML file with every station's latest
report) over HTTPS, inflates and scans it *as a stream* — the file is
never held in memory whole — and keeps just your stations' reports.
Four times a second it turns those reports into a frame: category
colors per airport, blinking for high wind (≥15 kt), double yellow
flashes over the category color for thunderstorms, a brief blank and
a sweep when new data lands. If a fetch fails, the previous frame
stays up and it tries again next cycle. Everything runs as **one**
program on the board, which is why the admin page can pause for a
few seconds while a fetch is in progress — that's normal.

## Updating later

The board keeps its own code up to date from this repository:

- Once an hour (and once after each boot) it asks GitHub what the
  code files there hash to, and compares with its own copies.
- When something changed, the status box shows an amber **Update
  available** box with an **Update now** button.
- Clicking it downloads each changed file, **verifies it against the
  hash GitHub publishes for it before replacing anything**, keeps the
  old copy as `<file>.bak`, and reboots into the new code.
- Only the four code files (`boot.py`, `main.py`, `metar_micro.py`,
  `scanner.py`) ever update this way. Your `config.json` (with your
  Wi-Fi password) and your `airports` list are yours — updates never
  touch them.

MicroPython itself (the firmware from Step 3) doesn't self-update;
reflashing it is a rare, by-hand event. If an update ever misbehaves,
the board also keeps the pre-update files as `.bak` copies — copying
one back over its file with Thonny restores it.

## Troubleshooting

Work from the console text (Thonny) whenever you can — the board
says what it's doing. Lines starting `[boot]` are the Wi-Fi/clock
stage; `[metar-micro]` is the application.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Worked yesterday, dead after a power outage | Board booted before the router finished booting | Press **RST** (or your frame reset button) once the router is back |
| Boot loop printing `invalid header: 0xffffffff` | The firmware didn't land where the chip looks for it — classically an old (3.x) Thonny installer, which writes this chip at the wrong address while reporting success | Check Thonny's version (Help → About): upgrade to 4.1+ and reflash per Step 3, or use Step 3's manual esptool commands |
| `[boot] Wi-Fi join failed` | Wrong SSID/password, or a 5 GHz-only network name | Check `config.json` credentials; use a 2.4 GHz SSID |
| VFR shows red, IFR shows green | Strip is RGB color order | Settings → **Color order** → RGB, Save |
| Every LED silver (dim white) | No weather data — fetch failing, or the IDs in `airports` don't match real station IDs | Check the console for `fetch ok` vs `fetch failed`; fix IDs to match the METAR data |
| Red **Board Not Running** banner in the status box | The board is alive (the page loaded) but weather data has stopped: 3 failed fetches in a row, or 20 minutes with no new data | Read the banner's last-error line: a network error points at Wi-Fi/internet, an HTTP error at the weather server. Fetches keep retrying every 5 minutes and the banner clears on its own once data flows again |
| Board resets, Wi-Fi drops, or the admin page comes and goes | Marginal 5 V feed to the board through its 5V pin — enough on some maps, not on others | Connect USB power to the board as well (Step 7); if that cures it, the pin feed was the problem |
| Just one airport always silver | That station isn't reporting (common at small fields overnight) — the map is fine | Check the station in the **Stations** section; no raw METAR = nothing to show |
| Admin page occasionally slow to load | The 5-minute fetch monopolizes the board for a few seconds | Normal on a single-board version; wait and reload |
| Times on the page are off by an hour | Daylight-saving change | Adjust `tz_offset_min` by 60 (Step 4); Arizona stays put |
| Far-end LEDs dim or reddish | Voltage sag along the strip | Feed strip power at both ends; check supply size |
| First LED flickers or wrong colors | Noisy data line or missing common ground | Shorten the data wire, add the 220–470 Ω series resistor, verify the shared ground |
| `update check` lines show errors in the console | GitHub unreachable at that moment; a dropped connection is retried once automatically | Usually self-heals by the next hourly check or a reboot; the exact console line tells you what GitHub answered |

## Security notes

- The admin page has **no password** — anyone on your local network
  can open it and change settings (the same is true of the Pi
  version). Keep the board on a network you trust, and **never**
  port-forward it to the internet.
- Your Wi-Fi password is stored in `config.json` on the board in
  plain text (the board needs it to join). Physical access to the
  board is access to that password — mount it accordingly.
- Weather shown is for a wall map, not for navigation: as the admin
  page footer says, displayed conditions are indicative only. Get
  your real briefing from ForeFlight or 1-800-WX-BRIEF.

## Files on the board (reference)

| File | Yours to edit? | Notes |
| --- | --- | --- |
| `boot.py` | No | Wi-Fi join + clock sync at power-up; self-updates |
| `main.py` | No | Starts the app; self-updates |
| `metar_micro.py` | No | The application; self-updates |
| `scanner.py` | No | Weather-file parser; self-updates |
| `config.json` | **Yes** | Your settings; never touched by updates |
| `airports` | **Yes** | Your layout; never touched by updates |
| `airports.bak` | — | Previous layout, kept automatically |
| `*.py.bak` | — | Previous code files, kept by the updater |
| `builder.json` | — | Map Builder's remembered position |

Welcome to the map wall. When something behaves oddly, capture the
console lines around it — with those, every problem in this guide
was diagnosed in minutes.
