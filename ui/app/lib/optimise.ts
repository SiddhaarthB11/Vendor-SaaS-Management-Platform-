export type OptCategory = "licence" | "governance" | "budget";
export type Severity = "high" | "medium" | "low";

export interface OptItem {
  id: string;
  label: string;
  sub?: string;
  right?: string;
  rightTone?: "default" | "danger" | "warning" | "success";
}

export type ReclaimKind = "unassign" | "decommission";

export interface Recommendation {
  id: string;
  category: OptCategory;
  severity: Severity;
  title: string;
  summary: string;
  recommendation: string;
  items?: OptItem[];
  metric?: { label: string; value: string; tone?: "danger" | "warning" | "success" };
  reclaim?: { kind: ReclaimKind; label: string };
}

export interface OptimiseResult {
  recommendations: Recommendation[];
  idleLicences: number;
  budgetAlerts: number;
  expiringSoon: number;
  governanceIssues: number;
}

const CYCLE_MULTIPLIER: Record<string, number> = {
  monthly: 12,
  quarterly: 4,
  annual: 1,
};

const SEVERITY_RANK: Record<Severity, number> = { high: 0, medium: 1, low: 2 };
const NEAR_EXPIRY_DAYS = 30;
const BUDGET_WARN_RATIO = 0.8;

function daysUntil(isoDate: string): number | null {
  if (!isoDate) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const target = new Date(isoDate + "T00:00:00");
  if (Number.isNaN(target.getTime())) return null;
  return Math.round((target.getTime() - today.getTime()) / 86_400_000);
}

function formatAmount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function computeOptimisations(
  licences: any[],
  budgets: any[],
  payments: any[],
  subscriptions: any[],
  employees: any[],
  vendors: any[],
  currency: string,
  fxRates: Record<string, number> = {}
): OptimiseResult {
  const toTargetCurrency = (amount: number, fromCurrency: string): number => {
    if (!fromCurrency || fromCurrency === currency || !fxRates[fromCurrency]) return amount;
    return (amount / fxRates[fromCurrency]) * (fxRates[currency] ?? 1);
  };
  const recommendations: Recommendation[] = [];

  const subById = (id: string) => subscriptions.find((s) => s.id === id);
  const empById = (id: string) => employees.find((e) => e.id === id);

  // Estimated annual per-seat cost, derived from the linked subscription
  // divided by the number of seats sharing that subscription.
  const seatsPerSub = new Map<string, number>();
  for (const l of licences) {
    if (l.subscription_id)
      seatsPerSub.set(l.subscription_id, (seatsPerSub.get(l.subscription_id) ?? 0) + 1);
  }
  
  const perSeatCost = (subscriptionId: string) => {
    const sub = subById(subscriptionId);
    if (!sub || !sub.amount) return null;
    const annual = Number(sub.amount) * (CYCLE_MULTIPLIER[sub.billing_cycle] ?? 1);
    const seats = Math.max(seatsPerSub.get(subscriptionId) ?? 1, 1);
    return { amount: annual / seats, currency: sub.currency_code };
  };
  
  const costLabel = (subscriptionId: string) => {
    const c = perSeatCost(subscriptionId);
    return c ? `${formatAmount(c.amount)} ${c.currency}/yr` : "Cost unknown";
  };

  // --- Idle licences: paid-for but unassigned seats
  const idle = licences.filter((l) => l.status === "available" || !l.assigned_to_person_id);
  if (idle.length) {
    recommendations.push({
      id: "opt_idle",
      category: "licence",
      severity: "high",
      title: `${idle.length} idle licence${idle.length === 1 ? "" : "s"}`,
      summary:
        "These seats are paid for but not assigned to any employee — reclaimable spend.",
      recommendation:
        "Reclaim or reassign these seats, or drop them at the next renewal to stop paying for unused capacity.",
      metric: {
        label: "Reclaimable seats",
        value: String(idle.length),
        tone: "warning",
      },
      reclaim: { kind: "decommission", label: "Decommission" },
      items: idle.map((l) => ({
        id: l.id,
        label: l.licence_name,
        sub: l.subscription_id ? subById(l.subscription_id)?.name ?? "" : "",
        right: l.subscription_id ? costLabel(l.subscription_id) : undefined,
        rightTone: "warning",
      })),
    });
  }

  // --- Licences assigned to inactive employees (waste + access risk)
  const orphaned = licences.filter((l) => {
    if (!l.assigned_to_person_id) return false;
    const emp = empById(l.assigned_to_person_id);
    return emp?.status === "inactive" || emp?.status === "left_org";
  });
  if (orphaned.length) {
    recommendations.push({
      id: "opt_orphaned",
      category: "governance",
      severity: "high",
      title: `${orphaned.length} licence${
        orphaned.length === 1 ? "" : "s"
      } held by inactive employees`,
      summary:
        "Active seats are still attached to employees marked inactive or left the organisation — both wasted spend and an access-governance risk.",
      recommendation:
        "Revoke immediately and return the seats to the available pool.",
      metric: { label: "Access risk", value: String(orphaned.length), tone: "danger" },
      reclaim: { kind: "unassign", label: "Reclaim seat" },
      items: orphaned.map((l) => ({
        id: l.id,
        label: l.licence_name,
        sub: `${empById(l.assigned_to_person_id)?.full_name ?? "Unknown"} · inactive`,
        right: l.subscription_id ? costLabel(l.subscription_id) : undefined,
        rightTone: "danger",
      })),
    });
  }

  // --- Licences expiring soon or already expired
  const expiring = licences
    .map((l) => ({ l, d: daysUntil(l.expires_at) }))
    .filter((x) => x.d !== null && x.d <= NEAR_EXPIRY_DAYS)
    .sort((a, b) => (a.d ?? 0) - (b.d ?? 0));
  if (expiring.length) {
    const expired = expiring.filter((x) => (x.d ?? 0) < 0).length;
    recommendations.push({
      id: "opt_expiring",
      category: "licence",
      severity: expired ? "high" : "medium",
      title: `${expiring.length} licence${
        expiring.length === 1 ? "" : "s"
      } expiring within ${NEAR_EXPIRY_DAYS} days`,
      summary: expired
        ? `${expired} already expired and ${expiring.length - expired} due soon.`
        : "Seats approaching expiry — confirm whether to renew or decommission.",
      recommendation:
        "Decide renew vs. drop ahead of expiry so you neither lose access unexpectedly nor auto-renew unused seats.",
      items: expiring.map(({ l, d }) => ({
        id: l.id,
        label: l.licence_name,
        sub: `Expires ${l.expires_at || "—"}`,
        right: d !== null && d < 0 ? `${Math.abs(d)}d overdue` : `${d}d left`,
        rightTone: d !== null && d < 0 ? "danger" : "warning",
      })),
    });
  }

  // --- Budget utilisation: tracked spend vs allocated, per budget
  for (const b of budgets) {
    const allocated = toTargetCurrency(Number(b.allocated_amount), b.currency_code);
    const spend = payments
      .filter(
        (p) =>
          p.budget_id === b.id &&
          p.status !== "cancelled" &&
          p.status !== "failed"
      )
      .reduce((a, p) => a + toTargetCurrency(Number(p.amount), p.currency_code), 0);
    if (spend <= 0 || allocated <= 0) continue;
    const ratio = spend / allocated;
    if (ratio < BUDGET_WARN_RATIO) continue;

    const over = ratio >= 1;
    recommendations.push({
      id: `opt_budget_${b.id}`,
      category: "budget",
      severity: over ? "high" : "medium",
      title: `${b.department} ${b.fiscal_year} budget ${
        over ? "exceeded" : "near limit"
      }`,
      summary: `Tracked spend is ${formatAmount(Math.round(spend))} against an allocation of ${formatAmount(
        Math.round(allocated)
      )} ${currency} (${Math.round(ratio * 100)}%).`,
      recommendation: over
        ? "Spend has surpassed the allocation — reforecast the budget or halt non-critical payments."
        : "Spend is approaching the cap — review upcoming payments before committing more.",
      metric: {
        label: "Utilisation",
        value: `${Math.round(ratio * 100)}%`,
        tone: over ? "danger" : "warning",
      },
    });
  }

  recommendations.sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);

  return {
    recommendations,
    idleLicences: idle.length,
    budgetAlerts: recommendations.filter((r) => r.category === "budget").length,
    expiringSoon: expiring.length,
    governanceIssues: orphaned.length,
  };
}
