from datetime import UTC, datetime, timedelta

import pytest

from stockroom.inventory.domain import (
    InvalidReservationRequestError,
    InvalidTransitionError,
    ReservationStatus,
    Shortfall,
    StockKey,
    find_shortfalls,
    normalize_lines,
    resolve_status,
    should_commit,
    should_release,
)

CORE_WEST = StockKey("CORE-001", "eu-west")
CORE_CENTRAL = StockKey("CORE-001", "eu-central")
EXP_WEST = StockKey("EXP-001", "eu-west")
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def test_normalize_merges_repeated_items_and_sorts_by_key() -> None:
    lines = [(EXP_WEST, 1), (CORE_WEST, 2), (EXP_WEST, 3)]

    normalized = normalize_lines(lines)

    assert list(normalized.items()) == [(CORE_WEST, 2), (EXP_WEST, 4)]


@pytest.mark.parametrize("quantity", [0, -1])
def test_normalize_rejects_non_positive_quantities(quantity: int) -> None:
    with pytest.raises(InvalidReservationRequestError):
        normalize_lines([(CORE_WEST, quantity)])


def test_normalize_rejects_empty_request() -> None:
    with pytest.raises(InvalidReservationRequestError):
        normalize_lines([])


def test_shortfalls_list_only_lines_that_do_not_fit() -> None:
    requested = {CORE_WEST: 3, EXP_WEST: 1}
    available = {CORE_WEST: 2, EXP_WEST: 1}

    shortfalls = find_shortfalls(requested, available)

    assert shortfalls == [Shortfall(CORE_WEST, requested=3, available=2)]


def test_item_missing_from_availability_counts_as_zero() -> None:
    shortfalls = find_shortfalls({CORE_CENTRAL: 1}, {})

    assert shortfalls == [Shortfall(CORE_CENTRAL, requested=1, available=0)]


def test_active_reservation_past_deadline_is_expired() -> None:
    status = resolve_status(ReservationStatus.ACTIVE, NOW, NOW)

    assert status is ReservationStatus.EXPIRED


def test_active_reservation_before_deadline_stays_active() -> None:
    status = resolve_status(ReservationStatus.ACTIVE, NOW + timedelta(seconds=1), NOW)

    assert status is ReservationStatus.ACTIVE


def test_settled_reservation_is_never_reported_as_expired() -> None:
    status = resolve_status(ReservationStatus.COMMITTED, NOW - timedelta(hours=1), NOW)

    assert status is ReservationStatus.COMMITTED


@pytest.mark.parametrize(
    ("current", "expected"),
    [(ReservationStatus.ACTIVE, True), (ReservationStatus.COMMITTED, False)],
)
def test_commit_applies_once(current: ReservationStatus, expected: bool) -> None:
    assert should_commit(current) is expected


@pytest.mark.parametrize("current", [ReservationStatus.RELEASED, ReservationStatus.EXPIRED])
def test_commit_is_refused_once_stock_was_freed(current: ReservationStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        should_commit(current)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (ReservationStatus.ACTIVE, True),
        (ReservationStatus.RELEASED, False),
        (ReservationStatus.EXPIRED, False),
    ],
)
def test_release_applies_once(current: ReservationStatus, expected: bool) -> None:
    assert should_release(current) is expected


def test_release_is_refused_after_shipment() -> None:
    with pytest.raises(InvalidTransitionError):
        should_release(ReservationStatus.COMMITTED)
