#!/usr/bin/env python3
"""Docker-HEALTHCHECK: GET /healthz (öffentlich, auch bei Basic-Auth)."""

import os
import sys
import urllib.request

port = os.environ.get("PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:  # noqa: S310
        sys.exit(0 if r.status == 200 else 1)
except Exception:  # noqa: BLE001
    sys.exit(1)
