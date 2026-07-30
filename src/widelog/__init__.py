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
_LEVELS = ("debug", "info", "warn", "error")

_current: contextvars.ContextVar[WideEvent | None] = contextvars.ContextVar("widelog_event", default=None)

_config: dict[str, Any] = {
    "service": os.getenv("SERVICE_NAME", "app"),
    "environment": os.getenv("ENVIRONMENT", "development"),
    "redact": {"password", "token", "secret", "authorization", "apikey", "cookie"},
    "sink": None,  # callable(dict) -> None; default is NDJSON on stdout
}


def init(**config: Any) -> None:
    """Configure once at startup: service, environment, redact, sink."""
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
        if level and _LEVELS.index(level) > _LEVELS.index(self.level):
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

    def emit(self, **overrides: Any) -> dict[str, Any] | None:
        """Emit the wide event. Seals the logger."""
        # ponytail: unlocked flag check, so a Lambda timeout racing a normal return can
        # duplicate one line. Cheaper than taking a lock on every emit.
        if self._emitted:
            return None
        self._emitted = True
        _merge(self.fields, overrides)
        event = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "level": self._explicit_level or self.level,
            "service": _config["service"],
            "environment": _config["environment"],
            "duration_ms": round((time.perf_counter() - self._started) * 1000, 2),
            **_redact(self.fields, {_norm(k) for k in _config["redact"]}),
        }
        sink = _config["sink"]
        if sink:
            sink(event)
        else:
            print(json.dumps(event, default=str), file=sys.stdout, flush=True)
        return event


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
                    log.set(status=message["status"])
                    if message["status"] >= 500:
                        log.set_level("error")
                await send(message)

            await self.app(scope, receive, send_wrapper)


_cold_start = True


def _lambda_fields(event: Any, context: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}

    if context is not None:
        fields["request_id"] = getattr(context, "aws_request_id", None)
        fn = {
            "name": getattr(context, "function_name", None),
            "version": getattr(context, "function_version", None),
            "memory_mb": getattr(context, "memory_limit_in_mb", None),
        }
        fields["function"] = {k: v for k, v in fn.items() if v is not None} or None

    # X-Ray trace id, so the wide event joins the trace instead of floating beside it
    fields["trace_id"] = os.getenv("_X_AMZN_TRACE_ID")

    if isinstance(event, dict):
        request_context = event.get("requestContext")
        http = request_context.get("http", {}) if isinstance(request_context, dict) else {}
        http = http if isinstance(http, dict) else {}
        # Function URL / API Gateway v2, then v1 / ALB
        fields["method"] = http.get("method") or event.get("httpMethod")
        fields["path"] = http.get("path") or event.get("rawPath") or event.get("path")

    return {k: v for k, v in fields.items() if v is not None}


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

        with wide_event(**fields) as log:

            def emit_before_timeout() -> None:
                log.set_level("error")
                log.emit(timed_out=True)

            timer = None
            remaining = getattr(context, "get_remaining_time_in_millis", None)
            if remaining:
                budget = remaining() / 1000 - 0.5
                if budget > 0:
                    timer = threading.Timer(budget, emit_before_timeout)
                    timer.daemon = True
                    timer.start()
            try:
                result = handler(event, context)
                if isinstance(result, dict) and isinstance(result.get("statusCode"), int):
                    log.set(status=result["statusCode"])
                    if result["statusCode"] >= 500:
                        log.set_level("error")
                if remaining:
                    log.set(remaining_ms=remaining())
                return result
            finally:
                if timer:
                    timer.cancel()

    return wrapper
