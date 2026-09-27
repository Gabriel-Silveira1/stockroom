# Stockroom

Event-driven inventory and fulfillment for a fictional online game store: base games,
expansion packs and accessories, stocked in more than one warehouse.

The core problem is not selling what is not there, even with concurrent orders,
duplicated webhooks and services failing mid-flow.

| Service | Port | Owns |
|---|---|---|
| `inventory` | 8101 | Stock ledger, reservations with TTL |
| `orders` | 8102 | Order webhook (idempotent), order state machine and history |
| `fulfillment` | 8103 | Picking, carrier dispatch with retries, compensation |
| `carrier` | 8104 | A mock shipping carrier that fails on purpose |

They share one Postgres instance but each owns its own schema, and they talk only
through events on RabbitMQ (management UI on <http://localhost:15672>, `stockroom` /
`stockroom`).

## Run it

Requires Docker.

```sh
docker compose up -d --build
curl localhost:8101/stock

curl -X POST localhost:8102/webhooks/orders   -H 'Idempotency-Key: demo-1' -H 'Content-Type: application/json'   -d '{"id": 1001, "line_items": [{"sku": "CORE-001", "quantity": 2}]}'
```

Follow the order with `curl localhost:8102/orders/<id>`: it moves through `reserved`,
`picked` and `shipped`, with a tracking number. Make the carrier fail every call to
watch retries end in a cancelled order and released stock:

```sh
curl -X PUT localhost:8104/admin/behaviour   -H 'Content-Type: application/json' -d '{"failure_rate": 1, "latency_ms": 50}'
```

Each service serves its API docs at `/docs`. Postgres is exposed on port `5433`.

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pre-commit install
docker compose up -d postgres rabbitmq   # integration tests use the real thing
uv run pytest
```

## Design decisions

- [ADR 0001: Stock as an append-only ledger](docs/adr/0001-stock-as-an-append-only-ledger.md)
- [ADR 0002: Row lock per stock item for reservations](docs/adr/0002-row-lock-per-stock-item-for-reservations.md)
- [ADR 0003: Transactional outbox and idempotent consumers](docs/adr/0003-transactional-outbox-and-idempotent-consumers.md)
- [ADR 0004: Carrier dispatch and compensation](docs/adr/0004-carrier-dispatch-and-compensation.md)
