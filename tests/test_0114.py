"""0.114 „Entdecken & Beobachten“: Discovery und Watchlist als ein Bereich. Teil A prüft die Verknüpfung (ohne eigene
Tabelle), die Routen und die Kompatibilität; Teil B (unten) die Oberfläche.

Keine echten KI- oder Netzaufrufe; synthetische SEC-Daten aus tests/test_discovery.py, Kurse über den Demo-Anbieter."""

from __future__ import annotations

import re

import pytest

from app.discovery import service as SV
from app.discovery import watch as W
from app.watchlist.service import watchlist_service
from tests.test_discovery import _add, make_client


@pytest.fixture
def c(config, monkeypatch):
    yield from make_client(config, monkeypatch)


def cid_of(c, q: str) -> int:
    return int(_add(c, q).headers["location"].split("/c/")[1].split("?")[0])


def post(c, url: str, **data):
    return c.post(url, data={"csrf_token": c.token, **data}, follow_redirects=False)


def loc(r) -> str:
    from urllib.parse import unquote_plus

    return unquote_plus(r.headers["location"])


# ------------------------------------------------------------------------------------- Teil A: Verknüpfung und Routen

def test_observe_is_idempotent_on_every_entry_point(c):
    ctx = c.app.state.ctx
    alph = cid_of(c, "ALPH")
    r = post(c, f"/discovery/c/{alph}/beobachten")
    assert r.status_code == 303 and "wird beobachtet (Liste „Watchlist“)" in loc(r)
    assert "kein zweiter Eintrag" in loc(post(c, f"/discovery/c/{alph}/beobachten"))
    assert "kein zweiter Eintrag" in loc(post(c, f"/discovery/c/{alph}/watchlist"))  # alte Adresse (bis 0.113)
    assert "kein zweiter Eintrag" in loc(post(c, f"/discovery/c/{alph}/status", status="beobachten"))
    assert ctx.db.scalar("SELECT COUNT(*) FROM watchlist_item WHERE quote_source='yahoo' AND quote_id='ALPH'") == 1
    assert SV.company(ctx.db, alph)["status"] == "beobachten"
    assert post(c, f"/discovery/c/{alph}/status", status="kaputt").status_code == 400
    assert post(c, "/discovery/c/9999/beobachten").status_code == 404


def test_lists_remove_one_then_last_keeps_company_and_analysis(c):
    ctx = c.app.state.ctx
    db = ctx.db
    alph = cid_of(c, "ALPH")
    post(c, f"/discovery/c/{alph}/analyse", mode="daten")
    n_an = db.scalar("SELECT COUNT(*) FROM disc_analysis WHERE company_id=?", (alph,))
    second = int(post(c, "/watchlist/lists", name="Langfristig").headers["location"].split("list=")[1])
    post(c, f"/discovery/c/{alph}/beobachten")
    assert "Liste „Langfristig“" in loc(post(c, f"/discovery/c/{alph}/beobachten", list=str(second)))
    post(c, f"/discovery/c/{alph}/beobachten", list=str(second))  # erneut: genau einmal je Liste
    mem = W.memberships(db, SV.company(db, alph))
    assert [m["list_name"] for m in mem] == ["Watchlist", "Langfristig"]
    assert W.watched_index(db) == {alph: ["Watchlist", "Langfristig"]}
    # aus einer von mehreren Listen entfernen: bleibt beobachtet, Rücksprung zur Unternehmensseite
    r = post(c, f"/watchlist/item/{mem[0]['item_id']}/remove", back=f"/discovery/c/{alph}")
    assert r.headers["location"] == f"/discovery/c/{alph}"
    assert SV.company(db, alph)["status"] == "beobachten" and len(W.memberships(db, SV.company(db, alph))) == 1
    # aus der letzten Liste entfernen: verschwindet aus „Beobachtet“, alter Status bringt ihn nicht zurück
    post(c, f"/watchlist/item/{mem[1]['item_id']}/remove")
    co = SV.company(db, alph)
    assert co["status"] == "zurueckgestellt" and W.memberships(db, co) == []
    assert W.unlisted(db) == [] and W.count_watched(db) == 0
    assert alph not in [k["id"] for k in SV.radar(ctx)["cards"]]  # auch nicht unter den offenen Kandidaten
    assert db.scalar("SELECT COUNT(*) FROM disc_analysis WHERE company_id=?", (alph,)) == n_an  # Analyse bleibt
    assert c.get(f"/discovery/c/{alph}").status_code == 200


def test_discarding_a_suggestion_keeps_list_membership(c):
    ctx = c.app.state.ctx
    alph = cid_of(c, "ALPH")
    post(c, f"/discovery/c/{alph}/beobachten")
    post(c, f"/discovery/c/{alph}/status", status="verworfen", back="/discovery")
    assert SV.company(ctx.db, alph)["status"] == "verworfen"
    assert len(W.memberships(ctx.db, SV.company(ctx.db, alph))) == 1  # Vorschlag verworfen ≠ Beobachtung beendet
    card = next(k for k in SV.all_cards(ctx) if k["id"] == alph)
    assert card["watched"] and card["lists"] == ["Watchlist"] and "beobachten" in card["cats"]
    assert alph not in [k["id"] for k in SV.radar(ctx)["cards"]]  # nicht mehr unter den offenen Kandidaten
    assert "Alpha Robotics" in c.get("/watchlist").text


def test_legacy_status_only_company_stays_visible_and_jobs_unchanged(c):
    ctx = c.app.state.ctx
    beta = cid_of(c, "BETA")
    SV.set_status(ctx.db, beta, "beobachten")  # Altbestand: nur Discovery-Status, kein Watchlist-Eintrag
    assert [u["ticker"] for u in W.unlisted(ctx.db)] == ["BETA"] and W.count_watched(ctx.db) == 1
    assert "Beta Photonics" in c.get("/discovery?cat=beobachten").text
    from app.discovery import engine as EN

    assert beta in [p["id"] for group in EN.refresh_plan(ctx).values() for p in group]  # Joblogik unverändert
    post(c, f"/discovery/c/{beta}/beobachten")  # nachträglich in die Standardliste
    assert W.unlisted(ctx.db) == [] and W.count_watched(ctx.db) == 1


def test_other_exchange_crypto_and_ambiguous_symbols_are_not_linked(c):
    ctx = c.app.state.ctx
    db = ctx.db
    ws = watchlist_service(ctx)
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    entry, _errs, _ = ws.resolve("security", "ALPH.DE")  # anderer Börsenplatz: eigener Eintrag, kein US-Steckbrief
    ws.add(ws.default_id(), entry)
    assert W.company_for(db, "yahoo", "ALPH.DE") == (None, False)
    assert W.company_for(db, "yahoo", "alph") == (alph, False)
    assert W.company_for(db, "coingecko", "ALPH") == (None, False)  # anderer Kursanbieter / Wertpapierart
    assert W.watched_index(db) == {}
    assert c.get("/watchlist").status_code == 200
    db.x("UPDATE disc_company SET yahoo_symbol='BETA' WHERE id=?", (alph,))  # zwei Unternehmen, ein Symbol
    assert W.company_for(db, "yahoo", "BETA") == (None, True)
    r = post(c, f"/discovery/c/{alph}/beobachten")
    assert "ohne Kursliste (Symbol mehrdeutig)" in loc(r) and SV.company(db, alph)["status"] == "beobachten"
    assert db.scalar("SELECT COUNT(*) FROM watchlist_item WHERE quote_id='BETA'") == 0
    assert [u["id"] for u in W.unlisted(db)] == [alph] and W.memberships(db, SV.company(db, beta)) == []
    db.x("UPDATE disc_company SET yahoo_symbol='??' WHERE id=?", (alph,))
    assert W.symbol_of(SV.company(db, alph)) is None


def test_open_observe_and_remove_never_call_ai_or_start_jobs(c, monkeypatch):
    from app.news import ai as NAI

    ctx = c.app.state.ctx
    calls: list[int] = []
    monkeypatch.setattr(NAI.AiGateway, "generate", lambda *a, **k: calls.append(1))
    alph = cid_of(c, "ALPH")
    jobs = ctx.db.scalar("SELECT COUNT(*) FROM disc_job")
    post(c, f"/discovery/c/{alph}/beobachten")
    item = W.memberships(ctx.db, SV.company(ctx.db, alph))[0]["item_id"]
    for url in ("/watchlist", "/discovery", f"/discovery/c/{alph}", f"/watchlist/item/{item}", "/discovery/status"):
        assert c.get(url).status_code == 200, url
    post(c, f"/watchlist/item/{item}/remove")
    assert not calls and ctx.db.scalar("SELECT COUNT(*) FROM disc_job") == jobs
    assert ctx.db.scalar("SELECT COUNT(*) FROM disc_ai_call") == 0


def test_back_target_is_restricted_and_keeps_filters(c):
    alph = cid_of(c, "ALPH")
    assert loc(post(c, f"/discovery/c/{alph}/beobachten", back="https://evil.example/")).startswith(
        f"/discovery/c/{alph}?msg=")
    assert loc(post(c, f"/discovery/c/{alph}/beobachten", back="//evil.example")).startswith(f"/discovery/c/{alph}?")
    r = post(c, f"/discovery/c/{alph}/beobachten", back="/discovery?cat=neu&sort=signal#radar")
    assert r.headers["location"].startswith("/discovery?cat=neu&sort=signal&msg=") and r.headers["location"].endswith(
        "#radar")
    item = W.memberships(c.app.state.ctx.db, SV.company(c.app.state.ctx.db, alph))[0]["item_id"]
    r = post(c, f"/watchlist/item/{item}/remove", back="https://evil.example/")
    assert r.headers["location"].startswith("/watchlist?list=")


def test_radar_builds_cards_once_per_request(c, monkeypatch):
    cid_of(c, "ALPH")
    n: list[int] = []
    orig = SV.all_cards
    monkeypatch.setattr(SV, "all_cards", lambda ctx: n.append(1) or orig(ctx))
    page = c.get("/discovery?cat=beobachten&sort=prioritaet")
    assert page.status_code == 200 and len(n) == 1


def test_old_links_and_single_navigation_entry(c):
    ctx = c.app.state.ctx
    alph = cid_of(c, "ALPH")
    post(c, f"/discovery/c/{alph}/beobachten")
    second = int(post(c, "/watchlist/lists", name="Zwei").headers["location"].split("list=")[1])
    item = W.memberships(ctx.db, SV.company(ctx.db, alph))[0]["item_id"]
    urls = ["/watchlist", f"/watchlist?list={second}&sort=name", "/discovery", "/discovery?cat=beobachten&sort=signal",
            "/discovery?alle=1&q=alpha", f"/discovery/c/{alph}", f"/watchlist/item/{item}", "/discovery/protokoll",
            f"/discovery/c/{alph}/vergleich", "/discovery/export.json"]
    for url in urls:
        assert c.get(url).status_code == 200, url
    assert c.get("/discovery/t/ALPH", follow_redirects=False).headers["location"] == f"/discovery/c/{alph}"
    assert c.get("/discovery/t/NIX").status_code == 200  # „unbekannt“-Seite statt Fehler
    nav = r'class="nav-link active" href="/watchlist"\s+aria-current="page"'
    for url in ("/watchlist", "/discovery", f"/discovery/c/{alph}"):
        page = c.get(url).text
        assert re.search(nav, page), url  # ein Menüpunkt für beide Ansichten
        assert "Entdecken &amp; Beobachten" in page and 'class="nav-link" href="/discovery"' not in page


def test_brief_uses_stored_data_only(c):
    ctx = c.app.state.ctx
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    post(c, f"/discovery/c/{alph}/analyse", mode="daten")
    b = SV.briefs(ctx.db, [alph, beta, 999])
    assert set(b) == {alph, beta}
    assert b[alph]["stage"] == "daten" and b[alph]["nash"] and b[alph]["analysed_at"]
    assert b[beta]["stage"] == "offen" and b[beta]["nash"] is None


# ------------------------------------------------------------------------------------------- Teil B: Oberfläche

def row_of(page: str, item_id: int) -> str:
    return page.split(f'id="wl-{item_id}"')[1].split("</tr>")[0]


def card_of(page: str, cid: int) -> str:
    return page.split(f'id="karte-{cid}"')[1].split("</article>")[0]


def test_tabs_on_both_views(c):
    cid_of(c, "ALPH")
    for url, active in (("/watchlist", "/watchlist"), ("/discovery", "/discovery")):
        page = c.get(url).text
        assert '<nav class="tabs" aria-label="Entdecken &amp; Beobachten">' in page, url
        tabs = page.split('aria-label="Entdecken &amp; Beobachten">')[1].split("</nav>")[0]
        assert 'href="/watchlist"' in tabs and 'href="/discovery"' in tabs
        assert re.search(rf'class="active" href="{active}" aria-current="page"', tabs), url


def test_watched_view_shows_lists_links_and_analysis_state(c):
    ctx = c.app.state.ctx
    ws = watchlist_service(ctx)
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    post(c, f"/discovery/c/{alph}/analyse", mode="daten")
    post(c, f"/discovery/c/{alph}/beobachten")
    SV.set_status(ctx.db, beta, "beobachten")  # Altbestand ohne Liste
    for sym in ("ALPH.DE", "ZZZZ"):  # anderer Börsenplatz / US-Symbol ohne Discovery-Eintrag
        entry, _e, _c = ws.resolve("security", sym)
        ws.add(ws.default_id(), entry)
    ids = {r["quote_id"]: int(r["id"]) for r in ctx.db.q("SELECT id, quote_id FROM watchlist_item")}
    page = c.get("/watchlist").text
    assert 'href="#add">Titel hinzufügen</a>' in page
    linked = row_of(page, ids["ALPH"])
    assert f'href="/discovery/c/{alph}"' in linked and "Details öffnen" in linked and "Kursverlauf" in linked
    assert SV.STAGES["daten"] in linked and "Portfolia-Score" in linked and "/65 Daten" in linked
    other = row_of(page, ids["ALPH.DE"])
    assert "/discovery/" not in other and f'href="/watchlist/item/{ids["ALPH.DE"]}"' in other  # kein US-Steckbrief
    plain = row_of(page, ids["ZZZZ"])
    assert 'href="/discovery/t/ZZZZ"' in plain and "Analyse suchen" in plain
    section = page.split('id="ohne-liste"')[1].split("</section>")[0]
    assert "Beta Photonics" in section and f'action="/discovery/c/{beta}/beobachten"' in section
    assert 'name="status" value="zurueckgestellt"' in section and "Nicht mehr beobachten" in section
    r = post(c, f"/discovery/c/{beta}/status", status="zurueckgestellt", back="/watchlist?list=1")
    assert r.headers["location"] == "/watchlist?list=1" and 'id="ohne-liste"' not in c.get("/watchlist").text
    ctx.db.x("UPDATE disc_company SET yahoo_symbol='ALPH' WHERE id=?", (beta,))  # zwei Unternehmen, ein Symbol
    assert "Zuordnung mehrdeutig" in row_of(c.get("/watchlist").text, ids["ALPH"])


def test_empty_watched_view_points_to_discover(c):
    page = c.get("/watchlist").text
    assert "Noch keine Einträge" in page
    assert 'href="/discovery">Entdecken</a>' in page.split("Noch keine Einträge")[1][:200]  # Hinweis im Leertext


def test_discover_view_primary_start_and_cards(c, monkeypatch):
    ctx = c.app.state.ctx
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    page = c.get("/discovery?sort=signal").text
    assert 'data-primary-start="screening"' in page and 'data-primary-start="ki_discovery"' not in page  # KI aus
    more = page.split('id="weitere-suchlaeufe"')[1].split("</details>")[0]
    assert "Weitere Suchläufe" in more
    for kind in ("ki_discovery", "voll", "news"):
        assert f'action="/discovery/start/{kind}"' in more, kind
    assert 'action="/discovery/add"' in page.split('id="weitere-suchlaeufe"')[0]  # manuelle Aufnahme sichtbar
    card = card_of(page, alph)
    assert f'action="/discovery/c/{alph}/beobachten"' in card and "Vorschlag verwerfen" in card
    assert 'name="back" value="/discovery?sort=signal"' in card and 'data-watched="1"' not in page
    post(c, f"/discovery/c/{alph}/beobachten")
    page = c.get("/discovery").text
    assert f'id="karte-{alph}" data-watched="1"' in page
    card = card_of(page, alph)
    assert f'action="/discovery/c/{alph}/beobachten"' not in card and "Beobachtet ✓" in card and "(Watchlist)" in card
    assert f'action="/discovery/c/{beta}/beobachten"' in card_of(page, beta)
    from app.news import ai as NAI

    ctx.settings.set("llm.enabled", True)
    monkeypatch.setattr(NAI.AiGateway, "key_present", lambda self, provider=None: True)
    assert 'data-primary-start="ki_discovery"' in c.get("/discovery").text
    r = post(c, "/discovery/start/news")
    assert r.status_code == 303 and "Nachrichten-Discovery" in loc(r)


def test_company_page_has_one_observe_control(c):
    ctx = c.app.state.ctx
    alph = cid_of(c, "ALPH")
    page = c.get(f"/discovery/c/{alph}").text
    assert "Zur Watchlist" not in page and f'action="/discovery/c/{alph}/watchlist"' not in page
    assert page.count(f'action="/discovery/c/{alph}/beobachten"') == 1 and '<select id="beob-liste"' not in page
    assert "Vorschlag verwerfen" in page and 'id="beobachtung"' not in page
    second = int(post(c, "/watchlist/lists", name="Langfristig").headers["location"].split("list=")[1])
    assert '<select id="beob-liste" name="list">' in c.get(f"/discovery/c/{alph}").text  # Liste wählbar
    post(c, f"/discovery/c/{alph}/beobachten", list=str(second))
    page = c.get(f"/discovery/c/{alph}").text
    block = page.split('id="beobachtung"')[1].split("</div>")[0]
    item = W.memberships(ctx.db, SV.company(ctx.db, alph))[0]["item_id"]
    assert "Beobachtet in:" in block and "Langfristig" in block
    assert f'action="/watchlist/item/{item}/remove"' in block and f'name="back" value="/discovery/c/{alph}"' in block
    assert "Auch in „Watchlist“" in block and page.count(f'action="/discovery/c/{alph}/beobachten"') == 1
    assert "Portfolia-Score" in page  # Steckbrief unverändert vorhanden
    post(c, f"/watchlist/item/{item}/remove", back=f"/discovery/c/{alph}")
    SV.set_status(ctx.db, alph, "beobachten")
    assert "Beobachtet (ohne Liste)" in c.get(f"/discovery/c/{alph}").text


def test_ai_status_shows_waiting_and_failure_reason(c, monkeypatch):
    from app.discovery import jobs as J
    from app.news import ai as NAI

    ctx = c.app.state.ctx
    assert "KI deaktiviert" in c.get("/discovery").text and 'id="ki-hinweis"' not in c.get("/discovery").text
    ctx.settings.set("llm.enabled", True)
    monkeypatch.setattr(NAI.AiGateway, "key_present", lambda self, provider=None: True)
    assert "KI bereit" in c.get("/discovery").text
    jid, _ = J.create(ctx, "ki_discovery")
    ctx.db.x("UPDATE disc_job SET status='wartet', wait_until='2999-01-01T00:00:00+00:00', wait_reason='Tageslimit' "
             "WHERE id=?", (jid,))
    hint = c.get("/discovery").text.split('id="ki-hinweis"')[1].split("</p>")[0]
    assert "wartet bis" in hint and "Tageslimit" in hint and 'href="/discovery/protokoll"' in hint
    ctx.db.x("UPDATE disc_job SET status='abgebrochen', wait_until=NULL WHERE id=?", (jid,))
    ctx.db.x("INSERT INTO disc_ai_call(job_id, step, status, error, started_at) VALUES (?,?,?,?,?)",
             (jid, "themen", "fehler", "Zeitüberschreitung beim Anbieter", "2026-11-11T10:00:00+00:00"))
    hint = c.get("/discovery").text.split('id="ki-hinweis"')[1].split("</p>")[0]
    assert "fehlgeschlagen" in hint and "Zeitüberschreitung beim Anbieter" in hint
    assert "KI-Discovery starten" in c.get("/discovery").text  # manueller Start bleibt sichtbar


# ------------------------------------------------------------------------------- Listenverwaltung, Menü, Sammelabfragen

def test_lists_can_be_renamed_made_default_and_deleted(c):
    ctx = c.app.state.ctx
    db = ctx.db
    ws = watchlist_service(ctx)
    alph, beta = cid_of(c, "ALPH"), cid_of(c, "BETA")
    first = ws.default_id()
    second = int(post(c, "/watchlist/lists", name="Langfristig").headers["location"].split("list=")[1])
    post(c, f"/discovery/c/{alph}/beobachten")                      # Standardliste
    post(c, f"/discovery/c/{alph}/beobachten", list=str(second))    # und zweite Liste
    post(c, f"/discovery/c/{beta}/beobachten", list=str(second))    # nur zweite Liste
    r = post(c, f"/watchlist/lists/{second}/rename", name="  Kern   Depot ")
    assert r.status_code == 303 and db.q1("SELECT name FROM watchlist WHERE id=?", (second,))["name"] == "Kern Depot"
    assert "err=" in post(c, f"/watchlist/lists/{second}/rename", name=" ").headers["location"]
    assert "Standardliste" in loc(post(c, f"/watchlist/lists/{first}/delete"))  # Standardliste bleibt
    assert db.scalar("SELECT COUNT(*) FROM watchlist") == 2
    post(c, f"/watchlist/lists/{second}/default")
    assert ws.default_id() == second and db.scalar("SELECT COUNT(*) FROM watchlist WHERE is_default=1") == 1
    assert "Liste „Kern Depot“" in loc(post(c, f"/discovery/c/{alph}/beobachten"))  # neue Standardliste, kein Doppel
    post(c, f"/watchlist/lists/{first}/default")
    r = post(c, f"/watchlist/lists/{second}/delete")
    assert "Liste gelöscht (2 Einträge entfernt)" in loc(r) and db.scalar("SELECT COUNT(*) FROM watchlist") == 1
    assert SV.company(db, alph)["status"] == "beobachten"       # steht noch in der anderen Liste
    assert SV.company(db, beta)["status"] == "zurueckgestellt"  # war nur in der gelöschten Liste
    assert post(c, "/watchlist/lists/999/delete").headers["location"].count("err=") == 1
    assert post(c, f"/watchlist/lists/{first}/kaputt").status_code == 404
    page = c.get("/watchlist").text
    assert 'id="listen"' in page and "Listen verwalten" in page and "Standardliste" in page


def test_mobile_more_menu_is_generated_from_navigation(c):
    page = c.get("/watchlist").text
    more = page.split('id="more-menu"')[1].split("</div>")[0]
    assert more.count("<a href=") == 7 and 'href="/watchlist"' in more and "Entdecken &amp; Beobachten" in more
    for href in ("/journal", "/accounts", "/plans", "/tax", "/quality", "/settings"):
        assert f'href="{href}"' in more, href
    for href in ('href="/"', 'href="/positions"', 'href="/performance"', 'href="/news"', 'href="/discovery"'):
        assert href not in more, href


def test_cards_use_bulk_queries_and_match_single_lookups(c):
    ctx = c.app.state.ctx
    db = ctx.db
    ids = [cid_of(c, t) for t in ("ALPH", "BETA", "MTP", "GOOGL")]
    post(c, f"/discovery/c/{ids[0]}/analyse", mode="daten")
    SV.record_candidate(db, None, "wendepunkt", name="Beta Photonics Corp", ticker="BETA", company_id=ids[1],
                        status="neu", reason="Margenwende. Kennzahlen: x", priority=7)
    n = {"q": 0}
    for name in ("q", "q1", "scalar"):
        orig = getattr(db, name)
        setattr(db, name, lambda *a, _o=orig, **k: (n.__setitem__("q", n["q"] + 1), _o(*a, **k))[1])
    cards = {k["id"]: k for k in SV.all_cards(ctx)}
    used = n["q"]
    assert set(cards) == set(ids) and used <= 20, used  # unabhängig von der Zahl der Unternehmen
    for cid in ids:
        co = SV.company(db, cid)
        assert cards[cid]["why"] == SV.why_found(db, cid, co["origin"])
        assert cards[cid]["preview"] == SV.preview_of(db, cid)
        prio = db.scalar("SELECT MAX(priority) FROM disc_candidate WHERE company_id=?", (cid,))
        assert cards[cid]["priority"] == prio
        an = SV.analysis(db, cid)
        res = an["result"] if an else {}
        stage = SV._stage(res, SV.preview_of(db, cid) is not None, SV.current_filed(db, co, res))
        assert cards[cid]["stage"] == stage
    assert cards[ids[1]]["priority"] == 7 and cards[ids[1]]["why"]["path"] == "wendepunkt"


# ------------------------------------------------------------------------------------ SEC-Kennzahlen für den Bestand

class _Asset:
    def __init__(self, asset_id: str, quote_id: str, security: bool = True, source: str = "yahoo") -> None:
        self.asset_id, self.quote_id, self.quote_source, self.is_security = asset_id, quote_id, source, security
        self.name = asset_id


class _Pos:
    def __init__(self, asset: _Asset, qty: float = 3.0) -> None:
        self.asset, self.qty = asset, qty


class _Val:
    def __init__(self, positions: list[_Pos]) -> None:
        self.positions = positions


def test_us_symbol_rules():
    from app.discovery import holdings as H

    assert H.us_symbol("yahoo", "aapl") == "AAPL" and H.us_symbol("yahoo", "BRK-B") == "BRK-B"
    for bad in ("SAP.DE", "^GSPC", "EURUSD=X", "", None):
        assert H.us_symbol("yahoo", bad) is None, bad
    assert H.us_symbol("coingecko", "AAPL") is None


def test_holdings_get_sec_figures_in_background_without_ai(c, monkeypatch):
    from app.discovery import engine as EN
    from app.discovery import holdings as H
    from app.discovery import jobs as J
    from app.news import ai as NAI

    ctx = c.app.state.ctx
    db = ctx.db
    calls: list[int] = []
    monkeypatch.setattr(NAI.AiGateway, "generate", lambda *a, **k: calls.append(1))
    alph, sap = _Asset("WKN:1", "ALPH"), _Asset("WKN:2", "SAP.DE")
    etf, coin = _Asset("WKN:3", "ZZZZ"), _Asset("DOGE", "ALPH", security=False, source="coingecko")
    held = [_Pos(alph), _Pos(sap), _Pos(etf), _Pos(coin), _Pos(_Asset("WKN:4", "BETA"), qty=0)]
    with monkeypatch.context() as m:
        m.setattr(ctx, "valuation", lambda: _Val(held))
        assert H.held_symbols(ctx) == {"ALPH": "WKN:1", "ZZZZ": "WKN:3"}  # nur gehaltene US-Symbole
    monkeypatch.setattr(H, "held_symbols", lambda ctx: {"ALPH": "WKN:1", "ZZZZ": "WKN:3"})
    assert H.brief_for_asset(ctx, sap) is None and H.brief_for_asset(ctx, coin) is None
    assert H.brief_for_asset(ctx, alph) == {"state": "unbekannt", "symbol": "ALPH"}
    ctx.settings.set("discovery.enabled", False)  # „Automatische Suche: Aus“ stoppt den Bestandslauf nicht
    assert EN.run_scheduled(ctx, "analysen") == {"skipped": "Discovery ausgeschaltet"}
    H.run_scheduled(ctx)
    job = J.recent(db, 1)[0]
    assert job["kind"] == "bestand" and job["status"] == "abgeschlossen", job
    res = job["result"]
    assert res["neu"] == ["ALPH"] and "ZZZZ" in res["ohne_sec"] and len(res["analysiert"]) == 1
    co = SV.by_ticker(db, "ALPH")
    assert co["origin"] == "portfolio" and co["status"] == "neu"
    assert db.scalar("SELECT COUNT(*) FROM disc_company") == 1  # SAP.DE, ZZZZ, Krypto und BETA (0 Stück) nicht
    b = H.brief_for_asset(ctx, alph)
    assert b["state"] == "geladen" and b["company_id"] == co["id"] and b["nash"] and b["nash"]["quali_open"]
    keys = {n["key"] for n in b["numbers"]}
    assert {"revenue", "revenue_growth", "op_margin"} <= keys and b["scores"]["quality"] is not None
    assert H.brief_for_asset(ctx, etf) == {"state": "unbekannt", "symbol": "ZZZZ"}
    n_an = db.scalar("SELECT COUNT(*) FROM disc_analysis")
    H.run_scheduled(ctx)  # gleich danach: nichts fällig, kein zweites Unternehmen, keine neue Analyseversion
    assert db.scalar("SELECT COUNT(*) FROM disc_company") == 1
    assert db.scalar("SELECT COUNT(*) FROM disc_analysis") == n_an
    card = next(k for k in SV.all_cards(ctx) if k["id"] == co["id"])
    assert "neu" not in card["cats"] and card["why"]["label"] == "Aktie im Bestand (SEC-Kennzahlen)"
    assert not calls and db.scalar("SELECT COUNT(*) FROM disc_ai_call") == 0
    # vollständig verkauft: nur bestandsbedingte Unternehmen werden zurückgestellt, Daten bleiben
    beta = cid_of(c, "BETA")
    monkeypatch.setattr(H, "held_symbols", lambda ctx: {})
    assert H.run_scheduled(ctx) == {"skipped": "keine US-Aktien im Bestand"}
    assert H.sync(ctx)["zurueckgestellt"] == ["ALPH"]
    assert SV.by_ticker(db, "ALPH")["status"] == "zurueckgestellt" and SV.company(db, beta)["status"] == "neu"
    assert db.scalar("SELECT COUNT(*) FROM disc_analysis") == n_an
    assert H.brief_for_asset(ctx, alph)["state"] == "geladen"


def test_watched_holding_is_not_parked_after_sale(c, monkeypatch):
    from app.discovery import holdings as H

    ctx = c.app.state.ctx
    monkeypatch.setattr(H, "held_symbols", lambda ctx: {"ALPH": "WKN:1"})
    H.sync(ctx)
    cid = SV.by_ticker(ctx.db, "ALPH")["id"]
    post(c, f"/discovery/c/{cid}/beobachten")
    monkeypatch.setattr(H, "held_symbols", lambda ctx: {})
    assert H.sync(ctx)["zurueckgestellt"] == [] and SV.company(ctx.db, cid)["status"] == "beobachten"
