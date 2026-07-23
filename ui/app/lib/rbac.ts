export const MODULE_ROLES: Record<string, string[]> = {
  dashboard: ["master_admin", "finance", "it_admin", "hr_admin", "auditor", "employee", "line_manager"],
  organisations: ["master_admin"],
  workflows: ["master_admin", "finance", "it_admin", "line_manager", "hr_admin", "auditor", "employee"],
  "tool-requests": ["master_admin", "it_admin", "employee"],
  "recycle-bin": ["master_admin"],
  users: ["master_admin", "it_admin"],
  contracts: ["master_admin", "it_admin", "finance", "auditor"],
  subscriptions: ["master_admin", "it_admin", "finance", "hr_admin", "auditor", "line_manager"],
  vendors: ["master_admin", "it_admin", "finance", "auditor"],
  licences: ["master_admin", "it_admin", "finance", "hr_admin", "auditor", "line_manager"],
  budgets: ["master_admin", "finance", "auditor"],
  payments: ["master_admin", "finance", "auditor"],
  employees: ["master_admin", "it_admin", "hr_admin", "auditor"],
  renewals: ["master_admin", "finance", "it_admin", "auditor"],
  insights: ["master_admin", "finance", "auditor"],
  copilot: ["master_admin", "finance", "it_admin", "hr_admin", "auditor"],
  "audit-logs": ["master_admin", "auditor"],
  settings: ["master_admin", "finance", "it_admin", "hr_admin", "auditor"],
};

export function canAccessModule(moduleKey: string, roles: string[] = []): boolean {
  if (!roles.length) return false;
  if (roles.includes("master_admin")) return true;
  if (moduleKey === "settings" || moduleKey === "dashboard") return true;
  const allowed = MODULE_ROLES[moduleKey];
  if (!allowed) return false;
  return allowed.some((role) => roles.includes(role));
}

export function canViewDashboardBudget(roles: string[] = []): boolean {
  const normalized = normalizeRoles(roles);
  return normalized.includes("master_admin") || normalized.includes("finance");
}

export function normalizeRoles(roles: unknown): string[] {
  if (Array.isArray(roles)) {
    return roles.map((role) => String(role).trim()).filter(Boolean);
  }
  if (typeof roles === "string") {
    const trimmed = roles.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("[")) {
      try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) {
          return parsed.map((role) => String(role).trim()).filter(Boolean);
        }
      } catch {
        // fall through to comma split
      }
    }
    return trimmed.split(",").map((role) => role.trim()).filter(Boolean);
  }
  return [];
}

export function canViewFinancialSpend(roles: string[] = []): boolean {
  return roles.includes("master_admin") || roles.includes("finance") || roles.includes("auditor");
}

export function canViewProcurementDetails(roles: string[] = []): boolean {
  return roles.includes("master_admin") || roles.includes("finance") || roles.includes("auditor");
}

export function canViewRenewalAmounts(roles: string[] = []): boolean {
  return canViewFinancialSpend(roles);
}

export function canConfigureEmail(roles: string[] = []): boolean {
  return roles.includes("master_admin") || roles.includes("it_admin");
}

export type NavItem = {
  label: string;
  href: string;
  icon: string;
  roles: string[];
};

export function filterNavItems<T extends NavItem>(items: T[], userRoles: string[] = []): T[] {
  return items.filter((item) => {
    const moduleKey = item.href.replace(/^\//, "");
    return canAccessModule(moduleKey, userRoles);
  });
}
