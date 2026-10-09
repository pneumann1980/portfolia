"""Portfolia – self-hosted Portfolio-Dashboard (nur lesend)."""

import os

__version__ = "0.23.2"
APP_NAME = "Portfolia"
# Commit des Images (CI setzt PORTFOLIA_REVISION beim Bauen) – belegt, welcher Stand im Container läuft
REVISION = "".join(c for c in os.environ.get("PORTFOLIA_REVISION", "") if c in "0123456789abcdef")[:40]
