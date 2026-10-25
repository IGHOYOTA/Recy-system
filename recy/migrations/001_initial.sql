-- Keep these tables out of Supabase's public Data API schema.
CREATE SCHEMA IF NOT EXISTS recy;
REVOKE ALL ON SCHEMA recy FROM PUBLIC;

CREATE OR REPLACE FUNCTION recy.normalize_upc(value TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE WHEN value ~ '^[0-9 -]+$'
        AND length(replace(replace(value, ' ', ''), '-', '')) BETWEEN 4 AND 18
        THEN replace(replace(value, ' ', ''), '-', '') ELSE NULL END
$$;

CREATE TABLE IF NOT EXISTS recy.sync_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
    source_url TEXT NOT NULL, pages INTEGER NOT NULL,
    added INTEGER NOT NULL, removed INTEGER NOT NULL,
    total INTEGER NOT NULL, headers TEXT NOT NULL
);

-- One durable, unpublished scrape checkpoint, replaced when source version changes.
CREATE TABLE IF NOT EXISTS recy.scrape_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint TEXT NOT NULL, started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recy.scrape_pages (
    url TEXT PRIMARY KEY, html TEXT NOT NULL
);
REVOKE ALL ON ALL TABLES IN SCHEMA recy FROM PUBLIC;
