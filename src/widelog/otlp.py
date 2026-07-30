"""Wide events as OTLP, over HTTP with JSON, using nothing outside the standard library.

One wide event becomes one OTLP log record: `service` and `environment` describe
the resource, `level` becomes a severity, and everything else becomes attributes.

This module is optional. Importing `widelog` does not import it, so a project that
writes NDJSON to stdout pays nothing for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from widelog import __version__

# OpenTelemetry's severity numbers. The gaps are deliberate: the scale has four
# steps per level, and widelog only uses the unqualified one of each.
_SEVERITY = {"debug": (5, "DEBUG"), "info": (9, "INFO"), "warn": (13, "WARN"), "error": (17, "ERROR")}

# Fields that describe the emitter rather than the operation. OTLP puts these on
# the resource, so a backend can group by them without reading every record.
_RESOURCE = {
    "service": "service.name",
    "environment": "deployment.environment",
    "version": "service.version",
    "region": "cloud.region",
}

# Consumed into the record itself, so they must not also appear as attributes.
_CONSUMED = frozenset({"timestamp", "level", *_RESOURCE})

_HEX = frozenset("0123456789abcdefABCDEF")


def _nanos(timestamp: str) -> str:
    """OTLP JSON carries 64-bit integers as strings, so no collector rounds them."""
    # fromisoformat cannot read a trailing Z until 3.11, and widelog supports 3.10.
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return str(int(parsed.timestamp() * 1_000_000) * 1_000)


def _is_id(value: Any, digits: int) -> bool:
    """A trace or span id is a fixed number of hex characters and nothing else.

    Anything else -- an X-Ray header, a hand-rolled correlation id -- would be
    rejected or silently dropped by the collector, so it stays an attribute where
    it is still searchable.
    """
    return isinstance(value, str) and len(value) == digits and all(c in _HEX for c in value)


def _value(value: Any) -> dict[str, Any]:
    """One Python value as an OTLP AnyValue."""
    # bool subclasses int, so it has to be tested first or True serializes as 1.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_value(item) for item in value]}}
    if isinstance(value, dict):
        # Inside a list there is no key to flatten onto, so the shape is kept.
        return {"kvlistValue": {"values": [{"key": str(k), "value": _value(v)} for k, v in value.items()]}}
    if value is None:
        return {}
    return {"stringValue": value if isinstance(value, str) else str(value)}


def _flatten(key: str, value: Any, out: list[dict[str, Any]]) -> None:
    """Nested dicts become dotted keys, which is what OTLP backends index on."""
    if isinstance(value, dict):
        for inner, item in value.items():
            _flatten(f"{key}.{inner}", item, out)
    else:
        out.append({"key": key, "value": _value(value)})


def _body(event: dict[str, Any]) -> dict[str, Any] | None:
    """The line a backend shows in a list. A failure earns it, then a message."""
    error = event.get("error")
    if isinstance(error, dict) and error.get("message"):
        return {"stringValue": str(error["message"])}
    messages = event.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict) and last.get("message"):
            return {"stringValue": str(last["message"])}
    return None


def to_otlp(event: dict[str, Any], resource_attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """One wide event as an OTLP/HTTP JSON ExportLogsServiceRequest."""
    severity, severity_text = _SEVERITY.get(str(event.get("level")), _SEVERITY["info"])

    record: dict[str, Any] = {
        "timeUnixNano": _nanos(str(event["timestamp"])),
        "severityNumber": severity,
        "severityText": severity_text,
    }
    record["observedTimeUnixNano"] = record["timeUnixNano"]

    body = _body(event)
    if body:
        record["body"] = body

    attributes: list[dict[str, Any]] = []
    for key, value in event.items():
        if key in _CONSUMED:
            continue
        # Promote a real trace id onto the record, where a backend links on it.
        if key == "trace_id" and _is_id(value, 32):
            record["traceId"] = value
            continue
        if key == "span_id" and _is_id(value, 16):
            record["spanId"] = value
            continue
        _flatten(key, value, attributes)
    record["attributes"] = attributes

    resource = [
        {"key": name, "value": _value(event[field])} for field, name in _RESOURCE.items() if field in event
    ]
    resource += [{"key": k, "value": _value(v)} for k, v in (resource_attributes or {}).items()]

    return {
        "resourceLogs": [
            {
                "resource": {"attributes": resource},
                "scopeLogs": [
                    {"scope": {"name": "widelog", "version": __version__}, "logRecords": [record]},
                ],
            }
        ]
    }
