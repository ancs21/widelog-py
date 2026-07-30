"""The OTLP transport, against a real HTTP server on localhost.

Nothing here is mocked: the sink makes actual requests to a collector stand-in,
because what is under test is whether the bytes arrive in the right shape.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from widelog import init, wide_event
from widelog.otlp import OTLPSink


class Collector:
    """A stand-in OTLP collector that records what it was sent."""

    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.requests: list[dict] = []
        self.status = status
        self.body = body
        received = self.requests
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                received.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(self.rfile.read(length) or b"{}"),
                    }
                )
                self.send_response(outer.status)
                self.send_header("content-length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args: object) -> None:
                pass  # keep pytest output readable

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def collector():
    started = Collector()
    yield started
    started.close()


def records(request: dict) -> list[dict]:
    return [
        record
        for resource_logs in request["body"]["resourceLogs"]
        for scope_logs in resource_logs["scopeLogs"]
        for record in scope_logs["logRecords"]
    ]


def test_an_event_arrives_at_the_collector_as_otlp(collector):
    sink = OTLPSink(endpoint=collector.endpoint)
    init(service="checkout", environment="prod", sink=sink)

    with wide_event(op="checkout") as log:
        log.set(user={"id": "u_1"})
    sink.close()

    (request,) = collector.requests
    assert request["path"] == "/v1/logs"
    assert request["headers"]["Content-Type"] == "application/json"

    (record,) = records(request)
    assert record["severityText"] == "INFO"
    attrs = {a["key"]: a["value"] for a in record["attributes"]}
    assert attrs["op"] == {"stringValue": "checkout"}
    assert attrs["user.id"] == {"stringValue": "u_1"}


def test_configured_headers_are_sent(collector):
    sink = OTLPSink(endpoint=collector.endpoint, headers={"Authorization": "Bearer xyz"})
    init(service="checkout", sink=sink)

    with wide_event(op="checkout"):
        pass
    sink.close()

    assert collector.requests[0]["headers"]["Authorization"] == "Bearer xyz"


def test_the_endpoint_may_already_name_the_signal(collector):
    """Half the world's OTLP config already ends in /v1/logs. Do not double it."""
    sink = OTLPSink(endpoint=f"{collector.endpoint}/v1/logs")
    init(service="checkout", sink=sink)

    with wide_event(op="checkout"):
        pass
    sink.close()

    assert collector.requests[0]["path"] == "/v1/logs"


def test_events_queued_together_are_sent_as_one_request(collector):
    sink = OTLPSink(endpoint=collector.endpoint)

    for index in range(5):
        sink({"timestamp": "2026-07-30T07:44:05.349Z", "level": "info", "service": "s", "n": index})
    sink.close()

    # Opportunistic batching: whatever piled up goes in one request. The point is
    # that five events never cost five round trips.
    assert sum(len(records(request)) for request in collector.requests) == 5
    assert len(collector.requests) < 5


def test_two_services_do_not_get_merged_into_one_resource(collector):
    """A batch spanning services must keep them apart, or a backend attributes
    one service's events to another."""
    sink = OTLPSink(endpoint=collector.endpoint)

    for service in ("gateway", "orders"):
        sink({"timestamp": "2026-07-30T07:44:05.349Z", "level": "info", "service": service})
    sink.close()

    resources = [
        tuple(sorted((a["key"], a["value"]["stringValue"]) for a in rl["resource"]["attributes"]))
        for request in collector.requests
        for rl in request["body"]["resourceLogs"]
    ]
    assert sorted(resources) == [
        (("service.name", "gateway"),),
        (("service.name", "orders"),),
    ]


def test_extra_resource_attributes_are_attached(collector):
    sink = OTLPSink(endpoint=collector.endpoint, resource_attributes={"cloud.provider": "aws"})
    init(service="checkout", sink=sink)

    with wide_event(op="checkout"):
        pass
    sink.close()

    (resource_logs,) = collector.requests[0]["body"]["resourceLogs"]
    attrs = {a["key"]: a["value"] for a in resource_logs["resource"]["attributes"]}
    assert attrs["cloud.provider"] == {"stringValue": "aws"}


# --- the guarantee that matters ----------------------------------------------


def test_a_dead_collector_does_not_reach_the_caller(capsys):
    """widelog's one rule: logging cannot fail the request it describes. Nothing
    is listening on this port, so every send fails."""
    sink = OTLPSink(endpoint="http://127.0.0.1:9")
    init(service="checkout", sink=sink)

    with wide_event(op="checkout") as log:
        log.set(user={"id": "u_1"})
    sink.close()

    assert "[widelog/otlp]" in capsys.readouterr().err


def test_a_rejecting_collector_is_reported_not_raised(capsys):
    rejecting = Collector(status=401)
    try:
        sink = OTLPSink(endpoint=rejecting.endpoint)
        sink({"timestamp": "2026-07-30T07:44:05.349Z", "level": "info", "service": "s"})
        sink.close()
    finally:
        rejecting.close()

    assert "401" in capsys.readouterr().err


def test_a_rejection_reports_what_the_collector_said(capsys):
    """A status code alone sends you to curl to find out what was wrong with the
    payload. Real case: OpenObserve answers 400 and the body names the field.
    """
    rejecting = Collector(
        status=400,
        body=b'{"code":400,"message":"Invalid json: invalid type: map, expected f64"}',
    )
    try:
        sink = OTLPSink(endpoint=rejecting.endpoint)
        sink({"timestamp": "2026-07-30T07:44:05.349Z", "level": "info", "service": "s"})
        sink.close()
    finally:
        rejecting.close()

    err = capsys.readouterr().err
    assert "400" in err
    assert "expected f64" in err


def test_the_caller_is_never_blocked_by_a_full_queue(capsys):
    """Back pressure from a slow collector must cost events, never latency."""
    sink = OTLPSink(endpoint="http://127.0.0.1:9", queue_size=2)

    for index in range(500):
        sink({"timestamp": "2026-07-30T07:44:05.349Z", "level": "info", "service": "s", "n": index})
    sink.close()

    assert sink.dropped > 0
    assert "dropped" in capsys.readouterr().err


def test_an_endpoint_is_required():
    with pytest.raises(ValueError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        OTLPSink(endpoint=None)
