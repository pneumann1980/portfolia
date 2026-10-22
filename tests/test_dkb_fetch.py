"""Fetcher ``portfolia-dkb-fetch`` (GPL, ``contrib/``): Zeilenschnittstelle, Pfadsicherheit, nur lesend.

dkb-robo wird durch Attrappen ersetzt – es gibt keinen Zugriff auf die Bank. Geprüft wird, was Portfolia sich auf die
Schnittstelle verlässt: JSON-Zeilen auf stdout (nichts sonst), Ereignisfolge, bekannte Dokumente werden übersprungen,
Pfade bleiben im Zielordner, Dokumente werden nicht als gelesen markiert, Zugangsdaten tauchen nirgends auf."""

from __future__ import annotations

import io
import json
import signal
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib" / "portfolia-dkb-fetch"))
from portfolia_dkb_fetch import cli

PASSWORD = "geheimes-passwort-123"


class FakeItem:
    def __init__(self, id: str, category: str, account: str, date: str, filename: str, doctype: str,
                 data: bytes = b"%PDF-1.4 test", mismatch: bool = False) -> None:
        self.id = id
        self._c, self._a, self._d, self._f = category, account, date, filename
        self.message = SimpleNamespace(documentType=doctype)
        self.data, self.mismatch, self.downloads = data, mismatch, 0

    def category(self) -> str:
        return self._c

    def account(self, card_lookup=None) -> str:
        return self._a

    def date(self) -> str:
        return self._d

    def filename(self) -> str:
        return self._f

    def subject(self) -> str:
        return f"Betreff {self.id}"

    def download(self, client, target: Path, overwrite: bool = False):
        self.downloads += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        target = target.with_name(target.name + ".checksum_mismatch") if self.mismatch else target
        target.write_bytes(self.data)
        return 200

    def mark_read(self, *a, **k):  # pragma: no cover - darf nie aufgerufen werden
        raise AssertionError("Dokumente dürfen nicht als gelesen markiert werden")


@pytest.fixture
def bank(monkeypatch):
    state = SimpleNamespace(items={}, login_error=None, kwargs=None)

    class APPAuthentication:
        def _print(self, devicename):
            print(f'check your banking app on "{devicename}" and confirm login...')

    class DKBRobo:
        def __init__(self, **kw):
            state.kwargs = kw
            self.account_dic = {"a": {}, "b": {}}
            self.wrapper = SimpleNamespace(client=object())

        def __enter__(self):
            print("Bibliothek schreibt auf stdout")  # darf die Schnittstelle nicht stören
            APPAuthentication()._print("Pixel")
            if state.login_error:
                raise RuntimeError(state.login_error)
            return self

        def __exit__(self, *a):
            return False

        def _accounts_by_id(self):
            return {}

    class PostBox:
        def __init__(self, client):
            pass

        def fetch_items(self):
            return state.items

    mods = {
        "dkb_robo": types.SimpleNamespace(DKBRobo=DKBRobo, __version__="0.31.2"),
        "dkb_robo.postbox": types.SimpleNamespace(PostBox=PostBox),
        "dkb_robo.authentication": types.SimpleNamespace(APPAuthentication=APPAuthentication),
    }
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    monkeypatch.setattr(cli, "PAUSE", 0)
    old = signal.getsignal(signal.SIGTERM)
    yield state
    signal.signal(signal.SIGTERM, old)


def run(tmp_path: Path, creds: dict | None = None, known: list[str] | None = None, bank=None):
    args = ["--out", str(tmp_path / "out")]
    if known is not None:
        kf = tmp_path / "known.json"
        kf.write_text(json.dumps(known))
        args += ["--known", str(kf)]
    stdin = io.StringIO(json.dumps(creds if creds is not None else {"user": "u123", "password": PASSWORD}) + "\n")
    out = io.StringIO()
    rc = cli.main(args, stdin=stdin, stdout=out)
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    return rc, events, out.getvalue()


def test_version_is_json(capsys) -> None:
    out = io.StringIO()
    assert cli.main(["--version"], stdout=out) == 0
    d = json.loads(out.getvalue())
    assert d["version"] == cli.__version__ and "dkb_robo" in d["libs"]


def test_happy_path_events_files_and_known_skipped(tmp_path, bank) -> None:
    bank.items = {
        "d1": FakeItem("d1", "Wertpapierdokumente", "1234567890", "2025-01-02", "Abrechnung_Kauf.pdf",
                       "dwpOrderStatement", b"%PDF order"),
        "d2": FakeItem("d2", "Kontoauszüge", "DE00 1234", "2025-01-31", "Kontoauszug.pdf", "bankAccountStatement"),
        "d3": FakeItem("d3", "Kreditkartenabrechnungen", "4***9", "2025-02-01", "Karte.pdf", "creditCardStatement"),
    }
    rc, ev, raw = run(tmp_path, known=["d3"])
    assert rc == 0
    assert [e["event"] for e in ev] == ["hello", "login", "awaiting_2fa", "logged_in", "listed", "document", "document",
                                        "done"]
    assert PASSWORD not in raw and "u123" not in raw  # Zugangsdaten tauchen nirgends auf
    assert ev[3]["accounts"] == 2 and ev[4] == {"event": "listed", "total": 3, "new": 2}
    docs = {e["id"]: e for e in ev if e["event"] == "document"}
    assert set(docs) == {"d1", "d2"} and bank.items["d3"].downloads == 0
    d1 = docs["d1"]
    assert d1["rel"] == "Wertpapierdokumente/1234567890/2025-01-02_Abrechnung_Kauf.pdf" and d1["status"] == "downloaded"
    assert d1["doctype"] == "dwpOrderStatement" and d1["checksum_ok"] is True and d1["size"] == len(b"%PDF order")
    assert (tmp_path / "out" / d1["rel"]).read_bytes() == b"%PDF order"
    assert ev[-1] == {"event": "done", "cancelled": False, "downloaded": 2, "existing": 0, "failed": 0}
    # Anmeldung: App-Freigabe, kein chipTAN, genau ein Versuch
    assert bank.kwargs["chip_tan"] is False and bank.kwargs["dkb_user"] == "u123"


def test_existing_file_is_reported_not_downloaded_again(tmp_path, bank) -> None:
    it = FakeItem("d1", "Wertpapierdokumente", "111", "2025-01-02", "a.pdf", "dwpOrderStatement")
    bank.items = {"d1": it}
    f = tmp_path / "out" / "Wertpapierdokumente" / "111" / "2025-01-02_a.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"alt")
    rc, ev, _ = run(tmp_path)
    doc = next(e for e in ev if e["event"] == "document")
    assert rc == 0 and doc["status"] == "exists" and it.downloads == 0 and f.read_bytes() == b"alt"


def test_paths_never_leave_the_target_folder(tmp_path, bank) -> None:
    bank.items = {"x": FakeItem("x", "../../etc", "../konto/..", "2025-01-02", "../../evil.pdf", "dwpOrderStatement"),
                  "y": FakeItem("y", "", "", "2025-01-02", "..", "other")}
    rc, ev, _ = run(tmp_path)
    assert rc == 0
    out = (tmp_path / "out").resolve()
    for e in ev:
        if e["event"] == "document":
            p = (out / e["rel"]).resolve()
            assert str(p).startswith(str(out) + "/") and p.is_file() and ".." not in Path(e["rel"]).parts
    assert not (tmp_path / "evil.pdf").exists() and not (tmp_path.parent / "evil.pdf").exists()


def test_checksum_mismatch_is_reported(tmp_path, bank) -> None:
    bank.items = {"m": FakeItem("m", "Kontoauszüge", "1", "2025-01-02", "a.pdf", "bankAccountStatement", mismatch=True)}
    rc, ev, _ = run(tmp_path)
    doc = next(e for e in ev if e["event"] == "document")
    assert rc == 0 and doc["checksum_ok"] is False and doc["rel"].endswith(".checksum_mismatch")


def test_one_failing_document_does_not_stop_the_run(tmp_path, bank) -> None:
    class Boom(FakeItem):
        def download(self, *a, **k):
            raise OSError(f"kaputt {PASSWORD}")

    bank.items = {"a": Boom("a", "Kontoauszüge", "1", "2025-01-02", "a.pdf", "bankAccountStatement"),
                  "b": FakeItem("b", "Kontoauszüge", "1", "2025-01-03", "b.pdf", "bankAccountStatement")}
    rc, ev, raw = run(tmp_path)
    assert rc == 0 and PASSWORD not in raw
    assert any(e["event"] == "document_error" and e["id"] == "a" for e in ev)
    assert ev[-1]["downloaded"] == 1 and ev[-1]["failed"] == 1


@pytest.mark.parametrize(("message", "kind"), [
    ("Login failed: wrong credentials", "login"),
    ("2fa chanceled by user", "2fa_canceled"),
    ("Login failed: 2fa timed out", "2fa_timeout"),
    ("Connection timed out", "network"),
    ("Unexpected format of response", "changed"),
    ("etwas anderes", "other"),
])
def test_errors_are_classified_and_secrets_masked(tmp_path, bank, message, kind) -> None:
    bank.login_error = f"{message} {PASSWORD}"
    rc, ev, raw = run(tmp_path)
    err = ev[-1]
    assert rc == 2 and err["event"] == "error" and err["kind"] == kind and PASSWORD not in raw
    assert err["message"].startswith("RuntimeError: ") and err["fetcher"] == cli.__version__
    assert not (tmp_path / "out" / "Kontoauszüge").exists()


def test_missing_credentials_and_missing_library(tmp_path, bank, monkeypatch) -> None:
    rc, ev, _ = run(tmp_path, creds={"user": "", "password": ""})
    assert rc == 2 and ev[-1]["kind"] == "login"
    monkeypatch.setitem(sys.modules, "dkb_robo", None)  # Import schlägt fehl
    rc, ev, _ = run(tmp_path)
    assert rc == 2 and ev[-1]["kind"] == "dependency"
