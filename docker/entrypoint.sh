#!/bin/sh
# Startet Portfolia als Nicht-Root-Benutzer (PUID/PGID, Unraid-Standard 99:100).
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"
EXPORT_DIR="${EXPORT_DIR:-}"

mkdir -p "$DATA_DIR"
if [ -n "$EXPORT_DIR" ]; then
  mkdir -p "$EXPORT_DIR"
fi

# Besitzrechte nur anpassen, wo nötig (z. B. nach PUID-Wechsel oder Dateien aus `docker exec` als root)
fix_owner() {
  if [ "$(stat -c %u "$1")" != "$PUID" ] || [ "$(stat -c %g "$1")" != "$PGID" ]; then
    echo "portfolia: setze Besitzer von $1 auf $PUID:$PGID" >&2
    chown -R "$PUID:$PGID" "$1"
  else
    find "$1" -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  fi
}

# Master-Key-Dateien (verschlüsselte API-Keys) gehören oft root, z. B. auf dem Unraid-USB-Stick (nur root-lesbar):
# als root lesen und nur für den App-Benutzer lesbar bereitstellen – bevorzugt im RAM (/dev/shm), damit keine Kopie
# im Container-Dateisystem landet. Der Inhalt wird nie ausgegeben; fehlt die Datei, bleibt die Variable unverändert.
provide_key() {
  var="$1"
  eval "src=\${$var:-}"
  [ -n "$src" ] || return 0
  dir=/run/portfolia
  if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    dir=/dev/shm/portfolia
  fi
  rm -f "/run/portfolia/$2" "/dev/shm/portfolia/$2"
  if [ -f "$src" ] && [ -r "$src" ]; then
    install -d -m 0700 -o "$PUID" -g "$PGID" "$dir"
    install -m 0400 -o "$PUID" -g "$PGID" "$src" "$dir/$2"
    export "$var=$dir/$2"
  fi
}

if [ "$(id -u)" = "0" ]; then
  provide_key PORTFOLIA_MASTER_KEY_FILE master.key
  provide_key PORTFOLIA_MASTER_KEY_OLD_FILE master-old.key
  fix_owner "$DATA_DIR"
  if [ -n "$EXPORT_DIR" ]; then
    fix_owner "$EXPORT_DIR"
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all --bounding-set=-all -- "$@"
fi

exec "$@"
