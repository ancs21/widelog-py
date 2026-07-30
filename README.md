# widelog

widelog emits one structured event per operation instead of a line per step. Each event carries
the context you attached during the operation, how long it took, and errors that say why they
happened and what to do about them. It has no dependencies outside the standard library.

Python implementation of the wide-event approach from [evlog](https://evlog.dev). `contextvars`
takes the place of `AsyncLocalStorage`, and one ASGI adapter covers what evlog handles with
fifteen framework integrations.

## Install

```bash
uv add widelog-py
```

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
  "timestamp": "2026-07-30T10:23:45Z",
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

`tests/test_examples.py` exercises both of these, so they stay working.

```bash
uv run examples/aws_lambda_handler.py
uv run --with fastapi --with uvicorn uvicorn examples.fastapi_app:app
```

The Lambda example needs no AWS account and prints three events: one authorized, one declined,
one that hits the timeout guard.

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
| `WidelogMiddleware` | ASGI. Covers FastAPI, Starlette, Litestar, and Django-async. |
| `lambda_wide_event` | Decorator for an AWS Lambda handler. |

`sink` takes a `callable(dict)`. Point it at your backend, or leave it unset to write NDJSON to
stdout.

### Redaction

Keys ending in `password`, `token`, `secret`, `authorization`, `apikey`, or `cookie` are
replaced with `[REDACTED]` at any depth. Matching ignores case, underscores, and hyphens, and
looks at the end of the key, so `refresh_token`, `x-api-key`, `set-cookie`,
`proxy-authorization`, and `apiKey` all match. `tokens_used` does not, so metrics stay
readable. Pass `init(redact={…})` to replace the set with your own names.
`WidelogError(internal=…)` is never serialized into the event or into `to_dict()`.

### Limits

Fields are copied as they enter the event, so widelog never writes back into the dicts and
lists you pass to `set()`. Nesting is kept to 32 levels and anything deeper becomes
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

## License

MIT, An Pham. The wide-event design comes from [evlog](https://github.com/HugoRCD/evlog)
(MIT, HugoRCD). This is an independent Python implementation of it.
