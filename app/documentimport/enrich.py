"""Evidence-Based Enrichment (M25/AP3): fehlende Angaben eines Belegs sorgfältig ergänzen – mit Herkunft je Feld.

Recherchehierarchie (Abbruch, sobald ein Feld belegt ist; jede Stufe protokolliert Anfrage und Ergebnis):

1. **Dasselbe Dokument / derselbe Upload-Stapel** – andere Seiten (bereits in :mod:`.profiles`), weitere Belege
   desselben Vorgangs (gleiche Auftrags-/Transaktions-ID bzw. gleicher Hash) → Felder mit Herkunft ``batch``.
2. **Bestehende Portfolia-Daten** – Buchungen (Import, App), nur bei **technischer Identität** (Hash, Anbieter-ID):
   EUR-Wert, Gebühr, Zeitpunkt der vorhandenen Buchung (Herkunft ``portfolio``, „rekonstruiert“). Asset-Zuordnung über
   ISIN/WKN/Symbol/Alias der vorhandenen Assets (mehrdeutig → ungelöst mit Kandidaten).
3. **Angebundene Originalquellen** – bereits abgerufene Ereignisse der Datenquellen (Rohdaten im Prüf-Stapel bzw.
   übernommene Vorgänge) mit derselben Kennung: Zeitpunkt, Gebühr, Hash (Herkunft ``provider``). Es wird **kein**
   zusätzlicher Live-Abruf je Dokument ausgelöst (Ratenlimits; der reguläre Sync holt neue Ereignisse).
4. **Öffentliche/Referenzdaten** – lokal gespeicherte Kurse und EZB-Devisenkurse (keine Netzwerkanfrage) **nur als
   Schätzung**; Blockchain-Explorer (nur mit Freigabe ``documents.public_lookup``, nur der Hash wird gesendet,
   begrenzte Anfragen/Zeit) für Bestätigung, Blockzeit und Netzwerkgebühr.

Nie: Hash, Gebühr oder Zeitpunkt aus „ähnlicher Menge und Zeit“; Marktkurs als Ausführungskurs oder Anschaffungs-
kosten; Gebühr aus Mengendifferenzen; erfundene Uhrzeit.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.csvimport.events import identity_keys, normalize_hash, source_ref_keys
from app.csvimport.identity import note_identity_keys
from app.documentimport.evidence import FieldEvidence
from app.documentimport.profiles import CENT, DocTx, _money, parse_date, parse_time

log = logging.getLogger(__name__)
NATIVE_PROVIDERS = {"bitpanda", "binance", "coinbase", "kraken"}
MAX_PUBLIC_REQUESTS = 6  # je Upload-Stapel
PUBLIC_DEADLINE_S = 25.0
MAX_TX_PER_DOC_LOOKUP = 20


@dataclass
class Step:
    stage: int  # 1–4
    field: str
    source: str
    query: str
    result: str
    ok: bool


@dataclass
class Budget:
    requests: int = MAX_PUBLIC_REQUESTS
    deadline: float = field(default_factory=lambda: time.monotonic() + PUBLIC_DEADLINE_S)
    used: int = 0
    errors: list[str] = field(default_factory=list)

    def take(self) -> bool:
        if self.used >= self.requests or time.monotonic() > self.deadline:
            return False
        self.used += 1
        return True


def _ev(tx: DocTx, fld: str, value: object, origin: str, source: str, where: str, status: str, reason: str,
        *, category: str = "original", event_key: str | None = None, verified: bool = False) -> None:
    tx.evidence.append(FieldEvidence(field=fld, value=str(value), origin=origin, source_ref=source,  # type: ignore[arg-type]
                                     location=where, status=status, reason=reason,  # type: ignore[arg-type]
                                     event_key=event_key, category=category, verified_link=verified))


def tx_keys(tx: DocTx) -> set[str]:
    """Technische Identität des Belegvorgangs: Anbieter-ID (``bitpanda:<uuid>``) und Hash (``h:<hash>``)."""
    keys: set[str] = set()
    ext, prov = tx.value("ext_id"), tx.provider
    if ext and prov in NATIVE_PROVIDERS:
        keys |= identity_keys(None, (), f"{prov}:{ext}")
    h = normalize_hash(tx.value("txhash"))
    if h:
        keys.add(f"h:{h}")
    return keys


# ----------------------------------------------------------------------------------------------------
# Stufe 1: mehrere Belege desselben Vorgangs im Stapel
# ----------------------------------------------------------------------------------------------------

def merge_batch(items: list[tuple[str, DocTx]]) -> list[tuple[str, DocTx]]:
    """Belege desselben wirtschaftlichen Vorgangs (gleiche technische Identität, sonst gleiche ISIN/Symbol + Menge
    + Datum + Vorgangsart) zusammenführen: ein Vorgang, Felder aller Belege mit Herkunft ``batch`` – Widersprüche
    bleiben sichtbar. Rückgabe: verbleibende (Dokument-SHA, Vorgang)."""
    groups: dict[str, list[tuple[str, DocTx]]] = defaultdict(list)
    for sha, tx in items:
        keys = sorted(tx_keys(tx))
        asset = tx.value("isin") or tx.value("symbol")
        q, d = tx.value("quantity"), tx.value("date")
        if keys:
            gk = "k:" + keys[0]
        elif asset and q and d and tx.kind != "unknown":
            gk = f"f:{tx.kind}|{asset}|{Decimal(q).normalize()}|{d}"
        else:
            gk = f"u:{sha}:{tx.n}"
        groups[gk].append((sha, tx))
    out: list[tuple[str, DocTx]] = []
    for members in groups.values():
        lead_sha, lead = max(members, key=lambda m: (len(m[1].decisions), -m[1].n))
        for sha, other in members:
            if other is lead:
                continue
            lead.sources.append(sha)
            for e in other.evidence:
                if e.origin == "document":
                    lead.evidence.append(FieldEvidence(
                        e.field, e.value, "batch", e.source_ref, e.location, e.status, e.reason or "weiterer Beleg",
                        category=e.category, page=e.page, line=e.line, box=e.box, conf=e.conf))
            lead.warnings += [f"weiterer Beleg: {w}" for w in other.warnings]
        if len(members) > 1:
            lead.resolve()
            lead.warnings.append(f"{len(members)} Belege beschreiben denselben Vorgang – zusammengeführt "
                                 "(eine Buchung)")
        out.append((lead_sha, lead))
    return out


# ----------------------------------------------------------------------------------------------------
# Stufe 2: Portfolia-Daten
# ----------------------------------------------------------------------------------------------------

@dataclass
class Context:
    """Nachschlage-Indizes über Portfolio, Datenquellen-Ereignisse und Kurse (einmal je Stapel aufgebaut)."""

    ctx: Any
    by_key: dict[str, list[Any]] = field(default_factory=lambda: defaultdict(list))
    events: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    assets: dict[str, Any] = field(default_factory=dict)
    journal_meta: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def build(cls, ctx: Any) -> Context:
        from app.csvimport.transfer_side import hash_lookup

        self = cls(ctx)
        pf = ctx.portfolio()
        if pf is None:
            return self
        self.assets = dict(pf.assets)
        meta = {r["tx_id"]: dict(r) for r in ctx.db.q(
            "SELECT tx_id, source, event_key, external_id, tx_hash, datasource_id FROM journal_tx "
            "WHERE status='active'")}
        self.journal_meta = meta
        aliases: dict[str, set[str]] = defaultdict(set)
        for r in ctx.db.q("SELECT key, tx_id FROM journal_event_alias"):
            aliases[r["tx_id"]].add(r["key"])
        hashes = hash_lookup(ctx.db)
        for t in pf.txs:
            if t.origin == "plan":
                continue
            keys: set[str] = {f"h:{h}" for h in hashes(t)}
            if t.origin == "journal" and t.tx_id in meta:
                m = meta[t.tx_id]
                keys |= identity_keys(m["event_key"], aliases.get(t.tx_id, set()), m["external_id"])
            else:
                keys |= source_ref_keys(t.source, t.source_ref) | note_identity_keys(
                    t.source, t.to_account or t.from_account, t.note, t.source_ref)
            for k in keys:
                self.by_key[k].append(t)
        # Datenquellen-Ereignisse (Originaldaten der Anbieter, auch noch nicht übernommene)
        for r in ctx.db.q("SELECT r.event_key, r.rec_json, b.source, r.status FROM csv_row r JOIN csv_batch b "
                          "ON b.id = r.batch_id WHERE b.kind='sync' AND b.datasource_id IS NOT NULL "
                          "AND r.event_key IS NOT NULL"):
            from app.csvimport.service import rec_from_json

            try:
                rec = rec_from_json(r["rec_json"])
            except (ValueError, KeyError, TypeError):
                continue
            ks = identity_keys(rec.event_key, rec.aliases, rec.ext_id)
            if rec.txhash:
                ks.add(f"h:{normalize_hash(rec.txhash)}")
            for k in ks:
                self.events[k].append({"rec": rec, "source": r["source"], "status": r["status"]})
        return self


def _asset_candidates(c: Context, tx: DocTx) -> tuple[str | None, list[str], str]:
    """Portfolio-Asset zum Beleg: ISIN/WKN exakt, sonst Symbol/Alias eindeutig. (Asset, Kandidaten, Grund)."""
    isin, wkn, sym = tx.value("isin"), tx.value("wkn"), (tx.value("symbol") or "").upper()
    if isin:
        hits = [aid for aid, a in c.assets.items() if (a.isin or "").upper() == isin or aid.upper() == isin]
        if len(hits) == 1:
            return hits[0], hits, f"ISIN {isin} = Asset {hits[0]}"
        if hits:
            return None, hits, f"ISIN {isin} bei mehreren Assets"
    if wkn:
        hits = [aid for aid, a in c.assets.items() if (a.wkn or "").upper() == wkn]
        if len(hits) == 1:
            return hits[0], hits, f"WKN {wkn} = Asset {hits[0]}"
    if sym:
        hits = sorted({aid for aid, a in c.assets.items() if not a.is_fiat and (
            aid.upper() == sym or a.symbol.upper() == sym or sym in {x.upper() for x in a.aliases})})
        if len(hits) == 1:
            return hits[0], hits, f"Symbol {sym} eindeutig = Asset {hits[0]}"
        if hits:
            return None, hits, f"Symbol {sym} passt zu mehreren Assets ({', '.join(hits[:5])})"
    return None, [], "kein vorhandenes Asset passt"


def _catalog_candidates(c: Context, sym: str) -> list[dict[str, Any]]:
    """Lokaler CoinGecko-Katalog (kein Netz): Coins mit diesem Symbol."""
    try:
        from app.prices.sources import cached_catalog, catalog_path

        cat = cached_catalog(catalog_path(c.ctx))
        return list(cat.candidates(sym))[:10] if cat is not None else []
    except Exception:
        return []


def _stage2(c: Context, tx: DocTx, steps: list[Step]) -> Any | None:
    """Vorhandene Buchung mit technischer Identität; deren Felder werden als rekonstruierte Belege ergänzt."""
    keys = tx_keys(tx)
    hits: dict[str, Any] = {}
    for k in keys:
        for t in c.by_key.get(k, []):
            hits[t.tx_id] = t
    if not keys:
        steps.append(Step(2, "Identität", "Portfolia-Buchungen", "–", "keine Anbieter-ID bzw. kein Hash im Beleg – "
                          "Abgleich nur über Menge/Datum im Prüf-Stapel", False))
        return None
    if len(hits) != 1:
        steps.append(Step(2, "Identität", "Portfolia-Buchungen", ", ".join(sorted(keys))[:120],
                          "keine Buchung gefunden" if not hits else f"{len(hits)} Buchungen – nicht eindeutig", False))
        return None
    t = next(iter(hits.values()))
    ek = sorted(keys)[0]
    src = f"journal:{t.tx_id}" if t.origin == "journal" else f"import:{t.tx_id}"
    steps.append(Step(2, "Identität", "Portfolia-Buchungen", ek, f"Buchung {t.tx_id} (technische Identität)", True))
    if t.value_eur is not None and t.flag != "estimated":
        _ev(tx, "value_eur", _money(t.value_eur), "portfolio", src, t.tx_id, "rekonstruiert",
            f"EUR-Wert der vorhandenen Buchung {t.tx_id}", event_key=ek, verified=True)
    if t.fee_qty and t.fee_asset:
        _ev(tx, "network_fee" if tx.kind in ("deposit", "withdrawal") else "fee", t.fee_qty, "portfolio", src,
            t.tx_id, "rekonstruiert", f"Gebühr der vorhandenen Buchung {t.tx_id}", event_key=ek, verified=True)
    if not t.date_only:
        _ev(tx, "time", t.ts.astimezone(UTC).time().replace(microsecond=0).isoformat(), "portfolio", src, t.tx_id,
            "rekonstruiert", f"Zeitpunkt der vorhandenen Buchung {t.tx_id} (UTC)", event_key=ek, verified=True)
        _ev(tx, "tz", "UTC", "portfolio", src, t.tx_id, "rekonstruiert", "Zeitzone der Buchung", event_key=ek,
            verified=True)
    return t


# ----------------------------------------------------------------------------------------------------
# Stufe 3: Datenquellen-Ereignisse
# ----------------------------------------------------------------------------------------------------

def _stage3(c: Context, tx: DocTx, steps: list[Step]) -> None:
    keys = tx_keys(tx)
    found = []
    for k in keys:
        found += c.events.get(k, [])
    if not keys:
        return
    uniq = {id(f["rec"]): f for f in found}
    if not uniq:
        steps.append(Step(3, "Ereignis", "Datenquellen (abgerufene Originaldaten)", ", ".join(sorted(keys))[:120],
                          "kein abgerufenes Ereignis mit dieser Kennung", False))
        return
    recs = list(uniq.values())
    ev_keys = {f["rec"].event_key for f in recs}
    if len(ev_keys) != 1:
        steps.append(Step(3, "Ereignis", "Datenquellen", ", ".join(sorted(keys))[:120],
                          f"{len(ev_keys)} Ereignisse – nicht eindeutig, nichts übernommen", False))
        return
    ek = next(iter(ev_keys)) or sorted(keys)[0]
    src = f"provider:{recs[0]['source']}:{ek}"
    for f in recs:
        r = f["rec"]
        if not r.ts_missing and not r.date_only:
            _ev(tx, "time", r.ts.astimezone(UTC).time().replace(microsecond=0).isoformat(), "provider", src, ek,
                "belegt", f"Zeitpunkt laut Originaldaten ({f['source']})", event_key=ek, verified=True)
            _ev(tx, "date", r.ts.astimezone(UTC).date().isoformat(), "provider", src, ek, "belegt",
                f"Datum laut Originaldaten ({f['source']})", event_key=ek, verified=True)
            _ev(tx, "tz", "UTC", "provider", src, ek, "belegt", "Originaldaten in UTC", event_key=ek, verified=True)
        if r.fee_qty and r.fee_sym:
            _ev(tx, "fee", r.fee_qty, "provider", src, ek, "belegt", f"Gebühr laut Originaldaten ({r.fee_sym})",
                event_key=ek, verified=True)
            _ev(tx, "fee_ccy", r.fee_sym, "provider", src, ek, "belegt", "Gebührenwährung laut Originaldaten",
                event_key=ek, verified=True)
        if r.txhash:
            _ev(tx, "txhash", normalize_hash(r.txhash), "provider", src, ek, "belegt", "Hash laut Originaldaten",
                event_key=ek, verified=True)
    steps.append(Step(3, "Ereignis", "Datenquellen (abgerufene Originaldaten)", ek,
                      f"Ereignis gefunden ({recs[0]['source']}, Status {recs[0]['status']})", True))


# ----------------------------------------------------------------------------------------------------
# Stufe 4: Referenzdaten (lokal) und öffentliche Explorer (Freigabe)
# ----------------------------------------------------------------------------------------------------

def _stage4_local(c: Context, tx: DocTx, asset_id: str | None, steps: list[Step]) -> None:
    from app.csvimport.service import Valuer

    tx.resolve()
    d = parse_date(tx.value("date"))
    if d is None:
        return
    pf = c.ctx.portfolio()
    V = Valuer(c.ctx, c.assets, pf, date.today())
    if tx.value("value_eur") is None:
        gross, ccy = tx.dec("gross"), tx.value("ccy")
        if gross is not None and ccy and ccy != "EUR":
            hit = V.fx(gross, ccy, d)
            if hit:
                _ev(tx, "value_eur", _money(hit[0]), "public", "reference:ecb", hit[1], "geschaetzt",
                    f"{gross} {ccy} mit Referenzkurs ({hit[1]}) – nicht der Abrechnungskurs", category="reference_fx")
                steps.append(Step(4, "EUR-Wert", "EZB-Referenzkurs (lokal gespeichert)", f"{ccy} {d}",
                                  f"{_money(hit[0])} EUR (Schätzung)", True))
            else:
                steps.append(Step(4, "EUR-Wert", "EZB-Referenzkurs (lokal)", f"{ccy} {d}", "kein Kurs gespeichert",
                                  False))
        q = tx.dec("quantity")
        if tx.value("value_eur") is None and gross is None and q and asset_id:
            hit = V.price(asset_id, d)
            if hit:
                _ev(tx, "value_eur", _money(q * hit[0]), "public", "reference:market", hit[1], "geschaetzt",
                    f"Menge × Marktkurs ({hit[1]}) – Schätzung, kein Ausführungskurs, keine Anschaffungskosten",
                    category="market_price")
                steps.append(Step(4, "EUR-Wert", "gespeicherte Marktkurse", f"{asset_id} {d}",
                                  f"{_money(q * hit[0])} EUR (Schätzung)", True))
            else:
                steps.append(Step(4, "EUR-Wert", "gespeicherte Marktkurse", f"{asset_id} {d}",
                                  "kein Kurs gespeichert – bleibt ungelöst", False))


def public_lookup_enabled(ctx: Any) -> bool:
    return bool(ctx.settings.get("documents.public_lookup", False))


def _stage4_public(c: Context, tx: DocTx, budget: Budget, steps: list[Step], transport: Any = None) -> None:
    """Blockchain-Explorer (Bitcoin: mempool.space, Kaspa: api.kaspa.org) – nur der Hash wird übertragen."""
    from app.documentimport import explorer

    h = normalize_hash(tx.value("txhash"))
    if not h or tx.kind not in ("deposit", "withdrawal"):
        return
    chain = explorer.chain_for(tx.value("symbol"), tx.value("chain"), h)
    if chain is None:
        steps.append(Step(4, "Bestätigung", "Explorer", h[:12] + "…", "Netzwerk nicht bestimmbar bzw. kein "
                          "unterstützter Explorer (Bitcoin, Kaspa) – nicht abgefragt", False))
        return
    if not budget.take():
        steps.append(Step(4, "Bestätigung", chain, h[:12] + "…", "Anfrage-/Zeitbudget des Stapels erschöpft", False))
        return
    try:
        info = explorer.lookup(chain, h, transport=transport)
    except explorer.LookupError as e:
        budget.errors.append(str(e))
        steps.append(Step(4, "Bestätigung", chain, h[:12] + "…", str(e), False))
        return
    if info is None:
        steps.append(Step(4, "Bestätigung", chain, h[:12] + "…", "Transaktion nicht gefunden", False))
        return
    src = f"public:{chain}:{h}"
    if info.block_time is not None:
        _ev(tx, "chain_time", info.block_time.isoformat(), "public", src, info.explorer, "belegt",
            "Blockzeit laut Explorer (Bestätigung auf der Chain, nicht Auftragszeit einer Börse)")
        if tx.value("time") is None and tx.value("date") == info.block_time.astimezone(UTC).date().isoformat():
            _ev(tx, "time", info.block_time.astimezone(UTC).time().replace(microsecond=0).isoformat(), "public", src,
                info.explorer, "rekonstruiert", "Uhrzeit aus der Blockzeit (Datum stimmt mit dem Beleg überein)")
            _ev(tx, "tz", "UTC", "public", src, info.explorer, "rekonstruiert", "Blockzeit in UTC")
    if info.fee is not None and tx.kind == "withdrawal":
        _ev(tx, "network_fee", info.fee, "public", src, info.explorer, "belegt",
            f"Netzwerkgebühr der Transaktion laut Explorer ({info.fee_sym}) – trägt der Absender")
        _ev(tx, "network_fee_sym", info.fee_sym, "public", src, info.explorer, "belegt", "Gebühren-Asset")
    steps.append(Step(4, "Bestätigung", info.explorer, h[:12] + "…", "bestätigt" if info.confirmed else
                      "unbestätigt", True))


# ----------------------------------------------------------------------------------------------------
# Einstieg
# ----------------------------------------------------------------------------------------------------

@dataclass
class Enriched:
    tx: DocTx
    asset_id: str | None
    asset_candidates: list[str]
    existing: Any | None  # vorhandene Buchung bei technischer Identität
    steps: list[Step]


def enrich(c: Context, tx: DocTx, budget: Budget, *, public: bool = False, transport: Any = None) -> Enriched:
    steps: list[Step] = []
    asset_id, cands, why = _asset_candidates(c, tx)
    steps.append(Step(2, "Asset", "Portfolia-Assets", tx.value("isin") or tx.value("symbol") or "–", why,
                      asset_id is not None))
    if asset_id is None and tx.value("symbol") and not tx.value("isin") and not cands:
        cat = _catalog_candidates(c, tx.value("symbol") or "")
        if cat:
            steps.append(Step(4, "Asset", "CoinGecko-Katalog (lokal)", tx.value("symbol") or "",
                              f"{len(cat)} Kandidat(en): " + ", ".join(str(x.get("id")) for x in cat[:5]),
                              len(cat) == 1))
            cands = [f"coingecko:{x.get('id')}" for x in cat]
    tx.identity = tx_keys(tx)
    existing = _stage2(c, tx, steps)
    _stage3(c, tx, steps)
    _stage4_local(c, tx, asset_id, steps)
    if public:
        _stage4_public(c, tx, budget, steps, transport=transport)
    tx.resolve()
    return Enriched(tx, asset_id, cands, existing, steps)


def ts_of(tx: DocTx) -> tuple[datetime | None, bool, str]:
    """Zeitpunkt des Vorgangs aus Datum/Uhrzeit/Zeitzone: (UTC, nur Datum, Begründung)."""
    from app.documentimport import parse as P

    d = parse_date(tx.value("date"))
    if d is None:
        return None, True, "kein Datum"
    t = parse_time(tx.value("time"))
    return P.combine(d, t, tx.value("tz"))


__all__ = ["CENT", "Budget", "Context", "Enriched", "Step", "enrich", "merge_batch", "public_lookup_enabled",
           "timedelta", "ts_of", "tx_keys"]
