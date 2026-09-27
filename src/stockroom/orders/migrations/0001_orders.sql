CREATE SCHEMA orders;

CREATE TABLE orders.orders (
    id             uuid        PRIMARY KEY,
    external_id    text        NOT NULL UNIQUE,
    status         text        NOT NULL
                   CHECK (status IN ('pending', 'reserved', 'picked', 'shipped', 'cancelled')),
    cancel_reason  text,
    reservation_id uuid,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX orders_status_idx ON orders.orders (status, created_at DESC);

CREATE TABLE orders.order_lines (
    order_id     uuid    NOT NULL REFERENCES orders.orders,
    sku          text    NOT NULL,
    warehouse_id text    NOT NULL,
    quantity     integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (order_id, sku, warehouse_id)
);

-- The order's timeline: every status change and the event that caused it.
CREATE TABLE orders.order_history (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id   uuid        NOT NULL REFERENCES orders.orders,
    status     text        NOT NULL,
    reason     text,
    cause      text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX order_history_order_idx ON orders.order_history (order_id, id);

-- Webhook replay protection. Written in the same transaction as the order it created,
-- so a key is never recorded for an order that was rolled back.
CREATE TABLE orders.idempotency_keys (
    key          text        PRIMARY KEY,
    request_hash text        NOT NULL,
    -- Deferred: the key is claimed first, so a concurrent request with the same key
    -- blocks on it, and the order it points to is inserted later in the transaction.
    order_id     uuid        NOT NULL REFERENCES orders.orders DEFERRABLE INITIALLY DEFERRED,
    status_code  integer     NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders.outbox (
    position     bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id           uuid        PRIMARY KEY,
    type         text        NOT NULL,
    data         jsonb       NOT NULL,
    occurred_at  timestamptz NOT NULL,
    published_at timestamptz
);

CREATE INDEX outbox_unpublished_idx ON orders.outbox (position) WHERE published_at IS NULL;

CREATE TABLE orders.processed_messages (
    message_id   uuid        PRIMARY KEY,
    type         text        NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
