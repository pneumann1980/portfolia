"""Gemeinsame Fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import Config, Secrets
from app.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "app.sqlite")
    d.migrate()
    return d


@pytest.fixture
def config(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    imp = tmp_path / "import"
    imp.mkdir(parents=True, exist_ok=True)
    return Config(data_dir=data, import_dir=imp, demo_mode=True, scheduler_enabled=False, startup_jobs=False,
                  log_format="text", secrets=Secrets())


@pytest.fixture(autouse=True)
def _no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.endswith("_API_KEY") or k.startswith(("AUTH_", "PORTFOLIA_MASTER_KEY", "PORTFOLIA_DS_")):
            monkeypatch.delenv(k, raising=False)
