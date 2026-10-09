/* Früh geladen (im <head>), damit kein Farbflackern entsteht. */
(function () {
  try {
    var t = localStorage.getItem("portfolia-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    // Depotwert verborgen (Augen-Symbol auf der Übersicht) – vor dem ersten Zeichnen, damit nichts aufblitzt
    if (localStorage.getItem("portfolia-hide-total") === "1") document.documentElement.classList.add("hide-total");
  } catch (e) { /* Speicher gesperrt – Systemeinstellung gilt */ }
})();
