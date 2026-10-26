# Application API

All paths below use `/api/v1`. The production entrypoint is
`recy.wsgi:application`; the legacy `recy serve` development command serves only
the registry API. Discover routes at `GET /api/v1/openapi.json`.

Amounts are integer CAD cents. Each accepted physical bottle earns **7 cents**.
The [portal and operations guide](portal.md) documents the bundled frontend,
HttpOnly browser sessions, deposit-review resolution, fill telemetry and evacuation
APIs introduced in migration 004. `/me` additionally returns active staff roles.
UPCs are strings so leading zeroes survive. IDs are UUIDs; timestamps use ISO 8601
with an explicit timezone. JSON request bodies require `Content-Type:
application/json`. Errors include `error`, `code` and `request_id`.

## Authentication and account management

The backend uses Supabase Auth with `SUPABASE_URL` and
`SUPABASE_PUBLISHABLE_KEY`. It verifies bearer tokens through Supabase, checks that
the corresponding `auth.sessions` row remains live, and checks application account
status on each authenticated request. Passwords and raw tokens are never stored
in application tables. App roles come from `app.staff_roles`, never user metadata.

| Method | Path | JSON input / behavior |
| --- | --- | --- |
| POST | `/auth/signup` | `first_name`, `last_name`, `email`, `phone` (E.164), `password` (8–1024 characters). Returns 202 and verification instructions. |
| POST | `/auth/login` | `identifier` (email or verified phone), `password`; `email` or `phone` may substitute for `identifier`. Returns tokens and `user.full_name` for a web login. |
| POST | `/auth/refresh` | `refresh_token`; returns replacement tokens and user. |
| POST | `/auth/logout` | Bearer token; revokes only that Supabase session. |
| POST | `/auth/recover` | `email`; generic recovery response. |
| POST | `/auth/verify` | `type` (`email`, `recovery` or `phone_change`), `token`, and `email` or `phone` as appropriate. Phone change also requires the existing user's bearer token and recent login. |
| POST | `/auth/phone` | Recent bearer login; `phone`; initiates phone-change OTP. |
| POST | `/auth/email` | Recent bearer login; `email`; initiates email change. |
| POST | `/auth/password` | Recent bearer login; new `password`. Recovery verification first supplies a recovery session. |
| GET | `/me` | Bearer token; profile and contact details. |
| PATCH | `/me` | Bearer token; `first_name`, `last_name`. |

Signup creates one email/password Auth identity with requested phone in metadata.
Confirm the email, sign in, call `/auth/phone`, and verify the SMS with
`type: "phone_change"` to attach the phone to the **same account**. An unverified
requested phone cannot log in. Configure Supabase email confirmation, URL allowlist,
email templates/SMTP and Phone provider/SMS before onboarding users. The verification
endpoint expects the OTP from the configured template; a browser confirmation link
can instead complete Supabase verification directly before signing in here.

Web clients send `Authorization: Bearer <access_token>` and retain refresh tokens
securely in their client session. Refresh does not satisfy a recent-login check:
withdrawals, contact/password changes and staff mutations require an Auth session
created within the last 15 minutes. Sign in again when
`code: reauthentication_required` is returned.

## Machine collection flow

Each machine has its own `X-Machine-Key`. The administrator creates and activates
the machine and issues its key through the admin API. Store the key on the machine;
never place it in a public web client. Only its hash is retained in the database.

1. Call `POST /auth/login` with a machine key, credentials and a fresh
   `Idempotency-Key`. `POST /collection-sessions` is an equivalent entrypoint.
   The response includes `user_id`, `full_name`, `session_id` and
   `auth_scope: "machine_collection_only"`. The fresh Supabase login is immediately
   revoked in exchange for this binding; the machine never receives wallet-capable
   access or refresh tokens. Independent web logins remain active.
2. Before accepting a container, call
   `POST /collection-sessions/{session_id}/bottles` with a durable event UUID,
   sequential attempt number, UPC and observed time:

   ```json
   {
     "machine_event_id": "11111111-1111-4111-8111-111111111111",
     "sequence_no": 1,
     "upc": "001234567890",
     "observed_at": "2026-10-26T10:00:00Z"
   }
   ```

   The result has `eligibility_status` (`eligible`, `ineligible` or `unavailable`)
   and `status`. An eligible lookup is still `pending`; lookup alone earns no money.
   Missing or older-than-eight-day registry snapshots produce a rejected attempt
   with `reason_code: registry_unavailable`. The machine must return that bottle.
   Retries use the identical event data. A new physical bottle gets a new event UUID
   even when its UPC matches another bottle.
3. After the hardware confirms physical capture, call
   `POST /collection-sessions/{session_id}/bottles/{machine_event_id}/capture`:

   ```json
   {
     "status": "accepted",
     "capture_event_id": "22222222-2222-4222-8222-222222222222",
     "captured_at": "2026-10-26T10:00:02Z"
   }
   ```

   A rejected capture uses `status: "rejected"` and optional `reason_code`, with no
   capture UUID/time. Capture evidence belongs to one bottle only. An unregistered
   UPC cannot be turned into an accepted bottle. A trusted machine is responsible
   for accurate sensor evidence; a barcode scan cannot prove physical capture.
4. **Firmware owns the 30-second inactivity timer.** When firmware ends the
   session, submit the complete final dictionary to
   `POST /collection-sessions/{session_id}/submit` with `Idempotency-Key`:

   ```json
   {
     "machine_end_event_id": "33333333-3333-4333-8333-333333333333",
     "end_reason": "inactivity",
     "machine_ended_at": "2026-10-26T10:00:32Z",
     "final_sequence_no": 1,
     "bottles": [
       {
         "machine_event_id": "11111111-1111-4111-8111-111111111111",
         "sequence_no": 1,
         "upc": "001234567890",
         "status": "accepted"
       }
     ]
   }
   ```

   Include every accepted **and rejected** attempt, in sequence starting at 1.
   A capture that was not previously acknowledged may include `capture_event_id`
   and `captured_at` in its final row. Every attempt must already have a server
   eligibility verdict. An empty session submits an empty list and final sequence 0.
   Other end reasons are `machine_fault`, `restart` and `admin_recovery`.

   A valid submission durably closes the collection binding and records logout,
   then reconciles the server-verified bottle events to the wallet, normally in
   the same HTTP request. The result contains `user_logged_out: true`, counts,
   `reward_total_cents` and `reconciliation_status`. A complete but inconsistent
   manifest ends the session with `needs_review` and **no credit**. Invalid JSON,
   missing required fields or invalid sequences return 400 without accepting the
   end signal; firmware must retain and retry a valid submission. Clear the user's
   name/password from the machine UI once submission is acknowledged.

The backend never infers session end from elapsed inactivity. Store event UUIDs,
capture evidence and the exact final body/idempotency key durably on the device
before sending. On a timeout or 503 retry the exact request; a committed submission
survives a failed response. A changed final dictionary conflicts with its original.
No new attempts can be added after submission; identical retries remain available.

`GET /collection-sessions/{id}` and `/bottles` accept the owning machine key or
owner's web bearer token. `GET /collection-sessions` lists the logged-in user's
history. `POST /machines/heartbeat` accepts optional `firmware_version`.

`POST /machines/sync-batches` accepts `session_id`, `machine_boot_id`,
`device_batch_id` (UUIDs) and `bottles` (1–500 event objects in the scan format,
optionally containing final capture fields). This retries events for a session
started online; it does not authorize offline acceptance. Individual acknowledgements
commit independently, so a partially interrupted batch can be retried. Reuse the
same batch ID and exact body. A batch does not end a session.

## Wallet and manual payouts

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/wallet` | Available, reserved and total balance in cents. |
| GET | `/wallet/transactions` | User's immutable ledger entries and transaction references. |
| POST | `/payouts` | Recent bearer login and `Idempotency-Key`; JSON `email`, `amount_cents` only. |
| GET | `/payouts` | User's requests and deadlines. |
| GET | `/payouts/{id}` | User's request detail. |
| POST | `/payouts/{id}/cancel` | Cancel a pending, unclaimed request and release the reservation. |
| GET | `/notifications` | User's in-app notifications. |
| POST | `/notifications/{id}/read` | Mark an in-app notification as read (`status: sent`). |

Minimum payout is **500 cents**, with partial or full available-balance withdrawals
and **zero fees**. Submission immediately moves money from available to reserved;
concurrent requests cannot spend it twice. The reservation remains a user liability.
Staff completion moves it out of reserved without deducting available again. A
cancelled or confirmed-unpaid rejected payout releases funds exactly once. The
database records a processing deadline 24 hours after submission.

Payout submission is a form, not an automatic Interac transfer. Staff must send the
payment separately, then record the reference. Do not reject/release a request if
the external transfer may already have been sent; mark it for review instead.
Email/SMS notifications and banking integrations are not configured by this change;
in-app notifications and operational records are available immediately.

## Back office

Bootstrap the first administrator from a trusted terminal with `DATABASE_URL` set,
after that person signs up, verifies and signs in:

```sh
python -m recy grant-staff --auth-user-id AUTH_USER_UUID --role administrator
```

This requires database-owner access; there is no public role bootstrap route.
Staff use their own bearer login. All mutations require recent authentication.

| Method | Path | Input / behavior |
| --- | --- | --- |
| GET | `/admin/payouts`, `/admin/payouts/{id}` | Pending/processing queue and details. |
| POST | `/admin/payouts/{id}/claim` | Claim for the current operator before sending funds. |
| POST | `/admin/payouts/{id}/review` | `reason`; flag an uncertain transfer for investigation. |
| POST | `/admin/payouts/{id}/complete` | Assigned operator; `payment_reference`, `paid_at`. Records the externally sent payment. |
| POST | `/admin/payouts/{id}/reject` | `reason`, `confirmed_unpaid: true`; releases funds. |
| GET | `/admin/collection-sessions`, `/admin/collection-sessions/{id}`, `/admin/collection-sessions/{id}/bottles` | Inspect server-recorded sessions/evidence. |
| POST | `/admin/collection-sessions/{id}/reconcile` | Retry a validated submission; cannot override a review or invent a count. |
| GET | `/admin/operations` | Liability, credit/payout SLA, machine and registry views. |
| GET | `/admin/operational-alerts`, `/admin/machine-incidents` | Inspect operational records. |
| PATCH | `/admin/operational-alerts/{id}` | `status`: open/acknowledged/resolved. |
| PATCH | `/admin/machine-incidents/{id}` | `status`: open/investigating/resolved; `reason`. Resolving an incident does not change money. |
| POST | `/admin/jobs/run` | Optional `limit` (1–20); process durable recovery jobs. |
| GET | `/admin/support-cases` | Support queue. |
| PATCH | `/admin/support-cases/{id}` | `status`: open/investigating/resolved/closed; optional `resolution_code`. |

Administrators additionally manage:

| Method | Path | Input / behavior |
| --- | --- | --- |
| GET/POST | `/admin/machines` | Create with `machine_code`, `name`, optional `location`, `status`, `operating_mode`; idempotency key required. Defaults disabled/test. |
| PATCH | `/admin/machines/{id}` | `status` active/maintenance/disabled; `operating_mode` live/test. Only active/live starts or accepts new bottles. |
| POST | `/admin/machines/{id}/credentials` | Idempotency key; returns `credential_id` and `machine_key` **once**. If lost, revoke the key and issue another. |
| POST | `/admin/machines/{id}/credentials/{credential_id}/revoke` | Revoke the machine credential. |
| POST | `/admin/staff-roles` | `user_id` (application profile UUID), `role` administrator/payout_processor; optional `revoke: true`. |
| GET/POST | `/admin/wallet-adjustments` | Propose with `wallet_id`, nonzero `amount_cents`, `reason_code`, optional owned `support_case_id`; idempotency key. |
| POST | `/admin/wallet-adjustments/{id}/approve` | Post a proposed correction once; cannot overdraw. Preserves the original bottle/session evidence. |

`POST /support-cases` accepts `category` (`missing_credit`, `bottle_dispute`,
`payout_problem`, `account_recovery`), `description`, and optional owned
`session_id`, `bottle_event_id`, `payout_id`; idempotency key required.
`GET /support-cases` and `GET /support-cases/{id}` are user-scoped.

Lists generally accept `?limit=50&offset=0` (limit 1–100); machine/adjustment admin
lists and aggregate operations are capped at 100. Authentication requests have
persistent per-identity and peer rate limits. Configure `RECY_ALLOWED_ORIGINS`
with exact comma-separated browser origins; CORS never replaces authentication.

## Deploying this release

1. Back up the database and inspect `python -m recy migrate --plan`, then run
   `python -m recy migrate` with the release code and the existing Supabase
   session-pooler `DATABASE_URL`. Migration 003 is additive and required before
   serving application routes. Existing migrations/checksums are unchanged.
   Neither startup nor an HTTP request runs migrations.
2. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `RECY_WORKER_ENABLED=1` and
   `RECY_ALLOWED_ORIGINS` in Render, alongside existing server-only
   `DATABASE_URL` and legacy `RECY_API_KEY`. Root `.env` is local only; the process
   reads environment variables and does not automatically load that file.
3. Deploy the approved commit with the `render.yaml` start command. The Gunicorn
   config starts recovery while the web process is awake. Verify `/health`, the
   existing UPC lookup, and the new OpenAPI route; then use a verified test account
   and provision a test machine before enabling a production machine.
4. Optionally run `python -m recy work` in a continuously running process, or
   `python -m recy work --once` for an operational recovery pass. No paid service
   is created by this project.

The target is credit within 60 seconds of backend submission and payout within
24 hours. Credit is synchronous when possible; durable jobs recover a crash after
submission and overdue views/alerts expose delays. **Render Free can sleep or be
unavailable, so it cannot guarantee a 60-second recovery SLA.** A strict guarantee
needs an always-running worker/host and monitoring. Manual payout deadlines also
need staffed operations. No backend inactivity timer is introduced.

Application tables remain private with RLS and no Supabase client grants. This
release uses the existing privileged server connection and enforces ownership in
the API. Keep its credentials server-side; using a narrower runtime DB role is a
separate hardening step. Bulk bank-statement import/reconciliation, automated
external messaging, account deletion/export and a web UI are outside this release.
