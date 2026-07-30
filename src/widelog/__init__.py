"""widelog: one wide event per operation, instead of a line per step.

Wide-event logging for Python, inspired by evlog (https://evlog.dev).
`contextvars` replaces AsyncLocalStorage; one ASGI adapter replaces evlog's
per-framework integrations.
"""

from __future__ import annotations

import contextvars
import functools
import json
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

__all__ = [
    "REDACTED",
    "TRUNCATED",
    "ErrorCatalog",
    "ErrorFactory",
    "ErrorSpec",
    "WideEvent",
    "WidelogError",
    "WidelogMiddleware",
    "init",
    "lambda_wide_event",
    "use_logger",
    "wide_event",
]

__version__ = "0.1.0"

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
# Deep enough for real payloads, shallow enough that a hostile one cannot exhaust the stack.
_MAX_DEPTH = 32
# Deep enough to show the path into the failure without burying the event.
_DEFAULT_STACK_DEPTH = 5
_MAX_CAUSES = 5
_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}

_current: contextvars.ContextVar[WideEvent | None] = contextvars.ContextVar("widelog_event", default=None)

_config: dict[str, Any] = {
    "service": os.getenv("SERVICE_NAME", "app"),
    "environment": os.getenv("ENVIRONMENT", "development"),
    # Normalized by init(), so emit() never has to re-normalize per event.
    "redact": ("password", "token", "secret", "authorization", "apikey", "cookie"),
    "sink": None,  # callable(dict) -> None; default is NDJSON on stdout
    "stack_depth": _DEFAULT_STACK_DEPTH,
}


def init(**config: Any) -> None:
    """Configure once at startup: service, environment, redact, sink."""
    if "redact" in config:
        config["redact"] = tuple(_norm(key) for key in config["redact"])
    _config.update(config)


class WidelogError(Exception):
    """An error that explains itself: why it happened and how to fix it."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int = 500,
        why: str | None = None,
        fix: str | None = None,
        link: str | None = None,
        cause: BaseException | None = None,
        internal: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.why = why
        self.fix = fix
        self.link = link
        self.internal = internal  # backend-only, never serialized
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"message": self.message, "status": self.status}
        for key in ("code", "why", "fix", "link"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


# Everything a call site may override. What is left over is a message parameter.
_OVERRIDES = frozenset({"message", "status", "why", "fix", "link", "internal", "cause"})


@dataclass(frozen=True)
class ErrorSpec:
    """One error, declared once: the parts that are the same at every call site.

    `message` is either a constant or a function whose parameters become required
    keyword arguments when the error is raised.
    """

    message: str | Callable[..., str]
    status: int = 500
    why: str | None = None
    fix: str | None = None
    link: str | None = None
    tags: tuple[str, ...] = ()
    internal: dict[str, Any] | None = None


@dataclass(frozen=True)
class ErrorFactory:
    """An ErrorSpec bound to a stable code. Call it to build the WidelogError.

    ```python
    fraud_detected = ErrorFactory("billing.FRAUD", ErrorSpec(status=403, message="Flagged"))
    raise fraud_detected()
    ```
    """

    code: str
    spec: ErrorSpec

    def __getattr__(self, name: str) -> Any:
        # Read spec fields off the factory: PAYMENT_DECLINED.status, .fix, .tags
        if name.startswith("_") or name == "spec":
            raise AttributeError(name)
        return getattr(self.spec, name)

    def _render(self, params: dict[str, Any]) -> str:
        if callable(self.spec.message):
            return self.spec.message(**params)
        if params:
            raise TypeError(f"{self.code} takes no message params, got {sorted(params)}")
        return self.spec.message

    def __call__(self, **kwargs: Any) -> WidelogError:
        overrides = {key: kwargs.pop(key) for key in list(kwargs) if key in _OVERRIDES}
        spec = self.spec
        internal = {**(spec.internal or {}), **(overrides.get("internal") or {})}
        return WidelogError(
            overrides.get("message") or self._render(kwargs),
            code=self.code,
            status=overrides.get("status", spec.status),
            why=overrides.get("why", spec.why),
            fix=overrides.get("fix", spec.fix),
            link=overrides.get("link", spec.link),
            cause=overrides.get("cause"),
            internal=internal or None,
        )


class ErrorCatalog:
    """A namespace of related errors that share one code prefix.

    Each ErrorSpec in the body becomes a factory whose code is
    `prefix.ATTRIBUTE_NAME`, so the code cannot drift from the name.

    ```python
    class BillingErrors(ErrorCatalog, prefix="billing"):
        CART_EMPTY = ErrorSpec(status=400, message="Cart is empty")

    raise BillingErrors.CART_EMPTY()          # code: billing.CART_EMPTY
    ```
    """

    _prefix: ClassVar[str]

    def __init_subclass__(cls, prefix: str, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not prefix:
            raise ValueError("an error catalog needs a prefix, so its codes stay unambiguous")
        cls._prefix = prefix
        for name, value in list(vars(cls).items()):
            if isinstance(value, ErrorSpec):
                setattr(cls, name, ErrorFactory(f"{prefix}.{name}", value))

    @classmethod
    def codes(cls) -> tuple[str, ...]:
        """Every code in this catalog, in declaration order."""
        return tuple(v.code for v in vars(cls).values() if isinstance(v, ErrorFactory))


def _norm(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def _copy(value: Any, depth: int) -> Any:
    """Snapshot the caller's data, so a later merge never writes back into it."""
    if depth >= _MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        return {k: _copy(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v, depth + 1) for v in value]
    return value


def _merge(target: dict[str, Any], source: dict[str, Any], depth: int = 0) -> None:
    """Recursive merge: dicts deep-merge, lists concat, everything else replaces.

    `target` is always owned by widelog and `source` always belongs to the caller,
    so every value crossing over is copied. The depth cap bounds both this and the
    later redaction walk, so a hostile payload cannot exhaust the stack.
    """
    for key, value in source.items():
        if value is None:
            continue
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, dict) and depth < _MAX_DEPTH:
            _merge(current, value, depth + 1)
        elif isinstance(value, list) and isinstance(current, list):
            current.extend(_copy(item, depth + 1) for item in value)
        else:
            target[key] = _copy(value, depth)


def _is_secret(key: str, needles: tuple[str, ...]) -> bool:
    """Match on the end of the key, so `refresh_token` and `x-api-key` are caught.

    A substring match would also hit `tokens_used`, which is an ordinary metric.
    """
    normalized = _norm(key)
    return any(normalized.endswith(needle) for needle in needles)


def _redact(value: Any, needles: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret(key, needles) else _redact(item, needles)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, needles) for item in value]
    return value


def _frames(tb: Any) -> list[str]:
    """The innermost frames, minus widelog's own, which are never the bug."""
    frames = [f for f in traceback.extract_tb(tb) if f.filename != __file__]
    depth = _config["stack_depth"]
    return [f"{f.filename}:{f.lineno} in {f.name}" for f in frames[-depth:]]


def _next_in_chain(error: BaseException) -> BaseException | None:
    """`raise X from Y` beats an incidental context, and `from None` ends the chain."""
    if error.__cause__ is not None:
        return error.__cause__
    return None if error.__suppress_context__ else error.__context__


def _chain(error: BaseException) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    visited = {id(error)}
    current = _next_in_chain(error)
    while current is not None and id(current) not in visited and len(causes) < _MAX_CAUSES:
        visited.add(id(current))
        entry: dict[str, Any] = {"type": type(current).__name__, "message": str(current)}
        frames = _frames(current.__traceback__)
        if frames:
            entry["stack"] = frames
        causes.append(entry)
        current = _next_in_chain(current)
    return causes


def _describe(error: BaseException | str) -> dict[str, Any]:
    if isinstance(error, str):
        return {"message": error}
    out = error.to_dict() if isinstance(error, WidelogError) else {"message": str(error)}
    out["type"] = type(error).__name__
    frames = _frames(error.__traceback__)
    if frames:
        out["stack"] = frames
    causes = _chain(error)
    if causes:
        out["causes"] = causes
    return out


class WideEvent:
    """Accumulates every field for one operation, emits exactly one log line."""

    def __init__(self, fields: dict[str, Any] | None = None, *, _immediate: bool = False) -> None:
        self.fields: dict[str, Any] = {}
        _merge(self.fields, fields or {})
        self.level = "info"
        self._explicit_level: str | None = None
        self._started = time.perf_counter()
        self._emitted = False
        self._immediate = _immediate

    def _mutate(self, data: dict[str, Any], level: str | None = None) -> None:
        if self._emitted:
            print(
                f"[widelog] mutation after emit, dropped: {sorted(data)}. "
                "Wrap background work in its own wide_event().",
                file=sys.stderr,
            )
            return
        _merge(self.fields, data)
        if level and _RANK[level] > _RANK[self.level]:
            self.level = level
        if self._immediate:
            self.emit()
            self._reset()

    def _reset(self) -> None:
        """Immediate mode reuses one object per call site, so clear the last event."""
        self.fields = {}
        self.level = "info"
        self._explicit_level = None
        self._emitted = False

    def set(self, context: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Add context to the wide event. Dicts deep-merge, lists concat."""
        self._mutate({**(context or {}), **kwargs})

    def set_level(self, level: str) -> None:
        """Pin the level explicitly; later error()/warn() calls won't override it."""
        self._explicit_level = level

    def error(self, error: BaseException | str, context: dict[str, Any] | None = None) -> None:
        self._mutate({"error": _describe(error), **(context or {})}, "error")

    def warn(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._mutate({"messages": [{"level": "warn", "message": message}], **(context or {})}, "warn")

    def info(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._mutate({"messages": [{"level": "info", "message": message}], **(context or {})}, "info")

    def debug(self, message: str, context: dict[str, Any] | None = None) -> None:
        self._mutate({"messages": [{"level": "debug", "message": message}], **(context or {})}, "debug")

    def _build(self, overrides: dict[str, Any]) -> dict[str, Any]:
        _merge(self.fields, overrides)
        # An immediate one-off line measures no operation, so it carries no duration.
        elapsed = (
            {} if self._immediate else {"duration_ms": round((time.perf_counter() - self._started) * 1000, 2)}
        )
        return {
            # Millisecond precision, because backends sort on this field.
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": self._explicit_level or self.level,
            "service": _config["service"],
            "environment": _config["environment"],
            **elapsed,
            **_redact(self.fields, _config["redact"]),
        }

    def emit(self, **overrides: Any) -> dict[str, Any] | None:
        """Emit the wide event and seal the logger. Never raises."""
        # ponytail: unlocked flag check, so a Lambda timeout racing a normal return can
        # duplicate one line. Cheaper than taking a lock on every emit.
        if self._emitted:
            return None
        self._emitted = True
        try:
            event = self._build(overrides)
            sink = _config["sink"]
            if sink:
                sink(event)
            else:
                print(json.dumps(event, default=str), file=sys.stdout, flush=True)
            return event
        except Exception as exc:
            # A broken sink must never fail the caller's request or replace its exception.
            print(f"[widelog] dropped the event: {exc!r}", file=sys.stderr)
            return None


def _record_status(log: WideEvent, status: int) -> None:
    """A 5xx is the one status that makes the whole event an error."""
    log.set(status=status)
    if status >= 500:
        log.set_level("error")


def use_logger() -> WideEvent:
    """The wide event for the current operation.

    Outside one, returns a standalone logger that emits on every call, so logs
    are never silently dropped.
    """
    return _current.get() or WideEvent(_immediate=True)


@contextmanager
def wide_event(**fields: Any) -> Iterator[WideEvent]:
    """Scope one wide event. Captures exceptions, always emits exactly once."""
    event = WideEvent(fields)
    token = _current.set(event)
    try:
        yield event
    except BaseException as exc:
        event.error(exc)
        if isinstance(exc, WidelogError):
            event.set(status=exc.status)
        raise
    finally:
        event.emit()
        _current.reset(token)


class WidelogMiddleware:
    """ASGI: one wide event per request. FastAPI, Starlette, Litestar, Django-async."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        with wide_event(method=scope.get("method"), path=scope.get("path")) as log:

            async def send_wrapper(message: dict) -> None:
                if message["type"] == "http.response.start":
                    _record_status(log, message["status"])
                await send(message)

            await self.app(scope, receive, send_wrapper)


_cold_start = True


def _as_dict(value: Any) -> dict[str, Any]:
    """Lambda payloads are whatever the trigger sent, so never assume a shape."""
    return value if isinstance(value, dict) else {}


def _lambda_fields(event: Any, context: Any) -> dict[str, Any]:
    # getattr tolerates context=None, so local invocations need no special case
    fn = {
        "name": getattr(context, "function_name", None),
        "version": getattr(context, "function_version", None),
        "memory_mb": getattr(context, "memory_limit_in_mb", None),
    }
    payload = _as_dict(event)
    http = _as_dict(_as_dict(payload.get("requestContext")).get("http"))

    fields = {
        "request_id": getattr(context, "aws_request_id", None),
        # X-Ray trace id, so the event joins the trace instead of floating beside it
        "trace_id": os.getenv("_X_AMZN_TRACE_ID"),
        "function": {k: v for k, v in fn.items() if v is not None} or None,
        # Function URL / API Gateway v2 first, then v1 / ALB
        "method": http.get("method") or payload.get("httpMethod"),
        "path": http.get("path") or payload.get("rawPath") or payload.get("path"),
    }
    return {k: v for k, v in fields.items() if v is not None}


@contextmanager
def _timeout_guard(log: WideEvent, remaining: Callable[[], int] | None) -> Iterator[None]:
    """Emit before Lambda kills the process at the deadline, or the event is lost."""
    budget = remaining() / 1000 - 0.5 if remaining else 0

    def emit_before_timeout() -> None:
        log.set_level("error")
        log.emit(timed_out=True)

    timer = threading.Timer(budget, emit_before_timeout) if budget > 0 else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        yield
    finally:
        if timer:
            timer.cancel()


def lambda_wide_event(handler: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    """One wide event per Lambda invocation.

    stdout is already CloudWatch Logs, so the default sink needs no drain. Emits
    early if the invocation is about to hit its timeout, otherwise the process is
    killed and the event is lost.

    ```python
    @lambda_wide_event
    def handler(event, context):
        use_logger().set(order={"id": "o_1"})
        return {"statusCode": 200}
    ```
    """

    @functools.wraps(handler)
    def wrapper(event: Any, context: Any = None) -> Any:
        global _cold_start
        fields = _lambda_fields(event, context)
        if _cold_start:
            fields["cold_start"] = True
            _cold_start = False

        remaining = getattr(context, "get_remaining_time_in_millis", None)
        with wide_event(**fields) as log, _timeout_guard(log, remaining):
            result = handler(event, context)
            status = _as_dict(result).get("statusCode")
            if isinstance(status, int):
                _record_status(log, status)
            if remaining:
                log.set(remaining_ms=remaining())
            return result

    return wrapper
