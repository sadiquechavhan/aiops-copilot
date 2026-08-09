-- Postgres runs every file in /docker-entrypoint-initdb.d exactly once, on first
-- start with an empty data directory. Editing this file later has no effect unless
-- the pgdata volume is removed:  docker compose down -v

CREATE TABLE IF NOT EXISTS inventory (
    sku       TEXT PRIMARY KEY,
    available INTEGER NOT NULL CHECK (available >= 0)
);

-- Seeded absurdly high so a two-week soak test never runs out of stock and starts
-- returning 409s that look like a fault we did not inject. A real system would
-- have a restock flow; this one has a big number.
INSERT INTO inventory (sku, available) VALUES
    ('SKU-1', 1000000000),
    ('SKU-2', 1000000000),
    ('SKU-3', 1000000000),
    ('SKU-4', 1000000000),
    ('SKU-5', 1000000000)
ON CONFLICT (sku) DO NOTHING;
