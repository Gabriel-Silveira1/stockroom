# 6. Three services, and why a modular monolith would be the honest default

- Status: accepted
- Date: 2026-09-27

## Context

The domain splits cleanly into three responsibilities: stock (`inventory`), the
customer's order (`orders`), and getting parcels out (`fulfillment`). The question is
whether those boundaries should be process boundaries.

At this scale, with one team and one release cadence, they should not. A modular
monolith with the same three modules would ship one artefact, and it could let
`orders` and `inventory` share one database transaction instead of coordinating
through events. It would have fewer moving parts, simpler debugging and no eventual
consistency where none is wanted. That is what I would build first for a real team
of this size.

## Decision

Three services anyway, because this project exists to show how the hard parts behave
when they are real: a service down mid-flow, duplicated and reordered messages,
compensation instead of rollback. Inside a monolith those are hypothetical.

The split is kept cheap:

- One repository and one image; each service is a different entrypoint.
- One Postgres instance, but each service owns a schema and never reads another's.
  They talk only through events. Separating databases later is a configuration change.
- Messaging (outbox, relay, consumer, retries) is one shared module, not three copies.

## Consequences

What the split costs, visibly:

- Eventual consistency. Right after a cancellation the order can read `cancelled`
  while inventory still shows the units reserved, for a few hundred milliseconds.
- Every cross-service step needs an event, a handler, idempotency and a test, where a
  monolith would need a function call.
- Each service repeats the outbox and processed-messages tables in its migrations.

What it buys, also visibly:

- Failure isolation. With `fulfillment` stopped, orders are still accepted and
  reserved; the work waits in its queue and completes when it returns (demo 4 in the
  README).
- Independent scaling where it matters: dispatch is I/O-bound on the carrier and could
  run more replicas without touching order intake.

If this were a product, I would start as a modular monolith with these module
boundaries and the outbox already in place. I would split out `fulfillment` first,
once carrier integrations or scaling gave a reason. The code is organised so that
move is mechanical.
