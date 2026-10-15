"""Derivate in der Konfliktklärung: Lücken zwischen Importkennzeichnung, Ledger und Auswertung – ohne zweite Engine.

Bestandsaufnahme (Code, 0.28.3)

* Importkennzeichnungen ``realized_gain``, ``realized_loss``, ``futures_pnl``, ``funding``, ``liquidation``,
  ``margin_interest`` und ``margin`` (:data:`app.importer.contract.DERIVATIVE_TAGS`) sind mengenwirksam: Zugang →
  Lot mit Herkunft „derivative“, Abgang → Veräußerung der Art „derivative“. Beides zählt **nicht** zu
  ``REALIZED_KINDS`` und ist daher in keiner Ergebnisauswertung sichtbar. Das ist richtig, solange nicht still
  steuerlich eingeordnet wird – aber es gibt dadurch **keinen gesonderten Ausweis des wirtschaftlichen
  Derivateergebnisses**. Dieses Modul rechnet ihn getrennt aus den Buchungen (:func:`economics`) und fügt „derivative“
  bewusst **nicht** zu ``REALIZED_KINDS`` hinzu: Der Gewinn aus verbrauchten Settlement-Asset-Lots ist nicht der
  Handelsverlust des Derivats (ein realisierter Verlust von 30 USDT verbraucht Lots mit eigenem Einstand; der
  Handelsverlust sind die 30 USDT, nicht Erlös − Einstand dieser Lots).
* ``margin`` (Umbuchung auf das Derivatekonto) wird als Abgang + Zugang abgebildet: das Zugangs-Lot erhält ein neues
  Anschaffungsdatum. Eine Umbuchung ist kein Handelsverlust. Ob eine ``margin``-Zeile eine reine Umbuchung ist, hängt
  von der Anbietersemantik ab – das Etikett „Margin“ genügt nicht. Paare gleicher Menge werden deshalb nur als
  **Kandidat** für einen internen Transfer angeboten (nie automatisch).
* CSV-Profile (z. B. Blockpit, CoinTracking, Binance) überspringen Margin-/Derivatezeilen und zählen sie nur in der
  Zusammenfassung des Stapels. Solche Zeilen sind **nicht erledigt**: :func:`unsupported` macht daraus konkrete offene
  Vorgänge, die bestehen bleiben, bis jemand sie ausdrücklich zurückstellt.
* Doppelabzug: Ob ein ausgewiesenes Ergebnis Gebühren/Funding bereits enthält, ist anbieterabhängig. Es sind keine
  Anbietersemantiken verifiziert hinterlegt (:data:`VERIFIED`) – daher bleibt jeder Fall mit Derivatebuchungen
  hinsichtlich möglicher Doppelerfassung offen und wird nicht still bereinigt.
* Die steuerliche Behandlung von Derivaten ist **nicht** implementiert und bleibt ausdrücklich offen.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from app.importer import contract as C

ZERO = Decimal(0)
CENT = Decimal("0.01")
MARGIN_WINDOW = timedelta(minutes=5)  # Suchfenster für Umbuchungspaare (Suchhinweis, kein Beleg)

# Verifizierte Anbietersemantik (Quelle: Anbieterdokumentation, geprüft). Leer: nichts wird angenommen.
VERIFIED: dict[str, dict[str, Any]] = {}

ROLE = {  # Kennzeichnung → (Bezeichnung, Vorzeichen aus der Buchungsrichtung)
    "realized_gain": ("realisierter Gewinn", "dir"), "realized_loss": ("realisierter Verlust", "dir"),
    "futures_pnl": ("Futures-Ergebnis", "dir"), "funding": ("Funding", "dir"),
    "liquidation": ("Liquidation", "dir"), "margin_interest": ("Marginzinsen", "dir"),
    "margin": ("Margin-Umbuchung", "neutral"),
}


@dataclass
class DEvent:
    tx_id: str
    ts: Any
    account: str
    asset: str
    tag: str
    qty: Decimal  # vorzeichenbehaftet: Zugang +, Abgang −
    value_eur: Decimal | None  # Betrag der Buchung (positiv); None = unbekannt
    signed_eur: Decimal | None = None


@dataclass
class Econ:
    """Wirtschaftliches Derivateergebnis (getrennt vom Spot-Ergebnis; keine steuerliche Einordnung)."""

    year: int
    account: str
    asset: str
    by_tag: dict[str, Decimal] = field(default_factory=dict)  # vorzeichenbehaftete Mengen je Kennzeichnung
    result_qty: Decimal = ZERO  # Summe ohne Margin-Umbuchungen
    result_eur: Decimal | None = ZERO  # None, sobald ein Betrag unbekannt ist (kein stilles 0)
    n: int = 0
    n_unknown: int = 0
    margin_qty: Decimal = ZERO  # nur Angabe: Umbuchungen sind kein Ergebnis


def events(txs: list[Any], positions: set[tuple[str, str]] | None = None) -> list[DEvent]:
    out: list[DEvent] = []
    for t in txs:
        tag = (t.tag or "").lower()
        if tag not in C.DERIVATIVE_TAGS:
            continue
        if t.type == "deposit" and t.to_account and t.to_asset and t.to_qty is not None:
            acc, asset, q = t.to_account, t.to_asset, t.to_qty
        elif t.type == "withdrawal" and t.from_account and t.from_asset and t.from_qty is not None:
            acc, asset, q = t.from_account, t.from_asset, -t.from_qty
        else:
            continue
        if positions is not None and (acc, asset) not in positions:
            continue
        v = abs(t.value_eur) if t.value_eur is not None else None
        sv = None if v is None else (v if q >= 0 else -v)
        out.append(DEvent(t.tx_id, t.ts, acc, asset, tag, q, v, sv))
    return out


def economics(txs: list[Any], positions: set[tuple[str, str]] | None = None) -> list[Econ]:
    """Wirtschaftliches Derivateergebnis je Jahr, Konto und Asset – aus den Buchungen, getrennt vom Spot-Ergebnis.
    Margin-Umbuchungen zählen nicht zum Ergebnis; unbekannte Beträge machen die EUR-Summe unbekannt (nicht 0)."""
    acc: dict[tuple[int, str, str], Econ] = {}
    for e in events(txs, positions):
        key = (e.ts.year, e.account, e.asset)
        r = acc.setdefault(key, Econ(*key))
        r.by_tag[e.tag] = r.by_tag.get(e.tag, ZERO) + e.qty
        r.n += 1
        if e.tag == "margin":
            r.margin_qty += e.qty
            continue
        r.result_qty += e.qty
        if e.signed_eur is None:
            r.n_unknown += 1
            r.result_eur = None
        elif r.result_eur is not None:
            r.result_eur += e.signed_eur
    return sorted(acc.values(), key=lambda r: (r.year, r.account, r.asset))


def margin_pairs(txs: list[Any], positions: set[tuple[str, str]] | None = None) -> list[tuple[Any, Any]]:
    """Eindeutige 1:1-Paare ``margin``-Abgang/-Zugang (gleiches Asset, gleiche Menge, verschiedene Konten, kurzer
    Abstand). Mehrdeutige Zuordnungen (mehrere gleich gute Partner) ergeben kein Paar. Suchhinweis, kein Beleg."""
    outs = [t for t in txs if (t.tag or "").lower() == "margin" and t.type == "withdrawal" and t.from_qty]
    ins = [t for t in txs if (t.tag or "").lower() == "margin" and t.type == "deposit" and t.to_qty]
    cand: dict[str, list[Any]] = {}
    back: dict[str, list[Any]] = defaultdict(list)
    for w in outs:
        for d in ins:
            if w.from_asset == d.to_asset and w.from_qty == d.to_qty and w.from_account != d.to_account \
                    and abs(d.ts - w.ts) <= MARGIN_WINDOW:
                cand.setdefault(w.tx_id, []).append(d)
                back[d.tx_id].append(w)
    pairs = []
    for w in outs:
        ds = cand.get(w.tx_id, [])
        if len(ds) == 1 and len(back[ds[0].tx_id]) == 1:
            w_, d_ = w, ds[0]
            if positions is None or (w_.from_account, w_.from_asset) in positions or \
                    (d_.to_account, d_.to_asset) in positions:
                pairs.append((w_, d_))
    return pairs


@dataclass
class Unsupported:
    batch_id: int
    filename: str
    reason: str
    count: int

    @property
    def key(self) -> str:
        return "un-" + hashlib.sha256(f"{self.batch_id}|{self.reason}".encode()).hexdigest()[:12]


def unsupported(db: Any) -> list[Unsupported]:
    """Von CSV-Profilen übersprungene Margin-/Derivatezeilen als konkrete offene Vorgänge (nicht übernommen, nicht
    erledigt). Rein lesend."""
    out: list[Unsupported] = []
    try:
        rows = db.q("SELECT id, filename, summary_json, status FROM csv_batch WHERE status!='deleted' ORDER BY id")
    except Exception:  # Datenbank ohne CSV-Modul
        return out
    for r in rows:
        try:
            sk = (json.loads(r["summary_json"] or "{}").get("skipped") or {})
        except ValueError:
            continue
        for reason, n in sorted(sk.items()):
            low = str(reason).lower()
            if int(n or 0) > 0 and ("derivat" in low or "margin" in low or "futures" in low) \
                    and "nicht unterstützt" in low:
                out.append(Unsupported(int(r["id"]), str(r["filename"] or ""), str(reason), int(n)))
    return out


def dossier(txs: list[Any], positions: set[tuple[str, str]]) -> list[str]:
    """Zeilen für das Dossier eines Falls: Derivatebuchungen der Positionen, Ergebnis getrennt, offene Fragen."""
    ev = events(txs, positions)
    if not ev:
        return []
    by_tag: dict[str, list[DEvent]] = defaultdict(list)
    for e in ev:
        by_tag[e.tag].append(e)
    lines = [f"Derivatebuchungen auf den Positionen: {len(ev)} (" + ", ".join(
        f"{ROLE[t][0]} {len(v)}×" for t, v in sorted(by_tag.items())) + ")."]
    for r in economics(txs, positions):
        eur = "unbekannt (Beträge fehlen)" if r.result_eur is None else f"{r.result_eur:,.2f} €"
        lines.append(f"Wirtschaftliches Derivateergebnis {r.year} · {r.account} · {r.asset}: {r.result_qty:+f} "
                     f"{r.asset} ({eur}); Margin-Umbuchungen {r.margin_qty:+f} {r.asset} zählen nicht dazu.")
    lines.append("Ob ein ausgewiesenes Ergebnis Gebühren/Funding bereits enthält, ist für diesen Anbieter nicht "
                 "verifiziert – mögliche Doppelerfassung bleibt offen. Steuerliche Behandlung: nicht implementiert.")
    return lines
