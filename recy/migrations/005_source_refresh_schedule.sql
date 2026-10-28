-- Source-only monitoring. Product columns and existing application tables are unchanged.
ALTER TABLE recy.sync_runs ADD COLUMN IF NOT EXISTS source_updated_on date;
CREATE TABLE IF NOT EXISTS recy.source_monitor (
 id integer PRIMARY KEY CHECK (id=1),
 last_checked_at timestamptz NOT NULL,
 observed_source_date date NOT NULL
);
CREATE TABLE IF NOT EXISTS recy.source_refresh_jobs (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 source_updated_on date NOT NULL UNIQUE,
 detected_at timestamptz NOT NULL, due_at timestamptz NOT NULL,
 timezone text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','superseded')),
 attempts integer NOT NULL DEFAULT 0,
 lease_owner uuid, lease_until timestamptz,
 last_attempt_at timestamptz, last_error_code text,
 completed_sync_run_id bigint REFERENCES recy.sync_runs(id),
 completed_at timestamptz,
 CHECK ((lease_owner IS NULL)=(lease_until IS NULL))
);
CREATE INDEX IF NOT EXISTS source_refresh_due ON recy.source_refresh_jobs(due_at) WHERE status IN ('pending','running');
ALTER TABLE recy.source_monitor ENABLE ROW LEVEL SECURITY;
ALTER TABLE recy.source_refresh_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON recy.source_monitor,recy.source_refresh_jobs FROM PUBLIC;
DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
  IF EXISTS (SELECT FROM pg_roles WHERE rolname=r) THEN
   EXECUTE format('REVOKE ALL ON recy.source_monitor,recy.source_refresh_jobs FROM %I',r);
  END IF;
 END LOOP;
END $$;
