"use client";

import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useEffect, useMemo, useState } from "react";
import { filterNavItems } from "../lib/rbac";
import OperatorCommandBar from "./OperatorCommandBar";

type LoggedInUser = {
  id?: string;
  name?: string;
  email?: string;
  roles?: string[];
};

type Organisation = {
  id: string;
  name: string;
  code: string;
  currency_code?: string;
};

type NavIcon =
  | "dashboard"
  | "organisation"
  | "workflow"
  | "users"
  | "subscription"
  | "vendor"
  | "licence"
  | "budget"
  | "payment"
  | "employee"
  | "renewal"
  | "audit"
  | "settings"
  | "logout"
  | "contracts"
  | "insights"
  | "copilot"
  | "recycle"
  | "inbox"
  | "mail";

const navItems = [
  { label: "Dashboard", href: "/dashboard", icon: "dashboard", roles: ["master_admin", "finance", "it_admin", "hr_admin", "auditor"] },
  { label: "Organisations", href: "/organisations", icon: "organisation", roles: ["master_admin"] },
  { label: "Workflows", href: "/workflows", icon: "workflow", roles: ["master_admin", "finance", "it_admin", "line_manager", "hr_admin"] },
  { label: "Tool Requests", href: "/tool-requests", icon: "inbox", roles: ["master_admin", "it_admin", "employee"] },
  { label: "Recycle Bin", href: "/recycle-bin", icon: "recycle", roles: ["master_admin"] },
  { label: "Users", href: "/users", icon: "users", roles: ["master_admin", "it_admin"] },
  { label: "Contracts", href: "/contracts", icon: "contracts", roles: ["master_admin", "it_admin", "finance", "auditor"] },
  { label: "Subscriptions", href: "/subscriptions", icon: "subscription", roles: ["master_admin", "it_admin", "finance", "hr_admin", "auditor"] },
  { label: "Vendors", href: "/vendors", icon: "vendor", roles: ["master_admin", "it_admin", "finance", "auditor"] },
  { label: "Licences", href: "/licences", icon: "licence", roles: ["master_admin", "it_admin", "finance", "hr_admin", "auditor"] },
  { label: "Budgets", href: "/budgets", icon: "budget", roles: ["master_admin", "finance", "auditor"] },
  { label: "Payments", href: "/payments", icon: "payment", roles: ["master_admin", "finance", "auditor"] },
  { label: "Employees", href: "/employees", icon: "employee", roles: ["master_admin", "it_admin", "hr_admin", "auditor"] },
  { label: "Renewals", href: "/renewals", icon: "renewal", roles: ["master_admin", "finance", "it_admin", "auditor"] },
  { label: "Insights", href: "/insights", icon: "insights", roles: ["master_admin", "finance", "auditor"] },
  { label: "AI Co-pilot", href: "/copilot", icon: "copilot", roles: ["master_admin", "finance", "it_admin", "hr_admin", "auditor"] },
  { label: "Audit logs", href: "/audit-logs", icon: "audit", roles: ["master_admin", "auditor"] },
  { label: "Diagnostics", href: "/diagnostics", icon: "audit", roles: ["master_admin"] },
];

const fallbackOrganisations: Organisation[] = [
  { id: "derisk360_group", code: "derisk360_group", name: "Derisk360 Group" },
];

function SidebarIcon({ name }: { name: NavIcon }) {
  const paths: Record<NavIcon, string> = {
    dashboard: "M4 13h6V4H4v9Zm10 7h6V4h-6v16ZM4 20h6v-4H4v4Z",
    organisation: "M4 21V7l8-4 8 4v14M9 21v-8h6v8M7 10h.01M17 10h.01",
    workflow: "M6 6h12M6 12h12M6 18h8M18 16l2 2-2 2",
    users: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75",
    subscription: "M7 4h10l3 4v12H4V4h3ZM14 4v5h6M8 13h8M8 17h5",
    vendor: "M3 21h18M5 21V7l8-4 6 4v14M9 21v-8h6v8",
    licence: "M12 3l7 4v5c0 5-3 8-7 9-4-1-7-4-7-9V7l7-4ZM9 12l2 2 4-4",
    budget: "M4 7h16M6 7v13h12V7M9 11h6M9 15h6M10 3h4v4",
    payment: "M3 7h18v10H3V7ZM3 10h18M7 15h3",
    employee: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21a8 8 0 0 1 16 0",
    renewal: "M20 12a8 8 0 0 1-13.7 5.7M4 12a8 8 0 0 1 13.7-5.7M18 3v4h-4M6 21v-4h4",
    audit: "M5 4h14v16H5V4ZM8 8h8M8 12h8M8 16h5",
    settings: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.4 1a7 7 0 0 0-1.7-1L14.5 3h-5l-.3 3.1a7 7 0 0 0-1.7 1l-2.4-1-2 3.4 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.4-1a7 7 0 0 0 1.7 1l.3 3.1h5l.3-3.1a7 7 0 0 0 1.7-1l2.4 1 2-3.4-2-1.5c.1-.3.1-.7.1-1Z",
    logout: "M10 17l5-5-5-5M15 12H3M21 3v18h-7",
    contracts: "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z",
    insights: "M9.663 17h4.673M12 3v1m6.364.364l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 113.536 0V21h-2v-3.343z",
    copilot: "M12 2a3 3 0 00-3 3v1H5a2 2 0 00-2 2v12a2 2 0 002 2h14a2 2 0 002-2V8a2 2 0 00-2-2h-4V5a3 3 0 00-3-3zm3 4H9V5a1 1 0 011-1h4a1 1 0 011 1v1zm-6 7a1 1 0 112 0 1 1 0 01-2 0zm6 0a1 1 0 112 0 1 1 0 01-2 0z",
    recycle: "M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16",
    inbox: "M4 6h16v12H4V6Zm0 0 8 7 8-7",
    mail: "M4 4h16a2 2 0 012 2v12a2 2 0 01-2 2H4a2 2 0 01-2-2V6a2 2 0 012-2Zm0 0 8 5 8-5",
  };

  return (
    <svg aria-hidden="true" fill="none" height="17" viewBox="0 0 24 24" width="17">
      <path d={paths[name]} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
    </svg>
  );
}

export default function AppShell({
  children,
  title,
  description,
}: {
  children: ReactNode;
  title: string;
  description: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<LoggedInUser | null>(null);
  const [checkingSession, setCheckingSession] = useState(true);
  const [organisations, setOrganisations] = useState<Organisation[]>(fallbackOrganisations);
  const [selectedCompany, setSelectedCompany] = useState(fallbackOrganisations[0].name);

  const handleCompanyChange = (companyName: string) => {
    setSelectedCompany(companyName);
    const org = organisations.find((o) => o.name === companyName);
    if (org) {
      sessionStorage.setItem("slmct_selected_org_id", org.id);
      sessionStorage.setItem("slmct_selected_org_currency", org.currency_code || "AED");
      window.dispatchEvent(new Event("slmct_org_changed"));
    }
  };

  useEffect(() => {
    const stored = sessionStorage.getItem("slmct_user");
    if (stored) {
      setUser(JSON.parse(stored));
      setCheckingSession(false);
      return;
    }

    router.replace("/");
  }, [router]);

  useEffect(() => {
    async function loadOrganisations() {
      try {
        const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
        const response = await fetch(`${baseUrl}/organisations`);
        if (!response.ok) return;
        const data = await response.json();
        if (Array.isArray(data) && data.length > 0) {
          const activeOrgs = data.filter((o: { is_active?: boolean }) => o.is_active !== false);
          if (activeOrgs.length === 0) return;
          setOrganisations(activeOrgs);

          const storedOrgId = sessionStorage.getItem("slmct_selected_org_id");
          const matchedOrg = storedOrgId ? activeOrgs.find((o: { id: string }) => o.id === storedOrgId) : null;
          if (matchedOrg) {
            setSelectedCompany(matchedOrg.name);
            sessionStorage.setItem("slmct_selected_org_currency", matchedOrg.currency_code);
          } else {
            setSelectedCompany(activeOrgs[0].name);
            sessionStorage.setItem("slmct_selected_org_id", activeOrgs[0].id);
            sessionStorage.setItem("slmct_selected_org_currency", activeOrgs[0].currency_code);
          }
          window.dispatchEvent(new Event("slmct_org_changed"));
        }
      } catch {
        setOrganisations(fallbackOrganisations);
      }
    }

    loadOrganisations();
    window.addEventListener("slmct_orgs_updated", loadOrganisations);
    return () => window.removeEventListener("slmct_orgs_updated", loadOrganisations);
  }, []);

  const visibleNavItems = useMemo(() => filterNavItems(navItems, user?.roles ?? []), [user]);

  async function handleLogout() {
    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
      await fetch(`${baseUrl}/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: user?.id ?? null,
          email: user?.email ?? null,
        }),
      });
    } finally {
      sessionStorage.removeItem("slmct_user");
      router.push("/");
    }
  }

  if (checkingSession) {
    return (
      <main className="app-shell">
        <section className="app-main">
          <p className="module-message">Checking secure session...</p>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <aside className="app-sidebar" aria-label="Main navigation">
        <div className="sidebar-logo">
          <img src="/derisk-logo-png.avif" alt="Derisk360" />
          <span>SLMCT Platform</span>
        </div>

        <nav className="sidebar-nav">
          {visibleNavItems.map((item) => (
            <a className={pathname === item.href ? "active" : ""} href={item.href} key={item.href}>
              <SidebarIcon name={item.icon as NavIcon} />
              <span>{item.label}</span>
            </a>
          ))}
        </nav>

      </aside>

      <section className="app-main">
        <nav className="top-navbar" aria-label="Account navigation">
          <label className="company-switcher compact-switcher">
            <span>Selected organisation</span>
            <select value={selectedCompany} onChange={(event) => handleCompanyChange(event.target.value)}>
              {organisations.map((company) => (
                <option value={company.name} key={company.id}>
                  {company.name}
                </option>
              ))}
            </select>
          </label>
          <div className="account-menu">
            <div className="avatar" aria-hidden="true">
              {(user?.name ?? "A").slice(0, 1).toUpperCase()}
            </div>
            <div className="account-copy">
              <strong>{user?.name ?? "Master Admin"}</strong>
              <span>{user?.email ?? "Not signed in"} · {user?.roles?.join(", ") || "master_admin"}</span>
            </div>
            <a href="/settings" title="Settings">
              <SidebarIcon name="settings" />
            </a>
            <button type="button" onClick={handleLogout} title="Logout" aria-label="Logout">
              <SidebarIcon name="logout" />
            </button>
          </div>
        </nav>

        <header className="app-header">
          <div>
            <h1>{title}</h1>
            <p>{description}</p>
          </div>
        </header>

        <div className="scope-banner">
          <span>Current reporting scope</span>
          <strong>{selectedCompany}</strong>
        </div>

        {children}
        <OperatorCommandBar />
      </section>
    </main>
  );
}
