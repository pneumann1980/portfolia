"""Diagnose-Regeln: Befunde aus dem lesenden Schnappschuss (:mod:`app.diagnosis.collect`).

Grundsätze
* **Nur lesen.** Die Regeln arbeiten auf Kopien im Speicher; sie ändern keine Buchung, Zuordnung, Lots, Kurse oder
  Bestände. Auswirkungen erscheinen ausschließlich als **Szenario** (hypothetisch, nie gebucht).
* **Wissen ≠ Vermutung.** Jeder Befund trennt, was aus den Daten folgt (``known``), von dem, was nur vermutet wird
  (``suspected``), und nennt Belege, Unsicherheiten und die Entscheidung, die eine spätere Korrektur bräuchte.
* **Deterministisch.** Gleiche Daten → gleiche Befunde in gleicher Reihenfolge mit gleichen Kennungen.

Regeln (Kurzfassung, Details im README „Diagnose“)
* Dublette, gleicher Hash: gleiche Blockchain-Transaktion und identische Buchungsangaben. Unterschiedliche
  Ereignisindizes (Log-/Output-Index, Unterkennung) → legitim; fehlende Indizes → Verdacht.
* Dublette, gleiche Menge: dieselbe exakte Menge auf demselben Konto in ≤ 36 h, mindestens eine Buchung manuell.
* Dublette, gleiche Anbieter-Kennung: dieselbe Anbieter-ID (z. B. Bitpanda-UUID) im Import und in App-Buchungen.
* Transfer: Abgang und Zugang desselben Assets auf verschiedenen eigenen Konten (−2 h … +72 h, Menge 90–100,1 %
  oder gleicher Hash) ohne Verknüpfung.
* Asset: Anbieter-Kürzel mit abweichender Kursquelle (TH bei Bitpanda), mehrere Contracts je Asset, Kurszuordnung
  nur über das Symbol.
* Bestand: beobachtet (Börse/Blockchain) vs. berechnet; Soll des kuratierten Imports vs. berechnet.
* Historie, Schätzung, Kurs, Migration: siehe die jeweiligen Funktionen.
"""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from app.csvimport.events import identity_keys, normalize_hash, source_ref_keys
from app.csvimport.identity import PROVIDER_LABEL, confirms, identity, note_identity_keys, provider_of
from app.csvimport.reconcile import hashes_in
from app.diagnosis.collect import JournalMeta, Observed, Snapshot
from app.diagnosis.model import Finding, HoldingRow, Report, Scenario, TxRef
from app.importer import contract as C
from app.ledger.engine import DUST, REALIZED_KINDS, Disposal, DisposalPart, Lot
from app.ledger.models import AssetInfo, Tx
from app.web.fmt import eur, qty_exact

ZERO = Decimal(0)
DUP_WINDOW = timedelta(hours=36)
TRANSFER_BEFORE = timedelta(hours=2)  # Zugang darf (Uhrzeit-Ungenauigkeit) leicht vor dem Abgang liegen
TRANSFER_AFTER = timedelta(hours=72)
TRANSFER_MIN_RATIO = Decimal("0.9")
TRANSFER_MAX_RATIO = Decimal("1.001")
MIGRATION_TOL = Decimal("1e-4")
MIGRATION_POWERS = (3, 6, 9, 12, 18)
EXTERNAL_FRESH = timedelta(hours=48)
STALE_DAILY = {"crypto": timedelta(days=2), "security": timedelta(days=5)}  # Schlusskurs gilt danach als veraltet
DISTINCT_DIGITS = 6  # ab so vielen signifikanten Stellen gilt eine Menge als unverwechselbar
MAX_PAIRS_SHOWN = 40
FEE_ASSETS = frozenset({"ETH", "BNB", "AVAX", "SOL", "KAS", "BTC", "MATIC", "POL"})
INCOME = frozenset(C.INCOME_TAGS)
NEUTRAL_TAGS = frozenset({"", "internal", "exchange"})  # Zu-/Abgänge, die ein interner Transfer sein können
_PLACEHOLDER_TIMES = frozenset({(0, 0, 0), (12, 0, 0)})
_TYPE_LABEL = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "deposit": "Zugang", "withdrawal": "Abgang",
               "transfer": "Transfer", "corporate_action": "Kapitalmaßnahme"}


# ----------------------------------------------------------------------------------------------------
# Formatierung (Texte der Befunde: Zeitpunkte in UTC, Mengen exakt)
# ----------------------------------------------------------------------------------------------------

def _q(v: Decimal | None) -> str:
    return qty_exact(v)


def _px(v: float | Decimal | None) -> str:
    """Kurs in EUR, 8 signifikante Stellen, deutsches Format."""
    if v is None:
        return "–"
    return qty_exact(Decimal(f"{float(v):.8g}")) + " €"


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%d.%m.%Y %H:%M:%S UTC")


def _d(d: date | datetime | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "–"


def _short(h: str, n: int = 10) -> str:
    return h if len(h) <= n + 8 else f"{h[:n]}…{h[-6:]}"


def _dur(td: timedelta) -> str:
    s = int(abs(td.total_seconds()))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    h, m = divmod(s // 60, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} Tage"


def _sig_digits(v: Decimal) -> int:
    t = v.normalize().as_tuple()
    return len(t.digits) if isinstance(t.exponent, int) else 0


def _flags(t: Tx) -> list[str]:
    return [f.strip() for f in (t.flag or "").split(";") if f.strip()] if t.origin != "plan" else []


# ----------------------------------------------------------------------------------------------------
# Index über den Schnappschuss
# ----------------------------------------------------------------------------------------------------

class _Index:
    def __init__(self, snap: Snapshot) -> None:
        assert snap.pf is not None and snap.ledger is not None
        self.snap = snap
        self.pf = snap.pf
        self.led = snap.ledger
        self.txs: list[Tx] = sorted(self.pf.txs, key=lambda t: (t.ts, t.seq, t.tx_id))
        self.by_id: dict[str, Tx] = {t.tx_id: t for t in self.txs}
        self.meta: dict[str, JournalMeta] = {t.tx_id: m for t in self.txs if t.origin == "journal"
                                             and (m := snap.journal.get(t.tx_id)) is not None}
        self.hashes: dict[str, set[str]] = {t.tx_id: self._hashes(t) for t in self.txs}
        self._refs: dict[str, TxRef] = {}
        self.lots_by_tx: dict[str, list[Lot]] = defaultdict(list)
        for lot in self.led.lots:
            self.lots_by_tx[lot.acq_tx].append(lot)
        self.parts_by_tx: dict[str, list[tuple[Disposal, DisposalPart]]] = defaultdict(list)
        for d in self.led.disposals:
            for p in d.parts:
                if p.acq_tx:
                    self.parts_by_tx[p.acq_tx].append((d, p))
        self.flow_txs = {f.tx_id for f in self.led.flows}
        self.checks: dict[tuple[str | None, str], dict[str, Any]] = {
            (h.get("account"), h["asset_id"]): h for h in self.pf.holdings_check}
        self.findings_by_pos: dict[tuple[str, str], list[Finding]] = defaultdict(list)
        self.dup_txs: set[str] = set()
        self.twin_weak: set[str] = set()
        self.transfer_txs: set[str] = set()  # von _transfers zugeordnete Abgänge/Zugänge
        self.econ: dict[tuple[str, str], list[tuple[str, Tx, Tx, str]]] = defaultdict(list)  # (Status, gilt, Dublette)
        self.unmatched_out: list[Tx] = []  # Abgänge ohne Gegenbuchung (audit.outflows)

    # -- Buchungen ------------------------------------------------------------------------------------
    def _hashes(self, t: Tx) -> set[str]:
        hs = hashes_in(t.note, t.source_ref)
        m = self.meta.get(t.tx_id)
        if m is not None and m.tx_hash:
            h = normalize_hash(m.tx_hash)
            if h:
                hs.add(h)
        return hs

    def event_index(self, t: Tx) -> str | None:
        """Ereignisindex innerhalb einer Transaktion (Log-/Output-Index bzw. Unterkennung) – nur aus App-Buchungen
        und Portfolia-Exporten bekannt (``<ereignis>#<index>``); Koinly & Co. liefern keinen."""
        m = self.meta.get(t.tx_id)
        ref = m.external_id if m is not None else t.source_ref
        if ref and "#" in ref and ":" in ref.split("#", 1)[0]:
            return ref.split("#", 1)[1] or None
        if m is not None and m.event_key and m.event_line is not None:
            return str(m.event_line)
        return None

    def source_label(self, t: Tx) -> str:
        if t.origin == "plan":
            return "Sparplan-Schätzung" if t.flag == "estimated" else "Sparplan (freigegeben)"
        if t.origin == "journal":
            m = self.meta.get(t.tx_id)
            try:
                from app.journal.service import source_label

                return "App · " + source_label(m.source if m is not None else t.source)
            except ImportError:  # pragma: no cover
                return "App"
        return f"Import · {t.source}" if t.source else "Import"

    def manual(self, t: Tx) -> list[str]:
        """Hinweise auf manuelle Erfassung (ohne Beleg der Quelle)."""
        out = []
        m = self.meta.get(t.tx_id)
        if t.origin == "journal" and (m is None or m.source == "manual"):
            out.append("in der App manuell erfasst")
        src = (t.source or "").lower()
        if t.origin == "import" and src in ("manual", "portfolia:manual"):
            out.append(f"Quelle „{t.source}“ (manuell)")
        out += [f"Kennzeichen {f}" for f in _flags(t) if "MANUAL" in f.upper()]
        return out

    def placeholder_time(self, t: Tx) -> bool:
        ts = t.ts.astimezone(UTC)
        return t.date_only or ((ts.hour, ts.minute, ts.second) in _PLACEHOLDER_TIMES and ts.microsecond == 0)

    def ref(self, t: Tx) -> TxRef:
        r = self._refs.get(t.tx_id)
        if r is None:
            r = TxRef(
                tx_id=t.tx_id, ts=t.ts, type=t.type, tag=t.tag,
                out=(t.from_account or "", t.from_asset, t.from_qty) if t.from_asset and t.from_qty else None,
                inn=(t.to_account or "", t.to_asset, t.to_qty) if t.to_asset and t.to_qty else None,
                fee=(t.fee_asset, t.fee_qty) if t.fee_asset and t.fee_qty else None,
                value_eur=t.value_eur, source=self.source_label(t), source_ref=t.source_ref, origin=t.origin,
                hashes=sorted(self.hashes.get(t.tx_id, ())), event_index=self.event_index(t), flags=_flags(t),
                note=(t.note[:240] + "…") if t.note and len(t.note) > 240 else t.note, edit_url=self.edit_url(t))
            self._refs[t.tx_id] = r
        return r

    def edit_url(self, t: Tx) -> str | None:
        """Bearbeiten im Journal: Import-Buchungen als Überlagerung, App-Buchungen nur aus bearbeitbaren Quellen."""
        if t.origin == "journal":
            m = self.meta.get(t.tx_id)
            if m is None or m.status != "active" or not (m.source in ("manual", "transfer")
                                                          or m.source.startswith(("csv:", "sync:", "doc:"))):
                return None
        elif t.origin != "import":
            return None
        return f"/journal/{quote(t.tx_id, safe='')}/edit"

    def describe(self, t: Tx) -> str:
        """Einzeilige Beschreibung einer Buchung (UTC, exakte Mengen)."""
        parts = [f"{t.tx_id}: {_TYPE_LABEL.get(t.type, t.type)}{' (' + t.tag + ')' if t.tag else ''}", _ts(t.ts)]
        if t.from_asset and t.from_qty:
            parts.append(f"−{_q(t.from_qty)} {t.from_asset}" + (f" von {t.from_account}" if t.from_account else ""))
        if t.to_asset and t.to_qty:
            parts.append(f"+{_q(t.to_qty)} {t.to_asset}" + (f" auf {t.to_account}" if t.to_account else ""))
        if t.fee_asset and t.fee_qty:
            parts.append(f"Gebühr {_q(t.fee_qty)} {t.fee_asset}")
        if t.value_eur is not None:
            parts.append(f"Wert {eur(t.value_eur)}")
        parts.append(self.source_label(t))
        return " · ".join(parts)

    # -- Bestände, Kurse, Namen -----------------------------------------------------------------------
    def asset(self, aid: str) -> AssetInfo:
        return self.pf.assets.get(aid) or AssetInfo(asset_id=aid, name=aid, asset_class="crypto")

    def label(self, aid: str) -> str:
        a = self.pf.assets.get(aid)
        return f"{a.name} ({aid})" if a is not None and a.name and a.name != aid else aid

    def bal(self, account: str, aid: str) -> Decimal:
        return self.led.balances.get((account, aid), ZERO)

    def price(self, aid: str) -> Any:
        p = self.snap.prices.get(aid)
        return p if p is not None and p.valued else None

    def value(self, aid: str, q: Decimal) -> Decimal | None:
        p = self.price(aid)
        return (q * Decimal(str(p.price_eur))).quantize(Decimal("0.01")) if p is not None else None

    def lots_from(self, tx_ids: Iterable[str]) -> tuple[Decimal, Decimal, int]:
        q = c = ZERO
        n = 0
        for tid in tx_ids:
            for lot in self.lots_by_tx.get(tid, ()):
                q += lot.qty
                c += lot.cost
                n += 1
        return q, c, n

    def consumed_from(self, tx_ids: Iterable[str]) -> dict[int, tuple[Decimal, Decimal, Decimal, set[str]]]:
        """Veräußerungen aus Lots dieser Buchungen je Jahr: (Menge, Einstand, G/V, Arten)."""
        out: dict[int, list[Any]] = {}
        for tid in tx_ids:
            for d, p in self.parts_by_tx.get(tid, ()):
                slot = out.setdefault(d.date.year, [ZERO, ZERO, ZERO, set()])
                slot[0] += p.qty
                slot[1] += p.cost
                slot[2] += p.gain if d.kind in REALIZED_KINDS else ZERO
                slot[3].add(d.kind)
        return {y: (v[0], v[1], v[2], v[3]) for y, v in sorted(out.items())}

    def attach(self, f: Finding, derive_positions: bool = True) -> Finding:
        """Befund vervollständigen (Konten, Assets, Quellen, Kennungen aus den Buchungen) und für den
        Bestandsabgleich registrieren (betroffene Konto/Asset-Paare: aus den Buchungen oder vorgegeben)."""
        txs = [*f.txs, *(x for a, b, _ in f.pairs for x in (a, b))]
        seen: set[str] = set()
        for r in txs:
            if r.tx_id in seen:
                continue
            seen.add(r.tx_id)
            for leg in (r.out, r.inn):
                if leg is not None:
                    if leg[0] and leg[0] not in f.accounts:
                        f.accounts.append(leg[0])
                    if leg[1] not in f.assets:
                        f.assets.append(leg[1])
                    if derive_positions and leg[0] and (leg[0], leg[1]) not in f.positions:
                        f.positions.append((leg[0], leg[1]))
            if r.source and r.source not in f.sources:
                f.sources.append(r.source)
            for ident in [r.tx_id, *([r.source_ref] if r.source_ref else []), *(_short(h) for h in r.hashes)]:
                if ident not in f.identifiers:
                    f.identifiers.append(ident)
        for pos in f.positions:
            self.findings_by_pos[pos].append(f)
        return f


# ----------------------------------------------------------------------------------------------------
# Einstieg
# ----------------------------------------------------------------------------------------------------

def diagnose(snap: Snapshot) -> Report:
    """Alle Regeln auf dem Schnappschuss ausführen (rein, ohne Schreibzugriff)."""
    stats: dict[str, int] = {}
    findings: list[Finding] = []
    holdings: list[HoldingRow] = []
    idx = None
    if snap.pf is not None and snap.ledger is not None:
        from app.diagnosis import audit

        idx = _Index(snap)
        for rule in (_dup_same_hash, _dup_same_qty, _dup_same_id, _dup_identical, _dup_conversion_twin,
                     _dup_transfer_side, audit.econ_duplicates, _transfers, audit.outflows,
                     _assets, _history,
                     _estimated, _prices, _price_history, _migrations, _rename_trades):
            findings += rule(idx, stats)
        rows, hf = _holdings(idx, stats)
        holdings = rows
        findings += hf
        findings += audit.missing_at_check(idx, rows)
        findings += audit.source_breakdown(idx, stats, {(r.account, r.asset): r for r in rows
                                                        if r.reference is not None})
        findings += audit.inactive_accounts(idx, stats)
        audit.explain_negatives(idx, findings)
        cases = audit.split_cases(idx, findings)
        stats["cases"] = len(cases)
        findings += cases
        stats["txs"] = len(idx.txs)
    findings += _open_batches(snap, stats)
    findings.sort(key=lambda f: (*f.sort_key(), f.id))
    if snap.pf is not None:
        from app.diagnosis import audit as _audit

        _audit.case_states(snap, findings)
    return Report(findings=findings, holdings=holdings, generated_for=snap.today, stats=stats, snapshot=snap,
                  index=idx)


# ----------------------------------------------------------------------------------------------------
# Dubletten
# ----------------------------------------------------------------------------------------------------

def _signature(t: Tx) -> tuple[Any, ...]:
    return (t.ts, t.type, t.tag or "", t.from_account or "", t.from_asset or "", t.from_qty, t.to_account or "",
            t.to_asset or "", t.to_qty, t.fee_asset or "", t.fee_qty, t.value_eur)


def _effect(txs: Iterable[Tx]) -> dict[tuple[str, str], Decimal]:
    """Bestandswirkung von Buchungen je (Konto, Asset) – wie die Ledger-Engine (Gebühr am Abgangskonto)."""
    out: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for t in txs:
        if t.from_asset and t.from_account and t.from_qty:
            out[(t.from_account, t.from_asset)] -= t.from_qty
        if t.to_asset and t.to_account and t.to_qty:
            out[(t.to_account, t.to_asset)] += t.to_qty
        acc = t.from_account or t.to_account
        if t.fee_asset and t.fee_qty and acc:
            out[(acc, t.fee_asset)] -= t.fee_qty
    return {k: v for k, v in out.items() if v}


def _scenario_without(idx: _Index, removed: list[Tx], text: str) -> Scenario:
    """Szenario: Bestände ohne die genannten Buchungen (rein rechnerisch, nichts wird gebucht)."""
    sc = Scenario(text=text)
    for (acc, aid), delta in sorted(_effect(removed).items()):
        cur = idx.bal(acc, aid)
        new = cur - delta
        sc.rows.append((f"Bestand {acc} · {aid}", _q(cur), f"{_q(new)} (Δ {'+' if -delta > 0 else '−'}"
                                                           f"{_q(abs(delta))})"))
        v_cur, v_new = idx.value(aid, cur), idx.value(aid, new)
        if v_cur is not None and v_new is not None and v_cur != v_new:
            p = idx.price(aid)
            sc.rows.append((f"Wert {aid} zum aktuellen Kurs ({p.source}, {_d(p.ts)})", eur(v_cur), eur(v_new)))
    q, c, n = idx.lots_from(t.tx_id for t in removed)
    if n:
        sc.rows.append(("offene Lots aus diesen Buchungen", f"{_q(q)} Stück, Einstand {eur(c)}", "entfielen"))
    for year, (qq, cost, gain, kinds) in idx.consumed_from(t.tx_id for t in removed).items():
        sc.rows.append((f"Abgänge {year} aus diesen Lots ({', '.join(sorted(kinds))})",
                        f"{_q(qq)} Stück, Einstand {eur(cost)}, G/V {eur(gain)}", "Lots anderer Zugänge bzw. "
                                                                                      "Fehlbestand"))
    return sc


def _dup_same_hash(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Gleicher Transaktions-Hash und identische Buchungsangaben (Zeitpunkt, Konten, Assets, Mengen, EUR-Wert)."""
    groups: dict[tuple[str, tuple[Any, ...]], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        if t.origin == "plan":
            continue
        for h in sorted(idx.hashes[t.tx_id]):
            groups[(h, _signature(t))].append(t)
    seen: set[frozenset[str]] = set()
    clusters: dict[tuple[Any, ...], list[tuple[str, list[Tx], str]]] = defaultdict(list)
    legit = 0
    for (h, _sig), txs in sorted(groups.items(), key=lambda kv: (kv[1][0].ts, kv[0][0])):
        txs = sorted(txs, key=lambda t: t.tx_id)  # gleiche Angaben, gleicher Zeitpunkt: nach Kennung, nicht Datei
        ids = frozenset(t.tx_id for t in txs)
        if len(ids) < 2 or ids in seen:
            continue
        seen.add(ids)
        indices = [idx.event_index(t) for t in txs]
        if all(i is not None for i in indices) and len(set(indices)) == len(indices):
            legit += 1  # mehrere Bewegungen derselben Transaktion – unterschiedliche Ereignisindizes
            continue
        known_idx = [i for i in indices if i is not None]
        status = "wahrscheinlich" if len(known_idx) != len(set(known_idx)) else "verdacht"
        t0 = txs[0]
        pos = tuple(sorted(_effect([t0])))
        sources = tuple(sorted({idx.source_label(t) for t in txs}))
        clusters[(status, pos, sources)].append((h, txs, status))
    stats["same_hash_distinct_index"] = legit
    out: list[Finding] = []
    for (status, pos, _sources), items in sorted(clusters.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        pairs: list[tuple[TxRef, TxRef, str]] = []
        extra: list[Tx] = []
        evidence: list[str] = []
        all_tx: list[Tx] = []
        pair_ids: list[list[str]] = []
        pair_hashes: list[str] = []
        for h, txs, _st in items:
            first, rest = txs[0], txs[1:]
            all_tx += txs
            for t in rest:
                extra.append(t)
                pair_ids.append([first.tx_id, t.tx_id])
                pair_hashes.append(h)
                ia, ib = idx.event_index(first), idx.event_index(t)
                why = (f"gleicher Hash {_short(h)}, gleicher Zeitpunkt, gleiches Konto, gleiche Richtung, Menge und "
                       f"EUR-Wert; Kennungen {first.tx_id} ≠ {t.tx_id}; Ereignisindex "
                       + (f"{ia} = {ib}" if ia is not None and ia == ib else "fehlt" if ia is None or ib is None
                          else f"{ia} / {ib}"))
                pairs.append((idx.ref(first), idx.ref(t), why))
            evidence.append(f"{_ts(first.ts)} · {_TYPE_LABEL.get(first.type, first.type)} "
                            f"{_q(first.to_qty or first.from_qty)} {first.to_asset or first.from_asset} · Hash "
                            f"{_short(h)} · {len(txs)}× ({', '.join(t.tx_id for t in txs)})")
        idx.dup_txs.update(t.tx_id for t in all_tx)
        n_pairs = len(pairs)
        accs = sorted({p[0] for p in pos})
        assets = sorted({p[1] for p in pos})
        days = sorted({_d(t.ts) for t in all_tx})
        title = (f"{n_pairs} {'Paar' if n_pairs == 1 else 'Paare'} gleicher Buchungen mit gleichem Hash – "
                 f"{', '.join(accs)} · {', '.join(assets)}")
        f = Finding(
            kind="duplicate", status=status, title=title, priority=1,
            known=[f"{len(all_tx)} Buchungen bilden {n_pairs} {'Paar' if n_pairs == 1 else 'Paare'}: je Paar gleicher "
                   "Transaktions-Hash, Zeitpunkt, Konto, Richtung, Menge und EUR-Wert.",
                   "Die Buchungen tragen unterschiedliche Kennungen der Quelle (tx_id/source_ref).",
                   f"Tage: {', '.join(days)}.",
                   "Alle Buchungen gehen unverändert in Bestand, Lots und Performance ein."],
            suspected=["Je Paar könnte eine Buchung denselben Vorgang ein zweites Mal abbilden (z. B. doppelter "
                       "Import beim Steuertool)."],
            uncertainty=["Eine Blockchain-Transaktion kann mehrere gleichartige Bewegungen enthalten (z. B. zwei "
                         "Outputs an dieselbe Adresse). Ohne Ereignisindex (Output-/Log-Index) ist nicht "
                         "unterscheidbar, ob zwei legitime Bewegungen oder eine doppelte Buchung vorliegen."
                         if status == "verdacht" else
                         "Gleicher Ereignisindex in beiden Buchungen: dieselbe Bewegung wurde über zwei Wege "
                         "erfasst – eine legitime Teilung ist trotzdem möglich, wenn die Quelle einen Vorgang "
                         "aufteilt."],
            evidence=evidence[:MAX_PAIRS_SHOWN] + ([f"… und {len(evidence) - MAX_PAIRS_SHOWN} weitere"]
                                                  if len(evidence) > MAX_PAIRS_SHOWN else []),
            pairs=pairs[:MAX_PAIRS_SHOWN],
            scenario=_scenario_without(idx, extra, "Szenario (hypothetisch): je Paar nur eine Buchung gezählt. "
                                                   "Welche Buchung entfiele, ist offen – es wird nichts gebucht."),
            decision="Je Paar im Block-Explorer prüfen, ob die Transaktion zwei Bewegungen an dieses Konto enthält. "
                     "Nur wenn nicht: eine Buchung je Paar im kuratierten Import entfernen bzw. in Portfolia "
                     "löschen (Überlagerung). Portfolia ändert nichts automatisch.",
            key="hash|" + "|".join(sorted(t.tx_id for t in all_tx)),
            weight=sum((abs(v) for v in _effect(extra).values()), ZERO),
            data={"type": "hash_pairs", "pairs": pair_ids, "hashes": pair_hashes, "accounts": accs})
        f.txs = [idx.ref(t) for t in all_tx[:2 * MAX_PAIRS_SHOWN]]
        out.append(idx.attach(f))
    return out + _dup_cross_source(idx)


def _dup_cross_source(idx: _Index) -> list[Finding]:
    """Gleicher Hash, gleiche Richtung, gleiches Asset und gleiche Menge (± 0,5 %) im kuratierten Import und in
    App-Buchungen – auch wenn die Konten unterschiedlich heißen (z. B. Wallet-Datenquelle vs. Koinly-Wallet)."""
    from app.csvimport.reconcile import qty_eq, tx_legs

    by: dict[tuple[str, str, str], dict[str, list[tuple[Tx, Decimal]]]] = defaultdict(
        lambda: {"import": [], "journal": []})
    for t in idx.txs:
        if t.origin not in ("import", "journal") or t.tx_id in idx.dup_txs:
            continue
        for h in idx.hashes[t.tx_id]:
            for side, aid, q, _acc in tx_legs(t):
                if side != "fee":
                    by[(h, side, aid)][t.origin].append((t, q))
    out: list[Finding] = []
    seen: set[frozenset[str]] = set()
    for (h, side, aid), sides in sorted(by.items()):
        for ti, qi in sides["import"]:
            for tj, qj in sides["journal"]:
                pair = frozenset((ti.tx_id, tj.tx_id))
                if pair in seen or not qty_eq(qj, qi):
                    continue
                seen.add(pair)
                idx.dup_txs.update(pair)
                f = Finding(
                    kind="duplicate", status="wahrscheinlich", priority=1,
                    title=f"Vorgang im Import und in App-Buchungen: {'Zugang' if side == 'in' else 'Abgang'} "
                          f"{_q(qi)} {aid}, Hash {_short(h)}",
                    known=[idx.describe(ti), idx.describe(tj),
                           "Gleicher Transaktions-Hash, gleiche Richtung, gleiches Asset und gleiche Menge (± 0,5 %)."],
                    suspected=["Dieselbe Bewegung ist zweimal gebucht: im kuratierten Import und über eine Datenquelle "
                               "bzw. CSV in der App."],
                    evidence=[f"Konten: {ti.to_account or ti.from_account} (Import) / "
                              f"{tj.to_account or tj.from_account} (App)"],
                    uncertainty=["Enthält die Transaktion mehrere gleich große Bewegungen desselben Assets, können "
                                 "beide Buchungen legitim sein – Ereignisindizes fehlen im Import."],
                    pairs=[(idx.ref(ti), idx.ref(tj), f"gleicher Hash {_short(h)}, gleiche Richtung und Menge")],
                    scenario=_scenario_without(idx, [tj], f"Szenario (hypothetisch): ohne die App-Buchung "
                                                          f"{tj.tx_id}."),
                    decision="App-Buchung als „im Import enthalten“ markieren bzw. zurücknehmen (Journal → Abgleich). "
                             "Portfolia ändert nichts automatisch.",
                    key=f"cross|{h}|{'|'.join(sorted(pair))}", weight=qi,
                    data={"type": "import_vs_app", "imports": [ti.tx_id], "journals": [tj.tx_id], "hashes": [h]})
                f.txs = [idx.ref(ti), idx.ref(tj)]
                out.append(idx.attach(f))
    return out


def _dup_identical(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Vollständig gleiche Buchungen (Zeitpunkt, Art, Konten, Assets, Mengen, Gebühr, EUR-Wert) ohne Beleg, dass es
    verschiedene Vorgänge sind (verschiedene Hashes, verschiedene Ereignisindizes oder verschiedene Kennungen derselben
    Quelle). Bleibt ein **Verdacht**: zwei gleiche Käufe in derselben Sekunde sind möglich – Portfolia schlägt nur vor,
    welche Buchung entfiele, und ändert nichts ohne Bestätigung."""
    groups: dict[tuple[Any, ...], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        if t.origin == "plan" or t.tx_id in idx.dup_txs or "SAVINGS_PLAN" in _flags(t) \
                or (t.source_ref or "").startswith("sparplan|"):
            continue
        groups[_signature(t)].append(t)
    legit = 0
    clusters: dict[tuple[Any, ...], list[list[Tx]]] = defaultdict(list)
    for _sig, txs in sorted(groups.items(), key=lambda kv: (kv[1][0].ts, kv[1][0].tx_id)):
        txs = sorted(txs, key=lambda t: t.tx_id)  # bleibende Buchung unabhängig von der Dateireihenfolge
        if len(txs) < 2 or any(idx.manual(t) for t in txs):
            continue  # manuell + importiert: Regel „gleiche Menge“ (_dup_same_qty)
        hs = [frozenset(idx.hashes[t.tx_id]) for t in txs]
        ix = [idx.event_index(t) for t in txs]
        refs = [(idx.source_label(t), (t.source_ref or "").strip()) for t in txs]
        if (all(hs) and len(set(hs)) == len(hs)) or (all(i is not None for i in ix) and len(set(ix)) == len(ix)) \
                or (all(r[1] for r in refs) and len(set(refs)) == len(refs) and len({r[0] for r in refs}) == 1):
            legit += 1  # verschiedene Blockchain-Transaktionen bzw. Ereignisse bzw. Kennungen derselben Quelle
            continue
        clusters[tuple(sorted(_effect([txs[0]])))].append(txs)
    stats["identical_legit"] = legit
    out: list[Finding] = []
    for pos, groups_ in sorted(clusters.items()):
        pairs: list[tuple[TxRef, TxRef, str]] = []
        pair_ids: list[list[str]] = []
        extra: list[Tx] = []
        all_tx: list[Tx] = []
        for txs in groups_:
            first = txs[0]
            all_tx += txs
            for t in txs[1:]:
                extra.append(t)
                pair_ids.append([first.tx_id, t.tx_id])
                pairs.append((idx.ref(first), idx.ref(t),
                              "alle Buchungsangaben gleich (Zeitpunkt, Art, Konten, Assets, Mengen, Gebühr, EUR-Wert); "
                              f"Kennungen {first.tx_id} ≠ {t.tx_id}; kein Hash bzw. Ereignisindex, der sie "
                              "unterscheidet"))
        idx.dup_txs.update(t.tx_id for t in all_tx)
        accs = sorted({p[0] for p in pos})
        assets = sorted({p[1] for p in pos})
        n = len(pairs)
        f = Finding(
            kind="duplicate", status="verdacht", priority=1,
            title=f"{n} {'Paar' if n == 1 else 'Paare'} vollständig gleicher Buchungen – {', '.join(accs)} · "
                  f"{', '.join(assets)}",
            known=[f"{len(all_tx)} Buchungen mit identischen Angaben bilden {n} {'Paar' if n == 1 else 'Paare'}.",
                   "Keine Kennung, kein Hash und kein Ereignisindex belegt, dass es verschiedene Vorgänge sind.",
                   "Alle Buchungen gehen unverändert in Bestand, Lots und Performance ein."],
            suspected=["Derselbe Vorgang wurde mehrfach erfasst (z. B. Datei doppelt importiert, Zeile kopiert)."],
            uncertainty=["Zwei gleiche Ausführungen in derselben Sekunde sind möglich (z. B. Teilausführungen ohne "
                         "eigene Kennung) – nur der Kontoauszug bzw. die Transaktionsliste der Quelle entscheidet."],
            pairs=pairs[:MAX_PAIRS_SHOWN],
            scenario=_scenario_without(idx, extra, "Szenario (hypothetisch): je Paar nur die erste Buchung gezählt – "
                                                   "es wird nichts gebucht."),
            decision="Kontoauszug bzw. Transaktionsliste prüfen; nur bei einem Vorgang die zusätzliche Buchung "
                     "ausblenden. Portfolia ändert nichts automatisch.",
            key="ident|" + "|".join(sorted(t.tx_id for t in all_tx)),
            weight=sum((abs(v) for v in _effect(extra).values()), ZERO),
            data={"type": "identical", "pairs": pair_ids, "accounts": accs})
        f.txs = [idx.ref(t) for t in all_tx[:2 * MAX_PAIRS_SHOWN]]
        out.append(idx.attach(f))
    stats["identical_pairs"] = sum(len(f.data["pairs"]) for f in out)
    return out


def _is_conversion(idx: _Index, t: Tx) -> bool:
    """Tausch bzw. Kapitalmaßnahme Krypto → Krypto (Abgangs- und Zugangsbein auf demselben Konto, kein Fiat)."""
    return (t.origin != "plan" and t.type in ("trade", "corporate_action") and bool(t.from_asset and t.to_asset)
            and bool(t.from_qty and t.to_qty) and t.from_asset != t.to_asset
            and bool(t.from_account) and t.from_account == t.to_account
            and not idx.asset(t.from_asset).is_fiat and not idx.asset(t.to_asset).is_fiat)


def _balance_before(idx: _Index, account: str, aid: str, t: Tx) -> Decimal:
    """Bestand (Konto, Asset) unmittelbar vor ``t`` – Reihenfolge wie im Ledger (Zeitpunkt, Folge, Kennung)."""
    q = ZERO
    stop = (t.ts, t.seq, t.tx_id)
    for x in idx.txs:
        if (x.ts, x.seq, x.tx_id) >= stop:
            break
        q += _effect([x]).get((account, aid), ZERO)
    return q


def same_instrument(idx: _Index, a: str, b: str) -> list[str]:
    """Belege, dass zwei Asset-IDs dasselbe Instrument bezeichnen (gleiche Kurszuordnung bzw. gleiches Symbol)."""
    if a == b:
        return ["dasselbe Asset"]
    x, y = idx.asset(a), idx.asset(b)
    out = []
    if x.quote_source not in ("", "none") and x.quote_id and (x.quote_source, x.quote_id) == (y.quote_source,
                                                                                               y.quote_id):
        out.append(f"gleiche Kurszuordnung {x.quote_source} „{x.quote_id}“")
    if x.symbol.upper() == y.symbol.upper():
        out.append(f"gleiches Symbol {x.symbol.upper()}")
    return out


def _dup_conversion_twin(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Derselbe Umtausch zweimal gebucht – unter verschiedenen Ausgangs-Assets bzw. Buchungsarten, z. B. die
    Ticker-Umbenennung AITECH → ACN: im kuratierten Import als Tausch des (dort schon umbenannten) Altbestands
    ``ACN#…`` → ``ACN``, aus der Börsen-API als Kapitalmaßnahme ``AITECH`` → ``ACN``. Gleiches Konto, gleiches
    Ziel-Asset, exakt gleiche Mengen auf beiden Seiten, Abstand ≤ 36 h, verschiedene Quellen.

    Die Buchung, deren Ausgangs-Asset vorher keinen ausreichenden Bestand hatte (Ursache von „Bestand zeitweise
    negativ“), ist die zusätzliche. Ändert nichts – Lösungen erst nach Vorschau und Bestätigung."""
    groups: dict[tuple[str, str, Decimal, Decimal], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        if _is_conversion(idx, t) and t.tx_id not in idx.dup_txs:
            groups[(t.to_account or "", t.to_asset or "", t.to_qty, t.from_qty)].append(t)  # type: ignore[index]
    out: list[Finding] = []
    for (acc, new, q_to, q_from), txs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], str(kv[0][2]))):
        if len(txs) < 2:
            continue
        txs = sorted(txs, key=lambda t: (t.ts, t.seq, t.tx_id))
        for i, a in enumerate(txs):
            for b in txs[i + 1:]:
                if b.ts - a.ts > DUP_WINDOW or a.tx_id in idx.dup_txs or b.tx_id in idx.dup_txs:
                    continue
                if idx.source_label(a) == idx.source_label(b) and a.source_ref and b.source_ref:
                    continue  # zwei Vorgänge derselben Quelle mit eigenen Kennungen
                ha, hb = idx.hashes[a.tx_id], idx.hashes[b.tx_id]
                if ha and hb and not (ha & hb):
                    continue  # verschiedene Blockchain-Transaktionen
                f = _twin_finding(idx, acc, new, q_to, q_from, a, b)
                if f is not None:
                    idx.dup_txs.update((a.tx_id, b.tx_id))
                    out.append(f)
    stats["conversion_twins"] = len(out)
    return out


def _twin_finding(idx: _Index, acc: str, new: str, q_to: Decimal, q_from: Decimal, a: Tx, b: Tx) -> Finding | None:
    before = {t.tx_id: _balance_before(idx, acc, t.from_asset or "", t) for t in (a, b)}
    short = {t.tx_id: before[t.tx_id] < q_from - max(DUST, q_from * Decimal("1e-9")) for t in (a, b)}
    inst = same_instrument(idx, a.from_asset or "", b.from_asset or "")
    distinct = _sig_digits(q_from) >= DISTINCT_DIGITS and _sig_digits(q_to) >= DISTINCT_DIGITS
    if not (inst or short[a.tx_id] != short[b.tx_id]) and not distinct:
        return None  # runde Mengen ohne weiteren Beleg: gleichartige Umtausche sind plausibel
    if short[a.tx_id] != short[b.tx_id]:
        weak, strong = (a, b) if short[a.tx_id] else (b, a)
    elif a.origin == "journal" and b.origin == "import":
        weak, strong = a, b
    else:
        weak, strong = b, a
    old_w, old_s = weak.from_asset or "", strong.from_asset or ""
    status = "wahrscheinlich" if distinct and (inst or short[weak.tx_id]) else "verdacht"
    rename = old_w != old_s
    known = [f"Zwei Umtausche auf {acc} ergeben jeweils exakt {_q(q_to)} {new} aus exakt {_q(q_from)} "
             f"{'des Ausgangs-Assets' if not rename else old_s + ' bzw. ' + old_w} (Abstand {_dur(abs(b.ts - a.ts))}).",
             idx.describe(strong), idx.describe(weak),
             f"Bestand {old_s} auf {acc} vor {strong.tx_id}: {_q(before[strong.tx_id])}; "
             f"Bestand {old_w} vor {weak.tx_id}: {_q(before[weak.tx_id])}."]
    evidence = [f"Mengen mit {_sig_digits(q_from)} bzw. {_sig_digits(q_to)} signifikanten Stellen auf beiden Seiten "
                "identisch" + (" – zufällige Gleichheit ist unwahrscheinlich" if distinct else "")]
    if short[weak.tx_id]:
        evidence.append(f"{weak.tx_id} tauscht {_q(q_from)} {old_w}, vorher waren nur {_q(before[weak.tx_id])} "
                        "gebucht – der Bestand wird negativ (Ursache von „Bestand zeitweise negativ“).")
    evidence += [f"{old_s} / {old_w}: {x}" for x in inst if rename]
    if rename:
        evidence.append(f"Typisch für eine Ticker-Umbenennung: Die eine Quelle führt den Altbestand unter "
                        f"{old_s}, die andere unter {old_w}.")
    f = Finding(
        kind="duplicate", status=status, priority=1,
        title=f"Umtausch doppelt gebucht: {_q(q_from)} {old_w if rename else old_s} → {_q(q_to)} {new} auf {acc}",
        known=known,
        suspected=[f"{weak.tx_id} ({idx.source_label(weak)}) bildet denselben Umtausch ein zweites Mal ab wie "
                   f"{strong.tx_id} ({idx.source_label(strong)}); {new} zählt dadurch doppelt."
                   + (f" {old_w} ist vermutlich das Börsen-Symbol des Altbestands, den der Import als {old_s} führt."
                      if rename else "")],
        evidence=evidence,
        uncertainty=["Zwei gleiche Umtausche in kurzer Folge sind möglich – der Kontoauszug der Quelle entscheidet.",
                     "Ob der Umtausch steuerlich ein Tausch oder eine steuerneutrale Umbenennung ist, beantwortet "
                     "diese Prüfung nicht (siehe Befund „Umbenennung als Tausch gebucht“)."],
        pairs=[(idx.ref(weak), idx.ref(strong), "gleiches Konto und Ziel-Asset, exakt gleiche Mengen, "
                                                f"Abstand {_dur(abs(b.ts - a.ts))}")],
        scenario=_scenario_without(idx, [weak], f"Szenario (hypothetisch): ohne die Buchung {weak.tx_id}. Es wird "
                                                "nichts gebucht und nichts ausgeschlossen."),
        decision=f"Kontoauszug prüfen; bei einem Vorgang {weak.tx_id} nicht mehr zählen lassen (App-Buchung neben "
                 "Import-Buchung: „im Import enthalten“). Portfolia ändert nichts automatisch.",
        key=f"twin|{'|'.join(sorted((a.tx_id, b.tx_id)))}", weight=abs(q_to),
        positions=[(acc, new), (acc, old_w), (acc, old_s)] if rename else [(acc, new), (acc, old_s)],
        data={"type": "conversion_twin", "weak": weak.tx_id, "strong": strong.tx_id, "account": acc, "asset": new,
              "old_weak": old_w, "old_strong": old_s, "short": short[weak.tx_id]})
    f.txs = [idx.ref(strong), idx.ref(weak)]
    idx.twin_weak.add(weak.tx_id)
    return idx.attach(f, derive_positions=False)


def _dup_same_qty(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Gleiche exakte Menge auf demselben Konto (Zugang bzw. Abgang) innerhalb von 36 h, mindestens eine Buchung
    manuell erfasst – Muster „manuell nachgetragen und zusätzlich importiert“."""
    legs: dict[tuple[str, str, str, Decimal], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        if t.origin == "plan" or "SAVINGS_PLAN" in _flags(t) or (t.source_ref or "").startswith("sparplan|"):
            continue
        if t.to_asset and t.to_account and t.to_qty and t.type in ("deposit", "transfer", "buy", "trade"):
            legs[("in", t.to_account, t.to_asset, t.to_qty)].append(t)
        if t.from_asset and t.from_account and t.from_qty and t.type in ("withdrawal", "transfer", "sell", "trade"):
            legs[("out", t.from_account, t.from_asset, t.from_qty)].append(t)
    out: list[Finding] = []
    done: set[frozenset[str]] = set()
    manual = {t.tx_id: m for t in idx.txs if (m := idx.manual(t))}
    for (side, acc, aid, q), txs in sorted(legs.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0],
                                                                         str(kv[0][3]))):
        if len(txs) < 2 or not any(t.tx_id in manual for t in txs):
            continue
        txs = sorted(txs, key=lambda t: (t.ts, t.tx_id))
        times = [t.ts for t in txs]
        for m_tx in (t for t in txs if t.tx_id in manual):
            lo, hi = bisect_left(times, m_tx.ts - DUP_WINDOW), bisect_right(times, m_tx.ts + DUP_WINDOW)
            for other in txs[lo:hi]:
                a, b = (m_tx, other) if (m_tx.ts, m_tx.tx_id) <= (other.ts, other.tx_id) else (other, m_tx)
                pair = frozenset((a.tx_id, b.tx_id))
                if a.tx_id == b.tx_id or pair in done:
                    continue
                if a.tx_id in idx.dup_txs and b.tx_id in idx.dup_txs:
                    continue  # bereits als Hash-Dublette erfasst
                ha, hb = idx.hashes[a.tx_id], idx.hashes[b.tx_id]
                if ha and hb and not (ha & hb):
                    continue  # zwei verschiedene Blockchain-Transaktionen
                done.add(pair)
                out.append(_qty_finding(idx, side, acc, aid, q, a, b, manual.get(a.tx_id, []),
                                        manual.get(b.tx_id, [])))
    stats["same_qty_pairs"] = len(out)
    return out


def _qty_finding(idx: _Index, side: str, acc: str, aid: str, q: Decimal, a: Tx, b: Tx, ma: list[str],
                 mb: list[str]) -> Finding:
    # die „schwächer belegte“ Buchung (manuell, ohne Hash) ist Gegenstand des Szenarios
    def strength(t: Tx, m: list[str]) -> tuple[int, int, int]:
        return (0 if m else 1, 1 if idx.hashes[t.tx_id] else 0, 0 if idx.placeholder_time(t) else 1)

    weak, strong = (a, b) if strength(a, ma) <= strength(b, mb) else (b, a)
    m_weak = ma if weak is a else mb
    shared = idx.hashes[a.tx_id] & idx.hashes[b.tx_id]
    distinct = _sig_digits(q) >= DISTINCT_DIGITS
    strong_proof = bool(idx.hashes[strong.tx_id]) and not idx.manual(strong)
    status = "wahrscheinlich" if (distinct and (strong_proof or shared)) else "verdacht"
    verb = "gutgeschrieben" if side == "in" else "abgebucht"
    gap = abs(b.ts - a.ts)
    known = [f"Zwei Buchungen haben dem Konto {acc} jeweils exakt {_q(q)} {aid} {verb} (Abstand {_dur(gap)}).",
             idx.describe(a), idx.describe(b)]
    chk = idx.checks.get((acc, aid))
    cur = idx.bal(acc, aid)
    if chk is not None:
        same = abs(Decimal(str(chk["qty"])) - cur) <= max(Decimal("1e-8"), abs(cur) * Decimal("1e-6"))
        known.append(f"Soll-Bestand laut kuratiertem Import: {_q(Decimal(str(chk['qty'])))} {aid}; berechnet "
                     f"{_q(cur)} – {'intern konsistent (beide Buchungen enthalten)' if same else 'abweichend'}.")
    evidence = [f"Menge mit {_sig_digits(q)} signifikanten Stellen identisch"
                + (" – zufällige Gleichheit ist unwahrscheinlich" if distinct else " – runde Menge, Gleichheit kann "
                                                                                   "Zufall sein")]
    evidence += [f"{weak.tx_id}: {r}" for r in m_weak]
    if idx.placeholder_time(weak):
        evidence.append(f"{weak.tx_id}: Uhrzeit {weak.ts.astimezone(UTC).strftime('%H:%M:%S')} UTC bzw. nur Datum "
                        "– typisch für nachgetragene Buchungen")
    if not idx.hashes[weak.tx_id]:
        evidence.append(f"{weak.tx_id}: kein Transaktions-Hash")
    for h in sorted(idx.hashes[strong.tx_id]):
        evidence.append(f"{strong.tx_id}: Transaktions-Hash {_short(h)} – dieser Vorgang ist belegt")
    if shared:
        evidence.append("Beide Buchungen nennen denselben Hash.")
    if strong.type == "transfer" and strong.from_account:
        evidence.append(f"{strong.tx_id} ist ein Transfer von {strong.from_account} – der Zugang hat damit eine "
                        "bekannte Herkunft.")
    if weak.value_eur in (None, ZERO):
        evidence.append(f"{weak.tx_id}: ohne EUR-Wert (Einstand 0 €)")
    uncertainty = ["Zwei getrennte Vorgänge gleicher Menge sind möglich (z. B. erneuter Zugang, Bridge in zwei "
                   "Schritten). Nur ein Abgleich mit dem Explorer bzw. der Quelle zeigt, ob der Bestand zweimal "
                   "eingegangen ist."]
    if not any(s.account == acc for s in idx.snap.sources):
        uncertainty.append(f"Für {acc} liegt kein beobachteter Bestand vor (keine Datenquelle) – extern nicht "
                           "prüfbar.")
    f = Finding(
        kind="duplicate", status=status, priority=1,
        title=f"Gleiche Menge zweimal {verb}: {_q(q)} {aid} auf {acc}",
        known=known,
        suspected=[f"Die manuell erfasste Buchung {weak.tx_id} könnte denselben Vorgang ein zweites Mal abbilden "
                   f"wie {strong.tx_id}."],
        evidence=evidence, uncertainty=uncertainty,
        pairs=[(idx.ref(weak), idx.ref(strong), "gleiches Konto, gleiches Asset, exakt gleiche Menge, "
                                                f"Abstand {_dur(gap)}")],
        scenario=_scenario_without(idx, [weak], f"Szenario (hypothetisch): ohne die Buchung {weak.tx_id}. Es wird "
                                                "nichts gebucht und nichts ausgeschlossen."),
        decision=f"Beim Konto {acc} (Explorer bzw. Anbieter) prüfen, ob {_q(q)} {aid} einmal oder zweimal "
                 f"eingegangen sind. Nur wenn einmal: {weak.tx_id} im kuratierten Import entfernen bzw. in Portfolia "
                 "löschen (Überlagerung). Portfolia ändert nichts automatisch.",
        key=f"qty|{side}|{'|'.join(sorted((a.tx_id, b.tx_id)))}", weight=abs(q), positions=[(acc, aid)],
        data={"type": "same_qty", "weak": weak.tx_id, "strong": strong.tx_id, "account": acc, "asset": aid,
              "hashes": sorted(idx.hashes[strong.tx_id])})
    f.txs = [idx.ref(weak), idx.ref(strong)]
    return idx.attach(f, derive_positions=False)


def _dup_same_id(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Gleiche Anbieter-Kennung in Import und App-Buchungen (z. B. Bitpanda-UUID in der Koinly-Notiz und als
    API-Kennung) sowie manuelle Buchungen, die einer Import-Buchung stark ähneln."""
    by_key: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"import": set(), "journal": set()})
    for t in idx.txs:
        if t.origin == "import":
            keys = source_ref_keys(t.source, t.source_ref) | note_identity_keys(
                t.source, t.to_account or t.from_account, t.note, t.source_ref)
            for k in keys:
                by_key[k]["import"].add(t.tx_id)
        elif t.origin == "journal":
            m = idx.meta.get(t.tx_id)
            if m is not None:
                for k in identity_keys(m.event_key, m.aliases, m.external_id):
                    by_key[k]["journal"].add(t.tx_id)
    out: list[Finding] = []
    seen: set[frozenset[str]] = set()
    for k, sides in sorted(by_key.items()):
        if not sides["import"] or not sides["journal"]:
            continue
        ids = frozenset(sides["import"] | sides["journal"])
        if ids in seen:
            continue
        seen.add(ids)
        imp = [idx.by_id[x] for x in sorted(sides["import"])]
        jrn = [idx.by_id[x] for x in sorted(sides["journal"])]
        f = Finding(
            kind="duplicate", status="wahrscheinlich", priority=1,
            title=f"Gleiche Anbieter-Kennung im Import und in App-Buchungen: {_short(k, 24)}",
            known=[f"Kennung {k} kommt im kuratierten Import ({', '.join(t.tx_id for t in imp)}) und in "
                   f"App-Buchungen ({', '.join(t.tx_id for t in jrn)}) vor.",
                   *(idx.describe(t) for t in [*imp, *jrn])],
            suspected=["Derselbe Vorgang ist zweimal gebucht – über das Steuertool im Import und über die "
                       "Datenquelle bzw. CSV in der App."],
            evidence=["Anbieter-Kennungen (UUIDs) sind global eindeutig."],
            uncertainty=["Teilt eine Quelle einen Vorgang anders auf (z. B. Gebühr als eigene Zeile), passen die "
                         "Mengen je Buchung nicht 1:1 – die Summe je Vorgang ist maßgeblich."],
            pairs=[(idx.ref(imp[0]), idx.ref(j), f"gleiche Anbieter-Kennung {_short(k, 24)}") for j in jrn],
            scenario=_scenario_without(idx, jrn, "Szenario (hypothetisch): ohne die App-Buchungen dieses Vorgangs."),
            decision="Entscheiden, welche Fassung gilt: App-Buchung zurücknehmen bzw. als „im Import enthalten“ "
                     "markieren (Journal → Abgleich) – Portfolia ändert nichts automatisch.",
            key=f"id|{k}",
            data={"type": "import_vs_app", "imports": [t.tx_id for t in imp], "journals": [t.tx_id for t in jrn]})
        f.txs = [idx.ref(t) for t in [*imp, *jrn]]
        idx.dup_txs.update(ids)
        out.append(idx.attach(f))
    for jid, hits in sorted(idx.snap.journal_dups.items()):
        j = idx.by_id.get(jid)
        imps = [idx.by_id[h] for h in sorted(hits) if h in idx.by_id]
        if j is None or not imps or frozenset([jid, *hits]) in seen:
            continue
        f = Finding(
            kind="duplicate", status="verdacht", priority=2,
            title=f"Manuelle Buchung ähnelt einer Import-Buchung: {jid}",
            known=[idx.describe(j), *(idx.describe(t) for t in imps)],
            suspected=["Die manuelle Buchung könnte einen Vorgang wiederholen, den der Import bereits enthält."],
            evidence=["gleicher Typ, gleiche Konten und Assets, Datum ± 2 Tage, Menge ± 1 %"],
            uncertainty=["Grober Vergleich ohne Kennungen – regelmäßige gleichartige Vorgänge sind möglich."],
            pairs=[(idx.ref(j), idx.ref(t), "gleicher Typ, Konten und Assets; Datum ± 2 Tage; Menge ± 1 %")
                   for t in imps],
            scenario=_scenario_without(idx, [j], f"Szenario (hypothetisch): ohne die manuelle Buchung {jid}."),
            decision="Manuelle Buchung prüfen und bei Doppelung in Portfolia löschen – Portfolia ändert nichts "
                     "automatisch.",
            key=f"journal|{jid}|{'|'.join(sorted(hits))}",
            data={"type": "import_vs_app", "imports": [t.tx_id for t in imps], "journals": [jid], "rough": True})
        f.txs = [idx.ref(j), *(idx.ref(t) for t in imps)]
        out.append(idx.attach(f))
    stats["same_id"] = len(out)
    return out


def _dup_transfer_side(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """App-Zu- bzw. -Abgang (z. B. aus einer Wallet-Datenquelle), der einer Seite eines Import-Transfers entspricht –
    auch verzögert gutgeschrieben (Auszahlung der Börse Stunden bis Tage später) und unter anderem Kontonamen
    (Regeln: :mod:`app.csvimport.transfer_side`). Ohne Entscheidung zählt die Menge doppelt."""
    out: list[Finding] = []
    for jid, hits in sorted(idx.snap.journal_sides.items()):
        j = idx.by_id.get(jid)
        h = hits[0] if hits else None
        t = idx.by_id.get(h["tx"]) if h else None
        if j is None or t is None or h is None or (jid in idx.dup_txs and t.tx_id in idx.dup_txs):
            continue
        inn = h["role"] == "in"
        aid = (j.to_asset if inn else j.from_asset) or ""
        q = (j.to_qty if inn else j.from_qty) or ZERO
        acc_j = (j.to_account if inn else j.from_account) or ""
        side = "Zugangsseite" if inn else "Abgangsseite"
        status = "wahrscheinlich" if h.get("note_time") or (h["same"] and h["exact"]) else "verdacht"
        evidence = [f"Menge {'exakt ' if h['exact'] else 'nahezu '}gleich ({_q(q)} {aid})",
                    f"Abstand: {_dur(abs(j.ts - t.ts))} {'nach' if j.ts >= t.ts else 'vor'} dem Transfer"
                    + (" – verzögerte Auszahlung bzw. Gutschrift" if inn and j.ts - t.ts > timedelta(hours=2) else ""),
                    f"gleiches Konto {acc_j}" if h["same"] else
                    f"Konten: „{acc_j}“ (App) / „{h['account']}“ (Import) – vermutlich dasselbe Wallet, zwei Namen"]
        if h.get("note_time"):
            evidence.append("Zeitpunkt der Gutschrift laut Notiz der Import-Buchung bestätigt")
        uncertainty = ["Ohne gemeinsamen Transaktions-Hash beruht die Zuordnung auf Asset, Menge und Zeit – bei "
                       "exakter, unverwechselbarer Menge ist ein Zufall unwahrscheinlich, aber nicht ausgeschlossen."]
        if not h["same"]:
            uncertainty.append(f"Ob „{acc_j}“ und „{h['account']}“ dasselbe Wallet sind, folgt nicht aus den Buchungen "
                               "– Adresse bzw. Kontoauszug prüfen.")
        f = Finding(
            kind="duplicate", status=status, priority=1,
            title=f"{'Zugang' if inn else 'Abgang'} {_q(q)} {aid} doppelt? {side} des Import-Transfers {t.tx_id}",
            known=[idx.describe(j), idx.describe(t), f"Vermutlich {h['text']}."],
            suspected=[f"Die App-Buchung bildet dieselbe Bewegung ab wie die {side} des Transfers im kuratierten "
                       "Import – die Menge zählt doppelt, und als Zugang begänne sie einen neuen Einstand."],
            evidence=evidence, uncertainty=uncertainty,
            pairs=[(idx.ref(t), idx.ref(j), h["text"])],
            scenario=_scenario_without(idx, [j], f"Szenario (hypothetisch): ohne die App-Buchung {jid} – es zählt "
                                                 "nur der Transfer."),
            decision="App-Buchung als „im Import enthalten“ markieren (hier mit Vorschau bzw. unter Journal → "
                     "Abgleich); bei abweichendem Kontonamen danach die Konten angleichen. Portfolia ändert nichts "
                     "automatisch.",
            key=f"tside|{jid}|{t.tx_id}", weight=q,
            data={"type": "import_vs_app", "imports": [t.tx_id], "journals": [jid],
                  "side": {k: h[k] for k in ("role", "same", "exact", "account") if k in h}})
        f.txs = [idx.ref(t), idx.ref(j)]
        idx.dup_txs.update((jid, t.tx_id))
        out.append(idx.attach(f))
    stats["transfer_sides"] = len(out)
    return out


# ----------------------------------------------------------------------------------------------------
# Transfers
# ----------------------------------------------------------------------------------------------------

def _transfers(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Nicht verknüpfte Abgänge und Zugänge desselben Assets auf verschiedenen eigenen Konten."""
    def candidate(t: Tx, side: str) -> bool:
        if t.origin == "plan" or (t.tag or "").lower() not in NEUTRAL_TAGS:
            return False
        if side == "out":
            return t.type == "withdrawal" and bool(t.from_asset and t.from_account and t.from_qty
                                                     and not idx.asset(t.from_asset).is_fiat)
        return t.type == "deposit" and bool(t.to_asset and t.to_account and t.to_qty
                                              and not idx.asset(t.to_asset).is_fiat)

    deps: dict[str, list[Tx]] = defaultdict(list)
    for t in idx.txs:
        if candidate(t, "in"):
            deps[t.to_asset].append(t)  # type: ignore[index]
    dep_ts = {a: [t.ts for t in lst] for a, lst in deps.items()}
    cands: list[tuple[tuple[Any, ...], Tx, Tx, Decimal]] = []
    for w in idx.txs:
        if not candidate(w, "out"):
            continue
        lst = deps.get(w.from_asset or "", [])
        if not lst:
            continue
        ts = dep_ts[w.from_asset]  # type: ignore[index]
        lo, hi = bisect_left(ts, w.ts - TRANSFER_BEFORE), bisect_right(ts, w.ts + TRANSFER_AFTER)
        hw = idx.hashes[w.tx_id]
        for d in lst[lo:hi]:
            if d.to_account == w.from_account:
                continue
            hd = idx.hashes[d.tx_id]
            same_hash = bool(hw & hd)
            if not same_hash and hw and hd:
                continue  # zwei verschiedene Blockchain-Transaktionen
            ratio = (d.to_qty / w.from_qty) if w.from_qty else ZERO  # type: ignore[operator]
            if not same_hash and not (TRANSFER_MIN_RATIO <= ratio <= TRANSFER_MAX_RATIO):
                continue
            score = (0 if same_hash else 1, abs(1 - ratio), abs((d.ts - w.ts).total_seconds()), w.tx_id, d.tx_id)
            cands.append((score, w, d, ratio))
    cands.sort(key=lambda c: c[0])
    from app.diagnosis.audit import assign_transfers

    accepted, ambiguous = assign_transfers(idx, [(w, d, ratio) for _s, w, d, ratio in cands])
    used = idx.transfer_txs
    groups: dict[tuple[str, str, str, str], list[tuple[Tx, Tx, Decimal, bool]]] = defaultdict(list)
    for w, d, ratio in accepted:
        used.update((w.tx_id, d.tx_id))
        same_hash = bool(idx.hashes[w.tx_id] & idx.hashes[d.tx_id])
        gap = d.ts - w.ts
        if same_hash:
            status = "wahrscheinlich"
        elif ratio >= Decimal("0.98") and abs(gap) <= timedelta(hours=24):
            status = "verdacht"
        else:
            status = "hinweis"
        groups[(status, w.from_account or "", d.to_account or "", w.from_asset or "")].append(
            (w, d, ratio, same_hash))
    out: list[Finding] = []
    own_addr = {a.lower(): s.account for s in idx.snap.sources for a in s.addresses if len(a) >= 20}
    for (status, src, dst, aid), items in sorted(groups.items()):
        items.sort(key=lambda x: (x[0].ts, x[0].tx_id))
        pairs = []
        evidence = []
        for w, d, ratio, same_hash in items[:MAX_PAIRS_SHOWN]:
            gap = d.ts - w.ts
            diff = (w.from_qty or ZERO) - (d.to_qty or ZERO)
            why = [f"Abstand {_dur(gap)}{' (Zugang vor Abgang)' if gap < timedelta(0) else ''}",
                   f"Zugang = {qty_exact((ratio * 100).quantize(Decimal('0.01')))} % des Abgangs"]
            if diff > 0:
                why.append(f"Differenz {_q(diff)} {aid} (passt zu einer Netzwerk- bzw. Auszahlungsgebühr)"
                           if ratio >= Decimal("0.98") else f"Differenz {_q(diff)} {aid}")
            if w.fee_asset and w.fee_qty:
                why.append(f"Gebühr am Abgang {_q(w.fee_qty)} {w.fee_asset}")
            if (w.tx_id, d.tx_id) in getattr(idx, "transfer_exclusive", set()):
                why.append("Zuordnung nur durch Ausschluss konkurrierender Kandidaten eindeutig (globale Zuordnung)")
            why.append("gleicher Transaktions-Hash" if same_hash else "Hash fehlt auf mindestens einer Seite"
                       if not (idx.hashes[w.tx_id] and idx.hashes[d.tx_id]) else "verschiedene Hashes")
            for t in (w, d):
                for addr, acc in own_addr.items():
                    if addr in (t.note or "").lower() or addr in (t.source_ref or "").lower():
                        why.append(f"{t.tx_id} nennt eine Adresse der eigenen Datenquelle {acc}")
            pairs.append((idx.ref(w), idx.ref(d), "; ".join(why)))
            evidence.append(f"{_ts(w.ts)} −{_q(w.from_qty)} {aid} ({src}) → {_ts(d.ts)} +{_q(d.to_qty)} {aid} ({dst})")
        n = len(items)
        tot_w = sum((w.from_qty or ZERO for w, _d2, _r, _h in items), ZERO)
        val_in = sum((d.value_eur or ZERO for _w, d, _r, _h in items), ZERO)
        f = Finding(
            kind="transfer", status=status, priority=2 if status != "hinweis" else 3,
            title=f"{'Möglicher interner Transfer' if n == 1 else f'{n} mögliche interne Transfers'}: "
                  f"{src} → {dst} · {aid}",
            known=[f"{n} Abgang/Zugang-{'Paar' if n == 1 else 'Paare'} ohne Verknüpfung: Abgang von {src}, Zugang auf "
                   f"{dst}, gleiches Asset {aid}, Summe Abgänge {_q(tot_w)} {aid}.",
                   "Ohne Verknüpfung zählt der Abgang als Abgang ohne Gegenbuchung (Lots verlassen das Portfolio) "
                   "und der Zugang als neue Anschaffung zum EUR-Wert der Buchung (Summe "
                   f"{eur(val_in)}) – Haltedauer beginnt neu."],
            suspected=["Beide Seiten könnten derselbe Transfer zwischen eigenen Konten sein."],
            evidence=evidence + ([f"… und {n - MAX_PAIRS_SHOWN} weitere"] if n > MAX_PAIRS_SHOWN else []),
            uncertainty=(["Gleicher Hash belegt dieselbe Blockchain-Transaktion; ob beide Konten dem Nutzer gehören, "
                          "folgt aus der Kontoliste."] if status == "wahrscheinlich" else
                         ["Ohne gemeinsamen Hash beruht die Zuordnung auf Zeit und Menge – ein Abgang an Dritte und "
                          "ein zufällig ähnlicher Zugang sind möglich.",
                          "Adressen der Gegenseite liegen in den Buchungen meist nicht vor."]),
            pairs=pairs,
            scenario=Scenario(text="Szenario (hypothetisch): als interner Transfer verknüpft. Bestände je Konto "
                                   "blieben gleich; Einstand und Anschaffungsdatum gingen vom Abgangskonto über, der "
                                   "Zugang wäre keine neue Anschaffung. Es wird keine Verknüpfung angelegt.",
                              rows=[("Zugänge als Anschaffung", f"{n} × (Summe {eur(val_in)})",
                                     "0 – Lots vom Abgangskonto"),
                                    ("Abgänge ohne Gegenbuchung", f"{n}", "0")]),
            decision="Bestätigen, dass beide Konten dem Nutzer gehören und es sich um denselben Vorgang handelt; "
                     "dann im kuratierten Import als Transfer zusammenführen bzw. im Journal verknüpfen. Portfolia "
                     "legt keine Verknüpfung automatisch an.",
            key=f"transfer|{status}|{src}|{dst}|{aid}|" + "|".join(f"{w.tx_id}>{d.tx_id}" for w, d, _r, _h in items),
            weight=val_in,
            data={"type": "transfer", "pairs": [[w.tx_id, d.tx_id] for w, d, _r, _h in items]})
        f.txs = [x for w, d, _r, _h in items[:MAX_PAIRS_SHOWN] for x in (idx.ref(w), idx.ref(d))]
        out.append(idx.attach(f))
    stats["transfer_pairs"] = sum(len(v) for v in groups.values())
    from app.diagnosis.audit import ambiguous_transfers

    out += ambiguous_transfers(idx, ambiguous)
    stats["transfer_ambiguous"] = len(ambiguous)
    return out


# ----------------------------------------------------------------------------------------------------
# Asset- und Kurszuordnung
# ----------------------------------------------------------------------------------------------------

def _cg_name(idx: _Index, aid: str, cg_id: str | None) -> str | None:
    row = idx.snap.asset_sources.get(aid)
    if not row or not cg_id:
        return None
    try:
        for c in json.loads(row.get("candidates_json") or "[]"):
            if c.get("id") == cg_id:
                return str(c.get("name") or "") or None
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def _assets(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    out: list[Finding] = []
    flagged: set[str] = set()
    # (a) Anbieter-Kürzel mit abweichender Kursquelle (z. B. Bitpanda „TH“ = Threshold Network)
    prov_txs: dict[tuple[str, str], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        for acc, aid in ((t.to_account, t.to_asset), (t.from_account, t.from_asset)):
            if not aid:
                continue
            m = idx.meta.get(t.tx_id)
            prov = provider_of(m.source if m is not None else None, None, acc)
            if prov and identity(prov, idx.asset(aid).symbol):
                prov_txs[(prov, aid)].append(t)
    for (prov, aid), txs in sorted(prov_txs.items()):
        a = idx.asset(aid)
        pa = identity(prov, a.symbol)
        if pa is None or confirms(pa, a):
            continue
        txs = sorted({t.tx_id: t for t in txs}.values(), key=lambda t: (t.ts, t.tx_id))
        flagged.add(aid)
        src_row = idx.snap.asset_sources.get(aid) or {}
        checked = (src_row.get("checked_at") or "")[:10]
        checked_de = ".".join(reversed(checked.split("-"))) if len(checked) == 10 else checked
        how = (f"automatisch über das Symbol zugeordnet ({src_row.get('reason') or 'ohne Begründung'}, "
               f"{checked_de})" if src_row.get("origin") == "auto" and
               src_row.get("status") == "active" else "vom Nutzer zugeordnet" if src_row.get("origin") == "user"
               else "laut Import")
        cg_name = _cg_name(idx, aid, a.quote_id)
        mapped = (f"CoinGecko „{a.quote_id}“" + (f" ({cg_name})" if cg_name else "")) \
            if a.quote_source == "coingecko" and a.quote_id else f"{a.quote_source}" + (
            f" „{a.quote_id}“" if a.quote_id else "")
        held = sum((q for (acc, x), q in idx.led.balances.items() if x == aid), ZERO)
        p = idx.snap.prices.get(aid)
        known = [f"{PROVIDER_LABEL.get(prov, prov)} führt das Kürzel {pa.symbol} für {pa.name} (CoinGecko-ID "
                 f"{pa.coingecko}{', übliches Kürzel ' + pa.ticker if pa.ticker else ''}).",
                 f"Das Asset {aid} hat die Kursquelle {mapped} – {how}.",
                 f"{len(txs)} Buchung(en) mit {aid} auf {PROVIDER_LABEL.get(prov, prov)}-Konten, z. B. "
                 f"{idx.describe(txs[0])}.",
                 f"Bestand {_q(held)} {aid}."]
        evidence = []
        cur_val = None
        if p is not None and p.valued:
            cur_val = (held * Decimal(str(p.price_eur))).quantize(Decimal("0.01"))
            known.append(f"Angezeigte Bewertung: {_q(held)} × {_px(p.price_eur)} = {eur(cur_val)} (Kurs "
                         f"{p.source}, {p.kind}, Stand {_d(p.ts)}).")
        buys = [t for t in txs if t.to_asset == aid and t.to_qty and t.value_eur]
        sc = None
        if buys:
            last = buys[-1]
            unit = (last.value_eur / last.to_qty)  # type: ignore[operator]
            evidence.append(f"Kaufkurs am {_d(last.ts)}: {_px(unit)} je {aid} ({eur(last.value_eur)} für "
                            f"{_q(last.to_qty)})")
            if p is not None and p.valued and unit:
                factor = (Decimal(str(p.price_eur)) / unit).quantize(Decimal("0.01"))
                evidence.append(f"Zugeordneter Kurs {_px(p.price_eur)} = {qty_exact(factor)}-facher Kaufkurs")
            sc = Scenario(text=f"Szenario (hypothetisch): Bewertung zum Kaufkurs vom {_d(last.ts)} – das ist kein "
                               "Marktkurs, nur eine Größenordnung. Kurse und Zuordnung bleiben unverändert.",
                          rows=[(f"Wert {aid}", eur(cur_val) if cur_val is not None else "unbewertet",
                                 f"{eur((held * unit).quantize(Decimal('0.01')))} (Kaufkurs {_d(last.ts)})")])
        other_coin = a.quote_source in ("coingecko", "yahoo") and bool(a.quote_id)
        f = Finding(
            kind="asset", status="belegt" if other_coin else "hinweis", priority=1 if other_coin else 2,
            title=(f"{pa.symbol} bei {PROVIDER_LABEL.get(prov, prov)} ist {pa.name} – Asset {aid} wird mit "
                   f"{cg_name or a.quote_id} bewertet" if other_coin else
                   f"{pa.symbol} bei {PROVIDER_LABEL.get(prov, prov)} ist {pa.name} – Asset {aid} ohne passende "
                   "Kursquelle"),
            known=known,
            suspected=[],
            evidence=[*evidence, f"Die Kursquelle bestätigt den Anbieter-Coin nicht ({a.quote_id or '–'} ≠ "
                                 f"{pa.coingecko})."],
            uncertainty=["Einen aktuellen Kurs für den Anbieter-Coin fragt die Diagnose nicht ab – die tatsächliche "
                         "Bewertung ist hier nicht berechnet."],
            scenario=sc,
            decision=f"Kursquelle von {aid} auf CoinGecko „{pa.coingecko}“ ändern (Einstellungen → Kursquellen) "
                     "oder ein eigenes Asset anlegen. Portfolia ändert die bestehende Zuordnung nicht selbst; neue "
                     f"{pa.symbol}-Vorgänge von {PROVIDER_LABEL.get(prov, prov)} gehen in die Prüfung.",
            key=f"provider|{prov}|{aid}", weight=cur_val or ZERO,
            data={"type": "provider_quote", "asset": aid, "coin": pa.coingecko, "coin_name": pa.name,
                  "provider": prov, "current": a.quote_id if other_coin else None},
            positions=sorted((acc, x) for (acc, x), v in idx.led.balances.items() if x == aid and v))
        f.txs = [idx.ref(t) for t in txs[:20]]
        f.identifiers.append(f"{pa.symbol}@{prov.upper()}")
        out.append(idx.attach(f, derive_positions=False))
        f.assets = [aid]  # Gegenbeine (z. B. EUR) sind nicht betroffen
    # (b) mehrere Contracts je Asset und Chain
    for aid, keys in sorted(idx.snap.token_keys.items()):
        by_chain: dict[str, set[str]] = defaultdict(set)
        for k in keys:
            _sym, _, rest = k.partition("@")
            chain, _, contract = rest.partition(":")
            if contract:
                by_chain[chain.upper()].add(contract.lower())
        for chain, contracts in sorted(by_chain.items()):
            if len(contracts) < 2:
                continue
            on_chain = sorted(k for k in keys if k.upper().split("@", 1)[1].startswith(chain))
            f = Finding(
                kind="asset", status="verdacht", priority=2,
                title=f"{len(contracts)} Token-Contracts auf {chain} sind demselben Asset {aid} zugeordnet",
                known=[f"Zuordnungen: {', '.join(on_chain)}."],
                suspected=["Ein Contract könnte ein anderer Token mit gleichem Symbol sein (Fälschung, Spam) – oder "
                           "ein alter bzw. neuer Contract nach einer Migration."],
                evidence=[f"Asset {aid}: Kursquelle {idx.asset(aid).quote_source} {idx.asset(aid).quote_id or ''}"],
                uncertainty=["Ob beide Contracts denselben Coin darstellen, zeigt nur der Explorer bzw. die "
                             "Projektseite."],
                decision="Contracts im Explorer prüfen; eine falsche Zuordnung unter Einstellungen → CSV-Import → "
                         "Zuordnungen löschen. Bestehende Buchungen bleiben unverändert.",
                key=f"contracts|{aid}|{chain}",
                data={"type": "contracts", "asset": aid, "keys": on_chain})
            f.assets.append(aid)
            f.identifiers += sorted(contracts)
            out.append(idx.attach(f))
    # (c) automatische Kurszuordnungen nur über das Symbol (Einordnung)
    auto = [r for aid, r in sorted(idx.snap.asset_sources.items())
            if r.get("status") == "active" and r.get("origin") == "auto" and aid not in flagged
            and aid in idx.pf.assets]
    if auto:
        lines = [f"{r['asset_id']} → {r.get('quote_id')} ({r.get('confidence') or '–'}: {r.get('reason') or '–'})"
                 for r in auto]
        f = Finding(
            kind="asset", status="hinweis", priority=3,
            title=f"{len(auto)} Kurszuordnung(en) automatisch über das Symbol",
            known=["Für diese Assets hat Portfolia die CoinGecko-ID über Symbol, Chain und Kursplausibilität "
                   "bestimmt; eine Anbieter-ID oder Contract-Adresse lag nicht vor."],
            evidence=lines[:60] + ([f"… und {len(lines) - 60} weitere"] if len(lines) > 60 else []),
            uncertainty=["Gleiche Symbole bezeichnen bei verschiedenen Anbietern oft verschiedene Coins (z. B. TH)."],
            decision="Bei Zweifeln die Zuordnung unter Einstellungen → Kursquellen prüfen. Portfolia ändert "
                     "bestehende Zuordnungen nicht selbst.",
            key="auto-sources|" + "|".join(r["asset_id"] for r in auto))
        f.assets = [r["asset_id"] for r in auto]
        out.append(f)
    # (d) dieselbe Kurs-ID für mehrere Assets
    by_qid: dict[tuple[str, str], list[str]] = defaultdict(list)
    for aid, a in sorted(idx.pf.assets.items()):
        if a.quote_id and a.quote_source in ("coingecko", "yahoo") and not a.is_fiat:
            by_qid[(a.quote_source, a.quote_id)].append(aid)
    held = idx.led.holdings_by_asset()
    for (qs, qid), aids in sorted(by_qid.items()):
        if len(aids) < 2 or sum(1 for x in aids if held.get(x)) < 1:
            continue
        f = Finding(
            kind="asset", status="verdacht", priority=2,
            title=f"{len(aids)} Assets mit derselben Kursquelle {qs} „{qid}“: {', '.join(aids)}",
            known=[f"{x}: {idx.asset(x).name}, Bestand {_q(held.get(x, ZERO))}" for x in aids],
            suspected=["Zwei verschiedene Coins könnten mit demselben Kurs bewertet werden – oder ein Coin ist "
                       "unter zwei Asset-IDs geführt."],
            uncertainty=["Eine Umbenennung oder Migration kann zwei Asset-IDs für denselben Coin erklären."],
            decision="Zuordnungen prüfen; Portfolia ändert sie nicht selbst.",
            key=f"same-quote|{qs}|{qid}")
        f.assets = list(aids)
        out.append(f)
    stats["asset_findings"] = len(out)
    return out


# ----------------------------------------------------------------------------------------------------
# Historie
# ----------------------------------------------------------------------------------------------------

_ISSUE_TEXT = {
    "negative_balance": ("Bestand zeitweise negativ", "belegt", 1,
                         "Ein Abgang ist größer als der zu diesem Zeitpunkt gebuchte Bestand: Zugänge davor fehlen "
                         "oder sind falsch datiert."),
    "missing_lots": ("Abgang ohne Anschaffung", "belegt", 2,
                     "Für einen Teil des Abgangs gibt es kein Lot: Einstand 0 €, Haltedauer unbekannt."),
    "attribution_gap": ("Kontenzuordnung der Lots lückenhaft", "hinweis", 3,
                        "Beim konto-übergreifenden FIFO ließen sich Lots nicht vollständig umbuchen."),
    "ca_without_from": ("Kapitalmaßnahme ohne Abgangsbein", "hinweis", 3,
                        "Zugang aus einer Kapitalmaßnahme ohne zugehörigen Abgang."),
    "transfer_excess": ("Transfer: mehr empfangen als gesendet", "belegt", 2,
                        "Bei einem internen Transfer kam mehr an, als abging. Die Differenz wird ohne Anschaffung "
                        "geführt (Einstand 0 €, Haltedauer unbekannt, steuerlich nie als steuerfrei gewertet)."),
}
_HISTORY_FLAGS = {
    "KOINLY_NEG_BALANCE": "Koinly meldete einen negativen Bestand (fehlende Zugänge)",
    "SOURCE_ACCOUNT_WITHOUT_DATA": "Quellkonto ohne importierte Daten",
}


def _history(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    out: list[Finding] = []
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for i in idx.led.issues:
        if i.code in _ISSUE_TEXT:
            grouped[(i.code, i.asset or "", i.account or "")].append(i)
    for (code, aid, acc), issues in sorted(grouped.items()):
        label, status, prio, expl = _ISSUE_TEXT[code]
        txs = [idx.by_id[i.tx_id] for i in issues if i.tx_id in idx.by_id]
        twins = [t.tx_id for t in txs if t.tx_id in idx.twin_weak]
        f = Finding(
            kind="history", status=status, priority=prio,
            title=f"{label}: {aid or '–'}{' auf ' + acc if acc else ''}" + (f" ({len(issues)}×)" if len(issues) > 1
                                                                           else ""),
            known=[expl, *(i.message for i in issues[:10])] + ([f"… und {len(issues) - 10} weitere"]
                                                               if len(issues) > 10 else []),
            suspected=([f"Ursache wahrscheinlich ein doppelt gebuchter Umtausch ({', '.join(twins)}): siehe Befund "
                        "„Umtausch doppelt gebucht“ – dort lösen, nicht hier ausblenden."] if twins else
                       ["Die Transaktionshistorie dieses Kontos ist unvollständig (fehlender Import, nicht erfasster "
                        "Transfer oder falsche Reihenfolge)."]) if status != "hinweis" else [],
            uncertainty=["Ursache und fehlende Buchung lassen sich aus den vorhandenen Daten nicht bestimmen."],
            decision="Fehlende Zugänge belegen (Export der Quelle, Explorer) und im kuratierten Import ergänzen. "
                     "Portfolia ergänzt nichts automatisch.",
            key=f"issue|{code}|{aid}|{acc}")
        f.txs = [idx.ref(t) for t in txs[:20]]
        if acc and aid:
            f.positions.append((acc, aid))
        out.append(idx.attach(f, derive_positions=False))
    unmatched = next((i for i in idx.led.issues if i.code == "unmatched_transfers"), None)
    if unmatched is not None:
        out.append(Finding(kind="history", status="hinweis", priority=3, title="Zu- und Abgänge ohne Gegenbuchung",
                           known=[unmatched.message],
                           uncertainty=["Ein Teil davon sind Vorgänge mit Dritten, ein Teil nicht verknüpfte eigene "
                                        "Transfers (siehe „Möglicher interner Transfer“)."],
                           key="unmatched-transfers"))
    flagged: dict[tuple[str, str, str], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        for fl in _flags(t):
            if fl in _HISTORY_FLAGS:
                acc = t.from_account or t.to_account or ""
                flagged[(fl, acc, t.from_asset or t.to_asset or "")].append(t)
    for (fl, acc, aid), txs in sorted(flagged.items()):
        f = Finding(
            kind="history", status="belegt", priority=2,
            title=f"{_HISTORY_FLAGS[fl]}: {aid} auf {acc}",
            known=[f"{len(txs)} Buchung(en) mit Kennzeichen {fl}.", *(idx.describe(t) for t in txs[:5])],
            uncertainty=["Welche Zugänge fehlen, geht aus dem Kennzeichen nicht hervor."],
            decision="Daten des Quellkontos nachimportieren; Portfolia ergänzt nichts automatisch.",
            key=f"flag|{fl}|{acc}|{aid}", positions=[(acc, aid)] if acc and aid else [])
        f.txs = [idx.ref(t) for t in txs[:20]]
        out.append(idx.attach(f, derive_positions=False))
    opening = [t for t in idx.txs if (t.tag or "") == "opening_balance" and t.source != "reconstructed"
               and not any(fl.startswith("RECONSTRUCTED") for fl in _flags(t))]
    for t in opening:
        f = Finding(kind="history", status="hinweis", priority=3,
                    title=f"Anfangsbestand ohne Einzelbelege: {t.to_asset} auf {t.to_account}",
                    known=[idx.describe(t)],
                    uncertainty=["Die Historie vor diesem Datum fehlt; Einstand und Anschaffungsdatum des "
                                 "Anfangsbestands sind Annahmen."],
                    key=f"opening|{t.tx_id}")
        f.txs = [idx.ref(t)]
        out.append(idx.attach(f))
    for s in idx.snap.sources:
        bc = s.balance_check
        if bc.get("checked") and int(bc.get("differences") or 0):
            out.append(Finding(
                kind="history", status="belegt", priority=2,
                title=f"Datenquelle {s.name} ({s.provider_label}): Bestände weichen von den abgerufenen Vorgängen ab",
                known=[f"Bestandsprüfung beim letzten Abruf: {bc.get('differences')} von {bc.get('assets')} Assets "
                       "weichen von der Summe der abgerufenen Vorgänge ab.",
                       *(f"Beispiel: {x}" for x in (bc.get("examples") or [])[:5])],
                suspected=["Vorgänge fehlen im Abruf (Zeitraum, nicht abgebildete Vorgangsarten) oder Gebühren sind "
                           "anders verbucht."],
                uncertainty=["Verglichen wird mit der Summe der abgerufenen Vorgänge, nicht mit dem Bestand in "
                             "Portfolia."],
                decision="Fehlende Vorgänge per CSV-Export der Quelle ergänzen; Portfolia gleicht Bestände nie "
                         "automatisch aus.",
                accounts=[s.account], key=f"source-balance|{s.id}"))
        if s.gaps or s.backfill:
            out.append(Finding(
                kind="history", status="belegt" if s.gaps else "hinweis", priority=2,
                title=f"Datenquelle {s.name} ({s.provider_label}): Abruf {'mit Lücken' if s.gaps else 'unvollständig'}",
                known=[f"Zustand: {s.state}.", *(f"Lücke: {g}" for g in s.gaps)]
                + (["Erstabruf noch nicht abgeschlossen."] if s.backfill else []),
                uncertainty=["Vorgänge außerhalb des abgerufenen Zeitraums fehlen in Portfolia."],
                decision="Abruf fortsetzen bzw. Zeitraum per CSV-Export der Quelle ergänzen.",
                accounts=[s.account], key=f"source-gap|{s.id}"))
    stats["history_findings"] = len(out)
    return out


def _open_batches(snap: Snapshot, stats: dict[str, int]) -> list[Finding]:
    by_batch: dict[int, list[Any]] = defaultdict(list)
    for r in snap.open_rows:
        by_batch[r.batch_id].append(r)
    out = []
    for bid, rows in sorted(by_batch.items()):
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[r.status] += 1
        label = {"new": "neu", "invalid": "unvollständig", "unclear": "ungeklärt", "duplicate": "mögliche Dublette"}
        accs = sorted({r.account for r in rows if r.account})
        f = Finding(
            kind="history", status="hinweis", priority=2,
            title=f"Prüf-Stapel #{bid} ({rows[0].source}): {len(rows)} offene Vorgänge – noch nicht gebucht",
            known=[", ".join(f"{n} {label.get(k, k)}" for k, n in sorted(counts.items())) + ".",
                   "Offene Vorgänge zählen weder im Bestand noch in der Performance."],
            uncertainty=["Bis zur Entscheidung im Prüf-Stapel kann der berechnete Bestand vom tatsächlichen "
                         "abweichen."],
            decision=f"Prüf-Stapel #{bid} unter Journal → CSV-Import bzw. Datenquellen prüfen.",
            accounts=accs, key=f"batch|{bid}")
        f.positions = sorted({(r.account, a) for r in rows if r.account for a in r.assets})
        out.append(f)
    stats["open_rows"] = len(snap.open_rows)
    return out


# ----------------------------------------------------------------------------------------------------
# Rekonstruiert / geschätzt
# ----------------------------------------------------------------------------------------------------

def _estimate_group(t: Tx) -> str | None:
    flags = _flags(t)
    ref = (t.source_ref or "").lower()
    rec = (t.source or "") == "reconstructed" or any(f.startswith("RECONSTRUCTED") for f in flags)
    if not rec and "AVG_PRICE" not in flags:
        return None
    if ref.startswith("ausgleich|") or "ausgleich" in (t.note or "").lower():
        return "ausgleich"
    if ref.startswith("sparplan|") or "RECONSTRUCTED_PLAN" in flags:
        return "sparplan"
    if ref.startswith("gap|") or "RECONSTRUCTED_GAP" in flags or (t.tag or "") == "opening_balance":
        return "luecke"
    return "sonstige"


_EST_LABEL = {"ausgleich": "Ausgleichsbuchung", "sparplan": "rekonstruierte Sparplan-Ausführungen",
              "luecke": "rekonstruierter Anfangsbestand (Lücke)", "sonstige": "rekonstruierte Buchungen"}


def _estimated(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    out: list[Finding] = []
    groups: dict[tuple[str, str, str], list[Tx]] = defaultdict(list)
    for t in idx.txs:
        g = _estimate_group(t) if t.origin != "plan" else None
        if g:
            groups[(g, t.to_account or t.from_account or "", t.to_asset or t.from_asset or "")].append(t)
    n_rec = 0
    for (g, acc, aid), txs in sorted(groups.items()):
        n_rec += len(txs)
        ids = [t.tx_id for t in txs]
        a = idx.asset(aid)
        qty = sum((t.to_qty or ZERO for t in txs), ZERO)
        val = sum((t.value_eur or ZERO for t in txs), ZERO)
        flags = sorted({f for t in txs for f in _flags(t)})
        lots_q, lots_c, n_lots = idx.lots_from(ids)
        consumed = idx.consumed_from(ids)
        affected = []
        if n_lots:
            affected.append(f"Offene Lots aus diesen Buchungen: {_q(lots_q)} Stück, Einstand {eur(lots_c)} "
                            "(geschätzt) – unrealisierter G/V und Einstand der Position.")
        for year, (qq, cost, gain, kinds) in consumed.items():
            affected.append(f"Abgänge {year}: {_q(qq)} Stück aus diesen Lots ({', '.join(sorted(kinds))}), "
                            f"Einstand {eur(cost)}, G/V {eur(gain)} – Steuerauswertung {year}.")
        if not consumed:
            affected.append("Bisher keine Veräußerung aus diesen Lots – realisierte Gewinne und Steuerauswertungen "
                            "sind (noch) nicht betroffen.")
        if a.is_crypto:
            affected.append("Haltefrist (1 Jahr, § 23 EStG) zählt ab dem geschätzten Anschaffungsdatum – "
                            "Einstufung steuerfrei/steuerpflichtig kann sich verschieben.")
        else:
            affected.append("Wertpapier: FIFO-Reihenfolge und Einstand späterer Verkäufe hängen vom geschätzten "
                            "Datum bzw. Kurs ab.")
        flows = [t for t in txs if t.tx_id in idx.flow_txs]
        if flows:
            affected.append(f"Performance: {len(flows)} Zahlungsströme (TTWROR/IRR) mit geschätztem Wert bzw. Datum.")
        affected.append("Historie und Wertentwicklung ab " + _d(txs[0].ts) + " enthalten die geschätzten Werte.")
        tax_type = (a.extra or {}).get("tax_type")
        if tax_type and str(tax_type).startswith(("etf", "fund")):
            affected.append("Fonds: Bestand zum Jahresende geht in die Vorabpauschale ein.")
        title = f"{_EST_LABEL[g]}: {aid} auf {acc}" + (f" ({len(txs)} Buchungen)" if len(txs) > 1 else "")
        if g == "ausgleich":
            title = f"Ausgleichsbuchung {acc}: {aid}"
        known = [f"{len(txs)} Buchung(en) als rekonstruiert gekennzeichnet (Quelle „{txs[0].source or '–'}“, "
                 f"Kennzeichen {', '.join(flags) or '–'}).",
                 f"Zeitraum {_d(txs[0].ts)} – {_d(txs[-1].ts)}; Menge {_q(qty)}; Wert {eur(val)}."]
        if any(t.date_only for t in txs):
            known.append("Datum ohne Uhrzeit.")
        if "AVG_PRICE" in flags:
            known.append("Kurs = Durchschnittskurs (AVG_PRICE), nicht der Kurs am Ausführungstag.")
        if txs[0].note:
            known.append("Notiz: " + (txs[0].note[:300] + ("…" if len(txs[0].note) > 300 else "")))
        f = Finding(
            kind="estimated", status="belegt", priority=2 if g != "sparplan" else 3,
            title=title, known=known,
            suspected=["Anschaffungsdatum und Einstand weichen vermutlich von den tatsächlichen Werten ab."],
            evidence=affected,
            uncertainty=["Tatsächliche Kaufdaten und -kurse liegen nicht vor (keine Abrechnung) – Richtung und Größe "
                         "der Abweichung sind unbekannt."],
            decision="Abrechnungen bzw. Kontoauszüge nachreichen; erst dann die rekonstruierten Buchungen im "
                     "kuratierten Import ersetzen. Portfolia ändert sie nicht.",
            key=f"estimated|{g}|{acc}|{aid}", weight=val, positions=[(acc, aid)] if acc and aid else [])
        f.txs = [idx.ref(t) for t in txs[:40]]
        out.append(idx.attach(f, derive_positions=False))
    stats["reconstructed"] = n_rec
    # Sparplan-Schätzungen der App
    plans: dict[str, list[Tx]] = defaultdict(list)
    for t in idx.txs:
        if t.origin == "plan" and t.flag == "estimated":
            plans[t.source_ref or ""].append(t)
    for key, txs in sorted(plans.items()):
        f = Finding(
            kind="estimated", status="belegt", priority=3,
            title=f"{len(txs)} geschätzte Sparplan-Ausführung(en): {txs[0].to_asset} auf {txs[0].to_account}",
            known=[f"Von Portfolia fortgeschrieben (Sparplan {key}); noch nicht durch Import oder Freigabe belegt.",
                   f"Zeitraum {_d(txs[0].ts)} – {_d(txs[-1].ts)}."],
            uncertainty=["Ausführungskurs und -tag sind Schätzungen, bis der Import sie ersetzt."],
            decision="Ausführungen unter Sparpläne freigeben oder verwerfen bzw. den nächsten Import abwarten.",
            key=f"plan|{key}")
        f.txs = [idx.ref(t) for t in txs[:20]]
        out.append(idx.attach(f))
    # Buchungen ohne EUR-Wert (Einstand 0 €)
    missing = [t for t in idx.txs if "KOINLY_MISSING_RATE" in _flags(t)]
    if missing:
        by_asset: dict[str, int] = defaultdict(int)
        for t in missing:
            by_asset[t.to_asset or t.from_asset or "–"] += 1
        f = Finding(
            kind="estimated", status="belegt", priority=3,
            title=f"{len(missing)} Buchungen ohne EUR-Kurs beim Import (Einstand bzw. Erlös 0 €)",
            known=["Kennzeichen KOINLY_MISSING_RATE: Das Steuertool kannte keinen Kurs; der EUR-Wert ist 0 bzw. "
                   "fehlt.", "Je Asset: " + ", ".join(f"{a} ({n})" for a, n in sorted(by_asset.items()))],
            evidence=["Zugänge mit 0 € erhöhen bei späterem Verkauf den Gewinn; Abgänge mit 0 € mindern den Erlös."],
            uncertainty=["Ob der Token zum Zeitpunkt überhaupt handelbar war, ist unbekannt."],
            decision="Bei relevanten Beträgen EUR-Werte im kuratierten Import nachtragen.",
            key="missing-rate")
        f.txs = [idx.ref(t) for t in missing[:40]]
        out.append(idx.attach(f))
    return out


# ----------------------------------------------------------------------------------------------------
# Kurse
# ----------------------------------------------------------------------------------------------------

def _prices(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Gehaltene Positionen ohne gültigen Kurs (je Asset), mit Ersatzkurs (manuell/Transaktion, je Asset) und mit
    veraltetem Marktkurs (zusammengefasst – meist eine gemeinsame Ursache wie ein ausgefallener Abruf)."""
    from app.prices.fallback import KIND_LABEL, FallbackPrices

    held = {a: q for a, q in idx.led.holdings_by_asset().items() if q > 0 and not idx.asset(a).is_fiat}
    fb = FallbackPrices(idx.pf, idx.snap.settings, only=set(held))
    out: list[Finding] = []
    stale: list[tuple[str, str, Decimal]] = []

    def positions(aid: str) -> list[tuple[str, str]]:
        return sorted((acc, x) for (acc, x), v in idx.led.balances.items() if x == aid and v > 0)

    for aid in sorted(held):
        a = idx.asset(aid)
        q = held[aid]
        p = idx.snap.prices.get(aid)
        src_row = idx.snap.asset_sources.get(aid) or {}
        if a.quote_id and a.quote_source != "none":
            source = f"{a.quote_source} „{a.quote_id}“"
        elif src_row.get("status") == "suggested" and src_row.get("quote_id"):
            source = (f"keine – Vorschlag CoinGecko „{src_row['quote_id']}“ ({src_row.get('confidence') or '–'}) nicht "
                      "übernommen")
        else:
            source = "keine Kursquelle" + (f" (Suche: {src_row.get('reason')})" if src_row.get("reason") else "")
        if p is not None and p.valued and p.kind not in ("manual", "tx"):
            age = (idx.snap.now - p.ts) if p.ts else None
            limit = STALE_DAILY["crypto" if a.is_crypto else "security"]
            if (p.kind == "quote" and p.stale) or (p.kind == "daily" and (age is None or age > limit)):
                stale.append((aid, f"{aid}: {_px(p.price_eur)} ({p.source}, "
                                   f"{'Schlusskurs' if p.kind == 'daily' else 'Kurs'} vom {_d(p.ts)}"
                                   + (f", {age.days} Tage alt" if age is not None else "") + f"; Bestand {_q(q)})",
                              q * Decimal(str(p.price_eur))))
            continue
        lat = fb.latest(a, idx.snap.today)
        last = lat.point or lat.expired
        last_txt = (f"{KIND_LABEL[last.kind]} vom {_d(last.date)}: {_px(last.price)} (Alter "
                    f"{(idx.snap.today - last.date).days} Tage)") if last else "kein Kurspunkt (manuell/Transaktion)"
        data: dict[str, Any] = {"type": "price_fallback", "asset": aid}
        if p is None or not p.valued:
            status, prio = "belegt", 2
            title = f"Kein gültiger Kurs: {aid} wird mit 0 € bewertet"
            data = {"type": "unvalued", "asset": aid,
                    "suggestion": src_row.get("quote_id") if src_row.get("status") == "suggested" else None,
                    "suggestion_confidence": src_row.get("confidence"),
                    "positions": [[acc, str(v)] for acc, x in positions(aid) if (v := idx.bal(acc, x)) > 0]}
            known = [f"Bestand {_q(q)} {aid}; Kursquelle: {source}.",
                     f"Letzter Kurspunkt: {last_txt}"
                     + (f" – älter als das Höchstalter von {lat.max_age} Tagen, daher nicht verwendet."
                        if lat.expired is not None and lat.max_age else "."),
                     f"Bewertung: {p.note if p is not None and p.note else 'unbewertet'}."]
            weight = q * Decimal(str(last.price)) if last else ZERO
        elif p.kind in ("manual", "tx"):
            status, prio = "hinweis", 3
            age = (idx.snap.today - p.ts.date()).days if p.ts else None
            title = (f"{aid} mit {'manuellem Kurs' if p.kind == 'manual' else 'Transaktionskurs'} bewertet – kein "
                     "Marktkurs")
            known = [f"Bestand {_q(q)} {aid}; Kursquelle: {source}.",
                     f"Verwendet: {p.note or last_txt}" + (f" (Alter {age} Tage)" if age is not None else "") + ".",
                     "Das ist keine aktuelle Marktbewertung."]
            weight = q * Decimal(str(p.price_eur))
        else:
            continue
        f = Finding(kind="price", status=status, priority=prio, title=title, known=known,
                    uncertainty=["Der tatsächliche Marktwert ist unbekannt; die Diagnose fragt keine Kurse ab."],
                    decision="Kursquelle zuordnen (Einstellungen → Kursquellen) oder einen aktuellen manuellen Kurs im "
                             "kuratierten Import hinterlegen. Portfolia ändert Kurse und Werte nicht.",
                    key=f"price|{aid}", weight=weight.quantize(Decimal("0.01")) if weight else ZERO, data=data,
                    assets=[aid], positions=positions(aid), accounts=sorted({acc for acc, _x in positions(aid)}))
        for pos in f.positions:
            idx.findings_by_pos[pos].append(f)
        out.append(f)
    if stale:
        stale.sort(key=lambda x: (-x[2], x[0]))
        f = Finding(kind="price", status="belegt", priority=2,
                    title=f"{len(stale)} Marktkurs(e) veraltet",
                    known=["Für diese Positionen liegt kein aktueller Marktkurs vor; bewertet wird mit dem letzten "
                           "Kurs bzw. Schlusskurs (Stand je Zeile)."],
                    evidence=[line for _a, line, _w in stale[:80]]
                    + ([f"… und {len(stale) - 80} weitere"] if len(stale) > 80 else []),
                    uncertainty=["Häufige Ursache ist ein ausgefallener oder gedrosselter Kursabruf (Container aus, "
                                 "Kontingent) – siehe Datenqualität → Kursquellen."],
                    decision="Kursabruf prüfen; Portfolia ändert gespeicherte Kurse und Werte nicht.",
                    key="price-stale|" + "|".join(sorted(a for a, _l, _w in stale)), data={"type": "stale"},
                    weight=sum((w for _a, _l, w in stale), ZERO).quantize(Decimal("0.01")),
                    assets=[a for a, _l, _w in stale])
        f.positions = [pos for a, _l, _w in stale for pos in positions(a)]
        for pos in f.positions:
            idx.findings_by_pos[pos].append(f)
        out.append(f)
    stats["price_findings"] = len(out)
    stats["price_stale"] = len(stale)
    return out


# ----------------------------------------------------------------------------------------------------
# Migrationen
# ----------------------------------------------------------------------------------------------------

def _balance_at(idx: _Index, account: str, aid: str, when: datetime) -> Decimal:
    q = ZERO
    for t in idx.txs:
        if t.ts > when:
            break
        q += _effect([t]).get((account, aid), ZERO)
    return q


def _ratio_power(a: Decimal, b: Decimal) -> int | None:
    if a <= 0 or b <= 0:
        return None
    r = a / b
    for k in MIGRATION_POWERS:
        for sign in (1, -1):
            target = Decimal(10) ** (sign * k)
            if abs(r / target - 1) <= MIGRATION_TOL:
                return sign * k
    return None


def _price_history(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Historische Bewertung ohne Marktkurs des Kursanbieters (Tabelle ``price_gap`` der letzten Neuberechnung):
    geschätzt (Transaktions-, manueller, erster Marktkurs), fortgeschrieben oder ohne Kurs – je Asset mit Zeitraum,
    Tagen, Methode, Quelle und dem Ergebnis der Suche nach einem Ersatzanbieter."""
    from app.analytics.quality import KIND_LABEL

    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in idx.snap.price_gaps:
        if g.get("kind") in ("tx", "manual", "first", "none", "interp"):
            by_asset[str(g["asset_id"])].append(g)
    if not by_asset:
        return []
    lines: list[tuple[int, str]] = []
    est_days = gap_days = 0
    for aid, gs in by_asset.items():
        a = idx.asset(aid)
        series = (f"cg:{a.quote_id}" if a.quote_source == "coingecko" else f"yahoo:{a.quote_id}") if a.quote_id else ""
        meta = idx.snap.series_meta.get(series) or idx.snap.series_meta.get(f"demo:{series}") or {}
        days = sum(int(g["days"]) for g in gs)
        est_days += sum(int(g["days"]) for g in gs if g["kind"] in ("tx", "manual", "first"))
        gap_days += sum(int(g["days"]) for g in gs if g["kind"] in ("none", "interp"))
        parts = [f"{_d(date.fromisoformat(g['date_from']))}–{_d(date.fromisoformat(g['date_to']))}: {g['days']} Tage "
                 f"{KIND_LABEL.get(g['kind'], g['kind'])}" + (f" ({g['source']})" if g.get("source") else "")
                 for g in gs[:4]]
        why = ""
        if meta.get("alt_status") in ("rejected", "none") and meta.get("alt_note"):
            why = f" – Ersatzanbieter: {meta['alt_note']}"
        elif not a.quote_id or a.quote_source in ("none", "manual"):
            why = " – keine Kursquelle zugeordnet"
        lines.append((days, f"{aid}: " + "; ".join(parts) + (" …" if len(gs) > 4 else "") + why))
    lines.sort(key=lambda x: (-x[0], x[1]))
    f = Finding(
        kind="price", status="hinweis", priority=3,
        title=f"Historische Kurse: {len(by_asset)} Asset(s) zeitweise ohne Marktkurs",
        known=[f"An {est_days} gehaltenen Tagen ist der Kurs geschätzt, an {gap_days} Tagen fortgeschrieben bzw. "
               "nicht vorhanden (Summe über Assets).",
               "Häufigste Ursache: Der Kursanbieter liefert ältere Kurse im Tarif nicht (CoinGecko-Demo: 365 Tage). "
               "Portfolia ergänzt diese Zeit über einen Ersatzanbieter (Yahoo), aber nur nach bestandenem Abgleich im "
               "Überlappungszeitraum; sonst bleibt es bei der gekennzeichneten Schätzung."],
        evidence=[line for _d0, line in lines[:80]] + ([f"… und {len(lines) - 80} weitere"] if len(lines) > 80 else []),
        uncertainty=["Geschätzte Tage beeinflussen Wertverlauf und Rendite (TTWROR/IRR), nicht Bestände oder "
                     "Einstandswerte."],
        decision="Ersatzanbieter je Symbol ausdrücklich zuordnen (Einstellungen → Kurse) oder hinnehmen. Portfolia "
                 "ändert dabei keine Buchungen.",
        key="price-history|" + "|".join(sorted(by_asset)), data={"type": "price_history"},
        assets=sorted(by_asset))
    stats["price_history_assets"] = len(by_asset)
    return [f]


def _migrations(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    by_acc_sym: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (acc, aid), q in idx.led.balances.items():
        if q > DUST and not idx.asset(aid).is_fiat:
            by_acc_sym[(acc, idx.asset(aid).symbol.upper())].add(aid)
    out: list[Finding] = []
    for (acc, sym), aids in sorted(by_acc_sym.items()):
        if len(aids) < 2:
            continue
        lst = sorted(aids)
        for i, x in enumerate(lst):
            for y in lst[i + 1:]:
                qx, qy = idx.bal(acc, x), idx.bal(acc, y)
                k = _ratio_power(qx, qy)
                old, new, q_old, q_new = (x, y, qx, qy) if k is not None and k > 0 else (y, x, qy, qx)
                first_new = next((t for t in idx.txs if t.to_asset == new and t.to_account == acc), None)
                k_at = None
                if first_new is not None and first_new.to_qty:
                    k_at = _ratio_power(_balance_at(idx, acc, old, first_new.ts), first_new.to_qty)
                if k is None and k_at is None:
                    continue
                power = abs(k if k is not None else k_at)  # type: ignore[arg-type]
                a_old, a_new = idx.asset(old), idx.asset(new)
                contracts = {z: idx.snap.token_keys.get(z, []) for z in (old, new)}
                known = [f"Konto {acc}: {_q(q_old)} {old} und {_q(q_new)} {new} – Verhältnis 10^{power} : 1 "
                         f"(Abweichung ≤ {MIGRATION_TOL:%})." if k is not None else
                         f"Zugang {new} am {_d(first_new.ts)} ({_q(first_new.to_qty)}) entspricht dem damaligen "
                         f"Bestand {old} im Verhältnis 10^{power} : 1."]
                if first_new is not None:
                    known.append(f"Erster Zugang {new}: {idx.describe(first_new)}")
                for z, a_ in ((old, a_old), (new, a_new)):
                    known.append(f"{z}: {a_.name}" + (f", Koinly-ID {a_.koinly_id}" if a_.koinly_id else "")
                                 + (f", Status {a_.status}" if a_.status else "")
                                 + (f", Kategorie {a_.category}" if a_.category else ""))
                evidence = [f"gleiches Symbol {sym} auf demselben Konto",
                            f"Mengenverhältnis 10^{power} : 1 (typisch für eine Redenominierung)"]
                if a_new.status == "spam" or a_old.status == "spam":
                    evidence.append(f"{new if a_new.status == 'spam' else old} ist als Spam markiert – Spam-Airdrops "
                                    "imitieren gezielt Mengen echter Bestände.")
                if contracts[old] or contracts[new]:
                    evidence.append("Contract-Adressen: " + "; ".join(f"{z}: {', '.join(v) or 'keine'}"
                                                                      for z, v in contracts.items()))
                f = Finding(
                    kind="migration", status="verdacht", priority=2,
                    title=f"Möglicher Token-Migrationsvorgang: {old} → {new} (10^{power} : 1) auf {acc}",
                    known=known,
                    suspected=[f"{new} könnte der migrierte bzw. redenominierte Nachfolger von {old} sein – dann "
                               f"stünde derselbe Wert doppelt (alt und neu) im Bestand; oder {new} ist ein Spam-Token "
                               "mit nachgeahmter Menge."],
                    evidence=evidence,
                    uncertainty=["Ohne Contract-Adressen beider Token und Angaben des Projekts ist eine Migration "
                                 "nicht belegbar." if not (contracts[old] and contracts[new]) else
                                 "Contract-Adressen liegen vor; ob der neue Contract der offizielle Nachfolger ist, "
                                 "zeigt nur die Projektseite."],
                    scenario=Scenario(text=f"Szenario (hypothetisch): Wäre {new} der Nachfolger von {old}, entspräche "
                                           f"{_q(q_new)} {new} dem Bestand {_q(q_old)} {old}. Es erfolgt keine "
                                           "Abschreibung, Umrechnung oder Änderung des Spam-Status.",
                                      rows=[(f"Bestand {old}", _q(q_old), "0 (umgetauscht)"),
                                            (f"Bestand {new}", _q(q_new), _q(q_new))]),
                    decision="Contract-Adressen beider Token im Explorer bzw. beim Projekt prüfen; danach über "
                             "Migration (Kapitalmaßnahme), Abschreibung oder Spam-Status entscheiden.",
                    key=f"migration|{acc}|{old}|{new}",
                    data={"type": "migration", "account": acc, "old": old, "new": new, "q_old": str(q_old),
                          "q_new": str(q_new), "receipt": first_new.tx_id if first_new is not None else None,
                          "spam": new if a_new.status == "spam" else old if a_old.status == "spam" else None})
                if first_new is not None:
                    f.txs = [idx.ref(first_new)]
                f.positions = [(acc, old), (acc, new)]
                f.assets = [old, new]
                f.accounts = [acc]
                for z in (old, new):
                    f.identifiers += contracts[z]
                for pos in f.positions:
                    idx.findings_by_pos[pos].append(f)
                out.append(f)
    stats["migration_findings"] = len(out)
    return out


def _rename_ratio(q_from: Decimal, q_to: Decimal) -> int | None:
    """Verhältnis 1 : 1 bzw. 10^k : 1 (Redenominierung) – sonst ``None``."""
    if q_from <= 0 or q_to <= 0:
        return None
    if abs(q_from / q_to - 1) <= MIGRATION_TOL:
        return 0
    return _ratio_power(q_from, q_to)


def _rename_trades(idx: _Index, stats: dict[str, int]) -> list[Finding]:
    """Ticker-Umbenennung bzw. Redenominierung, die als steuerpflichtiger **Tausch** gebucht ist (z. B. vom Steuertool
    Koinly: ``ACN#…`` → ``ACN`` 1 : 1): Ausgangs- und Ziel-Asset bezeichnen dasselbe Instrument (gleiche
    Kurszuordnung bzw. gleiches Symbol), Mengenverhältnis 1 : 1 bzw. 10^k : 1, der Altbestand des Kontos geht
    vollständig über. Der Tausch realisiert dann einen Scheingewinn bzw. -verlust und setzt Anschaffungsdatum und
    Haltedauer neu. Lösung (nach Vorschau): als Kapitalmaßnahme „migration“ buchen – Einstand und Anschaffungsdatum
    gehen über."""
    weak = idx.twin_weak  # zusätzliche Buchung eines doppelten Umtauschs: dort zu lösen
    disp: dict[str, list[Disposal]] = defaultdict(list)
    for d in idx.led.disposals:
        disp[d.tx_id].append(d)
    out: list[Finding] = []
    for t in idx.txs:
        if t.type != "trade" or t.tx_id in weak or not _is_conversion(idx, t):
            continue
        old, new, acc = t.from_asset or "", t.to_asset or "", t.from_account or ""
        k = _rename_ratio(t.from_qty, t.to_qty)  # type: ignore[arg-type]
        inst = same_instrument(idx, old, new)
        if k is None or not inst:
            continue
        rest = _balance_before(idx, acc, old, t) - t.from_qty - (t.fee_qty if t.fee_asset == old and t.fee_qty
                                                                 else ZERO)
        if abs(rest) > max(DUST, t.from_qty * Decimal("1e-6")):  # type: ignore[operator]
            continue  # Altbestand geht nicht vollständig über – eher ein gewöhnlicher (Teil-)Tausch
        ds = [d for d in disp.get(t.tx_id, []) if d.asset == old]
        gain = sum((d.gain for d in ds), ZERO)
        cost = sum((d.cost for d in ds), ZERO)
        proceeds = sum((d.proceeds for d in ds), ZERO)
        acq = sorted({p.acq_date for d in ds for p in d.parts if p.acq_date})
        later_old = any(x.ts > t.ts and old in (x.from_asset, x.to_asset) and acc in (x.from_account, x.to_account)
                        for x in idx.txs)
        ratio = "1 : 1" if k == 0 else f"10^{abs(k)} : 1"
        status = "wahrscheinlich" if len(inst) >= 2 and not later_old else "verdacht"
        known = [idx.describe(t),
                 f"Der gesamte Bestand {old} auf {acc} geht über (danach 0); Mengenverhältnis {ratio}.",
                 f"Gebucht als Tausch: Veräußerung von {_q(t.from_qty)} {old} zum Erlös {eur(proceeds)} bei Einstand "
                 f"{eur(cost)} – realisiert {eur(gain)}; {new} beginnt mit Anschaffungsdatum {_d(t.ts)}."
                 if ds else "Gebucht als Tausch (Veräußerung und Neuanschaffung)."]
        if acq:
            known.append(f"Anschaffungsdaten des Altbestands: {_d(acq[0])}" + (f" … {_d(acq[-1])}" if len(acq) > 1
                                                                               else ""))
        evidence = [*inst, f"Mengenverhältnis {ratio}"]
        if not later_old:
            evidence.append(f"{old} kommt auf {acc} danach nicht mehr vor")
        f = Finding(
            kind="migration", status=status, priority=2,
            title=f"Umbenennung als Tausch gebucht: {old} → {new} auf {acc} ({_d(t.ts)})",
            known=known,
            suspected=[f"{new} ist derselbe Token wie {old} unter neuem Ticker bzw. neuer Kennung. Als Tausch gebucht "
                       "entstehen ein Scheingewinn bzw. -verlust und eine neue Haltedauer (bei Krypto: neue "
                       "Jahresfrist)."],
            evidence=evidence,
            uncertainty=["Ob es eine reine Umbenennung (steuerneutral, gleicher Token) oder ein Umtausch in einen "
                         "neuen Token (Vertragswechsel, ggf. steuerlich ein Tausch) war, zeigen nur die Angaben des "
                         "Projekts bzw. der Börse – im Zweifel steuerlich beraten lassen.",
                         "Ein Steuertool kann den Vorgang bewusst als Tausch führen; die Steuerberichte des Tools "
                         "weichen nach einer Umbuchung in Portfolia dann ab."],
            scenario=Scenario(text=f"Szenario (hypothetisch): als Kapitalmaßnahme gebucht gingen Einstand "
                                   f"{eur(cost)} und Anschaffungsdaten von {old} auf {new} über; der realisierte "
                                   f"Betrag {eur(gain)} entfiele. Es wird nichts gebucht.",
                              rows=[("realisiert durch den Tausch", eur(gain), eur(ZERO)),
                                    (f"Anschaffungsdatum {new}", _d(t.ts),
                                     _d(acq[0]) + (" …" if len(acq) > 1 else "") if acq else "wie Altbestand")]),
            decision="Angaben der Börse bzw. des Projekts prüfen; bei einer Umbenennung als Kapitalmaßnahme "
                     "(Migration) buchen. Portfolia ändert nichts automatisch.",
            key=f"rename-trade|{t.tx_id}", weight=abs(gain),
            data={"type": "rename_trade", "tx": t.tx_id, "account": acc, "old": old, "new": new,
                  "gain": str(gain), "ratio": k})
        f.txs = [idx.ref(t)]
        f.positions = [(acc, old), (acc, new)]
        out.append(idx.attach(f, derive_positions=False))
    stats["rename_trades"] = len(out)
    return out


# ----------------------------------------------------------------------------------------------------
# Bestandsabgleich
# ----------------------------------------------------------------------------------------------------

def _tol(v: Decimal) -> Decimal:
    return max(Decimal("1e-8"), abs(v) * Decimal("1e-9"))


def _soll_eq(expected: Decimal, v: Decimal) -> bool:
    return abs(expected - v) <= max(Decimal("1e-8"), abs(expected) * Decimal("1e-6"))


_APP_LABEL = {"journal": "App-Buchung(en)", "plan": "Sparplan-Buchung(en)", "hidden": "in Portfolia ausgeblendete "
              "Import-Buchung(en)", "edited": "in Portfolia geänderte Import-Buchung(en)"}


def _app_changes(idx: _Index) -> tuple[dict[tuple[str, str], Decimal] | None, dict[tuple[str, str], dict[str, int]]]:
    """Bestände laut unverändertem Import (je Konto/Asset) und Änderungen in Portfolia, die eine Position berühren
    (App-Buchungen, Sparplan-Buchungen, ausgeblendete bzw. geänderte Import-Buchungen)."""
    base = idx.snap.base
    if base is None:
        return None, {}
    raw: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    raw_by_id: dict[str, dict[tuple[str, str], Decimal]] = {}
    for t in base.txs:
        e = _effect([t])
        raw_by_id[t.tx_id] = e
        for k, v in e.items():
            raw[k] += v
    why: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    present: set[str] = set()
    for t in idx.txs:
        e = _effect([t])
        if t.origin == "import" and t.tx_id in raw_by_id:
            present.add(t.tx_id)
            r = raw_by_id[t.tx_id]
            for k in set(r) | set(e):
                if r.get(k, ZERO) != e.get(k, ZERO):
                    why[k]["edited"] += 1
        elif t.origin in ("journal", "plan"):
            for k in e:
                why[k][t.origin] += 1
    for tid, r in raw_by_id.items():
        if tid not in present:
            for k in r:
                why[k]["hidden"] += 1
    return raw, why


def _app_text(row: HoldingRow, raw_q: Decimal, why: dict[str, int]) -> str:
    parts = ", ".join(f"{n} {_APP_LABEL[k]}" for k, n in sorted(why.items()) if n)
    return (f"Soll laut Import {_q(row.expected)} = Bestand aus den Import-Buchungen ({_q(raw_q)}); die Differenz "
            f"{_q(row.computed - raw_q)} stammt aus Änderungen in Portfolia: {parts}")


def _holdings(idx: _Index, stats: dict[str, int]) -> tuple[list[HoldingRow], list[Finding]]:
    snap = idx.snap
    src_by_id = {s.id: s for s in snap.sources}
    reporting = {o.source_id for o in snap.observed}  # Quellen, die Bestände melden (Wallets)
    src_accounts: dict[str, list[Any]] = defaultdict(list)
    for s in snap.sources:
        if s.id in reporting:
            src_accounts[s.account].append(s)
    obs: dict[tuple[str, str], list[Observed]] = defaultdict(list)
    unmapped: dict[str, list[Observed]] = defaultdict(list)
    for o in snap.observed:
        s = src_by_id.get(o.source_id)
        if s is None:
            continue
        if o.asset_id:
            obs[(s.account, o.asset_id)].append(o)
        elif o.how != "ignored" and o.qty:
            unmapped[s.account].append(o)
    from app.diagnosis import audit

    refs: dict[tuple[str, str], Any] = {}
    for r in snap.references:  # je Position der jüngste Referenzbestand
        cur = refs.get((r.account, r.asset_id))
        if cur is None or (r.as_of, r.id) > (cur.as_of, cur.id):
            refs[(r.account, r.asset_id)] = r
    keys = {k for k, v in idx.led.balances.items() if abs(v) > DUST} | set(obs) | set(refs)
    keys |= {(h["account"], h["asset_id"]) for h in idx.pf.holdings_check if h.get("account")}
    last_sync: dict[str, datetime] = {}
    for s in snap.sources:
        if s.last_success_at is not None and (s.account not in last_sync or s.last_success_at > last_sync[s.account]):
            last_sync[s.account] = s.last_success_at
    open_by_acc: dict[str, list[Any]] = defaultdict(list)
    for r in snap.open_rows:
        if r.account:
            open_by_acc[r.account].append(r)
    rows: list[HoldingRow] = []
    findings: list[Finding] = []
    totals = idx.led.holdings_by_asset()
    raw, why = _app_changes(idx)
    raw_tot: dict[str, Decimal] = defaultdict(lambda: ZERO)
    why_tot: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for (_acc, x), v in (raw or {}).items():
        raw_tot[x] += v
    for (_acc, x), w in why.items():
        for k, n in w.items():
            why_tot[x][k] += n

    def internal(row: HoldingRow, raw_q: Decimal | None, changes: dict[str, int] | None) -> str:
        assert row.expected is not None
        if _soll_eq(row.expected, row.computed):
            return "intern_ok"
        if raw_q is not None and changes and _soll_eq(row.expected, raw_q):
            row.explanations.append(_app_text(row, raw_q, changes))
            return "intern_app"
        return "intern_diff"

    for h in idx.pf.holdings_check:
        if h.get("account"):
            continue
        aid = h["asset_id"]
        row = HoldingRow(account="(alle Konten)", asset=aid, name=idx.asset(aid).name,
                         computed=totals.get(aid, ZERO), expected=Decimal(str(h["qty"])),
                         expected_as_of=idx.pf.valuation_date)
        row.status = internal(row, raw_tot.get(aid, ZERO) if raw is not None else None, why_tot.get(aid))
        rows.append(row)
        f = _holding_finding(idx, row)
        if f is not None:
            findings.append(f)
    for acc, aid in sorted(keys):
        comp = idx.bal(acc, aid)
        row = HoldingRow(account=acc, asset=aid, name=idx.asset(aid).name, computed=comp,
                         platform=audit.platform(idx, acc), identity=audit.identity(idx, aid),
                         last_sync=last_sync.get(acc), families=audit.families_of(idx, acc, aid))
        chk = idx.checks.get((acc, aid))
        self_ref = False
        if chk is not None:
            row.expected = Decimal(str(chk["qty"]))
            extra = chk.get("extra") or {}
            as_of = extra.get("as_of")
            # Soll aus einem Portfolia-Gesamtexport: von Portfolia selbst berechnet – keine unabhängige Referenz
            self_ref = str(extra.get("note") or "").strip() == "Export"
            try:
                row.expected_as_of = date.fromisoformat(str(as_of)[:10]) if as_of else idx.pf.valuation_date
            except ValueError:
                row.expected_as_of = idx.pf.valuation_date
        o_list = obs.get((acc, aid), [])
        srcs = src_accounts.get(acc, [])
        if o_list:
            row.observed = sum((o.qty for o in o_list), ZERO)
            times = [o.observed_at for o in o_list if o.observed_at]
            row.observed_at = min(times) if times else None
            used = [src_by_id[o.source_id] for o in o_list]
            row.observed_by = ", ".join(sorted({f"{s.provider_label} ({s.name})" for s in used}))
            row.observed_state = "; ".join(sorted({s.state for s in used}))
            fresh = row.observed_at is not None and snap.now - row.observed_at <= EXTERNAL_FRESH
            complete = all(s.complete for s in used)
            # gleicher Stichtag: Soll zum Abrufzeitpunkt (Buchungen danach zählen nicht)
            soll = audit.soll_at_time(idx, acc, aid, row.observed_at) if row.observed_at else comp
            row.computed_at_obs = soll
            if soll != comp:
                row.explanations.append(f"Buchungen nach dem Abruf ändern den Bestand um "
                                        f"{_q(comp - soll)} (nicht Teil des Vergleichs)")
            equal = abs(row.observed - soll) <= _tol(soll)
            row.status = ("extern_ok" if equal else "extern_diff") if fresh and complete else "extern_unsicher"
            if row.status == "extern_ok":
                row.quality = "aktueller Bestand bestätigt – kein Nachweis einer vollständigen Buchungshistorie"
            if not fresh:
                row.explanations.append("Abruf älter als 48 h" if row.observed_at else "Abrufzeit unbekannt")
            if not complete:
                row.explanations.append("Abruf unvollständig bzw. mit Lücken: " + row.observed_state)
        elif srcs:
            row.status = "extern_unsicher"
            row.observed_by = ", ".join(sorted({f"{s.provider_label} ({s.name})" for s in srcs}))
            row.observed_state = "; ".join(sorted({s.state for s in srcs}))
            row.explanations.append("Anbieter meldet für dieses Asset keinen Bestand (nicht geliefert, nicht "
                                    "zugeordnet oder 0)")
        elif row.expected is not None and not self_ref:
            row.status = internal(row, raw.get((acc, aid), ZERO) if raw is not None else None, why.get((acc, aid)))
        else:
            row.status = "offen"
        if self_ref:
            row.explanations.append("Soll stammt aus einem Portfolia-Gesamtexport (von Portfolia selbst berechnet) – "
                                    "keine unabhängige Referenz; dafür einen Referenzbestand hinterlegen")
        ref = refs.get((acc, aid))
        if ref is not None:
            audit.apply_reference(idx, row, ref)
        _explain(idx, row, open_by_acc.get(acc, []), unmapped.get(acc, []))
        row.quality = row.quality or row.observed_state or ("Referenzbestand (Nutzer)" if ref is not None else
                                             "Soll aus Portfolia-Export" if self_ref else
                                             "Soll aus kuratiertem Import" if row.expected is not None else
                                             "nur Buchungen")
        row.confidence = {"extern_ok": "belegt", "extern_diff": "belegt", "ref_ok": "belegt", "ref_diff": "belegt",
                          "extern_unsicher": "verdacht", "intern_diff": "belegt"}.get(row.status, "hinweis"
                                                                                      if row.status != "offen" else "")
        rows.append(row)
        f = _holding_finding(idx, row) if row.status != "ref_diff" else audit.reference_finding(idx, row)
        if f is not None:
            findings.append(f)
    for k in ("extern_ok", "extern_diff", "extern_unsicher", "intern_ok", "intern_app", "intern_diff", "ref_ok",
              "ref_diff", "offen"):
        stats[f"holdings_{k}"] = sum(1 for r in rows if r.status == k)
    return rows, findings


def _explain(idx: _Index, row: HoldingRow, open_rows: list[Any], unmapped: list[Observed]) -> None:
    acc, aid = row.account, row.asset
    n_open = sum(1 for r in open_rows if aid in r.assets)
    n_open_acc = len(open_rows) - n_open
    if n_open:
        row.explanations.append(f"{n_open} offene Vorgänge im Prüf-Stapel (noch nicht gebucht)")
    elif n_open_acc and row.status in ("extern_diff", "extern_unsicher", "intern_diff"):
        row.explanations.append(f"{n_open_acc} offene Vorgänge des Kontos im Prüf-Stapel (Asset nicht zugeordnet)")
    if unmapped and row.status in ("extern_diff", "extern_unsicher"):
        row.explanations.append(f"{len(unmapped)} beobachtete Bestände des Kontos ohne Asset-Zuordnung")
    if aid.upper() in FEE_ASSETS and row.status in ("extern_diff", "extern_unsicher") and row.diff:
        row.explanations.append("Netzwerkgebühren können fehlen (Gebühren-Asset der Chain)")
    for f in idx.findings_by_pos.get((acc, aid), []):
        # nur Befunde, die eine Mengendifferenz erklären können (Kurse, Zuordnung, EUR-Werte ändern keine Menge)
        txt = {"duplicate": "Dublettenverdacht", "transfer": "nicht verknüpfter Transfer",
               "migration": "möglicher Migrationsvorgang", "estimated": "geschätzte Menge",
               "history": "Historie"}.get(f.kind)
        if txt and f.key != "missing-rate":
            line = f"{txt}: {f.title}"
            if line not in row.explanations:
                row.explanations.append(line)


def _holding_finding(idx: _Index, row: HoldingRow) -> Finding | None:
    if row.status == "extern_diff":
        status, prio = "belegt", 1
        title = f"Bestand weicht von der externen Quelle ab: {row.asset} auf {row.account}"
    elif row.status == "extern_unsicher" and row.observed is not None and row.diff:
        status, prio = "verdacht", 2
        title = f"Abweichung zu einem nicht bestätigten externen Bestand: {row.asset} auf {row.account}"
    elif row.status == "intern_diff":
        status, prio = "belegt", 2
        title = f"Berechneter Bestand weicht vom Soll des Imports ab: {row.asset} auf {row.account}"
    else:
        return None
    known = [f"Berechnet aus Buchungen: {_q(row.computed)} {row.asset}."]
    if row.observed is not None:
        known.append(f"Beobachtet: {_q(row.observed)} {row.asset} ({row.observed_by}, Stand "
                     f"{_ts(row.observed_at) if row.observed_at else 'unbekannt'}; {row.observed_state}).")
        known.append(f"Differenz (beobachtet − berechnet): {_q(row.diff)} {row.asset}.")
    if row.expected is not None:
        known.append(f"Soll laut kuratiertem Import ({_d(row.expected_as_of)}): {_q(row.expected)} {row.asset}; "
                     f"Differenz {_q(row.internal_diff)}.")
    f = Finding(kind="holdings", status=status, priority=prio, title=title, known=known,
                suspected=[f"mögliche Erklärung: {e}" for e in row.explanations] or
                ["keine Erklärung in den Daten erkennbar"],
                uncertainty=["Ein veralteter oder unvollständiger Abruf kann die Differenz allein erklären."]
                if row.status == "extern_unsicher" else [],
                decision="Ursache klären (Prüf-Stapel, fehlende Buchungen, Gebühren); Portfolia gleicht Bestände nie "
                         "automatisch aus und speichert keine Bestände.",
                accounts=[row.account], assets=[row.asset], positions=[(row.account, row.asset)],
                key=f"holding|{row.account}|{row.asset}|{row.status}",
                weight=abs(row.diff if row.diff is not None else row.internal_diff or ZERO),
                data={"type": "holding", "account": row.account, "asset": row.asset, "status": row.status,
                      "computed": str(row.computed), "observed": None if row.observed is None else str(row.observed),
                      "expected": None if row.expected is None else str(row.expected),
                      "observed_at": row.observed_at.isoformat() if row.observed_at else None})
    return f


def report_for(ctx: Any) -> Report:
    """Diagnose des laufenden Kontexts (nur lesend)."""
    from app.diagnosis.collect import collect

    return diagnose(collect(ctx))

