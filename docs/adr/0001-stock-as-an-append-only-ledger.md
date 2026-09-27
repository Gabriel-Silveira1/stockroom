# 1. Stock as an append-only ledger

- Status: accepted
- Date: 2026-09-27

## Context

The inventory service must never sell stock it does not have, and when numbers look
wrong someone has to be able to explain them. A mutable `quantity` column answers
"how many now" but not "why": every write overwrites the previous state.

## Decision

Physical stock is recorded as rows in `inventory.stock_movements`: receipts are
positive, shipments negative. Rows are never updated or deleted. A trigger rejects both,
and corrections are new movements.

On-hand is `sum(quantity)`. Holds are kept apart in `reservations` and
`reservation_lines`, because a hold is not a physical movement. It can be released or
expire without anything leaving the shelf. `available = on_hand - active holds`,
exposed by the `inventory.stock_levels` view.

Expiry is derived rather than stored: an `active` reservation whose `expires_at` has
passed stops counting as a hold, with no background job involved. A sweeper can tidy
statuses later, but correctness does not depend on it running.

## Consequences

- Every unit can be traced to the receipt or shipment that moved it.
- Reading a balance costs a sum over the item's movements. At this scale that is an
  indexed scan of a few rows. If it grows, a snapshot table (balance at movement N, plus
  the movements after N) keeps reads cheap without giving up the ledger.
- There is no quantity column to update conditionally, which shapes the locking
  strategy (ADR 0002).
