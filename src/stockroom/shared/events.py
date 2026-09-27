import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

type EventData = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    data: EventData
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MalformedEventError(ValueError):
    pass


def encode(event: Event) -> bytes:
    return json.dumps(
        {
            "id": str(event.id),
            "type": event.type,
            "occurred_at": event.occurred_at.isoformat(),
            "data": event.data,
        }
    ).encode()


def decode(body: bytes) -> Event:
    try:
        raw = json.loads(body)
        return Event(
            id=UUID(raw["id"]),
            type=str(raw["type"]),
            occurred_at=datetime.fromisoformat(raw["occurred_at"]),
            data=dict(raw["data"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise MalformedEventError(str(exc)) from exc
