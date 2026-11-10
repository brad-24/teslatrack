#!/usr/bin/env python3
"""
TeslaTrack: a private logbook and status page for your Tesla, on a Raspberry Pi.
https://github.com/brad-24/teslatrack  (MIT License)

Listens to Teslemetry's streaming data for the car and keeps, on the Pi:
  car status (locks, doors, windows, Sentry, software), battery level and health,
  temperatures, tyre pressures, every trip with its route, and charging away from home.
Serves a dashboard on the local network, and the few values SolarCharge needs.

Standard library only, written for Python 3.7 or later.

  python3 teslatrack.py                 normal
  python3 teslatrack.py --simulate      made up car and drives, for trying the dashboard
  python3 teslatrack.py --simulate --speed 30   same, with the clock running 30x faster
"""

import argparse
import configparser
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0"
MILE_KM = 1.609344
BAR_PSI = 14.5038

log = logging.getLogger("teslatrack")


# ---------------------------------------------------------------- config

DEFAULTS = {
    "tesla": {
        "vin": "",
        # the same Teslemetry token file SolarCharge uses, readable only by you (chmod 600)
        "token_file": "~/.solarcharge_token",
        "api_base": "https://api.teslemetry.com",
    },
    "home": {
        # Where the car lives. Leave blank to learn it: from SolarCharge (where the car is plugged
        # into the Wall Charger), or from where the car spends the night.
        "latitude": "",
        "longitude": "",
        "radius_m": "250",
    },
    "trips": {
        # a trip ends once the car has been in Park this long (so the lights or a drive through don't split it)
        "park_minutes": "3",
        # ignore moves shorter than this (shuffling the car in the driveway)
        "min_km": "0.3",
        # look up suburb names for where trips start and end (OpenStreetMap Nominatim, free)
        "place_names": "true",
    },
    "tariff": {
        # rough prices for charging away from home, cents per kWh
        "away_dc_c_per_kwh": "60",
        "away_ac_c_per_kwh": "40",
    },
    "solarcharge": {
        # SolarCharge on this Pi, for learning where home is and the rain forecast. Blank to turn off.
        "url": "http://localhost:8080",
    },
    "storage": {
        "data_dir": "~/teslatrack/data",
        "save_minutes": "30",
        "events_max_kb": "512",
        # bring across car history from SolarCharge the first time TeslaTrack starts
        "import_from": "~/solarcharge/data/state.json",
    },
    "web": {
        "bind": "0.0.0.0",
        "port": "8084",
    },
}


class Settings(object):
    def __init__(self, path):
        # strict=False: a setting listed twice (say, a line added twice) uses the last one instead of failing
        cp = configparser.ConfigParser(strict=False)
        cp.read_dict(DEFAULTS)
        if path:
            cp.read(path)
        g = lambda sec, key: cp.get(sec, key).strip()
        gf = lambda sec, key: float(g(sec, key))
        gb = lambda sec, key: cp.getboolean(sec, key)
        ex = lambda p: os.path.expanduser(p) if p else p
        self.vin = g("tesla", "vin")
        self.token_file = ex(g("tesla", "token_file"))
        self.api_base = g("tesla", "api_base").rstrip("/")
        lat, lon = g("home", "latitude"), g("home", "longitude")
        self.home = [float(lat), float(lon)] if lat and lon else None
        self.home_radius = gf("home", "radius_m")
        self.park_s = gf("trips", "park_minutes") * 60
        self.min_km = gf("trips", "min_km")
        self.place_names = gb("trips", "place_names")
        self.away_dc_price = gf("tariff", "away_dc_c_per_kwh") / 100.0
        self.away_ac_price = gf("tariff", "away_ac_c_per_kwh") / 100.0
        self.sc_url = g("solarcharge", "url").rstrip("/")
        self.data_dir = ex(g("storage", "data_dir"))
        self.save_minutes = gf("storage", "save_minutes")
        self.events_max = int(gf("storage", "events_max_kb") * 1024)
        self.import_from = ex(g("storage", "import_from"))
        self.web_bind = g("web", "bind")
        self.web_port = int(gf("web", "port"))


# ---------------------------------------------------------------- Teslemetry stream

def _num(v):
    if isinstance(v, dict):
        for k in ("value", "intValue", "floatValue", "doubleValue", "stringValue"):
            if k in v:
                v = v[k]
                break
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    if isinstance(v, dict):
        v = v.get("boolValue", v.get("value"))
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return None


def _text(v):
    if isinstance(v, dict):
        v = v.get("stringValue", v.get("value"))
    return v.strip() if isinstance(v, str) else None


def _enum(v, prefix):
    t = _text(v)
    return t.replace(prefix, "") if t else None


def parse_fields(data):
    """Turns one Teslemetry stream message into our own field names and units (km, psi, kWh)."""
    upd = {}

    def num(key, name, lo, hi, scale=1.0, digits=1):
        if key in data:
            v = _num(data[key])
            if v is not None and lo <= v <= hi:
                upd[name] = round(v * scale, digits) if digits else int(round(v * scale))

    for key in ("BatteryLevel", "Soc"):
        if key in data and "soc" not in upd:
            num(key, "soc", 0, 100, digits=0)
    num("ChargeLimitSoc", "limit", 1, 100, digits=0)
    num("TimeToFullCharge", "ttf_h", 0, 99, digits=2)
    num("InsideTemp", "cabin", -40, 90)
    num("OutsideTemp", "outside", -40, 70)
    for key in ("RatedRange", "EstBatteryRange"):
        if key in data and "range_km" not in upd:
            num(key, "range_km", 0, 999, MILE_KM, 0)
    num("EnergyRemaining", "energy_kwh", 0, 200, digits=2)
    num("BrickVoltageMax", "brick_max", 1.5, 5, digits=3)
    num("BrickVoltageMin", "brick_min", 1.5, 5, digits=3)
    num("VehicleSpeed", "speed_kmh", 0, 200, MILE_KM, 0)
    num("Odometer", "odo_km", 0, 2000000, MILE_KM, 1)
    num("DCChargingEnergyIn", "dc_in", 0, 500, digits=3)
    num("ACChargingEnergyIn", "ac_in", 0, 500, digits=3)
    for key, name in (("TpmsPressureFl", "tp_fl"), ("TpmsPressureFr", "tp_fr"),
                      ("TpmsPressureRl", "tp_rl"), ("TpmsPressureRr", "tp_rr")):
        num(key, name, 0.5, 6, BAR_PSI, 1)
    if any(k in upd for k in ("tp_fl", "tp_fr", "tp_rl", "tp_rr")):
        upd["tp_t"] = time.time()
    if "Locked" in data:
        v = _bool(data["Locked"])
        if v is not None:
            upd["locked"] = v
    if "DoorState" in data and isinstance(data["DoorState"], dict):
        upd["doors_open"] = sorted(k for k, v in data["DoorState"].items() if _bool(v))
    for key, name in (("FdWindow", "win_fd"), ("FpWindow", "win_fp"), ("RdWindow", "win_rd"), ("RpWindow", "win_rp")):
        if key in data:
            v = _enum(data[key], "WindowState")
            if v:
                upd[name] = v
    if "SentryMode" in data:
        v = _enum(data["SentryMode"], "SentryModeState")
        if v:
            upd["sentry"] = v
    if "Gear" in data:
        v = _enum(data["Gear"], "ShiftState")
        if v in ("P", "R", "N", "D"):
            upd["gear"] = v
    if "FastChargerType" in data:
        v = _text(data["FastChargerType"])
        if v:
            upd["fast_type"] = v
    if "FastChargerPresent" in data:
        v = _bool(data["FastChargerPresent"])
        if v is not None:
            upd["fast_charger"] = v
    if "Location" in data and isinstance(data["Location"], dict):
        lat, lon = _num(data["Location"].get("latitude")), _num(data["Location"].get("longitude"))
        if lat is not None and lon is not None and (lat or lon):
            upd["_loc"] = (lat, lon)
    if "Version" in data:
        v = _text(data["Version"])
        if v:
            upd["version"] = v.split()[0]
    if "SoftwareUpdateVersion" in data:
        v = _text(data["SoftwareUpdateVersion"])
        upd["update_version"] = v.split()[0] if v else ""
    return upd


class Stream(object):
    """Teslemetry's streaming feed (server sent events). Uses no credits and never wakes the car."""

    def __init__(self, s, on_update):
        self.s = s
        self.on_update = on_update
        self.connected = False
        self.last_error = ""
        self.last_msg = 0.0

    def start(self):
        th = threading.Thread(target=self._run, name="stream")
        th.daemon = True
        th.start()

    def _token(self):
        try:
            with open(self.s.token_file) as f:
                return f.read().strip()
        except IOError:
            return ""

    def _run(self):
        backoff = 5
        while True:
            token = self._token()
            if not token or not self.s.vin:
                self.last_error = "Set [tesla] vin and the token file"
                time.sleep(60)
                continue
            try:
                req = Request("%s/sse/%s" % (self.s.api_base, self.s.vin))
                req.add_header("Authorization", "Bearer " + token)
                req.add_header("Accept", "text/event-stream")
                req.add_header("User-Agent", "teslatrack/" + VERSION)
                with urlopen(req, timeout=600) as r:
                    if not self.connected:
                        log.info("Car data stream connected")
                    self.connected = True
                    backoff = 5
                    buf = []
                    for raw in r:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            buf.append(line[5:].strip())
                        elif line == "" and buf:
                            self._handle("".join(buf))
                            buf = []
            except HTTPError as e:
                self.last_error = "HTTP %d" % e.code
            except Exception as e:
                self.last_error = str(e)
            if self.connected:
                log.info("Car data stream disconnected (%s), reconnecting", self.last_error or "closed")
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def _handle(self, payload):
        try:
            ev = json.loads(payload)
        except ValueError:
            return
        data = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(data, dict):
            return
        upd = parse_fields(data)
        if upd:
            self.last_msg = time.time()
            self.on_update(upd)


# ---------------------------------------------------------------- place names

class Places(object):
    """Suburb names for trip ends, from OpenStreetMap's Nominatim. Asked at most once a second,
    remembered on the Pi, and only for where trips start and end."""

    def __init__(self, s, cache):
        self.s = s
        self.cache = cache          # {"lat,lon" to 3 decimals: name}
        self.last = 0.0
        self.lock = threading.Lock()

    def name(self, lat, lon):
        if not self.s.place_names:
            return None
        key = "%.3f,%.3f" % (lat, lon)
        if key in self.cache:
            return self.cache[key]
        with self.lock:
            wait = 1.1 - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                url = "https://nominatim.openstreetmap.org/reverse?" + urlencode(
                    {"format": "jsonv2", "lat": "%.5f" % lat, "lon": "%.5f" % lon, "zoom": 14, "addressdetails": 1})
                req = Request(url, headers={"User-Agent": "teslatrack/%s (personal trip log)" % VERSION})
                with urlopen(req, timeout=15) as r:
                    j = json.loads(r.read().decode("utf-8"))
            except Exception as e:
                log.debug("place name lookup failed: %s", e)
                return None
        a = j.get("address") or {}
        name = (a.get("suburb") or a.get("town") or a.get("village") or a.get("city_district")
                or a.get("city") or a.get("county") or j.get("name"))
        if name:
            self.cache[key] = name
        return name


# ---------------------------------------------------------------- storage

class Storage(object):
    """State is saved to the SD card every half hour; trips and routes when each trip ends."""

    def __init__(self, s):
        self.s = s
        self.dir = s.data_dir
        self.routes_dir = os.path.join(self.dir, "routes")
        self.archive_dir = os.path.join(self.dir, "archive")
        for d in (self.dir, self.routes_dir, self.archive_dir):
            os.makedirs(d, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.trips_path = os.path.join(self.dir, "trips.json")
        self.charging_path = os.path.join(self.dir, "away_charging.json")
        self.events_path = os.path.join(self.dir, "events.log")

    def _load(self, path, empty):
        try:
            with open(path) as f:
                v = json.load(f)
            return v if isinstance(v, type(empty)) else empty
        except (IOError, ValueError):
            return empty

    def _save(self, path, obj):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(obj, f, separators=(",", ":"))
            os.replace(tmp, path)
        except IOError as e:
            log.warning("could not save %s: %s", path, e)

    def load_state(self):
        return self._load(self.state_path, {})

    def save_state(self, st):
        self._save(self.state_path, st)

    def load_trips(self):
        return self._load(self.trips_path, [])

    def save_trips(self, trips):
        self._save(self.trips_path, trips)

    def load_charging(self):
        return self._load(self.charging_path, [])

    def save_charging(self, rows):
        self._save(self.charging_path, rows)

    def save_route(self, tid, points):
        self._save(os.path.join(self.routes_dir, "%d.json" % tid), points)

    def load_route(self, tid):
        return self._load(os.path.join(self.routes_dir, "%d.json" % int(tid)), [])

    DAILY_HEADER = ("date,odometer_km,driven_km,trips,energy_used_kwh,wh_per_km,top_kmh,battery_in_kwh,"
                    "away_charged_kwh,battery_capacity_kwh,cell_spread_mv,tyre_fl_psi,tyre_fr_psi,"
                    "tyre_rl_psi,tyre_rr_psi,home_hours\n")

    def append_daily(self, line):
        path = os.path.join(self.archive_dir, "daily.csv")
        try:
            new = not os.path.exists(path)
            with open(path, "a") as f:
                if new:
                    f.write(self.DAILY_HEADER)
                f.write(line)
        except IOError as e:
            log.warning("could not write %s: %s", path, e)

    def read_file(self, name):
        if name not in ("daily.csv",):
            return None
        try:
            with open(os.path.join(self.archive_dir, name), "rb") as f:
                return f.read()
        except IOError:
            return None


def setup_logging(storage, s):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%d/%m/%Y %H:%M:%S")
    if storage:
        fh = RotatingFileHandler(storage.events_path, maxBytes=s.events_max, backupCount=2)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------- the tracker

def dist_m(a, b):
    """Distance in metres between two (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, h)))


class Tracker(object):
    CAR_KEYS = ("soc", "cabin", "outside", "range_km", "tp_fl", "tp_fr", "tp_rl", "tp_rr", "energy_kwh")
    CAR_KEEP_S = 90 * 86400       # car history is kept for 90 days:
    CAR_FULL_S = 7 * 86400        # every reading for the last week, then
    CAR_BUCKET_S = 1800           # one point per half hour for older readings

    def __init__(self, s, storage, clock=time.time, simulated=False):
        self.s = s
        self.store = storage
        self.clock = clock
        self.simulated = simulated
        self.lock = threading.RLock()
        self.events = deque(maxlen=60)
        st = storage.load_state() if storage else {}
        if not st and storage and not simulated:
            st = self._import_solarcharge()
        self.vehicle = st.get("vehicle") or {}
        self.car_history = deque(st.get("car_history", []), maxlen=20000)
        self.car_compacted = 0.0
        self.tyre_days = st.get("tyre_days") or {}
        self.health = st.get("health") or {}
        self.home_loc = s.home or st.get("home_loc")
        self.night_spots = st.get("night_spots") or []       # candidate home spots, learned overnight
        self.days = st.get("days") or {}
        self.trip = st.get("trip")                            # the trip under way, if any
        self.away = st.get("away")                            # charging away from home, under way
        self.archived_through = st.get("archived_through")
        self.place_cache = st.get("places") or {}
        self.places = Places(s, self.place_cache)
        self.trips = storage.load_trips() if storage else []
        self.charging = storage.load_charging() if storage else []
        self.last_loc = None
        self.solar = {}                                       # what SolarCharge says (plugged in, rain)
        self.today_key = time.strftime("%Y-%m-%d", time.localtime(clock()))
        self.today = self.days.get(self.today_key) or {}
        self.last_save = clock()
        self.last_tick = None

    # ---- events

    def event(self, msg, level=logging.INFO):
        log.log(level, msg)
        self.events.appendleft({"t": self.clock(), "msg": msg})

    # ---- import from SolarCharge (once)

    def _import_solarcharge(self):
        path = self.s.import_from
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path) as f:
                old = json.load(f)
        except (IOError, ValueError):
            return {}
        st = {k: old[k] for k in ("vehicle", "car_history", "tyre_days", "health", "home_loc") if old.get(k)}
        if st:
            log.info("Brought across car history from SolarCharge (%s)", ", ".join(sorted(st)))
        return st

    # ---- updates from the stream

    def vehicle_update(self, upd):
        now = self.clock()
        with self.lock:
            v = dict(self.vehicle)
            loc = upd.pop("_loc", None)
            if loc:
                self._place(now, loc, upd, v)
            self._day_counters(now, upd, v)
            for key in ("ac_in", "dc_in"):
                if upd.get(key) is not None:
                    self._battery_in(now, key, upd[key], v.get(key), upd.get("fast_charger", v.get("fast_charger")),
                                     upd.get("home", v.get("home")))
            if self.away is not None and "supercharger" in str(upd.get("fast_type", v.get("fast_type")) or "").lower():
                self.away["supercharger"] = True
            self._trip_update(now, upd, v, loc)
            v.update(upd)
            v["t"] = time.time()
            if v.get("brick_max") and v.get("brick_min"):
                v["cell_mv"] = int(round((v["brick_max"] - v["brick_min"]) * 1000))
            self.vehicle = v
            self._car_point(now, upd, v)
            self._tyre_point(now, upd)
            self._health_point(now, v, upd)

    def _day_counters(self, now, upd, v):
        t = self.today
        if upd.get("odo_km") is not None:
            if "odo_start" not in t:
                t["odo_start"] = v.get("odo_km") if v.get("odo_km") is not None else upd["odo_km"]
            t["odo_end"] = upd["odo_km"]
        if upd.get("energy_kwh") is not None:
            if "e_start" not in t:
                t["e_start"] = v.get("energy_kwh") if v.get("energy_kwh") is not None else upd["energy_kwh"]
            t["e_end"] = upd["energy_kwh"]
        if upd.get("speed_kmh"):
            t["top_kmh"] = max(t.get("top_kmh", 0), upd["speed_kmh"])

    # ---- home or away

    def _place(self, now, loc, upd, v):
        self.last_loc = (loc[0], loc[1], now)
        if self.solar.get("plugged") and self.solar.get("t", 0) > now - 120:
            # plugged into the Wall Charger at home, according to SolarCharge
            if not self.home_loc or dist_m(self.home_loc, loc) > 50:
                first = not self.home_loc
                self.home_loc = [round(loc[0], 5), round(loc[1], 5)]
                if first:
                    self.event("Learned where home is, from where the car is plugged in (kept on the Pi only)")
        elif not self.home_loc and 1 <= time.localtime(now).tm_hour < 5 and (v.get("gear") in (None, "P")):
            self._night_spot(now, loc)
        if self.home_loc:
            upd["home"] = dist_m(self.home_loc, loc) <= self.s.home_radius

    def _night_spot(self, now, loc):
        """Learns home as the place the car spends the night, once it's been there three nights."""
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        for sp in self.night_spots:
            if dist_m(sp["loc"], loc) < 120:
                if day not in sp["days"]:
                    sp["days"].append(day)
                if len(sp["days"]) >= 3:
                    self.home_loc = [round(sp["loc"][0], 5), round(sp["loc"][1], 5)]
                    self.night_spots = []
                    self.event("Learned where home is, from where the car spends the night (kept on the Pi only)")
                return
        self.night_spots.append({"loc": [round(loc[0], 5), round(loc[1], 5)], "days": [day]})
        self.night_spots = self.night_spots[-10:]

    def _home_time(self, now, dt):
        """Counts daytime hours the car is at home, so SolarCharge can learn which days it's home."""
        hr = time.localtime(now).tm_hour
        if dt <= 0 or dt > 120 or not 9 <= hr < 15:
            return
        home = True if self.solar.get("plugged") else self.vehicle.get("home")
        if home is None:
            return
        self.today["home_known_s"] = self.today.get("home_known_s", 0) + dt
        if home:
            self.today["home_s"] = self.today.get("home_s", 0) + dt

    def home_days(self):
        """Weekdays the car is usually home during the day, from the last 8 weeks, once there's enough data."""
        with self.lock:
            days = dict(self.days)
            days[self.today_key] = dict(self.today)
        seen, home = {}, {}
        for key, t in days.items():
            if t.get("home_known_s", 0) < 3 * 3600:
                continue
            wd = date(*map(int, key.split("-"))).weekday()
            seen[wd] = seen.get(wd, 0) + 1
            if t.get("home_s", 0) >= 4 * 3600:
                home[wd] = home.get(wd, 0) + 1
        known = sorted(wd for wd in seen if seen[wd] >= 2)
        return {"known": known, "home": sorted(wd for wd in known if home.get(wd, 0) * 2 >= seen[wd])}

    # ---- charging away from home

    def _battery_in(self, now, key, val, prev, fast, home):
        if prev is None or val < prev - 0.05:
            delta = val if prev is not None else 0.0    # counter reset: a new charge started
        else:
            delta = max(0.0, val - prev)
        if delta <= 0 or delta > 100:
            return
        self.today["batt_in_kwh"] = self.today.get("batt_in_kwh", 0.0) + delta
        kind = "dc" if key == "dc_in" or fast else "ac"
        if home or (home is None and kind == "ac"):
            return          # home charging is SolarCharge's job
        if self.away is None:
            self.away = {"start": now, "kind": kind, "kwh": 0.0, "last": now,
                         "loc": list(self.last_loc[:2]) if self.last_loc else None}
            self.event("Charging away from home (%s)" % ("fast charger" if kind == "dc" else "AC charger"))
        self.away["kwh"] += delta
        self.away["last"] = now
        if kind == "dc":
            self.away["kind"] = "dc"
        self.today["away_kwh"] = self.today.get("away_kwh", 0.0) + delta

    def _end_away(self, now):
        a, self.away = self.away, None
        if not a or a["kwh"] < 0.1:
            return
        price = self.s.away_dc_price if a["kind"] == "dc" else self.s.away_ac_price
        kwh = round(a["kwh"], 2)
        place = self.places.name(*a["loc"]) if a.get("loc") else None
        sc = bool(a.get("supercharger")) or "supercharger" in (place or "").lower()
        rec = {"start": int(a["start"]), "end": int(a["last"]), "kwh": kwh, "kind": a["kind"],
               "cost": round(kwh * price, 2), "price": price, "place": place, "supercharger": sc}
        with self.lock:
            self.charging.append(rec)
        if self.store:
            self.store.save_charging(self.charging)
        self.event("Away charging: %.1f kWh at a %s%s, about $%.2f" %
                   (kwh, "Supercharger" if sc else "fast charger" if a["kind"] == "dc" else "public AC charger",
                    " in " + place if place else "", rec["cost"]))

    # ---- trips

    def _driving(self, upd, v):
        gear = upd.get("gear", v.get("gear"))
        if gear in ("D", "R", "N"):
            return True
        if gear == "P":
            return False
        return (upd.get("speed_kmh") or 0) > 3

    def _trip_update(self, now, upd, v, loc):
        tr = self.trip
        driving = self._driving(upd, v) if ("gear" in upd or "speed_kmh" in upd) else None
        if tr is None:
            if driving:
                self.trip = tr = {
                    "id": int(now), "start": now, "park_since": None,
                    "start_odo": upd.get("odo_km", v.get("odo_km")), "start_soc": v.get("soc"),
                    "start_e": v.get("energy_kwh"), "start_loc": list(self.last_loc[:2]) if self.last_loc else None,
                    "top": 0, "temps": [], "points": [],
                }
                if tr["start_loc"]:
                    tr["points"].append([round(tr["start_loc"][0], 5), round(tr["start_loc"][1], 5), int(now)])
            return
        if driving is True:
            tr["park_since"] = None
        elif driving is False and not tr.get("park_since"):
            tr["park_since"] = now
        if upd.get("speed_kmh"):
            tr["top"] = max(tr["top"], upd["speed_kmh"])
        if upd.get("outside") is not None:
            tr["temps"].append(upd["outside"])
            tr["temps"] = tr["temps"][-200:]
        if loc:
            if not tr.get("start_loc"):
                tr["start_loc"] = [loc[0], loc[1]]
            pts = tr["points"]
            if not pts or dist_m(pts[-1][:2], loc) > 15:
                pts.append([round(loc[0], 5), round(loc[1], 5), int(now)])
        if tr.get("start_odo") is None and upd.get("odo_km") is not None:
            tr["start_odo"] = upd["odo_km"]
        if tr.get("start_e") is None and upd.get("energy_kwh") is not None:
            tr["start_e"] = upd["energy_kwh"]
        if tr.get("start_soc") is None and upd.get("soc") is not None:
            tr["start_soc"] = upd["soc"]

    def _end_trip(self, now):
        tr, self.trip = self.trip, None
        if not tr:
            return
        v = self.vehicle
        end = tr.get("park_since") or now
        pts = tr.get("points") or []
        route_km = sum(dist_m(pts[i][:2], pts[i + 1][:2]) for i in range(len(pts) - 1)) / 1000.0
        km = None
        if tr.get("start_odo") is not None and v.get("odo_km") is not None:
            km = v["odo_km"] - tr["start_odo"]
        if km is None or km < 0:
            km = route_km
        if km < self.s.min_km:
            return
        kwh = None
        if tr.get("start_e") is not None and v.get("energy_kwh") is not None:
            kwh = tr["start_e"] - v["energy_kwh"]
            if kwh <= 0:
                kwh = None
        mins = max(1.0, (end - tr["start"]) / 60.0)
        start_loc = tr.get("start_loc") or (pts[0][:2] if pts else None)
        end_loc = list(self.last_loc[:2]) if self.last_loc else (pts[-1][:2] if pts else None)
        temps = tr.get("temps") or []
        rec = {
            "id": tr["id"], "start": int(tr["start"]), "end": int(end), "km": round(km, 1),
            "minutes": int(round(mins)), "kwh": round(kwh, 2) if kwh else None,
            "wh_km": int(round(kwh * 1000.0 / km)) if kwh and km >= 1 else None,
            "avg_kmh": int(round(km / (mins / 60.0))), "top_kmh": tr.get("top") or None,
            "soc_start": tr.get("start_soc"), "soc_end": v.get("soc"),
            "outside": round(sum(temps) / len(temps), 1) if temps else v.get("outside"),
            "start_loc": [round(x, 5) for x in start_loc] if start_loc else None,
            "end_loc": [round(x, 5) for x in end_loc] if end_loc else None,
            "start_place": None, "end_place": None, "points": len(pts),
        }
        for which, loc in (("start", start_loc), ("end", end_loc)):
            if loc and self.home_loc and dist_m(self.home_loc, loc) <= self.s.home_radius:
                rec[which + "_place"] = "Home"
        with self.lock:
            self.trips.append(rec)
            self.today["trips"] = self.today.get("trips", 0) + 1
        if self.store:
            self.store.save_route(rec["id"], pts)
            self.store.save_trips(self.trips)
        self.event("Trip: %.1f km in %d min%s" % (rec["km"], rec["minutes"],
                                                  ", %d Wh/km" % rec["wh_km"] if rec["wh_km"] else ""))
        # suburb names can take a few seconds to look up, so do it in the background
        th = threading.Thread(target=self._name_places, args=(rec,), name="places")
        th.daemon = True
        th.start()

    def _name_places(self, rec):
        changed = False
        for which in ("start", "end"):
            if rec.get(which + "_place") or not rec.get(which + "_loc"):
                continue
            name = self.places.name(*rec[which + "_loc"])
            if name:
                rec[which + "_place"] = name
                changed = True
        if changed and self.store:
            with self.lock:
                self.store.save_trips(self.trips)

    # ---- history for the charts

    def _car_point(self, now, upd, v):
        vals = [upd.get(k) for k in self.CAR_KEYS]
        if all(x is None for x in vals):
            return
        h = self.car_history
        n = 1 + len(self.CAR_KEYS)
        last = (list(h[-1]) + [None] * n)[:n] if h else None
        if last and now - last[0] < 120:
            h[-1] = [last[0]] + [x if x is not None else o for x, o in zip(vals, last[1:])]
            return
        if last and now - last[0] < 600 and all(x is None or x == o for x, o in zip(vals, last[1:])):
            return
        h.append([now] + vals)
        while h and now - h[0][0] > self.CAR_KEEP_S:
            h.popleft()
        if now - self.car_compacted > 6 * 3600:
            self._compact_car(now)

    def _compact_car(self, now):
        self.car_compacted = now
        cut = now - self.CAR_FULL_S
        h = self.car_history
        if not h or h[0][0] >= cut:
            return
        n = 1 + len(self.CAR_KEYS)
        out, cur, key = [], None, None
        for p in h:
            if p[0] >= cut:
                break
            p = (list(p) + [None] * n)[:n]
            b = int(p[0] // self.CAR_BUCKET_S)
            if b != key:
                if cur:
                    out.append(cur)
                cur, key = p, b
            else:
                cur = [p[0]] + [x if x is not None else o for x, o in zip(p[1:], cur[1:])]
        if cur:
            out.append(cur)
        recent = [p for p in h if p[0] >= cut]
        h.clear()
        h.extend(out + recent)

    def _tyre_point(self, now, upd):
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        for w in ("fl", "fr", "rl", "rr"):
            if upd.get("tp_" + w) is not None:
                acc = self.tyre_days.setdefault(day, {}).setdefault(w, [0.0, 0])
                acc[0] += upd["tp_" + w]
                acc[1] += 1
        for old in sorted(self.tyre_days)[:-90]:
            del self.tyre_days[old]

    def _health_point(self, now, v, upd):
        """Usable capacity once a day, from a reading at 95% or more."""
        if upd.get("energy_kwh") is None and upd.get("soc") is None:
            return
        soc, e = v.get("soc"), v.get("energy_kwh")
        if soc is None or e is None or soc < 95:
            return
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        cur = self.health.get(day)
        if cur and cur.get("soc", 0) > soc:
            return
        self.health[day] = {"cap": round(e / (soc / 100.0), 2), "soc": soc, "cell_mv": v.get("cell_mv")}
        for old in sorted(self.health)[:-1100]:
            del self.health[old]

    # ---- the loop: day roll over, trip and away endings, saving

    def tick(self):
        now = self.clock()
        dt = now - self.last_tick if self.last_tick else 0
        self.last_tick = now
        with self.lock:
            key = time.strftime("%Y-%m-%d", time.localtime(now))
            if key != self.today_key:
                self.days[self.today_key] = self.today
                for old in sorted(self.days)[:-60]:
                    del self.days[old]
                self.today_key, self.today = key, {}
                v = self.vehicle
                if v.get("odo_km") is not None:
                    self.today["odo_start"] = v["odo_km"]
                if v.get("energy_kwh") is not None:
                    self.today["e_start"] = v["energy_kwh"]
            self._home_time(now, dt)
            trip_done = self.trip and self.trip.get("park_since") and now - self.trip["park_since"] >= self.s.park_s
            away_done = self.away and now - self.away["last"] > 900
        if trip_done:
            self._end_trip(now)
        if away_done:
            self._end_away(now)
        yday = (datetime.fromtimestamp(now).date() - timedelta(days=1)).isoformat()
        if self.store and self.archived_through != yday:
            self._archive(now, yday)
        if self.store and now - self.last_save >= self.s.save_minutes * 60:
            self.save(now)

    def _archive(self, now, yday):
        with self.lock:
            days = dict(self.days)
        start = date(*map(int, self.archived_through.split("-"))) + timedelta(days=1) if self.archived_through \
            else date(*map(int, yday.split("-")))
        d = start
        while d.isoformat() <= yday:
            k = d.isoformat()
            t = days.get(k)
            if t:
                self.store.append_daily(self._daily_line(k, t))
            d += timedelta(days=1)
        self.archived_through = yday
        self.save(now)

    def _daily_line(self, key, t):
        km = (t["odo_end"] - t["odo_start"]) if t.get("odo_end") is not None and t.get("odo_start") is not None else None
        used = (t["e_start"] + t.get("batt_in_kwh", 0.0) - t["e_end"]) \
            if t.get("e_start") is not None and t.get("e_end") is not None else None
        whkm = int(round(used * 1000.0 / km)) if km and km >= 5 and used and used > 0 else None
        hp = self.health.get(key) or {}
        tp = self.tyre_days.get(key) or {}
        tyres = ["%.1f" % (tp[w][0] / tp[w][1]) if tp.get(w) and tp[w][1] else "" for w in ("fl", "fr", "rl", "rr")]
        f = lambda x, fmt="%.1f": "" if x is None else fmt % x
        return ",".join([key, f(t.get("odo_end")), f(km), str(t.get("trips", 0)), f(used, "%.2f"), f(whkm, "%d"),
                         f(t.get("top_kmh"), "%d"), f(t.get("batt_in_kwh"), "%.2f"), f(t.get("away_kwh"), "%.2f"),
                         f(hp.get("cap"), "%.2f"), f(hp.get("cell_mv"), "%d")] + tyres +
                        [f(t["home_s"] / 3600.0 if t.get("home_s") else None)]) + "\n"

    def save(self, now=None):
        self.last_save = now or self.clock()
        if not self.store:
            return
        with self.lock:
            days = dict(self.days)
            days[self.today_key] = self.today
            st = {"vehicle": self.vehicle, "car_history": list(self.car_history), "tyre_days": self.tyre_days,
                  "health": self.health, "home_loc": self.home_loc, "night_spots": self.night_spots,
                  "days": days, "trip": self.trip, "away": self.away, "archived_through": self.archived_through,
                  "places": self.place_cache, "saved": time.time()}
        self.store.save_state(st)

    # ---- what the pages and SolarCharge ask for

    def snapshot(self, stream):
        with self.lock:
            v = dict(self.vehicle)
            tr = self.trip
            days = dict(self.days)
            days[self.today_key] = dict(self.today)
            return {
                "time": self.clock(), "version": VERSION, "simulated": self.simulated,
                "vehicle": v, "home_known": bool(self.home_loc),
                "stream": {"connected": stream.connected if stream else self.simulated,
                           "error": stream.last_error if stream else ""},
                "trip": {"start": tr["start"], "km_so_far": (v.get("odo_km") - tr["start_odo"])
                         if tr.get("start_odo") is not None and v.get("odo_km") is not None else None} if tr else None,
                "away": dict(self.away) if self.away else None,
                "today": days[self.today_key],
                "solarcharge": {"rain12": self.solar.get("rain12")},
                "events": list(self.events)[:25],
            }

    def vehicle_for_solarcharge(self):
        with self.lock:
            v = self.vehicle
            out = {k: v.get(k) for k in ("soc", "limit", "ttf_h", "range_km", "energy_kwh", "ac_in", "home", "t")}
        out["home_days"] = self.home_days()
        return out

    def get_car_history(self, hours):
        with self.lock:
            h = list(self.car_history)
        if hours > 0:
            cut = self.clock() - hours * 3600
            h = [p for p in h if p[0] >= cut]
        return h

    def get_tyres(self, days):
        with self.lock:
            td = {k: dict(v) for k, v in self.tyre_days.items()}
        out = []
        for day in sorted(td)[-days:]:
            row = [day]
            for w in ("fl", "fr", "rl", "rr"):
                acc = td[day].get(w)
                row.append(round(acc[0] / acc[1], 1) if acc and acc[1] else None)
            out.append(row)
        return out

    def get_health(self):
        with self.lock:
            h = dict(self.health)
        return [[d, h[d].get("cap"), h[d].get("cell_mv"), h[d].get("soc")] for d in sorted(h)]

    def get_days(self):
        with self.lock:
            out = dict(self.days)
            out[self.today_key] = dict(self.today)
        return out

    def get_odometer(self):
        """Odometer at the end of each day (archive plus recent days) and now, for distance over any period."""
        out = {}
        if self.store:
            try:
                with open(os.path.join(self.store.archive_dir, "daily.csv")) as f:
                    f.readline()
                    for line in f:
                        c = line.split(",")
                        if len(c) > 1 and c[1]:
                            try:
                                out[c[0]] = float(c[1])
                            except ValueError:
                                pass
            except IOError:
                pass
        with self.lock:
            for k, t in self.days.items():
                if t.get("odo_end") is not None:
                    out[k] = t["odo_end"]
            now_odo = self.vehicle.get("odo_km")
            if self.today.get("odo_end") is not None:
                out[self.today_key] = self.today["odo_end"]
            trips = [{"start": t["start"], "end": t.get("end"), "km": t.get("km"), "kwh": t.get("kwh"),
                      "minutes": t.get("minutes"), "from": t.get("start_place"), "to": t.get("end_place"),
                      "work": bool(t.get("work"))} for t in self.trips if t.get("km")]
        return {"now": now_odo, "days": {k: round(v, 1) for k, v in out.items()}, "trips": trips}

    def get_trips(self, days):
        cut = self.clock() - days * 86400 if days > 0 else 0
        with self.lock:
            return [dict(t) for t in self.trips if t["start"] >= cut]

    def set_work(self, tid, work):
        """Mark a trip as a work trip (or not). Saved straight away, since it's something you entered."""
        with self.lock:
            rec = next((t for t in self.trips if t["id"] == tid), None)
            if rec is None:
                return False
            rec["work"] = bool(work)
            trips = list(self.trips)
        if self.store:
            self.store.save_trips(trips)
        return True

    def get_trip(self, tid):
        with self.lock:
            rec = next((dict(t) for t in self.trips if t["id"] == tid), None)
        if not rec:
            return None
        rec["route"] = self.store.load_route(tid) if self.store else []
        return rec

    def get_charging(self):
        with self.lock:
            return list(self.charging)

    # ---- SolarCharge, for whether the car's plugged in at home and the rain forecast

    def poll_solarcharge(self):
        if not self.s.sc_url:
            return
        while True:
            try:
                with urlopen(self.s.sc_url + "/api/status", timeout=5) as r:
                    st = json.loads(r.read().decode("utf-8"))
                ch = st.get("charger") or {}
                self.solar["plugged"] = bool(ch.get("plugged"))
                self.solar["t"] = self.clock()
            except Exception:
                self.solar["plugged"] = False
            try:
                with urlopen(self.s.sc_url + "/api/weather", timeout=5) as r:
                    w = json.loads(r.read().decode("utf-8"))
                if w.get("ok"):
                    self.solar["rain12"] = max([h.get("rain") or 0 for h in w.get("hours", [])] + [0])
            except Exception:
                pass
            time.sleep(30)


# ---------------------------------------------------------------- web

class Handler(BaseHTTPRequestHandler):
    tracker = None
    stream = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype, code=200, cache="no-store", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

    def _page(self, name):
        try:
            with open(os.path.join(BASE_DIR, name), "rb") as f:
                self._send(f.read(), "text/html; charset=utf-8", cache="no-cache")
        except IOError:
            self.send_error(500, name + " missing")

    def do_GET(self):
        u = urlparse(self.path)
        t = self.tracker
        q = parse_qs(u.query)
        qi = lambda k, d: int(q.get(k, [d])[0]) if q.get(k, [""])[0].lstrip("-").isdigit() else d
        if u.path in ("/", "/index.html"):
            self._page("car.html")
        elif u.path == "/trips":
            self._page("trips.html")
        elif u.path.startswith("/vendor/leaflet/"):
            # the map library, kept on the Pi so the map works without outside websites
            name = u.path[len("/vendor/leaflet/"):]
            types = {".js": "application/javascript", ".css": "text/css", ".png": "image/png"}
            ext = os.path.splitext(name)[1]
            if ext not in types or ".." in name:
                self.send_error(404)
                return
            try:
                with open(os.path.join(BASE_DIR, "vendor", "leaflet", name), "rb") as f:
                    self._send(f.read(), types[ext], cache="max-age=604800")
            except IOError:
                self.send_error(404)
        elif u.path in ("/img/icon-180.png", "/favicon.ico"):
            try:
                with open(os.path.join(BASE_DIR, "img", "icon-180.png"), "rb") as f:
                    self._send(f.read(), "image/png", cache="max-age=86400")
            except IOError:
                self.send_error(404)
        elif u.path == "/api/status":
            self._json(t.snapshot(self.stream))
        elif u.path == "/api/vehicle":
            self._json(t.vehicle_for_solarcharge())
        elif u.path == "/api/carhistory":
            self._json(t.get_car_history(qi("hours", 168)))
        elif u.path == "/api/tyres":
            self._json(t.get_tyres(qi("days", 90)))
        elif u.path == "/api/health":
            self._json(t.get_health())
        elif u.path == "/api/days":
            self._json(t.get_days())
        elif u.path == "/api/odometer":
            self._json(t.get_odometer())
        elif u.path == "/api/trips":
            self._json(t.get_trips(qi("days", 0)))
        elif u.path.startswith("/api/trip/"):
            try:
                rec = t.get_trip(int(u.path.rsplit("/", 1)[1]))
            except ValueError:
                rec = None
            if rec is None:
                self.send_error(404)
            else:
                self._json(rec)
        elif u.path == "/api/charging":
            self._json(t.get_charging())
        elif u.path == "/export/daily.csv":
            body = t.store.read_file("daily.csv") if t.store else None
            if body is None:
                self.send_error(404)
            else:
                self._send(body, "text/csv; charset=utf-8",
                           extra={"Content-Disposition": 'attachment; filename="teslatrack-daily.csv"'})
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        parts = u.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "trip"] and parts[3] == "work":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 1000)) or b"{}")
                ok = self.tracker.set_work(int(parts[2]), body.get("work"))
            except (ValueError, AttributeError):
                self.send_error(400)
                return
            if ok:
                self._json({"ok": True})
            else:
                self.send_error(404)
        else:
            self.send_error(404)


def start_web(tracker, stream, s):
    Handler.tracker = tracker
    Handler.stream = stream
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer((s.web_bind, s.web_port), Handler)
    except OSError as e:
        log.error("Cannot open the dashboard on port %d (%s). Is TeslaTrack already running? "
                  "Try: sudo systemctl stop teslatrack", s.web_port, e)
        raise SystemExit(1)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, name="web")
    th.daemon = True
    th.start()
    return srv


# ---------------------------------------------------------------- simulation

class SimClock(object):
    def __init__(self, speed):
        self.speed = speed
        lt = time.localtime()
        self.t0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 7, 30, 0, 0, 0, -1))
        self.real0 = time.time()

    def __call__(self):
        return self.t0 + (time.time() - self.real0) * self.speed


def simulate(tracker, clock):
    """A made up car: overnight at home, a school run, a drive into town and back, charging, a week of history."""
    home = (-27.6012, 151.9431)
    tracker.home_loc = list(home)
    # a year of battery health and a month of tyres, so the charts have something to show
    for i in range(365, 0, -3):
        day = time.strftime("%Y-%m-%d", time.localtime(clock() - i * 86400))
        tracker.health[day] = {"cap": round(58.9 - (365 - i) * 0.0035 + random.uniform(-0.25, 0.25), 2), "soc": 100,
                               "cell_mv": random.randint(12, 26)}
    for i in range(30, 0, -1):
        day = time.strftime("%Y-%m-%d", time.localtime(clock() - i * 86400))
        if random.random() < 0.8:
            base = 41.5 + random.uniform(-0.4, 0.4)
            tracker.tyre_days[day] = {"fl": [base + 0.3, 1], "fr": [base, 1], "rl": [base + 0.1, 1],
                                      "rr": [base - (30 - i) * 0.12, 1]}
    # some past trips
    routes = [
        [home, (-27.5930, 151.9500), (-27.5800, 151.9550), (-27.5650, 151.9530), (-27.5606, 151.9507)],
        [home, (-27.6050, 151.9300), (-27.6200, 151.9100), (-27.6370, 151.8960)],
    ]
    names = ["Toowoomba City", "Westbrook"]
    t = clock() - 6 * 86400
    for day in range(6):
        for ri, r in enumerate(routes):
            start = t + day * 86400 + (8 + ri * 7) * 3600
            for leg in (r, list(reversed(r))):
                pts, tt = [], start
                for a, b in zip(leg, leg[1:]):
                    for k in range(6):
                        f = k / 6.0
                        pts.append([round(a[0] + (b[0] - a[0]) * f, 5), round(a[1] + (b[1] - a[1]) * f, 5), int(tt)])
                        tt += 40
                pts.append([leg[-1][0], leg[-1][1], int(tt)])
                km = sum(dist_m(pts[i][:2], pts[i + 1][:2]) for i in range(len(pts) - 1)) / 1000.0
                kwh = km * random.uniform(0.13, 0.19)
                rec = {"id": int(start), "start": int(start), "end": int(tt), "km": round(km, 1),
                       "minutes": int((tt - start) / 60), "kwh": round(kwh, 2), "wh_km": int(kwh * 1000 / km),
                       "avg_kmh": int(km / ((tt - start) / 3600.0)), "top_kmh": random.randint(60, 100),
                       "soc_start": 80, "soc_end": 78, "outside": round(random.uniform(16, 28), 1),
                       "start_loc": list(leg[0]), "end_loc": list(leg[-1]),
                       "start_place": "Home" if leg[0] == home else names[ri],
                       "end_place": "Home" if leg[-1] == home else names[ri], "points": len(pts)}
                tracker.trips.append(rec)
                tracker._sim_routes[rec["id"]] = pts
                start = tt + 2 * 3600
    tracker.charging.append({"start": int(clock() - 3 * 86400), "end": int(clock() - 3 * 86400 + 1800), "kwh": 24.6,
                             "kind": "dc", "cost": 14.76, "price": 0.6, "place": "Gatton", "supercharger": True})

    def feed():
        odo, soc, ac = 12480.0, 64.0, 0.0
        upd = {"version": "2026.38.6", "update_version": "2026.44.1", "locked": True, "doors_open": [],
               "win_fd": "Closed", "win_fp": "Closed", "win_rd": "Closed", "win_rp": "Closed", "sentry": "Off",
               "limit": 90, "brick_max": 3.342, "brick_min": 3.326, "gear": "P", "_loc": home}
        tracker.vehicle_update(upd)
        drive = routes[0] + list(reversed(routes[0]))
        while True:
            lt = time.localtime(clock())
            hr = lt.tm_hour + lt.tm_min / 60.0
            cabin = 21 + 17 * max(0.0, math.sin((hr - 8) / 12.0 * math.pi)) + random.uniform(-0.4, 0.4)
            outside = 17 + 11 * max(0.0, math.sin((hr - 7) / 13.0 * math.pi))
            warm = (outside - 17) * 0.12
            u = {"cabin": round(cabin, 1), "outside": round(outside, 1), "soc": int(soc),
                 "energy_kwh": round(soc / 100.0 * 57.6, 2), "range_km": int(soc * 4.3), "odo_km": round(odo, 1),
                 "tp_fl": round(41.2 + warm, 1), "tp_fr": round(40.9 + warm, 1), "tp_rl": round(41.0 + warm, 1),
                 "tp_rr": round(38.6 + warm, 1)}
            on_road = 8.0 <= hr < 8.5
            if on_road:
                f = (hr - 8.0) / 0.5 * (len(drive) - 1)
                i = min(int(f), len(drive) - 2)
                a, b = drive[i], drive[i + 1]
                loc = (a[0] + (b[0] - a[0]) * (f - i), a[1] + (b[1] - a[1]) * (f - i))
                u.update({"gear": "D", "speed_kmh": random.randint(40, 80), "_loc": loc, "locked": True})
                odo += 0.02 * clock.speed
                soc -= 0.004 * clock.speed
            else:
                u.update({"gear": "P", "speed_kmh": 0, "_loc": home})
                if 10 <= hr < 14 and soc < 90:
                    soc += 0.003 * clock.speed
                    ac += 0.003 * clock.speed * 0.576
                    u["ac_in"] = round(ac, 3)
            tracker.vehicle_update(u)
            time.sleep(2)

    th = threading.Thread(target=feed, name="simfeed")
    th.daemon = True
    th.start()


# ---------------------------------------------------------------- main

class Shutdown(BaseException):
    pass


def main():
    ap = argparse.ArgumentParser(description="TeslaTrack")
    ap.add_argument("--config", default=os.path.join(BASE_DIR, "config.ini"))
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--speed", type=float, default=1.0)
    args = ap.parse_args()
    s = Settings(args.config)

    if args.simulate:
        clock = SimClock(args.speed)
        s.data_dir = "/dev/shm/teslatrack-sim"
        s.sc_url = ""
        storage = Storage(s)
        for f in ("state.json", "trips.json", "away_charging.json"):
            try:
                os.remove(os.path.join(s.data_dir, f))
            except OSError:
                pass
        setup_logging(None, s)
        tracker = Tracker(s, storage, clock=clock, simulated=True)
        tracker._sim_routes = {}
        storage.load_route = lambda tid: tracker._sim_routes.get(int(tid)) or Storage.load_route(storage, tid)
        simulate(tracker, clock)
        stream = None
    else:
        storage = Storage(s)
        setup_logging(storage, s)
        tracker = Tracker(s, storage)
        stream = Stream(s, tracker.vehicle_update)
        stream.start()
        th = threading.Thread(target=tracker.poll_solarcharge, name="solarcharge")
        th.daemon = True
        th.start()

    log.info("TeslaTrack %s starting%s", VERSION, " (simulation)" if args.simulate else "")
    log.info("Dashboard on http://<pi address>:%d", s.web_port)
    start_web(tracker, stream, s)

    def stop(*_):
        raise Shutdown()
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            tracker.tick()
            time.sleep(10)
    except (Shutdown, KeyboardInterrupt):
        pass
    finally:
        tracker.save()
        log.info("TeslaTrack stopped")


if __name__ == "__main__":
    main()
