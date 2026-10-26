# Frontend proposal — draft for approval

Status: approved for implementation with the user's capacity update: 8,000 accepted
bottles since confirmed evacuation means almost full, with machine sensor reports
providing additional fill levels. This document retains the original planning
scope; the implemented contracts and release boundaries are in `docs/portal.md`.

## Experience and navigation

One responsive web application with a customer area and a protected back office.
Customer screens prioritize mobile use; staff tables and reports also work on
desktop. Field staff can submit evacuation reports from a phone. Bottle scanning
and the 30-second inactivity timer remain machine functions.

Public navigation: Home · How it works · Sign up · Log in · Help.

Customer navigation: Overview · Transactions · Withdraw · Notifications ·
Support · Account.

Back-office navigation: Overview · Deposit reviews · Withdrawals · Machines ·
Evacuations · Receiving depots · Support · Reports · Staff/settings.

Use clear labels, readable CAD amounts, keyboard-accessible forms, visible focus,
status text alongside color, and useful loading, empty, error and retry states.
Show dates in the viewer's timezone with an explicit timezone on detailed records.
Use “bottle deposit” for a recycling session and “receiving depot” for a physical
delivery destination so the two meanings cannot be confused.

## Customer screens

| Screen | Proposed content and actions |
| --- | --- |
| Home | Short explanation of the service, 7-cent reward per accepted bottle, Sign up and Log in buttons. No unsupported environmental-impact claims. |
| Sign up | First/last name, email, international phone, password, confirmation, terms/privacy links and acceptance. Email confirmation, then optional phone-verification completion with clear progress. |
| Log in | Email or verified phone plus password, show/hide password, forgot-password link, verification/recovery routes. |
| Overview | Full name, available balance, reserved for withdrawals, total held balance, latest bottle deposit, recent activity, Withdraw button. Explain that a held withdrawal is still awaiting processing. |
| Transactions | Three selectable views: Bottle deposits, Withdrawals, Wallet activity. Date/status filters, pagination and detail views. |
| Withdraw | Interac email, amount in CAD, minimum $5, available-balance shortcut, no fees, final confirmation and expected processing within 24 hours. No bank-provider integration. |
| Notifications | Credited deposits, payout changes and support updates where an event is generated; unread indicator and record links. |
| Support | Create a case linked to a deposit/bottle/withdrawal, view its status, and display a useful reference number. |
| Account | Names, email/phone verification, password/contact changes, log out, policy links. Reauthentication prompt for sensitive actions. |

### Transaction details

Bottle deposits show the session reference, machine/location, start/end time,
accepted and rejected counts, reward amount, credit status and credit date. Detail
shows each UPC and verdict/rejection reason. A session under review shows
“Under review”; the UI does not treat its claimed amount as an available credit.
Do not expose raw internal errors or capture evidence to the customer by default.

Withdrawals show requested amount, Interac email, requested date, current status,
24-hour processing deadline, outcome/rejection reason and payment reference when
paid. The customer can cancel only a pending, unclaimed request. A failed HTTP
response is not assumed to mean the request was never created.

Wallet activity shows credits, payout holds, released holds, payments and
corrections, with links back to their source records. A hold transfers value from
available to reserved; completing payment consumes reserved value. Display one
understandable activity per transaction with its balance effects, rather than
presenting the balanced ledger's individual entries as separate withdrawals.
An event's display sign depends on the balance being shown: a release increases
available balance without creating new earnings.

Retain the machine's session logout behavior. Ending a machine session does not
sign the customer out of an independent web session.

## Administrative and operational screens

### Overview

Cards and actionable queues for deposits awaiting review, overdue credits,
withdrawals awaiting action/overdue, available and reserved wallet liabilities,
machine connectivity, machines nearing capacity/full, open incidents and overdue
deliveries. Display the last update time and a refresh control. Cross-system totals
must come from backend aggregates, not the currently loaded page of records.

### Deposit reviews

Filter by status, date and machine. Open a case to compare the machine submission
with stored UPC verdicts, physical-capture acknowledgements and wallet history.
Show why it needs review, assigned reviewer and previous decisions.

Actions: claim a case, request investigation, resolve without credit with a reason,
or approve an auditable correction with supporting evidence. Never turn a claimed
accepted count into money solely because a reviewer clicked Approve. Preserve
the original scan records; link any approved wallet adjustment to the review and
protect against duplicate compensation. An incident marked resolved alone does
not settle a financial review.

### Withdrawal reviews

Workflow: Requested → approve/claim for manual processing → send Interac externally
→ record payment reference/date → Paid. Reject requires a reason and confirmation
that no money was sent, and releases the reservation. Uncertain transfers remain
under review with funds held. Approval alone does not mark a transfer paid.

The current claim endpoint can represent “Approve for processing” and changes the
status to `processing`. A separate approval stage or second approver would require
an additional backend workflow; it is not assumed in this draft. Show assignment,
deadline, amount, destination and audit history. Only the assigned operator may
complete or reject a claimed payout. Require reauthentication when necessary.

### Machines

Fleet list: machine code/name/location, active/maintenance/disabled state, live/test
mode, last heartbeat, connectivity, lifetime physically accepted count, accepted
count since last completed emptying, fill percentage/source/time, last evacuation
and open incidents. Machine detail adds daily acceptance/rejection trends,
collection history, capacity readings and evacuation history.

Keep “physical bottles received” distinct from “bottles credited”: an accepted
capture can await financial review. Keep rejected attempts and uncertain capture
incidents visible separately. A test machine's activity must be distinguishable
from production totals.

Approved capacity rules: Almost full at 8,000 accepted acknowledgements since
confirmed emptying; full when reported by a sensor. The implementation distinguishes
count-based state from sensor freshness and never invents a count-based full
threshold. Show maintenance/offline status independently of capacity.

Prefer measured machine/bin telemetry. A configured bottle-count capacity may
provide an explicitly labelled estimate; bottle count alone cannot establish actual
fullness. Do not silently clamp or reset sensor readings or assume different bottle
sizes occupy equal volume. Firmware's stop-on-full behavior requires a separate
hardware agreement; a dashboard warning cannot enforce it.

### Evacuations and receiving depots

Create an evacuation report linked to a machine and collection/bin cycle. Record
who emptied it, pickup time, full or partial emptying, containers/bag seal IDs,
measured count/weight where available, notes and intended receiving depot. Staff
identity comes from the authenticated account. An assignment can name a collector;
the actual pickup/delivery actors are recorded separately.

Track Scheduled → Collected → In transit → Delivered → Verified; also allow a
reasoned cancellation before pickup and a discrepancy/needs-review path. An
authorized receiver records actual delivery time/depot, who delivered, who
received/verified, receipt reference, and measured count/weight. Retain both the
planned and actual destination when they differ.

Provide list/detail reports searchable by date, machine, collector, depot and
status, plus discrepancy and overdue-delivery views. Pickup and delivery are
separate events: a pickup must not imply delivery. Model item rows if a trip
collects multiple machines or bags; do not overwrite one machine's identity with
another's.

Only a confirmed full emptying starts a new emptying cycle and resets its estimated
contents, exactly once. It never resets lifetime counts or user credits. Partial
emptying records what was removed and leaves remaining contents uncertain unless
measured. Serialize the pickup cutoff against new captures; bottles accepted after
the cutoff belong to the next cycle. Compare physical receipts against capture/bin
records, with tolerances; do not automatically debit customers for transport or
receipt discrepancies.

Suggested receiving-depot records: code, name, address, contact, active status.
Keep referenced depots and staff identities historically identifiable when disabled.
Photo/receipt uploads are optional scope and require protected object storage,
upload restrictions and retention rules; do not put image blobs into ledger tables.

## Roles

| Role | Proposed access |
| --- | --- |
| Customer | Own account, transactions, balance, withdrawal requests and support. |
| Finance/payout processor | Assigned payout work and finance queues. Other operational access should follow an explicit permission policy. |
| Operations reviewer | Deposit reviews, incidents, fleet and evacuation oversight; no unrestricted wallet corrections. Proposed new permission set. |
| Collector | Assigned pickups and deliveries; no customer balances or payout destinations. Proposed new role. |
| Depot receiver | Verify deliveries to permitted depots; no payout access. Proposed new role. |
| Administrator | Staff/permissions, machine provisioning, configuration and authorized financial corrections. |

The current backend defines only `administrator` and `payout_processor`; operational
roles and depot/assignment restrictions need backend enforcement. Hidden navigation
alone never grants or limits access. Machine credentials remain on the machines;
customer/admin browser sessions must never receive them during ordinary use.
Credential provisioning remains an explicit administrator action with one-time display.

## Existing API coverage and required extensions

| Capability | Existing support | Proposed extension |
| --- | --- | --- |
| Signup/login/profile, verification, recovery | Auth and `/me` endpoints | Return active permissions to the signed-in user; record terms/privacy consent version/time if signup requires agreement. |
| Customer deposits, withdrawals and wallet | Collection, payout and ledger-list endpoints | Server-side date/status filters and safe ordering; transaction summaries/source links and balance-after values where required. |
| Withdrawal processing | List, claim, review, reject, complete, cancel | Review UI can use existing actions. Add audit-history access and approved staff-only filtering/assignment views. |
| Deposit review | Inspect sessions/bottles; retry validated reconciliation; wallet adjustments | Review assignment, evidence/decision records, resolution endpoint and exactly-once compensation linkage. Existing reconcile is not an approval action for `needs_review`. |
| Machine health/configuration | Machines, heartbeat, operations, incidents | Paginated per-machine aggregate counts/trends, capacity configuration/readings, freshness and machine-authenticated telemetry. |
| Evacuations/deliveries/depots | No existing domain tables or API | New depot, bin-cycle, pickup/delivery item and history records; authorized workflows and reporting endpoints. |
| Notifications/support | In-app listing/read markers; cases and staff updates | Generate additional review/support notifications; outbound email/SMS and threaded support replies are optional additions. |
| Staff/security/audit | Grant/revoke two existing roles; internal audit records | Permission discovery, scoped operational roles, safe audit listing; staff MFA/step-up policy before operational rollout. |
| Reporting/export | Capped operational views and paginated records | Server-side totals, scoped date filters and export if included; never claim a 100-row view is a complete report. |

Suggested extension families, with exact contracts designed after approval:
`/admin/deposit-reviews`, `/admin/machines/{id}/stats`,
machine-authenticated `/machines/telemetry`, `/admin/receiving-depots`,
role-scoped `/evacuations` and delivery-verification actions, `/me/permissions`,
and `/admin/audit-events`. Existing collection/payout/wallet routes remain the
source of truth for financial activity; the frontend does not query private
application tables directly through Supabase's Data API.

## Delivery and review checkpoints

1. Approve this scope, capacity assumptions and operational workflow. Then produce
   the screen layouts and navigation for visual review before implementation.
2. Implement customer and existing staff flows against the released APIs. Resolve
   the minimum API gaps (permissions, review resolution, filtering) through reviewed
   backend changes rather than pretending an unsupported action succeeded.
3. Add capacity telemetry, evacuation/depot workflows and operational roles with
   reviewed migrations/API changes, then connect their screens.
4. Verify real account flows, record ownership, reauthentication, payout holds,
   review/correction retries, stale telemetry and pickup/delivery reconciliation.
   Publish only through the agreed PR/release approval process.

Use the existing Render API with an explicitly allowed frontend origin and Supabase
Auth configuration. Select frontend tooling/hosting during implementation; a
static responsive frontend is the starting deployment shape. Do not expose server
database credentials or the registry shared API key in a browser bundle. Browser
session persistence and refresh-token handling require an explicit implementation
decision and security review; the web app must not copy the machine login flow.

Decisions reviewed: the above navigation; the 8,000-bottle almost-full threshold
and additional sensor messages; complete pickup-through-receipt workflow; operational
roles; existing claim-as-approval payout flow; and staged delivery with backend gaps
addressed alongside their screens. Branding/name/logo/colors and any hardware
capacity specification can be supplied before visual design. This draft does not
change the 7-cent reward, $5 minimum, zero fees, session timer ownership, or manual
payout policy already agreed.
