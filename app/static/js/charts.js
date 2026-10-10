/* Portfolia – Diagramme mit Apache ECharts (lokal gebündelt, Locale DE).
   Gestaltung: dünne Linien (2px), Flächen als 10-%-Tönung, Haarlinien-Raster, 2px Flächenabstand,
   Werte in Tooltips immer mit Einheit, Farben aus CSS-Tokens (hell/dunkel). */
(function () {
  "use strict";

  var instances = new Map();
  var NF2 = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  var NF0 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
  var NFP = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" });
  var DF = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
  var DTF = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

  // Datenschutz-Modus (nur Dashboard): Beträge in Achsen und Tooltips maskieren, Prozentwerte bleiben sichtbar
  function masked() {
    return document.documentElement.classList.contains("hide-total") && !!document.querySelector("[data-privacy-scope]");
  }
  function eur(v, dec) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    if (masked()) return "••• €";
    if (dec === 0) return NF0.format(v) + " €";
    var a = Math.abs(v);
    if (dec === undefined && a > 0 && a < 1) {
      return new Intl.NumberFormat("de-DE", { maximumSignificantDigits: 4 }).format(v) + " €";
    }
    return NF2.format(v) + " €";
  }
  function eurCompact(v) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    if (masked()) return "••• €";
    var a = Math.abs(v);
    if (a >= 1e6) return NF2.format(v / 1e6) + " Mio. €";
    if (a >= 1e4) return new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 }).format(v / 1e3) + " Tsd. €";
    return NF0.format(v) + " €";
  }
  function pct(v, dec) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    var f = new Intl.NumberFormat("de-DE", { minimumFractionDigits: dec === undefined ? 2 : dec, maximumFractionDigits: dec === undefined ? 2 : dec, signDisplay: "exceptZero" });
    return f.format(v) + " %";
  }
  function share(v) { return new Intl.NumberFormat("de-DE", { maximumFractionDigits: v < 10 ? 1 : 0 }).format(v) + " %"; }
  function num(v, dec) {
    return new Intl.NumberFormat("de-DE", { maximumFractionDigits: dec === undefined ? 8 : dec }).format(v);
  }
  function fdate(s) { var d = new Date(s); return isNaN(d) ? s : DF.format(d); }
  function fdt(s) { var d = new Date(s); return isNaN(d) ? s : DTF.format(d); }
  // Index des Datums, das dem Zeitwert des Mauszeigers am nächsten liegt. Die Tooltips lesen ihre Werte direkt aus
  // den Rohdaten: Serien mit Downsampling (lttb) fehlen sonst an Stellen, an denen ein Punkt weggelassen wurde.
  function nearestIndex(dates, ts) {
    var x = typeof ts === "number" ? ts : Date.parse(ts);
    if (isNaN(x) || !dates.length) return -1;
    var lo = 0, hi = dates.length - 1;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (Date.parse(dates[mid]) < x) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(Date.parse(dates[lo - 1]) - x) <= Math.abs(Date.parse(dates[lo]) - x)) lo -= 1;
    return lo;
  }
  function axisTs(ps) { return ps && ps.length ? (ps[0].axisValue !== undefined ? ps[0].axisValue : ps[0].value[0]) : NaN; }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function mode() { return window.PortfoliaTheme ? window.PortfoliaTheme.effective() : "light"; }
  function luminance(hex) {
    var h = (hex || "#888888").replace("#", "");
    var c = [0, 2, 4].map(function (i) {
      var v = parseInt(h.substr(i, 2), 16) / 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  // Beschriftung in farbiger Fläche: Weiß oder Tinte, je nachdem was mehr Kontrast hat
  function onFill(hex) {
    var L = luminance(hex);
    return (1.05 / (L + 0.05)) >= ((L + 0.05) / 0.0533) ? "#ffffff" : "#0b0b0b";
  }
  function tok() {
    var cs = getComputedStyle(document.documentElement);
    function g(n) { return cs.getPropertyValue(n).trim(); }
    return {
      mode: mode(), surface: g("--surface"), ink: g("--ink"), ink2: g("--ink-2"), muted: g("--muted"),
      grid: g("--grid"), axis: g("--axis"), s1: g("--series-1"), s2: g("--series-2"), s3: g("--series-3"),
      s4: g("--series-4"), pos: g("--div-pos"), neg: g("--div-neg"), mid: g("--div-mid"), up: g("--up"), down: g("--down"),
      border: g("--border"), warn: g("--warn-mark"),
    };
  }

  function tooltipBase(t) {
    return {
      backgroundColor: t.surface, borderColor: t.border, borderWidth: 1, padding: [8, 10],
      textStyle: { color: t.ink, fontSize: 12, fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif" },
      extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,.12); border-radius: 8px;", confine: true,
    };
  }
  function axisCommon(t) {
    return {
      axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false },
      axisLabel: { color: t.muted, fontSize: 11 }, splitLine: { lineStyle: { color: t.grid, width: 1, type: "solid" } },
    };
  }
  function lineKey(color) {
    return '<span style="display:inline-block;width:12px;height:2px;background:' + color + ';vertical-align:middle;margin-right:6px"></span>';
  }
  function row(color, label, value) {
    return '<div style="display:flex;justify-content:space-between;gap:14px;align-items:center">' +
      '<span style="color:' + tok().ink2 + '">' + (color ? lineKey(color) : "") + esc(label) + "</span><b>" + value + "</b></div>";
  }

  function empty(el, text) {
    var inst = instances.get(el);
    if (inst) { inst.dispose(); instances.delete(el); }
    el.innerHTML = '<div class="chart-empty">' + esc(text) + "</div>";
  }

  function getInstance(el) {
    var inst = instances.get(el);
    if (inst && !inst.isDisposed()) return inst;
    el.innerHTML = "";
    inst = echarts.init(el, null, { locale: "DE", renderer: "canvas" });
    instances.set(el, inst);
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(function () { if (!inst.isDisposed()) inst.resize(); });
      ro.observe(el);
      el._ro = ro;
    }
    return inst;
  }

  function buildUrl(el) {
    var src = el.dataset.src;
    var u = new URL(src, location.origin);
    ["range", "kind", "mode", "level", "expand", "color"].forEach(function (k) {
      if (el.dataset[k] !== undefined && el.dataset[k] !== "") u.searchParams.set(k, el.dataset[k]);
    });
    return u.pathname + u.search;
  }

  function load(el, keepFrame) {
    var type = el.dataset.chart;
    var builder = BUILDERS[type];
    if (builder && el.dataset.inline && !el.dataset.src) {  // Daten liegen im Dokument (kein Abruf nötig)
      try { el._data = JSON.parse(document.getElementById(el.dataset.inline).textContent); }
      catch (e) { return empty(el, "Diagrammdaten ungültig."); }
      return builder(el, el._data);
    }
    if (!builder || !el.dataset.src) return;
    el.classList.add("loading");
    var url = buildUrl(el);
    fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (data) {
      el._data = data;
      el.classList.remove("loading");
      builder(el, data);
    }).catch(function (err) {
      el.classList.remove("loading");
      if (!keepFrame) empty(el, "Daten konnten nicht geladen werden (" + err.message + ").");
    });
  }

  function rerender(el) {
    var b = BUILDERS[el.dataset.chart];
    if (b && el._data) b(el, el._data);
  }

  // ------------------------------------------------------------------------------------------------
  // Allokation: Sunburst (Segment → Kategorie → Position) oder Donut
  function allocation(el, data) {
    var t = tok();
    if (!data.data || !data.data.length) return empty(el, "Keine bewerteten Positionen.");
    var m = t.mode;
    var total = data.total;
    var narrow = el.clientWidth < 460;
    function conv(n, depth) {
      var col = (n.colors || {})[m] || t.muted;
      var o = { name: n.name, value: n.value, _id: n.id, _share: n.share, _other: !!n.other, _members: n.members || [],
        itemStyle: { color: col }, label: { color: onFill(col) } };
      if (n.children) o.children = n.children.map(function (c) { return conv(c, depth + 1); });
      return o;
    }
    var inst = getInstance(el);
    var tooltip = Object.assign(tooltipBase(t), {
      trigger: "item",
      formatter: function (p) {
        var d = p.data || {};
        var s = "<b>" + esc(p.name) + "</b>" + row(null, "Wert", eur(p.value)) + row(null, "Anteil", share(d._share || 0));
        if (d._other && d._members.length) {
          s += '<div style="margin-top:6px;color:' + t.ink2 + ';max-width:240px;white-space:normal">' + esc(d._members.join(", ")) +
            "</div><div style=\"margin-top:4px;color:" + t.ink2 + "\">Klicken zum Aufklappen</div>";
        }
        return s;
      },
    });
    var opt;
    if (el.dataset.mode === "donut") {
      opt = {
        tooltip: tooltip,
        series: [{
          type: "pie", radius: narrow ? ["42%", "68%"] : ["46%", "74%"], center: ["50%", "50%"], avoidLabelOverlap: true,
          minShowLabelAngle: 6,
          itemStyle: { borderColor: t.surface, borderWidth: 2, borderRadius: 3 },
          label: {
            color: t.ink2, fontSize: 11, lineHeight: 14,
            formatter: function (p) { return "{n|" + p.name + "}\n" + share(p.data._share) + " · " + eurCompact(p.value); },
            rich: { n: { color: t.ink, fontWeight: 600, fontSize: 11 } },
          },
          labelLine: { lineStyle: { color: t.axis }, length: 8, length2: 8 },
          data: data.data.map(function (n) { return conv(n, 1); }),
        }],
        graphic: [{ type: "text", left: "center", top: "middle", style: { text: "Gesamt\n" + eurCompact(total), fill: t.ink, fontSize: 14, fontWeight: 600, align: "center", lineHeight: 20 } }],
      };
    } else {
      opt = {
        tooltip: tooltip,
        series: [{
          type: "sunburst", radius: narrow ? ["20%", "96%"] : ["19%", "94%"], sort: null, nodeClick: false,
          data: data.data.map(function (n) { return conv(n, 1); }),
          itemStyle: { borderColor: t.surface, borderWidth: 2 },
          emphasis: { focus: "ancestor" },
          levels: [
            {},
            { r0: narrow ? "20%" : "19%", r: narrow ? "42%" : "39%", label: { rotate: 0, fontSize: 11, fontWeight: 600, minAngle: 18,
              formatter: function (p) { return p.name + "\n" + share(p.data._share); } } },
            { r0: narrow ? "42%" : "39%", r: narrow ? "64%" : "61%", label: { rotate: "tangential", fontSize: 10, minAngle: 16,
              formatter: function (p) { return p.name.length > 18 ? p.name.slice(0, 17) + "…" : p.name; } } },
            { r0: narrow ? "64%" : "61%", r: narrow ? "96%" : "94%", label: { rotate: "radial", fontSize: 10, minAngle: 7,
              formatter: function (p) { var n = p.name.length > 14 ? p.name.slice(0, 13) + "…" : p.name; return n + " " + share(p.data._share); } } },
          ],
        }],
        graphic: [{ type: "text", left: "center", top: "middle", style: { text: "Gesamt\n" + eurCompact(total), fill: t.ink, fontSize: narrow ? 10 : 12, fontWeight: 650, align: "center", lineHeight: 15 } }],
      };
    }
    inst.setOption(opt, true);
    inst.off("click");
    inst.on("click", function (p) {
      var d = p.data || {};
      var id = d._id || "";
      if (d._other) {
        var key = id.replace(/^other:/, "");
        var cur = (el.dataset.expand || "").split(",").filter(Boolean);
        if (cur.indexOf(key) < 0) cur.push(key);
        el.dataset.expand = el.dataset.mode === "donut" ? "all" : cur.join(",");
        load(el, true);
        return;
      }
      if (id.indexOf("seg:") === 0 || id.indexOf("cat:") === 0) {
        if (el.dataset.mode !== "donut") inst.dispatchAction({ type: "sunburstRootToNode", targetNode: p.dataIndex });
        return;
      }
      if (p.treePathInfo && p.treePathInfo.length === 1) { // Mitte: zurück zur Wurzel
        inst.dispatchAction({ type: "sunburstRootToNode", targetNode: 0 });
        return;
      }
      if (id && window.PortfoliaPanel) {
        var q = encodeURIComponent(id);
        window.PortfoliaPanel.open("/panel/asset/" + q, p.name, "/asset/" + q);
      }
    });
  }

  // ------------------------------------------------------------------------------------------------
  // Wertentwicklung: Depotwert vs. eingesetztes Kapital
  function history(el, d) {
    var t = tok();
    if (!d.dates || d.dates.length < 2) return empty(el, "Noch keine Historie – historische Kurse werden im Hintergrund geladen.");
    var inst = getInstance(el);
    var val = d.dates.map(function (x, i) { return [x, d.value[i]]; });
    var inv = d.dates.map(function (x, i) { return [x, d.invested[i]]; });
    inst.setOption({
      animation: false,
      grid: { left: 8, right: el.clientWidth > 420 ? 78 : 16, top: 16, bottom: 8, containLabel: true },
      tooltip: Object.assign(tooltipBase(t), {
        trigger: "axis", axisPointer: { type: "line", lineStyle: { color: t.axis } },
        formatter: function (ps) {
          var i = nearestIndex(d.dates, axisTs(ps));
          if (i < 0) return "";
          var v = d.value[i], c = d.invested[i];
          var s = "<b>" + fdate(d.dates[i]) + "</b>";
          s += row(t.s1, "Depotwert", v === null || v === undefined ? "–" : eur(v)) +
            row(t.muted, "Eingesetztes Kapital", c === null || c === undefined ? "–" : eur(c));
          if (v !== null && v !== undefined && c !== null && c !== undefined) s += row(null, "Gewinn/Verlust", eur(v - c));
          return s;
        },
      }),
      xAxis: Object.assign({ type: "time", boundaryGap: false }, axisCommon(t), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", scale: true }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11, formatter: eurCompact } }),
      series: [
        { name: "Depotwert", type: "line", data: val, showSymbol: false, lineStyle: { width: 2, color: t.s1 }, itemStyle: { color: t.s1 },
          areaStyle: { color: t.s1, opacity: 0.1 }, sampling: "lttb",
          endLabel: { show: el.clientWidth > 420, color: t.ink, fontSize: 11, formatter: function (p) { return eurCompact(p.value[1]); } } },
        { name: "Eingesetztes Kapital", type: "line", step: "end", data: inv, showSymbol: false, lineStyle: { width: 1.5, color: t.muted }, itemStyle: { color: t.muted } },
      ],
    }, true);
  }

  // ------------------------------------------------------------------------------------------------
  // Kursverlauf mit Kauf-/Verkaufsmarkern und Ø-Einstand
  function price(el, d) {
    var t = tok();
    var pts = d.points || [];
    if (pts.length < 2) return empty(el, d.intraday ? "Keine Intraday-Daten verfügbar." : "Keine Kursdaten für diesen Zeitraum.");
    var inst = getInstance(el);
    var candle = el.dataset.kind === "candle" && d.candles && d.candles.length > 1;
    var series = [];
    if (candle) {
      series.push({ name: "Kurs", type: "candlestick", data: d.candles.map(function (c) { return [c[0], c[1], c[2], c[3], c[4]]; }),
        itemStyle: { color: t.pos, color0: t.neg, borderColor: t.pos, borderColor0: t.neg }, barMaxWidth: 12 });
    } else {
      series.push({ name: "Kurs", type: "line", data: pts.map(function (p) { return [p[0], p[1], p[2]]; }), showSymbol: false, sampling: "lttb",
        lineStyle: { width: 2, color: t.s1 }, itemStyle: { color: t.s1 }, areaStyle: { color: t.s1, opacity: 0.08 } });
    }
    var buys = (d.markers || []).filter(function (m) { return m.side === "buy"; });
    var sells = (d.markers || []).filter(function (m) { return m.side === "sell"; });
    function mk(list, name, color, rot) {
      return { name: name, type: "scatter", symbol: "triangle", symbolRotate: rot, symbolSize: 12, z: 5,
        itemStyle: { color: color, borderColor: t.surface, borderWidth: 2 },
        data: list.map(function (m) { return { value: [m.t, m.price], _m: m }; }) };
    }
    if (buys.length) series.push(mk(buys, "Kauf", t.s2, 0));
    if (sells.length) series.push(mk(sells, "Verkauf", t.s3, 180));
    var graphic = [];
    if (d.avg_cost) {
      var vals = pts.map(function (p) { return p[1]; }).filter(function (v) { return v !== null; });
      var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
      if (d.avg_cost < lo * 0.85 || d.avg_cost > hi * 1.15) {
        graphic.push({ type: "text", left: 56, top: 2, style: { text: "Ø Einstand " + eur(d.avg_cost) + (d.avg_cost < lo ? " (unterhalb)" : " (oberhalb)"),
          fill: t.ink2, fontSize: 11 } });
      }
      series[0].markLine = { symbol: "none", silent: true, lineStyle: { color: t.ink2, width: 1, type: [4, 3] },
        label: { position: "insideEndTop", color: t.ink2, fontSize: 11, formatter: "Ø Einstand " + eur(d.avg_cost) },
        data: [{ yAxis: d.avg_cost }] };
    }
    inst.setOption({
      animation: false,
      grid: { left: 8, right: 16, top: 20, bottom: 8, containLabel: true },
      tooltip: Object.assign(tooltipBase(t), {
        trigger: "axis", axisPointer: { type: "line", lineStyle: { color: t.axis } },
        formatter: function (ps) {
          var first = ps[0];
          var dt = first.value[0];
          var s = "<b>" + (d.intraday ? fdt(dt) : fdate(dt)) + "</b>";
          ps.forEach(function (p) {
            if (p.seriesType === "candlestick") {
              var v = p.value; // [idx?, o, c, l, h] ECharts liefert Kategorie + Werte
              var o = v[1], c = v[2], l = v[3], h = v[4];
              s += row(null, "Eröffnung", eur(o)) + row(null, "Hoch", eur(h)) + row(null, "Tief", eur(l)) + row(t.s1, "Schluss", eur(c));
            } else if (p.seriesType === "line") {
              s += row(t.s1, "Kurs", eur(p.value[1]));
              if (d.ccy && d.ccy !== "EUR" && p.value[2] != null) s += row(null, "Originalwährung", num(p.value[2], 4) + " " + d.ccy);
            } else if (p.data && p.data._m) {
              var m = p.data._m;
              s += '<div style="margin-top:6px"><b>' + (m.side === "buy" ? "Kauf" : "Verkauf") + "</b></div>" +
                row(null, "Menge", num(m.qty, 6)) + row(null, "Kurs", eur(m.price)) + row(null, "Wert", eur(m.value));
            }
          });
          return s;
        },
      }),
      xAxis: Object.assign({ type: "time", boundaryGap: candle }, axisCommon(t), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", scale: true }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11, formatter: function (v) { return eur(v, Math.abs(v) >= 100 ? 0 : undefined); } } }),
      series: series,
      graphic: graphic,
    }, true);
  }

  // ------------------------------------------------------------------------------------------------
  // Positionsverlauf: Stück und Wert als zwei Diagramme (keine Doppelachse)
  function position(el, d) {
    var t = tok();
    if (!d.dates || d.dates.length < 2) return empty(el, "Kein Positionsverlauf vorhanden.");
    var inst = getInstance(el);
    var q = d.dates.map(function (x, i) { return [x, d.qty[i]]; });
    var v = d.dates.map(function (x, i) { return [x, d.value[i]]; });
    inst.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [{ left: 8, right: 16, top: 22, height: "34%", containLabel: true }, { left: 8, right: 16, top: "58%", bottom: 8, containLabel: true }],
      title: [{ text: "Stück", left: 4, top: 0, textStyle: { fontSize: 11, color: t.ink2, fontWeight: 600 } },
              { text: "Wert", left: 4, top: "50%", textStyle: { fontSize: 11, color: t.ink2, fontWeight: 600 } }],
      tooltip: Object.assign(tooltipBase(t), {
        trigger: "axis",
        formatter: function (ps) {
          var i = nearestIndex(d.dates, axisTs(ps));
          if (i < 0) return "";
          var qv = d.qty[i], vv = d.value[i];
          return "<b>" + fdate(d.dates[i]) + "</b>" + row(t.s1, "Stück", qv === null || qv === undefined ? "–" : num(qv, 8)) +
            row(t.s1, "Wert", vv === null || vv === undefined ? "–" : eur(vv));
        },
      }),
      xAxis: [Object.assign({ type: "time", gridIndex: 0 }, axisCommon(t), { axisLabel: { show: false }, splitLine: { show: false } }),
              Object.assign({ type: "time", gridIndex: 1 }, axisCommon(t), { splitLine: { show: false } })],
      yAxis: [Object.assign({ type: "value", gridIndex: 0, scale: true, splitNumber: 3 }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 10, formatter: function (x) { return num(x, 4); } } }),
              Object.assign({ type: "value", gridIndex: 1, scale: true, splitNumber: 3 }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 10, formatter: eurCompact } })],
      series: [
        { type: "line", step: "end", xAxisIndex: 0, yAxisIndex: 0, data: q, showSymbol: false, lineStyle: { width: 2, color: t.s1 }, itemStyle: { color: t.s1 } },
        { type: "line", xAxisIndex: 1, yAxisIndex: 1, data: v, showSymbol: false, sampling: "lttb", lineStyle: { width: 2, color: t.s1 }, itemStyle: { color: t.s1 }, areaStyle: { color: t.s1, opacity: 0.1 } },
      ],
    }, true);
  }

  // ------------------------------------------------------------------------------------------------
  // Treemap: Fläche = Wert, Farbe = Tagesänderung (divergierend blau/rot, Mitte neutral)
  function mix(a, b, f) {
    function p(h) { h = h.replace("#", ""); return [parseInt(h.substr(0, 2), 16), parseInt(h.substr(2, 2), 16), parseInt(h.substr(4, 2), 16)]; }
    var x = p(a), y = p(b);
    var r = x.map(function (v, i) { return Math.round(v + (y[i] - v) * f); });
    return "#" + r.map(function (v) { return ("0" + v.toString(16)).slice(-2); }).join("");
  }
  function divColor(t, v, scale) {
    if (v === null || v === undefined) return t.mid;
    var f = Math.min(1, Math.abs(v) / (scale || 4));
    return mix(t.mid, v >= 0 ? t.pos : t.neg, 0.25 + 0.75 * f);
  }
  function treemap(el, d) {
    var t = tok();
    if (!d.data || !d.data.length) return empty(el, "Keine Positionen.");
    var inst = getInstance(el);
    var label = d.metric === "total" ? "G/V gesamt" : "Heute";
    var data = d.data.map(function (g) {
      return { name: g.name, children: g.children.map(function (c) {
        return { name: c.name, value: c.value, _c: c, itemStyle: { color: divColor(t, c.change_pct, d.scale) } };
      }) };
    });
    inst.setOption({
      tooltip: Object.assign(tooltipBase(t), {
        formatter: function (p) {
          var c = p.data && p.data._c;
          if (!c) return "<b>" + esc(p.name) + "</b>" + row(null, "Wert", eur(p.value));
          var html = "<b>" + esc(c.full_name) + "</b>" + row(null, "Wert", eur(c.value)) + row(null, "Gewicht", share(c.weight)) +
            row(null, label, c.change_pct === null ? "–" : pct(c.change_pct) + " (" + eur(c.change_eur) + ")");
          if (c.members) html += "<div style='max-width:240px;white-space:normal'>" + esc(c.members.join(", ")) + "</div>";
          return html;
        },
      }),
      series: [{
        type: "treemap", roam: false, nodeClick: false, breadcrumb: { show: false }, top: 4, left: 4, right: 4, bottom: 4,
        visibleMin: 80, childrenVisibleMin: 40,
        itemStyle: { borderColor: t.surface, borderWidth: 2, gapWidth: 2 },
        upperLabel: { show: true, height: 20, color: t.ink, fontWeight: 600, fontSize: 11 },
        label: { show: true, fontSize: 11, color: t.ink, overflow: "truncate",
          formatter: function (p) { var c = p.data && p.data._c; return c ? p.name + "\n" + (c.change_pct === null ? "–" : pct(c.change_pct)) : p.name; } },
        levels: [{ itemStyle: { borderColor: t.surface, borderWidth: 0, gapWidth: 4 }, upperLabel: { show: false } },
                 { itemStyle: { borderColor: t.grid, borderWidth: 2, gapWidth: 2 }, upperLabel: { show: true, color: t.ink2, backgroundColor: "transparent" } }, {}],
        data: data,
      }],
    }, true);
    inst.off("click");
    inst.on("click", function (p) {
      var c = p.data && p.data._c;
      if (c && c.other) {  // „Sonstige“ aufklappen
        el.dataset.expand = "all";
        load(el, true);
        return;
      }
      if (c && window.PortfoliaPanel) {
        var q = encodeURIComponent(c.id);
        window.PortfoliaPanel.open("/panel/asset/" + q, c.full_name, "/asset/" + q);
      }
    });
  }

  // Zeilen einer ECharts-Legende (Symbol 14 px + 5 px Abstand + Text + 10 px Lücke, Schrift 12 px)
  var LEGEND_FONT = "system-ui, -apple-system, Segoe UI, sans-serif";
  function legendLines(names, width) {
    var c = legendLines.ctx || (legendLines.ctx = document.createElement("canvas").getContext("2d"));
    c.font = "12px " + LEGEND_FONT;
    var lines = 1, x = 0;
    names.forEach(function (n) {
      var w = 14 + 5 + c.measureText(n).width + 10;
      if (x > 0 && x + w > width) { lines += 1; x = 0; }
      x += w;
    });
    return lines;
  }

  // ------------------------------------------------------------------------------------------------
  // Performance: kumulierte Rendite vs. Benchmark
  function perf(el, d) {
    var t = tok();
    if (!d.dates || d.dates.length < 2) return empty(el, "Nicht genügend Historie.");
    var inst = getInstance(el);
    var colors = [t.s1, t.s2, t.s3, t.s4];
    var series = [{ name: "Portfolio (TTWROR)", values: d.portfolio, data: d.dates.map(function (x, i) { return [x, d.portfolio[i]]; }) }];
    (d.benchmarks || []).forEach(function (b) {
      if (b.values.some(function (v) { return v !== null; })) series.push({ name: b.name, values: b.values, data: d.dates.map(function (x, i) { return [x, b.values[i]]; }) });
    });
    inst.setOption({
      animation: false,
      color: colors,
      legend: { top: 0, left: 0, textStyle: { color: t.ink2, fontSize: 12, fontFamily: LEGEND_FONT }, icon: "path://M0,4 L14,4 L14,6 L0,6 Z", itemWidth: 14, itemHeight: 4 },
      // Platz für die (auf schmalen Bildschirmen umbrechende) Legende, sonst überdeckt sie die Achse
      grid: { left: 8, right: 16, top: 12 + 22 * legendLines(series.map(function (x) { return x.name; }), el.clientWidth - 10), bottom: 8, containLabel: true },
      tooltip: Object.assign(tooltipBase(t), {
        trigger: "axis",
        formatter: function (ps) {
          var i = nearestIndex(d.dates, axisTs(ps));
          if (i < 0) return "";
          var s = "<b>" + fdate(d.dates[i]) + "</b>";
          series.forEach(function (sr, k) { s += row(colors[k], sr.name, pct(sr.values[i])); });
          return s;
        },
      }),
      xAxis: Object.assign({ type: "time" }, axisCommon(t), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", scale: true }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11, formatter: function (v) { return pct(v, 0); } } }),
      series: series.map(function (s, i) {
        return { name: s.name, type: "line", data: s.data, showSymbol: false, sampling: "lttb", connectNulls: true,
          lineStyle: { width: i === 0 ? 2 : 1.5, color: colors[i] }, itemStyle: { color: colors[i] },
          markLine: i === 0 ? { symbol: "none", silent: true, label: { show: false }, lineStyle: { color: t.axis, width: 1, type: "solid" }, data: [{ yAxis: 0 }] } : undefined };
      }),
    }, true);
  }

  function drawdown(el, d) {
    var t = tok();
    if (!d.dates || d.dates.length < 2) return empty(el, "Nicht genügend Historie.");
    var inst = getInstance(el);
    inst.setOption({
      animation: false,
      grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
      tooltip: Object.assign(tooltipBase(t), { trigger: "axis", formatter: function (ps) { return "<b>" + fdate(ps[0].value[0]) + "</b>" + row(t.neg, "Drawdown", pct(ps[0].value[1])); } }),
      xAxis: Object.assign({ type: "time" }, axisCommon(t), { splitLine: { show: false } }),
      yAxis: Object.assign({ type: "value", max: 0 }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11, formatter: function (v) { return pct(v, 0); } } }),
      series: [{ type: "line", data: d.dates.map(function (x, i) { return [x, d.drawdown[i]]; }), showSymbol: false, sampling: "lttb",
        lineStyle: { width: 1.5, color: t.neg }, itemStyle: { color: t.neg }, areaStyle: { color: t.neg, opacity: 0.1 } }],
    }, true);
  }

  function annual(el, d) {
    var t = tok();
    var ys = d.years || [];
    if (!ys.length) return empty(el, "Nicht genügend Historie.");
    var inst = getInstance(el);
    inst.setOption({
      animation: false,
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      tooltip: Object.assign(tooltipBase(t), { trigger: "item", formatter: function (p) {
        var y = ys[p.dataIndex];
        return "<b>" + y.year + (y.partial ? " (Teiljahr)" : "") + "</b>" + row(null, "TTWROR", pct(y.ttwror)) + row(null, "G/V", eur(y.gain));
      } }),
      xAxis: Object.assign({ type: "category", data: ys.map(function (y) { return String(y.year) + (y.partial ? "*" : ""); }) }, axisCommon(t)),
      yAxis: Object.assign({ type: "value" }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11, formatter: function (v) { return pct(v, 0); } } }),
      series: [{ type: "bar", barMaxWidth: 24,
        data: ys.map(function (y) {
          var neg = y.ttwror < 0;
          return { value: y.ttwror, itemStyle: { color: neg ? t.neg : t.pos, opacity: y.partial ? 0.6 : 1, borderRadius: neg ? [0, 0, 4, 4] : [4, 4, 0, 0] } };
        }),
        label: { show: true, position: "top", color: t.ink2, fontSize: 11, formatter: function (p) { return pct(p.value, 1); } },
        labelLayout: function (p) { return p.value < 0 ? { dy: 16 } : {}; },
      }],
    }, true);
  }

  // Wasserfall horizontal: Beiträge je Position (von…bis je Balken), Summe am Ende. Custom-Serie, damit auch
  // negative Zwischensummen korrekt gezeichnet werden.
  function waterfall(el, d) {
    var t = tok();
    var items = d.items || [];
    if (!items.length) return empty(el, "Keine Beiträge im Zeitraum.");
    var inst = getInstance(el);
    var rows = [], cum = 0;
    items.forEach(function (it, i) {
      rows.push({ name: it.name, start: cum, end: cum + it.gain, gain: it.gain, total: false });
      cum += it.gain;
    });
    rows.push({ name: "Summe", start: 0, end: cum, gain: cum, total: true });
    var names = rows.map(function (r) { return r.name; });
    var data = rows.map(function (r, i) { return [i, r.start, r.end, r.gain, r.total ? 1 : 0]; });
    inst.setOption({
      animation: false,
      grid: { left: 8, right: 70, top: 8, bottom: 8, containLabel: true },
      tooltip: Object.assign(tooltipBase(t), { trigger: "item", formatter: function (p) {
        var r = rows[p.dataIndex];
        return "<b>" + esc(r.name) + "</b>" + row(null, r.total ? "G/V gesamt" : "Beitrag", eur(r.gain)) +
          (r.total ? "" : row(null, "Kumuliert", eur(r.end)));
      } }),
      yAxis: Object.assign({ type: "category", inverse: true, data: names }, axisCommon(t),
        { axisLabel: { color: t.ink2, fontSize: 11, width: 140, overflow: "truncate" }, splitLine: { show: false } }),
      xAxis: Object.assign({ type: "value", scale: false }, axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11, formatter: eurCompact } }),
      series: [{
        type: "custom", encode: { x: [1, 2], y: 0 }, data: data,
        renderItem: function (params, api) {
          var yi = api.value(0), x0 = api.value(1), x1 = api.value(2), g = api.value(3), isTotal = api.value(4) === 1;
          var a = api.coord([x0, yi]), b = api.coord([x1, yi]);
          var bandH = api.size([0, 1])[1];
          var h = Math.min(18, bandH * 0.62);
          var left = Math.min(a[0], b[0]), w = Math.max(1, Math.abs(b[0] - a[0]));
          var color = isTotal ? t.ink2 : (g >= 0 ? t.pos : t.neg);
          var labelX = Math.max(a[0], b[0]) + 6;
          return { type: "group", children: [
            { type: "rect", shape: { x: left, y: a[1] - h / 2, width: w, height: h, r: 3 }, style: { fill: color } },
            { type: "text", x: labelX, y: a[1], style: { text: (g >= 0 ? "+" : "−") + eurCompact(Math.abs(g)), fill: isTotal ? t.ink : t.ink2,
              fontSize: 11, fontWeight: isTotal ? 600 : 400, verticalAlign: "middle" } },
          ] };
        },
      }],
    }, true);
  }


  // ------------------------------------------------------------------------------------------------
  // Abweichungsfenster (Diagnose): Referenzbestände sind Nachweiszeitpunkte – nur Marker, keine verbindende Linie.
  // Oben: Bestand laut Referenz (gefüllt) und laut Ledger (Ring) je Zeitpunkt; unten: Differenz Referenz − Ledger.
  // Getönte Bänder = Fenster zwischen zwei Nachweisen; Klick auf Band oder Marker öffnet die Details darunter.
  var WIN_TINT = { erstmals: "neg", veraendert: "warn", stabil: "s1", verschwindet: "pos", nicht_eingrenzbar: "mid", ok: null };
  function devwindows(el, d) {
    var t = tok();
    var pts = d.points || [];
    if (!pts.length) return empty(el, "Kein Referenzbestand.");
    var inst = getInstance(el);
    var narrow = el.clientWidth < 520;
    var xs = pts.map(function (p) { return Date.parse(p.t); });
    var span = Math.max(1, Math.max.apply(null, xs) - Math.min.apply(null, xs));
    var pad = Math.max(span * 0.06, 86400000 * 2);
    var xmin = Math.min.apply(null, xs) - pad, xmax = Math.max.apply(null, xs) + pad;
    function fmt(v) { return num(v, d.fiat ? 2 : 8); }
    var refs = pts.map(function (p, i) { return { value: [xs[i], Number(p.qty)], _p: p }; });
    var leds = pts.map(function (p, i) { return { value: [xs[i], Number(p.ledger)], _p: p }; });
    var diffs = pts.map(function (p, i) { return { value: [xs[i], Number(p.diff)], _p: p }; });
    var areas = (d.segs || []).filter(function (s) { return WIN_TINT[s.kind]; }).map(function (s) {
      var c = t[WIN_TINT[s.kind]] || t.mid;
      var a = s.start ? Date.parse(s.start) : xmin;
      return [{ xAxis: a, name: s.label, _s: s, itemStyle: { color: c, opacity: s.kind === "nicht_eingrenzbar" ? 0.07 : 0.14 } },
              { xAxis: Date.parse(s.end) }];
    });
    function tipPoint(p) {
      var s = "<b>" + esc(p.when) + "</b><div style='color:" + t.ink2 + "'>" + esc(p.basis) + "</div>";
      s += row(null, "Referenz", fmt(p.qty)) + row(null, "Ledger", fmt(p.ledger)) + row(null, "Differenz", fmt(p.diff));
      s += "<div style='color:" + t.ink2 + "'>" + esc(p.src) + (p.conflict ? " · widersprüchlich" : "") + "</div>";
      return s;
    }
    var axisX = function (gi) {
      return Object.assign({ type: "time", gridIndex: gi, min: xmin, max: xmax }, axisCommon(t),
        { splitLine: { show: false }, axisLabel: { color: t.muted, fontSize: 11, show: gi === 1, hideOverlap: true } });
    };
    inst.setOption({
      animation: false,
      legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 12, textStyle: { color: t.ink2, fontSize: 12, fontFamily: LEGEND_FONT },
        data: [{ name: "Referenz (Nachweis)", icon: "circle" }, { name: "Ledger zum selben Zeitpunkt", icon: "circle", itemStyle: { color: t.surface, borderColor: t.ink2, borderWidth: 2 } },
               { name: "Differenz", icon: "rect" }] },
      grid: [{ left: 8, right: 16, top: narrow ? 64 : 40, height: "32%", containLabel: true },
             { left: 8, right: 16, top: "62%", bottom: 28, containLabel: true }],
      xAxis: [axisX(0), axisX(1)],
      yAxis: [Object.assign({ type: "value", gridIndex: 0, scale: true }, axisCommon(t)),
              Object.assign({ type: "value", gridIndex: 1, scale: true }, axisCommon(t))],
      tooltip: Object.assign(tooltipBase(t), { trigger: "item", formatter: function (p) {
        if (p.componentType === "markArea") {
          var s = p.data && p.data._s;
          if (!s) return "";
          return "<b>" + esc(s.label) + "</b>" + row(null, "Änderung der Differenz", fmt(s.delta)) + row(null, "Buchungen im Fenster", String(s.n_tx)) +
            "<div style='color:" + t.ink2 + "'>Klicken für Buchungen und Kandidaten</div>";
        }
        return p.data && p.data._p ? tipPoint(p.data._p) : "";
      } }),
      series: [
        { name: "Referenz (Nachweis)", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: refs, symbol: "circle", symbolSize: 9,
          itemStyle: { color: t.s1, borderColor: t.surface, borderWidth: 2 }, z: 3 },
        { name: "Ledger zum selben Zeitpunkt", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, data: leds, symbol: "circle", symbolSize: 15,
          itemStyle: { color: t.surface, borderColor: t.ink2, borderWidth: 2 }, z: 2 },
        { name: "Differenz", type: "bar", xAxisIndex: 1, yAxisIndex: 1, barWidth: 3, z: 1,
          data: diffs.map(function (x) { return { value: x.value, _p: x._p, itemStyle: { color: t.s1, borderRadius: Number(x.value[1]) < 0 ? [0, 0, 2, 2] : [2, 2, 0, 0] } }; }),
          markArea: { silent: false, data: areas, label: { show: !narrow, color: t.ink2, fontSize: 10, position: "insideTop", formatter: function (p) { return p.name; } } },
          markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: t.axis, width: 1, type: "solid" }, data: [{ yAxis: 0 }] } },
        { name: "Differenz", type: "scatter", xAxisIndex: 1, yAxisIndex: 1, data: diffs, symbol: "circle", symbolSize: 9,
          itemStyle: { color: t.s1, borderColor: t.surface, borderWidth: 2 }, z: 4, tooltip: {} },
      ],
    }, true);
    inst.off("click");
    inst.on("click", function (p) {
      var key = p.componentType === "markArea" ? (p.data && p.data._s && p.data._s.key) : (p.data && p.data._p && p.data._p.seg);
      if (!key) return;
      var target = document.getElementById("win-" + key);
      if (!target) return;
      if (target.tagName === "DETAILS") target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }

  var BUILDERS = { devwindows: devwindows, allocation: allocation, history: history, price: price, position: position, treemap: treemap,
    perf: perf, drawdown: drawdown, annual: annual, waterfall: waterfall };

  function initAll(root) {
    (root || document).querySelectorAll("[data-chart]").forEach(function (el) {
      if (el._inited) return;
      if (el.dataset.lazy !== undefined && el.offsetParent === null) return;  // in geschlossenem Bereich: beim Öffnen
      el._inited = true;
      load(el);
    });
  }
  function disposeIn(root) {
    root.querySelectorAll("[data-chart]").forEach(function (el) {
      var inst = instances.get(el);
      if (inst) { inst.dispose(); instances.delete(el); }
      if (el._ro) el._ro.disconnect();
    });
  }

  window.PortfoliaCharts = {
    initAll: initAll, reload: function (el, keep) { load(el, keep); }, disposeIn: disposeIn, register: function (name, fn) { BUILDERS[name] = fn; },
    util: { eur: eur, eurCompact: eurCompact, pct: pct, share: share, num: num, fdate: fdate, esc: esc, tok: tok, row: row,
      tooltipBase: tooltipBase, axisCommon: axisCommon, getInstance: getInstance, empty: empty },
  };
  document.addEventListener("portfolia:privacy", function () {
    instances.forEach(function (inst, el) { rerender(el); });
  });
  document.addEventListener("portfolia:theme", function () {
    instances.forEach(function (inst, el) { rerender(el); });
  });
  document.addEventListener("DOMContentLoaded", function () { initAll(document); });
  document.addEventListener("toggle", function (e) {  // Diagramme in <details> erst beim Aufklappen zeichnen
    if (e.target.tagName === "DETAILS" && e.target.open) initAll(e.target);
  }, true);
})();
