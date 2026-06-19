# Te_FITI – Tesla Fleet Integration, Telemetry & Infotainment

A **multi-camera dashcam viewer** for Home Assistant that plays Tesla
Dashcam and Sentry clips directly from your NAS — with a live **HUD overlay**
(speed, gear, steering, gas, brake, blinkers, autopilot) extracted from
in-stream telemetry, a **GPS map** and an **event info panel**.

**Encryption is optional.** If your Tesla encrypts clips (firmware 2026.20+,
`EncryptedClips`, eCryptfs format), Te_FITI can decrypt them — either
temporarily for viewing or permanently saved to your NAS. This requires a
**one-time login to your Tesla account** to fetch the per-file encryption
keys (FEKs). After that first contact the keys are stored locally and
decryption works fully offline, with no further Tesla communication needed.

If your car does not encrypt clips (or you disable it in
*Controls → Safety → Encrypt dashcam recordings*), Te_FITI works as a
pure local viewer without any Tesla account interaction at all.

## Features

- **6-camera grid player** — front, rear, left, right, pillar L/R with
  synchronised playback, seek and speed control
- **Telemetry HUD** — speed, gear, steering wheel, accelerator bar, brake
  indicator, blinkers, autopilot status (extracted from H.264 SEI NALs)
- **GPS map** — live track from telemetry or single-point from event.json
- **"Mehr für Nerds" panel** — raw telemetry values + event metadata
  (trigger reason, location, camera)
- **Clip browser** — grouped by source folder, searchable, filterable by
  driving telemetry / event, and by GPS area (marker map with rectangle
  selection)
- **Thumbnail grid** — auto-generated or from Tesla's thumb.png
- **Per-camera download** and full-clip ZIP export
- **Batch operations** — bulk key fetch, bulk decrypt, bulk telemetry
  extraction, bulk thumbnail generation

## How encryption works

Tesla protects each file with an individual encryption key (FEK) bound to
your account and vehicle. The key can only be retrieved from
`dashcam.tesla.com` after authentication.

Te_FITI supports two ways to obtain the keys:

1. **Direct API (recommended):** Log in once via the viewer's 🔑 panel.
   Te_FITI stores a refresh token and fetches keys automatically for all
   new clips — one-time setup, then hands-off.
2. **Browser bookmarklet (fallback):** If the Direct API is blocked, a
   bookmarklet runs inside your logged-in `dashcam.tesla.com` session to
   download the keys as a JSON file, which you upload to the viewer.

Once fetched, FEKs are stored persistently next to the encrypted files on
your NAS (`.teslacam_keys.json`). From that point on, decryption is **fully
local and offline** — no further contact with Tesla is needed.

## Installation

1. **Settings → Add-ons → Add-on Store**
2. Top right **⋮ → Repositories**
3. Add: `https://github.com/bernd780/Te_FITI`
4. Reload the store → install **Te_FITI**

## Configuration

| Option | Description |
|---|---|
| `smb_host` | IP address of your NAS |
| `smb_share` | SMB share name (e.g. `Tesla_Video`) |
| `enc_subpath` | Sub-path to encrypted clips (e.g. `TeslaCam/EncryptedClips`) |
| `dec_subpath` | Output folder for decrypted clips (e.g. `decrypted`) |
| `clips_subpath` | Root of the full TeslaCam tree for the viewer (e.g. `TeslaCam`) |
| `smb_username` | SMB username |
| `smb_password` | SMB password |
| `smb_version` | SMB protocol version (default: `3.0`) |
| `interval_seconds` | Polling interval for new clips (default: `300`) |
| `auto_decrypt` | Automatically decrypt clips when a key is available |
| `enable_direct_api` | Use the Tesla API directly (requires one-time login) |
| `key_after_decrypt` | What to do with the FEK after decryption: `hidden` (default) or `embed` |
| `delete_originals` | Delete encrypted originals after successful decryption |

## Privacy

- All keys and decrypted videos stay on your own hardware.
- The only external communication is the one-time key fetch from Tesla
  (and map tiles from CartoDB/OpenStreetMap when using the GPS map).
- Intended only for your own vehicle recordings with your own Tesla account.
