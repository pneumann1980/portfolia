"""News- und Video-Pipeline: Abruf → Qualitätsfilter → Asset-Zuordnung → Deduplizierung → Speicherung."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.context import AppContext
from app.ledger.models import AssetInfo
from app.news.config import Channel, Source, SourcesConfig
from app.news.dedupe import TitleIndex, normalize_title, normalize_url
from app.news.parse import RawItem, parse_binance, parse_bybit, parse_cryptopanic, parse_feed, parse_finnhub
from app.news.relevance import Matcher, is_clickbait
from app.news.youtube import YouTubeClient
from app.util.http import HttpError, RateLimiter, request_with_retry
from app.util.timeutil import iso, parse_iso

log = logging.getLogger(__name__)

EXAMPLE_SOURCES = Path(__file__).resolve().parent.parent.parent / "examples" / "sources.yaml"
HOST_INTERVALS = {"news.google.com": 2.5, "feeds.finance.yahoo.com": 1.2, "www.youtube.com": 1.0}
_LIVE_WORDS = ("live", "livestream", "live stream", "🔴")


@dataclass
class CycleStats:
    sources: int = 0
    requests: int = 0
    new_items: int = 0
    duplicates: int = 0
    hidden: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"sources": self.sources, "requests": self.requests, "new": self.new_items,
                "duplicates": self.duplicates, "hidden": self.hidden, "errors": self.errors[:15]}


class NewsService:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.db = ctx.db
        example = EXAMPLE_SOURCES if EXAMPLE_SOURCES.exists() else Path("/opt/portfolia/examples/sources.yaml")
        self.cfg = SourcesConfig(ctx.config.sources_path, example)
        self.http = ctx.http
        self.yt = YouTubeClient(self.http, self.db, ctx.config.secrets.youtube_api_key)
        self._limiters: dict[str, RateLimiter] = {}

    # -- Kontext ------------------------------------------------------------------------------------
    def held(self) -> tuple[list[AssetInfo], dict[str, float]]:
        val = self.ctx.valuation()
        if val is None:
            return [], {}
        assets = [p.asset for p in val.positions if not p.asset.is_fiat]
        weights = {p.asset_id: p.weight for p in val.positions}
        return assets, weights

    def matcher(self) -> tuple[Matcher, dict[str, float]]:
        assets, weights = self.held()
        m = Matcher(assets, weights, self.cfg.settings)
        return m, {e.asset.asset_id: e.position_factor for e in m.entries}

    def _limiter(self, url: str) -> RateLimiter:
        host = httpx.URL(url).host
        if host not in self._limiters:
            self._limiters[host] = RateLimiter(HOST_INTERVALS.get(host, 0.5))
        return self._limiters[host]

    def _get(self, url: str, **kw: Any) -> httpx.Response:
        return request_with_retry(self.http, "GET", url, limiter=self._limiter(url), retries=1,
                                  timeout=float(self.cfg.settings.get("request_timeout_s", 20)), **kw)

    # -- Zyklus -------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        stats = CycleStats()
        if self.ctx.portfolio() is None:
            return {"skipped": "kein Import"}
        matcher, pfac = self.matcher()
        settings = self.cfg.settings
        index = self._title_index()
        max_items = int(settings.get("max_items_per_feed", 60))
        for src in self.cfg.sources(include_inactive=False):
            stats.sources += 1
            try:
                if src.type == "rss":
                    if not src.url:
                        continue
                    self._rss(src, matcher, pfac, index, stats, max_items)
                elif src.type == "rss_per_asset":
                    self._per_asset(src, matcher, pfac, index, stats, max_items)
                elif src.type == "finnhub":
                    self._finnhub(src, matcher, pfac, index, stats)
                elif src.type == "cryptopanic":
                    self._cryptopanic(src, matcher, pfac, index, stats)
                elif src.type in ("binance_api", "bybit_api"):
                    self._json_api(src, matcher, pfac, index, stats)
            except Exception as e:  # eine Quelle darf den Zyklus nie abbrechen
                stats.errors.append(f"{src.id}: {type(e).__name__}: {e}"[:200])
                log.warning("Quelle %s: unerwarteter Fehler %s", src.id, e)
        try:
            self.youtube_cycle(matcher, pfac, index, stats)
        except Exception as e:
            stats.errors.append(f"youtube: {type(e).__name__}: {e}"[:200])
            log.warning("YouTube-Zyklus fehlgeschlagen: %s", e)
        self.retention()
        return stats.as_dict()

    def _title_index(self) -> TitleIndex:
        idx = TitleIndex()
        since = iso(datetime.now(UTC) - timedelta(days=3))
        for r in self.db.q("SELECT id, title_norm, published_at FROM news_item WHERE published_at>=?", (since,)):
            pub = parse_iso(r["published_at"])
            if pub:
                idx.add(r["id"], r["title_norm"], pub)
        return idx

    # -- Quelltypen ----------------------------------------------------------------------------------
    def _fetch_feed(self, sid: str, name: str, url: str, conditional: bool = True) -> bytes | None:
        guard = self.ctx.guard
        if not guard.allowed(sid):
            return None
        headers = guard.cached_headers(sid) if conditional else {}
        try:
            r = self._get(url, headers=headers)
        except (HttpError, httpx.HTTPError) as e:
            guard.failure(sid, "news", f"{type(e).__name__}: {e}", name)
            return None
        if r.status_code == 304:
            guard.success(sid, "news", name)
            return None
        etag = r.headers.get("etag") if conditional else None
        lm = r.headers.get("last-modified") if conditional else None
        guard.success(sid, "news", name, etag=etag, last_modified=lm)
        return r.content

    def _rss(self, src: Source, matcher: Matcher, pfac: dict[str, float], index: TitleIndex, stats: CycleStats,
             max_items: int) -> None:
        stats.requests += 1
        content = self._fetch_feed(src.id, src.name, src.url)
        if content is None:
            return
        items, err = parse_feed(content, max_items)
        if err:
            self.ctx.guard.failure(src.id, "news", err, src.name)
            return
        self.db.x("UPDATE source_status SET items_last=? WHERE source_id=?", (len(items), src.id))
        for it in items:
            self._process(it, src, matcher, pfac, index, stats, implicit=src.assets)

    def rotation(self, key: str, assets: list[AssetInfo], size: int) -> list[AssetInfo]:
        """Feeds je Asset rotierend abrufen (begrenzt Last und Rate-Limits)."""
        if not assets:
            return []
        start = int(self.db.get_state(f"rotation:{key}", 0) or 0) % len(assets)
        chunk = (assets + assets)[start:start + min(size, len(assets))]
        self.db.set_state(f"rotation:{key}", (start + len(chunk)) % len(assets))
        return chunk

    def _per_asset(self, src: Source, matcher: Matcher, pfac: dict[str, float], index: TitleIndex,
                   stats: CycleStats, max_items: int) -> None:
        assets, weights = self.held()
        top_n = int(self.cfg.settings.get("per_asset_top_n", 40))
        assets = sorted(assets, key=lambda a: -weights.get(a.asset_id, 0))[:top_n]
        if src.applies_to in ("security", "crypto"):
            assets = [a for a in assets if a.asset_class == src.applies_to]
        if "{ticker}" in src.url_template:
            assets = [a for a in assets if a.quote_source == "yahoo" and a.quote_id]
        batch = self.rotation(src.id, assets, 12)
        ok = fail = 0
        for a in batch:
            ticker = a.quote_id or a.symbol
            suffix = ("crypto" if a.is_crypto else ("Aktie" if src.language == "de" else "stock"))
            query = quote_plus(f'"{a.name}" {suffix} when:7d')
            url = src.url_template.replace("{ticker}", quote_plus(ticker or "")).replace("{query}", query)
            stats.requests += 1
            try:
                r = self._get(url)
                ok += 1
            except (HttpError, httpx.HTTPError) as e:
                fail += 1
                stats.errors.append(f"{src.id}/{a.symbol}: {e}"[:160])
                continue
            items, err = parse_feed(r.content, max_items)
            if err:
                continue
            for it in items:
                self._process(it, src, matcher, pfac, index, stats, implicit=[a.asset_id])
        if batch:
            if ok:
                self.ctx.guard.success(src.id, "news", src.name, items=ok)
            elif fail:
                self.ctx.guard.failure(src.id, "news", f"alle {fail} Abrufe fehlgeschlagen", src.name)

    def _finnhub(self, src: Source, matcher: Matcher, pfac: dict[str, float], index: TitleIndex,
                 stats: CycleStats) -> None:
        key = self.ctx.config.secrets.finnhub_api_key
        if not key or not self.ctx.guard.allowed(src.id):
            return
        assets, weights = self.held()
        secs = sorted([a for a in assets if a.is_security and a.quote_source == "yahoo" and a.quote_id
                       and "." not in a.quote_id], key=lambda a: -weights.get(a.asset_id, 0))
        today = datetime.now(UTC).date()
        for a in self.rotation(src.id, secs, 10):
            stats.requests += 1
            try:
                r = self._get("https://finnhub.io/api/v1/company-news",
                              params={"symbol": a.quote_id, "from": (today - timedelta(days=7)).isoformat(),
                                      "to": today.isoformat(), "token": key})
            except (HttpError, httpx.HTTPError) as e:
                self.ctx.guard.failure(src.id, "news", f"{e}", src.name)
                return
            for it in parse_finnhub(r.json())[:40]:
                self._process(it, src, matcher, pfac, index, stats, implicit=[a.asset_id])
        self.ctx.guard.success(src.id, "news", src.name)

    def _cryptopanic(self, src: Source, matcher: Matcher, pfac: dict[str, float], index: TitleIndex,
                     stats: CycleStats) -> None:
        key = self.ctx.config.secrets.cryptopanic_api_key
        if not key or not self.ctx.guard.allowed(src.id):
            return
        assets, _ = self.held()
        codes = sorted({a.symbol.upper() for a in assets if a.is_crypto})[:50]
        if not codes:
            return
        stats.requests += 1
        try:
            r = self._get("https://cryptopanic.com/api/developer/v2/posts/",
                          params={"auth_token": key, "currencies": ",".join(codes), "public": "true"})
        except (HttpError, httpx.HTTPError) as e:
            self.ctx.guard.failure(src.id, "news", f"{e}", src.name)
            return
        self.ctx.guard.success(src.id, "news", src.name)
        for it in parse_cryptopanic(r.json()):
            self._process(it, src, matcher, pfac, index, stats)

    def _json_api(self, src: Source, matcher: Matcher, pfac: dict[str, float], index: TitleIndex,
                  stats: CycleStats) -> None:
        if not src.url or not self.ctx.guard.allowed(src.id):
            return
        stats.requests += 1
        try:
            r = self._get(src.url)
            data = r.json()
        except (HttpError, httpx.HTTPError, ValueError) as e:
            self.ctx.guard.failure(src.id, "news", f"{type(e).__name__}: {e}", src.name)
            return
        items = parse_binance(data) if src.type == "binance_api" else parse_bybit(data)
        self.ctx.guard.success(src.id, "news", src.name, items=len(items))
        for it in items:
            self._process(it, src, matcher, pfac, index, stats)

    # -- Verarbeitung -------------------------------------------------------------------------------
    def _process(self, it: RawItem, src: Source | None, matcher: Matcher, pfac: dict[str, float], index: TitleIndex,
                 stats: CycleStats, implicit: list[str] | None = None, weight: float | None = None,
                 source_id: str | None = None, source_name: str | None = None, language: str | None = None,
                 keep_unmatched: bool | None = None, extra: dict[str, Any] | None = None) -> int | None:
        settings = self.cfg.settings
        now = datetime.now(UTC)
        published = it.published or now
        if published > now + timedelta(hours=2):
            published = now
        retention = int(settings.get("retention_days", 90))
        if published < now - timedelta(days=retention):
            return None
        lang = language or (src.language if src else "en")
        if lang not in (settings.get("languages") or ["de", "en"]):
            return None
        w = weight if weight is not None else (src.weight if src else 0.6)
        sid = source_id or (src.id if src else "?")
        sname = source_name or (src.name if src else sid)
        matches = matcher.match(it.title, it.summary, implicit=implicit)
        require = src.require_match if src else False
        keep = keep_unmatched if keep_unmatched is not None else (src.keep_unmatched if src else False)
        if not matches and require and not keep:
            return None
        hidden = None
        cb = is_clickbait(it.title, settings.get("title_blocklist") or [])
        if cb:
            hidden = f"reißerisch ({cb})"
        if it.channel_id and it.channel_id in self.cfg.blocked_ids():
            hidden = "Kanal blockiert"
        url_norm = normalize_url(it.url)
        title_norm = normalize_title(it.title)
        scores = {m.asset_id: m.score(w, pfac.get(m.asset_id, 0.6)) for m in matches}
        relevance = max(scores.values()) if scores else round(0.25 * w, 4)
        existing = self.db.q1("SELECT id FROM news_item WHERE url_norm=?", (url_norm,))
        dup_id = existing["id"] if existing else index.find(title_norm, published)
        if dup_id is not None:
            self.db.x("UPDATE news_item SET dup_count=dup_count+1, relevance=MAX(relevance, ?) WHERE id=?",
                      (relevance, dup_id))
            self._store_matches(dup_id, matches, scores)
            stats.duplicates += 1
            return dup_id
        ex = extra or {}
        cur = self.db.x(
            """INSERT INTO news_item(kind, url, url_norm, title, title_norm, summary, source_id, source_name,
                   source_weight, language, published_at, fetched_at, image_url, video_id, channel_id, channel_name,
                   duration_s, views, is_short, is_live, relevance, hidden_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it.kind, it.url, url_norm, it.title, title_norm, it.summary, sid, sname, w, lang, iso(published),
             iso(now), it.image_url, it.video_id, it.channel_id, it.channel_name, ex.get("duration_s"),
             ex.get("views", it.views), ex.get("is_short"), ex.get("is_live"), relevance, hidden),
        )
        item_id = int(cur.lastrowid)  # type: ignore[arg-type]
        index.add(item_id, title_norm, published)
        self._store_matches(item_id, matches, scores)
        stats.new_items += 1
        if hidden:
            stats.hidden += 1
        return item_id

    def _store_matches(self, item_id: int, matches: list[Any], scores: dict[str, float]) -> None:
        if not matches:
            return
        self.db.xmany(
            """INSERT INTO news_asset(item_id, asset_id, score, matched) VALUES (?,?,?,?)
               ON CONFLICT(item_id, asset_id) DO UPDATE SET score=MAX(score, excluded.score),
                   matched=excluded.matched""",
            [(item_id, m.asset_id, scores[m.asset_id], json.dumps({"where": m.where, "terms": m.terms}))
             for m in matches],
        )

    # -- YouTube --------------------------------------------------------------------------------------
    def channel_state(self) -> list[dict[str, Any]]:
        rows = {r["handle"]: r for r in self.db.q("SELECT * FROM yt_channel")}
        out = []
        for c in self.cfg.channels():
            r = rows.get(c.handle) if c.handle else None
            cid = c.channel_id or (r["channel_id"] if r and r["status"] == "ok" else None)
            status = "pinned" if c.channel_id else (r["status"] if r else "pending")
            out.append({"channel": c, "channel_id": cid, "status": status,
                        "title": c.title or (r["title"] if r else None), "subscribers": r["subscribers"] if r else None,
                        "error": r["error"] if r else None, "resolved_at": r["resolved_at"] if r else None})
        return out

    def resolve_channels(self, force: bool = False) -> dict[str, Any]:
        res = {"resolved": 0, "unresolvable": 0, "pending": 0}
        for c in self.cfg.channels():
            if not c.handle or c.channel_id or not c.active:
                continue
            if not force and not self.yt.needs_resolution(c.handle):
                continue
            r = self.yt.resolve(c.handle)
            self.yt.store_resolved(r)
            res[{"ok": "resolved"}.get(r.status, r.status)] = res.get({"ok": "resolved"}.get(r.status, r.status), 0) + 1
            if r.status == "unresolvable":
                log.warning("YouTube-Handle %s nicht auflösbar: %s – Kanal wird nicht abgerufen", c.handle, r.error)
        return res

    def youtube_cycle(self, matcher: Matcher, pfac: dict[str, float], index: TitleIndex, stats: CycleStats) -> None:
        self.resolve_channels()
        blocked = self.cfg.blocked_ids()
        new_ids: list[tuple[int, str]] = []
        for st in self.channel_state():
            c: Channel = st["channel"]
            cid = st["channel_id"]
            if not c.active or not cid or cid in blocked:
                continue
            sid = f"yt:{cid}"
            name = st["title"] or c.key
            if not self.ctx.guard.allowed(sid):
                continue
            stats.requests += 1
            try:
                content = self.yt.feed(cid)
            except (HttpError, httpx.HTTPError) as e:
                self.ctx.guard.failure(sid, "youtube", f"{e}", name)
                continue
            items, err = parse_feed(content, 15)
            if err:
                self.ctx.guard.failure(sid, "youtube", err, name)
                continue
            self.ctx.guard.success(sid, "youtube", name, items=len(items))
            for it in items:
                it.channel_id = it.channel_id or cid
                is_live = any(w in it.title.lower() for w in _LIVE_WORDS) or None
                known = self.db.q1("SELECT id FROM news_item WHERE url_norm=?", (normalize_url(it.url),))
                item_id = self._process(it, None, matcher, pfac, index, stats, implicit=c.assets, weight=c.weight,
                                        source_id=sid, source_name=name, language=c.language, keep_unmatched=True,
                                        extra={"is_live": 1 if is_live else 0})
                if item_id and not known and it.video_id:
                    new_ids.append((item_id, it.video_id))
        self.enrich_videos(new_ids)

    def enrich_videos(self, items: list[tuple[int, str]]) -> None:
        if not items:
            return
        if self.yt.api_key:
            try:
                details = self.yt.video_details([v for _, v in items])
            except (HttpError, httpx.HTTPError) as e:
                log.info("YouTube-Videodetails nicht verfügbar: %s", e)
                details = {}
            for item_id, vid in items:
                d = details.get(vid)
                if not d:
                    continue
                dur = d.get("duration_s")
                short = dur is not None and 0 < dur <= 60
                self.db.x("UPDATE news_item SET duration_s=?, views=COALESCE(?, views), is_live=?, is_short=? "
                          "WHERE id=?",
                          (dur, d.get("views"), 1 if d.get("is_live") else 0, 1 if short else 0, item_id))
        # Shorts ohne API: /shorts/{id} prüfen (nur für neue Videos, max. 25 je Zyklus)
        for item_id, vid in items[:25]:
            row = self.db.q1("SELECT is_short FROM news_item WHERE id=?", (item_id,))
            if row is not None and row["is_short"] is None:
                s = self.yt.is_short(vid)
                if s is not None:
                    self.db.x("UPDATE news_item SET is_short=? WHERE id=?", (1 if s else 0, item_id))

    def discover(self) -> dict[str, Any]:
        """Tägliche Entdeckung (nur mit YOUTUBE_API_KEY): Suche nach den größten Positionen."""
        disc = self.cfg.discovery()
        if not self.yt.api_key or not disc.get("enabled", True):
            return {"skipped": "kein YOUTUBE_API_KEY oder deaktiviert"}
        budget = int(disc.get("daily_unit_budget", 2500))
        assets, weights = self.held()
        top = sorted(assets, key=lambda a: -weights.get(a.asset_id, 0))[: int(disc.get("top_n_positions", 10))]
        subscribed = {st["channel_id"] for st in self.channel_state() if st["channel_id"]}
        blocked, ignored = self.cfg.blocked_ids(), self.cfg.ignored_ids()
        since = datetime.now(UTC) - timedelta(days=int(disc.get("published_within_days", 7)))
        found: dict[str, dict[str, Any]] = {}
        searches = 0
        for a in top:
            if self.yt.units_used() + 100 + 5 > budget:
                break
            suffix = disc.get("query_suffix_crypto" if a.is_crypto else "query_suffix_security") or ""
            try:
                results = self.yt.search(f'"{a.name}" {suffix}'.strip(), since)
            except (HttpError, httpx.HTTPError) as e:
                log.info("YouTube-Suche fehlgeschlagen: %s", e)
                break
            searches += 1
            for r in results:
                if r["channel_id"] in blocked or r["channel_id"] in ignored or r["channel_id"] in subscribed:
                    continue
                found.setdefault(r["video_id"], {**r, "assets": []})["assets"].append(a.asset_id)
        if not found:
            return {"searches": searches, "suggestions": 0}
        details = self.yt.video_details(list(found))
        chans = self.yt.channel_stats(sorted({d.get("channel_id") for d in details.values() if d.get("channel_id")}))
        matcher, pfac = self.matcher()
        index = self._title_index()
        stats = CycleStats()
        min_subs, min_views = int(disc.get("min_subscribers", 100000)), int(disc.get("min_views", 5000))
        now = iso(datetime.now(UTC))
        new_sugg = 0
        for vid, info in found.items():
            d = details.get(vid) or {}
            ch = chans.get(d.get("channel_id") or "") or {}
            if (ch.get("subscribers") or 0) < min_subs or (d.get("views") or 0) < min_views:
                continue
            dur = d.get("duration_s")
            it = RawItem(title=d.get("title") or info.get("title") or "", url=f"https://www.youtube.com/watch?v={vid}",
                         summary=(d.get("description") or "")[:600], published=parse_iso(d.get("published")),
                         image_url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", kind="video", video_id=vid,
                         channel_id=d.get("channel_id"), channel_name=d.get("channel_name"), views=d.get("views"))
            lang = (d.get("language") or "en")[:2]
            self._process(it, None, matcher, pfac, index, stats, implicit=info["assets"], weight=0.4,
                          source_id="yt:discovery", source_name=f"Entdeckt: {d.get('channel_name') or ''}",
                          language=lang if lang in ("de", "en") else "en", keep_unmatched=False,
                          extra={"duration_s": dur, "is_short": 1 if dur is not None and dur <= 60 else 0,
                                 "is_live": 1 if d.get("is_live") else 0, "views": d.get("views")})
            cid = d.get("channel_id")
            names = ", ".join(self.ctx.portfolio().asset(x).name for x in info["assets"])  # type: ignore[union-attr]
            cur = self.db.x(
                """INSERT INTO yt_suggestion(channel_id, title, handle, subscribers, found_for, sample_video,
                       sample_title, first_seen, last_seen, hits, status) VALUES (?,?,?,?,?,?,?,?,?,1,'new')
                   ON CONFLICT(channel_id) DO UPDATE SET title=excluded.title, subscribers=excluded.subscribers,
                       found_for=excluded.found_for, sample_video=excluded.sample_video,
                       sample_title=excluded.sample_title, last_seen=excluded.last_seen, hits=hits+1""",
                (cid, ch.get("title"), ch.get("handle"), ch.get("subscribers"), names, vid, it.title, now, now))
            new_sugg += cur.rowcount
        return {"searches": searches, "videos": len(found), "suggestions": new_sugg,
                "units_today": self.yt.units_used()}

    # -- Pflege -----------------------------------------------------------------------------------------
    def rematch(self) -> dict[str, Any]:
        """Nach Import/Konfigurationsänderung: Zuordnungen und Relevanz aller gespeicherten Meldungen neu berechnen."""
        matcher, pfac = self.matcher()
        sources = {s.id: s for s in self.cfg.sources()}
        chans = {f"yt:{st['channel_id']}": st["channel"] for st in self.channel_state() if st["channel_id"]}
        rows = self.db.q("SELECT id, title, summary, source_id, source_weight FROM news_item")
        with self.db.transaction() as c:
            c.execute("DELETE FROM news_asset")
            for r in rows:
                implicit = []
                src = sources.get(r["source_id"])
                if src and src.assets:
                    implicit = src.assets
                ch = chans.get(r["source_id"])
                if ch and ch.assets:
                    implicit = ch.assets
                matches = matcher.match(r["title"], r["summary"] or "", implicit=implicit)
                w = r["source_weight"]
                scores = {m.asset_id: m.score(w, pfac.get(m.asset_id, 0.6)) for m in matches}
                for m in matches:
                    c.execute("INSERT OR REPLACE INTO news_asset(item_id, asset_id, score, matched) VALUES (?,?,?,?)",
                              (r["id"], m.asset_id, scores[m.asset_id],
                               json.dumps({"where": m.where, "terms": m.terms})))
                rel = max(scores.values()) if scores else round(0.25 * w, 4)
                c.execute("UPDATE news_item SET relevance=? WHERE id=?", (rel, r["id"]))
        return {"items": len(rows)}

    def retention(self) -> int:
        days = int(self.cfg.settings.get("retention_days", 90))
        cutoff = iso(datetime.now(UTC) - timedelta(days=days))
        with self.db.transaction() as c:
            c.execute("DELETE FROM news_asset WHERE item_id IN (SELECT id FROM news_item WHERE published_at<?)",
                      (cutoff,))
            n = c.execute("DELETE FROM news_item WHERE published_at<?", (cutoff,)).rowcount
        return n
