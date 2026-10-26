-- Infrastructure required by user/API access; no grants to public Auth clients.
CREATE TABLE app.auth_rate_limits (
    bucket text NOT NULL, window_start timestamptz NOT NULL,
    requests integer NOT NULL CHECK (requests>0), expires_at timestamptz NOT NULL,
    PRIMARY KEY (bucket,window_start)
);
ALTER TABLE app.auth_rate_limits ENABLE ROW LEVEL SECURITY;
CREATE INDEX auth_limit_expiry ON app.auth_rate_limits(expires_at);
REVOKE ALL ON app.auth_rate_limits FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
    FOREACH r IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=r) THEN
            EXECUTE format('REVOKE ALL ON app.auth_rate_limits FROM %I',r);
        END IF;
    END LOOP;
END $$;

-- A kiosk exchanges its fresh Auth session for a collection-only binding.
ALTER TABLE app.machine_logins ADD COLUMN auth_revoked_before_collection boolean NOT NULL DEFAULT false;

-- Completed and rejected submissions remain durable; API errors never erase their end signal.
ALTER TABLE app.session_submissions ADD COLUMN review_reason text;

-- Server-generated verdict IDs are the stored bottle-event IDs themselves; no client eligibility flag is trusted.
