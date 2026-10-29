# metar_micro.py - LS Lite Micro for MicroPython (ESP32 / ESP32-S3).
#
# Behavior mirrors the Raspberry Pi LiveSectional Lite, restructured
# for a microcontroller: the AWC cache file is downloaded over HTTPS
# and its gzip stream is inflated and scanned incrementally
# (scanner.py), so the whole file is never held in RAM. METARs only,
# one LED per airport position from the `airports` file; tagged
# legends, silver no-data, category-first lightning, blank-then-wipe;
# admin page with live status, a Stations list (raw METAR popups), an
# airports-file upload, and a Map Builder like the Pi's.
#
# Files on the board: boot.py, main.py, metar_micro.py, scanner.py,
# airports, config.json. See README.md for flashing / wiring / setup.

import binascii
import gc
import hashlib
import json
import os
import socket
import ssl
import time

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

try:
    import deflate
except ImportError:
    deflate = None

if deflate is None:
    # CPython bench testing only - MicroPython firmware always has the
    # deflate module. Stand in a zlib-backed DeflateIO with the same
    # interface, so the whole fetch path can also run on a PC.
    try:
        import zlib as _zlib
    except ImportError:
        _zlib = None
    if _zlib is not None:
        class _DeflateIO:
            def __init__(self, stream, _fmt):
                self._d = _zlib.decompressobj(47)   # 47 = gzip/zlib auto
                self._s = stream
                self._buf = bytearray()
                self._eof = False

            def readinto(self, b):
                while not self._buf and not self._eof:
                    chunk = bytearray(4096)
                    n = self._s.readinto(chunk)
                    if not n:
                        self._buf += self._d.flush()
                        self._eof = True
                        break
                    self._buf += self._d.decompress(bytes(chunk[:n]))
                    if self._d.eof:
                        self._buf += self._d.flush()
                        self._eof = True
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                self._buf = self._buf[n:]
                return n

        class _DeflateShim:
            GZIP = 1
            DeflateIO = _DeflateIO

        deflate = _DeflateShim()

from scanner import MetarScanner

if not hasattr(time, "ticks_ms"):
    # CPython bench testing only; MicroPython provides these natively.
    time.ticks_ms = lambda: int(time.time() * 1000)
    time.ticks_add = lambda t, d: t + d
    time.ticks_diff = lambda a, b: a - b

# ---------------------------------------------------------------------------
# Constants (mirror metar_lite.py)
# ---------------------------------------------------------------------------
CACHE_HOST = "aviationweather.gov"
CACHE_PATH = "/data/cache/metars.cache.xml.gz"
FETCH_INTERVAL_MS = 300 * 1000        # 5 minutes between fetches
OFFLINE_FAILS = 3          # failed fetches in a row before the status
                           # box raises its "Board Not Running" banner
OFFLINE_AGE_MIN = 20       # ... or this many minutes with no new data
FETCH_TIMEOUT_S = 45
RENDER_INTERVAL_S = 0.25
WIND_THRESHOLD_KT = 15
NIGHT_BRIGHTNESS_FACTOR = 0.25
WIPE_SECONDS = 1.5
BLANK_SECONDS = 0.5            # dark beat before the wipe, so updates show
USER_AGENT = "metar-map-micro/1.0 (personal LED weather map)"

# Self-update from GitHub (the board has no git; this is the file-based
# equivalent of the Pi's pull-and-restart): the admin page checks the
# repo hourly, and Update now downloads the code files below, verifies
# each against the git blob SHA GitHub publishes for it, and reboots.
# config.json and airports are never touched - they belong to the
# board, not the repo.
GITHUB_REPO = "markyharris/livesectional-lite-micro"
UPDATE_FILES = ("boot.py", "main.py", "metar_micro.py", "scanner.py")
UPDATE_CHECK_MS = 3600 * 1000         # hourly, plus once after boot
UPDATE_TIMEOUT_S = 20
MAX_API_BYTES = 32768                 # repo listing JSON
MAX_CODE_BYTES = 131072               # one code file

COLORS = {"VFR": (0, 255, 0), "MVFR": (0, 0, 255),
          "IFR": (255, 0, 0), "LIFR": (255, 0, 255)}
OFF = (0, 0, 0)
NO_DATA = (32, 32, 32)         # dim white ("silver") - station not reporting
FLASH_COLOR = (255, 255, 0)    # bright yellow for lightning
BUILDER_COLOR = (255, 255, 255)  # bright white, stands out from wx colors

# Legend (LGND) LEDs, mirroring metar_lite: a legend position can name
# its role in the airports file - LGND:VFR, LGND:NODATA, LGND:WIND,
# LGND:TS - so it always matches the printed legend card next to it. A
# bare LGND (or an unrecognized tag) takes the next role in
# LEGEND_TAG_ORDER. The values are the steady/display colors; WIND and
# TS animate in compute_frame when their features are enabled.
LEGEND_TAGS = {
    "VFR": COLORS["VFR"],
    "MVFR": COLORS["MVFR"],
    "IFR": COLORS["IFR"],
    "LIFR": COLORS["LIFR"],
    "NODATA": NO_DATA,
    "WIND": COLORS["VFR"],      # blinks VFR green, like a windy VFR airport
    "TS": FLASH_COLOR,          # bright yellow when lightning is disabled
}
LEGEND_TAG_ORDER = ("VFR", "MVFR", "IFR", "LIFR", "NODATA", "WIND", "TS")
LEGEND = [LEGEND_TAGS[tag] for tag in LEGEND_TAG_ORDER]

DEFAULT_CONFIG = {
    "brightness": 60,             # percent, 0-100
    "night_mode": True,
    "night_start": "22:00",       # local time
    "night_end": "06:00",         # local time
    "night_brightness": 25,       # at night, scale to this % of normal (0 = off)
    "high_wind": True,
    "lightning": True,
    "use_wipe": True,             # blank + wipe transition on each update
    "map_on": True,               # master on/off, toggled from the web page
    "map_name": "",               # optional name shown on the settings page
    "wifi_ssid": "",
    "wifi_password": "",
    "led_pin": 5,                 # GPIO driving the strip's data line
    "color_order": "GRB",         # WS2812 standard; use "RGB" if R/G swap
    "tz_offset_min": -420,        # minutes from UTC (Arizona: -420, no DST)
    "watchdog": True,
}

STATE = {"metars": {}, "last_fetch": "never", "last_fetch_ts": 0,
         "reporting": 0, "total": 0,
         # Fetch health, for the status box's "Board Not Running"
         # banner: consecutive failed (or empty) fetches, the last
         # error seen, and when data last arrived (ticks clock, so
         # NTP jumps can't fake or hide staleness).
         "fetch_failures": 0, "fetch_attempts": 0, "last_error": "",
         "last_ok_ticks": 0}

UPDATE_STATE = {"available": False, "files": 0, "failed": "",
                "applying": False}

# Map builder (mirrors the Pi page): light one LED, walk the string,
# fix the entry there. State persists in builder.json, like Lite's.
BUILDER = {"position": 0, "active": False, "error": ""}
BUILDER_FILE = "builder.json"

# The live airports list - the same list object the main loop renders
# from, set by run(). Builder saves edit it in place, so a saved entry
# shows on the map right away, with no reboot.
_LIVE = {"entries": None}

# Set by the Reboot button: blank the strip immediately, then reset.
FORCE_OFF = False


def log(msg):
    print("[metar-micro]", msg)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open("config.json") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError) as e:
        log("config read failed (%s); using defaults" % e)
    try:
        cfg["brightness"] = max(0, min(100, int(cfg["brightness"])))
    except (TypeError, ValueError):
        cfg["brightness"] = DEFAULT_CONFIG["brightness"]
    return cfg


def save_config(cfg):
    with open("config.json", "w") as f:
        json.dump(cfg, f)


def load_airports():
    # Uppercased so a hand-edited file with lowercase IDs still matches
    # the (always uppercase) station IDs in the METAR cache.
    with open("airports") as f:
        return [ln.strip().upper() for ln in f if ln.strip()]


# ---------------------------------------------------------------------------
# Time helpers (RTC holds UTC after NTP sync; local = UTC + tz offset)
# ---------------------------------------------------------------------------
def _local_hhmm(cfg):
    t = time.gmtime(time.time() + int(cfg["tz_offset_min"]) * 60)
    return t[3] * 60 + t[4]


def _parse_hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def in_night_window(cfg):
    try:
        start = _parse_hhmm(cfg["night_start"])
        end = _parse_hhmm(cfg["night_end"])
    except (KeyError, ValueError):
        return False
    cur = _local_hhmm(cfg)
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def effective_brightness(cfg):
    """Brightness after the night reduction. At night the level scales
    to cfg['night_brightness'] % of normal (default 25); 0 = fully off."""
    pct = cfg["brightness"]
    if cfg.get("night_mode") and in_night_window(cfg):
        try:
            night_pct = max(0, min(100, int(cfg.get("night_brightness", 25))))
        except (TypeError, ValueError):
            night_pct = int(NIGHT_BRIGHTNESS_FACTOR * 100)
        pct = pct * night_pct / 100.0
    return pct


# ---------------------------------------------------------------------------
# HTTPS fetch with streaming gzip inflate + scan
# ---------------------------------------------------------------------------
class _ChunkedStream:
    """readinto() wrapper that decodes HTTP chunked transfer encoding."""

    def __init__(self, sock):
        self._s = sock
        self._buf = bytearray()
        self._left = -1
        self._eof = False

    def _readline(self):
        line = bytearray()
        while not line.endswith(b"\r\n"):
            ch = self._s.read(1)
            if not ch:
                break
            line += ch
        return bytes(line)

    def readinto(self, b):
        if self._eof:
            return 0
        while not self._buf:
            if self._left == -1:
                line = self._readline()
                try:
                    self._left = int(line.strip().split(b";")[0], 16)
                except (ValueError, IndexError):
                    self._eof = True
                    return 0
                if self._left == 0:
                    while True:
                        t = self._readline()
                        if t in (b"\r\n", b"\n", b""):
                            break
                    self._eof = True
                    return 0
            data = self._s.read(min(self._left, 4096))
            if not data:
                self._eof = True
                return 0
            self._left -= len(data)
            self._buf += data
            if self._left == 0:
                self._readline()       # trailing CRLF after the chunk
                self._left = -1
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        # Slice-rebind, not del buf[:n]: MicroPython bytearray has no
        # item deletion (found on first hardware run, Oct 28, 2026).
        self._buf = self._buf[n:]
        return n


def _fetch_once(host, path, wanted, wdt):
    """One HTTPS GET of the cache file; returns the scanner result dict,
    or None on any failure (caller keeps the previous data)."""
    addr = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0][-1]
    sock = socket.socket()
    sock.settimeout(FETCH_TIMEOUT_S)
    sock.connect(addr)
    if hasattr(ssl, "wrap_socket"):
        sock = ssl.wrap_socket(sock, server_hostname=host)
    else:
        # CPython 3.12+ removed ssl.wrap_socket (bench testing only;
        # MicroPython takes the branch above).
        sock = ssl.create_default_context().wrap_socket(
            sock, server_hostname=host)
    try:
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
               "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
               % (path, host, USER_AGENT))
        sock.write(req.encode())
        # Read response headers one byte at a time so no body bytes are
        # consumed past the header boundary.
        head = bytearray()
        while not head.endswith(b"\r\n\r\n"):
            ch = sock.read(1)
            if not ch:
                raise OSError("connection closed during headers")
            head += ch
            if len(head) > 8192:
                raise OSError("headers too large")
        lines = bytes(head).decode().split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("location", "")
            log("redirect -> %s" % loc)
            return ("redirect", loc)
        if status != 200:
            log("HTTP status %d" % status)
            STATE["last_error"] = "HTTP %d from the weather server" % status
            return None
        if "chunked" in headers.get("transfer-encoding", ""):
            body = _ChunkedStream(sock)
        else:
            body = sock
        if deflate is None:
            log("firmware lacks the deflate module; cannot inflate gzip")
            return None
        stream = deflate.DeflateIO(body, deflate.GZIP)
        scanner = MetarScanner(wanted)
        buf = bytearray(2048)
        while True:
            n = stream.readinto(buf)
            if not n:
                break
            scanner.feed(buf[:n])
            if wdt:
                wdt.feed()
            gc.collect()
        return scanner.finish()
    finally:
        try:
            sock.close()
        except OSError:
            pass


def fetch_metars(wanted, wdt=None):
    """Fetch + scan the cache file, following one redirect if offered.
    Returns {station: metar} or None on failure."""
    host, path = CACHE_HOST, CACHE_PATH
    for _ in range(2):
        try:
            res = _fetch_once(host, path, wanted, wdt)
        except Exception as e:
            log("fetch failed: %s" % e)
            STATE["last_error"] = ("%s: %s" % (type(e).__name__, e))[:80]
            return None
        if isinstance(res, tuple) and res[0] == "redirect":
            loc = res[1]
            if loc.startswith("https://"):
                rest = loc[len("https://"):]
                host, _, p = rest.partition("/")
                path = "/" + p
                continue
            return None
        return res
    return None


# ---------------------------------------------------------------------------
# Self-update from GitHub (see the constants near the top)
# ---------------------------------------------------------------------------
def _blob_sha_bytes(data):
    """git blob SHA-1 of a byte string: sha1 of 'blob <len>' + NUL +
    content - the same hash GitHub lists for every file in a repo."""
    h = hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode() + b"\x00")
    h.update(data)
    return binascii.hexlify(h.digest()).decode()


def _file_blob_sha(path):
    """Blob SHA of a file on the board, or None if it doesn't exist."""
    try:
        size = os.stat(path)[6]
    except OSError:
        return None
    h = hashlib.sha1()
    h.update(b"blob " + str(size).encode() + b"\x00")
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(2048)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return binascii.hexlify(h.digest()).decode()


def _https_get_url(url, max_bytes, depth=0):
    """Small HTTPS GET for the update machinery: returns the body
    bytes, or None on any failure. Follows up to 2 redirects. (The
    METAR fetch has its own streaming path; this one serves the repo
    listing and individual code files, which are small.)"""
    if depth > 2 or not url.startswith("https://"):
        return None
    rest = url[len("https://"):]
    host, _, p = rest.partition("/")
    path = "/" + p
    try:
        addr = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0][-1]
        sock = socket.socket()
        sock.settimeout(UPDATE_TIMEOUT_S)
        sock.connect(addr)
        if hasattr(ssl, "wrap_socket"):
            sock = ssl.wrap_socket(sock, server_hostname=host)
        else:
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)
        try:
            # No Accept header: the GitHub API answers 415 to
            # 'application/octet-stream' on the contents listing.
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
                   "Accept-Encoding: identity\r\n"
                   "Connection: close\r\n\r\n"
                   % (path, host, USER_AGENT))
            sock.write(req.encode())
            head = bytearray()
            while not head.endswith(b"\r\n\r\n"):
                ch = sock.read(1)
                if not ch:
                    return None
                head += ch
                if len(head) > 8192:
                    return None
            lines = bytes(head).decode().split("\r\n")
            status = int(lines[0].split(" ")[1])
            headers = {}
            for ln in lines[1:]:
                if ":" in ln:
                    k, v = ln.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if status in (301, 302, 303, 307, 308):
                return _https_get_url(headers.get("location", ""),
                                      max_bytes, depth + 1)
            if status != 200:
                log("update GET %s -> HTTP %d" % (path, status))
                return None
            if "chunked" in headers.get("transfer-encoding", ""):
                body = _ChunkedStream(sock)
            else:
                body = sock
            out = bytearray()
            buf = bytearray(2048)
            while len(out) <= max_bytes:
                n = body.readinto(buf)
                if not n:
                    break
                out += buf[:n]
            if len(out) > max_bytes:
                log("update GET %s: body too large" % path)
                return None
            return bytes(out)
        finally:
            try:
                sock.close()
            except OSError:
                pass
    except Exception as e:
        log("update GET failed: %s" % e)
        return None


def _parse_listing(data):
    """GitHub contents-listing JSON -> {name: {sha, download_url}}
    for files only, or None if it doesn't parse."""
    if data is None:
        return None
    try:
        if isinstance(data, bytes):
            data = data.decode()
        items = json.loads(data)
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    out = {}
    for it in items:
        if isinstance(it, dict) and it.get("type") == "file":
            out[it.get("name", "")] = {
                "sha": it.get("sha", ""),
                "download_url": it.get("download_url") or ""}
    return out


def _fetch_repo_listing():
    # One retry: GitHub's edge occasionally drops the TLS connection
    # mid-handshake (MBEDTLS_ERR_SSL_CONN_EOF seen on hardware).
    for attempt in (1, 2):
        listing = _parse_listing(_https_get_url(
            "https://api.github.com/repos/%s/contents/" % GITHUB_REPO,
            MAX_API_BYTES))
        if listing is not None:
            return listing
        if attempt == 1:
            time.sleep(2)
    return None


def _changed_files(listing):
    """(name, info) pairs whose repo SHA differs from the board's."""
    plan = []
    for name in UPDATE_FILES:
        info = listing.get(name)
        if info and _file_blob_sha(name) != info["sha"]:
            plan.append((name, info))
    return plan


def _check_update():
    """Refresh UPDATE_STATE from GitHub. Quiet on network failure -
    a board that's offline shows it on the map already; only available
    updates and apply failures are surfaced on the page."""
    listing = _fetch_repo_listing()
    if listing is None:
        return
    plan = _changed_files(listing)
    UPDATE_STATE["available"] = len(plan) > 0
    UPDATE_STATE["files"] = len(plan)
    if not plan:
        UPDATE_STATE["failed"] = ""
    log("update check: %s" % (
        ("%d file(s) changed on GitHub" % len(plan)) if plan
        else "code is current with GitHub"))


# ---------------------------------------------------------------------------
# Frame rendering (mirrors metar_lite.compute_frame)
# ---------------------------------------------------------------------------
def compute_frame(layout, metars, cfg):
    if FORCE_OFF:
        # An update or reboot is in flight: every LED dark, no
        # exceptions (the Pi's power-blank equivalent).
        return [OFF] * len(layout), ["."] * len(layout)
    if _builder_active(layout):
        # Map builder: darken the whole string and light just the one
        # position being edited, so the builder can see which airport
        # on the physical map it is. Overrides the weather display and
        # the map on/off switch; fetching continues underneath.
        colors = [OFF] * len(layout)
        symbols = ["."] * len(layout)
        colors[BUILDER["position"]] = BUILDER_COLOR
        symbols[BUILDER["position"]] = "B"
        return colors, symbols
    if not cfg.get("map_on", True):
        # Map switched off from the web page: every LED dark, legend
        # included. Data keeps fetching, so turning it back on is instant.
        return [OFF] * len(layout), ["."] * len(layout)
    ticks = time.ticks_ms()
    blink_on = (ticks // 1000) % 2 == 0               # ~1 s wind blink cycle
    # Lightning sequence, in 0.25 s steps: category, category, yellow
    # flash, category, yellow flash, category - a 1.5 s cycle in which
    # the flight-category color is the baseline, not darkness or white.
    flash_on = (ticks // 250) % 6 in (2, 4)
    colors, symbols = [], []
    legend_i = 0
    for pos in layout:
        if pos == "NULL":
            colors.append(OFF); symbols.append("."); continue
        if pos == "LGND" or pos.startswith("LGND:"):
            tag = pos[5:] if pos.startswith("LGND:") else ""
            if tag not in LEGEND_TAGS:
                tag = LEGEND_TAG_ORDER[legend_i % len(LEGEND_TAG_ORDER)]
            color = LEGEND_TAGS[tag]
            symbol = "G"
            if tag == "WIND" and cfg.get("high_wind") and not blink_on:
                # Demonstrate the same ~1 s blink used for a windy airport.
                color, symbol = OFF, "."
            elif tag == "TS" and cfg.get("lightning"):
                # Demonstrate a thunderstorm over an IFR flight category:
                # IFR red is the baseline, with bright-yellow flashes.
                if flash_on:
                    color, symbol = FLASH_COLOR, "*"
                else:
                    color, symbol = COLORS["IFR"], "G"
            colors.append(color); symbols.append(symbol)
            legend_i += 1; continue
        m = metars.get(pos)
        if m is None:
            colors.append(NO_DATA); symbols.append("N"); continue
        if cfg.get("lightning") and ("TS" in m["wx"] or "LTNG" in m["wx"]):
            if flash_on:
                colors.append(FLASH_COLOR); symbols.append("*")
            else:
                colors.append(COLORS[m["category"]])
                symbols.append(m["category"][0])
            continue
        windy = max(m["wind_kt"] or 0, m["gust_kt"] or 0) >= WIND_THRESHOLD_KT
        if cfg.get("high_wind") and windy and not blink_on:
            colors.append(OFF); symbols.append("."); continue
        colors.append(COLORS[m["category"]])
        symbols.append(m["category"][0])          # V, M, I, or L
    return colors, symbols


def _scale(color, pct):
    f = pct / 100.0
    return (int(color[0] * f), int(color[1] * f), int(color[2] * f))


# ---------------------------------------------------------------------------
# Strip drivers
# ---------------------------------------------------------------------------
class NeoStrip:
    """Real WS2812 strip via the built-in neopixel driver (ESP32 RMT)."""

    def __init__(self, count, pin_no, order):
        from machine import Pin
        import neopixel
        self.np = neopixel.NeoPixel(Pin(pin_no), count)
        self.count = count
        self.order = order

    def _set(self, i, color, pct):
        r, g, b = _scale(color, pct)
        self.np[i] = (g, r, b) if self.order == "GRB" else (r, g, b)

    def show(self, colors, pct, symbols=None):
        for i, c in enumerate(colors):
            self._set(i, c, pct)
        self.np.write()

    def wipe(self, colors, pct, symbols=None):
        # The update transition: blank the whole string for a beat,
        # then a front sweeps it, each LED switching to its new color
        # as the front passes - so an update is visible even when most
        # colors didn't change (mirrors metar_lite's PiStrip.wipe).
        for i in range(self.count):
            self._set(i, OFF, 100)
        self.np.write()
        time.sleep(BLANK_SECONDS)
        delay = WIPE_SECONDS / self.count
        for i, c in enumerate(colors):
            self._set(i, c, pct)
            self.np.write()
            time.sleep(delay)


class SimStrip:
    """No LEDs attached: print the frame as category letters (REPL use)."""

    def __init__(self, count):
        self.count = count

    def show(self, colors, pct, symbols=None):
        line = "".join(symbols) if symbols else "?" * self.count
        print("bright=%5.1f%% %s" % (pct, line))

    def wipe(self, colors, pct, symbols=None):
        print("--- blank (%.1fs) -> wipe (%.1fs) -> new data ---"
              % (BLANK_SECONDS, WIPE_SECONDS))
        self.show(colors, pct, symbols)


def make_strip(count, cfg):
    try:
        import neopixel  # noqa: F401
        import machine   # noqa: F401
        log("neopixel found - driving the real strip on GPIO %d"
            % int(cfg["led_pin"]))
        return NeoStrip(count, int(cfg["led_pin"]), cfg.get("color_order", "GRB"))
    except ImportError:
        log("neopixel not available - simulation mode")
        return SimStrip(count)


# ---------------------------------------------------------------------------
# Settings web page (tiny asyncio HTTP server; same four settings + Wi-Fi)
# ---------------------------------------------------------------------------
def _unquote(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "+":
            out.append(" ")
            i += 1
        elif c == "%" and i + 2 < len(s) + 1 and i + 3 <= len(s):
            try:
                out.append(chr(int(s[i + 1:i + 3], 16)))
                i += 3
            except ValueError:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_form(body):
    form = {}
    for pair in body.split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            form[_unquote(k)] = _unquote(v)
    return form


def _esc(s):
    """Minimal HTML escaping for values interpolated into the page."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_CAT_HEX = {"VFR": "#00b050", "MVFR": "#0066ff", "IFR": "#ff0000",
            "LIFR": "#e600e6"}

# Tab icon: the Pi admin page's sectional-chart icon, with the
# airport LED recolored from VFR green to IFR red and enlarged
# ~1.6x, so the two controllers' browser tabs are easy to tell
# apart at a glance.
# 64x64 palette PNG, 1832 bytes.
FAVICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAMAAACdt4HsAAADAFBMVEXr6dH18Nzb4szIw6v49OHl"
    "5c6xu6iuAwPj2caxBge7x8fN1MvY2MiqFxezFBbCvqfQc63g0L5oeZrayr21OTPLcqy2JyjRnay7"
    "Zljy7dvInKt0dJutKCG7cmjCZVpVeJiuMim3RznWyMRFd5dMg51nbZt6qKiMe3mqIx22SUPHiXzH"
    "ioLYpacbaJZShaJtaIlulKRyJzl5c4l6iaODqqieDRKptae0DRG8LjOyNCu99MbGb6zEvsTJysUa"
    "WoIiTYc6aJc5c5pXdKCFVm+OiomIhpqbbHOSd3uap6moGyGpOjWvx62wIR66OUG4VEm9Wlm+eXa+"
    "lo2ww6vBKi7ClIjBnpfEw7bV074QXpMfYH4jUn02daJFR2NHVGVOb5dCcX9Ni6RTjJ1oITxjeKVs"
    "iI9gg6B1f6GCIi+EJDiGRU2FeIaLjKqEn6edDg6SEB6cFCCVMDaffYybgW2Xh46Zko+TlZykDBGt"
    "EAyvQzWuWEapXlmvcWu8HiG/bGO9eau3hWq4gne9lX2+oZC/0MfBOUHCRTfAW0nGW1fNX2HLYGDH"
    "anPMcXrNurHB78PM98rUvLbgw7zg8tEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAACU43hiAAAD40lEQVR42q2YB3vbNhCGSZAQQZCgKC7J2pL3dmzH29l776RN2qR7"
    "7z3TJh1Jm3T84wJUNEiCFFnne/TIlOx7eXe4O4AWclIuqnw+L6VTTsgLfIm+5GGShJwAqHp2A5cB"
    "UlfBz0IXgECcH8kAsQdAAOwJEEcQUwNAAfAIGQDMBV7+UgO4hEwAPwiwFwAnDRkBfhBgD4BoGrIC"
    "ImnwDYI1nQwIpyEI4HkUBtAgdF0PlwPwX1yFAaCAdITgoDGAEPpvXEgEAAp60UYjI90/oMZ4Ymls"
    "bGniCoVE25sBQFAF0zSLds/+7MIR693JyWPHjyychT4iEUB/jxCac91tm3Uy1N9vHL8+W2q1SrPX"
    "v2istCOEAKCbMCq77LpF6v7K+OXatONVq54zXXvwpvU30mGA0M/BQMY7iKLr2qvj1+rN6hRhmmpe"
    "nFlcvKGB4MzsJzGydnb5jZcvl5rGOcNQVdUwjOqv3x68p90MlMXgKoQJ8OZh66IzRc29DUpQFeX0"
    "vgPza1r8MoYBuUOfnzl9zvjK+f3xNFEU5Uvy1ksHXtFhoDYDddD/nhUjnLCuOYSozv2ZuZ+/oQBl"
    "/4dv3xnVUgH8Elg6Vt8khlef+evfrRILQSGt70YfxgLCEehjkxc86sCjOWHraYnZq+TrS+M3YgDh"
    "3YAuw9jkb5tEVX/5c3drdoOaK6rhXLJ2eIDwIJN9Fw6//ukZ8oFinC+VnPUOYLNWaQd6jQLynFne"
    "ATTm7+4j+5nrzH9GMFonLH0ogK2uDzh164UOQHkGIF59eRXpQloAnn/x1Y+IX0K+D+uGM2Nd0YcC"
    "eiHoCwffuTq1/iwAhRjN0vJ7KFJICQB86Gi9Stj9VZWQ750fPrHyoYEXA2CjkwHWFo9e2PCIwTrp"
    "4+naZ6/lgvMuASAKIwLGIF+pzNZbTc+7ev6k1Rid2MVYC87tWIAsYB0CZJ+ylk/U/vipdrJxe1TS"
    "dzUmzMQuKEsSJJG3Jcsa9qNoI7xqVSqVcWtl7ccyDMxrH5YTJJl3KsCaPMIaCul0o2jv7DxpQ1B2"
    "YfQEIvEAMpAEmY52yEQBiEmHtiulAogidV8Wxe7mQCE+AcKym84DzGpR7O+LsCt7204BYLePAcDt"
    "8nAApvZybysfJNCfZTdUR2GACDBgSeyWY1gF8x+UCNBwZxU6B0SOzGIiAGv98y3/ODBnRntBCt4+"
    "WZJpIxjjAcYx55pgEgr8EFjtigPn41iZRT5g0P1kgMkFYE1ICSjyAH7nDvZSRkA4+4keSBwA5ykn"
    "G0DOBigMb+ckFYpDPBCFbHruAPH/AMS9AsKP05GHFDHp4ZyeUKRMCv9fIPcfywRfdVSmhDAAAAAA"
    "SUVORK5CYII=")

_favicon_cache = None


def _favicon_png():
    global _favicon_cache
    if _favicon_cache is None:
        import binascii
        _favicon_cache = binascii.a2b_base64(FAVICON_B64)
    return _favicon_cache


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _ordinal(d):
    if 11 <= d % 100 <= 13:
        return "%dth" % d
    return "%d%s" % (d, {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th"))


def _pretty_fetch(cfg):
    """Last-fetch time the way the Pi page shows it - 'Wednesday Oct
    28th at 2:24 pm' - in local time (UTC + the configured offset)."""
    ts = STATE.get("last_fetch_ts", 0)
    if not ts:
        return "never"
    t = time.gmtime(ts + int(cfg.get("tz_offset_min", 0)) * 60)
    hour = t[3] % 12 or 12
    return "%s %s %s at %d:%02d %s" % (
        _WEEKDAYS[t[6]], _MONTHS[t[1] - 1], _ordinal(t[2]),
        hour, t[4], "am" if t[3] < 12 else "pm")


def _board_ip():
    try:
        import network
        return network.WLAN(network.STA_IF).ifconfig()[0]
    except Exception:
        return ""


def _health():
    """Is the data pipeline alive? Returns (offline, detail).

    The board itself is up whenever this page answers at all - the web
    server and the fetch loop are the same program - so "offline" here
    means the map has stopped getting weather: OFFLINE_FAILS failed
    fetches in a row, or OFFLINE_AGE_MIN minutes since the last data
    arrived. The detail line says which, and the last error seen."""
    fails = STATE.get("fetch_failures", 0)
    age_min = None
    if STATE.get("fetch_attempts", 0):
        age_min = time.ticks_diff(time.ticks_ms(),
                                  STATE.get("last_ok_ticks", 0)) // 60000
    offline = (fails >= OFFLINE_FAILS
               or (age_min is not None and age_min >= OFFLINE_AGE_MIN))
    if not offline:
        return False, ""
    bits = []
    if age_min:
        bits.append("No new weather data for %d minutes." % age_min)
    else:
        bits.append("No weather data received yet.")
    if fails:
        bits.append("Last %d fetch attempt(s) failed." % fails)
    if STATE.get("last_error"):
        bits.append("Last error: %s" % STATE["last_error"])
    return True, " ".join(bits)


def _status_dict(cfg):
    """Status-box data, shared by the page and /api/status (the page
    polls the JSON every 30 s, mirroring the Pi admin page)."""
    counts = {"VFR": 0, "MVFR": 0, "IFR": 0, "LIFR": 0}
    stations = {}
    for sid, m in STATE["metars"].items():
        if m["category"] in counts:
            counts[m["category"]] += 1
        stations[sid] = {"category": m["category"],
                         "raw": m.get("raw", "")}
    offline, offline_detail = _health()
    return {"last_fetch_pretty": _pretty_fetch(cfg),
            "stations_reporting": STATE["reporting"],
            "stations_total": STATE["total"],
            "counts": counts, "stations": stations,
            "map_on": bool(cfg.get("map_on", True)),
            "offline": offline, "offline_detail": offline_detail,
            "update_available": UPDATE_STATE["available"],
            "update_files": UPDATE_STATE["files"],
            "update_failed": UPDATE_STATE["failed"]}


def _stations_html():
    """The Stations section: every airport on the map (airports-file
    order, duplicates once) with a category-color dot and its raw
    METAR in a hover/tap popup. Mirrors the Pi admin page."""
    try:
        layout = load_airports()
    except OSError:
        layout = []
    seen = []
    for p in layout:
        if p == "NULL" or p.startswith("LGND") or p in seen:
            continue
        seen.append(p)
    if not seen:
        return '<p style="color:#555">No airports file found yet.</p>'
    metars = STATE["metars"]
    rows = []
    for sid in seen:
        m = metars.get(sid)
        if m and m["category"] in _CAT_HEX:
            hexcol = _CAT_HEX[m["category"]]
            title = m["category"]
            tip = m.get("raw") or "No METAR text in the latest report."
        else:
            hexcol = "#c0c0c0"
            title = "No data"
            tip = "No METAR received in the latest fetch."
        rows.append(
            '<span class="sta" data-sid="%s"><span class="dot" '
            'title="%s" style="background:%s"></span>'
            '<span class="sta-id">%s<span class="tip">%s</span>'
            '</span></span>'
            % (sid, title, hexcol, sid, _esc(tip)))
    return ('<p style="margin:0.6em 0 0"><input type="search" '
            'id="sta-filter" placeholder="Filter by airport ID" '
            'autocomplete="off"> <span id="sta-count" '
            'style="color:#555">%d airports</span></p>'
            '<div class="sta-grid">%s</div>'
            '<p id="sta-nomatch" style="display:none;color:#555">'
            'No airports match that filter.</p>'
            % (len(seen), "".join(rows)))


# ---------------------------------------------------------------------------
# Airports-file upload (mirrors the Pi admin page's validation)
# ---------------------------------------------------------------------------
MAX_AIRPORTS = 2500
MAX_UPLOAD_BYTES = 200_000


def _valid_airport_id(s):
    """Station IDs as they appear in the METAR cache (3-5 chars), plus
    the layout keywords NULL, LGND and tagged legends (LGND:WIND...)."""
    if s in ("NULL", "LGND"):
        return True
    if s.startswith("LGND:"):
        return s[5:] in LEGEND_TAGS
    if not 3 <= len(s) <= 5:
        return False
    for ch in s:
        if not ("A" <= ch <= "Z" or "0" <= ch <= "9"):
            return False
    return True


def _parse_airports_upload(raw):
    """Validate uploaded airports-file bytes. Returns (entries, error):
    entries normalized (trimmed, uppercased, blanks dropped, any line
    endings), or None and a plain-language error."""
    if raw is None:
        return None, "Choose an airports file to upload first."
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, "That file is too large to be an airports file."
    try:
        text = raw.decode("utf-8")
    except Exception:
        return None, ("That file doesn't look like plain text. Upload "
                      "the airports file itself - one station per line.")
    numbered = []
    for n, line in enumerate(text.split("\n"), 1):
        e = line.strip().upper()
        if e:
            numbered.append((n, e))
    entries = [e for _, e in numbered]
    if not entries:
        return None, "No airport entries found in that file."
    if len(entries) > MAX_AIRPORTS:
        return None, ("That file lists %d positions - the limit here "
                      "is %d." % (len(entries), MAX_AIRPORTS))
    bad = [(n, e) for n, e in numbered if not _valid_airport_id(e)][:3]
    if bad:
        shown = ", ".join("line %d ('%s')" % (n, e) for n, e in bad)
        return None, ("Some entries don't look like station IDs: %s. "
                      "Use the ID as it appears in the METAR data, or "
                      "NULL / LGND." % shown)
    return entries, None


def _save_airports(entries):
    """Replace the airports file, keeping the old one as airports.bak
    so a wrong upload is undoable by copying it back."""
    try:
        with open("airports") as f:
            old = f.read()
    except OSError:
        old = ""
    if old:
        try:
            with open("airports.bak", "w") as f:
                f.write(old)
        except OSError:
            pass
    with open("airports", "w") as f:
        f.write("\n".join(entries) + "\n")


def _clamp_builder(count):
    if count:
        BUILDER["position"] = max(0, min(count - 1,
                                         BUILDER["position"]))
    else:
        BUILDER["position"] = 0


def _load_builder(count):
    """Restore the persisted builder state, clamped to the layout."""
    try:
        with open(BUILDER_FILE) as f:
            saved = json.load(f)
        BUILDER["position"] = int(saved.get("position", 0))
        BUILDER["active"] = bool(saved.get("active", False))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    _clamp_builder(count)


def _save_builder():
    try:
        with open(BUILDER_FILE, "w") as f:
            json.dump({"position": BUILDER["position"],
                       "active": BUILDER["active"]}, f)
    except OSError:
        pass


def _builder_active(layout):
    """True when the builder light should be showing for this layout."""
    return bool(BUILDER["active"]
                and 0 <= BUILDER["position"] < len(layout))


def _live_entries():
    """The live airports entries (the list the main loop renders
    from), or a fresh read of the file if the loop hasn't started."""
    if _LIVE["entries"] is not None:
        return _LIVE["entries"]
    try:
        return load_airports()
    except OSError:
        return []


def _multipart_file(body, boundary, field):
    """Pull one file part's content out of a multipart/form-data body.
    Just enough parsing for a browser file-upload form."""
    needle = ('name="%s"' % field).encode()
    for part in body.split(b"--" + boundary):
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep or needle not in head:
            continue
        if content.endswith(b"\r\n"):
            content = content[:-2]
        return content
    return None


def _notice(title, detail_html):
    return ("<html><body style=\"font-family:sans-serif\">"
            "<h2>%s</h2><p>%s</p></body></html>"
            % (title, detail_html)).encode()


_PAGE_HEAD = """<!DOCTYPE html><html><head><title>%%TITLE%%</title>
<link rel="icon" type="image/png" href="data:image/png;base64,%%FAVICON_B64%%">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body { font-family: sans-serif; max-width: 34em; margin: 1em auto; padding: 0 1em; }
.status { background: #f0f0f0; padding: 0.8em 1em; border-radius: 6px; }
.status p { margin: 0.35em 0; }
details.section { margin: 0.2em 0; }
summary { cursor: pointer; }
.row { display: flex; gap: 1em; flex-wrap: wrap; }
.sta-grid { display: flex; flex-wrap: wrap; gap: 0.4em 1.1em; margin-top: 0.6em; }
.sta .dot { display: inline-block; width: 0.72em; height: 0.72em;
            border-radius: 50%; margin-right: 0.35em; vertical-align: -0.05em; }
.sta-id { position: relative; border-bottom: 1px dotted #999; cursor: pointer; }
.sta-id .tip { display: none; position: absolute; left: 0; bottom: 1.5em;
               z-index: 10; background: #222; color: #eee;
               padding: 0.5em 0.7em; border-radius: 6px;
               font-family: monospace; font-size: 0.85em;
               white-space: normal; width: max-content;
               max-width: min(24em, 80vw);
               box-shadow: 0 2px 8px rgba(0,0,0,0.35); }
.sta-id.open .tip { display: block; }
#sta-filter { padding: 0.35em 0.5em; font-size: 1em; width: 12em; max-width: 100%; }
@media (hover: hover) { .sta-id:hover .tip { display: block; } }
@media (max-width: 600px) {
  .sta-id .tip { position: fixed; left: 1em; right: 1em; top: 4em;
                 bottom: auto; width: auto; max-width: none; }
}
</style></head><body>
"""

_PAGE_BODY = """
<div style="display:flex;align-items:center;gap:0.6em;margin-bottom:0.8em">
<img src="data:image/png;base64,%%FAVICON_B64%%" alt="" width="52" height="52" style="border-radius:8px">
<h2 style="margin:0">LS Lite Micro%%HEADING_NAME%%</h2>
</div>
<div class="status">
<div id="st-offline" style="%%OFFLINE_STYLE%%color:#fff;background:#b00;padding:0.5em 0.6em;border-radius:4px;margin-bottom:0.5em">
<p style="margin:0;font-weight:bold">&#9888; Board Not Running</p>
<p id="st-offline-detail" style="margin:0.2em 0 0">%%OFFLINE_DETAIL%%</p>
</div>
<p>Map is <strong id="st-map">%%MAP_STATE%%</strong></p>
<p>Last fetch: <strong id="st-fetch">%%LAST_FETCH%%</strong></p>
<p>Stations reporting: <strong id="st-reporting">%%REPORTING%%</strong>
   of <span id="st-total">%%TOTAL%%</span> mapped</p>
<p>On the map:
   <span title="VFR" style="color:#00b050;font-size:1.6em;vertical-align:-0.12em">&#9679;</span> <span id="st-vfr">%%VFR%%</span>
   <span title="MVFR" style="color:#0066ff;font-size:1.6em;vertical-align:-0.12em">&#9679;</span> <span id="st-mvfr">%%MVFR%%</span>
   <span title="IFR" style="color:#ff0000;font-size:1.6em;vertical-align:-0.12em">&#9679;</span> <span id="st-ifr">%%IFR%%</span>
   <span title="LIFR" style="color:#e600e6;font-size:1.6em;vertical-align:-0.12em">&#9679;</span> <span id="st-lifr">%%LIFR%%</span></p>
<p>Board: <strong>ESP32-S3</strong>%%BOARD_IP%%</p>
<div id="st-update" style="%%UPD_STYLE%%color:#8a6d00;background:#fff3cd;padding:0.4em 0.6em;border-radius:4px;margin-top:0.5em">
<div id="st-upavail" style="%%UPAVAIL_STYLE%%">
<p style="margin:0.2em 0">Update available: <strong id="st-upfiles">%%UPFILES%%</strong> file(s) changed on GitHub.</p>
<form method="post" action="/update-apply" onsubmit="return confirm('Update now? The board will download the new code from GitHub and reboot.')"><p style="margin:0.2em 0"><button type="submit">Update now</button></p></form>
</div>
<p id="st-upfailed" style="color:#b00;margin:0.3em 0;%%UPFAILED_STYLE%%">%%UPFAILED%%</p>
</div>
</div>
<hr>
<details class="section" id="sec-builder"%%BUILDER_OPEN%%>
<summary style="margin-top:1em"><h2 style="display:inline;margin:0">Map Builder</h2></summary>
<p style="color:#555;margin:0.2em 0 0">Light one LED on the string, look at
  where it sits on the map, and type the airport that belongs there - no more
  counting LEDs by hand. While the builder light is on, the weather display
  pauses on that one LED (data keeps updating underneath).</p>
%%BUILDER_ERROR%%
<div class="status" style="margin-top:0.8em">
<p>Builder light: <strong>%%BUILDER_STATE%%</strong></p>
<p>Position: <strong>LED %%BUILDER_POS%%</strong>
   (line %%BUILDER_LINE%% of %%BUILDER_COUNT%% in the airports file)</p>
<p>Current entry: <strong>%%BUILDER_CURRENT%%</strong></p>
%%BUILDER_NEIGHBORS%%
</div>
<form method="post" action="/builder">
  <input type="hidden" name="builder_toggle" value="1">
  <button type="submit" style="margin-top:0.5em">%%BUILDER_TOGGLE_LABEL%%</button>
</form>
<form method="post" action="/builder">
  <p style="margin:0.6em 0 0"><label for="builder_pos">Go to LED</label>
  <input type="number" id="builder_pos" name="builder_pos"
         min="0" max="%%BUILDER_MAX%%" value="%%BUILDER_POS%%" size="6">
  <button type="submit" name="builder_goto" value="1">Go</button></p>
  <p style="margin:0.4em 0 0"><button type="submit" name="builder_step" value="-1">&larr; Previous</button>
  <button type="submit" name="builder_step" value="1">Next &rarr;</button></p>
  <p style="margin:0.2em 0 0;color:#555">Stepping or jumping lights the new LED automatically.</p>
</form>
<form method="post" action="/builder">
  <p style="margin:0.6em 0 0"><label for="builder_id">Airport at LED %%BUILDER_POS%%</label>
  <input type="text" id="builder_id" name="builder_id" value="%%BUILDER_CURRENT%%"
         size="8" style="text-transform:uppercase" autocomplete="off">
  <button type="submit" name="builder_save" value="1">Save airport</button></p>
  <p style="margin:0.2em 0 0;color:#555">Station ID as it appears in the METAR
     data (e.g. KFLG), or NULL for an unused position, LGND for a legend LED
     (a tagged legend like LGND:WIND pins that LED's color - the buttons
     below set those for you). Saving takes effect on the map right
     away - no restart needed.</p>
</form>
<form method="post" action="/builder">
  <p style="margin:0.6em 0 0;color:#555">Or make this LED a legend position
     with a fixed color, so it always matches your printed legend:</p>
  <p style="margin:0.2em 0 0"><button type="submit" name="builder_legend" value="VFR">VFR</button>
  <button type="submit" name="builder_legend" value="MVFR">MVFR</button>
  <button type="submit" name="builder_legend" value="IFR">IFR</button>
  <button type="submit" name="builder_legend" value="LIFR">LIFR</button>
  <button type="submit" name="builder_legend" value="NODATA">No data</button>
  <button type="submit" name="builder_legend" value="WIND">Wind</button>
  <button type="submit" name="builder_legend" value="TS">Storms</button>
  <button type="submit" name="builder_legend" value="">Plain LGND</button></p>
</form>
<form method="post" action="/upload-airports" enctype="multipart/form-data"
      onsubmit="return confirm('Replace the whole airports list with the uploaded file? Your current list is kept as airports.bak.')">
  <p style="margin:0.6em 0 0"><label for="airports_file">Upload an airports file</label>
  <input type="file" id="airports_file" name="airports_file" accept=".txt,text/plain" required>
  <button type="submit" style="margin-top:0.5em">Upload and replace list</button></p>
  <p style="margin:0.2em 0 0;color:#555">Already have a list - for example
     the airports file from an original LiveSectional? Its format is the
     same (one station ID, NULL or LGND per line, in LED order), so it
     uploads as-is. The upload replaces the whole list and the board
     reboots to load it. Your previous list is kept as airports.bak.</p>
</form>
</details>
<hr>
<details class="section" id="sec-power">
<summary style="margin-top:1em"><h2 style="display:inline;margin:0">Power</h2></summary>
<div class="row">
<form method="post" action="/toggle"><p><button type="submit">%%TOGGLE_LABEL%%</button></p></form>
<form method="post" action="/reboot" onsubmit="return confirm('Reboot the board now?')"><p><button type="submit">Reboot board</button></p></form>
</div>
</details>
<hr>
<details class="section" id="sec-settings">
<summary style="margin-top:1em"><h2 style="display:inline;margin:0">Settings</h2></summary>
<form method="post" action="/save">
<p>Map name (shown above, optional): <input name="map_name" value="%%MAP_NAME%%"></p>
<p>Brightness (0-100%): <input name="brightness" type="number" min="0" max="100" value="%%BRIGHTNESS%%"></p>
<p><label><input name="night_mode" type="checkbox" %%NIGHT_CHK%%> Night mode</label>
dim from <input name="night_start" size="5" value="%%NIGHT_START%%">
to <input name="night_end" size="5" value="%%NIGHT_END%%"><br>
Night brightness: <input name="night_brightness" type="number" min="0" max="100" value="%%NIGHT_BRIGHTNESS%%">% of normal (0 = off overnight)</p>
<p><label><input name="high_wind" type="checkbox" %%WIND_CHK%%> High-wind blink (&ge;15 kt)</label></p>
<p><label><input name="lightning" type="checkbox" %%LTNG_CHK%%> Lightning flash</label></p>
<p><label><input name="use_wipe" type="checkbox" %%WIPE_CHK%%> Use wipe (each update blanks the string briefly, then sweeps in the new data)</label></p>
<p>UTC offset (minutes): <input name="tz_offset_min" type="number" value="%%TZ_OFFSET%%"></p>
<p>LED data GPIO: <input name="led_pin" type="number" value="%%LED_PIN%%">
Color order: <select name="color_order"><option%%GRB_SEL%%>GRB</option><option%%RGB_SEL%%>RGB</option></select></p>
<p>Wi-Fi SSID: <input name="wifi_ssid" value="%%WIFI_SSID%%"><br>
Wi-Fi password: <input name="wifi_password" type="password" placeholder="(unchanged if blank)"></p>
<p><button type="submit">Save</button></p></form>
<p style="color:#666">Display settings apply on the next cycle. Wi-Fi and
GPIO changes apply after a reboot.</p>
</details>
<hr>
<details class="section" id="sec-stations">
<summary style="margin-top:1em"><h2 style="display:inline;margin:0">Stations</h2></summary>
<p style="color:#555;margin:0.2em 0 0">Every airport on the map with its
current flight-category color. Hover or tap an airport ID to see its
raw METAR. The list refreshes along with the status box, every 30
seconds.</p>
%%STATIONS%%
</details>
<p style="color:#888;font-size:0.85em;margin-top:1.8em">Weather data:
NOAA Aviation Weather Center, refreshed every 5 minutes. Displayed
conditions are indicative only - not for navigation.</p>
<p style="text-align:center;color:#888;font-size:0.85em">LS Lite Micro &middot; LED METAR map</p>
"""

_PAGE_SCRIPTS = """<script>
(function () {
  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value;
  }
  var CAT_HEX = { VFR: "#00b050", MVFR: "#0066ff", IFR: "#ff0000",
                  LIFR: "#e600e6" };
  function updateStations(stations) {
    document.querySelectorAll(".sta").forEach(function (row) {
      var info = stations ? stations[row.getAttribute("data-sid")] : null;
      var dot = row.querySelector(".dot");
      var tip = row.querySelector(".tip");
      if (info && CAT_HEX[info.category]) {
        if (dot) { dot.style.background = CAT_HEX[info.category];
                   dot.title = info.category; }
        if (tip) tip.textContent = info.raw ||
            "No METAR text in the latest report.";
      } else {
        if (dot) { dot.style.background = "#c0c0c0"; dot.title = "No data"; }
        if (tip) tip.textContent = "No METAR received in the latest fetch.";
      }
    });
  }
  function updateStatus() {
    fetch("/api/status").then(function (r) { return r.json(); })
      .then(function (s) {
        setText("st-map", s.map_on ? "ON" : "OFF");
        setText("st-fetch", s.last_fetch_pretty);
        setText("st-reporting", s.stations_reporting);
        setText("st-total", s.stations_total);
        setText("st-vfr", s.counts.VFR);
        setText("st-mvfr", s.counts.MVFR);
        setText("st-ifr", s.counts.IFR);
        setText("st-lifr", s.counts.LIFR);
        updateStations(s.stations);
        var off = document.getElementById("st-offline");
        if (off) {
          off.style.display = s.offline ? "" : "none";
          if (s.offline) setText("st-offline-detail", s.offline_detail);
        }
        var upd = document.getElementById("st-update");
        if (upd) {
          var av = document.getElementById("st-upavail");
          var fl = document.getElementById("st-upfailed");
          if (av) av.style.display = s.update_available ? "" : "none";
          if (s.update_available) setText("st-upfiles", s.update_files);
          if (fl) {
            fl.style.display = s.update_failed ? "" : "none";
            if (s.update_failed) fl.textContent = s.update_failed;
          }
          upd.style.display =
            (s.update_available || s.update_failed) ? "" : "none";
        }
      }).catch(function () { /* keep the values already on screen */ });
  }
  setInterval(updateStatus, 30000);
})();
</script>
<script>
document.querySelectorAll(".sta-id").forEach(function (el) {
  el.addEventListener("click", function (ev) {
    ev.stopPropagation();
    var wasOpen = el.classList.contains("open");
    document.querySelectorAll(".sta-id.open").forEach(function (o) {
      o.classList.remove("open");
    });
    if (!wasOpen) el.classList.add("open");
  });
});
document.addEventListener("click", function () {
  document.querySelectorAll(".sta-id.open").forEach(function (o) {
    o.classList.remove("open");
  });
});
</script>
<script>
// Collapsible sections: remember which ones this browser left open,
// so a page reload - every button here posts and reloads - doesn't
// slam the section shut mid-task. (Same behavior as the Pi page.)
document.querySelectorAll("details.section").forEach(function (d) {
  try {
    if (localStorage.getItem("lsl-open-" + d.id) === "1") d.open = true;
  } catch (e) { /* storage unavailable - sections just start closed */ }
  d.addEventListener("toggle", function () {
    try {
      localStorage.setItem("lsl-open-" + d.id, d.open ? "1" : "0");
    } catch (e) {}
  });
});
</script>
<script>
(function () {
  var box = document.getElementById("sta-filter");
  if (!box) return;
  var rows = document.querySelectorAll(".sta");
  var count = document.getElementById("sta-count");
  var nomatch = document.getElementById("sta-nomatch");
  box.addEventListener("input", function () {
    var q = box.value.trim().toUpperCase();
    var shown = 0;
    rows.forEach(function (row) {
      var hit = !q || row.getAttribute("data-sid").indexOf(q) !== -1;
      row.style.display = hit ? "" : "none";
      if (hit) shown++;
    });
    if (count) count.textContent = q
      ? shown + " of " + rows.length + " airports"
      : rows.length + " airports";
    if (nomatch) nomatch.style.display = shown ? "none" : "";
  });
})();
</script>
</body></html>"""


def _builder_neighbors(entries, pos):
    """The orientation line in the builder section: the current
    position with two neighbors on either side (Lite's window)."""
    if not entries:
        return ""
    parts = []
    for p in range(max(0, pos - 2), min(len(entries), pos + 3)):
        parts.append("LED %d: %s" % (p, _esc(entries[p])))
    return ('<p style="color:#555">' + " &middot; ".join(parts)
            + "</p>")


def _render_page(cfg):
    sd = _status_dict(cfg)
    name = cfg.get("map_name", "") or ""
    title = (name + " - " if name else "") + "LS Lite Micro"
    ip = _board_ip()
    b_entries = _live_entries()
    _clamp_builder(len(b_entries))
    b_pos = BUILDER["position"]
    b_current = b_entries[b_pos] if b_entries else "(no airports file)"
    b_err = BUILDER["error"]
    BUILDER["error"] = ""       # shown once, on this render
    repl = {
        "%%FAVICON_B64%%": FAVICON_B64,
        "%%HEADING_NAME%%": (" &mdash; " + _esc(name)) if name else "",
        "%%MAP_STATE%%": "ON" if sd["map_on"] else "OFF",
        "%%TOGGLE_LABEL%%": "Turn map OFF" if sd["map_on"] else "Turn map ON",
        "%%LAST_FETCH%%": _esc(sd["last_fetch_pretty"]),
        "%%OFFLINE_STYLE%%": "" if sd["offline"] else "display:none;",
        "%%OFFLINE_DETAIL%%": _esc(sd["offline_detail"]),
        "%%REPORTING%%": str(sd["stations_reporting"]),
        "%%TOTAL%%": str(sd["stations_total"]),
        "%%VFR%%": str(sd["counts"]["VFR"]),
        "%%MVFR%%": str(sd["counts"]["MVFR"]),
        "%%IFR%%": str(sd["counts"]["IFR"]),
        "%%LIFR%%": str(sd["counts"]["LIFR"]),
        "%%BOARD_IP%%": (" &middot; " + _esc(ip)) if ip else "",
        "%%UPD_STYLE%%": "" if (UPDATE_STATE["available"]
                                or UPDATE_STATE["failed"])
                         else "display:none;",
        "%%UPAVAIL_STYLE%%": "" if UPDATE_STATE["available"]
                             else "display:none;",
        "%%UPFILES%%": str(UPDATE_STATE["files"]),
        "%%UPFAILED_STYLE%%": "" if UPDATE_STATE["failed"]
                              else "display:none;",
        "%%UPFAILED%%": _esc(UPDATE_STATE["failed"]),
        "%%BUILDER_OPEN%%": " open" if (BUILDER["active"] or b_err)
                            else "",
        "%%BUILDER_ERROR%%": ('<p style="color:#b00">%s</p>'
                              % _esc(b_err)) if b_err else "",
        "%%BUILDER_STATE%%": "ON" if BUILDER["active"] else "OFF",
        "%%BUILDER_POS%%": str(b_pos),
        "%%BUILDER_LINE%%": str(b_pos + 1),
        "%%BUILDER_COUNT%%": str(len(b_entries)),
        "%%BUILDER_MAX%%": str(max(0, len(b_entries) - 1)),
        "%%BUILDER_CURRENT%%": _esc(b_current),
        "%%BUILDER_NEIGHBORS%%": _builder_neighbors(b_entries, b_pos),
        "%%BUILDER_TOGGLE_LABEL%%": "Turn builder light OFF"
                                    if BUILDER["active"]
                                    else "Light this LED",
        "%%STATIONS%%": _stations_html(),
        "%%MAP_NAME%%": _esc(name),
        "%%BRIGHTNESS%%": str(cfg["brightness"]),
        "%%NIGHT_CHK%%": "checked" if cfg["night_mode"] else "",
        "%%WIND_CHK%%": "checked" if cfg["high_wind"] else "",
        "%%LTNG_CHK%%": "checked" if cfg["lightning"] else "",
        "%%WIPE_CHK%%": "checked" if cfg.get("use_wipe", True) else "",
        "%%NIGHT_START%%": _esc(cfg["night_start"]),
        "%%NIGHT_END%%": _esc(cfg["night_end"]),
        "%%NIGHT_BRIGHTNESS%%": str(cfg.get("night_brightness", 25)),
        "%%TZ_OFFSET%%": str(cfg["tz_offset_min"]),
        "%%LED_PIN%%": str(cfg["led_pin"]),
        "%%GRB_SEL%%": " selected" if cfg["color_order"] == "GRB" else "",
        "%%RGB_SEL%%": " selected" if cfg["color_order"] == "RGB" else "",
        "%%WIFI_SSID%%": _esc(cfg["wifi_ssid"]),
    }
    body = _PAGE_BODY
    for token, value in repl.items():
        body = body.replace(token, value)
    return (_PAGE_HEAD.replace("%%TITLE%%", _esc(title))
            .replace("%%FAVICON_B64%%", FAVICON_B64)
            + body + _PAGE_SCRIPTS)


async def _reset_later():
    """Reset the board ~1 s after the reboot response has been sent."""
    await asyncio.sleep(1)
    try:
        import machine
        machine.reset()
    except ImportError:
        log("reboot requested (no machine module here; ignored)")


async def _apply_update():
    """Download the changed code files from GitHub, verify each
    against its published blob SHA, swap them in (old copies kept as
    .bak), and reboot. Any failure before the swap leaves the running
    code untouched and records why in UPDATE_STATE."""
    global FORCE_OFF
    if UPDATE_STATE["applying"]:
        return
    UPDATE_STATE["applying"] = True
    FORCE_OFF = True            # dark map while the code is in flight
    staged = []
    try:
        listing = _fetch_repo_listing()
        if listing is None:
            raise Exception("could not reach GitHub")
        plan = _changed_files(listing)
        if not plan:
            UPDATE_STATE["available"] = False
            UPDATE_STATE["files"] = 0
            UPDATE_STATE["failed"] = ""
            FORCE_OFF = False
            return
        for name, info in plan:
            data = _https_get_url(info["download_url"], MAX_CODE_BYTES)
            if data is None:
                await asyncio.sleep(2)   # one retry, as in the check
                data = _https_get_url(info["download_url"],
                                      MAX_CODE_BYTES)
            if data is None:
                raise Exception("download of %s failed" % name)
            if _blob_sha_bytes(data) != info["sha"]:
                raise Exception("verification failed for %s" % name)
            with open(name + ".new", "wb") as f:
                f.write(data)
            staged.append(name)
        for name in staged:
            if _file_blob_sha(name) is not None:
                try:
                    os.remove(name + ".bak")
                except OSError:
                    pass
                os.rename(name, name + ".bak")
            try:
                os.rename(name + ".new", name)
            except OSError:
                try:
                    os.rename(name + ".bak", name)
                except OSError:
                    pass
                raise Exception("could not install %s" % name)
        UPDATE_STATE["available"] = False
        UPDATE_STATE["files"] = 0
        UPDATE_STATE["failed"] = ""
        log("update applied (%s); rebooting" % ", ".join(staged))
        await _reset_later()
        FORCE_OFF = False       # only reached if the reset didn't fire
    except Exception as e:
        UPDATE_STATE["failed"] = ("The last update attempt failed: %s"
                                  % e)
        log("update failed: %s" % e)
        FORCE_OFF = False
        for name in staged:
            try:
                os.remove(name + ".new")
            except OSError:
                pass
    finally:
        UPDATE_STATE["applying"] = False


async def _handle_client(reader, writer):
    try:
        line = await reader.readline()
        if not line:
            return
        parts = line.decode().split(" ")
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]
        length = 0
        ctype = ""
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            if h.lower().startswith(b"content-length:"):
                length = int(h.split(b":")[1])
            elif h.lower().startswith(b"content-type:"):
                ctype = h.split(b":", 1)[1].strip().decode()
        cfg = load_config()
        if method == "GET" and path == "/favicon.ico":
            data = _favicon_png()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                         b"Content-Length: %d\r\n\r\n" % len(data))
            writer.write(data)
            await writer.drain()
            return
        if method == "GET" and path == "/api/status":
            data = json.dumps(_status_dict(cfg)).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: "
                         b"application/json\r\nContent-Length: %d\r\n\r\n"
                         % len(data))
            writer.write(data)
            await writer.drain()
            return
        if method == "POST" and path == "/reboot":
            global FORCE_OFF
            FORCE_OFF = True   # display loop blanks the strip within a frame
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
                         b"<html><body style=\"font-family:sans-serif\">"
                         b"<h2>Rebooting...</h2><p>The map will be back "
                         b"in a few seconds; reload this page then.</p>"
                         b"</body></html>")
            await writer.drain()
            asyncio.create_task(_reset_later())
            return
        if method == "POST" and path == "/update-apply":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n"
                         b"<html><head><meta http-equiv=\"refresh\" "
                         b"content=\"25;url=/\"></head>"
                         b"<body style=\"font-family:sans-serif\">"
                         b"<h2>Updating...</h2><p>The board is downloading "
                         b"the new code from GitHub and will reboot. This "
                         b"page reloads itself in 25 seconds.</p>"
                         b"</body></html>")
            await writer.drain()
            asyncio.create_task(_apply_update())
            return
        if method == "POST" and path == "/toggle":
            cfg["map_on"] = not cfg.get("map_on", True)
            save_config(cfg)
            writer.write(b"HTTP/1.1 303 See Other\r\nLocation: /\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            return
        if method == "POST" and path == "/upload-airports":
            body = b""
            while len(body) < length:
                chunk = await reader.read(min(4096, length - len(body)))
                if not chunk:
                    break
                body += chunk
            boundary = ""
            for part in ctype.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip('"')
            raw = (_multipart_file(body, boundary.encode(),
                                   "airports_file")
                   if boundary else None)
            entries, err = _parse_airports_upload(raw)
            if err:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: "
                             b"text/html\r\n\r\n")
                writer.write(_notice(
                    "Upload not accepted",
                    _esc(err) + ' <a href="/">Back to the '
                    'settings page</a>'))
                await writer.drain()
                return
            _save_airports(entries)
            if _LIVE["entries"] is not None:
                _LIVE["entries"][:] = entries
            BUILDER["position"] = 0  # the list changed - start at top
            _save_builder()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: "
                         b"text/html\r\n\r\n")
            writer.write(_notice(
                "Airports list updated",
                "%d positions saved. The board is rebooting to load "
                "the new list; reload this page in a few seconds."
                % len(entries)))
            await writer.drain()
            asyncio.create_task(_reset_later())
            return
        if method == "POST" and path == "/builder":
            body = b""
            while len(body) < length:
                chunk = await reader.read(length - len(body))
                if not chunk:
                    break
                body += chunk
            form = _parse_form(body.decode())
            entries = _live_entries()
            _clamp_builder(len(entries))
            if "builder_toggle" in form:
                BUILDER["active"] = not BUILDER["active"]
            elif "builder_step" in form:
                try:
                    step = int(form["builder_step"])
                except ValueError:
                    step = 0
                if entries:
                    BUILDER["position"] = max(
                        0, min(len(entries) - 1,
                               BUILDER["position"] + step))
                    BUILDER["active"] = True  # stepping lights the LED
            elif "builder_goto" in form:
                try:
                    pos = int(form.get("builder_pos",
                                       BUILDER["position"]))
                except ValueError:
                    pos = BUILDER["position"]
                if entries:
                    BUILDER["position"] = max(
                        0, min(len(entries) - 1, pos))
                    BUILDER["active"] = True
            elif "builder_legend" in form:
                tag = (form.get("builder_legend")
                       or "").strip().upper()
                if not entries or (tag and tag not in LEGEND_TAGS):
                    BUILDER["error"] = ("That legend choice isn't "
                                        "recognized.")
                else:
                    entries[BUILDER["position"]] = \
                        "LGND:" + tag if tag else "LGND"
                    _save_airports(entries)
                    BUILDER["active"] = True  # keep the light on it
            elif "builder_save" in form:
                new_id = (form.get("builder_id")
                          or "").strip().upper()
                if not entries or not _valid_airport_id(new_id):
                    BUILDER["error"] = (
                        "That doesn't look like a station ID. Use "
                        "the ID as it appears in the METAR data "
                        "(e.g. KFLG), or NULL / LGND.")
                else:
                    entries[BUILDER["position"]] = new_id
                    _save_airports(entries)
                    BUILDER["active"] = True  # keep the light on it
            _save_builder()
            writer.write(b"HTTP/1.1 303 See Other\r\nLocation: /\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            return
        if method == "POST" and path == "/save":
            body = b""
            while len(body) < length:
                chunk = await reader.read(length - len(body))
                if not chunk:
                    break
                body += chunk
            form = _parse_form(body.decode())
            cfg["brightness"] = int(form.get("brightness", cfg["brightness"]))
            cfg["night_mode"] = "night_mode" in form
            cfg["high_wind"] = "high_wind" in form
            cfg["lightning"] = "lightning" in form
            cfg["night_start"] = form.get("night_start", cfg["night_start"])
            cfg["night_end"] = form.get("night_end", cfg["night_end"])
            cfg["night_brightness"] = max(0, min(100, int(form.get(
                "night_brightness", cfg.get("night_brightness", 25)))))
            cfg["tz_offset_min"] = int(form.get("tz_offset_min", cfg["tz_offset_min"]))
            cfg["led_pin"] = int(form.get("led_pin", cfg["led_pin"]))
            cfg["color_order"] = form.get("color_order", cfg["color_order"])
            cfg["use_wipe"] = "use_wipe" in form
            cfg["map_name"] = form.get(
                "map_name", cfg.get("map_name", "")).strip()
            if form.get("wifi_ssid"):
                cfg["wifi_ssid"] = form["wifi_ssid"]
            if form.get("wifi_password"):
                cfg["wifi_password"] = form["wifi_password"]
            save_config(cfg)
            writer.write(b"HTTP/1.1 303 See Other\r\nLocation: /\r\n"
                         b"Content-Length: 0\r\n\r\n")
            await writer.drain()
            return
        data = _render_page(cfg).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(data))
        writer.write(data)
        await writer.drain()
    except Exception as e:
        log("web client error: %s" % e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def _amain(layout, strip, wdt):
    server = await asyncio.start_server(_handle_client, "0.0.0.0", 80)
    log("settings page on http://<board-ip>/")
    wanted = set(p for p in layout
                 if p != "NULL" and not p.startswith("LGND"))
    STATE["total"] = len(wanted)
    last_fetch = time.ticks_add(time.ticks_ms(), -FETCH_INTERVAL_MS)
    last_update = time.ticks_add(time.ticks_ms(), -UPDATE_CHECK_MS)
    last_ntp = time.ticks_ms()
    # The staleness clock for the "Board Not Running" banner starts at
    # boot: a board that never gets data raises the banner after
    # OFFLINE_AGE_MIN minutes even before OFFLINE_FAILS failures pile up.
    STATE["last_ok_ticks"] = last_ntp
    while True:
        cfg = load_config()
        # The builder light shows at full brightness (you're standing
        # at the map looking for one LED), whatever the day/night level.
        bright = 100.0 if _builder_active(layout) \
            else effective_brightness(cfg)
        now = time.ticks_ms()
        if time.ticks_diff(now, last_fetch) >= FETCH_INTERVAL_MS:
            # Recomputed each cycle from the live list, so builder
            # edits are fetched for from the next cycle on.
            wanted = set(p for p in layout
                         if p != "NULL" and not p.startswith("LGND"))
            STATE["total"] = len(wanted)
            fresh = fetch_metars(wanted, wdt)
            STATE["fetch_attempts"] += 1
            if fresh is not None and len(fresh):
                STATE["fetch_failures"] = 0
                STATE["last_error"] = ""
                STATE["last_ok_ticks"] = time.ticks_ms()
            elif fresh is not None:
                # The fetch worked but the cache held none of our
                # stations - the map is just as starved as a failure.
                STATE["fetch_failures"] += 1
                if not STATE["last_error"]:
                    STATE["last_error"] = ("cache file held none of "
                                           "the mapped stations")
            else:
                STATE["fetch_failures"] += 1
            if fresh is not None:
                STATE["metars"] = fresh
                STATE["reporting"] = len(fresh)
                STATE["last_fetch_ts"] = time.time()
                t = time.gmtime()
                STATE["last_fetch"] = "%04d-%02d-%02d %02d:%02d UTC" % (
                    t[0], t[1], t[2], t[3], t[4])
                colors, symbols = compute_frame(layout, fresh, cfg)
                if cfg.get("use_wipe", True):
                    strip.wipe(colors, bright, symbols)
                else:
                    strip.show(colors, bright, symbols)
                log("fetch ok: %d of %d stations reporting"
                    % (len(fresh), len(wanted)))
            else:
                log("fetch failed; keeping previous frame")
            last_fetch = time.ticks_ms()
        if time.ticks_diff(now, last_update) >= UPDATE_CHECK_MS:
            try:
                _check_update()
            except Exception as e:
                # The updater must never disturb the map itself.
                log("update check error: %s" % e)
            last_update = time.ticks_ms()
        if time.ticks_diff(now, last_ntp) > 24 * 3600 * 1000:
            last_ntp = now
            try:
                import ntptime
                ntptime.settime()
            except Exception:
                pass
        colors, symbols = compute_frame(layout, STATE["metars"], cfg)
        if isinstance(strip, SimStrip):
            strip.show(colors, bright, symbols)
        else:
            strip.show(colors, bright)
        if wdt:
            wdt.feed()
        await asyncio.sleep(RENDER_INTERVAL_S)


def run():
    layout = load_airports()
    _LIVE["entries"] = layout
    _load_builder(len(layout))
    cfg = load_config()
    strip = make_strip(len(layout), cfg)
    wdt = None
    if cfg.get("watchdog"):
        try:
            from machine import WDT
            wdt = WDT(timeout=300000)   # 5 min; fed every render + fetch chunk
            log("watchdog on (300 s)")
        except Exception as e:
            log("watchdog unavailable: %s" % e)
    try:
        asyncio.run(_amain(layout, strip, wdt))
    finally:
        try:
            asyncio.new_event_loop()
        except Exception:
            pass
