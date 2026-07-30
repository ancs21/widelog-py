from __future__ import annotations

import pytest

from widelog import WidelogError, init, wide_event


def level_4():
    raise ValueError("connection reset")


def level_3():
    level_4()


def level_2():
    level_3()


def level_1():
    level_2()


def frames_in(event) -> str:
    return " ".join(event["error"]["stack"])


def test_stack_reaches_past_the_innermost_two_frames(seen):
    with pytest.raises(ValueError), wide_event():
        level_1()

    stack = seen[0]["error"]["stack"]
    assert len(stack) == 5
    joined = " ".join(stack)
    for name in ("level_1", "level_2", "level_3", "level_4"):
        assert name in joined, joined


def test_stack_depth_is_configurable(seen):
    init(stack_depth=2)
    with pytest.raises(ValueError), wide_event():
        level_1()

    assert len(seen[0]["error"]["stack"]) == 2


def test_widelogs_own_frames_are_not_in_the_stack(seen):
    with pytest.raises(WidelogError), wide_event():
        raise WidelogError("Payment failed", status=402)

    joined = frames_in(seen[0])
    assert "widelog/__init__.py" not in joined, joined
    assert "wide_event" not in joined, joined


def test_explicit_cause_reaches_the_event(seen):
    with pytest.raises(WidelogError), wide_event():
        try:
            level_1()
        except ValueError as exc:
            raise WidelogError("Payment failed", status=402, cause=exc)  # noqa: B904

    error = seen[0]["error"]
    assert error["message"] == "Payment failed"

    (cause,) = error["causes"]
    assert cause["type"] == "ValueError"
    assert cause["message"] == "connection reset"
    assert "level_4" in " ".join(cause["stack"])


def test_implicit_context_reaches_the_event(seen):
    with pytest.raises(WidelogError), wide_event():
        try:
            level_4()
        except ValueError:
            raise WidelogError("Payment failed", status=402)  # noqa: B904

    (cause,) = seen[0]["error"]["causes"]
    assert cause["type"] == "ValueError" and cause["message"] == "connection reset"


def test_raise_from_none_suppresses_the_chain(seen):
    with pytest.raises(WidelogError), wide_event():
        try:
            level_4()
        except ValueError:
            raise WidelogError("Payment failed", status=402) from None

    assert "causes" not in seen[0]["error"]


def test_a_long_chain_is_capped(seen):
    def nest(depth: int) -> None:
        if depth == 0:
            raise ValueError("bottom")
        try:
            nest(depth - 1)
        except Exception as exc:
            raise RuntimeError(f"layer {depth}") from exc

    with pytest.raises(RuntimeError), wide_event():
        nest(12)

    assert len(seen[0]["error"]["causes"]) == 5


def test_a_cyclic_chain_does_not_hang(seen):
    first = ValueError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first

    with wide_event() as log:
        log.error(first)

    assert len(seen[0]["error"]["causes"]) <= 5


def test_to_dict_carries_no_stack_or_causes(seen):
    """What goes to the client stays free of internals."""
    try:
        level_1()
    except ValueError as exc:
        err = WidelogError("Payment failed", status=402, why="Card declined", cause=exc)

    body = err.to_dict()
    assert body == {"message": "Payment failed", "status": 402, "why": "Card declined"}
    assert "stack" not in body and "causes" not in body


def test_a_string_error_has_no_stack(seen):
    with wide_event() as log:
        log.error("something went wrong")

    assert seen[0]["error"] == {"message": "something went wrong"}
