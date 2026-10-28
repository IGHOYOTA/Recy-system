import { request, money, date, escape as e } from "./api.js";
const app = document.querySelector("#app"),
  dialog = document.querySelector("#dialog");
let me = null,
  current = "",
  generation = 0,
  activeTab = "deposits",
  offset = 0,
  filters = {};
const PAGE = 20;
const paths = {
  home: "M3 10 12 3l9 7v10H3Zm6 10v-7h6v7",
  transactions: "M4 7h16M4 12h16M4 17h16m-3-3 3 3-3 3M7 4 4 7l3 3",
  wallet: "M3 6h17v14H3Zm0 0 14-3v3m-3 6h7v5h-7Z",
  bell: "M6 9a6 6 0 0 1 12 0v7l2 2H4l2-2Zm4 12h4",
  user: "M8 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0M4 21a8 8 0 0 1 16 0",
  help: "M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3m0 3v1M2 12a10 10 0 1 0 20 0 10 10 0 0 0-20 0",
  arrow: "M5 12h14m-5-5 5 5-5 5",
  bottle: "M10 3h4v4l3 4v9H7v-9l3-4Zm-3 11h10",
  check: "m5 12 4 4L19 6",
  machine: "M5 2h14v20H5Zm4 4h6v6H9Zm0 11h6",
  truck:
    "M2 6h12v11H2Zm12 5h4l4 4v2h-8M5 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4m13 0a2 2 0 1 0 0 4 2 2 0 0 0 0-4",
  pin: "M12 22S4 14 4 9a8 8 0 0 1 16 0c0 5-8 13-8 13m-11-13a3 3 0 1 0 6 0 3 3 0 0 0-6 0",
  review: "M5 3h14v18H5Zm4 5h6m-6 4h6m-6 4h3",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2",
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.review}"/></svg>`;
const logo = () =>
  `<a class="logo" href="#/"><img src="/assets/mark.svg" alt="">recy<span class="dot"></span></a>`;
const bottle = () =>
  `<svg class="bottle-art" viewBox="0 0 120 190" aria-hidden="true"><rect x="43" y="7" width="34" height="18" rx="4" fill="#28583b"/><path d="M47 25h26v19c0 11 22 18 22 39v82c0 10-7 17-17 17H42c-10 0-17-7-17-17V83c0-21 22-28 22-39Z" fill="#a9c48b" stroke="#718f58" stroke-width="2"/><path d="M33 91h54v50H33Z" fill="#f7f6e7"/><path d="m47 120 9-15 7 4m-9-2 2-5 8 1m10 19-1 10H55m4-4-5 4 5 5m-16-14-5-8 9-14m-1 5 2-6-7 1" fill="none" stroke="#42653d" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="M36 154h48M36 161h48" stroke="#8aa971" stroke-width="2"/><path d="M39 63c-5 7-7 13-7 21" stroke="#d4e4b8" stroke-width="5" stroke-linecap="round"/></svg>`;
const label = (s) =>
  String(s || "pending")
    .replaceAll("_", " ")
    .replace(/^./, (x) => x.toUpperCase());
const badge = (s) =>
  `<span class="badge ${["needs_review", "pending", "processing", "almost_full", "investigating", "open", "scheduled", "collected", "in_transit", "delivered"].includes(s) ? "warn" : ["full", "rejected", "disabled", "cancelled"].includes(s) ? "bad" : ["active", "posted", "paid", "verified", "approved", "resolved", "normal"].includes(s) ? "" : "neutral"}">${e(label(s))}</span>`;
const localTimestamp = () => {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 19);
};
const short = (id) =>
  String(id || "")
    .slice(0, 8)
    .toUpperCase();
const can = (role) => me?.roles?.includes(role);
const ops = () => can("administrator") || can("operations_reviewer");
const finance = () => can("administrator") || can("payout_processor");
const staff = () => Boolean(me?.roles?.length);
const field = (name, title, type = "text", extra = "", value = "") =>
  `<label class="field"><span>${title}</span><input name="${name}" type="${type}" value="${e(value)}" ${extra}></label>`;
const textarea = (name, title, required = true) =>
  `<label class="field"><span>${title}</span><textarea name="${name}" maxlength="2000" ${required ? "required" : ""}></textarea></label>`;
const select = (name, title, options) =>
  `<label class="field"><span>${title}</span><select name="${name}" aria-label="${e(title)}" required>${options}</select></label>`;
const option = (value, name) =>
  `<option value="${e(value)}">${e(name)}</option>`;
const details = (obj) =>
  `<dl class="details-grid">${Object.entries(obj)
    .map(([k, v]) => `<div><dt>${e(k)}</dt><dd>${e(v ?? "—")}</dd></div>`)
    .join("")}</dl>`;
const empty = (
  title = "Nothing here yet",
  description = "Your records will appear here when there is activity.",
) =>
  `<div class="empty">${icon("bottle")}<h3>${e(title)}</h3><p>${e(description)}</p></div>`;
const table = (headers, rows) =>
  rows.length
    ? `<div class="table-wrap"><table><thead><tr>${headers.map((x) => `<th>${x}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`
    : empty();
const pageHeading = (title, description, action = "") =>
  `<div class="page-heading"><div><h1>${e(title)}</h1><p>${e(description)}</p></div><div class="heading-actions">${action}</div></div>`;
const button = (text, action, cls = "secondary", attributes = "") =>
  `<button class="button ${cls}" data-action="${action}" ${attributes}>${text}</button>`;
const number = (value) =>
  new Intl.NumberFormat("en-CA").format(Number(value || 0));
function toast(message) {
  const t = document.querySelector("#toast");
  t.textContent = message;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 5000);
}
function errorHTML(error) {
  return `<div class="alert error" role="alert">${e(error.message)}${error.code === "reauthentication_required" ? '<br><a href="#/login">Log in again to confirm this action.</a>' : ""}${error.reference ? `<br><small>Reference: ${e(error.reference)}</small>` : ""}</div>`;
}
function errorMessage(error, root = dialog) {
  const target = root.querySelector("[data-error]");
  if (target) target.innerHTML = errorHTML(error);
  else toast(error.message);
}
function formData(form) {
  return Object.fromEntries(new FormData(form));
}
function modal(title, body, onSubmit) {
  dialog.innerHTML = `<div class="dialog-heading"><h2 id="dialog-title">${e(title)}</h2><button class="close" data-action="close-dialog" aria-label="Close">×</button></div><div data-error></div>${body}`;
  dialog.showModal();
  const form = dialog.querySelector("form");
  if (form) {
    form.dataset.key = crypto.randomUUID();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = form.querySelector("[type=submit]");
      submit.disabled = true;
      dialog.querySelector("[data-error]").innerHTML = "";
      try {
        await onSubmit(formData(form), form.dataset.key, form);
        dialog.close();
        toast("Saved successfully");
        await route();
      } catch (error) {
        errorMessage(error);
      } finally {
        submit.disabled = false;
      }
    });
  }
}
const submitButton = (name = "Save") =>
  `<div class="form-actions"><button class="button" type="submit">${name}</button><button class="button secondary" type="button" data-action="close-dialog">Cancel</button></div>`;
const pager = (count) =>
  `<div class="table-footer"><span>Showing ${offset + 1}–${offset + count} · ${PAGE} per page</span><div><button data-action="previous" ${offset === 0 ? "disabled" : ""}>Previous</button> <button data-action="next" ${count < PAGE ? "disabled" : ""}>Next</button></div></div>`;
const query = () =>
  `?limit=${PAGE}&offset=${offset}${Object.entries(filters)
    .filter(([, v]) => v)
    .map(([k, v]) => `&${k}=${encodeURIComponent(v)}`)
    .join("")}`;
function publicHeader() {
  return `<header class="public-header">${logo()}<nav><a class="link how-link" href="#/how-it-works">How it works</a><a class="link" href="#/login">Log in</a><a class="button" href="#/signup">Create account ${icon("arrow")}</a></nav></header>`;
}
const publicFooter = () =>
  `<footer class="public-footer"><span>Recy · Make every bottle count.</span><span>All rewards in CAD · <a href="#/help">Help</a></span></footer>`;
function landing() {
  app.innerHTML =
    publicHeader() +
    `<main class="landing"><section class="hero"><div><p class="eyebrow">A little return. A bigger difference.</p><h1>Good for the planet.<br><em>Good for your pocket.</em></h1><p>Give your bottles a new beginning. Recycle at a Recy machine, earn rewards, and keep track of every return in one place.</p><div class="form-actions"><a class="button" href="#/signup">Start your recycling journey ${icon("arrow")}</a><a class="link" href="#/how-it-works">See how it works</a></div><p class="supporting">7¢ per accepted bottle. Simple, transparent rewards.</p></div><div class="hero-art">${bottle()}<div class="reward-label"><strong>+$0.07</strong><small>for every accepted bottle</small></div></div></section><section class="how-it-works" id="how-it-works"><p class="eyebrow">Small habits, real rewards</p><h2>Your next return starts here.</h2><div class="grid three">${[
      [
        "01",
        "Recycle your bottles",
        "Log in at a Recy machine and deposit your bottles. Each physically accepted bottle earns 7¢.",
      ],
      [
        "02",
        "Watch your rewards grow",
        "See your bottle deposits and wallet credits, with the details of every recycling session.",
      ],
      [
        "03",
        "Request your withdrawal",
        "Withdraw $5 or more to your Interac email. No fees, with manual processing within 24 hours.",
      ],
    ]
      .map(
        ([n, t, d]) =>
          `<article class="card step"><span class="step-number">${n}</span><h3>${t}</h3><p>${d}</p></article>`,
      )
      .join("")}</div></section></main>` +
    publicFooter();
}
function shell(title) {
  const admin = current.startsWith("admin/");
  const customer = [
    ["overview", "home", "Overview"],
    ["transactions", "transactions", "Transactions"],
    ["withdraw", "wallet", "Withdraw"],
    ["notifications", "bell", "Notifications"],
    ["support", "help", "Support"],
    ["account", "user", "Account"],
  ];
  const adminNav = [
    ...(ops()
      ? [
          ["admin/overview", "home", "Overview"],
          ["admin/reviews", "review", "Deposit reviews"],
        ]
      : []),
    ...(finance() ? [["admin/withdrawals", "wallet", "Withdrawals"]] : []),
    ...(ops() ? [["admin/machines", "machine", "Machines"]] : []),
    ...(ops() || can("collector") || can("depot_receiver")
      ? [
          ["admin/evacuations", "truck", "Evacuations"],
          ["admin/depots", "pin", "Receiving depots"],
        ]
      : []),
    ...(finance() ? [["admin/support", "help", "Support queue"]] : []),
    ...(ops() ? [["admin/audit", "review", "Audit log"]] : []),
    ...(can("administrator")
      ? [["admin/settings", "settings", "Staff & machines"]]
      : []),
  ];
  const initial =
    (me.user.first_name?.[0] || "") + (me.user.last_name?.[0] || "");
  app.innerHTML = `<div class="app-shell"><aside class="sidebar">${logo()}<p class="eyebrow nav-label">${admin ? "Back office" : "Your space"}</p><nav class="nav">${(admin ? adminNav : customer).map(([href, i, name]) => `<a href="#/${href}" class="${current === href ? "active" : ""}" ${current === href ? 'aria-current="page"' : ""}>${icon(i)}${name}</a>`).join("")}</nav><div class="sidebar-footer"><div class="recycle-note">${icon("bottle")}<strong>Small returns. Lasting habits.</strong>Every accepted bottle is another step toward a circular future.</div><div class="user-chip"><div class="avatar">${e(initial)}</div><div><strong>${e(me.user.first_name + " " + me.user.last_name)}</strong><small>${admin ? "Staff workspace" : "Recy member"}</small></div></div></div></aside><section class="workspace"><header class="topbar"><div><button class="mobile-menu" data-action="menu" aria-label="Open navigation">☰</button><span class="breadcrumb">${admin ? "Back office" : "My Recy"} <span aria-hidden="true"> / </span> ${e(title)}</span></div><div class="top-actions">${staff() ? `<a class="link" href="#/${admin ? "overview" : ops() ? "admin/overview" : finance() ? "admin/withdrawals" : "admin/evacuations"}">${admin ? "Customer view" : "Staff workspace"}</a>` : ""}<button data-action="refresh" aria-label="Refresh this page">${icon("transactions")}</button><a href="#/notifications" aria-label="Notifications">${icon("bell")}</a><button data-action="logout">Log out</button><span class="avatar">${e(initial)}</span></div></header><main class="main" id="view"><div class="loading" role="status">Loading ${e(title.toLowerCase())}…</div></main></section></div>`;
}
function stat(title, value, note, i = "wallet", featured = false) {
  return `<article class="card stat ${featured ? "featured" : ""}"><div class="stat-top">${title}<span class="stat-icon">${icon(i)}</span></div><span class="value number">${value}</span><p>${note}</p></article>`;
}
function depositRows(items) {
  return items.map(
    (s) =>
      `<tr><td><strong>Bottle deposit</strong><small>#${short(s.id)}</small></td><td>${e(date(s.ended_at || s.started_at))}</td><td>${e(s.machine_code || short(s.machine_id))}</td><td>${s.accepted_count ?? "—"} <small>accepted</small></td><td>${money(s.review_amount_cents || s.reward_total_cents)}</td><td>${badge(s.review_resolution || s.reconciliation_status)}</td><td><button class="link" data-action="deposit-detail" data-id="${s.id}">View ${icon("arrow")}</button></td></tr>`,
  );
}
function payoutRows(items, admin = false) {
  return items.map(
    (p) =>
      `<tr><td><strong>${money(p.amount_cents)}</strong><small>#${short(p.id)}</small></td><td>${e(p.destination_email)}</td><td>${e(date(p.requested_at))}</td><td>${badge(p.status)}</td><td>${e(date(p.processing_due_at))}</td><td><button class="link" data-action="${admin ? "admin-payout-detail" : "payout-detail"}" data-id="${p.id}">View ${icon("arrow")}</button></td></tr>`,
  );
}
async function overview() {
  const [wallet, summary, records] = await Promise.all([
    request("/wallet"),
    request("/me/summary"),
    request("/collection-sessions?limit=5"),
  ]);
  return (
    pageHeading(
      `Welcome back, ${me.user.first_name}.`,
      "A little progress, with every return.",
      `<a class="button" href="#/withdraw">Request withdrawal ${icon("arrow")}</a>`,
    ) +
    `<div class="grid three">${stat("Available balance", money(wallet.available_cents) + '<span class="currency">CAD</span>', "Ready to withdraw · $5 minimum", "wallet", true)}${stat("Reserved for withdrawals", money(wallet.reserved_cents), "Held while your request is processed", "transactions")}${stat("Bottles accepted", number(summary.accepted_bottles), "Across all your recycling sessions", "bottle")}</div>` +
    `<div class="journey section-gap"><article class="rewards-banner"><div><span class="eyebrow">Your next good habit</span><h2>Every bottle has<br>another chapter.</h2><p>Earn 7¢ for each bottle accepted at a Recy machine. Your next return is a step forward.</p><a class="link" href="#/transactions">See your recycling history ${icon("arrow")}</a></div>${bottle()}</article><article class="card"><div class="card-title"><h2>Your rewards, at a glance</h2>${icon("bottle")}</div><div class="mini-stat"><span>Completed bottle deposits</span><strong>${number(summary.completed_sessions)}</strong></div><div class="mini-stat"><span>Withdrawals paid</span><strong>${money(summary.paid_withdrawals_cents)}</strong></div><div class="mini-stat"><span>Total wallet balance</span><strong>${money(Number(wallet.available_cents) + Number(wallet.reserved_cents))}</strong></div></article></div>` +
    `<section class="card table-card section-gap"><div class="card-title"><h2>Recent bottle deposits</h2><a class="link" href="#/transactions">View all transactions ${icon("arrow")}</a></div>${table(["Activity", "Date", "Machine", "Bottles", "Reward", "Status", ""], depositRows(records.items))}</section><div class="footer-note"><span>Make every bottle count.</span><span>Rewards are in Canadian dollars · Updated just now</span></div>`
  );
}
const filterToolbar = (statuses) =>
  `<form id="filters" class="toolbar"><label>From (UTC)<input name="from" type="date" value="${e(filters.from || "")}"></label><label>To (UTC)<input name="to" type="date" value="${e(filters.to || "")}"></label><label>Status<select name="status"><option value="">All statuses</option>${statuses.map((s) => `<option value="${s}" ${filters.status === s ? "selected" : ""}>${label(s)}</option>`).join("")}</select></label><button class="button secondary small" type="submit">Apply filters</button><button type="button" class="link" data-action="reset-filters">Reset</button></form>`;
async function transactions() {
  let content, items;
  if (activeTab === "deposits") {
    ({ items } = await request("/collection-sessions" + query()));
    content = table(
      ["Activity", "Date", "Machine", "Bottles", "Reward", "Status", ""],
      depositRows(items),
    );
  } else if (activeTab === "withdrawals") {
    ({ items } = await request("/payouts" + query()));
    content = table(
      ["Amount", "Interac email", "Requested", "Status", "Due by", ""],
      payoutRows(items),
    );
  } else {
    ({ items } = await request("/wallet/activity" + query()));
    content = table(
      [
        "Activity",
        "Date",
        "Available change",
        "Reserved change",
        "Available after",
        "",
      ],
      items.map(
        (t) =>
          `<tr><td><strong>${e(label(t.kind))}</strong><small>#${short(t.id)}</small></td><td>${e(date(t.created_at))}</td><td class="number ${t.available_delta_cents >= 0 ? "positive" : "negative"}">${t.available_delta_cents > 0 ? "+" : ""}${money(t.available_delta_cents)}</td><td class="number">${money(t.reserved_delta_cents)}</td><td>${money(t.available_after_cents)}</td><td>${t.session_id ? button("View deposit", "deposit-detail", "secondary small", `data-id="${t.session_id}"`) : t.payout_id ? button("View withdrawal", "payout-detail", "secondary small", `data-id="${t.payout_id}"`) : "Correction"}</td></tr>`,
      ),
    );
  }
  return (
    pageHeading(
      "Transactions",
      "Every return, credit and withdrawal. All in one place.",
    ) +
    `<div class="tabs" role="group" aria-label="Transaction views">${[
      ["deposits", "Bottle deposits"],
      ["withdrawals", "Withdrawals"],
      ["wallet", "Wallet activity"],
    ]
      .map(
        ([t, n]) =>
          `<button data-action="transaction-tab" data-tab="${t}" class="${activeTab === t ? "active" : ""}" aria-pressed="${activeTab === t}">${n}</button>`,
      )
      .join("")}</div>` +
    filterToolbar(
      activeTab === "deposits"
        ? [
            "pending",
            "posted",
            "no_credit",
            "needs_review",
            "approved",
            "rejected",
          ]
        : activeTab === "withdrawals"
          ? [
              "pending",
              "processing",
              "paid",
              "needs_review",
              "rejected",
              "cancelled",
            ]
          : [
              "session_credit",
              "payout_reserve",
              "payout_release",
              "payout_settle",
              "adjustment",
            ],
    ) +
    `<section class="card table-card">${content}${items.length ? pager(items.length) : ""}</section><p class="supporting">A withdrawal hold moves money to reserved. A payment uses that reservation; it does not charge your available balance again.</p>`
  );
}
async function withdraw() {
  const wallet = await request("/wallet");
  return (
    pageHeading(
      "Request a withdrawal",
      "Your recycling rewards, ready for their next chapter.",
    ) +
    `<div class="grid two"><section class="card form-card"><div class="card-title"><h2>Send to your Interac email</h2>${icon("wallet")}</div><div data-error></div><form id="withdraw-form" data-key="${crypto.randomUUID()}">${field("email", "Interac email", "email", 'required autocomplete="email"', me.email || "")}${field("amount", "Amount (CAD)", "number", `required min="5" max="${Number(wallet.available_cents) / 100}" step="0.01" inputmode="decimal"`)}<button type="button" class="link" data-action="use-balance" data-cents="${wallet.available_cents}">Use available balance (${money(wallet.available_cents)})</button><div class="summary-line section-gap"><span>Available balance</span><strong>${money(wallet.available_cents)}</strong></div><div class="summary-line"><span>Processing fee</span><strong>$0.00</strong></div><div class="form-actions"><button class="button" type="submit" ${wallet.available_cents < 500 ? "disabled" : ""}>Review withdrawal ${icon("arrow")}</button></div></form></section><aside class="card"><p class="eyebrow">A clear, simple process</p><h2 class="section-gap">Your rewards. No fees.</h2><p class="supporting">Request any amount from $5 up to your available balance. Once submitted, that amount is reserved while our team processes your Interac transfer.</p><div class="summary-line section-gap"><span>Minimum withdrawal</span><strong>$5.00 CAD</strong></div><div class="summary-line"><span>Processing target</span><strong>Within 24 hours</strong></div><div class="summary-line"><span>Fees</span><strong>None</strong></div><p class="supporting">Please check your email carefully. A pending request can be cancelled before an operator starts processing it.</p></aside></div>`
  );
}
async function notifications() {
  const { items } = await request("/notifications" + query());
  return (
    pageHeading("Notifications", "Updates about your rewards and requests.") +
    `<section class="card">${items.length ? items.map((n) => `<article class="notice"><div><strong>${e(label(n.kind))}</strong><p>${e(date(n.created_at))} · #${short(n.resource_id)}</p></div><div>${n.resource_type === "collection_session" ? button("View", "deposit-detail", "secondary small", `data-id="${n.resource_id}"`) : n.resource_type === "payout_request" ? button("View", "payout-detail", "secondary small", `data-id="${n.resource_id}"`) : ""} ${n.status === "pending" ? button("Mark read", "read-notification", "secondary small", `data-id="${n.id}"`) : "<small>Read</small>"}</div></article>`).join("") + pager(items.length) : empty("You’re all caught up", "Notifications appear here when your records change.")}</section>`
  );
}
function account() {
  return (
    pageHeading(
      "Your account",
      "Keep your details and sign-in information up to date.",
    ) +
    `<div class="grid two"><section class="card"><div class="card-title"><h2>Personal details</h2>${icon("user")}</div><div data-error></div><form id="profile-form"><div class="field-row">${field("first_name", "First name", "text", 'required maxlength="100" autocomplete="given-name"', me.user.first_name)}${field("last_name", "Last name", "text", 'required maxlength="100" autocomplete="family-name"', me.user.last_name)}</div><button class="button" type="submit">Save details</button></form></section><section class="card"><div class="card-title"><h2>Security & contact</h2>${icon("settings")}</div>${details({ Email: me.email, Phone: me.phone || "Not linked" })}<div class="form-actions">${button("Change email", "change-email")}${button("Change password", "change-password")}</div><p class="supporting">Contact changes require verification. Sign in again before sensitive changes if prompted.</p></section></div>`
  );
}
async function support(admin = false) {
  const { items } = await request(
    (admin ? "/admin/support-cases" : "/support-cases") + query(),
  );
  return (
    pageHeading(
      admin ? "Support queue" : "How can we help?",
      admin
        ? "Review and resolve customer support cases."
        : "Get help with a deposit, a bottle or a withdrawal.",
      !admin ? button("Open a support case", "new-support", "") : "",
    ) +
    `<section class="card table-card">${table(
      ["Case", "Category", "Description", "Status", "Created", ""],
      items.map(
        (s) =>
          `<tr><td>#${short(s.id)}</td><td>${e(label(s.category))}</td><td>${e(s.description)}</td><td>${badge(s.status)}</td><td>${e(date(s.created_at))}</td><td>${admin ? button("Update", "support-update", "secondary small", `data-id="${s.id}"`) : ""}</td></tr>`,
      ),
    )}${items.length ? pager(items.length) : ""}</section>`
  );
}
async function adminOverview() {
  const s = await request("/admin/overview");
  return (
    pageHeading(
      "Operations overview",
      "The signals that need your attention.",
      button("Refresh", "refresh"),
    ) +
    `<div class="grid four">${stat("Deposit reviews", number(s.deposit_reviews), "Awaiting an audited decision", "review", true)}${stat("Pending withdrawals", number(s.pending_withdrawals), `${number(s.overdue_withdrawals)} past the 24-hour target`, "wallet")}${stat("Almost full / full", `${s.almost_full_machines} / ${s.full_machines}`, "8,000 accepted bottles triggers almost full", "machine")}${stat("Wallet liability", money(s.wallet_liability_cents), "Available + reserved customer balances", "transactions")}</div><div class="grid three section-gap">${stat("Bottles physically accepted", number(s.lifetime_accepted), "Lifetime count across all machines", "bottle")}${stat("Open evacuations", number(s.open_evacuations), "Track pickup through verified receipt", "truck")}${stat("Offline / no heartbeat", number(s.offline_machines), "No heartbeat within 5 minutes", "machine")}</div><div class="grid two section-gap"><article class="card"><h2>Close the loop on reviews</h2><p class="supporting">Review evidence before approving a correction. Scan histories are preserved and approved credits are posted once.</p><div class="form-actions"><a class="button secondary" href="#/admin/reviews">Open deposit reviews ${icon("arrow")}</a></div></article><article class="card"><h2>Keep collections moving</h2><p class="supporting">Check the fleet, schedule an emptying and track delivery to a receiving depot. Capacity resets at confirmed pickup.</p><div class="form-actions"><a class="button secondary" href="#/admin/evacuations">Manage evacuations ${icon("arrow")}</a></div></article></div>`
  );
}
async function reviews() {
  const { items } = await request("/admin/deposit-reviews" + query());
  return (
    pageHeading(
      "Deposit reviews",
      "Inspect capture evidence and resolve flagged deposits.",
    ) +
    `<section class="card table-card">${table(
      [
        "Session",
        "Customer",
        "Machine",
        "Captured bottles",
        "Review",
        "Decision",
        "",
      ],
      items.map(
        (r) =>
          `<tr><td>#${short(r.session_id)}<small>${e(date(r.ended_at))}</small></td><td>${e(r.full_name)}</td><td>${e(r.machine_code)}</td><td>${number(r.evidenced_accepted)}</td><td>${badge(r.review_status)}</td><td>${r.resolution ? badge(r.resolution) + "<small>" + money(r.amount_cents) + "</small>" : "—"}</td><td>${button("Review", "review-detail", "secondary small", `data-id="${r.session_id}"`)}</td></tr>`,
      ),
    )}${items.length ? pager(items.length) : ""}</section>`
  );
}
async function adminWithdrawals() {
  const { items } = await request("/admin/payouts" + query());
  return (
    pageHeading(
      "Withdrawal requests",
      "Approve for manual processing, then record the external transfer.",
    ) +
    filterToolbar([
      "pending",
      "processing",
      "paid",
      "needs_review",
      "rejected",
      "cancelled",
    ]) +
    `<section class="card table-card">${table(["Amount", "Interac email", "Requested", "Status", "Due by", ""], payoutRows(items, true))}${items.length ? pager(items.length) : ""}</section>`
  );
}
async function machines() {
  const { items } = await request("/admin/fleet" + query());
  return (
    pageHeading(
      "Machine fleet",
      "Physical bottle counts, capacity signals and machine health.",
      button("Schedule evacuation", "new-evacuation", ""),
    ) +
    `<form id="filters" class="toolbar"><label>Capacity<select name="status" aria-label="Capacity"><option value="">All levels</option>${["normal", "almost_full", "full"].map((level) => `<option value="${level}" ${filters.status === level ? "selected" : ""}>${label(level)}</option>`).join("")}</select></label><button class="button secondary small" type="submit">Apply filter</button><button class="link" type="button" data-action="reset-filters">Reset</button></form><section class="card table-card">${table(
      [
        "Machine",
        "Lifetime bottles",
        "Since evacuation",
        "Capacity",
        "Sensor updated",
        "Connectivity",
        "",
      ],
      items.map(
        (m) =>
          `<tr><td><strong>${e(m.machine_code)}</strong><small>${e(m.location || m.name)} · ${e(m.operating_mode)}</small></td><td class="number">${number(m.lifetime_accepted)}</td><td class="number">${number(m.accepted_since_evacuation)}<small>8,000 → almost full</small></td><td>${badge(m.fill_status)}<small>${m.sensor_percent != null ? m.sensor_percent + "% measured" : "Count estimate"}</small></td><td>${e(date(m.sensor_observed_at))}${m.sensor_stale ? "<small>Sensor stale / unavailable</small>" : ""}</td><td>${badge(m.last_heartbeat_at && Date.now() - new Date(m.last_heartbeat_at) < 300000 ? "active" : "offline")}<small>${e(m.status)}</small></td><td>${button("Details", "machine-detail", "secondary small", `data-id="${m.id}"`)}</td></tr>`,
      ),
    )}${items.length ? pager(items.length) : ""}</section><p class="supporting">An almost-full count is based on accepted acknowledgements since confirmed emptying. A full sensor signal remains active until the next confirmed emptying.</p>`
  );
}
async function evacuations() {
  const { items } = await request("/admin/evacuations" + query());
  return (
    pageHeading(
      "Machine evacuations",
      "From confirmed pickup to verified delivery.",
      ops() ? button("Schedule evacuation", "new-evacuation", "") : "",
    ) +
    filterToolbar([
      "scheduled",
      "collected",
      "in_transit",
      "delivered",
      "verified",
      "needs_review",
      "cancelled",
    ]) +
    `<section class="card table-card">${table(
      [
        "Machine",
        "Collector",
        "Pickup",
        "Destination",
        "Status",
        "Verified by",
        "",
      ],
      items.map(
        (r) =>
          `<tr><td><strong>${e(r.machine_code)}</strong><small>#${short(r.id)}</small></td><td>${e(r.collector_name)}</td><td>${e(date(r.collected_at || r.scheduled_at))}<small>${r.collected_at ? "Collected" : "Scheduled"}</small></td><td>${e(r.actual_depot_name || r.planned_depot_name)}</td><td>${badge(r.status)}</td><td>${e(r.verified_by_name || "—")}</td><td>${button("View report", "evacuation-detail", "secondary small", `data-id="${r.id}"`)}</td></tr>`,
      ),
    )}${items.length ? pager(items.length) : ""}</section>`
  );
}
async function depots() {
  const { items } = await request("/admin/depots" + query());
  return (
    pageHeading(
      "Receiving depots",
      "Delivery destinations and receipt verification.",
      ops() ? button("Add depot", "new-depot", "") : "",
    ) +
    `<section class="card table-card">${table(
      ["Depot", "Address", "Contact", "Status", ""],
      items.map(
        (d) =>
          `<tr><td><strong>${e(d.name)}</strong><small>${e(d.code)}</small></td><td>${e(d.address)}</td><td>${e(d.contact || "—")}</td><td>${badge(d.active ? "active" : "disabled")}</td><td>${ops() ? button(d.active ? "Deactivate" : "Activate", "depot-toggle", "secondary small", `data-id="${d.id}" data-active="${!d.active}"`) : ""}</td></tr>`,
      ),
    )}${items.length ? pager(items.length) : ""}</section>`
  );
}
async function audit() {
  const { items } = await request("/admin/audit-events" + query());
  return (
    pageHeading("Audit trail", "Who changed what, and when.") +
    `<section class="card table-card">${table(
      ["Date", "Action", "Actor", "Resource", "Reference"],
      items.map(
        (a) =>
          `<tr><td>${e(date(a.created_at))}</td><td>${e(label(a.action))}</td><td>${e(a.actor_type)}<small>${short(a.actor_id)}</small></td><td>${e(label(a.resource_type))}</td><td class="code">${e(a.resource_id || "—")}</td></tr>`,
      ),
    )}${items.length ? pager(items.length) : ""}</section>`
  );
}
async function settings() {
  const { items } = await request("/admin/team?limit=100");
  return (
    pageHeading(
      "Staff & machine settings",
      "Manage authorized staff and machine provisioning.",
      button("Create machine", "new-machine", ""),
    ) +
    `<section class="card table-card"><div class="card-title"><h2>Authorized staff</h2>${button("Grant / revoke role", "staff-role", "secondary small")}</div>${table(
      ["Name", "User ID", "Roles"],
      items.map(
        (p) =>
          `<tr><td>${e(p.first_name + " " + p.last_name)}</td><td class="code">${p.id}</td><td>${p.roles.map((r) => e(label(r))).join(", ")}</td></tr>`,
      ),
    )}</section><section class="card section-gap"><h2>Machine credentials</h2><p class="supporting">Issue a separate credential to each machine. Keys are shown once and should only be installed on the physical machine.</p><div class="form-actions">${button("Issue credential", "issue-credential")}${button("Revoke credential", "revoke-credential")}${button("Set machine state", "machine-state")}</div></section>`
  );
}
let verificationContact = "";
function authPage(type) {
  const signup = type === "signup",
    recover = type === "recover",
    verify = ["verify", "verify-recovery"].includes(type),
    reset = type === "reset-password";
  const recoveryVerification = type === "verify-recovery";
  const title = signup
    ? "A fresh start for your bottles."
    : recover
      ? "Let’s get you back in."
      : verify
        ? recoveryVerification
          ? "Confirm your password reset."
          : "Verify your email."
        : reset
          ? "Choose a new password."
          : "Welcome back.";
  const intro = signup
    ? "Create your Recy account and start earning rewards."
    : recover
      ? "We’ll send recovery instructions to your email."
      : verify
        ? recoveryVerification
          ? "Enter the password recovery code sent to your email."
          : "Enter the confirmation code sent to your email."
        : reset
          ? "Use a strong, unique password for your Recy account."
          : "Log in to see your returns, rewards and wallet.";
  app.innerHTML =
    publicHeader() +
    `<main class="auth-layout"><section class="card"><p class="eyebrow">Your recycling journey</p><h1 class="section-gap">${title}</h1><p class="intro">${intro}</p><div data-error></div><form id="auth-form" data-type="${type}">${signup ? `<div class="field-row">${field("first_name", "First name", "text", 'required autocomplete="given-name" maxlength="100"')}${field("last_name", "Last name", "text", 'required autocomplete="family-name" maxlength="100"')}</div>` : ""}${signup || recover ? field("email", "Email", "email", 'required autocomplete="email"') : ""}${signup ? field("phone", "Phone number", "tel", 'required placeholder="+16045550123" autocomplete="tel"') + '<p class="supporting">Use your international country code. Account verification is by email.</p>' : ""}${type === "login" ? field("identifier", "Email", "email", 'required autocomplete="username"') : ""}${signup || type === "login" || reset ? field("password", reset ? "New password" : "Password", "password", `required minlength="8" maxlength="1024" autocomplete="${type === "login" ? "current-password" : "new-password"}"`) + `<button class="link" data-action="toggle-password" type="button">Show password</button>` : ""}${signup || reset ? field("confirm_password", "Confirm password", "password", 'required minlength="8" autocomplete="new-password"') : ""}${verify ? field("contact", "Email", "email", 'required autocomplete="email"', verificationContact) + field("token", "Verification code", "text", 'required autocomplete="one-time-code" maxlength="300"') : ""}<div class="form-actions"><button class="button" type="submit">${signup ? "Create account" : recover ? "Send recovery instructions" : verify ? "Verify account" : reset ? "Save new password" : "Log in"} ${icon("arrow")}</button></div></form><p class="auth-switch">${signup ? 'Already have an account? <a class="link" href="#/login">Log in</a>' : type === "login" ? 'New to Recy? <a class="link" href="#/signup">Create account</a><br><br><a class="link" href="#/recover">Forgot password?</a>' : '<a class="link" href="#/login">Back to log in</a>'}</p>${signup ? '<p class="supporting">Bottle rewards are credited after your machine session is submitted. Withdrawals start at $5 CAD with no fees.</p>' : ""}</section></main>` +
    publicFooter();
}
async function depositDetail(id) {
  const [s, bottles] = await Promise.all([
    request("/collection-sessions/" + id),
    request("/collection-sessions/" + id + "/bottles?limit=100"),
  ]);
  modal(
    "Bottle deposit #" + short(id),
    `${badge(s.review_resolution || s.reconciliation_status)}${details({ Machine: s.machine_code || short(s.machine_id), "Session ended": date(s.ended_at), "Accepted bottles": s.accepted_count ?? "Awaiting reconciliation", Reward: money(s.review_amount_cents || s.reward_total_cents), "Credit posted": date(s.review_resolved_at || s.credit_posted_at), Reference: s.id })}${s.review_resolution ? '<div class="alert success">Review resolved: ' + e(label(s.review_resolution)) + ". Any approved correction is linked in your wallet.</div>" : ""}<h3>Bottle records</h3>${table(
      ["UPC", "Decision", "Reason"],
      bottles.items.map(
        (b) =>
          `<tr><td class="code">${e(b.upc)}</td><td>${badge(b.status)}</td><td>${e(label(b.reason_code))}</td></tr>`,
      ),
    )}<p class="supporting">Showing up to 100 bottle records. A review preserves the original records and creates a separate correction when needed.</p><div class="form-actions">${button("Get help with this deposit", "new-support", "secondary", `data-session="${id}"`)}</div>`,
  );
}
async function payoutDetail(id, admin) {
  const p = await request((admin ? "/admin/payouts/" : "/payouts/") + id);
  let actions = "";
  if (admin) {
    if (p.status === "pending")
      actions += button(
        "Approve for processing",
        "payout-claim",
        "",
        `data-id="${id}"`,
      );
    if (
      ["processing", "needs_review"].includes(p.status) &&
      p.assigned_to === me.user.id
    )
      actions += button(
        "Record payment sent",
        "payout-complete",
        "",
        `data-id="${id}"`,
      );
    if (["pending", "processing", "needs_review"].includes(p.status)) {
      actions += button(
        "Flag for review",
        "payout-review",
        "secondary",
        `data-id="${id}"`,
      );
      if (!p.assigned_to || p.assigned_to === me.user.id)
        actions += button(
          "Reject request",
          "payout-reject",
          "danger",
          `data-id="${id}"`,
        );
    }
  } else if (p.status === "pending")
    actions = button(
      "Cancel request",
      "payout-cancel",
      "danger",
      `data-id="${id}"`,
    );
  modal(
    "Withdrawal #" + short(id),
    `${badge(p.status)}${details({ Amount: money(p.amount_cents) + " CAD", Fees: money(p.fee_cents), "Interac email": p.destination_email, Requested: date(p.requested_at), "Processing due": date(p.processing_due_at), "Payment reference": p.payment_reference || "—", "Paid at": date(p.paid_at), "Rejection reason": p.rejection_reason || "—" })}<p class="supporting">Approval reserves the request for manual processing. Mark paid only after sending the transfer externally.</p><div class="form-actions">${actions}</div>`,
  );
}
async function reviewDetail(id) {
  const data = await request("/admin/deposit-reviews/" + id);
  const r = data.review;
  const resolved = r?.status === "resolved";
  modal(
    "Review deposit #" + short(id),
    `${badge(r?.status || "open")}${details({ Session: id, "Review reason": data.submission?.review_reason || data.session.last_error_code, "Assigned reviewer": r?.assigned_to ? short(r.assigned_to) : "Unassigned", "Recorded decision": r?.resolution ? label(r.resolution) : "—", Correction: money(r?.amount_cents) })}${r?.reason ? `<div class="alert">${e(r.reason)}</div>` : ""}<h3>Stored bottle evidence</h3>${table(
      ["UPC", "Status", "Eligibility", "Capture"],
      data.bottles.map(
        (b) =>
          `<tr><td class="code">${e(b.upc)}</td><td>${badge(b.status)}</td><td>${e(b.eligibility_status)}</td><td>${b.capture_event_id ? "Confirmed" : "No confirmed capture"}</td></tr>`,
      ),
    )}${
      !resolved
        ? `<div class="form-actions">${button("Assign to me", "claim-review", "secondary", `data-id="${id}"`)}</div><form><div class="section-gap">${select("resolution", "Resolution", (can("administrator") ? option("approved", "Approve evidenced bottles and credit correction") : "") + option("rejected", "Reject credit with reason"))}</div><p class="supporting">Only stored, eligible and physically captured bottles can be credited. Select the evidence below. The original session will remain intact.</p><div class="scroll-list">${
            data.bottles
              .filter((b) => b.status === "accepted" && b.capture_event_id)
              .map(
                (b) =>
                  `<label class="checkbox"><input type="checkbox" name="evidence" value="${b.id}"><span>Attempt ${b.sequence_no} · UPC ${e(b.upc)} · 7¢</span></label>`,
              )
              .join("") ||
            '<p class="supporting">No confirmed accepted bottles available for credit.</p>'
          }</div>${textarea("reason", "Decision reason / evidence reference")}${submitButton("Resolve review")}</form>`
        : ""
    }`,
    async (data, key, form) => {
      await request("/admin/deposit-reviews/" + id + "/resolve", {
        method: "POST",
        key,
        body: {
          resolution: data.resolution,
          reason: data.reason,
          accepted_event_ids:
            data.resolution === "approved"
              ? [...form.querySelectorAll("[name=evidence]:checked")].map(
                  (x) => x.value,
                )
              : [],
        },
      });
    },
  );
}
async function machineDetail(id) {
  const { machine: m, daily_counts } = await request("/admin/fleet/" + id);
  modal(
    m.machine_code + " · " + m.name,
    `${badge(m.fill_status)}${details({ Location: m.location, "Operating state": m.status + " / " + m.operating_mode, "Lifetime accepted": number(m.lifetime_accepted), "Since evacuation": number(m.accepted_since_evacuation), "Almost full threshold": "8,000 accepted bottles", "Sensor level": m.sensor_level || "No reading", "Sensor reading": m.sensor_percent != null ? m.sensor_percent + "%" : "—", "Sensor timestamp": date(m.sensor_observed_at), "Last heartbeat": date(m.last_heartbeat_at), "Current bin cycle": m.capacity_cycle_id })}<h3>Daily capture counts · last 30 days</h3>${table(
      ["Date", "Accepted", "Rejected"],
      daily_counts.map(
        (d) =>
          `<tr><td>${e(date(d.day))}</td><td>${number(d.accepted)}</td><td>${number(d.rejected)}</td></tr>`,
      ),
    )}<div class="form-actions">${button("Schedule evacuation", "new-evacuation", "", `data-machine="${id}"`)}</div>`,
  );
}
async function evacuationDetail(id) {
  const { evacuation: r, events } = await request("/admin/evacuations/" + id);
  const attrs = `data-id="${id}"`;
  let actions = "";
  if (
    r.status === "scheduled" &&
    (ops() || r.assigned_collector_id === me.user.id)
  )
    actions += button(
      "Confirm pickup & emptying",
      "evacuation-collect",
      "",
      attrs,
    );
  if (
    r.status === "collected" &&
    (ops() || r.assigned_collector_id === me.user.id)
  )
    actions += button(
      "Mark in transit",
      "evacuation-transit",
      "secondary",
      attrs,
    );
  if (
    ["collected", "in_transit", "needs_review"].includes(r.status) &&
    !r.delivered_at &&
    (ops() || r.assigned_collector_id === me.user.id)
  )
    actions += button("Record delivery", "evacuation-deliver", "", attrs);
  if (
    ["delivered", "needs_review"].includes(r.status) &&
    r.delivered_at &&
    (ops() || can("depot_receiver")) &&
    r.delivered_by !== me.user.id
  )
    actions += button("Verify receipt", "evacuation-verify", "", attrs);
  if (
    ["collected", "in_transit", "delivered", "needs_review"].includes(
      r.status,
    ) &&
    (ops() || can("depot_receiver"))
  )
    actions += button(
      "Flag discrepancy",
      "evacuation-review",
      "secondary",
      attrs,
    );
  if (r.status === "scheduled" && ops())
    actions += button("Cancel pickup", "evacuation-cancel", "danger", attrs);
  modal(
    "Evacuation #" + short(id),
    `${badge(r.status)}${details({ Machine: r.machine_code, "Machine location": r.machine_location, "Pickup instructions": r.notes || "—", "Assigned collector": r.collector_name, Scheduled: date(r.scheduled_at), Collected: date(r.collected_at), "Collected by": r.collected_by_name || short(r.collected_by), "Expected bottles at pickup": r.estimated_count_at_pickup ?? "—", "Planned depot": r.planned_depot_name, "Depot address": r.actual_depot_address || r.planned_depot_address, "Delivered to": r.actual_depot_name || "—", Delivered: date(r.delivered_at), "Delivered by": r.delivered_by_name || short(r.delivered_by), "Received bottles": r.received_count ?? "—", "Verified by": r.verified_by_name || "—", Receipt: r.receipt_reference || "—", "Bag / seal reference": r.seal_reference || "—" })}<div class="form-actions">${actions}</div><h3 class="section-gap">Custody history</h3><div class="timeline">${events.map((ev) => `<article class="timeline-item"><p><strong>${e(label(ev.action))}</strong> · ${e(label(ev.to_status))}</p><small>${e(date(ev.created_at))} · ${e(ev.actor_name || short(ev.actor_id))}</small>${ev.reason ? `<p>${e(ev.reason)}</p>` : ""}</article>`).join("")}</div>`,
  );
}
async function newEvacuation(machineId) {
  const [fleet, depots, team] = await Promise.all([
    request("/admin/fleet?limit=100"),
    request("/admin/depots?limit=100"),
    request("/admin/team?limit=100"),
  ]);
  const collectors = team.items.filter((p) =>
    p.roles.some((r) =>
      ["administrator", "operations_reviewer", "collector"].includes(r),
    ),
  );
  modal(
    "Schedule machine evacuation",
    `<form>${select("machine_id", "Machine", fleet.items.map((m) => `<option value="${m.id}" ${m.id === machineId ? "selected" : ""}>${e(m.machine_code + " · " + m.name)}</option>`).join(""))}${select(
      "depot_id",
      "Receiving depot",
      depots.items
        .filter((d) => d.active)
        .map((d) => option(d.id, d.name))
        .join(""),
    )}${select("collector_id", "Assigned collector", collectors.map((p) => option(p.id, p.first_name + " " + p.last_name)).join(""))}${field("scheduled_at", "Pickup date and time", "datetime-local", "required")}${textarea("notes", "Pickup instructions", false)}<p class="supporting">Confirm emptying only after the machine has been completely emptied. Capacity will reset at that confirmation, while lifetime counts stay intact.</p>${submitButton("Schedule pickup")}</form>`,
    async (data, key) => {
      data.scheduled_at = new Date(data.scheduled_at).toISOString();
      await request("/admin/evacuations", { method: "POST", key, body: data });
    },
  );
}
async function evacuationAction(id, action) {
  let fields = "";
  if (action === "collect")
    fields = `<label class="checkbox"><input type="checkbox" name="emptying_confirmed" required><span>I confirm this machine has been completely emptied. This starts a new capacity cycle.</span></label>${field("seal_reference", "Bag / seal reference", "text", 'maxlength="200"')}`;
  if (action === "deliver") {
    const { items } = await request("/admin/depots?limit=100");
    fields = select(
      "depot_id",
      "Actual receiving depot",
      items
        .filter((d) => d.active)
        .map((d) => option(d.id, d.name))
        .join(""),
    );
  }
  if (action === "verify")
    fields =
      field(
        "received_count",
        "Bottles received",
        "number",
        'required min="0" max="1000000" step="1"',
      ) +
      field(
        "receipt_reference",
        "Receipt reference",
        "text",
        'required maxlength="200"',
      ) +
      textarea(
        "reason",
        "Discrepancy explanation (required when counts differ)",
        false,
      );
  if (["review", "cancel"].includes(action))
    fields = textarea(
      "reason",
      action === "review"
        ? "Discrepancy / review reason"
        : "Cancellation reason",
    );
  modal(
    label(action) + " evacuation",
    `<form>${fields || '<p class="supporting">Confirm this update to the evacuation custody record.</p>'}${submitButton("Confirm " + label(action).toLowerCase())}</form>`,
    async (data, key) => {
      if (action === "collect")
        data.emptying_confirmed = Boolean(data.emptying_confirmed);
      if (action === "verify")
        data.received_count = Number(data.received_count);
      await request("/admin/evacuations/" + id + "/" + action, {
        method: "POST",
        key,
        body: data,
      });
    },
  );
}

async function action(name, target) {
  const id = target.dataset.id;
  if (name === "close-dialog") {
    dialog.close();
    return;
  }
  if (name === "menu") {
    document.querySelector(".sidebar")?.classList.toggle("open");
    return;
  }
  if (name === "refresh") {
    await route();
    return;
  }
  if (name === "logout") {
    await request("/auth/logout", { method: "POST", body: {} });
    me = null;
    location.hash = "/";
    return;
  }
  if (name === "transaction-tab") {
    activeTab = target.dataset.tab;
    offset = 0;
    filters = {};
    await route();
    return;
  }
  if (name === "previous" || name === "next") {
    offset = Math.max(0, offset + (name === "next" ? PAGE : -PAGE));
    await route();
    return;
  }
  if (name === "reset-filters") {
    filters = {};
    offset = 0;
    await route();
    return;
  }
  if (name === "toggle-password") {
    const input = document.querySelector("[name=password]");
    input.type = input.type === "password" ? "text" : "password";
    target.textContent =
      input.type === "password" ? "Show password" : "Hide password";
    return;
  }
  if (name === "use-balance") {
    document.querySelector("[name=amount]").value = (
      Number(target.dataset.cents) / 100
    ).toFixed(2);
    return;
  }
  if (name === "deposit-detail") return depositDetail(id);
  if (name === "payout-detail" || name === "admin-payout-detail")
    return payoutDetail(id, name.startsWith("admin"));
  if (name === "review-detail") return reviewDetail(id);
  if (name === "machine-detail") return machineDetail(id);
  if (name === "evacuation-detail") return evacuationDetail(id);
  if (name === "new-evacuation") return newEvacuation(target.dataset.machine);
  if (name.startsWith("evacuation-"))
    return evacuationAction(id, name.slice(11));
  if (name === "read-notification") {
    await request("/notifications/" + id + "/read", {
      method: "POST",
      body: {},
    });
    await route();
    return;
  }
  if (name === "claim-review") {
    await request("/admin/deposit-reviews/" + id + "/claim", {
      method: "POST",
      body: {},
      key: crypto.randomUUID(),
    });
    dialog.close();
    toast("Review assigned to you");
    await route();
    return;
  }
  if (name.startsWith("payout-")) {
    const action = name.slice(7),
      staffAction = action !== "cancel";
    let fields =
      action === "complete"
        ? field(
            "payment_reference",
            "External payment reference",
            "text",
            'required maxlength="200"',
          ) +
          field(
            "paid_at",
            "Date and time payment was sent",
            "datetime-local",
            'required step="1"',
            localTimestamp(),
          )
        : ["review", "reject"].includes(action)
          ? textarea("reason", "Reason")
          : "";
    if (action === "reject")
      fields +=
        '<label class="checkbox"><input name="confirmed_unpaid" type="checkbox" required><span>I confirm that no transfer was sent. Rejection releases the reserved funds.</span></label>';
    modal(
      action === "claim"
        ? "Approve for manual processing"
        : action === "complete"
          ? "Record payment already sent"
          : label(action) + " withdrawal",
      `<form>${fields || '<p class="supporting">' + (action === "claim" ? "Claim this withdrawal before sending the Interac transfer externally. This action does not mark the request as paid." : "Cancel this pending withdrawal and restore your available balance.") + "</p>"}${submitButton("Confirm")}</form>`,
      async (data, key) => {
        if (action === "complete")
          data.paid_at = new Date(data.paid_at).toISOString();
        if (action === "reject")
          data.confirmed_unpaid = Boolean(data.confirmed_unpaid);
        await request(
          (staffAction ? "/admin/payouts/" : "/payouts/") + id + "/" + action,
          { method: "POST", key, body: data },
        );
      },
    );
    return;
  }
  if (name === "new-support") {
    modal(
      "Open a support case",
      `<form>${select("category", "What do you need help with?", ["missing_credit", "bottle_dispute", "payout_problem", "account_recovery"].map((c) => option(c, label(c))).join(""))}${field("session_id", "Session ID (optional)", "text", "", target.dataset.session || "")}${field("payout_id", "Withdrawal ID (optional)", "text")}${textarea("description", "Tell us what happened")}${submitButton("Submit case")}</form>`,
      async (data, key) => {
        for (const f of ["session_id", "payout_id"])
          if (!data[f]) delete data[f];
        await request("/support-cases", { method: "POST", key, body: data });
      },
    );
    return;
  }
  if (name === "support-update") {
    modal(
      "Update support case",
      `<form>${select("status", "Status", ["open", "investigating", "resolved", "closed"].map((s) => option(s, label(s))).join(""))}${field("resolution_code", "Resolution code / reference", "text", 'maxlength="200"')}${submitButton("Update case")}</form>`,
      async (data) => {
        await request("/admin/support-cases/" + id, {
          method: "PATCH",
          body: data,
        });
      },
    );
    return;
  }
  if (name === "new-depot") {
    modal(
      "Add receiving depot",
      `<form>${field("code", "Depot code", "text", 'required maxlength="100"')}${field("name", "Depot name", "text", 'required maxlength="200"')}${field("address", "Address", "text", 'required maxlength="500"')}${field("contact", "Contact (optional)", "text", 'maxlength="200"')}${submitButton("Create depot")}</form>`,
      async (data, key) => {
        await request("/admin/depots", { method: "POST", key, body: data });
      },
    );
    return;
  }
  if (name === "depot-toggle") {
    modal(
      "Change depot availability",
      `<form><p class="supporting">${target.dataset.active === "true" ? "Activate this receiving depot." : "Deactivate this depot for new pickups and deliveries. Its history remains available."}</p>${submitButton("Confirm")}</form>`,
      async () => {
        await request("/admin/depots/" + id, {
          method: "PATCH",
          body: { active: target.dataset.active === "true" },
        });
      },
    );
    return;
  }
  if (["change-email", "change-password"].includes(name)) {
    const kind = name.slice(7);
    modal(
      "Change " + kind,
      `<form>${field(kind, kind === "email" ? "New email" : "New password", kind, "required" + (kind === "password" ? ' minlength="8" autocomplete="new-password"' : ""))}${submitButton("Continue")}</form>`,
      async (data) => {
        await request("/auth/" + kind, { method: "POST", body: data });
        if (kind === "email") toast("Check your email to confirm the change");
      },
    );
    return;
  }
  if (name === "new-machine") {
    modal(
      "Create a machine",
      `<form>${field("machine_code", "Machine code", "text", 'required maxlength="100"')}${field("name", "Machine name", "text", 'required maxlength="200"')}${field("location", "Location", "text", 'maxlength="300"')}<p class="supporting">New machines start disabled in test mode. Activate only after configuration and testing.</p>${submitButton("Create machine")}</form>`,
      async (data, key) => {
        await request("/admin/machines", { method: "POST", body: data, key });
      },
    );
    return;
  }
  if (name === "staff-role") {
    modal(
      "Grant or revoke staff role",
      `<form>${field("user_id", "Application user ID", "text", "required")}${select("role", "Role", ["administrator", "payout_processor", "operations_reviewer", "collector", "depot_receiver"].map((r) => option(r, label(r))).join(""))}${select("operation", "Operation", option("grant", "Grant role") + option("revoke", "Revoke role"))}${submitButton("Update authorization")}</form>`,
      async (data) => {
        await request("/admin/staff-roles", {
          method: "POST",
          body: {
            user_id: data.user_id,
            role: data.role,
            revoke: data.operation === "revoke",
          },
        });
        me = await request("/me");
      },
    );
    return;
  }
  if (
    ["issue-credential", "revoke-credential", "machine-state"].includes(name)
  ) {
    const { items } = await request("/admin/fleet?limit=100");
    const extra =
      name === "revoke-credential"
        ? field("credential_id", "Credential ID to revoke", "text", "required")
        : name === "machine-state"
          ? select(
              "status",
              "Machine status",
              ["disabled", "maintenance", "active"]
                .map((s) => option(s, label(s)))
                .join(""),
            ) +
            select(
              "operating_mode",
              "Mode",
              option("test", "Test") + option("live", "Live"),
            )
          : "";
    modal(
      label(name.replaceAll("-", " ")),
      `<form>${select("machine_id", "Machine", items.map((m) => option(m.id, m.machine_code)).join(""))}${extra}${submitButton("Continue")}</form>`,
      async (data, key) => {
        const base = "/admin/machines/" + data.machine_id;
        if (name === "machine-state")
          await request(base, {
            method: "PATCH",
            body: { status: data.status, operating_mode: data.operating_mode },
          });
        if (name === "revoke-credential")
          await request(
            base + "/credentials/" + data.credential_id + "/revoke",
            { method: "POST", body: {} },
          );
        if (name === "issue-credential") {
          const r = await request(base + "/credentials", {
            method: "POST",
            key,
            body: {},
          });
          setTimeout(
            () =>
              modal(
                "Save this machine key now",
                `<div class="alert">Shown once. Install it securely on this machine. If lost, revoke the credential and issue another.</div>${details({ "Credential ID": r.credential_id })}<label class="field"><span>Machine key</span><textarea readonly>${e(r.machine_key)}</textarea></label>`,
              ),
            0,
          );
        }
      },
    );
    return;
  }
}

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  event.preventDefault();
  if (target.disabled) return;
  target.disabled = true;
  try {
    await action(target.dataset.action, target);
  } catch (error) {
    errorMessage(error, dialog.open ? dialog : app);
  } finally {
    target.disabled = false;
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (form.closest("dialog")) return;
  if (
    !["auth-form", "profile-form", "withdraw-form", "filters"].includes(form.id)
  )
    return;
  event.preventDefault();
  const data = formData(form);
  const submit = form.querySelector("[type=submit]");
  if (submit) submit.disabled = true;
  const errors = app.querySelector("[data-error]");
  if (errors) errors.innerHTML = "";
  try {
    if (form.id === "filters") {
      filters = data;
      offset = 0;
      await route();
      return;
    }
    if (form.id === "profile-form") {
      await request("/me", { method: "PATCH", body: data });
      me = await request("/me");
      toast("Personal details saved");
      await route();
      return;
    }
    if (form.id === "withdraw-form") {
      if (!/^\d+(\.\d{1,2})?$/.test(data.amount))
        throw new Error("Enter an amount with up to two decimal places.");
      const amount = Math.round(Number(data.amount) * 100);
      if (!Number.isSafeInteger(amount) || amount < 500)
        throw new Error("Minimum withdrawal is $5.00 CAD.");
      modal(
        "Review your withdrawal",
        `<form><div class="summary-line"><span>Amount</span><strong>${money(amount)} CAD</strong></div><div class="summary-line"><span>Interac email</span><strong>${e(data.email)}</strong></div><div class="summary-line"><span>Fees</span><strong>$0.00</strong></div><p class="supporting">Submitting reserves this amount immediately. Our team aims to process your transfer within 24 hours.</p>${submitButton("Submit withdrawal")}</form>`,
        async (_, key) => {
          await request("/payouts", {
            method: "POST",
            key,
            body: { email: data.email, amount_cents: amount },
          });
          activeTab = "withdrawals";
          location.hash = "/transactions";
        },
      );
      return;
    }
    const type = form.dataset.type;
    if (
      data.confirm_password !== undefined &&
      data.password !== data.confirm_password
    )
      throw new Error("Passwords do not match.");
    delete data.confirm_password;
    if (type === "signup") {
      await request("/auth/signup", { method: "POST", body: data });
      verificationContact = data.email;
      location.hash = "/verify";
      toast("Check your email to confirm your account.");
    }
    if (type === "login") {
      await request("/auth/login", { method: "POST", body: data });
      me = await request("/me");
      location.hash = "/overview";
    }
    if (type === "recover") {
      await request("/auth/recover", { method: "POST", body: data });
      verificationContact = data.email;
      location.hash = "/verify-recovery";
      toast("If recovery is available, check your email.");
    }
    if (["verify", "verify-recovery"].includes(type)) {
      const payload = {
        type: type === "verify-recovery" ? "recovery" : "email",
        token: data.token,
        email: data.contact,
      };
      await request("/auth/verify", { method: "POST", body: payload });
      try {
        me = await request("/me");
      } catch {
        me = null;
      }
      location.hash =
        type === "verify-recovery"
          ? "/reset-password"
          : me
            ? "/overview"
            : "/login";
      toast("Verification completed.");
    }
    if (type === "reset-password") {
      await request("/auth/password", {
        method: "POST",
        body: { password: data.password },
      });
      location.hash = "/overview";
      toast("Password updated.");
    }
  } catch (error) {
    errorMessage(error, app);
  } finally {
    if (submit) submit.disabled = false;
  }
});
const views = {
  overview,
  transactions,
  withdraw,
  notifications,
  account,
  support,
  "admin/overview": adminOverview,
  "admin/reviews": reviews,
  "admin/withdrawals": adminWithdrawals,
  "admin/machines": machines,
  "admin/evacuations": evacuations,
  "admin/depots": depots,
  "admin/audit": audit,
  "admin/settings": settings,
  "admin/support": () => support(true),
};
async function route() {
  const next = location.hash.replace(/^#\/?/, "").split("?")[0];
  if (next !== current) {
    offset = 0;
    filters = {};
  }
  current = next;
  const ticket = ++generation;
  if (dialog.open) dialog.close();
  if (
    [
      "login",
      "signup",
      "recover",
      "verify",
      "verify-recovery",
      "reset-password",
    ].includes(current)
  ) {
    authPage(current);
    return;
  }
  if (current === "help") {
    app.innerHTML =
      publicHeader() +
      `<main class="privacy"><h1>Help with your Recy account</h1><section class="card section-gap"><h2>Recycling and rewards</h2><p class="supporting">Each eligible, physically accepted bottle earns 7¢ CAD. Your machine ends its session after 30 seconds without a new scan and submits its records. Credits normally appear within one minute of backend submission; some sessions need review.</p><h2 class="section-gap">Withdrawals</h2><p class="supporting">Request $5 or more to an Interac email, with no fees. Manual processing targets 24 hours. Reserved funds are held for your request and released if a pending request is cancelled or an unpaid request is rejected.</p><h2 class="section-gap">Need help with a record?</h2><p class="supporting">Log in and open Support to submit a case linked to your deposit or withdrawal. For password recovery, use the forgot-password link.</p><div class="form-actions"><a class="button" href="#/support">Open support</a><a class="button secondary" href="#/recover">Recover account</a></div></section></main>` +
      publicFooter();
    return;
  }
  if (current === "how-it-works") {
    landing();
    document.querySelector("#how-it-works")?.scrollIntoView();
    return;
  }
  if (!current) {
    if (me) {
      location.hash = "/overview";
      return;
    }
    landing();
    return;
  }
  if (!me) {
    location.hash = "/login";
    return;
  }
  const view = views[current];
  if (!view) {
    location.hash = "/overview";
    return;
  }
  const title =
    {
      "admin/reviews": "Deposit reviews",
      "admin/machines": "Machines",
      "admin/evacuations": "Evacuations",
      "admin/depots": "Receiving depots",
      "admin/audit": "Audit log",
      "admin/settings": "Staff & machines",
      "admin/withdrawals": "Withdrawals",
      "admin/support": "Support queue",
    }[current] || label(current.replace("admin/", ""));
  shell(title);
  try {
    const html = await view();
    if (ticket === generation) document.querySelector("#view").innerHTML = html;
  } catch (error) {
    if (ticket === generation) {
      document.querySelector("#view").innerHTML =
        pageHeading(title, "We couldn’t load this view.") +
        errorHTML(error) +
        button("Try again", "refresh");
    }
  }
}
window.addEventListener("hashchange", route);
try {
  me = await request("/me");
} catch (error) {
  if (error.status !== 401)
    console.info("Account session is unavailable. Sign in to continue.");
}
await route();
