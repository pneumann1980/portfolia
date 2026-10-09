"""Dokumentverständnis (M25): Anbieter, Dokumentart und Vorgänge mit Belegstellen.

Aufbau
* **Generische Regeln** für alle Dokumente: Schlüssel/Wert-Paare (``Kurswert  1.234,50 EUR``, ``Datum: 02.01.2025``)
  über ein Synonymverzeichnis (deutsch/englisch), Tabellen mit erkannter Kopfzeile (Spalten aus Abständen bzw.
  Koordinaten), Zeilen mit Vorgangswort, Datum, Menge und Betrag (Tabellen ohne Kopfzeile, Screenshots).
* **Profile** (wenige, getestete) legen Dokumentart und Buchungslogik fest: Wertpapierabrechnung Kauf/Verkauf,
  Dividenden-/Ertragsgutschrift, Krypto-Abrechnung (z. B. Bitpanda, Coinbase, Binance), Wallet-Transaktion
  (z. B. Ledger Live, Explorer), Konto-/Transaktionsübersicht (Tabelle). Anbieter werden nur aus Schlüsselwörtern des
  Dokuments erkannt und dienen der Einordnung, nicht als Beleg für Werte.

Jeder Wert wird mit Belegstelle (Seite, Zeile, Box, OCR-Konfidenz) als :class:`FieldEvidence` (Herkunft
``document``) geführt. Abgeleitete Werte (z. B. Kurswert = Stück × Kurs) sind „rekonstruiert“ mit Begründung; nichts
wird erfunden – fehlende Pflichtangaben bleiben **ungelöst**.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal

from app.documentimport import parse as P
from app.documentimport.evidence import FieldDecision, FieldEvidence, resolve_fields
from app.documentimport.extract import DocumentResult, Line

CENT = Decimal("0.01")
OCR_MIN_CONF = 80.0  # darunter gilt ein OCR-Wert als unsicher (Prüfhinweis, keine Sammelübernahme)
MAX_TX = 300

# Felder je Vorgang (kanonische Namen)
FIELD_LABEL = {
    "kind": "Vorgang", "date": "Datum", "time": "Uhrzeit", "value_date": "Valuta", "asset": "Bezeichnung",
    "symbol": "Symbol", "isin": "ISIN", "wkn": "WKN", "contract": "Contract", "chain": "Netzwerk",
    "quantity": "Menge", "price": "Kurs/Preis", "gross": "Kurswert/Brutto", "net": "Ausmachender Betrag/Gesamt",
    "ccy": "Währung", "fx_rate": "Devisenkurs", "value_eur": "EUR-Gegenwert", "fee": "Gebühr",
    "fee_ccy": "Gebührenwährung", "network_fee": "Netzwerkgebühr", "network_fee_sym": "Netzwerkgebühr (Asset)",
    "tax": "Steuern", "withholding_tax": "Quellensteuer", "txhash": "Transaktions-Hash",
    "ext_id": "Auftrags-/Transaktions-ID",
    "account": "Konto/Depot/Wallet", "from_address": "Von", "to_address": "An", "status": "Status",
}
_SYN: list[tuple[str, tuple[str, ...]]] = [
    # Reihenfolge: spezifisch vor allgemein
    ("net", ("ausmachender betrag", "endbetrag", "zu ihren lasten", "zu ihren gunsten", "zu lasten", "zu gunsten",
             "gesamtbetrag", "net amount", "total amount", "gesamtsumme", "summe", "total", "gesamt")),
    ("gross", ("kurswert", "bruttobetrag", "brutto", "gross amount", "subtotal", "zwischensumme", "betrag",
               "amount paid", "you paid", "you received", "ausschüttung gesamt", "dividendenbetrag")),
    ("network_fee", ("netzwerkgebühr", "netzwerkgebuehr", "network fee", "gas fee", "gasgebühr", "miner fee",
                     "transaktionsgebühr (netzwerk)", "blockchain fee")),
    ("withholding_tax", ("quellensteuer", "withholding tax", "ausländische quellensteuer")),
    ("tax", ("kapitalertragsteuer", "kapitalertragssteuer", "solidaritätszuschlag", "solidaritaetszuschlag",
             "kirchensteuer", "abgeltungsteuer")),
    ("fee", ("provision", "ordergebühr", "orderentgelt", "transaktionsentgelt", "handelsplatzgebühr",
             "handelsplatzentgelt", "fremde spesen", "börsengebühr", "maklercourtage", "trading fee", "gebühren",
             "gebühr", "gebuehr", "spesen", "entgelt", "fees", "fee", "kommission", "commission")),
    ("fx_rate", ("devisenkurs", "umrechnungskurs", "wechselkurs", "exchange rate", "fx rate")),
    ("price", ("ausführungskurs", "ausfuehrungskurs", "kurs je stück", "preis je einheit", "stückpreis",
               "price per coin", "price per unit", "dividende pro stück", "ausschüttung pro stück",
               "dividend per share", "kurs", "preis", "price", "rate")),
    ("quantity", ("stück", "stueck", "stk.", "anzahl", "nominale", "menge", "quantity", "anteile", "shares",
                  "amount", "units")),
    ("isin", ("isin",)),
    ("wkn", ("wkn",)),
    ("trade_date", ("handelstag", "schlusstag", "ausführungstag", "ausfuehrungstag", "trade date", "execution date",
                    "auftragsdatum")),
    ("value_date", ("valuta", "valutadatum", "zahltag", "wertstellung", "value date", "settlement date",
                    "pay date", "zahlbar")),
    ("time", ("handelszeit", "ausführungszeit", "ausfuehrungszeit", "uhrzeit", "zeit", "time")),
    ("date", ("datum", "date", "abrechnungstag", "buchungstag", "ex-tag")),
    ("txhash", ("transaktions-hash", "transaktionshash", "transaction hash", "tx hash", "tx-hash", "txhash",
                "txid", "hash")),
    ("ext_id", ("auftragsnummer", "ordernummer", "order-nr", "order id", "order-id", "transaktions-id",
                "transaktionsnummer", "transaction id", "trade id", "referenznummer", "referenz", "reference",
                "abrechnungsnr", "abrechnungsnummer", "ref.", "id")),
    ("asset", ("wertpapierbezeichnung", "gattungsbezeichnung", "wertpapier", "bezeichnung", "kryptowährung",
               "kryptowaehrung", "asset", "coin", "token", "titel")),
    ("account", ("depotnummer", "depot", "kontonummer", "konto", "wallet", "account")),
    ("from_address", ("absender", "von", "from")),
    ("to_address", ("empfänger", "empfaenger", "an", "to")),
    ("status", ("status",)),
    ("chain", ("netzwerk", "network", "blockchain", "chain")),
    ("ccy", ("währung", "waehrung", "currency")),
]
_LABELS = sorted(((syn, f) for f, syns in _SYN for syn in syns), key=lambda x: -len(x[0]))
_PROVIDERS = [
    ("bitpanda", ("bitpanda",)), ("binance", ("binance",)), ("coinbase", ("coinbase",)), ("kraken", ("kraken",)),
    ("ledger", ("ledger live", "ledger nano", "ledger")), ("trade_republic", ("trade republic",)),
    ("scalable", ("scalable capital",)), ("comdirect", ("comdirect",)), ("consorsbank", ("consorsbank",)),
    ("ing", ("ing-diba", "ing deutschland")), ("dkb", ("deutsche kreditbank", "dkb")), ("flatex", ("flatex",)),
    ("etherscan", ("etherscan",)), ("kaspa", ("kaspa explorer", "kas.fyi")),
]
PROVIDER_LABEL = {"bitpanda": "Bitpanda", "binance": "Binance", "coinbase": "Coinbase", "kraken": "Kraken",
                  "ledger": "Ledger Live", "trade_republic": "Trade Republic", "scalable": "Scalable Capital",
                  "comdirect": "comdirect", "consorsbank": "Consorsbank", "ing": "ING", "dkb": "DKB",
                  "flatex": "flatex", "etherscan": "Etherscan", "kaspa": "Kaspa Explorer"}
DOC_LABEL = {"securities_trade": "Wertpapierabrechnung", "dividend": "Dividenden-/Ertragsgutschrift",
             "crypto_trade": "Krypto-Abrechnung", "wallet_tx": "Wallet-Transaktion",
             "statement": "Konto-/Transaktionsübersicht", "unknown": "unbekanntes Dokument"}
KIND_LABEL = {"buy": "Kauf", "sell": "Verkauf", "dividend": "Dividende/Ertrag", "deposit": "Eingang",
              "withdrawal": "Ausgang", "trade": "Tausch", "unknown": "unbestimmt"}
_ACTION = [("sell", ("verkauf", "verkauft", "sell", "sold")), ("buy", ("kauf", "gekauft", "buy", "bought", "purchase")),
           ("dividend", ("dividende", "dividendengutschrift", "ertragsgutschrift", "ausschüttung", "dividend",
                         "distribution")),
           ("deposit", ("empfangen", "eingang", "erhalten", "einzahlung", "received", "receive", "deposit",
                        "incoming")),
           ("withdrawal", ("gesendet", "ausgang", "auszahlung", "sent", "send", "withdrawal", "outgoing")),
           ("trade", ("tausch", "swap", "convert", "umtausch"))]
_ACTION_RE = re.compile(r"(?<![\w-])(" + "|".join(sorted({w for _k, ws in _ACTION for w in ws}, key=len, reverse=True))
                        + r")(?![\w-])", re.I)
ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")
WKN_RE = re.compile(r"\b([A-HJ-NP-Z0-9]{6})\b")
EVM_HASH = re.compile(r"\b0x[0-9a-fA-F]{64}\b")
HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
EVM_ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
SYMBOL_QTY = re.compile(r"(?<![\w.,])([-−+]?\d[\d.,' ]*\d|\d)\s*([A-Z][A-Z0-9]{1,9})(?![\w])")
_FIAT = P.FIAT


def isin_ok(s: str) -> bool:
    """ISIN-Prüfziffer (Luhn über Ziffernfolge) – schützt vor OCR-Fehlern und zufälligen Treffern."""
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[:-1])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(s[-1])


@dataclass
class DocTx:
    """Ein erkannter Vorgang mit allen Feldbelegen (noch keine Buchung)."""

    n: int
    kind: str
    doc_type: str
    provider: str | None
    evidence: list[FieldEvidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: list[tuple[int, int]] = field(default_factory=list)  # (Seite, Zeile) der Fundstellen
    decisions: dict[str, FieldDecision] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)  # weitere Dokumente desselben Vorgangs (SHA-256)

    def resolve(self) -> None:
        self.decisions = resolve_fields(self.evidence)

    def value(self, name: str) -> str | None:
        d = self.decisions.get(name)
        return d.selected.value if d is not None and d.selected is not None else None

    def dec(self, name: str) -> Decimal | None:
        v = self.value(name)
        return Decimal(v) if v not in (None, "") else None

    def status(self, name: str) -> str:
        d = self.decisions.get(name)
        if d is None or d.selected is None:
            return "ungeloest"
        return d.selected.status

    def conflicts(self) -> list[str]:
        return [n for n, d in self.decisions.items() if d.conflicts]


@dataclass
class Analysis:
    doc_type: str
    provider: str | None
    convention: P.Convention
    txs: list[DocTx] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    account_label: str | None = None


# ----------------------------------------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------------------------------------

def _cells(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"\s{2,}|\t|\s\|\s", text) if c.strip()]


def _label_of(cell: str) -> tuple[str | None, str]:
    """Zelle beginnt mit einer bekannten Bezeichnung? → (Feld, Rest der Zelle)."""
    low = cell.lower().strip()
    for syn, fld in _LABELS:
        if low == syn or low.startswith(syn + ":") or low.startswith(syn + " ") or low == syn + ":":
            rest = cell[len(syn):].lstrip(" :.").strip()
            if fld == "ext_id" and syn == "id" and rest and not re.search(r"\d", rest):
                continue
            return fld, rest
    return None, cell


def _ev(fld: str, value: object, ln: Line, sha: str, status: str = "belegt", reason: str = "",
        category: str = "original") -> FieldEvidence:
    return FieldEvidence(field=fld, value=str(value), origin="document", source_ref=f"document:{sha}",
                         location=f"Seite {ln.page}, Zeile {ln.no}", status=status,  # type: ignore[arg-type]
                         reason=reason or f"Beleg: „{ln.text[:80]}“", category=category, page=ln.page, line=ln.no,
                         box=ln.box, conf=ln.conf)


def _derived(fld: str, value: object, sha: str, reason: str, status: str = "rekonstruiert") -> FieldEvidence:
    return FieldEvidence(field=fld, value=str(value), origin="document", source_ref=f"document:{sha}",
                         location="abgeleitet", status=status, reason=reason)  # type: ignore[arg-type]


def _money(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def detect_provider(text: str) -> str | None:
    low = text.lower()
    for key, words in _PROVIDERS:
        if any(re.search(rf"(?<![\w]){re.escape(w)}(?![\w])", low) for w in words):
            return key
    return None


def detect_type(text: str) -> str:
    low = text.lower()
    has_isin = any(isin_ok(m.group(1)) for m in ISIN_RE.finditer(text))
    if has_isin and re.search(r"dividend|ertragsgutschrift|ausschüttung|ausschuettung|distribution", low):
        return "dividend"
    if has_isin and re.search(r"wertpapierabrechnung|abrechnung|kurswert|ausmachender betrag|order", low):
        return "securities_trade"
    if EVM_HASH.search(text) or re.search(r"\b(transaction hash|tx hash|transaktions-hash|txid)\b", low):
        return "wallet_tx"
    return "unknown"


def _action(text: str) -> str | None:
    m = _ACTION_RE.search(text)
    if not m:
        return None
    w = m.group(1).lower()
    return next(k for k, ws in _ACTION if w in ws)


# ----------------------------------------------------------------------------------------------------
# Schlüssel/Wert-Dokumente (eine Abrechnung = ein Vorgang)
# ----------------------------------------------------------------------------------------------------

def _kv_pairs(lines: Iterable[Line]) -> list[tuple[str, str, Line]]:
    """(Feld, Rohwert, Zeile): „Label  Wert  Label  Wert“, „Label: Wert“ sowie Wert in der Zeile darunter."""
    out: list[tuple[str, str, Line]] = []
    lines = list(lines)
    for i, ln in enumerate(lines):
        cells = _cells(ln.text)
        if len(cells) == 1 and ":" in cells[0]:
            a, _, b = cells[0].partition(":")
            cells = [a + ":", b] if b.strip() else [a]
        j = 0
        while j < len(cells):
            fld, rest = _label_of(cells[j])
            if fld is None:
                j += 1
                continue
            val = rest
            if not val and j + 1 < len(cells) and _label_of(cells[j + 1])[0] is None:
                val = cells[j + 1]
                j += 1
            if not val and j == len(cells) - 1 and i + 1 < len(lines):
                nxt = _cells(lines[i + 1].text)
                if len(nxt) == 1 and _label_of(nxt[0])[0] is None:
                    val = nxt[0]
            if val:
                out.append((fld, val, ln))
            j += 1
    return out


def _qty_symbol(raw: str, conv: P.Convention) -> tuple[Decimal | None, str | None, str | None]:
    """„0,00512345 BTC“ → (Menge, Symbol, Hinweis)."""
    m = SYMBOL_QTY.search(raw)
    try:
        if m and m.group(2) not in _FIAT:
            return abs(P.number(m.group(1), conv)), m.group(2), None
        nums = P.NUM_RE.findall(raw)
        if nums:
            return abs(P.number(nums[0], conv)), None, None
    except P.Ambiguous as e:
        return None, None, str(e)
    return None, None, None


def _kv_tx(doc: DocumentResult, an: Analysis, kind: str, lines: list[Line], n: int) -> DocTx:
    sha = doc.sha256
    conv = an.convention
    tx = DocTx(n, kind, an.doc_type, an.provider)
    ev = tx.evidence
    for fld, raw, ln in _kv_pairs(lines):
        tx.lines.append((ln.page, ln.no))
        try:
            if fld in ("gross", "net", "fee", "tax", "withholding_tax", "price", "fx_rate", "network_fee"):
                if fld == "network_fee":
                    q, sym, note = _qty_symbol(raw, conv)
                    if note:
                        tx.warnings.append(note)
                    if q is not None and sym:
                        ev += [_ev("network_fee", q, ln, sha), _ev("network_fee_sym", sym, ln, sha)]
                        continue
                amts, notes = P.amounts(raw, conv)
                tx.warnings += notes
                if not amts:
                    continue
                a = amts[0]
                val = abs(a.value) if fld != "fx_rate" else a.value
                ev.append(_ev(fld, val, ln, sha))
                ccy = a.currency or P.currency_of(raw)
                if fld == "gross" and ccy:
                    ev.append(_ev("ccy", ccy, ln, sha))
                elif fld in ("net", "price", "tax", "withholding_tax") and ccy:
                    ev.append(_ev(f"{fld}_ccy", ccy, ln, sha))
                elif fld in ("fee", "network_fee") and ccy:
                    ev.append(_ev("fee_ccy", ccy, ln, sha))
                if fld == "fx_rate":
                    pair = re.search(r"\b(EUR)\s*/\s*([A-Z]{3})\b|\b([A-Z]{3})\s*/\s*(EUR)\b", raw)
                    if pair:
                        ev.append(_ev("fx_pair", pair.group(0).replace(" ", ""), ln, sha))
            elif fld == "quantity":
                q, sym, note = _qty_symbol(raw, conv)
                if note:
                    tx.warnings.append(f"Menge: {note}")
                if q is not None:
                    ev.append(_ev("quantity", q, ln, sha))
                if sym:
                    ev.append(_ev("symbol", sym, ln, sha))
            elif fld in ("trade_date", "value_date", "date"):
                ds, notes = P.dates(raw)
                tx.warnings += notes
                if ds:
                    ev.append(_ev("date" if fld != "value_date" else "value_date", ds[0][0].isoformat(), ln, sha,
                                  reason=("Handelstag laut Beleg" if fld == "trade_date" else
                                          "Valuta/Zahltag laut Beleg" if fld == "value_date" else
                                          "Datum laut Beleg")))
                    ts = P.times(raw[ds[0][2]:])
                    if ts and fld != "value_date":
                        ev.append(_ev("time", ts[0][0].isoformat(), ln, sha))
                        if ts[0][1]:
                            ev.append(_ev("tz", ts[0][1], ln, sha))
            elif fld == "time":
                ts = P.times(raw)
                if ts:
                    ev.append(_ev("time", ts[0][0].isoformat(), ln, sha))
                    if ts[0][1]:
                        ev.append(_ev("tz", ts[0][1], ln, sha))
            elif fld == "isin":
                m = ISIN_RE.search(raw.replace(" ", ""))
                if m and isin_ok(m.group(1)):
                    ev.append(_ev("isin", m.group(1), ln, sha))
                elif m:
                    tx.warnings.append(f"ISIN {m.group(1)} mit ungültiger Prüfziffer – nicht übernommen")
            elif fld == "wkn":
                m = WKN_RE.search(raw.upper())
                if m:
                    ev.append(_ev("wkn", m.group(1), ln, sha))
            elif fld == "txhash":
                m = EVM_HASH.search(raw) or HEX64.search(raw)
                if m:
                    ev.append(_ev("txhash", m.group(0).lower(), ln, sha))
            elif fld == "ext_id":
                v = raw.strip().split()[0][:120] if raw.strip() else ""
                if v and re.search(r"\d", v):
                    ev.append(_ev("ext_id", v, ln, sha))
            elif fld == "asset":
                ev.append(_ev("asset", raw[:120], ln, sha))
            elif fld == "account":
                ev.append(_ev("account", _mask(raw), ln, sha, reason="Konto/Depot laut Beleg (maskiert)"))
            elif fld in ("from_address", "to_address"):
                m = EVM_ADDR.search(raw) or re.search(r"\b[a-z0-9]{2,6}[:1][a-z0-9]{25,90}\b", raw)
                if m:
                    ev.append(_ev(fld, m.group(0), ln, sha))
            elif fld == "status":
                ev.append(_ev("status", raw[:40], ln, sha))
            elif fld == "chain":
                ev.append(_ev("chain", raw[:40], ln, sha))
            elif fld == "ccy":
                c = P.currency_of(raw) or (raw.strip().upper()[:3] if raw.strip().upper()[:3] in _FIAT else None)
                if c:
                    ev.append(_ev("ccy", c, ln, sha))
        except P.Ambiguous as e:
            tx.warnings.append(f"{FIELD_LABEL.get(fld, fld)}: {e}")
    # Zusatzfunde ohne Bezeichnung (Hash, ISIN im Titel, Menge+Symbol im Titel)
    have = {e.field for e in ev}
    for ln in lines:
        if "isin" not in have:
            for m in ISIN_RE.finditer(ln.text):
                if isin_ok(m.group(1)):
                    ev.append(_ev("isin", m.group(1), ln, sha, reason="ISIN im Text"))
                    have.add("isin")
                    break
        if "txhash" not in have:
            m = EVM_HASH.search(ln.text)
            if m:
                ev.append(_ev("txhash", m.group(0).lower(), ln, sha, reason="Hash im Text"))
                have.add("txhash")
        if "ext_id" not in have and an.provider == "bitpanda":
            m = UUID_RE.search(ln.text)
            if m:
                ev.append(_ev("ext_id", m.group(0).lower(), ln, sha, reason="Bitpanda-Transaktions-ID im Text"))
                have.add("ext_id")
    if "quantity" not in have and kind in ("buy", "sell", "deposit", "withdrawal"):
        for ln in lines:
            if _action(ln.text) is None:
                continue
            q, sym, _note = _qty_symbol(ln.text, conv)
            if q is not None and sym:
                ev += [_ev("quantity", q, ln, sha, reason="Menge im Vorgangstitel"),
                       _ev("symbol", sym, ln, sha, reason="Symbol im Vorgangstitel")]
                break
    if "date" not in have:
        for ln in lines:
            ds, _n = P.dates(ln.text)
            if ds:
                ev.append(_ev("date", ds[0][0].isoformat(), ln, sha, reason="Datum im Text (ohne Bezeichnung)"))
                ts = P.times(ln.text[ds[0][2]:])
                if ts:
                    ev.append(_ev("time", ts[0][0].isoformat(), ln, sha))
                    if ts[0][1]:
                        ev.append(_ev("tz", ts[0][1], ln, sha))
                break
    return tx


def _mask(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 6:
        return f"…{digits[-4:]}"
    return raw.strip()[:40]


# ----------------------------------------------------------------------------------------------------
# Tabellen (Übersichten mit mehreren Vorgängen)
# ----------------------------------------------------------------------------------------------------

_COLS = {
    "date": ("datum", "date", "buchungstag", "handelstag", "zeitpunkt", "time", "zeit"),
    "kind": ("typ", "type", "art", "vorgang", "transaktion", "transaction", "aktion", "action", "side", "richtung"),
    "symbol": ("asset", "coin", "währung", "waehrung", "symbol", "token", "wertpapier", "instrument", "pair", "markt",
               "market", "kryptowährung"),
    "quantity": ("menge", "anzahl", "stück", "amount", "quantity", "qty", "größe", "volumen"),
    "price": ("preis", "kurs", "price", "rate"),
    "amount": ("betrag", "wert", "summe", "gesamt", "total", "value", "eur", "kosten", "erlös", "netto"),
    "fee": ("gebühr", "gebühren", "fee", "fees", "provision"),
    "txhash": ("hash", "tx", "txid", "tx-hash"),
    "ext_id": ("id", "referenz", "reference", "order", "auftrag"),
    "status": ("status",),
}


def _header(cells: list[str]) -> dict[int, str] | None:
    m: dict[int, str] = {}
    for i, c in enumerate(cells):
        low = c.lower().strip(" :")
        for col, syns in _COLS.items():
            if low in syns or any(low.startswith(s + " ") or low.startswith(s + "(") for s in syns):
                if col not in m.values():
                    m[i] = col
                break
    return m if len(m) >= 3 and "date" in m.values() else None


def _table_txs(doc: DocumentResult, an: Analysis) -> list[DocTx]:
    sha, conv = doc.sha256, an.convention
    out: list[DocTx] = []
    for page in doc.pages:
        hdr: dict[int, str] | None = None
        width = 0
        for ln in page.lines:
            cells = _cells(ln.text)
            h = _header(cells)
            if h:
                hdr, width = h, len(cells)
                continue
            if hdr is None or len(cells) < 2:
                continue
            ds, _n = P.dates(ln.text)
            if not ds:
                continue
            if len(out) >= MAX_TX:
                an.warnings.append(f"Mehr als {MAX_TX} Zeilen – Rest nicht ausgewertet.")
                return out
            tx = DocTx(len(out) + 1, "unknown", "statement", an.provider)
            tx.lines.append((ln.page, ln.no))
            if len(cells) != width:
                tx.warnings.append(f"Zeile hat {len(cells)} statt {width} Spalten – Zuordnung prüfen "
                                   "(abgeschnittene oder zusammengefasste Spalte)")
            _row_fields(tx, cells, hdr if len(cells) == width else {}, ln, sha, conv)
            out.append(tx)
    return out


def _row_fields(tx: DocTx, cells: list[str], hdr: dict[int, str], ln: Line, sha: str, conv: P.Convention) -> None:
    """Eine Tabellenzeile: mit Kopf nach Spalten, sonst nach Mustern (Datum, Vorgangswort, Menge+Symbol, Betrag)."""
    ev = tx.evidence
    text = ln.text
    ds, notes = P.dates(text)
    tx.warnings += notes
    if ds:
        ev.append(_ev("date", ds[0][0].isoformat(), ln, sha, reason="Datum der Tabellenzeile"))
        ts = P.times(text)
        if ts:
            ev.append(_ev("time", ts[0][0].isoformat(), ln, sha))
            if ts[0][1]:
                ev.append(_ev("tz", ts[0][1], ln, sha))
    act = _action(" ".join(cells[i] for i, c in hdr.items() if c == "kind")) if hdr else None
    act = act or _action(text)
    if act:
        tx.kind = act
        ev.append(_ev("kind", act, ln, sha, reason="Vorgangswort der Zeile"))
    by_col = {c: cells[i] for i, c in hdr.items() if i < len(cells)}
    try:
        if by_col.get("quantity"):
            q, sym, note = _qty_symbol(by_col["quantity"], conv)
            if note:
                tx.warnings.append(f"Menge: {note}")
            if q is not None:
                ev.append(_ev("quantity", q, ln, sha))
            if sym:
                ev.append(_ev("symbol", sym, ln, sha))
        if by_col.get("symbol") and not any(e.field == "symbol" for e in ev):
            s = re.sub(r"[^A-Z0-9]", "", by_col["symbol"].upper().split("/")[0].split("-")[0])[:10]
            if ISIN_RE.fullmatch(s or "") and isin_ok(s):
                ev.append(_ev("isin", s, ln, sha))
            elif s and s not in _FIAT:
                ev.append(_ev("symbol", s, ln, sha))
        for col, fld in (("amount", "gross"), ("price", "price"), ("fee", "fee")):
            if by_col.get(col):
                amts, n2 = P.amounts(by_col[col], conv)
                tx.warnings += n2
                if amts:
                    ev.append(_ev(fld, abs(amts[0].value), ln, sha))
                    if amts[0].currency:
                        ev.append(_ev("ccy" if fld == "gross" else f"{fld}_ccy", amts[0].currency, ln, sha))
        for col in ("txhash", "ext_id", "status"):
            if by_col.get(col):
                v = by_col[col].strip()
                if col == "txhash":
                    m = EVM_HASH.search(v) or HEX64.search(v)
                    if m:
                        ev.append(_ev("txhash", m.group(0).lower(), ln, sha))
                elif v:
                    ev.append(_ev(col, v[:120], ln, sha))
        if not hdr:  # ohne Kopfzeile: Muster
            q, sym, note = _qty_symbol(text, conv)
            if note:
                tx.warnings.append(f"Menge: {note}")
            if q is not None and sym:
                ev += [_ev("quantity", q, ln, sha, reason="Menge+Symbol der Zeile"),
                       _ev("symbol", sym, ln, sha, reason="Menge+Symbol der Zeile")]
            amts, n3 = P.amounts(text, conv)
            tx.warnings += n3
            money_ = [a for a in amts if a.currency in _FIAT]
            if money_:
                ev.append(_ev("gross", abs(money_[0].value), ln, sha, reason="Betrag mit Währung der Zeile"))
                ev.append(_ev("ccy", money_[0].currency, ln, sha))
            m = EVM_HASH.search(text)
            if m:
                ev.append(_ev("txhash", m.group(0).lower(), ln, sha))
            for m2 in ISIN_RE.finditer(text):
                if isin_ok(m2.group(1)):
                    ev.append(_ev("isin", m2.group(1), ln, sha))
                    break
    except P.Ambiguous as e:
        tx.warnings.append(str(e))


def _line_txs(doc: DocumentResult, an: Analysis) -> list[DocTx]:
    """Ohne Kopfzeile/Profil: jede Zeile mit Vorgangswort **und** Datum wird ein Kandidat – Nachbarzeilen werden
    nicht zusammengeführt (keine Übertragung von Werten auf andere Vorgänge)."""
    out: list[DocTx] = []
    for ln in doc.lines():
        if _action(ln.text) is None or not P.dates(ln.text)[0]:
            continue
        if len(out) >= MAX_TX:
            an.warnings.append(f"Mehr als {MAX_TX} Vorgänge – Rest nicht ausgewertet.")
            break
        tx = DocTx(len(out) + 1, "unknown", "statement", an.provider)
        tx.lines.append((ln.page, ln.no))
        _row_fields(tx, _cells(ln.text), {}, ln, doc.sha256, an.convention)
        out.append(tx)
    return out


# ----------------------------------------------------------------------------------------------------
# Plausibilität und Ableitungen (nur aus Werten desselben Belegs)
# ----------------------------------------------------------------------------------------------------

def _validate(tx: DocTx, sha: str) -> None:
    tx.resolve()
    q, price, gross, net = tx.dec("quantity"), tx.dec("price"), tx.dec("gross"), tx.dec("net")
    fee = tx.dec("fee") or Decimal(0)
    tax = (tx.dec("tax") or Decimal(0)) + (tx.dec("withholding_tax") or Decimal(0))
    fees_listed = "fee" in tx.decisions or "tax" in tx.decisions or "withholding_tax" in tx.decisions
    if tx.kind in ("buy", "sell") and q is not None and price is not None:
        calc = _money(q * price)
        if gross is None:
            tx.evidence.append(_derived("gross", calc, sha, f"Kurswert = Menge × Kurs = {q} × {price}"))
        elif abs(calc - gross) > max(CENT * 2, gross * Decimal("0.002")):
            tx.warnings.append(f"Menge × Kurs = {calc} weicht vom Kurswert {gross} ab – Werte prüfen")
    if tx.kind in ("buy", "sell") and price is None and q and gross is not None and q != 0:
        tx.evidence.append(_derived("price", (gross / q).quantize(Decimal("0.00000001")), sha,
                                    f"Kurs = Kurswert / Menge = {gross} / {q} (gerundet)"))
    ccy0 = tx.value("ccy") or tx.value("price_ccy")
    same_ccy = all(tx.value(f"{n}_ccy") in (None, ccy0) for n in ("net", "fee", "tax", "withholding_tax"))
    if gross is not None and net is not None and tx.kind in ("buy", "sell", "dividend") and same_ccy:
        sign = 1 if tx.kind == "buy" else -1
        expected = gross + sign * (fee + (tax if tx.kind != "buy" else Decimal(0)))
        if abs(expected - net) > CENT * 2:
            diff = net - gross
            tx.warnings.append(f"Ausmachender Betrag {net} ≠ Kurswert {gross} {'+' if sign > 0 else '−'} Gebühren/"
                               f"Steuern laut Beleg (Differenz {diff}) – nicht zugeordnete Kosten oder Steuern; "
                               "nichts wird daraus abgeleitet")
    if gross is None and net is not None and tx.kind == "buy" and fees_listed and "tax" not in tx.decisions \
            and same_ccy:
        tx.evidence.append(_derived("gross", _money(net - fee), sha,
                                    f"Kurswert = Gesamtbetrag − ausgewiesene Gebühr = {net} − {fee}"))
    tx.resolve()
    ccy = tx.value("ccy") or tx.value("price_ccy")
    gross = tx.dec("gross")
    if gross is not None and ccy == "EUR" and "value_eur" not in tx.decisions:
        tx.evidence.append(_derived("value_eur", _money(gross), sha, "Kurswert/Brutto in EUR laut Beleg",
                                    status="belegt"))
    elif gross is not None and ccy and ccy != "EUR" and tx.dec("fx_rate"):
        fx = tx.dec("fx_rate") or Decimal(1)
        pair = (tx.value("fx_pair") or "").upper()
        val = gross * fx if pair.endswith("/EUR") and not pair.startswith("EUR") else gross / fx
        tx.evidence.append(_derived("value_eur", _money(val), sha,
                                    f"{gross} {ccy} mit Devisenkurs {fx} laut Beleg ({pair or 'EUR/' + ccy})"))
    tx.resolve()
    v_eur, fx = tx.dec("value_eur"), tx.dec("fx_rate")
    if v_eur is not None and fx and tx.value("net_ccy") == "EUR" and tx.dec("net") is not None and ccy != "EUR":
        wht = tx.dec("withholding_tax") or Decimal(0)
        tax_eur = _money(((tx.dec("tax") or Decimal(0)) + wht) / fx) if tx.value("withholding_tax_ccy") != "EUR" \
            else _money(tx.dec("tax") or Decimal(0)) + wht
        sign = 1 if tx.kind == "buy" else -1
        net = tx.dec("net") or Decimal(0)
        if abs(v_eur + sign * (_money(fee / fx) if fee else Decimal(0)) - (tax_eur if sign < 0 else 0) - net) \
                > CENT * 3:
            tx.warnings.append(f"EUR-Gegenprobe: {v_eur} EUR {'+' if sign > 0 else '−'} Gebühren/Steuern ≠ "
                               f"ausmachender Betrag {net} EUR – Devisenkurs bzw. Abzüge prüfen")
    fee_ccy = tx.value("fee_ccy") or ccy
    if tx.dec("fee") is not None and fee_ccy == "EUR":
        tx.evidence.append(_derived("fee_eur", _money(tx.dec("fee") or 0), sha, "Gebühr in EUR laut Beleg",
                                    status="belegt"))
    elif tx.dec("fee") is not None and fee_ccy and fee_ccy != "EUR" and tx.dec("fx_rate"):
        fx = tx.dec("fx_rate") or Decimal(1)
        tx.evidence.append(_derived("fee_eur", _money((tx.dec("fee") or Decimal(0)) / fx), sha,
                                    f"Gebühr {tx.dec('fee')} {fee_ccy} mit Devisenkurs {fx} laut Beleg"))
    tx.resolve()
    # OCR-Unsicherheit: Wert mit geringer Konfidenz → Prüfhinweis
    for name in ("quantity", "gross", "net", "price", "fee", "date", "isin", "txhash"):
        d = tx.decisions.get(name)
        if d is not None and d.selected is not None and d.selected.conf is not None \
                and d.selected.conf < OCR_MIN_CONF:
            tx.warnings.append(f"{FIELD_LABEL.get(name, name)}: OCR-Konfidenz {d.selected.conf:.0f} % – Wert prüfen")
    for name in tx.conflicts():
        vals = {tx.decisions[name].selected.value, *(c.value for c in tx.decisions[name].conflicts)}  # type: ignore[union-attr]
        tx.warnings.append(f"{FIELD_LABEL.get(name, name)}: widersprüchliche Angaben im Beleg "
                           f"({', '.join(sorted(vals))})")


# ----------------------------------------------------------------------------------------------------
# Einstieg
# ----------------------------------------------------------------------------------------------------

def analyze(doc: DocumentResult) -> Analysis:
    texts = [ln.text for ln in doc.lines()]
    full = "\n".join(texts)
    conv = P.convention(texts)
    an = Analysis(detect_type(full), detect_provider(full), conv)
    if conv.decimal is None:
        an.warnings.append(f"Zahlenformat nicht eindeutig ({conv.evidence}) – mehrdeutige Zahlen bleiben ungelöst.")
    table = _table_txs(doc, an)
    if len(table) >= 1 and an.doc_type not in ("securities_trade", "dividend"):
        an.doc_type = "statement"
        an.txs = table
    elif an.doc_type in ("securities_trade", "dividend", "wallet_tx"):
        kind = "dividend" if an.doc_type == "dividend" else (_action(full) or "unknown")
        if an.doc_type == "wallet_tx" and kind not in ("deposit", "withdrawal"):
            kind = {"buy": "deposit", "sell": "withdrawal"}.get(kind, kind)
        an.txs = [_kv_tx(doc, an, kind, doc.lines(), 1)]
    else:
        single = _kv_tx(doc, an, _action(full) or "unknown", doc.lines(), 1)
        names = {e.field for e in single.evidence}
        lines = _line_txs(doc, an)
        if len(lines) > 1:
            an.txs = lines
        elif {"quantity", "date"} <= names and (names & {"gross", "net", "price"} or an.provider) \
                and _action(full):
            an.doc_type = "crypto_trade" if "isin" not in names else "securities_trade"
            an.txs = [single]
        elif lines:
            an.txs = lines
        elif names & {"quantity", "gross", "net", "txhash", "isin"}:
            an.txs = [single]
    for tx in an.txs:
        if tx.kind == "unknown":
            tx.warnings.append("Vorgangsart nicht erkennbar (Kauf/Verkauf/Eingang/Ausgang) – manuell festlegen")
        else:
            tx.evidence.append(FieldEvidence("kind", tx.kind, "document", f"document:{doc.sha256}",
                                             "Dokument", "belegt", f"Vorgangsart: {KIND_LABEL.get(tx.kind, tx.kind)}"))
        _validate(tx, doc.sha256)
    acc = next((e.value for tx in an.txs for e in tx.evidence if e.field == "account"), None)
    an.account_label = acc
    return an


def required_fields(kind: str, doc_type: str) -> set[str]:
    """Pflichtangaben für eine buchungsfähige Zeile (ohne sie: ungelöst, nie geraten)."""
    if kind in ("buy", "sell"):
        return {"date", "quantity", "gross"}
    if kind == "dividend":
        return {"date", "gross"}
    if kind in ("deposit", "withdrawal"):
        return {"date", "quantity"}
    if kind == "trade":
        return {"date", "quantity"}
    return {"date", "quantity", "kind"}


def parse_date(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def parse_time(v: str | None) -> time | None:
    try:
        return time.fromisoformat(v) if v else None
    except ValueError:
        return None
