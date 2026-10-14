"""Prüfstatus der Importprüfung – die eine Quelle für „offen“, „abgeschlossen“ und alle Zähler.

Vorher gab es sieben unterschiedliche Definitionen (Zeile, Stapelgruppe, Datenquelle, Stapelabschluss, Verwerfen,
Warteschlange, Datenhinweis); dieselben Zeilen ergaben je Ansicht andere Zahlen. Jetzt leiten Stapelseite,
Prüfwarteschlange, Datenquellenübersicht und Stapelabschluss ihren Stand nur noch aus :func:`state_of` ab.

Zustand je Zeile (``status`` der Auswertung + ``decision`` des Nutzers + Abgleich ``match``):

========================  ==============================================================  ===========
Zustand                   Bedeutung                                                       offen?
========================  ==============================================================  ===========
``offen``                 Prüfung erforderlich (ungeklärt, unvollständig, Dublettenverdacht,  ja
                          mittlere Sicherheit, Widerspruch, komplex)
``sicher``                sicherer Vorschlag bereit (Dublette/Ergänzung verknüpfen bzw. neu  ja
                          übernehmen, Sicherheit „sicher“/„hoch“)
``vorgemerkt``            Übernehmen gewählt, aber noch nicht gebucht                     ja
``zurueckgestellt``       bewusst zurückgestellt – bleibt offen                           ja
``beruecksichtigt``       bereits vorhanden (gleiche Kennung bzw. Blockchain-Transaktion)  nein
``verknuepft``            mit vorhandener Buchung verknüpft                               nein
``uebernommen``           neu gebucht (auch als Transfer)                                 nein
``nicht_uebernommen``     ausdrücklich nicht übernommen                                   nein
``ignoriert``             dauerhaft ignoriert                                             nein
``historie``              vor dem Stichtag – maßgeblich ist der kuratierte Import         nein
========================  ==============================================================  ===========

Zähleinheit ist der **wirtschaftliche Vorgang** (Ereignis-ID der Quelle; Zeilen ohne Ereignis-ID zählen einzeln):
Ein Bitpanda-Trade mit Gebührenzeile ist ein Vorgang, nicht zwei. Ein Vorgang ist offen, solange eine seiner Zeilen
offen ist. Ein Stapel ist abgeschlossen, wenn keiner seiner Vorgänge offen ist – eine Nullanzeige der automatischen
Vorschläge ist dafür kein Nachweis.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SAFE_CONF = ("sicher", "hoch")
STATES: dict[str, tuple[str, str]] = {  # Zustand → (Bezeichnung, Badge)
    "offen": ("Prüfung erforderlich", "warn"),
    "sicher": ("sicherer Vorschlag bereit", "good"),
    "vorgemerkt": ("zur Übernahme vorgemerkt", "info"),
    "zurueckgestellt": ("zurückgestellt", "info"),
    "beruecksichtigt": ("bereits berücksichtigt", ""),
    "verknuepft": ("mit vorhandener Buchung verknüpft", ""),
    "uebernommen": ("übernommen", ""),
    "nicht_uebernommen": ("ausdrücklich nicht übernommen", ""),
    "ignoriert": ("bewusst ignoriert", ""),
    "historie": ("vor dem Stichtag (kuratierter Import maßgeblich)", ""),
}
OPEN = ("offen", "zurueckgestellt", "vorgemerkt", "sicher")  # Reihenfolge = Vorrang innerhalb eines Vorgangs
CLOSED = ("uebernommen", "verknuepft", "beruecksichtigt", "nicht_uebernommen", "ignoriert", "historie")
_RANK = {s: i for i, s in enumerate((*OPEN, *CLOSED))}
REASON = {  # Grund des Prüfbedarfs je Zeilenstatus (offene Vorgänge)
    "unclear": "ungeklärte Vorgangsart", "invalid": "unvollständig (Asset, Konto bzw. EUR-Wert)",
    "duplicate": "mögliche Dublette", "new": "neu, noch nicht übernommen", "before": "vor Stichtag, Übernahme gewählt",
}


def is_safe(status: str, match: Mapping[str, Any] | None) -> bool:
    """Sicherer Vorschlag: Dublette/Ergänzung verknüpfen bzw. neu übernehmen, Sicherheit „sicher“ oder „hoch“."""
    if status not in ("new", "duplicate") or not match:
        return False
    if match.get("conf") not in SAFE_CONF:
        return False
    if match.get("cat") in ("dublette", "ergaenzung"):
        return match.get("action") == "link"
    return match.get("cat") == "neu" and match.get("action") == "include" and status == "new"


def state_of(status: str, decision: str | None, match: Mapping[str, Any] | None = None) -> str:
    """Zustand einer Prüfzeile – die einzige Stelle, die „offen“ definiert."""
    if status in ("committed", "merged"):
        return "uebernommen"
    if status == "linked":
        return "verknuepft"
    if status == "known":
        return "beruecksichtigt"
    if status == "ignored":
        return "ignoriert"
    if decision == "skip":
        return "nicht_uebernommen"
    if decision == "defer":
        return "zurueckgestellt"
    if decision == "include":
        return "vorgemerkt"
    if status == "before":
        return "historie"
    if is_safe(status, match):
        return "sicher"
    return "offen"


def is_open(state: str) -> bool:
    return state in OPEN


def event_key(batch_id: int, event: str | None, idx: int) -> tuple[int, str]:
    return (batch_id, event or f"#{idx}")


@dataclass
class Lite:
    """Prüfzeile ohne vollständige Auswertung (für Zähler über viele Stapel)."""

    batch_id: int
    idx: int
    status: str
    decision: str | None
    event: str | None
    cat: str | None
    action: str | None
    conf: str | None
    datasource_id: int | None = None
    source: str = ""
    rid: int = 0
    ts: str = ""  # Zeitpunkt laut Quelle (ISO; leer, wenn die Quelle keinen liefert)
    account: str = ""  # Konto laut Zeile bzw. Stapel
    kind: str = ""  # Vorgangsart der Quelle (Zwischenformat)

    @property
    def state(self) -> str:
        m = {"cat": self.cat, "action": self.action, "conf": self.conf} if self.cat else None
        return state_of(self.status, self.decision, m)


@dataclass
class Summary:
    """Zähler über Prüfzeilen: Vorgänge (wirtschaftliche Einheit) und Zeilen getrennt."""

    events: Counter[str] = field(default_factory=Counter)  # Zustand → Anzahl Vorgänge
    rows: Counter[str] = field(default_factory=Counter)  # Zustand → Anzahl Zeilen
    reasons: Counter[str] = field(default_factory=Counter)  # Prüfgrund offener Vorgänge → Anzahl
    by_batch: dict[int, int] = field(default_factory=dict)  # Stapel → offene Vorgänge
    history_gaps: int = 0  # vor dem Stichtag, im kuratierten Import nicht gefunden (Hinweis, keine Entscheidung)

    @property
    def open(self) -> int:
        return sum(self.events[s] for s in OPEN)

    @property
    def to_review(self) -> int:
        """Offen ohne sicheren Vorschlag (manuell zu entscheiden)."""
        return self.events["offen"] + self.events["zurueckgestellt"]

    @property
    def closed(self) -> int:
        return sum(self.events[s] for s in CLOSED)

    @property
    def total(self) -> int:
        return sum(self.events.values())

    @property
    def batches(self) -> list[int]:
        return sorted((b for b, n in self.by_batch.items() if n), reverse=True)


def summarize(rows: Iterable[Any]) -> Summary:
    """Zähler aus Zeilen (``Lite`` oder ``RowCtx``) – je Vorgang zählt der dringlichste Zustand seiner Zeilen."""
    s = Summary()
    best: dict[tuple[int, str], tuple[str, Any]] = {}
    for r in rows:
        st = r.state if isinstance(r, Lite) else row_state(r)
        s.rows[st] += 1
        bid = int(getattr(r, "batch_id", 0) or 0)  # RowCtx: ein Stapel
        key = event_key(bid, r.event if isinstance(r, Lite) else r.rec.event_key, r.idx)
        cur = best.get(key)
        if cur is None or _RANK[st] < _RANK[cur[0]]:
            best[key] = (st, r)
        s.by_batch.setdefault(bid, 0)
    for (bid, _ev), (st, r) in best.items():
        s.events[st] += 1
        if st in OPEN:
            s.by_batch[bid] = s.by_batch.get(bid, 0) + 1
            if st in ("offen", "zurueckgestellt"):
                s.reasons[_reason(r)] += 1
        elif st == "historie" and _cat(r) == "neu":
            s.history_gaps += 1
    return s


def _cat(r: Any) -> str | None:
    return r.cat if isinstance(r, Lite) else (getattr(r, "match", None) or {}).get("cat")


def _reason(r: Any) -> str:
    status = r.status
    if getattr(r, "decision", None) == "defer":
        return "zurückgestellt"
    cat = _cat(r)
    if status == "duplicate" or cat in ("widerspruch", "komplex"):
        return {"widerspruch": "widersprüchlich / unklar", "komplex": "komplexe Zuordnung"}.get(cat or "",
                                                                                            "mögliche Dublette")
    if status == "new" and cat in ("dublette", "ergaenzung"):
        return "möglicherweise vorhanden (mittlere Sicherheit)"
    return REASON.get(status, status)


def row_state(rc: Any) -> str:
    """Zustand einer ausgewerteten Zeile (``RowCtx``)."""
    return state_of(rc.status, rc.decision, getattr(rc, "match", None))


# ----------------------------------------------------------------------------------------------------
# Datenbank: Zähler ohne vollständige Auswertung
# ----------------------------------------------------------------------------------------------------

_SQL = ("SELECT r.id, r.batch_id, r.idx, r.status, r.decision, r.event_key, "
        "json_extract(r.messages, '$.match.cat') AS cat, json_extract(r.messages, '$.match.action') AS action, "
        "json_extract(r.messages, '$.match.conf') AS conf, json_extract(r.rec_json, '$.ts') AS ts, "
        "json_extract(r.rec_json, '$.ts_missing') AS ts_missing, json_extract(r.rec_json, '$.account') AS acc, "
        "json_extract(r.rec_json, '$.kind') AS rkind, b.account AS bacc, "
        "b.datasource_id, b.source, b.kind, b.profile FROM csv_row r JOIN csv_batch b ON b.id = r.batch_id ")


def load(db: Any, where: str = "", params: Iterable[Any] = ()) -> list[Lite]:
    out = []
    for r in db.q(_SQL + (f"WHERE {where} " if where else "") + "ORDER BY r.batch_id, r.idx", tuple(params)):
        src = r["source"] or (f"csv:{r['profile']}" if r["profile"] else "csv")
        out.append(Lite(int(r["batch_id"]), int(r["idx"]), r["status"], r["decision"], r["event_key"], r["cat"],
                        r["action"], r["conf"], r["datasource_id"], src, int(r["id"]),
                        "" if r["ts_missing"] else str(r["ts"] or ""), str(r["acc"] or r["bacc"] or ""),
                        str(r["rkind"] or "")))
    return out


ACTIVE = "b.status IN ('preview', 'partial')"


def batch_summary(db: Any, bid: int) -> Summary:
    return summarize(load(db, "r.batch_id=?", (bid,)))


def batch_closed(db: Any, bid: int) -> bool:
    return batch_summary(db, bid).open == 0


def source_summary(db: Any, sid: int) -> Summary:
    """Offene Vorgänge aller aktiven Prüf-Stapel einer Datenquelle (über Stapel hinweg, je Vorgang einmal)."""
    return summarize(load(db, f"b.datasource_id=? AND {ACTIVE}", (sid,)))


def active_summary(db: Any) -> Summary:
    return summarize(load(db, ACTIVE))


def by_source(db: Any) -> dict[int, Summary]:
    """Je Datenquelle die Zähler aller aktiven Stapel (eine Abfrage für die Übersicht)."""
    groups: dict[int, list[Lite]] = defaultdict(list)
    for r in load(db, f"b.datasource_id IS NOT NULL AND {ACTIVE}"):
        groups[int(r.datasource_id)].append(r)  # type: ignore[arg-type]
    return {sid: summarize(rows) for sid, rows in groups.items()}


# ----------------------------------------------------------------------------------------------------
# Verarbeitungsschritte eines Stapels (Nullanzeigen erklären)
# ----------------------------------------------------------------------------------------------------

def funnel(batch: Any, rows: list[Any], summary_json: Mapping[str, Any]) -> dict[str, Any]:
    """Nachvollziehbare Zahlen je Verarbeitungsschritt und – bei Nullergebnissen – der zutreffende Grund.

    Einheiten: ``rows_*`` sind Dateizeilen bzw. Vorgangszeilen, ``events_*`` wirtschaftliche Vorgänge."""
    s = summarize(rows)
    safe_link = sum(1 for rc in rows if row_state(rc) == "sicher" and (rc.match or {}).get("action") == "link")
    safe_new = sum(1 for rc in rows if row_state(rc) == "sicher" and (rc.match or {}).get("action") == "include")
    manual = s.rows["offen"] + s.rows["zurueckgestellt"]
    missing_asset = sum(1 for rc in rows if row_state(rc) in OPEN and rc.status == "invalid"
                        and any("Asset" in e or "Symbol" in e for e in rc.errors))
    missing_value = sum(1 for rc in rows if row_state(rc) in OPEN and rc.status == "invalid"
                        and any(e.startswith("EUR-Wert fehlt") for e in rc.errors))
    missing_account = sum(1 for rc in rows if row_state(rc) in OPEN and rc.status == "invalid"
                          and any("Konto" in e for e in rc.errors))
    out = {
        "rows_read": int(summary_json.get("rows_read") or len(rows)),
        "rows_valid": len(rows),
        "rows_unreadable": int(summary_json.get("error_count") or 0),
        "events": s.total,
        "events_open": s.open,
        "events_closed": s.closed,
        "events_known": s.events["beruecksichtigt"] + s.events["verknuepft"],
        "events_history": s.events["historie"],
        "history_gaps": s.history_gaps,
        "safe_link_rows": safe_link,
        "safe_new_rows": safe_new,
        "manual_events": s.to_review,
        "manual_rows": manual,
        "missing_asset": missing_asset,
        "missing_value": missing_value,
        "missing_account": missing_account,
        "evaluated": bool(summary_json.get("eval_v")) or batch["kind"] != "sync" or bool(rows),
        "error": (summary_json.get("eval_error") or ""),
        "summary": s,
    }
    out["zero_reason"] = zero_reason(out, batch)
    return out


def zero_reason(f: Mapping[str, Any], batch: Any) -> str:
    """Warum stehen keine sicheren Aktionen bzw. keine offenen Vorgänge an? „0 sichere Aktionen“ ist nicht „0
    offene Probleme“."""
    if f.get("error"):
        return f"Auswertung fehlgeschlagen: {f['error']}"
    if batch["status"] == "mapping":
        return "Noch keine Auswertung durchgeführt – zuerst das Dateiformat zuordnen."
    if not f["rows_valid"]:
        if f["rows_unreadable"]:
            return "Keine gültigen Transaktionsdaten erkannt – alle Zeilen sind unlesbar."
        return "Keine gültigen Transaktionsdaten erkannt."
    if f["safe_link_rows"] or f["safe_new_rows"]:
        return ""
    if f["events_open"]:
        parts = []
        if f["missing_asset"]:
            parts.append(f"{f['missing_asset']} ohne Asset-Zuordnung")
        if f["missing_account"]:
            parts.append(f"{f['missing_account']} ohne Konto-Zuordnung")
        if f["missing_value"]:
            parts.append(f"{f['missing_value']} ohne EUR-Wert")
        return ("Keine sicheren Vorschläge: alle offenen Vorgänge benötigen eine manuelle Prüfung"
                + (f" ({', '.join(parts)})" if parts else "") + ".")
    if f["events_history"] and f["events_history"] + f["events_known"] == f["events"]:
        return ("Alle Vorgänge liegen vor dem Stichtag bzw. sind bereits vorhanden – maßgeblich ist der kuratierte "
                "Import; keine Entscheidung nötig.")
    if f["events_known"] == f["events"]:
        return "Alle Vorgänge sind bereits vorhanden – nichts zu übernehmen."
    return "Alle Vorgänge sind abgeschlossen (übernommen, verknüpft, vorhanden, nicht übernommen oder ignoriert)."
