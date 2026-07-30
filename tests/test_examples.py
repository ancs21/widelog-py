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
