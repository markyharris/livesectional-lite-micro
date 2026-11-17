# test_address.py - desktop (CPython) checks for the two ways the
# map says where its admin page lives (standing rule, Nov 16, 2026:
# a connected device tells the owner its admin address).
#
# Part A, the provisioning completion page: the real async portal
# handler is driven with a fake station and a fake stream writer -
# a join that works must serve the completion page carrying the
# assigned address, save only then, and schedule the reboot after
# the read-it hold; a join that fails must hand the form back with
# nothing saved and no reboot, the portal still serving.
#
# Part B, the blink code: the code is played through a recording
# strip with the clock faked, and the blink/colour/pause schedule is
# asserted from the recorded LED writes themselves - for .24, .8,
# .108 and .213 - plus the wiring facts (boot path only, never the
# running loop) and the strip's GRB/RGB wire order, which is what
# makes the position colours come out true on a real strip.
#
# Run from the project root:  python3 test_address.py
# Exits 0 when every check passes, 1 otherwise.

import asyncio
import inspect
import sys
import types

import metar_micro as mm

FAILED = []


def check(name, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", name))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# Part A - the completion flow, through the real portal handler
# ---------------------------------------------------------------------------
class FakeSTA:
    """Stands in for network.WLAN(STA_IF): once connect() is called,
    isconnected() turns true after `polls_needed` polls - or never,
    when polls_needed is None (a password the router rejects)."""

    def __init__(self, ip, polls_needed=1):
        self._ip = ip
        self._polls_needed = polls_needed
        self._polls = 0
        self._joining = False
        self.ssid = None

    def active(self, on=None):
        return True

    def disconnect(self):
        self._joining = False
        self._polls = 0

    def connect(self, ssid, password):
        self.ssid = ssid
        self._joining = True
        self._polls = 0

    def isconnected(self):
        if not self._joining or self._polls_needed is None:
            return False
        self._polls += 1
        return self._polls > self._polls_needed

    def ifconfig(self):
        return (self._ip, "255.255.255.0", "192.168.87.1",
                "192.168.87.1")

    def scan(self):
        return []


class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, chunk):
        self.data += chunk

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def _reader_for(raw):
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    return reader


async def _drive(raw, writer, ctx):
    # The reader is built inside the loop: a StreamReader wants the
    # running loop, which asyncio.run only provides in here.
    await mm._prov_handle(_reader_for(raw), writer, ctx)


def _post_save(ssid, password):
    body = ("ssid=%s&wifi_password=%s" % (ssid, password)).encode()
    return (b"POST /save HTTP/1.1\r\nHost: 192.168.4.1\r\n"
            b"Content-Length: %d\r\n\r\n" % len(body)) + body


def _run_save(sta, ssid, password):
    """POST the setup form to the real handler, with config writes
    captured and the reboot turned into a recorder. The join wait is
    shortened so a failure case takes ~2 s, not 15."""
    saved = {}
    resets = []

    def fake_reset(delay=1):
        resets.append(delay)          # recorded at call time: the
        async def _noop():            # coroutine body may never be
            return None               # scheduled before loop close
        return _noop()

    orig = (mm.load_config, mm.save_config, mm._reset_later,
            mm.PROV_JOIN_WAIT_S)
    mm.load_config = lambda: dict(mm.DEFAULT_CONFIG)
    mm.save_config = lambda cfg: saved.update(cfg)
    mm._reset_later = fake_reset
    mm.PROV_JOIN_WAIT_S = 2
    try:
        writer = FakeWriter()
        ctx = {"sta": sta, "nets": [("HomeNet", -50)], "done": None}
        asyncio.run(_drive(_post_save(ssid, password), writer, ctx))
    finally:
        (mm.load_config, mm.save_config, mm._reset_later,
         mm.PROV_JOIN_WAIT_S) = orig
    return bytes(writer.data), resets, ctx, saved


# -- a join that works -------------------------------------------------------
resp, resets, ctx, saved = _run_save(FakeSTA("192.168.87.46"),
                                     "HomeNet", "pw123")
text = resp.decode()
check("portal success: completion page names the joined SSID",
      "Connected" in text and "HomeNet" in text)
check("portal success: completion page carries the assigned IP",
      "http://192.168.87.46/" in text)
check("portal success: credentials saved (and only after the join)",
      saved.get("wifi_ssid") == "HomeNet"
      and saved.get("wifi_password") == "pw123")
check("portal success: reboot scheduled after the read-it hold",
      resets == [mm.PROV_DONE_HOLD_S] and mm.PROV_DONE_HOLD_S == 45)
check("portal success: completion state kept for the hold",
      ctx.get("done") == ("HomeNet", "192.168.87.46"))

# -- during the hold, asking again re-serves the completion page -------------
writer = FakeWriter()
asyncio.run(_drive(b"GET / HTTP/1.1\r\n\r\n", writer, ctx))
check("portal hold: a re-ask re-serves the address, not the form",
      b"192.168.87.46" in bytes(writer.data)
      and b"wifi_password" not in bytes(writer.data))

# -- a join that fails --------------------------------------------------------
resp, resets, ctx2, saved2 = _run_save(
    FakeSTA("192.168.87.46", polls_needed=None), "HomeNet", "wrongpw")
text = resp.decode()
check("portal failure: the form comes back, with the error",
      "would not join" in text and 'name="wifi_password"' in text)
check("portal failure: nothing was saved", saved2 == {})
check("portal failure: no reboot was scheduled", resets == [])
check("portal failure: portal still serving (no done state)",
      ctx2.get("done") is None)

# -- the completion page itself ----------------------------------------------
page = mm._prov_done_page("A&B <Net>", "192.168.87.46").decode()
check("done page escapes the SSID",
      "A&amp;B &lt;Net&gt;" in page)
check("done page names the hold length",
      str(mm.PROV_DONE_HOLD_S) in page)


# ---------------------------------------------------------------------------
# Part B - the blink code, read back off the recorded LED writes
# ---------------------------------------------------------------------------
class RecordingStrip:
    """Stands in for the strip: every show() is recorded verbatim."""

    def __init__(self, count):
        self.count = count
        self.frames = []

    def show(self, colors, pct, symbols=None):
        self.frames.append((list(colors), pct))

    def wipe(self, colors, pct, symbols=None):
        self.frames.append((list(colors), pct))


def _play(ip, count=8):
    """Play the blink code with the clock faked; returns the strip
    and the (color, ms) events reconstructed from the recorded
    frames and sleeps - exactly what the LED was told to do."""
    strip = RecordingStrip(count)
    sleeps = []
    real_sleep = mm.time.sleep
    mm.time.sleep = lambda s: sleeps.append(int(round(s * 1000)))
    try:
        mm.blink_ip(strip, ip, count)
    finally:
        mm.time.sleep = real_sleep
    events = []
    for (frame, _pct), ms in zip(strip.frames, sleeps):
        lit = [c for c in frame if c != mm.OFF]
        events.append((lit[0] if lit else None, ms))
    return strip, events


# The spec, restated independently of the code under test: white,
# amber, violet by digit position; 300 ms blinks; 250 ms between
# blinks of a digit and as the beat after each digit; 900 ms between
# digits; a zero is one 900 ms blink; 2500 ms between repeats; the
# sequence runs three times.
_W, _A, _V = (255, 255, 255), (255, 160, 0), (148, 0, 211)


def _expected(digits):
    ev = []
    for rep in range(3):
        if rep:
            ev.append((None, 2500))
        for pos, d in enumerate(digits):
            if pos:
                ev.append((None, 900))
            color = (_W, _A, _V)[pos]
            if d == 0:
                ev.append((color, 900))
            else:
                for k in range(d):
                    if k:
                        ev.append((None, 250))
                    ev.append((color, 300))
            ev.append((None, 250))
    return ev


for ip, digits in (("192.168.87.24", [2, 4]),
                   ("192.168.87.8", [8]),
                   ("192.168.87.108", [1, 0, 8]),
                   ("192.168.87.213", [2, 1, 3])):
    strip, events = _play(ip)
    check("blink %s: exact blink/colour/pause schedule" % ip,
          events == _expected(digits),
          )
    check("blink %s: only the first LED ever lights, at 60%%" % ip,
          all(pct == 60 and all(c == mm.OFF for c in frame[1:])
              for frame, pct in strip.frames))
    check("blink %s: strip left dark at the end" % ip,
          all(c == mm.OFF for c in strip.frames[-1][0]))

_strip, _events = _play("192.168.87.213")
check("blink .213: the whole sequence takes 20.3 s",
      sum(ms for _c, ms in _events) == 20300)
_strip, _events = _play("192.168.87.255")
check("blink .255 (arithmetic worst case): 30.2 s",
      sum(ms for _c, ms in _events) == 30200)

_silent = RecordingStrip(8)
mm.blink_ip(_silent, "0.0.0.0", 8)
check("blink: 0.0.0.0 (not really connected) stays silent",
      _silent.frames == [])
check("blink: an unparseable address has no schedule",
      mm._blink_schedule("") == [] and mm._blink_schedule("10.0.0") == [])

# Boot joins only: the call lives in run() (the boot path); the
# running loop - which is where a mid-run reconnect would surface -
# never calls it.
check("blink: wired into run(), the boot path",
      "blink_ip" in inspect.getsource(mm.run))
check("blink: never called from the running loop",
      "blink_ip" not in inspect.getsource(mm._amain))

# The position colours must come out true on the wire: NeoStrip's
# own _set applies the configured color_order, so GRB glass gets
# (G, R, B) and RGB glass gets (R, G, B).
class _FakeNP:
    def __init__(self, pin, count):
        self.buf = [None] * count

    def __setitem__(self, i, v):
        self.buf[i] = v

    def write(self):
        pass


_fake_machine = types.ModuleType("machine")
_fake_machine.Pin = lambda *a, **k: object()
_fake_neopixel = types.ModuleType("neopixel")
_fake_neopixel.NeoPixel = _FakeNP
sys.modules["machine"] = _fake_machine
sys.modules["neopixel"] = _fake_neopixel
try:
    _ns = mm.NeoStrip(4, 5, "GRB")
    _ns.show([(255, 160, 0)] + [mm.OFF] * 3, 100)
    check("strip GRB: amber goes on the wire as (G, R, B)",
          _ns.np.buf[0] == (160, 255, 0))
    _ns = mm.NeoStrip(4, 5, "RGB")
    _ns.show([(148, 0, 211)] + [mm.OFF] * 3, 100)
    check("strip RGB: violet goes on the wire as (R, G, B)",
          _ns.np.buf[0] == (148, 0, 211))
finally:
    del sys.modules["machine"]
    del sys.modules["neopixel"]

print()
if FAILED:
    print("%d check(s) FAILED: %s" % (len(FAILED), FAILED))
    sys.exit(1)
print("all checks passed")
