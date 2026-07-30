"""FastAPI + widelog — one wide event per request.

    uv run --with fastapi --with uvicorn uvicorn examples.fastapi_app:app

    curl -sX POST localhost:8000/checkout -H 'content-type: application/json' \
      -d '{"user_id": "u_123", "card": "4242424242424242", "token": "idem_abc"}'
    curl -sX POST localhost:8000/checkout -H 'content-type: application/json' \
      -d '{"user_id": "u_123", "card": "4000000000000002", "token": "idem_abc"}'

The second card is declined: watch one `"level": "error"` line come out of the
server with why/fix and every field the request collected along the way.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from widelog import WidelogError, WidelogMiddleware, init, use_logger

init(service="checkout", environment="local")

app = FastAPI()
app.add_middleware(WidelogMiddleware)


class CheckoutBody(BaseModel):
    user_id: str
    card: str
    token: str


@app.exception_handler(WidelogError)
async def on_widelog_error(request: Request, exc: WidelogError) -> JSONResponse:
    """Put why/fix on the wire and in the wide event.

    FastAPI handles the exception before it reaches WidelogMiddleware, so the
    middleware only sees the response status — record the error here, or lose it.
    `internal=` is not in `to_dict()`, so it never leaves the server.
    """
    use_logger().error(exc)
    return JSONResponse(status_code=exc.status, content=exc.to_dict())


async def load_cart(user_id: str) -> dict:
    return {"items": 3, "total": 9999}


async def charge(card: str, token: str) -> str:
    # No logger was passed in. It still lands in this request's wide event.
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


@app.post("/checkout")
async def checkout(body: CheckoutBody) -> dict:
    log = use_logger()
    log.set(user={"id": body.user_id})
    log.set(cart=await load_cart(body.user_id))

    if not (await load_cart(body.user_id))["items"]:
        log.warn("empty cart")

    order_id = await charge(body.card, body.token)
    log.set(order={"id": order_id})
    return {"order_id": order_id}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
