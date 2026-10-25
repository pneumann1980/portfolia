"""Schema der Regelwerk-Dateien für Deutschland (Schlüssel und plausible Wertebereiche)."""

from app.tax.schema import Bool, Enum, Int, Num, Schema, YearMap

FUND_TYPES = ("etf_equity", "etf_mixed", "fund_realestate", "fund_realestate_foreign", "etf_other")

SCHEMA: Schema = {
    "rules": {
        "currency": Enum(("EUR",)),
        "crypto": {
            "holding_period_years": Int(0, 30),
            "freigrenze_23": Num(0, 10_000_000, warn_factor=5),
            "freigrenze_22_3": Num(0, 10_000_000, warn_factor=5),
            # Schalter (Standard: wie geltendes Recht) – für Gesetzesänderungen und frühere Rechtslagen
            "exempt_after_holding": Bool(),
            "freigrenze_mode": Enum(("freigrenze", "freibetrag")),
        },
        "capital": {
            "flat_rate": Num(0, 1, warn_factor=2),
            "soli_rate": Num(0, 0.2),
            "sparer_pauschbetrag_single": Num(0, 1_000_000, warn_factor=5),
            "sparer_pauschbetrag_joint": Num(0, 2_000_000, warn_factor=5),
            "vp_factor": Num(0, 1),
            "wht_credit_cap": Num(0, 1),
            "derivative_loss_cap": Num(0, 1_000_000_000, nullable=True),
            "teilfreistellung": {t: Num(0, 1) for t in FUND_TYPES},
        },
    },
    "per_year": {"basiszins": YearMap(Num(-0.1, 0.2))},
}
