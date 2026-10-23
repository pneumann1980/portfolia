/* DKB-Wertpapierdokumente (ZIP): Datei wählen, Größe prüfen, Hochladen mit Fortschritt (XHR). Ohne JavaScript bleibt
   das gewöhnliche Formular nutzbar. Übertragen wird nur die gewählte Datei – und nur an Portfolia selbst. */
(function () {
  "use strict";
  var form = document.getElementById("dkb-zip-form");
  if (!form) return;
  var input = document.getElementById("dkb-zip-file");
  var info = document.getElementById("dkb-zip-info");
  var box = document.getElementById("dkb-zip-progress");
  var text = document.getElementById("dkb-zip-progress-text");
  var bar = box.querySelector(".progress > div");
  var submit = document.getElementById("dkb-zip-submit");
  var abortBtn = document.getElementById("dkb-zip-abort");
  var maxBytes = parseInt(form.dataset.max || "0", 10);
  var xhr = null;

  function mb(n) { return (n / 1048576).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " MB"; }

  function check() {
    var f = input.files && input.files[0];
    info.textContent = ""; info.className = "hint";
    if (!f) { submit.disabled = true; return null; }
    if (!/\.zip$/i.test(f.name)) { info.textContent = f.name + ": bitte eine ZIP-Datei wählen."; info.className = "neg"; }
    else if (maxBytes && f.size > maxBytes) {
      info.textContent = f.name + ": " + mb(f.size) + " – größer als " + mb(maxBytes) + ". Bitte in mehrere ZIP-Dateien aufteilen.";
      info.className = "neg";
    } else { info.textContent = f.name + " · " + mb(f.size); submit.disabled = false; return f; }
    submit.disabled = true; return null;
  }
  input.addEventListener("change", check);

  form.addEventListener("submit", function (e) {
    if (!window.FormData || !window.XMLHttpRequest) return;
    e.preventDefault();
    var f = check();
    if (!f) return;
    var fd = new FormData();
    fd.append("file", f, f.name);
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
        : "Hochgeladen – das Archiv wird geprüft …";
    });
    xhr.addEventListener("load", function () {
      var r = xhr.response;
      if (xhr.status === 200 && r && r.redirect) { window.location.assign(r.redirect); return; }
      fail(r && r.error ? r.error : xhr.status === 413 ? "Datei zu groß." : xhr.status === 403 ?
        "Sitzung abgelaufen – Seite neu laden." : "Hochladen fehlgeschlagen (" + xhr.status + ").");
    });
    xhr.addEventListener("error", function () { fail("Verbindung unterbrochen."); });
    xhr.addEventListener("abort", function () { fail("Hochladen abgebrochen – nichts wurde verarbeitet."); });
    xhr.send(fd);
  });
  abortBtn.addEventListener("click", function () { if (xhr) xhr.abort(); });

  function fail(msg) {
    text.textContent = msg; bar.style.width = "0%";
    submit.disabled = false; abortBtn.hidden = true; xhr = null;
  }
  check();
})();
