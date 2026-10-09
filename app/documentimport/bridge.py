"""Beleg → Zwischenformat der Import-Pipeline (:class:`app.csvimport.model.Rec`).

Belegte Vorgänge werden als Zeilen im einheitlichen Format (``DIRECT``) übergeben – Kurswert, Gebühr in EUR und
Wertpapierbezug (``related_asset``) bleiben so exakt, wie der Beleg sie nennt. Asset- und Kontozuordnung, Bewertung,
Dubletten-, Transfer- und Quellenabgleich übernimmt der bestehende Prüf-Stapel unverändert.

Regeln
* **Pflichtangaben fehlen** → Zeile ``REVIEW`` („ungeklärt“, nie buchbar) mit allen erkannten Feldern.
* **Geschätzt, widersprüchlich, OCR-unsicher, Plausibilitätswarnung** → ``Rec.review`` (nie automatisch, nicht in
  der Sammelübernahme „sicher“). Ein geschätzter EUR-Wert wird **nicht** als Buchungswert eingetragen.
* **Identität:** Anbieter-ID bzw. Hash des Belegs als Ereignis-ID/Alias – derselbe Beleg (auch erneut hochgeladen,
  auch als geänderte Fassung) bzw. derselbe Börsenvorgang wird vom Abgleich als bekannt erkannt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.csvimport.model import DIRECT, REVIEW, Rec
from app.documentimport.enrich import Enriched, ts_of, tx_keys
from app.documentimport.profiles import DOC_LABEL, FIELD_LABEL, KIND_LABEL, required_fields

GOOD = ("belegt", "rekonstruiert")


def _s(v: Decimal | None) -> str:
    if v is None:
        return ""
    return format(v.normalize(), "f") if v != v.to_integral() else format(v.quantize(Decimal(1)), "f")


def event_key(e: Enriched, sha: str) -> tuple[str, list[str]]:
    """Ereignis-ID des Belegvorgangs (stabil über erneute Uploads) und Aliase (Anbieter-ID des Originalvorgangs)."""
    keys = sorted(tx_keys(e.tx))
    native = [k for k in keys if not k.startswith("h:")]
    if native:
        return f"doc:{native[0]}", native
    if keys:
        return f"doc:{keys[0]}", []
    ext = e.tx.value("ext_id")
    if ext:
        return f"doc:{(e.tx.provider or 'beleg')}:{ext}"[:200], []
    return f"doc:{sha[:16]}:{e.tx.n}", []


def _alternatives(d: Any, sel: Any) -> list[dict[str, Any]]:
    """Andere Werte für dasselbe Feld (gleiche Werte an weiteren Fundstellen sind keine Alternative)."""
    out: list[dict[str, Any]] = []
    seen = {sel.value} if sel is not None else set()
    for a in d.alternatives:
        if a is sel or a.value in seen:
            continue
        seen.add(a.value)
        out.append({"value": a.value, "origin": a.origin, "status": a.status, "where": a.location,
                    "reason": a.reason[:160]})
    return out[:5]


def provenance(e: Enriched, doc: dict[str, Any]) -> dict[str, Any]:
    """Herkunft je Feld (für ``Rec.raw`` und die Prüfoberfläche) – ohne Volltext des Dokuments."""
    fields: dict[str, Any] = {}
    for name, d in sorted(e.tx.decisions.items()):
        sel = d.selected
        fields[name] = {
            "label": FIELD_LABEL.get(name, name),
            "value": sel.value if sel else None,
            "status": sel.status if sel else "ungeloest",
            "origin": sel.origin if sel else None,
            "source": sel.source_ref if sel else None,
            "where": sel.location if sel else None,
            "page": sel.page if sel else None,
            "box": list(sel.box) if sel and sel.box else None,
            "conf": sel.conf if sel else None,
            "reason": (sel.reason if sel else d.unresolved_reason)[:300],
            "conflicts": [{"value": c.value, "origin": c.origin, "where": c.location, "reason": c.reason[:200]}
                          for c in d.conflicts][:5],
            "alternatives": _alternatives(d, sel),
        }
    return {"document": doc, "kind": e.tx.kind, "doc_type": e.tx.doc_type, "provider": e.tx.provider,
            "fields": fields, "warnings": e.tx.warnings[:20], "asset": e.asset_id,
            "asset_candidates": e.asset_candidates[:10],
            "existing": e.existing.tx_id if e.existing is not None else None,
            "sources": e.tx.sources,
            "steps": [{"stage": s.stage, "field": s.field, "source": s.source, "query": s.query, "result": s.result,
                       "ok": s.ok} for s in e.steps][:40]}


def missing(e: Enriched) -> list[str]:
    tx = e.tx
    need = required_fields(tx.kind, tx.doc_type)
    if tx.kind in ("buy", "sell", "dividend", "deposit", "withdrawal") and not (
            e.asset_id or tx.value("isin") or tx.value("symbol") or tx.kind == "dividend"):
        need = need | {"symbol"}
    if tx.kind == "dividend" and not (tx.value("isin") or tx.value("asset") or e.asset_id):
        need = need | {"isin"}
    if tx.kind in ("buy", "sell", "dividend") and not (tx.value("ccy") or tx.value("price_ccy")):
        need = need | {"ccy"}  # Währung nie annehmen (kein stilles „EUR“)
    return sorted(n for n in need if tx.status(n) not in GOOD)


def review_reasons(e: Enriched) -> list[str]:
    tx = e.tx
    out = list(dict.fromkeys(tx.warnings))
    for name in ("value_eur", "fee_eur", "time", "fee", "network_fee"):
        if tx.status(name) == "geschaetzt":
            d = tx.decisions[name].selected
            out.append(f"{FIELD_LABEL.get(name, name)} nur geschätzt ({d.reason if d else ''})")
    if tx.kind in ("buy", "sell", "dividend") and tx.status("value_eur") not in GOOD:
        out.append("EUR-Gegenwert nicht belegt – wird erst nach Prüfung bewertet (Schätzung möglich)")
    if e.asset_id is None and len(e.asset_candidates) > 1:
        out.append("Asset mehrdeutig: " + ", ".join(e.asset_candidates[:5]))
    if any(d.selected is not None and d.selected.origin == "public" for d in tx.decisions.values()):
        out.append("enthält Angaben aus öffentlichen Quellen – bitte bestätigen")
    return out


def to_recs(e: Enriched, *, sha: str, doc: dict[str, Any], account: str, n0: int) -> list[Rec]:
    """Rec-Zeilen eines Belegvorgangs (ein Ereignis, ggf. mehrere Zeilen: Dividende + Steuern)."""
    tx = e.tx
    ek, aliases = event_key(e, sha)
    ts, date_only, ts_why = ts_of(tx)
    raw = provenance(e, doc)
    raw["ts_reason"] = ts_why
    label = f"Beleg: {KIND_LABEL.get(tx.kind, tx.kind)} ({DOC_LABEL.get(tx.doc_type, tx.doc_type)})"
    gaps = missing(e)
    h = tx.value("txhash")
    base = {"line": n0, "ts": ts or datetime(1970, 1, 1, tzinfo=UTC), "event_key": ek, "aliases": aliases,
            "txhash": h, "raw": raw, "label": label, "date_only": bool(date_only and ts is not None),
            "ts_missing": ts is None, "account": account}
    if gaps or tx.kind in ("unknown", "trade"):
        why = ("fehlt: " + ", ".join(FIELD_LABEL.get(g, g) for g in gaps)) if gaps else \
            "Vorgangsart nicht eindeutig abbildbar"
        return [Rec(kind=REVIEW, ext_id=f"{ek}#0", event_line=0, note=f"Unvollständiger Beleg – {why}"[:300],
                    review=why, **base)]
    reasons = review_reasons(e)
    sym = e.asset_id or tx.value("isin") or tx.value("symbol") or ""
    q, gross = tx.dec("quantity"), tx.dec("gross")
    ccy = tx.value("ccy") or tx.value("price_ccy") or "EUR"
    v_eur = tx.dec("value_eur") if tx.status("value_eur") in GOOD else None
    fee, fee_ccy = tx.dec("fee"), tx.value("fee_ccy") or ccy
    fee_eur = tx.dec("fee_eur") if tx.status("fee_eur") in GOOD else None
    row: dict[str, str] = {}
    lines: list[tuple[dict[str, str], str]] = []
    note = f"Beleg {doc.get('filename', '')} (SHA {sha[:12]})"
    if tx.kind == "buy":
        row = {"type": "buy", "from_account": account, "from_asset": ccy, "from_qty": _s(gross),
               "to_account": account, "to_asset": sym, "to_qty": _s(q)}
    elif tx.kind == "sell":
        row = {"type": "sell", "from_account": account, "from_asset": sym, "from_qty": _s(q),
               "to_account": account, "to_asset": ccy, "to_qty": _s(gross)}
    elif tx.kind == "dividend":
        qty_eur = v_eur if v_eur is not None else gross
        row = {"type": "deposit", "tag": "dividend", "to_account": account,
               "to_asset": "EUR" if v_eur is not None else ccy, "to_qty": _s(qty_eur), "related_asset": sym}
        fx = tx.dec("fx_rate")
        for name, tag in (("withholding_tax", "withholding_tax"), ("tax", "tax")):
            amt = tx.dec(name)
            if amt is None or tx.status(name) not in GOOD:
                continue
            t_ccy = tx.value(f"{name}_ccy") or ccy
            eur = amt if t_ccy == "EUR" else (amt / fx).quantize(Decimal("0.01")) if fx else None
            lines.append(({"type": "withdrawal", "tag": tag, "from_account": account,
                           "from_asset": "EUR" if eur is not None else t_ccy, "from_qty": _s(eur if eur is not None
                                                                                             else amt),
                           "value_eur": _s(eur) if eur is not None else "", "related_asset": sym},
                          f"{FIELD_LABEL.get(name)} zur Dividende"))
    elif tx.kind == "deposit":
        row = {"type": "deposit", "to_account": account, "to_asset": sym, "to_qty": _s(q)}
    elif tx.kind == "withdrawal":
        row = {"type": "withdrawal", "from_account": account, "from_asset": sym, "from_qty": _s(q)}
        nf, nf_sym = tx.dec("network_fee"), tx.value("network_fee_sym")
        if nf and tx.status("network_fee") in GOOD and (nf_sym or tx.value("symbol")) == tx.value("symbol"):
            row |= {"fee_asset": sym, "fee_qty": _s(nf)}
    if fee and tx.kind in ("buy", "sell") and tx.status("fee") in GOOD:
        row |= {"fee_asset": fee_ccy, "fee_qty": _s(fee), "fee_eur": _s(fee_eur) if fee_eur is not None else ""}
    if (v_eur is not None and tx.kind != "dividend") or (tx.kind == "dividend" and v_eur is not None):
        row["value_eur"] = _s(v_eur)
    if ccy != "EUR" and tx.kind in ("buy", "sell") and tx.dec("price") is not None:
        row |= {"orig_price": _s(tx.dec("price")), "orig_ccy": ccy}
    row["note"] = note
    out = [Rec(kind=DIRECT, row=row, ext_id=f"{ek}#0", event_line=0, review="; ".join(reasons)[:500] or None,
               note=note, **base)]
    for i, (extra, why) in enumerate(lines, 1):
        extra["note"] = f"{why} · {note}"
        b = dict(base)
        b["line"] = n0 + i
        out.append(Rec(kind=DIRECT, row=extra, ext_id=f"{ek}#{i}", event_line=i, review=out[0].review,
                       note=extra["note"], **b))
    return out
