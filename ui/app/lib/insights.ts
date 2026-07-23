export type FindingType = "duplicate" | "overlap";
export type Severity = "high" | "medium";

export interface FindingItem {
  subscriptionId: string;
  name: string;
  vendorName: string;
  category: string;
  billing_cycle: string;
  amount: number;
  currency_code: string;
  annualised: number;
}

export interface Finding {
  id: string;
  type: FindingType;
  severity: Severity;
  title: string;
  summary: string;
  recommendation: string;
  items: FindingItem[];
  potentialAnnualSaving: number | null;
  currency: string | null;
}

export interface InsightsResult {
  findings: Finding[];
  analysedCount: number;
  duplicateCount: number;
  overlapCount: number;
  redundantLines: number;
}

const CYCLE_MULTIPLIER: Record<string, number> = {
  monthly: 12,
  quarterly: 4,
  annual: 1,
};

function annualise(amount: number, cycle: string): number {
  return amount * (CYCLE_MULTIPLIER[cycle] ?? 1);
}

function normalise(value: string): string {
  if (!value) return "";
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

export function computeInsights(
  subscriptions: any[],
  vendors: any[]
): InsightsResult {
  const vendorName = (id: string) =>
    vendors.find((v) => v.id === id)?.name ?? "—";

  const active = subscriptions.filter((s) => s.status !== "cancelled" && s.status !== "inactive");

  const toItem = (s: any): FindingItem => ({
    subscriptionId: s.id,
    name: s.name,
    vendorName: vendorName(s.vendor_id),
    category: s.category || "Uncategorised",
    billing_cycle: s.billing_cycle,
    amount: Number(s.amount) || 0,
    currency_code: s.currency_code,
    annualised: annualise(Number(s.amount) || 0, s.billing_cycle),
  });

  const findings: Finding[] = [];

  // --- Lens 1: duplicate products (same normalised name, more than one line)
  const byName = new Map<string, any[]>();
  for (const s of active) {
    const key = normalise(s.name);
    if (!key) continue;
    byName.set(key, [...(byName.get(key) ?? []), s]);
  }

  const duplicateNameKeys = new Set<string>();
  for (const [key, group] of byName) {
    if (group.length < 2) continue;
    duplicateNameKeys.add(key);
    const items = group.map(toItem).sort((a, b) => b.annualised - a.annualised);
    const currencies = new Set(items.map((i) => i.currency_code));
    const sameCurrency = currencies.size === 1;
    const total = items.reduce((a, i) => a + i.annualised, 0);
    const keep = items[0]?.annualised ?? 0; // keep the largest single line
    findings.push({
      id: `dup_${key}`,
      type: "duplicate",
      severity: "high",
      title: `${group[0].name} — ${group.length} active subscriptions`,
      summary: `${group.length} separate subscription lines share the same product name.`,
      recommendation:
        "Likely duplicate billing or fragmented purchasing. Consolidate onto a single agreement and cancel the redundant lines.",
      items,
      potentialAnnualSaving: sameCurrency ? total - keep : null,
      currency: sameCurrency ? items[0].currency_code : null,
    });
  }

  // --- Lens 2: category overlap (a category served by 2+ distinct products)
  const byCategory = new Map<string, any[]>();
  for (const s of active) {
    const key = normalise(s.category);
    if (!key) continue;
    byCategory.set(key, [...(byCategory.get(key) ?? []), s]);
  }

  for (const [, group] of byCategory) {
    const distinctProducts = new Map<string, any>();
    for (const s of group) {
      const nk = normalise(s.name);
      if (duplicateNameKeys.has(nk)) continue; // already covered by Lens 1
      if (!distinctProducts.has(nk)) distinctProducts.set(nk, s);
    }
    if (distinctProducts.size < 2) continue;
    const reps = [...distinctProducts.values()];
    const items = reps.map(toItem).sort((a, b) => b.annualised - a.annualised);
    findings.push({
      id: `ovl_${normalise(group[0].category)}`,
      type: "overlap",
      severity: "medium",
      title: `${group[0].category || "Uncategorised"} — ${reps.length} overlapping tools`,
      summary: `Multiple distinct products cover the "${group[0].category}" category.`,
      recommendation:
        "Evaluate whether these tools can be consolidated to reduce overlap and licensing spend.",
      items,
      potentialAnnualSaving: null,
      currency: null,
    });
  }

  findings.sort((a, b) => {
    if (a.severity !== b.severity) return a.severity === "high" ? -1 : 1;
    return (b.potentialAnnualSaving ?? 0) - (a.potentialAnnualSaving ?? 0);
  });

  const redundantLines = findings
    .filter((f) => f.type === "duplicate")
    .reduce((a, f) => a + (f.items.length - 1), 0);

  return {
    findings,
    analysedCount: active.length,
    duplicateCount: findings.filter((f) => f.type === "duplicate").length,
    overlapCount: findings.filter((f) => f.type === "overlap").length,
    redundantLines,
  };
}
