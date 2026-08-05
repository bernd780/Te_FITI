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
  GET  /api/event?id=         event.json data (seek offset, GPS, reason)
  POST /api/prepare           {id} -> decrypt clip on demand, return fresh clip
  POST /api/fetch             Direct API: fetch missing keys now
  POST /api/decrypt           decrypt all keyed clips now (batch)
  POST /api/telemetry_all     extract SEI telemetry for all plain clips missing it (batch)
  GET  /api/trips             clips grouped into trips (contiguous drives per vehicle)
  GET  /api/analytics         storage/clip/trip/event stats (cached 60s)
  POST /api/keys              FEKs (bookmarklet) -> store
  GET  /api/pending.json      items (without key) for the bookmarklet
  GET  /api/login/url         Direct API: login URL
  POST /api/login/exchange    Direct API: callback URL -> token
  GET  /api/zip?id=           clip (decrypted) as ZIP
  GET  /media/<scanrel>       file from cache OR plain (range-capable)
"""
import os, json, argparse, re, glob, posixpath, threading, time, base64, zipfile, hashlib, datetime, math
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import keybridge, pipeline, keystore
from keybridge import is_ecryptfs
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
DEBUG = False
LIST_TTL = 120
TRIP_GAP_MIN = 20     # minutes of inactivity that ends a trip
ANALYTICS_TTL = 300
ENC_WORKERS = 1       # parallel probes; >1 measured slower on SMB, see _classify_new
STATIC_CTYPES = {".css": "text/css", ".js": "application/javascript",
                 ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
                 ".woff2": "font/woff2", ".map": "application/json"}
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
_analytics_cache = {"t": 0.0, "data": None}
_trips_cache = {"t": 0.0, "data": None}
# Guards both derived caches and the set of builds currently in flight.
_derived_guard = threading.Lock()
_derived_running = set()
_rescan_running = False
_rescan_guard = threading.Lock()
# Serialises _scan() itself. _rescan_async() is single-flight, but the scheduler
# and the batch jobs call _scan() directly; without this they would start a
# second full scan on top of a running one, and two scans fighting over the same
# SMB mount are far slower than one (measured: 7 files/s down to 1.25).
_scan_lock = threading.Lock()
# Progress of the running scan, polled by the UI via /api/status. "phase" is one
# of walk (listing folders), index (per-file state), meta (telemetry/event JSON).
_scan_job = {"running": False, "phase": "", "done": 0, "total": 0,
             "clips": 0, "new": 0, "started": 0.0, "took": 0.0}

def _dbg(msg):
    if DEBUG:
        print(f"[debug] {msg}", flush=True)


# ---------- Persistent caches (survive add-on restarts) ----------
# Every entry below is keyed by a write-once path or clip id. TeslaCam files are
# never modified in place — a clip either exists or is deleted — so a cached
# answer stays valid until _scan() prunes the vanished path or the derived data
# is explicitly invalidated. Without this, each scan re-read every file off the
# NAS: on a network mount that dominates the request time by orders of magnitude.
_meta_cache = {}     # clip id  -> telemetry/event metadata (has_tel, gps, reason)
_enc_cache = {}      # scanrel  -> bool, file carries an eCryptfs header
_track_cache = {}    # clip id  -> decimated GPS track for the trip map
_size_cache = {}     # scanrel  -> file size in bytes (analytics storage stats)
_CACHE_FILES = {}    # name -> (dict, path)

def _cache_init(data_dir):
    """Bind each cache dict to its JSON file and load what is already there."""
    for name, d in (("meta", _meta_cache), ("enc", _enc_cache),
                    ("track", _track_cache), ("size", _size_cache)):
        path = os.path.join(data_dir, f".{name}_cache.json")
        _CACHE_FILES[name] = (d, path)
        if os.path.isfile(path):
            try:
                d.update(json.load(open(path, encoding="utf-8")))
            except Exception:
                pass
    _dbg(f"caches loaded: meta={len(_meta_cache)} enc={len(_enc_cache)} "
         f"track={len(_track_cache)} size={len(_size_cache)}")

def _cache_save(*names):
    """Atomically persist the named caches (all of them when called bare)."""
    for name in (names or tuple(_CACHE_FILES)):
        ent = _CACHE_FILES.get(name)
        if not ent:
            continue
        d, path = ent
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, separators=(",", ":"))
            os.replace(tmp, path)
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


# ---------- Directory walk ----------
def _listdir_tree(root, report=False):
    """All file paths under root, relative + posix, from one os.scandir pass per
    directory. SMB returns a whole directory listing in a single round trip, so
    this costs ~1 network op per folder — as opposed to one os.path.exists() per
    file, which is what made the old scan crawl on a NAS.

    report=True publishes folder progress to _scan_job. The total is unknown
    while walking (that is what the walk is finding out), so the UI shows this
    phase as indeterminate and only counts folders done."""
    files = set()
    if not root or not os.path.isdir(root):
        return files
    stack = [("", root)]
    folders = 0
    while stack:
        rel, absdir = stack.pop()
        try:
            entries = list(os.scandir(absdir))
        except OSError:
            continue
        folders += 1
        if report:
            _scan_job["done"] = folders
            _scan_job["clips"] = len(files)
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((r, e.path))
                else:
                    files.add(r)
            except OSError:
                continue
    return files


# ---------- Encrypted file detection (persistently cached) ----------
def _read_enc_header(abspath):
    """Does this file carry an eCryptfs header? One 28-byte read over SMB.
    Returns None if the file could not be read, so a transient failure is not
    cached as a definitive "not encrypted"."""
    try:
        with open(abspath, "rb") as f:
            return is_ecryptfs(f.read(28))
    except Exception:
        return None


def _is_encrypted(abspath, sr):
    """Cached answer for a single file. Normally a dict hit — _classify_new()
    has already filled the cache for everything the current scan will ask
    about; this only reads from disk on the single-clip paths."""
    if sr in _enc_cache:
        return _enc_cache[sr]
    result = _read_enc_header(abspath)
    if result is None:
        return False        # unreadable right now; do not cache the guess
    _enc_cache[sr] = result
    return result


def _probe_clip(srs):
    """Encryption state of a clip, from the first of its files that can be read.
    Falls back through the other cameras so one unreadable file does not
    misclassify the whole clip. None if none of them could be read."""
    for sr in srs:
        val = _read_enc_header(src_abspath(sr))
        if val is not None:
            return val
    return None


def _classify_new(mp4s):
    """Determine the encryption state of clips not seen before.

    Samples ONE camera file per clip and applies the answer to all six. The
    car writes a clip's cameras in a single pass, so they always share an
    encryption state — and this is a 6x cut in NAS round trips, which is what
    actually decides how long a first index takes.

    Reading fewer files is the lever here, not reading them faster: measured on
    a real SMB share, 8 parallel readers came out *slower* (5.0 files/s) than a
    single one (8.3 files/s) — the mount serialises the requests and the extra
    concurrency only adds contention. ENC_WORKERS stays available for shares
    that do benefit, but the default is deliberately 1.
    """
    by_clip = {}
    for sr, m in mp4s:
        if sr not in _enc_cache:
            by_clip.setdefault(posixpath.dirname(sr) + "|" + m.group(1), []).append(sr)
    if not by_clip:
        return 0
    clips = sorted(by_clip)
    _scan_job.update({"phase": "index", "done": 0, "total": len(clips), "new": len(clips)})

    def probe(cid):
        srs = by_clip[cid]
        # prefer the front camera: it is the one that always exists
        srs.sort(key=lambda s: ("-front.mp4" not in s.lower(), s))
        return _probe_clip(srs)

    done = files = 0
    with ThreadPoolExecutor(max_workers=max(1, ENC_WORKERS)) as ex:
        for i, (cid, enc) in enumerate(zip(clips, ex.map(probe, clips)), 1):
            if enc is not None:
                for sr in by_clip[cid]:
                    _enc_cache[sr] = enc
                    files += 1
            done = i
            if not i % 10:
                _scan_job["done"] = i
            if not i % 200:
                # Checkpoint: a restart during a long first index must not throw
                # away everything classified so far.
                _cache_save("enc")
    _scan_job["done"] = done
    _dbg(f"_classify_new: {done} clips probed -> {files} files classified")
    return files

# ---------- Clip state ----------
def _cam_state(sr, keys, out_files=None):
    """out_files: pre-walked set of paths under OUT_DIR. When given, the
    'is it already decrypted?' test is a set lookup instead of an SMB stat."""
    if not _is_encrypted(src_abspath(sr), sr):
        return {"state": "plain", "url": "media/" + sr}
    cached = sr in out_files if out_files is not None else os.path.exists(cache_abspath(sr))
    if cached:
        return {"state": "ready", "url": "media/" + sr}
    if sr in keys or enc_id(sr) in keys:
        return {"state": "key", "url": None}
    return {"state": "locked", "url": None}

def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))

def _compute_meta(c, out_files=None, src_files=None):
    """Expensive: reads telemetry + event JSON from disk. Only ever runs for
    clips missing from _meta_cache; the pre-walked sets spare it two SMB stats."""
    telsr = _telsr(c["folder"], c["timestamp"])
    telp = cache_abspath(telsr)
    ht = False
    gps_center = None
    has_tel_file = telsr in out_files if out_files is not None else os.path.isfile(telp)
    if has_tel_file:
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
    ejsr = (c["folder"] + "/" if c["folder"] else "") + "event.json"
    ejp = os.path.join(SCAN_DIR, c["folder"], "event.json")
    he = ejsr in src_files if src_files is not None else os.path.isfile(ejp)
    reason = None
    if he:
        try:
            ev = json.load(open(ejp, encoding="utf-8"))
            reason = ev.get("reason") or None
            if not gps_center:
                lat = float(ev.get("est_lat") or ev.get("lat") or 0)
                lon = float(ev.get("est_lon") or ev.get("lon") or 0)
                if lat and lon:
                    gps_center = {"center_lat": lat, "center_lon": lon}
        except Exception:
            pass
    return {"has_tel": ht, "has_event": he, "gps_bounds": gps_center, "has_data": ht or he, "reason": reason}

def _finalize(c, out_files=None, src_files=None):
    sts = [cm["state"] for cm in c["cameras"].values()]
    c["needs_prepare"] = "key" in sts
    c["has_locked"] = "locked" in sts
    c["playable"] = any(s in ("plain", "ready") for s in sts)
    cid = c["id"]
    cached = _meta_cache.get(cid)
    if cached is None or "reason" not in cached:
        cached = _compute_meta(c, out_files, src_files)
        _meta_cache[cid] = cached
    c["has_tel"] = cached["has_tel"]
    c["has_event"] = cached["has_event"]
    c["gps_bounds"] = cached.get("gps_bounds")
    c["has_data"] = cached["has_data"]
    c["reason"] = cached.get("reason")
    return c

def _clip_track(c):
    """Decimated GPS track (<=40 pts) for a clip, from its telemetry file.

    Kept in its own persistent cache rather than in _meta_cache: adding a key to
    the _meta_cache entries would trip the cache-sentinel check in _finalize()
    and force a synchronous re-read of every historical clip's telemetry/event
    JSON on the first request after deploy. Telemetry files are written once and
    never edited, so a cached track never goes stale — which turns /api/trips
    from "read every telemetry JSON off the NAS" into pure CPU work."""
    if not c.get("has_tel"):
        return []
    cid = c["id"]
    hit = _track_cache.get(cid)
    if hit is not None:
        return hit
    telp = cache_abspath(_telsr(c["folder"], c["timestamp"]))
    if not os.path.isfile(telp):
        return []
    try:
        tel = json.load(open(telp, encoding="utf-8"))
        pts = [[f["lat"], f["lon"]] for f in tel.get("frames", []) if f.get("lat") and f.get("lon")]
        step = max(1, len(pts) // 40)
        track = pts[::step] if pts else []
    except Exception:
        return []
    _track_cache[cid] = track
    return track


def _prune_caches(src_files, clip_ids):
    """Drop cache entries for files/clips that no longer exist (delete_originals,
    manual cleanup). Only walks the caches when something actually vanished."""
    dropped = 0
    if len(_enc_cache) > len(src_files):
        for sr in [k for k in _enc_cache if k not in src_files]:
            del _enc_cache[sr]
            dropped += 1
    if len(_size_cache) > len(src_files):
        for sr in [k for k in _size_cache if k not in src_files]:
            del _size_cache[sr]
            dropped += 1
    for cache in (_meta_cache, _track_cache):
        if len(cache) > len(clip_ids):
            for cid in [k for k in cache if k not in clip_ids]:
                del cache[cid]
                dropped += 1
    return dropped


def _scan(keys=None):
    with _scan_lock:
        return _scan_locked(keys)


def _scan_locked(keys=None):
    t0 = time.time()
    _scan_job.update({"running": True, "phase": "walk", "done": 0, "total": 0,
                      "clips": 0, "new": 0, "started": t0})
    try:
        if keys is None:
            keys = keystore.load(KEYS_FILE)
        # Two directory walks instead of thousands of individual stat/open calls.
        src_files = _listdir_tree(SCAN_DIR, report=True)
        out_files = (src_files if os.path.normpath(OUT_DIR) == os.path.normpath(SCAN_DIR)
                     else _listdir_tree(OUT_DIR))
        t_walk = time.time()

        mp4s = [(sr, m) for sr, m in
                ((sr, TS_RE.search(posixpath.basename(sr))) for sr in src_files) if m]
        # Classify unseen files up front and in parallel; afterwards the loop
        # below is pure dict lookups and touches the NAS no further.
        enc_misses = _classify_new(mp4s)
        t_enc = time.time()

        clips = {}
        for i, (sr, m) in enumerate(mp4s):
            ts, cam = m.group(1), m.group(2).lower()
            folder = posixpath.dirname(sr)
            ck = folder + "|" + ts
            top = folder.split("/")[0] if folder else ""
            vehicle = top if top.lower().startswith("tesla") and top.lower() not in ("teslacam",) else ""
            c = clips.setdefault(ck, {"id": ck, "folder": folder, "timestamp": ts,
                                      "source": top, "vehicle": vehicle,
                                      "cameras": {}, "telemetry": None})
            c["cameras"][cam] = _cam_state(sr, keys, out_files)
            if cam == "front":
                telsr = _telsr(folder, ts)
                if telsr in out_files:
                    c["telemetry"] = "media/" + telsr
        t_state = time.time()

        meta_misses = sum(1 for cid in clips
                          if cid not in _meta_cache or "reason" not in _meta_cache[cid])
        _scan_job.update({"phase": "meta", "done": 0, "total": len(clips),
                          "clips": len(clips), "new": meta_misses})
        out = []
        for i, c in enumerate(clips.values()):
            out.append(_finalize(c, out_files, src_files))
            if not i % 25:
                _scan_job["done"] = i
        # id as tie-breaker: src_files is a set, so equal timestamps in different
        # folders would otherwise reshuffle the list between scans
        out.sort(key=lambda x: (x["timestamp"], x["id"]), reverse=True)
        pruned = _prune_caches(src_files, clips)
        if enc_misses or meta_misses or pruned:
            # Only write when something actually changed — a periodic background
            # rescan must not rewrite megabytes of JSON to the SD card every time.
            _cache_save("enc", "meta")
        _dbg(f"_scan: {len(out)} clips (walk {t_walk-t0:.3f}s / {len(src_files)} src + "
             f"{len(out_files)} out files, classify {t_enc-t_walk:.3f}s for {enc_misses} "
             f"new files across {ENC_WORKERS} workers, state {t_state-t_enc:.3f}s, "
             f"{meta_misses} meta misses, {pruned} pruned, "
             f"finalize+save {time.time()-t_state:.3f}s, total {time.time()-t0:.3f}s)")
        return out
    finally:
        _scan_job.update({"running": False, "phase": "", "took": time.time() - t0})


def _rescan_async():
    """Refresh the clip list in the background, one scan at a time. Requests are
    never blocked by a scan — they get the previous list until this finishes."""
    global _rescan_running
    with _rescan_guard:
        if _rescan_running:
            return
        _rescan_running = True

    def work():
        global _rescan_running
        try:
            data = _scan()
            with _lcache_guard:
                _lcache["data"] = data
                _lcache["t"] = time.time()
            # The clip list just changed underneath trips/analytics — without
            # this they keep serving whatever they were built from, which on a
            # cold start is an empty list and reads as "0 clips, 0 trips".
            _derived_expire()
        except Exception as e:
            print("[scan]", e, flush=True)
        finally:
            with _rescan_guard:
                _rescan_running = False

    threading.Thread(target=work, daemon=True).start()


def clips_cached():
    """Stale-while-revalidate, and never blocking: always answers from memory.
    A stale list is served as-is while it refreshes in the background; a cold
    cache answers empty rather than holding the request open for the length of a
    full NAS scan — the UI renders _scan_job progress instead and reloads when
    the scan finishes. Blocking here is what made the whole viewer look hung."""
    with _lcache_guard:
        data = _lcache["data"]
        fresh = data is not None and time.time() - _lcache["t"] < LIST_TTL
    if not fresh:
        _rescan_async()
    return data if data is not None else []

def invalidate(clip_id=None):
    _lcache["t"] = 0.0
    _derived_expire()
    if clip_id:
        _meta_cache.pop(clip_id, None)
        _track_cache.pop(clip_id, None)
    # Deliberately does NOT kick off a rescan: make_thumb() invalidates once per
    # generated thumbnail, which during a batch job would chain scans back to
    # back. The next request picks the refresh up and is served stale meanwhile.


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

def _trip_route_and_events(clips):
    route = []
    events = {}
    for c in clips:
        track = _clip_track(c)
        if track:
            route.extend(track)
        elif c.get("gps_bounds"):
            route.append([c["gps_bounds"]["center_lat"], c["gps_bounds"]["center_lon"]])
        if c.get("has_event") and c.get("reason"):
            events[c["reason"]] = events.get(c["reason"], 0) + 1
    return route, events

def _make_trip(vehicle, clips):
    route, events = _trip_route_and_events(clips)
    dist = sum(_haversine_km(*route[i], *route[i + 1]) for i in range(len(route) - 1))
    bounds = None
    if route:
        lats = [p[0] for p in route]
        lons = [p[1] for p in route]
        bounds = {"min_lat": min(lats), "max_lat": max(lats), "min_lon": min(lons), "max_lon": max(lons)}
    return {
        "id": vehicle + "|" + clips[0]["timestamp"],
        "vehicle": vehicle,
        "start": clips[0]["timestamp"],
        "end": clips[-1]["timestamp"],
        "clip_ids": [c["id"] for c in clips],
        "clip_count": len(clips),
        "distance_km": round(dist, 2),
        "route": route,
        "bounds": bounds,
        "events": events,
        "event_total": sum(events.values()),
    }

def build_trips(clips, gap_min=TRIP_GAP_MIN):
    """Group clips per vehicle into contiguous trips by start-timestamp gap. Newest first."""
    t0 = time.time()
    tracks_before = len(_track_cache)
    by_vehicle = {}
    for c in clips:
        by_vehicle.setdefault(c.get("vehicle") or "", []).append(c)
    trips = []
    for vehicle, vclips in by_vehicle.items():
        vclips.sort(key=lambda c: c["timestamp"])
        group = []
        prev_dt = None
        for c in vclips:
            dt = datetime.datetime.strptime(c["timestamp"], "%Y-%m-%d_%H-%M-%S")
            if group and prev_dt and (dt - prev_dt).total_seconds() > gap_min * 60:
                trips.append(_make_trip(vehicle, group))
                group = []
            group.append(c)
            prev_dt = dt
        if group:
            trips.append(_make_trip(vehicle, group))
    trips.sort(key=lambda t: t["start"], reverse=True)
    if len(_track_cache) != tracks_before:
        _cache_save("track")
    _dbg(f"build_trips: {len(trips)} trips from {len(clips)} clips in {time.time()-t0:.3f}s "
         f"({len(_track_cache)-tracks_before} tracks read from disk)")
    return trips


def _derived_cached(name, cache, ttl, build):
    """Stale-while-revalidate for data derived from the clip list, with the same
    contract as clips_cached(): a request is never blocked by a build.

    The first build of either is expensive on a large library — trips reads the
    telemetry JSON of every clip with GPS data, analytics stats every camera
    file — and both were measured in the *minutes* on a NAS-backed share while
    holding the request open. They now fill their caches in the background.

    Nothing is built until the index exists: computing from the empty list of a
    cold start is what used to poison these caches with zeros for a whole TTL.
    """
    now = time.time()
    with _derived_guard:
        data = cache["data"]
        if data is not None and now - cache["t"] < ttl:
            return data
        if _lcache["data"] is None or name in _derived_running:
            return data                      # no index yet, or already building
        _derived_running.add(name)

    def work():
        try:
            result = build()
            with _derived_guard:
                cache["data"] = result
                cache["t"] = time.time()
        except Exception as e:
            print(f"[{name}]", e, flush=True)
        finally:
            with _derived_guard:
                _derived_running.discard(name)

    threading.Thread(target=work, daemon=True).start()
    return data


def _derived_expire():
    """Mark trips/analytics as stale after the clip list changed. Keeps the last
    good values so they are still served while the refresh runs."""
    with _derived_guard:
        _trips_cache["t"] = 0.0
        _analytics_cache["t"] = 0.0


def trips_cached():
    return _derived_cached("trips", _trips_cache, LIST_TTL,
                           lambda: build_trips(clips_cached())) or []


def _file_size(sr):
    """Size of a clip file, cached to disk — clips are write-once, so the value
    never changes. Saves one SMB stat per camera per clip on every analytics run."""
    hit = _size_cache.get(sr)
    if hit is not None:
        return hit
    full = resolve_media(sr)
    size = os.path.getsize(full) if full else 0
    _size_cache[sr] = size
    return size


def compute_analytics():
    t0 = time.time()
    clips = clips_cached()
    trips = trips_cached()
    sizes_before = len(_size_cache)
    by_folder = {}
    for c in clips:
        top = c["folder"].split("/")[0] if c["folder"] else "(root)"
        entry = by_folder.setdefault(top, {"folder": top, "bytes": 0, "clip_count": 0})
        entry["clip_count"] += 1
        for cam in c["cameras"]:
            entry["bytes"] += _file_size(_sr_of_cam(c["folder"], c["timestamp"], cam))
    if len(_size_cache) != sizes_before:
        _cache_save("size")
    events_by_reason = {}
    clips_by_month = {}
    for c in clips:
        if c.get("has_event") and c.get("reason"):
            events_by_reason[c["reason"]] = events_by_reason.get(c["reason"], 0) + 1
        m = c["timestamp"][:7]
        clips_by_month[m] = clips_by_month.get(m, 0) + 1
    distances = [t["distance_km"] for t in trips if t["distance_km"] > 0]
    _dbg(f"compute_analytics: {len(clips)} clips, {len(trips)} trips, storage stat'd in {time.time()-t0:.3f}s total")
    return {
        "storage": {"by_folder": sorted(by_folder.values(), key=lambda x: x["folder"])},
        "clips": counts(clips),
        "trips": {
            "total": len(trips),
            "total_distance_km": round(sum(distances), 1),
            "avg_distance_km": round(sum(distances) / len(distances), 1) if distances else 0,
            "longest_km": round(max(distances), 1) if distances else 0,
        },
        "events_by_reason": events_by_reason,
        "clips_by_month": [{"month": k, "count": v} for k, v in sorted(clips_by_month.items())],
        "pending": False,
    }

def _analytics_pending():
    """Shape the UI can render while the real numbers are still being built."""
    return {"storage": {"by_folder": []}, "clips": counts([]),
            "trips": {"total": 0, "total_distance_km": 0, "avg_distance_km": 0,
                      "longest_km": 0},
            "events_by_reason": {}, "clips_by_month": [], "pending": True}


def analytics_cached():
    return _derived_cached("analytics", _analytics_cache, ANALYTICS_TTL,
                           compute_analytics) or _analytics_pending()


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
        if ev.get("street"):
            result["street"] = ev["street"]
        if ev.get("camera") is not None:
            result["camera"] = ev["camera"]
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

def _key_for(sr, keys):
    """Find the FEK for an encrypted file (try full sr, then enc_id)."""
    if sr in keys:
        return base64.b64decode(keys[sr])
    eid = enc_id(sr)
    if eid in keys:
        return base64.b64decode(keys[eid])
    return None

def _decrypt_cam(sr, keys):
    fek = _key_for(sr, keys)
    if not fek:
        raise KeyError(f"no key for {sr}")
    pipeline.decrypt_and_cache(src_abspath(sr), cache_abspath(sr), fek, embed_key=EMBED_KEY)

def prepare_clip(cid):
    keys = keystore.load(KEYS_FILE)
    folder, ts, cams = _clip_cams(cid)
    if not cams:
        return {"ok": False, "error": "clip not found"}
    jobs = []
    for cam, sr in cams.items():
        if _is_encrypted(src_abspath(sr), sr):
            if not os.path.exists(cache_abspath(sr)) and _key_for(sr, keys):
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
    # Uses the cached list, not a fresh _scan(): this runs from the scheduler
    # every interval_seconds, and forcing a full NAS re-scan each time is what
    # made the index build crawl. A list up to LIST_TTL old only means a clip
    # gets decrypted one cycle later.
    jobs = [_sr_of_cam(c["folder"], c["timestamp"], cam)
            for c in clips_cached()
            for cam, info in c["cameras"].items() if info["state"] == "key"]
    def do(sr):
        try:
            _decrypt_cam(sr, keys)
        except Exception as e:
            print(f"[decrypt] {sr}: {e}", flush=True)
    if jobs:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(do, jobs))
    _meta_cache.clear()          # decrypting produces telemetry -> metadata and
    _track_cache.clear()         # GPS tracks must be recomputed
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
    clips = clips_cached()
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
            print(f"[thumbs] {_thumb_job['done']}/{_thumb_job['total']} thumbnails generated", flush=True)
    finally:
        invalidate()
        _thumb_job["running"] = False


def gen_all_telemetry():
    """Batch: extract SEI telemetry for all plain front-camera clips that don't have it cached yet."""
    global _tel_job
    if _tel_job.get("running"):
        return
    clips = clips_cached()
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
        _track_cache.clear()
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
        self._t0 = time.time()
        path = self.path.split("?")[0]
        _dbg(f"GET {self.path} - start")
        if path in ("/", "/index.html"):
            return self._file(os.path.join(WWW, "index.html"), "text/html",
                              {"Cache-Control": "no-cache, no-store, must-revalidate"})
        if path.startswith("/static/"):
            # Sub-paths are allowed (leaflet.css pulls images/*.png relative to
            # itself), so the traversal guard is an explicit containment check
            # rather than the basename() flattening this used to rely on.
            rel = posixpath.normpath(path[len("/static/"):]).lstrip("/")
            fp = os.path.normpath(os.path.join(WWW, rel))
            if not fp.startswith(os.path.normpath(WWW) + os.sep):
                return self._send(403, {"error": "forbidden"})
            if not os.path.isfile(fp):
                return self._send(404, {"error": "not found"})
            # Revalidate rather than cache blindly: a plain max-age would leave
            # browsers running the previous app.js for that long after an add-on
            # update. With an ETag the check costs one 304 and stays correct.
            st = os.stat(fp)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            ct = STATIC_CTYPES.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
            return self._file(fp, ct, {"ETag": etag, "Cache-Control": "no-cache"})
        if path == "/api/status":
            st = counts(clips_cached())
            st["busy"] = _busy
            st["auto_decrypt"] = AUTO_DECRYPT
            st["direct_api"] = DIRECT_API
            st["login"] = auth.status()
            st["last_api"] = _last_api
            st["thumb_job"] = _thumb_job
            st["tel_job"] = _tel_job
            st["scan_job"] = _scan_job
            # False until the first scan has produced a list — lets the UI tell
            # "still indexing" apart from "genuinely no clips on the share"
            st["ready"] = _lcache["data"] is not None
            with _derived_guard:
                st["building"] = {"trips": "trips" in _derived_running,
                                  "analytics": "analytics" in _derived_running}
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
        if path == "/api/trips":
            return self._send(200, trips_cached())
        if path == "/api/analytics":
            return self._send(200, analytics_cached())
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
        self._t0 = time.time()
        path = self.path.split("?")[0]
        _dbg(f"POST {self.path} - start")
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

    def log_message(self, fmt, *args):
        if DEBUG:
            dur = time.time() - getattr(self, "_t0", time.time())
            print(f"[http] {fmt % args} ({dur:.3f}s)", flush=True)


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
    p.add_argument("--debug", action="store_true", help="Verbose logging: request timing, scan/analytics duration, cache hit/miss counts")
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
    DEBUG = a.debug
    auth = TeslaAuth(os.path.join(DATA_DIR, "token_store.json"))
    os.makedirs(os.path.join(OUT_DIR, ".thumbs"), exist_ok=True)
    _cache_init(DATA_DIR)
    # Warm the clip list before the panel is first opened, so the very first
    # request is served from memory instead of waiting for a full NAS scan.
    _rescan_async()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Viewer :{a.port} scan={SCAN_DIR} enc={SRC_DIR} (prefix='{ENC_PREFIX}') "
          f"out={OUT_DIR} keys={KEYS_FILE} auto_decrypt={AUTO_DECRYPT} "
          f"embed={EMBED_KEY} direct_api={DIRECT_API} debug={DEBUG}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
