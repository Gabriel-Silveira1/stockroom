-- A pinned hold no longer expires: the goods are picked and packed.
ALTER TABLE inventory.reservations ADD COLUMN pinned boolean NOT NULL DEFAULT false;

CREATE OR REPLACE VIEW inventory.stock_levels AS
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
      AND (r.pinned OR r.expires_at > statement_timestamp())
) reserved;
