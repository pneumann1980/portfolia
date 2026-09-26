#!/bin/sh
# Startet Portfolia als Nicht-Root-Benutzer (PUID/PGID, Unraid-Standard 99:100).
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
DATA_DIR="${DATA_DIR:-/data}"

mkdir -p "$DATA_DIR"

if [ "$(id -u)" = "0" ]; then
  # Besitzrechte nur anpassen, wo nötig (z. B. nach PUID-Wechsel oder Dateien aus `docker exec` als root)
  if [ "$(stat -c %u "$DATA_DIR")" != "$PUID" ] || [ "$(stat -c %g "$DATA_DIR")" != "$PGID" ]; then
    echo "portfolia: setze Besitzer von $DATA_DIR auf $PUID:$PGID" >&2
    chown -R "$PUID:$PGID" "$DATA_DIR"
  else
    find "$DATA_DIR" -xdev \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  fi
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all --bounding-set=-all -- "$@"
fi

exec "$@"
