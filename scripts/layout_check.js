// Layoutprüfung bei Handybreite: Überlauf und abgeschnittene Inhalte auf den wichtigsten Seiten.
//
// Aufruf (laufende Instanz, z. B. Demo-Daten):  node scripts/layout_check.js [Basis-URL] [Breite]
//   Basis-URL: Standard http://127.0.0.1:8080 · Breite: Standard 390
// Voraussetzung: Node-Paket „playwright“ mit Chromium (nicht Teil der Python-Abhängigkeiten, läuft nicht in der CI).
//
// Je Seite drei Prüfungen:
//   1. Seite breiter als der Bildschirm (waagrechtes Scrollen),
//   2. Elemente, deren Inhalt breiter ist als sie selbst, ohne dass sie scrollen dürfen (abgeschnittener Text),
//   3. Belastungstest: ein Hinweiskasten und eine Tabellenzelle mit langer Token-Kennung und langer Zahl werden in
//      die erste Karte eingefügt – lange Wörter müssen umbrechen (deckt Fälle ab, die die Demo-Daten nicht enthalten).
// Ausgabe: eine Zeile je Seite; Exitcode 1, wenn etwas auffällt.

const { chromium } = require('playwright');

const BASE = process.argv[2] || 'http://127.0.0.1:8080';
const WIDTH = parseInt(process.argv[3] || '390', 10);
const PAGES = [
  '/', '/positions', '/quality', '/quality/assistent?new=1', '/quality/assistent/protokoll', '/quality/faelle',
  '/quality/klaerung', '/quality/diagnose', '/journal/pruefung', '/quality/diagnose/bulk', '/quality/diagnose/hints',
  '/quality/erneut', '/quality/belege', '/quality/diagnose/stock', '/quality/diagnose/windows', '/quality/kette',
  '/quality/abdeckung', '/quality/anschaffung', '/quality/cockpit', '/quality/integrity', '/quality/automatik',
  '/quality/autopilot', '/quality/diagnose/regeln', '/settings', '/settings/datasources',
];
const LONG = 'SEND@ETH:0x91d6cf4af9697ec95d9e1754e8c0a1b2c3d4e5f6 0.019429586733486074';

async function check(page, url) {
  await page.goto(BASE + url, { waitUntil: 'networkidle', timeout: 60000 });
  return page.evaluate((LONG) => {
    const vw = document.documentElement.clientWidth;
    const out = [];
    if (document.documentElement.scrollWidth > vw + 1) out.push(`Seite ${document.documentElement.scrollWidth}px`);
    const scrolls = (el) => {
      for (let a = el; a; a = a.parentElement) {
        const ox = getComputedStyle(a).overflowX;
        if (ox === 'auto' || ox === 'scroll') return true;
      }
      return false;
    };
    const name = (el) => el.tagName.toLowerCase() + (el.className && typeof el.className === 'string'
      ? '.' + el.className.trim().split(/\s+/).join('.') : '');
    // sichtbarer Nachfahre ragt rechts über das Element hinaus (versteckte/absolut positionierte zählen nicht)
    const sticksOut = (el) => {
      const edge = el.getBoundingClientRect().right + 1;
      for (const d of el.querySelectorAll('*')) {
        const r = d.getBoundingClientRect();
        const ds = getComputedStyle(d);
        if (parseFloat(ds.marginRight) < 0) continue;  // gewollt bis zum Kartenrand (z. B. .table-wrap)
        if (r.width && r.right > edge && ds.visibility !== 'hidden' && d.offsetParent !== null) return true;
      }
      return false;
    };
    for (const el of document.querySelectorAll('main *')) {
      const cs = getComputedStyle(el);
      if (cs.display === 'inline' || el.clientWidth === 0 || scrolls(el) || el.closest('details:not([open]) > :not(summary)')) continue;
      if (el.scrollWidth > el.clientWidth + 2 && ['hidden', 'clip', 'visible'].includes(cs.overflowX) && sticksOut(el)) {
        out.push(`abgeschnitten: ${name(el)} (${el.scrollWidth}>${el.clientWidth})`);
        if (out.length > 5) break;
      }
    }
    const card = document.querySelector('main .card');
    if (card) {
      card.insertAdjacentHTML('afterbegin', `<div class="alert warn" id="lc-a"><span class="icon">!</span><div>Test: ${LONG}</div></div>`
        + `<div class="table-wrap" id="lc-t"><table class="table cards"><tbody><tr><td class="wide">${LONG}</td></tr></tbody></table></div>`);
      const a = document.getElementById('lc-a');
      if (a.children[1].scrollWidth > a.clientWidth) out.push('Belastungstest: Hinweiskasten bricht lange Wörter nicht um');
      if (document.documentElement.scrollWidth > vw + 1) out.push('Belastungstest: Seite wird breiter');
      a.remove(); document.getElementById('lc-t').remove();
    }
    return out;
  }, LONG);
}

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: WIDTH, height: 844 } });
  let bad = 0;
  for (const url of PAGES) {
    try {
      const r = await check(page, url);
      if (r.length) bad += 1;
      console.log(`${r.length ? '✗' : '✓'} ${url}${r.length ? '  ' + r.join(' · ') : ''}`);
    } catch (e) {
      bad += 1;
      console.log(`✗ ${url}  Fehler: ${String(e.message).slice(0, 120)}`);
    }
  }
  await browser.close();
  console.log(bad ? `${bad} Seite(n) mit Auffälligkeiten` : 'Keine Auffälligkeiten');
  process.exit(bad ? 1 : 0);
})();
