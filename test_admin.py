# test_admin.py - desktop (CPython) checks for the admin page's
# UX pass (standing admin-page design rule, Nov 16, 2026 - the
# painter clock v24 pattern, adapted to this page):
#
# - the two bounded 0-100 settings (brightness, night
#   brightness) are real range sliders with live readouts;
# - one LED-grid SVG is both the tab favicon (a data URI
#   derived from the same string) and the icon beside the
#   title in the header;
# - every section wears its own accent colour;
# - every action lands back on the admin page itself: Save /
#   toggle / builder answer with the page plus a small note,
#   a rejected upload answers with the page plus the error,
#   and the rebooting actions (Reboot, Update now, a good
#   upload) answer with an auto-refreshing page back to it.
#
# The page checks render _render_page with a stub config; the
# action checks drive the real async _handle_client with a
# fake stream writer (the test_address.py pattern), in a temp
# directory so config / airports / builder writes stay there.
#
# Run from the project root:  python3 test_admin.py
# Exits 0 when every check passes, 1 otherwise.

import asyncio
import os
import sys
import tempfile

import metar_micro as mm

FAILED = []


def check(name, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", name))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------
cfg = dict(mm.DEFAULT_CONFIG, brightness=60, night_brightness=25,
           map_name="Porch")
page = mm._render_page(cfg)

check("sliders: exactly the two 0-100 settings are range inputs",
      page.count('type="range"') == 2)
for name, val in (("brightness", "60"), ("night_brightness", "25")):
    check("slider %s: carries the config value, with a live readout"
          % name,
          'name="%s"' % name in page and 'max="100"' in page
          and 'value="%s"' % val in page
          and 'id="v_%s"' % name in page
          and "getElementById('v_%s')" % name in page)
check("free-form settings stay plain inputs (tz offset, LED pin)",
      'name="tz_offset_min" type="number"' in page
      and 'name="led_pin" type="number"' in page)

check("icon: the tab favicon is the SVG data URI",
      '<link rel="icon" href="data:image/svg+xml;utf8,' in page
      and "%3Csvg" in page and "%23223140" in page)
check("icon: the same SVG sits inline beside the header title",
      mm.ICON_SVG in page
      and page.index(mm.ICON_SVG)
      < page.index('<h2 style="margin:0">LS Lite Micro'))
check("icon: the data URI is derived from the one SVG literal",
      mm.ICON_URI == ("data:image/svg+xml;utf8,"
                      + mm.ICON_SVG.replace("#", "%23")
                      .replace("<", "%3C").replace(">", "%3E"))
      and "#00b050" in mm.ICON_SVG and "#e600e6" in mm.ICON_SVG)

for sec, tint in (("sec-status", "#e9f1fd"),
                  ("sec-builder", "#e7f6ec"),
                  ("sec-power", "#fdeaea"),
                  ("sec-settings", "#e2f5f7"),
                  ("sec-stations", "#fff6d9")):
    check("section %s: present, wearing its accent colour %s"
          % (sec, tint),
          sec in page and (".%s { background: %s; }" % (sec, tint))
          in page)
check("status box is the blue Status section",
      'class="status sec sec-status"' in page
      and "<h2 style=\"margin:0 0 0.4em\">Status</h2>" in page)

for frag in ('name="map_name"', 'name="night_mode"',
             'name="night_start"', 'name="night_end"',
             'name="high_wind"', 'name="lightning"',
             'name="use_wipe"', 'name="color_order"',
             'name="wifi_ssid"', 'name="wifi_password"',
             'action="/save"', 'action="/builder"',
             'action="/toggle"', 'action="/reboot"',
             'action="/upload-airports"', 'action="/update-apply"',
             "Map Builder", "Power", "Settings", "Stations",
             'id="sta-filter"', "not for navigation"):
    check("controls kept: %s" % frag, frag in page)
check("a plain page render wears no note",
      'class="msg"' not in page)


# ---------------------------------------------------------------------------
# The actions, through the real handler
# ---------------------------------------------------------------------------
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


async def _drive(raw, writer):
    await mm._handle_client(_reader_for(raw), writer)


def _post(path, body=b"",
          ctype="application/x-www-form-urlencoded"):
    if isinstance(body, str):
        body = body.encode()
    return (b"POST %s HTTP/1.1\r\nHost: map\r\n"
            b"Content-Type: %s\r\nContent-Length: %d\r\n\r\n"
            % (path.encode(), ctype.encode(), len(body))) + body


def _upload_req(content):
    boundary = "TESTBOUNDARY"
    body = (("--%s\r\n" % boundary).encode()
            + b'Content-Disposition: form-data; '
              b'name="airports_file"; filename="airports.txt"\r\n'
            + b"Content-Type: text/plain\r\n\r\n"
            + content
            + ("\r\n--%s--\r\n" % boundary).encode())
    return _post("/upload-airports", body,
                 "multipart/form-data; boundary=%s" % boundary)


BASE_CFG = dict(mm.DEFAULT_CONFIG, brightness=60,
                night_brightness=25, wifi_ssid="HomeNet")
saved = {}
resets = []
applied = []


def _fake_reset(delay=1):
    resets.append(delay)              # recorded at call time: the
    async def _noop():                # coroutine body may never be
        return None                   # scheduled before loop close
    return _noop()


def _fake_apply():
    applied.append(1)
    async def _noop():
        return None
    return _noop()


def _run(raw):
    writer = FakeWriter()
    asyncio.run(_drive(raw, writer))
    return bytes(writer.data)


_orig = (mm.load_config, mm.save_config, mm._reset_later,
         mm._apply_update)
mm.load_config = lambda: dict(BASE_CFG)
mm.save_config = lambda cfg: saved.update(cfg)
mm._reset_later = _fake_reset
mm._apply_update = _fake_apply
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp())
try:
    with open("airports", "w") as f:
        f.write("KFLG\nKGEU\nNULL\nLGND\n")

    resp = _run(b"GET / HTTP/1.1\r\n\r\n")
    check("GET: the admin page, no note",
          resp.startswith(b"HTTP/1.1 200") and b"LS Lite Micro" in resp
          and b'class="msg"' not in resp)

    resp = _run(_post("/save",
                      "brightness=80&night_brightness=10"
                      "&night_mode=on&high_wind=on&lightning=on"
                      "&use_wipe=on&night_start=22%3A00"
                      "&night_end=06%3A00&tz_offset_min=-420"
                      "&led_pin=5&color_order=GRB&map_name=Porch"
                      "&wifi_ssid=HomeNet"))
    text = resp.decode()
    check("POST /save: saves as before (both slider values land)",
          saved.get("brightness") == 80
          and saved.get("night_brightness") == 10
          and saved.get("map_name") == "Porch")
    check("POST /save: answers with the admin page + the note",
          resp.startswith(b"HTTP/1.1 200")
          and "Settings saved." in text and 'class="msg"' in text)
    check("POST /save: the returned page wears the new values",
          'name="brightness"' in text and 'value="80"' in text)

    resp = _run(_post("/toggle"))
    check("POST /toggle: flips the map and answers with the page",
          saved.get("map_on") is False
          and "Map turned off." in resp.decode())

    resp = _run(_post("/builder", "builder_toggle=1"))
    check("POST /builder: acts, answers with the admin page itself",
          mm.BUILDER["active"] is True
          and resp.startswith(b"HTTP/1.1 200")
          and b"LS Lite Micro" in resp and b"303" not in resp[:20])
    mm.BUILDER["active"] = False

    resp = _run(_post("/reboot"))
    check("POST /reboot: resets, answers with the refresh page",
          resets == [1] and b'http-equiv="refresh"' in resp
          and b'content="40;url=/"' in resp and b'href="/"' in resp)

    resp = _run(_post("/update-apply"))
    check("POST /update-apply: applies, answers with refresh page",
          applied == [1] and b'http-equiv="refresh"' in resp
          and b'content="25;url=/"' in resp and b'href="/"' in resp)

    resp = _run(_upload_req(b"BOGUS!!\n"))
    text = resp.decode()
    check("POST /upload-airports (bad): the page, wearing the error",
          'class="msg err"' in text
          and "Upload not accepted" in text
          and "station IDs" in text)
    check("POST /upload-airports (bad): no reboot scheduled",
          resets == [1])

    resp = _run(_upload_req(b"KFLG\nKGEU\nLGND\n"))
    check("POST /upload-airports (good): refresh page, reboot set",
          b"Airports list updated" in resp
          and b'content="40;url=/"' in resp and resets == [1, 1])
    with open("airports") as f:
        check("POST /upload-airports (good): the list was replaced",
              f.read() == "KFLG\nKGEU\nLGND\n")
finally:
    os.chdir(_cwd)
    (mm.load_config, mm.save_config, mm._reset_later,
     mm._apply_update) = _orig

print()
if FAILED:
    print("%d check(s) FAILED: %s" % (len(FAILED), FAILED))
    sys.exit(1)
print("all checks passed")
