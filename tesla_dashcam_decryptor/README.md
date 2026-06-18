# Te_FITI – Tesla Fleet Integration, Telemetry & Infotainment

Decrypts Tesla dashcam and Sentry clips encrypted since firmware **2026.20**
(`EncryptedClips`, eCryptfs format) **locally** on your Home Assistant,
extracts the **SEI telemetry** embedded in the H.264 stream (speed, gear,
steering, blinkers, autopilot, GPS) and presents everything in a
**multi-camera viewer with HUD overlay**.

## Architecture

Tesla protects each file encryption key (FEK) per account and vehicle;
the key is only delivered by `dashcam.tesla.com` after login — protected by
Akamai bot detection that blocks server-side requests. Therefore:

- **The add-on does NOT communicate with Tesla** (no OAuth, no tokens in the container).
- A **browser bookmarklet** fetches the FEKs inside *your* logged-in
  `dashcam.tesla.com` session (bypasses Akamai because it's same-origin)
  and downloads a `keys.json` file.
- The add-on decrypts locally using those keys and extracts telemetry.
- FEKs are stored persistently next to the encrypted files on the NAS
  (`.teslacam_keys.json`) — once fetched, decryption works offline.

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
| `interval_seconds` | Polling interval for new clips with available FEK |
| `auto_decrypt` | Automatically decrypt clips when a key is available |
| `enable_direct_api` | Use the Tesla Fleet API directly (requires login via the viewer) |
| `delete_originals` | Delete encrypted originals after successful decryption (default: `false`) |

## Workflow

1. Start the add-on and open the **viewer** (ingress panel "Te_FITI").
2. **Option A – Direct API (recommended):** Click *Tesla Login*, complete the
   OAuth flow once. The add-on auto-fetches FEKs using a stored refresh token.
3. **Option B – Bookmarklet fallback:** Download `pending_items.json`, open
   [dashcam.tesla.com](https://dashcam.tesla.com), run the bookmarklet,
   select the file → `keys.json` is downloaded automatically.
4. Upload `keys.json` via the viewer → decryption starts immediately.

### Bookmarklet

Create a new browser bookmark and paste this as the URL
(or use the 🔑 button in the viewer to copy it):

```
javascript:(async()=>{const pick=document.createElement('input');pick.type='file';pick.accept='application/json,.json';pick.onchange=async()=>{try{const job=JSON.parse(await pick.files[0].text());const items=job.items||job;let raw=sessionStorage.getItem('ROCP_token'),token=raw;try{const p=JSON.parse(raw);token=(typeof p==='string')?p:(p.access_token||p.token||p.accessToken||raw);}catch(e){}if(!token){alert('No Tesla token - please log in at dashcam.tesla.com first.');return;}const out=[],CH=30;for(let i=0;i<items.length;i+=CH){const r=await fetch('/api/1/decrypt/batch',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token,'Accept':'application/json'},body:JSON.stringify({items:items.slice(i,i+CH)})});if(!r.ok){alert('API error '+r.status+' at block '+i);return;}const j=await r.json();(j.results||[]).forEach(x=>{if(x.key)out.push({id:x.id,key:x.key});});}const blob=new Blob([JSON.stringify({results:out})],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='keys.json';a.click();alert('Done: '+out.length+' keys -> keys.json');}catch(e){alert('Error: '+e.message);}};pick.click();})();
```

## Privacy & Notes

- FEKs and decrypted videos stay on your own hardware.
- Intended only for **your own** vehicle recordings with your own Tesla account.
- Alternative without the add-on: disable encryption in the car
  (*Controls → Safety → Encrypt dashcam recordings*).
