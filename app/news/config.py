"""/data/sources.yaml: Quellen für News & Videos (kommentarerhaltend gelesen und geschrieben)."""

from __future__ import annotations

import copy
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

log = logging.getLogger(__name__)

SOURCE_TYPES = {"rss", "rss_per_asset", "finnhub", "cryptopanic", "binance_api", "bybit_api"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "fetch_interval_minutes": 30,
    "retention_days": 90,
    "request_timeout_s": 20,
    "max_items_per_feed": 60,
    "languages": ["de", "en"],
    "per_asset_top_n": 40,
    "shorts_hidden": True,
    "title_blocklist": [],
    "context_words": ["crypto", "krypto", "coin", "token", "blockchain"],
    "ambiguous_terms": [],
    "exclude_patterns": {},
    "default_weights": {"primary": 1.0, "agency": 0.8, "crypto_portal": 0.7, "youtube": 0.6, "discovered": 0.4},
}

DEFAULT_DISCOVERY: dict[str, Any] = {
    "enabled": True, "top_n_positions": 10, "min_subscribers": 100000, "min_views": 5000,
    "published_within_days": 7, "daily_unit_budget": 2500, "query_suffix_crypto": "crypto",
    "query_suffix_security": "stock",
}


@dataclass
class Source:
    id: str
    name: str
    type: str
    url: str = ""
    url_template: str = ""
    applies_to: str = "all"
    weight: float = 0.7
    language: str = "en"
    active: bool = True
    require_match: bool = True
    keep_unmatched: bool = False
    assets: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Channel:
    handle: str | None
    channel_id: str | None = None
    title: str | None = None
    group: str = ""
    language: str = "en"
    weight: float = 0.6
    active: bool = True
    confirmed: bool = False
    assets: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.handle or self.channel_id or "?"


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


class SourcesConfig:
    def __init__(self, path: Path, example: Path) -> None:
        self.path = Path(path)
        self.example = Path(example)
        self._lock = threading.RLock()
        self._cache: tuple[float, Any] | None = None
        self.errors: list[str] = []

    def ensure(self) -> None:
        if not self.path.exists() and self.example.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.example, self.path)
            log.info("Beispielkonfiguration nach %s kopiert", self.path)

    def data(self) -> Any:
        with self._lock:
            self.ensure()
            if not self.path.exists():
                return CommentedMap()
            mtime = self.path.stat().st_mtime
            if self._cache and self._cache[0] == mtime:
                return self._cache[1]
            try:
                with open(self.path, encoding="utf-8") as f:
                    d = _yaml().load(f) or CommentedMap()
                self.errors = []
            except Exception as e:  # ungültiges YAML → leere Konfiguration, Fehler anzeigen
                self.errors = [f"sources.yaml ungültig: {e}"]
                log.error("sources.yaml konnte nicht gelesen werden: %s", e)
                d = CommentedMap()
            self._cache = (mtime, d)
            return d

    def save(self, d: Any) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sources-", suffix=".yaml")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _yaml().dump(d, f)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self._cache = None

    # -- Lesen ---------------------------------------------------------------------------------------
    @property
    def settings(self) -> dict[str, Any]:
        s = copy.deepcopy(DEFAULT_SETTINGS)
        raw = self.data().get("settings") or {}
        for k, v in dict(raw).items():
            s[k] = _plain(v)
        return s

    def sources(self, include_inactive: bool = True) -> list[Source]:
        out = []
        for i, raw in enumerate(self.data().get("sources") or []):
            try:
                r = dict(raw)
                typ = str(r.get("type") or "rss")
                if typ not in SOURCE_TYPES:
                    self.errors.append(f"Quelle #{i + 1}: unbekannter Typ '{typ}'")
                    continue
                src = Source(
                    id=str(r.get("id") or f"source{i}"), name=str(r.get("name") or r.get("id") or f"Quelle {i + 1}"),
                    type=typ, url=str(r.get("url") or ""), url_template=str(r.get("url_template") or ""),
                    applies_to=str(r.get("applies_to") or "all"), weight=float(r.get("weight", 0.7)),
                    language=str(r.get("language") or "en"), active=bool(r.get("active", True)),
                    require_match=bool(r.get("require_match", True)),
                    keep_unmatched=bool(r.get("keep_unmatched", False)),
                    assets=[str(a) for a in (r.get("assets") or [])], note=str(r.get("note") or ""),
                )
            except (TypeError, ValueError) as e:
                self.errors.append(f"Quelle #{i + 1}: {e}")
                continue
            if include_inactive or src.active:
                out.append(src)
        return out

    def youtube(self) -> Any:
        return self.data().get("youtube") or CommentedMap()

    def channels(self) -> list[Channel]:
        out = []
        for raw in self.youtube().get("channels") or []:
            r = dict(raw)
            handle = r.get("handle")
            if handle and not str(handle).startswith("@"):
                handle = "@" + str(handle)
            if not handle and not r.get("channel_id"):
                continue
            out.append(Channel(handle=str(handle) if handle else None, channel_id=r.get("channel_id"),
                               title=r.get("title"), group=str(r.get("group") or ""),
                               language=str(r.get("language") or "en"), weight=float(r.get("weight", 0.6)),
                               active=bool(r.get("active", True)), confirmed=bool(r.get("confirmed", False)),
                               assets=[str(a) for a in (r.get("assets") or [])]))
        return out

    def discovery(self) -> dict[str, Any]:
        d = copy.deepcopy(DEFAULT_DISCOVERY)
        d.update(_plain(self.youtube().get("discovery") or {}))
        return d

    def listed_channels(self, key: str) -> list[dict[str, Any]]:
        return [dict(x) if isinstance(x, dict) else {"channel_id": str(x)} for x in (self.youtube().get(key) or [])]

    def blocked_ids(self) -> set[str]:
        return {c.get("channel_id") for c in self.listed_channels("blocked_channels") if c.get("channel_id")}

    def ignored_ids(self) -> set[str]:
        return {c.get("channel_id") for c in self.listed_channels("ignored_channels") if c.get("channel_id")}

    # -- Schreiben (Entscheidungen aus der UI) --------------------------------------------------------
    def _yt(self, d: Any) -> Any:
        if d.get("youtube") is None:
            d["youtube"] = CommentedMap()
        return d["youtube"]

    def add_channel(self, channel_id: str, title: str | None, handle: str | None, language: str = "en",
                    weight: float = 0.6, group: str = "Entdeckt") -> None:
        with self._lock:
            d = self.data()
            yt = self._yt(d)
            if yt.get("channels") is None:
                yt["channels"] = CommentedSeq()
            for c in yt["channels"]:
                if c.get("channel_id") == channel_id or (handle and c.get("handle") == handle):
                    c["active"] = True
                    c["confirmed"] = True
                    self.save(d)
                    return
            entry = CommentedMap()
            if handle:
                entry["handle"] = handle
            entry["channel_id"] = channel_id
            if title:
                entry["title"] = title
            entry["group"] = group
            entry["language"] = language
            entry["weight"] = weight
            entry["active"] = True
            entry["confirmed"] = True
            entry.fa.set_flow_style()
            yt["channels"].append(entry)
            self._remove_from(yt, "ignored_channels", channel_id)
            self.save(d)

    def _remove_from(self, yt: Any, key: str, channel_id: str) -> None:
        lst = yt.get(key)
        if not lst:
            return
        keep = [x for x in lst if (x.get("channel_id") if isinstance(x, dict) else str(x)) != channel_id]
        yt[key] = CommentedSeq(keep)

    def list_channel(self, key: str, channel_id: str, title: str | None) -> None:
        """key: ignored_channels | blocked_channels"""
        with self._lock:
            d = self.data()
            yt = self._yt(d)
            if yt.get(key) is None:
                yt[key] = CommentedSeq()
            if not any((x.get("channel_id") if isinstance(x, dict) else str(x)) == channel_id for x in yt[key]):
                entry = CommentedMap()
                entry["channel_id"] = channel_id
                if title:
                    entry["title"] = title
                entry.fa.set_flow_style()
                yt[key].append(entry)
            if key == "blocked_channels":
                for c in yt.get("channels") or []:
                    if c.get("channel_id") == channel_id:
                        c["active"] = False
            self.save(d)

    def set_channel(self, key: str, **fields: Any) -> bool:
        with self._lock:
            d = self.data()
            for c in self._yt(d).get("channels") or []:
                h = c.get("handle")
                if h and not str(h).startswith("@"):
                    h = "@" + str(h)
                if h == key or c.get("channel_id") == key:
                    for k, v in fields.items():
                        c[k] = v
                    self.save(d)
                    return True
            return False

    def set_source(self, source_id: str, **fields: Any) -> bool:
        with self._lock:
            d = self.data()
            for s in d.get("sources") or []:
                if s.get("id") == source_id:
                    for k, v in fields.items():
                        s[k] = v
                    self.save(d)
                    return True
            return False


def _plain(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_plain(x) for x in v]
    return v
