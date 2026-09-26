/* Steuern & Haltefristen: Diagramm „Freigaben der nächsten 12 Monate“ (Balken, Wert in EUR). */
(function () {
  "use strict";
  if (!window.PortfoliaCharts) return;
  var U = window.PortfoliaCharts.util;
  var MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];

  function label(m) {
    var p = m.split("-");
    return MONTHS[parseInt(p[1], 10) - 1] + " " + p[0].slice(2);
  }

  window.PortfoliaCharts.register("releases", function (el, d) {
    var t = U.tok();
    var ms = d.months || [];
    if (!ms.length) return U.empty(el, "Keine Haltefristen enden in den nächsten 12 Monaten.");
    var inst = U.getInstance(el);
    inst.setOption({
      animation: false,
      grid: { left: 8, right: 12, top: 18, bottom: 8, containLabel: true },
      tooltip: Object.assign(U.tooltipBase(t), { trigger: "item", formatter: function (p) {
        var m = ms[p.dataIndex];
        return "<b>" + U.esc(label(m.month)) + "</b>" + U.row(null, "wird steuerfrei", U.eur(m.value)) +
          U.row(null, "davon unrealisierter G/V", U.eur(m.gain)) + U.row(null, "Lots", String(m.count));
      } }),
      xAxis: Object.assign({ type: "category", data: ms.map(function (m) { return label(m.month); }) }, U.axisCommon(t)),
      yAxis: Object.assign({ type: "value" }, U.axisCommon(t), { axisLabel: { color: t.muted, fontSize: 11,
        formatter: function (v) { return U.eurCompact(v); } } }),
      series: [{ type: "bar", barMaxWidth: 26, data: ms.map(function (m) {
        return { value: m.value, itemStyle: { color: t.s1, borderRadius: [4, 4, 0, 0] } };
      }) }],
    }, true);
  });
})();
