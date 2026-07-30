"""What one wide event costs, and how that compares to a line per step.

Run: uv run python benchmarks/bench.py

Every case throws its output away instead of writing it, so the numbers are
library cost plus serialization, without a write syscall polluting the
comparison. The "null sink" row drops serialization too, which isolates
widelog's own merge-and-build work.
"""

import json
import logging
import os
import sys
import timeit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import widelog


class Discard:
    """A stream that costs nothing, so both sides are measured on equal terms."""

    def write(self, _: str) -> int:
        return 0

    def flush(self) -> None:
        pass


SINK_STREAM = Discard()


# Mirrors emit()'s own json.dumps + print, minus the write, so the comparison
# against logging's StreamHandler is apples to apples.
def discard_sink(event: dict) -> None:
    print(json.dumps(event, default=str), file=SINK_STREAM)


PAYLOAD = {
    "user": {"id": "u_123", "email": "a@b.c", "password": "hunter2"},
    "request": {"headers": {"authorization": "Bearer xyz", "accept": "application/json"}},
    "items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}],
}


def bare() -> None:
    with widelog.wide_event(op="checkout"):
        pass


def eight_fields() -> None:
    with widelog.wide_event(op="checkout") as log:
        log.set(user_id="u_123", cart_size=3, total=49.99, currency="USD")
        log.set(payment="stripe", region="ap-southeast-1", cache_hit=False)


def nested_and_redacted() -> None:
    with widelog.wide_event(op="checkout") as log:
        log.set(PAYLOAD)


def chained_error() -> None:
    with widelog.wide_event(op="checkout") as log:
        try:
            try:
                try:
                    raise ValueError("row not found")
                except ValueError as inner:
                    raise KeyError("cart missing") from inner
            except KeyError as mid:
                raise RuntimeError("checkout failed") from mid
        except RuntimeError as exc:
            log.error(exc)


_logger = logging.getLogger("bench")
_logger.propagate = False
_logger.setLevel(logging.INFO)
_logger.addHandler(logging.StreamHandler(SINK_STREAM))


def logging_eight_lines() -> None:
    """The alternative widelog exists to replace: one line per step."""
    _logger.info("checkout started user_id=%s", "u_123")
    _logger.info("cart loaded size=%d", 3)
    _logger.info("total computed total=%.2f currency=%s", 49.99, "USD")
    _logger.info("payment provider=%s", "stripe")
    _logger.info("region=%s", "ap-southeast-1")
    _logger.info("cache_hit=%s", False)
    _logger.info("checkout ok")


def logging_one_line() -> None:
    _logger.info("checkout ok", extra={"fields": PAYLOAD})


CASES = [
    ("widelog: bare event", bare, None),
    ("widelog: 8 fields", eight_fields, None),
    ("widelog: nested + redaction", nested_and_redacted, None),
    ("widelog: 3-deep error chain", chained_error, None),
    ("widelog: 8 fields, null sink", eight_fields, lambda event: None),
    ("logging: 8 separate lines", logging_eight_lines, None),
    ("logging: 1 line + extra", logging_one_line, None),
]


def run(label: str, fn, sink, number: int = 20_000, repeat: int = 5) -> None:
    widelog.init(service="bench", sink=sink or discard_sink)
    fn()  # warm up import-time and first-call costs out of the measurement
    best = min(timeit.repeat(fn, number=number, repeat=repeat)) / number
    print(f"  {label:<32} {best * 1e6:8.2f} µs/op   {1 / best:>12,.0f} ops/sec")


if __name__ == "__main__":
    print(f"python {sys.version.split()[0]}, widelog {widelog.__version__}\n")
    for label, fn, sink in CASES:
        run(label, fn, sink)
