#!/usr/bin/env python3
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
"""
Te_FITI Viewer + hybrid orchestration (HA ingress, stdlib + ffmpeg only).

The viewer lists ALL clips under SCAN_ROOT (full TeslaCam tree). Per camera file:
  - plain   : never encrypted              -> directly playable
  - ready   : encrypted + in cache         -> directly playable
  - key     : encrypted + key available    -> decrypt on demand (prepare)
  - locked  : encrypted + NO key           -> fetch key first

Thumbnails: uses existing Tesla thumb.png; otherwise generates a frame via
ffmpeg at the event timestamp (event.json) or ~1 s and caches it.

  GET  /                      www/index.html
  GET  /api/status            counters + login + busy + last_api
  GET  /api/clips             ALL clips incl. camera states + has_tel
  GET  /api/thumb?id=         thumbnail (png/jpg), generated/cached
  POST /api/prepare           {id} -> decrypt clip on demand, return fresh clip
  POST /api/fetch             Direct API: fetch missing keys now
  POST /api/decrypt           decrypt all keyed clips now (batch)
  POST /api/telemetry_all     extract SEI telemetry for all plain clips missing it (batch)
  POST /api/keys              FEKs (bookmarklet) -> store
  GET  /api/pending.json      items (without key) for the bookmarklet
  GET  /api/login/url         Direct API: login URL
  POST /api/login/exchange    Direct API: callback URL -> token
  GET  /api/zip?id=           clip (decrypted) as ZIP
  GET  /media/<scanrel>       file from cache OR plain (range-capable)
"""
import os, json, argparse, re, glob, posixpath, threading, time, base64, zipfile, hashlib, datetime
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import keybridge, pipeline, keystore
from tesla_auth import TeslaAuth
import tesla_api

WWW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = OUT_DIR = SCAN_DIR = "."   # SRC=enc root, OUT=cache, SCAN=full clip tree
ENC_PREFIX = "EncryptedClips"         # SRC_DIR relative to SCAN_DIR
KEYS_FILE = ""
INTERVAL = 300
DELETE = False
AUTO_DECRYPT = False
EMBED_KEY = False
DIRECT_API = True
LIST_TTL = 15
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(.+)\.mp4$", re.I)
CAM_NAMES = ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar")

auth = None
_lock = threading.Lock()
_busy = False
_last_api = {"ok": None, "msg": "", "got": 0}
_prep_locks = {}
_prep_guard = threading.Lock()
_lcache = {"t": 0.0, "data": None}
_lcache_guard = threading.Lock()
_thumb_job = {"running": False, "done": 0, "total": 0}
_thumb_guard = threading.Lock()
_tel_job = {"running": False, "done": 0, "total": 0}
_tel_guard = threading.Lock()

# Persistent metadata cache — avoids re-reading telemetry/event JSON on every scan
_meta_cache = {}
_META_CACHE_FILE = ""

def _load_meta_cache():
    global _meta_cache
    if _META_CACHE_FILE and os.path.isfile(_META_CACHE_FILE):
        try:
            _meta_cache = json.load(open(_META_CACHE_FILE, encoding="utf-8"))
        except Exception:
            _meta_cache = {}

def _save_meta_cache():
    if not _META_CACHE_FILE:
        return
    try:
        tmp = _META_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_meta_cache, f, separators=(",", ":"))
        os.replace(tmp, _META_CACHE_FILE)
    except Exception:
        pass


# ---------- Path helpers (scanrel = path relative to SCAN_DIR, posix) ----------
def _norm(rel):
    return posixpath.normpath(rel).lstrip("/")

def is_enc_sr(sr):
    return bool(ENC_PREFIX) and (sr == ENC_PREFIX or sr.startswith(ENC_PREFIX + "/"))

def enc_id(sr):
    return sr[len(ENC_PREFIX) + 1:] if is_enc_sr(sr) else sr

def cache_abspath(sr):
    return os.path.normpath(os.path.join(OUT_DIR, sr))

def src_abspath(sr):
    return os.path.normpath(os.path.join(SCAN_DIR, sr))

def _sr_of_cam(folder, ts, cam):
    return (folder + "/" if folder else "") + f"{ts}-{cam}.mp4"

def _telsr(folder, ts):
    return (folder + "/" if folder else "") + f"{ts}-front.telemetry.json"


# ---------- Clip state ----------
def _cam_state(sr, keys):
    if is_enc_sr(sr):
        if os.path.exists(cache_abspath(sr)):
            return {"state": "ready", "url": "media/" + sr}
        if enc_id(sr) in keys:
            return {"state": "key", "url": None}
        return {"state": "locked", "url": None}
    return {"state": "plain", "url": "media/" + sr}

def _compute_meta(c):
    """Expensive: reads telemetry + event JSON from disk."""
    telp = cache_abspath(_telsr(c["folder"], c["timestamp"]))
    ht = False
    gps_center = None
    if os.path.isfile(telp):
        try:
            tel = json.load(open(telp, encoding="utf-8"))
            ht = tel.get("frame_count", 0) > 0
            gps_pts = [[f["lat"], f["lon"]] for f in tel.get("frames", []) if f.get("lat") and f.get("lon")]
            if gps_pts:
                avg_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                avg_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
                gps_center = {"center_lat": avg_lat, "center_lon": avg_lon}
        except Exception:
            ht = False
    ejp = os.path.join(SCAN_DIR, c["folder"], "event.json")
    he = os.path.isfile(ejp)
    if not gps_center and he:
        try:
            ev = json.load(open(ejp, encoding="utf-8"))
            lat = float(ev.get("est_lat") or ev.get("lat") or 0)
            lon = float(ev.get("est_lon") or ev.get("lon") or 0)
            if lat and lon:
                gps_center = {"center_lat": lat, "center_lon": lon}
        except Exception:
            pass
    return {"has_tel": ht, "has_event": he, "gps_bounds": gps_center, "has_data": ht or he}

def _finalize(c):
    sts = [cm["state"] for cm in c["cameras"].values()]
    c["needs_prepare"] = "key" in sts
    c["has_locked"] = "locked" in sts
    c["playable"] = any(s in ("plain", "ready") for s in sts)
    cid = c["id"]
    cached = _meta_cache.get(cid)
    if cached is None:
        cached = _compute_meta(c)
        _meta_cache[cid] = cached
    c["has_tel"] = cached["has_tel"]
    c["has_event"] = cached["has_event"]
    c["gps_bounds"] = cached.get("gps_bounds")
    c["has_data"] = cached["has_data"]
    return c


def _scan(keys=None):
    if keys is None:
        keys = keystore.load(KEYS_FILE)
    clips = {}
    for path in glob.glob(os.path.join(SCAN_DIR, "**", "*.mp4"), recursive=True):
        m = TS_RE.search(os.path.basename(path))
        if not m:
            continue
        ts, cam = m.group(1), m.group(2).lower()
        sr = os.path.relpath(path, SCAN_DIR).replace("\\", "/")
        folder = posixpath.dirname(sr)
        ck = folder + "|" + ts
        c = clips.setdefault(ck, {"id": ck, "folder": folder, "timestamp": ts,
                                  "source": folder.split("/")[0] if folder else "",
                                  "cameras": {}, "telemetry": None})
        c["cameras"][cam] = _cam_state(sr, keys)
        if cam == "front" and os.path.exists(cache_abspath(_telsr(folder, ts))):
            c["telemetry"] = "media/" + _telsr(folder, ts)
    out = [_finalize(c) for c in clips.values()]
    out.sort(key=lambda x: x["timestamp"], reverse=True)
    _save_meta_cache()
    return out


def clips_cached():
    now = time.time()
    with _lcache_guard:
        if _lcache["data"] is None or now - _lcache["t"] >= LIST_TTL:
            _lcache["data"] = _scan()
            _lcache["t"] = now
        return _lcache["data"]

def invalidate(clip_id=None):
    _lcache["t"] = 0.0
    if clip_id:
        _meta_cache.pop(clip_id, None)


def _clip_cams(cid):
    folder, ts = cid.rsplit("|", 1) if "|" in cid else ("", cid)
    cams = {}
    for path in glob.glob(os.path.join(SCAN_DIR, folder, f"{ts}-*.mp4")):
        m = TS_RE.search(os.path.basename(path))
        if m:
            cams[m.group(2).lower()] = _sr_of_cam(folder, ts, m.group(2).lower())
    return folder, ts, cams

def _scan_one(cid, keys=None):
    if keys is None:
        keys = keystore.load(KEYS_FILE)
    folder, ts, cams = _clip_cams(cid)
    if not cams:
        return None
    c = {"id": cid, "folder": folder, "timestamp": ts,
         "source": folder.split("/")[0] if folder else "", "cameras": {}, "telemetry": None}
    for cam, sr in cams.items():
        c["cameras"][cam] = _cam_state(sr, keys)
    if os.path.exists(cache_abspath(_telsr(folder, ts))):
        c["telemetry"] = "media/" + _telsr(folder, ts)
    return _finalize(c)


def counts(clips):
    cams = [cm for c in clips for cm in c["cameras"].values()]
    return {
        "clips": len(clips),
        "encrypted": sum(1 for cm in cams if cm["state"] in ("ready", "key", "locked")),
        "plain": sum(1 for cm in cams if cm["state"] == "plain"),
        "decrypted": sum(1 for cm in cams if cm["state"] == "ready"),
        "keyed": sum(1 for cm in cams if cm["state"] in ("ready", "key")),
        "need_keys": sum(1 for cm in cams if cm["state"] == "locked"),
        "need_decrypt": sum(1 for cm in cams if cm["state"] == "key"),
        "with_telemetry": sum(1 for c in clips if c.get("has_tel")),
        "with_data": sum(1 for c in clips if c.get("has_data")),
    }

def _get_event_data(cid):
    """Returns event.json data: seek offset, GPS, reason, etc."""
    folder, ts, _ = _clip_cams(cid)
    if not folder or not ts:
        return None
    ej = os.path.join(SCAN_DIR, folder, "event.json")
    if not os.path.isfile(ej):
        return None
    try:
        ev = json.load(open(ej, encoding="utf-8"))
        result = {}
        et = ev.get("timestamp", "")
        if et:
            cs = datetime.datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
            evt = datetime.datetime.strptime(et[:19], "%Y-%m-%dT%H:%M:%S")
            off = (evt - cs).total_seconds()
            if 0 <= off <= 3600:
                result["seek"] = off
        lat = float(ev.get("est_lat") or ev.get("lat") or 0)
        lon = float(ev.get("est_lon") or ev.get("lon") or 0)
        if lat and lon:
            result["lat"] = lat
            result["lon"] = lon
        if ev.get("reason"):
            result["reason"] = ev["reason"]
        if ev.get("city"):
            result["city"] = ev["city"]
        return result if result else None
    except Exception:
        return None


# ---------- Decryption ----------
def _clip_lock(cid):
    with _prep_guard:
        l = _prep_locks.get(cid)
        if l is None:
            l = threading.Lock()
            _prep_locks[cid] = l
        return l

def _decrypt_cam(sr, keys):
    fek = base64.b64decode(keys[enc_id(sr)])
    pipeline.decrypt_and_cache(src_abspath(sr), cache_abspath(sr), fek, embed_key=EMBED_KEY)

def prepare_clip(cid):
    keys = keystore.load(KEYS_FILE)
    folder, ts, cams = _clip_cams(cid)
    if not cams:
        return {"ok": False, "error": "clip not found"}
    jobs = []
    for cam, sr in cams.items():
        if is_enc_sr(sr):
            if not os.path.exists(cache_abspath(sr)) and enc_id(sr) in keys:
                jobs.append(("dec", sr))
        elif cam == "front":
            jobs.append(("tel", sr))
    errs = []
    def do(job):
        kind, sr = job
        try:
            if kind == "dec":
                _decrypt_cam(sr, keys)
            else:
                telp = os.path.splitext(cache_abspath(sr))[0] + ".telemetry.json"
                pipeline.telemetry_for_plain(src_abspath(sr), telp)
        except Exception as e:
            errs.append(f"{os.path.basename(sr)}: {e}")
    with _clip_lock(cid):
        if jobs:
            with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as ex:
                list(ex.map(do, jobs))
    invalidate(cid)
    return {"ok": not errs, "errors": errs, "clip": _scan_one(cid, keys)}

def ensure_all():
    keys = keystore.load(KEYS_FILE)
    jobs = [_sr_of_cam(c["folder"], c["timestamp"], cam)
            for c in _scan(keys)
            for cam, info in c["cameras"].items() if info["state"] == "key"]
    def do(sr):
        try:
            _decrypt_cam(sr, keys)
        except Exception as e:
            print(f"[decrypt] {sr}: {e}", flush=True)
    if jobs:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(do, jobs))
    _meta_cache.clear()
    invalidate()
    return {"decrypted": len(jobs)}


# ---------- Thumbnails ----------
def _event_seek(folder, ts):
    """Seek offset (s) of the event timestamp from event.json within this clip, or None."""
    ej = os.path.join(SCAN_DIR, folder, "event.json")
    if not os.path.isfile(ej):
        return None
    try:
        et = json.load(open(ej, encoding="utf-8")).get("timestamp", "")
        cs = datetime.datetime.strptime(ts, "%Y-%m-%d_%H-%M-%S")
        ev = datetime.datetime.strptime(et[:19], "%Y-%m-%dT%H:%M:%S")
        off = (ev - cs).total_seconds()
        return off if 0 <= off <= 120 else None
    except Exception:
        return None

def make_thumb(cid):
    """Returns path to a thumbnail (png/jpg) or None (e.g. locked)."""
    folder, ts, cams = _clip_cams(cid)
    if not cams and "|" in cid:
        folder, ts = cid.rsplit("|", 1)
    # use existing Tesla thumb.png (plain folders only)
    if not is_enc_sr(folder):
        tp = os.path.join(SCAN_DIR, folder, "thumb.png")
        if os.path.isfile(tp):
            return tp
    safe = hashlib.sha1(cid.encode()).hexdigest()[:20]
    cache = os.path.join(OUT_DIR, ".thumbs", safe + ".jpg")
    if os.path.isfile(cache):
        return cache
    front_sr = _sr_of_cam(folder, ts, "front")
    if not cams.get("front") and not os.path.isfile(src_abspath(front_sr)) \
            and not os.path.isfile(cache_abspath(front_sr)):
        return None
    keys = keystore.load(KEYS_FILE)
    with _clip_lock(cid):
        if os.path.isfile(cache):
            return cache
        if is_enc_sr(front_sr):
            cp = cache_abspath(front_sr)
            if not os.path.isfile(cp):
                if enc_id(front_sr) not in keys:
                    return None
                try:
                    _decrypt_cam(front_sr, keys)
                    invalidate()
                except Exception:
                    return None
            src = cp
        else:
            src = src_abspath(front_sr)
            if not os.path.isfile(src):
                return None
        seek = _event_seek(folder, ts)
        if seek is None:
            seek = 1.0
        return cache if pipeline.make_thumbnail(src, cache, seek=seek) else None


def _thumb_cache_path(cid):
    return os.path.join(OUT_DIR, ".thumbs", hashlib.sha1(cid.encode()).hexdigest()[:20] + ".jpg")

def gen_all_thumbs():
    """Batch: generate thumbnails for ALL clips with event/telemetry data (encrypted + plain)."""
    global _thumb_job
    if _thumb_job.get("running"):
        return
    clips = _scan()
    targets = []
    for c in clips:
        if c.get("has_data"):  # telemetry OR event present
            thumb_path = _thumb_cache_path(c["id"])
            if not os.path.isfile(thumb_path):
                targets.append(c["id"])
    _thumb_job = {"running": True, "done": 0, "total": len(targets), "started": time.time()}
    def do(cid):
        try:
            make_thumb(cid)
        except Exception:
            pass
        with _thumb_guard:
            _thumb_job["done"] += 1
    try:
        if targets:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(do, targets))
            print(f"[thumbs] {_thumb_job['done']}/{_thumb_job['total']} Vorschauen erzeugt", flush=True)
    finally:
        invalidate()
        _thumb_job["running"] = False


def gen_all_telemetry():
    """Batch: extract SEI telemetry for all plain front-camera clips that don't have it cached yet."""
    global _tel_job
    if _tel_job.get("running"):
        return
    clips = _scan()
    targets = []
    for c in clips:
        front = c["cameras"].get("front")
        if front and front["state"] == "plain" and not c.get("has_tel"):
            targets.append(_sr_of_cam(c["folder"], c["timestamp"], "front"))
    _tel_job = {"running": True, "done": 0, "total": len(targets)}
    def do(sr):
        try:
            telp = os.path.splitext(cache_abspath(sr))[0] + ".telemetry.json"
            pipeline.telemetry_for_plain(src_abspath(sr), telp)
        except Exception as e:
            print(f"[telemetry] {sr}: {e}", flush=True)
        with _tel_guard:
            _tel_job["done"] += 1
    try:
        if targets:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(do, targets))
            print(f"[telemetry] {_tel_job['done']}/{_tel_job['total']} extracted", flush=True)
    finally:
        _meta_cache.clear()
        invalidate()
        _tel_job["running"] = False


# ---------- Direct API ----------
def api_fetch(items):
    global _last_api
    if not DIRECT_API:
        return {"ok": False, "msg": "Direct API disabled"}
    token = auth.get_access_token()
    if not token:
        _last_api = {"ok": False, "msg": "not logged in", "got": 0}
        return _last_api
    got = 0
    for i in range(0, len(items), 30):
        chunk = items[i:i + 30]
        try:
            res = tesla_api.fetch_keys(chunk, token)
        except tesla_api.DecryptApiError as e:
            _last_api = {"ok": False, "msg": f"API: {e} (Bookmarklet nutzen)", "got": got}
            return _last_api
        got += keystore.merge(KEYS_FILE, res)
    _last_api = {"ok": True, "msg": "ok", "got": got}
    if got:
        invalidate()
    return _last_api

def run_cycle(do_fetch=True, do_decrypt=None):
    global _busy
    if do_decrypt is None:
        do_decrypt = AUTO_DECRYPT
    if not _lock.acquire(blocking=False):
        return {"skipped": "busy"}
    _busy = True
    try:
        if do_fetch and DIRECT_API and auth.get_access_token():
            items = keybridge.scan_items(SRC_DIR, keystore.load(KEYS_FILE))
            if items:
                r = api_fetch(items)
                print(f"[fetch] {len(items)} offen, +{r.get('got',0)} Keys ({r.get('msg')})", flush=True)
        if do_decrypt:
            r = ensure_all()
            if r["decrypted"]:
                print(f"[decrypt] {r}", flush=True)
    finally:
        _busy = False
        _lock.release()

def bg(fn, *a, **k):
    threading.Thread(target=fn, args=a, kwargs=k, daemon=True).start()

def scheduler():
    while True:
        try:
            run_cycle()
        except Exception as e:
            print("[sched]", e, flush=True)
        time.sleep(max(30, INTERVAL))


# ---------- Media serving ----------
def resolve_media(sr):
    sr = _norm(sr)
    cp = cache_abspath(sr)
    if cp.startswith(os.path.normpath(OUT_DIR)) and os.path.isfile(cp):
        return cp
    if not is_enc_sr(sr):
        sp = src_abspath(sr)
        if sp.startswith(os.path.normpath(SCAN_DIR)) and os.path.isfile(sp):
            return sp
    return None


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def _file(self, path, ctype, extra=None):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        f = open(path, "rb")
        try:
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
                end = min(end, size - 1)
                f.seek(start)
                chunk = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(len(chunk)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(size))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            f.close()

    def _qs(self, key):
        return parse_qs(urlparse(self.path).query).get(key, [""])[0]

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file(os.path.join(WWW, "index.html"), "text/html",
                              {"Cache-Control": "no-cache, no-store, must-revalidate"})
        if path.startswith("/static/"):
            fp = os.path.join(WWW, os.path.basename(path))
            if os.path.isfile(fp):
                ct = "text/css" if fp.endswith(".css") else "application/javascript"
                return self._file(fp, ct)
            return self._send(404, {"error": "not found"})
        if path == "/api/status":
            st = counts(clips_cached())
            st["busy"] = _busy
            st["auto_decrypt"] = AUTO_DECRYPT
            st["direct_api"] = DIRECT_API
            st["login"] = auth.status()
            st["last_api"] = _last_api
            st["thumb_job"] = _thumb_job
            st["tel_job"] = _tel_job
            return self._send(200, st)
        if path == "/api/clips":
            return self._send(200, clips_cached())
        if path == "/api/thumb":
            t = make_thumb(self._qs("id"))
            if not t:
                return self._send(404, {"error": "no thumb"})
            ct = "image/png" if t.endswith(".png") else "image/jpeg"
            return self._file(t, ct, {"Cache-Control": "max-age=86400"})
        if path == "/api/event":
            cid = self._qs("id")
            data = _get_event_data(cid)
            if data is None:
                return self._send(404, {"error": "no event"})
            return self._send(200, data)
        if path == "/api/all_gps":
            clips = clips_cached()
            pts = []
            for c in clips:
                gb = c.get("gps_bounds")
                if gb:
                    pts.append([gb["center_lat"], gb["center_lon"], c["id"]])
            return self._send(200, {"points": pts})
        if path == "/api/pending.json":
            items = keybridge.scan_items(SRC_DIR, keystore.load(KEYS_FILE))
            return self._send(200, {"items": items}, "application/json",
                              {"Content-Disposition": 'attachment; filename="pending_items.json"'})
        if path == "/api/login/url":
            return self._send(200, {"url": auth.make_login_url()})
        if path == "/api/zip":
            cid = self._qs("id")
            clip = _scan_one(cid)
            if not clip:
                return self._send(404, {"error": "clip"})
            if clip.get("needs_prepare"):
                prepare_clip(cid)
                clip = _scan_one(cid)
            members = []
            for cam in clip["cameras"]:
                full = resolve_media(_sr_of_cam(clip["folder"], clip["timestamp"], cam))
                if full and full.endswith(".mp4"):
                    members.append((full, os.path.basename(full)))
            tel = resolve_media(_telsr(clip["folder"], clip["timestamp"]))
            if tel:
                members.append((tel, os.path.basename(tel)))
            if not members:
                return self._send(404, {"error": "nichts zum Packen"})
            tmp = os.path.join(OUT_DIR, ".dl_%s.zip" % hashlib.sha1(cid.encode()).hexdigest()[:12])
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                    for full, arc in members:
                        z.write(full, arc)
                self._file(tmp, "application/zip",
                           {"Content-Disposition": 'attachment; filename="%s.zip"' % clip["timestamp"]})
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return
        if path.startswith("/media/"):
            full = resolve_media(path[len("/media/"):])
            if not full:
                return self._send(404, {"error": "not found"})
            ct = ("video/mp4" if full.endswith(".mp4") else
                  "application/json" if full.endswith(".json") else
                  "image/png" if full.endswith(".png") else "application/octet-stream")
            return self._file(full, ct)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if path == "/api/prepare":
            try:
                cid = json.loads(raw or b"{}").get("id", "")
            except Exception:
                return self._send(400, {"ok": False, "error": "bad json"})
            return self._send(200, prepare_clip(cid))
        if path == "/api/keys":
            try:
                norm = keybridge.normalize_results(json.loads(raw or b"{}"))
                stored = keystore.merge(KEYS_FILE, norm)
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
            if stored:
                invalidate()
            if AUTO_DECRYPT:
                bg(run_cycle, do_fetch=False, do_decrypt=True)
            return self._send(200, {"ok": True, "stored": stored})
        if path == "/api/fetch":
            bg(run_cycle, do_fetch=True, do_decrypt=False)
            return self._send(200, {"ok": True})
        if path == "/api/decrypt":
            bg(ensure_all)
            return self._send(200, {"ok": True})
        if path == "/api/thumbs_all":
            bg(gen_all_thumbs)
            return self._send(200, {"ok": True})
        if path == "/api/telemetry_all":
            bg(gen_all_telemetry)
            return self._send(200, {"ok": True})
        if path == "/api/login/exchange":
            try:
                tok = auth.exchange_code(json.loads(raw or b"{}").get("callback", ""))
                bg(run_cycle, do_fetch=True, do_decrypt=False)
                return self._send(200, {"ok": True, "refresh": bool(tok.get("refresh_token"))})
            except Exception as e:
                return self._send(400, {"ok": False, "error": str(e)})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=".")
    p.add_argument("--out", default=".")
    p.add_argument("--scan", default="")
    p.add_argument("--keys", default="")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--interval", type=int, default=300)
    p.add_argument("--delete", action="store_true")
    p.add_argument("--no-auto-decrypt", action="store_true")
    p.add_argument("--embed-key", action="store_true")
    p.add_argument("--no-direct-api", action="store_true")
    a = p.parse_args()
    SRC_DIR = os.path.abspath(a.src)
    OUT_DIR = os.path.abspath(a.out)
    SCAN_DIR = os.path.abspath(a.scan) if a.scan else SRC_DIR
    ENC_PREFIX = os.path.relpath(SRC_DIR, SCAN_DIR).replace("\\", "/")
    if ENC_PREFIX in (".", ""):
        ENC_PREFIX = ""
    KEYS_FILE = a.keys or keystore.default_path(SRC_DIR)
    INTERVAL = a.interval
    DELETE = a.delete
    AUTO_DECRYPT = not a.no_auto_decrypt
    EMBED_KEY = a.embed_key
    DIRECT_API = not a.no_direct_api
    auth = TeslaAuth(os.path.join(DATA_DIR, "token_store.json"))
    os.makedirs(os.path.join(OUT_DIR, ".thumbs"), exist_ok=True)
    _META_CACHE_FILE = os.path.join(DATA_DIR, ".meta_cache.json")
    _load_meta_cache()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Viewer :{a.port} scan={SCAN_DIR} enc={SRC_DIR} (prefix='{ENC_PREFIX}') "
          f"out={OUT_DIR} keys={KEYS_FILE} auto_decrypt={AUTO_DECRYPT} "
          f"embed={EMBED_KEY} direct_api={DIRECT_API}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
