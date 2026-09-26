/* Früh geladen (im <head>), damit kein Farbflackern entsteht. */
(function () {
  try {
    var t = localStorage.getItem("portfolia-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* Speicher gesperrt – Systemeinstellung gilt */ }
})();
