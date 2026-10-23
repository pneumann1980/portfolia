"""Artikeltext aus HTML gewinnen (ohne externe Extraktions-Bibliothek, nur ``lxml``).

Reihenfolge der Quellen: JSON-LD (``articleBody``) → Artikelcontainer im DOM (``<article>``, ``itemprop=articleBody``,
bekannte Klassen, sonst Absatzdichte). Das Ergebnis ist **reiner Text** in Absätzen – es werden weder HTML noch
Skripte gespeichert. Die Qualitätsprüfung unterscheidet vollständigen Text (``success``), Teiltext (``partial``),
Bezahl-/Zustimmungsschranke (``blocked``) und unbrauchbare Seiten (``failed``). Es wird nichts umgangen: sieht die Seite
nur einen Anreißer, bleibt es bei ``partial``/``blocked``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from lxml import etree
from lxml import html as lh

MAX_CHARS = 30_000
MIN_SUCCESS_WORDS = 150
MIN_PARTIAL_WORDS = 40

_WS = re.compile(r"[ \t\r\f\v ]+")
_TOKEN = re.compile(r"[a-z0-9]+")
_DROP_TAGS = ("script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form", "iframe",
              "svg", "button", "select", "dialog", "figure", "picture", "video", "audio", "canvas", "object", "embed")
_BOILER = {"comment", "comments", "share", "sharing", "social", "related", "newsletter", "cookie", "cookies", "consent",
           "banner", "promo", "advert", "advertisement", "ads", "adslot", "sidebar", "footer", "menu", "breadcrumb",
           "breadcrumbs", "teaser", "recommend", "recommendations", "subscribe", "subscription", "author-box",
           "authorbox", "tags", "toolbar", "byline-share", "taboola", "outbrain", "paywall-prompt", "modal", "popup",
           "overlay", "widget", "ticker", "toc"}
_BODY_CLASSES = {"articlebody", "article-body", "article__body", "story-body", "storybody", "post-content",
                 "entry-content", "article-content", "articlecontent", "content-body", "main-content", "post-body",
                 "article-text", "articletext", "richtext", "rich-text", "text-body", "story-content"}
_PAYWALL_TEXT = re.compile(
    r"(jetzt weiterlesen|weiterlesen mit|nur f(ü|u)r abonnenten|nur mit abo|bezahl(inhalt|schranke)|plus-artikel|"
    r"exklusiv f(ü|u)r abonnenten|subscribe to (continue|read)|subscribers only|to continue reading|"
    r"sign in to read|create (a free )?account to (continue|read)|already a subscriber|unlock this article|"
    r"become a member to)", re.I)
_CONSENT_TEXT = re.compile(r"(bevor sie fortfahren|before you continue to|cookies? (zu|zu\s+)?akzeptieren|"
                           r"zustimmung erforderlich|please enable javascript|enable javascript and cookies|"
                           r"verify you are (a )?human|checking your browser|access denied)", re.I)
_LINE_NOISE = re.compile(
    r"^(anzeige|werbung|advertisement|lesen sie auch|mehr zum thema|weiterlesen|read more|related( articles)?|"
    r"newsletter|teilen|share( this)?|folgen sie uns|follow us|©|copyright|alle rechte vorbehalten|"
    r"this (article|story) (was|is) (originally )?(published|first)|bildquelle|foto:|image:|photo:|"
    r"auch interessant|das könnte sie (auch )?interessieren)\b", re.I)


@dataclass
class Article:
    title: str = ""
    author: str = ""
    published: str = ""
    lang: str = ""
    image_url: str = ""
    canonical_url: str = ""
    description: str = ""
    paragraphs: list[str] = field(default_factory=list)
    source: str = ""  # jsonld | dom | none
    paywall: bool = False
    consent_wall: bool = False
    link_density: float = 0.0

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def words(self) -> int:
        return len(self.text.split())

    def content_hash(self) -> str:
        norm = re.sub(r"\s+", " ", self.text.lower()).strip()
        return hashlib.sha256(norm.encode()).hexdigest()


def _clean(s: str) -> str:
    return _WS.sub(" ", html.unescape(s or "")).strip()


def _tokens(el: Any) -> set[str]:
    return set(_TOKEN.findall(f"{el.get('class', '')} {el.get('id', '')}".lower())) | {
        (el.get("class") or "").lower().strip()}


def _is_boiler(el: Any) -> bool:
    cls = f"{el.get('class', '')} {el.get('id', '')}".lower()
    parts = set(re.split(r"[\s_]+", cls))
    parts |= set(_TOKEN.findall(cls))
    return bool(parts & _BOILER) and not (parts & _BODY_CLASSES)


def _meta(doc: Any, *keys: str) -> str:
    for k in keys:
        for el in doc.xpath(f'//meta[@property="{k}" or @name="{k}" or @itemprop="{k}"]'):
            v = _clean(el.get("content", ""))
            if v:
                return v
    return ""


def _jsonld(doc: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            types = node.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any((isinstance(t, str) and t.endswith("Article")) or t in ("BlogPosting", "Report") for t in types):
                out.append(node)
            walk(node.get("@graph"))

    for s in doc.xpath('//script[@type="application/ld+json"]'):
        raw = (s.text or "").strip()
        if not raw or len(raw) > 2_000_000:
            continue
        try:
            walk(json.loads(raw))
        except ValueError:
            continue
    return out


def _person(v: Any) -> str:
    if isinstance(v, list):
        return ", ".join(x for x in (_person(i) for i in v) if x)[:200]
    if isinstance(v, dict):
        return _clean(str(v.get("name", "")))[:120]
    return _clean(str(v or ""))[:120]


def _image(v: Any) -> str:
    if isinstance(v, list):
        return _image(v[0]) if v else ""
    if isinstance(v, dict):
        return str(v.get("url") or v.get("contentUrl") or "")
    return str(v or "")


def _split_body(raw: str) -> list[str]:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h[1-6]>", "\n", raw or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return [p for p in (_clean(x) for x in re.split(r"\n+", text)) if p]


def _drop(doc: Any) -> None:
    for tag in _DROP_TAGS:
        for el in list(doc.iter(tag)):
            if el.getparent() is not None:
                el.drop_tree()
    for el in list(doc.iter()):
        if not isinstance(el.tag, str) or el.getparent() is None:
            if isinstance(el, etree._Comment) and el.getparent() is not None:
                el.drop_tree()
            continue
        if el.tag not in ("html", "body", "article", "main") and _is_boiler(el):
            el.drop_tree()
    for br in doc.iter("br"):
        br.tail = "\n" + (br.tail or "")


def _link_chars(el: Any) -> int:
    return sum(len(_clean(a.text_content())) for a in el.iter("a"))


def _paragraphs(container: Any) -> tuple[list[str], float]:
    out: list[str] = []
    total = links = 0
    for el in container.iter("p", "h2", "h3", "h4", "li", "blockquote"):
        if el.tag in ("li", "blockquote") and el.xpath("ancestor::p"):
            continue
        if el.tag == "blockquote" and el.xpath(".//p"):
            continue
        if el.tag == "li" and (el.xpath(".//p") or not el.xpath("ancestor::*[self::ul or self::ol]")):
            continue
        for line in (_clean(x) for x in el.text_content().split("\n")):
            if not line:
                continue
            ll = _link_chars(el)
            if el.tag == "li" and len(line) < 50:
                continue
            if len(line) < 25 and el.tag == "p" and not re.search(r"[.!?:»“\"]$", line):
                continue
            if ll > 0.6 * len(line) and len(line) < 160:
                continue
            if _LINE_NOISE.match(line):
                continue
            if el.tag in ("h2", "h3", "h4"):
                line = f"## {line}"
            if out and out[-1] == line:
                continue
            out.append(line)
            total += len(line)
            links += min(ll, len(line))
    return out, (links / total if total else 0.0)


def _containers(doc: Any) -> list[Any]:
    found: list[Any] = []
    found += doc.xpath('//*[@itemprop="articleBody"]')
    found += doc.xpath("//article")
    for el in doc.iter("div", "section", "main"):
        if _tokens(el) & _BODY_CLASSES or set(re.split(r"\s+", (el.get("class") or "").lower())) & _BODY_CLASSES:
            found.append(el)
    found += doc.xpath("//main")
    return found


def _score(paragraphs: list[str]) -> int:
    return sum(len(p) for p in paragraphs if len(p) >= 40)


def _best_dom(doc: Any) -> tuple[list[str], float]:
    best: tuple[int, list[str], float] = (0, [], 0.0)
    seen: set[int] = set()
    cands = _containers(doc)
    if not cands:  # Absatzdichte: Elternknoten mit dem meisten zusammenhängenden Text
        agg: dict[int, tuple[int, Any]] = {}
        for p in doc.iter("p"):
            n = len(_clean(p.text_content()))
            par = p.getparent()
            if par is None or n < 40:
                continue
            k = id(par)
            agg[k] = (agg.get(k, (0, par))[0] + n, par)
        cands = [par for _, (_, par) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:5]]
    for c in cands:
        if id(c) in seen:
            continue
        seen.add(id(c))
        paras, ld = _paragraphs(c)
        sc = _score(paras)
        if sc > best[0]:
            best = (sc, paras, ld)
    return best[1], best[2]


def _cap(paragraphs: list[str], limit: int = MAX_CHARS) -> list[str]:
    out, n = [], 0
    for p in paragraphs:
        if n + len(p) > limit:
            break
        out.append(p)
        n += len(p) + 2
    return out


def parse_article(raw: str, base_url: str = "") -> Article:
    """HTML → :class:`Article`. Wirft nie bei kaputtem HTML; leere Ergebnisse haben ``source == 'none'``."""
    art = Article(source="none")
    try:
        doc = lh.document_fromstring(raw)
    except (etree.ParserError, ValueError):
        return art
    root_lang = (doc.get("lang") or "").lower()[:2]
    art.lang = root_lang if root_lang.isalpha() else ""
    canon = doc.xpath('//link[@rel="canonical"]/@href')
    if canon:
        art.canonical_url = urljoin(base_url, canon[0].strip())
    art.title = _meta(doc, "og:title", "twitter:title") or _clean((doc.xpath("//title/text()") or [""])[0])
    art.description = _meta(doc, "og:description", "description", "twitter:description")[:500]
    art.image_url = _meta(doc, "og:image", "twitter:image")
    art.author = _meta(doc, "author", "article:author")[:120]
    art.published = _meta(doc, "article:published_time", "datePublished", "og:article:published_time")
    ld = _jsonld(doc)
    ld_body: list[str] = []
    for node in ld:
        art.title = art.title or _clean(str(node.get("headline", "")))
        art.published = art.published or str(node.get("datePublished", ""))
        art.author = art.author or _person(node.get("author"))
        art.image_url = art.image_url or _image(node.get("image"))
        free = node.get("isAccessibleForFree")
        if free is False or str(free).lower() == "false":
            art.paywall = True
        body = node.get("articleBody")
        if isinstance(body, str) and len(body) > sum(len(x) for x in ld_body):
            ld_body = _split_body(body)
    if art.image_url:
        art.image_url = urljoin(base_url, art.image_url)
    full_text = _clean(doc.text_content())
    if _PAYWALL_TEXT.search(full_text[:60_000]):
        art.paywall = True
    if _CONSENT_TEXT.search(full_text[:3000]):
        art.consent_wall = True
    _drop(doc)
    dom, ld_density = _best_dom(doc)
    art.link_density = ld_density
    ld_len, dom_len = sum(len(p) for p in ld_body), sum(len(p) for p in dom)
    if ld_len >= 400 and ld_len >= 0.8 * dom_len:
        art.paragraphs, art.source = _cap(ld_body), "jsonld"
    elif dom:
        art.paragraphs, art.source = _cap(dom), "dom"
    elif ld_body:
        art.paragraphs, art.source = _cap(ld_body), "jsonld"
    return art


def assess(art: Article, teaser: str = "") -> tuple[str, str, float]:
    """(Status, Grund, Qualität 0–1). Der Grund ist ein stabiler Kurzcode."""
    words = art.words
    n = len(art.paragraphs)
    quality = round(min(1.0, words / 300) * 0.6 + min(1.0, n / 5) * 0.25 + (1 - min(1.0, art.link_density)) * 0.15, 3)
    if art.consent_wall and words < MIN_SUCCESS_WORDS:
        return "blocked", "consent", min(quality, 0.1)
    if art.paywall and words < 250:
        return "blocked", "paywall", min(quality, 0.3)
    if words >= MIN_SUCCESS_WORDS and n >= 3 and art.link_density < 0.5:
        return "success", "", quality
    if words >= MIN_PARTIAL_WORDS:
        teaser_words = len(teaser.split())
        return "partial", ("paywall" if art.paywall else ("short" if words > teaser_words * 1.3 else "teaser_only")), \
            min(quality, 0.5)
    return "failed", "no_content", 0.0


def excerpt(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


__all__ = ["MAX_CHARS", "Article", "assess", "excerpt", "parse_article"]
