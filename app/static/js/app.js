/* Portfolia – UI-Logik ohne Build-Schritt (HTMX + Vanilla JS, CSP-konform ohne Inline-Skripte). */
(function () {
  "use strict";

  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  // --- Installation als App (Chrome/Firefox auf Android; nur über HTTPS bzw. localhost möglich) -------------
  if ("serviceWorker" in navigator && window.isSecureContext) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", {scope: "/"}).catch(function () { /* ohne Worker weiter */ });
    });
  }

  // --- HTMX: CSRF-Header an alle Anfragen ---------------------------------------------------
  document.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["X-CSRF-Token"] = csrf;
  });
  document.addEventListener("htmx:afterSettle", function (e) {
    if (window.PortfoliaCharts) window.PortfoliaCharts.initAll(e.target);
    applyDotColors(e.target);
  });
  document.addEventListener("htmx:responseError", function (e) {
    toast("Anfrage fehlgeschlagen (" + (e.detail.xhr ? e.detail.xhr.status : "?") + ")", "crit");
  });

  // --- Theme ------------------------------------------------------------------------------------
  function effectiveTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "light" || t === "dark") return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  window.PortfoliaTheme = { effective: effectiveTheme };

  function applyDotColors(root) {
    var mode = effectiveTheme();
    (root || document).querySelectorAll("[data-color-light]").forEach(function (el) {
      el.style.background = el.getAttribute(mode === "dark" ? "data-color-dark" : "data-color-light");
    });
  }

  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("portfolia-theme", t); } catch (e) { /* ignorieren */ }
    applyDotColors(document);
    document.dispatchEvent(new CustomEvent("portfolia:theme", { detail: { theme: t } }));
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-theme-toggle]");
    if (!b) return;
    setTheme(effectiveTheme() === "dark" ? "light" : "dark");
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      applyDotColors(document);
      document.dispatchEvent(new CustomEvent("portfolia:theme", { detail: { theme: effectiveTheme() } }));
    });
  }

  // --- Kopieren (öffentliche Adressen) -------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var text = b.getAttribute("data-copy") || "";
    var done = function () { toast("Adresse kopiert", "good"); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
    } else {
      fallbackCopy(text, done);
    }
  });
  function fallbackCopy(text, done) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (err) { toast("Kopieren nicht möglich", "crit"); }
    document.body.removeChild(ta);
  }

  // --- Mehr-Menü (mobil) ------------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var menu = document.getElementById("more-menu");
    var btn = e.target.closest("[data-more-toggle]");
    if (btn) {
      var open = !menu.classList.contains("open");
      menu.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    if (menu && menu.classList.contains("open") && !e.target.closest("#more-menu")) menu.classList.remove("open");
  });

  // --- Detail-Panel (Desktop: Seitenpanel, Mobil: Vollbild-Sheet) ---------------------------------------
  var panel = document.getElementById("panel");
  var panelBody = document.getElementById("panel-body");
  var panelTitle = document.getElementById("panel-title");
  var panelFull = document.getElementById("panel-open-full");
  var panelOpen = false;

  function openPanel(url, title, fullHref) {
    if (!panel) { window.location.href = fullHref || url; return; }
    panelTitle.textContent = title || "Detail";
    panelFull.setAttribute("href", fullHref || url.replace("/panel/", "/"));
    panelBody.innerHTML = '<div class="row"><span class="spinner"></span> Lädt …</div>';
    document.body.classList.add("panel-open");
    panel.setAttribute("aria-hidden", "false");
    if (!panelOpen) {
      try { history.pushState({ panel: true }, "", "#detail"); } catch (e) { /* ignorieren */ }
    }
    panelOpen = true;
    htmx.ajax("GET", url, { target: "#panel-body", swap: "innerHTML" }).then(function () {
      panelBody.scrollTop = 0;
      var h = panel.querySelector(".panel-body h1");
      if (h) h.setAttribute("tabindex", "-1");
    });
  }
  function closePanel(fromHistory) {
    if (!panelOpen) return;
    panelOpen = false;
    document.body.classList.remove("panel-open");
    panel.setAttribute("aria-hidden", "true");
    if (window.PortfoliaCharts) window.PortfoliaCharts.disposeIn(panelBody);
    setTimeout(function () { if (!panelOpen) panelBody.innerHTML = ""; }, 250);
    if (!fromHistory && location.hash === "#detail") {
      try { history.back(); } catch (e) { /* ignorieren */ }
    }
  }
  window.PortfoliaPanel = { open: openPanel, close: closePanel };
  window.addEventListener("popstate", function () { if (panelOpen) closePanel(true); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePanel(false); });
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-panel-close]")) { e.preventDefault(); closePanel(false); return; }
    var trg = e.target.closest("[data-panel]");
    if (!trg || e.target.closest("a[href]:not([data-panel])")) return;
    if (e.ctrlKey || e.metaKey || e.shiftKey || e.button === 1) {
      var href = trg.getAttribute("href") || trg.getAttribute("data-href");
      if (href && trg.tagName !== "A") window.open(href, "_blank");
      return;
    }
    e.preventDefault();
    openPanel(trg.getAttribute("data-panel"), trg.getAttribute("data-title"),
      trg.getAttribute("href") || trg.getAttribute("data-href"));
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var trg = e.target.closest && e.target.closest("tr[data-panel]");
    if (trg) { e.preventDefault(); trg.click(); }
  });

  // --- Depotwert verbergen (Augen-Symbol auf der Übersicht; gilt je Gerät, gesetzt schon in theme.js) ----------
  function syncHideTotal() {
    var hidden = document.documentElement.classList.contains("hide-total");
    document.querySelectorAll("[data-hide-total]").forEach(function (b) {
      b.setAttribute("aria-pressed", hidden ? "true" : "false");
      var label = hidden ? "Depotwert anzeigen" : "Depotwert verbergen";
      b.setAttribute("aria-label", label);
      b.setAttribute("title", label);
    });
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-hide-total]")) return;
    var hidden = document.documentElement.classList.toggle("hide-total");
    try { localStorage.setItem("portfolia-hide-total", hidden ? "1" : "0"); } catch (err) { /* nur für diese Seite */ }
    syncHideTotal();
  });
  document.body.addEventListener("htmx:afterSwap", syncHideTotal);
  syncHideTotal();

  // --- Reiter: aktiven Reiter in horizontal scrollenden Leisten sichtbar machen ---------------------------
  document.querySelectorAll(".tabs a.active").forEach(function (a) {
    var bar = a.parentElement;
    if (bar && bar.scrollWidth > bar.clientWidth) bar.scrollLeft = a.offsetLeft - (bar.clientWidth - a.clientWidth) / 2;
  });

  // --- Formulare: automatisch absenden, Rückfrage vor dem Absenden ------------------------------------
  document.addEventListener("change", function (e) {
    var el = e.target.closest("[data-autosubmit]");
    if (el && el.form) el.form.submit();
  });
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // --- Mehrfachauswahl: „alle“-Kästchen und Zähler (data-check-all / data-check-count = Feldname) -------
  function syncChecks(form, name) {
    var boxes = form.querySelectorAll('input[type="checkbox"][name="' + name + '"]');
    var n = 0;
    boxes.forEach(function (b) { if (b.checked) n++; });
    form.querySelectorAll('[data-check-count="' + name + '"]').forEach(function (el) { el.textContent = String(n); });
    var all = form.querySelector('[data-check-all="' + name + '"]');
    if (all) { all.checked = n > 0 && n === boxes.length; all.indeterminate = n > 0 && n < boxes.length; }
  }
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.form || t.type !== "checkbox") return;
    var name = t.getAttribute("data-check-all");
    if (name) {
      t.form.querySelectorAll('input[type="checkbox"][name="' + name + '"]').forEach(function (b) { b.checked = t.checked; });
      syncChecks(t.form, name);
    } else if (t.name && t.form.querySelector('[data-check-all="' + t.name + '"]')) {
      syncChecks(t.form, t.name);
    }
  });
  document.querySelectorAll("[data-check-all]").forEach(function (el) {
    if (el.form) syncChecks(el.form, el.getAttribute("data-check-all"));
  });

  // --- Prüf-Stapel: Auswahl einzelner Zeilen (serverseitig gespeichert, gilt über Seiten hinweg) ----------
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (!t || !t.matches || !t.matches("input[data-select-url]")) return;
    var body = new URLSearchParams();
    body.set("op", "toggle");
    body.set("idx", t.value);
    body.set("on", t.checked ? "1" : "0");
    fetch(t.getAttribute("data-select-url"), {
      method: "POST", credentials: "same-origin", body: body.toString(),
      headers: { "X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(function (d) {
      document.querySelectorAll(".sel-count").forEach(function (el) { el.textContent = String(d.selected); });
    }).catch(function () {
      t.checked = !t.checked;
      toast("Auswahl nicht gespeichert", "crit");
    });
  });

  // --- Segment-Schalter (Zeitraum, Darstellung, Allokation) -------------------------------------------
  function pressIn(group, btn) {
    group.querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-pressed", b === btn ? "true" : "false"); });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".seg button");
    if (!btn) return;
    var group = btn.parentElement;
    var target;
    if (group.hasAttribute("data-range-group")) {
      target = document.getElementById(group.getAttribute("data-range-group"));
      if (target) { target.dataset.range = btn.dataset.range; pressIn(group, btn); window.PortfoliaCharts.reload(target); }
    } else if (group.hasAttribute("data-kind-group")) {
      target = document.getElementById(group.getAttribute("data-kind-group"));
      if (target) { target.dataset.kind = btn.dataset.kind; pressIn(group, btn); window.PortfoliaCharts.reload(target); }
    } else if (group.hasAttribute("data-alloc-toggle")) {
      target = document.getElementById("alloc-chart");
      setAllocMode(target, btn.dataset.mode);
      pressIn(group, btn);
      store("portfolia.alloc", btn.dataset.mode);
      window.PortfoliaCharts.reload(target);
    } else if (group.hasAttribute("data-tm-color")) {
      target = document.getElementById("alloc-chart");
      target.dataset.color = btn.dataset.color;
      pressIn(group, btn);
      store("portfolia.tmcolor", btn.dataset.color);
      window.PortfoliaCharts.reload(target);
    } else if (group.hasAttribute("data-movers-toggle")) {
      setMovers(btn.dataset.unit);
      store("portfolia.movers", btn.dataset.unit);
    } else if (group.hasAttribute("data-alloc-level")) {
      target = document.getElementById("alloc-chart");
      target.dataset.level = btn.dataset.level;
      target.dataset.expand = "";
      pressIn(group, btn);
      window.PortfoliaCharts.reload(target);
    }
  });

  // Allokation: Sunburst | Donut | Treemap (Treemap: eigene Daten, Farbe Heute bzw. G/V gesamt)
  function setAllocMode(target, mode) {
    if (!target) return;
    var tm = mode === "treemap";
    target.dataset.mode = mode;
    target.dataset.expand = "";
    target.dataset.chart = tm ? "treemap" : "allocation";
    target.dataset.src = tm ? "/api/treemap" : "/api/allocation";
    var lv = document.querySelector("[data-alloc-level]");
    if (lv) lv.style.display = mode === "donut" ? "" : "none";
    var tc = document.querySelector("[data-tm-color]");
    if (tc) tc.style.display = tm ? "" : "none";
  }
  // Top-Bewegungen: Prozent | Euro (Auswahl bleibt im Browser gespeichert)
  function setMovers(unit) {
    document.querySelectorAll("[data-movers]").forEach(function (el) { el.hidden = el.getAttribute("data-movers") !== unit; });
    document.querySelectorAll("[data-movers-toggle] button").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.unit === unit ? "true" : "false");
    });
  }
  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* privat/gesperrt – ignorieren */ } }
  function stored(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function restoreChoices() {
    var unit = stored("portfolia.movers");
    if (unit === "eur" || unit === "pct") setMovers(unit);
    var alloc = document.getElementById("alloc-chart");
    var mode = stored("portfolia.alloc");
    if (alloc && (mode === "treemap" || mode === "donut" || mode === "sunburst")) {
      setAllocMode(alloc, mode);
      document.querySelectorAll("[data-alloc-toggle] button").forEach(function (b) {
        b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false");
      });
      var color = stored("portfolia.tmcolor");
      if (color === "day" || color === "total") {
        alloc.dataset.color = color;
        document.querySelectorAll("[data-tm-color] button").forEach(function (b) {
          b.setAttribute("aria-pressed", b.dataset.color === color ? "true" : "false");
        });
      }
    }
  }
  restoreChoices();  // vor dem ersten Laden der Diagramme (charts.js initialisiert bei DOMContentLoaded)

  // --- Hinweise ---------------------------------------------------------------------------------------
  function toast(text, level) {
    var d = document.createElement("div");
    d.className = "alert " + (level || "");
    d.setAttribute("role", "status");
    d.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:76px;z-index:90;box-shadow:var(--shadow);max-width:92vw";
    d.textContent = text;
    document.body.appendChild(d);
    setTimeout(function () { d.remove(); }, 5000);
  }
  window.PortfoliaToast = toast;

  // --- Aktualisierung erkennen (neue Kurse/Importe) --------------------------------------------------
  var lastStatus = null;
  function pollStatus() {
    if (document.hidden) return;
    fetch("/api/status", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (s) {
      if (!s) return;
      var key = s.price_version + ":" + s.data_version + ":" + s.history_version;
      if (lastStatus !== null && key !== lastStatus && !panelOpen) {
        var page = document.body.getAttribute("data-page");
        if (page === "dashboard" || page === "positions") {
          document.querySelectorAll("[data-chart]").forEach(function (el) { window.PortfoliaCharts.reload(el, true); });
          htmx.ajax("GET", location.pathname + location.search, { target: "#content", select: "#content", swap: "outerHTML" });
        }
      }
      lastStatus = key;
    }).catch(function () { /* offline – ignorieren */ });
  }
  setInterval(pollStatus, 60000);
  document.addEventListener("DOMContentLoaded", function () {
    applyDotColors(document);
    pollStatus();
  });
})();
