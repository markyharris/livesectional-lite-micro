# boot.py - runs on every reset before main.py: join Wi-Fi, sync clock.
# Everything is guarded: a failure here must never stop main.py running.

import json
import time


def _cfg():
    try:
        with open("config.json") as f:
            return json.load(f)
    except Exception:
        return {}


def _connect(cfg):
    ssid = cfg.get("wifi_ssid", "")
    pw = cfg.get("wifi_password", "")
    if not ssid:
        print("[boot] wifi_ssid not set in config.json; skipping join")
        return None
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("[boot] joining Wi-Fi '%s'..." % ssid)
        wlan.connect(ssid, pw)
        for _ in range(40):            # up to ~20 s
            if wlan.isconnected():
                break
            time.sleep(0.5)
    if wlan.isconnected():
        print("[boot] Wi-Fi up:", wlan.ifconfig())
        return wlan
    print("[boot] Wi-Fi join failed; main loop will keep retrying renders")
    return None


def _sync_clock():
    for attempt in range(3):
        try:
            import ntptime
            ntptime.settime()
            print("[boot] clock synced (UTC):", time.gmtime())
            return
        except Exception as e:
            print("[boot] NTP sync failed (%s), retrying" % e)
            time.sleep(2)


_cfg_v = _cfg()
if _connect(_cfg_v):
    _sync_clock()
