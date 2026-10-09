# Hinweise zu Drittkomponenten

Portfolia selbst steht unter der [MIT-Lizenz](LICENSE). Das Docker-Image enthält außerdem die folgenden
Komponenten Dritter unter deren eigenen Lizenzen. Alle sind mit der MIT-Lizenz vereinbar (keine
Copyleft-Pflichten für den Portfolia-Code); die Lizenztexte liegen im Image bei den jeweiligen Komponenten.

Stand: 09.10.2026 (Portfolia 0.22.0; Python-Paketliste zuletzt vollständig erzeugt für 0.11.0, seither einzeln
ergänzt).

## Weboberfläche (`app/static/vendor/`, unverändert eingebunden)

| Komponente | Version | Lizenz | Lizenztext |
|---|---|---|---|
| [Apache ECharts](https://echarts.apache.org/) inkl. deutscher Sprachdatei | 6.1.0 | Apache-2.0 | `app/static/vendor/LICENSE-echarts.txt`, `NOTICE-echarts.txt` |
| – darin enthaltene Teile von [d3](https://github.com/d3/d3) | – | BSD-3-Clause | `app/static/vendor/LICENSE-d3.txt` |
| [htmx](https://htmx.org/) | 2.0.11 | 0BSD | `app/static/vendor/LICENSE-htmx.txt` |

NOTICE von Apache ECharts (gemäß Apache-2.0, § 4 d):

```text
Apache ECharts
Copyright 2017-2026 The Apache Software Foundation

This product includes software developed at
The Apache Software Foundation (https://www.apache.org/).
```

## PDF-Berichte

| Komponente | Lizenz | Lizenztext |
|---|---|---|
| Bitstream Vera Fonts (mit ReportLab ausgeliefert, in PDFs eingebettet) | Bitstream-Vera | `site-packages/reportlab/fonts/bitstream-vera-license.txt` |

Die Oberfläche nutzt ausschließlich Systemschriften; es werden keine Schriftdateien ausgeliefert.

## Python-Pakete (Laufzeit, direkt und transitiv)

Erzeugt mit `python scripts/third_party.py` aus `requirements.txt` (Versionen der Entwicklungsumgebung zum
Stand oben; der Image-Build installiert die gepinnten direkten Abhängigkeiten, transitive können abweichen).
Die Lizenztexte liegen im Image unter `/usr/local/lib/python3.12/site-packages/<paket>.dist-info/licenses/`.
Einige Wheels enthalten native Bibliotheken: numpy (u. a. OpenBLAS), Pillow (u. a. libjpeg-turbo, zlib),
lxml (libxml2, libxslt), cryptography (OpenSSL, Apache-2.0), pypdfium2 (PDFium von Google/Foxit, BSD-3-Clause,
mit Drittkomponenten wie FreeType, libjpeg-turbo, OpenJPEG, lcms2, zlib – aufgeführt in
`pypdfium2-*.dist-info/LicenseRef-PdfiumThirdParty.txt`) – deren Lizenzen sind in den Lizenzdateien dieser
Pakete aufgeführt; curl_cffi
enthält curl-impersonate (libcurl mit BoringSSL), dessen Lizenzhinweise die Projektseite von curl_cffi nennt.

| Paket | Version | Lizenz |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anthropic | 1.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| APScheduler | 3.11.3 | MIT |
| bcrypt | 5.0.0 | Apache-2.0 |
| beautifulsoup4 | 4.15.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| curl_cffi | 0.16.3 | MIT |
| defusedxml | 0.7.1 | PSF-2.0 |
| docstring_parser | 0.18.0 | MIT |
| fastapi | 0.141.1 | MIT |
| feedparser | 6.0.14 | BSD-2-Clause |
| feedparser-sgmllib | 2.1.0 | PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx2 | 2.13.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| jiter | 0.17.0 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| multitasking | 0.0.13 | Apache-2.0 |
| numpy | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| pandas | 3.0.6 | BSD-3-Clause |
| peewee | 4.5.1 | MIT |
| pillow | 12.3.0 | MIT-CMU |
| pypdfium2 | 4.30.0 | (Apache-2.0 OR BSD-3-Clause) AND LicenseRef-PdfiumThirdParty |
| platformdirs | 4.12.0 | MIT |
| protobuf | 7.36.2 | BSD-3-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| python-dateutil | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pytz | 2026.4 | MIT |
| reportlab | 5.0.1 | BSD-3-Clause |
| requests | 2.34.2 | Apache-2.0 |
| ruamel.yaml | 0.19.1 | MIT |
| six | 1.17.0 | MIT |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| soupsieve | 2.10 | MIT |
| starlette | 1.7.0 | BSD-3-Clause |
| truststore | 0.10.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| tzdata | 2026.4 | Apache-2.0 |
| tzlocal | 5.4.4 | MIT |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
| websockets | 17.1 | BSD-3-Clause |
| yfinance | 1.7.0 | Apache-2.0 |

## Texterkennung (Belegimport, M25)

| Komponente | Herkunft | Lizenz |
|---|---|---|
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) 5.3 (`tesseract-ocr`, `libtesseract5`) | Debian bookworm | Apache-2.0 |
| Sprachdaten Deutsch/Englisch (`tesseract-ocr-deu`, `tesseract-ocr-eng`, tessdata_fast) | Debian bookworm | Apache-2.0 |
| Leptonica (`liblept5`) | Debian bookworm | BSD-2-Clause (Leptonica-Lizenz) |
| ICU (`libicu72`), weitere Abhängigkeiten (u. a. HarfBuzz, Pango, Cairo, libarchive) | Debian bookworm | jeweils eigene Lizenzen (`/usr/share/doc/*/copyright` im Image) |

Tesseract wird als eigenständiges Programm aufgerufen (kein Linken mit dem Portfolia-Code). Die Daten zur
Ausrichtungserkennung (`osd.traineddata`) werden im Image entfernt, da Portfolia sie nicht nutzt. Bewusst **nicht**
verwendet: PyMuPDF/MuPDF (AGPL-3.0, mit der MIT-Lizenz des Images nicht ohne Weiteres vereinbar).

## Basis-Image

`python:3.12-slim-bookworm`: CPython (PSF-2.0) und Debian-Pakete unter ihren jeweiligen Lizenzen
(`/usr/share/doc/*/copyright` im Image).

## Datenquellen

Kurse, Devisenkurse, News und Videos werden zur Laufzeit von Diensten Dritter abgerufen (Yahoo Finance,
CoinGecko, EZB/Frankfurter, RSS-Feeds, YouTube, optional Anthropic). Für deren Nutzung gelten die
jeweiligen Nutzungsbedingungen; die Inhalte sind nicht Teil dieses Repositorys oder des Images.

Wallet-Daten (nur bei eingerichteten Wallet-Konten, nur öffentliche Adressen) kommen von Etherscan, Routescan,
mempool.space bzw. Blockstream (Esplora), dem öffentlichen Solana-RPC der Solana Foundation bzw. Helius,
api.kaspa.org (kaspa-rest-server), api.kasplex.org (Kasplex KRC-20-Indexer), xrplcluster.com bzw. s2.ripple.com,
Koios, Polygon-Blockscout, PubFi bzw. Subscan (Polkadot, peaq), dem PulseChain-Explorer api.scan.pulsechain.com
(Blockscout), rpc.pulsechain.com und NodeReal (BSCTrace, BNB Chain); es gelten deren Nutzungsbedingungen. KRC-20-
Kurse kommen vom KaspaCom-Marktplatz (api.kaspa.com). Börsendaten (nur mit eigenem, lesendem
API-Key) kommen von der Bitpanda Public API und der Binance Spot-API (api.binance.com).
Die Bitcoin-Ableitung (BIP32, nur öffentlich), Bech32/Bech32m, Base58, Keccak-256 und RIPEMD-160 sind eigene
Implementierungen nach den jeweiligen öffentlichen Spezifikationen (BIP32/44/49/84/86/173/350, FIPS 202/Keccak,
RIPEMD-160); die Testvektoren stammen aus diesen Spezifikationen.
