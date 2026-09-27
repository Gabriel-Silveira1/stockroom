# 3. Transactional outbox and idempotent consumers

- Status: accepted
- Date: 2026-09-27

## Context

Each service changes its own database and tells the others through RabbitMQ. The
database and the broker cannot share a transaction. A service that commits and then
publishes loses the event if it crashes in between. One that publishes and then
commits announces something that may roll back.

The broker also redelivers: a consumer that crashes before acking gets the same
message again.

## Decision

**Outbox.** A service never publishes inside a business transaction. It inserts the
event into its own `outbox` table in the same transaction as the state change, so the
two commit together or not at all. A relay in the same process reads unpublished rows
in insertion order with `FOR UPDATE SKIP LOCKED`, publishes them with publisher
confirms, and marks them sent. A crash after publishing and before marking means the
event goes out twice. That is at-least-once delivery, by design.

**Idempotent consumers.** Every event carries a unique id. A consumer handles a
message in one transaction that first inserts that id into `processed_messages`. A
duplicate hits the primary key and is skipped. The ack is sent only after the commit,
so a crash means redelivery, never loss. Handlers that start with a status check,
such as "already reserved", are safe even without this table, and the table makes
that true for all of them.

**Retries and dead letters.** A failing message is not requeued at once, which would
spin on a broken dependency. It is parked in `<queue>.retry`, whose TTL routes it back
after a delay. After the last attempt it goes to `<queue>.dlq` for a human.

**Choreography.** Services react to each other's events; no central coordinator:

```
orders     order.placed    -> inventory  reserve        -> stock.reserved | stock.rejected
inventory  stock.reserved  -> orders     mark reserved  -> order.reserved
inventory  stock.rejected  -> orders     cancel         -> order.cancelled
orders     order.cancelled -> inventory  release        -> stock.released
```

## Consequences

- No event is lost and none is applied twice, whatever crashes when.
- The outbox adds latency equal to the relay poll interval (200 ms by default).
  `LISTEN/NOTIFY` could remove it if it ever mattered.
- Events may arrive out of order after a retry. Handlers are written for that. For
  example, a `stock.reserved` for an order that was already cancelled makes `orders`
  announce the cancellation again, so the late hold is released.
- `tests/integration/bus.py` drives the real handlers without a broker for fast tests.
  `test_messaging_e2e.py` covers the relay, retries and the DLQ against real RabbitMQ.
