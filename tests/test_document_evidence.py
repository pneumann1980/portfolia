"""Synthetic tests for strict evidence attribution and economic-data safeguards."""
from app.documentimport.evidence import FieldEvidence, resolve_field, resolve_fields, safe_for_booking


def e(field="fee", value="1", origin="document", status="belegt", **kw):
    return FieldEvidence(field, value, origin, kw.pop("source_ref", "sha256:abc"), "Seite 1",
                         status, "synthetic evidence", **kw)


def test_verified_provider_event_can_reconstruct_fee():
    decision = resolve_field("fee", [
        e(value="0.25", origin="provider", event_key="binance:123",
          verified_link=True, status="rekonstruiert")
    ], event_key="binance:123")
    assert decision.selected is not None
    assert decision.selected.value == "0.25"


def test_unverified_original_source_never_used_for_other_event():
    decision = resolve_field("fee", [
        e(value="0.25", origin="provider", event_key="binance:123", verified_link=True)
    ], event_key="binance:456")
    assert decision.selected is None


def test_conflicts_preserved_instead_of_overwriting():
    decision = resolve_field("fee", [e(value="1.00"), e(value="2.00", source_ref="doc:other")])
    assert decision.selected is not None
    assert len(decision.conflicts) == 1
    assert decision.review_required


def test_decimal_equivalence_does_not_create_conflict():
    decision = resolve_field("fee", [e(value="1.00"), e(value="1.0", source_ref="doc:other")])
    assert not decision.conflicts


def test_public_reference_price_cannot_be_actual_execution():
    decision = resolve_field("price", [
        e(field="price", value="50", origin="public", status="geschaetzt",
          category="market_price")
    ])
    assert decision.selected is None


def test_estimate_does_not_pass_booking_guard():
    decisions = resolve_fields([e(field="value_eur", value="90", status="geschaetzt")])
    assert not safe_for_booking(decisions, {"value_eur"})


def test_verified_fields_only_pass_necessary_guard():
    decisions = resolve_fields([e(field="quantity", value="4"),
                                e(field="value_eur", value="92")])
    assert safe_for_booking(decisions, {"quantity", "value_eur"})
    assert not safe_for_booking(decisions, {"quantity", "value_eur", "fee"})
