"""Two services, one request, one trace.

    uv run --with fastapi --with httpx examples/microservices.py

Each service emits its own wide event. They share a `trace_id`, so one query
against your backend returns the whole path a request took:

    trace_id = "t_a1b2c3" | sort by timestamp

The gateway also records what the call downstream cost it and what came back,
so a slow request is attributable to a service without opening a second tool.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from widelog import WidelogError, WidelogMiddleware, init, use_logger

TRACE_HEADER = "x-trace-id"

init(service="gateway")


class ServiceContext:
    """Names the service and joins its event to the caller's trace.

    In production each service is its own process and calls `init(service=...)`
    once at startup, leaving this middleware with only the trace id to do. Both
    services share a process here so the example runs in one command, and a
    `service` field overrides the configured name because fields are merged last.

    Added before WidelogMiddleware, so it runs inside the event rather than
    around it: Starlette makes the last middleware added the outermost one.
    """

    def __init__(self, app: Any, service: str) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            # The edge mints the trace when the caller did not supply one.
            trace_id = headers.get(TRACE_HEADER.encode(), b"").decode() or f"t_{uuid.uuid4().hex[:12]}"
            use_logger().set(service=self.service, trace_id=trace_id)
            scope.setdefault("state", {})["trace_id"] = trace_id
        await self.app(scope, receive, send)


def build(service: str) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ServiceContext, service=service)
    app.add_middleware(WidelogMiddleware)

    @app.exception_handler(WidelogError)
    async def on_widelog_error(request: Request, exc: WidelogError) -> JSONResponse:
        use_logger().error(exc)
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    return app


# --- orders: the downstream service ------------------------------------------

orders = build("orders")


@orders.post("/orders")
async def create_order(request: Request) -> dict:
    body = await request.json()
    log = use_logger()
    log.set(user={"id": body["user_id"]}, sku=body["sku"])

    if body["sku"] == "SOLD-OUT":
        raise WidelogError(
            "Out of stock",
            code="OUT_OF_STOCK",
            status=409,
            why="The last unit was sold while the cart was open",
            fix="Offer the customer a backorder or a substitute",
            internal={"warehouse": "syd-3"},  # stays inside this service
        )

    log.set(order={"id": "o_1", "total": 9999})
    return {"order_id": "o_1"}


# --- gateway: the edge --------------------------------------------------------

gateway = build("gateway")


@gateway.post("/checkout")
async def checkout(request: Request) -> dict:
    body = await request.json()
    log = use_logger()
    log.set(user={"id": body["user_id"]})

    # Forwarding the header is what stitches the two events together. Without it
    # the downstream service mints its own trace and the request looks like two.
    headers = {TRACE_HEADER: request.state.trace_id}
    started = time.perf_counter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=orders),  # a real deployment uses base_url
        base_url="http://orders",
    ) as client:
        response = await client.post("/orders", json=body, headers=headers)

    # What the call cost and what it returned, on the caller's event. A slow
    # checkout is now attributable without opening a second tool.
    log.set(
        downstream={
            "service": "orders",
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    )

    if response.is_error:
        # The downstream explained itself. Pass that on rather than inventing a
        # worse message, and let the exception handler record it.
        downstream = response.json()
        raise WidelogError(
            downstream["message"],
            code=downstream.get("code"),
            status=response.status_code,
            why=downstream.get("why"),
            fix=downstream.get("fix"),
        )

    return response.json()


async def _post(sku: str) -> tuple[int, dict]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post("/checkout", json={"user_id": "u_123", "sku": sku})
        return response.status_code, response.json()


if __name__ == "__main__":
    import json

    captured: list[dict] = []
    init(sink=captured.append)

    for sku in ("WIDGET-1", "SOLD-OUT"):
        status, body = asyncio.run(_post(sku))
        print(f"\n=== POST /checkout {sku!r} -> {status} ===")
        for event in captured:
            print(json.dumps(event))
        # Both events carry the same trace, so a backend joins them on one field.
        traces = {event["trace_id"] for event in captured}
        print(f"services: {[e['service'] for e in captured]}  traces: {traces}")
        captured.clear()
