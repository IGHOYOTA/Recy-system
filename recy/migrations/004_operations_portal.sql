-- Additive portal operations. Existing journal and immutable scan rules remain authoritative.
ALTER TABLE app.staff_roles DROP CONSTRAINT staff_roles_role_check;
ALTER TABLE app.staff_roles ADD CHECK (role IN ('administrator','payout_processor','operations_reviewer','collector','depot_receiver'));
ALTER TABLE app.machines ADD COLUMN capacity_baseline_count bigint NOT NULL DEFAULT 0 CHECK (capacity_baseline_count>=0);
ALTER TABLE app.machines ADD COLUMN capacity_cycle_id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE app.machines ADD COLUMN capacity_cycle_started_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX machine_accepted_events ON app.bottle_events(machine_id) WHERE status='accepted';

CREATE TABLE app.deposit_reviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL UNIQUE REFERENCES app.collection_sessions(id),
 status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','resolved')),
 assigned_to uuid REFERENCES app.profiles(id), resolution text CHECK (resolution IN ('approved','rejected')),
 reason text, accepted_event_ids uuid[] NOT NULL DEFAULT '{}', amount_cents bigint NOT NULL DEFAULT 0 CHECK (amount_cents>=0),
 adjustment_id uuid UNIQUE REFERENCES app.wallet_adjustments(id), resolved_by uuid REFERENCES app.profiles(id),
 created_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
 CHECK ((status='resolved')=(resolution IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)),
 CHECK (resolution IS DISTINCT FROM 'approved' OR (adjustment_id IS NOT NULL AND amount_cents>0)),
 CHECK (resolution IS DISTINCT FROM 'rejected' OR (adjustment_id IS NULL AND amount_cents=0)),
 CHECK (amount_cents=cardinality(accepted_event_ids)*7)
);
CREATE TABLE app.receiving_depots (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), code text NOT NULL UNIQUE, name text NOT NULL,
 address text NOT NULL, contact text, active boolean NOT NULL DEFAULT true,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.evacuations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_id uuid NOT NULL REFERENCES app.machines(id),
 planned_depot_id uuid NOT NULL REFERENCES app.receiving_depots(id), actual_depot_id uuid REFERENCES app.receiving_depots(id),
 assigned_collector_id uuid NOT NULL REFERENCES app.profiles(id), created_by uuid NOT NULL REFERENCES app.profiles(id),
 status text NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','collected','in_transit','delivered','verified','needs_review','cancelled')),
 scheduled_at timestamptz NOT NULL, collected_at timestamptz, collected_by uuid REFERENCES app.profiles(id),
 delivered_at timestamptz, delivered_by uuid REFERENCES app.profiles(id), verified_at timestamptz, verified_by uuid REFERENCES app.profiles(id),
 received_count integer CHECK (received_count>=0), received_weight_kg numeric(12,3) CHECK (received_weight_kg>=0),
 accepted_total_at_pickup bigint, estimated_count_at_pickup bigint,
 previous_cycle_id uuid, next_cycle_id uuid, receipt_reference text, seal_reference text, notes text,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK (collected_at IS NULL OR (collected_by IS NOT NULL AND previous_cycle_id IS NOT NULL AND next_cycle_id IS NOT NULL)),
 CHECK (delivered_at IS NULL OR (delivered_by IS NOT NULL AND actual_depot_id IS NOT NULL)),
 CHECK (verified_at IS NULL OR (verified_by IS NOT NULL AND receipt_reference IS NOT NULL))
);
CREATE UNIQUE INDEX one_scheduled_pickup_per_machine ON app.evacuations(machine_id) WHERE status='scheduled';
CREATE TABLE app.evacuation_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), evacuation_id uuid NOT NULL REFERENCES app.evacuations(id),
 actor_id uuid NOT NULL REFERENCES app.profiles(id), action text NOT NULL,
 from_status text, to_status text NOT NULL, reason text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.machine_fill_reports (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_id uuid NOT NULL REFERENCES app.machines(id),
 machine_event_id uuid NOT NULL, cycle_id uuid NOT NULL, payload_hash text NOT NULL,
 level text NOT NULL CHECK (level IN ('normal','almost_full','full')),
 percent integer CHECK (percent BETWEEN 0 AND 100), observed_at timestamptz NOT NULL,
 received_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(machine_id,machine_event_id)
);
CREATE INDEX fill_report_cycle_time ON app.machine_fill_reports(machine_id,cycle_id,observed_at DESC);

-- Serialize accepted acknowledgements with the emptying cutoff. Never rewrite lifetime events.
CREATE FUNCTION app.serialize_capacity_capture() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.status='accepted' THEN PERFORM 1 FROM app.machines WHERE id=NEW.machine_id FOR UPDATE; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER zz_capacity_capture BEFORE INSERT OR UPDATE ON app.bottle_events
 FOR EACH ROW EXECUTE FUNCTION app.serialize_capacity_capture();
CREATE VIEW app.machine_capacity WITH (security_invoker=true) AS
SELECT m.*, totals.lifetime_accepted,
 greatest(0,totals.lifetime_accepted-m.capacity_baseline_count) AS accepted_since_evacuation,
 8000 AS almost_full_count,
 CASE WHEN EXISTS (SELECT 1 FROM app.machine_fill_reports f WHERE f.machine_id=m.id AND f.cycle_id=m.capacity_cycle_id AND (f.level='full' OR f.percent=100)) THEN 'full'
      WHEN totals.lifetime_accepted-m.capacity_baseline_count>=8000 THEN 'almost_full'
      WHEN latest.observed_at>=now()-interval '15 minutes' AND latest.level='almost_full' THEN 'almost_full'
      ELSE 'normal' END AS fill_status,
 latest.level AS sensor_level, latest.percent AS sensor_percent, latest.observed_at AS sensor_observed_at,
 coalesce(latest.observed_at<now()-interval '15 minutes',true) AS sensor_stale
FROM app.machines m
CROSS JOIN LATERAL (SELECT count(*) AS lifetime_accepted FROM app.bottle_events b WHERE b.machine_id=m.id AND b.status='accepted') totals
LEFT JOIN LATERAL (SELECT * FROM app.machine_fill_reports f WHERE f.machine_id=m.id AND f.cycle_id=m.capacity_cycle_id ORDER BY f.observed_at DESC,f.received_at DESC LIMIT 1) latest ON true;

CREATE FUNCTION app.guard_review_resolution() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.status='resolved' THEN RAISE EXCEPTION 'Resolved review is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.session_id<>OLD.session_id THEN RAISE EXCEPTION 'Review identity is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER review_resolution_guard BEFORE UPDATE OR DELETE ON app.deposit_reviews FOR EACH ROW EXECUTE FUNCTION app.guard_review_resolution();
CREATE FUNCTION app.guard_evacuation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' OR OLD.status IN ('verified','cancelled') THEN RAISE EXCEPTION 'Final evacuation is immutable' USING ERRCODE='23514'; END IF;
 IF ROW(NEW.machine_id,NEW.planned_depot_id,NEW.assigned_collector_id,NEW.created_by,NEW.scheduled_at)
 IS DISTINCT FROM ROW(OLD.machine_id,OLD.planned_depot_id,OLD.assigned_collector_id,OLD.created_by,OLD.scheduled_at) THEN
 RAISE EXCEPTION 'Evacuation identity is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER evacuation_guard BEFORE UPDATE OR DELETE ON app.evacuations FOR EACH ROW EXECUTE FUNCTION app.guard_evacuation();
DO $$ DECLARE t text; r text; BEGIN
 FOREACH t IN ARRAY ARRAY['deposit_reviews','receiving_depots','evacuations','evacuation_events','machine_fill_reports'] LOOP
  EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('REVOKE ALL ON app.%I FROM PUBLIC',t);
  FOREACH r IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
   IF EXISTS (SELECT FROM pg_roles WHERE rolname=r) THEN EXECUTE format('REVOKE ALL ON app.%I FROM %I',t,r); END IF;
  END LOOP;
 END LOOP;
 FOREACH t IN ARRAY ARRAY['evacuation_events','machine_fill_reports'] LOOP
  EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON app.%I FOR EACH ROW EXECUTE FUNCTION app.reject_mutation()',t);
  EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON app.%I FOR EACH STATEMENT EXECUTE FUNCTION app.reject_mutation()',t);
 END LOOP;
END $$;
REVOKE ALL ON app.machine_capacity FROM PUBLIC;
REVOKE ALL ON FUNCTION app.serialize_capacity_capture(),app.guard_review_resolution(),app.guard_evacuation() FROM PUBLIC;
