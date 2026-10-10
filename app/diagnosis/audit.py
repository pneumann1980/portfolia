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
    return abs(a - b) <= _tol(max(abs(a), abs(b)))


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

def econ_duplicates(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    from app.diagnosis.engine import _dur, _effect, _q, _scenario_without, _short, _ts

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

    cands: list[tuple[tuple[Any, ...], Leg, Leg, str]] = []
    partners: dict[str, int] = defaultdict(int)
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
                exact = 0 if how.startswith(("exakt", "brutto und")) else 1
                cands.append(((gap, exact, a.tx.tx_id, b.tx.tx_id), a, b, how))
                if gap <= ECON_WINDOW:
                    partners[a.tx.tx_id] += 1
                    partners[b.tx.tx_id] += 1
    cands.sort(key=lambda c: c[0])
    used: set[str] = set()
    chosen: list[tuple[Leg, Leg, str, timedelta, str]] = []
    pattern: dict[tuple[str, str, str, str, str, int], int] = defaultdict(int)
    for (gap, _e, _x, _y), a, b, how in cands:
        if a.tx.tx_id in used or b.tx.tx_id in used:
            continue
        used.update((a.tx.tx_id, b.tx.tx_id))
        unique = partners[a.tx.tx_id] <= 1 and partners[b.tx.tx_id] <= 1
        if gap <= STRONG_GAP and unique:
            status = "wahrscheinlich"
        elif gap <= ECON_WINDOW and (unique or gap <= STRONG_GAP):
            status = "verdacht"
        else:
            status = "hinweis"
            pk = _offset_key(a, b, gap)
            if pk is not None:
                pattern[pk] += 1
        chosen.append((a, b, how, gap, status))
    groups: dict[tuple[str, str, str, str, str, str], list[tuple[Leg, Leg, str, timedelta]]] = defaultdict(list)
    systematic: dict[str, int] = {}
    for a, b, how, gap, status in chosen:
        pk = _offset_key(a, b, gap) if status == "hinweis" else None
        if pk is not None and pattern[pk] >= SYSTEMATIC_MIN:
            status = "verdacht"  # regelmäßiger Versatz bei gleicher Uhrzeit: Zahlungs- vs. Ausführungsdatum
            systematic[a.tx.tx_id] = systematic[b.tx.tx_id] = pk[5]
        keep, drop = _keep_drop(a, b)
        groups[(status, a.account, a.asset, a.side, keep.fam, drop.fam)].append((keep, drop, how, gap))
    out: list[Finding] = []
    econ = getattr(idx, "econ", None)
    if econ is None:
        idx.econ = econ = defaultdict(list)
    for (status, acc, aid, side, fk, fd), items in sorted(groups.items()):
        items.sort(key=lambda x: (x[0].tx.ts, x[0].tx.tx_id))
        lk, ld = family_label(fk), family_label(fd)
        drops = [d.tx for _k, d, _h, _g in items]
        pairs, evidence = [], []
        for k, d, how, gap in items:
            econ[(acc, aid)].append((status, k.tx, d.tx, side))
            why = (f"gleiches Konto {acc}, Asset {aid}, {'Zugang' if side == 'in' else 'Abgang'}; Beträge {how}; "
                   f"Abstand {_dur(gap)}; Quellen „{lk}“ / „{ld}“")
            fund = _funding(idx, k, d)
            if fund:
                why += f"; {fund}"
            if k.tx.tx_id in systematic:
                why += (f"; regelmäßiger Versatz von {systematic[k.tx.tx_id]} Tagen bei gleicher Uhrzeit (mindestens "
                        f"{SYSTEMATIC_MIN} Paare) – typisch für Zahlungs- bzw. Wertstellungsdatum vs. Ausführung")
            pairs.append((idx.ref(k.tx), idx.ref(d.tx), why))
            evidence.append(f"{_ts(k.tx.ts)} {lk}: {_leg_text(k)} ({k.tx.tx_id}) ↔ {_ts(d.tx.ts)} {ld}: "
                            f"{_leg_text(d)} ({d.tx.tx_id})")
        n = len(items)
        eff = _effect(drops)
        total = sum((abs(v) for (a2, x), v in eff.items() if a2 == acc and x == aid), ZERO)
        word = "Zugänge" if side == "in" else "Abgänge"
        noun = ("Zugang" if side == "in" else "Abgang") if n == 1 else word
        title = (f"{n} wirtschaftlich gleiche{'r' if n == 1 else ''} {noun} aus zwei Quellen: {acc} · {aid}"
                 + (" (ohne ausreichenden Beleg)" if status == "hinweis" else ""))
        known = [f"{n} {'Paar' if n == 1 else 'Paare'}: gleiches Konto, gleiches Asset (Asset-ID {aid}), gleiche "
                 f"Richtung, Betrag brutto bzw. netto gleich – je eine Buchung aus „{lk}“ und aus „{ld}“.",
                 f"Beide Buchungen je Paar zählen derzeit: Bestand {acc} · {aid} "
                 f"{'um' if n == 1 else 'insgesamt um'} {_q(total)} {aid} {'zu hoch' if side == 'in' else 'zu niedrig'}"
                 f", falls es jeweils derselbe Vorgang ist.",
                 "Die Rohdaten beider Quellen bleiben unverändert erhalten."]
        if fd.startswith("sync:") and _curated(fk):
            known.append(f"„{ld}“ bildet die Buchung mit Gebühr (brutto) ab, „{lk}“ meist netto – unterschiedliche "
                         "Darstellung desselben Vorgangs ist typisch.")
        effect = "je Paar zählt eine Buchung zu viel" if status != "hinweis" else "möglich, aber nicht belegt"
        suspected = [f"Derselbe wirtschaftliche Vorgang ist über zwei Quellen erfasst (z. B. Steuertool-Import und "
                     f"Börsen-API) – {effect}."]
        if status == "wahrscheinlich":
            unc = ["Kein gemeinsamer Hash und keine gemeinsame Anbieter-Kennung: Die Zuordnung beruht auf Konto, "
                   "Asset, Richtung, Betrag (brutto/netto) und Zeit (≤ 1 h, je Buchung genau ein Partner). Zwei "
                   "echte, gleich hohe Vorgänge in derselben Stunde sind möglich – Kontoauszug prüfen."]
        elif status == "verdacht":
            unc = ["Abstand über 1 h bzw. mehrere mögliche Partner: Gleiche Höhe und zeitliche Nähe allein beweisen "
                   "keine Doppelbuchung."]
            if any(k.tx.tx_id in systematic for k, _d, _h, _g in items):
                unc.append("Bei regelmäßigen Vorgängen (Sparplan) kann der Versatz auch zwei verschiedene Ausführungen "
                           "verbinden – Kontoauszug bzw. Anzahl der Ausführungen je Monat prüfen.")
        else:
            unc = ["Gleiche Höhe und ein Abstand von mehreren Tagen sind kein Beleg für eine Doppelbuchung (z. B. zwei "
                   "gleich hohe Einzahlungen in einer Woche). Ohne Kontoauszug bzw. Anbieter-Kennung bleibt das "
                   "ungeklärt – Portfolia empfiehlt hier keine Korrektur.",
                   "Wirkung, falls doch doppelt: siehe Szenario; mit einem hinterlegten Referenzbestand zeigt der "
                   "Bestandsabgleich, ob das Szenario zum Kontoauszug passt."]
        hashes = sorted({_short(h) for k, d, _h, _g in items for h in idx.hashes[k.tx.tx_id] | idx.hashes[d.tx.tx_id]})
        f = Finding(
            kind="duplicate", status=status, priority={"wahrscheinlich": 1, "verdacht": 2}.get(status, 3),
            title=title, known=known, suspected=suspected, uncertainty=unc,
            evidence=evidence[:40] + ([f"… und {len(evidence) - 40} weitere"] if len(evidence) > 40 else [])
            + ([f"Hashes: {', '.join(hashes)}"] if hashes else []),
            pairs=pairs[:40],
            scenario=_scenario_without(idx, drops, f"Szenario (hypothetisch): je Paar nur die Buchung aus „{lk}“ "
                                                   f"gezählt (die aus „{ld}“ als enthalten verknüpft) – es wird "
                                                   "nichts gebucht."),
            decision=f"Je Paar mit Kontoauszug bzw. Historie der Börse prüfen. Nur wenn derselbe Vorgang: verknüpfen – "
                     f"die Buchung aus „{ld}“ zählt dann nicht mehr, bleibt mit Herkunft erhalten und lässt sich "
                     "zurücknehmen. Portfolia ändert nichts automatisch.",
            key=f"econ|{acc}|{aid}|{side}|" + "|".join(sorted(f"{k.tx.tx_id}>{d.tx.tx_id}" for k, d, _h, _g in items)),
            weight=total, positions=[(acc, aid)],
            data={"type": "econ_pairs", "pairs": [[k.tx.tx_id, d.tx.tx_id] for k, d, _h, _g in items],
                  "account": acc, "asset": aid, "side": side, "keep_family": fk, "drop_family": fd})
        f.txs = [x for k, d, _h, _g in items[:40] for x in (idx.ref(k.tx), idx.ref(d.tx))]
        if status != "hinweis":
            idx.dup_txs.update(t.tx_id for k, d, _h, _g in items for t in (k.tx, d.tx))
        out.append(idx.attach(f, derive_positions=False))
    stats["econ_pairs"] = sum(len(v) for v in groups.values())
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
    # Tokenwechsel (Bridge, Wrapped, Cross-Chain-Swap): verwandtes Asset laut Buchung oder gleicher EUR-Wert
    if w.value_eur:
        for d in all_deps:
            if d.to_asset == w.from_asset or d.to_account == w.from_account or d.tx_id in taken or not d.value_eur:
                continue
            if not (w.ts - ALT_BEFORE <= d.ts <= w.ts + timedelta(hours=24)):
                continue
            related = d.related_asset == w.from_asset or w.related_asset == d.to_asset
            vr = d.value_eur / w.value_eur
            if not related and not (Decimal("0.97") <= vr <= Decimal("1.01") and d.ts - w.ts <= timedelta(hours=6)):
                continue
            if not (Decimal("0.8") <= vr <= Decimal("1.02")):
                continue
            score = (30 if related else 10) + (15 if vr >= Decimal("0.97") else 5) + \
                (10 if abs(d.ts - w.ts) <= timedelta(hours=2) else 0)
            why = [f"Tokenwechsel {w.from_asset} → {d.to_asset}" + (" (verwandtes Asset laut Buchung)" if related else
                                                                     " (nur über den EUR-Wert zugeordnet)"),
                   f"EUR-Wert {(vr * 100).quantize(Decimal('0.1'))} %", f"Abstand {_dur(d.ts - w.ts)}"]
            out.append((min(70, score), d, why))
    out.sort(key=lambda x: (-x[0], x[1].ts, x[1].tx_id))
    return out[:ALT_SHOW]


def outflows(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    from app.diagnosis.engine import NEUTRAL_TAGS, _d, _q, _ts

    taken = set(getattr(idx, "transfer_txs", set())) | set(idx.dup_txs)
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
            loss_groups[("C", w.from_account, w.from_asset)].append(w)
            continue
        if tag in NO_COUNTERPART_TAGS or tag not in NEUTRAL_TAGS:
            continue  # Zahlung, Gebühr, Schenkung …: keine Gegenbuchung zu erwarten
        if comp and (cdate is None or w.date >= cdate):
            loss_groups[("C" if cdate else "B", w.from_account, w.from_asset)].append(w)
            unmatched.append(w)
            continue
        if w.value_eur is not None and abs(w.value_eur) < LOSS_MIN_EUR:
            small += 1
            continue
        alts = _alternatives(idx, w, deps, all_deps, nets, taken)
        unmatched.append(w)
        if alts and alts[0][0] >= ALT_LINK_SCORE:
            alt_groups[(w.from_account, w.from_asset)].append((w, alts))
        else:
            loss_groups[("B", w.from_account, w.from_asset)].append(w)
    idx.unmatched_out = unmatched
    out: list[Finding] = []
    for (acc, aid), items in sorted(alt_groups.items()):
        items.sort(key=lambda x: (x[0].ts, x[0].tx_id))
        evidence, pairs, link_pairs = [], [], []
        best_scores = []
        for w, alts in items:
            best = alts[0]
            best_scores.append(best[0])
            evidence.append(f"{_ts(w.ts)} −{_q(w.from_qty)} {aid} von {acc} ({w.tx_id}) – mögliche Gegenbuchungen:")
            for sc_, d, why in alts:
                evidence.append(f"  {sc_} % ({rating(sc_)}): {_d(d.ts)} +{_q(d.to_qty)} {d.to_asset} auf "
                                f"{d.to_account} ({d.tx_id}) – {'; '.join(why)}")
            pairs.append((idx.ref(w), idx.ref(best[1]), f"Sicherheit {best[0]} % ({rating(best[0])}): "
                                                        + "; ".join(best[2])))
            ambiguous = len(alts) > 1 and alts[1][0] >= best[0] - 15
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
            suspected=["Eigener Transfer, dessen Seiten getrennt gebucht sind – je Abgang ist die Gegenbuchung mit der "
                       "höchsten Sicherheit genannt; Alternativen stehen unter „Belege“."],
            evidence=evidence[:60],
            uncertainty=["Die Sicherheit ist eine Bewertung aus Hash, Menge, Zeit, Netzwerk und Tokenwechsel – keine "
                         "Gewissheit. Bei mehreren ähnlich guten Zielen schlägt Portfolia keine Verknüpfung vor.",
                         "Zieladressen fehlen in den meisten Buchungen; Bridge-Vorgänge sind nur über den Wert bzw. "
                         "„related_asset“ erkennbar."],
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
             f"Einordnung: C – {LOSS_CLASS['C'][0]} ({LOSS_CLASS['C'][1]}); bereits als Abgang ohne Gegenwert "
             "erfasst."]
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
    elif cls == "C":
        status = "wahrscheinlich"
        evidence.append(f"Konto als kompromittiert gekennzeichnet: „{note}“" + (f" (ab {_d(cdate)})" if cdate else ""))
        evidence.append("Abgänge ab diesem Datum ohne Gegenbuchung auf eigenen Konten")
        uncertainty.append("Ein Teil der Abgänge kann eine eigene Rettungs-Überweisung sein (Ziel nicht im System "
                           "erfasst) – Zieladressen im Explorer prüfen.")
    else:
        status = "verdacht" if valued and val >= LOSS_ALERT_EUR else "hinweis"
        known.append("Auf keinem eigenen Konto gibt es einen passenden Zugang (Menge 50–100,1 %, −2 h … +14 Tage, "
                     "auch Tokenwechsel über den Wert).")
        if comp:
            evidence.append(f"Konto als kompromittiert gekennzeichnet („{note}“), Datum unbekannt – Zusammenhang offen")
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
    row.reference_note = (ref.note or "") + (f" ({ref.source})" if ref.source else "")
    row.soll_at_ref = soll_at_date(idx, row.account, row.asset, ref.as_of)
    if row.status in ("extern_ok", "extern_diff"):
        return
    eq = abs(row.ref_diff or ZERO) <= _tol(row.soll_at_ref)
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
    known = [f"Referenzbestand (Prüfwert, keine Buchung) zum {_d(row.reference_at)}: {_q(row.reference)} {aid}"
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
