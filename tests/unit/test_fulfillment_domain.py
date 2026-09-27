from datetime import UTC, datetime, timedelta

import pytest

from stockroom.fulfillment.domain import (
    DispatchOutcome,
    ShipmentStatus,
    after_failure,
    can_cancel,
    retry_delay,
)
from stockroom.inventory.domain import (
    InvalidTransitionError,
    ReservationStatus,
    resolve_status,
    should_pin,
)

SECOND = timedelta(seconds=1)


@pytest.mark.parametrize(("attempt", "expected"), [(1, 1), (2, 2), (3, 4), (4, 8), (10, 30)])
def test_retry_delay_doubles_up_to_the_cap(attempt: int, expected: int) -> None:
    assert retry_delay(attempt, base=SECOND, cap=30 * SECOND) == expected * SECOND


@pytest.mark.parametrize(
    ("attempts_so_far", "permanent", "expected"),
    [
        (0, False, DispatchOutcome.RETRY),
        (3, False, DispatchOutcome.RETRY),
        (4, False, DispatchOutcome.FAILED),
        (0, True, DispatchOutcome.FAILED),
    ],
)
def test_failure_retries_until_the_last_attempt(
    attempts_so_far: int, permanent: bool, expected: DispatchOutcome
) -> None:
    assert after_failure(attempts_so_far, max_attempts=5, permanent=permanent) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ShipmentStatus.PICKED, True),
        (ShipmentStatus.SHIPPED, False),
        (ShipmentStatus.FAILED, False),
        (ShipmentStatus.CANCELLED, False),
    ],
)
def test_only_a_waiting_box_can_be_cancelled(status: ShipmentStatus, expected: bool) -> None:
    assert can_cancel(status) is expected


def test_pinned_reservation_never_expires() -> None:
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)

    status = resolve_status(ReservationStatus.ACTIVE, long_ago, datetime.now(UTC), pinned=True)

    assert status is ReservationStatus.ACTIVE


@pytest.mark.parametrize(
    ("status", "expected"),
    [(ReservationStatus.ACTIVE, True), (ReservationStatus.COMMITTED, False)],
)
def test_pin_applies_to_live_holds(status: ReservationStatus, expected: bool) -> None:
    assert should_pin(status) is expected


@pytest.mark.parametrize("status", [ReservationStatus.EXPIRED, ReservationStatus.RELEASED])
def test_lapsed_hold_cannot_be_pinned(status: ReservationStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        should_pin(status)
