# 4. Carrier dispatch and compensation

- Status: accepted
- Date: 2026-09-27

## Context

Shipping means calling a carrier over HTTP. The carrier is slow, sometimes down, and
outside our database. A call can succeed while our process dies before recording it.
When shipping ends in failure, stock held for the order must go back on sale.

## Decision

**The dispatch is a database-driven job, not a message handler.** `order.reserved`
only creates the shipment (`picked`: picked and packed). Background workers claim due
shipments with `FOR UPDATE SKIP LOCKED` and call the carrier while holding that row
lock. Workers never collide. A cancellation for the same order waits for the call's
outcome instead of racing it.

**The shipment id is the carrier's idempotency key.** If the process dies after the
carrier booked the parcel but before our commit, the row is still `picked`. The next
attempt sends the same key and gets the same tracking number back. No second truck.

**Failures back off, then give up.** Timeouts, 5xx and 429 are retried with
exponential backoff (`next_attempt_at`), up to a maximum number of attempts. A 4xx is
permanent and fails at once.

**Compensation by events, not rollback.** A failed shipment emits
`fulfillment.failed`. `orders` cancels (`carrier_failed`) and emits
`order.cancelled`. `inventory` releases the hold:

```
order.reserved      -> fulfillment  create shipment     -> fulfillment.picked
fulfillment.picked  -> inventory    pin the hold        (goods are in a box: no TTL)
                    -> orders       picked
dispatcher          -> carrier      book (idempotent)   -> fulfillment.shipped | failed
fulfillment.shipped -> inventory    commit: ledger -qty
                    -> orders       shipped + tracking
fulfillment.failed  -> orders       cancel              -> order.cancelled
order.cancelled     -> inventory    release
                    -> fulfillment  stop the shipment, or leave a tombstone
```

**Pinning.** Once picked, a reservation stops expiring: the goods are physically set
aside. A hold that lapsed before picking cannot be pinned, because its units may have
been sold again. `inventory` answers with `stock.rejected` and the order is cancelled.

**Tombstones.** An order cancelled before fulfillment saw it leaves a `cancelled`
shipment row, so a late `order.reserved` cannot start a shipment for it.

## Consequences

- A shipment is booked at most once and every failure path ends with the stock back on
  sale. The tests cover each branch with a fake carrier; the compose stack runs a mock
  carrier whose failure rate can be changed live.
- The carrier call holds a database row lock for up to the HTTP timeout (2 s). That is
  fine per shipment. Carriers with minute-long calls would need a claim column and a
  lease instead.
- A cancellation that arrives while the parcel is already with the carrier cannot be
  honoured. The order stays `cancelled`, the stock is shipped, and this is logged for a
  human. Real carriers offer recall APIs; this mock does not.
