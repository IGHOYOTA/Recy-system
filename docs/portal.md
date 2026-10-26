# Recy customer and operations portal

The responsive portal is served at `/` by the existing WSGI/Render application.
There is no separate frontend hosting bill, Node build or browser-side database
credential. Static HTML, CSS, JavaScript and SVG assets ship in the Python package.

Customer navigation includes signup/login, verification/recovery, overview,
Transactions (Bottle deposits / Withdrawals / Wallet activity), Withdraw,
notifications, support and account settings. Back-office navigation depends on
server-provided roles and includes review queues, fleet capacity, evacuations,
receiving depots, finance processing, staff provisioning and audit records.

Screenshots use synthetic QA data:
[customer desktop](previews/customer-desktop.png),
[customer mobile](previews/customer-mobile.png),
[admin overview](previews/admin-desktop.png), and
[evacuation report list](previews/evacuations-desktop.png).

## Local use

With Python dependencies installed, use:

```sh
python scripts/dev.py
```

Open `http://127.0.0.1:8000`. This loopback-only developer server loads root `.env`
without shell evaluation and disables the Secure cookie flag only for local HTTP.
It never migrates the configured database. Authenticated routes require a migrated
Supabase/staging database and configured Auth provider; public pages can be viewed
before those are ready. The older `python -m recy serve` remains registry-only.
Do not use this development server for hosting.

## Browser session handling

The frontend uses same-origin `/api/v1/browser/...` routes that adapt the existing
API routes. For example, `/api/v1/browser/wallet` calls `/api/v1/wallet` internally.
Login/verification/refresh responses retain Supabase access/refresh tokens in
HttpOnly, SameSite=Strict cookies scoped to the adapter path, with Secure enabled
by default. Tokens are removed from browser JSON and are never stored in localStorage
or application tables. Cookies are browser-session cookies. An expired access token
can be refreshed using the refresh cookie; refreshing does not replace recent
password authentication for sensitive actions.
Browser logout always clears its local cookies; provider revocation is attempted
and its outcome is returned as `provider_revoked`, so an Auth outage cannot trap a
user in a locally signed-in browser.

Mutations require `X-Recy-Browser: 1`; untrusted origins are rejected and only exact
same-origin or explicitly allowed origins pass the HTTP layer. The adapter is
intended for the same-origin bundled portal, not cross-origin credentialed CORS.
Frontend assets have a restrictive Content Security Policy and escaped API values.
Machine credentials are not accepted in the browser adapter. The administrator-only
credential-issuance action still intentionally displays a newly issued key once.

## Deposit review API

All paths below have `/api/v1` before them. User bearer and machine authentication
remain unchanged for non-browser clients.

- `GET /admin/deposit-reviews` lists flagged sessions and their review outcomes.
- `GET /admin/deposit-reviews/{session_id}` returns the original submission,
  original bottle evidence and review record.
- `POST /admin/deposit-reviews/{session_id}/claim` assigns an open case to the
  current reviewer. Supply an `Idempotency-Key` and `{}`.
- `POST /admin/deposit-reviews/{session_id}/resolve` records a final decision.
  Supply `Idempotency-Key`, `resolution` (`approved` or `rejected`), `reason` and
  `accepted_event_ids` (stored bottle row UUIDs, not machine event IDs).

Approval requires an administrator and at least one selected bottle with stored
eligible/accepted status and physical capture evidence. The backend derives the
amount as **7 cents × selected bottles**. It atomically posts a linked wallet
adjustment, records the immutable decision, resolves its pending recovery/credit
alert records and creates an in-app notification. Identical retries return the same
result; a second resolution cannot pay again. Rejection uses an empty evidence
list, credits nothing and requires a reason. Another reviewer cannot take an
already assigned case.

Original scans and the original `needs_review` reconciliation status remain intact;
review resolution and its separate correction are returned to the customer beside
them. A reviewer cannot invent captures or retroactively change a UPC verdict.
Unknown/unconfirmed captures still require operational investigation and are not
made payable through this endpoint. Generic wallet adjustments remain privileged
administrator tools and must not be used to compensate a review a second time.

## Capacity and machine sensor API

`GET /admin/fleet` returns paginated machine records with lifetime accepted count,
accepted count since confirmed emptying, sensor information and capacity status.
`GET /admin/fleet/{machine_id}` also returns daily counts for the past 30 days.

- Below 8,000 accepted acknowledgements since evacuation: count-based `normal`.
- At **8,000 or more**: `almost_full`, regardless of a normal sensor message.
- A sensor `full` report (or measured 100%) latches `full` for the current cycle
  until a confirmed full emptying. A later normal report does not clear it.
- A recent sensor `almost_full` signal also raises the warning below 8,000.
  Non-full sensor reports become stale after 15 minutes; the count remains usable.
- No count-based full threshold is invented. Actual fullness is supplied by the
  machine. The frontend clearly distinguishes sensor freshness from count status.

Machines send `POST /machines/fill-reports` with their `X-Machine-Key`:

```json
{
  "machine_event_id": "11111111-1111-4111-8111-111111111111",
  "cycle_id": "22222222-2222-4222-8222-222222222222",
  "observed_at": "2026-10-26T10:00:00Z",
  "level": "almost_full",
  "percent": 85
}
```

`level` is `normal`, `almost_full` or `full`; percent is optional (integer 0–100).
Retrieve the current `capacity_cycle_id` using `POST /machines/heartbeat`; its
response also includes `accepted_since_evacuation` and `fill_status`. A new cycle
invalidates new messages from the previous cycle. Identical event retries remain
acknowledgeable; reused IDs with changed bodies conflict. Machine clocks must be
synchronized: reports must belong to the current cycle and cannot be over one minute
in the future. Out-of-order reports do not replace a newer reading.

A full machine cannot start new collection sessions or authorize new bottle
attempts. Already acknowledged retries, physical capture confirmations and final
submissions remain possible so existing sessions can close safely. Firmware must
also stop taking bottles when its sensor reports full; the web dashboard does not
control the hardware. Counts track backend-acknowledged accepted captures, including
bottles awaiting financial review. An acknowledgement received after an emptying
cutoff conservatively counts toward the new cycle.

## Evacuation and custody API

- `GET/POST /admin/depots`: list or create a receiving depot using `code`, `name`,
  `address` and optional `contact`; creation requires an idempotency key.
- `PATCH /admin/depots/{id}` with boolean `active` changes availability without
  deleting history.
- `GET/POST /admin/evacuations`: list or schedule a pickup. Creation accepts
  `machine_id`, `depot_id`, `scheduled_at`, optional `collector_id` (defaults to
  current operator) and `notes`. The collector must have an active authorized role.
- `GET /admin/evacuations/{id}` returns the report and append-only custody history,
  including actor names. A machine can have one scheduled pickup at a time.

Every action below is POST with `Idempotency-Key`:

| Action suffix | Required data and effect |
| --- | --- |
| `/collect` | `emptying_confirmed: true`, optional `seal_reference`. Records pickup, actor and accepted-count cutoff; starts a new capacity cycle. |
| `/transit` | `{}`. Moves a collected pickup into transit. |
| `/deliver` | Optional actual `depot_id`, default planned destination. Records who delivered, actual depot and server receipt time. |
| `/verify` | `received_count`, `receipt_reference`, plus `reason` if received count differs from pickup estimate. Requires a different person from the deliverer. |
| `/review` | `reason`. Flags an unresolved custody/count discrepancy without changing customer credits. Delivery or verification can then continue as appropriate. |
| `/cancel` | `reason`. Allowed only before pickup; never resets capacity. |

Only **confirmed full emptying** resets the current estimate. Partial emptying
must not be reported as full; this release does not estimate partial removals.
Accepted capture writes and pickup cutoff are serialized on the machine row.
Retrying a pickup cannot reset its cycle twice. Lifetime bottle events and wallet
balances are never reset by pickup, delivery, discrepancies or verification.

A receipt-count discrepancy requires an explanation and remains visible in custody
history; staff can flag it for review before resolving verification. Reports retain
planned and actual depots, assigned collector, actual pickup/delivery actors and
verifying receiver. There is no photo/file-upload requirement in this release.

## Roles, lists and reporting

`/me` now includes active `roles` for navigation; every API still authorizes actions
independently. `administrator` controls all operational and financial actions.
`operations_reviewer` handles queues/fleet/depots/evacuations, but cannot approve a
financial correction. `collector` sees assigned pickups only and may collect,
transport and deliver them. `depot_receiver` can view company evacuation reports
and verify delivered receipts. Receiver access is company-wide in this single-
organization release. Neither collector nor receiver gets payout/admin-finance
access. The existing `payout_processor` role remains for finance operations.

`GET /admin/team` provides staff identities; `GET /admin/audit-events` provides a
paginated redacted audit trail. `GET /admin/overview` calculates database-wide queue,
fleet and wallet totals, rather than summing a capped page of records.

`GET /me/summary` returns all-time accepted bottles, ended sessions and paid
withdrawals. `GET /wallet/activity` groups ledger entries into financial activities
with available/reserved changes and chronological available-after balance, using
journal posting time. Review corrections link back to their original session.

Customer session and withdrawal lists, wallet activity, the admin withdrawal list
and evacuation reports accept inclusive `from=YYYY-MM-DD`, `to=YYYY-MM-DD` and
`status` filters, applied on the server before pagination. The date range uses UTC
calendar dates; displayed detailed timestamps use the viewer's timezone. Wallet
`status` selects transaction kind. Pagination uses limit 1–100 and offset. Depot,
collector and machine identities are available in evacuation reports; the initial
UI filters reports by date/status and shows their machine/collector/destination.

## Release and verification

The release requires **migration 004** (and 003 if not previously applied). It adds
five private tables, one invoker-security view, capacity columns, serialization and
immutability rules, and extends allowed staff roles. Applied migrations 001–003
are not edited. No production migration or frontend deployment happens on an HTTP
request or startup.

After approval, apply `python -m recy migrate` using the reviewed release code and
Supabase connection; then deploy the approved commit to the existing Render web
service. Keep `RECY_COOKIE_SECURE=1` on HTTPS hosting. The root portal and assets
are automatically available alongside existing APIs; no separate frontend key or
build configuration is needed. Configure Supabase Auth URL/keys, email confirmation
and recovery templates; phone login still requires an enabled Phone provider/SMS.

Regression tests use only disposable `recy_test` PostgreSQL. Browser QA uses a
separate temporary test Auth adapter and synthetic users, exercises the real API
and database, and does not create production accounts or financial records. Real
Supabase email/SMS delivery remains an environment-dependent release check. Render
Free sleep/outages still limit the one-minute credit recovery guarantee; normal
credits and reviewed corrections post synchronously when the service is available.
