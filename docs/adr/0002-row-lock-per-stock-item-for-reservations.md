# 2. Row lock per stock item for reservations

- Status: accepted
- Date: 2026-09-27

## Context

Many orders can ask for the last units of the same item at the same time. The check
"is there enough?" and the write "hold it" must behave as one step, or two transactions
both see 1 available and both reserve it.

The usual options:

1. **Conditional update** such as `UPDATE stock SET available = available - n WHERE available >= n`.
   It is a single statement and needs no explicit lock, but it requires a mutable
   balance column. ADR 0001 rules that out.
2. **`SERIALIZABLE` isolation.** Correct, but conflicts surface as serialization
   failures that every caller must retry, and under a burst on one item most
   transactions would fail and retry.
3. **Pessimistic lock** with `SELECT ... FOR UPDATE` on a per-item row, then read and
   write.

## Decision

Option 3. `inventory.stock_items` has one row per (SKU, warehouse) and holds no
quantity: it exists to be locked. A reservation:

1. Inserts the reservation header. The unique `order_id` makes retries of one order
   wait here and then return the first result. The same order with different lines is
   rejected as a conflict: that is a bug upstream, not a retry.
2. Locks the stock rows of all its lines, **sorted by (sku, warehouse)**, so two
   multi-line orders never wait on each other in a cycle.
3. Reads availability **in a separate statement**. Under READ COMMITTED each statement
   takes a fresh snapshot, so this read sees the holds committed while step 2 waited. A
   single statement that locked and summed would sum from a snapshot taken before the
   wait.
4. Writes all lines, or raises and rolls back everything (all-or-nothing).

Expiry is judged with `statement_timestamp()` after the lock, not `now()` (transaction
start). Commit takes the same stock locks before checking expiry. Otherwise a
reservation that expired and had its units taken could still be shipped by a
transaction that read the clock just before the deadline.

## Consequences

- Reservations of the same item serialize; different items proceed in parallel.
  The throughput limit is per hot item, which is where it has to be.
- `tests/integration/test_reservation_concurrency.py` runs 200 concurrent orders
  against 50 units on real Postgres and asserts exactly 50 succeed. With the
  `FOR UPDATE` removed, the same test oversells (57 reserved in one run).
- SQLite cannot prove any of this, so the test uses real Postgres, in CI too.
