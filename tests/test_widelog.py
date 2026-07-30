from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import widelog
from widelog import (
    REDACTED,
    WideEvent,
    WidelogError,
    WidelogMiddleware,
    init,
    lambda_wide_event,
    use_logger,
    wide_event,
)


@pytest.fixture
def seen() -> Any:
    """Capture emitted events instead of writing NDJSON to stdout."""
    saved = dict(widelog._config)
    captured: list[dict[str, Any]] = []
    init(service="checkout", environment="test", sink=captured.append)
    yield captured
    widelog._config.clear()
    widelog._config.update(saved)


@pytest.fixture
def cold() -> Any:
    """Each test gets a fresh container, as far as cold-start detection knows."""
    widelog._cold_start = True
    yield
    widelog._cold_start = True


class FakeContext:
    aws_request_id = "req_1"
    function_name = "checkout-fn"
    memory_limit_in_mb = 512

    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def test_fields_deep_merge_without_clobbering(seen):
    with wide_event(method="POST", path="/api/checkout") as log:
        log.set(user={"id": "u_123", "plan": "premium"})
        log.set(user={"cart_items": 3})

    (event,) = seen
    assert event["user"] == {"id": "u_123", "plan": "premium", "cart_items": 3}
    assert event["method"] == "POST" and event["path"] == "/api/checkout"
    assert event["service"] == "checkout" and event["environment"] == "test"
    assert event["duration_ms"] >= 0


def test_one_event_per_operation(seen):
    with wide_event(path="/api/checkout") as log:
        log.set(cart={"items": 3})
        log.info("coupon applied")
        log.set(order={"id": "o_1"})

    assert len(seen) == 1


def test_secrets_are_redacted_at_any_depth(seen):
    with wide_event() as log:
        log.set(user={"password": "hunter2", "api_key": "sk_live_x"})
        log.set(headers=[{"Authorization": "Bearer x"}])

    (event,) = seen
    assert event["user"] == {"password": REDACTED, "api_key": REDACTED}
    assert event["headers"] == [{"Authorization": REDACTED}]


def test_worst_level_wins(seen):
    with wide_event() as log:
        log.info("started")
        log.warn("slow upstream")
        log.debug("noise")

    assert seen[0]["level"] == "warn"


def test_explicit_level_beats_later_error(seen):
    with wide_event() as log:
        log.set_level("warn")
        log.error("recovered downstream failure")

    assert seen[0]["level"] == "warn"


def test_exception_keeps_context_and_reraises(seen):
    with pytest.raises(WidelogError), wide_event(path="/api/pay") as log:
        log.set(payment={"method": "card"})
        raise WidelogError("Payment failed", status=402, why="Card declined", fix="Use another card")

    (event,) = seen
    assert event["level"] == "error" and event["status"] == 402
    assert event["error"]["why"] == "Card declined"
    assert event["error"]["fix"] == "Use another card"
    assert event["error"]["type"] == "WidelogError"
    assert event["payment"] == {"method": "card"}  # context survives the throw


def test_plain_exception_is_captured(seen):
    with pytest.raises(ZeroDivisionError), wide_event() as log:
        log.set(step="math")
        _ = 1 / 0

    (event,) = seen
    assert event["level"] == "error" and event["error"]["type"] == "ZeroDivisionError"
    assert event["error"]["stack"]


def test_internal_never_reaches_the_event(seen):
    err = WidelogError("nope", internal={"db_dsn": "postgres://u:p@host/db"})
    with wide_event() as log:
        log.error(err)

    assert "internal" not in seen[0]["error"]
    assert "db_dsn" not in str(seen[0])


def test_post_emit_mutation_is_dropped(seen, capsys):
    with wide_event() as log:
        log.set(ok=True)

    log.set(late="dropped")
    assert len(seen) == 1 and "late" not in seen[0]
    assert "mutation after emit" in capsys.readouterr().err


def test_use_logger_returns_the_active_event(seen):
    def deep_helper():  # nobody passed it a logger
        use_logger().set(payment={"method": "card"})

    with wide_event(path="/api/pay"):
        deep_helper()

    (event,) = seen
    assert event["payment"] == {"method": "card"} and event["path"] == "/api/pay"


def test_standalone_logger_emits_immediately(seen):
    use_logger().info("no active request")

    (event,) = seen
    assert event["messages"] == [{"level": "info", "message": "no active request"}]


def test_nested_wide_events_are_independent(seen):
    with wide_event(path="/outer") as outer:
        with wide_event(path="/inner") as inner:
            inner.set(scope="inner")
        outer.set(scope="outer")

    inner_event, outer_event = seen
    assert inner_event["path"] == "/inner" and inner_event["scope"] == "inner"
    assert outer_event["path"] == "/outer" and "scope" in outer_event
    assert outer_event["scope"] == "outer"


def test_concurrent_tasks_do_not_share_an_event(seen):
    async def op(name: str, delay: float) -> None:
        with wide_event(path=f"/{name}") as log:
            await asyncio.sleep(delay)
            log.set(who=name)

    async def main() -> None:
        await asyncio.gather(op("a", 0.02), op("b", 0.0))

    asyncio.run(main())

    by_path = {e["path"]: e for e in seen}
    assert by_path["/a"]["who"] == "a"
    assert by_path["/b"]["who"] == "b"


def test_asgi_middleware_records_status_and_route(seen):
    async def app(scope, receive, send):
        use_logger().set(user={"id": "u_1"})
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    scope = {"type": "http", "method": "POST", "path": "/api/checkout"}
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    asyncio.run(WidelogMiddleware(app)(scope, None, send))

    (event,) = seen
    assert event["method"] == "POST" and event["path"] == "/api/checkout"
    assert event["status"] == 201 and event["level"] == "info"
    assert event["user"] == {"id": "u_1"}
    assert len(sent) == 2  # the response still went out untouched


def test_asgi_middleware_marks_5xx_as_error(seen):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        return None

    asyncio.run(WidelogMiddleware(app)({"type": "http", "method": "GET", "path": "/x"}, None, send))

    assert seen[0]["level"] == "error" and seen[0]["status"] == 503


def test_asgi_middleware_ignores_lifespan(seen):
    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"

    asyncio.run(WidelogMiddleware(app)({"type": "lifespan"}, None, None))
    assert not seen


def test_lambda_v2_payload_and_cold_start(seen, cold):
    @lambda_wide_event
    def handler(event, context):
        use_logger().set(order={"id": "o_1", "api_key": "sk_live_x"})
        return {"statusCode": 502}

    gateway_event = {"requestContext": {"http": {"method": "POST", "path": "/checkout"}}}
    result = handler(gateway_event, FakeContext(30_000))

    assert result == {"statusCode": 502}
    (event,) = seen
    assert event["method"] == "POST" and event["path"] == "/checkout"
    assert event["status"] == 502 and event["level"] == "error"
    assert event["request_id"] == "req_1" and event["cold_start"] is True
    assert event["function"] == {"name": "checkout-fn", "memory_mb": 512}
    assert event["order"] == {"id": "o_1", "api_key": REDACTED}
    assert event["remaining_ms"] == 30_000
    assert "timed_out" not in event


def test_lambda_cold_start_only_fires_once(seen, cold):
    @lambda_wide_event
    def handler(event, context):
        return {"statusCode": 200}

    handler({"httpMethod": "GET", "path": "/health"}, FakeContext(30_000))
    handler({"httpMethod": "GET", "path": "/health"}, FakeContext(30_000))

    first, second = seen
    assert first["cold_start"] is True and first["method"] == "GET"  # v1 payload shape
    assert "cold_start" not in second


def test_lambda_trace_id_from_xray(seen, cold, monkeypatch):
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-abc;Parent=def;Sampled=1")

    @lambda_wide_event
    def handler(event, context):
        return None

    handler({}, None)
    assert seen[0]["trace_id"] == "Root=1-abc;Parent=def;Sampled=1"


def test_lambda_emits_before_the_timeout_kills_us(seen, cold):
    @lambda_wide_event
    def handler(event, context):
        time.sleep(0.3)  # outlives the 100ms budget
        return {"statusCode": 200}

    handler({}, FakeContext(600))

    (event,) = seen  # exactly one line: the guard's, not the late normal return
    assert event["timed_out"] is True and event["level"] == "error"


def test_lambda_exception_emits_then_propagates(seen, cold):
    @lambda_wide_event
    def handler(event, context):
        raise WidelogError("boom", status=500, why="upstream down")

    with pytest.raises(WidelogError):
        handler({}, FakeContext(30_000))

    (event,) = seen
    assert event["level"] == "error" and event["error"]["why"] == "upstream down"


def test_emit_is_idempotent(seen):
    event = WideEvent()
    assert event.emit() is not None
    assert event.emit() is None
