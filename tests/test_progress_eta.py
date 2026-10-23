"""Restzeit des einheitlichen Fortschritts (app.progress, Erweiterung 8a): künstliche Uhr, keine Wartezeiten."""

from __future__ import annotations

from itertools import pairwise

from app.progress import Progress, fmt_clock, fmt_eta, view

PHASES = ["zip_unpack", "zip_eval", "zip_report"]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


def make(eta: bool = True, **kw):
    out: list[dict] = []
    clk = Clock()
    p = Progress(out.append, "ZIP", phases=PHASES, clock=clk, eta=eta, unit="Dokumente", **kw)
    return p, out, clk


def test_no_eta_fields_unless_enabled() -> None:
    p, out, clk = make(eta=False)
    p.phase("zip_unpack", total=10)
    clk.tick(30)
    p.update(5)
    assert "eta_text" not in out[-1] and "elapsed_s" not in out[-1]
    p.finish(True)
    assert "duration_text" not in out[-1]


def test_eta_waits_for_three_items_or_ten_seconds() -> None:
    p, out, clk = make()
    p.expect("zip_unpack", 100, 10).expect("zip_eval", 100, 2)
    p.phase("zip_unpack", total=100)
    assert out[-1]["eta_text"] == "Restzeit wird berechnet …" and out[-1]["eta_s"] is None
    clk.tick(1)
    p.update(1, work=1)
    clk.tick(1)
    p.update(2, work=2)
    assert out[-1]["eta_s"] is None  # zwei Einheiten, zwei Sekunden
    clk.tick(1)
    p.update(3, work=3)
    assert out[-1]["eta_s"] is not None and out[-1]["eta_text"].startswith("noch ")
    # alternativ nach 10 s Laufzeit auch mit wenigen Einheiten
    q, out2, clk2 = make()
    q.phase("zip_unpack", total=100)
    clk2.tick(11)
    q.update(1, work=1)
    assert out2[-1]["eta_s"] is not None


def test_eta_never_negative_never_zero_before_end_and_decreases() -> None:
    p, out, clk = make()
    p.expect("zip_unpack", 200, 100).expect("zip_eval", 200, 4)
    p.phase("zip_unpack", total=20, work_total=200)
    seen: list[int] = []
    for i in range(1, 21):
        clk.tick(0.5)
        p.update(i, work=i * 10)
        if out[-1]["eta_s"] is not None:
            seen.append(out[-1]["eta_s"])
    p.phase("zip_eval", total=20, work_total=200)
    for i in range(1, 20):
        clk.tick(1)
        p.update(i, work=i * 10)
        assert out[-1]["eta_s"] is None or out[-1]["eta_s"] >= 1  # nie 0, nie negativ
        assert out[-1]["pct"] < 100
        if out[-1]["eta_s"] is not None:
            seen.append(out[-1]["eta_s"])
    assert seen and min(seen) >= 1
    assert seen[-1] < seen[0]  # sinkt im Mittel
    assert out[-1]["pct"] < 100 and out[-1]["running"]


def test_eta_does_not_jump_by_more_than_factor_two() -> None:
    p, out, clk = make()
    p.expect("zip_unpack", 1000, 1000).expect("zip_eval", 1000, 100)
    p.phase("zip_unpack", total=10, work_total=1000)
    shown: list[tuple[float, int]] = []
    for i in range(1, 11):
        clk.tick(0.5)
        p.update(i, work=i * 100)
        if out[-1]["eta_s"] is not None:
            shown.append((clk.t, out[-1]["eta_s"]))
    p.phase("zip_eval", total=10, work_total=1000)  # Phasenwechsel: Geschwindigkeit ändert sich um Faktor 10
    for i in range(1, 6):
        clk.tick(0.5)
        p.update(i, work=i * 100)
        if out[-1]["eta_s"] is not None:
            shown.append((clk.t, out[-1]["eta_s"]))
    for (t0, a), (t1, b) in pairwise(shown):
        expected = max(a - (t1 - t0), 1)
        assert expected / 2 - 1 <= b <= expected * 2 + 1, (a, b)


def test_eta_uses_hint_for_phases_not_started_and_measured_speed_for_current() -> None:
    p, out, clk = make()
    p.expect("zip_unpack", 100, 10).expect("zip_eval", 600, 2)  # 600 / 2 = 300 s noch ausstehend
    p.phase("zip_unpack", total=10, work_total=100)
    for i in range(1, 6):
        clk.tick(2)
        p.update(i, work=i * 10)  # 5 Einheiten/s? nein: 10 je 2 s = 5/s → Rest 50 → 10 s
    eta = out[-1]["eta_s"]
    assert eta is not None and 300 <= eta <= 330


def test_eta_hidden_when_total_unknown() -> None:
    p, out, clk = make()
    p.phase("zip_unpack")
    clk.tick(20)
    p.update(50)
    assert out[-1]["eta_s"] is None and out[-1]["eta_text"] == "Restzeit wird berechnet …"


def test_finish_reports_actual_duration_and_measured_rates() -> None:
    p, out, clk = make()
    p.phase("zip_unpack", total=4, work_total=400)
    for i in range(1, 5):
        clk.tick(5)
        p.update(i, work=i * 100)
    p.phase("zip_eval", total=2, work_total=200)
    clk.tick(10)
    p.update(2, work=200)
    p.finish(True, "fertig")
    last = out[-1]
    assert last["duration_s"] == 30 and last["duration_text"] == "Fertig in 0:30 Min." and last["eta_text"] == ""
    assert last["pct"] == 100 and not last["running"]
    rates = p.measured_rates()
    assert round(rates["zip_unpack"], 1) == 20.0 and round(rates["zip_eval"], 1) == 20.0
    v = view(last)
    assert v["duration_text"] == "Fertig in 0:30 Min."


def test_cancelled_run_is_not_reported_as_finished_ok() -> None:
    p, out, clk = make()
    p.phase("zip_unpack", total=10)
    clk.tick(5)
    p.update(3)
    p.finish(False, "abgebrochen")
    assert out[-1]["pct"] < 100 and out[-1]["duration_text"].startswith("Beendet in")


def test_touch_writes_heartbeat() -> None:
    p, out, clk = make()
    n = len(out)
    clk.tick(6)
    p.touch()
    assert len(out) == n + 1 and out[-1]["elapsed_s"] == 6


def test_formatting() -> None:
    assert fmt_eta(0) == "unter 1 Min." and fmt_eta(-5) == "unter 1 Min." and fmt_eta(44) == "unter 1 Min."
    assert fmt_eta(50) == "ca. 1 Min." and fmt_eta(100) == "ca. 1,5 Min." and fmt_eta(180) == "ca. 3 Min."
    assert fmt_eta(299) == "ca. 5 Min." and fmt_eta(1500) == "ca. 25 Min."
    assert fmt_eta(3600) == "ca. 1 Std." and fmt_eta(3900) == "ca. 1 Std. 5 Min."
    assert fmt_eta(7500) == "ca. 2 Std. 5 Min."
    assert fmt_clock(161) == "2:41" and fmt_clock(3725) == "1:02:05" and fmt_clock(-3) == "0:00"
