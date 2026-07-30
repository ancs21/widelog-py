# widelog

**Digging through logs is not observability. It's hope.**

One wide event per operation, with all the context, and errors that explain *why* and what to
do next. Pure stdlib, zero dependencies.

Python port of the idea behind [evlog](https://evlog.dev) by
[HugoRCD](https://github.com/HugoRCD) — `contextvars` in place of `AsyncLocalStorage`, one
ASGI adapter in place of evlog's fifteen framework integrations.

```bash
pip install widelog-py
```

## The problem

```python
print("Request received")
print(f"User: {user.id}")
print("Cart loaded")
print("Payment failed")  # good luck finding this at 3am
raise Exception("Something went wrong")
```

## The solution

```python
from widelog import WidelogMiddleware, WidelogError, use_logger, init

init(service="checkout")
app.add_middleware(WidelogMiddleware)


@app.post("/api/checkout")
async def checkout():
    log = use_logger()
    log.set(user={"id": user.id, "plan": "premium"})
    log.set(cart={"items": 3, "total": 9999})

    if not paid:
        raise WidelogError(
            "Payment failed",
            status=402,
            why="Card declined by issuer",
            fix="Try a different payment method or contact your bank",
        )
```

One event on the way out, with everything in it:

```json
{
  "timestamp": "2026-07-30T10:23:45Z",
  "level": "error",
  "service": "checkout",
  "environment": "production",
  "duration_ms": 1204.7,
  "method": "POST",
  "path": "/api/checkout",
  "status": 402,
  "user": { "id": "u_123", "plan": "premium" },
  "cart": { "items": 3, "total": 9999 },
  "error": {
    "message": "Payment failed",
    "status": 402,
    "why": "Card declined by issuer",
    "fix": "Try a different payment method or contact your bank",
    "type": "WidelogError"
  }
}
```

## No logger to pass around

`use_logger()` reads a `contextvars` slot, so any function in the call stack writes into the
same event — no parameter threading, no DI container:

```python
async def charge(cart):  # nobody handed it a logger
    use_logger().set(payment={"method": "card"})
```

That's ambient context, not dependency injection. It's deliberate: a logger is cross-cutting,
and FastAPI's `Depends` only resolves in a handler signature, so DI would mean threading `log`
through every helper that wants to add a field. If you want it visible in the signature anyway,
wrap it — same object either way:

```python
Log = Annotated[WideEvent, Depends(use_logger)]


@app.post("/api/checkout")
async def checkout(log: Log): ...
```

## Anything that isn't a request

```python
from widelog import wide_event

with wide_event(job="nightly-reconcile") as log:
    log.set(rows=len(rows))
    # emits once on the way out, including if the block raises
```

## AWS Lambda

```python
from widelog import lambda_wide_event, use_logger


@lambda_wide_event
def handler(event, context):
    use_logger().set(order={"id": "o_1"})
    return {"statusCode": 200}
```

Adds `request_id`, `cold_start`, `function`, `remaining_ms`, X-Ray `trace_id`, plus `method`
and `path` from Function URL / API Gateway v1 and v2 payloads.

**Timeout guard.** When an invocation is 500ms from its deadline, the event is emitted early
with `"timed_out": true`. Without that, Lambda kills the process and you lose the event for
the one invocation you actually needed. `emit()` is idempotent, so a normal return afterwards
is a no-op.

No drain or `waitUntil` needed — on Lambda, stdout *is* CloudWatch Logs.

## API

| | |
|---|---|
| `init(service=…, environment=…, redact=…, sink=…)` | configure once at startup |
| `use_logger()` | the current event; standalone (emits per call) if there's none |
| `wide_event(**fields)` | context manager scoping one event |
| `log.set(dict)` / `log.set(**kw)` | add context — dicts deep-merge, lists concat |
| `log.info/warn/debug/error(…)` | record a message; worst level wins |
| `log.set_level(level)` | pin the level, ignoring later `error()`/`warn()` |
| `log.emit(**overrides)` | emit and seal — idempotent |
| `WidelogError(msg, code=, status=, why=, fix=, link=, internal=)` | self-explaining error |
| `WidelogMiddleware` | ASGI: FastAPI, Starlette, Litestar, Django-async |
| `lambda_wide_event` | AWS Lambda handler decorator |

`sink` takes a `callable(dict)` — point it at your backend. Default writes NDJSON to stdout.

Keys named `password`, `token`, `secret`, `authorization`, `apikey`, or `cookie` are replaced
with `[REDACTED]` at any depth, case- and separator-insensitively (`api_key`, `API-KEY`,
`apiKey`). Override with `init(redact={…})`. `WidelogError(internal=…)` never reaches the event
at all.

Mutating a sealed event warns on stderr and drops the data, rather than losing it silently.

## Not ported (yet)

Sampling, batched drain to a backend, pretty dev terminal, audit hash-chaining, the CLI,
SQS/SNS/EventBridge batch triggers, WSGI (Flask, Django-sync). Open an issue if you want one.

## License

MIT © An Pham. The wide-event design is from [evlog](https://github.com/HugoRCD/evlog)
(MIT © HugoRCD); this is an independent Python implementation of it.
