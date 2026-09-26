"""Allokationsdaten für Sunburst (Segment → Kategorie → Position), Donut und Treemap."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.analytics.colors import SEGMENTS, TINTS, asset_color, tint_index
from app.analytics.valuation import Valuation

SEGMENT_ORDER = {s: i for i, s in enumerate(SEGMENTS)}


def _node_colors(segment: str, key: str | None = None) -> dict[str, str]:
    idx = 2 if key is None else tint_index(key)
    return {m: TINTS[m].get(segment, TINTS[m]["Sonstige"])[idx] for m in ("light", "dark")}


def sunburst(val: Valuation, threshold_pct: float = 1.0, expand: set[str] | None = None) -> dict[str, Any]:
    """Positionen < threshold_pct werden je Kategorie zu „Sonstige“ zusammengefasst (per Klick aufklappbar)."""
    expand = expand or set()
    total = val.total_value
    tree: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for p in val.positions:
        if p.value <= 0:
            continue
        tree[p.segment][p.category].append(p)
    data = []
    for seg in sorted(tree, key=lambda s: SEGMENT_ORDER.get(s, 99)):
        seg_children = []
        seg_value = 0.0
        for cat in sorted(tree[seg], key=lambda c: -sum(p.value for p in tree[seg][c])):
            items = sorted(tree[seg][cat], key=lambda p: -p.value)
            children = []
            small = []
            for p in items:
                share = p.value / total * 100 if total else 0
                if share < threshold_pct and f"{seg}|{cat}" not in expand:
                    small.append(p)
                else:
                    children.append({"name": p.asset.name, "id": p.asset_id, "value": round(p.value, 2),
                                     "share": share, "symbol": p.asset.symbol,
                                     "colors": {m: asset_color(p.asset_id, seg, m) for m in ("light", "dark")}})
            if len(small) == 1:
                p = small[0]
                children.append({"name": p.asset.name, "id": p.asset_id, "value": round(p.value, 2),
                                 "share": p.value / total * 100 if total else 0, "symbol": p.asset.symbol,
                                 "colors": {m: asset_color(p.asset_id, seg, m) for m in ("light", "dark")}})
            elif small:
                sv = sum(p.value for p in small)
                children.append({"name": f"Sonstige ({len(small)})", "id": f"other:{seg}|{cat}", "other": True,
                                 "value": round(sv, 2), "share": sv / total * 100 if total else 0,
                                 "members": [p.asset.name for p in small][:30],
                                 "colors": _node_colors("Sonstige")})
            cv = sum(p.value for p in items)
            seg_value += cv
            seg_children.append({"name": cat, "id": f"cat:{seg}|{cat}", "value": round(cv, 2),
                                 "share": cv / total * 100 if total else 0, "children": children,
                                 "colors": _node_colors(seg, cat)})
        data.append({"name": seg, "id": f"seg:{seg}", "value": round(seg_value, 2),
                     "share": seg_value / total * 100 if total else 0, "children": seg_children,
                     "colors": _node_colors(seg)})
    return {"total": round(total, 2), "threshold": threshold_pct, "data": data}


def donut(val: Valuation, level: str = "position", threshold_pct: float = 1.0,
          expand: set[str] | None = None) -> dict[str, Any]:
    total = val.total_value
    if level == "segment":
        agg: dict[str, float] = defaultdict(float)
        for p in val.positions:
            agg[p.segment] += max(p.value, 0)
        items = [{"name": s, "id": f"seg:{s}", "value": round(v, 2), "share": v / total * 100 if total else 0,
                  "colors": _node_colors(s)} for s, v in sorted(agg.items(), key=lambda x: SEGMENT_ORDER.get(x[0], 9))]
        return {"total": round(total, 2), "level": level, "data": items}
    if level == "category":
        aggc: dict[tuple[str, str], float] = defaultdict(float)
        for p in val.positions:
            aggc[(p.segment, p.category)] += max(p.value, 0)
        items = [{"name": c, "id": f"cat:{s}|{c}", "value": round(v, 2), "share": v / total * 100 if total else 0,
                  "colors": _node_colors(s, c)}
                 for (s, c), v in sorted(aggc.items(), key=lambda x: (SEGMENT_ORDER.get(x[0][0], 9), -x[1]))]
        return {"total": round(total, 2), "level": level, "data": items}
    expand = expand or set()
    items = []
    small = []
    for p in sorted(val.positions, key=lambda p: (SEGMENT_ORDER.get(p.segment, 9), -p.value)):
        if p.value <= 0:
            continue
        share = p.value / total * 100 if total else 0
        if share < threshold_pct and "all" not in expand:
            small.append(p)
            continue
        items.append({"name": p.asset.name, "id": p.asset_id, "value": round(p.value, 2), "share": share,
                      "symbol": p.asset.symbol,
                      "colors": {m: asset_color(p.asset_id, p.segment, m) for m in ("light", "dark")}})
    if small:
        sv = sum(p.value for p in small)
        items.append({"name": f"Sonstige ({len(small)})", "id": "other:all", "other": True, "value": round(sv, 2),
                      "share": sv / total * 100 if total else 0, "members": [p.asset.name for p in small][:30],
                      "colors": _node_colors("Sonstige")})
    return {"total": round(total, 2), "level": level, "data": items}


def treemap(val: Valuation) -> dict[str, Any]:
    """Fläche = Wert, Farbe = Tagesänderung (divergierend, Mitte neutral)."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for p in val.positions:
        if p.value <= 0 or p.asset.is_fiat:
            continue
        groups[p.segment].append({
            "name": p.asset.symbol, "full_name": p.asset.name, "id": p.asset_id, "value": round(p.value, 2),
            "change_pct": round(p.day_change_pct * 100, 2) if p.day_change_pct is not None else None,
            "change_eur": round(p.day_change, 2) if p.day_change is not None else None,
            "weight": p.weight * 100,
        })
    return {"data": [{"name": s, "children": sorted(ch, key=lambda x: -x["value"])}
                     for s, ch in sorted(groups.items(), key=lambda x: SEGMENT_ORDER.get(x[0], 9))]}
