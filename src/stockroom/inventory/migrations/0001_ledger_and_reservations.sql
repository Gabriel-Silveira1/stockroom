CREATE SCHEMA inventory;

CREATE TABLE inventory.warehouses (
    id   text PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE inventory.skus (
    sku  text PRIMARY KEY,
    name text NOT NULL
);

-- Holds no quantity on purpose. It is the row a reservation locks, so that concurrent
-- reservations of the same SKU in the same warehouse run one at a time.
CREATE TABLE inventory.stock_items (
    sku          text NOT NULL REFERENCES inventory.skus,
    warehouse_id text NOT NULL REFERENCES inventory.warehouses,
    PRIMARY KEY (sku, warehouse_id)
);

-- The source of truth for physical stock. On-hand is the sum of this table.
CREATE TABLE inventory.stock_movements (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku          text        NOT NULL,
    warehouse_id text        NOT NULL,
    quantity     integer     NOT NULL CHECK (quantity <> 0),
    reason       text        NOT NULL CHECK (reason IN ('receipt', 'shipment')),
    reference    text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (sku, warehouse_id) REFERENCES inventory.stock_items
);

CREATE INDEX stock_movements_item_idx ON inventory.stock_movements (sku, warehouse_id);

CREATE FUNCTION inventory.reject_movement_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'stock_movements is append-only; record a correcting movement instead';
END;
$$;

CREATE TRIGGER stock_movements_append_only
    BEFORE UPDATE OR DELETE ON inventory.stock_movements
    FOR EACH ROW EXECUTE FUNCTION inventory.reject_movement_change();

CREATE TABLE inventory.reservations (
    id         uuid        PRIMARY KEY,
    order_id   text        NOT NULL UNIQUE,
    status     text        NOT NULL CHECK (status IN ('active', 'released', 'committed')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE inventory.reservation_lines (
    reservation_id uuid    NOT NULL REFERENCES inventory.reservations,
    sku            text    NOT NULL,
    warehouse_id   text    NOT NULL,
    quantity       integer NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (reservation_id, sku, warehouse_id),
    FOREIGN KEY (sku, warehouse_id) REFERENCES inventory.stock_items
);

CREATE INDEX reservation_lines_item_idx ON inventory.reservation_lines (sku, warehouse_id);

-- statement_timestamp(), not now(): a reservation waits for the row lock and only then
-- reads this view, so expiry must be judged at read time, not at transaction start.
CREATE VIEW inventory.stock_levels AS
SELECT si.sku,
       si.warehouse_id,
       on_hand.quantity                     AS on_hand,
       reserved.quantity                    AS reserved,
       on_hand.quantity - reserved.quantity AS available
FROM inventory.stock_items si
CROSS JOIN LATERAL (
    SELECT coalesce(sum(m.quantity), 0)::integer AS quantity
    FROM inventory.stock_movements m
    WHERE m.sku = si.sku AND m.warehouse_id = si.warehouse_id
) on_hand
CROSS JOIN LATERAL (
    SELECT coalesce(sum(l.quantity), 0)::integer AS quantity
    FROM inventory.reservation_lines l
    JOIN inventory.reservations r ON r.id = l.reservation_id
    WHERE l.sku = si.sku
      AND l.warehouse_id = si.warehouse_id
      AND r.status = 'active'
      AND r.expires_at > statement_timestamp()
) reserved;
