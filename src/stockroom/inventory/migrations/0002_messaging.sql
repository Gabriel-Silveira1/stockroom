CREATE TABLE inventory.outbox (
    position     bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    id           uuid        PRIMARY KEY,
    type         text        NOT NULL,
    data         jsonb       NOT NULL,
    occurred_at  timestamptz NOT NULL,
    published_at timestamptz
);

CREATE INDEX outbox_unpublished_idx ON inventory.outbox (position) WHERE published_at IS NULL;

CREATE TABLE inventory.processed_messages (
    message_id   uuid        PRIMARY KEY,
    type         text        NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now()
);
