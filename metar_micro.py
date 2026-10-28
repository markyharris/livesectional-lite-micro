# metar_micro.py - METAR Map Lite for MicroPython (ESP32 / ESP32-S3).
#
# Behavior mirrors the Raspberry Pi LiveSectional Lite, restructured
# for a microcontroller: the AWC cache file is downloaded over HTTPS
# and its gzip stream is inflated and scanned incrementally
# (scanner.py), so the whole file is never held in RAM. METARs only,
# one LED per airport position from the `airports` file; tagged
# legends, silver no-data, category-first lightning, blank-then-wipe;
# admin page with live status, a Stations list (raw METAR popups) and
# an airports-file upload.
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
         "reporting": 0, "total": 0}

UPDATE_STATE = {"available": False, "files": 0, "failed": "",
                "applying": False}

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
            req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
                   "Accept: application/octet-stream\r\n"
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
        items = json.loads(data)
    except ValueError:
        return None
    out = {}
    for it in items:
        if isinstance(it, dict) and it.get("type") == "file":
            out[it.get("name", "")] = {
                "sha": it.get("sha", ""),
                "download_url": it.get("download_url") or ""}
    return out


def _fetch_repo_listing():
    return _parse_listing(_https_get_url(
        "https://api.github.com/repos/%s/contents/" % GITHUB_REPO,
        MAX_API_BYTES))


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
    if FORCE_OFF or not cfg.get("map_on", True):
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
# airport LED recolored from VFR green to IFR red, so the two
# controllers' browser tabs are easy to tell apart.
# 64x64 palette PNG, 1844 bytes.
FAVICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAMAAACdt4HsAAADAFBMVEXs6tL28d3c483J"
    "w6v59eKxvKivAAHSnq0iXZOt3LTP1My8yMlnl6PQca0UTJHh0L/A8cHg28K2GBlWd524"
    "5rnBv6fOam8Zf3wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACRDMEgAAAB"
    "AHRSTlP///////////////////////////////8AAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAwt7xIwAAAuNJREFUeNqtmImWqyAMhllMAUFBsfb93/QGbK0sbtP7t2dqT81H"
    "FgI4BDiUej6f/JqAPEldNKo5EydAGGq121wmpI/S7+QDGNieH8cAugIGxn4C7BHoZQCz"
    "rEa4AQgu1PJ3GVAl3ALEINgvgEoabgJiEOwHQJmGu4AiDdEgndPHgDwNKaDmUQ4o0xAb"
    "LL6rygHM5m0VcQuTXQFgEMKSredMGy+lN8Bq7R0ALJXtuk7YFQGyR2tjfC/hAgB/H6wV"
    "yLChkwkzvVcuSuFlWxASAPkGGhgCv/ge3KSiJqekLAjfHGwyviKs6ZVTq6ZJ+u+NRRmL"
    "2lkRxldCiXkcZ/xQDnpDhmRabKtQlGiQXgCaP6JmMXtMKGf7ZcwB0AMYeNs/HqNEoQvp"
    "3EzmwWbw8MdIp8AE03kMhBc6gFm4BIjyBjOIDoyOTNGHl1HwLkQNUGRRGicUjj0T54IL"
    "o1AT9HwHkO8GC2AKgFG4mIhRCAToGiBfyJr4iQDwr2j6WAFlCM/KWr4AvMQGeD1WzUo4"
    "I9kpIFT3XUbMulzt0QE1SZN29SGglUgwSwmjfUgBMHY5BGJ6gzM4zORxNBDmsvSEXc8B"
    "1sFjLwSGEGDE5EzP2SUAXSrZgpQgAN4AtIfi5j1AeGlNOBKmaXEBW8lordt03d4FNES3"
    "GO9gMI8gsCdiJ7ZROihcIIsTTmtbctPqEAXeoQ32IJbDQ1ijtlM9woDwpnYq0G1D1z0B"
    "h+TYn6KrnEB4DdAwTprPrvRdLG3HLwEoRfcbmm9rpOpC1YPQb1WA7Sw9B4ThdwCkE+cA"
    "LH7TrFv5aQwZgDLNQhI/07ESwzGg1UsVCKmZxxgOAbr9nm/rx4EihgBIhz8Wz2PYeKD1"
    "zrnmMAkfQJi7dHM+3lWehA9g6/4xoKsCdEsuAkQNsHTuppduAvLsH3rAu/0yJvvaHUBz"
    "D2DP2/lIVpx4QMk9/XcA/QuA/grIH6eLhxR69HCOJxR+S/n/BeAfLu4jZhEgNj4AAAAA"
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
    return {"last_fetch_pretty": _pretty_fetch(cfg),
            "stations_reporting": STATE["reporting"],
            "stations_total": STATE["total"],
            "counts": counts, "stations": stations,
            "map_on": bool(cfg.get("map_on", True)),
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
<h2 style="margin:0">METAR Map Micro%%HEADING_NAME%%</h2>
</div>
<div class="status">
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
<form method="post" action="/toggle"><p><button type="submit">%%TOGGLE_LABEL%%</button></p></form>
<form method="post" action="/reboot" onsubmit="return confirm('Reboot the board now?')"><p><button type="submit">Reboot board</button></p></form>
<hr>
<h3 style="margin-bottom:0.3em">Stations</h3>
<p style="color:#555;margin:0.2em 0 0">Every airport on the map with its
current flight-category color. Hover or tap an airport ID to see its
raw METAR. The list refreshes along with the status box, every 30
seconds.</p>
%%STATIONS%%
<hr>
<h3 style="margin-bottom:0.3em">Settings</h3>
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
<hr>
<h3 style="margin-bottom:0.3em">Airports file</h3>
<p style="color:#555;margin:0.2em 0 0">Replace the station list (one
entry per line, in LED order - an original LiveSectional airports
backup works as-is). The current list is kept as airports.bak, and
the board reboots to load the new list.</p>
<form method="post" action="/upload-airports" enctype="multipart/form-data">
<p><input type="file" name="airports_file" required>
<button type="submit">Upload airports file</button></p></form>
<p style="color:#888;font-size:0.85em;margin-top:1.8em">Weather data:
NOAA Aviation Weather Center, refreshed every 5 minutes. Displayed
conditions are indicative only - not for navigation.</p>
<p style="text-align:center;color:#888;font-size:0.85em">METAR Map Micro &middot; LED METAR map</p>
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


def _render_page(cfg):
    sd = _status_dict(cfg)
    name = cfg.get("map_name", "") or ""
    title = (name + " - " if name else "") + "METAR Map Micro"
    ip = _board_ip()
    repl = {
        "%%FAVICON_B64%%": FAVICON_B64,
        "%%HEADING_NAME%%": (" &mdash; " + _esc(name)) if name else "",
        "%%MAP_STATE%%": "ON" if sd["map_on"] else "OFF",
        "%%TOGGLE_LABEL%%": "Turn map OFF" if sd["map_on"] else "Turn map ON",
        "%%LAST_FETCH%%": _esc(sd["last_fetch_pretty"]),
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
    while True:
        cfg = load_config()
        now = time.ticks_ms()
        if time.ticks_diff(now, last_fetch) >= FETCH_INTERVAL_MS:
            fresh = fetch_metars(wanted, wdt)
            if fresh is not None:
                STATE["metars"] = fresh
                STATE["reporting"] = len(fresh)
                STATE["last_fetch_ts"] = time.time()
                t = time.gmtime()
                STATE["last_fetch"] = "%04d-%02d-%02d %02d:%02d UTC" % (
                    t[0], t[1], t[2], t[3], t[4])
                colors, symbols = compute_frame(layout, fresh, cfg)
                if cfg.get("use_wipe", True):
                    strip.wipe(colors, effective_brightness(cfg), symbols)
                else:
                    strip.show(colors, effective_brightness(cfg), symbols)
                log("fetch ok: %d of %d stations reporting"
                    % (len(fresh), len(wanted)))
            else:
                log("fetch failed; keeping previous frame")
            last_fetch = time.ticks_ms()
        if time.ticks_diff(now, last_update) >= UPDATE_CHECK_MS:
            _check_update()
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
            strip.show(colors, effective_brightness(cfg), symbols)
        else:
            strip.show(colors, effective_brightness(cfg))
        if wdt:
            wdt.feed()
        await asyncio.sleep(RENDER_INTERVAL_S)


def run():
    layout = load_airports()
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
