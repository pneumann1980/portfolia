"""Read-only preview for M25 PDF and screenshot evidence extraction."""
from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.documentimport.candidates import candidates
from app.documentimport.evidence import FieldEvidence, resolve_fields
from app.documentimport.extract import DocumentError, extract_document, field_evidence

router = APIRouter()


def _page(body: str) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Dokumentanalyse – Portfolia</title></head><body>'
        '<main style="max-width:900px;margin:3rem auto;font:16px system-ui">'
        '<p><a href="/journal/csv">← Import</a></p><h1>Dokumentanalyse (M25-Vorschau)</h1>'
        '<p>Nur lokale Belegextraktion. Es werden keine Buchungen erstellt oder verändert.</p>'
        + body + '</main></body></html>'
    )


@router.get("/journal/documents", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    token = html.escape(getattr(request.state, "csrf_token", ""), quote=True)
    return _page(
        '<form method="post" enctype="multipart/form-data">'
        f'<input type="hidden" name="csrf_token" value="{token}">'
        '<input type="file" name="files" multiple accept=".pdf,.png,.jpg,.jpeg,.webp" required>'
        '<button type="submit">Lokal analysieren</button></form>'
    )


@router.post("/journal/documents", response_class=HTMLResponse)
async def preview(request: Request) -> HTMLResponse:
    form = await request.form(max_files=20, max_fields=10)
    uploaded = form.getlist("files")
    if not uploaded or len(uploaded) > 20 or not all(isinstance(f, UploadFile) for f in uploaded):
        return _page("<p>Bitte höchstens 20 gültige Dateien auswählen.</p>")
    output = []
    for file in uploaded:
        name = html.escape((file.filename or "Dokument")[:120])
        try:
            data = await file.read(25 * 1024 * 1024 + 1)
            doc = await run_in_threadpool(extract_document, data)
            evidence = field_evidence(doc)
            transactions = candidates(doc)
            field_candidates = [FieldEvidence(field, item.value, "document", item.source,
                                        item.location, item.status, item.reason)
                          for field, items in evidence.items() for item in items[:25]]
            decisions = resolve_fields(field_candidates)
            found = []
            for field, decision in decisions.items():
                for item in decision.alternatives:
                    found.append(
                        f"<li><strong>{html.escape(field)}:</strong> {html.escape(item.value)} "
                        f"({html.escape(item.location)}; {html.escape(item.status)}) – "
                        "unbestätigter Kandidat</li>"
                    )
                if decision.conflicts:
                    found.append("<li>Widerspruch: mehrere unterschiedliche Werte; manuelle Prüfung nötig.</li>")
            counts = f"{len(doc.pages)} Seite(n), Typ {html.escape(doc.file_type)}, SHA256 {doc.sha256}"
            preview = [f"<li>{html.escape(tx.kind)}: " + ", ".join(
                f"{html.escape(v.name)}={html.escape(v.value)}" for v in tx.fields
            ) + " – Prüfung erforderlich</li>" for tx in transactions]
            output.append(
                f"<section><h2>{name}</h2><p>{counts}</p>"
                + ("<h3>Vorgangskandidaten (keine Buchungen)</h3><ul>" + "".join(preview) + "</ul>"
                   if preview else "<p>Keine erkennbaren Vorgänge.</p>")
                + ("<ul>" + "".join(found) + "</ul>" if found else "<p>Keine sicheren Feldkandidaten gefunden.</p>")
                + "<p>Keine Buchungsfreigabe. Fachliche Prüfung erforderlich.</p></section>"
            )
        except DocumentError as exc:
            output.append(f"<section><h2>{name}</h2><p>Analyse nicht möglich: {html.escape(str(exc))}</p></section>")
        finally:
            await file.close()
    return _page("".join(output) + '<p><a href="/journal/documents">Weitere Dateien analysieren</a></p>')
