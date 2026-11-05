# test_provisioning.py - desktop (CPython) checks for the Wi-Fi
# provisioning pieces of metar_micro.py that are pure Python: the
# scan-list cleanup, the DNS catch-all reply builder, and the setup
# form's validation + config write. The board-side parts (AP mode,
# the BOOT sample, the async servers) are hardware behaviour,
# exercised on the bench, not here. metar_micro imports cleanly under
# CPython as-is (board-only modules are imported inside functions),
# so no stubbing is needed.
#
# Run from the project root:  python3 test_provisioning.py
# Exits 0 when every check passes, 1 otherwise.

import sys

import metar_micro as mm

FAILED = []


def check(name, cond):
    print("%s %s" % ("PASS" if cond else "FAIL", name))
    if not cond:
        FAILED.append(name)


def raises_valueerror(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


# ---------------------------------------------------------------------------
# Scan list: dedupe by SSID (strongest wins), drop hidden, sort by RSSI
# ---------------------------------------------------------------------------
def _entry(ssid, rssi):
    # wlan.scan() tuple: (ssid, bssid, channel, rssi, authmode, hidden)
    return (ssid, b"\x00\x01\x02\x03\x04\x05", 6, rssi, 3, 0)


raw = [
    _entry(b"HomeNet", -70),
    _entry(b"CoffeeShop", -40),
    _entry(b"HomeNet", -55),        # same SSID again, stronger radio
    _entry(b"", -30),               # hidden network: dropped
    _entry(b"   ", -20),            # whitespace-only name: dropped
    _entry(b"  Padded  ", -60),     # name is trimmed
    _entry(b"\xff\xfe\xfd", -10),   # undecodable name: dropped
    (b"Broken",),                   # truncated tuple: dropped
    _entry(b"Weak", -90),
]
nets = mm._prov_clean_scan(raw)
names = [ssid for ssid, _rssi in nets]
check("scan sorts strongest first",
      names == ["CoffeeShop", "HomeNet", "Padded", "Weak"])
check("scan dedupe keeps the strongest RSSI",
      dict(nets).get("HomeNet") == -55)
check("scan of nothing is an empty list",
      mm._prov_clean_scan([]) == [] and mm._prov_clean_scan(None) == [])

# ---------------------------------------------------------------------------
# DNS reply builder: well-formed query -> A answer of 192.168.4.1;
# anything malformed -> None (the caller drops it, never raises)
# ---------------------------------------------------------------------------
def _query(name, qid=b"\x12\x34"):
    out = bytearray(qid)
    out += b"\x01\x00"                      # flags: RD set
    out += b"\x00\x01"                      # QDCOUNT = 1
    out += b"\x00\x00\x00\x00\x00\x00"      # ANCOUNT/NSCOUNT/ARCOUNT
    for label in name.split("."):
        out.append(len(label))
        out += label.encode()
    out.append(0)
    out += b"\x00\x01\x00\x01"              # QTYPE A, QCLASS IN
    return bytes(out)


AP_BYTES = bytes([192, 168, 4, 1])
for name in ("connectivitycheck.gstatic.com", "a.b", "x"):
    q = _query(name)
    r = mm._dns_reply(q)
    check("DNS answers %s" % name,
          r is not None
          and r[0:2] == b"\x12\x34"              # transaction ID echoed
          and r[2] & 0x80                        # QR: it is a response
          and r[6:8] == b"\x00\x01"              # ANCOUNT = 1
          and r[12:len(q)] == q[12:]             # question echoed whole
          and r[-4:] == AP_BYTES)                # answer is the AP IP

check("DNS drops an empty packet", mm._dns_reply(b"") is None)
check("DNS drops a short packet", mm._dns_reply(b"\x00" * 11) is None)
check("DNS drops a zero-question header",
      mm._dns_reply(b"\x12\x34\x01\x00\x00\x00"
                    b"\x00\x00\x00\x00\x00\x00") is None)
check("DNS drops a truncated question",
      mm._dns_reply(_query("example.com")[:15]) is None)
_ptr = bytearray(_query("example.com"))
_ptr[12] = 0xC0                               # compression pointer up front
check("DNS drops a pointer in the question",
      mm._dns_reply(bytes(_ptr)) is None)
check("DNS drops random garbage",
      mm._dns_reply(bytes(range(256))) is None)

# ---------------------------------------------------------------------------
# /save validation + config write
# ---------------------------------------------------------------------------
check("validate takes the picked SSID",
      mm._prov_validate({"ssid": "HomeNet", "wifi_password": "pw"})
      == ("HomeNet", "pw"))
check("validate: manual entry wins over the pick",
      mm._prov_validate({"ssid": "Picked", "ssid_manual": "Typed",
                         "wifi_password": ""}) == ("Typed", ""))
check("validate: blank manual falls back to the pick",
      mm._prov_validate({"ssid": "Picked", "ssid_manual": "  "})
      == ("Picked", ""))
check("validate: no SSID at all is rejected",
      raises_valueerror(lambda: mm._prov_validate({"wifi_password": "x"})))
check("validate: SSID over 32 chars is rejected",
      raises_valueerror(lambda: mm._prov_validate({"ssid": "n" * 33})))
check("validate: password over 64 chars is rejected",
      raises_valueerror(lambda: mm._prov_validate(
          {"ssid": "Ok", "wifi_password": "p" * 65})))
check("validate: 32/64-char values pass",
      mm._prov_validate({"ssid": "n" * 32, "wifi_password": "p" * 64})
      == ("n" * 32, "p" * 64))

_saved = {}
_orig_load, _orig_save = mm.load_config, mm.save_config
mm.load_config = lambda: dict(mm.DEFAULT_CONFIG, map_name="Porch",
                              brightness=42)
mm.save_config = lambda cfg: _saved.update(cfg)
try:
    mm._prov_apply("NewNet", "s3cret")
finally:
    mm.load_config, mm.save_config = _orig_load, _orig_save
check("apply writes the two Wi-Fi fields",
      _saved.get("wifi_ssid") == "NewNet"
      and _saved.get("wifi_password") == "s3cret")
check("apply leaves every other setting alone",
      _saved.get("map_name") == "Porch"
      and _saved.get("brightness") == 42
      and set(_saved) == set(mm.DEFAULT_CONFIG))

# ---------------------------------------------------------------------------
# Page rendering + probe list sanity
# ---------------------------------------------------------------------------
page = mm._prov_page({"map_name": "Porch <Map>"},
                     [("Home&Net", -50), ("Cafe", -70)])
check("setup page titles itself LS Lite Micro WiFi Setup",
      "LS Lite Micro" in page and "WiFi Setup" in page)
check("setup page escapes the map name and SSIDs",
      "Porch &lt;Map&gt;" in page and "Home&amp;Net" in page
      and 'value="Cafe"' in page)
check("setup page offers manual entry, password and rescan",
      'name="ssid_manual"' in page and 'name="wifi_password"' in page
      and 'href="/rescan"' in page)
check("setup page offers a show-password peek toggle",
      'id="show_pw"' in page and "Show password" in page)
check("captive-portal probes are covered",
      "/generate_204" in mm._PROV_PROBES
      and "/hotspot-detect.html" in mm._PROV_PROBES
      and "/ncsi.txt" in mm._PROV_PROBES
      and "/connecttest.txt" in mm._PROV_PROBES)
check("setup AP constants are the spec values",
      mm.PROV_AP_SSID == "LSLite-Micro-Setup"
      and mm.PROV_AP_IP == "192.168.4.1")

print()
if FAILED:
    print("%d check(s) FAILED" % len(FAILED))
    sys.exit(1)
print("all checks passed")
