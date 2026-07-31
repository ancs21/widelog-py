from __future__ import annotations

import asyncio
import time

import pytest

from widelog import (
    REDACTED,
    TRUNCATED,
    WideEvent,
    WidelogError,
    WidelogMiddleware,
    init,
    lambda_wide_event,
    use_logger,
    wide_event,
)


class FakeContext:
    aws_request_id = "req_1"
    function_name = "checkout-fn"
    memory_limit_in_mb = 512

    def __init__(self, remaining_ms: int, memory_limit_in_mb: int = 512) -> None:
        self._remaining_ms = remaining_ms
        self.memory_limit_in_mb = memory_limit_in_mb

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


def test_lambda_emits_before_the_sandbox_kills_us_for_memory(seen, cold):
    @lambda_wide_event
    def handler(event, context):
        time.sleep(0.4)  # long enough for one poll to see the peak
        use_logger().set(late="dropped")  # the guard already sealed the event
        return {"statusCode": 200}

    handler({}, FakeContext(30_000, memory_limit_in_mb=1))  # any process exceeds 1MB

    (event,) = seen  # exactly one line, not the guard's plus the normal return
    assert event["memory_critical"] is True and event["level"] == "error"
    assert event["memory_limit_mb"] == 1 and event["rss_mb"] > 1
    assert "late" not in event


def test_memory_guard_leaves_a_function_under_its_limit_alone(seen, cold):
    @lambda_wide_event
    def handler(event, context):
        time.sleep(0.4)
        return {"statusCode": 200}

    handler({}, FakeContext(30_000, memory_limit_in_mb=1_000_000))

    (event,) = seen
    assert "memory_critical" not in event and event["level"] == "info"


def test_memory_headroom_of_zero_disables_the_guard(seen, cold):
    init(memory_headroom=0)
    try:

        @lambda_wide_event
        def handler(event, context):
            time.sleep(0.4)
            return {"statusCode": 200}

        handler({}, FakeContext(30_000, memory_limit_in_mb=1))
    finally:
        init(memory_headroom=0.95)

    (event,) = seen
    assert "memory_critical" not in event


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


# --- hardening: a logger must never be the reason a request fails ---


def test_failing_sink_does_not_break_a_successful_request(seen, capsys):
    def broken_sink(event):
        raise RuntimeError("backend down")

    init(sink=broken_sink)
    with wide_event(path="/ok") as log:
        log.set(ok=True)

    assert "dropped the event" in capsys.readouterr().err


def test_failing_sink_leaves_the_original_exception_alone(seen):
    def broken_sink(event):
        raise RuntimeError("backend down")

    init(sink=broken_sink)
    with pytest.raises(WidelogError) as caught, wide_event(path="/pay"):
        raise WidelogError("Payment failed", status=402)

    assert caught.value.status == 402


def test_emit_returns_none_when_the_sink_fails(seen):
    init(sink=lambda event: 1 / 0)
    assert WideEvent().emit() is None


def test_unserializable_field_does_not_break_the_request(seen, capsys):
    class Circular:
        pass

    obj = Circular()
    obj.self = obj  # json.dumps would recurse forever without default=str

    init(sink=None)  # exercise the real stdout writer
    with wide_event() as log:
        log.set(obj=obj)

    assert "Traceback" not in capsys.readouterr().err


# --- hardening: observation must not mutate what it observes ---


def test_set_does_not_mutate_the_callers_dict(seen):
    profile = {"id": "u_1"}
    with wide_event() as log:
        log.set(user=profile)
        log.set(user={"plan": "premium"})

    assert profile == {"id": "u_1"}
    assert seen[0]["user"] == {"id": "u_1", "plan": "premium"}


def test_set_does_not_mutate_a_nested_caller_dict(seen):
    profile = {"address": {"city": "Hanoi"}}
    with wide_event() as log:
        log.set(user=profile)
        log.set(user={"address": {"zip": "10000"}})

    assert profile == {"address": {"city": "Hanoi"}}
    assert seen[0]["user"]["address"] == {"city": "Hanoi", "zip": "10000"}


def test_set_does_not_mutate_the_callers_list(seen):
    tags = ["a"]
    with wide_event() as log:
        log.set(tags=tags)
        log.set(tags=["b"])

    assert tags == ["a"]
    assert seen[0]["tags"] == ["a", "b"]


def test_deeply_nested_payload_is_truncated_not_fatal(seen):
    deep = cursor = {}
    for _ in range(2000):
        cursor["n"] = {}
        cursor = cursor["n"]

    with wide_event() as log:
        log.set(body=deep)

    assert len(seen) == 1
    assert TRUNCATED in str(seen[0])


def test_self_referential_payload_does_not_hang(seen):
    looping: dict = {"name": "root"}
    looping["self"] = looping

    with wide_event() as log:
        log.set(body=looping)

    assert len(seen) == 1
    assert TRUNCATED in str(seen[0])


# --- hardening: redaction has to catch the prefixed spellings too ---


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "user_password",
        "token",
        "refresh_token",
        "access_token",
        "id_token",
        "api_key",
        "x-api-key",
        "apiKey",
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "secret",
        "client_secret",
    ],
)
def test_secret_key_spellings_are_redacted(seen, key):
    with wide_event() as log:
        log.set(**{key: "SENSITIVE"})

    assert seen[0][key] == REDACTED


@pytest.mark.parametrize("key", ["tokens_used", "token_count", "secretary", "cookies_accepted"])
def test_lookalike_keys_are_not_redacted(seen, key):
    with wide_event() as log:
        log.set(**{key: 42})

    assert seen[0][key] == 42


def test_custom_redact_set_is_normalized(seen):
    init(redact={"X-Trace-Secret"})
    with wide_event() as log:
        log.set(x_trace_secret="SENSITIVE", password="not-in-the-custom-set")

    assert seen[0]["x_trace_secret"] == REDACTED
    assert seen[0]["password"] == "not-in-the-custom-set"


def test_a_dict_keyed_by_status_code_still_emits(seen):
    """Redaction asked every key whether it ended in a secret name, which meant
    calling .lower() on it. An int key raised, emit() swallowed it, and the whole
    operation's event was lost over a field JSON serializes fine.
    """
    with wide_event(op="batch") as log:
        log.set(status_counts={200: 981, 404: 12}, rows=996)

    assert len(seen) == 1, "the event was dropped"
    assert seen[0]["status_counts"] == {200: 981, 404: 12}
    assert seen[0]["rows"] == 996


@pytest.mark.parametrize("key", [200, 3.5, True, None])
def test_every_key_type_json_allows_is_loggable(seen, key):
    """json.dumps accepts str, int, float, bool and None as keys. So must we."""
    with wide_event() as log:
        log.set(counts={key: 1})

    assert len(seen) == 1
    assert seen[0]["counts"] == {key: 1}


def test_a_non_string_key_is_never_treated_as_a_secret(seen):
    """Guarding the key type must not become a way to smuggle a secret past
    redaction: string keys around it still redact.
    """
    with wide_event() as log:
        log.set(mixed={200: "ok", "api_token": "SENSITIVE"})

    assert seen[0]["mixed"][200] == "ok"
    assert seen[0]["mixed"]["api_token"] == REDACTED


def test_one_key_follows_whichever_redact_set_is_current(seen):
    """The secret-ness of a key name is cached; the needles are part of that key.

    Without that, the first answer for "password" would outlive the config that
    produced it and leak on the next init(), or redact a field nobody asked for.
    """
    with wide_event() as log:
        log.set(password="SENSITIVE")
    assert seen[0]["password"] == REDACTED

    init(redact={"nothing-alike"})
    with wide_event() as log:
        log.set(password="now-an-ordinary-field")
    assert seen[1]["password"] == "now-an-ordinary-field"

    init(redact={"password"})
    with wide_event() as log:
        log.set(password="SENSITIVE")
    assert seen[2]["password"] == REDACTED


# --- hardening: the fields a backend sorts and charts on ---


def test_timestamp_has_millisecond_precision(seen):
    with wide_event() as log:
        log.set(ok=True)

    timestamp = seen[0]["timestamp"]
    assert timestamp.endswith("Z")
    seconds, _, millis = timestamp[:-1].rpartition(".")
    assert seconds and len(millis) == 3 and millis.isdigit()


def test_standalone_logger_reports_no_duration(seen):
    log = use_logger()  # no active operation, so every call emits on its own
    log.info("a")
    time.sleep(0.3)
    log.info("b")

    first, second = seen
    # A one-off line spans no operation. Reporting the gap since the last call
    # would attribute 300ms of unrelated work to info("b").
    assert "duration_ms" not in first
    assert "duration_ms" not in second
    assert second["messages"] == [{"level": "info", "message": "b"}]
