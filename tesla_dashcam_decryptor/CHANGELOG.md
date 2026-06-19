# Changelog

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
