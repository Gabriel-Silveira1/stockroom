import pytest

from stockroom.orders.domain import (
    InvalidOrderTransitionError,
    OrderLine,
    OrderStatus,
    request_hash,
    should_transition,
)
from stockroom.orders.service import merge_lines
from stockroom.shared.events import Event, MalformedEventError, decode, encode

S = OrderStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PENDING, S.RESERVED),
        (S.RESERVED, S.PICKED),
        (S.PICKED, S.SHIPPED),
        (S.PENDING, S.CANCELLED),
        (S.RESERVED, S.CANCELLED),
        (S.PICKED, S.CANCELLED),
    ],
)
def test_forward_moves_are_applied(current: OrderStatus, target: OrderStatus) -> None:
    assert should_transition(current, target) is True


@pytest.mark.parametrize("status", list(OrderStatus))
def test_reaching_the_current_status_again_is_a_no_op(status: OrderStatus) -> None:
    assert should_transition(status, status) is False


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.PENDING, S.SHIPPED),
        (S.RESERVED, S.PENDING),
        (S.SHIPPED, S.CANCELLED),
        (S.CANCELLED, S.RESERVED),
    ],
)
def test_skipping_or_reversing_is_refused(current: OrderStatus, target: OrderStatus) -> None:
    with pytest.raises(InvalidOrderTransitionError):
        should_transition(current, target)


def test_request_hash_ignores_key_order() -> None:
    assert request_hash({"a": 1, "b": [1, 2]}) == request_hash({"b": [1, 2], "a": 1})


def test_request_hash_changes_with_content() -> None:
    assert request_hash({"a": 1}) != request_hash({"a": 2})


def test_merge_lines_sums_repeated_items() -> None:
    lines = [OrderLine("B", "w", 1), OrderLine("A", "w", 2), OrderLine("B", "w", 3)]

    assert merge_lines(lines) == [OrderLine("A", "w", 2), OrderLine("B", "w", 4)]


def test_event_survives_a_round_trip() -> None:
    event = Event("order.placed", {"order_id": "x", "lines": [{"sku": "A"}]})

    assert decode(encode(event)) == event


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"id": "nope", "type": "t"}'])
def test_malformed_messages_are_reported(body: bytes) -> None:
    with pytest.raises(MalformedEventError):
        decode(body)
