# Changelog

## 0.7.1
- Fix: loading clips could take minutes (or appear to hang entirely) on installs with a lot of footage on a network share. `/api/status` and `/api/clips` re-scanned the whole TeslaCam tree from scratch every 15 seconds, and each scan opened *every* MP4 over SMB to read its 28-byte eCryptfs header — thousands of network round trips per request, with all other requests queued behind the scan lock
- The scan now walks each directory once with `os.scandir` (one SMB round trip per folder) instead of issuing an `os.path.exists()` per camera file
- New persistent caches in `/data`, all keyed by write-once paths so they stay valid across restarts: `.enc_cache.json` (eCryptfs header per file — a file is now read at most once, ever), `.track_cache.json` (GPS tracks, so `/api/trips` no longer re-reads every telemetry JSON off the NAS on each call), `.size_cache.json` (clip sizes for the Analytics storage stats). Stale entries are pruned when files disappear
- Requests are never blocked by a scan any more: a stale clip list is served immediately while it refreshes in the background, and the list is warmed at start-up rather than on the first page load
- `/api/trips` is now cached like `/api/clips` and `/api/analytics` instead of being rebuilt on every call
- Clip-list cache lifetime raised from 15 s to 120 s, analytics from 60 s to 300 s — with background refresh, a short TTL only caused redundant scans
- New progress bar under the header while the clip index is being built, with the current phase (indexing folders → reading clip states → reading telemetry & events), a count and a percentage. The folder walk shows as indeterminate because it cannot know its total until it has finished. The clip list reloads by itself the moment the scan completes
- The batch thumbnail and telemetry jobs now show the same progress bar instead of a bare `12/340…` counter
- `/api/status` reports the new `scan_job` and `ready` fields; `ready` lets the UI say "still indexing" instead of showing an empty list as if the share had no clips

## 0.7.0
- The clip list is the default view again (new **Clips** tab), with **Map** and **Analytics** as secondary tabs — the map is no longer the landing page
- Map area selection: draw a rectangle, see how many clips fall inside, and jump straight to the filtered list ("View list"). The active area filter shows as a removable chip in the Clips view
- All filters combine: an area filter from the map plus the Driving / Event / Honk checkboxes narrow the list together, with a live result count
- Trip card's "View clips" now filters the Clips list to that trip (removable chip) instead of a separate panel
- Removed the slide-out browser panel (its Events/Trips/All-Clips sub-tabs are covered by the Clips list filters and the map's trip card)

## 0.6.2
- New `debug_logging` option: verbose add-on log with per-request timing, `_scan`/`build_trips`/`compute_analytics` duration, and metadata-cache hit/miss counts (off by default; the previous silent `log_message` made incidents like 0.6.0's hang invisible in the log)
- The web UI now flags "still loading… taking longer than expected" instead of sitting silently if the initial load takes more than 8 seconds

## 0.6.1
- Fix: the 0.6.0 metadata-cache upgrade (for GPS "track" data) forced a synchronous full re-scan of every historical clip's telemetry/event JSON on the first request after updating, which could hang the UI ("loading" forever) on installs with a lot of accumulated footage on a network share. GPS tracks for trip routes are now read on demand inside `/api/trips` instead of being persisted in the shared clip cache, so `/api/status`/`/api/clips` are unaffected and existing caches stay valid across the update.

## 0.6.0
- New map-centric landing page with trip routes, event markers, and a slide-out clip browser panel (replaces the small GPS filter panel; rectangle-select-to-filter preserved on the new map)
- Trips: clips are grouped into drives by a 20-minute gap-threshold algorithm, with distance (GPS haversine, no odometer data available) and event counts; floating trip card with prev/next navigation
- New Analytics tab: storage usage per folder/vehicle, trip/clip statistics, events-by-reason and clips-by-month charts (dependency-free inline SVG/CSS, no new CDN dependency)
- Light/dark theme toggle (persisted, respects OS preference on first load) alongside the existing dark theme
- New inline SVG icon set for map markers, tab navigation, and the theme toggle (existing emoji elsewhere unchanged)
- index.html split into index.html + app.js + style.css, served via the existing /static/ route
- New GET /api/trips and GET /api/analytics endpoints

## 0.4.23
- All UI text and log messages translated to English
- HACS compatible (repository.json, hacs.json)

## 0.4.22
- README rewritten: viewer-first description with optional decryption

## 0.4.21
- Sidebar filters: "Driving" (clips with SEI telemetry) and "Event" (clips with event.json) replace the old "locked" filter

## 0.4.20
- "Nerd info" panel shows event metadata (trigger reason, location, camera) for all clips with event.json — even without SEI telemetry

## 0.4.19
- GPS map shown for clips with event.json location (even without driving telemetry)
- /api/event returns full event data (GPS, reason, city, street, camera)

## 0.4.18
- Autopilot indicator hidden when inactive

## 0.4.17
- Brake indicator only red when active (replaced emoji with CSS-styleable symbol)

## 0.4.16
- index.html served with Cache-Control: no-cache (fixes stale UI after updates)

## 0.4.15
- Heatmap replaced with clickable marker dots per clip
- New green accelerator bar in HUD
- Brake indicator dimmed when inactive

## 0.4.14
- Persistent metadata cache (.meta_cache.json) — much faster startup after first scan
- leaflet-draw replaced with native rectangle selection (fixes HA Ingress CSP block)

## 0.4.13
- Fixed: plain (unencrypted) clips never got SEI telemetry extracted automatically — `/api/prepare` (which runs telemetry extraction) was only triggered when a clip had an encrypted camera. Now triggered transparently in the background when opening a plain clip without cached telemetry.
- New: "🛰️ Extract all telemetry" batch button (Keys panel, step E) to backfill telemetry for all existing plain clips that are missing it.
- New: `POST /api/telemetry_all` endpoint + `tel_job` progress in `/api/status`.

## 0.4.12
- Loading indicator in sidebar ("⏳ Loading clips…") and header while the clip list fetches, so large libraries (1000s of clips) don't look stuck/empty

## 0.4.11
- Fixed: duplicate `id="tools"` element (stray leftover markup) broke the Keys/Decryption panel lookup

## 0.4.7
- GPS heatmap filter in sidebar (Leaflet + leaflet.heat + leaflet-draw)
- GPS coordinates from `event.json` as fallback for clips without telemetry
- Heatmap hidden by default, toggled via 🗺️ button in header
- CartoDB dark tiles (no Referer restriction, works inside HA ingress)

## 0.4.6
- Batch thumbnail generation for all clips with telemetry or event data
- Player auto-seeks to event timestamp from `event.json`
- `/api/event?id=` endpoint returns seek offset in seconds
- Fixed: event seek position lost on Play click (initialSeek variable)
- Fixed: ffmpeg `-f image2` flag for `.part` temp file format detection

## 0.4.5
- PKCE OAuth with refresh token for automated key re-fetch
- Direct API: batch size fixed to 30 (Tesla API maximum)
- Fixed: login URL populated dynamically via `/api/login/url`
- Fixed: HTML element IDs for login link and textarea

## 0.4.0
- Unified clip viewer: all clips (encrypted, plain, decrypted) in one list
- On-demand decryption via POST `/api/prepare`
- Telemetry HUD (gear, speed, steering, blinkers, brake, autopilot)
- Persistent FEK keystore (`.teslacam_keys.json` on NAS, never deleted)
- Thumbnail generation with ffmpeg at event timestamp

## 0.1.0
- Initial release: browser-bridge architecture
- Local eCryptfs decryption (AES-128-CBC) + SEI telemetry extraction
- FEKs via browser bookmarklet (dashcam.tesla.com), no Tesla login in container
- Ingress viewer with multi-camera layout + telemetry HUD
