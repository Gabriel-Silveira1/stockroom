CREATE SCHEMA fulfillment;

-- One shipment per order. 'picked' means picked and packed: the box is waiting for
-- the carrier, and the dispatcher polls these rows by next_attempt_at.
CREATE TABLE fulfillment.shipments (
    id              uuid        PRIMARY KEY,
    order_id        uuid        NOT NULL UNIQUE,
    -- Null only for a tombstone: an order cancelled before its shipment existed.
    reservation_id  uuid,
    status          text        NOT NULL
                    CHECK (status IN ('picked', 'shipped', 'failed', 'cancelled')),
    attempts        integer     NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    tracking_number text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX shipments_due_idx ON fulfillment.shipments (next_attempt_at)
    WHERE status = 'picked';

CREATE TABLE fulfillment.shipment_lines (
    shipment_id  uuid    NOT NULL REFERENCES fulfillment.shipments,
    sku          text    NOT NULL,
    warehouse_id text    NOT NULL,
    quantity     integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (shipment_id, sku, warehouse_id)
);

CREATE TABLE fulfillment.outbox (
    position     bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id           uuid        PRIMARY KEY,
    type         text        NOT NULL,
    data         jsonb       NOT NULL,
    occurred_at  timestamptz NOT NULL,
    published_at timestamptz
);

CREATE INDEX outbox_unpublished_idx ON fulfillment.outbox (position) WHERE published_at IS NULL;

CREATE TABLE fulfillment.processed_messages (
    message_id   uuid        PRIMARY KEY,
    type         text        NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
