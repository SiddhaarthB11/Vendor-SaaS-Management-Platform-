export interface RenewalRow {
  id: string;
  type: "subscription" | "licence" | "vendor" | "contract";
  name: string;
  vendor_name: string;
  renewal_date: string;
  amount: number | null;
  currency_code: string;
  status: string;
}

export type Urgency = "overdue" | "red" | "amber" | "green";

export function daysUntil(isoDate: string): number | null {
  if (!isoDate) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(isoDate + "T00:00:00");
  if (Number.isNaN(target.getTime())) return null;
  return Math.round((target.getTime() - today.getTime()) / 86_400_000);
}

export function urgencyFor(isoDate: string): Urgency | null {
  const d = daysUntil(isoDate);
  if (d === null) return null;
  if (d < 0) return "overdue";
  if (d < 30) return "red";
  if (d < 60) return "amber";
  if (d <= 90) return "green";
  return null;
}

export function computeRenewals(
  subscriptions: any[],
  licences: any[],
  vendors: any[],
  contracts: any[]
): RenewalRow[] {
  const vendorName = (id: string) =>
    vendors.find((v) => v.id === id)?.name ?? "-";
  const out: RenewalRow[] = [];

  for (const s of subscriptions) {
    if (!s.renewal_date) continue;
    out.push({
      id: `sub_${s.id}`,
      type: "subscription",
      name: s.name,
      vendor_name: vendorName(s.vendor_id),
      renewal_date: s.renewal_date,
      amount: Number(s.amount) || 0,
      currency_code: s.currency_code,
      status: s.status,
    });
  }

  for (const l of licences) {
    if (!l.expires_at) continue;
    const sub = subscriptions.find((s) => s.id === l.subscription_id);
    out.push({
      id: `lic_${l.id}`,
      type: "licence",
      name: l.licence_name,
      vendor_name: sub ? vendorName(sub.vendor_id) : "-",
      renewal_date: l.expires_at,
      amount: null,
      currency_code: "",
      status: l.status,
    });
  }

  for (const c of contracts.filter(
    (ct) => ct.status === "active" || ct.status === "renewed"
  )) {
    if (!c.end_date) continue;
    out.push({
      id: `ctr_${c.id}`,
      type: "contract",
      name: c.title,
      vendor_name: vendorName(c.vendor_id),
      renewal_date: c.end_date,
      amount: Number(c.value) || null,
      currency_code: c.currency_code,
      status: c.status,
    });
  }

  return out.sort((a, b) =>
    (a.renewal_date || "9999").localeCompare(b.renewal_date || "9999")
  );
}
