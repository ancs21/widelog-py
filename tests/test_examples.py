"""The examples are documentation. Keep them runnable."""

from __future__ import annotations

import asyncio
import json

import httpx
from examples.aws_lambda_handler import _api_gateway_v2_event, _LocalContext, handler

from widelog import REDACTED

SUCCESS_CARD = "4242424242424242"
DECLINE_CARD = "4000000000000002"


def test_lambda_example_authorizes(seen, cold):
    result = handler(_api_gateway_v2_event(SUCCESS_CARD), _LocalContext())

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"order_id": "o_1"}

    (event,) = seen
    assert event["level"] == "info" and event["cold_start"] is True
    assert event["method"] == "POST" and event["path"] == "/checkout"
    assert event["payment"] == {
        "method": "card",
        "last4": "4242",
        "token": REDACTED,
        "authorized": True,
    }


def test_lambda_example_declines_without_leaking_internals(seen, cold):
    result = handler(_api_gateway_v2_event(DECLINE_CARD), _LocalContext())

    assert result["statusCode"] == 402
    body = json.loads(result["body"])
    assert body["code"] == "CARD_DECLINED" and body["why"] == "Issuer declined the charge"
    assert "processor_ref" not in result["body"]  # internal= never goes on the wire

    (event,) = seen
    assert event["level"] == "error" and event["status"] == 402
    assert "processor_ref" not in str(event)


def _call(card: str) -> tuple[int, dict]:
    from examples.fastapi_app import app  # imported late: it calls init() at import time

    async def main() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/checkout",
                json={"user_id": "u_123", "card": card, "token": "idem_abc"},
            )
            return response.status_code, response.json()

    return asyncio.run(main())


def test_fastapi_example_authorizes(seen):
    status, body = _call(SUCCESS_CARD)

    assert status == 200 and body == {"order_id": "o_1"}
    (event,) = seen
    assert event["level"] == "info" and event["status"] == 200
    assert event["cart"] == {"items": 3, "total": 9999}
    assert event["order"] == {"id": "o_1"}


def _checkout(sku: str) -> tuple[int, dict]:
    from examples.microservices import gateway  # imported late: init() runs at import

    async def main() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=gateway)
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
            response = await client.post("/checkout", json={"user_id": "u_123", "sku": sku})
            return response.status_code, response.json()

    return asyncio.run(main())


def test_microservices_example_shares_one_trace_across_both_services(seen):
    status, body = _checkout("WIDGET-1")

    assert status == 200 and body == {"order_id": "o_1"}
    downstream, edge = seen
    assert [downstream["service"], edge["service"]] == ["orders", "gateway"]

    # The point of the example: one field joins the two events.
    assert downstream["trace_id"] == edge["trace_id"]
    assert edge["downstream"]["service"] == "orders"
    assert edge["downstream"]["status"] == 200


def test_microservices_example_carries_the_downstream_reason_to_the_edge(seen):
    status, body = _checkout("SOLD-OUT")

    assert status == 409
    assert body["why"].startswith("The last unit")

    downstream, edge = seen
    assert downstream["level"] == "error" and edge["level"] == "error"
    assert downstream["trace_id"] == edge["trace_id"]
    assert downstream["error"]["code"] == edge["error"]["code"] == "OUT_OF_STOCK"
    assert edge["downstream"]["status"] == 409

    # internal= is scoped to the service that raised. It must not reach that
    # service's own event, the caller's event, or the client.
    assert "syd-3" not in str(downstream)
    assert "syd-3" not in str(edge)
    assert "syd-3" not in json.dumps(body)


def test_fastapi_example_declines_with_why_and_fix(seen):
    status, body = _call(DECLINE_CARD)

    assert status == 402
    assert body["why"] == "Issuer declined the charge"
    assert body["fix"].startswith("Try a different")
    assert "processor_ref" not in json.dumps(body)

    # The exception handler is the wiring under test: without it FastAPI answers
    # 500 and the error never reaches the wide event.
    (event,) = seen
    assert event["level"] == "error" and event["status"] == 402
    assert event["error"]["code"] == "CARD_DECLINED"
    assert event["payment"]["last4"] == "0002"
    assert "processor_ref" not in str(event)
