#!/usr/bin/with-contenv bashio
set -e

HOST=$(bashio::config 'smb_host')
SHARE=$(bashio::config 'smb_share')
ENC=$(bashio::config 'enc_subpath')
DEC=$(bashio::config 'dec_subpath')
CLIPS=$(bashio::config 'clips_subpath')
if ! bashio::config.has_value 'clips_subpath'; then CLIPS="TeslaCam"; fi
INTERVAL=$(bashio::config 'interval_seconds')
DELETE=$(bashio::config 'delete_originals')
AUTO=$(bashio::config 'auto_decrypt')
DIRECT=$(bashio::config 'enable_direct_api')
KEYMODE=$(bashio::config 'key_after_decrypt')
VERS="3.0"
if bashio::config.has_value 'smb_version'; then VERS=$(bashio::config 'smb_version'); fi

export DATA_DIR=/data
MNT=/mnt/nas
mkdir -p "${MNT}"

OPTS="rw,iocharset=utf8,vers=${VERS},uid=0,gid=0,file_mode=0660,dir_mode=0770,nofail"
if bashio::config.has_value 'smb_username'; then
  # Zugangsdaten direkt uebergeben (keine Datei -> kein DAC-Lese-Problem im Container)
  OPTS="${OPTS},username=$(bashio::config 'smb_username'),password=$(bashio::config 'smb_password')"
  if bashio::config.has_value 'smb_domain'; then
    OPTS="${OPTS},domain=$(bashio::config 'smb_domain')"
  fi
else
  OPTS="${OPTS},guest"
fi

bashio::log.info "Te_FITI - Fleet Integration, Telemetry & Infotainment (Hybrid: Direkt-API + Bookmarklet)"
bashio::log.info "Mounte //${HOST}/${SHARE} (SMB ${VERS}) -> ${MNT}"
if ! mount -t cifs "//${HOST}/${SHARE}" "${MNT}" -o "${OPTS}"; then
  bashio::log.warning "Mount fehlgeschlagen - CIFS-Kernel-Meldung (dmesg):"
  dmesg 2>/dev/null | grep -iE 'cifs|smb' | tail -12 || true
  bashio::log.fatal "CIFS-Mount fehlgeschlagen. Host/Share/Zugangsdaten/SMB-Version pruefen."
  exit 1
fi

SRC="${MNT}/${ENC}"
OUT="${MNT}/${DEC}"
SCAN="${MNT}/${CLIPS}"
mkdir -p "${OUT}"
bashio::log.info "Scan  : ${SCAN}"
bashio::log.info "Quelle: ${SRC}"
bashio::log.info "Ziel  : ${OUT}   Keys: ${SRC}/.teslacam_keys.json (neben den Files)"
bashio::log.info "auto_decrypt=${AUTO} direct_api=${DIRECT} key_after_decrypt=${KEYMODE} delete_originals=${DELETE}"

FLAGS=""
[ "${DELETE}" = "true" ] && FLAGS="${FLAGS} --delete"
[ "${AUTO}" != "true" ] && FLAGS="${FLAGS} --no-auto-decrypt"
[ "${DIRECT}" != "true" ] && FLAGS="${FLAGS} --no-direct-api"
[ "${KEYMODE}" = "embed" ] && FLAGS="${FLAGS} --embed-key"

trap 'umount "${MNT}" 2>/dev/null || true' EXIT INT TERM

exec python3 /app/server.py --src "${SRC}" --out "${OUT}" --scan "${SCAN}" --port 8099 --interval "${INTERVAL}" ${FLAGS}
