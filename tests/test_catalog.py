from __future__ import annotations

import pytest

from widelog import ErrorCatalog, ErrorFactory, ErrorSpec, WidelogError, wide_event


class BillingErrors(ErrorCatalog, prefix="billing"):
    CART_EMPTY = ErrorSpec(status=400, message="Cart is empty")
    PAYMENT_DECLINED = ErrorSpec(
        status=402,
        message="Card declined",
        why="Issuer declined the charge",
        fix="Try a different payment method",
        link="https://docs.example.com/errors/payment-declined",
    )
    INSUFFICIENT_FUNDS = ErrorSpec(
        status=402,
        message=lambda available, required: f"Insufficient funds: ${available} of ${required}",
        fix="Add funds and retry",
    )
    FRAUD_HOLD = ErrorSpec(
        status=403,
        message="Held for review",
        tags=("billing", "manual-review"),
        internal={"queue": "manual"},
    )


def test_code_is_the_prefix_plus_the_attribute_name():
    assert BillingErrors.CART_EMPTY.code == "billing.CART_EMPTY"
    assert BillingErrors.PAYMENT_DECLINED.code == "billing.PAYMENT_DECLINED"


def test_calling_an_entry_builds_a_widelog_error():
    err = BillingErrors.PAYMENT_DECLINED()

    assert isinstance(err, WidelogError)
    assert err.code == "billing.PAYMENT_DECLINED" and err.status == 402
    assert err.message == "Card declined"
    assert err.why == "Issuer declined the charge"
    assert err.to_dict()["fix"] == "Try a different payment method"


def test_templated_message_takes_typed_params():
    err = BillingErrors.INSUFFICIENT_FUNDS(available=5, required=100)

    assert err.message == "Insufficient funds: $5 of $100"
    assert err.status == 402 and err.fix == "Add funds and retry"


def test_a_missing_template_param_fails_at_the_call_site():
    with pytest.raises(TypeError):
        BillingErrors.INSUFFICIENT_FUNDS(available=5)


def test_a_misspelled_param_on_a_constant_message_fails_loudly():
    with pytest.raises(TypeError, match="takes no message params"):
        BillingErrors.CART_EMPTY(availble=5)


def test_call_site_overrides_beat_the_spec():
    cause = ValueError("stripe said no")
    err = BillingErrors.PAYMENT_DECLINED(link="/support/payment-issues", cause=cause)

    assert err.link == "/support/payment-issues"
    assert err.__cause__ is cause
    assert err.why == "Issuer declined the charge"  # untouched defaults survive


def test_message_can_be_overridden_on_a_templated_entry():
    err = BillingErrors.INSUFFICIENT_FUNDS(message="Not enough money")

    assert err.message == "Not enough money"


def test_internal_merges_with_the_call_site_winning():
    err = BillingErrors.FRAUD_HOLD(internal={"queue": "priority", "score": 0.98})

    assert err.internal == {"queue": "priority", "score": 0.98}


def test_internal_defaults_survive_a_partial_override():
    err = BillingErrors.FRAUD_HOLD(internal={"score": 0.98})

    assert err.internal == {"queue": "manual", "score": 0.98}


def test_codes_lists_every_entry_in_declaration_order():
    assert BillingErrors.codes() == (
        "billing.CART_EMPTY",
        "billing.PAYMENT_DECLINED",
        "billing.INSUFFICIENT_FUNDS",
        "billing.FRAUD_HOLD",
    )


def test_spec_metadata_is_readable_without_raising():
    assert BillingErrors.PAYMENT_DECLINED.status == 402
    assert BillingErrors.FRAUD_HOLD.tags == ("billing", "manual-review")
    assert BillingErrors.CART_EMPTY.why is None


def test_tags_stay_out_of_the_wire_format():
    assert "tags" not in BillingErrors.FRAUD_HOLD().to_dict()


def test_branching_on_a_code_without_repeating_the_string(seen):
    with pytest.raises(WidelogError) as caught, wide_event(path="/api/pay"):
        raise BillingErrors.PAYMENT_DECLINED(internal={"processor_ref": "ch_live_x9"})

    assert caught.value.code == BillingErrors.PAYMENT_DECLINED.code

    (event,) = seen
    assert event["status"] == 402 and event["level"] == "error"
    assert event["error"]["code"] == "billing.PAYMENT_DECLINED"
    assert event["error"]["fix"] == "Try a different payment method"
    assert "processor_ref" not in str(event)


def test_a_standalone_factory_needs_no_catalog():
    fraud_detected = ErrorFactory(
        "billing.FRAUD_DETECTED",
        ErrorSpec(status=403, message="Transaction flagged for review"),
    )
    err = fraud_detected()

    assert err.code == "billing.FRAUD_DETECTED" and err.status == 403


def test_a_catalog_needs_a_prefix():
    with pytest.raises(ValueError, match="prefix"):

        class Bad(ErrorCatalog, prefix=""):
            X = ErrorSpec(message="x")


def test_non_spec_attributes_are_left_alone():
    class Mixed(ErrorCatalog, prefix="mixed"):
        RETRY_AFTER_SECONDS = 30
        LOCKED = ErrorSpec(status=423, message="Locked")

    assert Mixed.RETRY_AFTER_SECONDS == 30
    assert Mixed.codes() == ("mixed.LOCKED",)
