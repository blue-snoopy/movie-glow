#!/usr/bin/env python3
"""movie-glow: free "Ambilight" for Plex + Philips Hue.

While a movie plays in Plex, movie-glow reads the frame at the current playback position
straight from the movie file (ffmpeg, a tiny 32x18 image, HDR tone-mapped), averages zones
around the edge of the picture and sends them to a Hue gradient lightstrip behind the TV
(and, optionally, matching colours to nearby lamps). No capture hardware, no HDMI box.

Configuration: config.toml (see config.example.toml). Secrets come from the environment:
  PLEX_TOKEN   Plex token (an account token from Plex Web is fine)
  HUE_KEY      Hue application key (create one with:  movie_glow.py --pair <bridge-ip>)
  HA_TOKEN     optional, Home Assistant long-lived token

Usage:
  movie_glow.py                      run (reads $MOVIE_GLOW_CONFIG or ./config.toml)
  movie_glow.py --pair <bridge-ip>   create a Hue application key (press the bridge button)
  movie_glow.py --lights             list Hue lights (gradient strips marked)
  movie_glow.py --once FILE SECONDS [--hdr]   print the colours for one frame, lights untouched
"""
import colorsys, datetime, json, math, os, signal, ssl, subprocess, sys, time, tomllib, urllib.request

W, H = 32, 18
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE        # Hue bridges use a self-signed certificate


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def http(url, headers=None, data=None, method=None, timeout=8, insecure=False):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, body, h, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL if insecure else None) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------- config

DEFAULTS = {
    "plex": {"url": "http://127.0.0.1:32400", "player": "", "path_map": []},
    "hue": {"host": "", "strip": "", "reverse": False, "lamps": []},
    "sync": {"interval": 1.5, "lead": 1.0, "fade_ms": 1200, "lamp_fade_ms": 2500,
             "brightness_min": 25.0, "brightness_max": 80.0, "saturation": 1.35,
             "lamp_factor": 0.35, "ambient_sensitivity": 100},
    "after": {"mode": "fixed", "brightness": 75, "kelvin": 4000},
    "home_assistant": {"url": "", "switch": "", "media_player": "", "lamps_yield_on_pause_to": "",
                       "ambient_sensitivity_entity": ""},
}


def load_config():
    path = os.environ.get("MOVIE_GLOW_CONFIG", "config.toml")
    with open(path, "rb") as f:
        user = tomllib.load(f)
    cfg = {k: {**v, **user.get(k, {})} for k, v in DEFAULTS.items()}
    cfg["hue"]["host"] = os.environ.get("HUE_HOST") or cfg["hue"]["host"]
    cfg["secrets"] = {k: os.environ.get(k, "").strip() for k in ("PLEX_TOKEN", "HUE_KEY", "HA_TOKEN")}
    for need in ("PLEX_TOKEN", "HUE_KEY"):
        if not cfg["secrets"][need]:
            raise SystemExit(f"{need} is not set (see .env.example)")
    if not cfg["hue"]["host"] or not cfg["hue"]["strip"]:
        raise SystemExit("config: [hue] host and strip are required")
    if cfg["after"]["mode"] not in ("fixed", "restore", "leave"):
        raise SystemExit("config: [after] mode must be fixed, restore or leave")
    return cfg


# ---------------------------------------------------------------- Hue

class Hue:
    def __init__(self, host, key):
        self.base = f"https://{host}/clip/v2/resource/light"
        self.h = {"hue-application-key": key}

    def lights(self):
        return http(self.base, self.h, insecure=True)["data"]

    def get(self, lid):
        return http(f"{self.base}/{lid}", self.h, insecure=True)["data"][0]

    def put(self, lid, body):
        return http(f"{self.base}/{lid}", self.h, body, "PUT", insecure=True)

    def resolve(self, name_or_id):
        for l in self.lights():
            if name_or_id in (l["id"], l["metadata"]["name"]):
                return l["id"]
        raise SystemExit(f"Hue light not found: {name_or_id!r} (run --lights)")

    def snapshot(self, lid):
        l = self.get(lid)
        s = {"on": {"on": l["on"]["on"]}}
        if "dimming" in l:
            s["dimming"] = {"brightness": l["dimming"]["brightness"]}
        ct = l.get("color_temperature", {})
        if ct.get("mirek_valid") and ct.get("mirek"):
            s["color_temperature"] = {"mirek": ct["mirek"]}
        elif "color" in l:
            s["color"] = {"xy": l["color"]["xy"]}
        if l.get("gradient", {}).get("points") and "color_temperature" not in s:
            s["gradient"] = {"points": l["gradient"]["points"]}
        return s


def kelvin_to_mirek(k):
    return max(153, min(500, round(1_000_000 / k)))


# ---------------------------------------------------------------- Plex / Home Assistant

class Plex:
    def __init__(self, cfg, token):
        self.url = cfg["url"].rstrip("/")
        self.player = cfg["player"].lower()
        self.path_map = cfg["path_map"]
        self.h = {"X-Plex-Token": token}
        self.last = None            # (key, offset, seen_at)

    def session(self):
        """Matching session → dict(file, hdr, title, state, position) or None."""
        d = http(f"{self.url}/status/sessions", self.h)
        for m in d["MediaContainer"].get("Metadata", []):
            p = m.get("Player", {})
            ident = " ".join(str(p.get(k, "")) for k in ("title", "product", "device", "machineIdentifier")).lower()
            if self.player and self.player not in ident:
                continue
            if m.get("type") not in ("movie", "episode"):
                continue
            part = m["Media"][0]["Part"][0]
            f = part["file"]
            for a, b in self.path_map:
                if f.startswith(a):
                    f = b + f[len(a):]
                    break
            hdr = False
            for s in part.get("Stream", []):
                if s.get("streamType") == 1:
                    t = s.get("displayTitle", "")
                    hdr = s.get("colorTrc") in ("smpte2084", "arib-std-b67") or "HDR" in t or "Dolby Vision" in t
                    break
            # Plex only refreshes viewOffset every few seconds: extrapolate from when it changed.
            off, now, key = m.get("viewOffset", 0) / 1000, time.time(), m.get("ratingKey")
            if not self.last or self.last[0] != key or self.last[1] != off:
                self.last = (key, off, now)
            state = p.get("state", "playing")
            pos = self.last[1] + (now - self.last[2] if state in ("playing", "buffering") else 0)
            return {"file": f, "hdr": hdr, "title": m.get("title"), "state": state, "position": pos}
        self.last = None
        return None


class HomeAssistant:
    def __init__(self, cfg, token):
        self.url = cfg["url"].rstrip("/")
        self.cfg = cfg
        self.h = {"Authorization": "Bearer " + token}

    def state(self, eid):
        return http(f"{self.url}/api/states/{eid}", self.h)

    def switch_on(self):
        return not self.cfg["switch"] or self.state(self.cfg["switch"])["state"] == "on"

    def position(self):
        """(state, position) from the media player, or None."""
        if not self.cfg["media_player"]:
            return None
        s = self.state(self.cfg["media_player"])
        a = s["attributes"]
        if a.get("media_position") is None or not a.get("media_position_updated_at"):
            return None
        upd = datetime.datetime.fromisoformat(a["media_position_updated_at"]).timestamp()
        pos = float(a["media_position"]) + (time.time() - upd if s["state"] == "playing" else 0)
        return s["state"], pos

    def number(self, eid):
        try:
            return float(self.state(eid)["state"])
        except (ValueError, TypeError):
            return None

    def lamps_yield(self):
        e = self.cfg["lamps_yield_on_pause_to"]
        return bool(e) and self.state(e)["state"] == "on"


# ---------------------------------------------------------------- frames → colours

def grab(path, pos, hdr):
    vf = f"scale={W}:{H}:flags=area"
    if hdr:
        vf += (",zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
               "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=rgb24")
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "2",
                          "-ss", f"{max(pos, 0):.2f}", "-i", path, "-frames:v", "1", "-an", "-sn",
                          "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, timeout=15).stdout
    if len(out) != W * H * 3:
        return None
    return [[tuple(out[(y * W + x) * 3:(y * W + x) * 3 + 3]) for x in range(W)] for y in range(H)]


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def zone_avg(img, x0, x1, y0, y1):
    px = [img[y][x] for y in range(y0, y1) for x in range(x0, x1)]
    return tuple(sum(_lin(p[i]) for p in px) / len(px) for i in range(3))   # linear light


def to_xy(rgb, saturation):
    r, g, b = rgb
    if max(r, g, b) < 0.004:                       # black → neutral; brightness does the dimming
        return 0.3227, 0.3290
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * saturation), 1.0)
    X = r * 0.4124 + g * 0.3576 + b * 0.1805
    Y = r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = r * 0.0193 + g * 0.1192 + b * 0.9505
    t = X + Y + Z
    return round(X / t, 4), round(Y / t, 4)


def analyse(img, sync, reverse):
    """→ (5 gradient points, brightness %, {'left','right','center'} colours)."""
    sat = sync["saturation"]
    # Strip behind a TV usually runs up the left side, across the top, down the right side.
    zones = [(0, 5, 4, H), (0, 11, 0, 7), (11, 21, 0, 7), (21, W, 0, 7), (W - 5, W, 4, H)]
    pts = [to_xy(zone_avg(img, *z), sat) for z in zones]
    sides = {"left": to_xy(zone_avg(img, 0, 11, 0, H), sat),
             "right": to_xy(zone_avg(img, W - 11, W, 0, H), sat),
             "center": to_xy(zone_avg(img, 0, W, 0, H), sat)}
    if reverse:
        pts.reverse()
        sides["left"], sides["right"] = sides["right"], sides["left"]
    raw = {"left": zone_avg(img, 0, 11, 0, H), "right": zone_avg(img, W - 11, W, 0, H),
           "center": zone_avg(img, 0, W, 0, H)}
    if reverse:
        raw["left"], raw["right"] = raw["right"], raw["left"]
    r, g, b = raw["center"]
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    lo, hi = sync["brightness_min"], sync["brightness_max"]
    return pts, lo + (hi - lo) * min(1.0, (luma / 0.25) ** 0.5), sides, raw


# ---------------------------------------------------------------- ambient light sensitivity

AMBIENT_MAX_S = 1800    # sensitivity 1 ≈ average colour of the last 30 minutes


def ambient_tau(sensitivity):
    """Sensitivity 1–100 → smoothing time constant in seconds (100 → 0 = follow the picture;
    75 ≈ 7 s, 50 ≈ 40 s, 25 ≈ 4 min, 1 ≈ 30 min). Logarithmic so the whole range is useful."""
    s = max(1.0, min(100.0, float(sensitivity)))
    return (AMBIENT_MAX_S + 1) ** ((100 - s) / 99) - 1


class Ambient:
    """Per-lamp time-weighted average of colour (linear RGB) and brightness.
    Normalised exponential average: early in a movie it's the plain average of what's been seen,
    later it's dominated by roughly the last `tau` seconds."""
    def __init__(self):
        self.state = {}       # lamp id → [sum_r, sum_g, sum_b, sum_bri, weight, last_update]
        self.sent = {}        # lamp id → (x, y, bri) last sent

    def reset(self):
        self.state.clear(); self.sent.clear()

    def update(self, lid, rgb, bri, sensitivity, now):
        tau = ambient_tau(sensitivity)
        st = self.state.get(lid)
        if st is None or tau <= 0:
            self.state[lid] = [*rgb, bri, 1.0, now]
            return rgb, bri
        decay = math.exp(-(now - st[5]) / tau)
        for i, v in enumerate((*rgb, bri)):
            st[i] = st[i] * decay + v
        st[4] = st[4] * decay + 1.0
        st[5] = now
        return tuple(v / st[4] for v in st[:3]), st[3] / st[4]

    def changed(self, lid, x, y, bri):
        prev = self.sent.get(lid)
        if prev and abs(prev[0] - x) < 0.002 and abs(prev[1] - y) < 0.002 and abs(prev[2] - bri) < 0.5:
            return False
        self.sent[lid] = (x, y, bri)
        return True


# ---------------------------------------------------------------- main loop

def run(cfg):
    s = cfg["sync"]
    hue = Hue(cfg["hue"]["host"], cfg["secrets"]["HUE_KEY"])
    strip = hue.resolve(cfg["hue"]["strip"])
    strip_is_gradient = "gradient" in hue.get(strip)
    lamps = [(hue.resolve(l["light"]), l.get("side", "center"), float(l.get("factor", s["lamp_factor"])),
              l.get("sensitivity"))
             for l in cfg["hue"]["lamps"]]
    ambient, sens, movie = Ambient(), float(s["ambient_sensitivity"]), None
    sens_entity = cfg["home_assistant"]["ambient_sensitivity_entity"]
    plex = Plex(cfg["plex"], cfg["secrets"]["PLEX_TOKEN"])
    ha = HomeAssistant(cfg["home_assistant"], cfg["secrets"]["HA_TOKEN"]) \
        if cfg["home_assistant"]["url"] and cfg["secrets"]["HA_TOKEN"] else None
    log(f"movie-glow started: strip {cfg['hue']['strip']!r}{' (gradient)' if strip_is_gradient else ''}, "
        f"{len(lamps)} lamp(s), Home Assistant {'on' if ha else 'off'}, after={cfg['after']['mode']}, "
        f"ambient sensitivity {sens:g}{' (from ' + sens_entity + ')' if ha and sens_entity else ''}")

    active, snap, bri_s, tick, status, sess, last_poll = False, {}, None, 0, None, None, 0

    def on_term(*_):                     # docker stop / Ctrl-C: hand the lights back first
        if active:
            try:
                stop(cfg, hue, strip, lamps, snap, None, None)
            except Exception as e:
                log("error during shutdown:", e)
        log("exiting")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    while True:
        t0 = time.time()
        try:
            if t0 - last_poll > 3 or not active:
                last_poll = t0
                enabled = ha.switch_on() if ha else True
                sess = plex.session() if enabled else None
                want = bool(sess) and sess["state"] in ("playing", "buffering")
                ha_pos = ha.position() if (ha and want) else None
                if ha and sens_entity and want:
                    v = ha.number(sens_entity)
                    if v is not None and v != sens:
                        log(f"ambient sensitivity {sens:g} → {v:g}")
                        sens = v
                if sess and sess["file"] != movie:          # new movie: start the averages fresh
                    movie = sess["file"]
                    ambient.reset()
                if ha_pos and ha_pos[0] == "paused":
                    want = False
                new_status = f"syncing {sess['title']}" if want else \
                    ("idle (switch off)" if not enabled else f"idle ({sess['state'] if sess else 'nothing playing'})")
                if new_status != status:
                    log(new_status)
                    status = new_status

            if not want:
                if active:
                    active = False
                    stop(cfg, hue, strip, lamps, snap, ha, sess)
                time.sleep(2)
                continue

            if not active:
                active, bri_s, tick = True, None, 0
                if cfg["after"]["mode"] == "restore":
                    snap = {lid: hue.snapshot(lid) for lid in [strip] + [l[0] for l in lamps]}

            pos = (ha_pos[1] if ha_pos and ha_pos[0] == "playing" else sess["position"]) \
                + (time.time() - last_poll) + s["lead"]
            img = grab(sess["file"], pos, sess["hdr"])
            if img:
                pts, bri, sides, raw = analyse(img, s, cfg["hue"]["reverse"])
                bri_s = bri if bri_s is None else bri_s * 0.4 + bri * 0.6
                body = {"on": {"on": True}, "dimming": {"brightness": round(bri_s, 1)},
                        "dynamics": {"duration": s["fade_ms"]}}
                if strip_is_gradient:
                    body["gradient"] = {"points": [{"color": {"xy": {"x": x, "y": y}}} for x, y in pts]}
                else:
                    x, y = sides["center"]
                    body["color"] = {"xy": {"x": x, "y": y}}
                hue.put(strip, body)
                tick += 1
                now = time.time()
                for lid, side, factor, lamp_sens in lamps:
                    # every frame feeds the average; at sensitivity 100 this is just the current frame
                    rgb, lbri = ambient.update(lid, raw.get(side, raw["center"]), bri,
                                               lamp_sens if lamp_sens is not None else sens, now)
                    if tick % 2 == 1:              # lamps every other update: gentler on the room + bridge
                        x, y = to_xy(rgb, s["saturation"])
                        b = round(max(1.0, lbri * factor), 1)
                        if ambient.changed(lid, x, y, b) or tick % 20 == 1:
                            hue.put(lid, {"on": {"on": True}, "dimming": {"brightness": b},
                                          "color": {"xy": {"x": x, "y": y}},
                                          "dynamics": {"duration": s["lamp_fade_ms"]}})
        except Exception as e:
            log("error:", type(e).__name__, str(e)[:200])
            time.sleep(5)
        time.sleep(max(0.2, s["interval"] - (time.time() - t0)))


def stop(cfg, hue, strip, lamps, snap, ha, sess):
    a = cfg["after"]
    targets = [strip] + [l[0] for l in lamps]
    # Optional: while paused, let another automation (e.g. a "movie mode" pause scene) own the lamps.
    if ha and sess and sess.get("state") == "paused" and ha.lamps_yield():
        targets = [strip]
    for lid in targets:
        if a["mode"] == "restore" and lid in snap:
            hue.put(lid, {**snap[lid], "dynamics": {"duration": 2000}})
        elif a["mode"] == "fixed":
            hue.put(lid, {"on": {"on": True}, "dimming": {"brightness": a["brightness"]},
                          "color_temperature": {"mirek": kelvin_to_mirek(a["kelvin"])},
                          "dynamics": {"duration": 2000}})
    log(f"stopped ({a['mode']})")


# ---------------------------------------------------------------- helpers

def pair(host):
    print(f"Press the round link button on the Hue bridge at {host} now (waiting 60 s)...")
    for _ in range(30):
        r = http(f"https://{host}/api", data={"devicetype": "movie_glow#docker", "generateclientkey": True},
                 insecure=True)
        if r and "success" in r[0]:
            print("\nHUE_KEY=" + r[0]["success"]["username"])
            return
        time.sleep(2)
    raise SystemExit("Timed out — the link button wasn't pressed.")


def list_lights():
    host, key = os.environ.get("HUE_HOST"), os.environ.get("HUE_KEY")
    if not host:
        try:
            host = load_config()["hue"]["host"]
        except Exception:
            pass
    if not host or not key:
        raise SystemExit("Set HUE_HOST (or [hue] host in config.toml) and HUE_KEY first.")
    for l in sorted(Hue(host, key).lights(), key=lambda l: l["metadata"]["name"]):
        kind = "GRADIENT" if "gradient" in l else ("colour" if "color" in l else "white")
        print(f"{l['metadata']['name']:<32} {kind:<9} {l['id']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--pair"] and len(args) == 2:
        pair(args[1])
    elif args[:1] == ["--lights"]:
        list_lights()
    elif args[:1] == ["--once"] and len(args) >= 3:
        img = grab(args[1], float(args[2]), "--hdr" in args)
        if not img:
            raise SystemExit("could not read a frame")
        pts, bri, sides, _ = analyse(img, DEFAULTS["sync"], False)
        print(json.dumps({"gradient_points": pts, "brightness": round(bri, 1), "sides": sides}, indent=1))
    elif not args:
        run(load_config())
    else:
        print(__doc__)
        sys.exit(2)
