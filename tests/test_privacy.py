"""Abnahme: Keine Anfrage an externe Dienste enthält Stückzahlen, Werte oder Kontonamen des Portfolios.

Alle HTTP-Anfragen der App laufen über einen gemeinsamen httpx-Client (hier abgefangen); Yahoo (yfinance)
wird auf Methodenebene abgefangen. Geprüft werden Kurse, Devisen, Historie, News/YouTube-Abruf.
"""

import os
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx

from app import context as context_mod
from app.config import Secrets
from app.jobs import tasks
from app.main import build_app
from app.prices.yahoo import YahooProvider

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "beispiel-import.zip"


def test_outgoing_requests_contain_no_portfolio_data(config, monkeypatch):
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url) + " " + request.content.decode("utf-8", "replace"))
        if request.url.host.endswith("coingecko.com") and "simple/price" in request.url.path:
            return httpx.Response(200, json={"bitcoin": {"eur": 60000.0}, "ethereum": {"eur": 3000.0}})
        if request.url.host.endswith("coingecko.com"):
            return httpx.Response(200, json={"prices": []})
        return httpx.Response(404, text="not found")

    real_make_client = context_mod.make_client
    monkeypatch.setattr(context_mod, "make_client", lambda ua, timeout=20.0: httpx.Client(
        transport=httpx.MockTransport(handler), headers=real_make_client(ua).headers))
    yahoo_args: list[str] = []

    def fake(name):
        def f(self, *args, **kwargs):
            yahoo_args.append(f"{name}{args}{kwargs}")
            return {"quotes": ({}, {}, []), "history": ([], None), "intraday": [], "info": {}}[name]
        return f

    for name in ("quotes", "history", "intraday", "info"):
        monkeypatch.setattr(YahooProvider, name, fake(name))
    cfg = replace(config, demo_mode=False, secrets=Secrets(coingecko_api_key="demo-key-xyz"))
    dst = cfg.import_dir / "beispiel.zip"
    shutil.copy(SAMPLE, dst)
    old = time.time() - 3600
    os.utime(dst, (old, old))
    app = build_app(cfg, start_scheduler=False)
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        ctx = app.state.ctx
        tasks.import_check(ctx, "test")
        tasks.refresh_prices(ctx, force=True)
        tasks.fx_ecb(ctx)
        tasks.backfill(ctx)
        from app.news.module import news_service

        news_service(ctx).run()
        c.get("/asset/BTC")  # löst ggf. Info-Abruf aus
        pf = ctx.portfolio()
        led = ctx.ledger()
    assert captured, "keine externen Anfragen abgefangen"
    assert yahoo_args, "keine Yahoo-Aufrufe abgefangen"
    forbidden = set()
    for acc in pf.all_accounts():
        forbidden |= {acc, quote(acc), quote_plus(acc)}
    for (_acc, asset), q in led.balances.items():
        s = format(q.normalize(), "f")
        if len(s.replace(".", "")) >= 3 and not pf.asset(asset).is_fiat:
            forbidden.add(s)
    for t in pf.txs:
        if t.value_eur is not None and t.value_eur >= 100:
            forbidden.add(format(t.value_eur.normalize(), "f"))
    total = sum((float(v) for v in led.holdings_by_asset().values()), 0.0)
    forbidden.add(f"{total:.2f}")
    def contains(text: str, token: str) -> bool:
        if re.fullmatch(r"[\d.]+", token):  # Zahlen nur als eigenständiges Token (nicht Teil fester Feed-IDs)
            return re.search(rf"(?<![\d.]){re.escape(token)}(?![\d.])", text) is not None
        return token in text

    leaks = [(f, r) for r in captured + yahoo_args for f in forbidden if f and contains(r, f)]
    assert not leaks, leaks[:5]
