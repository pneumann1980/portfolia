"""Buchungsprüfung über Quellen und Konten (M27) – zusätzliche Regeln der bestehenden Diagnose.

Teil von :mod:`app.diagnosis.engine`: gleicher Index, gleiche Befunde, gleiche Empfehlungen, Vorschau, Übernahme und
„Rückgängig“ – keine zweite Engine. Alle Funktionen lesen nur; Auswirkungen erscheinen als Szenario.

Regeln
* **Wirtschaftliche Dublette über Quellen** (:func:`econ_duplicates`): Zu- bzw. Abgang auf demselben Konto, desselben
  Assets (gleiche Asset-ID – gleichnamige Tokens anderer Netzwerke sind andere Assets), aus zwei verschiedenen Quellen
  (z. B. Koinly-Import und Börsen-API), Betrag brutto/netto gleich (eine Gebühr im selben Asset erklärt die
  Differenz), deterministisch 1:1 zugeordnet. Gleiche Höhe und Nähe allein sind kein Beleg: ≤ 1 h und eindeutig →
  „wahrscheinlich“, ≤ 36 h → „verdacht“, ≤ 7 Tage → „hinweis“ (ohne ausreichenden Beleg, keine empfohlene Korrektur).
* **Bestand nach Quellen** (:func:`source_breakdown`): Saldo je Quelle, Zeiträume und Überschneidung – zerlegt eine
  Abweichung je Konto, statt einen Gesamtwert als falsch zu melden.
* **Negative Bestände** (:func:`explain_negatives`): erster negativer Zeitpunkt, Zwischenstand oder aktuelle
  Inkonsistenz, mögliche Ursachen (doppelte Auszahlung, Sparplan ohne Finanzierung, Ende der Quellhistorie, Gebühr,
  fehlender Eingang eines Eigenübertrags).
* **Abgänge ohne Gegenbuchung** (:func:`outflows`): mögliche Gegenbuchungen mit Sicherheitsbewertung (auch Bridge/
  Wrapped über ``related_asset`` bzw. Wert), sonst „ungeklärter Abgang“; Verlust nur bei Beleg (Verlust-Tag,
  dokumentierte Kompromittierung) – nie aus Inaktivität oder Kursverfall.
* **Inaktive Konten** (:func:`inactive_accounts`): letzte Buchung und letzte erfolgreiche Synchronisation getrennt,
  Restbestände, Verbindungszustand – Inaktivität ist nie ein Verlust.
* **Referenzbestände** (:func:`apply_reference`): Soll-Ist zum Stichtag eines vom Nutzer bestätigten Bestands.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.diagnosis.model import LOSS_CLASS, Finding, HoldingRow, Scenario
from app.ledger.models import Tx
from app.web.fmt import eur

if TYPE_CHECKING:  # pragma: no cover
    from app.diagnosis.engine import _Index

ZERO = Decimal(0)
STRONG_GAP = timedelta(hours=1)
ECON_WINDOW = timedelta(hours=36)
HINT_WINDOW = timedelta(days=7)
FUNDING_WINDOW = timedelta(hours=24)
SYSTEMATIC_MIN = 3  # so viele Paare mit gleichem Tagesversatz bei gleicher Uhrzeit gelten als regelmäßiges Muster
SYSTEMATIC_TOD = timedelta(minutes=15)
MULTI_MAX_CANDIDATES = 8  # 1:n – höchstens so viele Teilbuchungen im Fenster (begrenzte, deterministische Suche)
MULTI_MAX_PARTS = 3
ALT_BEFORE = timedelta(hours=2)
ALT_AFTER = timedelta(days=14)
ALT_MIN_RATIO = Decimal("0.5")
ALT_MAX_RATIO = Decimal("1.001")
ALT_SHOW = 3
ALT_LINK_SCORE = 45  # ab dieser Sicherheit gilt eine Gegenbuchung als Vorschlag (sonst ungeklärter Abgang)
INACTIVE_AFTER = timedelta(days=365)
SYNC_STALE = timedelta(days=7)
LOSS_MIN_EUR = Decimal("50")  # kleinere Abgänge ohne Gegenbuchung: nur gezählt (Gebühren, Reste)
LOSS_ALERT_EUR = Decimal("500")
MAX_LIST = 12
AUX_FAMILIES = frozenset({"diagnose", "transfer"})  # abgeleitete Buchungen – nie Teil einer Dublette
PRIMARY_EXCLUDE = frozenset({"sparplan", "diagnose", "transfer", "manual"})
FLOW_TYPES = frozenset({"deposit", "withdrawal", "transfer"})
NO_COUNTERPART_TAGS = frozenset({"fee", "cost", "gift", "donation", "tax", "withholding_tax", "burn", "lost",
                                 "stolen", "margin", "realized_loss", "payment", "spend"})
_COMPROMISED = re.compile(r"gehackt|kompromittiert|gestohlen|\bhack|compromised|stolen", re.I)
_DATE_DE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_RATING = ((70, "hoch"), (45, "mittel"), (0, "gering"))


# ----------------------------------------------------------------------------------------------------
# Quellen (Herkunft einer Buchung, unabhängig davon, ob sie aus dem Import oder der App kommt)
# ----------------------------------------------------------------------------------------------------

def _norm_family(src: str | None) -> str:
    s = (src or "manual").strip().lower()
    for prefix in ("sync:", "csv:", "doc:"):
        if s.startswith(prefix):
            rest = s[len(prefix):].split(":", 1)[0]
            return prefix + rest if rest else prefix[:-1]
    return s


def family(idx: _Index, t: Tx) -> str:
    """Quellfamilie einer Buchung: Steuertool-Import (``koinly``), Datenquelle (``sync:bitpanda``), CSV, Sparplan,
    manuell, Diagnose … – auch nach einem Portfolia-Export (``portfolia:sync:bitpanda``)."""
    if t.origin == "plan":
        return "sparplan"
    if t.origin == "journal":
        m = idx.meta.get(t.tx_id)
        return _norm_family(m.source if m is not None else "manual")
    src = (t.source or "").strip().lower()
    if src.startswith("portfolia:"):
        return _norm_family(src[len("portfolia:"):])
    return src or "import"


def family_label(fam: str) -> str:
    fixed = {"sparplan": "Sparplan-Ausführung (Portfolia)", "diagnose": "Korrektur aus der Diagnose",
             "manual": "manuell erfasst", "transfer": "Transfer (App)", "import": "Import", "koinly": "Koinly-Import",
             "reconstructed": "Rekonstruktion im Import"}
    if fam in fixed:
        return fixed[fam]
    kind, _, rest = fam.partition(":")
    if kind == "sync":
        return f"Datenquelle {rest or '?'} (API/Blockchain)"
    if kind == "csv":
        return f"CSV-Import {rest}".strip()
    if kind == "doc":
        return f"Beleg {rest}".strip()
    return f"Import · {fam}"


def _curated(fam: str) -> bool:
    """Kuratierter Import (Steuertool, Broker-Export) – gilt bei Doppelungen als maßgeblich."""
    return not (fam.startswith(("sync:", "csv:", "doc:")) or fam in PRIMARY_EXCLUDE)


def flows_by_family(idx: _Index) -> dict[tuple[str, str], dict[str, list[Any]]]:
    """Je (Konto, Asset) und Quelle: [Saldo, Anzahl, erste, letzte Buchung] – Wirkung wie die Ledger-Engine."""
    cached = getattr(idx, "_fam_flows", None)
    if cached is not None:
        return cached
    from app.diagnosis.engine import _effect

    out: dict[tuple[str, str], dict[str, list[Any]]] = defaultdict(dict)
    for t in idx.txs:
        fam = family(idx, t)
        for k, v in _effect([t]).items():
            slot = out[k].setdefault(fam, [ZERO, 0, t.ts, t.ts])
            slot[0] += v
            slot[1] += 1
            slot[3] = t.ts
    idx._fam_flows = out
    return out


def deltas(idx: _Index) -> dict[tuple[str, str], list[tuple[datetime, date, Decimal, str]]]:
    """Bestandswirkung je (Konto, Asset) in Buchungsreihenfolge: (Zeitpunkt, Datum, Änderung, Buchung)."""
    cached = getattr(idx, "_deltas", None)
    if cached is not None:
        return cached
    from app.diagnosis.engine import _effect

    out: dict[tuple[str, str], list[tuple[datetime, date, Decimal, str]]] = defaultdict(list)
    for t in idx.txs:
        for k, v in _effect([t]).items():
            out[k].append((t.ts, t.date, v, t.tx_id))
    idx._deltas = out
    return out


def families_of(idx: _Index, acc: str, aid: str) -> list[tuple[str, Decimal, int, datetime, datetime]]:
    rows = flows_by_family(idx).get((acc, aid), {})
    return sorted(((f, v[0], v[1], v[2], v[3]) for f, v in rows.items()), key=lambda r: (-abs(r[1]), r[0]))


def account_last(idx: _Index) -> dict[str, tuple[datetime, str]]:
    """Letzte Buchung je Konto (beliebiges Asset, auch Gebühren): Zeitpunkt und Buchung."""
    cached = getattr(idx, "_acc_last", None)
    if cached is not None:
        return cached
    out: dict[str, tuple[datetime, str]] = {}
    for t in idx.txs:
        for acc in {t.from_account, t.to_account}:
            if acc:
                out[acc] = (t.ts, t.tx_id)
    idx._acc_last = out
    return out


def _family_last(idx: _Index, acc: str) -> dict[str, datetime]:
    """Letzte Buchung je Quelle auf einem Konto (alle Assets)."""
    out: dict[str, datetime] = {}
    for (a, _aid), fams in flows_by_family(idx).items():
        if a != acc:
            continue
        for fam, v in fams.items():
            if fam not in out or v[3] > out[fam]:
                out[fam] = v[3]
    return out


# ----------------------------------------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------------------------------------

def _tol(v: Decimal) -> Decimal:
    return max(Decimal("1e-8"), abs(v) * Decimal("1e-9"))


def _eq(a: Decimal, b: Decimal) -> bool:
    """Gleiche Menge: relativ 1e-12 (Rundung der Quellen) – auch bei sehr kleinen Tokenmengen, ohne absolute
    Untergrenze."""
    return a == b or abs(a - b) <= max(abs(a), abs(b)) * Decimal("1e-12")


def rating(score: int) -> str:
    return next(label for lim, label in _RATING if score >= lim)


def _signed(v: Decimal) -> str:
    from app.diagnosis.engine import _q

    return ("+" if v > 0 else "−" if v < 0 else "±") + _q(abs(v))


def _prov_keys(idx: _Index, t: Tx) -> set[str]:
    """Anbieter-Kennungen (z. B. ``bitpanda:<uuid>``) der Buchung – verschiedene Kennungen desselben Anbieters belegen
    verschiedene Vorgänge."""
    from app.csvimport.events import identity_keys, source_ref_keys
    from app.csvimport.identity import note_identity_keys

    if t.origin == "journal":
        m = idx.meta.get(t.tx_id)
        return set(identity_keys(m.event_key, m.aliases, m.external_id)) if m is not None else set()
    ref = t.source_ref or ""
    keys = set(source_ref_keys(t.source, ref)) | set(note_identity_keys(t.source, t.to_account or t.from_account,
                                                                      t.note, ref))
    if t.source and t.source.startswith("portfolia:") and ":" in ref.split("#", 1)[0]:
        keys.add(ref.split("#", 1)[0].lower())
    return keys


def _prov_conflict(a: set[str], b: set[str]) -> bool:
    pa = defaultdict(set)
    for k in a:
        pa[k.split(":", 1)[0]].add(k)
    for k in b:
        p = k.split(":", 1)[0]
        if p in pa and k not in pa[p] and not (pa[p] & b):
            return True
    return False


@dataclass(frozen=True)
class Leg:
    tx: Tx
    side: str  # in | out
    account: str
    asset: str
    gross: Decimal  # Zugang: gutgeschrieben vor Gebühr; Abgang: inkl. Gebühr im selben Asset
    net: Decimal  # Wirkung ohne Gebühr (Zugang abzüglich, Abgang ohne Gebühr)
    fee: Decimal
    fam: str


def legs(idx: _Index, t: Tx) -> list[Leg]:
    """Zu-/Abgangsbeine einer Ein-/Auszahlung bzw. eines Transfers (Käufe/Tausch zählen nicht)."""
    if t.type not in FLOW_TYPES or t.origin == "plan":
        return []
    fam = family(idx, t)
    out: list[Leg] = []
    if t.from_account and t.from_asset and t.from_qty:
        fee = t.fee_qty if t.fee_asset == t.from_asset and t.fee_qty else ZERO
        out.append(Leg(t, "out", t.from_account, t.from_asset, t.from_qty + fee, t.from_qty, fee, fam))
    if t.to_account and t.to_asset and t.to_qty:
        fee = t.fee_qty if t.fee_asset == t.to_asset and t.fee_qty and not t.from_account else ZERO
        out.append(Leg(t, "in", t.to_account, t.to_asset, t.to_qty, t.to_qty - fee, fee, fam))
    return out


def _amount_match(a: Leg, b: Leg) -> str | None:
    """Wie die Beträge zusammenpassen (brutto/netto) – None, wenn nicht."""
    if _eq(a.net, b.net) and _eq(a.gross, b.gross):
        return "exakt gleich" if not (a.fee or b.fee) else "brutto und netto gleich"
    if _eq(a.net, b.net):
        return "netto gleich (Gebühr nur in einer Quelle)"
    if _eq(a.gross, b.net) or _eq(a.net, b.gross):
        return "brutto der einen = netto der anderen Quelle (Gebühr erklärt die Differenz)"
    if _eq(a.gross, b.gross):
        return "brutto gleich (Gebühr verschieden)"
    return None


def _leg_text(leg: Leg) -> str:
    from app.diagnosis.engine import _q

    if leg.fee:
        return f"brutto {_q(leg.gross)}, Gebühr {_q(leg.fee)}, netto {_q(leg.net)} {leg.asset}"
    return f"{_q(leg.net)} {leg.asset}"


def _keep_drop(a: Leg, b: Leg) -> tuple[Leg, Leg]:
    """Welche Buchung gilt: der kuratierte Import (maßgeblich), sonst die frühere."""
    ca, cb = _curated(a.fam), _curated(b.fam)
    if ca != cb:
        return (a, b) if ca else (b, a)
    if (a.tx.origin == "import") != (b.tx.origin == "import"):
        return (a, b) if a.tx.origin == "import" else (b, a)
    return (a, b) if (a.tx.ts, a.tx.tx_id) <= (b.tx.ts, b.tx.tx_id) else (b, a)


# ----------------------------------------------------------------------------------------------------
# Wirtschaftliche Dubletten über Quellen
# ----------------------------------------------------------------------------------------------------

EVIDENCE_LEVEL = {"belegt": "nachgewiesen (Identität belegt)", "wahrscheinlich": "möglich – starke Übereinstimmung",
                  "verdacht": "möglich – plausibel, nicht belegt", "hinweis": "möglich – schwacher Hinweis"}


def _shared_identity(idx: _Index, a: Tx, b: Tx, ka: set[str], kb: set[str]) -> str:
    """Belastbare Identität: gemeinsamer Transaktions-Hash bzw. gemeinsame Anbieter-Kennung."""
    from app.diagnosis.engine import _short

    hs = sorted(idx.hashes[a.tx_id] & idx.hashes[b.tx_id])
    if hs:
        return f"gleicher Transaktions-Hash {_short(hs[0])}"
    ks = sorted(ka & kb)
    if ks:
        return f"gleiche Anbieter-Kennung {_short(ks[0], 24)}"
    return ""


def _regular(lst: list[Leg], leg: Leg) -> bool:
    """Wiederkehrender gleich hoher Vorgang derselben Quelle (z. B. Sparplan): ≥ 3 Buchungen in 90 Tagen."""
    n = sum(1 for x in lst
            if x.fam == leg.fam and _eq(x.net, leg.net) and abs(x.tx.ts - leg.tx.ts) <= timedelta(days=90))
    return n >= 3 or "SAVINGS_PLAN" in (leg.tx.flag or "") or (leg.tx.source_ref or "").startswith("sparplan|")


def _case_evidence(idx: _Index, k: Leg, d: Leg, how: str, gap: timedelta, ident: str, unique: bool, regular: bool,
                   offset: int | None, alternatives: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Belege für und gegen einen gemeinsamen Vorgang sowie fehlende Informationen (für die Einzelprüfung)."""
    from app.diagnosis.engine import _d, _dur, _q

    pro, contra, missing = [], [], []
    if ident:
        pro.append(f"Identität belegt: {ident}")
    pro.append(f"gleiches Konto ({k.account}), gleiche Asset-ID ({k.asset}), gleiche Richtung")
    pro.append(f"Beträge {how}")
    for leg in (k, d):
        if leg.fee:
            pro.append(f"Gebühr {_q(leg.fee)} {leg.asset} in {leg.tx.tx_id} ausgewiesen ({family_label(leg.fam)})")
    if gap <= STRONG_GAP:
        pro.append(f"Abstand {_dur(gap)}")
    else:
        contra.append(f"Abstand {_dur(gap)} – zeitliche Nähe ist kein Beleg")
    if unique:
        pro.append("je Buchung genau ein möglicher Partner")
    else:
        contra.append("mehrere mögliche Partner – Zuordnung mehrdeutig"
                      + (f" (auch: {', '.join(alternatives[:4])})" if alternatives else ""))
    fund = _funding(idx, k, d)
    if fund:
        pro.append(fund)
    if offset:
        pro.append(f"regelmäßiger Versatz von {offset} Tagen bei gleicher Uhrzeit (mindestens {SYSTEMATIC_MIN} Paare) "
                   "– typisch für Zahlungs- bzw. Wertstellungsdatum vs. Ausführung")
    if k.tx.type != d.tx.type:
        contra.append(f"verschiedene Buchungsarten ({k.tx.type} / {d.tx.type})")
    if not how.startswith(("exakt", "brutto und")):
        contra.append("Beträge nicht identisch – die Differenz ist nur über die ausgewiesene Gebühr erklärt")
    if regular:
        contra.append("wiederkehrender gleich hoher Vorgang (z. B. Sparplan) – unabhängige Ausführungen könnten "
                      "verwechselt werden")
    if not ident:
        missing.append("gemeinsame Anbieterreferenz bzw. gemeinsamer Transaktions-Hash fehlen")
        for leg in (k, d):
            if not _prov_keys(idx, leg.tx) and not idx.hashes[leg.tx.tx_id]:
                missing.append(f"{leg.tx.tx_id} trägt keine Anbieterreferenz ({family_label(leg.fam)})")
        missing.append(f"Kontoauszug bzw. Transaktionshistorie der Börse um den {_d(k.tx.ts)}: Betrag einmal oder "
                       "zweimal enthalten?")
    return pro, contra, missing


def econ_duplicates(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    from app.diagnosis.engine import _effect, _q, _scenario_without, _short, _ts

    by_pos: dict[tuple[str, str, str], list[Leg]] = defaultdict(list)
    for t in idx.txs:
        if t.tx_id in idx.dup_txs:
            continue
        for leg in legs(idx, t):
            if leg.fam not in AUX_FAMILIES:
                by_pos[(leg.account, leg.asset, leg.side)].append(leg)
    pkeys: dict[str, set[str]] = {}

    def keys(t: Tx) -> set[str]:
        if t.tx_id not in pkeys:
            pkeys[t.tx_id] = _prov_keys(idx, t)
        return pkeys[t.tx_id]

    cands: list[tuple[tuple[Any, ...], Leg, Leg, str, str]] = []
    partners: dict[str, list[str]] = defaultdict(list)
    for _pos, lst in sorted(by_pos.items()):
        lst.sort(key=lambda x: (x.tx.ts, x.tx.tx_id))
        for i, a in enumerate(lst):
            for b in lst[i + 1:]:
                gap = b.tx.ts - a.tx.ts
                if gap > HINT_WINDOW:
                    break
                if a.fam == b.fam or a.tx.tx_id == b.tx.tx_id:
                    continue
                ha, hb = idx.hashes[a.tx.tx_id], idx.hashes[b.tx.tx_id]
                if ha and hb and not (ha & hb):
                    continue  # verschiedene Blockchain-Transaktionen
                if _prov_conflict(keys(a.tx), keys(b.tx)):
                    continue  # verschiedene Vorgänge laut Anbieter-Kennung
                how = _amount_match(a, b)
                if how is None:
                    continue
                ident = _shared_identity(idx, a.tx, b.tx, keys(a.tx), keys(b.tx))
                exact = 0 if how.startswith(("exakt", "brutto und")) else 1
                cands.append(((0 if ident else 1, gap, exact, a.tx.tx_id, b.tx.tx_id), a, b, how, ident))
                if gap <= ECON_WINDOW or ident:
                    partners[a.tx.tx_id].append(b.tx.tx_id)
                    partners[b.tx.tx_id].append(a.tx.tx_id)
    cands.sort(key=lambda c: c[0])
    used: set[str] = set()
    chosen: list[tuple[Leg, Leg, str, timedelta, str, str, bool]] = []
    pattern: dict[tuple[str, str, str, str, str, int], int] = defaultdict(int)
    for (_i, gap, _e, _x, _y), a, b, how, ident in cands:
        if a.tx.tx_id in used or b.tx.tx_id in used:
            continue
        used.update((a.tx.tx_id, b.tx.tx_id))
        unique = len(partners[a.tx.tx_id]) <= 1 and len(partners[b.tx.tx_id]) <= 1
        if ident:
            status = "belegt"
        elif gap <= STRONG_GAP and unique:
            status = "wahrscheinlich"
        elif gap <= ECON_WINDOW:
            status = "verdacht"
        else:
            status = "hinweis"
            pk = _offset_key(a, b, gap)
            if pk is not None:
                pattern[pk] += 1
        chosen.append((a, b, how, gap, status, ident, unique))
    groups: dict[tuple[str, str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for a, b, how, gap, status, ident, unique in chosen:
        pk = _offset_key(a, b, gap) if status == "hinweis" else None
        offset = pk[5] if pk is not None and pattern[pk] >= SYSTEMATIC_MIN else None
        if offset:
            status = "verdacht"  # regelmäßiger Versatz bei gleicher Uhrzeit: Zahlungs- vs. Ausführungsdatum
        keep, drop = _keep_drop(a, b)
        lst = by_pos[(a.account, a.asset, a.side)]
        regular = _regular(lst, a) or _regular(lst, b)
        alts = sorted(set(partners[a.tx.tx_id] + partners[b.tx.tx_id]) - {a.tx.tx_id, b.tx.tx_id})
        pro, contra, missing = _case_evidence(idx, keep, drop, how, gap, ident, unique, regular, offset, alts)
        groups[(status, a.account, a.asset, a.side, keep.fam, drop.fam)].append(
            {"keep": keep, "drop": drop, "how": how, "gap": gap, "ident": ident, "ambiguous": not unique,
             "alternatives": alts, "pro": pro, "contra": contra, "missing": missing, "status": status})
    out: list[Finding] = []
    econ = getattr(idx, "econ", None)
    if econ is None:
        idx.econ = econ = defaultdict(list)
    for (status, acc, aid, side, fk, fd), items in sorted(groups.items()):
        items.sort(key=lambda x: (x["keep"].tx.ts, x["keep"].tx.tx_id))
        lk, ld = family_label(fk), family_label(fd)
        drops = [c["drop"].tx for c in items]
        pairs, evidence = [], []
        for c in items:
            k, d = c["keep"], c["drop"]
            econ[(acc, aid)].append((status, k.tx, d.tx, side))
            pairs.append((idx.ref(k.tx), idx.ref(d.tx), "; ".join(c["pro"])
                          + (" – dagegen: " + "; ".join(c["contra"]) if c["contra"] else "")))
            evidence.append(f"{_ts(k.tx.ts)} {lk}: {_leg_text(k)} ({k.tx.tx_id}) ↔ {_ts(d.tx.ts)} {ld}: "
                            f"{_leg_text(d)} ({d.tx.tx_id})")
        n = len(items)
        eff = _effect(drops)
        total = sum((abs(v) for (a2, x), v in eff.items() if a2 == acc and x == aid), ZERO)
        word = "Zugänge" if side == "in" else "Abgänge"
        noun = ("Zugang" if side == "in" else "Abgang") if n == 1 else word
        ambiguous = sum(1 for c in items if c["ambiguous"])
        prefix = "Nachgewiesen doppelt" if status == "belegt" else "Möglicherweise doppelt"
        title = (f"{prefix}: {n} wirtschaftlich gleiche{'r' if n == 1 else ''} {noun} aus zwei Quellen – "
                 f"{acc} · {aid}" + (" (ohne ausreichenden Beleg)" if status == "hinweis" else ""))
        known = [f"{n} {'Vorgang' if n == 1 else 'Vorgänge'} (je ein Paar): gleiches Konto, gleiche Asset-ID ({aid}), "
                 f"gleiche Richtung, Betrag brutto bzw. netto gleich – je eine Buchung aus „{lk}“ und aus „{ld}“.",
                 f"Evidenzstufe: {EVIDENCE_LEVEL[status]}.",
                 f"Beide Buchungen je Paar zählen derzeit: Bestand {acc} · {aid} "
                 f"{'um' if n == 1 else 'insgesamt um'} {_q(total)} {aid} {'zu hoch' if side == 'in' else 'zu niedrig'}"
                 f", falls es jeweils derselbe Vorgang ist.",
                 "Die Rohdaten beider Quellen bleiben unverändert erhalten. Jeder Vorgang ist einzeln prüf- und "
                 "freigebbar (Einzelvorgänge unten)."]
        if ambiguous:
            known.append(f"{ambiguous} Vorgang/Vorgänge mit mehreren möglichen Partnern – Entscheidung zurückgestellt, "
                         "keine Empfehlung.")
        suspected = ([f"Derselbe wirtschaftliche Vorgang ist über zwei Quellen erfasst – durch "
                      f"{items[0]['ident'] or 'Kennung'} belegt."] if status == "belegt" else
                     ["Derselbe wirtschaftliche Vorgang könnte über zwei Quellen erfasst sein (z. B. Steuertool-Import "
                      "und Börsen-API) – nicht bewiesen."])
        if status == "belegt":
            unc = ["Identität über Hash bzw. Anbieter-Kennung belegt; prüfen, ob eine Quelle den Vorgang legitim in "
                   "mehrere Teile zerlegt (dann ist die Summe maßgeblich)."]
        elif status == "wahrscheinlich":
            unc = ["Kein gemeinsamer Hash und keine gemeinsame Anbieter-Kennung: Die Zuordnung beruht auf Konto, "
                   "Asset, Richtung, Betrag (brutto/netto) und Zeit (≤ 1 h, je Buchung genau ein Partner). Zwei "
                   "echte, gleich hohe Vorgänge in derselben Stunde sind möglich – Kontoauszug prüfen."]
        elif status == "verdacht":
            unc = ["Abstand über 1 h bzw. mehrere mögliche Partner: Gleiche Höhe und zeitliche Nähe allein beweisen "
                   "keine Doppelbuchung."]
            if any("regelmäßiger Versatz" in " ".join(c["pro"]) for c in items):
                unc.append("Bei regelmäßigen Vorgängen (Sparplan) kann der Versatz auch zwei verschiedene Ausführungen "
                           "verbinden – Kontoauszug bzw. Anzahl der Ausführungen je Monat prüfen.")
        else:
            unc = ["Gleiche Höhe und ein Abstand von mehreren Tagen sind kein Beleg für eine Doppelbuchung (z. B. zwei "
                   "gleich hohe Einzahlungen in einer Woche). Ohne Kontoauszug bzw. Anbieter-Kennung bleibt das "
                   "ungeklärt – Portfolia empfiehlt hier keine Korrektur."]
        unc.append("Eine rechnerische Übereinstimmung mit einer Bestandsabweichung ist kein Beleg und fließt nicht in "
                   "die Bewertung ein.")
        hashes = sorted({_short(h) for c in items
                         for h in idx.hashes[c["keep"].tx.tx_id] | idx.hashes[c["drop"].tx.tx_id]})
        f = Finding(
            kind="duplicate", status=status, priority={"belegt": 1, "wahrscheinlich": 1, "verdacht": 2}.get(status, 3),
            title=title, known=known, suspected=suspected, uncertainty=unc,
            evidence=evidence[:40] + ([f"… und {len(evidence) - 40} weitere"] if len(evidence) > 40 else [])
            + ([f"Hashes: {', '.join(hashes)}"] if hashes else []),
            pairs=pairs[:40],
            scenario=_scenario_without(idx, drops, f"Szenario (hypothetisch): je Paar nur die Buchung aus „{lk}“ "
                                                   f"gezählt (die aus „{ld}“ als enthalten verknüpft) – es wird "
                                                   "nichts gebucht."),
            decision="Jeden Vorgang einzeln mit Kontoauszug bzw. Historie der Börse prüfen und entscheiden: "
                     "verknüpfen (eine Buchung zählt, die andere bleibt mit Herkunft erhalten), verschiedene Vorgänge "
                     "(ablehnen) oder später prüfen. Portfolia ändert nichts automatisch.",
            key=f"econ|{acc}|{aid}|{side}|" + "|".join(sorted(f"{c['keep'].tx.tx_id}>{c['drop'].tx.tx_id}"
                                                               for c in items)),
            weight=total, positions=[(acc, aid)],
            data={"type": "econ_pairs", "pairs": [[c["keep"].tx.tx_id, c["drop"].tx.tx_id] for c in items],
                  "account": acc, "asset": aid, "side": side, "keep_family": fk, "drop_family": fd,
                  "cases": [{"pair": [c["keep"].tx.tx_id, c["drop"].tx.tx_id], "status": status,
                             "level": EVIDENCE_LEVEL[status], "how": c["how"], "identity": c["ident"],
                             "ambiguous": c["ambiguous"], "alternatives": c["alternatives"], "pro": c["pro"],
                             "contra": c["contra"], "missing": c["missing"]} for c in items]})
        f.txs = [x for c in items[:40] for x in (idx.ref(c["keep"].tx), idx.ref(c["drop"].tx))]
        if status != "hinweis":
            idx.dup_txs.update(t.tx_id for c in items for t in (c["keep"].tx, c["drop"].tx))
        out.append(idx.attach(f, derive_positions=False))
    stats["econ_pairs"] = sum(len(v) for v in groups.values())
    out += _multi_parts(idx, by_pos, used)
    return out


def _multi_parts(idx: _Index, by_pos: dict[tuple[str, str, str], list[Leg]], used: set[str]) -> list[Finding]:
    """Mehrteilige Darstellung (1:n, n:1, n:m): eine Quelle bucht einen Vorgang in Teilen. Nur als Hinweis – eine
    passende Summe ist kein Beleg; mehrere passende Kombinationen → mehrdeutig, keine Verknüpfung."""
    from itertools import combinations

    from app.diagnosis.engine import _q, _ts

    out: list[Finding] = []
    taken: set[str] = set(used)
    for (acc, aid, side), lst in sorted(by_pos.items()):
        rest = [x for x in lst if x.tx.tx_id not in taken]
        for single in rest:
            if single.tx.tx_id in taken:
                continue
            near = [x for x in rest if x.fam != single.fam and x.tx.tx_id not in taken
                    and abs(x.tx.ts - single.tx.ts) <= STRONG_GAP and x.tx.tx_id != single.tx.tx_id]
            near = sorted(near, key=lambda x: (x.tx.ts, x.tx.tx_id))[:MULTI_MAX_CANDIDATES]
            fams = {x.fam for x in near}
            if len(near) < 2 or len(fams) != 1:
                continue
            combos = []
            for size in range(2, min(MULTI_MAX_PARTS, len(near)) + 1):
                for combo in combinations(near, size):
                    s_net = sum((x.net for x in combo), ZERO)
                    s_gross = sum((x.gross for x in combo), ZERO)
                    if any(_eq(u, v) for u in (s_net, s_gross) for v in (single.net, single.gross)):
                        combos.append(combo)
            if not combos:
                continue
            parts = combos[0]
            ambiguous = len(combos) > 1
            ids = [single.tx.tx_id, *(x.tx.tx_id for x in parts)]
            taken.update(ids if not ambiguous else [single.tx.tx_id])
            keep_single = _curated(single.fam) or not _curated(parts[0].fam)
            pairs = ([[single.tx.tx_id, x.tx.tx_id] for x in parts] if keep_single else
                     [[x.tx.tx_id, single.tx.tx_id] for x in parts])
            shape = f"1:{len(parts)}"
            known = [f"Eine Buchung aus „{family_label(single.fam)}“ ({single.tx.tx_id}, {_leg_text(single)}) "
                     f"entspricht der Summe von {len(parts)} Buchungen aus „{family_label(parts[0].fam)}“ "
                     f"({', '.join(x.tx.tx_id for x in parts)}) innerhalb einer Stunde.",
                     *(f"{_ts(x.tx.ts)} {x.tx.tx_id}: {_leg_text(x)}" for x in parts)]
            if ambiguous:
                known.append(f"{len(combos)} verschiedene Kombinationen ergeben dieselbe Summe – mehrdeutig, "
                             "Entscheidung zurückgestellt.")
            f = Finding(
                kind="duplicate", status="hinweis", priority=3,
                title=f"Mögliche mehrteilige Darstellung ({shape}): {acc} · {aid}, {_q(single.net)} {aid}",
                known=known,
                suspected=["Eine Quelle könnte einen Vorgang in Teilbeträgen abbilden (z. B. Einzahlung und "
                           "Gebühr getrennt) – möglich, nicht belegt."],
                uncertainty=["Eine passende Summe ist kein Beleg für denselben Vorgang; unabhängige Teilbeträge "
                             "können zufällig dieselbe Summe ergeben.",
                             "Portfolia empfiehlt hier keine Korrektur; verknüpfen nur mit Kontoauszug."],
                decision="Kontoauszug prüfen; nur bei Beleg verknüpfen, sonst ablehnen bzw. später prüfen.",
                key=f"econ-multi|{acc}|{aid}|{side}|" + "|".join(sorted(ids)), positions=[(acc, aid)],
                weight=abs(single.net),
                data={"type": "econ_pairs", "pairs": [] if ambiguous else pairs, "multi": shape,
                      "account": acc, "asset": aid, "side": side,
                      "keep_family": single.fam if keep_single else parts[0].fam,
                      "drop_family": parts[0].fam if keep_single else single.fam, "ambiguous": ambiguous,
                      "cases": []})
            f.txs = [idx.ref(single.tx), *(idx.ref(x.tx) for x in parts)]
            out.append(idx.attach(f, derive_positions=False))
        out += _many_to_many(idx, acc, aid, side, [x for x in lst if x.tx.tx_id not in taken], taken)
    return out


def _many_to_many(idx: _Index, acc: str, aid: str, side: str, rest: list[Leg], taken: set[str]) -> list[Finding]:
    """n:m: in einem Zeitfenster (≤ 1 h zwischen Buchungen) buchen zwei Quellen je mehrere Teile mit gleicher Summe."""
    from app.diagnosis.engine import _q

    out: list[Finding] = []
    rest = sorted(rest, key=lambda x: (x.tx.ts, x.tx.tx_id))
    cluster: list[Leg] = []
    clusters: list[list[Leg]] = []
    for x in rest:
        if cluster and x.tx.ts - cluster[-1].tx.ts > STRONG_GAP:
            clusters.append(cluster)
            cluster = []
        cluster.append(x)
    if cluster:
        clusters.append(cluster)
    for cl in clusters:
        by_fam: dict[str, list[Leg]] = defaultdict(list)
        for x in cl:
            by_fam[x.fam].append(x)
        if len(by_fam) != 2 or any(len(v) < 2 for v in by_fam.values()):
            continue
        (fa, la), (fb, lb) = sorted(by_fam.items())
        if not _eq(sum((x.net for x in la), ZERO), sum((x.net for x in lb), ZERO)):
            continue
        ids = [x.tx.tx_id for x in (*la, *lb)]
        taken.update(ids)
        f = Finding(
            kind="duplicate", status="hinweis", priority=3,
            title=f"Mögliche mehrteilige Darstellung ({len(la)}:{len(lb)}): {acc} · {aid}, Summe "
                  f"{_q(sum((x.net for x in la), ZERO))} {aid}",
            known=[f"„{family_label(fa)}“: {', '.join(f'{x.tx.tx_id} ({_leg_text(x)})' for x in la)}",
                   f"„{family_label(fb)}“: {', '.join(f'{x.tx.tx_id} ({_leg_text(x)})' for x in lb)}",
                   "Beide Quellen buchen im selben Zeitfenster dieselbe Summe in unterschiedlich vielen Teilen."],
            suspected=["Möglicherweise derselbe Vorgang in unterschiedlicher Aufteilung – nicht belegt."],
            uncertainty=["Eine n:m-Zuordnung ist ohne Kennungen nicht eindeutig – Portfolia bietet keine "
                         "Verknüpfung an; erst mit Kontoauszug einzeln entscheiden."],
            decision="Kontoauszug prüfen; Teile einzeln zuordnen, sonst ablehnen bzw. später prüfen.",
            key=f"econ-nm|{acc}|{aid}|{side}|" + "|".join(sorted(ids)), positions=[(acc, aid)],
            data={"type": "econ_pairs", "pairs": [], "multi": f"{len(la)}:{len(lb)}", "account": acc, "asset": aid,
                  "side": side, "keep_family": fa, "drop_family": fb, "ambiguous": True, "cases": []})
        f.txs = [idx.ref(x.tx) for x in (*la, *lb)]
        out.append(idx.attach(f, derive_positions=False))
    return out


def _offset_key(a: Leg, b: Leg, gap: timedelta) -> tuple[str, str, str, str, str, int] | None:
    """Versatz in ganzen Tagen bei (nahezu) gleicher Uhrzeit – Schlüssel für regelmäßige Verschiebungen."""
    days = round(gap.total_seconds() / 86400)
    rest = abs(gap.total_seconds() - days * 86400)
    if days < 1 or rest > SYSTEMATIC_TOD.total_seconds():
        return None
    return (a.account, a.asset, a.side, a.fam, b.fam, days)


def _funding(idx: _Index, keep: Leg, drop: Leg) -> str:
    """Nachfolgende Käufe: verbrauchen sie genau einen der beiden Zugänge, spricht das für einen einzigen Vorgang."""
    if keep.side != "in":
        return ""
    from app.diagnosis.engine import _q

    start = min(keep.tx.ts, drop.tx.ts)
    used = ZERO
    for t in idx.txs:
        if t.ts < start:
            continue
        if t.ts > start + FUNDING_WINDOW:
            break
        if t.tx_id in (keep.tx.tx_id, drop.tx.tx_id) or t.type not in ("buy", "trade", "sell"):
            continue
        if t.from_account == keep.account and t.from_asset == keep.asset and t.from_qty:
            used += t.from_qty
    if used and _eq(used, keep.net):
        return f"Käufe in den folgenden 24 h verbrauchen {_q(used)} {keep.asset} – genau einen der beiden Zugänge"
    return ""


# ----------------------------------------------------------------------------------------------------
# Bestand nach Quellen (Zerlegung je Konto und Asset)
# ----------------------------------------------------------------------------------------------------

def source_breakdown(idx: _Index, stats: dict[str, int],
                     refs: dict[tuple[str, str], Any] | None = None) -> list[Finding]:
    """Konten, die im selben Zeitraum aus mehreren Quellen gebucht werden – Saldo je Quelle, Überschneidung, nur in
    einer Quelle vorhandene Ein-/Auszahlungen."""
    from app.diagnosis.engine import _d, _effect, _q

    out: list[Finding] = []
    econ = getattr(idx, "econ", {})
    for (acc, aid), fams in sorted(flows_by_family(idx).items()):
        prim = {f: v for f, v in fams.items() if f not in PRIMARY_EXCLUDE}
        if len(prim) < 2:
            continue
        start = max(v[2] for v in prim.values())
        end = min(v[3] for v in prim.values())
        if start > end + ECON_WINDOW:
            continue  # Quellen lösen sich zeitlich ab – keine Überschneidung
        start, end = min(start, end) - ECON_WINDOW, max(start, end) + ECON_WINDOW
        pairs = econ.get((acc, aid), [])
        matched = {t.tx_id for _s, k, d, _side in pairs for t in (k, d)}
        only: dict[str, list[Leg]] = defaultdict(list)
        for t in idx.txs:
            if t.ts < start or t.ts > end or t.tx_id in matched:
                continue
            for leg in legs(idx, t):
                if leg.account == acc and leg.asset == aid and leg.fam in prim:
                    only[leg.fam].append(leg)
        bal = idx.bal(acc, aid)
        strong = [p for p in pairs if p[0] in ("wahrscheinlich", "verdacht")]
        ref = (refs or {}).get((acc, aid))
        status = "verdacht" if strong or bal < 0 or (ref is not None and ref.ref_diff) else "hinweis"
        if status == "hinweis" and not idx.asset(aid).is_fiat:
            continue  # Krypto ohne Doppelbuchung/negativen Bestand: Zerlegung nur bei Bedarf (sonst Rauschen)
        known = [f"Bestand {acc} · {aid}: {_q(bal)} = Summe der Quellen:"]
        for fam, v in sorted(fams.items(), key=lambda kv: (-abs(kv[1][0]), kv[0])):
            known.append(f"– {family_label(fam)}: {v[1]} Buchungen {_d(v[2])}–{_d(v[3])}, Saldo {_signed(v[0])} {aid}")
        names = " und ".join(family_label(f) for f in sorted(prim))
        known.append(f"Zeitraum, in dem {names} dieses Konto gleichzeitig buchen (± 36 h): {_d(start)}–{_d(end)}.")
        evidence = []
        if pairs:
            by_st: dict[str, list[Any]] = defaultdict(list)
            for p in pairs:
                by_st[p[0]].append(p)
            for st in ("wahrscheinlich", "verdacht", "hinweis"):
                if by_st.get(st):
                    tot = sum((abs(v) for p in by_st[st] for (a2, x), v in _effect([p[2]]).items()
                               if a2 == acc and x == aid), ZERO)
                    evidence.append(f"{len(by_st[st])} wirtschaftlich gleiche Paare „{st}“ – Wirkung der zweiten "
                                    f"Buchung {_q(tot)} {aid} (siehe Befund „Mögliche Doppelbuchungen“)")
        for fam, lst in sorted(only.items()):
            s_in = sum((x.net for x in lst if x.side == "in"), ZERO)
            s_out = sum((x.gross for x in lst if x.side == "out"), ZERO)
            evidence.append(f"Nur in „{family_label(fam)}“ (im gemeinsamen Zeitraum, ohne Gegenstück in der anderen "
                            f"Quelle): {len(lst)} Ein-/Auszahlungen, Zugänge +{_q(s_in)}, Abgänge −{_q(s_out)} {aid}")
            for x in lst[:MAX_LIST]:
                amount = f"+{_q(x.net)}" if x.side == "in" else f"−{_q(x.gross)}"
                evidence.append(f"  {_d(x.tx.ts)} {amount} {aid} ({x.tx.tx_id})")
        sc = Scenario(text="Szenario (hypothetisch): Bestand, wenn jeweils nur eine Quelle zählte bzw. ohne die als "
                           "Doppelbuchung erkannten Buchungen – nichts wird gebucht.")
        for fam, v in sorted(prim.items()):
            sc.rows.append((f"nur „{family_label(fam)}“ (ohne übrige Quellen)", _q(bal),
                            _q(v[0] + sum((w[0] for f2, w in fams.items() if f2 in PRIMARY_EXCLUDE), ZERO))))
        if strong:
            drops = [p[2] for p in strong]
            delta = sum((v for (a2, x), v in _effect(drops).items() if a2 == acc and x == aid), ZERO)
            sc.rows.append(("ohne die Doppelbuchungen „wahrscheinlich“/„verdacht“", _q(bal), _q(bal - delta)))
        if ref is not None and ref.reference is not None:
            sc.rows.append((f"Referenzbestand {_d(ref.reference_at)} (Prüfwert)", _q(ref.soll_at_ref),
                            _q(ref.reference)))
        f = Finding(
            kind="holdings", status=status, priority=2 if status == "verdacht" else 3,
            title=f"Konto aus mehreren Quellen gebucht: {acc} · {aid} – Bestand nach Quellen zerlegt",
            known=known, evidence=evidence,
            suspected=["Überschneiden sich zwei Quellen zeitlich, kann derselbe Vorgang doppelt zählen – oder in "
                       "einer Quelle fehlen (unvollständige API-Historie, abweichende Darstellung z. B. Sparplan als "
                       "Einzahlung + Kauf statt nur Kauf)."],
            uncertainty=["Die Zerlegung zeigt nur, woher der Bestand stammt; welche Quelle richtig ist, belegt erst "
                         "der Kontoauszug zum selben Stichtag (Referenzbestand)."],
            scenario=sc,
            decision="Zuerst die Doppelbuchungen dieses Kontos klären; danach den Kontostand laut Auszug als "
                     "Referenzbestand zum Stichtag hinterlegen. Portfolia gleicht nichts automatisch aus.",
            key=f"breakdown|{acc}|{aid}", weight=abs(bal), positions=[(acc, aid)], accounts=[acc], assets=[aid],
            sources=[family_label(x) for x in sorted(fams)],
            data={"type": "breakdown", "account": acc, "asset": aid})
        f.txs = [idx.ref(x.tx) for lst in only.values() for x in lst[:MAX_LIST]]
        out.append(idx.attach(f, derive_positions=False))
    stats["breakdowns"] = len(out)
    return out


# ----------------------------------------------------------------------------------------------------
# Negative Bestände
# ----------------------------------------------------------------------------------------------------

def explain_negatives(idx: _Index, findings: list[Finding]) -> None:
    """Befunde „Bestand zeitweise negativ“ um Einordnung und mögliche Ursachen ergänzen (in place, nur Text/Daten)."""
    from app.diagnosis.engine import _d, _effect, _q, _scenario_without, _ts

    econ = getattr(idx, "econ", {})
    unmatched_out: list[Tx] = getattr(idx, "unmatched_out", [])
    for f in findings:
        if not f.key.startswith("issue|negative_balance|") or not f.positions:
            continue
        acc, aid = f.positions[0]
        run = ZERO
        first: tuple[Tx, Decimal] | None = None
        lowest = ZERO
        recovered: datetime | None = None
        streak: tuple[Tx, Decimal] | None = None  # Beginn der aktuellen negativen Phase
        for ts, _dd, e, tid in deltas(idx).get((acc, aid), ()):
            before = run
            run += e
            if run < -_tol(run) and first is None:
                first = (idx.by_id[tid], run)
            if first is not None and recovered is None and run >= -_tol(run) and ts > first[0].ts:
                recovered = ts
            if run < -_tol(run) and before >= -_tol(before):
                streak = (idx.by_id[tid], run)
            elif run >= -_tol(run):
                streak = None
            lowest = min(lowest, run)
        cur = idx.bal(acc, aid)
        if first is None:
            continue
        t0, v0 = streak if cur < -_tol(cur) and streak is not None else first
        causes: list[str] = []
        ids: list[str] = []
        drops: list[Tx] = []
        if cur < -_tol(cur):
            f.known.insert(0, f"Aktueller Bestand {_q(cur)} {aid} – kein historischer Zwischenstand, sondern eine "
                              "Inkonsistenz bzw. Datenlücke.")
        else:
            gap = (recovered - t0.ts) if recovered else None
            f.known.insert(0, f"Heute nicht negativ ({_q(cur)} {aid}); negativ ab {_ts(t0.ts)}, tiefster Stand "
                              f"{_q(lowest)}" + (f", ausgeglichen nach {_dur_text(gap)}" if gap else "") + ".")
            if gap is not None and gap <= timedelta(hours=72):
                causes.append("historischer Zwischenstand: Abgang vor dem zugehörigen Zugang gebucht (Zeitstempel, "
                              "Zeitzone bzw. Reihenfolge) – kein Bestandsfehler")
            f.priority = max(f.priority, 2)
        f.known.insert(1, (f"Durchgehend negativ seit {_q(v0)} {aid} nach " if (t0, v0) != first else
                           f"Erster negativer Stand {_q(v0)} {aid} nach ") + idx.describe(t0) + ".")
        if (t0, v0) != first:
            f.known.insert(2, f"Frühere, ausgeglichene negative Zwischenstände ab {_ts(first[0].ts)} (tiefster Stand "
                              f"{_q(lowest)}) – historisch, nicht Teil der aktuellen Abweichung.")
        # 1) doppelte Auszahlung aus zwei Quellen
        outs = [p for p in econ.get((acc, aid), []) if p[3] == "out" and p[0] in ("wahrscheinlich", "verdacht")]
        if outs:
            drops = [p[2] for p in outs]
            delta = sum((v for (a2, x), v in _effect(drops).items() if a2 == acc and x == aid), ZERO)
            after = cur - delta
            full = after >= -_tol(after)
            pair_ids = ", ".join(f"{p[1].tx_id}/{p[2].tx_id}" for p in outs)
            causes.append(f"doppelte Auszahlung aus zwei Quellen ({pair_ids})"
                          + (f" – erklärt den negativen Bestand vollständig: ohne die zweite Buchung {_q(after)} {aid}"
                             if full else f" – ohne die zweite Buchung {_q(after)} {aid}"))
            ids += [x for p in outs for x in (p[1].tx_id, p[2].tx_id)]
            if full and f.status == "belegt":
                f.suspected = [s for s in f.suspected if "unvollständig" not in s]
        # 2) Sparplan-Ausführungen ohne Finanzierung
        fam_last = _family_last(idx, acc)
        plan_out = [t for t in idx.txs if family(idx, t) == "sparplan" and t.from_account == acc
                    and t.from_asset == aid and t.ts >= t0.ts - timedelta(days=31)]
        if plan_out:
            s = sum((t.from_qty or ZERO for t in plan_out), ZERO)
            others = {fm: ts for fm, ts in fam_last.items() if fm not in PRIMARY_EXCLUDE}
            last_fam, last_ts = max(others.items(), key=lambda kv: kv[1]) if others else ("", None)
            plan_ids = ", ".join(t.tx_id for t in plan_out[:6])
            causes.append(f"{len(plan_out)} Sparplan-Ausführung(en) ohne Finanzierung ({plan_ids}, "
                          f"zusammen {_q(s)} {aid}): Portfolia führt den Sparplan fort, die Einzahlung dazu fehlt"
                          + (f" – die Historie von „{family_label(last_fam)}“ für dieses Konto endet am {_d(last_ts)}"
                             if last_ts else "")
                          + (f"; ohne diese Ausführungen {_q(cur + s)} {aid}" if cur < 0 else ""))
            ids += [t.tx_id for t in plan_out]
        # 3) Ende einer Quellhistorie vor dem negativen Zeitpunkt
        for fm, ts in sorted(fam_last.items()):
            if fm not in PRIMARY_EXCLUDE and ts < t0.ts and not plan_out:
                causes.append(f"Historie von „{family_label(fm)}“ für dieses Konto endet am {_d(ts)} – spätere "
                              "Zugänge fehlen möglicherweise (unvollständiger Import bzw. Abruf)")
        # 4) Gebühr
        if t0.fee_asset == aid and t0.fee_qty and abs(v0) <= t0.fee_qty:
            causes.append(f"Gebühr {_q(t0.fee_qty)} {aid} der Buchung {t0.tx_id} übersteigt den Bestand – "
                          "Gebühr evtl. doppelt bzw. im falschen Asset gebucht")
        # 5) fehlender Eingang eines Eigenübertrags
        for w in unmatched_out:
            if w.from_asset == aid and w.from_account != acc and t0.ts - timedelta(days=3) <= w.ts <= t0.ts:
                causes.append(f"möglicher fehlender Eingang eines Eigenübertrags: {w.tx_id} "
                              f"(−{_q(w.from_qty)} {aid} von {w.from_account}, {_d(w.ts)}) ohne Gegenbuchung")
                ids.append(w.tx_id)
                break
        known_cause = [s for s in f.suspected if s.startswith("Ursache wahr")]  # z. B. Umtausch doppelt gebucht
        if not causes and not known_cause:
            causes.append("keine Ursache in den Daten erkennbar – fehlende historische Buchungen, unvollständiger "
                          "Abruf (Pagination) oder abweichende Asset-Kennung möglich")
        f.suspected = known_cause + [f"mögliche Ursache: {c}" for c in causes]
        if drops:
            f.scenario = _scenario_without(idx, drops, "Szenario (hypothetisch): ohne die zweite Buchung der doppelten "
                                                       "Auszahlung – es wird nichts gebucht.")
        f.data = {"type": "negative", "account": acc, "asset": aid, "current": str(cur), "first": t0.tx_id,
                  "causes": causes, "txs": ids}
        have = {r.tx_id for r in f.txs}
        f.txs += [idx.ref(idx.by_id[x]) for x in dict.fromkeys(ids) if x in idx.by_id and x not in have]


def _dur_text(td: timedelta) -> str:
    from app.diagnosis.engine import _dur

    return _dur(td)


# ----------------------------------------------------------------------------------------------------
# Abgänge ohne Gegenbuchung: mögliche Gegenbuchungen (Sicherheit) bzw. ungeklärter Abgang / Verlust
# ----------------------------------------------------------------------------------------------------

TRANSFER_EXACT_LIMIT = 6  # Teilproblem bis zu so vielen Ab- bzw. Zugängen: exakte globale Zuordnung


def _max_matchings(edges: list[tuple[str, str]], limit: int = 64) -> tuple[int, list[list[tuple[str, str]]]]:
    """Alle Zuordnungen maximaler Größe (deterministisch, begrenzt). Rückgabe: Größe und bis zu ``limit``
    Zuordnungen."""
    ws = sorted({w for w, _d in edges})
    adj: dict[str, list[str]] = defaultdict(list)
    for w, d in sorted(edges):
        adj[w].append(d)
    best = 0
    found: list[list[tuple[str, str]]] = []

    def rec(i: int, taken: set[str], cur: list[tuple[str, str]]) -> None:
        nonlocal best
        if len(found) > limit:
            return
        if i == len(ws):
            if len(cur) > best:
                best = len(cur)
                found.clear()
            if len(cur) == best and best:
                found.append(list(cur))
            return
        if len(cur) + (len(ws) - i) < best:
            return  # kann die beste Größe nicht mehr erreichen
        w = ws[i]
        for d in adj[w]:
            if d not in taken:
                taken.add(d)
                cur.append((w, d))
                rec(i + 1, taken, cur)
                cur.pop()
                taken.discard(d)
        rec(i + 1, taken, cur)

    rec(0, set(), [])
    return best, found


def assign_transfers(idx: _Index, cands: list[tuple[Tx, Tx, Decimal]]
                     ) -> tuple[list[tuple[Tx, Tx, Decimal]], list[list[tuple[Tx, Tx, Decimal]]]]:
    """Abgänge ↔ Zugänge ohne vorschnelle (greedy) Belegung zuordnen.

    1. Gemeinsamer Hash belegt die Identität und geht vor; konkurriert ein Hash-Kandidat, ist das mehrdeutig.
    2. Übrige Kandidaten bilden zusammenhängende Teilprobleme. Ein Teilproblem mit genau einer Kante ist eindeutig.
    3. Kleine Teilprobleme (≤ 6 je Seite) werden exakt gelöst: Gibt es genau eine Zuordnung maximaler Größe, gilt sie
       (eindeutig durch Ausschluss); sonst – und bei größeren Teilproblemen – bleibt die Zuordnung offen.
    Rückgabe: angenommene Paare und mehrdeutige Teilprobleme (nie automatisch verknüpft)."""
    by_id: dict[str, Tx] = {}
    ratio: dict[tuple[str, str], Decimal] = {}
    hash_edges: list[tuple[str, str]] = []
    other: list[tuple[str, str]] = []
    for w, d, r in cands:
        by_id[w.tx_id], by_id[d.tx_id] = w, d
        ratio[(w.tx_id, d.tx_id)] = r
        (hash_edges if idx.hashes[w.tx_id] & idx.hashes[d.tx_id] else other).append((w.tx_id, d.tx_id))
    accepted: list[tuple[str, str]] = []
    ambiguous: list[list[tuple[str, str]]] = []
    deg: dict[str, int] = defaultdict(int)
    for w, d in hash_edges:
        deg[w] += 1
        deg[d] += 1
    blocked: set[str] = set()
    for w, d in sorted(hash_edges):
        if deg[w] == 1 and deg[d] == 1:
            accepted.append((w, d))
            blocked.update((w, d))
    rest_hash = [(w, d) for w, d in hash_edges if (w, d) not in accepted]
    edges = [(w, d) for w, d in [*rest_hash, *other] if w not in blocked and d not in blocked]
    # zusammenhängende Teilprobleme (Union-Find über Ab- und Zugänge)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for w, d in edges:
        parent[find(w)] = find(d)
    comps: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for w, d in edges:
        comps[find(w)].append((w, d))
    for comp in sorted(comps.values(), key=lambda c: sorted(c)):
        ws, ds = {w for w, _d in comp}, {d for _w, d in comp}
        if len(comp) == 1:
            accepted.append(comp[0])
            continue
        if len(ws) <= TRANSFER_EXACT_LIMIT and len(ds) <= TRANSFER_EXACT_LIMIT:
            _size, matchings = _max_matchings(comp)
            if len(matchings) == 1:
                accepted.extend(matchings[0])
                idx.transfer_exclusive = getattr(idx, "transfer_exclusive", set()) | set(matchings[0])
                continue
        ambiguous.append(sorted(comp))
    idx.transfer_ambiguous = {x for comp in ambiguous for e in comp for x in e}
    acc = [(by_id[w], by_id[d], ratio[(w, d)]) for w, d in sorted(accepted)]
    amb = [[(by_id[w], by_id[d], ratio[(w, d)]) for w, d in comp] for comp in ambiguous]
    return acc, amb


def ambiguous_transfers(idx: _Index, comps: list[list[tuple[Tx, Tx, Decimal]]]) -> list[Finding]:
    """Konkurrierende Transfer-Kandidaten: alle Zuordnungen zeigen, keine Verknüpfung empfehlen."""
    from app.diagnosis.engine import _d, _dur, _q

    out: list[Finding] = []
    for comp in comps:
        ws = sorted({w.tx_id: w for w, _d2, _r in comp}.values(), key=lambda t: (t.ts, t.tx_id))
        ds = sorted({d.tx_id: d for _w, d, _r in comp}.values(), key=lambda t: (t.ts, t.tx_id))
        aid = ws[0].from_asset or ""
        ev = []
        for w, d, r in comp:
            same = bool(idx.hashes[w.tx_id] & idx.hashes[d.tx_id])
            level = "Identität durch Hash belegt" if same else evidence_label(_edge_score(w, d, r))
            ev.append(f"{w.tx_id} ({_d(w.ts)}, −{_q(w.from_qty)} {aid}, {w.from_account}) → {d.tx_id} ({_d(d.ts)}, "
                      f"+{_q(d.to_qty)} {d.to_asset}, {d.to_account}): {level}; Menge "
                      f"{(r * 100).quantize(Decimal('0.1'))} %, Abstand {_dur(d.ts - w.ts)}")
        f = Finding(
            kind="transfer", status="hinweis", priority=2,
            title=f"Mehrdeutige Transfer-Zuordnung: {len(ws)} Abgänge, {len(ds)} mögliche Zugänge · {aid}",
            known=[f"{len(ws)} Abgänge und {len(ds)} Zugänge desselben Assets passen über Kreuz zusammen "
                   f"({len(comp)} mögliche Paare) – mehr als eine Zuordnung ist möglich.",
                   "Portfolia legt keine Zuordnung fest und empfiehlt keine Verknüpfung "
                   "(Entscheidung zurückgestellt)."],
            evidence=ev,
            uncertainty=["Ohne gemeinsamen Hash bzw. Referenz entscheidet erst der Explorer bzw. Kontoauszug, welcher "
                         "Abgang zu welchem Zugang gehört.",
                         "Matching-Stufen sind regelbasierte Bewertungen, keine Wahrscheinlichkeiten."],
            decision="Je Abgang im Explorer bzw. Kontoauszug das Ziel prüfen und den passenden Zugang einzeln "
                     "verknüpfen (Journal) – oder ablehnen bzw. später prüfen.",
            key="transfer-amb|" + "|".join(f"{w.tx_id}>{d.tx_id}" for w, d, _r in comp),
            data={"type": "transfer", "pairs": [], "ambiguous": True,
                  "alternatives": {w.tx_id: [d.tx_id for w2, d, _r in comp if w2.tx_id == w.tx_id] for w in ws}})
        f.txs = [idx.ref(t) for t in (*ws, *ds)]
        out.append(idx.attach(f))
    return out


def _edge_score(w: Tx, d: Tx, ratio: Decimal) -> int:
    gap = d.ts - w.ts
    return (25 if ratio >= Decimal("0.98") else 12) + (20 if abs(gap) <= timedelta(hours=2) else
                                                       12 if gap <= timedelta(hours=24) else 6)


def evidence_label(score: int, *, identity: bool = False, conflict: bool = False) -> str:
    """Qualitative Evidenzstufe eines regelbasierten Matching-Scores (keine kalibrierte Wahrscheinlichkeit)."""
    if identity:
        return "Identität durch Hash/Referenz belegt"
    if conflict:
        return "widersprüchliche Informationen"
    if score >= 70:
        return "starke Übereinstimmung"
    if score >= ALT_LINK_SCORE:
        return "plausibler Kandidat"
    return "schwacher Hinweis"


def _networks(idx: _Index) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for s in idx.snap.sources:
        if s.kind == "wallet":
            out[s.account].add(s.provider)
    return out


def compromised(idx: _Index, acc: str) -> tuple[bool, date | None, str]:
    """Konto laut Name bzw. Kontonotiz kompromittiert? (Datum aus der Notiz, falls angegeben)."""
    info = idx.pf.accounts.get(acc)
    text = " ".join(x for x in (acc, *(str(v) for v in ((info.extra or {}).values() if info else ()))) if x)
    if not _COMPROMISED.search(text):
        return False, None, ""
    m = _DATE_DE.search(text)
    d = None
    if m:
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            d = None
    note = str((info.extra or {}).get("note") or "") if info else ""
    return True, d, note or acc


def _alternatives(idx: _Index, w: Tx, deps: dict[str, list[Tx]], all_deps: list[Tx], nets: dict[str, set[str]],
                  taken: set[str]) -> list[tuple[int, Tx, list[str]]]:
    from app.diagnosis.engine import _dur

    out: list[tuple[int, Tx, list[str]]] = []
    q = w.from_qty or ZERO
    hw = idx.hashes[w.tx_id]
    for d in deps.get(w.from_asset or "", []):
        if d.to_account == w.from_account or d.tx_id in taken or not (w.ts - ALT_BEFORE <= d.ts <= w.ts + ALT_AFTER):
            continue
        ratio = (d.to_qty or ZERO) / q if q else ZERO
        if not (ALT_MIN_RATIO <= ratio <= ALT_MAX_RATIO):
            continue
        hd = idx.hashes[d.tx_id]
        if hw and hd and not (hw & hd):
            continue
        score, why = 0, []
        if hw & hd:
            score += 60
            why.append("gleicher Hash")
        score += 25 if ratio >= Decimal("0.98") else 12 if ratio >= Decimal("0.9") else 0
        why.append(f"Menge {(ratio * 100).quantize(Decimal('0.1'))} % des Abgangs")
        gap = d.ts - w.ts
        score += 20 if abs(gap) <= timedelta(hours=2) else 12 if gap <= timedelta(hours=24) else \
            6 if gap <= timedelta(hours=72) else 0
        why.append(f"Abstand {_dur(gap)}")
        from app.diagnosis.engine import DISTINCT_DIGITS, _sig_digits

        if _sig_digits(d.to_qty or ZERO) >= DISTINCT_DIGITS:
            score += 8
            why.append("unverwechselbare Menge")
        na, nb = nets.get(w.from_account or "", set()), nets.get(d.to_account or "", set())
        if na and nb and not (na & nb):
            score -= 30
            why.append(f"verschiedene Netzwerke ({', '.join(sorted(na))} → {', '.join(sorted(nb))}) – nur über Bridge "
                       "bzw. Börse plausibel")
        out.append((max(0, min(95, score)), d, why))
    # Tokenwechsel (Bridge, Wrapped, Cross-Chain-Swap): nur mit belegter Beziehung (``related_asset``) – ein ähnlicher
    # EUR-Wert allein ist kein Beleg für denselben Vorgang
    for d in all_deps:
        if d.to_asset == w.from_asset or d.to_account == w.from_account or d.tx_id in taken:
            continue
        if not (w.ts - ALT_BEFORE <= d.ts <= w.ts + timedelta(hours=24)):
            continue
        if not (d.related_asset == w.from_asset or w.related_asset == d.to_asset):
            continue
        vr = (d.value_eur / w.value_eur) if w.value_eur and d.value_eur else None
        score = 30 + (15 if vr is not None and Decimal("0.97") <= vr <= Decimal("1.02") else 0) + \
            (10 if abs(d.ts - w.ts) <= timedelta(hours=2) else 0)
        why = [f"Tokenwechsel {w.from_asset} → {d.to_asset} (Bridge/Wrapped/Cross-Chain; Beziehung laut "
               "„related_asset“ der Buchung)",
               f"EUR-Wert {(vr * 100).quantize(Decimal('0.1'))} %" if vr is not None else "EUR-Wert nicht vergleichbar",
               f"Abstand {_dur(d.ts - w.ts)}", "anderes Asset – keine automatische Transfer-Verknüpfung"]
        out.append((min(65, score), d, why))
    out.sort(key=lambda x: (-x[0], x[1].ts, x[1].tx_id))
    return out[:ALT_SHOW]


def outflows(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    from app.diagnosis.engine import NEUTRAL_TAGS, _d, _q, _ts

    taken = set(getattr(idx, "transfer_txs", set())) | set(idx.dup_txs) | set(getattr(idx, "transfer_ambiguous", set()))
    deps: dict[str, list[Tx]] = defaultdict(list)
    all_deps: list[Tx] = []
    for t in idx.txs:
        if t.type == "deposit" and t.to_account and t.to_asset and t.to_qty and t.origin != "plan" \
                and (t.tag or "").lower() in NEUTRAL_TAGS and not idx.asset(t.to_asset).is_fiat:
            deps[t.to_asset].append(t)
            all_deps.append(t)
    nets = _networks(idx)
    alt_groups: dict[tuple[str, str], list[tuple[Tx, list[tuple[int, Tx, list[str]]]]]] = defaultdict(list)
    loss_groups: dict[tuple[str, str, str], list[Tx]] = defaultdict(list)
    small = 0
    unmatched: list[Tx] = []
    for w in idx.txs:
        tag = (w.tag or "").lower()
        if w.type != "withdrawal" or w.origin == "plan" or not (w.from_account and w.from_asset and w.from_qty):
            continue
        if idx.asset(w.from_asset).is_fiat or w.tx_id in taken:
            continue
        comp, cdate, _note = compromised(idx, w.from_account)
        if tag in ("lost", "stolen"):
            loss_groups[("C", w.from_account, w.from_asset)].append(w)  # vom Nutzer als Verlust gebucht
            continue
        if tag in NO_COUNTERPART_TAGS or tag not in NEUTRAL_TAGS:
            continue  # Zahlung, Gebühr, Schenkung …: keine Gegenbuchung zu erwarten
        after_hack = comp and (cdate is None or w.date >= cdate)
        if not after_hack and w.value_eur is not None and abs(w.value_eur) < LOSS_MIN_EUR:
            small += 1
            continue
        # auch auf einem kompromittierten Konto zuerst nach eigenen Zielen suchen (Rettungsüberweisung)
        alts = _alternatives(idx, w, deps, all_deps, nets, taken)
        unmatched.append(w)
        if alts and alts[0][0] >= ALT_LINK_SCORE:
            alt_groups[(w.from_account, w.from_asset)].append((w, alts))
        else:
            # kompromittiert ist kein Verlustnachweis: ohne dokumentierte Verlustbuchung bleibt es ungeklärt (B)
            loss_groups[("B", w.from_account, w.from_asset)].append(w)
    idx.unmatched_out = unmatched
    out: list[Finding] = []
    best_of: dict[str, list[str]] = defaultdict(list)  # Zugang → Abgänge, für die er der beste Kandidat ist
    for items in alt_groups.values():
        for w, alts in items:
            best_of[alts[0][1].tx_id].append(w.tx_id)
    for (acc, aid), items in sorted(alt_groups.items()):
        items.sort(key=lambda x: (x[0].ts, x[0].tx_id))
        evidence, pairs, link_pairs = [], [], []
        best_scores = []
        for w, alts in items:
            best = alts[0]
            best_scores.append(best[0])
            comp, _cd, note = compromised(idx, acc)
            evidence.append(f"{_ts(w.ts)} −{_q(w.from_qty)} {aid} von {acc} ({w.tx_id}) – mögliche Gegenbuchungen:")
            for sc_, d, why in alts:
                same = bool(idx.hashes[w.tx_id] & idx.hashes[d.tx_id])
                evidence.append(f"  {evidence_label(sc_, identity=same)} (Matching-Score {sc_}/95, regelbasiert): "
                                f"{_d(d.ts)} +{_q(d.to_qty)} {d.to_asset} auf {d.to_account} ({d.tx_id}) – "
                                f"{'; '.join(why)}")
            if comp:
                evidence.append(f"  Konto als kompromittiert gekennzeichnet („{note}“) – ein Abgang an ein eigenes "
                                "Konto kann eine Rettungsüberweisung sein")
            same = bool(idx.hashes[w.tx_id] & idx.hashes[best[1].tx_id])
            pairs.append((idx.ref(w), idx.ref(best[1]), f"{evidence_label(best[0], identity=same)} (Matching-Score "
                                                        f"{best[0]}/95): " + "; ".join(best[2])))
            many_out = len(best_of[best[1].tx_id]) > 1
            ambiguous = (len(alts) > 1 and alts[1][0] >= best[0] - 15) or many_out
            if ambiguous:
                evidence.append(f"  mehrdeutig – {'mehrere ähnlich gute Ziele' if len(alts) > 1 else ''}"
                                f"{' bzw. ' if len(alts) > 1 and many_out else ''}"
                                f"{'derselbe Zugang passt zu mehreren Abgängen' if many_out else ''}"
                                " – keine Verknüpfung vorgeschlagen")
            if best[1].to_asset == aid and not ambiguous:
                link_pairs.append([w.tx_id, best[1].tx_id])
        top = max(best_scores)
        status = "verdacht" if top >= 70 and link_pairs else "hinweis"
        n = len(items)
        f = Finding(
            kind="transfer", status=status, priority=2 if status == "verdacht" else 3,
            title=f"{n} Abg{'ang' if n == 1 else 'änge'} ohne Gegenbuchung mit möglichen Zielen: {acc} · {aid}",
            known=[f"{n} Auszahlung(en) von {acc} ohne verknüpfte Gegenbuchung; auf eigenen Konten gibt es passende "
                   "Zugänge (Menge, Zeit, ggf. Hash bzw. Tokenwechsel).",
                   "Ohne Verknüpfung zählt der Abgang als Abgang ohne Gegenbuchung und der Zugang als neue "
                   "Anschaffung."],
            suspected=["Eigener Transfer, dessen Seiten getrennt gebucht sind – je Abgang ist der Kandidat mit der "
                       "stärksten Übereinstimmung genannt; Alternativen stehen unter „Belege“."],
            evidence=evidence[:60],
            uncertainty=["Matching-Scores sind regelbasierte Punktwerte aus Hash, Menge, Zeit, Netzwerk und "
                         "Tokenwechsel – keine kalibrierten Wahrscheinlichkeiten und ohne Hash/Referenz kein "
                         "Identitätsnachweis. Bei mehreren ähnlich guten Zielen schlägt Portfolia keine Verknüpfung "
                         "vor.",
                         "Zieladressen fehlen in den meisten Buchungen; Bridge-Vorgänge sind nur über „related_asset“ "
                         "erkennbar."],
            pairs=pairs[:40],
            decision="Je Abgang prüfen (Explorer, Kontoauszug) und den passenden Zugang als internen Transfer "
                     "verknüpfen; Portfolia legt keine Verknüpfung automatisch an.",
            key=f"transfer-alt|{acc}|{aid}|" + "|".join(w.tx_id for w, _a in items), weight=Decimal(top),
            data={"type": "transfer", "pairs": link_pairs, "alternatives": {w.tx_id: [[d.tx_id, s] for s, d, _w in a]
                                                                          for w, a in items}})
        f.txs = [x for w, a in items[:30] for x in (idx.ref(w), idx.ref(a[0][1]))]
        out.append(idx.attach(f))
    documented = [t for (cls, _a, _x), txs in loss_groups.items() if cls == "C" for t in txs
                  if (t.tag or "").lower() in ("lost", "stolen")]
    for (cls, acc, aid), txs in sorted(loss_groups.items()):
        rest = [t for t in txs if (t.tag or "").lower() not in ("lost", "stolen")]
        if rest:
            out.append(_loss_finding(idx, cls, acc, aid, rest))
    if documented:
        out.append(_documented_losses(idx, documented))
    stats["outflows_unmatched"] = len(unmatched)
    stats["outflows_small"] = small
    return out


def _documented_losses(idx: _Index, txs: list[Tx]) -> Finding:
    """Als Verlust/Diebstahl gebuchte Abgänge (Tag „lost“/„stolen“, auch Ausbuchungen wertloser Positionen) – eine
    Übersicht, Einordnung C (dokumentiert), keine Handlung nötig."""
    from app.diagnosis.engine import _d, _q

    txs.sort(key=lambda t: (t.ts, t.tx_id))
    by_acc: dict[str, list[Tx]] = defaultdict(list)
    for t in txs:
        by_acc[t.from_account or ""].append(t)
    val = sum((abs(t.value_eur) for t in txs if t.value_eur is not None), ZERO)
    known = [f"{len(txs)} Abgänge auf {len(by_acc)} Konten sind als Verlust bzw. Diebstahl gebucht (Tag „lost“/"
             f"„stolen“) – Wert zum Buchungszeitpunkt, soweit bekannt: {eur(val)}.",
             f"Einordnung: C – {LOSS_CLASS['C'][0]}, dokumentiert durch die Benutzerklassifikation der Buchung "
             "(Tag); nicht extern unabhängig verifiziert. Bereits als Abgang ohne Gegenwert erfasst."]
    for acc, lst in sorted(by_acc.items()):
        parts = "; ".join(f"{_q(t.from_qty)} {t.from_asset} ({_d(t.ts)})" for t in lst[:6])
        known.append(f"{acc}: {parts}" + (f" … und {len(lst) - 6} weitere" if len(lst) > 6 else ""))
    f = Finding(kind="loss", status="hinweis", priority=3,
                title=f"Dokumentierte Verluste und Ausbuchungen: {len(txs)} Abgänge auf {len(by_acc)} Konten",
                known=known,
                uncertainty=["Ausbuchungen wertloser Positionen und Verluste durch Hack stehen hier gemeinsam – die "
                             "Ursache steht in der jeweiligen Buchung (Notiz).",
                             "Ob ein Verlust steuerlich geltend gemacht werden kann, ist eine eigene Frage."],
                decision="Keine Korrektur nötig; bei Zweifeln die einzelne Buchung im Journal prüfen.",
                key="loss|C|documented|" + "|".join(t.tx_id for t in txs), weight=val,
                data={"type": "loss", "class": "C", "txs": [t.tx_id for t in txs], "documented": True,
                      "value_eur": str(val)})
    f.txs = [idx.ref(t) for t in txs[:40]]
    return idx.attach(f, derive_positions=False)


def _loss_finding(idx: _Index, cls: str, acc: str, aid: str, txs: list[Tx]) -> Finding:
    from app.diagnosis.engine import _d, _q

    txs.sort(key=lambda t: (t.ts, t.tx_id))
    qty = sum((t.from_qty or ZERO for t in txs), ZERO)
    vals = [t.value_eur for t in txs]
    val = sum((abs(v) for v in vals if v is not None), ZERO)
    valued = all(v is not None for v in vals)
    comp, cdate, note = compromised(idx, acc)
    tagged = all((t.tag or "").lower() in ("lost", "stolen") for t in txs)
    label, expl = LOSS_CLASS[cls]
    known = [f"{len(txs)} Abgang/Abgänge von {acc}: zusammen {_q(qty)} {aid}"
             + (f", Wert zum Buchungszeitpunkt {eur(val)}" if valued and val else
                ", Wert zum Buchungszeitpunkt nicht für alle Buchungen bekannt – kein Verlustbetrag berechnet"
                if not valued else "") + f" ({_d(txs[0].ts)}–{_d(txs[-1].ts)}).",
             f"Einordnung: {cls} – {label} ({expl})."]
    evidence: list[str] = []
    uncertainty: list[str] = []
    if cls == "C" and tagged:
        status = "hinweis"
        known.append("Als Verlust bzw. Diebstahl gebucht (Tag „lost“/„stolen“) – bereits als Abgang ohne Gegenwert "
                     "erfasst.")
        uncertainty.append("Ob ein Verlust steuerlich geltend gemacht werden kann, ist eine eigene Frage.")
    else:
        status = "verdacht" if valued and val >= LOSS_ALERT_EUR else "hinweis"
        known.append("Auf keinem eigenen Konto gibt es einen passenden Zugang (Menge 50–100,1 %, −2 h … +14 Tage, "
                     "auch Tokenwechsel über den Wert).")
        if comp:
            after = cdate is None or any(t.date >= cdate for t in txs)
            evidence.append(f"Konto als kompromittiert gekennzeichnet („{note}“)"
                            + (f", Abgänge ab dem genannten Datum {_d(cdate)}" if cdate and after else
                               ", Datum unbekannt" if cdate is None else ", Abgänge vor dem genannten Datum"))
            uncertainty.insert(0, "Die Kennzeichnung als kompromittiert beweist keinen Diebstahl: Rettungsüberweisung "
                                  "an eine nicht erfasste eigene Wallet, Verkauf oder eigener Übertrag sind möglich. "
                                  "Ein Verlust gilt erst als dokumentiert, wenn er als solcher gebucht ist.")
            if after:
                status = "verdacht"
        uncertainty += ["Auszahlung an eine eigene, nicht erfasste Wallet bzw. Börse ist möglich (Zielkonto fehlt "
                        "im System).",
                        "Auszahlung an Dritte (Zahlung, Verkauf außerhalb) ist möglich – dann ist es kein Verlust.",
                        "Kursverfall oder Illiquidität sind kein Nachweis eines Verlusts."]
    rec_txs = txs[:40]
    f = Finding(
        kind="loss", status=status, priority=2 if status in ("wahrscheinlich", "verdacht") else 3,
        title=f"{label}: {_q(qty)} {aid} von {acc}" + (f" ({len(txs)} Abgänge)" if len(txs) > 1 else ""),
        known=known, evidence=evidence, uncertainty=uncertainty,
        suspected=[] if cls == "C" and tagged else
        ["Vermögensabgang ohne nachvollziehbaren Gegenwert – Ziel unbekannt."],
        decision="Zieladressen bzw. Kontoauszug prüfen: eigenes Konto → Konto erfassen und als Transfer verknüpfen; "
                 "Dritte → als Zahlung/Verkauf einordnen; belegter Verlust → als Verlust buchen. Ohne Beleg als "
                 "ungeklärt markieren. Portfolia bucht nichts automatisch.",
        key=f"loss|{cls}|{acc}|{aid}|" + "|".join(t.tx_id for t in txs), weight=val,
        data={"type": "loss", "class": cls, "account": acc, "asset": aid, "txs": [t.tx_id for t in txs],
              "value_eur": str(val) if valued else None})
    f.txs = [idx.ref(t) for t in rec_txs]
    return idx.attach(f)


def missing_at_check(idx: _Index, rows: list[HoldingRow]) -> list[Finding]:
    """Historischer Bestand fehlt bei der aktuellen, vollständigen Wallet-/Börsenprüfung → ungeklärter Abgang (B).
    Veraltete bzw. unvollständige Abrufe oder Fehler der Datenquelle erzeugen keinen solchen Befund."""
    from app.diagnosis.engine import DUST, _q, _ts

    out: list[Finding] = []
    for row in rows:
        base = row.computed_at_obs if row.computed_at_obs is not None else row.computed
        if row.status != "extern_diff" or row.observed is None or base <= DUST or row.observed > DUST:
            continue
        v = idx.value(row.asset, base)
        f = Finding(
            kind="loss", status="verdacht", priority=2,
            title=f"{LOSS_CLASS['B'][0]}: {_q(base)} {row.asset} auf {row.account} fehlen bei der Bestandsprüfung",
            known=[f"Soll aus den Buchungen zum Abrufzeitpunkt: {_q(base)} {row.asset}"
                   + (f" (≈ {eur(v)} zum aktuellen Kurs)" if v is not None else "") + ".",
                   f"Aktueller, vollständiger Abruf ({row.observed_by}, "
                   f"{_ts(row.observed_at) if row.observed_at else '–'}) meldet {_q(row.observed)}.",
                   f"Einordnung: B – {LOSS_CLASS['B'][0]} ({LOSS_CLASS['B'][1]})."],
            suspected=["Abgang ohne Buchung (z. B. nicht erfasster Transfer, Kompromittierung) oder falsche "
                       "Asset-Zuordnung des Anbieters (anderes Netzwerk, Migration)."],
            uncertainty=["Ein Netzwerk-/Token-Wechsel (Migration, Bridge) kann den Bestand unter anderer Kennung "
                         "führen – erst Asset-Zuordnung und Explorer prüfen."],
            decision="Explorer bzw. Anbieter prüfen; fehlende Buchung ergänzen oder bei Beleg als Verlust buchen.",
            key=f"loss|missing|{row.account}|{row.asset}", weight=v or ZERO, accounts=[row.account],
            assets=[row.asset], positions=[(row.account, row.asset)],
            data={"type": "loss", "class": "B", "account": row.account, "asset": row.asset, "txs": [],
                  "value_eur": str(v) if v is not None else None})
        out.append(idx.attach(f, derive_positions=False))
    return out


# ----------------------------------------------------------------------------------------------------
# Inaktive Konten mit Restbestand
# ----------------------------------------------------------------------------------------------------

def inactive_accounts(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    from app.diagnosis.engine import DUST, _d, _q

    now = idx.snap.now
    last = account_last(idx)
    by_acc: dict[str, list[tuple[str, Decimal]]] = defaultdict(list)
    for (acc, aid), q in sorted(idx.led.balances.items()):
        fiat = idx.asset(aid).is_fiat
        if (q > DUST and not fiat) or (fiat and q >= Decimal("0.01")):
            by_acc[acc].append((aid, q))
    srcs: dict[str, list[Any]] = defaultdict(list)
    for s in idx.snap.sources:
        srcs[s.account].append(s)
    observed: dict[tuple[str, str], Decimal] = {}
    src_by_id = {s.id: s for s in idx.snap.sources}
    for o in idx.snap.observed:
        s = src_by_id.get(o.source_id)
        if s is not None and o.asset_id:
            observed[(s.account, o.asset_id)] = observed.get((s.account, o.asset_id), ZERO) + o.qty
    out: list[Finding] = []
    for acc, pos in sorted(by_acc.items()):
        lt = last.get(acc)
        if lt is None or now - lt[0] < INACTIVE_AFTER:
            continue
        comp, cdate, _note = compromised(idx, acc)
        known = [f"Letzte Buchung: {_d(lt[0])} ({lt[1]}) – vor {(now - lt[0]).days} Tagen."]
        sync_txt, states = _sync_state(srcs.get(acc, []), now)
        known.append(sync_txt)
        rows, total, unvalued = [], ZERO, 0
        for aid, q in pos:
            v = idx.value(aid, q)
            if v is None:
                unvalued += 1
            else:
                total += v
            obs = observed.get((acc, aid))
            rows.append(f"{_q(q)} {aid}" + (f" ≈ {eur(v)}" if v is not None else " (ohne aktuellen Kurs)")
                        + (f"; Anbieter meldet {_q(obs)}" if obs is not None else ""))
        known.append(f"Restbestände ({len(pos)}): " + "; ".join(rows[:MAX_LIST])
                     + (" …" if len(rows) > MAX_LIST else ""))
        cats = []
        if comp:
            cats.append("Konto als kompromittiert gekennzeichnet" + (f" (seit {_d(cdate)})" if cdate else "")
                        + " – Verbleib der Restbestände prüfen")
        if "error" in states:
            cats.append("Datenquelle meldet Fehler – Verbindung prüfen (kein Hinweis auf Verlust)")
        if "never" in states:
            cats.append("Datenquelle angelegt, aber noch nie erfolgreich synchronisiert")
        elif "stale" in states:
            cats.append("nicht mehr synchronisiert (letzte erfolgreiche Synchronisation älter als 7 Tage)")
        elif "fresh" in states:
            cats.append("Datenquelle aktuell – Bestände werden extern bestätigt (siehe Bestandsabgleich)")
        if not states:
            cats.append("ohne Datenquelle: Bestand nur aus Import/Buchungen – langfristige Verwahrung möglich, nicht "
                        "extern bestätigt")
        if unvalued:
            cats.append(f"{unvalued} Asset(s) ohne aktuellen Kurs – möglicherweise delistet, migriert oder nicht "
                        "unterstützt (siehe Befunde Kurs/Migration)")
        for aid, _q2 in pos:
            for g in idx.findings_by_pos.get((acc, aid), []):
                if g.kind == "migration":
                    cats.append(f"möglicher Migrationsvorgang: {g.title}")
        status = "verdacht" if comp else "hinweis"
        f = Finding(
            kind="inactive", status=status, priority=2 if comp else 3,
            title=f"Inaktiv seit {_d(lt[0])}, Restbestand: {acc}" + (f" ≈ {eur(total)}" if total else ""),
            known=known, suspected=[f"Einordnung: {c}" for c in dict.fromkeys(cats)],
            uncertainty=["Inaktivität allein ist kein Verlust: langfristige Verwahrung ohne Transaktionen ist normal.",
                         "Neuere Bewegungen außerhalb der erfassten Daten (z. B. nicht synchronisierte Wallet, "
                         "Auszahlung an ein nicht erfasstes Konto) sind möglich."],
            decision="Konto beim Anbieter bzw. im Explorer prüfen; Datenquelle (neu) verbinden oder den Kontostand als "
                     "Referenzbestand hinterlegen. Erst bei Beleg (Hack, Delisting, Ausbuchung) weiter vorgehen.",
            key=f"inactive|{acc}", weight=total, accounts=[acc], assets=[a for a, _q2 in pos],
            positions=[(acc, a) for a, _q2 in pos],
            data={"type": "inactive", "account": acc, "last_tx": lt[0].isoformat(), "value_eur": str(total),
                  "compromised": comp})
        out.append(idx.attach(f, derive_positions=False))
    stats["inactive_accounts"] = len(out)
    return out


def _sync_state(srcs: list[Any], now: datetime) -> tuple[str, set[str]]:
    from app.diagnosis.engine import _d

    if not srcs:
        return "Keine Datenquelle verbunden – letzte Synchronisation: nie.", set()
    parts, states = [], set()
    for s in srcs:
        if s.last_error and s.status == "error":
            states.add("error")
        if s.last_success_at is None:
            states.add("never")
            st = "noch nie erfolgreich"
        elif now - s.last_success_at > SYNC_STALE:
            states.add("stale")
            st = f"zuletzt erfolgreich {_d(s.last_success_at)}"
        else:
            states.add("fresh")
            st = f"zuletzt erfolgreich {_d(s.last_success_at)}"
        parts.append(f"{s.provider_label} „{s.name}“ ({'aktiv' if s.enabled else 'deaktiviert'}, {st}"
                     + (f", letzter Fehler: {s.last_error[:120]}" if s.last_error else "") + ")")
    return "Letzte erfolgreiche Synchronisation: " + "; ".join(parts) + ".", states


# ----------------------------------------------------------------------------------------------------
# Referenzbestände: Soll-Ist zum selben Stichtag
# ----------------------------------------------------------------------------------------------------

def soll_at_date(idx: _Index, acc: str, aid: str, d: date) -> Decimal:
    """Bestand aus allen wirksamen Buchungen bis einschließlich Stichtag (Buchungsdatum in Ortszeit) – aktueller
    Ledger-Bestand minus Wirkung späterer Buchungen (damit identisch zur Bestandsberechnung)."""
    later = sum((v for _ts, dd, v, _id in deltas(idx).get((acc, aid), ()) if dd > d), ZERO)
    return idx.bal(acc, aid) - later


def reference_instant(ref: Any) -> datetime | None:
    """Vergleichszeitpunkt eines Referenzbestands: exakter Zeitpunkt, sonst Ende des Stichtags in der angegebenen
    Zeitzone; ohne beides (ältere Einträge) None → Vergleich über das Buchungsdatum in Ortszeit."""
    at = getattr(ref, "at", None)
    if at is not None:
        return at
    tz = getattr(ref, "tz", None)
    if not tz:
        return None
    from datetime import UTC, time
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(tz)
    except Exception:
        return None
    return datetime.combine(ref.as_of, time(23, 59, 59, 999999), tzinfo=zone).astimezone(UTC)


def soll_at_time(idx: _Index, acc: str, aid: str, when: datetime) -> Decimal:
    """Bestand zum Zeitpunkt ``when`` (z. B. Abruf eines externen Bestands) – ohne Buchungen danach."""
    later = sum((v for ts, _dd, v, _id in deltas(idx).get((acc, aid), ()) if ts > when), ZERO)
    return idx.bal(acc, aid) - later


def apply_reference(idx: _Index, row: HoldingRow, ref: Any) -> None:
    """Referenzbestand in die Zeile übernehmen (Soll zum selben Stichtag) und Status setzen, sofern kein aktueller
    externer Bestand vorrangig ist."""
    from app.diagnosis.engine import _q

    row.reference = ref.qty
    row.reference_at = ref.as_of
    row.reference_note = (ref.note or "") + (f" ({ref.source})" if ref.source else "") + (
        "; Wertstellungsdatum – Buchungsdatum kann abweichen" if getattr(ref, "basis", None) == "value" else "")
    at = reference_instant(ref)
    row.reference_ts = at
    row.soll_at_ref = soll_at_time(idx, row.account, row.asset, at) if at is not None else \
        soll_at_date(idx, row.account, row.asset, ref.as_of)
    if row.status in ("extern_ok", "extern_diff"):
        return
    # Fiat centgenau, Krypto exakt (volle Präzision, keine Toleranz)
    if idx.asset(row.asset).is_fiat:
        eq = (row.reference.quantize(Decimal("0.01")) == row.soll_at_ref.quantize(Decimal("0.01")))
    else:
        eq = row.ref_diff == 0
    row.status = "ref_ok" if eq else "ref_diff"
    if not eq:
        row.explanations.insert(0, f"Referenzbestand {_q(ref.qty)} zum {ref.as_of.strftime('%d.%m.%Y')} − Soll zum "
                                   f"selben Stichtag {_q(row.soll_at_ref)} = {_signed(row.ref_diff or ZERO)}")
    if row.soll_at_ref != row.computed:
        row.explanations.append(f"Buchungen nach dem Stichtag ändern den Bestand um "
                                f"{_signed(row.computed - row.soll_at_ref)} (nicht Teil des Vergleichs)")


def reference_finding(idx: _Index, row: HoldingRow) -> Finding | None:
    """Befund „Differenz zum Referenzbestand“ mit Zerlegung nach Quellen und möglichen Ursachen."""
    from app.diagnosis.engine import _d, _q

    if row.status != "ref_diff" or row.ref_diff is None:
        return None
    acc, aid = row.account, row.asset
    diff = row.ref_diff
    from app.diagnosis.engine import _ts

    stamp = _ts(row.reference_ts) if row.reference_ts else f"Ende {_d(row.reference_at)} (Ortszeit)"
    known = [f"Referenzbestand (Prüfwert, keine Buchung) zum {stamp}: {_q(row.reference)} {aid}"
             + (f" – {row.reference_note.strip()}" if row.reference_note.strip() else "") + ".",
             f"Soll aus allen wirksamen Buchungen bis zum selben Stichtag: {_q(row.soll_at_ref)} {aid}.",
             f"Differenz Ist − Soll: {_signed(diff)} {aid}."]
    for fam, net, n, first, last_ in families_of(idx, acc, aid):
        known.append(f"Anteil „{family_label(fam)}“: {_signed(net)} {aid} aus {n} Buchungen ({_d(first)}–{_d(last_)})")
    causes: list[str] = []
    ids: list[str] = []
    econ = getattr(idx, "econ", {})
    rank = {"wahrscheinlich": 0, "verdacht": 1, "hinweis": 2}
    for st, k, d, _side in sorted(econ.get((acc, aid), []), key=lambda p: (rank.get(p[0], 9), p[2].ts, p[2].tx_id)):
        if d.date > (row.reference_at or date.max):
            continue
        from app.diagnosis.engine import _effect

        eff = _effect([d]).get((acc, aid), ZERO)
        causes.append(f"{'Doppelbuchung' if st != 'hinweis' else 'mögliche Doppelbuchung (ohne Beleg)'} „{st}“: "
                      f"{k.tx_id} / {d.tx_id} – Wirkung der zweiten Buchung {_signed(eff)} {aid}")
        ids += [k.tx_id, d.tx_id]
    rest = diff + sum((_eff_of(idx, d, acc, aid) for st, _k, d, _s in econ.get((acc, aid), [])
                       if st != "hinweis" and d.date <= (row.reference_at or date.max)), ZERO)
    if causes:
        causes.append(f"ohne die Doppelbuchungen „wahrscheinlich“/„verdacht“ verbliebe eine Differenz von "
                      f"{_signed(rest)} {aid}")
    # Sparplan-Ausführungen nach dem Ende der übrigen Quellen: Finanzierung fehlt in den Daten
    others = [ts for fm, ts in _family_last(idx, acc).items() if fm not in PRIMARY_EXCLUDE]
    last_other = max(others) if others else None
    plan = [t for t in idx.txs if family(idx, t) == "sparplan" and t.date <= (row.reference_at or date.max)
            and (last_other is None or t.ts > last_other) and _eff_of(idx, t, acc, aid) < 0]
    if plan:
        eff = sum((_eff_of(idx, t, acc, aid) for t in plan), ZERO)
        causes.append(f"{len(plan)} Sparplan-Ausführung(en) nach dem Ende der Quellhistorie "
                      f"({', '.join(t.tx_id for t in plan[:6])}): Wirkung {_signed(eff)} {aid}, Einzahlung dazu fehlt; "
                      f"ohne sie verbliebe eine Differenz von {_signed(diff + eff)} {aid}")
        ids += [t.tx_id for t in plan]
    for g in idx.findings_by_pos.get((acc, aid), []):
        if g.kind in ("history", "estimated", "transfer") and g.title not in causes:
            causes.append(f"{g.kind_label}: {g.title}")
    if not causes:
        causes.append("keine Ursache in den Daten erkennbar – fehlende Ein-/Auszahlungen, Zinsen bzw. Gebühren oder "
                      "eine falsch zugeordnete Buchung möglich")
    row.tx_ids = list(dict.fromkeys(ids))
    status = "belegt"
    f = Finding(
        kind="holdings", status=status, priority=1,
        title=f"Differenz zum Referenzbestand: {aid} auf {acc} ({_signed(diff)})",
        known=known, suspected=[f"mögliche Ursache: {c}" for c in causes],
        uncertainty=["Der Referenzbestand stammt vom Nutzer (z. B. Kontoauszug); Portfolia prüft ihn nicht. "
                     "Wertstellung und Buchungsdatum können um Tage abweichen – Stichtag prüfen."],
        decision="Ursachen der Reihe nach klären (Doppelbuchungen verknüpfen, fehlende Buchungen ergänzen). Eine "
                 "Ausgleichsbuchung nur, um die Differenz zu schließen, bietet Portfolia hier bewusst nicht an.",
        key=f"reference|{acc}|{aid}|{row.reference_at}|{row.reference}", weight=abs(diff), accounts=[acc],
        assets=[aid], positions=[(acc, aid)],
        data={"type": "reference", "account": acc, "asset": aid, "reference": str(row.reference),
              "as_of": row.reference_at.isoformat() if row.reference_at else None, "soll": str(row.soll_at_ref),
              "txs": row.tx_ids})
    f.txs = [idx.ref(idx.by_id[x]) for x in row.tx_ids[:40] if x in idx.by_id]
    return idx.attach(f, derive_positions=False)


def _eff_of(idx: _Index, t: Tx, acc: str, aid: str) -> Decimal:
    from app.diagnosis.engine import _effect

    return _effect([t]).get((acc, aid), ZERO)


def identity(idx: _Index, aid: str) -> str:
    """Asset-Identität: Asset-ID, dazu bekannte Netzwerk-/Contract-Schlüssel (gleichnamige Tokens anderer Netzwerke
    sind eigene Assets)."""
    keys = idx.snap.token_keys.get(aid) or []
    return aid + (f" · {', '.join(sorted(keys)[:3])}" if keys else "")


def platform(idx: _Index, acc: str) -> str:
    info = idx.pf.accounts.get(acc)
    labels = sorted({s.provider_label for s in idx.snap.sources if s.account == acc})
    return ", ".join([*([info.broker] if info and info.broker else []), *labels]) or "–"


# ----------------------------------------------------------------------------------------------------
# Einzelvorgänge: Sammelbefunde in einzeln prüf- und freigebbare Vorgänge zerlegen
# ----------------------------------------------------------------------------------------------------

def _case_title(idx: _Index, ids: list[str]) -> str:
    from app.diagnosis.engine import _d, _q

    parts = []
    for tid in ids:
        t = idx.by_id.get(tid)
        if t is None:
            continue
        q, a = (t.to_qty, t.to_asset) if t.to_qty and t.to_asset else (t.from_qty, t.from_asset)
        parts.append(f"{_q(q)} {a} ({tid})")
    first = idx.by_id.get(ids[0]) if ids else None
    return f"Vorgang {_d(first.ts) if first else ''}: " + " ↔ ".join(parts)


def split_cases(idx: _Index, findings: list[Finding]) -> list[Finding]:
    """Je Sammelbefund (mehrere Paare) einen eigenen Befund je Vorgang. Kennung und Prüfsumme hängen nur an den
    beteiligten Buchungen und ihrer Evidenz – nicht an der Gruppe –, damit eine Entscheidung zu einem Vorgang auch
    bestehen bleibt, wenn sich andere Vorgänge der Gruppe ändern."""
    from app.diagnosis.engine import _scenario_without

    out: list[Finding] = []
    for f in list(findings):
        d = f.data or {}
        typ = d.get("type")
        cases: list[tuple[list[str], dict[str, Any]]] = []
        if typ == "econ_pairs" and d.get("pairs") and not d.get("multi"):
            info = {tuple(c["pair"]): c for c in d.get("cases") or []}
            for pair in d["pairs"]:
                c = info.get(tuple(pair), {})
                cases.append((pair, {"type": "econ_pairs", "pairs": [pair], "account": d.get("account"),
                                     "asset": d.get("asset"), "side": d.get("side"),
                                     "keep_family": d.get("keep_family"), "drop_family": d.get("drop_family"),
                                     "case": {k: c.get(k) for k in ("level", "how", "identity", "ambiguous",
                                                                    "alternatives", "pro", "contra", "missing")},
                                     "_status": c.get("status", f.status)}))
        elif typ == "hash_pairs" and d.get("pairs"):
            for pair, h in zip(d["pairs"], d.get("hashes") or [], strict=False):
                cases.append((pair, {"type": "hash_pairs", "pairs": [pair], "hashes": [h],
                                     "accounts": d.get("accounts")}))
        elif typ == "transfer" and not d.get("ambiguous"):
            alts = d.get("alternatives")
            if alts:
                links = dict(d.get("pairs") or [])
                for w, lst in alts.items():
                    pair = [w, links[w]] if w in links else [w, lst[0][0]] if lst else [w]
                    cases.append((pair, {"type": "transfer", "pairs": [pair] if w in links else [],
                                         "alternatives": {w: lst}}))
            elif not alts and d.get("pairs"):
                for pair in d["pairs"]:
                    cases.append((pair, {"type": "transfer", "pairs": [pair]}))
        if not cases:
            continue
        why_by = {(a.tx_id, b.tx_id): why for a, b, why in f.pairs}
        for pair, data in cases:
            status = data.pop("_status", f.status)
            c = data.get("case") or {}
            refs = [idx.ref(idx.by_id[x]) for x in pair if x in idx.by_id]
            child = Finding(
                kind=f.kind, status=status, priority=f.priority, title=_case_title(idx, pair),
                known=[idx.describe(idx.by_id[x]) for x in pair if x in idx.by_id]
                + ([f"Evidenzstufe: {c['level']}."] if c.get("level") else [])
                + ([f"Mehrere mögliche Partner (auch {', '.join(c['alternatives'][:4])}) – Entscheidung "
                    "zurückgestellt."] if c.get("ambiguous") else []),
                evidence=list(c.get("pro") or []) or ([why_by[tuple(pair)]] if tuple(pair) in why_by else []),
                uncertainty=list(c.get("contra") or []) or list(f.uncertainty),
                suspected=[f"noch offen: {m}" for m in c.get("missing") or []] or list(f.suspected),
                decision=f.decision, data=data, parent=f.id,
                key=f"case|{data['type']}|{f.kind}|" + "|".join(pair), weight=f.weight)
            if len(pair) == 2 and tuple(pair) in why_by:
                child.pairs = [(refs[0], refs[1], why_by[tuple(pair)])]
            child.txs = refs
            if data["type"] in ("econ_pairs", "hash_pairs") and len(pair) == 2 and pair[1] in idx.by_id:
                child.scenario = _scenario_without(idx, [idx.by_id[pair[1]]], "Szenario (hypothetisch): ohne die "
                                                   f"zweite Buchung {pair[1]} – es wird nichts gebucht.")
            idx.attach(child, derive_positions=False)
            child.positions = []
            f.children.append(child.id)
            out.append(child)
    return out


def case_states(snap: Any, findings: list[Finding]) -> None:
    """Stand der Nutzerentscheidung je Befund (Anzeige; ändert nichts): offen, später prüfen, ungeklärt, abgelehnt,
    überholt (Entscheidung zu älteren Daten)."""
    from app.diagnosis.actions import fingerprint
    from app.diagnosis.model import MARK_STATE

    marks = getattr(snap, "marks", {}) or {}
    for f in findings:
        m = marks.get(f.id)
        if m is None:
            f.state = "offen"
        elif m[1] and m[1] != fingerprint(f):
            f.state = "ueberholt"
        else:
            f.state = MARK_STATE.get(m[0], "offen")

