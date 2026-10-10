"""Empfehlungen je Befund: was Portfolia rät, was vorher zu prüfen ist und welche Lösungen zur Wahl stehen.

Eine Empfehlung ist nie eine Aktion. Jede Lösung (``Option``) wird erst nach einer Vorschau mit berechneten
Auswirkungen und ausdrücklicher Bestätigung übernommen (:mod:`app.diagnosis.actions`). Neben der empfohlenen Lösung
stehen immer Alternativen bereit – mindestens „als geprüft markieren“ und, wo Buchungen betroffen sind, eine eigene
Auswahl auszublendender Buchungen sowie Links zum Bearbeiten im Journal.

Explorer-Links öffnet ausschließlich der Nutzer; Portfolia ruft sie nie ab. Sie enthalten nur den Transaktions-Hash
bzw. die Contract-Adresse – keine Mengen, Werte oder Kontonamen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode

from app.diagnosis.model import Finding
from app.ledger.models import Tx
from app.util.timeutil import today_local
from app.web.fmt import qty_exact, token_link

EXPLORER_TX = {"ethereum": ("Etherscan", "https://etherscan.io/tx/0x{}"),
               "bsc": ("BscScan", "https://bscscan.com/tx/0x{}"),
               "avalanche": ("Snowtrace", "https://snowtrace.io/tx/0x{}"),
               "bitcoin": ("mempool.space", "https://mempool.space/tx/{}"),
               "kaspa": ("Kaspa Explorer", "https://explorer.kaspa.org/txs/{}"),
               "solana": ("Solscan", "https://solscan.io/tx/{}")}
_EVM = ("ethereum", "bsc", "avalanche")
_NATIVE = {"ETH": "ethereum", "BNB": "bsc", "AVAX": "avalanche", "BTC": "bitcoin", "KAS": "kaspa", "SOL": "solana"}
_CHAIN_TAG = {"ETH": "ethereum", "BSC": "bsc", "AVAX": "avalanche", "SOL": "solana", "KAS": "kaspa"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_CHECK_LINKS = 12
DEPOSIT_TAGS = (("", "Zugang ohne Ertrag (Einstand = EUR-Wert)"), ("reward", "Belohnung (Ertrag)"),
                ("staking", "Staking-Ertrag"), ("airdrop", "Airdrop (Ertrag)"), ("other_income", "sonstiger Ertrag"))
WITHDRAWAL_TAGS = (("fee", "Gebühr (Veräußerung zum EUR-Wert)"), ("lost", "Verlust (Veräußerung zum EUR-Wert)"),
                   ("", "Abgang ohne Gegenbuchung"))


@dataclass
class Param:
    """Eingabe einer Lösung (Formularfeld ``p_<name>``)."""

    name: str
    label: str
    kind: str  # multi | select | text | decimal | date
    default: list[str] = field(default_factory=list)
    choices: list[tuple[str, str]] = field(default_factory=list)  # (Wert, Bezeichnung)
    hint: str = ""


@dataclass
class Option:
    key: str
    label: str
    summary: str  # was passiert (vor der Vorschau)
    recommended: bool = False
    params: list[Param] = field(default_factory=list)
    caution: str = ""
    dismiss: bool = False  # „als geprüft markieren“ – ändert keine Daten


@dataclass
class Link:
    label: str
    url: str
    external: bool = False


@dataclass
class Recommendation:
    text: str
    conditional: bool = False  # Empfehlung gilt erst nach der genannten Prüfung
    checks: list[tuple[str, list[Link]]] = field(default_factory=list)  # vorher prüfen (mit Explorer-Links)
    options: list[Option] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    def option(self, key: str) -> Option | None:
        return next((o for o in self.options if o.key == key), None)

    @property
    def primary(self) -> Option | None:
        return next((o for o in self.options if o.recommended and not o.dismiss), None)

    @property
    def fixes(self) -> list[Option]:
        return [o for o in self.options if not o.dismiss]


# ----------------------------------------------------------------------------------------------------
# Helfer
# ----------------------------------------------------------------------------------------------------

def _q(v: Any) -> str:
    try:
        return qty_exact(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return str(v)


def _date(t: Tx) -> str:
    return t.ts.astimezone(UTC).strftime("%d.%m.%Y")


def _what(t: Tx) -> str:
    if t.to_asset and t.to_qty and t.from_asset and t.from_qty:
        return f"{_q(t.from_qty)} {t.from_asset} → {_q(t.to_qty)} {t.to_asset}"
    if t.to_asset and t.to_qty:
        return f"+{_q(t.to_qty)} {t.to_asset}" + (f" auf {t.to_account}" if t.to_account else "")
    if t.from_asset and t.from_qty:
        return f"−{_q(t.from_qty)} {t.from_asset}" + (f" von {t.from_account}" if t.from_account else "")
    return t.type


def tx_short(t: Tx) -> str:
    return f"{t.tx_id} ({_date(t)}, {_what(t)})"


def origin_text(t: Tx) -> str:
    return "Import-Buchung" if t.origin == "import" else "App-Buchung" if t.origin == "journal" else "Sparplan-Buchung"


def hide_text(t: Tx) -> str:
    if t.origin == "import":
        return (f"Import-Buchung {t.tx_id} zählt nicht mehr (Überlagerung „gelöscht“ – die Import-Datei bleibt "
                "unverändert und ein späterer Import hebt das nicht auf)")
    return f"App-Buchung {t.tx_id} zählt nicht mehr (Status „gelöscht“, wiederherstellbar)"


class Facts:
    """Nachschlagen im Schnappschuss der Diagnose (Buchungen, Herkunft, Datenquellen, Token-Schlüssel)."""

    def __init__(self, report: Any) -> None:
        self.snap = report.snapshot
        pf = self.snap.pf if self.snap is not None else None
        self.by_id: dict[str, Tx] = {t.tx_id: t for t in pf.txs} if pf is not None else {}
        self.pf = pf
        self.report = report

    def closed_pairs(self, f: Finding) -> set[str]:
        """Paare von Einzelvorgängen, die der Nutzer abgelehnt bzw. als ungeklärt belassen hat (nicht in die
        Sammelbearbeitung)."""
        out: set[str] = set()
        for cid in f.children:
            c = self.report.by_id(cid) if self.report is not None else None
            if c is not None and c.state in ("abgelehnt", "ungeklaert"):
                out.update("|".join(p) for p in (c.data or {}).get("pairs") or [])
        return out

    def tx(self, tx_id: str) -> Tx | None:
        return self.by_id.get(tx_id)

    def hideable(self, t: Tx) -> bool:
        """Buchung lässt sich über die Diagnose ausblenden (Import-Überlagerung bzw. Status einer App-Buchung)."""
        if t.origin == "import":
            return True
        if t.origin != "journal" or self.snap is None:
            return False
        m = self.snap.journal.get(t.tx_id)
        return m is not None and m.status == "active" and m.source not in ("transfer", "diagnose")

    def providers(self, t: Tx) -> list[str]:
        """Blockchain der Buchung für Explorer-Links: Datenquelle des Kontos, sonst natives Asset bzw. Token-Chain."""
        if self.snap is None:
            return []
        accs = {a for a in (t.from_account, t.to_account) if a}
        out = [s.provider for s in self.snap.sources if s.account in accs and s.provider in EXPLORER_TX]
        if out:
            return list(dict.fromkeys(out))
        for aid in (t.to_asset, t.from_asset, t.fee_asset):
            if not aid:
                continue
            sym = self.pf.asset(aid).symbol.upper() if self.pf is not None else aid.upper()
            if sym in _NATIVE:
                return [_NATIVE[sym]]
            for key in self.snap.token_keys.get(aid, []):
                tag = key.split("@", 1)[1].split(":", 1)[0].upper() if "@" in key else ""
                if tag in _CHAIN_TAG:
                    return [_CHAIN_TAG[tag]]
        return []


def explorer_links(h: str, providers: list[str], texts: list[str | None]) -> list[Link]:
    """Links zur Transaktion im Explorer (öffnet der Nutzer; Portfolia ruft sie nie ab)."""
    if _HEX64.match(h):
        provs = [p for p in providers if p != "solana"]  # Chain des Kontos bekannt (Datenquelle, Asset, Contract)
        if not provs:  # sonst nach Schreibweise: 0x… = EVM-Chain, ohne Präfix = Bitcoin bzw. Kaspa
            provs = list(_EVM) if any(f"0x{h}" in (x or "").lower() for x in texts) else ["bitcoin", "kaspa"]
        return [Link(EXPLORER_TX[p][0], EXPLORER_TX[p][1].format(h), True) for p in provs]
    if 80 <= len(h) <= 90:  # Solana-Signatur: Groß-/Kleinschreibung aus dem Originaltext
        for x in texts:
            m = re.search(re.escape(h), x or "", re.I)
            if m:
                return [Link("Solscan", EXPLORER_TX["solana"][1].format(m.group(0)), True)]
    return []


def _hash_checks(facts: Facts, txs: list[Tx], text: str) -> list[tuple[str, list[Link]]]:
    from app.csvimport.events import normalize_hash
    from app.csvimport.reconcile import hashes_in

    out: list[tuple[str, list[Link]]] = []
    seen: set[str] = set()
    for t in txs:
        hs = sorted(hashes_in(t.note, t.source_ref))
        m = facts.snap.journal.get(t.tx_id) if facts.snap is not None else None
        if m is not None and m.tx_hash:
            h = normalize_hash(m.tx_hash)
            if h and h not in hs:
                hs.append(h)
        for h in hs:
            if h in seen or len(out) >= MAX_CHECK_LINKS:
                continue
            seen.add(h)
            short = h if len(h) <= 18 else f"{h[:10]}…{h[-6:]}"
            out.append((f"{text} – Hash {short}", explorer_links(h, facts.providers(t), [t.note, t.source_ref])))
    return out


def _custom_hide(facts: Facts, f: Finding) -> Option | None:
    """Eigene Lösung: beliebige der betroffenen Buchungen ausblenden (nichts vorausgewählt)."""
    txs = [t for r in f.txs if (t := facts.tx(r.tx_id)) is not None and facts.hideable(t)]
    for a, b, _why in f.pairs:
        for r in (a, b):
            t = facts.tx(r.tx_id)
            if t is not None and facts.hideable(t) and t not in txs:
                txs.append(t)
    if not txs:
        return None
    return Option("hide_custom", "Eigene Auswahl: Buchungen ausblenden",
                  "Du wählst selbst, welche der betroffenen Buchungen nicht mehr zählen sollen. Import-Buchungen "
                  "werden als Überlagerung ausgeblendet, App-Buchungen gelöscht (wiederherstellbar).",
                  params=[Param("txs", "Auszublendende Buchungen", "multi",
                                choices=[(t.tx_id, f"{tx_short(t)} – {origin_text(t)}") for t in txs[:80]])])


def _dismiss(label: str = "Als geprüft markieren – kein Handlungsbedarf") -> Option:
    return Option("dismiss", label, "Ändert keine Daten. Der Befund wandert in „Als geprüft markiert“ und kommt "
                                    "zurück, sobald sich seine Daten ändern.", dismiss=True)


def _journal_links(account: str | None, asset: str | None) -> list[Link]:
    q = {k: v for k, v in (("account", account), ("asset", asset)) if v}
    return [Link(f"Buchungen{' von ' + account if account else ''}{' · ' + asset if asset else ''} anzeigen",
                 "/journal?" + urlencode(q))] if q else []


# ----------------------------------------------------------------------------------------------------
# Empfehlungen je Befundtyp
# ----------------------------------------------------------------------------------------------------

def facts_for(report: Any) -> Facts:
    """Nachschlage-Index je Bericht nur einmal aufbauen (die Seite fragt Empfehlungen für alle Befunde ab)."""
    facts = getattr(report, "_facts", None)
    if facts is None:
        facts = Facts(report)
        report._facts = facts
    return facts


def recommend(report: Any, f: Finding) -> Recommendation:
    facts = facts_for(report)
    typ = f.data.get("type") if f.data else None
    fn = _BUILDERS.get(typ or "")
    rec = fn(facts, f) if fn is not None and facts.pf is not None else None
    if rec is None:
        rec = Recommendation(text=f.decision or "Keine Korrektur vorgesehen – zur Einordnung.", conditional=True)
    keys = {o.key for o in rec.options}
    if "hide_custom" not in keys and f.kind in ("duplicate", "transfer", "history", "migration") \
            and (f.data or {}).get("type") not in ("econ_pairs", "negative"):
        custom = _custom_hide(facts, f)
        if custom is not None:
            rec.options.append(custom)
    if "dismiss" not in keys:
        rec.options.append(_dismiss())
    rec.options.sort(key=lambda o: (o.dismiss, not o.recommended))  # Empfehlung, Alternativen, „geprüft“ zuletzt
    return rec


def _same_qty(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    weak, strong = facts.tx(d["weak"]), facts.tx(d["strong"])
    if weak is None or strong is None:
        return None
    q = weak.to_qty or weak.from_qty
    likely = f.status == "wahrscheinlich"
    text = (f"{weak.tx_id} ausblenden. Die manuell erfasste Buchung bildet sehr wahrscheinlich denselben Vorgang ab "
            f"wie {strong.tx_id}, der durch einen Transaktions-Hash belegt ist. Vorher im Explorer bzw. beim Anbieter "
            f"prüfen, ob {_q(q)} {d['asset']} nur einmal auf {d['account']} gebucht wurden." if likely else
            f"Erst prüfen, ob {_q(q)} {d['asset']} einmal oder zweimal auf {d['account']} gebucht wurden. Nur wenn "
            f"einmal: {weak.tx_id} ausblenden (die schwächer belegte Buchung). Sonst als geprüft markieren.")
    opts = []
    if facts.hideable(weak):
        opts.append(Option("hide_weak", f"{weak.tx_id} ausblenden", hide_text(weak) + f"; {strong.tx_id} bleibt.",
                           recommended=True))
    if facts.hideable(strong):
        opts.append(Option("hide_strong", f"Stattdessen {strong.tx_id} ausblenden",
                           hide_text(strong) + f"; {weak.tx_id} bleibt. Sinnvoll, wenn die manuelle Buchung die "
                                               "genauere ist."))
    opts.append(_dismiss("Beide Buchungen sind richtig – als geprüft markieren"))
    checks = _hash_checks(facts, [strong, weak], f"Explorer: Vorgang {strong.tx_id} ansehen")
    if not checks:
        checks = [(f"Kontoauszug bzw. Transaktionsliste von {d['account']} prüfen (kein Hash vorhanden)", [])]
    return Recommendation(text=text, conditional=not likely, checks=checks, options=opts,
                          links=_journal_links(d["account"], d["asset"]))


def pair_choice(facts: Facts, first: Tx, second: Tx) -> tuple[str, Tx, Tx]:
    """Je Hash-Paar: was entfällt? App-Buchung neben Import-Buchung → „im Import enthalten“ (Import hat Vorrang),
    sonst die zweite Buchung (gleiche Angaben, spätere Kennung)."""
    if first.origin == "journal" and second.origin == "import":
        return "cover", first, second
    if first.origin == "import" and second.origin == "journal":
        return "cover", second, first
    return "hide", second, first


def _hash_pairs(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    choices = []
    defaults = []
    txs = []
    for (a_id, b_id), h in zip(d["pairs"], d["hashes"], strict=True):
        a, b = facts.tx(a_id), facts.tx(b_id)
        if a is None or b is None:
            continue
        how, drop, keep = pair_choice(facts, a, b)
        if (how == "hide" and not facts.hideable(drop)) or f"{a_id}|{b_id}" in facts.closed_pairs(f):
            continue
        txs.append(a)
        val = f"{a_id}|{b_id}"
        short = h if len(h) <= 18 else f"{h[:10]}…{h[-6:]}"
        act = (f"{drop.tx_id} als „im Import enthalten“ markieren" if how == "cover" else
               f"{drop.tx_id} ausblenden")
        choices.append((val, f"{_date(a)} · {_what(a)} · Hash {short}: {act}, {keep.tx_id} bleibt"))
        defaults.append(val)
    if not choices:
        return None
    single = len(d["pairs"]) == 1
    likely = f.status == "wahrscheinlich" and (single or not f.children)
    text = ("Je Paar die zweite Buchung ausblenden: beide tragen denselben Ereignisindex – es ist dieselbe Bewegung."
            if likely else
            "Je Hash im Explorer nachsehen, ob die Transaktion eine oder zwei gleiche Bewegungen an dieses Konto "
            "enthält. Nur bei einer: je Paar die zweite Buchung ausblenden – die Auswahl ist je Paar möglich. Sind es "
            "zwei Bewegungen, als geprüft markieren.")
    opts = [Option("hide_second", ("Die zweite Buchung ausblenden" if single else
                                   "Sammelbearbeitung: je ausgewähltem Paar die zweite Buchung ausblenden"),
                   "Die erste Buchung je Paar bleibt; die zweite zählt nicht mehr (Import-Buchung: Überlagerung "
                   "„gelöscht“; App-Buchung neben einer Import-Buchung: „im Import enthalten“).",
                   recommended=single or not f.children,
                   params=[] if single else [Param("pairs", "Paare", "multi",
                                                   default=[] if f.children else defaults, choices=choices,
                                                   hint="Nur Paare auswählen, die im Explorer nur eine Bewegung "
                                                        "zeigen.")]),
            _dismiss("Zwei legitime Bewegungen je Hash – als geprüft markieren")]
    return Recommendation(text=text + _sammel_hint(f), conditional=not likely,
                          checks=_hash_checks(facts, txs, "Explorer: Bewegungen der Transaktion zählen"),
                          options=opts, links=_journal_links(d["accounts"][0] if d.get("accounts") else None, None))


def _identical(facts: Facts, f: Finding) -> Recommendation | None:
    """Vollständig gleiche Buchungen: bevorzugt je Paar die spätere Kennung ausblenden (die erste behält Anschaffungs-
    datum und Einstand) – aber erst nach Prüfung des Kontoauszugs (Verdacht, keine Kennung entscheidet)."""
    choices, defaults, txs = [], [], []
    for a_id, b_id in f.data["pairs"]:
        a, b = facts.tx(a_id), facts.tx(b_id)
        if a is None or b is None:
            continue
        how, drop, keep = pair_choice(facts, a, b)
        if how == "hide" and not facts.hideable(drop):
            continue
        txs.append(a)
        val = f"{a_id}|{b_id}"
        act = f"{drop.tx_id} als „im Import enthalten“ markieren" if how == "cover" else f"{drop.tx_id} ausblenden"
        choices.append((val, f"{_date(a)} · {_what(a)}: {act}, {keep.tx_id} bleibt"))
        defaults.append(val)
    if not choices:
        return None
    text = ("Im Kontoauszug bzw. in der Transaktionsliste der Quelle prüfen, ob der Vorgang einmal oder mehrfach "
            "stattfand. Nur bei einem Vorgang: je Paar die zusätzliche Buchung ausblenden – die erste bleibt mit "
            "Anschaffungsdatum und Einstand unverändert. Fanden mehrere gleiche Vorgänge statt: als unabhängig "
            "bestätigen.")
    opts = [Option("hide_second", "Je Paar die zusätzliche Buchung ausblenden",
                   "Die erste Buchung je Paar bleibt; die zusätzliche zählt nicht mehr (Import-Buchung: Überlagerung "
                   "„gelöscht“, rückgängig machbar).", recommended=True,
                   params=[Param("pairs", "Paare", "multi", default=defaults, choices=choices,
                                 hint="Nur Paare auswählen, die laut Kontoauszug ein einziger Vorgang sind.")]),
            _dismiss("Mehrere gleiche Vorgänge – als geprüft markieren")]
    accounts = f.data.get("accounts") or []
    return Recommendation(text=text, conditional=True,
                          checks=[(f"Transaktionsliste von {', '.join(accounts)} prüfen", [])], options=opts,
                          links=_journal_links(accounts[0] if accounts else None, None))


def _import_vs_app(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    imps = [t for x in d["imports"] if (t := facts.tx(x)) is not None]
    jrns = [t for x in d["journals"] if (t := facts.tx(x)) is not None]
    if not imps or not jrns:
        return None
    rough = bool(d.get("rough"))
    side = d.get("side") or {}
    j_ids = ", ".join(t.tx_id for t in jrns)
    i_ids = ", ".join(t.tx_id for t in imps)
    if side:  # Seite eines Import-Transfers (verzögert bzw. anderer Kontoname)
        acc_j = (jrns[0].to_account if side.get("role") == "in" else jrns[0].from_account) or ""
        text = (f"Ist {j_ids} die {'Gutschrift' if side.get('role') == 'in' else 'Auszahlung'} des Transfers {i_ids}, "
                f"als „im Import enthalten“ markieren: Der Transfer zählt (Einstand und Haltedauer wandern mit), die "
                "App-Buchung nicht mehr – sie bleibt mit Herkunft erhalten."
                + ("" if side.get("same", True) else
                   f" Anschließend die Konten angleichen: „{acc_j}“ und „{side.get('account')}“ bezeichnen dann "
                   "dasselbe Wallet – sonst laufen spätere Bewegungen der Datenquelle weiter auf „"
                   f"{acc_j}“, während der Bestand des Transfers auf „{side.get('account')}“ liegt."))
    else:
        text = (f"Erst prüfen, ob {j_ids} denselben Vorgang abbildet wie {i_ids} (Datum, Menge, Gegenkonto). Nur "
                "dann als „im Import enthalten“ markieren; sonst als geprüft markieren." if rough else
                f"{j_ids} als „im Import enthalten“ markieren. Die App-Buchung zählt dann nicht mehr, bleibt aber mit "
                "Herkunft erhalten – und zählt automatisch wieder, falls ein späterer Import den Vorgang nicht mehr "
                "enthält. So bleibt der kuratierte Import maßgeblich.")
    opts = [Option("cover", f"{j_ids} als „im Import enthalten“ markieren",
                   f"Verknüpfung App-Buchung ↔ Import-Buchung {imps[0].tx_id} (wie unter Journal → Abgleich); die "
                   "App-Buchung zählt nicht mehr.", recommended=True)]
    # einen Import-Transfer auszublenden entfernte auch dessen andere Seite (z. B. die Auszahlung der Börse)
    if all(t.origin == "import" for t in imps) and not side:
        opts.append(Option("hide_import", f"Stattdessen {i_ids} ausblenden – die App-Buchung gilt",
                           "; ".join(hide_text(t) for t in imps) + ". Sinnvoll, wenn die App-Buchung genauer ist "
                                                                    "(z. B. Gebühren, Uhrzeit)."))
    opts.append(_dismiss("Verschiedene Vorgänge – als geprüft markieren"))
    hashes = d.get("hashes") or []
    checks = _hash_checks(facts, [*imps, *jrns], "Explorer: Vorgang ansehen") if hashes else []
    acc = jrns[0].to_account or jrns[0].from_account
    if side and not side.get("same", True):
        checks.append((f"Adresse bzw. Kontoauszug: sind „{acc}“ und „{side.get('account')}“ dasselbe Wallet?", []))
    return Recommendation(text=text, conditional=rough or bool(side and not side.get("same", True)), checks=checks,
                          options=opts, links=[Link("Journal → Abgleich", "/journal/abgleich"),
                                               *_journal_links(acc, None)])


def _transfer(facts: Facts, f: Finding) -> Recommendation | None:
    choices, defaults, txs = [], [], []
    closed = facts.closed_pairs(f)
    for w_id, d_id in f.data["pairs"]:
        w, d = facts.tx(w_id), facts.tx(d_id)
        if w is None or d is None or not facts.hideable(w) or not facts.hideable(d) or f"{w_id}|{d_id}" in closed:
            continue
        txs += [w, d]
        val = f"{w_id}|{d_id}"
        choices.append((val, f"{_date(w)}: −{_q(w.from_qty)} {w.from_asset} ({w.from_account}, {w_id}) → "
                             f"+{_q(d.to_qty)} {d.to_asset} ({d.to_account}, {d_id})"))
        defaults.append(val)
    if not choices:
        return None
    single = len(f.data["pairs"]) == 1
    likely = f.status == "wahrscheinlich" and (single or not f.children)
    text = ("Als internen Transfer verbuchen: Der gleiche Transaktions-Hash belegt dieselbe Blockchain-Transaktion. "
            "Einstand und Anschaffungsdatum wandern dann vom Abgangskonto mit; der Zugang ist keine neue Anschaffung "
            "und die Haltefrist läuft weiter." if likely else
            "Nur wenn beide Konten dir gehören und es derselbe Vorgang ist: als internen Transfer verbuchen (Einstand "
            "und Anschaffungsdatum wandern mit). Ist es ein Abgang an Dritte bzw. ein Zugang von Dritten, als "
            "geprüft markieren.")
    opts = [Option("link", "Als internen Transfer verbuchen" if single or not f.children else
                   "Sammelbearbeitung: ausgewählte Paare als Transfer verbuchen",
                   "Je Paar entsteht eine Transfer-Buchung (App, „Korrektur aus der Diagnose“); Abgang und Zugang "
                   "zählen nicht mehr einzeln (Import-Buchungen: Überlagerung, App-Buchungen: zusammengeführt). Eine "
                   "Mengendifferenz gilt als Transfergebühr.", recommended=single or not f.children,
                   params=[] if single else [Param("pairs", "Paare", "multi", default=[] if f.children else defaults,
                                                   choices=choices)],
                   caution="Ändert Einstand, Haltedauer und damit realisierte Ergebnisse späterer Verkäufe – die "
                           "Vorschau zeigt die Steuerwerte je Jahr."),
            _dismiss("Kein interner Transfer – als geprüft markieren")]
    return Recommendation(text=text + _sammel_hint(f), conditional=not likely,
                          checks=_hash_checks(facts, txs, "Explorer: Absender und Empfänger prüfen")
                          or [("Kontoauszüge beider Konten vergleichen (kein Hash vorhanden)", [])],
                          options=opts)


def _coin_param(default: str = "") -> Param:
    return Param("coin", "CoinGecko-ID oder Link", "text", default=[default] if default else [],
                 hint="z. B. „threshold-network-token“ oder https://www.coingecko.com/de/munze/…")


def _cg_link(coin: str) -> Link:
    return Link(f"CoinGecko: {coin}", f"https://www.coingecko.com/en/coins/{quote(coin, safe='')}", True)


def _provider_quote(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    aid, coin = d["asset"], d["coin"]
    from app.csvimport.identity import PROVIDER_LABEL, identity

    prov = PROVIDER_LABEL.get(d["provider"], d["provider"])
    a = facts.pf.asset(aid)
    pa = identity(d["provider"], a.symbol)
    others = sorted({acc for acc, _x in f.positions if _x == aid and d["provider"] not in acc.lower()})
    caution = (f"{aid} wird auch auf {', '.join(others)} geführt – dort könnte das Kürzel einen anderen Coin "
               "bezeichnen. Dann statt der Kursquelle ein eigenes Asset anlegen (Journal)." if others else "")
    text = (f"Kursquelle von {aid} auf CoinGecko „{coin}“ ({d['coin_name']}) setzen: {prov} führt "
            f"{pa.symbol if pa else a.symbol} als {d['coin_name']}" +
            (f"; die bisherige Zuordnung „{d['current']}“ bewertet einen anderen Coin." if d.get("current") else "."))
    opts = [Option("set_quote", f"Kursquelle auf „{coin}“ setzen",
                   f"{aid} wird künftig mit dem Kurs von {d['coin_name']} bewertet (Kursabruf nach dem Übernehmen). "
                   "Buchungen, Mengen und Einstand bleiben unverändert.", recommended=not others, caution=caution),
            Option("set_quote_custom", "Andere CoinGecko-ID setzen",
                   "Kursquelle von " + aid + " auf eine selbst gewählte CoinGecko-ID setzen.", params=[_coin_param()]),
            _dismiss("Zuordnung ist richtig – als geprüft markieren")]
    return Recommendation(text=text, conditional=bool(others), options=opts,
                          checks=[("Coin auf CoinGecko ansehen", [_cg_link(coin)])],
                          links=[Link("Kursquellen", f"/quality/sources#a-{quote(aid, safe='')}")])


def _contracts(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    saved = facts.snap.saved_symbols if facts.snap is not None else {}
    keys = [k for k in d["keys"] if saved.get(k.upper()) == d["asset"] or saved.get(k) == d["asset"]]
    checks = [(f"Contract {k.split(':', 1)[1] if ':' in k else k}", [Link("Explorer", url, True)] if (
        url := token_link(k)) else []) for k in d["keys"]]
    opts = []
    if keys:
        opts.append(Option("unmap", "Falsche Zuordnung entfernen",
                           f"Die gewählte Token-Zuordnung zu {d['asset']} wird gelöscht. Das wirkt nur auf künftige "
                           "Importe und Abrufe (der Token geht dann in die Prüfung); bestehende Buchungen bleiben.",
                           params=[Param("symbol", "Zuordnung", "select", default=[keys[-1]],
                                         choices=[(k, k) for k in keys])]))
    opts.append(_dismiss(f"Alle Contracts gehören zu {d['asset']} (z. B. Migration) – als geprüft markieren"))
    return Recommendation(
        text=f"Im Explorer prüfen, welcher Contract zum Asset {d['asset']} gehört, und die falsche Zuordnung "
             "entfernen. Ohne diese Prüfung bitte nichts ändern.",
        conditional=True, checks=checks, options=opts,
        links=[Link("Gespeicherte Zuordnungen (CSV-Import)", "/journal/csv#zuordnungen")])


def _unvalued(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    aid, sug = d["asset"], d.get("suggestion")
    conf = d.get("suggestion_confidence") or "–"
    a = facts.pf.asset(aid)
    opts = []
    if sug and a.is_crypto:
        opts.append(Option("accept_suggestion", f"Vorschlag „{sug}“ übernehmen",
                           f"{aid} wird künftig mit dem Kurs von CoinGecko „{sug}“ bewertet (Sicherheit {conf}).",
                           recommended=conf in ("hoch", "mittel")))
    if a.is_crypto:
        opts.append(Option("set_quote_custom", "Eigene CoinGecko-ID setzen",
                           f"Kursquelle von {aid} auf eine selbst gewählte CoinGecko-ID setzen.",
                           params=[_coin_param()]))
    opts.append(_dismiss("Ohne Kurs lassen (z. B. wertlos) – als geprüft markieren"))
    text = (f"Vorschlag CoinGecko „{sug}“ übernehmen (Sicherheit {conf}) – vorher auf CoinGecko prüfen, ob Name und "
            "Chain passen." if sug else
            "Eine CoinGecko-ID zuordnen, falls der Token gehandelt wird. Wertlose Token (Spam, eingestellte Projekte) "
            "als geprüft markieren oder auf der eigenen Seite als Verlust ausbuchen.")
    acc = d["positions"][0][0] if d.get("positions") else ""
    links = [Link("Kursquellen", f"/quality/sources#a-{quote(aid, safe='')}"),
             Link("Als Verlust ausbuchen (eigene Seite)", "/journal/writeoff?" + urlencode(
                 {"asset": aid, **({"account": acc} if acc else {})}))]
    return Recommendation(text=text, conditional=not (sug and conf in ("hoch", "mittel")), options=opts,
                          checks=[("Coin auf CoinGecko ansehen", [_cg_link(sug)])] if sug else [], links=links)


def _price_fallback(facts: Facts, f: Finding) -> Recommendation | None:
    aid = f.data["asset"]
    a = facts.pf.asset(aid)
    opts = []
    if a.is_crypto:
        opts.append(Option("set_quote_custom", "CoinGecko-ID setzen",
                           f"Kursquelle von {aid} auf eine CoinGecko-ID setzen (Marktkurs statt Ersatzkurs).",
                           params=[_coin_param()]))
    opts.append(_dismiss("Ersatzkurs ist ausreichend – als geprüft markieren"))
    return Recommendation(
        text="Eine Kursquelle zuordnen, damit ein Marktkurs abgerufen wird – oder den manuellen Kurs im kuratierten "
             "Import aktuell halten." if a.is_crypto else
             "Kursquelle des Wertpapiers im kuratierten Import prüfen (Yahoo-Symbol) bzw. den manuellen Kurs "
             "aktualisieren.",
        conditional=True, options=opts, links=[Link("Kursquellen", f"/quality/sources#a-{quote(aid, safe='')}")])


def _stale(facts: Facts, f: Finding) -> Recommendation | None:
    return Recommendation(text="Kursabruf prüfen (Datenqualität → Kursquellen und Protokoll). Eine Korrektur an "
                               "Buchungen ist nicht nötig.", options=[_dismiss()],
                          links=[Link("Datenqualität", "/quality")])


def balance_before(facts: Facts, account: str, asset: str, t: Tx) -> Decimal:
    """Bestand (Konto, Asset) unmittelbar vor der Buchung ``t`` (Reihenfolge wie im Ledger)."""
    q = Decimal(0)
    for x in sorted(facts.pf.txs, key=lambda y: (y.ts, y.seq, y.tx_id)):
        if (x.ts, x.seq, x.tx_id) >= (t.ts, t.seq, t.tx_id):
            break
        if x.from_account == account and x.from_asset == asset and x.from_qty:
            q -= x.from_qty
        if x.to_account == account and x.to_asset == asset and x.to_qty:
            q += x.to_qty
        if x.fee_asset == asset and x.fee_qty and (x.from_account or x.to_account) == account:
            q -= x.fee_qty
    return q


def _migration(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    acc, old, new = d["account"], d["old"], d["new"]
    r = facts.tx(d["receipt"]) if d.get("receipt") else None
    opts = []
    if r is not None and r.type == "deposit" and facts.hideable(r) and r.to_qty:
        q_old = balance_before(facts, acc, old, r)
        if q_old > 0:
            opts.append(Option(
                "book_migration", f"Migration buchen: {_q(q_old)} {old} → {_q(r.to_qty)} {new}",
                f"Kapitalmaßnahme „migration“ am {_date(r)} auf {acc}: Einstand und Anschaffungsdatum gehen von {old} "
                f"auf {new} über; der Zugang {r.tx_id} zählt nicht mehr (sonst stünde der Bestand doppelt).",
                caution="Nur buchen, wenn der neue Contract laut Projekt der offizielle Nachfolger ist."))
        opts.append(Option("hide_receipt", f"Zugang {r.tx_id} ausblenden ({new} als Spam/wertlos)",
                           hide_text(r) + f"; {old} bleibt unverändert."))
    opts.append(_dismiss("Zwei verschiedene Token – als geprüft markieren"))
    spam = d.get("spam")
    text = (f"Contract-Adressen beider Token beim Projekt bzw. im Explorer prüfen. Ist {new} der offizielle "
            f"Nachfolger von {old}: Migration buchen (der Bestand stünde sonst doppelt). Ist {new} ein Spam-Token"
            + (f" (laut Import: {spam} = Spam)" if spam else "") + ": den Zugang ausblenden oder so lassen.")
    checks = [(f"Contract {k}", [Link("Explorer", url, True)] if (url := token_link(k)) else [])
              for z in (old, new) for k in (facts.snap.token_keys.get(z, []) if facts.snap is not None else [])]
    return Recommendation(text=text, conditional=True, options=opts, checks=checks,
                          links=_journal_links(acc, new))


def _conversion_twin(facts: Facts, f: Finding) -> Recommendation | None:
    """Doppelt gebuchter Umtausch: die zusätzliche Buchung (Ausgangs-Asset ohne Bestand) nicht mehr zählen – neben
    einer Import-Buchung als „im Import enthalten“, sonst ausblenden."""
    d = f.data
    weak, strong = facts.tx(d["weak"]), facts.tx(d["strong"])
    if weak is None or strong is None:
        return None
    acc, new = d["account"], d["asset"]
    cover = weak.origin == "journal" and strong.origin == "import"
    opts = []
    if cover:
        opts.append(Option("cover_twin", f"{weak.tx_id} als „im Import enthalten“ markieren",
                           f"Verknüpfung mit {strong.tx_id} (wie unter Journal → Abgleich): die App-Buchung zählt "
                           "nicht mehr, bleibt aber mit Herkunft erhalten.", recommended=True))
    elif facts.hideable(weak):
        opts.append(Option("hide_weak", f"{weak.tx_id} ausblenden", hide_text(weak) + f"; {strong.tx_id} bleibt.",
                           recommended=True))
    if facts.hideable(strong):
        opts.append(Option("hide_strong", f"Stattdessen {strong.tx_id} ausblenden",
                           hide_text(strong) + f"; {weak.tx_id} bleibt."
                           + (f" Nur sinnvoll, wenn vorher Bestand {d['old_weak']} gebucht war – sonst bleibt der "
                              "Bestand negativ." if d.get("short") else "")))
    opts.append(_dismiss("Zwei Umtausche – als geprüft markieren"))
    rename = d["old_weak"] != d["old_strong"]
    text = (f"{weak.tx_id} {'als „im Import enthalten“ markieren' if cover else 'ausblenden'}: Der Umtausch ist in "
            f"{strong.tx_id} bereits gebucht, {new} zählt sonst doppelt."
            + (f" {d['old_weak']} hatte vor dem Umtausch keinen Bestand – {d['old_weak']} ist das Börsen-Symbol "
               f"des Altbestands, den der Import als {d['old_strong']} führt (Ticker-Umbenennung)." if rename and
               d.get("short") else "")
            + " Bucht der Import den Vorgang als Tausch, erscheint dazu der Befund „Umbenennung als Tausch gebucht“.")
    return Recommendation(text=text, conditional=f.status != "wahrscheinlich",
                          checks=[(f"Kontoauszug bzw. Transaktionsliste von {acc}: ein oder zwei Umtausche?", [])],
                          options=opts, links=[Link("Journal → Abgleich", "/journal/abgleich"),
                                               *_journal_links(acc, new)])


def _rename_trade(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    t = facts.tx(d["tx"])
    if t is None:
        return None
    opts = []
    if facts.hideable(t):
        opts.append(Option(
            "trade_to_migration", f"Als Kapitalmaßnahme (Migration) buchen: {d['old']} → {d['new']}",
            f"Neue Kapitalmaßnahme „migration“ am {_date(t)} auf {d['account']} mit denselben Mengen; Einstand und "
            f"Anschaffungsdaten gehen von {d['old']} auf {d['new']} über, der Tausch {t.tx_id} zählt nicht mehr "
            f"({origin_text(t)}).", recommended=f.status == "wahrscheinlich",
            caution="Steuerlich nur richtig, wenn es eine reine Umbenennung bzw. Redenominierung desselben Tokens war. "
                    "Steuerberichte eines externen Steuertools weichen danach ab."))
    opts.append(_dismiss("Echter Tausch – als geprüft markieren"))
    text = (f"Angaben der Börse bzw. des Projekts prüfen. War {d['new']} nur der neue Ticker bzw. die neue Einheit "
            f"von {d['old']}: als Kapitalmaßnahme (Migration) buchen – der Scheingewinn entfällt, Anschaffungsdaten "
            "und Haltedauer bleiben erhalten. War es ein Umtausch in einen neuen Token: als geprüft markieren.")
    return Recommendation(text=text, conditional=True,
                          checks=[("Mitteilung der Börse bzw. des Projekts zur Umstellung (Ticker, Verhältnis, "
                                   "Contract)", [])],
                          options=opts, links=_journal_links(d["account"], d["new"]))


def _holding(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    acc, aid, st = d["account"], d["asset"], d["status"]
    comp = Decimal(d["computed"])
    if acc == "(alle Konten)":
        return Recommendation(text="Soll und berechneten Bestand je Konto vergleichen (Bestandsabgleich unten); "
                                   "die Differenz lässt sich keinem Konto zuordnen.", conditional=True,
                              options=[_dismiss()], links=_journal_links(None, aid))
    target = Decimal(d["observed"]) if st in ("extern_diff", "extern_unsicher") and d.get("observed") else (
        Decimal(d["expected"]) if d.get("expected") else None)
    opts = []
    if target is not None and target != comp:
        diff = target - comp
        tags = DEPOSIT_TAGS if diff > 0 else WITHDRAWAL_TAGS
        ref = "beobachteten Bestand" if st.startswith("extern") else "Soll des Imports"
        opts.append(Option(
            "adjust", f"Ausgleichsbuchung über {_q(abs(diff))} {aid} ({'Zugang' if diff > 0 else 'Abgang'})",
            f"Neue App-Buchung, die den berechneten Bestand auf den {ref} ({_q(target)}) bringt. Notlösung: Sie "
            "verdeckt die Ursache.",
            params=[Param("tag", "Art", "select", default=[tags[0][0]], choices=list(tags)),
                    Param("value", "EUR-Wert", "decimal", default=["0"],
                          hint="Zugang: Einstand bzw. Ertrag; Abgang: Erlös (0 = Verlust in Höhe des Einstands)"),
                    Param("date", "Datum", "date", default=[today_local().isoformat()])],
            caution="Ändert Bestand, Einstand und ggf. realisierte Ergebnisse – vorher die Ursache suchen."
            + (" Der externe Bestand ist nicht bestätigt (Abruf veraltet oder unvollständig)."
               if st == "extern_unsicher" else "")))
    opts.append(_dismiss("Differenz ist erklärt – als geprüft markieren"))
    if st == "extern_diff":
        text = ("Ursache klären: fehlende Buchungen (Prüf-Stapel, nicht abgerufene Vorgänge), Netzwerkgebühren, "
                "falsche Zuordnung. Erst wenn die Ursache nicht zu finden ist, als Notlösung eine Ausgleichsbuchung.")
    elif st == "extern_unsicher":
        text = ("Datenquelle neu abrufen – erst ein aktueller, vollständiger Abruf bestätigt eine Differenz. Danach "
                "wie bei einer Differenz vorgehen.")
    else:
        text = ("Prüfen, welche Buchung im Import bzw. in der App fehlt oder doppelt ist (Buchungen des Kontos). Ein "
                "Ausgleich auf das Soll ist nur sinnvoll, wenn das Soll des Imports stimmt.")
    links = _journal_links(acc, aid)
    if st.startswith("extern"):
        links.append(Link("Datenquellen", "/settings/datasources"))
    return Recommendation(text=text, conditional=True, options=opts, links=links)


# ----------------------------------------------------------------------------------------------------
# Buchungsprüfung über Quellen (M27)
# ----------------------------------------------------------------------------------------------------

def _sammel_hint(f: Finding) -> str:
    return (f" Dieser Befund fasst {len(f.children)} Einzelvorgänge zusammen – jeden Vorgang einzeln prüfen und "
            "entscheiden; die Sammelbearbeitung übernimmt nur ausdrücklich ausgewählte Vorgänge.") \
        if len(f.children) > 1 else ""


def _econ_pairs(facts: Facts, f: Finding) -> Recommendation | None:
    from app.diagnosis.audit import family_label

    d = f.data
    lk, ld = family_label(d.get("keep_family") or ""), family_label(d.get("drop_family") or "")
    case = d.get("case") or {}
    closed = facts.closed_pairs(f)
    choices, keep_txs, drop_txs = [], [], []
    for keep_id, drop_id in d["pairs"]:
        k, x = facts.tx(keep_id), facts.tx(drop_id)
        if k is None or x is None or f"{keep_id}|{drop_id}" in closed:
            continue
        keep_txs.append(k)
        drop_txs.append(x)
        choices.append((f"{keep_id}|{drop_id}", f"{tx_short(k)} ({keep_id}) ↔ {tx_short(x)} ({drop_id})"))
    single = len(d["pairs"]) == 1
    ambiguous = bool(case.get("ambiguous") or d.get("ambiguous"))
    checks = [("Kontoauszug bzw. Transaktionshistorie der Börse: Ist jeder Betrag einmal oder zweimal enthalten?", [])]
    checks += _hash_checks(facts, [*keep_txs, *drop_txs], "Explorer: Vorgang ansehen")
    links = [*_journal_links(d.get("account"), d.get("asset")),
             Link("Referenzbestand hinterlegen", "/quality/diagnose#referenzen")]
    if not choices:
        text = ("Keine Verknüpfung möglich bzw. sinnvoll: "
                + ("mehrere gleich gute Kombinationen – die Entscheidung ist zurückgestellt. " if ambiguous else "")
                + "Mit Kontoauszug prüfen; sonst ablehnen (verschiedene Vorgänge) oder später prüfen.")
        return Recommendation(text=text, conditional=True, checks=checks,
                              options=[_dismiss("Ungeklärt lassen – als geprüft markieren")], links=links)
    strong = f.status in ("belegt", "wahrscheinlich") and single and not ambiguous
    hint = f.status == "hinweis"
    how = ("App-Buchungen werden als „im Import enthalten“ verknüpft, Import-Buchungen als Doppelbuchung der geltenden "
           "Buchung ausgeblendet (Überlagerung, die Import-Datei bleibt unverändert). Rohdaten und Herkunft bleiben "
           "erhalten; „Rückgängig“ stellt alles wieder her.")
    pparams = [] if single else [Param("pairs", "Vorgänge", "multi", default=[], choices=choices,
                                       hint="Nur Vorgänge wählen, die laut Kontoauszug je derselbe Vorgang sind.")]
    caution = ("Kein ausreichender Beleg für eine Doppelbuchung – nur mit Kontoauszug verknüpfen." if hint else
               "Mehrdeutig: mehrere mögliche Partner – nur mit Kontoauszug verknüpfen." if ambiguous else
               "Ändert Bestand und ggf. Einstand/realisierte Ergebnisse – die Vorschau zeigt die Werte je Jahr.")
    prefix = "" if single else "Sammelbearbeitung: ausgewählte Vorgänge – "
    opts = [Option("link_econ", f"{prefix}Als einen Vorgang verknüpfen – „{lk}“ bleibt maßgeblich",
                   f"Es zählt nur die Buchung aus „{lk}“; die aus „{ld}“ wird als bereits enthalten verknüpft. {how}",
                   recommended=strong, params=pparams, caution=caution),
            Option("link_econ_swap", f"{prefix}Als einen Vorgang verknüpfen – „{ld}“ bleibt maßgeblich",
                   f"Es zählt die Buchung aus „{ld}“ (z. B. weil sie Gebühr und genaue Uhrzeit enthält); die aus "
                   f"„{lk}“ wird als bereits enthalten verknüpft bzw. ausgeblendet. {how}",
                   params=[Param(p.name, p.label, p.kind, [], p.choices, p.hint) for p in pparams],
                   caution="Der kuratierte Import ist sonst maßgeblich – nur wählen, wenn die zweite Quelle genauer "
                           "ist."),
            _dismiss("Ungeklärt lassen – als geprüft markieren")]
    if f.status == "belegt" and single:
        text = (f"Identität belegt ({case.get('identity') or 'Kennung'}): als einen Vorgang verknüpfen – es zählt die "
                f"Buchung aus „{lk}“.")
    elif strong:
        text = (f"Möglicherweise derselbe Vorgang (starke Übereinstimmung, nicht bewiesen): Konto, Asset, Richtung und "
                f"Betrag (brutto/netto) passen, Abstand unter einer Stunde, je Buchung genau ein Partner. Nach Prüfung "
                f"des Kontoauszugs verknüpfen – es zählt dann nur die Buchung aus „{lk}“.")
    elif hint:
        text = ("Keine Korrektur empfohlen: Gleiche Höhe und ein Abstand von Tagen belegen keine Doppelbuchung. Mit "
                "dem Kontoauszug prüfen, ob der Betrag einmal oder zweimal eingegangen ist; dann verknüpfen oder "
                "ablehnen.")
    elif ambiguous:
        text = ("Mehrdeutig – Entscheidung zurückgestellt. Erst mit Kontoauszug klären, welche Buchungen "
                "zusammengehören.")
    else:
        text = ("Erst mit Kontoauszug bzw. Historie der Börse prüfen, ob der Vorgang einmal oder zweimal stattfand. "
                "Nur dann verknüpfen, sonst ablehnen.")
    return Recommendation(text=text + _sammel_hint(f), conditional=not (strong and f.status == "belegt"),
                          checks=checks, options=opts, links=links)


def _breakdown(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    return Recommendation(
        text="Die Zerlegung zeigt, welche Quelle wie viel zum Bestand beiträgt. Zuerst die Doppelbuchungen dieses "
             "Kontos klären, dann den Kontostand laut Auszug als Referenzbestand zum Stichtag hinterlegen – der "
             "Bestandsabgleich vergleicht dann Soll und Ist zum selben Datum.",
        conditional=True, options=[_dismiss("Zerlegung geprüft – als geprüft markieren")],
        links=[*_journal_links(d["account"], d["asset"]),
               Link("Mögliche Doppelbuchungen", "/quality/diagnose?kind=duplicate#findings"),
               Link("Referenzbestand hinterlegen", "/quality/diagnose#referenzen")])


def _negative(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    causes = d.get("causes") or []
    links = _journal_links(d["account"], d["asset"])
    if any("Sparplan" in c for c in causes):
        links.append(Link("Sparpläne", "/plans"))
    if any("doppelte Auszahlung" in c for c in causes):
        links.append(Link("Mögliche Doppelbuchungen", "/quality/diagnose?kind=duplicate#findings"))
        text = ("Die doppelte Auszahlung lösen (Befund „Mögliche Doppelbuchungen“) – danach ist der Bestand nicht mehr "
                "negativ. Keine Ausgleichsbuchung.")
    elif any("Sparplan" in c for c in causes):
        text = ("Fehlende Einzahlung zur Sparplan-Ausführung ergänzen (Kontoauszug, nächster Import bzw. Abruf) oder "
                "die Schätzung unter Sparpläne prüfen. Portfolia ergänzt keine Einzahlung automatisch.")
    elif any("Zwischenstand" in c for c in causes):
        text = "Kein Bestandsfehler: Reihenfolge bzw. Zeitstempel prüfen; ohne Handlungsbedarf als geprüft markieren."
    else:
        text = ("Fehlende Zugänge belegen (Export der Quelle, Explorer) und im kuratierten Import ergänzen bzw. die "
                "Datenquelle vollständig abrufen. Keine Ausgleichsbuchung nur zum Schließen der Lücke.")
    return Recommendation(text=text, conditional=True, options=[_dismiss("Ursache geklärt – als geprüft markieren")],
                          links=links)


def _loss(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    cls = d.get("class")
    if d.get("documented"):
        return Recommendation(text="Bereits als Verlust bzw. Ausbuchung gebucht – keine Korrektur nötig.",
                              options=[_dismiss("Dokumentiert – als geprüft markieren")])
    txs = [t for x in d.get("txs") or [] if (t := facts.tx(x)) is not None]
    checks = _hash_checks(facts, txs, "Explorer: Zieladresse prüfen") or [
        ("Kontoauszug bzw. Auszahlungshistorie: Zieladresse und Empfänger prüfen", [])]
    if cls == "C":
        text = ("Kompromittierung ist dokumentiert: Zieladressen im Explorer prüfen. Gehört das Ziel nicht dir, die "
                "Abgänge als Verlust/Diebstahl buchen (Journal: Abgang bearbeiten, Art „Verlust“); sonst das "
                "Zielkonto erfassen und als Transfer verknüpfen.")
    else:
        text = ("Ziel klären: eigenes, nicht erfasstes Konto → Konto bzw. Datenquelle erfassen, danach erscheint der "
                "Transfer-Vorschlag; Zahlung/Verkauf an Dritte → Buchung entsprechend einordnen; nur bei Beleg als "
                "Verlust buchen. Ohne Beleg als ungeklärt markieren (Notiz).")
    acc = d.get("account")
    return Recommendation(text=text, conditional=True, checks=checks,
                          options=[_dismiss("Als ungeklärt markieren (Prüfung dokumentiert)" if cls != "C" else
                                            "Geprüft – als geprüft markieren")],
                          links=[*_journal_links(acc, d.get("asset")), Link("Datenquellen", "/settings/datasources")])


def _inactive(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    return Recommendation(
        text="Konto beim Anbieter bzw. im Explorer prüfen. Besteht der Bestand, die Datenquelle (neu) verbinden oder "
             "den Kontostand als Referenzbestand hinterlegen. Inaktivität ist kein Verlust – eine Ausbuchung nur bei "
             "Beleg (Hack, Delisting, Insolvenz).",
        conditional=True, options=[_dismiss("Langfristige Verwahrung – als geprüft markieren")],
        links=[*_journal_links(d["account"], None), Link("Datenquellen", "/settings/datasources"),
               Link("Referenzbestand hinterlegen", "/quality/diagnose#referenzen")])


def _reference(facts: Facts, f: Finding) -> Recommendation | None:
    d = f.data
    return Recommendation(
        text="Die genannten Ursachen der Reihe nach klären (zuerst Doppelbuchungen). Stimmt danach Soll und "
             "Referenzbestand nicht überein, fehlende Buchungen anhand des Kontoauszugs ergänzen – eine "
             "Ausgleichsbuchung nur zum Schließen der Differenz bietet Portfolia hier nicht an.",
        conditional=True, options=[_dismiss("Differenz erklärt – als geprüft markieren")],
        links=[*_journal_links(d["account"], d["asset"]), Link("Referenzbestände", "/quality/diagnose#referenzen")])


_BUILDERS = {
    "same_qty": _same_qty, "hash_pairs": _hash_pairs, "identical": _identical, "import_vs_app": _import_vs_app,
    "transfer": _transfer,
    "provider_quote": _provider_quote, "contracts": _contracts, "unvalued": _unvalued,
    "price_fallback": _price_fallback, "stale": _stale, "migration": _migration, "holding": _holding,
    "conversion_twin": _conversion_twin, "rename_trade": _rename_trade,
    "econ_pairs": _econ_pairs, "breakdown": _breakdown, "negative": _negative, "loss": _loss, "inactive": _inactive,
    "reference": _reference,
}
