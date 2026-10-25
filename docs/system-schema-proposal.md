# User, machine, session, wallet, and payout schema proposal

Status: design for review. No tables, migrations, API behavior, or cloud configuration
are changed by this document.

The product registry and its four existing `recy` tables remain unchanged,
including the `Photo` column. Supabase Auth owns credentials and login sessions.
New business tables are proposed in a private `app` schema, separate from the
registry scraper. Application data is accessed through the backend.

## Agreed direction and open decisions

- Accepted bottles earn **CAD $0.07 per bottle** (`amount_per_bottle_cents = 7`).
  Store a versioned rate and freeze it when each collection session starts.
- Use CAD initially; represent money as integer cents, never floating point.
- The **machine alone** runs the 30-second inactivity timer and submits its final
  scan dictionary when no new bottle scan arrives. Receipt of that authenticated
  dictionary tells the backend that the session has ended; the backend records
  the end and logs the user out of that machine. There is no backend/database
  inactivity timer and no requirement to report every scan to maintain one.
  Every new bottle attempt resets this timer, including rejected attempts;
  heartbeats and HTTP retries do not. With no first scan, time starts at session start.
- Wallet credit is due **within 60 seconds of the backend recording the submitted
  session end**. Manual payout
  completion is due **within 24 hours of the payout request**; these are elapsed
  times, including weekends, with timestamps and overdue monitoring in the schema.
- Payout is a **form submission for manual back-office processing**. Submitting
  it immediately reduces the user's available balance and records the amount as
  pending payout. Staff send the Interac transfer outside the application and
  record its outcome. No payment-provider integration is required for this release.
- Proposed first release: online-only machines, one active collection session per
  machine, and one active machine session per user. Offline capture needs a
  separate protocol and is outside this initial design.
- The minimum payout is **CAD $5.00** (`500` cents). Both partial and full-balance
  withdrawals are allowed, subject only to this minimum and sufficient available
  funds. There are **no fees**: the requested amount equals the wallet debit and
  the amount staff must send. A balance below $5 stays available until it grows.
- Still to decide: SMS and SMTP services, supported physical container types,
  machine capture-sensor contract, retention periods, and recovery/alert execution
  hosting. These are implementation dependencies, not undecided reward/payout rules.

## Common conventions

- New entity IDs: UUID primary keys. Dates: `timestamptz`, stored in UTC.
- `created_at` is required on new entities; mutable entities also have `updated_at`.
- Foreign keys and checked status values are enforced by PostgreSQL.
- Amounts are `bigint` in cents. Counts are nonnegative integers. Currency is
  `char(3)`; CAD is the initial supported currency.
- UPCs are text, preserving leading zeroes. No uniqueness constraint on UPC in
  bottle events: many real bottles can legitimately have the same UPC.
- Authentication identities, collection sessions, and individual bottle events
  have different IDs and different purposes.

## Authentication and profiles

**Supabase-managed `auth.users` and associated Auth tables**

Supabase owns user IDs, email/phone login identities, password hashes,
confirmations, access tokens, refresh tokens, recovery, and logout. Do not create
an application password column or store copies of passwords/tokens in scan logs.

**`app.profiles`**

| Field | Type / rule |
| --- | --- |
| `id` | UUID PK; the permanent application user ID |
| `auth_user_id` | Nullable UUID, unique FK to `auth.users.id`, `ON DELETE SET NULL` |
| `first_name`, `last_name` | Nonempty text |
| `status` | `active`, `suspended`, `closed` |
| `created_at`, `updated_at`, `closed_at` | Timestamps; `closed_at` nullable |

All business `user_id` fields reference `profiles.id`. `auth_user_id` maps a
verified Auth JWT subject to that application user. A stable application ID lets
financial records survive an Auth-account deletion. Account closure disables
access and anonymizes personal profile data under an agreed retention policy;
it does not cascade-delete ledger entries. `full_name` is derived from first and
last name rather than independently maintained.

Email and phone identities live in Supabase Auth and are read through its trusted
APIs. Avoid another unsynchronized login directory in `profiles`. Signup accepts
first name, last name, email, phone, and password; the flow must associate and
verify both contact methods on the **same Auth user**, rather than creating an
email account and a separate phone account. A phone in editable user metadata
does not establish a verified phone login identity.

Email/password and phone/password login are supported by Supabase. Email
confirmation can prevent immediate login after signup. Phone verification needs
an SMS provider. Contact verification is not payout authorization.

**`app.machine_logins`**

`id`, `user_id` FK, `machine_id` FK, `supabase_auth_session_id` (UUID identifier,
not a token), `started_at`, `backend_closed_at`,
`logout_status` (`active`, `revocation_pending`, `revoked`, `needs_review`),
`revocation_requested_at`, `revoked_at`, `logout_reason`.
Include an immutable server-issued `access_scope = machine_collection_only` for
machine logins. These login bindings cannot authorize wallet payouts or profile
changes; such actions require a separate web/app login. This restriction must
not come from user-editable JWT metadata.

Associate each collection session with its machine login through a composite
ownership FK. When the backend receives and durably records the authenticated
final dictionary, close the machine login and revoke the **machine's local Auth
session**. The submission response instructs the kiosk to clear tokens and
displayed personal details. While waiting/retrying submission, the machine locks
the finished collection screen against further user actions. Other browser/phone
sessions remain signed in. A failed network logout
is recorded for retry and cannot reopen the kiosk session. Backend authorization
checks the closed login/session binding so an unexpired JWT cannot keep using it.
Do not store passwords, access tokens, or refresh tokens in this table. Revoke
through supported Auth APIs rather than directly editing Supabase's managed tables.

Supabase's default signout scope is global and already-issued JWTs can remain
valid until expiry. Implementation must explicitly select local signout and
enforce this application-level closure; clearing the UI alone is insufficient.
Machine-authenticated final submissions and retries remain allowed after user
logout, but only for the bound closed session's sealed event manifest.

**`app.account_change_requests`**

`id`, `user_id` FK, `kind` (`email_change`, `phone_change`, `account_recovery`),
`status` (`pending_verification`, `verified`, `applied`, `rejected`, `expired`),
`auth_reference`, `requested_at`, `verified_at`, `expires_at`, `completed_at`,
`reviewed_by` (nullable staff FK). Track application recovery/review workflows
without copying passwords, reset tokens, OTPs, or superseded login directories.
Supabase remains authoritative for identity changes and password recovery.
Email/phone changes require proof through the Auth flow or a separately approved
manual recovery process. User-supplied metadata cannot verify a contact change.
SMTP and SMS delivery must be configured and monitored before public signup.

## Machines and collection sessions

**`app.machines`**

`id`, `machine_code` (unique text), `name`, `location` (text), `status`
(`active`, `maintenance`, `disabled`), `last_seen_at`, `created_at`, `updated_at`.

Add `operating_mode` (`live`, `test`), `firmware_version`,
`capture_protocol_version`, `last_boot_id`, `last_heartbeat_at`,
`last_clock_sync_at`, and `clock_offset_ms`. Only live-mode, enabled machines can
create creditable sessions; diagnostics cannot enter the production reward ledger.

**`app.machine_incidents`**

`id`, `machine_id` FK, `session_id` (nullable FK), `bottle_event_id` (nullable FK),
`kind` (`jam`, `capture_uncertain`, `restart`, `network_loss`, `tamper`, `clock_skew`),
`status` (`open`, `investigating`, `resolved`), `opened_at`, `resolved_at`,
`assigned_to`, `resolution_code`, `redacted_details`. Record hardware/recovery
evidence separately from eligibility and preserve any linked reward dispute.

**`app.machine_credentials`**

`id`, `machine_id` FK, `key_prefix`, `secret_hash`, `created_at`, `expires_at`,
`revoked_at`, `last_used_at`. Generate a high-entropy secret for each machine;
store only its verifier/hash. Credential rotation does not change machine identity.
The existing shared registry API token should not become the fleet's machine
identity. Machine IDs are obtained from verified credentials, not trusted request fields.

**`app.reward_rates`**

`id`, `version` (unique), `currency`, `amount_per_bottle_cents` (> 0),
`effective_from`, `effective_until` (nullable), `created_at`.

Prevent overlapping effective periods per currency. Referenced rates are
immutable: a price change creates a new version. This is a business reward rule,
not an assumption that every source container has the same deposit refund.

**`app.collection_sessions`**

| Field | Purpose |
| --- | --- |
| `id` | Collection session UUID returned to the machine |
| `user_id`, `machine_id` | FKs identifying the owner and assigned machine |
| `machine_login_id` | FK to the bound machine login |
| `collection_status` | `active`, `submitted`, `cancelled`; changes on explicit submitted commands |
| `reconciliation_status` | `pending`, `processing`, `posted`, `no_credit`, `needs_review` |
| `reward_rate_id` | FK to the selected immutable rate |
| `currency`, `reward_per_bottle_cents` | Frozen rate snapshot |
| `accepted_count`, `rejected_count` | Server-derived final totals; null until finalized |
| `reward_total_cents` | Server-derived final reward; null until finalized |
| `credit_transaction_id` | Nullable unique FK to the ledger transaction |
| `last_scan_at` | Optional machine-reported evidence; not a backend timer input |
| `end_reason` | `inactivity`, `machine_fault`, `restart`, `admin_recovery` |
| `machine_end_event_id`, `machine_ended_at` | Device end event ID and claimed end timestamp |
| `ended_at`, `end_received_at` | Backend-recorded end and first durable receipt of the final dictionary |
| `timing_quality` | `verified`, `estimated`, `needs_review`; distinguish device clock claims |
| `credit_due_at`, `credit_posted_at` | End time + 60 seconds, and actual credit posting time |
| `reconciliation_attempts`, `next_reconciliation_at`, `last_error_code` | Durable retry/recovery tracking |
| `started_at`, `submitted_at`, `reconciled_at` | Lifecycle timestamps |

Use partial unique indexes for one `active` session per machine and per user.
Use a unique `(id, user_id, machine_id)` constraint to support scan ownership
foreign keys. Submitted sessions cannot acquire new bottle events. A posted
session requires `credit_transaction_id` and `reconciled_at`. A zero-bottle session
can be `no_credit` with no zero-value ledger transaction. This is more precise
than a standalone `is_reconciled` flag.

A cancellation or machine interruption does not erase confirmed captured bottles. Such a
session must be finalized from recorded evidence or sent to review before its
financial outcome is settled. The firmware uses its own monotonic timer. Optional
machine timestamps describe physical activity; they neither trigger backend
closure nor determine the wallet credit deadline. Keep `machine_ended_at` separate
from `ended_at` so transmission delays remain visible.

At the machine's 30-second deadline, stop new intake, seal the scan sequence,
and submit the dictionary. Already-started captures may report their final
outcome under their original attempt IDs. Before this submission reaches the
backend, the database still records the session as active. On first durable
receipt, atomically record `submitted_at`/`ended_at`, mark the session submitted,
close its machine-login authorization, and enqueue reconciliation/logout work.
Supabase signout then completes; wallet reconciliation proceeds independently.
Submission retries preserve the original end time, credit deadline and result.
A new session cannot take over while the previous session controls in-flight
hardware or has not successfully handed off its final dictionary.

A recovery runner retries work after a recorded submission. If a machine loses
connectivity before submitting, flag an incident and request replay of its final
dictionary; do not invent an inactivity end, auto-log out through a backend
30-second timer, or credit from a heartbeat gap. Startup checks detect unresolved
sessions before enabling intake. Staff can investigate orphaned sessions through
an explicit audited recovery procedure, not a silent timer-based close.

**`app.bottle_events`**

| Field | Purpose |
| --- | --- |
| `id` | Server UUID for this physical bottle attempt |
| `machine_event_id` | Machine-generated UUID, reused on retries |
| `session_id`, `user_id`, `machine_id` | Composite FK to the same collection session |
| `sequence_no` | Monotonically increasing attempt number within the session |
| `upc` | Normalized text identifier; may repeat for different bottles |
| `eligibility_status` | `eligible`, `ineligible`, `unavailable` |
| `status` | `pending`, `accepted`, `rejected` |
| `reason_code` | E.g. `registered`, `not_registered`, `capture_failed`, `registry_unavailable` |
| `registry_sync_run_id` | Nullable FK to `recy.sync_runs.id`, recording lookup provenance |
| `capture_event_id` | Nullable device-generated evidence ID for physical acceptance |
| `captured_at`, `observed_at`, `received_at`, `decided_at` | Device and server timestamps |
| `reward_cents` | Server-assigned frozen rate for accepted captures, zero otherwise |
| `machine_boot_id`, `scan_monotonic_ms` | Device restart/elapsed-time evidence |
| `payload_hash`, `capture_evidence_hash` | Retry consistency and capture-evidence integrity |
| `backend_acknowledged_at` | Time the backend durably accepted the event |

Unique `(machine_id, machine_event_id)` and `(session_id, sequence_no)` prevent
retries from becoming extra bottle credits. A capture evidence ID is unique per
machine when present. The same UPC can appear dozens of times with distinct
physical event IDs. Receiving the same event ID with different data is a conflict.

The registry check establishes eligibility; **credit requires confirmed physical
capture**. A registered UPC followed by a jam or returned bottle is not credited.
Registry unavailability is distinct from an unregistered product and cannot
produce an accepted event. Device proof is only as trustworthy as the device;
unique IDs alone cannot prevent a compromised machine from inventing events.

The device must keep a durable local journal, writing an attempt ID before
processing it and persisting the capture outcome before acknowledging success.
Retain unacknowledged records across network loss/restart and replay their
original IDs. Mark an event acknowledged only after the backend transaction
commits. A barcode left in view cannot create new attempts without a new physical
intake cycle; enforce this in the machine controller. Jams and uncertain capture
evidence create incidents/review cases rather than guessed credit/rejection.
Define the accepted material/container and sensor rules before implementation;
UPC registration alone does not validate the machine's physical compatibility.

Do not FK `upc` to `registered_products`: source UPCs can repeat and the scraper
replaces product rows. Keep the original verdict and source-run provenance for
historical accounting even when the registry later changes.

**`app.session_submissions`**

`id`, `session_id` (unique FK), `machine_id`, `idempotency_key`, `request_hash`,
`claimed_accepted_count`, `claimed_rejected_count`, `final_sequence_no`, `status`
(`received`, `validated`, `needs_review`), `received_at`, `validated_at`,
`machine_end_event_id`, `end_reason`, `machine_ended_at`, `last_scan_at`,
`idle_duration_ms` (optional machine diagnostic), `expected_attempt_count`,
`manifest_hash`, `acknowledged_at`.

The final dictionary has the shape below; the server derives ownership from the
bound session rather than trusting additional `user_id` fields:

```json
{
  "session_id": "collection-session-uuid",
  "machine_end_event_id": "stable-device-end-event-uuid",
  "end_reason": "inactivity",
  "machine_ended_at": "2026-10-25T21:00:00Z",
  "idle_duration_ms": 30000,
  "final_sequence_no": 2,
  "bottles": [
    {"machine_event_id": "attempt-1-uuid", "sequence_no": 1, "upc": "080696922510", "status": "accepted", "capture_event_id": "capture-1-uuid"},
    {"machine_event_id": "attempt-2-uuid", "sequence_no": 2, "upc": "000000000000", "status": "rejected", "reason_code": "not_registered"}
  ]
}
```

The actual API contract must include the previously issued eligibility verdict
reference and physical capture evidence. Client status is a claim checked against
these records, not authority to create wallet credit. A manifest seals attempt
IDs/order; a pending capture may finalize under its original attempt ID, but
conflicting final verdicts or changed payloads require review.

The machine submits a session manifest identifying the event sequence and claimed
totals. The server calculates its own totals from `bottle_events`. Missing
sequence numbers, pending attempts, or mismatching counts block credit and send
the submission to review. The submission endpoint can replay missing attempts
using their original IDs; it cannot simply assert a larger accepted count.
Use a composite FK to enforce the submission's session/machine pairing.

Allow replay of original, pre-end attempts in the sealed manifest after logout,
including attempts whose first network delivery was lost. Do not accept newly
invented post-end attempts. An identical submission returns the original result;
conflicting retries return a conflict and preserve the first manifest.

**`app.machine_sync_batches`**

`id`, `machine_id`, `machine_boot_id`, `session_id`, `device_batch_id`,
`payload_hash`, `first_sequence_no`, `last_sequence_no`, `status`
(`received`, `applied`, `conflict`, `needs_review`), `missing_sequence_numbers`,
`received_at`, `applied_at`, `acknowledged_at`. Unique `(machine_id, device_batch_id)`.
Track replay receipt/acknowledgement and missing events without using raw request
logs as the financial record. This supports interruption recovery; it does not
authorize new intake while the machine is disconnected.

## Wallet and immutable ledger

Use a small balanced journal rather than treating a mutable balance column as
the record of money owed. Credits, reservations, settlements, and corrections
remain auditable.

**`app.wallets`**

`id`, `user_id` FK, `currency`, `status` (`active`, `frozen`, `closed`),
`available_account_id`, `reserved_account_id`, `created_at`, `updated_at`.
Unique `(user_id, currency)`. The two account FKs must belong to that wallet and
currency, with the corresponding account purposes. Wallets contain no editable
authoritative `balance` field.

**`app.ledger_accounts`**

`id`, `wallet_id` (nullable FK), `currency`, `purpose`, `created_at`.
Purposes: `user_available`, `user_reserved`, `platform_rewards`,
`platform_payout_clearing`. Wallet accounts have a wallet; platform accounts do
not. Create exactly one account per wallet/purpose and one platform account per
currency/purpose. Insert a wallet and its accounts atomically, using deferred
ownership constraints where needed for their reciprocal references.

**`app.ledger_transactions`**

`id`, `kind`, `currency`, `session_id` (nullable FK), `payout_id` (nullable FK),
`reverses_transaction_id` (nullable FK), `idempotency_key` (unique), `created_at`.
Kinds: `session_credit`, `payout_reserve`, `payout_settle`, `payout_release`,
`adjustment`, `reversal`. Reference the appropriate source for each kind.
Unique session credit per session; unique reservation, settlement, and release
per payout. Unique reversal per original transaction for a full-reversal model.

**`app.ledger_entries`**

`id`, `transaction_id` FK, `account_id` FK, `amount_cents` (signed, nonzero),
`created_at`. For every posted transaction: at least two entries, matching
currency, and a sum of zero. Enforce these with a controlled posting function
and deferred database checks. Ordinary API roles cannot insert/update journal
rows directly. Posted entries are never edited or deleted; corrections append
linked reversing transactions.

**`app.wallet_balances` view**

`wallet_id`, `user_id`, `currency`, `available_cents`, `reserved_cents` are derived
from journal entries. Lock the wallet row before any posting that can reduce its
available funds. Check the ledger-derived available balance while holding the
lock, so simultaneous payout requests cannot spend the same funds.
Controlled posting must keep both user available and reserved balances nonnegative.

Example movements using the agreed 7-cent rate and $5 payout minimum:

| Event | Balanced entries |
| --- | --- |
| 100 bottles reconciled | User available +700; platform rewards -700 |
| Request $5 payout | User available -500; user reserved +500; $2 stays available |
| Staff record completed payment | User reserved -500; platform payout clearing +500 |
| Unpaid request rejected/cancelled | User reserved -500; user available +500 |

A full withdrawal of $7 from the same balance is also valid. Reconciliation must
post the seven-cent reward exactly once within 60 seconds of `ended_at` under
normal operation. Reconciliation jobs are durable and idempotent; a timeout or
server restart must resume the work, not duplicate or abandon the credit.
Evidence disputes and service outages after submission may breach the deadline;
record and alert on the breach instead of fabricating acceptance or moving the
deadline. Transmission time before first receipt is measured separately.

The settlement entry records the application's payout liability movement;
staff reconcile completed payments against external bank statements. This
wallet journal is not a complete company general ledger.

## Manual Interac payout lifecycle

The web app submits the amount and Interac email address. The backend takes
`user_id` from the authenticated user, verifies the requested amount against the
available balance, and creates a pending request. The displayed spendable balance
decreases immediately; pending payouts are displayed separately. The reservation
keeps a complete record of money still owed while preventing another withdrawal
of the same funds.

**`app.staff_roles`**

`user_id` FK, `role` (`payout_processor`, `administrator`), `granted_by` FK,
`granted_at`, `revoked_at`. Unique active `(user_id, role)`. Only a trusted
administrative operation grants roles; user-editable Auth metadata cannot do so.
Back-office actions require an active staff role and are audited.

**`app.payout_requests`**

| Field | Purpose |
| --- | --- |
| `id`, `user_id`, `wallet_id` | Identity and ownership FKs |
| `destination_email` | Interac email entered on the form; frozen for this request |
| `currency`, `amount_cents` | CAD; amount at least `500` cents and no greater than available funds |
| `fee_cents`, `wallet_debit_cents`, `recipient_amount_cents` | Checked: fee = `0`; debit = recipient amount = requested amount |
| `status` | `pending`, `processing`, `paid`, `rejected`, `cancelled`, `needs_review` |
| `request_idempotency_key`, `request_hash` | Client retry protection, unique per user/key |
| `reserve_transaction_id`, `settle_transaction_id`, `release_transaction_id` | Nullable unique journal FKs |
| `assigned_to`, `processed_by` | Nullable staff profile FKs |
| `payment_reference`, `paid_at` | Required when staff mark a request paid |
| `rejection_reason`, `staff_note` | Reason for rejection or processing notes |
| `requested_at`, `processing_started_at`, `completed_at` | Lifecycle timestamps |
| `processing_due_at`, `overdue_notified_at` | Request time + 24 hours; escalation tracking |

Enforce matching user/currency for the wallet with a composite ownership FK.
Store the request and its reservation journal transaction atomically while
holding the wallet lock. If either write fails, neither is committed. Matching
retries return the same payout request without a second balance deduction.
The amount and destination cannot be edited after submission: cancel an unpaid
pending request and submit a new one to correct them.
Partial and full-balance payouts are both valid. Do not require the remaining
balance to meet the payout minimum: withdrawing $5 from $7 leaves $2. Multiple
requests are allowed when separately covered by remaining available funds, with
no arbitrary count restriction. Amounts must have at most two decimal places;
validate and convert to integer cents without floating-point rounding.

Staff claim a pending request with an atomic `pending → processing` transition,
so two operators cannot claim it simultaneously. The assigned operator sends the
transfer manually, then records a payment reference and marks it paid. This
settles the pending amount; it **does not deduct the available balance again**.

Users can cancel only while a request is pending. Staff can reject an unpaid
request with a reason. Rejection/cancellation releases the reserved amount back
to the available balance exactly once. A processing request can be rejected only
after staff establish that no payment was sent. If the transfer outcome is
uncertain, set `needs_review` and retain the reservation until it is resolved.
Never automatically cancel a processing request or assign it to a second operator
after a timeout: the original operator may already have sent the money.

State transitions under a payout-row lock prevent both settlement and release.
A later confirmed return of an already paid transfer requires a new linked
reversal workflow, rather than editing the original journal entries. Manual
bank transfers themselves are outside the database transaction; staff must check
bank records before resending when an outcome is unclear.
The 24-hour deadline is for completing payment, not merely claiming a request.
`paid_at` records the operator's claimed actual transfer time; `completed_at`
records the server-side status update. Both are audited and checked against bank
evidence so a backdated claim cannot silently conceal a missed target. Do not reset
the deadline on reassignment, review, or retries. Overdue requests alert staff;
they are not automatically paid, cancelled, or refunded. Record rejected/cancelled
resolution separately from successful payout-time metrics.

**`app.payout_status_events`**

`id`, `payout_id` FK, `from_status` (nullable for creation), `to_status`,
`actor_user_id` FK, `reason`, `request_id`, `created_at`. Append-only record of
submission, staff assignment, completion, cancellation, and rejection. Write each
status event in the same transaction as its payout and ledger changes.

**`app.idempotency_requests`**

`id`, `principal_type`, `principal_id`, `operation`, `key`, `request_hash`,
`resource_type`, `resource_id`, `created_at`, `expires_at`.
Unique `(principal_type, principal_id, operation, key)`. Use for session creation
and other retriable writes. Store resource references, not cached Auth responses
or refresh tokens. Matching retries return the existing resource; different
payloads with the same key return a conflict. Source-record and ledger unique
constraints remain authoritative after an idempotency record expires.

**`app.audit_events`**

`id`, `actor_type`, `actor_id`, `action`, `resource_type`, `resource_id`,
`request_id`, `created_at`, `redacted_details` (JSONB). Append-only audit trail
for machine credential changes, session decisions, rate changes, payout
destination submissions, and financial/admin operations. Do not log
passwords, refresh tokens, or raw machine secrets.

## Recovery, customer support, and operational monitoring

**`app.work_jobs`**

`id`, `kind` (`session_reconcile`, `machine_auth_revoke`, `submission_recover`,
`notification_send`, `bank_reconcile`), `resource_type`, `resource_id`,
`deduplication_key` (unique), `status` (`pending`, `running`, `retry`, `completed`,
`needs_review`), `priority`, `available_at`, `deadline_at`, `locked_until`,
`attempts`, `last_error_code`, `created_at`, `completed_at`.
Insert reconciliation work atomically with the final submission. A leased worker
can recover work after a crash. An immediate processing attempt can meet normal
latency, but must leave a durable retry job on failure. A persistent runner must
check jobs frequently enough to meet the post-submission 60-second target,
detect missing final submissions for investigation/replay, and escalate overdue
payouts. It does not execute the machine's inactivity timer. The existing daily GitHub scraper
schedule cannot provide this latency. Render Free cold starts/outages and network
loss mean a strict unconditional one-minute guarantee is not achievable on the
current hosting alone; an always-available execution option must be agreed before
promising the target as a guaranteed service level.

**`app.support_cases`**

`id`, `user_id` FK, `session_id`, `bottle_event_id`, `payout_id` (nullable FKs),
`category` (`missing_credit`, `bottle_dispute`, `payout_problem`, `account_recovery`),
`status` (`open`, `investigating`, `resolved`, `closed`), `description`,
`assigned_to`, `resolution_code`, `adjustment_transaction_id` (nullable unique FK),
`created_at`, `resolved_at`, `closed_at`. Verify ownership of every linked record.
The machine's session/reference number lets a user raise a missing-credit case
after logout; it is a reference, not permission to view another user's records.

**`app.wallet_adjustments`**

`id`, `wallet_id`, `support_case_id` (nullable), `amount_cents` (signed, nonzero),
`reason_code`, `requested_by`, `approved_by`, `status`
(`proposed`, `approved`, `posted`, `rejected`, `needs_review`),
`ledger_transaction_id` (nullable unique), `created_at`, `approved_at`, `posted_at`.
Require an authorized reason and audit trail. Debit adjustments cannot overdraw
available funds; disputed credits already spent require review/freeze rather
than silently creating negative balances. Reversals link the original posting;
they never rewrite bottle events or paid payout history.

**`app.operational_alerts`**

`id`, `kind` (`credit_overdue`, `payout_overdue`, `machine_offline`, `logout_failed`,
`registry_stale`, `unreconciled_capture`, `ledger_mismatch`, `funding_shortfall`),
`resource_type`, `resource_id`, `deduplication_key`, `severity`, `status`
(`open`, `acknowledged`, `resolved`), `first_seen_at`, `last_seen_at`,
`acknowledged_by`, `resolved_at`, `redacted_details`. Unique active deduplication
keys prevent repeated alerts for the same incident. Acknowledging an alert does
not resolve the underlying debt or change its deadline.

**`app.notifications`**

`id`, `user_id` (nullable), `staff_user_id` (nullable), `kind`, `resource_type`,
`resource_id`, `channel` (`in_app`, `email`, `sms`), `status`
(`pending`, `sent`, `failed`), `deduplication_key` (unique), `attempts`,
`created_at`, `sent_at`, `last_error_code`. Notify users about session credits,
payout requests/outcomes and staff about overdue work; SMTP/SMS are external
delivery dependencies. A failed notification does not roll back a wallet credit.

**`app.bank_reconciliation_runs`** and **`app.bank_statement_items`**

Runs: `id`, `bank_source_reference`, `period_start`, `period_end`, `status`,
`imported_by`, `reviewed_by`, `created_at`, `completed_at`.
Items: `id`, `run_id`, `external_reference`, `import_fingerprint`,
`occurred_at`, `amount_cents`, `currency`, `direction` (`in`, `out`), `payout_id`
(nullable), `match_status` (`unmatched`, `matched`, `disputed`), `matched_by`,
`matched_at`. Deduplicate statement imports using account/source and external
reference or a stable import fingerprint. Support reviewed multiple-item matches
when the bank reports grouped payments rather than silently forcing one-to-one.
Do not store online banking credentials. Matching a statement item cannot issue
another wallet debit. Keep manual payment evidence separate from staff-entered
claims that a payout has been sent.

**`app.funding_snapshots`** and **`app.recovery_checks`**

Funding snapshots: `id`, `currency`, `as_of`, `available_liability_cents`,
`reserved_liability_cents`, `verified_cash_available_cents`, `source_reference`,
`recorded_by`, `created_at`. Distinguish seven-cent reward liabilities from
recycling proceeds actually received; flag a funding shortfall without editing
earned rewards. A recorded cash snapshot is not a live bank balance guarantee.
Recovery checks: `id`, `kind` (`backup`, `restore_drill`, `post_restore_reconcile`),
`started_at`, `completed_at`, `status`, `artifact_reference`, `reviewed_by`,
`notes`. After restore, reconcile external transfers and unacknowledged machine
records before resuming payouts, preventing duplicate transfers or lost credits.
Backups need separate execution and protected storage; an audit row is evidence
of a check, not a backup itself. Set PII, device evidence, and financial retention
periods and an account-closure process before launch; do not cascade-delete
financial records with an Auth account.

**Operational views and indexes**

- `session_credit_sla`: session ID, `ended_at`, `credit_due_at`, `credit_posted_at`,
  calculated delay/overdue flag and review reason. Zero-bottle `no_credit` sessions
  are finalizations, not missing credit postings.
- `payout_processing_sla`: request ID, `requested_at`, `processing_due_at`,
  `paid_at`, `completed_at`, current status and calculated overdue flag.
- `wallet_liabilities`: available and reserved amounts by currency plus their
  total. Reservations move funds between buckets and must not double-count debt.
- `machine_health`: last heartbeat, operational status and unresolved incidents;
  `registry_health`: last published registry and its age/configured freshness limit.
- Index active sessions by machine/user, unresolved jobs by priority/available time,
  unreconciled sessions by credit deadline, unpaid payouts by deadline, and wallet
  entries by account/transaction. Overdue flags are time-based views, not booleans
  that require a write to become true.

## End-to-end behavior to implement after schema approval

1. Signup creates one Supabase Auth identity and an application profile. Contact
   verification/linking finishes the account setup. Wallet provisioning is
   transactional/idempotent and must finish before starting a collection session.
2. Login returns Auth tokens and `user: {id, first_name, last_name, full_name}`.
   Browser login does not automatically create a machine session.
3. An authenticated user starts a session on an authenticated active machine.
   Return `collection_session_id`, `full_name`, currency, and the frozen reward.
   Machine login may orchestrate steps 2 and 3 into one response. Supabase's Auth
   session ID and the recycling collection session ID are never interchangeable.
4. Each machine attempt is validated against its assigned active session. The
   backend checks the UPC and records eligibility; the machine's physical capture
   acknowledgement finalizes accepted/rejected status using the same attempt ID.
5. The machine independently detects 30 seconds without a new attempt, stops
   intake, seals the session and submits its dictionary under machine
   authentication. Receipt of that dictionary is the backend's end-of-session
   signal. The backend durably records the end, closes the machine login and
   performs local-scope logout. Set `credit_due_at = ended_at + 60 seconds` using
   the original backend receipt/end time. Retries do not require the closed user
   login. Validate sequence coverage, ownership and capture evidence and calculate
   totals on the server; do not run or validate a backend inactivity timer.
6. One transaction locks the session and wallet, credits the accepted-bottle
   reward exactly once, records the credit transaction ID, and marks the session
   reconciled. Retries return the same result. Zero-bottle sessions finalize
   without a zero-value journal posting. An immediate attempt and durable recovery
   work aim to complete within one minute; unresolved evidence or service failures
   are recorded as overdue incidents rather than silently omitted credits.
7. The signed-in web user requests a payout. The backend verifies ownership,
   email format, amount (at least CAD $5), and available funds, then creates a
   pending request and reduces the available balance through a reservation in
   one transaction. Partial or full withdrawals incur no fee. Set
   `processing_due_at = requested_at + 24 hours`.
8. Authorized back-office staff claim the request, manually send the Interac
   transfer, and record completion and its payment reference. Unpaid rejected or
   cancelled requests restore available funds exactly once. The completion target
   is within 24 elapsed hours, and overdue requests remain visible and escalated.

Proposed API surface for later review:

| Endpoint | Actor / purpose |
| --- | --- |
| `POST /auth/signup` | Register profile and begin contact verification |
| `POST /auth/login` | Email-or-phone plus password; return full name and Auth tokens |
| `POST /auth/refresh`, `/auth/logout` | Supabase session lifecycle |
| `GET /me` | User's own profile |
| `POST /collection-sessions` | Authenticated user starts a session on an authenticated machine |
| `POST /collection-sessions/{id}/bottles` | Assigned machine creates an idempotent attempt/verdict |
| `POST /collection-sessions/{id}/bottles/{event_id}/capture` | Assigned machine confirms physical capture outcome |
| `POST /collection-sessions/{id}/submit` | Assigned machine submits the final manifest |
| `POST /machines/heartbeat` | Machine reports health; does not reset the scan inactivity timer |
| `POST /machines/sync-batches` | Machine replays durable pre-end events after interruption |
| `GET /collection-sessions/{id}` | Owner or authorized operator reads results |
| `GET /wallet`, `/wallet/transactions` | Owner reads balance and history |
| `POST /payouts`, `GET /payouts` | Owner submits amount/email or reads history |
| `POST /payouts/{id}/cancel` | Owner cancels an unpaid pending request |
| `GET /admin/payouts` | Authorized staff list requests awaiting processing |
| `POST /admin/payouts/{id}/claim` | Staff atomically claim a pending request |
| `POST /admin/payouts/{id}/complete` | Assigned staff record paid status and payment reference |
| `POST /admin/payouts/{id}/reject` | Staff reject a confirmed unpaid request and restore funds |
| `POST /admin/payouts/{id}/review` | Staff flag an uncertain outcome for investigation |
| `POST /support-cases`, `GET /support-cases` | User reports missing credit or payout problems |
| `GET /admin/support-cases`, `POST /admin/wallet-adjustments` | Staff investigate and make authorized journal corrections |
| `GET /admin/operations` | Staff see credit/payout deadlines, machine health and funding reports |

## Access and accounting boundaries

Validate Supabase JWTs on user routes. Resolve the user from the verified token;
never trust a supplied `user_id` or allow a user-editable metadata role to grant
admin access. Use separate, revocable machine authentication on machine routes.
A session UUID alone is not authorization. Public signup/login routes need rate
limiting; payout submissions and staff processing should require recent authentication.
Only the machine-bound login is closed on collection end; independent web/app
logins remain available for reviewing credits and requesting payouts. Closed
machine logins cannot authorize new attempts, and machine authentication alone
does not allow withdrawals. Final submissions are limited to the bound session's
pre-end manifest and remain retryable after logout.

Keep `app` private alongside private `recy`. Use narrowly scoped backend database
roles and controlled posting functions for financial mutations. Direct frontend
access, if introduced later, requires explicit grants and owner-scoped RLS;
private schemas alone do not restrict a privileged backend connection. Users can
read only their own profile/sessions/wallet/payouts; machines can write only their
assigned sessions. Administrative adjustments require a separate authorized role
and audit event. No user or machine can write wallet balances or journal entries.

## References checked for this proposal

- [Supabase password authentication](https://supabase.com/docs/guides/auth/passwords)
  supports email/password and phone/password, and documents phone-provider setup.
- [Supabase user management](https://supabase.com/docs/guides/auth/managing-user-data)
  documents references to the managed Auth primary key and separate profile data.
- [Supabase users](https://supabase.com/docs/guides/auth/users) documents verified
  identities and warns against trusting user-editable metadata for authorization.
- [Supabase signout scopes](https://supabase.com/docs/guides/auth/signout) documents
  local versus global logout and the expiry behavior of previously issued JWTs.
- Interac transfers are handled manually outside the application in this design;
  payment references are entered by authorized back-office staff.
