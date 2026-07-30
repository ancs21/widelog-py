# widelog

[![PyPI](https://img.shields.io/pypi/v/widelog-py?color=black)](https://pypi.org/project/widelog-py/)
[![Python](https://img.shields.io/pypi/pyversions/widelog-py?color=black)](https://pypi.org/project/widelog-py/)
[![CI](https://img.shields.io/github/actions/workflow/status/ancs21/widelog-py/ci.yml?branch=main&color=black)](https://github.com/ancs21/widelog-py/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-black)](https://github.com/ancs21/widelog-py/blob/main/LICENSE)

widelog emits one structured event per operation instead of a line per step. Each event carries
the context you attached during the operation, how long it took, and errors that say why they
happened and what to do about them. It has no dependencies outside the standard library.

## Install

```bash
uv add widelog-py     # or: pip install widelog-py
```

Python 3.10 or later. The distribution is `widelog-py`, the import is `widelog`.

## Log a request

Add the middleware once at startup. Then call `use_logger()` anywhere in the request to attach
fields to that request's event.

```python
from widelog import WidelogMiddleware, init, use_logger

init(service="checkout")
app.add_middleware(WidelogMiddleware)


@app.post("/api/checkout")
async def checkout():
    log = use_logger()
    log.set(user={"id": user.id, "plan": "premium"})
    log.set(cart={"items": 3, "total": 9999})
    return {"ok": True}
```

One JSON line goes out when the response does:

```json
{
  "timestamp": "2026-07-30T10:23:45.612Z",
  "level": "info",
  "service": "checkout",
  "environment": "production",
  "duration_ms": 1204.7,
  "method": "POST",
  "path": "/api/checkout",
  "status": 200,
  "user": { "id": "u_123", "plan": "premium" },
  "cart": { "items": 3, "total": 9999 }
}
```

`log.set()` merges. Dictionaries merge key by key, so a later `log.set(user={"tier": "gold"})`
adds to `user` rather than replacing it. Lists concatenate. Anything else overwrites.

## Attach fields from anywhere in the call stack

`use_logger()` reads a `contextvars` slot, so a function several frames deep writes to the same
event without taking a logger argument.

```python
async def charge(cart):
    use_logger().set(payment={"method": "card"})
```

This is ambient context rather than dependency injection, and the choice is deliberate.
FastAPI's `Depends` resolves only in a handler signature, so injecting the logger would mean
passing `log` down through every helper that wants to add a field. If you want the dependency
visible in the signature anyway, wrap it. You get the same object.

```python
Log = Annotated[WideEvent, Depends(use_logger)]


@app.post("/api/checkout")
async def checkout(log: Log): ...
```

## Raise errors that explain themselves

`WidelogError` carries an HTTP status and a machine-readable code, plus two fields written for
whoever reads the log at 3am: `why` says what went wrong, `fix` says what to do next.

```python
raise WidelogError(
    "Payment failed",
    code="CARD_DECLINED",
    status=402,
    why="Issuer declined the charge",
    fix="Try a different payment method or contact your bank",
    internal={"processor_ref": "ch_live_x9"},
)
```

`to_dict()` returns everything except `internal`, so you can put the error straight on the
wire. FastAPI converts the exception into a response before the middleware sees it, so record
it in an exception handler:

```python
@app.exception_handler(WidelogError)
async def on_widelog_error(request, exc):
    use_logger().error(exc)
    return JSONResponse(status_code=exc.status, content=exc.to_dict())
```

Without that handler the client gets a 500 and the event has no `error` field.

## Declare your errors once

Once a service has more than a handful of errors, writing `why` and `fix` at each `raise` means
they drift. Declare them in a catalog instead. Each `ErrorSpec` becomes a factory whose code is
`prefix.ATTRIBUTE_NAME`, so the code cannot fall out of step with the name.

```python
from widelog import ErrorCatalog, ErrorSpec


class BillingErrors(ErrorCatalog, prefix="billing"):
    CART_EMPTY = ErrorSpec(status=400, message="Cart is empty")
    PAYMENT_DECLINED = ErrorSpec(
        status=402,
        message="Card declined",
        why="Issuer declined the charge",
        fix="Try a different payment method",
        link="https://docs.example.com/errors/payment-declined",
    )
    INSUFFICIENT_FUNDS = ErrorSpec(
        status=402,
        message=lambda available, required: f"Insufficient funds: ${available} of ${required}",
        fix="Add funds and retry",
    )
```

Raising one reads as the name of the thing that went wrong:

```python
if not cart.items:
    raise BillingErrors.CART_EMPTY()

raise BillingErrors.INSUFFICIENT_FUNDS(available=balance, required=cart.total, cause=exc)
```

A `message` that is a function turns its parameters into required keyword arguments, so a
templated error cannot be raised with a missing value. Any spec field can be overridden at the
call site, and `internal` merges with the call site winning:

```python
raise BillingErrors.PAYMENT_DECLINED(
    link="/support/payment-issues",  # overrides the spec
    internal={"processor_ref": "ch_x"},  # merged, stays server-side
    cause=stripe_error,
)
```

Branch on the code without repeating the string anywhere:

```python
except WidelogError as exc:
    if exc.code == BillingErrors.PAYMENT_DECLINED.code:
        ...

BillingErrors.CART_EMPTY.status   # 400
BillingErrors.codes()             # ('billing.CART_EMPTY', 'billing.PAYMENT_DECLINED', ...)
```

For a single error with no group to join, bind a spec to a code directly:

```python
from widelog import ErrorFactory

fraud_detected = ErrorFactory(
    "billing.FRAUD_DETECTED",
    ErrorSpec(status=403, message="Transaction flagged for review"),
)
```

Coming from evlog: `ErrorCatalog` is `defineErrorCatalog`, `ErrorFactory` is `defineError`. A
class body replaces the entry map because it is what gives Python editors autocomplete on
`BillingErrors.` and lets a type checker see the attributes. A dict would type as `Any`.

## Log work that is not a request

```python
from widelog import wide_event

with wide_event(job="nightly-reconcile") as log:
    log.set(rows=len(rows))
```

The event is emitted when the block exits, including when the block raises.

## Deploy to AWS Lambda

```python
from widelog import lambda_wide_event, use_logger


@lambda_wide_event
def handler(event, context):
    use_logger().set(order={"id": "o_1"})
    return {"statusCode": 200}
```

The decorator adds `request_id`, `cold_start`, `function`, `remaining_ms`, and the X-Ray
`trace_id`. It reads `method` and `path` from Function URL, API Gateway v1, and API Gateway v2
payloads.

500ms before the invocation deadline, widelog emits the event with `"timed_out": true`. Lambda
kills the process at the deadline, so otherwise a timed-out invocation leaves no record at all.
`emit()` is idempotent, so a normal return after the guard has fired does nothing.

The default sink writes to stdout, which Lambda forwards to CloudWatch Logs. You do not need a
drain or a `waitUntil` callback.

## Examples

```bash
uv run examples/aws_lambda_handler.py
uv run --with fastapi --with uvicorn uvicorn examples.fastapi_app:app
```

The Lambda example needs no AWS account. It prints three events: one authorized, one declined,
and one that hits the timeout guard. Both examples are covered by `tests/test_examples.py`, so
they cannot drift out of date without CI noticing.

## API

| | |
|---|---|
| `init(service=…, environment=…, redact=…, sink=…)` | Configure once at startup. |
| `use_logger()` | The current event. Outside an operation, returns a standalone logger that emits on each call. |
| `wide_event(**fields)` | Context manager that scopes one event. |
| `log.set(dict)` or `log.set(**kw)` | Attach context. Dictionaries merge, lists concatenate. |
| `log.info/warn/debug/error(…)` | Record a message. The most severe level wins. |
| `log.set_level(level)` | Pin the level so later `error()` and `warn()` calls cannot raise it. |
| `log.emit(**overrides)` | Emit and seal the event. Idempotent. |
| `WidelogError(msg, code=, status=, why=, fix=, link=, internal=)` | Error with a status and an explanation. |
| `ErrorSpec(message=, status=, why=, fix=, link=, tags=, internal=)` | One error declared once. `message` may be a function of required params. |
| `ErrorCatalog` (subclass with `prefix=`) | Turns each `ErrorSpec` in the body into a factory coded `prefix.NAME`. `codes()` lists them. |
| `ErrorFactory(code, spec)` | A spec bound to a code, for an error with no catalog. |
| `WidelogMiddleware` | ASGI. Covers FastAPI, Starlette, Litestar, and Django-async. |
| `lambda_wide_event` | Decorator for an AWS Lambda handler. |

`sink` takes a `callable(dict)`. Point it at your backend, or leave it unset to write NDJSON to
stdout.

### Tracebacks

An `error` on the event carries `stack`, the innermost frames as `path:line in function`, and
`causes`, the chain behind it. The chain follows `__cause__` before `__context__`, so
`raise X from Y` and `WidelogError(cause=Y)` win over an exception that happened to be in flight.
`raise X from None` ends the chain. widelog filters its own frames out, so the first entry is
always your code. `init(stack_depth=N)` changes how many frames are kept, five by default, and
none of it reaches `to_dict()`.

### Redaction

Keys ending in `password`, `token`, `secret`, `authorization`, `apikey`, or `cookie` are
replaced with `[REDACTED]` at any depth. Matching ignores case, underscores, and hyphens, and
looks at the end of the key, so `refresh_token`, `x-api-key`, `set-cookie`,
`proxy-authorization`, and `apiKey` all match. `tokens_used` does not, so metrics stay
readable. Pass `init(redact={…})` to replace the set with your own names.
`WidelogError(internal=…)` is never serialized into the event or into `to_dict()`.

### Limits

widelog copies every field as it enters the event, so it never writes back into the dicts and
lists you pass to `set()`. It keeps nesting to 32 levels and replaces anything deeper with
`[TRUNCATED]`, which also makes a self-referential payload safe to log.

### Sealed events

`emit()` seals the event. A `set()` after that prints a warning to stderr and drops the data, so
you can see the loss instead of wondering where a field went.

### Failure

`emit()` never raises. If your sink is down, or a field cannot be serialized, widelog reports
the dropped event on stderr and returns `None`. Logging is not allowed to fail the request it
is describing, or to replace the exception the application is already handling.

## Not implemented

Sampling, batched delivery to a backend, a pretty development terminal, audit hash-chaining, the
CLI, SQS and SNS and EventBridge batch triggers, and WSGI for Flask and Django-sync. Open an
issue if you need one of them.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
uv build
```

The docs site under `docs/` is an Astro project built with
[Nimbus](https://nimbus-docs.com). It is a separate toolchain from the package, which has no
Node dependency at all.

```bash
cd docs
bun install
bun run dev          # http://localhost:4321
bun run lint:docs    # frontmatter shape and internal links
bun run build
```

## License

MIT, An Pham. The wide-event design comes from [evlog](https://github.com/HugoRCD/evlog)
(MIT, HugoRCD). This is an independent Python implementation of it.
