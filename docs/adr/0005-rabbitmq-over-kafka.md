# 5. RabbitMQ over Kafka

- Status: accepted
- Date: 2026-09-27

## Context

The services need a broker for commands-as-events between three consumers: a few event
types, low volume, and every message is work that must be done exactly once in effect.
What matters is per-message acknowledgement, delayed retries, a dead-letter queue, and
routing each event type to the services that care.

## Decision

RabbitMQ, with one durable topic exchange, one queue per service bound by routing key,
and a `<queue>.retry` (TTL, dead-letters back) and `<queue>.dlq` per service.

| Need | RabbitMQ | Kafka |
|---|---|---|
| Ack one message, retry it later | Built in (ack, TTL queue) | Offsets are per partition; a failing message blocks the ones behind it unless you build retry topics |
| Dead-letter queue | Built in (dead-letter exchange) | Convention you implement |
| Route by event type to each service | Topic exchange bindings | Topic per type or filter in consumers |
| Replay history, many independent readers | Not its model | Its core strength |
| Operate for a demo | One small container | Heavier, even with KRaft |

## Consequences

- Retries and the DLQ took a few declarations instead of custom plumbing
  (`shared/consumer.py`).
- There is no broker-side replay. That is acceptable because the source of truth for
  every event is the publishing service's `outbox` table: history lives in Postgres
  and can be re-published from there.
- The outbox and idempotent consumers make the broker replaceable. Switching to Kafka
  would change the relay and the consumer loop, not the services.
- Kafka becomes the better fit if events turn into a shared stream with many readers:
  analytics, search indexing, rebuilding read models, audit.
