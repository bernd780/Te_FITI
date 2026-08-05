# Changelog

## 0.7.8
- **Fix: `delete_originals` never did anything.** The option was read from the config, passed to `run.sh` and printed in the log, but `server.py` only ever assigned `DELETE` and never used it — the sole code path honouring it lives in `pipeline.decrypt_pending()`, which is called exclusively from the unused legacy CLI `main.py`. Encrypted originals were therefore kept forever no matter what the option said. It now works, with guards: an original is removed only after the decrypt returned without raising *and* the decrypted output exists and is non-empty
- **New "Decrypt everything" button** in the Keys panel. `POST /api/decrypt` existed but no UI element ever called it, so the only way to decrypt was one clip at a time from the player. The button shows a progress bar, reports failures, and when `delete_originals` is on it names the consequence and asks for confirmation first — the originals cannot be recovered afterwards
- Fetching missing keys no longer scans the whole tree: candidates come from the index, where a camera marked `locked` already means encrypted-and-keyless. The old path globbed everything and read an 8 KB header from every media file absent from the key store — which is every *plain* clip, since those are never in it. On a library with 8,596 plain files that was ~8,900 SMB reads to find 273 keys, repeated on every scheduler cycle and every button press
- The decrypt run can be **cancelled** (`POST /api/decrypt/cancel`): files already in flight finish, so a clip is never left half-written, and nothing further is started. The result line reports how much was done before stopping
- **Fix: events were counted per one-minute segment instead of per event.** Tesla saves the rolling buffer as one-minute segments in a single folder sharing one `event.json`, so every event counted once per minute it recorded. On a real library that turned 525 events into 2,154 — object detection alone read 1,558 instead of 437. Both the Analytics chart and the trip card's event counts now count distinct event folders
- Analytics no longer mixes denominators for trips: `total` counts every cluster of clips (mostly parked Sentry sessions), while the distance figures only cover the ones that moved. The tile now reads "410 (50 driven)" and the average is labelled "Avg driven trip", instead of inviting the reading 410 × 6 km

## 0.7.7
- New **filter by event reason**. Two ways in: a dropdown in the Clips filter bar listing the reasons actually present with their counts, or clicking a row in the Analytics *Events by reason* chart — which filters the list and jumps straight to it, like the trip card's "View clips"
- The active reason shows as a removable chip next to the area and trip chips, and combines with search, the Driving/Event/Honk checkboxes, the map area filter and the trip filter, all reflected in the live result count
- Clicking a reason in Analytics clears an active trip filter first: intersecting the two would usually produce an empty list for no visible reason

## 0.7.6
- Fix: the Analytics trip statistics read `0 trips` even when the Map showed plenty. `compute_analytics()` used the non-blocking `trips_cached()`, which answers empty while its own build is running, and that empty answer was then frozen into the analytics cache for a full TTL. Analytics already runs on a background thread, so it now computes the trips itself when the cache is not current
- Fix: Tesla appends the measured magnitude to some trigger reasons (`sentry_aware_accel_0.469145`). Because the number was part of the string, every measurement counted as its own category — a real library showed **14 near-identical rows** of 2–4 events each in the events-by-reason chart instead of one bucket. The magnitude is now split off and reported separately as `reason_value`
- The same suffix meant the UI's label lookup never matched: `REASON_LABELS` is keyed on the bare `sentry_aware_accel`, so the raw string was shown everywhere. Labels now resolve, with the measured value appended, and the labels for door-handle, emergency-braking and the two other manual-save reasons were filled in

## 0.7.5
- Fix: `/api/trips` and `/api/analytics` blocked the request while they were built. On a 3,431-clip library the first `/api/trips` ran for **over 15 minutes** — it reads the telemetry JSON of every clip with GPS data — and `/api/analytics` for **122 seconds**, since it stats all 13,641 camera files. Long enough that the Map and Analytics tabs simply timed out. Both now build in the background with the same stale-while-revalidate contract the clip list already had, and `/api/status` reports `building.trips` / `building.analytics` so the UI reloads them the moment they are ready
- Fix: both caches could be built from the *empty* clip list of a cold start and then serve that for a whole TTL — which is why Analytics showed all zeros right after a first index. Nothing derived is computed until the index exists, and finishing a scan now expires them. `invalidate()` expires analytics too; it previously only expired trips
- A failed build no longer leaves the builder marked as running, so a transient NAS error cannot wedge trips or analytics until the add-on is restarted
- Analytics answers with `pending: true` while it is being built, and the tab explains what is happening instead of showing zeros

## 0.7.4
- The first index now reads **one file per clip instead of six**. A clip's six camera files are written by the car in a single pass and always share an encryption state, so probing one and applying the answer to the rest cuts NAS round trips by 6x — which is what actually decides how long indexing takes
- Reverts the parallel probing added in 0.7.3 to a single reader. Measured on a real SMB share it made things *worse*, not better: 8 parallel readers managed 5.0 files/s where a single one managed 8.3 — the mount serialises the requests and the extra concurrency only adds contention. (The 0.7.3 benchmark that suggested an 8x speedup used a simulated delay, which parallelises perfectly and real CIFS round trips do not.) `ENC_WORKERS` remains available for shares that do benefit, defaulting to 1
- A file that cannot be read is no longer cached as "not encrypted". The probe falls through to the clip's other cameras, and if none can be read the clip is simply retried on the next scan

## 0.7.3
- The eCryptfs classification of new files now runs across 8 threads instead of one. It is pure network latency — 28 bytes per file — so concurrency, not bandwidth, decides how long a first index takes. Measured under simulated NAS latency: 8x faster, near-linear; on a 13,625-file share this turns ~28 minutes into a few
- Fix (introduced in 0.7.2): static assets were served with `Cache-Control: max-age=86400`, so after an add-on update browsers kept running the *previous* `app.js` for up to a day. They now revalidate via `ETag`/`304`, and the asset URLs carry a `?v=` matching the add-on version so already-cached copies are bypassed immediately
- The map is no longer built during page load — it is created the first time the Map tab is opened. Loading the viewer now issues **no external requests at all**; map tiles are fetched only once you look at the map
- `api/login/url` is no longer awaited during start-up; it only fills the login link in the Keys panel and was delaying the status line and the clip list
- Removed `/api/all_gps`, dead since 0.6.0 — the map reads clip positions straight from `/api/clips`
- README: the add-on options are now documented in full, grouped by what they affect (NAS connection, decryption, behaviour), each with its default, plus a note on what the first start actually does

## 0.7.2
- Fix (regression in 0.7.1): the scheduler and the batch jobs called `_scan()` directly, bypassing the single-flight guard, so with `auto_decrypt` on a **second** full scan started every `interval_seconds` on top of the one already running. Several scans then competed for the same SMB mount and overwrote each other's progress — measured on a 13,625-file share, the index build dropped from ~7 files/s to ~1.25 files/s. They now use the cached list, and `_scan()` additionally serialises itself
- The eCryptfs classification is checkpointed to `.enc_cache.json` every 500 files. Previously the cache was only written when a scan finished, so restarting the add-on during a long first index threw away all of the work
- Leaflet 1.9.4 (JS, CSS and its images) now ships inside the add-on image instead of being pulled from `unpkg.com`. It was a render-blocking `<script>` in `<head>`, so an HA host without internet — or a slow CDN — stalled the whole viewer before any of its own code ran. It is now also `defer`red, and still executes before `app.js`
- Static files are served with a `Cache-Control` header, correct content types for images, and support sub-paths (`static/images/…`) with an explicit path-traversal check

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
