/* Portfolia – Service Worker. Zweck: Installierbarkeit als App (Chrome verlangt einen Service Worker mit
   fetch-Handler) und eine Hinweisseite ohne Netz. Es wird NICHTS zwischengespeichert: Portfolio-Daten bleiben
   ausschließlich auf dem Server. Bei Netzverbindung greift der Worker nicht ein – Seitenaufrufe, Anmeldung
   (Basic Auth) und alle Anfragen laufen unverändert über den Browser. */
"use strict";

var OFFLINE = '<!doctype html><html lang="de"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Portfolia – offline</title></head>' +
  '<body style="font:16px system-ui,sans-serif;margin:0;display:grid;place-items:center;min-height:100vh;' +
  'background:#1a1a19;color:#f2f2ef;text-align:center"><main style="padding:24px"><h1 style="font-size:1.3rem">' +
  'Portfolia ist nicht erreichbar</h1><p>Keine Verbindung zum Server. Bitte Netz bzw. VPN prüfen und neu laden.</p>' +
  '<p><button onclick="location.reload()" style="font:inherit;padding:8px 16px">Neu laden</button></p></main>' +
  '</body></html>';

self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (event) { event.waitUntil(self.clients.claim()); });

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.mode !== "navigate" || self.navigator.onLine !== false) {
    return;  // online bzw. keine Seitenaufrufe: Browser wie ohne Service Worker
  }
  event.respondWith(fetch(req).catch(function () {
    return new Response(OFFLINE, {status: 503, headers: {"Content-Type": "text/html; charset=utf-8",
                                                         "Cache-Control": "no-store"}});
  }));
});
