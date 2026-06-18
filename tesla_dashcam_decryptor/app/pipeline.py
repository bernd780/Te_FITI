"""
Entschluesselt Dateien, fuer die bereits ein FEK im Key-Store vorliegt.
KEIN Tesla-Kontakt. Keys werden hier NIE geloescht (liegen im keystore).

Pro Datei:
  1) eCryptfs lokal entschluesseln (AES-128-CBC) + ftyp-Pruefung
  2) optional FEK ins entschluesselte MP4 einbetten (uuid-Box, Spieler ignorieren sie)
  3) SEI-Telemetrie -> <name>.telemetry.json
  4) optional verschluesseltes Original loeschen (Key bleibt im Store!)
"""
import os, json, base64, struct, subprocess
from ecryptfs import EcryptfsFile
from telemetry import extract_telemetry
import keybridge


def make_thumbnail(src_mp4, out_jpg, seek=1.0, width=240):
    """Einen Frame aus einer (entschluesselten/plain) mp4 als JPG -> out_jpg (ffmpeg)."""
    os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
    tmp = out_jpg + ".part"
    cmd = ["ffmpeg", "-nostdin", "-y", "-ss", f"{max(0.0, seek):.2f}", "-i", src_mp4,
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", tmp]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out_jpg)
            return True
        print(f"[thumb] ffmpeg rc={r.returncode} src={src_mp4} err={r.stderr[-300:]!r}", flush=True)
    except FileNotFoundError:
        print("[thumb] ffmpeg NICHT gefunden (nicht installiert?)", flush=True)
    except Exception as e:
        print(f"[thumb] ffmpeg EXC {e} src={src_mp4}", flush=True)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False

# feste UUID (16 B) fuer die eingebettete-FEK-Box
_FEK_UUID = bytes.fromhex("54e5d0c0da5c4f1e9b3a7c0011223344")
_FEK_MAGIC = b"TDCFEK01"


def embed_fek(mp4: bytes, fek: bytes) -> bytes:
    payload = _FEK_MAGIC + fek
    box = struct.pack(">I", 8 + 16 + len(payload)) + b"uuid" + _FEK_UUID + payload
    return mp4 + box


def _write_tel(mp4: bytes, tel_path: str, source: str):
    tel = extract_telemetry(mp4)
    tel["source"] = source
    os.makedirs(os.path.dirname(tel_path) or ".", exist_ok=True)
    json.dump(tel, open(tel_path, "w", encoding="utf-8"), separators=(",", ":"))


def decrypt_and_cache(enc_path, out_path, fek, embed_key=False, with_telemetry=True):
    """Entschluesselt EINE Datei atomar -> out_path (+ optional Telemetrie-JSON)."""
    data = open(enc_path, "rb").read()
    plain = EcryptfsFile(data).decrypt(fek)
    is_mp4 = enc_path.lower().endswith(".mp4")
    if is_mp4 and plain[4:8] != b"ftyp":
        raise ValueError("falscher FEK (kein ftyp)")
    if is_mp4 and embed_key:
        plain = embed_fek(plain, fek)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(plain)
    os.replace(tmp, out_path)
    if with_telemetry and is_mp4:
        try:
            _write_tel(plain, os.path.splitext(out_path)[0] + ".telemetry.json",
                       os.path.basename(out_path))
        except Exception:
            pass
    return True


def telemetry_for_plain(src_path, tel_path):
    """Telemetrie aus einer UNverschluesselten mp4 ziehen (kein Decrypt, nur lesen)."""
    if os.path.exists(tel_path):
        return
    data = open(src_path, "rb").read()
    _write_tel(data, tel_path, os.path.basename(src_path))


def decrypt_pending(src_dir, out_dir, keys, delete_originals=False,
                    embed_key=False, log=print):
    cand = keybridge.files_needing_decrypt(src_dir, out_dir, keys)
    done = errs = 0
    for src_path in cand:
        cid = keybridge.clip_id(src_dir, src_path)
        out_path = os.path.join(out_dir, cid)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        try:
            data = open(src_path, "rb").read()
            fek = base64.b64decode(keys[cid])
            plain = EcryptfsFile(data).decrypt(fek)
        except Exception as e:
            log(f"  [fehler] {cid}: {e}"); errs += 1; continue
        is_mp4 = cid.lower().endswith(".mp4")
        if is_mp4 and plain[4:8] != b"ftyp":
            log(f"  [fehler] {cid}: falscher FEK (kein ftyp)"); errs += 1; continue
        if is_mp4 and embed_key:
            plain = embed_fek(plain, fek)
        tmp = out_path + ".part"
        with open(tmp, "wb") as f:
            f.write(plain)
        os.replace(tmp, out_path)
        done += 1
        if is_mp4:
            try:
                tel = extract_telemetry(plain)
                tel["source"] = os.path.basename(cid)
                json.dump(tel, open(os.path.splitext(out_path)[0] + ".telemetry.json",
                                    "w", encoding="utf-8"), separators=(",", ":"))
            except Exception as e:
                log(f"  [warn] Telemetrie {cid}: {e}")
        if delete_originals:   # Key bleibt im Store erhalten
            try:
                os.remove(src_path)
            except OSError as e:
                log(f"  [warn] loeschen {cid}: {e}")
    return {"need_decrypt": len(cand), "decrypted": done, "errors": errs}
