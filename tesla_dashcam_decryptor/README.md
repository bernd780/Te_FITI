# Tesla Dashcam Decryptor

Entschlüsselt die seit Firmware **2026.20** verschlüsselten Tesla-Dashcam-/Sentry-Clips
(`EncryptedClips`, eCryptfs) **lokal** auf deinem Home Assistant, extrahiert die im
Video eingebettete **SEI-Telemetrie** (Speed, Gang, Lenkung, Blinker, Autopilot, GPS)
und zeigt alles in einem **Multikamera-Viewer mit HUD**.

## Architektur (wichtig)

Tesla schützt den Datei-Schlüssel (FEK) konto-/fahrzeuggebunden; ihn liefert nur
`dashcam.tesla.com` nach Login — geschützt durch Akamai-Bot-Erkennung, die
Server-seitige Aufrufe blockt. Deshalb:

- **Das Add-on spricht NICHT mit Tesla** (kein OAuth, keine Tokens im Container).
- Ein **Browser-Bookmarklet** holt die FEKs in *deiner* eingeloggten
  `dashcam.tesla.com`-Session (umgeht Akamai, weil same-origin) und lädt eine
  `keys.json` herunter.
- Das Add-on entschlüsselt damit lokal und extrahiert die Telemetrie.

## Konfiguration

| Option | Bedeutung |
|---|---|
| `src_dir` | Ordner mit den verschlüsselten Clips (z. B. NAS-Mount unter `/media/...`) |
| `out_dir` | Zielordner für entschlüsselte Clips + `*.telemetry.json` |
| `interval_seconds` | Intervall, in dem nach neuen Clips mit vorliegendem FEK gesucht wird |
| `delete_originals` | Verschlüsselte Originale nach Erfolg löschen (Default: `false`) |

NAS einbinden: **Einstellungen → System → Speicher → Netzwerkspeicher hinzufügen**,
dann `src_dir`/`out_dir` auf die Mountpfade unter `/media` bzw. `/share` setzen.

## Ablauf

1. Add-on starten, **Viewer öffnen** (Ingress, „Dashcam" in der Seitenleiste).
2. **1 · `pending_items.json` herunterladen** (Liste offener Clips, nur Krypto-Header).
3. **2 · FEKs holen:** [dashcam.tesla.com](https://dashcam.tesla.com) öffnen, einloggen,
   Bookmarklet ausführen → `pending_items.json` wählen → es lädt `keys.json` herunter.
4. **3 · `keys.json` hochladen** → Entschlüsselung startet automatisch.

### Bookmarklet

Neues Lesezeichen anlegen, als Adresse einfügen (oder im Viewer auf „🔑 Schlüssel"):

```
javascript:(async()=>{const pick=document.createElement('input');pick.type='file';pick.accept='application/json,.json';pick.onchange=async()=>{try{const job=JSON.parse(await pick.files[0].text());const items=job.items||job;let raw=sessionStorage.getItem('ROCP_token'),token=raw;try{const p=JSON.parse(raw);token=(typeof p==='string')?p:(p.access_token||p.token||p.accessToken||raw);}catch(e){}if(!token){alert('Kein Tesla-Token - erst auf dashcam.tesla.com einloggen.');return;}const out=[],CH=50;for(let i=0;i<items.length;i+=CH){const r=await fetch('/api/1/decrypt/batch',{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+token,'Accept':'application/json'},body:JSON.stringify({items:items.slice(i,i+CH)})});if(!r.ok){alert('API-Fehler '+r.status+' bei Block '+i);return;}const j=await r.json();(j.results||[]).forEach(x=>{if(x.key)out.push({id:x.id,key:x.key});});console.log('FEKs',out.length,'/',items.length);}const blob=new Blob([JSON.stringify({results:out})],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='keys.json';a.click();alert('Fertig: '+out.length+' Schluessel -> keys.json');}catch(e){alert('Fehler: '+e.message);}};pick.click();})();
```

## Datenschutz / Hinweise

- FEKs und entschlüsselte Videos bleiben auf deiner Hardware.
- Nur für **eigene** Fahrzeuge/Aufnahmen mit eigenem Tesla-Konto gedacht.
- Alternative ohne Add-on: Verschlüsselung im Auto abschalten
  (*Bedienelemente → Sicherheit → Dashcam-Aufnahmen verschlüsseln*).
