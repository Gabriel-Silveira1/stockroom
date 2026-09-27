# Stockroom

Event-driven inventory and fulfillment for a fictional online game store: base games,
expansion packs and accessories, stocked in more than one warehouse.

The core problem is not selling what is not there, even with concurrent orders,
duplicated webhooks and services failing mid-flow.

> Work in progress. The `inventory` service is in place; `orders` and `fulfillment`
> come next.

## Run it

Requires Docker.

```sh
docker compose up -d --build
curl localhost:8001/stock
```

API docs: <http://localhost:8001/docs>. Postgres is exposed on port `5433`.

## Develop

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pre-commit install
docker compose up -d postgres   # integration tests need real Postgres
uv run pytest
```

## Design decisions

- [ADR 0001: Stock as an append-only ledger](docs/adr/0001-stock-as-an-append-only-ledger.md)
- [ADR 0002: Row lock per stock item for reservations](docs/adr/0002-row-lock-per-stock-item-for-reservations.md)
