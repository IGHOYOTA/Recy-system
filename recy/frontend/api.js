export class APIError extends Error {
  constructor(result, status) {
    super(result.error || "Request could not be completed");
    this.code = result.code;
    this.status = status;
    this.reference = result.request_id;
  }
}
export async function request(
  path,
  { method = "GET", body, key, signal } = {},
) {
  const response = await fetch("/api/v1/browser" + path, {
    method,
    credentials: "same-origin",
    signal,
    headers: {
      "Content-Type": "application/json",
      "X-Recy-Browser": "1",
      ...(key ? { "Idempotency-Key": key } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new APIError(result, response.status);
  return result;
}
export const money = (value) =>
  new Intl.NumberFormat("en-CA", { style: "currency", currency: "CAD" }).format(
    Number(value || 0) / 100,
  );
export const date = (value) =>
  value
    ? new Intl.DateTimeFormat("en-CA", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
export const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export function groupLedger(rows) {
  const grouped = new Map();
  for (const row of rows) {
    const item = grouped.get(row.id) || {
      ...row,
      available_cents: 0,
      reserved_cents: 0,
    };
    if (row.purpose === "user_available")
      item.available_cents += Number(row.amount_cents);
    if (row.purpose === "user_reserved")
      item.reserved_cents += Number(row.amount_cents);
    grouped.set(row.id, item);
  }
  return [...grouped.values()];
}
