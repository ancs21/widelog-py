"""Wide events as OTLP, over HTTP with JSON, using nothing outside the standard library.

    from widelog import init
    from widelog.otlp import OTLPSink

    init(service="checkout", sink=OTLPSink(endpoint="http://localhost:4318"))

One wide event becomes one OTLP log record: `service` and `environment` describe
the resource, `level` becomes a severity, and everything else becomes attributes.
Sending happens on a background thread, because widelog is not allowed to make
your request wait on a collector.

JSON over HTTP rather than protobuf over gRPC, because that is the OTLP encoding
reachable from the standard library. Every collector accepts it.

This module is optional. Importing `widelog` does not import it, so a project that
writes NDJSON to stdout pays nothing for it.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
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


def _log_record(event: dict[str, Any]) -> dict[str, Any]:
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
    return record


def _resource(event: dict[str, Any], extra: dict[str, Any] | None) -> list[dict[str, Any]]:
    attributes = [
        {"key": name, "value": _value(event[field])} for field, name in _RESOURCE.items() if field in event
    ]
    return attributes + [{"key": k, "value": _value(v)} for k, v in (extra or {}).items()]


def to_otlp_batch(
    events: list[dict[str, Any]], resource_attributes: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Many wide events as one export request, grouped by the resource they describe.

    The grouping is not cosmetic. One process can emit for more than one service
    -- a gateway calling a downstream in the same runtime, anything that overrides
    `service` per event -- and flattening those into a single resource would file
    one service's events under another's name.
    """
    groups: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    for event in events:
        attributes = _resource(event, resource_attributes)
        key = json.dumps(attributes, sort_keys=True)
        groups.setdefault(key, (attributes, []))[1].append(_log_record(event))

    return {
        "resourceLogs": [
            {
                "resource": {"attributes": attributes},
                "scopeLogs": [
                    {"scope": {"name": "widelog", "version": __version__}, "logRecords": records},
                ],
            }
            for attributes, records in groups.values()
        ]
    }


def to_otlp(event: dict[str, Any], resource_attributes: dict[str, Any] | None = None) -> dict[str, Any]:
    """One wide event as an OTLP/HTTP JSON ExportLogsServiceRequest."""
    return to_otlp_batch([event], resource_attributes)


def _env_headers() -> dict[str, str]:
    """OTEL_EXPORTER_OTLP_HEADERS is `k=v,k2=v2`, and Grafana url-encodes its values."""
    raw = os.getenv("OTEL_EXPORTER_OTLP_HEADERS") or os.getenv("OTLP_HEADERS") or ""
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {k.strip(): urllib.parse.unquote(v.strip()) for k, v in pairs}


_STOP = object()


class OTLPSink:
    """A widelog sink that exports to an OTLP collector.

        init(service="checkout", sink=OTLPSink(endpoint="http://localhost:4318"))

    Events go onto a queue and a background thread sends them, so the request
    being logged never waits on the network. When the queue is full events are
    dropped and counted rather than blocking the caller: widelog is not allowed
    to make the thing it describes slower or more likely to fail.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        resource_attributes: dict[str, Any] | None = None,
        timeout: float = 5.0,
        batch_size: int = 100,
        queue_size: int = 10_000,
    ) -> None:
        endpoint = endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTLP_ENDPOINT")
        if not endpoint:
            raise ValueError(
                "widelog.otlp needs a collector: pass endpoint= or set OTEL_EXPORTER_OTLP_ENDPOINT"
            )
        # Half the world's OTLP config is a base URL and the other half already
        # names the signal. Accept both rather than making it the caller's problem.
        trimmed = endpoint.rstrip("/")
        self.url = trimmed if trimmed.endswith("/v1/logs") else f"{trimmed}/v1/logs"
        self.headers = {"Content-Type": "application/json", **_env_headers(), **(headers or {})}
        self.resource_attributes = resource_attributes
        self.timeout = timeout
        self.batch_size = batch_size
        self.dropped = 0

        self._closed = False
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._worker = threading.Thread(target=self._run, name="widelog-otlp", daemon=True)
        self._worker.start()
        atexit.register(self.close)

    def __call__(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1:
                # Once. A collector that cannot keep up would otherwise produce
                # more output on stderr than it is failing to accept.
                print(
                    "[widelog/otlp] queue full, dropped an event. "
                    "Further drops are silent; check OTLPSink.dropped.",
                    file=sys.stderr,
                )

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            # ponytail: batch whatever has piled up rather than waiting on a timer.
            # Busy processes batch, idle ones send at once. Add a linger interval if
            # request count against the collector ever matters more than latency.
            batch, stopping = [item], False
            while len(batch) < self.batch_size:
                try:
                    following = self._queue.get_nowait()
                except queue.Empty:
                    break
                if following is _STOP:
                    stopping = True
                    break
                batch.append(following)

            self._send(batch)
            for _ in batch:
                self._queue.task_done()
            if stopping:
                self._queue.task_done()
                return

    def _send(self, batch: list[dict[str, Any]]) -> None:
        """Never raises. A collector's bad day is not the application's."""
        try:
            payload = json.dumps(to_otlp_batch(batch, self.resource_attributes), default=str)
            request = urllib.request.Request(
                self.url, data=payload.encode(), headers=self.headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except urllib.error.HTTPError as exc:
            # The body is where a collector says which field it disliked. Without
            # it a 400 is a scavenger hunt with curl. Truncated, since a rejection
            # can echo the payload back.
            try:
                detail = exc.read()[:400].decode(errors="replace").strip()
            except Exception:
                detail = ""
            print(
                f"[widelog/otlp] collector rejected {len(batch)} event(s): {exc.code} {detail}".rstrip(),
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"[widelog/otlp] dropped {len(batch)} event(s): {exc!r}", file=sys.stderr)

    def flush(self) -> None:
        """Block until everything queued has been sent."""
        self._queue.join()

    def close(self) -> None:
        """Flush and stop the worker. Registered to run at interpreter exit."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STOP)
        self._worker.join(timeout=self.timeout + 1)
