# Application database migration

The approved design is implemented in `recy/migrations/002_application_schema.sql`.
The migration adds 30 tables and six views in the private `app` schema. Existing
Return-It product data and scraper checkpoints stay in `recy`; Supabase-managed
Auth tables are referenced without being changed or populated.

## Run

Install the project and load the project `.env` privately into the shell. Use the
Supabase Session pooler connection string as `DATABASE_URL`.

```sh
python -m recy migrate --plan
python -m recy migrate
```

`migrate` tracks filenames and SHA-256 checksums in `recy.schema_migrations`,
serializes migration attempts, and applies pending scripts and tracking records
in one transaction. Repeating the command makes no changes. An edited previously
applied migration is rejected; future changes require a new numbered file.
An unsuccessful migration rolls back. There is no destructive automatic down
migration. `init-db` still initializes only the existing registry schema, so the
scheduled scraper never implicitly upgrades the application tables.

The migration requires existing `auth.users`, as supplied by Supabase. Local
integration tests create a minimal Auth stub only inside the disposable
`recy_test` database; production SQL never creates that stub.

## Objects

| Area | Tables |
| --- | --- |
| User identity | `profiles`, `machine_logins`, `account_change_requests`, `staff_roles` |
| Machines | `machines`, `machine_credentials`, `machine_incidents` |
| Collection | `reward_rates`, `collection_sessions`, `bottle_events`, `session_submissions`, `machine_sync_batches` |
| Wallet accounting | `wallets`, `ledger_accounts`, `ledger_transactions`, `ledger_entries`, `wallet_adjustments` |
| Manual payouts | `payout_requests`, `payout_status_events` |
| Recovery/support | `work_jobs`, `support_cases`, `idempotency_requests`, `audit_events`, `operational_alerts`, `notifications` |
| Reconciliation | `bank_reconciliation_runs`, `bank_statement_items`, `bank_payout_matches`, `funding_snapshots`, `recovery_checks` |

Views: `wallet_balances`, `wallet_liabilities`, `session_credit_sla`,
`payout_processing_sla`, `machine_health`, and `registry_health`.
Select schema **app** in Supabase's Table Editor/Database Tables to inspect them.

## Database rules

- The initial immutable reward rate is **CAD 7 cents per physically accepted bottle**.
  Bottle UPCs can repeat; event IDs and session sequence numbers cannot.
- The database has **no 30-second inactivity timer**. A final authenticated
  submission must be written by the future backend. Inserting it records the
  backend end time, closes the machine-login binding, and creates durable
  reconciliation and Auth-revocation jobs with a 60-second deadline. The database
  does not call Supabase Auth or execute those jobs itself.
- Late bottle replay must belong to the sealed manifest. Submitted ownership,
  end times, and frozen reward terms cannot be changed; finalized verdicts and
  credited sessions cannot be rewritten.
- Payouts require **at least 500 CAD cents**, with zero fees. Recipient/debit
  amounts equal the request. Partial/full withdrawal limits are determined by
  available funds. The processing deadline is 24 elapsed hours and cannot reset.
- `app.post_journal(...)` is a trusted-backend-only SQL posting primitive, not a
  public API. It serializes wallet access, checks idempotency, seals a balanced
  journal, and requires source links to be finalized in the same transaction.
  Deferred checks prevent overdrafts, unbalanced entries, unsupported source
  amounts, duplicate credits, and payouts without reservations.
- User credits require captured-bottle evidence and a validated session manifest.
  Payout completion settles a reservation without a second available-balance
  debit. Rejected/cancelled unpaid payouts release it once. Financial source
  changes, journals, and bank transfer claims stay auditable.
- Journal records cannot be edited, deleted, truncated, or appended after sealing.
  Wallet adjustments require an approved administrator and post against the
  platform reward account. Balances are derived rather than directly editable.
- All 30 tables have RLS enabled. `anon`, `authenticated`, `service_role`, and
  `PUBLIC` receive no application-schema/table/function access. Views use
  `security_invoker`. Future API releases must explicitly provision their narrow
  backend roles and enforce verified user/machine/staff authorization.

No user accounts, machines, personal wallets, or financial balances are seeded.
Only the reward policy and two platform ledger accounts are created. No money is
sent, no SMTP/SMS configuration is changed, and no new HTTP endpoints or worker
are deployed by this migration. The one-minute credit and 24-hour manual-payout
targets require the backend, worker, and back-office processes described in the design.

## Verification

Tests run only with a disposable `TEST_DATABASE_URL` ending in `/recy_test`.
They reset that database's `app` and `recy` schemas. Never use the production URL.

```sh
python -m unittest discover -v
```

Coverage includes migration reruns/checksums, registry checkpoint preservation,
RLS/grants, captured-bottle credit, concurrent payout requests, minimum/zero-fee
constraints, full withdrawals, cancellation, settlement, journal immutability,
ownership, session submission/logout jobs, and the existing scraper/API suite.
