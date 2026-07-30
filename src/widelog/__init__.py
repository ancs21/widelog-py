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
from typing import Any

__all__ = [
    "REDACTED",
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
_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}

_current: contextvars.ContextVar[WideEvent | None] = contextvars.ContextVar("widelog_event", default=None)

_config: dict[str, Any] = {
    "service": os.getenv("SERVICE_NAME", "app"),
    "environment": os.getenv("ENVIRONMENT", "development"),
    # Normalized by init(), so emit() never has to re-normalize per event.
    "redact": ("password", "token", "secret", "authorization", "apikey", "cookie"),
    "sink": None,  # callable(dict) -> None; default is NDJSON on stdout
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


def _norm(key: str) -> str:
    return key.lower().replace("_", "").replace("-", "")


def _merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Recursive merge: dicts deep-merge, lists concat, everything else replaces."""
    for key, value in source.items():
        if value is None:
            continue
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge(current, value)
        elif isinstance(value, list) and isinstance(current, list):
            current.extend(value)
        else:
            target[key] = value


def _redact(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if _norm(k) in keys else _redact(v, keys) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, keys) for v in value]
    return value


def _describe(error: BaseException | str) -> dict[str, Any]:
    if isinstance(error, str):
        return {"message": error}
    out = error.to_dict() if isinstance(error, WidelogError) else {"message": str(error)}
    out["type"] = type(error).__name__
    frames = traceback.extract_tb(error.__traceback__)[-2:]
    if frames:
        out["stack"] = [f"{f.filename}:{f.lineno} in {f.name}" for f in frames]
    return out


class WideEvent:
    """Accumulates every field for one operation, emits exactly one log line."""

    def __init__(self, _immediate: bool = False, **fields: Any) -> None:
        self.fields: dict[str, Any] = {k: v for k, v in fields.items() if v is not None}
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
            self.fields, self._emitted, self._explicit_level = {}, False, None

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
        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "level": self._explicit_level or self.level,
            "service": _config["service"],
            "environment": _config["environment"],
            "duration_ms": round((time.perf_counter() - self._started) * 1000, 2),
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
    event = WideEvent(**fields)
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
