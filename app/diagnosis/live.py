"""Aktuelle Bestände problematischer Positionen abfragen (M28.1): Bestandsabweichungen und Integritätsbefunde werden mit
dem tatsächlichen Bestand laut Börsen- bzw. Wallet-API verglichen – das geht schneller, als Ursachen in Buchungen zu
suchen.

Die Abfrage nutzt die vorhandene Verbindungsprüfung der Datenquellen (``DatasourceService.check`` – liest nur Bestände,
keine Buchungen, nichts wird übernommen) im Hintergrund der Sammelaktualisierung. Das Ergebnis erscheint als „Ist“ zum
Abrufzeitpunkt in der Diagnose; Soll und Ist werden zum selben Zeitpunkt verglichen (nie ein aktueller Bestand gegen
einen späteren Buchungsstand). Quellenrang: API-Daten gelten vor Steuertool-Import und CSV (``audit.SOURCE_RANK``).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.datasources.service import datasource_service


def sources_for(ctx: Any, accounts: Iterable[str]) -> list[dict[str, Any]]:
    """Aktive Datenquellen mit automatischer Anbindung, die zu den Konten gehören (Name, Anbieter, letzter Abruf)."""
    wanted = {a for a in accounts if a}
    if not wanted:
        return []
    out = []
    for ds in datasource_service(ctx).list():
        if ds.supported and ds.enabled and ds.account in wanted:
            out.append({"id": int(ds.id), "name": ds.name, "provider": ds.provider_label, "account": ds.account,
                        "last_success_at": getattr(ds, "last_success_at", None)})
    return sorted(out, key=lambda x: (x["provider"], x["name"]))


def context(ctx: Any, accounts: Iterable[str], back: str) -> dict[str, Any]:
    svc = datasource_service(ctx)
    return {"sources": sources_for(ctx, accounts), "back": back, "batch": svc.batch_progress()}


def start(ctx: Any, accounts: Iterable[str], back: str) -> dict[str, Any]:
    """Bestandsabfrage der zugehörigen Datenquellen im Hintergrund starten (``{"started", "count"}`` bzw. ``error``)."""
    srcs = sources_for(ctx, accounts)
    if not srcs:
        return {"error": "Für diese Konten gibt es keine aktive Börsen- oder Wallet-Anbindung."}
    return datasource_service(ctx).start_sync_many([s["id"] for s in srcs], "problematische Positionen",
                                                   mode="check", back=back)


def explorer_links(ctx: Any) -> dict[str, str]:
    """Konto → Explorer-Link der Wallet-Adresse (öffnet der Nutzer selbst; Portfolia ruft ihn nie ab) – damit lässt sich
    der tatsächliche Bestand je Chain mit einem Klick gegenprüfen."""
    out: dict[str, str] = {}
    for ds in datasource_service(ctx).list():
        url = ds.explorer_url if ds.is_wallet else None
        if url and ds.account:
            out.setdefault(ds.account, url)
    return out
