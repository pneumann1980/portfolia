"""``python -m app`` startet den Webserver (Uvicorn, ein Prozess)."""

from __future__ import annotations

import os
import sys

import uvicorn

from app.config import Config


def main() -> None:
    if len(sys.argv) > 1:
        from app.cli import main as cli_main

        cli_main(sys.argv[1:])
        return
    cfg = Config.from_env()
    uvicorn.run(
        "app.main:app_factory",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - Container-intern, Veröffentlichung steuert Docker
        port=cfg.port,
        log_level=cfg.log_level.lower(),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        workers=1,
        timeout_keep_alive=15,
        log_config=None,  # Uvicorn-Logs über das JSON-Logging der App
    )


if __name__ == "__main__":
    main()
