# Changelog

All notable changes to widelog are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0]

First release.

### Added

- `wide_event()` and `use_logger()`: one event per operation, with fields merged from anywhere in
  the call stack through a `contextvars` slot. Dicts merge key by key, lists concatenate.
- `WidelogError`, carrying an HTTP status, a machine-readable code, and `why` and `fix` fields
  for whoever reads the log later. `internal` stays out of the event and out of `to_dict()`.
- `ErrorSpec`, `ErrorCatalog`, and `ErrorFactory` for declaring errors once and deriving each
  code from its attribute name. A `message` that is a function makes its parameters required at
  the call site.
- `WidelogMiddleware`, an ASGI adapter covering FastAPI, Starlette, Litestar, and Django-async.
- `lambda_wide_event`, an AWS Lambda decorator that records `cold_start`, `request_id`,
  `function`, `remaining_ms`, and the X-Ray `trace_id`, reads `method` and `path` from all three
  payload shapes, and emits early with `"timed_out": true` 500ms before the deadline so a killed
  invocation still leaves a record.
- Redaction of key names ending in `password`, `token`, `secret`, `authorization`, `apikey`, or
  `cookie`, at any depth, ignoring case, underscores, and hyphens.
- A copy-on-write field merge, so widelog never mutates the dicts and lists you pass it, with
  nesting capped at 32 levels.
- Tracebacks on the event: `error.stack` holds the innermost frames with widelog's own filtered
  out, and `error.causes` holds the chain behind the error, following `__cause__` before
  `__context__` and stopping at `raise ... from None`. `init(stack_depth=N)` sets how many frames
  to keep. Neither reaches `to_dict()`.

### Notes

- `emit()` never raises. A failing sink or an unserializable field is reported on stderr and
  dropped, rather than failing the request or replacing the exception already in flight.
- Requires Python 3.10 or later, and nothing outside the standard library.
