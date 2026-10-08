"""Formular-Vorlagen für manuelle Buchungen → Zeilen im Format von ``transactions.csv``.

Jede Vorlage übersetzt verständliche Eingaben (Kauf: Konto, Asset, Stück, Betrag, Gebühr …) in das Buchungsmodell
des Datenvertrags mit Abgangs-, Zugangs- und Gebührenbein. Fehlende EUR-Werte werden aus gespeicherten Kursen bzw.
Devisenkursen ergänzt; die Herkunft steht in ``value_sources``. Die fachliche Endprüfung übernimmt anschließend
derselbe Validator wie beim Import.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.importer import contract as C
from app.ledger.models import AssetInfo
from app.util.numbers import parse_number
from app.util.timeutil import fmt_de_date, local_tz

CENT = Decimal("0.01")

KINDS: dict[str, str] = {
    "buy": "Kauf",
    "sell": "Verkauf",
    "trade": "Tausch (z. B. Krypto gegen Krypto)",
    "transfer": "Übertrag zwischen eigenen Konten",
    "income": "Ertrag (Dividende, Zinsen, Staking …)",
    "deposit": "Einzahlung / Zugang von außen",
    "withdrawal": "Auszahlung / Abgang nach außen",
    "cost": "Kosten / Verlust",
    "corporate": "Kapitalmaßnahme (Split, Fusion …)",
    "expert": "Experte (alle Felder des Datenvertrags)",
}
KIND_SHORT: dict[str, str] = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "transfer": "Übertrag",
                              "income": "Ertrag", "deposit": "Einzahlung", "withdrawal": "Auszahlung",
                              "cost": "Kosten", "corporate": "Kapitalmaßnahme", "expert": "Experte"}
KIND_HINTS: dict[str, str] = {
    "buy": "Kauf gegen Geld vom Verrechnungskonto; Betrag ohne Gebühr, Gebühr separat.",
    "sell": "Verkauf gegen Geld; Erlös ohne Abzug der Gebühr, Gebühr separat.",
    "trade": "Tausch zweier Assets auf demselben Konto; ohne Angabe wird der EUR-Gegenwert aus dem Tageskurs "
             "ermittelt.",
    "transfer": "Verschiebung zwischen eigenen Konten/Wallets – kein Verkauf, Anschaffungsdaten bleiben erhalten. "
                "Menge ohne Netzwerkgebühr, Gebühr separat.",
    "income": "Zufluss mit Ertragsart. Dividenden brutto erfassen; einbehaltene Quellensteuer wird als eigene "
              "Buchung angelegt.",
    "deposit": "Zugang ohne Gegenbuchung (z. B. Einzahlung, Übertrag von einem nicht erfassten Konto, Schenkung).",
    "withdrawal": "Abgang ohne Gegenbuchung (z. B. Auszahlung, Übertrag auf ein nicht erfasstes Konto, Schenkung).",
    "cost": "Abgang ohne Gegenwert (Gebühr, Bezahlung mit Krypto, Verlust, Diebstahl, Burn).",
    "corporate": "Abgang des alten und Zugang des neuen Bestands; Anschaffungsdaten bleiben erhalten.",
    "expert": "Direkte Eingabe aller Felder (Datenvertrag, Punkt oder Komma als Dezimaltrenner).",
}
TAG_CHOICES: dict[str, list[tuple[str, str]]] = {
    "income": [("dividend", "Dividende / Ausschüttung"), ("interest", "Zinsen"), ("staking", "Staking"),
               ("lending", "Lending"), ("reward", "Reward"), ("bonus", "Bonus"), ("mining", "Mining"),
               ("airdrop", "Airdrop"), ("cashback", "Cashback"), ("fork", "Fork"),
               ("other_income", "sonstiger Ertrag")],
    "deposit": [("", "Einzahlung / Zugang"), ("gift_received", "Schenkung erhalten")],
    "withdrawal": [("", "Auszahlung / Abgang"), ("gift", "Schenkung"), ("donation", "Spende")],
    "cost": [("fee", "Gebühr"), ("cost", "Bezahlung mit Krypto"), ("lost", "Verlust"), ("stolen", "Diebstahl"),
             ("burn", "Burn")],
    "corporate": [("split", "Split"), ("reverse_split", "Reverse Split"), ("merger", "Fusion"),
                  ("spinoff", "Spin-off"), ("migration", "Migration"), ("rename", "Umbenennung"),
                  ("swap", "Token-Swap")],
}
TAG_LABEL: dict[str, str] = {k: v for choices in TAG_CHOICES.values() for k, v in choices if k} | {
    "withholding_tax": "Quellensteuer", "tax": "einbehaltene Steuer", "internal": "intern", "exchange": "Börse",
    "margin": "Margin", "realized_gain": "realisierter Gewinn", "realized_loss": "realisierter Verlust",
    "bridge": "Bridge", "wrap": "Wrap", "unwrap": "Unwrap", "liquidity_in": "Liquidität eingebracht",
    "liquidity_out": "Liquidität entnommen", "refund": "Erstattung"}
TYPE_LABEL = {"buy": "Kauf", "sell": "Verkauf", "trade": "Tausch", "deposit": "Einzahlung",
              "withdrawal": "Auszahlung", "transfer": "Übertrag", "corporate_action": "Kapitalmaßnahme"}
FORM_FIELDS = ("kind", "date", "time", "note", "account", "asset", "qty", "amount", "price", "ccy", "fee",
               "value_eur", "from_account", "to_account", "from_asset", "from_qty", "to_asset", "to_qty",
               "fee_asset", "fee_qty", "fee_eur", "tag", "related_asset", "wht", "type", "price_source")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(:[0-5]\d)?$")
_DE_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")

# (asset_id, Tag) → (Kurs EUR, Quelle) bzw. (Betrag, Währung, Tag) → (EUR, Quelle)
PriceFn = Callable[[str, date], tuple[Decimal, str] | None]
FxFn = Callable[[Decimal, str, date], tuple[Decimal, str] | None]


def s(v: Decimal | None) -> str:
    """Dezimalzahl für CSV (Punkt, ohne Exponent und überflüssige Nullen)."""
    if v is None:
        return ""
    return "0" if v == 0 else format(v.normalize(), "f")


def label_for(t: Any) -> str:
    """Anzeige-Bezeichnung einer Buchung (Typ + Tag)."""
    base = TYPE_LABEL.get(t.type, t.type)
    if t.tag:
        return f"{base} · {TAG_LABEL.get(t.tag, t.tag)}"
    return base


@dataclass
class Draft:
    rows: list[dict[str, str]] = field(default_factory=list)  # Hauptbuchung, ggf. Quellensteuer
    value_sources: list[str | None] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    local_date: date | None = None


class _Form:
    def __init__(self, data: Mapping[str, Any], assets: Mapping[str, AssetInfo], draft: Draft) -> None:
        self.data = data
        self.assets = assets
        self.d = draft

    def raw(self, key: str) -> str:
        v = self.data.get(key)
        return str(v).strip() if v is not None else ""

    def text(self, key: str, label: str, required: bool = True) -> str | None:
        v = self.raw(key)
        if not v:
            if required:
                self.d.errors.append(f"{label} fehlt.")
            return None
        if len(v) > 120:
            self.d.errors.append(f"{label} ist zu lang (max. 120 Zeichen).")
            return None
        return v

    def num(self, key: str, label: str, required: bool = True, allow_zero: bool = False) -> Decimal | None:
        raw = self.raw(key)
        if not raw:
            if required:
                self.d.errors.append(f"{label} fehlt.")
            return None
        v = parse_number(raw)
        if v is None:
            self.d.errors.append(f"{label}: „{raw}“ ist keine Zahl.")
            return None
        if v < 0 or (v == 0 and not allow_zero):
            self.d.errors.append(f"{label} muss größer als 0 sein.")
            return None
        return v

    def asset(self, key: str, label: str, required: bool = True) -> str | None:
        raw = self.raw(key)
        if not raw:
            if required:
                self.d.errors.append(f"{label} fehlt.")
            return None
        aid = resolve_asset(raw, self.assets)
        if aid is None:
            self.d.errors.append(f"{label}: „{raw}“ ist unbekannt – bitte zuerst unter „Neues Asset“ anlegen.")
        return aid

    def currency(self, key: str) -> str | None:
        raw = (self.raw(key) or "EUR").upper()
        a = self.assets.get(raw)
        if raw in C.ISO_CURRENCIES or (a is not None and a.is_fiat):
            return raw
        self.d.errors.append(f"Währung „{raw}“ ist keine bekannte Fiat-Währung.")
        return None

    def when(self, today: date) -> str | None:
        raw = self.raw("date")
        m = _DE_DATE_RE.match(raw)
        try:
            d = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else date.fromisoformat(raw)
        except ValueError:
            self.d.errors.append("Datum fehlt oder ist ungültig (TT.MM.JJJJ).")
            return None
        if d > today + timedelta(days=1):
            self.d.errors.append("Datum liegt in der Zukunft.")
            return None
        if d.year < 1990:
            self.d.errors.append("Datum liegt vor 1990.")
            return None
        self.d.local_date = d
        t_raw = self.raw("time")
        if not t_raw:
            return d.isoformat()  # nur Datum → 12:00 Uhr laut Datenvertrag
        tm = _TIME_RE.match(t_raw)
        if not tm:
            self.d.errors.append("Uhrzeit ungültig (HH:MM).")
            return None
        sec = int(tm.group(3)[1:]) if tm.group(3) else 0
        local = datetime.combine(d, time(int(tm.group(1)), int(tm.group(2)), sec), tzinfo=local_tz())
        return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_asset(raw: str, assets: Mapping[str, AssetInfo]) -> str | None:
    """Eingabe → asset_id (exakt, „ID – Name“, ohne Groß-/Kleinschreibung, eindeutiger Name, ISO-Währung)."""
    v = raw.strip()
    if v in assets:
        return v
    for sep in (" – ", " - ", " — "):
        if sep in v:
            head = v.split(sep, 1)[0].strip()
            if head in assets:
                return head
    low = v.lower()
    hits = [a for a in assets if a.lower() == low]
    if len(hits) == 1:
        return hits[0]
    names = [a.asset_id for a in assets.values() if a.name.lower() == low]
    if len(names) == 1:
        return names[0]
    if v.upper() in C.ISO_CURRENCIES:
        return v.upper()
    return None


def _row(**kw: Any) -> dict[str, str]:
    row = dict.fromkeys(("tx_id", "datetime", "type", "tag", "from_account", "from_asset", "from_qty", "to_account",
                         "to_asset", "to_qty", "fee_asset", "fee_qty", "fee_eur", "value_eur", "orig_price",
                         "orig_ccy", "source", "source_ref", "flag", "note", "related_asset"), "")
    for k, v in kw.items():
        row[k] = s(v) if isinstance(v, Decimal) else ("" if v is None else str(v))
    return row


def build(kind: str, data: Mapping[str, Any], assets: Mapping[str, AssetInfo], price: PriceFn, fx: FxFn,
          today: date) -> Draft:
    """Formulareingaben einer Vorlage → Buchungszeile(n). Fehler und Hinweise stehen im Ergebnis."""
    d = Draft()
    if kind not in KINDS:
        d.errors.append("Unbekannter Vorgang.")
        return d
    f = _Form(data, assets, d)
    when = f.when(today)
    note = f.text("note", "Notiz", required=False) or ""
    handler = _BUILDERS[kind]
    handler(f, d, when or "", note, price, fx)
    if when is None or d.errors:
        d.rows = []
    return d


def _eur_of(asset: str | None, qty: Decimal | None, day: date | None, assets: Mapping[str, AssetInfo],
            price: PriceFn, fx: FxFn) -> tuple[Decimal, str] | None:
    """EUR-Wert einer Menge am Tag (Fiat per Devisenkurs, sonst Tageskurs)."""
    if not asset or qty is None or day is None:
        return None
    a = assets.get(asset)
    if asset.upper() in C.ISO_CURRENCIES or (a is not None and a.is_fiat):
        return fx(qty, asset, day)
    p = price(asset, day)
    if p is None:
        return None
    return (qty * p[0]).quantize(CENT), f"{p[1]} × Menge"


def _money(f: _Form, d: Draft, amount: Decimal, ccy: str, label: str, fx: FxFn) -> tuple[Decimal, str] | None:
    if ccy == "EUR":
        return amount, "Eingabe"
    conv = fx(amount, ccy, d.local_date) if d.local_date else None
    if conv is None:
        d.errors.append(f"{label}: kein Devisenkurs {ccy} zum {fmt_de_date(d.local_date)} – EUR-Gegenwert angeben.")
        return None
    return conv[0].quantize(CENT), conv[1]


def _buy_sell(f: _Form, d: Draft, when: str, note: str, price: PriceFn, fx: FxFn, sell: bool) -> None:
    acc = f.text("account", "Konto")
    asset = f.asset("asset", "Asset")
    qty = f.num("qty", "Stückzahl")
    ccy = f.currency("ccy")
    amount = f.num("amount", "Betrag", required=False)
    unit = f.num("price", "Kurs je Stück", required=False)
    if amount is None and unit is not None and qty is not None:
        amount = (qty * unit).quantize(CENT)
    market_src = None
    if amount is None and not f.raw("amount") and not f.raw("price") and f.raw("price_source") == "market" \
            and asset and qty is not None and d.local_date is not None and ccy == "EUR":
        # Schnellkauf/-verkauf: ohne Kurs und Betrag gilt der historische Marktkurs des Tages (wie bei Tausch)
        p = price(asset, d.local_date)
        if p is None:
            d.errors.append(f"Kein Kurs für {asset} zum {fmt_de_date(d.local_date)} – Kurs oder Betrag angeben.")
            return
        amount, market_src = (qty * p[0]).quantize(CENT), f"{p[1]} × Menge"
    if amount is None and not f.raw("amount") and not f.raw("price"):
        d.errors.append("Betrag oder Kurs je Stück angeben.")
    fee = f.num("fee", "Gebühr", required=False, allow_zero=True) or Decimal(0)
    if d.errors or None in (acc, asset, qty, ccy, amount):
        return
    assert acc and asset and qty is not None and ccy and amount is not None
    override = f.num("value_eur", "Gegenwert in EUR", required=False)
    if override is not None:
        value, src = override, "Eingabe"
    elif market_src is not None:
        value, src = amount, market_src
    else:
        conv = _money(f, d, amount, ccy, "Betrag", fx)
        if conv is None:
            return
        value, src = conv
    fee_eur = None
    if fee:
        fee_eur = fee if ccy == "EUR" else (fee * value / amount).quantize(CENT) if amount else None
    legs_money = (acc, ccy, amount)
    legs_asset = (acc, asset, qty)
    frm, to = (legs_asset, legs_money) if sell else (legs_money, legs_asset)
    d.rows.append(_row(datetime=when, type="sell" if sell else "buy", from_account=frm[0], from_asset=frm[1],
                       from_qty=frm[2], to_account=to[0], to_asset=to[1], to_qty=to[2],
                       fee_asset=ccy if fee else "", fee_qty=fee if fee else "", fee_eur=fee_eur, value_eur=value,
                       orig_price=(amount / qty).quantize(Decimal("1e-8")) if qty else None, orig_ccy=ccy,
                       note=note))
    d.value_sources.append(src)


def _trade(f: _Form, d: Draft, when: str, note: str, price: PriceFn, fx: FxFn) -> None:
    acc = f.text("account", "Konto")
    fa = f.asset("from_asset", "Abgang: Asset")
    fq = f.num("from_qty", "Abgang: Menge")
    ta = f.asset("to_asset", "Zugang: Asset")
    tq = f.num("to_qty", "Zugang: Menge")
    if fa and ta and fa == ta:
        d.errors.append("Abgang und Zugang sind dasselbe Asset – dafür „Übertrag“ oder „Kapitalmaßnahme“ nutzen.")
    fee_asset = f.asset("fee_asset", "Gebühr: Asset", required=False)
    fee_qty = f.num("fee_qty", "Gebühr: Menge", required=False, allow_zero=True)
    value = f.num("value_eur", "Gegenwert in EUR", required=False)
    if d.errors:
        return
    src = "Eingabe"
    if value is None:
        auto = (_eur_of(ta, tq, d.local_date, f.assets, price, fx)
                or _eur_of(fa, fq, d.local_date, f.assets, price, fx))
        if auto is None:
            d.errors.append(f"Kein Kurs für {ta} oder {fa} zum {fmt_de_date(d.local_date)} – Gegenwert in EUR "
                            "angeben.")
            return
        value, src = auto
    fee_eur = _fee_eur(f, d, fee_asset, fee_qty, price, fx)
    d.rows.append(_row(datetime=when, type="trade", from_account=acc, from_asset=fa, from_qty=fq, to_account=acc,
                       to_asset=ta, to_qty=tq, fee_asset=fee_asset if fee_qty else "",
                       fee_qty=fee_qty if fee_qty else "", fee_eur=fee_eur, value_eur=value, note=note))
    d.value_sources.append(src)


def _fee_eur(f: _Form, d: Draft, fee_asset: str | None, fee_qty: Decimal | None, price: PriceFn,
             fx: FxFn) -> Decimal | None:
    if not fee_qty:
        return None
    if not fee_asset:
        d.errors.append("Gebühr: Asset fehlt.")
        return None
    override = f.num("fee_eur", "Gebühr in EUR", required=False, allow_zero=True)
    if override is not None:
        return override
    auto = _eur_of(fee_asset, fee_qty, d.local_date, f.assets, price, fx)
    if auto is None:
        d.warnings.append(f"Kein Kurs für die Gebühr ({fee_asset}) – sie wird mit 0 € bewertet.")
        return None
    return auto[0]


def _flow(f: _Form, d: Draft, when: str, note: str, price: PriceFn, fx: FxFn, typ: str, kind: str) -> None:
    """Zugang (deposit/income) bzw. Abgang (withdrawal/cost) ohne Gegenbuchung."""
    acc = f.text("account", "Konto")
    asset = f.asset("asset", "Asset")
    qty = f.num("qty", "Menge" if kind != "income" else "Betrag bzw. Menge (brutto)")
    tag = f.raw("tag").lower() or None
    allowed = {k for k, _ in TAG_CHOICES.get(kind, [])}
    if tag and allowed and tag not in allowed:
        d.errors.append("Unbekannte Art.")
    if kind in ("income", "cost") and not tag:
        d.errors.append("Art fehlt.")
    related = f.asset("related_asset", "Bezogenes Wertpapier", required=False) if kind == "income" else None
    wht = f.num("wht", "Quellensteuer", required=False, allow_zero=True) if kind == "income" else None
    value = f.num("value_eur", "Wert in EUR", required=False, allow_zero=True)
    if d.errors:
        return
    assert acc and asset and qty is not None
    src = "Eingabe"
    if value is None:
        auto = _eur_of(asset, qty, d.local_date, f.assets, price, fx)
        if auto is None:
            d.errors.append(f"Kein Kurs für {asset} zum {fmt_de_date(d.local_date)} – Wert in EUR angeben.")
            return
        value, src = auto
    leg = {"to_account": acc, "to_asset": asset, "to_qty": qty} if typ == "deposit" else {
        "from_account": acc, "from_asset": asset, "from_qty": qty}
    d.rows.append(_row(datetime=when, type=typ, tag=tag or "", value_eur=value, related_asset=related or "",
                       note=note, **leg))
    d.value_sources.append(src)
    if wht:
        wa = asset if (asset.upper() in C.ISO_CURRENCIES or (f.assets.get(asset) and f.assets[asset].is_fiat)) \
            else "EUR"
        conv = _eur_of(wa, wht, d.local_date, f.assets, price, fx) if wa != "EUR" else (wht, "Eingabe")
        if conv is None:
            d.errors.append(f"Quellensteuer: kein Devisenkurs {wa} zum {fmt_de_date(d.local_date)}.")
            d.rows.clear()
            return
        d.rows.append(_row(datetime=when, type="withdrawal", tag="withholding_tax", from_account=acc, from_asset=wa,
                           from_qty=wht, value_eur=conv[0], related_asset=related or "",
                           note=f"Quellensteuer{' – ' + note if note else ''}"))
        d.value_sources.append(conv[1])


def _transfer(f: _Form, d: Draft, when: str, note: str, price: PriceFn, fx: FxFn) -> None:
    fa = f.text("from_account", "Von Konto")
    ta = f.text("to_account", "Nach Konto")
    asset = f.asset("asset", "Asset")
    qty = f.num("qty", "Menge (ohne Gebühr)")
    fee_qty = f.num("fee_qty", "Netzwerkgebühr", required=False, allow_zero=True)
    if fa and ta and fa == ta:
        d.errors.append("Von- und Nach-Konto sind gleich.")
    if d.errors:
        return
    fee_eur = _fee_eur(f, d, asset, fee_qty, price, fx)
    d.rows.append(_row(datetime=when, type="transfer", from_account=fa, from_asset=asset, from_qty=qty,
                       to_account=ta, to_asset=asset, to_qty=qty, fee_asset=asset if fee_qty else "",
                       fee_qty=fee_qty if fee_qty else "", fee_eur=fee_eur, note=note))
    d.value_sources.append(None)


def _corporate(f: _Form, d: Draft, when: str, note: str, price: PriceFn, fx: FxFn) -> None:
    acc = f.text("account", "Konto")
    fa = f.asset("from_asset", "Bisheriges Asset")
    fq = f.num("from_qty", "Bisherige Menge")
    ta = f.asset("to_asset", "Neues Asset", required=False) or fa
    tq = f.num("to_qty", "Neue Menge")
    tag = f.raw("tag").lower()
    if tag not in {k for k, _ in TAG_CHOICES["corporate"]}:
        d.errors.append("Art der Kapitalmaßnahme fehlt.")
    if d.errors:
        return
    d.rows.append(_row(datetime=when, type="corporate_action", tag=tag, from_account=acc, from_asset=fa,
                       from_qty=fq, to_account=acc, to_asset=ta, to_qty=tq, note=note))
    d.value_sources.append(None)


def _expert(f: _Form, d: Draft, when: str, note: str, price: PriceFn, fx: FxFn) -> None:
    typ = f.raw("type").lower()
    if typ not in C.TX_TYPES:
        d.errors.append("Typ fehlt oder ist unbekannt.")
    vals: dict[str, Any] = {"datetime": when, "type": typ, "tag": f.raw("tag").lower(), "note": note}
    for side in ("from", "to"):
        acc = f.text(f"{side}_account", f"{side}_account", required=False)
        asset = f.asset(f"{side}_asset", f"{side}_asset", required=False)
        qty = f.num(f"{side}_qty", f"{side}_qty", required=False, allow_zero=True)
        vals |= {f"{side}_account": acc, f"{side}_asset": asset, f"{side}_qty": qty}
    vals["fee_asset"] = f.asset("fee_asset", "fee_asset", required=False)
    vals["fee_qty"] = f.num("fee_qty", "fee_qty", required=False, allow_zero=True)
    vals["fee_eur"] = f.num("fee_eur", "fee_eur", required=False, allow_zero=True)
    vals["value_eur"] = f.num("value_eur", "value_eur", required=False, allow_zero=True)
    vals["related_asset"] = f.asset("related_asset", "related_asset", required=False)
    if d.errors:
        return
    d.rows.append(_row(**vals))
    d.value_sources.append("Eingabe" if vals["value_eur"] is not None else None)


_BUILDERS: dict[str, Callable[..., None]] = {
    "buy": lambda f, d, w, n, p, x: _buy_sell(f, d, w, n, p, x, sell=False),
    "sell": lambda f, d, w, n, p, x: _buy_sell(f, d, w, n, p, x, sell=True),
    "trade": _trade,
    "transfer": _transfer,
    "income": lambda f, d, w, n, p, x: _flow(f, d, w, n, p, x, "deposit", "income"),
    "deposit": lambda f, d, w, n, p, x: _flow(f, d, w, n, p, x, "deposit", "deposit"),
    "withdrawal": lambda f, d, w, n, p, x: _flow(f, d, w, n, p, x, "withdrawal", "withdrawal"),
    "cost": lambda f, d, w, n, p, x: _flow(f, d, w, n, p, x, "withdrawal", "cost"),
    "corporate": _corporate,
    "expert": _expert,
}
