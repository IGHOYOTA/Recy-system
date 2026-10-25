-- Approved application schema. Supabase owns auth.users; no Auth rows are altered.
-- All money is integer CAD cents. No database-side bottle inactivity timer.
CREATE SCHEMA app;
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE TABLE app.profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_user_id uuid UNIQUE REFERENCES auth.users(id) ON DELETE SET NULL,
    first_name text NOT NULL CHECK (length(btrim(first_name)) > 0),
    last_name text NOT NULL CHECK (length(btrim(last_name)) > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','closed')),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz
);
CREATE TABLE app.staff_roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id),
    role text NOT NULL CHECK (role IN ('payout_processor','administrator')),
    granted_by uuid REFERENCES app.profiles(id), granted_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE UNIQUE INDEX staff_active_role ON app.staff_roles(user_id,role) WHERE revoked_at IS NULL;

CREATE TABLE app.machines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_code text NOT NULL UNIQUE,
    name text NOT NULL, location text,
    status text NOT NULL DEFAULT 'disabled' CHECK (status IN ('active','maintenance','disabled')),
    operating_mode text NOT NULL DEFAULT 'test' CHECK (operating_mode IN ('live','test')),
    firmware_version text, capture_protocol_version text, last_boot_id uuid,
    last_seen_at timestamptz, last_heartbeat_at timestamptz,
    last_clock_sync_at timestamptz, clock_offset_ms bigint,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.machine_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_id uuid NOT NULL REFERENCES app.machines(id),
    key_prefix text NOT NULL UNIQUE, secret_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
    revoked_at timestamptz, last_used_at timestamptz,
    CHECK (expires_at IS NULL OR expires_at > created_at)
);
CREATE TABLE app.machine_logins (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id),
    machine_id uuid NOT NULL REFERENCES app.machines(id), supabase_auth_session_id uuid NOT NULL UNIQUE,
    access_scope text NOT NULL DEFAULT 'machine_collection_only' CHECK (access_scope='machine_collection_only'),
    started_at timestamptz NOT NULL DEFAULT now(), backend_closed_at timestamptz,
    logout_status text NOT NULL DEFAULT 'active' CHECK (logout_status IN ('active','revocation_pending','revoked','needs_review')),
    revocation_requested_at timestamptz, revoked_at timestamptz, logout_reason text,
    UNIQUE (id,user_id,machine_id),
    CHECK (logout_status <> 'revoked' OR (backend_closed_at IS NOT NULL AND revoked_at IS NOT NULL))
);
CREATE TABLE app.account_change_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id),
    kind text NOT NULL CHECK (kind IN ('email_change','phone_change','account_recovery')),
    status text NOT NULL DEFAULT 'pending_verification' CHECK (status IN ('pending_verification','verified','applied','rejected','expired')),
    auth_reference text, requested_at timestamptz NOT NULL DEFAULT now(), verified_at timestamptz,
    expires_at timestamptz NOT NULL, completed_at timestamptz, reviewed_by uuid REFERENCES app.profiles(id),
    CHECK (expires_at > requested_at)
);

CREATE TABLE app.reward_rates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), version text NOT NULL UNIQUE,
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'),
    amount_per_bottle_cents bigint NOT NULL CHECK (amount_per_bottle_cents=7),
    effective_from timestamptz NOT NULL, effective_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id,currency,amount_per_bottle_cents),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    EXCLUDE USING gist (tstzrange(effective_from,effective_until,'[)') WITH &&)
);

CREATE TABLE app.wallets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','frozen','closed')),
    available_account_id uuid NOT NULL UNIQUE, reserved_account_id uuid NOT NULL UNIQUE,
    ledger_version bigint NOT NULL DEFAULT 0 CHECK (ledger_version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id,currency), UNIQUE (id,user_id,currency), UNIQUE (id,currency),
    CHECK (available_account_id <> reserved_account_id)
);
CREATE TABLE app.ledger_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), wallet_id uuid,
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'),
    purpose text NOT NULL CHECK (purpose IN ('user_available','user_reserved','platform_rewards','platform_payout_clearing')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (wallet_id,currency) REFERENCES app.wallets(id,currency) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((wallet_id IS NOT NULL AND purpose IN ('user_available','user_reserved')) OR
           (wallet_id IS NULL AND purpose IN ('platform_rewards','platform_payout_clearing')))
);
CREATE UNIQUE INDEX wallet_account_purpose ON app.ledger_accounts(wallet_id,purpose) WHERE wallet_id IS NOT NULL;
CREATE UNIQUE INDEX platform_account_purpose ON app.ledger_accounts(currency,purpose) WHERE wallet_id IS NULL;
ALTER TABLE app.wallets ADD FOREIGN KEY (available_account_id) REFERENCES app.ledger_accounts(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.wallets ADD FOREIGN KEY (reserved_account_id) REFERENCES app.ledger_accounts(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.collection_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id),
    machine_id uuid NOT NULL REFERENCES app.machines(id), machine_login_id uuid NOT NULL UNIQUE,
    collection_status text NOT NULL DEFAULT 'active' CHECK (collection_status IN ('active','submitted','cancelled')),
    reconciliation_status text NOT NULL DEFAULT 'pending' CHECK (reconciliation_status IN ('pending','processing','posted','no_credit','needs_review')),
    reward_rate_id uuid NOT NULL, currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'),
    reward_per_bottle_cents bigint NOT NULL DEFAULT 7 CHECK (reward_per_bottle_cents=7),
    accepted_count integer CHECK (accepted_count >= 0), rejected_count integer CHECK (rejected_count >= 0),
    reward_total_cents bigint CHECK (reward_total_cents >= 0), credit_transaction_id uuid UNIQUE,
    last_scan_at timestamptz, end_reason text CHECK (end_reason IN ('inactivity','machine_fault','restart','admin_recovery')),
    machine_end_event_id uuid, machine_ended_at timestamptz, ended_at timestamptz, end_received_at timestamptz,
    timing_quality text CHECK (timing_quality IN ('verified','estimated','needs_review')),
    credit_due_at timestamptz, credit_posted_at timestamptz,
    reconciliation_attempts integer NOT NULL DEFAULT 0 CHECK (reconciliation_attempts>=0),
    next_reconciliation_at timestamptz, last_error_code text,
    started_at timestamptz NOT NULL DEFAULT now(), submitted_at timestamptz, reconciled_at timestamptz,
    UNIQUE (id,user_id,machine_id), UNIQUE (id,user_id), UNIQUE (id,machine_id), UNIQUE (machine_id,machine_end_event_id),
    FOREIGN KEY (machine_login_id,user_id,machine_id) REFERENCES app.machine_logins(id,user_id,machine_id),
    FOREIGN KEY (reward_rate_id,currency,reward_per_bottle_cents) REFERENCES app.reward_rates(id,currency,amount_per_bottle_cents),
    CHECK (reward_total_cents IS NULL OR (accepted_count IS NOT NULL AND reward_total_cents=accepted_count::bigint*reward_per_bottle_cents)),
    CHECK (collection_status <> 'submitted' OR (ended_at IS NOT NULL AND end_received_at IS NOT NULL AND submitted_at IS NOT NULL AND machine_end_event_id IS NOT NULL)),
    CHECK (credit_due_at IS NOT DISTINCT FROM ended_at + interval '60 seconds'),
    CHECK (reconciliation_status NOT IN ('posted','no_credit') OR collection_status='submitted'),
    CHECK (reconciliation_status <> 'posted' OR (credit_transaction_id IS NOT NULL AND reconciled_at IS NOT NULL AND credit_posted_at IS NOT NULL AND reward_total_cents IS NOT NULL AND reward_total_cents>0)),
    CHECK (reconciliation_status <> 'no_credit' OR (accepted_count IS NOT NULL AND accepted_count=0 AND reward_total_cents IS NOT NULL AND reward_total_cents=0 AND reconciled_at IS NOT NULL AND credit_transaction_id IS NULL))
);
CREATE UNIQUE INDEX machine_active_collection ON app.collection_sessions(machine_id) WHERE collection_status='active';
CREATE UNIQUE INDEX user_active_collection ON app.collection_sessions(user_id) WHERE collection_status='active';
CREATE INDEX session_credit_deadline ON app.collection_sessions(credit_due_at) WHERE reconciliation_status IN ('pending','processing','needs_review');

CREATE TABLE app.bottle_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_event_id uuid NOT NULL,
    session_id uuid NOT NULL, user_id uuid NOT NULL, machine_id uuid NOT NULL,
    sequence_no integer NOT NULL CHECK (sequence_no>0), upc text NOT NULL CHECK (upc ~ '^[0-9]{4,18}$'),
    eligibility_status text NOT NULL CHECK (eligibility_status IN ('eligible','ineligible','unavailable')),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    reason_code text NOT NULL, registry_sync_run_id bigint REFERENCES recy.sync_runs(id),
    capture_event_id uuid, captured_at timestamptz, observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz,
    reward_cents bigint NOT NULL DEFAULT 0 CHECK (reward_cents IN (0,7)),
    machine_boot_id uuid, scan_monotonic_ms bigint CHECK (scan_monotonic_ms>=0),
    payload_hash text NOT NULL, capture_evidence_hash text, backend_acknowledged_at timestamptz,
    UNIQUE (id,user_id), UNIQUE (id,machine_id), UNIQUE (machine_id,machine_event_id), UNIQUE (session_id,sequence_no),
    UNIQUE (machine_id,capture_event_id),
    FOREIGN KEY (session_id,user_id,machine_id) REFERENCES app.collection_sessions(id,user_id,machine_id),
    CHECK ((status='accepted' AND eligibility_status='eligible' AND capture_event_id IS NOT NULL AND
            captured_at IS NOT NULL AND decided_at IS NOT NULL AND reward_cents=7 AND registry_sync_run_id IS NOT NULL) OR
           (status <> 'accepted' AND reward_cents=0))
);
CREATE INDEX bottle_session_status ON app.bottle_events(session_id,status);
CREATE TABLE app.session_submissions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL UNIQUE, machine_id uuid NOT NULL,
    idempotency_key text NOT NULL, request_hash text NOT NULL,
    claimed_accepted_count integer NOT NULL CHECK (claimed_accepted_count>=0),
    claimed_rejected_count integer NOT NULL CHECK (claimed_rejected_count>=0),
    final_sequence_no integer NOT NULL CHECK (final_sequence_no>=0),
    status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','validated','needs_review')),
    received_at timestamptz NOT NULL DEFAULT now(), validated_at timestamptz,
    machine_end_event_id uuid NOT NULL, end_reason text NOT NULL CHECK (end_reason IN ('inactivity','machine_fault','restart','admin_recovery')),
    machine_ended_at timestamptz, last_scan_at timestamptz, idle_duration_ms bigint CHECK (idle_duration_ms>=0),
    expected_attempt_count integer NOT NULL CHECK (expected_attempt_count>=0),
    manifest_hash text NOT NULL, sealed_machine_event_ids uuid[] NOT NULL DEFAULT '{}', acknowledged_at timestamptz,
    UNIQUE (machine_id,idempotency_key), UNIQUE (machine_id,machine_end_event_id),
    FOREIGN KEY (session_id,machine_id) REFERENCES app.collection_sessions(id,machine_id),
    CHECK (expected_attempt_count=final_sequence_no),
    CHECK (cardinality(sealed_machine_event_ids)=expected_attempt_count),
    CHECK (status <> 'validated' OR (validated_at IS NOT NULL AND claimed_accepted_count+claimed_rejected_count=expected_attempt_count))
);
CREATE TABLE app.machine_sync_batches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_id uuid NOT NULL, machine_boot_id uuid NOT NULL,
    session_id uuid NOT NULL, device_batch_id uuid NOT NULL, payload_hash text NOT NULL,
    first_sequence_no integer NOT NULL CHECK (first_sequence_no>0), last_sequence_no integer NOT NULL,
    status text NOT NULL DEFAULT 'received' CHECK (status IN ('received','applied','conflict','needs_review')),
    missing_sequence_numbers integer[] NOT NULL DEFAULT '{}', received_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz, acknowledged_at timestamptz,
    UNIQUE (machine_id,device_batch_id), CHECK (last_sequence_no>=first_sequence_no),
    FOREIGN KEY (session_id,machine_id) REFERENCES app.collection_sessions(id,machine_id)
);

CREATE TABLE app.payout_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id), wallet_id uuid NOT NULL,
    destination_email text NOT NULL CHECK (destination_email ~ '^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$'),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'), amount_cents bigint NOT NULL CHECK (amount_cents>=500),
    fee_cents bigint NOT NULL DEFAULT 0 CHECK (fee_cents=0),
    wallet_debit_cents bigint GENERATED ALWAYS AS (amount_cents) STORED,
    recipient_amount_cents bigint GENERATED ALWAYS AS (amount_cents) STORED,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','paid','rejected','cancelled','needs_review')),
    request_idempotency_key text NOT NULL, request_hash text NOT NULL,
    reserve_transaction_id uuid UNIQUE, settle_transaction_id uuid UNIQUE, release_transaction_id uuid UNIQUE,
    assigned_to uuid REFERENCES app.profiles(id), processed_by uuid REFERENCES app.profiles(id),
    payment_reference text, paid_at timestamptz, rejection_reason text, staff_note text,
    requested_at timestamptz NOT NULL DEFAULT now(), processing_started_at timestamptz, completed_at timestamptz,
    processing_due_at timestamptz NOT NULL DEFAULT (now()+interval '24 hours'), overdue_notified_at timestamptz,
    UNIQUE (user_id,request_idempotency_key), UNIQUE (id,user_id),
    FOREIGN KEY (wallet_id,user_id,currency) REFERENCES app.wallets(id,user_id,currency),
    CHECK (processing_due_at=requested_at+interval '24 hours'),
    CHECK (NOT (settle_transaction_id IS NOT NULL AND release_transaction_id IS NOT NULL)),
    CHECK (status <> 'processing' OR (assigned_to IS NOT NULL AND processing_started_at IS NOT NULL)),
    CHECK (status <> 'paid' OR (payment_reference IS NOT NULL AND length(btrim(payment_reference))>0 AND paid_at IS NOT NULL AND completed_at IS NOT NULL AND processed_by IS NOT NULL AND settle_transaction_id IS NOT NULL)),
    CHECK (status NOT IN ('rejected','cancelled') OR (completed_at IS NOT NULL AND release_transaction_id IS NOT NULL)),
    CHECK (status <> 'rejected' OR (rejection_reason IS NOT NULL AND length(btrim(rejection_reason))>0))
);
CREATE INDEX payout_pending_deadline ON app.payout_requests(processing_due_at) WHERE status IN ('pending','processing','needs_review');

CREATE TABLE app.ledger_transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('session_credit','payout_reserve','payout_settle','payout_release','adjustment','reversal')),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'),
    session_id uuid REFERENCES app.collection_sessions(id), payout_id uuid REFERENCES app.payout_requests(id),
    reverses_transaction_id uuid UNIQUE REFERENCES app.ledger_transactions(id),
    idempotency_key text NOT NULL UNIQUE, request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), posted_at timestamptz,
    CHECK ((kind='session_credit' AND session_id IS NOT NULL AND payout_id IS NULL AND reverses_transaction_id IS NULL) OR
           (kind IN ('payout_reserve','payout_settle','payout_release') AND payout_id IS NOT NULL AND session_id IS NULL AND reverses_transaction_id IS NULL) OR
           (kind='adjustment' AND session_id IS NULL AND payout_id IS NULL AND reverses_transaction_id IS NULL) OR
           (kind='reversal' AND reverses_transaction_id IS NOT NULL AND session_id IS NULL AND payout_id IS NULL)),
    CHECK (reverses_transaction_id IS DISTINCT FROM id)
);
CREATE UNIQUE INDEX session_credit_once ON app.ledger_transactions(session_id) WHERE kind='session_credit';
CREATE UNIQUE INDEX payout_posting_once ON app.ledger_transactions(payout_id,kind) WHERE payout_id IS NOT NULL;
CREATE TABLE app.ledger_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), transaction_id uuid NOT NULL REFERENCES app.ledger_transactions(id),
    account_id uuid NOT NULL REFERENCES app.ledger_accounts(id), amount_cents bigint NOT NULL CHECK (amount_cents<>0),
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (transaction_id,account_id)
);
CREATE INDEX ledger_account_history ON app.ledger_entries(account_id,created_at);
ALTER TABLE app.collection_sessions ADD FOREIGN KEY (credit_transaction_id) REFERENCES app.ledger_transactions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.payout_requests ADD FOREIGN KEY (reserve_transaction_id) REFERENCES app.ledger_transactions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.payout_requests ADD FOREIGN KEY (settle_transaction_id) REFERENCES app.ledger_transactions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.payout_requests ADD FOREIGN KEY (release_transaction_id) REFERENCES app.ledger_transactions(id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.payout_status_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), payout_id uuid NOT NULL REFERENCES app.payout_requests(id),
    from_status text CHECK (from_status IN ('pending','processing','paid','rejected','cancelled','needs_review')),
    to_status text NOT NULL CHECK (to_status IN ('pending','processing','paid','rejected','cancelled','needs_review')),
    actor_user_id uuid NOT NULL REFERENCES app.profiles(id), reason text, request_id uuid,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.idempotency_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), principal_type text NOT NULL CHECK (principal_type IN ('user','machine','staff')),
    principal_id uuid NOT NULL, operation text NOT NULL, key text NOT NULL, request_hash text NOT NULL,
    resource_type text NOT NULL, resource_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
    UNIQUE (principal_type,principal_id,operation,key), CHECK (expires_at>created_at)
);
CREATE TABLE app.audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), actor_type text NOT NULL CHECK (actor_type IN ('user','machine','staff','system')),
    actor_id uuid, action text NOT NULL, resource_type text NOT NULL, resource_id uuid,
    request_id uuid, created_at timestamptz NOT NULL DEFAULT now(), redacted_details jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE app.work_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('session_reconcile','machine_auth_revoke','submission_recover','notification_send','bank_reconcile')),
    resource_type text NOT NULL, resource_id uuid NOT NULL, deduplication_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','retry','completed','needs_review')),
    priority integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(), deadline_at timestamptz,
    locked_until timestamptz, attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0), last_error_code text,
    created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
    CHECK (status <> 'running' OR locked_until IS NOT NULL)
);
CREATE INDEX work_job_claim ON app.work_jobs(priority DESC,available_at) WHERE status IN ('pending','retry','running');

CREATE TABLE app.support_cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES app.profiles(id),
    session_id uuid, bottle_event_id uuid, payout_id uuid,
    category text NOT NULL CHECK (category IN ('missing_credit','bottle_dispute','payout_problem','account_recovery')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','resolved','closed')),
    description text NOT NULL, assigned_to uuid REFERENCES app.profiles(id), resolution_code text,
    adjustment_transaction_id uuid UNIQUE REFERENCES app.ledger_transactions(id),
    created_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz, closed_at timestamptz,
    FOREIGN KEY (session_id,user_id) REFERENCES app.collection_sessions(id,user_id),
    FOREIGN KEY (bottle_event_id,user_id) REFERENCES app.bottle_events(id,user_id),
    FOREIGN KEY (payout_id,user_id) REFERENCES app.payout_requests(id,user_id)
);
CREATE TABLE app.wallet_adjustments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), wallet_id uuid NOT NULL REFERENCES app.wallets(id),
    support_case_id uuid REFERENCES app.support_cases(id), amount_cents bigint NOT NULL CHECK (amount_cents<>0),
    reason_code text NOT NULL, requested_by uuid NOT NULL REFERENCES app.profiles(id),
    approved_by uuid REFERENCES app.profiles(id),
    status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','posted','rejected','needs_review')),
    ledger_transaction_id uuid UNIQUE REFERENCES app.ledger_transactions(id) DEFERRABLE INITIALLY DEFERRED,
    created_at timestamptz NOT NULL DEFAULT now(), approved_at timestamptz, posted_at timestamptz,
    CHECK (status NOT IN ('approved','posted') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)),
    CHECK (status <> 'posted' OR (ledger_transaction_id IS NOT NULL AND posted_at IS NOT NULL))
);
CREATE TABLE app.machine_incidents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), machine_id uuid NOT NULL REFERENCES app.machines(id),
    session_id uuid, bottle_event_id uuid,
    kind text NOT NULL CHECK (kind IN ('jam','capture_uncertain','restart','network_loss','tamper','clock_skew')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','resolved')),
    opened_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
    assigned_to uuid REFERENCES app.profiles(id), resolution_code text, redacted_details jsonb NOT NULL DEFAULT '{}',
    FOREIGN KEY (session_id,machine_id) REFERENCES app.collection_sessions(id,machine_id),
    FOREIGN KEY (bottle_event_id,machine_id) REFERENCES app.bottle_events(id,machine_id)
);
CREATE TABLE app.operational_alerts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('credit_overdue','payout_overdue','machine_offline','logout_failed','registry_stale','unreconciled_capture','ledger_mismatch','funding_shortfall')),
    resource_type text NOT NULL, resource_id uuid, deduplication_key text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('info','warning','critical')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_by uuid REFERENCES app.profiles(id), resolved_at timestamptz, redacted_details jsonb NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX active_alert_key ON app.operational_alerts(deduplication_key) WHERE status <> 'resolved';
CREATE TABLE app.notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid REFERENCES app.profiles(id), staff_user_id uuid REFERENCES app.profiles(id),
    kind text NOT NULL, resource_type text NOT NULL, resource_id uuid,
    channel text NOT NULL CHECK (channel IN ('in_app','email','sms')),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','failed')),
    deduplication_key text NOT NULL UNIQUE, attempts integer NOT NULL DEFAULT 0 CHECK (attempts>=0),
    created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz, last_error_code text,
    CHECK (num_nonnulls(user_id,staff_user_id)=1)
);
CREATE TABLE app.bank_reconciliation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), bank_source_reference text NOT NULL,
    period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','completed','needs_review')),
    imported_by uuid NOT NULL REFERENCES app.profiles(id), reviewed_by uuid REFERENCES app.profiles(id),
    created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz, CHECK (period_end>period_start)
);
CREATE TABLE app.bank_statement_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), run_id uuid NOT NULL REFERENCES app.bank_reconciliation_runs(id),
    bank_source_reference text NOT NULL, external_reference text, import_fingerprint text NOT NULL,
    occurred_at timestamptz NOT NULL, amount_cents bigint NOT NULL CHECK (amount_cents>0),
    currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'), direction text NOT NULL CHECK (direction IN ('in','out')),
    payout_id uuid REFERENCES app.payout_requests(id), match_status text NOT NULL DEFAULT 'unmatched' CHECK (match_status IN ('unmatched','matched','disputed')),
    matched_by uuid REFERENCES app.profiles(id), matched_at timestamptz,
    UNIQUE (bank_source_reference,import_fingerprint)
);
CREATE UNIQUE INDEX bank_external_reference ON app.bank_statement_items(bank_source_reference,external_reference) WHERE external_reference IS NOT NULL;
CREATE TABLE app.bank_payout_matches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    statement_item_id uuid NOT NULL REFERENCES app.bank_statement_items(id),
    payout_id uuid NOT NULL REFERENCES app.payout_requests(id),
    allocated_amount_cents bigint NOT NULL CHECK (allocated_amount_cents>0),
    matched_by uuid NOT NULL REFERENCES app.profiles(id), matched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (statement_item_id,payout_id)
);
CREATE TABLE app.funding_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), currency char(3) NOT NULL DEFAULT 'CAD' CHECK (currency='CAD'),
    as_of timestamptz NOT NULL, available_liability_cents bigint NOT NULL CHECK (available_liability_cents>=0),
    reserved_liability_cents bigint NOT NULL CHECK (reserved_liability_cents>=0),
    verified_cash_available_cents bigint NOT NULL CHECK (verified_cash_available_cents>=0),
    source_reference text NOT NULL, recorded_by uuid NOT NULL REFERENCES app.profiles(id), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.recovery_checks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), kind text NOT NULL CHECK (kind IN ('backup','restore_drill','post_restore_reconcile')),
    started_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','passed','failed','needs_review')),
    artifact_reference text, reviewed_by uuid REFERENCES app.profiles(id), notes text
);

-- Ownership, immutable financial records, and explicit machine submission.
CREATE FUNCTION app.reject_mutation() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='23514'; END $$;

CREATE FUNCTION app.touch_updated_at() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN NEW.updated_at=clock_timestamp(); RETURN NEW; END $$;

CREATE FUNCTION app.guard_session() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE m app.machines; l app.machine_logins;
BEGIN
    IF TG_OP='INSERT' THEN
        SELECT * INTO m FROM app.machines WHERE id=NEW.machine_id FOR NO KEY UPDATE;
        SELECT * INTO l FROM app.machine_logins WHERE id=NEW.machine_login_id FOR NO KEY UPDATE;
        IF m.status <> 'active' OR m.operating_mode <> 'live' OR l.logout_status <> 'active' OR
           l.backend_closed_at IS NOT NULL OR NOT EXISTS (SELECT 1 FROM app.profiles WHERE id=NEW.user_id AND status='active') THEN
            RAISE EXCEPTION 'Session requires active user, live machine, and open machine login' USING ERRCODE='23514';
        END IF;
    ELSE
        IF ROW(NEW.user_id,NEW.machine_id,NEW.machine_login_id,NEW.reward_rate_id,NEW.currency,NEW.reward_per_bottle_cents,NEW.started_at)
           IS DISTINCT FROM ROW(OLD.user_id,OLD.machine_id,OLD.machine_login_id,OLD.reward_rate_id,OLD.currency,OLD.reward_per_bottle_cents,OLD.started_at) THEN
            RAISE EXCEPTION 'Session ownership and reward snapshot are immutable' USING ERRCODE='23514';
        END IF;
        IF OLD.collection_status <> 'active' AND ROW(NEW.collection_status,NEW.ended_at,NEW.end_received_at,NEW.submitted_at,NEW.credit_due_at,NEW.machine_end_event_id)
           IS DISTINCT FROM ROW(OLD.collection_status,OLD.ended_at,OLD.end_received_at,OLD.submitted_at,OLD.credit_due_at,OLD.machine_end_event_id) THEN
            RAISE EXCEPTION 'Recorded session end cannot be reopened or reset' USING ERRCODE='23514';
        END IF;
        IF OLD.reconciliation_status IN ('posted','no_credit') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'Finalized session is immutable; use an audited adjustment' USING ERRCODE='23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_session BEFORE INSERT OR UPDATE ON app.collection_sessions FOR EACH ROW EXECUTE FUNCTION app.guard_session();

CREATE FUNCTION app.guard_bottle_event() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE s app.collection_sessions;
BEGIN
    SELECT * INTO s FROM app.collection_sessions WHERE id=NEW.session_id FOR NO KEY UPDATE;
    IF s.reconciliation_status IN ('posted','no_credit') THEN
        RAISE EXCEPTION 'Finalized collection cannot acquire or change bottles' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.status <> 'pending' OR ROW(NEW.session_id,NEW.user_id,NEW.machine_id,NEW.machine_event_id,NEW.sequence_no,NEW.upc,NEW.payload_hash)
           IS DISTINCT FROM ROW(OLD.session_id,OLD.user_id,OLD.machine_id,OLD.machine_event_id,OLD.sequence_no,OLD.upc,OLD.payload_hash) THEN
            RAISE EXCEPTION 'Bottle identity and final verdict are immutable' USING ERRCODE='23514';
        END IF;
    ELSIF s.collection_status <> 'active' AND NOT EXISTS (
        SELECT 1 FROM app.session_submissions x WHERE x.session_id=s.id
        AND NEW.machine_event_id=ANY(x.sealed_machine_event_ids) AND NEW.sequence_no<=x.final_sequence_no
    ) THEN
        RAISE EXCEPTION 'Late bottle must belong to the sealed session manifest' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_bottle BEFORE INSERT OR UPDATE ON app.bottle_events FOR EACH ROW EXECUTE FUNCTION app.guard_bottle_event();

CREATE FUNCTION app.record_submission_end() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE s app.collection_sessions; ended timestamptz=clock_timestamp();
BEGIN
    SELECT * INTO s FROM app.collection_sessions WHERE id=NEW.session_id FOR NO KEY UPDATE;
    IF NOT FOUND OR s.machine_id<>NEW.machine_id OR s.collection_status<>'active' THEN
        RAISE EXCEPTION 'Final dictionary requires its assigned active session' USING ERRCODE='23514';
    END IF;
    IF cardinality(NEW.sealed_machine_event_ids) <> (SELECT count(DISTINCT x) FROM unnest(NEW.sealed_machine_event_ids) x) THEN
        RAISE EXCEPTION 'Manifest attempt IDs must be unique' USING ERRCODE='23514';
    END IF;
    NEW.received_at=ended;
    UPDATE app.collection_sessions SET collection_status='submitted', ended_at=ended, end_received_at=ended,
        submitted_at=ended, credit_due_at=ended+interval '60 seconds', end_reason=NEW.end_reason,
        machine_end_event_id=NEW.machine_end_event_id, machine_ended_at=NEW.machine_ended_at, last_scan_at=NEW.last_scan_at
        WHERE id=s.id;
    UPDATE app.machine_logins SET backend_closed_at=ended, logout_status='revocation_pending',
        revocation_requested_at=ended, logout_reason='collection_submitted' WHERE id=s.machine_login_id;
    INSERT INTO app.work_jobs(kind,resource_type,resource_id,deduplication_key,deadline_at)
    VALUES ('session_reconcile','collection_session',s.id,'reconcile:'||s.id,ended+interval '60 seconds'),
           ('machine_auth_revoke','machine_login',s.machine_login_id,'logout:'||s.machine_login_id,ended+interval '60 seconds');
    RETURN NEW;
END $$;
CREATE TRIGGER record_end BEFORE INSERT ON app.session_submissions FOR EACH ROW EXECUTE FUNCTION app.record_submission_end();

CREATE FUNCTION app.guard_payout() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status <> 'pending' THEN RAISE EXCEPTION 'Payout starts pending' USING ERRCODE='23514'; END IF;
        NEW.requested_at=clock_timestamp(); NEW.processing_due_at=NEW.requested_at+interval '24 hours';
        IF NOT EXISTS (SELECT 1 FROM app.wallets WHERE id=NEW.wallet_id AND status='active') THEN
            RAISE EXCEPTION 'Payout requires an active wallet' USING ERRCODE='23514';
        END IF;
    ELSE
        IF ROW(NEW.user_id,NEW.wallet_id,NEW.currency,NEW.amount_cents,NEW.destination_email,NEW.request_idempotency_key,NEW.request_hash,NEW.requested_at,NEW.processing_due_at)
           IS DISTINCT FROM ROW(OLD.user_id,OLD.wallet_id,OLD.currency,OLD.amount_cents,OLD.destination_email,OLD.request_idempotency_key,OLD.request_hash,OLD.requested_at,OLD.processing_due_at) THEN
            RAISE EXCEPTION 'Payout amount, destination, owner, and deadline are immutable' USING ERRCODE='23514';
        END IF;
        IF OLD.status IN ('paid','rejected','cancelled') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'Terminal payout cannot be changed' USING ERRCODE='23514';
        END IF;
        IF NEW.status<>OLD.status AND NOT (
            (OLD.status='pending' AND NEW.status IN ('processing','rejected','cancelled','needs_review')) OR
            (OLD.status='processing' AND NEW.status IN ('paid','rejected','needs_review')) OR
            (OLD.status='needs_review' AND NEW.status IN ('processing','paid','rejected'))
        ) THEN RAISE EXCEPTION 'Invalid payout transition' USING ERRCODE='23514'; END IF;
        IF OLD.reserve_transaction_id IS NOT NULL AND NEW.reserve_transaction_id IS DISTINCT FROM OLD.reserve_transaction_id THEN
            RAISE EXCEPTION 'Payout reservation cannot be replaced' USING ERRCODE='23514';
        END IF;
    END IF;
    IF NEW.status='processing' AND NOT EXISTS (SELECT 1 FROM app.staff_roles WHERE user_id=NEW.assigned_to AND revoked_at IS NULL) THEN
        RAISE EXCEPTION 'Assigned payout operator requires an active staff role' USING ERRCODE='23514';
    END IF;
    IF NEW.status IN ('paid','rejected') AND NOT EXISTS (SELECT 1 FROM app.staff_roles WHERE user_id=NEW.processed_by AND revoked_at IS NULL) THEN
        RAISE EXCEPTION 'Payout resolution requires an active staff operator' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER guard_payout BEFORE INSERT OR UPDATE ON app.payout_requests FOR EACH ROW EXECUTE FUNCTION app.guard_payout();
CREATE FUNCTION app.record_payout_status() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        INSERT INTO app.payout_status_events(payout_id,to_status,actor_user_id) VALUES (NEW.id,NEW.status,NEW.user_id);
    ELSIF NEW.status<>OLD.status THEN
        INSERT INTO app.payout_status_events(payout_id,from_status,to_status,actor_user_id,reason)
        VALUES (NEW.id,OLD.status,NEW.status,CASE WHEN NEW.status='cancelled' THEN NEW.user_id
            ELSE coalesce(NEW.processed_by,NEW.assigned_to,NEW.user_id) END,NEW.rejection_reason);
    END IF; RETURN NULL;
END $$;
CREATE TRIGGER payout_status_audit AFTER INSERT OR UPDATE ON app.payout_requests FOR EACH ROW EXECUTE FUNCTION app.record_payout_status();

CREATE FUNCTION app.guard_wallet() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF ROW(NEW.user_id,NEW.currency,NEW.available_account_id,NEW.reserved_account_id)
       IS DISTINCT FROM ROW(OLD.user_id,OLD.currency,OLD.available_account_id,OLD.reserved_account_id) THEN
        RAISE EXCEPTION 'Wallet ownership and accounts are immutable' USING ERRCODE='23514';
    END IF; RETURN NEW;
END $$;
CREATE TRIGGER guard_wallet BEFORE UPDATE ON app.wallets FOR EACH ROW EXECUTE FUNCTION app.guard_wallet();
CREATE FUNCTION app.check_wallet_accounts() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.ledger_accounts WHERE id=NEW.available_account_id AND wallet_id=NEW.id AND currency=NEW.currency AND purpose='user_available') OR
       NOT EXISTS (SELECT 1 FROM app.ledger_accounts WHERE id=NEW.reserved_account_id AND wallet_id=NEW.id AND currency=NEW.currency AND purpose='user_reserved') THEN
        RAISE EXCEPTION 'Wallet account ownership/purpose mismatch' USING ERRCODE='23514';
    END IF; RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER wallet_accounts AFTER INSERT OR UPDATE ON app.wallets
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_wallet_accounts();

-- A transaction is built privately inside one database transaction, then sealed.
CREATE FUNCTION app.guard_journal_header() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_OP='DELETE' OR OLD.posted_at IS NOT NULL OR NEW.posted_at IS NULL OR
       (to_jsonb(NEW)-'posted_at') IS DISTINCT FROM (to_jsonb(OLD)-'posted_at') THEN
        RAISE EXCEPTION 'Posted journal is immutable' USING ERRCODE='23514';
    END IF; RETURN NEW;
END $$;
CREATE TRIGGER journal_header_immutable BEFORE UPDATE OR DELETE ON app.ledger_transactions FOR EACH ROW EXECUTE FUNCTION app.guard_journal_header();
CREATE FUNCTION app.guard_journal_entry() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE sealed timestamptz; owner_wallet uuid;
BEGIN
    SELECT posted_at INTO sealed FROM app.ledger_transactions WHERE id=NEW.transaction_id FOR UPDATE;
    IF sealed IS NOT NULL THEN RAISE EXCEPTION 'Cannot append to a posted journal' USING ERRCODE='23514'; END IF;
    SELECT wallet_id INTO owner_wallet FROM app.ledger_accounts WHERE id=NEW.account_id;
    -- A real row update also serializes callers using repeatable-read isolation.
    IF owner_wallet IS NOT NULL THEN UPDATE app.wallets SET ledger_version=ledger_version+1 WHERE id=owner_wallet; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER journal_entry_guard BEFORE INSERT ON app.ledger_entries FOR EACH ROW EXECUTE FUNCTION app.guard_journal_entry();

CREATE FUNCTION app.validate_journal(tx uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE t app.ledger_transactions; p app.payout_requests; s app.collection_sessions;
        w app.wallets; n bigint; total numeric; a uuid; b uuid; amount bigint;
        accepted bigint; rejected bigint;
BEGIN
    SELECT * INTO t FROM app.ledger_transactions WHERE id=tx;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT count(*),coalesce(sum(amount_cents),0) INTO n,total FROM app.ledger_entries WHERE transaction_id=tx;
    IF t.posted_at IS NULL OR n<2 OR total<>0 OR EXISTS (
        SELECT 1 FROM app.ledger_entries e JOIN app.ledger_accounts x ON x.id=e.account_id
        WHERE e.transaction_id=tx AND x.currency<>t.currency
    ) THEN RAISE EXCEPTION 'Journal must be sealed, balanced, and in one currency' USING ERRCODE='23514'; END IF;
    IF EXISTS (SELECT 1 FROM app.ledger_entries e JOIN app.ledger_accounts x ON x.id=e.account_id
               WHERE e.transaction_id=tx AND x.wallet_id IS NOT NULL AND
               (SELECT coalesce(sum(y.amount_cents),0) FROM app.ledger_entries y WHERE y.account_id=x.id)<0) THEN
        RAISE EXCEPTION 'User wallet balance cannot be negative' USING ERRCODE='23514';
    END IF;
    IF t.kind='session_credit' THEN
        SELECT * INTO s FROM app.collection_sessions WHERE id=t.session_id;
        SELECT * INTO w FROM app.wallets WHERE user_id=s.user_id AND currency=s.currency;
        SELECT count(*) FILTER (WHERE status='accepted'),count(*) FILTER (WHERE status='rejected')
            INTO accepted,rejected FROM app.bottle_events WHERE session_id=s.id;
        IF s.reconciliation_status<>'posted' OR s.credit_transaction_id IS DISTINCT FROM tx OR
           s.accepted_count IS DISTINCT FROM accepted OR s.rejected_count IS DISTINCT FROM rejected OR
           EXISTS (SELECT 1 FROM app.bottle_events WHERE session_id=s.id AND status='pending') OR
           NOT EXISTS (SELECT 1 FROM app.session_submissions WHERE session_id=s.id AND status='validated'
                       AND expected_attempt_count=accepted+rejected) OR
           EXISTS (SELECT 1 FROM app.bottle_events e JOIN app.session_submissions x ON x.session_id=e.session_id
                   WHERE e.session_id=s.id AND (NOT (e.machine_event_id=ANY(x.sealed_machine_event_ids)) OR e.sequence_no>x.final_sequence_no)) THEN
            RAISE EXCEPTION 'Session credit requires validated captured bottles and matching reconciliation' USING ERRCODE='23514';
        END IF;
        a=w.available_account_id; amount=s.reward_total_cents;
        SELECT id INTO b FROM app.ledger_accounts WHERE purpose='platform_rewards' AND currency=t.currency;
    ELSIF t.payout_id IS NOT NULL THEN
        SELECT * INTO p FROM app.payout_requests WHERE id=t.payout_id;
        SELECT * INTO w FROM app.wallets WHERE id=p.wallet_id;
        amount=p.amount_cents;
        IF t.kind='payout_reserve' THEN
            IF p.reserve_transaction_id IS DISTINCT FROM tx THEN RAISE EXCEPTION 'Reservation link mismatch' USING ERRCODE='23514'; END IF;
            a=w.reserved_account_id; b=w.available_account_id;
        ELSIF t.kind='payout_settle' THEN
            IF p.status<>'paid' OR p.settle_transaction_id IS DISTINCT FROM tx THEN RAISE EXCEPTION 'Settlement requires paid payout' USING ERRCODE='23514'; END IF;
            SELECT id INTO a FROM app.ledger_accounts WHERE purpose='platform_payout_clearing' AND currency=t.currency;
            b=w.reserved_account_id;
        ELSE
            IF p.status NOT IN ('rejected','cancelled') OR p.release_transaction_id IS DISTINCT FROM tx THEN RAISE EXCEPTION 'Release requires rejected/cancelled unpaid payout' USING ERRCODE='23514'; END IF;
            a=w.available_account_id; b=w.reserved_account_id;
        END IF;
    ELSIF t.kind='reversal' THEN
        IF EXISTS ((SELECT account_id,amount_cents FROM app.ledger_entries WHERE transaction_id=tx)
                   EXCEPT (SELECT account_id,-amount_cents FROM app.ledger_entries WHERE transaction_id=t.reverses_transaction_id)) OR
           EXISTS ((SELECT account_id,-amount_cents FROM app.ledger_entries WHERE transaction_id=t.reverses_transaction_id)
                   EXCEPT (SELECT account_id,amount_cents FROM app.ledger_entries WHERE transaction_id=tx)) THEN
            RAISE EXCEPTION 'Full reversal must exactly offset original entries' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT z.available_account_id,j.amount_cents INTO a,amount FROM app.wallet_adjustments j
            JOIN app.wallets z ON z.id=j.wallet_id WHERE j.ledger_transaction_id=tx AND j.status='posted'
            AND EXISTS (SELECT 1 FROM app.staff_roles r WHERE r.user_id=j.approved_by AND r.role='administrator' AND r.revoked_at IS NULL);
        SELECT id INTO b FROM app.ledger_accounts WHERE purpose='platform_rewards' AND currency=t.currency;
        IF a IS NULL OR amount IS NULL OR n<>2 OR
           NOT EXISTS (SELECT 1 FROM app.ledger_entries WHERE transaction_id=tx AND account_id=a AND amount_cents=amount) OR
           NOT EXISTS (SELECT 1 FROM app.ledger_entries WHERE transaction_id=tx AND account_id=b AND amount_cents=-amount) THEN
            RAISE EXCEPTION 'Adjustment requires a posted approved wallet adjustment' USING ERRCODE='23514';
        END IF;
    END IF;
    IF t.kind IN ('session_credit','payout_reserve','payout_settle','payout_release') AND
       (n<>2 OR a IS NULL OR b IS NULL OR amount IS NULL OR amount<=0 OR
        NOT EXISTS (SELECT 1 FROM app.ledger_entries WHERE transaction_id=tx AND account_id=a AND amount_cents=amount) OR
        NOT EXISTS (SELECT 1 FROM app.ledger_entries WHERE transaction_id=tx AND account_id=b AND amount_cents=-amount)) THEN
        RAISE EXCEPTION 'Journal entries do not match source amount and wallet' USING ERRCODE='23514';
    END IF;
END $$;
CREATE FUNCTION app.check_journal_trigger() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    IF TG_TABLE_NAME='ledger_entries' THEN PERFORM app.validate_journal(NEW.transaction_id);
    ELSE PERFORM app.validate_journal(NEW.id); END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER journal_complete AFTER INSERT OR UPDATE ON app.ledger_transactions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_journal_trigger();
CREATE CONSTRAINT TRIGGER journal_entries_complete AFTER INSERT ON app.ledger_entries
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_journal_trigger();

-- Trusted backend-only primitive. Source linkage is completed in the same transaction.
CREATE FUNCTION app.post_journal(p_kind text,p_key text,p_entries jsonb,
    p_session_id uuid DEFAULT NULL,p_payout_id uuid DEFAULT NULL,p_reverses_id uuid DEFAULT NULL)
RETURNS uuid LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE tx uuid; existing_hash text; request_hash text; wid uuid; item jsonb;
BEGIN
    IF jsonb_typeof(p_entries)<>'array' OR jsonb_array_length(p_entries)<2 THEN
        RAISE EXCEPTION 'Posting requires at least two entries' USING ERRCODE='23514';
    END IF;
    request_hash=encode(sha256(convert_to(jsonb_build_array(p_kind,p_entries,p_session_id,p_payout_id,p_reverses_id)::text,'UTF8')),'hex');
    PERFORM pg_advisory_xact_lock(hashtextextended('journal:'||p_key,0));
    SELECT id,t.request_hash INTO tx,existing_hash FROM app.ledger_transactions t WHERE idempotency_key=p_key;
    IF FOUND THEN
        IF existing_hash<>request_hash THEN RAISE EXCEPTION 'Idempotency key payload conflict' USING ERRCODE='23514'; END IF;
        RETURN tx;
    END IF;
    FOR wid IN SELECT id FROM app.wallets WHERE id IN (
        SELECT wallet_id FROM app.ledger_accounts WHERE id IN
        (SELECT (x->>'account_id')::uuid FROM jsonb_array_elements(p_entries) x)
    ) ORDER BY id FOR NO KEY UPDATE LOOP
        UPDATE app.wallets SET ledger_version=ledger_version+1 WHERE id=wid;
    END LOOP;
    INSERT INTO app.ledger_transactions(kind,idempotency_key,request_hash,session_id,payout_id,reverses_transaction_id)
        VALUES (p_kind,p_key,request_hash,p_session_id,p_payout_id,p_reverses_id) RETURNING id INTO tx;
    FOR item IN SELECT value FROM jsonb_array_elements(p_entries) LOOP
        INSERT INTO app.ledger_entries(transaction_id,account_id,amount_cents)
        VALUES (tx,(item->>'account_id')::uuid,(item->>'amount_cents')::bigint);
    END LOOP;
    UPDATE app.ledger_transactions SET posted_at=clock_timestamp() WHERE id=tx;
    RETURN tx;
END $$;

CREATE FUNCTION app.check_source_finances() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE p app.payout_requests; s app.collection_sessions; j app.wallet_adjustments;
BEGIN
    IF TG_TABLE_NAME='payout_requests' THEN
        SELECT * INTO p FROM app.payout_requests WHERE id=NEW.id;
        IF p.reserve_transaction_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM app.ledger_transactions WHERE id=p.reserve_transaction_id AND payout_id=p.id AND kind='payout_reserve'
        ) THEN RAISE EXCEPTION 'Payout and reservation must commit together' USING ERRCODE='23514'; END IF;
        PERFORM app.validate_journal(p.reserve_transaction_id);
        IF p.settle_transaction_id IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM app.ledger_transactions WHERE id=p.settle_transaction_id AND payout_id=p.id AND kind='payout_settle') THEN
                RAISE EXCEPTION 'Settlement link mismatch' USING ERRCODE='23514'; END IF;
            PERFORM app.validate_journal(p.settle_transaction_id);
        END IF;
        IF p.release_transaction_id IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM app.ledger_transactions WHERE id=p.release_transaction_id AND payout_id=p.id AND kind='payout_release') THEN
                RAISE EXCEPTION 'Release link mismatch' USING ERRCODE='23514'; END IF;
            PERFORM app.validate_journal(p.release_transaction_id);
        END IF;
    ELSIF TG_TABLE_NAME='collection_sessions' THEN
        SELECT * INTO s FROM app.collection_sessions WHERE id=NEW.id;
        IF s.credit_transaction_id IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM app.ledger_transactions WHERE id=s.credit_transaction_id AND session_id=s.id AND kind='session_credit') THEN
                RAISE EXCEPTION 'Session credit link mismatch' USING ERRCODE='23514'; END IF;
            PERFORM app.validate_journal(s.credit_transaction_id);
        ELSIF s.reconciliation_status='no_credit' THEN
            IF EXISTS (SELECT 1 FROM app.bottle_events WHERE session_id=s.id AND status IN ('pending','accepted')) OR
               NOT EXISTS (SELECT 1 FROM app.session_submissions WHERE session_id=s.id AND status='validated'
                   AND expected_attempt_count=s.rejected_count AND expected_attempt_count=(SELECT count(*) FROM app.bottle_events WHERE session_id=s.id)) THEN
                RAISE EXCEPTION 'Zero-credit session requires a complete validated manifest without accepted/pending bottles' USING ERRCODE='23514';
            END IF;
        END IF;
    ELSE
        SELECT * INTO j FROM app.wallet_adjustments WHERE id=NEW.id;
        IF j.ledger_transaction_id IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM app.ledger_transactions WHERE id=j.ledger_transaction_id AND kind='adjustment') THEN
                RAISE EXCEPTION 'Adjustment journal link mismatch' USING ERRCODE='23514'; END IF;
            PERFORM app.validate_journal(j.ledger_transaction_id);
        END IF;
    END IF; RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER payout_finances AFTER INSERT OR UPDATE ON app.payout_requests
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_source_finances();
CREATE CONSTRAINT TRIGGER session_finances AFTER INSERT OR UPDATE ON app.collection_sessions
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_source_finances();
CREATE CONSTRAINT TRIGGER adjustment_finances AFTER INSERT OR UPDATE ON app.wallet_adjustments
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.check_source_finances();

-- Views evaluate permissions as their caller, never as the migration owner.
CREATE VIEW app.wallet_balances WITH (security_invoker=true) AS
SELECT w.id AS wallet_id,w.user_id,w.currency,
    coalesce(sum(e.amount_cents) FILTER (WHERE e.account_id=w.available_account_id),0) AS available_cents,
    coalesce(sum(e.amount_cents) FILTER (WHERE e.account_id=w.reserved_account_id),0) AS reserved_cents
FROM app.wallets w LEFT JOIN app.ledger_entries e ON e.account_id IN (w.available_account_id,w.reserved_account_id)
GROUP BY w.id;
CREATE VIEW app.wallet_liabilities WITH (security_invoker=true) AS
SELECT currency,sum(available_cents) AS available_cents,sum(reserved_cents) AS reserved_cents,
    sum(available_cents+reserved_cents) AS total_liability_cents FROM app.wallet_balances GROUP BY currency;
CREATE VIEW app.session_credit_sla WITH (security_invoker=true) AS
SELECT id AS session_id,ended_at,credit_due_at,credit_posted_at,reconciliation_status,last_error_code,
    extract(epoch FROM (credit_posted_at-ended_at)) AS delay_seconds,
    reconciliation_status<>'no_credit' AND credit_due_at<coalesce(credit_posted_at,clock_timestamp()) AS overdue
FROM app.collection_sessions WHERE ended_at IS NOT NULL;
CREATE VIEW app.payout_processing_sla WITH (security_invoker=true) AS
SELECT id AS payout_id,requested_at,processing_due_at,paid_at,completed_at,status,
    CASE WHEN status IN ('cancelled','rejected') THEN false
         ELSE processing_due_at<coalesce(paid_at,clock_timestamp()) END AS overdue
FROM app.payout_requests;
CREATE VIEW app.machine_health WITH (security_invoker=true) AS
SELECT m.id AS machine_id,m.machine_code,m.status,m.operating_mode,m.last_heartbeat_at,
    extract(epoch FROM (clock_timestamp()-m.last_heartbeat_at)) AS heartbeat_age_seconds,
    (SELECT count(*) FROM app.machine_incidents i WHERE i.machine_id=m.id AND i.status<>'resolved') AS unresolved_incidents
FROM app.machines m;
CREATE VIEW app.registry_health WITH (security_invoker=true) AS
SELECT id AS sync_run_id,completed_at,total,
    extract(epoch FROM (clock_timestamp()-completed_at::timestamptz)) AS age_seconds
FROM recy.sync_runs ORDER BY id DESC LIMIT 1;

-- Deny direct Data API access; API-specific roles/policies are a later release.
DO $$ DECLARE r record; role_name text; BEGIN
    FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='app' LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY',r.tablename);
    END LOOP;
    FOR r IN SELECT unnest(ARRAY['ledger_entries','ledger_accounts','reward_rates','audit_events','payout_status_events','funding_snapshots']) AS name LOOP
        EXECUTE format('CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON app.%I FOR EACH ROW EXECUTE FUNCTION app.reject_mutation()',r.name);
    END LOOP;
    FOR r IN SELECT unnest(ARRAY['ledger_transactions','ledger_entries','ledger_accounts','reward_rates','audit_events','payout_status_events','funding_snapshots']) AS name LOOP
        EXECUTE format('CREATE TRIGGER prevent_truncate BEFORE TRUNCATE ON app.%I FOR EACH STATEMENT EXECUTE FUNCTION app.reject_mutation()',r.name);
    END LOOP;
    FOR r IN SELECT unnest(ARRAY['profiles','machines','wallets']) AS name LOOP
        EXECUTE format('CREATE TRIGGER touch_updated_at BEFORE UPDATE ON app.%I FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at()',r.name);
    END LOOP;
    FOREACH role_name IN ARRAY ARRAY['anon','authenticated','service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA app FROM %I',role_name);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA app FROM %I',role_name);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM %I',role_name);
        END IF;
    END LOOP;
END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA app FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- Seed only policy/platform accounts, after all DDL and deferred constraints exist.
INSERT INTO app.reward_rates(version,amount_per_bottle_cents,effective_from)
VALUES ('cad-7c-v1',7,now());
INSERT INTO app.ledger_accounts(purpose) VALUES ('platform_rewards'),('platform_payout_clearing');
