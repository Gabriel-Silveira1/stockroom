# Stockroom

Event-driven inventory and fulfillment for a fictional online game store: base games,
expansion packs and accessories, stocked in more than one warehouse.

The core problem is not selling what is not there, even with concurrent orders,
duplicated webhooks and services failing mid-flow.

> Work in progress. `inventory` and `orders` are in place; `fulfillment` comes next.

| Service | Port | Owns |
|---|---|---|
| `inventory` | 8101 | Stock ledger, reservations with TTL |
| `orders` | 8102 | Order webhook (idempotent), order state machine |

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

API docs: <http://localhost:8101/docs> and <http://localhost:8102/docs>. Postgres is
exposed on port `5433`.

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
