# Changelog

All notable changes to widelog are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions are
[CalVer](https://calver.org/): `YYYY.M.MICRO`, where MICRO counts releases
within the month. A version says when it shipped, not what it promises about
compatibility, so read the notes below before upgrading.

## [2026.7.3]

### Added

- `widelog.otlp`, which exports wide events to any OTLP collector over HTTP with JSON, using
  nothing outside the standard library. `init(sink=OTLPSink(endpoint=...))` is the whole setup.
  A separate import, so nothing changes for a project writing NDJSON to stdout.
  - One event becomes one log record. `service`, `environment`, `version` and `region` become
    resource attributes under their OpenTelemetry names, `level` becomes a severity number,
    nested fields flatten to dotted keys, and the error message or the last message becomes the
    body. `trace_id` is promoted to the record's trace id only when it really is 32 hex
    characters, so an X-Ray header stays an attribute rather than being rejected.
  - Sending is on a background thread and batches whatever has queued. A collector that is slow
    or down costs events, never latency: the queue is bounded, drops are counted on
    `OTLPSink.dropped`, and nothing raises into the request being described.
  - Verified against `otel/opentelemetry-collector-contrib`, not only against its own tests.
- `examples/microservices.py`: a gateway and a downstream service, each emitting its own event,
  joined by one forwarded header. The gateway records what the call cost and what came back.

## [2026.7.2]

### Fixed

- A dict keyed by anything other than a string no longer costs you the event. Redaction asked
  every key whether it ended in a secret name, which meant calling `.lower()` on it, so
  `log.set(status_counts={200: 981, 404: 12})` raised inside `emit()`, and `emit()` dropped the
  whole event rather than fail the request describing it. One integer key took the operation's
  entire record with it. JSON allows `str`, `int`, `float`, `bool` and `None` keys, and now so
  does widelog. Keys that JSON cannot represent at all, such as a tuple, still drop the event on
  stderr as before.

### Changed

- The author email matches the address the commits and project URLs use.

## [2026.7.1]

### Changed

- Redaction caches whether a key name is a secret, instead of re-deriving it on every event. A
  field name's secret-ness never changes, but working it out meant lowercasing the key, stripping
  separators, and testing six suffixes, for every field, every time: 61% of the cost of an event.
  The redact set is part of the cache key, so changing it through `init()` still takes effect.
- Stack extraction no longer reads each frame's source line off disk. Only the file, line number
  and function name are recorded, so those lines were loaded and thrown away.

Measured with `benchmarks/bench.py`, µs per event:

| | 2026.7.0 | 2026.7.1 |
| --- | --- | --- |
| event with 8 fields | 9.70 | 6.33 |
| event with a nested, partly redacted payload | 13.47 | 7.76 |
| event carrying a 3-deep exception chain | 34.01 | 13.17 |

## [2026.7.0]

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

### Docs

- The introduction runs its full example in the browser on Pyodide. widelog is one stdlib-only
  module, so its source is inlined at build time and written into Pyodide's filesystem, with no
  package install and no dependency on the release being on PyPI. Pyodide loads only on click.

### Notes

- `emit()` never raises. A failing sink or an unserializable field is reported on stderr and
  dropped, rather than failing the request or replacing the exception already in flight.
- Requires Python 3.10 or later, and nothing outside the standard library.
- Since the version number carries no compatibility promise, every breaking change gets its own
  `### Changed` or `### Removed` entry here. Pin `widelog-py==2026.7.0` if you need one.
