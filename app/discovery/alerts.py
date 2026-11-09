"""Hinweise ganz oben auf der Unternehmensseite (0.110) – ohne KI, aus SEC-Einreichungen, Kennzahlen und Signalen.

* **Übernahme/Fusion:** SEC-Formulare, die nur bei einem Unternehmenszusammenschluss entstehen (Übernahmeangebot,
  Fusions-Abstimmung, Form 425) – belegt; zusätzlich Meldungen mit Übernahme-Begriffen – nur „gemeldet“, ungeprüft
  (Rolle Käufer/Ziel nicht bestimmt).
* **Sondereffekte:** wesentliche Einmalposten des letzten Geschäftsjahres (siehe ``fundamentals.special_items``).
* **Datenlücken:** geringe Abdeckung der Kernkennzahlen, alter Jahresabschluss, fehlender Kurs, mehrere Währungen.
* **Neuer SEC-Bericht:** jüngster Quartals-/Jahresbericht (für „KI-Bericht veraltet“).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.discovery.text import G, N, P
from app.util.timeutil import today_local

TAKEOVER_FORMS = {
    "SC 14D9": "Stellungnahme des Unternehmens zu einem Übernahmeangebot",
    "SC TO-T": "Übernahmeangebot eines Dritten",
    "PREM14A": "vorläufige Einladung zur Abstimmung über eine Fusion",
    "DEFM14A": "endgültige Einladung zur Abstimmung über eine Fusion",
    "425": "Mitteilung zu einem Unternehmenszusammenschluss",
}
PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A", "10-KT")
NEWS_WORDS = re.compile(r"übernahme|übernimmt|übernommen|fusion|merger|acquir|takeover|kaufangebot|akquisition|"
                        r"zusammenschluss|buyout", re.I)
TAKEOVER_DAYS = 180
NEWS_DAYS = 90
OLD_ANNUAL_DAYS = 16 * 31


def _recent(sub: dict[str, Any] | None) -> list[dict[str, str]]:
    rec = (((sub or {}).get("filings") or {}).get("recent") or {})
    forms, dates, accns = rec.get("form") or [], rec.get("filingDate") or [], rec.get("accessionNumber") or []
    return [{"form": str(f), "filed": str(d), "accn": str(a)} for f, d, a in zip(forms, dates, accns, strict=False)]


def latest_periodic(sub: dict[str, Any] | None) -> dict[str, str] | None:
    """Jüngster Quartals- oder Jahresbericht laut SEC-Einreichungsliste."""
    rows = [r for r in _recent(sub) if r["form"] in PERIODIC_FORMS]
    return max(rows, key=lambda r: r["filed"]) if rows else None


def takeover_filings(sub: dict[str, Any] | None, days: int = TAKEOVER_DAYS) -> list[dict[str, str]]:
    since = (today_local() - timedelta(days=days)).isoformat()
    out = [r | {"label": TAKEOVER_FORMS[r["form"]]} for r in _recent(sub)
           if r["form"] in TAKEOVER_FORMS and r["filed"] >= since]
    return sorted(out, key=lambda r: r["filed"], reverse=True)


def takeover_news(signals: list[dict[str, Any]], days: int = NEWS_DAYS) -> list[dict[str, Any]]:
    since = (today_local() - timedelta(days=days)).isoformat()
    return [s for s in signals if s.get("kind") == "nachricht" and str(s.get("published_at") or
                                                                     s.get("observed_at") or "")[:10] >= since
            and NEWS_WORDS.search(f"{s.get('title') or ''} {s.get('detail') or ''}")]


def _money(v: float | None, ccy: str) -> str:
    if v is None:
        return "–"
    a = abs(v)
    s = f"{N(v / 1e9, 2)} Mrd." if a >= 1e9 else f"{N(v / 1e6, 1)} Mio." if a >= 1e6 else G(round(v))
    return f"{s} {ccy}".strip()


def build(*, fundamentals: dict[str, Any], market: dict[str, Any], signals: list[dict[str, Any]],
          data_coverage: float, sub: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Hinweise in Anzeigereihenfolge: ``level`` (crit | warn | info), ``title``, ``text``, ``source``."""
    out: list[dict[str, Any]] = []
    tk = takeover_filings(sub)
    if tk:
        f = tk[0]
        more = f" (insgesamt {len(tk)} solche Einreichungen seit {tk[-1]['filed']})" if len(tk) > 1 else ""
        out.append({"level": "crit", "kind": "uebernahme", "title": "Übernahme/Fusion laut SEC-Einreichung",
                    "text": f"{f['label']} (Form {f['form']}, eingereicht {f['filed']}){more}. Der Kurs folgt dann "
                            "meist den Bedingungen des Angebots – Bewertung und Scores verlieren an Aussagekraft.",
                    "source": f"sec:{f['accn']}"})
    news = takeover_news(signals)
    if news:
        n = news[0]
        out.append({"level": "warn" if tk else "crit", "kind": "uebernahme_news",
                    "title": "Übernahme/Fusion in Meldungen erwähnt" + (" (ungeprüft)" if not tk else ""),
                    "text": f"„{n.get('title')}“ ({str(n.get('published_at') or n.get('observed_at') or '')[:10]}"
                            f"{', ' + n['source_name'] if n.get('source_name') else ''}). Ob das Unternehmen Käufer "
                            "oder Übernahmeziel ist, wird nicht automatisch bestimmt.",
                    "source": n.get("url") or ""})
    ccy = fundamentals.get("currency") or ""
    for it in market.get("special_items") or []:
        op = "im operativen Ergebnis enthalten" if it["in_operating"] else "nicht im operativen Ergebnis"
        out.append({"level": "warn", "kind": "sondereffekt",
                    "title": f"Sondereffekt Geschäftsjahr {it['period'][:4]}",
                    "text": f"{it['label']}: {_money(it['value'], ccy)} ({op}). "
                            + (f"Nettoergebnis bereinigt ≈ {_money(market.get('ni_adj'), ccy)} (vor Steuereffekt)"
                               + (f", KGV bereinigt {G(round(market['pe_adj'], 1))}×" if market.get("pe_adj") else "")
                               + "." if market.get("ni_adj") is not None else ""),
                    "source": f"sec:{it['accn']}" if it.get("accn") else ""})
    years = fundamentals.get("years") or []
    if not years:
        out.append({"level": "warn", "kind": "daten", "title": "Keine SEC-Kennzahlen",
                    "text": "Für dieses Unternehmen liegen keine Jahreskennzahlen (XBRL) vor – Scores bleiben "
                            "weitgehend offen.", "source": ""})
    else:
        if data_coverage < 0.7:
            out.append({"level": "warn", "kind": "daten", "title": "Datenlücke",
                        "text": f"Nur {P(data_coverage, 0)} der Kernkennzahlen der letzten fünf Jahre vorhanden – "
                                "Kriterien ohne Daten bleiben unbewertet.", "source": "K"})
        try:
            age = (today_local() - date.fromisoformat(years[-1])).days
        except ValueError:
            age = 0
        if age > OLD_ANNUAL_DAYS:
            out.append({"level": "warn", "kind": "daten", "title": "Alter Jahresabschluss",
                        "text": f"Letztes Geschäftsjahr endete am {years[-1]} – neuere Jahreszahlen fehlen.",
                        "source": "K"})
    if not market.get("price"):
        out.append({"level": "info", "kind": "daten", "title": "Kein Kurs",
                    "text": "Kurs bzw. Marktdaten nicht verfügbar – Bewertung und Rendite bleiben offen.",
                    "source": ""})
    for n in fundamentals.get("notes") or []:
        if "Währung" in n:
            out.append({"level": "info", "kind": "daten", "title": "Mehrere Berichtswährungen", "text": n,
                        "source": "K"})
    return out
