"""Vollständigkeitscockpit (Financial Integrity, Phase 9) – nur synthetische Daten.

Abgedeckt: getrennte Dimensionen (keine Gesamtnote), Zustände aus den Fachsichten, Fallanalyse mit Belegquellen,
externe KI-Dienste standardmäßig aus und nie für Finanzdaten, Seiten nur lesend.
"""

# ruff: noqa: F811
from __future__ import annotations

import ast
from pathlib import Path

from app.diagnosis import cockpit as CP
from app.settings_store import DEFAULTS
from tests.test_datasources import batch_of, client, create_source, fake, sync  # noqa: F401
from tests.test_review_workflow import snapshot, unknown

APP = Path(__file__).resolve().parent.parent / "app"


def ctx(c):
    return c.app.state.ctx


def test_dimensions_are_separate_and_reflect_review_state(client, fake):
    c = client
    cp = CP.build(ctx(c))
    keys = [d.key for d in cp.dims]
    assert keys == ["pruefung", "abdeckung", "bestand", "bewertung", "gruppen", "anschaffung", "einordnung",
                    "diagnose"]
    assert next(d for d in cp.dims if d.key == "pruefung").state == "ok"
    sid = create_source(c)
    fake.events = [unknown("kraken:U1", "2024-06-03T10:00:00")]
    batch_of(sync(c, sid))
    cp = CP.build(ctx(c))
    pr = next(d for d in cp.dims if d.key == "pruefung")
    assert pr.state == "handlung" and pr.figure == "1 offen" and pr.link == "/journal/pruefung"
    # keine Gesamtnote: andere Dimensionen bleiben unberührt
    assert next(d for d in cp.dims if d.key == "gruppen").state == "ok"


def test_case_analysis_lists_evidence_sources(client, fake):
    c = client
    sid = create_source(c)
    fake.events = [unknown("kraken:U1", "2024-06-03T10:00:00")]
    batch_of(sync(c, sid))
    fall = CP.case(ctx(c), "Kraken", "ZZQX")
    assert len(fall.review) == 1 and fall.review[0].event == "kraken:U1"
    assert any("Schnittstelle" in s for s in fall.sources) and any("Exportdatei" in s for s in fall.sources)
    assert any("Kontoauszug" in s for s in fall.sources)
    before = snapshot(c)
    page = c.get("/quality/cockpit/fall?pos=Kraken|ZZQX").text
    assert "Mögliche Belegquellen" in page and "kraken:U1" in page
    assert c.get("/quality/cockpit/fall", follow_redirects=False).status_code == 303
    assert "Vollständigkeitscockpit" in c.get("/quality/cockpit").text
    assert snapshot(c) == before


def test_external_ai_is_off_by_default_and_never_used_for_financial_data(client):
    assert DEFAULTS["llm.enabled"] is False
    cp = CP.build(ctx(client))
    assert cp.ai["enabled"] is False and "nie" in cp.ai["scope"]
    # statische Prüfung: Finanzpfade importieren keinen KI-Client
    for pkg in ("csvimport", "diagnosis", "ledger", "tax", "datasources", "journal", "importer", "documentimport"):
        for f in (APP / pkg).rglob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            names |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            assert "anthropic" not in names and "openai" not in names, f
            assert not any((n.module or "").startswith("app.news.llm") for n in ast.walk(tree)
                           if isinstance(n, ast.ImportFrom)), f
