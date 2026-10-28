# scanner.py - Streaming scanner for the AWC METAR cache XML.
#
# The cache file is far too large to hold or DOM-parse on a
# microcontroller, so this scanner consumes the *decompressed* XML as a
# byte stream (feed it chunks of any size), extracts each <METAR> block
# as it completes, and keeps only the newest report for the stations we
# care about. Peak memory is one chunk plus one METAR block (~2 KB).
#
# Pure Python: no board-specific imports, so the identical file runs
# under MicroPython on the ESP32 and under CPython for bench testing.

_OPEN = b"<METAR>"
_CLOSE = b"</METAR>"


def _tag_text(block, tag):
    """Bytes between <tag> and </tag> in block, or None."""
    open_t = b"<" + tag + b">"
    i = block.find(open_t)
    if i < 0:
        return None
    i += len(open_t)
    j = block.find(b"</" + tag + b">", i)
    if j < 0:
        return None
    return block[i:j].strip()


def _to_int(b):
    if b is None:
        return None
    try:
        return int(float(b.decode()))
    except (ValueError, UnicodeError):
        return None


def _to_float(b):
    if b is None:
        return None
    try:
        return float(b.decode())
    except (ValueError, UnicodeError):
        return None


def _vis(b):
    """Visibility in statute miles; AWC writes '10+' and sometimes
    fractions like '1/2' - mirrors metar_lite._vis."""
    if b is None:
        return None
    s = b.decode().strip().rstrip("+")
    if "/" in s:
        num, _, den = s.partition("/")
        try:
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _ceiling_ft(block):
    """Lowest BKN/OVC/OVX layer base (ft AGL) in a METAR block, or None."""
    best = None
    i = 0
    while True:
        i = block.find(b"<sky_condition", i)
        if i < 0:
            return best
        j = block.find(b"/>", i)
        if j < 0:
            return best
        tag = block[i:j]
        i = j + 2
        cover = None
        k = tag.find(b'sky_cover="')
        if k >= 0:
            k += len(b'sky_cover="')
            e = tag.find(b'"', k)
            if e > k:
                cover = tag[k:e]
        if cover not in (b"BKN", b"OVC", b"OVX"):
            continue
        k = tag.find(b'cloud_base_ft_agl="')
        if k < 0:
            continue
        k += len(b'cloud_base_ft_agl="')
        e = tag.find(b'"', k)
        if e <= k:
            continue
        base = _to_int(tag[k:e])
        if base is not None and (best is None or base < best):
            best = base
    return best


def compute_category(vis, ceiling):
    """Flight category from visibility (SM) and ceiling (ft), FAA rules.
    Identical thresholds to metar_lite.compute_category."""
    if (vis is not None and vis < 1) or (ceiling is not None and ceiling < 500):
        return "LIFR"
    if (vis is not None and vis < 3) or (ceiling is not None and ceiling < 1000):
        return "IFR"
    if (vis is not None and vis <= 5) or (ceiling is not None and ceiling <= 3000):
        return "MVFR"
    return "VFR"


_CATEGORIES = (b"VFR", b"MVFR", b"IFR", b"LIFR")


class MetarScanner:
    """feed(bytes) decompressed XML in chunks; read .metars at EOF.

    wanted: set of station IDs as str (e.g. {"KFLG", "KGCN"}).
    Result: {station_id (str): {"obs_time": str, "wind_kt": int|None,
    "gust_kt": int|None, "vis": float|None, "wx": str, "category": str,
    "raw": str (the raw METAR text, for the admin page's Stations list)}}
    """

    def __init__(self, wanted):
        self.wanted = set(w.encode() for w in wanted)
        self.metars = {}
        self._buf = bytearray()

    def feed(self, data):
        if not data:
            return
        self._buf += data
        while True:
            start = self._buf.find(_OPEN)
            if start < 0:
                # No block opening: keep only a tail that could be the
                # start of a split '<METAR>' tag.
                keep = len(_OPEN) - 1
                if len(self._buf) > keep:
                    # NOTE: slice-rebind, not del buf[:n] - MicroPython
                    # bytearray does not support item deletion.
                    self._buf = self._buf[len(self._buf) - keep:]
                return
            if start > 0:
                self._buf = self._buf[start:]
            end = self._buf.find(_CLOSE)
            if end < 0:
                return  # block not complete yet; wait for more data
            end += len(_CLOSE)
            block = bytes(self._buf[:end])
            self._buf = self._buf[end:]
            self._handle(block)

    def _handle(self, block):
        sid_b = _tag_text(block, b"station_id")
        if sid_b is None or sid_b not in self.wanted:
            return
        obs_b = _tag_text(block, b"observation_time") or b""
        prev = self.metars.get(sid_b)
        if prev is not None and prev["_obs_b"] >= obs_b:
            return  # keep only the newest report per station
        cat_b = _tag_text(block, b"flight_category")
        if cat_b in _CATEGORIES:
            category = cat_b.decode()
        else:
            category = compute_category(
                _vis(_tag_text(block, b"visibility_statute_mi")),
                _ceiling_ft(block))
        wx_b = _tag_text(block, b"wx_string") or b""
        raw_b = _tag_text(block, b"raw_text") or b""
        try:
            raw = raw_b.decode()
        except Exception:
            raw = ""
        self.metars[sid_b] = {
            "_obs_b": obs_b,
            "obs_time": obs_b.decode(),
            "wind_kt": _to_int(_tag_text(block, b"wind_speed_kt")),
            "gust_kt": _to_int(_tag_text(block, b"wind_gust_kt")),
            "vis": _vis(_tag_text(block, b"visibility_statute_mi")),
            "wx": wx_b.decode(),
            "category": category,
            "raw": raw,
        }

    def finish(self):
        """Return the public result dict (str keys, no byte internals)."""
        out = {}
        for sid_b, m in self.metars.items():
            out[sid_b.decode()] = {
                "obs_time": m["obs_time"],
                "wind_kt": m["wind_kt"],
                "gust_kt": m["gust_kt"],
                "vis": m["vis"],
                "wx": m["wx"],
                "category": m["category"],
                "raw": m["raw"],
            }
        return out
