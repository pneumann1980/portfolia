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

if [ "$(id -u)" = "0" ]; then
  fix_owner "$DATA_DIR"
  if [ -n "$EXPORT_DIR" ]; then
    fix_owner "$EXPORT_DIR"
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all --bounding-set=-all -- "$@"
fi

exec "$@"
