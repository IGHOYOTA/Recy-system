-- Preserve actual source provenance: missing dates stay NULL, never today's date.
ALTER TABLE recy.sync_runs ADD COLUMN IF NOT EXISTS source_fingerprint text;
ALTER TABLE recy.source_monitor ALTER COLUMN observed_source_date DROP NOT NULL;
ALTER TABLE recy.source_monitor ADD COLUMN IF NOT EXISTS observed_source_fingerprint text;
ALTER TABLE recy.source_refresh_jobs ALTER COLUMN source_updated_on DROP NOT NULL;
ALTER TABLE recy.source_refresh_jobs ADD COLUMN IF NOT EXISTS source_fingerprint text;
CREATE UNIQUE INDEX IF NOT EXISTS source_content_job_version ON recy.source_refresh_jobs(source_fingerprint)
 WHERE source_updated_on IS NULL;
ALTER TABLE recy.source_refresh_jobs ADD CONSTRAINT source_job_has_version
 CHECK (source_updated_on IS NOT NULL OR source_fingerprint IS NOT NULL);
ALTER TABLE recy.source_monitor ADD CONSTRAINT source_monitor_has_version
 CHECK (observed_source_date IS NOT NULL OR observed_source_fingerprint IS NOT NULL);
