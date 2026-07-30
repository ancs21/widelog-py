"""AWS Lambda with widelog: one wide event per invocation.

Run it locally without an AWS account or any dependencies:

    uv run examples/aws_lambda_handler.py

Deploy it as it is. The default sink writes NDJSON to stdout, and Lambda forwards
stdout to CloudWatch Logs, so you do not need a drain or a waitUntil callback.
"""

from __future__ import annotations

import json
from typing import Any

from widelog import WidelogError, init, lambda_wide_event, use_logger

init(service="checkout")


def charge(card: str, token: str) -> str:
    # No logger was passed in. It still lands in this invocation's wide event.
    log = use_logger()
    log.set(payment={"method": "card", "last4": card[-4:], "token": token})

    if card.startswith("4000"):  # 4000 0000 0000 0002 is the test decline card
        raise WidelogError(
            "Payment failed",
            code="CARD_DECLINED",
            status=402,
            why="Issuer declined the charge",
            fix="Try a different payment method or contact your bank",
            internal={"processor_ref": "ch_live_x9"},  # stays server-side
        )

    log.set(payment={"authorized": True})
    return "o_1"


@lambda_wide_event
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    body = json.loads(event.get("body") or "{}")
    log = use_logger()
    log.set(user={"id": body.get("user_id")})

    try:
        order_id = charge(body.get("card", ""), body.get("token", ""))
    except WidelogError as exc:
        # Catch and return, so API Gateway answers 402 instead of 502.
        # Let it raise instead when you want Lambda to mark the invocation
        # failed and retry. The event is emitted either way.
        log.error(exc)
        return {"statusCode": exc.status, "body": json.dumps(exc.to_dict())}

    log.set(order={"id": order_id})
    return {"statusCode": 200, "body": json.dumps({"order_id": order_id})}


def _api_gateway_v2_event(card: str) -> dict[str, Any]:
    return {
        "version": "2.0",
        "requestContext": {"http": {"method": "POST", "path": "/checkout"}},
        "body": json.dumps({"user_id": "u_123", "card": card, "token": "idem_abc"}),
    }


class _LocalContext:
    """Stand-in for the Lambda context object."""

    aws_request_id = "req_local_1"
    function_name = "checkout-fn"
    function_version = "$LATEST"
    memory_limit_in_mb = 512

    def __init__(self, remaining_ms: int = 30_000) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


if __name__ == "__main__":
    print("→ authorized (first invocation, so cold_start: true)")
    print(handler(_api_gateway_v2_event("4242424242424242"), _LocalContext()), end="\n\n")

    print("→ declined (level: error, why/fix on the event, internal withheld)")
    print(handler(_api_gateway_v2_event("4000000000000002"), _LocalContext()), end="\n\n")

    print("→ 300ms of work with 600ms left on the clock: the timeout guard emits first")
    import time

    @lambda_wide_event
    def slow_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
        time.sleep(0.3)
        return {"statusCode": 200}

    slow_handler({}, _LocalContext(remaining_ms=600))
