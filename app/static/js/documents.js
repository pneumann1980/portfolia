/* PDF & Screenshot: Dateien per Drag-and-drop auswählen, Liste mit Größenprüfung, Hochladen mit Fortschritt (XHR).
   Ohne JavaScript bleibt das gewöhnliche Formular nutzbar. Es wird nichts außer den gewählten Dateien übertragen –
   und nur an Portfolia selbst. */
(function () {
  "use strict";
  var form = document.getElementById("doc-upload");
  if (!form) return;
  var input = document.getElementById("doc-files");
  var drop = document.getElementById("doc-drop");
  var list = document.getElementById("doc-filelist");
  var box = document.getElementById("doc-progress");
  var text = document.getElementById("doc-progress-text");
  var bar = box ? box.querySelector(".progress > div") : null;
  var submit = document.getElementById("doc-submit");
  var abortBtn = document.getElementById("doc-abort");
  var maxFiles = parseInt(form.dataset.maxFiles || "20", 10);
  var maxFile = parseInt(form.dataset.maxFile || "0", 10);
  var maxTotal = parseInt(form.dataset.maxTotal || "0", 10);
  var OK = /\.(pdf|png|jpe?g|webp)$/i;
  var files = [];

  function mb(n) { return (n / 1048576).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " MB"; }

  function problems() {
    var out = [], total = 0;
    if (files.length > maxFiles) out.push("Höchstens " + maxFiles + " Dateien je Stapel.");
    files.forEach(function (f) {
      total += f.size;
      if (!OK.test(f.name)) out.push(f.name + ": nur PDF, PNG, JPEG oder WebP.");
      else if (maxFile && f.size > maxFile) out.push(f.name + ": größer als " + mb(maxFile) + ".");
    });
    if (maxTotal && total > maxTotal) out.push("Zusammen größer als " + mb(maxTotal) + ".");
    return out;
  }

  function render() {
    list.textContent = "";
    files.forEach(function (f, i) {
      var li = document.createElement("li");
      li.textContent = f.name + " · " + mb(f.size) + " ";
      var rm = document.createElement("button");
      rm.type = "button"; rm.className = "btn ghost sm"; rm.textContent = "entfernen";
      rm.setAttribute("aria-label", f.name + " entfernen");
      rm.addEventListener("click", function () { files.splice(i, 1); sync(); });
      li.appendChild(rm);
      list.appendChild(li);
    });
    problems().forEach(function (p) {
      var li = document.createElement("li");
      li.className = "neg"; li.textContent = p;
      list.appendChild(li);
    });
    submit.disabled = !files.length || problems().length > 0;
  }

  function sync() {
    try {  // Auswahl auch für das gewöhnliche Absenden aktuell halten (nicht überall unterstützt)
      var dt = new DataTransfer();
      files.forEach(function (f) { dt.items.add(f); });
      input.files = dt.files;
    } catch (e) { /* ältere Browser: XHR-Weg nutzt die Liste direkt */ }
    render();
  }

  function add(fl) {
    for (var i = 0; i < fl.length; i++) {
      var f = fl[i], dup = files.some(function (g) { return g.name === f.name && g.size === f.size; });
      if (!dup) files.push(f);
    }
    sync();
  }

  input.addEventListener("change", function () { files = []; add(input.files); });
  ["dragenter", "dragover"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); });
  });
  drop.addEventListener("drop", function (e) { if (e.dataTransfer && e.dataTransfer.files) add(e.dataTransfer.files); });

  var xhr = null;
  form.addEventListener("submit", function (e) {
    if (!window.FormData || !window.XMLHttpRequest) return;  // gewöhnliches Absenden
    e.preventDefault();
    if (!files.length || problems().length) { render(); return; }
    var fd = new FormData();
    files.forEach(function (f) { fd.append("files", f, f.name); });
    ["account", "public", "reanalyze"].forEach(function (n) {
      var el = form.elements[n];
      if (!el || el.disabled) return;
      if (el.type === "checkbox") { if (el.checked) fd.append(n, el.value); } else fd.append(n, el.value);
    });
    xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.setRequestHeader("X-CSRF-Token", form.elements.csrf_token.value);
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    xhr.responseType = "json";
    box.hidden = false; submit.disabled = true; abortBtn.hidden = false;
    xhr.upload.addEventListener("progress", function (ev) {
      if (!ev.lengthComputable) return;
      var pct = Math.round(ev.loaded / ev.total * 100);
      bar.style.width = Math.max(pct, 2) + "%";
      bar.parentNode.setAttribute("aria-valuenow", String(pct));
      text.textContent = pct < 100 ? "Hochladen … " + pct + " % (" + mb(ev.loaded) + " von " + mb(ev.total) + ")"
        : "Hochgeladen – Prüfung der Dateien …";
    });
    xhr.addEventListener("load", function () {
      var r = xhr.response;
      if (xhr.status === 200 && r && r.redirect) { window.location.assign(r.redirect); return; }
      fail(xhr.status === 413 ? "Anfrage zu groß." : xhr.status === 403 ? "Sitzung abgelaufen – Seite neu laden." :
        "Hochladen fehlgeschlagen (" + xhr.status + ").");
    });
    xhr.addEventListener("error", function () { fail("Verbindung unterbrochen."); });
    xhr.addEventListener("abort", function () { fail("Hochladen abgebrochen – nichts wurde ausgewertet."); });
    xhr.send(fd);
  });
  abortBtn.addEventListener("click", function () { if (xhr) xhr.abort(); });

  function fail(msg) {
    text.textContent = msg; bar.style.width = "0%";
    submit.disabled = false; abortBtn.hidden = true; xhr = null;
  }
  render();
})();
