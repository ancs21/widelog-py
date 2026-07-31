"""The OTLP mapping: one wide event becomes one OTLP log record."""

from __future__ import annotations

import pytest

from widelog.otlp import to_otlp

TS = "2026-07-30T07:44:05.349Z"
TS_NANOS = "1785397445349000000"


def event(**fields):
    base = {"timestamp": TS, "level": "info", "service": "checkout", "environment": "prod"}
    return {**base, **fields}


def record(payload):
    """The single log record inside the export request."""
    (resource_logs,) = payload["resourceLogs"]
    (scope_logs,) = resource_logs["scopeLogs"]
    (log_record,) = scope_logs["logRecords"]
    return log_record


def attributes(payload):
    """Log record attributes as a flat dict, so tests read as assertions not walks."""
    return {a["key"]: a["value"] for a in record(payload)["attributes"]}


def resource_attributes(payload):
    (resource_logs,) = payload["resourceLogs"]
    return {a["key"]: a["value"] for a in resource_logs["resource"]["attributes"]}


# --- envelope ----------------------------------------------------------------


def test_the_envelope_is_one_export_request_with_one_record():
    payload = to_otlp(event(op="checkout"))

    assert list(payload) == ["resourceLogs"]
    (resource_logs,) = payload["resourceLogs"]
    (scope_logs,) = resource_logs["scopeLogs"]
    assert scope_logs["scope"]["name"] == "widelog"
    assert len(scope_logs["logRecords"]) == 1


def test_service_and_environment_become_resource_attributes():
    """A backend groups by resource, so these belong there and not on the record."""
    payload = to_otlp(event())

    assert resource_attributes(payload) == {
        "service.name": {"stringValue": "checkout"},
        "deployment.environment": {"stringValue": "prod"},
    }
    assert "service" not in attributes(payload)
    assert "environment" not in attributes(payload)


def test_version_and_region_map_to_their_otel_names():
    payload = to_otlp(event(version="1.4.0", region="ap-southeast-2"))

    res = resource_attributes(payload)
    assert res["service.version"] == {"stringValue": "1.4.0"}
    assert res["cloud.region"] == {"stringValue": "ap-southeast-2"}


# --- time and severity -------------------------------------------------------


def test_timestamp_becomes_nanoseconds_as_a_string():
    """OTLP JSON carries 64-bit ints as strings, so a collector cannot lose precision."""
    payload = to_otlp(event())

    assert record(payload)["timeUnixNano"] == TS_NANOS
    assert isinstance(record(payload)["timeUnixNano"], str)


@pytest.mark.parametrize(
    ("level", "number", "text"),
    [("debug", 5, "DEBUG"), ("info", 9, "INFO"), ("warn", 13, "WARN"), ("error", 17, "ERROR")],
)
def test_each_level_maps_to_its_otel_severity(level, number, text):
    payload = to_otlp(event(level=level))

    assert record(payload)["severityNumber"] == number
    assert record(payload)["severityText"] == text


# --- attribute types ---------------------------------------------------------


def test_each_scalar_maps_to_the_typed_value_otlp_expects():
    payload = to_otlp(event(name="cart", total=99.5, retries=3))

    attrs = attributes(payload)
    assert attrs["name"] == {"stringValue": "cart"}
    assert attrs["total"] == {"doubleValue": 99.5}
    assert attrs["retries"] == {"intValue": "3"}  # int64 is a string in OTLP JSON


def test_a_bool_is_not_an_int():
    """bool subclasses int in Python. Checked in the wrong order, True becomes 1."""
    payload = to_otlp(event(ok=True, cache_hit=False))

    attrs = attributes(payload)
    assert attrs["ok"] == {"boolValue": True}
    assert attrs["cache_hit"] == {"boolValue": False}


def test_a_nested_dict_is_carried_as_json_text():
    """Matches evlog: the object keeps its shape, as a string. A backend stores it
    verbatim, so `user.id` is not a column and only substring matching reaches it.
    """
    payload = to_otlp(event(user={"id": "u_1", "tier": "gold"}))

    attrs = attributes(payload)
    assert attrs["user"] == {"stringValue": '{"id":"u_1","tier":"gold"}'}
    assert "user.id" not in attrs


def test_a_list_is_carried_as_json_text_too():
    payload = to_otlp(event(items=[{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]))

    assert attributes(payload)["items"] == {"stringValue": '[{"sku":"A","qty":2},{"sku":"B","qty":1}]'}


def test_a_key_json_refuses_costs_the_field_and_not_the_batch():
    """`json.dumps` takes `str`, `int`, `float`, `bool` and `None` keys and raises on
    anything else, and `default=` rescues values but never keys. The sink serializes a
    whole batch in one call, so one tuple-keyed dict would take up to `batch_size`
    unrelated events with it -- the 2026.7.2 bug again, with a wider blast radius.
    """
    payload = to_otlp(event(grid={(0, 0): "empty"}))

    assert attributes(payload)["grid"] == {"stringValue": "{(0, 0): 'empty'}"}


def test_scalars_keep_their_types():
    """Only the nested shapes become text. A number stays a number, so the fields
    a backend aggregates on still work.
    """
    payload = to_otlp(event(total=99.5, retries=3, ok=True, name="cart"))

    attrs = attributes(payload)
    assert attrs["total"] == {"doubleValue": 99.5}
    assert attrs["retries"] == {"intValue": "3"}
    assert attrs["ok"] == {"boolValue": True}
    assert attrs["name"] == {"stringValue": "cart"}


# --- trace correlation -------------------------------------------------------


def test_a_real_trace_id_is_promoted_to_the_record():
    payload = to_otlp(event(trace_id="4bf92f3577b34da6a3ce929d0e0e4736", span_id="00f067aa0ba902b7"))

    assert record(payload)["traceId"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert record(payload)["spanId"] == "00f067aa0ba902b7"
    assert "trace_id" not in attributes(payload)


@pytest.mark.parametrize(
    "trace_id",
    [
        "t_a1b2c3d4e5f6",  # what the microservices example mints
        "Root=1-5759e988-bd862e3fe1be46a994272793",  # what X-Ray puts in the env
        "4bf92f3577b34da6",  # right alphabet, wrong length
        "zzzz2f3577b34da6a3ce929d0e0e4736",  # right length, not hex
    ],
)
def test_an_id_that_is_not_a_trace_id_stays_an_attribute(trace_id):
    """OTLP wants 16 bytes of hex. Anything else would be rejected or silently
    dropped by the collector, so keep it where it is still searchable.
    """
    payload = to_otlp(event(trace_id=trace_id))

    assert "traceId" not in record(payload)
    assert attributes(payload)["trace_id"] == {"stringValue": trace_id}


# --- body --------------------------------------------------------------------


def test_the_error_message_becomes_the_body():
    """Body is the line a backend shows in a list. An error is what you want there."""
    payload = to_otlp(event(level="error", error={"message": "Payment failed", "code": "DECLINED"}))

    assert record(payload)["body"] == {"stringValue": "Payment failed"}
    assert attributes(payload)["error"] == {"stringValue": '{"message":"Payment failed","code":"DECLINED"}'}


def test_the_last_message_becomes_the_body_when_nothing_failed():
    payload = to_otlp(
        event(
            messages=[
                {"level": "info", "message": "cart loaded"},
                {"level": "warn", "message": "cart nearly empty"},
            ]
        )
    )

    assert record(payload)["body"] == {"stringValue": "cart nearly empty"}


def test_an_event_with_nothing_to_say_has_no_body():
    payload = to_otlp(event(op="checkout"))

    assert "body" not in record(payload)
