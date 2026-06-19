# Changelog

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
