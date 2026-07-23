"use client";

import { useParams } from "next/navigation";
import { ChangeEvent, FormEvent, Fragment, useEffect, useMemo, useRef, useState } from "react";
import AppShell from "../components/AppShell";
import DevtoolsControlPanel from "../components/DevtoolsControlPanel";
import WorkflowSubmissionModal, {
  SubmissionEmailOverride,
  SubmissionResult,
} from "../components/WorkflowSubmissionModal";
import { getApprovalRoutingPreview } from "../lib/workflow-submission-forms";
import { WorkflowType } from "../lib/workflow-governance";
import { computeInsights } from "../lib/insights";
import { computeOptimisations, Recommendation } from "../lib/optimise";
import { computeRenewals, daysUntil, urgencyFor } from "../lib/renewals";
import {
  canAccessModule,
  canConfigureEmail,
  canViewDashboardBudget,
  canViewFinancialSpend,
  canViewProcurementDetails,
  canViewRenewalAmounts,
  normalizeRoles,
} from "../lib/rbac";
import {
  canLineManagerApprove,
  getRoleDashboardSections,
  requiresLineManager,
  resolveWorkflowType,
  workflowTypeLabel,
} from "../lib/workflow-governance";
import {
  canDirectCreateSoftwareRecords,
  canManageToolRequestInbox,
  canSubmitEmployeeSoftwareRequest,
  canSubmitSoftwareWorkflow,
  getAllowedWorkflowTypes,
} from "../lib/workflow-submission-permissions";

type AnyRecord = Record<string, unknown>;

type FieldConfig = {
  name: string;
  label: string;
  type?: "text" | "email" | "number" | "date" | "password" | "textarea" | "select" | "checkbox";
  required?: boolean;
  options?: string[];
  placeholder?: string;
  source?: "organisations" | "vendors" | "subscriptions" | "budgets" | "employees" | "roles";
};

type ModuleConfig = {
  title: string;
  description: string;
  endpoint: string;
  tableFields: string[];
  createFields: FieldConfig[];
  readOnly?: boolean;
  workflowCreate?: boolean;
  bulkUpload?: boolean;
  hideActions?: boolean;
  exportXlsx?: boolean;
};

type LoggedInUser = {
  id?: string;
  name?: string;
  email?: string;
  roles?: string[];
  organisation_id?: string;
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const itWorkflowModules = new Set(["organisations", "vendors", "subscriptions", "licences", "contracts"]);
const hrWorkflowModules = new Set(["subscriptions", "licences"]);
const financeWorkflowModules = new Set(["budgets", "payments", "contracts"]);
const bulkUploadModules = new Set(["vendors", "subscriptions", "licences", "budgets", "payments", "employees", "contracts"]);
const currencyOptions = ["AED", "INR", "GBP", "USD", "EUR", "SAR", "QAR", "OMR", "BHD", "KWD"];
const countryCodeAliases: Record<string, string> = {
  ae: "AE",
  uae: "AE",
  "united arab emirates": "AE",
  in: "IN",
  india: "IN",
  gb: "GB",
  uk: "GB",
  "united kingdom": "GB",
  us: "US",
  usa: "US",
  "united states": "US",
  sa: "SA",
  "saudi arabia": "SA",
  qa: "QA",
  qatar: "QA",
  om: "OM",
  oman: "OM",
  bh: "BH",
  bahrain: "BH",
  kw: "KW",
  kuwait: "KW",
};

const moduleConfigs: Record<string, ModuleConfig> = {
  "recycle-bin": {
    title: "Recycle Bin",
    description: "Browse soft-deleted/archived items and restore them to active use.",
    endpoint: "/recycle-bin",
    tableFields: ["name", "module", "status", "deleted_at"],
    createFields: [],
    readOnly: true,
  },
  organisations: {
    title: "Organisations",
    description:
      "Organisations allow parent companies to manage sister entities with separate budgets, employees, subscriptions, and workflows. Create and manage Derisk360 sister entities used for reporting scope and record ownership.",
    endpoint: "/organisations",
    tableFields: ["name", "code", "country_code", "currency_code", "is_sister_entity", "is_active"],
    createFields: [
      { name: "name", label: "Organisation name", required: true },
      { name: "code", label: "Code", required: true },
      { name: "country_code", label: "Country code", type: "select", options: ["AE", "IN", "GB", "US", "SA", "QA", "OM", "BH", "KW"], required: true },
      { name: "currency_code", label: "Currency", type: "select", options: currencyOptions, required: true },
      { name: "website_url", label: "Website URL" },
      { name: "is_sister_entity", label: "Sister entity", type: "checkbox" },
    ],
    workflowCreate: true,
  },
  vendors: {
    title: "Vendors",
    description: "Maintain vendor master data, commercial contacts, websites, and active/inactive state.",
    endpoint: "/vendors",
    tableFields: ["name", "legal_name", "contact_email", "status"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations" },
      { name: "name", label: "Vendor name", required: true },
      { name: "legal_name", label: "Legal name" },
      { name: "website_url", label: "Website URL" },
      { name: "contact_name", label: "Contact name" },
      { name: "contact_email", label: "Contact email", type: "email" },
      { name: "status", label: "Status", type: "select", options: ["active", "inactive", "archived"] },
      { name: "soc2_certified", label: "SOC 2 Certified", type: "select", options: ["true", "false"] },
      { name: "iso27001_certified", label: "ISO 27001 Certified", type: "select", options: ["true", "false"] },
      { name: "gdpr_compliant", label: "GDPR Compliant", type: "select", options: ["true", "false"] },
      { name: "data_residency", label: "Data Residency", placeholder: "e.g. EU, US, Global" },
    ],
    bulkUpload: true,
    exportXlsx: true,
  },
  subscriptions: {
    title: "Subscriptions",
    description: "Track software subscriptions, ownership, renewal dates, billing cycle, and spend baseline.",
    endpoint: "/subscriptions",
    tableFields: ["name", "category", "department", "renewal_date", "billing_cycle", "amount", "currency_code", "status"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "vendor_id", label: "Vendor", type: "select", source: "vendors" },
      { name: "name", label: "Subscription name", required: true },
      { name: "category", label: "Category" },
      { name: "department", label: "Department", type: "select", options: ["Software Engineering", "Human Resources", "Finance & Accounts", "Marketing", "Sales", "IT", "Product", "Operations"] },
      { name: "start_date", label: "Start date", type: "date" },
      { name: "renewal_date", label: "Renewal date", type: "date" },
      { name: "billing_cycle", label: "Billing cycle", type: "select", options: ["monthly", "quarterly", "annual"] },
      { name: "amount", label: "Amount", type: "number" },
      { name: "currency_code", label: "Currency", type: "select", options: currencyOptions },
      {
        name: "status",
        label: "Status",
        type: "select",
        options: ["active", "trial", "pending_renewal", "cancelled", "expired"],
      },
      { name: "notes", label: "Tool purpose / notes", type: "textarea", placeholder: "Describe how this tool will be used, e.g. team communication and internal meetings." },
    ],
    workflowCreate: true,
    bulkUpload: true,
    exportXlsx: true,
  },
  licences: {
    title: "Licences",
    description: "Manage licence assignments, employee allocation, and expiry dates.",
    endpoint: "/licences",
    tableFields: ["licence_name", "assigned_at", "expires_at", "status"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "subscription_id", label: "Subscription", type: "select", source: "subscriptions" },
      { name: "assigned_to_person_id", label: "Assigned employee", type: "select", source: "employees" },
      { name: "licence_name", label: "Licence name", required: true },
      { name: "assigned_at", label: "Assigned date", type: "date" },
      { name: "expires_at", label: "Expiry date", type: "date" },
      { name: "status", label: "Status", type: "select", options: ["available", "assigned", "suspended", "revoked", "expired"] },
      { name: "notes", label: "Tool purpose / notes", type: "textarea", placeholder: "Describe how this licence will be used." },
    ],
    workflowCreate: true,
    bulkUpload: true,
    exportXlsx: true,
  },
  budgets: {
    title: "Budgets",
    description: "Maintain department budgets by organisation and fiscal year for spend governance.",
    endpoint: "/budgets",
    readOnly: true,
    tableFields: ["fiscal_year", "department", "allocated_amount", "currency_code", "status"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "fiscal_year", label: "Fiscal year", type: "number", required: true },
      { name: "department", label: "Department", type: "select", options: ["Software Engineering", "Human Resources", "Finance & Accounts", "Marketing", "Sales", "IT", "Product", "Operations"], required: true },
      { name: "allocated_amount", label: "Allocated amount", type: "number", required: true },
      { name: "currency_code", label: "Currency", type: "select", options: currencyOptions },
      { name: "status", label: "Status", type: "select", options: ["draft", "approved", "locked", "closed"] },
      { name: "notes", label: "Notes", type: "textarea" },
    ],
  },
  payments: {
    title: "Payments",
    description: "Track planned, pending, paid, failed, and cancelled payments against vendors and budgets.",
    endpoint: "/payments",
    tableFields: ["name", "due_date", "payment_date", "amount", "currency_code", "status", "reference"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "subscription_id", label: "Subscription", type: "select", source: "subscriptions" },
      { name: "vendor_id", label: "Vendor", type: "select", source: "vendors" },
      { name: "budget_id", label: "Budget", type: "select", source: "budgets" },
      { name: "name", label: "Payment for", required: true, placeholder: "e.g. Microsoft 365 annual renewal" },
      { name: "due_date", label: "Due date", type: "date" },
      { name: "payment_date", label: "Payment date", type: "date" },
      { name: "amount", label: "Amount", type: "number", required: true },
      { name: "currency_code", label: "Currency", type: "select", options: currencyOptions },
      { name: "status", label: "Status", type: "select", options: ["planned", "pending", "paid", "failed", "cancelled"] },
      { name: "reference", label: "Reference / invoice no." },
      { name: "notes", label: "Notes", type: "textarea" },
    ],
    workflowCreate: true,
    bulkUpload: true,
    exportXlsx: true,
  },
  users: {
    title: "Application Users",
    description: "Provision people who actively use SLMCT and assign role-based access. Use your real Outlook or company emails — legacy @derisk360.local and @demo.derisk360.com accounts can be removed with one click.",
    endpoint: "/users",
    tableFields: ["full_name", "work_email", "roles", "user_status", "must_change_password", "temp_password_expires_at"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "full_name", label: "Full name", required: true },
      { name: "work_email", label: "Work email", type: "email", required: true, placeholder: "name@outlook.com or name@company.com" },
      { name: "role_code", label: "Application role", type: "select", source: "roles", required: true },
      { name: "password", label: "Temporary password (optional)", type: "password" },
    ],
    exportXlsx: true,
  },
  employees: {
    title: "Employees",
    description: "Employee records synced from your Microsoft 365 tenant (or configured app users) for licence allocation.",
    endpoint: "/employees",
    tableFields: ["employee_number", "full_name", "work_email", "department", "job_title", "line_manager_email", "status"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "employee_number", label: "Employee number" },
      { name: "full_name", label: "Full name", required: true },
      { name: "work_email", label: "Work email", type: "email", required: true },
      { name: "department", label: "Department" },
      { name: "job_title", label: "Job title" },
      { name: "line_manager_email", label: "Line Manager Email", type: "email", placeholder: "manager@company.com" },
      { name: "status", label: "Status", type: "select", options: ["active", "inactive", "left_org"] },
    ],
    bulkUpload: true,
    exportXlsx: true,
  },
  workflows: {
    title: "Workflows",
    description: "Review IT-submitted activation requests, approve from Finance, and complete activation after payment confirmation.",
    endpoint: "/workflow-requests",
    tableFields: ["created_at", "requested_module", "requested_action", "status", "requested_by_email", "activated_entity_id"],
    createFields: [],
    readOnly: true,
  },
  "tool-requests": {
    title: "Tool Requests",
    description: "Review employee software requests and convert valid requests into procurement workflows.",
    endpoint: "/tool-requests",
    tableFields: ["requester_name", "requester_email", "department", "requested_tool", "created_at", "status"],
    createFields: [],
    readOnly: true,
    hideActions: true,
  },
  renewals: {
    title: "Renewals",
    description: "Monitor upcoming vendor, licence, and subscription renewals across the selected operating scope.",
    endpoint: "/renewals",
    tableFields: ["renewal_type", "name", "vendor_name", "renewal_date", "amount", "currency_code", "status"],
    createFields: [],
    readOnly: true,
  },
  settings: {
    title: "Settings",
    description: "Manage your profile security, Outlook email delivery, and change your password.",
    endpoint: "/settings/change-password",
    tableFields: [],
    createFields: [],
    readOnly: true,
  },
  "audit-logs": {
    title: "Audit logs",
    description: "Review security-relevant actions across authentication and operational records.",
    endpoint: "/audit-logs",
    tableFields: ["created_at", "actor_email", "action", "entity_type", "entity_id"],
    createFields: [],
    readOnly: true,
    hideActions: true,
    exportXlsx: true,
  },
  diagnostics: {
    title: "Diagnostics",
    description: "Run automated end-to-end tests to verify all workflows and features are working correctly.",
    endpoint: "",
    tableFields: [],
    createFields: [],
    readOnly: true,
    hideActions: true,
  },
  contracts: {
    title: "Contracts",
    description: "Manage vendor contracts, values, expiry dates, auto-renewal terms, and attached files.",
    endpoint: "/contracts",
    tableFields: ["title", "contract_number", "contract_type", "end_date", "value", "currency_code", "status"],
    createFields: [
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", required: true },
      { name: "vendor_id", label: "Vendor", type: "select", source: "vendors" },
      { name: "subscription_id", label: "Subscription", type: "select", source: "subscriptions" },
      { name: "title", label: "Contract title", required: true },
      { name: "contract_number", label: "Contract number" },
      { name: "contract_type", label: "Contract type", type: "select", options: ["MSA", "SaaS", "Order Form", "SOW", "NDA", "Other"] },
      { name: "start_date", label: "Start date", type: "date" },
      { name: "end_date", label: "End date", type: "date" },
      { name: "value", label: "Value", type: "number" },
      { name: "currency_code", label: "Currency", type: "select", options: currencyOptions },
      { name: "auto_renew", label: "Auto renew", type: "checkbox" },
      { name: "notice_period_days", label: "Notice period (days)", type: "number" },
      { name: "owner", label: "Internal owner" },
      { name: "status", label: "Status", type: "select", options: ["draft", "active", "expired", "terminated", "renewed"] },
      { name: "notes", label: "Notes", type: "textarea" },
    ],
    bulkUpload: true,
    exportXlsx: true,
  },
  insights: {
    title: "Insights & Optimisation",
    description: "Spend analysis, duplicate detection, and license allocation optimization.",
    endpoint: "",
    tableFields: [],
    createFields: [],
    readOnly: true,
  },
  copilot: {
    title: "AI Co-pilot",
    description: "Ask your AI assistant questions about software spend, licenses, compliance, and logs.",
    endpoint: "",
    tableFields: [],
    createFields: [],
    readOnly: true,
  },
};

// Which field holds the monetary amount for each module that has currency_code
const MODULE_AMOUNT_FIELD: Record<string, string> = {
  subscriptions: "amount",
  budgets: "allocated_amount",
  payments: "amount",
  contracts: "value",
};

const relationLabels: Record<string, string> = {
  organisations: "name",
  vendors: "name",
  subscriptions: "name",
  budgets: "department",
  employees: "full_name",
  roles: "name",
};

function Icon({ name }: { name: "add" | "refresh" | "edit" | "archive" | "wait" | "approve" | "reject" | "close" | "reopen" | "download" | "upload" | "key" | "list" }) {
  const paths = {
    add: "M12 5v14M5 12h14",
    refresh: "M20 12a8 8 0 0 1-13.7 5.7M4 12a8 8 0 0 1 13.7-5.7M18 3v4h-4M6 21v-4h4",
    edit: "M4 20h4l10.5-10.5a2.8 2.8 0 0 0-4-4L4 16v4ZM13.5 6.5l4 4",
    archive: "M4 7h16M6 7l1 13h10l1-13M9 7V4h6v3M10 11h4",
    wait: "M6 3h12M6 21h12M8 3c0 6 8 6 8 9s-8 3-8 9M16 3c0 6-8 6-8 9s8 3 8 9",
    approve: "M20 6 9 17l-5-5",
    reject: "M6 6l12 12M18 6 6 18",
    close: "M5 12h14M12 5v14M7 7l10 10",
    reopen: "M4 12a8 8 0 0 1 13.7-5.7M18 3v4h-4M20 12a8 8 0 0 1-13.7 5.7M6 21v-4h4",
    download: "M12 3v12M7 10l5 5 5-5M5 21h14",
    upload: "M12 21V9M7 14l5-5 5 5M5 3h14",
    key: "M15 7a4 4 0 1 0-3.5 3.97L3 19.5V22h2.5L7 20.5H9V18h2l3.03-3.03A4 4 0 0 0 15 7Z",
    list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  };

  return (
    <svg aria-hidden="true" fill="none" height="17" viewBox="0 0 24 24" width="17">
      <path d={paths[name]} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
    </svg>
  );
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return new Intl.NumberFormat("en-US").format(value);
  if (Array.isArray(value)) return value.join(", ");
  return String(value);
}

function toOrgCurrency(
  amount: number,
  fromCurrency: string,
  rates: Record<string, number>,
  toCurrency: string
): number {
  if (!fromCurrency || fromCurrency === toCurrency || !rates[fromCurrency]) return amount;
  return (amount / rates[fromCurrency]) * (rates[toCurrency] ?? 1);
}

function formatRoleLabel(roleCode: unknown): string {
  const code = String(roleCode || "").trim();
  if (!code) return "-";
  return code
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function openRecordForEdit(record: AnyRecord, moduleKey: string): AnyRecord {
  if (moduleKey !== "users") return record;
  const roles = Array.isArray(record.roles) ? (record.roles as string[]) : [];
  return {
    ...record,
    role_code: String(record.role_code || roles[0] || ""),
  };
}

function isLegacyDemoEmail(email: unknown): boolean {
  const value = String(email || "").trim().toLowerCase();
  return value.endsWith("@derisk360.local") || value.endsWith("@demo.derisk360.com");
}

function isLikelyInternalIdentifier(value: unknown): boolean {
  const str = String(value ?? "").trim();
  if (!str) return true;
  if (/^\d+$/.test(str)) return true;
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(str)) return true;
  if (/^[0-9a-f]{8}$/i.test(str)) return true;
  return false;
}

function formatCategoryLabel(value: unknown): string {
  const raw = String(value ?? "").trim();
  if (!raw || isLikelyInternalIdentifier(raw)) return "-";
  return raw
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

type SoftwareToolSummary = {
  title: string;
  description: string;
  meta: string[];
};

type WorkflowEmailPreview = {
  to: string;
  toEmails?: string[];
  cc?: string;
  ccEmails?: string[];
  subject: string;
  body: string;
  eventType: string;
  stage?: string;
};

function buildSoftwareToolSummary(
  moduleKey: string,
  draft: Record<string, string>,
  subscriptions: AnyRecord[],
  vendors: AnyRecord[]
): SoftwareToolSummary | null {
  if (moduleKey === "licences") {
    const subscriptionId = draft.subscription_id?.trim();
    if (!subscriptionId) return null;
    const subscription = subscriptions.find((item) => String(item.id) === subscriptionId);
    if (!subscription) return null;
    const vendor = vendors.find((item) => String(item.id) === String(subscription.vendor_id));
    const description =
      String(draft.notes || subscription.notes || "").trim() ||
      "No purpose description recorded yet for this subscription.";
    return {
      title: String(subscription.name || "Selected subscription"),
      description,
      meta: [
        subscription.category ? `Category: ${formatCategoryLabel(subscription.category)}` : "",
        subscription.department ? `Department: ${String(subscription.department)}` : "",
        vendor?.name ? `Vendor: ${String(vendor.name)}` : "",
        subscription.billing_cycle ? `Billing: ${String(subscription.billing_cycle)}` : "",
      ].filter(Boolean),
    };
  }

  if (moduleKey === "subscriptions") {
    const name = draft.name?.trim();
    if (!name || name.length < 2) return null;
    const existing = subscriptions.find((item) => String(item.name).toLowerCase() === name.toLowerCase());
    const vendor = draft.vendor_id
      ? vendors.find((item) => String(item.id) === draft.vendor_id)
      : existing?.vendor_id
        ? vendors.find((item) => String(item.id) === String(existing.vendor_id))
        : null;
    const description =
      String(draft.notes || existing?.notes || "").trim() ||
      `Request to add or renew the ${name} subscription. Describe the business purpose in the notes field.`;
    return {
      title: name,
      description,
      meta: [
        draft.category || existing?.category
          ? `Category: ${formatCategoryLabel(draft.category || existing?.category)}`
          : "",
        draft.department || existing?.department ? `Department: ${String(draft.department || existing?.department)}` : "",
        vendor?.name ? `Vendor: ${String(vendor.name)}` : "",
      ].filter(Boolean),
    };
  }

  return null;
}

function renderSoftwareToolSummary(summary: SoftwareToolSummary) {
  return (
    <section className="tool-summary-panel" aria-live="polite">
      <p className="eyebrow">Tool summary</p>
      <h3>{summary.title}</h3>
      <p>{summary.description}</p>
      {summary.meta.length ? (
        <div className="tool-summary-meta">
          {summary.meta.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function formatSubscriptionStatus(value: unknown): string {
  const raw = String(value ?? "").trim();
  if (!raw || raw === "-") return "-";
  return raw
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function formatWorkflowDateOnly(value: unknown): string {
  if (!value) return "-";
  return new Date(String(value)).toLocaleDateString("en-US", {
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

function getWorkflowSoftwareName(record: AnyRecord, allSubscriptions: AnyRecord[] = []): string {
  const payload = (record.payload || {}) as Record<string, unknown>;
  const activated = record.activated_entity_id
    ? allSubscriptions.find((s) => String(s.id) === String(record.activated_entity_id))
    : null;

  if (activated?.name && !isLikelyInternalIdentifier(activated.name)) {
    return String(activated.name);
  }

  const linkedSubscription = payload.subscription_id
    ? allSubscriptions.find((s) => String(s.id) === String(payload.subscription_id))
    : null;
  if (linkedSubscription?.name && !isLikelyInternalIdentifier(linkedSubscription.name)) {
    const licenceLabel = payload.licence_name ? String(payload.licence_name).trim() : "";
    if (licenceLabel && licenceLabel.toLowerCase() !== String(linkedSubscription.name).toLowerCase()) {
      return `${linkedSubscription.name} — ${licenceLabel}`;
    }
    return String(linkedSubscription.name);
  }

  const candidates = [
    payload.name,
    payload.tool_requested,
    payload.licence_name,
    payload.title,
    payload.full_name,
    payload.employee_number,
    payload.fiscal_year ? `FY${payload.fiscal_year} Budget` : null,
  ];

  for (const candidate of candidates) {
    if (candidate && !isLikelyInternalIdentifier(candidate)) {
      return String(candidate);
    }
  }

  return "(Unnamed Software Request)";
}

function resolveUserDisplay(allUsers: AnyRecord[], userId: unknown, roleFallback?: string): string {
  if (!userId) return "-";
  const idStr = String(userId);
  const match = allUsers.find((u) => String(u.user_id || "") === idStr || String(u.id || "") === idStr);
  if (match) {
    const name = String(match.full_name || match.name || "").trim();
    const email = String(match.work_email || match.email || "").trim();
    if (name) return name;
    if (email) return email;
  }
  if (roleFallback === "master") return "Master Admin";
  if (roleFallback === "finance") return "Finance Manager";
  return "-";
}

function resolveRequesterDisplay(record: AnyRecord, allUsers: AnyRecord[]): string {
  const byUserId = resolveUserDisplay(allUsers, record.requested_by);
  if (byUserId !== "-") return byUserId;

  const email = String(record.requested_by_email || "").trim();
  if (!email) return "-";

  const match = allUsers.find(
    (u) => String(u.work_email || u.email || "").toLowerCase() === email.toLowerCase()
  );
  if (match) {
    const name = String(match.full_name || match.name || "").trim();
    if (name) return name;
  }

  return email;
}

function resolveCategoryName(
  rawCategory: unknown,
  activatedSubscription: AnyRecord | null | undefined
): string {
  const fromSubscription = activatedSubscription?.category;
  if (fromSubscription && !isLikelyInternalIdentifier(fromSubscription)) {
    return formatCategoryLabel(fromSubscription);
  }
  return formatCategoryLabel(rawCategory);
}

function getWorkflowStatusLabel(status: unknown, workflowType?: unknown, preInfoStatus?: unknown): string {
  const stat = String(status || "").toLowerCase();
  const wfType = String(workflowType || "");
  const needsLineManager = requiresLineManager(wfType);
  const isOffboarding = wfType === "employee_offboarding";

  if (stat === "info_requested") {
    const pre = String(preInfoStatus || "").toLowerCase();
    if (pre === "submitted" || pre === "reopened") {
      return needsLineManager
        ? "More Info Needed · Waiting for LM Approval"
        : isOffboarding
          ? "More Info Needed · Awaiting IT Action"
          : "More Info Needed · Waiting for Budget Approval";
    }
    if (pre === "line_manager_approved" || pre === "master_approved") {
      return "More Info Needed · Waiting for Budget Approval";
    }
    if (pre === "finance_approved") {
      return "More Info Needed · Awaiting Procurement";
    }
    return "More Information Requested";
  }

  if (isOffboarding) {
    if (stat === "submitted" || stat === "reopened") return "Awaiting IT Action";
    if (stat === "it_confirmed") return "IT Confirmed — Ready to Complete";
    if (stat === "completed") return "Completed — Licences Revoked";
  }

  if ((stat === "submitted" || stat === "reopened") && needsLineManager) {
    return "Waiting for Line Manager Approval";
  }
  if (
    (stat === "submitted" || stat === "reopened" || stat === "master_approved") &&
    !needsLineManager
  ) {
    return "Waiting for Budget Approval";
  }
  const labels: Record<string, string> = {
    submitted: "Submitted",
    reopened: "Reopened",
    line_manager_approved: "Waiting for Budget Approval",
    master_approved: "Waiting for Budget Approval",
    finance_approved: "Awaiting Procurement",
    it_confirmed: "IT Confirmed",
    completed: "Completed",
    finance_closed: "Completed",
    rejected: "Rejected",
  };
  return labels[stat] || String(status || "-").toUpperCase().replace(/_/g, " ");
}

function getWorkflowStatusBadgeClass(status: unknown): string {
  const lowerVal = String(status || "").toLowerCase().replace(/_/g, "-");
  let statusClass = "status-badge";
  if (lowerVal === "master-approved" || lowerVal === "line-manager-approved") {
    statusClass += " status-badge--line-manager-approved";
  } else if (lowerVal === "finance-approved" || lowerVal === "awaiting-procurement") {
    statusClass += " status-badge--finance-approved";
  } else if (lowerVal.includes("finance-closed") || lowerVal === "completed") {
    statusClass += " status-badge--completed";
  } else if (lowerVal.includes("approved")) {
    statusClass += " status-badge--approved";
  } else if (lowerVal.includes("rejected")) {
    statusClass += " status-badge--rejected";
  } else if (lowerVal.includes("info-requested")) {
    statusClass += " status-badge--info-requested";
  } else if (lowerVal.includes("submitted")) {
    statusClass += " status-badge--submitted";
  } else if (lowerVal.includes("reopened")) {
    statusClass += " status-badge--reopened";
  }
  return statusClass;
}

function formatWorkflowDate(value: unknown): string {
  if (!value) return "-";
  return new Date(String(value)).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function resolveUserEmail(allUsers: AnyRecord[], userId: unknown, roleFallback?: string): string {
  return resolveUserDisplay(allUsers, userId, roleFallback);
}

function resolveVendorName(allVendors: AnyRecord[], vendorId: unknown): string {
  if (!vendorId) return "-";
  const match = allVendors.find((v) => String(v.id) === String(vendorId));
  return match ? String(match.name || match.legal_name || "Unknown") : "-";
}

function getPurchaseStatus(record: AnyRecord): string {
  const stat = String(record.status || "").toLowerCase();
  if (stat === "completed" || stat === "finance_closed") return "Purchased";
  if (stat === "finance_approved") return "Awaiting Confirmation";
  if (stat === "rejected") return "Not Purchased";
  if (["master_approved", "submitted", "reopened"].includes(stat)) return "Pending Approval";
  return "-";
}

function getWorkflowEmailContacts(allUsers: AnyRecord[]) {
  const approverEmails = allUsers
    .filter((u) => Array.isArray(u.roles) && (u.roles as string[]).includes("master_admin"))
    .map((u) => String(u.email || ""))
    .filter(Boolean);
  const financeEmails = allUsers
    .filter((u) => Array.isArray(u.roles) && (u.roles as string[]).includes("finance"))
    .map((u) => String(u.email || ""))
    .filter(Boolean);

  return {
    approverEmails: approverEmails.length > 0 ? approverEmails : ["-"],
    financeEmails: financeEmails.length > 0 ? financeEmails : ["-"],
  };
}

function getEmailDeliveryStatus(logs: Array<{ status?: string }>): string {
  if (!logs.length) return "NOT SENT";
  if (logs.some((log) => log.status === "FAILED")) return "DELIVERY FAILED";
  if (logs.every((log) => log.status === "MOCK_MODE")) return "MOCK MODE";
  return "EMAILS SENT";
}

function getWorkflowBudgetInfo(
  record: AnyRecord,
  allBudgets: AnyRecord[],
  allSubscriptions: AnyRecord[],
  selectedOrgId: string,
  fxRates: Record<string, number> = {}
) {
  const payload = (record.payload || {}) as Record<string, unknown>;
  const department = String(payload.department || "");
  const orgId = record.organisation_id || selectedOrgId;
  const currentYear = new Date().getFullYear();
  const deptBudgets = allBudgets.filter(
    (b) =>
      String(b.department || "").toLowerCase().trim() === department.toLowerCase().trim() &&
      (orgId ? b.organisation_id === orgId : true) &&
      b.status === "approved"
  );
  const deptBudget = deptBudgets.find((b) => Number(b.fiscal_year) === currentYear) || deptBudgets[0];
  const budgetCurrency = String(deptBudget?.currency_code || payload.currency_code || "AED");
  const requestAmount = toOrgCurrency(
    Number(payload.amount) || 0,
    String(payload.currency_code || budgetCurrency),
    fxRates,
    budgetCurrency
  );
  const activeSubs = allSubscriptions.filter(
    (s) =>
      String(s.department || "").toLowerCase().trim() === department.toLowerCase().trim() &&
      (orgId ? s.organisation_id === orgId : true) &&
      ["active", "trial", "pending_renewal"].includes(String(s.status || ""))
  );
  const currentSpend = activeSubs.reduce(
    (sum, s) => sum + toOrgCurrency(Number(s.amount || 0), String(s.currency_code || budgetCurrency), fxRates, budgetCurrency),
    0
  );
  const allocatedAmount = deptBudget ? Number(deptBudget.allocated_amount) : 0;
  const totalAfterPurchase = currentSpend + requestAmount;
  const exceedsBudget = deptBudget ? totalAfterPurchase > allocatedAmount : false;
  const workflowStatus = String(record.status || "").toLowerCase();
  const hasBudget = !!deptBudget;

  let budgetStatus = "Budget Not Assigned";
  if (hasBudget) {
    if (exceedsBudget) {
      budgetStatus = "Over Budget";
    } else if (workflowStatus === "master_approved") {
      budgetStatus = "Budget Validation Pending";
    } else if (workflowStatus === "finance_approved") {
      budgetStatus = "Budget Validated";
    } else if (["completed", "finance_closed"].includes(workflowStatus)) {
      budgetStatus = "Within Budget";
    } else {
      budgetStatus = "Within Budget";
    }
  } else if (workflowStatus === "master_approved") {
    budgetStatus = "Budget Validation Pending";
  } else if (workflowStatus === "finance_approved") {
    budgetStatus = "Awaiting Budget Assignment";
  }

  return {
    budgetName: deptBudget
      ? `${String(deptBudget.department || department)} FY${String(deptBudget.fiscal_year || currentYear)}`
      : department
        ? `${department} Budget`
        : "Department budget not configured",
    budgetAmount: deptBudget
      ? `${formatValue(allocatedAmount)} ${String(deptBudget.currency_code || payload.currency_code || "AED")}`
      : "Not assigned",
    budgetStatus,
    hasBudget,
  };
}

function getToolRequestStatusLabel(status: unknown): string {
  const labels: Record<string, string> = {
    new: "New",
    under_review: "Under Review",
    converted_to_workflow: "Converted To Workflow",
    rejected: "Rejected",
    closed: "Closed",
  };
  return labels[String(status || "").toLowerCase()] || String(status || "-");
}

function getToolRequestStatusClass(status: unknown): string {
  const stat = String(status || "").toLowerCase();
  if (stat === "new") return "tool-request-status tool-request-status--new";
  if (stat === "under_review") return "tool-request-status tool-request-status--review";
  if (stat === "converted_to_workflow") return "tool-request-status tool-request-status--converted";
  if (stat === "rejected") return "tool-request-status tool-request-status--rejected";
  return "tool-request-status tool-request-status--closed";
}

type ActivationMethod = "license_key" | "company_account";

type ActivationFormState = {
  activation_method: ActivationMethod | "";
  assigned_employee_name: string;
  assigned_employee_email: string;
  license_key: string;
  activation_code: string;
  username: string;
  password: string;
};

const activationMethodOptions: Array<{ value: ActivationMethod; label: string }> = [
  { value: "license_key", label: "License Key" },
  { value: "company_account", label: "Account Activation (Username / Password)" },
];

function getActivationMethodLabel(method: unknown): string {
  const match = activationMethodOptions.find((option) => option.value === method);
  return match?.label || formatValue(method);
}

function maskActivationSecret(value: unknown): string {
  const raw = String(value || "").trim();
  if (!raw) return "-";
  if (raw.length <= 4) return "****";
  return `${"*".repeat(Math.max(4, raw.length - 4))}${raw.slice(-4)}`;
}

function buildDefaultActivationForm(record: AnyRecord, allUsers: AnyRecord[]): ActivationFormState {
  return {
    activation_method: "",
    assigned_employee_name: "",
    assigned_employee_email: "",
    license_key: "",
    activation_code: "",
    username: "",
    password: "",
  };
}

function validateActivationForm(form: ActivationFormState): string | null {
  if (!form.activation_method) return "Select an activation method before confirming purchase.";
  if (form.activation_method === "license_key") {
    if (!form.license_key.trim()) return "License key is required.";
  }
  if (form.activation_method === "company_account") {
    if (!form.username.trim()) return "Username is required for account activation.";
    if (!form.password.trim()) return "Password is required for account activation.";
  }
  return null;
}

function buildActivationPayload(form: ActivationFormState) {
  return {
    activation_method: form.activation_method,
    license_key: form.license_key.trim(),
    activation_code: form.activation_code.trim(),
    username: form.username.trim(),
    password: form.password.trim(),
  };
}

function buildActivationInformationRows(record: AnyRecord): Array<[string, string]> {
  const details = (record.activation_details || {}) as Record<string, unknown>;
  const method = String(record.activation_method || "");
  const rows: Array<[string, string]> = [
    ["Activation Method", record.activation_method ? getActivationMethodLabel(record.activation_method) : "-"],
    ["Activation Status", formatCategoryLabel(record.activation_status || "-")],
    ["Assigned Employee", String(record.assigned_employee_name || "-")],
    ["Assigned Employee Email", String(record.assigned_employee_email || "-")],
  ];

  if (method === "company_account") {
    rows.push(["Username", String(details.username || "-")]);
    rows.push(["Password", details.password ? "••••••••" : "-"]);
  } else if (method === "invitation_email") {
    rows.push(["Recipient Email", String(details.recipient_email || "-")]);
    rows.push(["Invitation Status", String(details.invitation_status || "-")]);
  } else if (method === "license_key") {
    rows.push(["License Key", details.license_key ? maskActivationSecret(details.license_key) : "-"]);
    rows.push(["Activation Code", details.activation_code ? maskActivationSecret(details.activation_code) : "-"]);
  } else if (method === "vendor_provisioned") {
    rows.push(["Vendor Reference", String(details.vendor_reference_id || "-")]);
    rows.push(["Provisioning Notes", String(details.provisioning_notes || "-")]);
  }

  return rows;
}

function preparePayload(moduleKey: string, payload: AnyRecord): AnyRecord {
  const next = { ...payload };
  if (typeof next.code === "string") {
    next.code = next.code.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
  }
  if (typeof next.country_code === "string") {
    const country = next.country_code.trim();
    next.country_code = countryCodeAliases[country.toLowerCase()] ?? country.toUpperCase();
  }
  if (typeof next.currency_code === "string") {
    next.currency_code = next.currency_code.trim().toUpperCase();
  }
  if (moduleKey !== "organisations" && typeof next.status === "string" && next.status.trim() === "") {
    delete next.status;
  }
  return next;
}

async function responseError(response: Response): Promise<string> {
  try {
    const data = await response.json();
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail.map((item: { msg?: string }) => item.msg ?? "Validation error").join("; ");
    }
  } catch {
    return await response.text();
  }
  return "Request failed.";
}

function recordLabel(record: AnyRecord, source: string): string {
  const field = relationLabels[source] ?? "name";
  const primary = record[field];
  const secondary = record.code ?? record.work_email ?? record.currency_code;
  return [primary, secondary].filter(Boolean).join(" - ") || String(record.id);
}

function normaliseFormPayload(form: HTMLFormElement, fields: FieldConfig[]): AnyRecord {
  const formData = new FormData(form);
  const payload: AnyRecord = {};

  fields.forEach((field) => {
    if (field.type === "checkbox") {
      payload[field.name] = formData.get(field.name) === "on";
      return;
    }

    const value = formData.get(field.name);
    if (value === null || value === "") return;
    payload[field.name] = field.type === "number" ? Number(value) : String(value);
  });

  return payload;
}

function downloadFromUrl(url: string, filename: string) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}


function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result ?? "");
      resolve(result.includes(",") ? result.split(",")[1] : result);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// ── Diagnostics Panel ─────────────────────────────────────────────────────────
const DIAG_SUITES = [
  {
    id: "workflow",
    label: "Employee Software Request",
    description: "Runs a full synthetic employee software request from submission to completion and checks every step.",
    endpoint: "/api/diagnostics/run-workflow-test",
    buttonLabel: "▶ Run Full Workflow Test",
    runningLabel: "Running… (may take ~60s)",
    placeholder: "Creates a real workflow, validates every stage, checks all emails, AI review, pricing URLs, licence creation, and budget updates — then cleans up after itself.",
  },
  {
    id: "it-subscription",
    label: "IT Subscription Request",
    description: "Runs a full IT-initiated new subscription request: IT submits, finance validates, IT completes — no line manager step.",
    endpoint: "/api/diagnostics/run-it-subscription-test",
    buttonLabel: "▶ Run IT Subscription Test",
    runningLabel: "Running… (may take ~60s)",
    placeholder: "Tests the IT admin subscription request flow — verifies submission, finance approval, IT completion, emails at each stage, and licence creation.",
  },
  {
    id: "licence-assignment",
    label: "Licence Assignment Request",
    description: "Tests the IT licence assignment workflow — IT assigns an existing subscription seat to an employee, finance approves, IT completes.",
    endpoint: "/api/diagnostics/run-licence-assignment-test",
    buttonLabel: "▶ Run Licence Assignment Test",
    runningLabel: "Running… (may take ~60s)",
    placeholder: "Creates a temp subscription, submits a licence assignment request, validates finance approval, IT completion, emails at every stage, licence record created and linked to both the subscription and the employee.",
  },
  {
    id: "renewal",
    label: "Renewal Request",
    description: "Tests the renewal request workflow — IT submits a renewal, finance approves, IT marks complete — verifying the renewed subscription and payment are created.",
    endpoint: "/api/diagnostics/run-renewal-test",
    buttonLabel: "▶ Run Renewal Test",
    runningLabel: "Running… (may take ~60s)",
    placeholder: "Submits a renewal_request workflow, checks finance approval from submitted status, verifies IT completion, emails at each stage, renewed subscription record created in DB, payment linked, audit log and status history correct.",
  },
  {
    id: "master-admin",
    label: "Master Admin",
    description: "Tests both Master Admin paths: direct addition (POST /subscriptions + /licences without workflow) and the full workflow path (submitted → master_approved → finance_approved → completed).",
    endpoint: "/api/diagnostics/run-master-admin-test",
    buttonLabel: "▶ Run Master Admin Test",
    runningLabel: "Running… (may take ~90s)",
    placeholder: "Part A: Master admin directly creates a subscription and licence, verifies both appear in DB, checks audit logs. Part B: Master admin submits a workflow, approves it, finance validates, IT completes — checks all downstream effects (subscription, payment, status history, audit log).",
  },
  {
    id: "url-scraping",
    label: "Vendor URL Health",
    description: "Checks every pricing URL stored in the vendor catalogue — verifies reachability, response time, and that the page actually contains pricing content.",
    endpoint: "/api/diagnostics/run-url-scraping-test",
    buttonLabel: "▶ Run URL Health Check",
    runningLabel: "Checking URLs… (may take ~30s)",
    placeholder: "For each vendor catalogue entry with a scrape_url: checks HTTP status, response time (flags anything over 5s), detects pricing keywords on the page, and confirms the vendor name appears. Deduplicates URLs so each is only fetched once.",
  },
  {
    id: "full-lifecycle",
    label: "Full HR Lifecycle",
    description: "End-to-end test: HR onboards a brand-new employee (creates person record, portal login, assigns licence), then offboards that same employee (deactivates account, revokes licence, removes from system).",
    endpoint: "/api/diagnostics/run-full-lifecycle-test",
    buttonLabel: "▶ Run Full Lifecycle Test",
    runningLabel: "Running… (may take ~90s)",
    placeholder: "Creates a temp employee via HR onboarding request (finance approves, IT completes), verifies licence is assigned and portal login is active, then runs the full offboarding flow for the same employee and verifies the account is deactivated, licence is revoked, and no active licences remain. All test data is cleaned up on exit.",
  },
  {
    id: "hr-onboarding",
    label: "HR Onboarding",
    description: "Tests the full HR onboarding workflow — HR admin raises a licence request for a new employee, finance approves, IT completes, and the licence is auto-assigned with status 'assigned' to the employee.",
    endpoint: "/api/diagnostics/run-hr-onboarding-test",
    buttonLabel: "▶ Run HR Onboarding Test",
    runningLabel: "Running… (may take ~60s)",
    placeholder: "HR admin submits an hr_onboarding_request for a new employee. Finance approves directly (no LM step). IT completes via invitation email. Verifies: licence created with status=assigned, linked to the correct employee person record and subscription, employee welcome email sent, payment created, full audit trail.",
  },
  {
    id: "ai",
    label: "AI Feature Test",
    description: "Tests every AI-powered feature end-to-end and validates that outputs are meaningful, not fallbacks or errors.",
    endpoint: "/api/diagnostics/run-ai-test",
    buttonLabel: "▶ Run AI Feature Test",
    runningLabel: "Running… (may take ~90s)",
    placeholder: "Tests contract extraction, tool summaries, the AI Copilot, workflow review across all 5 roles, and AI-generated email content quality.",
  },
  {
    id: "renewal-alerts",
    label: "Renewal Alerts",
    description: "Tests the automated renewal alert system — verifies that subscriptions due in 30, 60, and 90 days are detected and alert emails are sent to finance and master admin.",
    endpoint: "/api/diagnostics/run-renewal-alerts-test",
    buttonLabel: "▶ Run Renewal Alerts Test",
    runningLabel: "Running… (may take ~15s)",
    placeholder: "Creates a [DIAG] subscription due in 30 days, runs the renewal alert engine, verifies the email was sent and logged, checks the manual trigger endpoint, then cleans up.",
  },
  {
    id: "offboarding",
    label: "Employee Offboarding",
    description: "Tests the full employee offboarding workflow — HR submits, IT confirms licences cleared, IT completes — verifying the employee is deactivated and all licences are revoked.",
    endpoint: "/api/diagnostics/run-offboarding-test",
    buttonLabel: "▶ Run Offboarding Test",
    runningLabel: "Running… (may take ~30s)",
    placeholder: "Creates a temp employee with an assigned licence, runs the full offboarding flow (submitted → it_confirmed → completed), then verifies the employee is inactive and the licence is revoked. Cleans up all test data on exit.",
  },
  {
    id: "slack",
    label: "Slack Integration",
    description: "Tests the Slack webhook and bot token — verifies the channel post works and that a DM can be delivered to each workflow user (Employee, Line Manager, Finance, IT, Master Admin).",
    endpoint: "/api/diagnostics/run-slack-test",
    buttonLabel: "▶ Run Slack Test",
    runningLabel: "Running… (may take ~10s)",
    placeholder: "Checks env vars, posts a test message to #software-requests, authenticates the bot token, then sends a DM to each workflow user and reports pass/fail per user.",
  },
  {
    id: "upload-export",
    label: "Upload / Export",
    description: "Tests template download, bulk upload with dummy data, duplicate detection, and export for every module (vendors, subscriptions, licences, budgets, payments, employees, contracts). Cleans up all test records on exit.",
    endpoint: "/api/diagnostics/run-upload-export-test",
    buttonLabel: "▶ Run Upload / Export Test",
    runningLabel: "Running… (may take ~20s)",
    placeholder: "Downloads each module template, uploads two dummy rows per module, re-uploads the same rows to verify duplicates are skipped, exports each module to XLSX, and deletes all created test data.",
  },
  {
    id: "operator-eval",
    label: "AI Operator Eval",
    description: "Phase 2e — seeds operator_eval_org, tests write tools, plan validation, execute/verifier, RBAC, audit source ai_operator, and LLM planner cases. Cleans up on exit.",
    endpoint: "/api/diagnostics/run-operator-eval",
    buttonLabel: "▶ Run Operator Eval",
    runningLabel: "Running… (may take 2–5 min with LLM planner checks)",
    placeholder: "Programmatic checks plus LLM planner tests for missing-detail questions and employee approve refusal.",
  },
  {
    id: "copilot-eval",
    label: "AI Copilot Eval",
    description: "Phase 1a — seeds copilot_eval_org, asks ~31 questions in tools mode, scores answers against SQL truth, teardown. Requires GEMINI_API_KEY.",
    endpoint: "/api/diagnostics/run-copilot-eval",
    buttonLabel: "▶ Run Copilot Eval (tools)",
    runningLabel: "Running… (may take 3–8 min)",
    placeholder: 'POST body: {"copilot_mode":"tools"}. Score should be 29+/31.',
    postBody: { copilot_mode: "tools" },
  },
  {
    id: "copilot-routing",
    label: "Copilot Routing Benchmark",
    description: "Phase 1d — compares single-model vs Not Diamond routed copilot on the same fixture. Requires NOTDIAMOND_ROUTING_ENABLED.",
    endpoint: "/api/diagnostics/run-copilot-eval",
    buttonLabel: "▶ Run Routing Benchmark",
    runningLabel: "Running… (may take 6–10 min)",
    placeholder: "Routed score must be >= baseline with routed_calls > 0.",
    postBody: { routing_benchmark: true, copilot_mode: "tools" },
  },
];

function DiagnosticsPanel({
  apiBaseUrl,
  user,
}: {
  apiBaseUrl: string;
  user: { id?: string; email?: string; roles?: string[] } | null;
}) {
  type DiagResult = {
    checks: { name: string; status: string; detail: string }[];
    summary: { total: number; passed: number; failed: number; warned: number; skipped: number; overall: string };
    workflow_id: string | null;
    ai_analysis: string;
  };
  const [suiteId, setSuiteId] = useState("workflow");
  const [running, setRunning] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [result, setResult] = useState<DiagResult | null>(null);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [autoDiscovering, setAutoDiscovering] = useState(false);
  const [autoDiscoverResult, setAutoDiscoverResult] = useState<{ fixed: number; skipped: number; failed: number; results: { name: string; status: string; detail: string }[] } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const suite = DIAG_SUITES.find(s => s.id === suiteId)!;

  const run = async () => {
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    setStopped(false);
    setResult(null);
    setError("");
    setElapsed(null);
    const t0 = Date.now();
    try {
      const init: RequestInit = { method: "POST", signal: controller.signal };
      const postBody = (suite as { postBody?: Record<string, unknown> }).postBody;
      if (postBody) {
        init.headers = { "Content-Type": "application/json" };
        init.body = JSON.stringify(postBody);
      }
      const r = await fetch(`${apiBaseUrl}${suite.endpoint}`, init);
      const data = await r.json();
      setResult(data);
      setElapsed(Math.round((Date.now() - t0) / 1000));
    } catch (e: unknown) {
      if (e instanceof DOMException && e.name === "AbortError") {
        setError("Test stopped by user. The backend will finish its current step and clean up.");
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setRunning(false);
      abortRef.current = null;
    }
  };

  const stop = () => {
    if (abortRef.current) {
      abortRef.current.abort();
      setStopped(true);
    }
  };

  const statusIcon = (s: string) => s === "pass" ? "✅" : s === "warn" ? "⚠️" : s === "skip" ? "⏭" : "❌";
  const statusColor = (s: string) => s === "pass" ? "#22c55e" : s === "warn" ? "#f59e0b" : s === "skip" ? "#4b5563" : "#ef4444";

  return (
    <div style={{ padding: "0 0 40px 0" }}>
      <DevtoolsControlPanel apiBaseUrl={apiBaseUrl} user={user} />
      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        {/* Header row */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <div style={{ flex: 1, minWidth: 260 }}>
            <p className="eyebrow">System Health</p>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap", marginBottom: 6 }}>
              <h2 style={{ margin: 0 }}>{suite.label}</h2>
              <select
                value={suiteId}
                onChange={e => { setSuiteId(e.target.value); setResult(null); setError(""); }}
                disabled={running}
                style={{
                  background: "var(--surface-2)", color: "var(--text-primary)",
                  border: "1px solid var(--border)", borderRadius: 6,
                  padding: "4px 10px", fontSize: "0.82rem", cursor: "pointer",
                }}
              >
                {DIAG_SUITES.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
              </select>
            </div>
            <p style={{ color: "var(--text-secondary)", marginTop: 0, fontSize: "0.9rem" }}>
              {suite.description}
            </p>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              onClick={run}
              disabled={running}
              style={{
                background: running ? "var(--surface-2)" : "var(--brand-cyan)",
                color: running ? "var(--text-secondary)" : "#000",
                border: "none", borderRadius: 8, padding: "12px 28px",
                fontWeight: 700, fontSize: "0.95rem", cursor: running ? "not-allowed" : "pointer",
                display: "flex", alignItems: "center", gap: 8, minWidth: 200,
              }}
            >
              {running ? (
                <>
                  <span style={{ display: "inline-block", width: 16, height: 16, border: "2px solid var(--text-secondary)", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
                  {suite.runningLabel}
                </>
              ) : suite.buttonLabel}
            </button>
            {running && (
              <button
                onClick={stop}
                style={{
                  background: "#1a0a0a", color: "#ef4444",
                  border: "1px solid #ef4444", borderRadius: 8, padding: "12px 20px",
                  fontWeight: 700, fontSize: "0.95rem", cursor: "pointer",
                }}
              >
                ■ Stop
              </button>
            )}
          </div>
        </div>

        {error && (
          <div style={{ background: "#1a0a0a", border: "1px solid #ef4444", borderRadius: 8, padding: 16, color: "#ef4444" }}>
            <strong>Error:</strong> {error}
          </div>
        )}

        {result && !result.summary && (
          <div style={{ background: "var(--surface-1)", border: "1px solid #ef4444", borderRadius: 10, padding: "16px 20px", color: "#ef4444" }}>
            ❌ Test error: {String((result as Record<string, unknown>).detail ?? "Unknown error")}
          </div>
        )}
        {result && result.summary && (
          <>
            {/* Summary bar */}
            <div style={{
              display: "flex", gap: 16, flexWrap: "wrap",
              background: "var(--surface-1)", border: `1px solid ${result.summary.overall === "pass" ? "#22c55e" : "#ef4444"}`,
              borderRadius: 10, padding: "16px 20px", alignItems: "center",
            }}>
              <div style={{ fontSize: "1.4rem", fontWeight: 800, color: result.summary.overall === "pass" ? "#22c55e" : "#ef4444" }}>
                {result.summary.overall === "pass" ? "✅ ALL PASS" : "❌ FAILURES DETECTED"}
              </div>
              <div style={{ display: "flex", gap: 24, marginLeft: "auto", flexWrap: "wrap" }}>
                <span style={{ color: "#22c55e", fontWeight: 700 }}>✅ {result.summary.passed} passed</span>
                {result.summary.failed > 0 && <span style={{ color: "#ef4444", fontWeight: 700 }}>❌ {result.summary.failed} failed</span>}
                {result.summary.warned > 0 && <span style={{ color: "#f59e0b", fontWeight: 700 }}>⚠️ {result.summary.warned} warnings</span>}
                <span style={{ color: "var(--text-secondary)" }}>of {result.summary.total} checks</span>
                {result.summary.skipped > 0 && <span style={{ color: "#4b5563" }}>⏭ {result.summary.skipped} skipped</span>}
                {elapsed !== null && <span style={{ color: "var(--text-secondary)" }}>⏱ {elapsed}s</span>}
              </div>
            </div>

            {/* Check rows */}
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {result.checks.map((c, i) => (
                <div key={i} style={{
                  display: "flex", alignItems: "center", gap: 12,
                  background: "var(--surface-1)", borderRadius: 7, padding: "10px 14px",
                  borderLeft: `3px solid ${statusColor(c.status)}`,
                  opacity: c.status === "skip" ? 0.4 : 1,
                }}>
                  <span style={{ fontSize: "1rem", minWidth: 24 }}>{statusIcon(c.status)}</span>
                  <span style={{ flex: 1, fontWeight: 500, fontSize: "0.88rem" }}>{c.name}</span>
                  {c.detail && (
                    <span style={{ fontSize: "0.78rem", color: c.status === "fail" ? "#ef4444" : c.status === "warn" ? "#f59e0b" : "var(--text-secondary)", fontFamily: "monospace", maxWidth: 380, textAlign: "right" }}>
                      {c.detail}
                    </span>
                  )}
                </div>
              ))}
            </div>

            {/* Auto-discover missing URLs (URL health suite only) */}
            {suiteId === "url-scraping" && result.checks.some(c => c.name.startsWith("Missing URL:") && c.status === "warn") && (
              <div style={{ background: "rgba(20,184,166,0.06)", border: "1px solid rgba(20,184,166,0.25)", borderRadius: 10, padding: "18px 22px" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
                  <div>
                    <strong style={{ color: "var(--brand-cyan)", fontSize: "0.9rem" }}>Missing pricing URLs detected</strong>
                    <p style={{ margin: "4px 0 0", fontSize: "0.82rem", color: "var(--text-secondary)" }}>
                      Click to automatically discover and save pricing URLs for subscriptions that are missing them.
                    </p>
                  </div>
                  <button
                    onClick={async () => {
                      setAutoDiscovering(true);
                      setAutoDiscoverResult(null);
                      try {
                        const r = await fetch(`${apiBaseUrl}/vendor-catalogue/auto-discover-urls`, { method: "POST" });
                        setAutoDiscoverResult(await r.json());
                      } catch { /* ignore */ }
                      setAutoDiscovering(false);
                    }}
                    disabled={autoDiscovering}
                    style={{ background: autoDiscovering ? "var(--surface-2)" : "var(--brand-cyan)", color: autoDiscovering ? "var(--text-secondary)" : "#000", border: "none", borderRadius: 8, padding: "10px 20px", fontWeight: 700, fontSize: "0.88rem", cursor: autoDiscovering ? "not-allowed" : "pointer", whiteSpace: "nowrap", display: "flex", alignItems: "center", gap: 7 }}
                  >
                    {autoDiscovering ? (
                      <><span style={{ display: "inline-block", width: 13, height: 13, border: "2px solid var(--text-secondary)", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} /> Discovering…</>
                    ) : "✦ Auto-discover Missing URLs"}
                  </button>
                </div>
                {autoDiscoverResult && (
                  <div style={{ marginTop: 14, borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: 14 }}>
                    <p style={{ margin: "0 0 10px", fontSize: "0.85rem", color: "var(--text-primary)" }}>
                      <strong style={{ color: "#22c55e" }}>{autoDiscoverResult.fixed} fixed</strong>
                      {autoDiscoverResult.skipped > 0 && <span style={{ color: "var(--text-secondary)" }}> · {autoDiscoverResult.skipped} already had URLs</span>}
                      {autoDiscoverResult.failed > 0 && <span style={{ color: "#f59e0b" }}> · {autoDiscoverResult.failed} could not be found</span>}
                    </p>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {autoDiscoverResult.results.filter(r => r.status !== "skipped").map((r, i) => (
                        <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start", fontSize: "0.8rem" }}>
                          <span style={{ color: r.status === "created" || r.status === "updated" ? "#22c55e" : r.status === "not_found" ? "#f59e0b" : "#ef4444", minWidth: 60, fontWeight: 700 }}>
                            {r.status === "created" ? "CREATED" : r.status === "updated" ? "UPDATED" : r.status === "not_found" ? "NOT FOUND" : "ERROR"}
                          </span>
                          <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{r.name}</span>
                          <span style={{ color: "var(--text-secondary)", flex: 1 }}>{r.detail}</span>
                        </div>
                      ))}
                    </div>
                    {autoDiscoverResult.fixed > 0 && (
                      <p style={{ margin: "10px 0 0", fontSize: "0.8rem", color: "var(--text-secondary)" }}>
                        Re-run the health check to see updated results.
                      </p>
                    )}
                  </div>
                )}
              </div>
            )}

            {/* AI Analysis */}
            {result.ai_analysis && (
              <div style={{
                background: "var(--surface-1)",
                border: `1px solid ${result.summary.overall === "pass" ? "#22c55e33" : "#ef444433"}`,
                borderRadius: 10, padding: "20px 24px",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                  <span style={{ fontSize: "1.1rem" }}>🤖</span>
                  <strong style={{ fontSize: "0.95rem", color: "var(--brand-cyan)" }}>AI Diagnostic Analysis</strong>
                </div>
                <p style={{ margin: 0, fontSize: "0.9rem", lineHeight: 1.7, color: "var(--text-primary)", whiteSpace: "pre-wrap" }}>
                  {result.ai_analysis}
                </p>
              </div>
            )}
          </>
        )}

        {!result && !running && !error && (
          <div style={{ background: "var(--surface-1)", borderRadius: 10, padding: 32, textAlign: "center", color: "var(--text-secondary)" }}>
            {suite.placeholder}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ModulePage() {
  const params = useParams<{ module: string }>();
  const moduleKey = params.module;
  const config = moduleConfigs[moduleKey];
  const [records, setRecords] = useState<AnyRecord[]>([]);
  const [relations, setRelations] = useState<Record<string, AnyRecord[]>>({});
  const [summary, setSummary] = useState<AnyRecord | null>(null);
  const [message, setMessage] = useState("");
  const [toast, setToast] = useState("");
  const [toastTitle, setToastTitle] = useState("");
  const [toastType, setToastType] = useState("info");

  function triggerToast(title: string, message: string, type: "success" | "danger" | "info" | "warning" = "info") {
    setToastTitle(title);
    setToast(message);
    setToastType(type);
  }

  // Email notifications state hooks
  const [emailLogs, setEmailLogs] = useState<any[]>([]);
  const [expandedEmailLogs, setExpandedEmailLogs] = useState<Record<string, boolean>>({});
  const [allEmailLogs, setAllEmailLogs] = useState<any[]>([]);
  const [emailStatus, setEmailStatus] = useState<AnyRecord | null>(null);
  const [demoRecipientOptions, setDemoRecipientOptions] = useState<AnyRecord[]>([]);
  const [testEmailTo, setTestEmailTo] = useState("deriskemployee1@outlook.com");
  const [testEmailLoading, setTestEmailLoading] = useState(false);
  const [employeeSyncLoading, setEmployeeSyncLoading] = useState(false);
  const [employeeSyncStatus, setEmployeeSyncStatus] = useState<AnyRecord | null>(null);

  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState<LoggedInUser | null>(null);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingRecord, setEditingRecord] = useState<AnyRecord | null>(null);
  const [editFormCurrency, setEditFormCurrency] = useState<string>("");
  const [workflowFormDraft, setWorkflowFormDraft] = useState<Record<string, string>>({});
  const [workflowEmailPreview, setWorkflowEmailPreview] = useState<WorkflowEmailPreview[]>([]);
  const [workflowEmailPreviewLoading, setWorkflowEmailPreviewLoading] = useState(false);
  const [workflowEmailPreviewError, setWorkflowEmailPreviewError] = useState("");
  const [workflowStatusHistory, setWorkflowStatusHistory] = useState<AnyRecord[]>([]);

  // Scoping and context states
  const [selectedOrgId, setSelectedOrgId] = useState<string>("");
  const [selectedOrgCurrency, setSelectedOrgCurrency] = useState<string>("AED");

  const [allVendors, setAllVendors] = useState<AnyRecord[]>([]);
  const [vendorCatalogue, setVendorCatalogue] = useState<AnyRecord[]>([]);
  const [fxRates, setFxRates] = useState<Record<string, number>>({ USD: 1, AED: 3.6725, GBP: 0.79, EUR: 0.92, INR: 83.5, SAR: 3.75, QAR: 3.64, KWD: 0.307 });
  const [catalogueVendorId, setCatalogueVendorId] = useState<string | null>(null); // which vendor's modal is open
  const [catalogueEditId, setCatalogueEditId] = useState<string | null>(null);
  const [catalogueEditFields, setCatalogueEditFields] = useState<{ price: string; currency_code: string; name: string; scrape_url: string }>({ price: "", currency_code: "AED", name: "", scrape_url: "" });
  const [catalogueAddFields, setCatalogueAddFields] = useState<{ name: string; price: string; currency_code: string; scrape_url: string }>({ name: "", price: "", currency_code: "AED", scrape_url: "" });
  const [catalogueFetchingAdd, setCatalogueFetchingAdd] = useState(false);
  const [catalogueFetchingEdit, setCatalogueFetchingEdit] = useState(false);
  const [allSubscriptions, setAllSubscriptions] = useState<AnyRecord[]>([]);
  const [allLicences, setAllLicences] = useState<AnyRecord[]>([]);
  const [allBudgets, setAllBudgets] = useState<AnyRecord[]>([]);
  const [allPayments, setAllPayments] = useState<AnyRecord[]>([]);
  const [allEmployees, setAllEmployees] = useState<AnyRecord[]>([]);
  const [allContracts, setAllContracts] = useState<AnyRecord[]>([]);

  // Budget edit / add state
  const [budgetEditState, setBudgetEditState] = useState<{ id: string; department: string; allocated_amount: string; currency_code: string; status: string; notes: string; fiscal_year: string } | null>(null);
  const [budgetAddOpen, setBudgetAddOpen] = useState(false);
  const [budgetAddFields, setBudgetAddFields] = useState<{ department: string; allocated_amount: string; currency_code: string; status: string; notes: string; fiscal_year: string }>({ department: "", allocated_amount: "", currency_code: "AED", status: "approved", notes: "", fiscal_year: String(new Date().getFullYear()) });
  const [budgetSaving, setBudgetSaving] = useState(false);
  const [allOrganisations, setAllOrganisations] = useState<AnyRecord[]>([]);
  const [allUsers, setAllUsers] = useState<AnyRecord[]>([]);
  const [roleMailboxEmails, setRoleMailboxEmails] = useState<Record<string, string>>({});

  // AI Co-pilot states
  const [copilotMessages, setCopilotMessages] = useState<{ id: string; role: "user" | "assistant"; content: string; timestamp: string }[]>([]);
  const [copilotInput, setCopilotInput] = useState("");
  const [copilotLoading, setCopilotLoading] = useState(false);

  // Contracts AI Extract states
  const [extractOpen, setExtractOpen] = useState(false);
  const [extractText, setExtractText] = useState("");
  const [extractDoc, setExtractDoc] = useState<{ name: string; dataUrl: string } | null>(null);
  const [extracting, setExtracting] = useState(false);
  const [extractHealth, setExtractHealth] = useState<{ ok: boolean; provider: string; configured: boolean } | null>(null);
  const [extractHealthChecked, setExtractHealthChecked] = useState(false);

  // Custom contract attachment states
  const [contractDocName, setContractDocName] = useState("");
  const [contractDocData, setContractDocData] = useState("");

  // Kanban board active drag state
  const [activeDragColumn, setActiveDragColumn] = useState<string | null>(null);
  const [financeConfirmRecord, setFinanceConfirmRecord] = useState<AnyRecord | null>(null);
  const [purchaseConfirmRecord, setPurchaseConfirmRecord] = useState<AnyRecord | null>(null);
  const [procurementStep, setProcurementStep] = useState<"redirect" | "credentials">("redirect");
  const [procurementVendorUrl, setProcurementVendorUrl] = useState<{ url: string; label: string } | null>(null);
  const [procurementUrlLoading, setProcurementUrlLoading] = useState(false);
  const [activationForm, setActivationForm] = useState<ActivationFormState>(() => buildDefaultActivationForm({}, []));
  const [workflowDetailsRecord, setWorkflowDetailsRecord] = useState<AnyRecord | null>(null);
  const [wfAiReview, setWfAiReview] = useState<{
    loading: boolean;
    recommendation: string;
    confidence: number;
    reasons: string[];
    concerns: string[];
  } | null>(null);
  const [toolRequestModalRecord, setToolRequestModalRecord] = useState<AnyRecord | null>(null);
  const [kanbanPages, setKanbanPages] = useState<Record<string, number>>({});
  const [masterSoftwareCreateMode, setMasterSoftwareCreateMode] = useState<"direct" | "workflow">("direct");
  const [employeeSoftwareRequestOpen, setEmployeeSoftwareRequestOpen] = useState(false);
  const [offboardingEmployee, setOffboardingEmployee] = useState<AnyRecord | null>(null);
  const [offboardingConfirmRecord, setOffboardingConfirmRecord] = useState<AnyRecord | null>(null);
  const [offboardingCompleting, setOffboardingCompleting] = useState(false);
  const [renewalPage, setRenewalPage] = useState(0);
  const [renewalsPageIdx, setRenewalsPageIdx] = useState(0);
  const [recycleBinPages, setRecycleBinPages] = useState<Record<string, number>>({});
  const [vendorDraft, setVendorDraft] = useState<Record<string, string>>({});
  const [vendorAutofilling, setVendorAutofilling] = useState(false);

  // Employee dashboard state
  const [myWorkflowRequests, setMyWorkflowRequests] = useState<AnyRecord[]>([]);
  const [myEmailNotifications, setMyEmailNotifications] = useState<AnyRecord[]>([]);

  // Line manager dashboard state
  const [lmTeamMembers, setLmTeamMembers] = useState<AnyRecord[]>([]);
  const [lmTeamWorkflowRequests, setLmTeamWorkflowRequests] = useState<AnyRecord[]>([]);
  const [lmTeamEmailNotifications, setLmTeamEmailNotifications] = useState<AnyRecord[]>([]);
  const [lmSoftwareViewMembers, setLmSoftwareViewMembers] = useState<{ subId: string; name: string } | null>(null);

  // Finance dashboard state
  const [financeWorkflowQueue, setFinanceWorkflowQueue] = useState<AnyRecord[]>([]);

  // IT Admin dashboard state
  const [itAllWorkflows, setItAllWorkflows] = useState<AnyRecord[]>([]);
  const [itToolRequests, setItToolRequests] = useState<AnyRecord[]>([]);

  // Info request response modal state
  const [infoResponseModal, setInfoResponseModal] = useState<{ workflow: AnyRecord; text: string } | null>(null);
  // Approver view: workflow that has been resubmitted after info request
  const [infoReviewModal, setInfoReviewModal] = useState<AnyRecord | null>(null);

  // Subscription creation log (for audit-logs page)
  const [subscriptionCreationLog, setSubscriptionCreationLog] = useState<AnyRecord[]>([]);
  const [subLogLoading, setSubLogLoading] = useState(false);

  // Fetch all email logs whenever the records change or we switch modules
  useEffect(() => {
    fetch(`${apiBaseUrl}/api/email/role-mailboxes`)
      .then((res) => (res.ok ? res.json() : []))
      .then((mailboxes) => {
        const mapped: Record<string, string> = {};
        if (Array.isArray(mailboxes)) {
          mailboxes.forEach((entry) => {
            if (entry?.role && entry?.email) {
              mapped[String(entry.role)] = String(entry.email);
            }
          });
        }
        setRoleMailboxEmails(mapped);
      })
      .catch(() => setRoleMailboxEmails({}));
  }, []);

  useEffect(() => {
    if (moduleKey === "workflows" || moduleKey === "tool-requests") {
      fetch(`${apiBaseUrl}/api/email/logs`)
        .then((res) => (res.ok ? res.json() : []))
        .then((data) => setAllEmailLogs(data))
        .catch((err) => console.error("Error loading email logs:", err));
    }
  }, [moduleKey, records]);

  // Fetch email logs for the active workflow detail modal
  useEffect(() => {
    if (workflowDetailsRecord?.id) {
      fetch(`${apiBaseUrl}/api/email/logs/${workflowDetailsRecord.id}`)
        .then((res) => (res.ok ? res.json() : []))
        .then((data) => setEmailLogs(data))
        .catch((err) => {
          console.error("Failed to fetch email logs:", err);
          setEmailLogs([]);
        });
      fetch(`${apiBaseUrl}/workflow-requests/${workflowDetailsRecord.id}/status-history`)
        .then((res) => (res.ok ? res.json() : []))
        .then((data) => setWorkflowStatusHistory(Array.isArray(data) ? data : []))
        .catch(() => setWorkflowStatusHistory([]));
    } else {
      setEmailLogs([]);
      setWorkflowStatusHistory([]);
    }
  }, [workflowDetailsRecord]);

  // Fetch AI review when a pending workflow details modal opens
  useEffect(() => {
    const status = String(workflowDetailsRecord?.status || "");
    const isPending = workflowDetailsRecord?.id && !["completed", "rejected", "cancelled", "finance_closed"].includes(status);
    if (!isPending) {
      setWfAiReview(null);
      return;
    }
    setWfAiReview({ loading: true, recommendation: "", confidence: 0, reasons: [], concerns: [] });
    fetch(`${apiBaseUrl}/api/workflow-review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        workflow_request_id: workflowDetailsRecord!.id,
        reviewer_role: (() => { const r = normalizeRoles(user?.roles); return r.includes("master_admin") ? "master_admin" : r.includes("finance") ? "finance" : r.includes("line_manager") ? "line_manager" : r.includes("it_admin") ? "it_admin" : r.includes("hr_admin") ? "hr_admin" : "approver"; })(),
      }),
    })
      .then((r) => r.json())
      .then((data) => setWfAiReview({ loading: false, ...data }))
      .catch(() => setWfAiReview(null));
  }, [workflowDetailsRecord?.id]);

  // Fetch email logs for the active tool request modal
  useEffect(() => {
    if (toolRequestModalRecord?.id) {
      fetch(`${apiBaseUrl}/api/email/logs/tool-request/${toolRequestModalRecord.id}`)
        .then((res) => (res.ok ? res.json() : []))
        .then((data) => setEmailLogs(data))
        .catch((err) => {
          console.error("Failed to fetch email logs for tool request:", err);
          setEmailLogs([]);
        });
    } else if (!workflowDetailsRecord) {
      setEmailLogs([]);
    }
  }, [toolRequestModalRecord, workflowDetailsRecord]);

  const renderSentEmailMailbox = (logs: any[], emptyMessage: string) => {
    if (logs.length === 0) {
      return (
        <p style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "13px" }}>
          {emptyMessage}
        </p>
      );
    }

    return (
      <div className="mock-email-container">
        <div className="mock-email-header">
          <span>Email History</span>
          <span className="mock-email-badge">{getEmailDeliveryStatus(logs)}</span>
        </div>
        <div className="mock-email-body">
          {logs.map((log, idx) => {
            const isExpanded = !!expandedEmailLogs[log.id];
            const formattedTime = log.createdAt
              ? new Date(log.createdAt).toLocaleString("en-US", {
                  month: "short",
                  day: "numeric",
                  year: "numeric",
                  hour: "numeric",
                  minute: "2-digit",
                })
              : "";
            const previewBody = String(log.bodyPreview || "");
            const shortBody = previewBody.length > 180 ? `${previewBody.slice(0, 180)}...` : previewBody;

            return (
              <div
                key={log.id}
                className="mock-email-item"
                style={{
                  borderTop: idx > 0 ? "1px dashed rgba(255,255,255,0.08)" : "none",
                  paddingTop: idx > 0 ? "8px" : "0",
                  marginTop: idx > 0 ? "8px" : "0",
                }}
              >
                <div className="mock-email-field">
                  <strong>To:</strong> <span>{log.to}</span>
                </div>
                {log.from ? (
                  <div className="mock-email-field">
                    <strong>From:</strong> <span>{log.from}</span>
                  </div>
                ) : null}
                {log.cc ? (
                  <div className="mock-email-field">
                    <strong>CC:</strong> <span>{log.cc}</span>
                  </div>
                ) : null}
                <div className="mock-email-field">
                  <strong>Subject:</strong> <span>{log.subject}</span>
                </div>
                {log.workflowStage ? (
                  <div className="mock-email-field">
                    <strong>Stage:</strong> <span>{log.workflowStage}</span>
                  </div>
                ) : null}
                <div className="mock-email-field" style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                  <strong>Timestamp:</strong>
                  <span>{formattedTime || "-"}</span>
                </div>
                {log.eventType ? (
                  <div className="mock-email-field">
                    <strong>Template:</strong> <span>{String(log.eventType).replace(/_/g, " ")}</span>
                  </div>
                ) : null}
                <div className="mock-email-field" style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                  <strong>Status:</strong>
                  <span>{log.status}</span>
                </div>
                {log.errorMessage ? (
                  <div style={{ fontSize: "12px", color: "#f87171", marginTop: "6px" }}>
                    <strong>Error:</strong> {log.errorMessage}
                  </div>
                ) : null}
                <div className="mock-email-content">{isExpanded ? previewBody : shortBody}</div>
                {previewBody.length > 180 ? (
                  <button
                    type="button"
                    onClick={() => setExpandedEmailLogs((prev) => ({ ...prev, [log.id]: !isExpanded }))}
                    style={{
                      background: "none",
                      border: "none",
                      padding: 0,
                      color: "var(--brand-cyan)",
                      fontSize: "12px",
                      cursor: "pointer",
                      textDecoration: "underline",
                      fontWeight: 600,
                      marginTop: "6px",
                    }}
                  >
                    {isExpanded ? "Show less" : "Show full message"}
                  </button>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  const renderWorkflowEmailPreview = () => {
    if (workflowEmailPreviewLoading) {
      return (
        <p style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "13px" }}>
          Loading email preview...
        </p>
      );
    }

    if (workflowEmailPreviewError) {
      return (
        <p style={{ color: "#f87171", fontSize: "13px" }}>
          {workflowEmailPreviewError}
        </p>
      );
    }

    if (!workflowEmailPreview.length) {
      return (
        <p style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "13px" }}>
          No recipients configured yet. Add active app users for Master Admin and IT Admin roles.
        </p>
      );
    }

    return (
      <div className="mock-email-container">
        <div className="mock-email-header">
          <span>✉️ Email preview</span>
          <span className="mock-email-badge">Not sent</span>
        </div>
        <div className="mock-email-body">
          {workflowEmailPreview.map((preview, idx) => (
            <div
              key={`${preview.eventType}-${idx}`}
              className="mock-email-item"
              style={{
                borderTop: idx > 0 ? "1px dashed rgba(255,255,255,0.08)" : "none",
                paddingTop: idx > 0 ? "8px" : "0",
                marginTop: idx > 0 ? "8px" : "0",
              }}
            >
              <div className="mock-email-field">
                <strong>To:</strong> <span>{preview.to}</span>
              </div>
              {"cc" in preview && preview.cc ? (
                <div className="mock-email-field">
                  <strong>CC:</strong> <span>{preview.cc}</span>
                </div>
              ) : null}
              <div className="mock-email-field">
                <strong>Subject:</strong> <span>{preview.subject}</span>
              </div>
              <div className="mock-email-content">{preview.body}</div>
            </div>
          ))}
        </div>
      </div>
    );
  };

  useEffect(() => {
    function updateScope() {
      const storedId = sessionStorage.getItem("slmct_selected_org_id") || "";
      const storedCurrency = sessionStorage.getItem("slmct_selected_org_currency") || "AED";
      setSelectedOrgId(storedId);
      setSelectedOrgCurrency(storedCurrency);
    }
    updateScope();
    window.addEventListener("slmct_org_changed", updateScope);
    return () => window.removeEventListener("slmct_org_changed", updateScope);
  }, []);

  const getScopedList = <T extends { organisation_id?: unknown; id?: unknown }>(list: T[]): T[] => {
    if (!selectedOrgId || !allOrganisations.length) return list;
    const selectedOrg = allOrganisations.find((o) => o.id === selectedOrgId);
    if (!selectedOrg || selectedOrg.code === "derisk360_group") {
      return list;
    }
    return list.filter((r) => {
      if (r.organisation_id != null && r.organisation_id !== "") {
        return String(r.organisation_id) === selectedOrgId;
      }
      if (moduleKey === "organisations" && r.id != null) {
        return String(r.id) === selectedOrgId;
      }
      return false;
    });
  };

  const scopedRecords = useMemo(() => {
    return getScopedList(records);
  }, [records, selectedOrgId, allOrganisations]);

  const legacyDemoUsers = useMemo(
    () => (moduleKey === "users" ? scopedRecords.filter((record) => isLegacyDemoEmail(record.work_email)) : []),
    [moduleKey, scopedRecords]
  );

  const scopedVendors = useMemo(() => getScopedList(allVendors), [allVendors, selectedOrgId, allOrganisations]);
  const scopedSubscriptions = useMemo(() => getScopedList(allSubscriptions), [allSubscriptions, selectedOrgId, allOrganisations]);
  const scopedLicences = useMemo(() => getScopedList(allLicences), [allLicences, selectedOrgId, allOrganisations]);
  const scopedBudgets = useMemo(() => getScopedList(allBudgets), [allBudgets, selectedOrgId, allOrganisations]);
  const scopedPayments = useMemo(() => getScopedList(allPayments), [allPayments, selectedOrgId, allOrganisations]);
  const scopedEmployees = useMemo(() => getScopedList(allEmployees), [allEmployees, selectedOrgId, allOrganisations]);
  const scopedContracts = useMemo(() => getScopedList(allContracts), [allContracts, selectedOrgId, allOrganisations]);

  // Departments available for software requests: derived from the selected org's
  // budget records so each org only offers departments that actually exist there.
  // Falls back to the full static list when the org has no budgets yet.
  const requestDepartmentOptions = useMemo(() => {
    const depts = Array.from(
      new Set(
        scopedBudgets
          .filter((b) => String(b.status) !== "closed")
          .map((b) => String(b.department || "").trim())
          .filter(Boolean)
      )
    ).sort();
    return depts.length
      ? depts
      : ["Software Engineering", "Human Resources", "Finance & Accounts", "Marketing", "Sales", "IT", "Product", "Operations"];
  }, [scopedBudgets]);

  const departmentStats = useMemo(() => {
    const currentYear = new Date().getFullYear();
    // Build the department list dynamically from budget records + subscriptions
    const deptSet = new Set<string>();
    scopedBudgets.forEach((b) => { if (b.department) deptSet.add(String(b.department)); });
    scopedSubscriptions.forEach((s) => { if (s.department) deptSet.add(String(s.department)); });
    const departments = Array.from(deptSet).sort();

    return departments.map((dept) => {
      const deptSubs = scopedSubscriptions.filter(
        (s) =>
          String(s.department || "").toLowerCase().trim() === dept.toLowerCase().trim() &&
          ["active", "trial", "pending_renewal"].includes(String(s.status || ""))
      );
      const totalCost = deptSubs.reduce((sum, s) =>
        sum + toOrgCurrency(Number(s.amount || 0), String(s.currency_code || selectedOrgCurrency), fxRates, selectedOrgCurrency), 0);

      const deptBuds = scopedBudgets.filter(
        (b) =>
          String(b.department || "").toLowerCase().trim() === dept.toLowerCase().trim() &&
          b.status !== "closed"
      );
      const deptBud = deptBuds.find((b) => Number(b.fiscal_year) === currentYear) || deptBuds[0];
      const allocatedAmount = deptBud
        ? toOrgCurrency(Number(deptBud.allocated_amount), String(deptBud.currency_code || selectedOrgCurrency), fxRates, selectedOrgCurrency)
        : 0;
      const budgetLeft = allocatedAmount - totalCost;

      return {
        department: dept,
        budgetId: deptBud ? String(deptBud.id) : null,
        budgetRaw: deptBud ?? null,
        softwareCount: deptSubs.length,
        totalCost,
        allocatedAmount,
        budgetLeft,
        hasBudget: !!deptBud,
        subscriptions: deptSubs,
      };
    });
  }, [scopedSubscriptions, scopedBudgets, fxRates, selectedOrgCurrency]);

  const requiredSources = useMemo(() => {
    if (!config) return [];
    return Array.from(new Set(config.createFields.map((field) => field.source).filter(Boolean))) as string[];
  }, [config]);

  const userRoles = normalizeRoles(user?.roles);
  const isMasterAdmin = userRoles.includes("master_admin");
  const isItAdmin = userRoles.includes("it_admin");
  const isFinance = userRoles.includes("finance");
  const isHrAdmin = userRoles.includes("hr_admin");
  const isLineManager = userRoles.includes("line_manager");
  const isEmployee = userRoles.includes("employee");
  const showDashboardBudget = Boolean(user) && canViewDashboardBudget(userRoles);
  const showDepartmentalGovernance = showDashboardBudget;
  const showFinancialSpend = canViewFinancialSpend(userRoles);
  const showRenewalAmounts = canViewRenewalAmounts(userRoles);
  const showProcurementDetails = canViewProcurementDetails(userRoles);
  const showEmailSettings = canConfigureEmail(userRoles);
  const isSoftwareModule = moduleKey === "subscriptions" || moduleKey === "licences";
  const masterUsesWorkflow = isMasterAdmin && masterSoftwareCreateMode === "workflow";
  const canDirectCreateSoftware =
    isSoftwareModule && canDirectCreateSoftwareRecords(userRoles) && !masterUsesWorkflow;
  const allowedSoftwareWorkflowTypes = getAllowedWorkflowTypes(moduleKey, userRoles, { masterUsesWorkflow });
  const canSubmitSoftwareWorkflowRequest = canSubmitSoftwareWorkflow(moduleKey, userRoles, { masterUsesWorkflow });
  const canMutate =
    Boolean(config && !config.readOnly) &&
    (isSoftwareModule
      ? canDirectCreateSoftware || canSubmitSoftwareWorkflowRequest
      : isMasterAdmin ||
        (["users", "employees"].includes(moduleKey) && isItAdmin) ||
        (moduleKey === "employees" && isHrAdmin) ||
        (itWorkflowModules.has(moduleKey) && isItAdmin) ||
        (hrWorkflowModules.has(moduleKey) && isHrAdmin) ||
        (financeWorkflowModules.has(moduleKey) && isFinance));
  const usesWorkflow = Boolean(
    config?.workflowCreate && (!isMasterAdmin || masterUsesWorkflow) && !(isFinance && moduleKey === "payments") && canMutate && !isSoftwareModule
  );
  const isWorkflowSoftwareCreate =
    canSubmitSoftwareWorkflowRequest && !editingRecord?.id && isSoftwareModule;
  const softwareToolSummary = useMemo(() => {
    if (!isWorkflowSoftwareCreate) return null;
    return buildSoftwareToolSummary(moduleKey, workflowFormDraft, scopedSubscriptions, allVendors);
  }, [isWorkflowSoftwareCreate, moduleKey, workflowFormDraft, scopedSubscriptions, allVendors]);
  const canBulkUpload = Boolean(config?.bulkUpload && bulkUploadModules.has(moduleKey) && (isMasterAdmin || isItAdmin || isFinance || isHrAdmin));

  useEffect(() => {
    if (!isWorkflowSoftwareCreate || !isModalOpen || !user?.email) {
      setWorkflowEmailPreview([]);
      setWorkflowEmailPreviewError("");
      setWorkflowEmailPreviewLoading(false);
      return;
    }

    let cancelled = false;
    setWorkflowEmailPreviewLoading(true);
    setWorkflowEmailPreviewError("");

    fetch(`${apiBaseUrl}/api/email/preview/workflow`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        actor_user_id: user.id ?? null,
        actor_email: user.email ?? null,
        requested_module: moduleKey,
        workflow_type: resolveWorkflowType(moduleKey, workflowFormDraft),
        payload: workflowFormDraft,
        event: "submitted",
      }),
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(await responseError(response));
        }
        return response.json();
      })
      .then((data) => {
        if (!cancelled) {
          setWorkflowEmailPreview(Array.isArray(data) ? data : []);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setWorkflowEmailPreview([]);
          setWorkflowEmailPreviewError(
            error instanceof Error ? error.message : "Could not load email preview."
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setWorkflowEmailPreviewLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [isWorkflowSoftwareCreate, isModalOpen, user?.id, user?.email, moduleKey, workflowFormDraft]);
  const canResetPassword = moduleKey === "users" && (isMasterAdmin || isItAdmin);
  const showActions = !config?.hideActions;
  const canApproveWorkflow = isMasterAdmin;
  const canLineManagerApproveWorkflow = canLineManagerApprove(userRoles);
  const canValidateBudget = isFinance || isMasterAdmin;
  const canCompleteWorkflow = isItAdmin || isMasterAdmin;
  const canReopenWorkflow = isItAdmin || isFinance || isMasterAdmin;

  useEffect(() => {
    const stored = sessionStorage.getItem("slmct_user");
    if (stored) {
      const parsed = JSON.parse(stored);
      setUser(parsed);
      const roles: string[] = parsed?.roles ?? [];
      if (roles.includes("employee") && parsed?.email) {
        loadEmployeeDashboardData(String(parsed.email));
      }
      if (roles.includes("line_manager") && parsed?.email) {
        loadLineManagerDashboardData(String(parsed.email));
      }
      if (roles.includes("finance")) {
        loadFinanceDashboardData();
      }
      if (roles.includes("it_admin")) {
        loadItAdminDashboardData();
      }
    }
  }, []);

  async function loadAllContext() {
    try {
      const [orgs, vens, subs, lics, budg, pays, emps, ctrs, users, cat, fxResp] = await Promise.all([
        fetch(`${apiBaseUrl}/organisations`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/vendors`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/subscriptions`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/licences`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/budgets`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/payments`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/employees`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/contracts`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/users`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/vendor-catalogue`).then((r) => (r.ok ? r.json() : [])),
        fetch(`${apiBaseUrl}/fx-rates`).then((r) => (r.ok ? r.json() : null)),
      ]);
      setAllOrganisations(orgs);
      setAllVendors(vens);
      setAllSubscriptions(subs);
      setAllLicences(lics);
      setAllBudgets(budg);
      setAllPayments(pays);
      setAllEmployees(emps);
      setAllContracts(ctrs);
      setAllUsers(users);
      setVendorCatalogue(cat);
      if (fxResp?.rates) setFxRates(fxResp.rates);
    } catch (err) {
      console.error("Failed to load context data:", err);
    }
  }

  useEffect(() => {
    loadAllContext();
  }, [moduleKey, selectedOrgId]);

  async function loadRecords(options?: { silent?: boolean }) {
    if (!config || moduleKey === "dashboard" || moduleKey === "settings" || !config.endpoint) return;
    if (!options?.silent) setLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}${config.endpoint}`);
      if (!response.ok) throw new Error(await response.text());
      setRecords(await response.json());
    } catch {
      setMessage("Unable to load records. Confirm the API service is running.");
    } finally {
      if (!options?.silent) setLoading(false);
    }
  }

  function workflowStatusFromAction(
    action: "approve" | "line-manager-approve" | "complete" | "reject" | "reopen" | "validate-budget" | "request-info"
  ): string {
    if (action === "line-manager-approve") return "line_manager_approved";
    if (action === "validate-budget") return "finance_approved";
    if (action === "approve") return "master_approved";
    if (action === "complete") return "completed";
    if (action === "reject") return "rejected";
    if (action === "reopen") return "reopened";
    if (action === "request-info") return "info_requested";
    return "submitted";
  }

  function patchWorkflowRecord(recordId: unknown, patch: Partial<AnyRecord>) {
    setRecords((previous) =>
      previous.map((record) => (String(record.id) === String(recordId) ? { ...record, ...patch } : record))
    );
    setWorkflowDetailsRecord((previous) =>
      previous && String(previous.id) === String(recordId) ? { ...previous, ...patch } : previous
    );
  }

  async function reloadEmailLogs() {
    if (moduleKey !== "workflows" && moduleKey !== "tool-requests") return;
    try {
      const response = await fetch(`${apiBaseUrl}/api/email/logs`);
      if (response.ok) setAllEmailLogs(await response.json());
    } catch {
      /* ignore */
    }
  }

  async function refreshWorkflowBoard() {
    await Promise.all([loadRecords({ silent: true }), reloadEmailLogs()]);
  }

  // Workflow actions can be triggered from a role dashboard (not just the Workflows
  // page), whose queues live in separate state (itAllWorkflows, financeWorkflowQueue,
  // etc.) that refreshWorkflowBoard/loadRecords never touches. Re-fetch whichever
  // dashboard is currently showing so completed/approved items move out of its queue.
  async function refreshDashboardData() {
    if (moduleKey !== "dashboard") return;
    if (isItAdmin) await loadItAdminDashboardData();
    if (isFinance) await loadFinanceDashboardData();
    if (isMasterAdmin) await loadDashboard();
    if (user?.email) {
      if (userRoles.includes("employee")) await loadEmployeeDashboardData(String(user.email));
      if (canLineManagerApproveWorkflow) await loadLineManagerDashboardData(String(user.email));
    }
  }

  async function loadDashboard() {
    setLoading(true);
    try {
      const query = selectedOrgId ? `?organisation_id=${encodeURIComponent(selectedOrgId)}` : "";
      const response = await fetch(`${apiBaseUrl}/dashboard/summary${query}`);
      if (!response.ok) throw new Error(await response.text());
      setSummary(await response.json());
    } catch {
      setMessage("Unable to load dashboard summary. Confirm the API service is running.");
    } finally {
      setLoading(false);
    }
  }

  async function loadEmployeeDashboardData(email: string) {
    try {
      const [wfRes, emailRes] = await Promise.all([
        fetch(`${apiBaseUrl}/workflow-requests`),
        fetch(`${apiBaseUrl}/api/email/logs`),
      ]);
      const wfAll: AnyRecord[] = wfRes.ok ? await wfRes.json() : [];
      const emailAll: AnyRecord[] = emailRes.ok ? await emailRes.json() : [];
      const emailLower = email.toLowerCase();
      setMyWorkflowRequests(
        wfAll.filter((w) =>
          String(w.requested_by_email || "").toLowerCase() === emailLower ||
          String(w.assigned_employee_email || "").toLowerCase() === emailLower
        )
      );
      setMyEmailNotifications(
        emailAll.filter((e) => {
          const to = String(e.to || e.to_email || "").toLowerCase();
          return to.includes(emailLower);
        }).slice(0, 30)
      );
    } catch {
      // silently ignore — employee dashboard degrades gracefully
    }
  }

  async function loadLineManagerDashboardData(email: string) {
    try {
      const [teamRes, wfRes, emailRes] = await Promise.all([
        fetch(`${apiBaseUrl}/employees/my-team?manager_email=${encodeURIComponent(email)}`),
        fetch(`${apiBaseUrl}/workflow-requests`),
        fetch(`${apiBaseUrl}/api/email/logs`),
      ]);
      const team: AnyRecord[] = teamRes.ok ? await teamRes.json() : [];
      const wfAll: AnyRecord[] = wfRes.ok ? await wfRes.json() : [];
      const emailAll: AnyRecord[] = emailRes.ok ? await emailRes.json() : [];

      const teamEmails = new Set(team.map((m) => String(m.work_email || "").toLowerCase()));

      // Only workflow requests from team members
      const teamWf = wfAll.filter((w) =>
        teamEmails.has(String(w.requested_by_email || "").toLowerCase()) ||
        teamEmails.has(String(w.assigned_employee_email || "").toLowerCase())
      );

      // Email logs sent to this line manager
      const emailLower = email.toLowerCase();
      const lmEmails = emailAll.filter((e) => {
        const to = String(e.to || e.to_email || "").toLowerCase();
        return to.includes(emailLower);
      }).slice(0, 30);

      setLmTeamMembers(team);
      setLmTeamWorkflowRequests(teamWf);
      setLmTeamEmailNotifications(lmEmails);
    } catch {
      // silently ignore — line manager dashboard degrades gracefully
    }
  }

  async function loadFinanceDashboardData() {
    try {
      const res = await fetch(`${apiBaseUrl}/workflow-requests`);
      const all: AnyRecord[] = res.ok ? await res.json() : [];
      const queue = all.filter((w) =>
        ["line_manager_approved", "master_approved"].includes(String(w.status || ""))
      );
      setFinanceWorkflowQueue(queue);
    } catch {
      // degrade gracefully
    }
  }

  async function loadItAdminDashboardData() {
    try {
      const [wfRes, trRes] = await Promise.all([
        fetch(`${apiBaseUrl}/workflow-requests`),
        fetch(`${apiBaseUrl}/tool-requests`),
      ]);
      setItAllWorkflows(wfRes.ok ? await wfRes.json() : []);
      setItToolRequests(trRes.ok ? await trRes.json() : []);
    } catch {
      // degrade gracefully
    }
  }

  useEffect(() => {
    if (moduleKey === "dashboard") {
      loadDashboard();
      return;
    }
    if (moduleKey === "audit-logs") {
      setSubLogLoading(true);
      const q = selectedOrgId ? `?organisation_id=${encodeURIComponent(selectedOrgId)}` : "";
      fetch(`${apiBaseUrl}/subscription-creation-log${q}`)
        .then((r) => (r.ok ? r.json() : []))
        .then((data) => setSubscriptionCreationLog(data))
        .catch(() => setSubscriptionCreationLog([]))
        .finally(() => setSubLogLoading(false));
    }

    loadRecords();
  }, [moduleKey, selectedOrgId]);

  useEffect(() => {
    if (moduleKey !== "employees") return;

    fetch(`${apiBaseUrl}/employees/sync/status`)
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => setEmployeeSyncStatus(data))
      .catch(() => setEmployeeSyncStatus(null));
  }, [moduleKey]);

  useEffect(() => {
    if (moduleKey !== "settings") return;

    fetch(`${apiBaseUrl}/api/email/status`)
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => setEmailStatus(data))
      .catch(() => setEmailStatus(null));

    fetch(`${apiBaseUrl}/api/email/role-mailboxes`)
      .then((response) => (response.ok ? response.json() : []))
      .then((data) => {
        if (Array.isArray(data) && data.length) {
          setDemoRecipientOptions(data);
          if (!testEmailTo) {
            setTestEmailTo(String(data[0]?.email || "deriskemployee1@outlook.com"));
          }
        }
      })
      .catch(() => setDemoRecipientOptions([]));
  }, [moduleKey]);

  useEffect(() => {
    async function loadRelations() {
      const loaded: Record<string, AnyRecord[]> = {};
      await Promise.all(
        requiredSources.map(async (source) => {
          const response = await fetch(`${apiBaseUrl}/${source}`);
          loaded[source] = response.ok ? await response.json() : [];
        }),
      );
      setRelations(loaded);
    }

    if (requiredSources.length > 0) {
      loadRelations();
    }
  }, [requiredSources]);

  if (user && !canAccessModule(moduleKey, userRoles)) {
    return (
      <AppShell title="Access denied" description="Your role does not include this module.">
        <section className="workspace-panel">
          <p>You do not have permission to view this area. Open a module from the sidebar that matches your assigned role.</p>
        </section>
      </AppShell>
    );
  }

  if (!config && moduleKey !== "dashboard") {
    return (
      <AppShell title="Module not found" description="The requested workspace module is not available.">
        <section className="workspace-panel">
          <p>Use the sidebar to open a supported SLMCT module.</p>
        </section>
      </AppShell>
    );
  }

  // ── Employee-specific dashboard ────────────────────────────────────────────
  if (moduleKey === "dashboard" && isEmployee) {
    const myEmployee = allEmployees.find(
      (e) => String(e.work_email || "").toLowerCase() === String(user?.email || "").toLowerCase()
    );
    const myLicences = myEmployee
      ? allLicences.filter((l) => String(l.assigned_to_person_id) === String(myEmployee.id))
      : [];

    // Also surface subscriptions activated through this employee's completed workflows
    // when no licence seat is directly assigned to them yet
    const myLicencedSubIds = new Set(myLicences.map((l) => String(l.subscription_id)));
    const myCompletedSubRows = myWorkflowRequests
      .filter((w) => w.status === "completed" && w.requested_module === "subscriptions" && w.activated_entity_id)
      .map((w) => {
        const sub = allSubscriptions.find((s) => String(s.id) === String(w.activated_entity_id));
        return sub ? { _type: "subscription_via_workflow" as const, sub, wf: w } : null;
      })
      .filter((x): x is { _type: "subscription_via_workflow"; sub: AnyRecord; wf: AnyRecord } =>
        x !== null && !myLicencedSubIds.has(String(x.sub.id))
      );

    const notifIcons: Record<string, string> = {
      workflow_completed: "✅",
      workflow_created: "📝",
      master_approved: "👍",
      finance_approved_requester: "💰",
      workflow_rejected: "❌",
      tool_request_acknowledgement: "📨",
      tool_request_rejected: "🚫",
      default: "🔔",
    };

    return (
      <AppShell
        title="My Workspace"
        description="Your assigned software, submitted requests, and recent workflow notifications."
      >
        {message ? <p className="module-message">{message}</p> : null}

        <div className="emp-dashboard">
          {/* A — My Active Software */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>My Active Software &amp; Licences</h2>
                <p>Software assigned to you — licences, access details, and expiry dates.</p>
              </div>
              <span className="emp-section-count">{myLicences.length + myCompletedSubRows.length} item{(myLicences.length + myCompletedSubRows.length) !== 1 ? "s" : ""}</span>
            </div>
            {myLicences.length === 0 && myCompletedSubRows.length === 0 ? (
              <div className="emp-empty">No licences are currently assigned to your account.</div>
            ) : (
              <table className="emp-table">
                <thead>
                  <tr>
                    <th>Software</th>
                    <th>Vendor</th>
                    <th>Status</th>
                    <th>Assigned</th>
                    <th>Expires</th>
                    <th>Seat / Reference</th>
                  </tr>
                </thead>
                <tbody>
                  {myLicences.map((lic) => {
                    const sub = allSubscriptions.find((s) => String(s.id) === String(lic.subscription_id));
                    const vendor = sub ? allVendors.find((v) => String(v.id) === String(sub.vendor_id)) : null;
                    const softwareName = String(lic.licence_name || sub?.name || "Software");
                    return (
                      <tr key={String(lic.id)}>
                        <td>
                          <span className="primary">{softwareName}</span>
                          {sub?.name && lic.licence_name && lic.licence_name !== sub.name ? (
                            <span className="secondary">{String(sub.name)}</span>
                          ) : null}
                        </td>
                        <td>{String(vendor?.name || "-")}</td>
                        <td><span className={getWorkflowStatusBadgeClass(lic.status)}>{formatSubscriptionStatus(lic.status)}</span></td>
                        <td>{lic.assigned_at ? formatWorkflowDateOnly(lic.assigned_at) : "-"}</td>
                        <td>{lic.expires_at ? formatWorkflowDateOnly(lic.expires_at) : "-"}</td>
                      </tr>
                    );
                  })}
                  {myCompletedSubRows.map(({ sub, wf }) => {
                    const vendor = allVendors.find((v) => String(v.id) === String(sub.vendor_id));
                    const softwareName = String(sub.name || "Software");
                    return (
                      <tr key={`wf-sub-${String(wf.id)}`}>
                        <td>
                          <span className="primary">{softwareName}</span>
                          <span className="secondary">Via completed request #{String(wf.id).slice(0, 8).toUpperCase()}</span>
                        </td>
                        <td>{String(vendor?.name || "-")}</td>
                        <td><span className={getWorkflowStatusBadgeClass(sub.status)}>{formatSubscriptionStatus(sub.status)}</span></td>
                        <td>{wf.created_at ? formatWorkflowDateOnly(wf.created_at) : "-"}</td>
                        <td>-</td>
                        <td>-</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>

          {/* B — Action Required: Info Requested */}
          {myWorkflowRequests.filter((r) => r.status === "info_requested").length > 0 && (
            <section className="emp-section" style={{ border: "1px solid rgba(251,191,36,0.35)", borderRadius: "12px", background: "rgba(251,191,36,0.04)" }}>
              <div className="emp-section-header">
                <div>
                  <h2 style={{ color: "#fbbf24" }}>⚠ Action Required — More Information Needed</h2>
                  <p>An approver has asked for more details on the requests below. Click "Respond" to provide the information.</p>
                </div>
                <span className="emp-section-count">{myWorkflowRequests.filter((r) => r.status === "info_requested").length}</span>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                {myWorkflowRequests.filter((r) => r.status === "info_requested").map((req) => (
                  <div key={String(req.id)} style={{
                    background: "rgba(255,255,255,0.04)", borderRadius: "8px", padding: "14px 16px",
                    border: "1px solid rgba(251,191,36,0.2)", display: "flex", flexDirection: "column", gap: "8px",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "8px" }}>
                      <div>
                        <span style={{ fontWeight: 700, fontSize: "13px" }}>{getWorkflowSoftwareName(req, allSubscriptions)}</span>
                        <span style={{ color: "rgba(255,255,255,0.45)", fontSize: "12px", marginLeft: "10px" }}>
                          #{String(req.id).slice(0, 8).toUpperCase()} · {formatWorkflowDateOnly(req.created_at)}
                        </span>
                      </div>
                      <button
                        type="button"
                        className="k-btn k-btn--approve"
                        style={{ fontSize: "12px", padding: "5px 14px" }}
                        onClick={() => setInfoResponseModal({ workflow: req, text: "" })}
                      >
                        Respond to Request
                      </button>
                    </div>
                    {req.info_request_message ? (
                      <div style={{
                        background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.25)",
                        borderRadius: "6px", padding: "8px 12px", fontSize: "12px", color: "#fbbf24",
                      }}>
                        <strong>What was asked:</strong> {String(req.info_request_message)}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* C — My Requests */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>My Requests</h2>
                <p>All software requests you have submitted — current status and approval stage.</p>
              </div>
              <span className="emp-section-count">{myWorkflowRequests.length} request{myWorkflowRequests.length !== 1 ? "s" : ""}</span>
            </div>
            {myWorkflowRequests.length === 0 ? (
              <div className="emp-empty">No requests submitted yet. Use the sidebar to submit a software request.</div>
            ) : (
              <table className="emp-table">
                <thead>
                  <tr>
                    <th>Workflow ID</th>
                    <th>Request Type</th>
                    <th>Software</th>
                    <th>Submitted</th>
                    <th>Status</th>
                    <th>Current Stage</th>
                    <th>Current Approver</th>
                  </tr>
                </thead>
                <tbody>
                  {myWorkflowRequests.map((req) => (
                    <tr key={String(req.id)}>
                      <td>
                        <span style={{ fontFamily: "monospace", fontSize: "0.78rem", color: "var(--brand-cyan)" }}>
                          #{String(req.id || "").slice(0, 8).toUpperCase()}
                        </span>
                      </td>
                      <td>{workflowTypeLabel(req.workflow_type)}</td>
                      <td>
                        <span className="primary">{getWorkflowSoftwareName(req, allSubscriptions)}</span>
                      </td>
                      <td>{formatWorkflowDateOnly(req.created_at)}</td>
                      <td>
                        <span className={getWorkflowStatusBadgeClass(req.status)}>
                          {getWorkflowStatusLabel(req.status, req.workflow_type, req.pre_info_request_status)}
                        </span>
                      </td>
                      <td>{String(req.workflow_stage || req.status || "-").replace(/_/g, " ")}</td>
                      <td>
                        {req.current_approver_name ? (
                          <span>
                            {String(req.current_approver_name)}
                            {req.current_approver_email ? (
                              <span className="secondary">{String(req.current_approver_email)}</span>
                            ) : null}
                          </span>
                        ) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {/* C — My Notifications */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>My Notifications</h2>
                <p>Recent workflow events and updates sent to your account.</p>
              </div>
              <span className="emp-section-count">{myEmailNotifications.filter((n) => ["request_approved","workflow_completed","request_completed","request_rejected","workflow_started","info_requested"].includes(String(n.eventType || ""))).length} notification{myEmailNotifications.filter((n) => ["request_approved","workflow_completed","request_completed","request_rejected","workflow_started","info_requested"].includes(String(n.eventType || ""))).length !== 1 ? "s" : ""}</span>
            </div>
            {myEmailNotifications.length === 0 ? (
              <div className="emp-empty">No notifications yet. Updates will appear here as your requests progress.</div>
            ) : (
              <table className="emp-table">
                <thead>
                  <tr>
                    <th>Workflow ID</th>
                    <th>Notification</th>
                    <th>Date &amp; Time</th>
                  </tr>
                </thead>
                <tbody>
                  {myEmailNotifications
                    .filter((n) => ["request_approved","workflow_completed","request_completed","request_rejected","workflow_started","info_requested"].includes(String(n.eventType || "")))
                    .map((notif) => {
                      const wfId = String(notif.workflowId || notif.relatedRequestId || "");
                      const eventType = String(notif.eventType || "");
                      const stage = String(notif.workflowStage || "");

                      // Who approved based on which stage it moved to
                      const approverByStage: Record<string, string> = {
                        "Waiting for Budget Approval": "Line Manager",
                        "Awaiting Procurement": "Finance",
                        "Completed": "IT Admin",
                      };
                      const approver = approverByStage[stage] || "";

                      // Build notification message
                      let message: React.ReactNode;
                      if (eventType === "workflow_started") {
                        message = <>Request submitted — awaiting approval</>;
                      } else if (eventType === "request_approved") {
                        message = <>Your request has been{" "}
                          <strong style={{ color: "#22c55e", fontWeight: 800 }}>APPROVED</strong>
                          {approver ? <> by {approver}</> : null}
                          {stage ? <> — now at: {stage}</> : null}
                        </>;
                      } else if (eventType === "workflow_completed" || eventType === "request_completed") {
                        message = <>Your request is{" "}
                          <strong style={{ color: "#22c55e", fontWeight: 800 }}>COMPLETED</strong>
                          {" "}— your software is now active
                        </>;
                      } else if (eventType === "request_rejected") {
                        message = <>Your request has been{" "}
                          <strong style={{ color: "#ef4444", fontWeight: 800 }}>REJECTED</strong>
                        </>;
                      } else if (eventType === "info_requested") {
                        message = <>An approver has requested{" "}
                          <strong style={{ color: "#fbbf24", fontWeight: 800 }}>MORE INFORMATION</strong>
                          {" "}— check the Action Required section above
                        </>;
                      } else {
                        message = <>{stage || eventType}</>;
                      }

                      const rawDt = notif.createdAt || notif.sentAt || notif.created_at;
                      const dt = rawDt ? new Date(String(rawDt)) : null;
                      const dateStr = dt && !isNaN(dt.getTime()) ? dt.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—";
                      const timeStr = dt && !isNaN(dt.getTime()) ? dt.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "";

                      return (
                        <tr key={String(notif.id)}>
                          <td>
                            <span style={{ fontFamily: "monospace", fontSize: "0.78rem", color: "var(--brand-cyan)" }}>
                              {wfId ? `#${wfId.slice(0, 8).toUpperCase()}` : "—"}
                            </span>
                          </td>
                          <td><span style={{ fontSize: "13px" }}>{message}</span></td>
                          <td style={{ whiteSpace: "nowrap" }}>
                            <span style={{ fontSize: "12px" }}>{dateStr}</span>
                            {timeStr && <span className="secondary" style={{ fontSize: "11px" }}>{timeStr}</span>}
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            )}
          </section>
        </div>

        {/* ── Employee: Respond to Info Request Modal ── */}
        {infoResponseModal && (
          <div className="modal-backdrop" role="presentation" onClick={() => setInfoResponseModal(null)}>
            <div className="modal-panel" style={{ maxWidth: "520px" }} onClick={(e) => e.stopPropagation()}>
              <div className="modal-header">
                <div>
                  <p className="eyebrow">More Information Required</p>
                  <h2>{getWorkflowSoftwareName(infoResponseModal.workflow, allSubscriptions)}</h2>
                </div>
                <button type="button" onClick={() => setInfoResponseModal(null)}>Close</button>
              </div>
              <div style={{ padding: "20px 24px", display: "flex", flexDirection: "column", gap: "16px" }}>
                {infoResponseModal.workflow.info_request_message ? (
                  <div style={{
                    background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.3)",
                    borderRadius: "8px", padding: "12px 16px", fontSize: "13px", color: "#fbbf24",
                  }}>
                    <strong>What the approver asked:</strong><br />
                    {String(infoResponseModal.workflow.info_request_message)}
                  </div>
                ) : null}
                <label style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "13px" }}>
                  <span style={{ fontWeight: 600 }}>Your response</span>
                  <textarea
                    rows={5}
                    style={{
                      background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.15)",
                      borderRadius: "8px", color: "#fff", padding: "10px 12px", fontSize: "13px", resize: "vertical",
                    }}
                    placeholder="Provide the additional information requested…"
                    value={infoResponseModal.text}
                    onChange={(e) => setInfoResponseModal((prev) => prev ? { ...prev, text: e.target.value } : null)}
                  />
                </label>
              </div>
              <div className="modal-actions">
                <button type="button" onClick={() => setInfoResponseModal(null)}>Cancel</button>
                <button
                  type="button"
                  className="k-btn k-btn--approve"
                  disabled={!infoResponseModal.text.trim()}
                  onClick={async () => {
                    try {
                      const res = await fetch(`${apiBaseUrl}/workflow-requests/${infoResponseModal.workflow.id}/resubmit`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                          actor_user_id: user?.id ?? null,
                          actor_email: user?.email ?? null,
                          actor_roles: userRoles,
                          payload: { info_response: infoResponseModal.text },
                        }),
                      });
                      if (!res.ok) throw new Error("Failed");
                      await loadAllContext();
                      if (user?.email) loadEmployeeDashboardData(String(user.email));
                      setInfoResponseModal(null);
                      setToast("Response submitted — your request is back in the approval queue.");
                      setToastType("success");
                      setToastTitle("Response sent");
                    } catch {
                      setToast("Failed to submit response.");
                      setToastType("error");
                      setToastTitle("Error");
                    }
                  }}
                >
                  Submit Response
                </button>
              </div>
            </div>
          </div>
        )}

      </AppShell>
    );
  }

  // ── Line Manager dashboard ────────────────────────────────────────────────────
  if (moduleKey === "dashboard" && isLineManager) {
    const teamEmails = new Set(lmTeamMembers.map((m) => String(m.work_email || "").toLowerCase()));

    const closedStatuses = ["completed", "rejected", "cancelled", "finance_closed"];

    const pendingApprovals = lmTeamWorkflowRequests.filter(
      (w) => w.status === "submitted" || w.status === "reopened"
    );
    const inProgressCount = lmTeamWorkflowRequests.filter(
      (w) => !["submitted", "reopened", ...closedStatuses].includes(String(w.status))
    ).length;

    // Licences assigned to team members
    const teamLicences = allLicences.filter((l) => {
      const emp = allEmployees.find((e) => String(e.id) === String(l.assigned_to_person_id));
      return emp && teamEmails.has(String(emp.work_email || "").toLowerCase());
    });

    // Software used by team: subscriptions that have a licence assigned to a team member
    const teamSubIds = new Set(teamLicences.map((l) => String(l.subscription_id)));
    const teamSoftware = allSubscriptions
      .filter((sub) => teamSubIds.has(String(sub.id)))
      .map((sub) => ({
        id: String(sub.id),
        name: String(sub.name || "Unknown"),
        department: String(sub.department || ""),
        licenceCount: teamLicences.filter((l) => String(l.subscription_id) === String(sub.id)).length,
        status: String(sub.status || ""),
      }))
      .sort((a, b) => b.licenceCount - a.licenceCount);

    // Active requests only (for stat cards and per-member counts)
    const activeTeamRequests = lmTeamWorkflowRequests.filter(
      (w) => !closedStatuses.includes(String(w.status))
    );

    // Recent activity shows all requests including completed/rejected — newest first
    const recentActivity = [...lmTeamWorkflowRequests]
      .sort((a, b) => new Date(String(b.created_at || 0)).getTime() - new Date(String(a.created_at || 0)).getTime())
      .slice(0, 8);

    const statusColour = (s: unknown): string => {
      const v = String(s || "");
      if (v === "submitted" || v === "reopened") return "#f97316";
      if (v === "completed") return "#3b82f6";
      if (v === "rejected" || v === "cancelled") return "#ef4444";
      return "#22c55e";
    };

    const activityDescription = (req: AnyRecord): string => {
      const who = String(req.requested_by || req.requested_by_email || "Someone");
      const what = getWorkflowSoftwareName(req, allSubscriptions) || String(req.workflow_type || "a request");
      const s = String(req.status || "");
      if (s === "submitted") return `${who} submitted a request for ${what}`;
      if (s === "reopened") return `${who}'s request for ${what} was reopened`;
      if (s === "completed") return `${what} request for ${who} completed`;
      if (s === "rejected" || s === "cancelled") return `${what} request for ${who} was ${s}`;
      return `${who}'s ${what} request is ${s.replace(/_/g, " ")}`;
    };

    return (
      <AppShell
        title="Line Manager Dashboard"
        description="Pending approvals, team members, software in use, and recent activity — scoped to your direct reports."
      >
        {message ? <p className="module-message">{message}</p> : null}

        <div className="emp-dashboard">

          {/* ── 1. Pending Approvals ── */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>Pending Approvals</h2>
                <p>Requests from your direct reports waiting for your sign-off.</p>
              </div>
              <span className="emp-section-count"
                style={pendingApprovals.length > 0
                  ? { background: "rgba(249,115,22,0.15)", color: "#f97316", borderColor: "rgba(249,115,22,0.4)" }
                  : undefined}>
                {pendingApprovals.length} waiting
              </span>
            </div>
            {lmTeamMembers.length === 0 ? (
              <div className="emp-empty">No team members assigned yet. An HR or IT admin must set the Line Manager Email field on each employee record in the Employees page.</div>
            ) : pendingApprovals.length === 0 ? (
              <div className="emp-empty">No requests pending your approval right now.</div>
            ) : (
              <table className="emp-table">
                <thead>
                  <tr>
                    <th>Employee</th>
                    <th>Software Requested</th>
                    <th>Request Type</th>
                    <th>Submitted</th>
                    <th>Stage</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {pendingApprovals.map((req) => (
                    <tr key={String(req.id)}>
                      <td>
                        <span className="primary">{String(req.requested_by || "—")}</span>
                        {req.requested_by_email ? (
                          <span className="secondary">{String(req.requested_by_email)}</span>
                        ) : null}
                      </td>
                      <td>
                        <span className="primary">{getWorkflowSoftwareName(req, allSubscriptions) || "—"}</span>
                      </td>
                      <td>{workflowTypeLabel(req.workflow_type)}</td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <span style={{ fontSize: "12px" }}>{formatWorkflowDateOnly(req.created_at)}</span>
                      </td>
                      <td>
                        <span style={{
                          fontSize: "0.72rem", fontWeight: 700, padding: "3px 8px", borderRadius: "999px",
                          background: "rgba(249,115,22,0.15)", color: "#f97316", border: "1px solid rgba(249,115,22,0.35)", whiteSpace: "nowrap",
                        }}>
                          Awaiting LM Approval
                        </span>
                      </td>
                      <td>
                        <button
                          type="button"
                          className="k-btn k-btn--approve"
                          onClick={() => setWorkflowDetailsRecord(req)}
                        >
                          Review
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {/* ── Info Request Responses — LM ── */}
          {(() => {
            const responded = lmTeamWorkflowRequests.filter(
              (w) => (w.status === "submitted" || w.status === "reopened") &&
                     (w.payload as Record<string, unknown>)?.info_response
            );
            if (responded.length === 0) return null;
            return (
              <section className="emp-section" style={{ border: "1px solid rgba(56,189,248,0.3)", borderRadius: "12px", background: "rgba(56,189,248,0.03)" }}>
                <div className="emp-section-header">
                  <div>
                    <h2 style={{ color: "#38bdf8" }}>ℹ Responses Received — Ready to Review</h2>
                    <p>These employees have responded to your information request. Review their response then approve or reject.</p>
                  </div>
                  <span className="emp-section-count">{responded.length}</span>
                </div>
                <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                  {responded.map((req) => (
                    <div key={String(req.id)} style={{
                      background: "rgba(255,255,255,0.04)", borderRadius: "8px", padding: "12px 16px",
                      border: "1px solid rgba(56,189,248,0.15)", display: "flex", alignItems: "center", justifyContent: "space-between", gap: "12px", flexWrap: "wrap",
                    }}>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: "13px" }}>{getWorkflowSoftwareName(req, allSubscriptions)}</div>
                        <div style={{ color: "rgba(255,255,255,0.45)", fontSize: "12px", marginTop: "2px" }}>
                          {String(req.requested_by_email || "—")} · #{String(req.id).slice(0, 8).toUpperCase()}
                        </div>
                      </div>
                      <button type="button" className="k-btn k-btn--approve" style={{ fontSize: "12px" }} onClick={() => setInfoReviewModal(req)}>
                        View Response &amp; Decide
                      </button>
                    </div>
                  ))}
                </div>
              </section>
            );
          })()}

          {/* ── 2. My Team & Summary ── */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>My Team</h2>
                <p>Your direct reports and their software request summary.</p>
              </div>
              <span className="emp-section-count">{lmTeamMembers.length} member{lmTeamMembers.length !== 1 ? "s" : ""}</span>
            </div>

            {/* Stat cards */}
            <div className="dashboard-grid" style={{ padding: "16px 16px 0", gap: "12px" }}>
              {[
                { label: "Team Members", value: lmTeamMembers.length, color: "var(--brand-cyan)" },
                { label: "Pending Approval", value: pendingApprovals.length, color: "#f97316" },
                { label: "In Progress", value: inProgressCount, color: "#22c55e" },
                { label: "Active Requests", value: activeTeamRequests.length, color: "var(--brand-cyan)" },
                { label: "Licences Held", value: teamLicences.length, color: "var(--brand-cyan)" },
              ].map((stat) => (
                <article key={stat.label} className="dashboard-card" style={{ minHeight: "auto", padding: "14px 18px" }}>
                  <span style={{ fontSize: "0.72rem", color: "rgba(255,255,255,0.55)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                    {stat.label}
                  </span>
                  <strong style={{ display: "block", fontSize: "1.6rem", fontWeight: 800, color: stat.color, marginTop: "4px" }}>
                    {stat.value}
                  </strong>
                </article>
              ))}
            </div>

            {/* Team member list */}
            {lmTeamMembers.length === 0 ? (
              <div className="emp-empty">No team members assigned. HR or IT admin should set the Line Manager Email on each employee record.</div>
            ) : (
              <table className="emp-table" style={{ marginTop: "12px" }}>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Department</th>
                    <th>Job Title</th>
                    <th>Licences</th>
                    <th>Requests</th>
                  </tr>
                </thead>
                <tbody>
                  {lmTeamMembers.map((member) => {
                    const memberEmail = String(member.work_email || "").toLowerCase();
                    const memberLicences = teamLicences.filter((l) => {
                      const emp = allEmployees.find((e) => String(e.id) === String(l.assigned_to_person_id));
                      return emp && String(emp.work_email || "").toLowerCase() === memberEmail;
                    });
                    const memberRequests = lmTeamWorkflowRequests.filter(
                      (w) => String(w.requested_by_email || "").toLowerCase() === memberEmail
                        && !closedStatuses.includes(String(w.status))
                    );
                    return (
                      <tr key={String(member.id)}>
                        <td><span className="primary">{String(member.full_name || "—")}</span></td>
                        <td><span style={{ fontSize: "12px" }}>{String(member.work_email || "—")}</span></td>
                        <td>{String(member.department || "—")}</td>
                        <td>{String(member.job_title || "—")}</td>
                        <td>
                          <span style={{ fontWeight: 700, color: "var(--brand-cyan)" }}>{memberLicences.length}</span>
                        </td>
                        <td>
                          <span style={{ fontWeight: 700, color: memberRequests.some((r) => r.status === "submitted" || r.status === "reopened") ? "#f97316" : "rgba(255,255,255,0.7)" }}>
                            {memberRequests.length}
                          </span>
                          {memberRequests.some((r) => r.status === "submitted" || r.status === "reopened") ? (
                            <span className="secondary" style={{ color: "#f97316" }}>pending</span>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>

          {/* ── 3. Team Software in Use ── */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>Software in Use by Your Team</h2>
                <p>Subscriptions with active licence seats held by your direct reports. Use this before approving duplicate requests.</p>
              </div>
              <span className="emp-section-count">{teamSoftware.length} app{teamSoftware.length !== 1 ? "s" : ""}</span>
            </div>
            {teamSoftware.length === 0 ? (
              <div className="emp-empty">No licences currently assigned to your team members.</div>
            ) : (
              <>
                <table className="emp-table">
                  <thead>
                    <tr>
                      <th>Software</th>
                      <th>Department</th>
                      <th>Team Seats</th>
                      <th>Status</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {teamSoftware.map((sw) => {
                      const isExpanded = lmSoftwareViewMembers?.subId === sw.id;
                      const membersUsing = lmTeamMembers.filter((m) =>
                        teamLicences.some(
                          (l) => String(l.subscription_id) === sw.id &&
                            allEmployees.some((e) => String(e.id) === String(l.assigned_to_person_id) &&
                              String(e.work_email || "").toLowerCase() === String(m.work_email || "").toLowerCase()
                            )
                        )
                      );
                      return (
                        <Fragment key={sw.id}>
                          <tr>
                            <td><span className="primary">{sw.name}</span></td>
                            <td>{sw.department || "—"}</td>
                            <td>
                              <span style={{ fontWeight: 700, color: "var(--brand-cyan)" }}>{sw.licenceCount}</span>
                              <span className="secondary">{sw.licenceCount === 1 ? "seat" : "seats"}</span>
                            </td>
                            <td><span className={getWorkflowStatusBadgeClass(sw.status)}>{sw.status}</span></td>
                            <td>
                              <button
                                type="button"
                                className="k-btn"
                                onClick={() => setLmSoftwareViewMembers(isExpanded ? null : { subId: sw.id, name: sw.name })}
                              >
                                {isExpanded ? "Hide" : "View Members"}
                              </button>
                            </td>
                          </tr>
                          {isExpanded && (
                            <tr>
                              <td colSpan={5} style={{ padding: "0", background: "rgba(49,195,234,0.05)", borderLeft: "3px solid var(--brand-cyan)" }}>
                                <div style={{ padding: "12px 20px" }}>
                                  <p style={{ fontSize: "0.75rem", color: "var(--brand-cyan)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", margin: "0 0 10px" }}>
                                    Team members using {sw.name}
                                  </p>
                                  {membersUsing.length === 0 ? (
                                    <p style={{ fontSize: "13px", color: "rgba(255,255,255,0.45)", margin: 0 }}>No members found.</p>
                                  ) : (
                                    <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                                      {membersUsing.map((m) => (
                                        <span key={String(m.id)} style={{
                                          fontSize: "0.8rem", padding: "4px 12px", borderRadius: "999px",
                                          background: "rgba(49,195,234,0.12)", color: "var(--brand-cyan)",
                                          border: "1px solid rgba(49,195,234,0.3)", fontWeight: 600,
                                        }}>
                                          {String(m.full_name || m.work_email)}
                                        </span>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </>
            )}
          </section>

          {/* ── 4. Recent Team Activity ── */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>Recent Team Activity</h2>
                <p>Latest workflow requests and status changes from your direct reports.</p>
              </div>
              <span className="emp-section-count">{recentActivity.length} recent</span>
            </div>
            {recentActivity.length === 0 ? (
              <div className="emp-empty">No recent activity from your team members.</div>
            ) : (
              <table className="emp-table">
                <thead>
                  <tr>
                    <th>Activity</th>
                    <th>Status</th>
                    <th>Date</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {recentActivity.map((req) => (
                    <tr key={String(req.id)}>
                      <td style={{ maxWidth: "420px" }}>
                        <span style={{ fontSize: "13px" }}>{activityDescription(req)}</span>
                      </td>
                      <td>
                        <span style={{
                          fontSize: "0.72rem", fontWeight: 700, padding: "3px 8px", borderRadius: "999px",
                          background: `${statusColour(req.status)}20`, color: statusColour(req.status),
                          border: `1px solid ${statusColour(req.status)}55`, whiteSpace: "nowrap",
                        }}>
                          {String(req.status || "").replace(/_/g, " ")}
                        </span>
                      </td>
                      <td style={{ whiteSpace: "nowrap", fontSize: "12px", color: "rgba(255,255,255,0.55)" }}>
                        {formatWorkflowDateOnly(req.created_at)}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="k-btn"
                          onClick={() => setWorkflowDetailsRecord(req)}
                        >
                          View
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          {/* ── 5. My Notifications ── */}
          <section className="emp-section">
            <div className="emp-section-header">
              <div>
                <h2>My Notifications</h2>
                <p>Recent workflow emails sent to your account.</p>
              </div>
              <span className="emp-section-count">{lmTeamEmailNotifications.length} notification{lmTeamEmailNotifications.length !== 1 ? "s" : ""}</span>
            </div>
            {lmTeamEmailNotifications.length === 0 ? (
              <div className="emp-empty">No notifications found for your account.</div>
            ) : (
              <table className="emp-table">
                <thead>
                  <tr>
                    <th>Subject</th>
                    <th>Event</th>
                    <th>Date</th>
                  </tr>
                </thead>
                <tbody>
                  {lmTeamEmailNotifications.map((notif) => {
                    const rawDt = notif.createdAt || notif.sentAt || notif.created_at;
                    const dt = rawDt ? new Date(String(rawDt)) : null;
                    const dateStr = dt && !isNaN(dt.getTime()) ? dt.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—";
                    return (
                      <tr key={String(notif.id)}>
                        <td><span style={{ fontSize: "13px" }}>{String(notif.subject || notif.eventType || "Notification")}</span></td>
                        <td><span style={{ fontSize: "0.72rem", color: "rgba(255,255,255,0.55)" }}>{String(notif.eventType || "—").replace(/_/g, " ")}</span></td>
                        <td style={{ whiteSpace: "nowrap", fontSize: "12px", color: "rgba(255,255,255,0.55)" }}>{dateStr}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>

        </div>

        {/* Modal must render inside this AppShell return so Review/View buttons work */}
        {renderWorkflowDetailsModal()}
        {renderPurchaseConfirmModal()}
        {renderOffboardingConfirmModal()}
        {renderFinanceConfirmModal()}
        {renderInfoReviewModal("line-manager-approve")}
      </AppShell>
    );
  }

  // ── Finance Manager Dashboard ──────────────────────────────────────────────
  if (isFinance && moduleKey === "dashboard") {
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const daysFromNow = (iso: string) => {
      if (!iso) return null;
      const d = new Date(iso + "T00:00:00");
      if (isNaN(d.getTime())) return null;
      return Math.round((d.getTime() - today.getTime()) / 86_400_000);
    };

    // Payments — scheduled but not yet paid
    const pendingPayments = scopedPayments.filter((p) =>
      ["pending", "planned"].includes(String(p.status || ""))
    );

    // Renewals in buckets
    const renewals30: AnyRecord[] = [];
    const renewals60: AnyRecord[] = [];
    const renewals90: AnyRecord[] = [];
    for (const s of scopedSubscriptions) {
      const d = daysFromNow(String(s.renewal_date || ""));
      if (d === null || d < 0) continue;
      if (d <= 30) renewals30.push(s);
      else if (d <= 60) renewals60.push(s);
      else if (d <= 90) renewals90.push(s);
    }
    const allUpcomingRenewals = [...renewals30, ...renewals60, ...renewals90];

    const deptOver = departmentStats.filter((d) => d.hasBudget && d.allocatedAmount > 0 && d.totalCost / d.allocatedAmount >= 0.9);

    const fmtMoney = (n: number) =>
      new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n);

    const vendorById = (id: string) =>
      allVendors.find((v) => String(v.id) === String(id));

    const subById = (id: string) =>
      scopedSubscriptions.find((s) => String(s.id) === String(id));

    const statusBadge = (status: string) => {
      const s = String(status || "").toLowerCase();
      const map: Record<string, [string, string]> = {
        pending: ["#f97316", "rgba(249,115,22,0.1)"],
        planned: ["#f97316", "rgba(249,115,22,0.1)"],
        paid: ["#22c55e", "rgba(34,197,94,0.1)"],
        active: ["#22c55e", "rgba(34,197,94,0.1)"],
        overdue: ["#ef4444", "rgba(239,68,68,0.1)"],
        cancelled: ["#6b7280", "rgba(107,114,128,0.1)"],
        pending_renewal: ["#f59e0b", "rgba(245,158,11,0.1)"],
      };
      const [color, bg] = map[s] ?? ["#94a3b8", "rgba(148,163,184,0.1)"];
      return (
        <span style={{
          fontSize: "11px", fontWeight: 700, padding: "2px 8px",
          borderRadius: "999px", background: bg, color, textTransform: "uppercase",
          letterSpacing: "0.05em", whiteSpace: "nowrap",
        }}>
          {status || "—"}
        </span>
      );
    };

    const sectionHead = (title: string, sub?: string) => (
      <div style={{ marginBottom: "16px" }}>
        <h2 style={{ margin: 0, fontSize: "15px", fontWeight: 700, color: "#fff", letterSpacing: "0.02em" }}>{title}</h2>
        {sub && <p style={{ margin: "4px 0 0", fontSize: "12px", color: "rgba(255,255,255,0.45)" }}>{sub}</p>}
      </div>
    );

    const card = (children: React.ReactNode, style?: React.CSSProperties) => (
      <div style={{
        background: "rgba(217,244,250,0.04)", border: "1px solid rgba(217,244,250,0.1)",
        borderRadius: "12px", padding: "20px", ...style,
      }}>
        {children}
      </div>
    );

    return (
      <AppShell title="Finance Dashboard" description="Procurement queue, budget oversight, renewals and payment tracking">
        <div style={{ display: "flex", flexDirection: "column", gap: "28px", padding: "8px 0 40px" }}>

          {/* ── 1. ACTION CENTER ── */}
          <section>
            {sectionHead("Action Center", "Items requiring your attention today")}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "14px" }}>
              {[
                {
                  label: "Pending Approvals",
                  value: financeWorkflowQueue.length,
                  color: financeWorkflowQueue.length > 0 ? "#f97316" : "#22c55e",
                  icon: "M9 12l2 2 4-4M7 4h10l3 4v12H4V4h3Z",
                  href: "/workflows",
                },
                {
                  label: "Renewals (Next 30 days)",
                  value: renewals30.length,
                  color: renewals30.length > 0 ? "#f59e0b" : "#22c55e",
                  icon: "M20 12a8 8 0 0 1-13.7 5.7M4 12a8 8 0 0 1 13.7-5.7M18 3v4h-4M6 21v-4h4",
                  href: "/renewals",
                },
                {
                  label: "Payments Awaiting Processing",
                  value: pendingPayments.length,
                  color: pendingPayments.length > 0 ? "#f97316" : "#22c55e",
                  icon: "M3 7h18v10H3V7ZM3 10h18M7 15h3",
                  href: "/payments",
                },
                {
                  label: "Depts Over 90% Budget",
                  value: deptOver.length,
                  color: deptOver.length > 0 ? "#ef4444" : "#22c55e",
                  icon: "M4 7h16M6 7v13h12V7M9 11h6M9 15h6M10 3h4v4",
                  href: "/budgets",
                },
              ].map((item) => (
                <a key={item.label} href={item.href} style={{ textDecoration: "none" }}>
                  <div style={{
                    background: "rgba(217,244,250,0.04)", border: `1px solid ${item.color}33`,
                    borderRadius: "12px", padding: "18px 20px", cursor: "pointer",
                    transition: "border-color 0.15s",
                    display: "flex", flexDirection: "column", gap: "10px",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <svg fill="none" height="20" viewBox="0 0 24 24" width="20">
                        <path d={item.icon} stroke={item.color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
                      </svg>
                      <span style={{
                        fontSize: "28px", fontWeight: 800, color: item.color, lineHeight: 1,
                      }}>
                        {item.value}
                      </span>
                    </div>
                    <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.55)", fontWeight: 500 }}>{item.label}</span>
                  </div>
                </a>
              ))}
            </div>
          </section>

          {/* ── 2. PROCUREMENT / APPROVAL QUEUE ── */}
          <section>
            {sectionHead("Procurement Approval Queue", "Workflow requests awaiting your financial review")}
            {card(
              financeWorkflowQueue.length === 0 ? (
                <p style={{ margin: 0, color: "rgba(255,255,255,0.35)", fontSize: "13px", textAlign: "center", padding: "24px 0" }}>
                  No requests pending finance approval.
                </p>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid rgba(217,244,250,0.1)" }}>
                        {["Requested By", "Type", "Department", "Submission Date", "Status", "Action"].map((h) => (
                          <th key={h} style={{
                            padding: "8px 12px", textAlign: "left", fontSize: "11px",
                            fontWeight: 600, color: "rgba(255,255,255,0.4)", textTransform: "uppercase",
                            letterSpacing: "0.07em", whiteSpace: "nowrap",
                          }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {financeWorkflowQueue.map((wf) => (
                        <tr key={String(wf.id)} style={{ borderBottom: "1px solid rgba(217,244,250,0.06)" }}>
                          <td style={{ padding: "12px 12px" }}>
                            <span style={{ color: "#fff", fontWeight: 600 }}>{String(wf.requested_by || wf.requested_by_email || "—")}</span>
                            {wf.requested_by_email ? (
                              <span style={{ display: "block", fontSize: "11px", color: "rgba(255,255,255,0.35)", marginTop: "2px" }}>{String(wf.requested_by_email)}</span>
                            ) : null}
                          </td>
                          <td style={{ padding: "12px 12px", color: "rgba(255,255,255,0.7)", whiteSpace: "nowrap" }}>
                            {String(wf.workflow_type || "—").replaceAll("_", " ")}
                          </td>
                          <td style={{ padding: "12px 12px", color: "rgba(255,255,255,0.7)" }}>
                            {String((wf.payload as AnyRecord)?.department || "—")}
                          </td>
                          <td style={{ padding: "12px 12px", color: "rgba(255,255,255,0.5)", whiteSpace: "nowrap", fontSize: "12px" }}>
                            {wf.created_at ? new Date(String(wf.created_at)).toLocaleDateString() : "—"}
                          </td>
                          <td style={{ padding: "12px 12px" }}>{statusBadge(String(wf.status || ""))}</td>
                          <td style={{ padding: "12px 12px" }}>
                            <button
                              className="k-btn k-btn--approve"
                              type="button"
                              onClick={() => setWorkflowDetailsRecord(wf)}
                            >
                              Review
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            )}
          </section>

          {/* ── Info Request Responses — Finance ── */}
          {(() => {
            const responded = financeWorkflowQueue.filter(
              (w) => (w.payload as Record<string, unknown>)?.info_response
            );
            if (responded.length === 0) return null;
            return (
              <section>
                {sectionHead("ℹ Responses Received", "Requesters have replied to your information request — review then approve or reject")}
                {card(
                  <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                    {responded.map((wf) => (
                      <div key={String(wf.id)} style={{
                        display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "10px",
                        padding: "10px 0", borderBottom: "1px solid rgba(217,244,250,0.06)",
                      }}>
                        <div>
                          <div style={{ fontWeight: 600, fontSize: "13px" }}>{getWorkflowSoftwareName(wf, allSubscriptions)}</div>
                          <div style={{ color: "rgba(255,255,255,0.45)", fontSize: "12px" }}>{String(wf.requested_by_email || "—")} · #{String(wf.id).slice(0, 8).toUpperCase()}</div>
                        </div>
                        <button type="button" className="k-btn k-btn--approve" style={{ fontSize: "12px" }} onClick={() => setInfoReviewModal(wf)}>
                          View Response &amp; Decide
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            );
          })()}

          {/* ── 3. DEPARTMENT BUDGET OVERVIEW ── */}
          <section>
            {sectionHead("Department Budget Overview", "Budget allocation vs. subscription spend per department")}
            <div className="dept-analytics-grid">
              {departmentStats.filter((d) => d.hasBudget || d.totalCost > 0).map((stat) => {
                const pct = stat.allocatedAmount > 0
                  ? Math.min(Math.round((stat.totalCost / stat.allocatedAmount) * 100), 100)
                  : 0;
                const fillColor = pct >= 90 ? "#ef4444" : pct >= 75 ? "#f59e0b" : "#22c55e";
                const isOver = stat.totalCost > stat.allocatedAmount && stat.allocatedAmount > 0;
                return (
                  <div key={stat.department} className="dept-analytics-card" style={{ borderColor: isOver ? "rgba(239,68,68,0.4)" : undefined }}>
                    <div className="dept-analytics-card__header">
                      <span className="dept-analytics-card__dept">{stat.department}</span>
                      <span style={{
                        fontSize: "11px", fontWeight: 700, padding: "2px 7px", borderRadius: "999px",
                        background: isOver ? "rgba(239,68,68,0.12)" : "rgba(217,244,250,0.08)",
                        color: isOver ? "#ef4444" : "rgba(255,255,255,0.4)",
                      }}>
                        {stat.softwareCount} tool{stat.softwareCount !== 1 ? "s" : ""}
                      </span>
                    </div>
                    {stat.hasBudget ? (
                      <>
                        <div className="dept-progress-bar">
                          <div
                            className={`dept-progress-fill dept-progress-fill--${pct >= 90 ? "danger" : pct >= 75 ? "warning" : "normal"}`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: "8px", background: "rgba(255,255,255,0.03)", padding: "10px", borderRadius: "8px" }}>
                          {[
                            { label: "Allocated", val: `${selectedOrgCurrency} ${fmtMoney(stat.allocatedAmount)}`, color: undefined },
                            { label: "Spend", val: `${selectedOrgCurrency} ${fmtMoney(stat.totalCost)}`, color: undefined },
                            { label: "Remaining", val: `${selectedOrgCurrency} ${fmtMoney(Math.max(stat.budgetLeft, 0))}`, color: isOver ? "#ef4444" : "#22c55e" },
                            { label: "Utilisation", val: `${pct}%`, color: fillColor },
                          ].map(({ label, val, color }) => (
                            <div key={label} style={{ display: "flex", flexDirection: "column", gap: "4px", textAlign: "center" }}>
                              <span style={{ fontSize: "10px", color: "rgba(255,255,255,0.5)", textTransform: "uppercase", fontWeight: 700, letterSpacing: "0.05em" }}>{label}</span>
                              <strong style={{ fontSize: "12px", color: color ?? "#fff", fontWeight: 800 }}>{val}</strong>
                            </div>
                          ))}
                        </div>
                        {stat.subscriptions.length > 0 && (
                          <div className="dept-app-list">
                            {stat.subscriptions.map((s) => (
                              <div key={String(s.id)} className="dept-app-item" style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                                <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.75)" }}>{String(s.name || "—")}</span>
                                <span style={{ fontSize: "12px", fontWeight: 700, color: "#fff", whiteSpace: "nowrap" }}>{String(s.currency_code || selectedOrgCurrency)} {fmtMoney(Number(s.amount || 0))}</span>
                              </div>
                            ))}
                          </div>
                        )}
                      </>
                    ) : (
                      <p style={{ margin: "8px 0 0", fontSize: "12px", color: "rgba(255,255,255,0.3)" }}>No approved budget set</p>
                    )}
                  </div>
                );
              })}
            </div>
          </section>

          {/* ── 4. UPCOMING RENEWALS ── */}
          <section>
            {sectionHead("Upcoming Renewals", "Subscriptions due for renewal — plan spend and decisions ahead of time")}
            {allUpcomingRenewals.length === 0 ? (
              card(<p style={{ margin: 0, color: "rgba(255,255,255,0.35)", fontSize: "13px", textAlign: "center", padding: "24px 0" }}>No renewals due in the next 90 days.</p>)
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                {([
                  { label: "Due within 30 days", items: renewals30, urgency: "#ef4444" },
                  { label: "31 – 60 days", items: renewals60, urgency: "#f59e0b" },
                  { label: "61 – 90 days", items: renewals90, urgency: "#22c55e" },
                ] as { label: string; items: AnyRecord[]; urgency: string }[]).filter((g) => g.items.length > 0).map((group) => (
                  <div key={group.label}>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
                      <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: group.urgency, flexShrink: 0 }} />
                      <span style={{ fontSize: "12px", fontWeight: 600, color: group.urgency }}>{group.label}</span>
                      <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.35)" }}>· {group.items.length} renewal{group.items.length !== 1 ? "s" : ""}</span>
                    </div>
                    {card(
                      <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
                          <thead>
                            <tr style={{ borderBottom: "1px solid rgba(217,244,250,0.1)" }}>
                              {["Subscription", "Vendor", "Renewal Date", "Amount", "Department", "Status"].map((h) => (
                                <th key={h} style={{
                                  padding: "6px 10px", textAlign: "left", fontSize: "11px",
                                  fontWeight: 600, color: "rgba(255,255,255,0.4)", textTransform: "uppercase",
                                  letterSpacing: "0.07em", whiteSpace: "nowrap",
                                }}>{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {group.items.map((s) => {
                              const vendor = vendorById(String(s.vendor_id || ""));
                              const d = daysFromNow(String(s.renewal_date || ""));
                              return (
                                <tr key={String(s.id)} style={{ borderBottom: "1px solid rgba(217,244,250,0.05)" }}>
                                  <td style={{ padding: "10px 10px", color: "#fff", fontWeight: 600 }}>{String(s.name || "—")}</td>
                                  <td style={{ padding: "10px 10px", color: "rgba(255,255,255,0.6)" }}>{vendor ? String(vendor.name || vendor.trading_name || "—") : "—"}</td>
                                  <td style={{ padding: "10px 10px", whiteSpace: "nowrap" }}>
                                    <span style={{ color: group.urgency, fontWeight: 600 }}>{String(s.renewal_date || "—")}</span>
                                    {d !== null ? <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.35)", display: "block" }}>{d === 0 ? "Today" : `in ${d}d`}</span> : null}
                                  </td>
                                  <td style={{ padding: "10px 10px", color: "#fff", fontWeight: 700, whiteSpace: "nowrap" }}>
                                    {s.amount ? `${String(s.currency_code || selectedOrgCurrency)} ${fmtMoney(Number(s.amount))}` : "—"}
                                  </td>
                                  <td style={{ padding: "10px 10px", color: "rgba(255,255,255,0.6)" }}>{String(s.department || "—")}</td>
                                  <td style={{ padding: "10px 10px" }}>{statusBadge(String(s.status || ""))}</td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* ── 5. PENDING PAYMENTS ── */}
          <section>
            {sectionHead("Pending Payments", "Payments scheduled but not yet confirmed as paid — click Mark Paid to record payment")}
            {card(
              pendingPayments.length === 0 ? (
                <p style={{ margin: 0, color: "rgba(255,255,255,0.35)", fontSize: "13px", textAlign: "center", padding: "24px 0" }}>No pending payments.</p>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid rgba(217,244,250,0.1)" }}>
                        {["Vendor", "Subscription", "Amount", "Due Date", "Status", "Action"].map((h) => (
                          <th key={h} style={{
                            padding: "8px 12px", textAlign: "left", fontSize: "11px",
                            fontWeight: 600, color: "rgba(255,255,255,0.4)", textTransform: "uppercase",
                            letterSpacing: "0.07em", whiteSpace: "nowrap",
                          }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {pendingPayments.map((p) => {
                        const vendor = vendorById(String(p.vendor_id || ""));
                        const sub = subById(String(p.subscription_id || ""));
                        const d = daysFromNow(String(p.due_date || ""));
                        const isOverdue = d !== null && d < 0;
                        return (
                          <tr key={String(p.id)} style={{
                            borderBottom: "1px solid rgba(217,244,250,0.06)",
                            background: isOverdue ? "rgba(239,68,68,0.04)" : undefined,
                          }}>
                            <td style={{ padding: "12px 12px", color: "#fff", fontWeight: 600 }}>
                              {vendor ? String(vendor.name || vendor.trading_name || "—") : String(p.name || "—")}
                            </td>
                            <td style={{ padding: "12px 12px", color: "rgba(255,255,255,0.6)" }}>
                              {sub ? String(sub.name || "—") : "—"}
                            </td>
                            <td style={{ padding: "12px 12px", fontWeight: 700, color: "#fff", whiteSpace: "nowrap" }}>
                              {p.amount ? `${String(p.currency_code || selectedOrgCurrency)} ${fmtMoney(Number(p.amount))}` : "—"}
                            </td>
                            <td style={{ padding: "12px 12px", whiteSpace: "nowrap" }}>
                              <span style={{ color: isOverdue ? "#ef4444" : "rgba(255,255,255,0.6)", fontWeight: isOverdue ? 700 : 400 }}>
                                {p.due_date ? String(p.due_date) : "—"}
                              </span>
                              {isOverdue && <span style={{ display: "block", fontSize: "11px", color: "#ef4444" }}>{Math.abs(d!)}d overdue</span>}
                            </td>
                            <td style={{ padding: "12px 12px" }}>{statusBadge(isOverdue ? "overdue" : String(p.status || ""))}</td>
                            <td style={{ padding: "12px 12px" }}>
                              <button
                                className="k-btn k-btn--approve"
                                type="button"
                                onClick={async () => {
                                  try {
                                    const today = new Date().toISOString().split("T")[0];
                                    await fetch(`${apiBaseUrl}/payments/${p.id}`, {
                                      method: "PATCH",
                                      headers: { "Content-Type": "application/json" },
                                      body: JSON.stringify({ status: "paid", payment_date: today }),
                                    });
                                    await loadAllContext();
                                    setToast(`Payment marked as paid.`);
                                    setToastType("success");
                                    setToastTitle("Payment confirmed");
                                  } catch {
                                    setToast("Failed to update payment.");
                                    setToastType("error");
                                    setToastTitle("Error");
                                  }
                                }}
                              >
                                Mark Paid
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )
            )}
          </section>

        </div>
        {renderWorkflowDetailsModal()}
        {renderPurchaseConfirmModal()}
        {renderOffboardingConfirmModal()}
        {renderFinanceConfirmModal()}
        {renderInfoReviewModal("approve")}
      </AppShell>
    );
  }
  // ── End Finance Manager Dashboard ──────────────────────────────────────────

  // ── IT Admin Dashboard ─────────────────────────────────────────────────────
  if (isItAdmin && moduleKey === "dashboard") {
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const daysFromNow = (iso: string) => {
      if (!iso) return null;
      const d = new Date(iso + "T00:00:00");
      if (isNaN(d.getTime())) return null;
      return Math.round((d.getTime() - today.getTime()) / 86_400_000);
    };

    const fmtMoney = (n: number) =>
      new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(n);

    // Procurement queue — requests IT admin needs to review or activate
    const itActionStatuses = ["submitted", "reopened", "finance_approved"];
    const procurementQueue = itAllWorkflows.filter((w) => itActionStatuses.includes(String(w.status || "")));

    // Tool requests awaiting triage
    const pendingToolRequests = itToolRequests.filter((t) =>
      ["new", "under_review"].includes(String(t.status || ""))
    );

    // Renewals buckets
    const renewals30: AnyRecord[] = [];
    const renewals60: AnyRecord[] = [];
    const renewals90: AnyRecord[] = [];
    for (const s of scopedSubscriptions) {
      const d = daysFromNow(String(s.renewal_date || ""));
      if (d === null || d < 0) continue;
      if (d <= 30) renewals30.push(s);
      else if (d <= 60) renewals60.push(s);
      else if (d <= 90) renewals90.push(s);
    }
    const allUpcomingRenewals = [...renewals30, ...renewals60, ...renewals90];

    // Licence health
    const totalLicences = scopedLicences.length;
    const assignedLicences = scopedLicences.filter((l) => l.assigned_to_person_id);
    const availableLicences = scopedLicences.filter((l) => !l.assigned_to_person_id);
    const expiringLicences = scopedLicences.filter((l) => {
      const d = daysFromNow(String(l.expires_at || ""));
      return d !== null && d >= 0 && d <= 30;
    });
    const idleLicences = scopedLicences.filter((l) => l.status === "available" && !l.assigned_to_person_id);

    // Workflow request status buckets for status summary
    const wfBuckets = {
      "New / Submitted": itAllWorkflows.filter((w) => ["submitted", "reopened"].includes(String(w.status || ""))).length,
      "Info Requested": itAllWorkflows.filter((w) => w.status === "info_requested").length,
      "Awaiting Finance": itAllWorkflows.filter((w) => ["line_manager_approved", "master_approved"].includes(String(w.status || ""))).length,
      "Finance Approved": itAllWorkflows.filter((w) => w.status === "finance_approved").length,
      "Completed": itAllWorkflows.filter((w) => w.status === "completed").length,
      "Rejected": itAllWorkflows.filter((w) => w.status === "rejected").length,
    };

    // Operational alerts
    const alerts: { msg: string; level: "critical" | "warning" | "info" }[] = [];
    expiringLicences.forEach((l) => {
      const d = daysFromNow(String(l.expires_at || ""))!;
      alerts.push({ msg: `${String(l.licence_name || "Licence")} expires in ${d} day${d !== 1 ? "s" : ""}`, level: d <= 7 ? "critical" : "warning" });
    });
    scopedContracts.forEach((c) => {
      const d = daysFromNow(String(c.end_date || ""));
      if (d === null) return;
      if (d < 0) alerts.push({ msg: `Contract with ${String(c.vendor_name || c.name || "vendor")} expired ${Math.abs(d)} day${Math.abs(d) !== 1 ? "s" : ""} ago`, level: "critical" });
      else if (d <= 30) alerts.push({ msg: `Contract with ${String(c.vendor_name || c.name || "vendor")} expires in ${d} day${d !== 1 ? "s" : ""}`, level: "warning" });
    });
    const staleRequests = itAllWorkflows.filter((w) => {
      if (!itActionStatuses.includes(String(w.status || ""))) return false;
      const d = daysFromNow(String(w.created_at || "").slice(0, 10));
      return d !== null && Math.abs(d) >= 5;
    });
    if (staleRequests.length > 0) {
      alerts.push({ msg: `${staleRequests.length} request${staleRequests.length !== 1 ? "s" : ""} waiting more than 5 days for IT action`, level: "warning" });
    }
    renewals30.forEach((s) => {
      const d = daysFromNow(String(s.renewal_date || ""))!;
      alerts.push({ msg: `${String(s.name || "Subscription")} renews in ${d} day${d !== 1 ? "s" : ""}`, level: d <= 7 ? "critical" : "warning" });
    });

    const vendorById = (id: string) => allVendors.find((v) => String(v.id) === String(id));

    const statusBadge = (status: string) => {
      const s = String(status || "").toLowerCase();
      const map: Record<string, [string, string]> = {
        submitted: ["#f97316", "rgba(249,115,22,0.1)"],
        reopened: ["#f97316", "rgba(249,115,22,0.1)"],
        info_requested: ["#3b82f6", "rgba(59,130,246,0.1)"],
        line_manager_approved: ["#8b5cf6", "rgba(139,92,246,0.1)"],
        master_approved: ["#8b5cf6", "rgba(139,92,246,0.1)"],
        finance_approved: ["#06b6d4", "rgba(6,182,212,0.1)"],
        completed: ["#22c55e", "rgba(34,197,94,0.1)"],
        rejected: ["#ef4444", "rgba(239,68,68,0.1)"],
        new: ["#f97316", "rgba(249,115,22,0.1)"],
        under_review: ["#3b82f6", "rgba(59,130,246,0.1)"],
        active: ["#22c55e", "rgba(34,197,94,0.1)"],
        pending_renewal: ["#f59e0b", "rgba(245,158,11,0.1)"],
        converted_to_workflow: ["#22c55e", "rgba(34,197,94,0.1)"],
      };
      const [color, bg] = map[s] ?? ["#94a3b8", "rgba(148,163,184,0.1)"];
      return (
        <span style={{
          fontSize: "11px", fontWeight: 700, padding: "2px 8px", borderRadius: "999px",
          background: bg, color, textTransform: "uppercase", letterSpacing: "0.05em", whiteSpace: "nowrap",
        }}>
          {String(status || "").replaceAll("_", " ")}
        </span>
      );
    };

    const sectionHead = (title: string, sub?: string) => (
      <div style={{ marginBottom: "16px" }}>
        <h2 style={{ margin: 0, fontSize: "15px", fontWeight: 700, color: "#fff", letterSpacing: "0.02em" }}>{title}</h2>
        {sub && <p style={{ margin: "4px 0 0", fontSize: "12px", color: "rgba(255,255,255,0.45)" }}>{sub}</p>}
      </div>
    );

    const card = (children: React.ReactNode, style?: React.CSSProperties) => (
      <div style={{
        background: "rgba(217,244,250,0.04)", border: "1px solid rgba(217,244,250,0.1)",
        borderRadius: "12px", padding: "20px", ...style,
      }}>
        {children}
      </div>
    );

    const emptyRow = (msg: string) => (
      <p style={{ margin: 0, color: "rgba(255,255,255,0.35)", fontSize: "13px", textAlign: "center", padding: "24px 0" }}>{msg}</p>
    );

    const thStyle: React.CSSProperties = {
      padding: "8px 12px", textAlign: "left", fontSize: "11px",
      fontWeight: 600, color: "rgba(255,255,255,0.4)", textTransform: "uppercase",
      letterSpacing: "0.07em", whiteSpace: "nowrap",
    };
    const tdStyle: React.CSSProperties = { padding: "11px 12px", borderBottom: "1px solid rgba(217,244,250,0.06)" };

    return (
      <AppShell title="IT Admin Dashboard" description="Procurement queue, licence health, renewals and operational alerts">
        <div style={{ display: "flex", flexDirection: "column", gap: "28px", padding: "8px 0 40px" }}>

          {/* ── 1. ACTION CENTER ── */}
          <section>
            {sectionHead("Action Center", "Work requiring your attention right now")}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))", gap: "14px" }}>
              {[
                { label: "Procurement Queue", value: procurementQueue.length, color: procurementQueue.length > 0 ? "#f97316" : "#22c55e", icon: "M9 12l2 2 4-4M7 4h10l3 4v12H4V4h3Z", href: "/workflows" },
                { label: "Tool Requests", value: pendingToolRequests.length, color: pendingToolRequests.length > 0 ? "#3b82f6" : "#22c55e", icon: "M4 6h16v12H4V6Zm0 0 8 7 8-7", href: "/tool-requests" },
                { label: "Renewals (30 days)", value: renewals30.length, color: renewals30.length > 0 ? "#f59e0b" : "#22c55e", icon: "M20 12a8 8 0 0 1-13.7 5.7M4 12a8 8 0 0 1 13.7-5.7M18 3v4h-4M6 21v-4h4", href: "/renewals" },
                { label: "Expiring Licences", value: expiringLicences.length, color: expiringLicences.length > 0 ? "#f59e0b" : "#22c55e", icon: "M12 3l7 4v5c0 5-3 8-7 9-4-1-7-4-7-9V7l7-4ZM9 12l2 2 4-4", href: "/licences" },
              ].map((item) => (
                <a key={item.label} href={item.href} style={{ textDecoration: "none" }}>
                  <div style={{
                    background: "rgba(217,244,250,0.04)", border: `1px solid ${item.color}33`,
                    borderRadius: "12px", padding: "18px 20px", display: "flex", flexDirection: "column", gap: "10px",
                  }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <svg fill="none" height="20" viewBox="0 0 24 24" width="20">
                        <path d={item.icon} stroke={item.color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
                      </svg>
                      <span style={{ fontSize: "28px", fontWeight: 800, color: item.color, lineHeight: 1 }}>{item.value}</span>
                    </div>
                    <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.55)", fontWeight: 500 }}>{item.label}</span>
                  </div>
                </a>
              ))}
            </div>
          </section>

          {/* ── 2. PROCUREMENT QUEUE ── */}
          <section>
            {sectionHead("Procurement Queue", "Workflow requests waiting for IT action or activation")}
            {card(
              procurementQueue.length === 0 ? emptyRow("No requests pending IT action.") : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid rgba(217,244,250,0.1)" }}>
                        {["Software", "Type", "Department", "Submitted", "Stage", "Action"].map((h) => (
                          <th key={h} style={thStyle}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {procurementQueue.map((wf) => {
                        const payload = (wf.payload || {}) as AnyRecord;
                        const name = String(payload.licence_name || payload.software_name || payload.name || wf.name || "—");
                        return (
                          <tr key={String(wf.id)}>
                            <td style={{ ...tdStyle, color: "#fff", fontWeight: 600 }}>
                              {name}
                              {wf.requested_by_email ? <span style={{ display: "block", fontSize: "11px", color: "rgba(255,255,255,0.35)", marginTop: "2px" }}>{String(wf.requested_by_email)}</span> : null}
                            </td>
                            <td style={{ ...tdStyle, color: "rgba(255,255,255,0.6)", whiteSpace: "nowrap" }}>{String(wf.workflow_type || "—").replaceAll("_", " ")}</td>
                            <td style={{ ...tdStyle, color: "rgba(255,255,255,0.6)" }}>{String(payload.department || "—")}</td>
                            <td style={{ ...tdStyle, color: "rgba(255,255,255,0.45)", fontSize: "12px", whiteSpace: "nowrap" }}>
                              {wf.created_at ? new Date(String(wf.created_at)).toLocaleDateString() : "—"}
                            </td>
                            <td style={tdStyle}>{statusBadge(String(wf.status || ""))}</td>
                            <td style={tdStyle}>
                              <button className="k-btn k-btn--approve" type="button" onClick={() => setWorkflowDetailsRecord(wf)}>
                                Review
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )
            )}
          </section>


          {/* ── Info Request Responses — IT Admin ── */}
          {(() => {
            const responded = procurementQueue.filter(
              (w) => (w.payload as Record<string, unknown>)?.info_response
            );
            if (responded.length === 0) return null;
            return (
              <section>
                {sectionHead("ℹ Responses Received", "Requesters have replied to your information request — review then approve or reject")}
                {card(
                  <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                    {responded.map((wf) => (
                      <div key={String(wf.id)} style={{
                        display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: "10px",
                        padding: "10px 0", borderBottom: "1px solid rgba(217,244,250,0.06)",
                      }}>
                        <div>
                          <div style={{ fontWeight: 600, fontSize: "13px" }}>{getWorkflowSoftwareName(wf, allSubscriptions)}</div>
                          <div style={{ color: "rgba(255,255,255,0.45)", fontSize: "12px" }}>{String(wf.requested_by_email || "—")} · #{String(wf.id).slice(0, 8).toUpperCase()}</div>
                        </div>
                        <button type="button" className="k-btn k-btn--approve" style={{ fontSize: "12px" }} onClick={() => setInfoReviewModal(wf)}>
                          View Response &amp; Decide
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            );
          })()}

          {/* ── 4. LICENCE HEALTH ── */}
          <section>
            {sectionHead("Licence Health", "Current state of your licence portfolio")}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: "14px" }}>
              {[
                { label: "Total Licences", value: totalLicences, color: "var(--brand-cyan)" },
                { label: "Assigned", value: assignedLicences.length, color: "#22c55e" },
                { label: "Available", value: availableLicences.length, color: "#3b82f6" },
                { label: "Expiring (30d)", value: expiringLicences.length, color: expiringLicences.length > 0 ? "#f59e0b" : "#22c55e" },
                { label: "Idle Seats", value: idleLicences.length, color: idleLicences.length > 0 ? "#f97316" : "#22c55e" },
              ].map((item) => (
                <div key={item.label} style={{
                  background: "rgba(217,244,250,0.04)", border: `1px solid ${item.color}33`,
                  borderRadius: "12px", padding: "18px 20px",
                  display: "flex", flexDirection: "column", gap: "8px",
                }}>
                  <span style={{ fontSize: "32px", fontWeight: 800, color: item.color, lineHeight: 1 }}>{item.value}</span>
                  <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.5)", fontWeight: 500 }}>{item.label}</span>
                </div>
              ))}
            </div>
          </section>

          {/* ── 7. UPCOMING RENEWALS ── */}
          <section>
            {sectionHead("Upcoming Renewals", "Subscriptions due for renewal — prepare or renegotiate before expiry")}
            {allUpcomingRenewals.length === 0 ? (
              card(emptyRow("No renewals due in the next 90 days."))
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                {([
                  { label: "Due within 30 days", items: renewals30, urgency: "#ef4444" },
                  { label: "31 – 60 days", items: renewals60, urgency: "#f59e0b" },
                  { label: "61 – 90 days", items: renewals90, urgency: "#22c55e" },
                ] as { label: string; items: AnyRecord[]; urgency: string }[]).filter((g) => g.items.length > 0).map((group) => (
                  <div key={group.label}>
                    <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
                      <div style={{ width: "8px", height: "8px", borderRadius: "50%", background: group.urgency, flexShrink: 0 }} />
                      <span style={{ fontSize: "12px", fontWeight: 600, color: group.urgency }}>{group.label}</span>
                      <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.35)" }}>· {group.items.length} renewal{group.items.length !== 1 ? "s" : ""}</span>
                    </div>
                    {card(
                      <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px" }}>
                          <thead>
                            <tr style={{ borderBottom: "1px solid rgba(217,244,250,0.1)" }}>
                              {["Subscription", "Vendor", "Renewal Date", "Amount", "Department", "Status"].map((h) => (
                                <th key={h} style={thStyle}>{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {group.items.map((s) => {
                              const vendor = vendorById(String(s.vendor_id || ""));
                              const d = daysFromNow(String(s.renewal_date || ""));
                              return (
                                <tr key={String(s.id)}>
                                  <td style={{ ...tdStyle, color: "#fff", fontWeight: 600 }}>{String(s.name || "—")}</td>
                                  <td style={{ ...tdStyle, color: "rgba(255,255,255,0.6)" }}>{vendor ? String(vendor.name || vendor.trading_name || "—") : "—"}</td>
                                  <td style={{ ...tdStyle, whiteSpace: "nowrap" }}>
                                    <span style={{ color: group.urgency, fontWeight: 600 }}>{String(s.renewal_date || "—")}</span>
                                    {d !== null ? <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.35)", display: "block" }}>{d === 0 ? "Today" : `in ${d}d`}</span> : null}
                                  </td>
                                  <td style={{ ...tdStyle, color: "#fff", fontWeight: 700, whiteSpace: "nowrap" }}>
                                    {s.amount ? `${String(s.currency_code || selectedOrgCurrency)} ${fmtMoney(Number(s.amount))}` : "—"}
                                  </td>
                                  <td style={{ ...tdStyle, color: "rgba(255,255,255,0.6)" }}>{String(s.department || "—")}</td>
                                  <td style={tdStyle}>{statusBadge(String(s.status || ""))}</td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>



        </div>
        {renderWorkflowDetailsModal()}
        {renderPurchaseConfirmModal()}
        {renderOffboardingConfirmModal()}
        {renderFinanceConfirmModal()}
        {renderInfoReviewModal("complete")}
      </AppShell>
    );
  }
  // ── End IT Admin Dashboard ─────────────────────────────────────────────────

  // ── HR Admin Dashboard ────────────────────────────────────────────────────
  if (moduleKey === "dashboard" && isHrAdmin && !isMasterAdmin) {
    const activeEmps = scopedEmployees.filter((e) => String(e.status || "") === "active");
    const unassigned = activeEmps.filter((e) => !e.line_manager_email);
    const lmUsers = allUsers.filter((u) => (u.roles as string[] | undefined)?.includes("line_manager"));

    const managerReports: Record<string, { name: string; count: number }> = {};
    for (const lm of lmUsers) {
      const email = String(lm.email || "");
      const name = String(lm.full_name || lm.name || email);
      const count = activeEmps.filter((e) => String(e.line_manager_email || "") === email).length;
      managerReports[email] = { name, count };
    }

    const deptMap: Record<string, number> = {};
    for (const e of activeEmps) {
      const d = String(e.department || "Unknown");
      deptMap[d] = (deptMap[d] ?? 0) + 1;
    }
    const deptEntries = Object.entries(deptMap).sort((a, b) => b[1] - a[1]);

    const hrSectionHead = (title: string, sub?: string) => (
      <div style={{ marginBottom: "16px" }}>
        <h2 style={{ margin: 0, fontSize: "15px", fontWeight: 700, color: "#fff", letterSpacing: "0.02em" }}>{title}</h2>
        {sub && <p style={{ margin: "4px 0 0", fontSize: "12px", color: "rgba(255,255,255,0.45)" }}>{sub}</p>}
      </div>
    );

    const hrCard = (children: React.ReactNode, style?: React.CSSProperties) => (
      <div style={{
        background: "rgba(217,244,250,0.04)", border: "1px solid rgba(217,244,250,0.1)",
        borderRadius: "12px", padding: "20px", ...style,
      }}>
        {children}
      </div>
    );

    return (
      <AppShell title="HR Dashboard" description="Employee setup status, manager assignments, and headcount by department">
        <div style={{ display: "flex", flexDirection: "column", gap: "28px", padding: "8px 0 40px" }}>

          {/* ── 1. HEADCOUNT OVERVIEW ── */}
          <section>
            {hrSectionHead("Headcount Overview", "Active employees across the organisation")}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "14px" }}>
              {[
                { label: "Active Employees", value: activeEmps.length, color: "#22c55e", icon: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" },
                { label: "Without Line Manager", value: unassigned.length, color: unassigned.length > 0 ? "#f97316" : "#22c55e", icon: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM15 8h6M18 5v6" },
                { label: "Line Managers", value: lmUsers.length, color: "#818cf8", icon: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" },
                { label: "Departments", value: deptEntries.length, color: "#38bdf8", icon: "M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM9 22V12h6v10" },
              ].map((item) => (
                <div key={item.label} style={{
                  background: "rgba(217,244,250,0.04)", border: `1px solid ${item.color}33`,
                  borderRadius: "12px", padding: "18px 20px",
                  display: "flex", flexDirection: "column", gap: "10px",
                }}>
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <svg fill="none" height="20" viewBox="0 0 24 24" width="20">
                      <path d={item.icon} stroke={item.color} strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" />
                    </svg>
                    <span style={{ fontSize: "28px", fontWeight: 800, color: item.color, lineHeight: 1 }}>{item.value}</span>
                  </div>
                  <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.55)", fontWeight: 500 }}>{item.label}</span>
                </div>
              ))}
            </div>
          </section>

          {/* ── 2. UNASSIGNED EMPLOYEES ── */}
          <section>
            {hrSectionHead(
              `Employees Without a Line Manager${unassigned.length > 0 ? ` (${unassigned.length})` : ""}`,
              "Assign a line manager so these employees can submit software requests and receive approvals"
            )}
            {hrCard(
              unassigned.length === 0 ? (
                <p style={{ margin: 0, color: "rgba(255,255,255,0.35)", fontSize: "13px", textAlign: "center", padding: "24px 0" }}>
                  All employees have a line manager assigned.
                </p>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                  {unassigned.map((emp) => (
                    <div key={String(emp.id)} style={{
                      display: "flex", alignItems: "center", justifyContent: "space-between",
                      padding: "10px 0", borderBottom: "1px solid rgba(255,255,255,0.06)",
                    }}>
                      <div>
                        <div style={{ fontWeight: 600, fontSize: "13px", color: "#fff" }}>{String(emp.full_name || emp.name || "—")}</div>
                        <div style={{ color: "rgba(255,255,255,0.45)", fontSize: "12px", marginTop: "2px" }}>
                          {String(emp.department || "—")} · {String(emp.job_title || "—")}
                        </div>
                      </div>
                      <select
                        style={{
                          background: "rgba(255,255,255,0.07)", border: "1px solid rgba(255,255,255,0.15)",
                          borderRadius: "8px", color: "#fff", padding: "6px 10px", fontSize: "12px", cursor: "pointer",
                          minWidth: "180px",
                        }}
                        defaultValue=""
                        onChange={async (ev) => {
                          const val = ev.target.value;
                          if (!val) return;
                          try {
                            await fetch(`${apiBaseUrl}/employees/${emp.id}`, {
                              method: "PATCH",
                              headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({ line_manager_email: val }),
                            });
                            await loadAllContext();
                            setToast("Line manager assigned.");
                            setToastType("success");
                            setToastTitle("Employee updated");
                          } catch {
                            setToast("Failed to assign line manager.");
                            setToastType("error");
                            setToastTitle("Error");
                          }
                        }}
                      >
                        <option value="" disabled>Assign line manager…</option>
                        {lmUsers.map((lm) => (
                          <option key={String(lm.id)} value={String(lm.email || "")}>
                            {String(lm.full_name || lm.name || lm.email)}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              )
            )}
          </section>

          {/* ── 3. MANAGER WORKLOAD + DEPT BREAKDOWN side by side ── */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "20px" }}>

            <section>
              {hrSectionHead("Manager Workload", "How many employees each line manager is responsible for")}
              {hrCard(
                lmUsers.length === 0 ? (
                  <p style={{ margin: 0, color: "rgba(255,255,255,0.35)", fontSize: "13px", textAlign: "center", padding: "16px 0" }}>No line managers in the system.</p>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                    {Object.entries(managerReports).map(([email, { name, count }]) => (
                      <div key={email} style={{
                        display: "flex", alignItems: "center", justifyContent: "space-between",
                        padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.06)",
                      }}>
                        <div>
                          <div style={{ fontWeight: 600, fontSize: "13px", color: "#fff" }}>{name}</div>
                          <div style={{ color: "rgba(255,255,255,0.4)", fontSize: "11px", marginTop: "2px" }}>{email}</div>
                        </div>
                        <span style={{
                          background: count === 0 ? "rgba(239,68,68,0.15)" : "rgba(99,102,241,0.15)",
                          color: count === 0 ? "#ef4444" : "#818cf8",
                          borderRadius: "20px", padding: "3px 12px", fontSize: "12px", fontWeight: 600, whiteSpace: "nowrap",
                        }}>
                          {count} {count === 1 ? "direct report" : "direct reports"}
                        </span>
                      </div>
                    ))}
                  </div>
                )
              )}
            </section>

            <section>
              {hrSectionHead("Employees by Department", "Headcount split across all departments")}
              {hrCard(
                <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                  {deptEntries.map(([dept, count]) => (
                    <div key={dept} style={{
                      display: "flex", alignItems: "center", justifyContent: "space-between",
                      padding: "8px 0", borderBottom: "1px solid rgba(255,255,255,0.06)",
                    }}>
                      <span style={{ fontSize: "13px", color: "#fff" }}>{dept}</span>
                      <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                        <div style={{ width: "80px", height: "5px", borderRadius: "3px", background: "rgba(255,255,255,0.1)", overflow: "hidden" }}>
                          <div style={{
                            width: `${Math.round((count / activeEmps.length) * 100)}%`,
                            height: "100%", background: "#38bdf8", borderRadius: "3px",
                          }} />
                        </div>
                        <span style={{ fontWeight: 700, fontSize: "13px", color: "#38bdf8", minWidth: "16px", textAlign: "right" }}>{count}</span>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

          </div>

        </div>
        {renderWorkflowDetailsModal()}
        {renderPurchaseConfirmModal()}
        {renderOffboardingConfirmModal()}
        {renderFinanceConfirmModal()}
      </AppShell>
    );
  }
  // ── End HR Admin Dashboard ──────────────────────────────────────────────────

  if (moduleKey === "dashboard") {
    const cards = [
      { label: "Active subscriptions", value: formatValue(summary?.active_subscriptions ?? 0) },
      { label: "Renewals due", value: formatValue(summary?.renewals_due ?? 0) },
      ...(showDashboardBudget
        ? [
            { label: "Allocated budget", value: `${formatValue(summary?.allocated_budget)} ${selectedOrgCurrency}` },
            { label: "Tracked spend", value: `${formatValue(summary?.tracked_spend)} ${selectedOrgCurrency}` },
          ]
        : []),
      { label: "Assigned licences", value: formatValue(summary?.assigned_licences ?? 0) },
    ];



    // Build upcoming renewals from live context data
    const upcomingRenewals = computeRenewals(
      scopedSubscriptions,
      scopedLicences,
      scopedVendors,
      scopedContracts,
    ).filter((r) => {
      const urg = urgencyFor(r.renewal_date);
      return urg !== null; // overdue, red (<30d), amber (<60d), green (≤90d)
    });

    const urgencyLabel: Record<string, string> = {
      overdue: "Overdue",
      red: "< 30 days",
      amber: "30–60 days",
      green: "60–90 days",
    };
    const urgencyOrder: Record<string, number> = { overdue: 0, red: 1, amber: 2, green: 3 };
    const typeIcon: Record<string, string> = {
      subscription: "🔁",
      licence: "🪪",
      vendor: "🏢",
      contract: "📄",
    };

    return (
      <AppShell
        title="Dashboard"
        description={
          showDashboardBudget
            ? "Combined analytics across organisations, subscriptions, budgets, payments, and licence allocations."
            : "Operational overview of subscriptions, renewals, and licence allocations for your role."
        }
      >
        {message ? <p className="module-message">{message}</p> : null}

        <section className="renewal-board" aria-label="Role dashboard" style={{ marginTop: "0" }}>
          {getRoleDashboardSections(userRoles).map((section) => (
            <article key={section.title} className="dashboard-card" style={{ marginBottom: "14px" }}>
              <span>{section.title}</span>
              <strong style={{ display: "block", fontSize: "16px", marginTop: "8px" }}>{section.description}</strong>
              <ul style={{ margin: "10px 0 0", paddingLeft: "18px", color: "rgba(255,255,255,0.72)", fontSize: "13px" }}>
                {section.items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </article>
          ))}
        </section>

        {/* ── Summary stat cards ── */}
        <section className="dashboard-grid" aria-label="Dashboard summary">
          {cards.map((card) => (
            <article className="dashboard-card" key={card.label}>
              <span>{card.label}</span>
              <strong>{loading ? "..." : card.value}</strong>
              <p>Current operational dataset</p>
            </article>
          ))}
        </section>

        {/* ── Departmental subscriptions (finance + master admin only) ── */}
        {showDepartmentalGovernance ? (
        <section className="renewal-board" aria-label="Department subscriptions overview" style={{ marginTop: "24px" }}>
          <header className="renewal-board-header">
            <div>
              <p className="eyebrow">Departmental Governance</p>
              <h2 className="renewal-board-title">
                Departmental Subscriptions, Spending & Budget Track
              </h2>
            </div>
          </header>

          <div className="dept-analytics-grid">
            {departmentStats.map((stat) => {
              const percent = stat.allocatedAmount > 0 
                ? Math.min(100, Math.round((stat.totalCost / stat.allocatedAmount) * 100))
                : 0;

              const progressClass = percent > 90 
                ? "dept-progress-fill--danger" 
                : percent > 75 
                ? "dept-progress-fill--warning" 
                : "dept-progress-fill--normal";

              return (
                <div key={stat.department} className="dept-analytics-card">
                  <div className="dept-card-header">
                    <h3>{stat.department}</h3>
                    <span className="kanban-column-count">{stat.softwareCount} App{stat.softwareCount !== 1 ? "s" : ""}</span>
                  </div>

                  <div className="dept-card-stats">
                    <div className="dept-stat-item">
                      <span className="dept-stat-label">Budget</span>
                      <span className="dept-stat-val" style={{ color: "#fff" }}>
                        {stat.hasBudget ? `${formatValue(stat.allocatedAmount)}` : "-"}
                      </span>
                    </div>
                    <div className="dept-stat-item">
                      <span className="dept-stat-label">Spent</span>
                      <span className="dept-stat-val" style={{ color: stat.totalCost > 0 ? "var(--brand-cyan)" : "#fff" }}>
                        {formatValue(stat.totalCost)}
                      </span>
                    </div>
                    <div className="dept-stat-item">
                      <span className="dept-stat-label">Remaining</span>
                      <span className="dept-stat-val" style={{ color: stat.budgetLeft < 0 ? "#fca5a5" : "#34d399" }}>
                        {stat.hasBudget ? `${formatValue(stat.budgetLeft)}` : "-"}
                      </span>
                    </div>
                  </div>

                  {stat.hasBudget && (
                    <div className="dept-progress-container">
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "10px", color: "var(--muted)", fontWeight: "bold" }}>
                        <span>Utilization</span>
                        <span>{percent}%</span>
                      </div>
                      <div className="dept-progress-bar">
                        <div className={`dept-progress-fill ${progressClass}`} style={{ width: `${percent}%` }} />
                      </div>
                    </div>
                  )}

                  <div style={{ marginTop: "6px" }}>
                    <span style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.6)", fontWeight: "bold", display: "block", marginBottom: "4px" }}>
                      Active Catalog
                    </span>
                    <div className="dept-app-list">
                      {stat.subscriptions.map((sub: any) => (
                        <div key={String(sub.id)} className="dept-app-item">
                          <span className="dept-app-name" title={String(sub.name)}>{String(sub.name)}</span>
                          {showFinancialSpend ? (
                            <span className="dept-app-cost">{formatValue(sub.amount)} {sub.currency_code}</span>
                          ) : null}
                        </div>
                      ))}
                      {stat.subscriptions.length === 0 && (
                        <div style={{ fontSize: "12px", fontWeight: 700, color: "var(--text)", padding: "10px 0", textAlign: "center" }}>
                          No active subscriptions in this department.
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
        ) : null}


        {/* ── Upcoming Renewals Board ── */}
        <section className="renewal-board" aria-label="Upcoming renewals">
          <header className="renewal-board-header">
            <div>
              <p className="eyebrow">Upcoming Renewals</p>
              <h2 className="renewal-board-title">Renewal Tracker</h2>
            </div>
            <span className="renewal-board-count">
              {upcomingRenewals.length} item{upcomingRenewals.length !== 1 ? "s" : ""} within 90 days
            </span>
          </header>

          {loading ? (
            <p className="renewal-board-empty">Loading renewal data…</p>
          ) : upcomingRenewals.length === 0 ? (
            <p className="renewal-board-empty">✅ No renewals due within the next 90 days.</p>
          ) : (() => {
            const ITEMS_PER_PAGE = 5;
            const sortedRenewals = [...upcomingRenewals].sort((a, b) => {
              const urgA = urgencyOrder[urgencyFor(a.renewal_date) ?? "green"];
              const urgB = urgencyOrder[urgencyFor(b.renewal_date) ?? "green"];
              if (urgA !== urgB) return urgA - urgB;
              return (daysUntil(a.renewal_date) ?? 999) - (daysUntil(b.renewal_date) ?? 999);
            });
            const totalPages = Math.ceil(sortedRenewals.length / ITEMS_PER_PAGE);
            const safePage = Math.min(renewalPage, totalPages - 1);
            const pageItems = sortedRenewals.slice(safePage * ITEMS_PER_PAGE, (safePage + 1) * ITEMS_PER_PAGE);

            return (
              <>
                <div className="renewal-lanes" style={{ display: "grid", gridTemplateColumns: "1fr", gap: "10px" }}>
                  {pageItems.map((r) => {
                    const days = daysUntil(r.renewal_date);
                    const urg = urgencyFor(r.renewal_date) ?? "green";
                    return (
                      <article key={r.id} className={`renewal-card renewal-card--${urg}`} style={{ width: "100%" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
                          <span className={`renewal-badge renewal-badge--${urg}`} style={{ flexShrink: 0 }}>{urgencyLabel[urg]}</span>
                          <span className="renewal-type-icon" style={{ flexShrink: 0 }}>{typeIcon[r.type] ?? "📋"}</span>
                          <strong className="renewal-card-name" style={{ flex: 1, margin: 0 }}>{r.name}</strong>
                          {r.vendor_name && r.vendor_name !== "-" && (
                            <span className="renewal-card-vendor" style={{ margin: 0, flexShrink: 0 }}>{r.vendor_name}</span>
                          )}
                          <span className="renewal-card-date" style={{ flexShrink: 0 }}>
                            📅 {r.renewal_date}
                          </span>
                          <span className={`renewal-days renewal-days--${urg}`} style={{ flexShrink: 0 }}>
                            {days === null
                              ? ""
                              : days < 0
                              ? `${Math.abs(days)}d overdue`
                              : days === 0
                              ? "Due today"
                              : `${days}d left`}
                          </span>
                          {showRenewalAmounts && r.amount != null && r.amount > 0 && (
                            <span className="renewal-card-amount" style={{ margin: 0, flexShrink: 0 }}>
                              {new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(r.amount)}{" "}
                              <span>{r.currency_code}</span>
                            </span>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
                {totalPages > 1 && (
                  <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: "16px", marginTop: "16px" }}>
                    <button
                      className="k-btn"
                      disabled={safePage === 0}
                      style={{ opacity: safePage === 0 ? 0.4 : 1, minWidth: "36px", padding: "6px 12px" }}
                      type="button"
                      onClick={() => setRenewalPage(Math.max(0, safePage - 1))}
                    >
                      ◀
                    </button>
                    <span style={{ fontSize: "0.8rem", color: "var(--muted)", fontWeight: 600 }}>
                      {safePage + 1} / {totalPages}
                    </span>
                    <button
                      className="k-btn"
                      disabled={safePage >= totalPages - 1}
                      style={{ opacity: safePage >= totalPages - 1 ? 0.4 : 1, minWidth: "36px", padding: "6px 12px" }}
                      type="button"
                      onClick={() => setRenewalPage(Math.min(totalPages - 1, safePage + 1))}
                    >
                      ▶
                    </button>
                  </div>
                )}
              </>
            );
          })()}
        </section>
      </AppShell>
    );
  }

  function closeRecordModal() {
    setIsModalOpen(false);
    setEditingRecord(null);
    setEditFormCurrency("");
    setWorkflowFormDraft({});
    setContractDocName("");
    setContractDocData("");
    setVendorDraft({});
  }

  function updateWorkflowDraft(fieldName: string, value: string) {
    setWorkflowFormDraft((previous) => ({ ...previous, [fieldName]: value }));
  }

  async function submitWorkflowRequest(
    payload: Record<string, string>,
    workflowType: string,
    emailOverrides: SubmissionEmailOverride[] = [],
    requestedModule: string = moduleKey
  ): Promise<SubmissionResult> {
    if (!config) {
      throw new Error("Module configuration is unavailable.");
    }
    const response = await fetch(`${apiBaseUrl}/workflow-requests`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requested_module: requestedModule,
        requested_action: "create",
        workflow_type: workflowType,
        payload,
        email_overrides: emailOverrides,
        notes:
          typeof payload.justification_notes === "string" && payload.justification_notes.trim()
            ? payload.justification_notes.trim()
            : typeof payload.notes === "string" && payload.notes.trim()
              ? payload.notes.trim()
              : undefined,
        actor_user_id: user?.id ?? null,
        actor_email: user?.email ?? null,
        actor_roles: userRoles,
      }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const responseData = await response.json();
    const routing = getApprovalRoutingPreview(
      workflowType as WorkflowType,
      payload.requester_name,
      payload.requester_email,
      roleMailboxEmails
    );
    setMessage("Workflow request submitted.");
    triggerToast("REQUEST SUBMITTED", `${config.title} request submitted successfully.`, "success");
    if (responseData?.warning) {
      triggerToast("WARNING", responseData.warning, "warning");
    }
    await loadRecords();
    await loadAllContext();
    return {
      workflowId: String(responseData.id),
      currentStage: routing.currentStage,
      approverEmail: routing.nextApproverEmail,
    };
  }

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!config) return;

    const form = event.currentTarget;
    const payload = preparePayload(moduleKey, normaliseFormPayload(form, config.createFields));
    if (moduleKey === "contracts") {
      payload.document_name = contractDocName || editingRecord?.document_name || "";
      payload.document_data = contractDocData || editingRecord?.document_data || "";
    }
    try {
      const endpoint = usesWorkflow ? "/workflow-requests" : config.endpoint;
      const body = usesWorkflow
        ? {
            requested_module: moduleKey,
            requested_action: "create",
            workflow_type: resolveWorkflowType(moduleKey, payload),
            payload,
            notes: typeof payload.notes === "string" && payload.notes.trim() ? payload.notes.trim() : undefined,
            actor_user_id: user?.id ?? null,
            actor_email: user?.email ?? null,
            actor_roles: userRoles,
          }
        : {
            ...payload,
            actor_user_id: user?.id ?? null,
            actor_email: user?.email ?? null,
            actor_roles: userRoles,
          };
      const response = await fetch(`${apiBaseUrl}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const responseData = await response.json();
      form.reset();
      closeRecordModal();
      setMessage(usesWorkflow ? "Workflow request submitted." : "Record created successfully.");
      if (moduleKey === "users" && responseData.temporary_password) {
        setToast(`Temporary password for ${responseData.work_email}: ${responseData.temporary_password}. It expires in 24 hours.`);
      } else {
        setToast(usesWorkflow ? `${config.title} workflow submitted.` : `${config.title} record saved.`);
      }
      if (responseData && responseData.warning) {
        triggerToast("WARNING", responseData.warning, "warning");
      }
      await loadRecords();
      if (moduleKey === "organisations") {
        window.dispatchEvent(new Event("slmct_orgs_updated"));
      }
      await loadAllContext();
    } catch (error) {
      setMessage(`Create failed. ${error instanceof Error ? error.message : "Check required fields and duplicate constraints."}`);
    }
  }

  async function handleEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!config || !editingRecord?.id) return;

    const form = event.currentTarget;
    const payload = preparePayload(moduleKey, normaliseFormPayload(form, config.createFields));
    if (moduleKey === "contracts") {
      payload.document_name = contractDocName || editingRecord?.document_name || "";
      payload.document_data = contractDocData || editingRecord?.document_data || "";
    }
    try {
      const endpoint = usesWorkflow ? "/workflow-requests" : `${config.endpoint}/${editingRecord.id}`;
      const body = usesWorkflow
        ? {
            requested_module: moduleKey,
            requested_action: "update",
            payload: { ...payload, id: editingRecord.id },
            actor_user_id: user?.id ?? null,
            actor_email: user?.email ?? null,
          }
        : {
            ...payload,
            actor_user_id: user?.id ?? null,
            actor_email: user?.email ?? null,
            actor_roles: userRoles,
          };
      const response = await fetch(`${apiBaseUrl}${endpoint}`, {
        method: usesWorkflow ? "POST" : "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const responseData = await response.json();
      form.reset();
      closeRecordModal();
      setMessage(usesWorkflow ? "Update workflow request submitted." : "Record updated successfully.");
      setToast(usesWorkflow ? `${config.title} update workflow submitted.` : `${config.title} record updated.`);
      if (responseData && responseData.warning) {
        triggerToast("WARNING", responseData.warning, "warning");
      }
      await loadRecords();
    } catch (error) {
      setMessage(`Update failed. ${error instanceof Error ? error.message : "Check required fields and duplicate constraints."}`);
    }
  }

  async function handleDelete(recordId: unknown) {
    if (!config || !recordId) return;

    const confirmMessage =
      moduleKey === "users"
        ? "Remove this application user? They will lose login access until re-provisioned."
        : undefined;
    if (confirmMessage && !window.confirm(confirmMessage)) return;

    try {
      const response =
        moduleKey === "users"
          ? await fetch(`${apiBaseUrl}/users/${recordId}/deactivate`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                actor_user_id: user?.id ?? null,
                actor_email: user?.email ?? null,
                actor_roles: userRoles,
              }),
            })
          : usesWorkflow
            ? await fetch(`${apiBaseUrl}/workflow-requests`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  requested_module: moduleKey,
                  requested_action: "archive",
                  payload: { id: recordId },
                  actor_user_id: user?.id ?? null,
                  actor_email: user?.email ?? null,
                }),
              })
            : await fetch(`${apiBaseUrl}${config.endpoint}/${recordId}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));
      let responseData: any = null;
      try {
        responseData = await response.json();
      } catch (e) {}
      setMessage(usesWorkflow ? "Archive workflow request submitted." : moduleKey === "users" ? "User removed successfully." : "Record archived successfully.");
      setToast(usesWorkflow ? `${config.title} archive workflow submitted.` : moduleKey === "users" ? "Application user removed." : `${config.title} record archived.`);
      if (responseData && responseData.warning) {
        triggerToast("WARNING", responseData.warning, "warning");
      }
      await loadRecords();
      if (moduleKey === "organisations") {
        if (String(recordId) === sessionStorage.getItem("slmct_selected_org_id")) {
          sessionStorage.removeItem("slmct_selected_org_id");
          sessionStorage.removeItem("slmct_selected_org_currency");
        }
        window.dispatchEvent(new Event("slmct_orgs_updated"));
      }
      await loadAllContext();
    } catch (error) {
      setMessage(
        moduleKey === "users"
          ? `Remove failed. ${error instanceof Error ? error.message : "The user deactivate API may need a container rebuild (docker compose up -d --build)."}`
          : `Archive failed. ${error instanceof Error ? error.message : "Check whether the record exists or is linked to protected data."}`
      );
    }
  }

  async function handleCleanupLegacyDemoUsers() {
    if (moduleKey !== "users" || legacyDemoUsers.length === 0) return;
    const ok = window.confirm(
      `Remove ${legacyDemoUsers.length} legacy demo account(s) (@derisk360.local and @demo.derisk360.com)? Your Outlook accounts will be kept.`
    );
    if (!ok) return;

    try {
      const response = await fetch(`${apiBaseUrl}/users/cleanup-legacy-demo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: userRoles,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json();
      const count = Number(data.removed_count ?? legacyDemoUsers.length);
      setMessage(`Removed ${count} legacy demo account(s).`);
      triggerToast("LEGACY USERS REMOVED", `Removed ${count} @derisk360.local / @demo.derisk360.com account(s).`, "success");
      await loadRecords();
      await loadAllContext();
    } catch (error) {
      setMessage(
        `Cleanup failed. ${error instanceof Error ? error.message : "Rebuild the API container if this endpoint is missing."}`
      );
      triggerToast("CLEANUP FAILED", error instanceof Error ? error.message : "Could not remove legacy demo users.", "danger");
    }
  }

  async function handleRemoveWorkflow(recordId: unknown) {
    if (!recordId) return;
    const ok = window.confirm("Remove this workflow from the board? It will move to the Recycle Bin.");
    if (!ok) return;

    try {
      const response = await fetch(`${apiBaseUrl}/workflow-requests/${recordId}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: user?.roles ?? [],
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setToast("Workflow removed from the board.");
      await loadRecords();
    } catch (error) {
      setMessage(`Workflow removal failed. ${error instanceof Error ? error.message : "Unable to remove workflow."}`);
    }
  }

  async function handleRestore(targetModule: string, recordId: unknown) {
    if (!recordId || !targetModule) return;
    try {
      const response = await fetch(`${apiBaseUrl}/recycle-bin/${targetModule}/${recordId}/restore`, {
        method: "POST"
      });
      if (!response.ok) throw new Error(await responseError(response));
      setMessage("Record successfully restored.");
      setToast("Record restored.");
      await loadRecords();
      if (targetModule === "organisations") {
        window.dispatchEvent(new Event("slmct_orgs_updated"));
      }
      await loadAllContext();
    } catch (error) {
      setMessage(`Restore failed. ${error instanceof Error ? error.message : "Error restoring the record."}`);
    }
  }

  async function handleDeleteForever(targetModule: string, recordId: unknown) {
    if (!recordId || !targetModule) return;
    if (!window.confirm("Are you sure you want to delete this record permanently? This action cannot be undone.")) return;
    try {
      const response = await fetch(`${apiBaseUrl}/recycle-bin/${targetModule}/${recordId}`, {
        method: "DELETE"
      });
      if (!response.ok) throw new Error(await responseError(response));
      setMessage("Record permanently deleted.");
      setToast("Record permanently deleted.");
      await loadRecords();
    } catch (error) {
      setMessage(`Permanent deletion failed. ${error instanceof Error ? error.message : "Error deleting the record."}`);
    }
  }

  async function handleResetPassword(recordId: unknown) {
    if (!recordId) return;

    try {
      const response = await fetch(`${apiBaseUrl}/users/${recordId}/reset-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: user?.roles ?? [],
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json();
      setToast(`Temporary password for ${data.email}: ${data.temporary_password}. It expires in 24 hours.`);
      setMessage("Temporary password reset completed.");
      await loadRecords();
    } catch (error) {
      setMessage(`Password reset failed. ${error instanceof Error ? error.message : "Check role permissions."}`);
    }
  }

  function handleDownloadTemplate() {
    downloadFromUrl(`${apiBaseUrl}/bulk-templates/${moduleKey}.xlsx`, `${moduleKey}-bulk-template.xlsx`);
  }

  function handleExportXlsx() {
    downloadFromUrl(`${apiBaseUrl}/exports/${moduleKey}.xlsx`, `${moduleKey}.xlsx`);
  }

  async function handleBulkUpload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;

    try {
      const contentBase64 = await fileToBase64(file);
      const response = await fetch(`${apiBaseUrl}/bulk-uploads/${moduleKey}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: file.name,
          content_base64: contentBase64,
          // Rows always land in the organisation currently selected on this page —
          // any organisation_id column in the uploaded file is ignored server-side.
          organisation_id: selectedOrgId,
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: user?.roles ?? [],
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json();
      setMessage(data.message ?? (data.mode === "direct" ? `Upload completed for ${data.count} rows.` : `Upload workflow submitted for ${data.count} rows.`));
      setToast(data.message ?? (data.mode === "direct" ? `${config.title} upload completed.` : `${config.title} upload submitted to workflow.`));
      await loadRecords();
    } catch (error) {
      setMessage(`Bulk upload failed. ${error instanceof Error ? error.message : "Check the XLSX template and required fields."}`);
    }
  }

  async function handleWorkflowAction(
    recordId: unknown,
    action: "approve" | "line-manager-approve" | "complete" | "reject" | "reopen" | "validate-budget" | "request-info"
  ) {
    if (!recordId) return;

    if (action === "validate-budget") {
      const record = scopedRecords.find((r) => r.id === recordId);
      if (record) {
        if (String(record.workflow_type) === "employee_offboarding") {
          setOffboardingConfirmRecord(record);
          return;
        }
        setFinanceConfirmRecord(record);
        return;
      }
    }

    if (action === "complete") {
      const record = scopedRecords.find((r) => r.id === recordId);
      if (record) {
        openProcurementModal(record);
        return;
      }
    }

    if (action === "reject") {
      const record = scopedRecords.find((r) => r.id === recordId);
      if (record) {
        const currentStatus = String(record.status || "");
        const workflowType = String(record.workflow_type || "");
        const financeAllowed =
          isFinance && ["line_manager_approved", "master_approved", "finance_approved"].includes(currentStatus);
        const lineManagerAllowed =
          canLineManagerApproveWorkflow &&
          requiresLineManager(workflowType) &&
          ["submitted", "reopened"].includes(currentStatus);
        const isMasterAllowed = canApproveWorkflow;
        if (!financeAllowed && !lineManagerAllowed && !isMasterAllowed) {
          triggerToast(
            "UNAUTHORIZED",
            "You do not have permission to perform this action.",
            "danger"
          );
          return;
        }
      }
    }

    let rejectionReason = "";
    let infoRequestMessage = "";
    if (action === "reject") {
      const reason = window.prompt("Enter rejection reason:");
      if (reason === null) return;
      rejectionReason = reason;
    }
    if (action === "request-info") {
      const message = window.prompt(
        "What additional information do you need from the requester?",
        "Please provide additional details."
      );
      if (message === null) return;
      infoRequestMessage = message;
    }

    try {
      const endpointAction =
        action === "line-manager-approve"
          ? "line-manager-approve"
          : action === "request-info"
            ? "request-info"
            : action;
      const response = await fetch(`${apiBaseUrl}/workflow-requests/${recordId}/${endpointAction}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: userRoles,
          rejection_reason: rejectionReason || null,
          info_request_message: infoRequestMessage || null,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const responseData = await response.json();
      
      if (action === "approve") {
        setMessage("");
        triggerToast("APPROVED", "The workflow request has been approved.", "success");
      } else if (action === "line-manager-approve") {
        setMessage("");
        triggerToast("LINE MANAGER APPROVED", "The request has been sent to Finance for budget validation.", "success");
      } else if (action === "reject") {
        setMessage("");
        triggerToast("REJECTED", "The workflow request has been rejected.", "danger");
      } else if (action === "request-info") {
        setMessage("");
        triggerToast("MORE INFORMATION REQUESTED", "The requester has been notified to provide additional details.", "info");
      } else if (action === "reopen") {
        setMessage("");
        triggerToast("WORKFLOW REOPENED", "The workflow request has been reopened.", "info");
      } else if (action === "complete") {
        setMessage("");
        const record = scopedRecords.find((r) => r.id === recordId);
        const payload = (record?.payload || {}) as Record<string, any>;
        const itemName = payload.name || payload.title || payload.full_name || record?.activated_entity_id || "(No Title)";
        const entityType = String(record?.requested_module || "record").replace(/s$/, "").toUpperCase();
        triggerToast(
          "WORKFLOW COMPLETED",
          `PURCHASE CONFIRMED: ${entityType} '${itemName}' has been activated and is live.`,
          "success"
        );
      } else {
        setMessage(`Workflow ${action} action completed.`);
      }
      
      if (responseData && responseData.warning) {
        triggerToast("WARNING", responseData.warning, "warning");
      }
      patchWorkflowRecord(recordId, {
        status: String(responseData?.status || workflowStatusFromAction(action)),
      });
      await refreshWorkflowBoard();
      await refreshDashboardData();
    } catch (error) {
      setMessage(`Workflow action failed. ${error instanceof Error ? error.message : "Check the current status and role permissions."}`);
    }
  }

  function canRunWorkflowAction(
    record: AnyRecord,
    action: "approve" | "line-manager-approve" | "complete" | "reject" | "reopen" | "validate-budget" | "request-info"
  ) {
    const currentStatus = String(record.status ?? "");
    const workflowType = String(record.workflow_type || "");
    if (action === "line-manager-approve") {
      return (
        canLineManagerApproveWorkflow &&
        requiresLineManager(workflowType) &&
        ["submitted", "reopened"].includes(currentStatus)
      );
    }
    if (action === "approve") {
      return canApproveWorkflow && ["submitted", "reopened"].includes(currentStatus) && !requiresLineManager(workflowType);
    }
    if (action === "reject") {
      if (
        canLineManagerApproveWorkflow &&
        requiresLineManager(workflowType) &&
        ["submitted", "reopened"].includes(currentStatus)
      ) {
        return true;
      }
      if (isFinance && ["line_manager_approved", "master_approved", "finance_approved"].includes(currentStatus)) return true;
      if (canApproveWorkflow && ["submitted", "reopened", "master_approved", "finance_approved", "line_manager_approved"].includes(currentStatus)) return true;
      return false;
    }
    if (action === "request-info") {
      if (
        canLineManagerApproveWorkflow &&
        requiresLineManager(workflowType) &&
        ["submitted", "reopened"].includes(currentStatus)
      ) {
        return true;
      }
      if (isFinance && ["line_manager_approved", "master_approved", "submitted", "reopened"].includes(currentStatus)) return true;
      if ((isItAdmin || isMasterAdmin) && currentStatus === "finance_approved") return true;
      return canApproveWorkflow && ["submitted", "reopened", "line_manager_approved", "master_approved", "finance_approved"].includes(currentStatus);
    }
    if (action === "validate-budget") {
      if (workflowType === "employee_offboarding") {
        return (isItAdmin || isMasterAdmin) && ["submitted", "reopened"].includes(currentStatus);
      }
      if (!canValidateBudget) return false;
      if (currentStatus === "master_approved" || currentStatus === "line_manager_approved") return true;
      return !requiresLineManager(workflowType) && ["submitted", "reopened"].includes(currentStatus);
    }
    if (action === "complete") {
      if (workflowType === "employee_offboarding") return (isItAdmin || isMasterAdmin) && currentStatus === "it_confirmed";
      return canCompleteWorkflow && currentStatus === "finance_approved";
    }
    if (action === "reopen") return canReopenWorkflow && currentStatus === "rejected";
    return false;
  }

  function closeProcurementModal() {
    setPurchaseConfirmRecord(null);
    setProcurementStep("redirect");
    setProcurementVendorUrl(null);
    setProcurementUrlLoading(false);
  }

  function openProcurementModal(record: AnyRecord) {
    setActivationForm(buildDefaultActivationForm(record, allUsers));
    setProcurementStep("redirect");
    setProcurementVendorUrl(null);
    setPurchaseConfirmRecord(record);

    const payload = (record.payload || {}) as Record<string, unknown>;
    const vendorId = String(payload.vendor_id || "");
    const vendor = allVendors.find((v) => String(v.id) === vendorId);
    const vendorName = vendor?.name || String(payload.vendor_name || "");
    const subscriptionName = String(payload.name || payload.licence_name || "");
    const subscriptionId = String(payload.subscription_id || "");
    if (!vendorName && !vendorId && !subscriptionId) return;

    setProcurementUrlLoading(true);
    fetch(`${apiBaseUrl}/vendor-purchase-url`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vendor_name: vendorName, vendor_id: vendorId || undefined, subscription_name: subscriptionName, subscription_id: subscriptionId || undefined }),
    })
      .then((r) => r.json())
      .then((data) => { if (data.purchase_url) setProcurementVendorUrl({ url: data.purchase_url, label: data.label }); })
      .catch(() => {})
      .finally(() => setProcurementUrlLoading(false));
  }

  async function submitPurchaseConfirmation(recordId: unknown, form: ActivationFormState) {
    const validationError = validateActivationForm(form);
    if (validationError) {
      setMessage(validationError);
      return;
    }

    try {
      const response = await fetch(`${apiBaseUrl}/workflow-requests/${recordId}/complete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: user?.roles ?? [],
          ...buildActivationPayload(form),
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const responseData = await response.json();

      const record = scopedRecords.find((r) => r.id === recordId);
      const payload = (record?.payload || {}) as Record<string, unknown>;
      const itemName = getWorkflowSoftwareName(record || {}, allSubscriptions);
      const entityType = String(record?.requested_module || "record").replace(/s$/, "").toUpperCase();

      setPurchaseConfirmRecord(null);
      setMessage("");
      triggerToast(
        "WORKFLOW COMPLETED",
        `PURCHASE CONFIRMED: ${entityType} '${itemName}' has been activated with ${getActivationMethodLabel(form.activation_method)}.`,
        "success"
      );
      if (responseData && responseData.warning) {
        triggerToast("WARNING", responseData.warning, "warning");
      }
      patchWorkflowRecord(recordId, {
        status: String(responseData?.status || "completed"),
      });
      await refreshWorkflowBoard();
      await refreshDashboardData();
    } catch (error) {
      setMessage(`Purchase confirmation failed. ${error instanceof Error ? error.message : "Check activation details and try again."}`);
    }
  }

  async function handleToolRequestAction(
    recordId: unknown,
    action: "create-workflow" | "reject" | "request-info"
  ) {
    if (!recordId) return;

    let rejectionReason = "";
    let infoMessage = "";
    if (action === "reject") {
      const reason = window.prompt("Enter rejection reason for the employee:");
      if (reason === null) return;
      rejectionReason = reason;
    }
    if (action === "request-info") {
      const message = window.prompt(
        "What additional information do you need from the requester?",
        "Please provide additional business justification and estimated cost."
      );
      if (message === null) return;
      infoMessage = message;
    }

    try {
      const response = await fetch(`${apiBaseUrl}/tool-requests/${recordId}/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: user?.roles ?? [],
          rejection_reason: rejectionReason || null,
          info_request_message: infoMessage || null,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));

      if (action === "create-workflow") {
        triggerToast(
          "WORKFLOW CREATED",
          "The tool request has been converted into a procurement workflow.",
          "success"
        );
      } else if (action === "reject") {
        triggerToast("REQUEST REJECTED", "The employee tool request has been rejected.", "danger");
      } else {
        triggerToast("INFO REQUESTED", "The requester has been notified to provide more information.", "info");
      }

      setToolRequestModalRecord(null);
      await loadRecords();
    } catch (error) {
      setMessage(`Tool request action failed. ${error instanceof Error ? error.message : "Check permissions and request status."}`);
    }
  }

  async function handleColumnDrop(e: React.DragEvent, targetStatus: string) {
    e.preventDefault();
    setActiveDragColumn(null);
    const recordId = e.dataTransfer.getData("text/plain");
    if (!recordId) return;

    const record = scopedRecords.find((r) => String(r.id) === recordId);
    if (!record) return;

    const currentStatus = String(record.status || "");
    if (currentStatus === targetStatus) return;

    let action: "approve" | "line-manager-approve" | "complete" | "reject" | "reopen" | "validate-budget" | null = null;

    if (targetStatus === "line_manager_approved") {
      if (["submitted", "reopened"].includes(currentStatus)) {
        action = "line-manager-approve";
      }
    } else if (targetStatus === "master_approved") {
      if (["submitted", "reopened"].includes(currentStatus)) {
        action = "approve";
      }
    } else if (targetStatus === "finance_approved") {
      if (currentStatus === "master_approved" || currentStatus === "line_manager_approved") {
        action = "validate-budget";
      } else if (["submitted", "reopened"].includes(currentStatus)) {
        action = "validate-budget";
      }
    } else if (targetStatus === "completed") {
      if (currentStatus === "finance_approved") {
        action = "complete";
      }
    } else if (targetStatus === "rejected") {
      if (["submitted", "reopened", "master_approved", "finance_approved"].includes(currentStatus)) {
        action = "reject";
      }
    } else if (targetStatus === "submitted" || targetStatus === "reopened") {
      if (currentStatus === "rejected") {
        action = "reopen";
      }
    }

    if (!action) {
      setToast("Invalid status transition.");
      return;
    }

    if (action === "approve" && !canApproveWorkflow) {
      setToast("Unauthorized: Only Master Admins can approve workflow requests.");
      return;
    }
    if (action === "reject") {
      const financeAllowed =
        isFinance && ["line_manager_approved", "master_approved", "finance_approved"].includes(currentStatus);
      const lineManagerAllowed =
        canLineManagerApproveWorkflow &&
        requiresLineManager(String(record.workflow_type || "")) &&
        ["submitted", "reopened"].includes(currentStatus);
      const isMasterAllowed = canApproveWorkflow;
      if (!financeAllowed && !lineManagerAllowed && !isMasterAllowed) {
        setToast("You do not have permission to perform this action.");
        return;
      }
    }
    if (action === "line-manager-approve" && !canLineManagerApproveWorkflow) {
      setToast("You do not have permission to perform this action.");
      return;
    }
    if (action === "validate-budget" && !canValidateBudget) {
      setToast("You do not have permission to perform this action.");
      return;
    }
    if (action === "complete" && !canCompleteWorkflow) {
      setToast("You do not have permission to perform this action.");
      return;
    }
    if (action === "reopen" && !canReopenWorkflow) {
      setToast("Unauthorized: Only IT/Master Admins can reopen requests.");
      return;
    }

    if (action === "validate-budget") {
      if (String(record.workflow_type) === "employee_offboarding") {
        setOffboardingConfirmRecord(record);
        return;
      }
      setFinanceConfirmRecord(record);
      return;
    }

    if (action === "complete") {
      openProcurementModal(record);
      return;
    }

    let rejectionReason = "";
    if (action === "reject") {
      const reason = window.prompt("Enter rejection reason:");
      if (reason === null) return;
      rejectionReason = reason;
    }

    try {
      const endpointAction = action === "line-manager-approve" ? "line-manager-approve" : action;
      const response = await fetch(`${apiBaseUrl}/workflow-requests/${recordId}/${endpointAction}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          actor_user_id: user?.id ?? null,
          actor_email: user?.email ?? null,
          actor_roles: user?.roles ?? [],
          rejection_reason: rejectionReason || null,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const responseData = await response.json();
      
      if (action === "approve") {
        setMessage("");
        triggerToast("MASTER APPROVED", "The workflow request has been successfully approved by the Master Admin.", "success");
      } else if (action === "line-manager-approve") {
        setMessage("");
        triggerToast("LINE MANAGER APPROVED", "The request has been sent to Finance for budget validation.", "success");
      } else if (action === "reject") {
        setMessage("");
        triggerToast("REJECTED", "The workflow request has been rejected.", "danger");
      } else if (action === "reopen") {
        setMessage("");
        triggerToast("WORKFLOW REOPENED", "The workflow request has been reopened.", "info");
      } else if (action === "validate-budget") {
        setMessage("");
        triggerToast("BUDGET VALIDATED", "The budget has been validated and request is awaiting purchase confirmation.", "success");
      } else if (action === "complete") {
        setMessage("");
        const payload = (record.payload || {}) as Record<string, any>;
        const itemName = payload.name || payload.title || payload.full_name || record.activated_entity_id || "(No Title)";
        const entityType = String(record.requested_module || "record").replace(/s$/, "").toUpperCase();
        triggerToast(
          "WORKFLOW COMPLETED",
          `PURCHASE CONFIRMED: ${entityType} '${itemName}' has been activated and is live.`,
          "success"
        );
      } else {
        setMessage("");
        setToast(`Workflow request updated.`);
      }
      
      if (responseData && responseData.warning) {
        triggerToast("WARNING", responseData.warning, "warning");
      }
      await loadRecords();
    } catch (error) {
      setMessage(`Workflow action failed. ${error instanceof Error ? error.message : "Error occurred."}`);
    }
  }

  async function handleSyncEmployees() {
    setEmployeeSyncLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/employees/sync`, { method: "POST" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(String(data.detail || "Employee sync failed."));
      }
      setMessage(String(data.message || "Employees synced from organization directory."));
      triggerToast("EMPLOYEES SYNCED", String(data.message || "Organization directory imported."), "success");
      await loadRecords();
      const statusResponse = await fetch(`${apiBaseUrl}/employees/sync/status`);
      if (statusResponse.ok) {
        setEmployeeSyncStatus(await statusResponse.json());
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Employee sync failed.");
    } finally {
      setEmployeeSyncLoading(false);
    }
  }

  async function handleTestEmail(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const recipient = testEmailTo.trim() || String(user?.email || "").trim();
    if (!recipient) {
      setMessage("Enter a recipient email address to send a test message.");
      return;
    }

    setTestEmailLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/api/email/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to_email: recipient }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(String(data.detail || "Test email failed."));
      }
      setMessage(`Test email sent from ${String(data.smtp?.fromAddress || "Derisk360 Gmail")} to ${recipient}. Check the recipient inbox.`);
      const statusResponse = await fetch(`${apiBaseUrl}/api/email/status`);
      if (statusResponse.ok) {
        setEmailStatus(await statusResponse.json());
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Test email failed.");
    } finally {
      setTestEmailLoading(false);
    }
  }

  async function handlePasswordChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = normaliseFormPayload(form, [
      { name: "current_password", label: "Current password", type: "password" },
      { name: "new_password", label: "New password", type: "password" },
    ]);

    try {
      const response = await fetch(`${apiBaseUrl}/settings/change-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: user?.id,
          ...payload,
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      form.reset();
      setMessage("Password updated successfully.");
    } catch {
      setMessage("Password update failed. Check your current password and minimum length.");
    }
  }

  // ── AI Co-pilot & Custom UI Handlers ───────────────────────────────────────

  useEffect(() => {
    if (!selectedOrgId) return;
    const cached = sessionStorage.getItem(`slmct.copilot.history.${selectedOrgId}`);
    if (cached) {
      try {
        setCopilotMessages(JSON.parse(cached));
        return;
      } catch {}
    }
    setCopilotMessages([
      {
        id: "welcome",
        role: "assistant",
        content: "Hello! I am your **Derisk360 AI Co-pilot**. I answer questions about subscriptions, licences, budgets, payments, and contracts for your selected organisation scope using live database tools. Ask me anything!",
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      },
    ]);
  }, [selectedOrgId]);

  useEffect(() => {
    if (selectedOrgId && copilotMessages.length > 0) {
      sessionStorage.setItem(`slmct.copilot.history.${selectedOrgId}`, JSON.stringify(copilotMessages));
    }
  }, [copilotMessages, selectedOrgId]);

  async function handleCopilotSend(textToSend: string) {
    if (!textToSend.trim() || copilotLoading) return;
    if (!selectedOrgId) {
      setToast("Select an organisation before using the Co-pilot.");
      return;
    }

    const userMsg = {
      id: `msg_${Date.now()}`,
      role: "user" as const,
      content: textToSend,
      timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    };

    setCopilotInput("");
    setCopilotMessages((prev) => [...prev, userMsg]);
    setCopilotLoading(true);

    try {
      const historyContext = [...copilotMessages, userMsg]
        .filter((m) => m.id !== "welcome" && String(m.content || "").trim())
        .map((m) => ({
          role: m.role,
          content: m.content,
          id: m.id,
        }));

      const res = await fetch(`${apiBaseUrl}/api/copilot`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: historyContext,
          organisation_id: selectedOrgId,
          actor_roles: userRoles,
          actor_user_id: user?.id ?? null,
        }),
      });

      const data = await res.json();
      if (!res.ok) {
        setToast(data.detail || "Co-pilot failed to respond.");
        return;
      }

      const assistantMsg = {
        id: `msg_${Date.now() + 1}`,
        role: "assistant" as const,
        content: data.responseText || "No response received.",
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      };

      setCopilotMessages((prev) => [...prev, assistantMsg]);
    } catch (err) {
      console.error(err);
      setToast("Could not reach the AI Co-pilot service. Ensure the backend server is running.");
    } finally {
      setCopilotLoading(false);
    }
  }

  function handleCopilotClear() {
    sessionStorage.removeItem(`slmct.copilot.history.${selectedOrgId}`);
    setCopilotMessages([
      {
        id: "welcome",
        role: "assistant",
        content: "Hello! I am your **Derisk360 AI Co-pilot**. I answer questions about subscriptions, licences, budgets, payments, and contracts for your selected organisation scope using live database tools. Ask me anything!",
        timestamp: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      },
    ]);
    setToast("Conversation history cleared.");
  }

  function formatInline(text: string) {
    const parts = text.split(/(\*\*.*?\*\*)/g);
    return parts.map((part, i) => {
      if (part.startsWith("**") && part.endsWith("**")) {
        return (
          <strong key={i} className="font-semibold" style={{ color: "#fff" }}>
            {part.slice(2, -2)}
          </strong>
        );
      }
      return part;
    });
  }

  function renderMarkdown(text: string) {
    const blocks = text.split("\n\n");
    return blocks.map((block, idx) => {
      const trimmed = block.trim();
      
      if (trimmed.startsWith("|") && trimmed.includes("\n|")) {
        const lines = trimmed.split("\n");
        const headers = lines[0]
          .split("|")
          .map((s) => s.trim())
          .filter(Boolean);
        const rows = lines
          .slice(2)
          .map((line) => line.split("|").map((s) => s.trim()).filter(Boolean))
          .filter((row) => row.length > 0);
          
        return (
          <div key={idx} className="my-3 overflow-x-auto rounded-lg border" style={{ borderColor: "rgba(255, 255, 255, 0.1)", background: "rgba(6, 23, 36, 0.5)" }}>
            <table className="w-full text-left text-xs" style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr className="border-b" style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.1)", background: "rgba(255, 255, 255, 0.05)" }}>
                  {headers.map((h, i) => (
                    <th key={i} className="px-3.5 py-2.5 font-bold tracking-wider uppercase" style={{ padding: "10px 14px", color: "#cbd5e1" }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, rIdx) => (
                  <tr key={rIdx} className="border-b" style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.05)" }}>
                    {row.map((cell, cIdx) => (
                      <td key={cIdx} className="px-3.5 py-2" style={{ padding: "8px 14px", color: "#e2e8f0" }}>
                        {formatInline(cell)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }

      if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
        const items = trimmed
          .split("\n")
          .map((l) => l.replace(/^[-*]\s+/, "").trim())
          .filter(Boolean);
        return (
          <ul key={idx} className="my-2 list-disc pl-5 space-y-1 text-sm" style={{ paddingLeft: "20px", color: "#cbd5e1" }}>
            {items.map((it, i) => (
              <li key={i} style={{ marginBottom: "4px" }}>{formatInline(it)}</li>
            ))}
          </ul>
        );
      }

      return (
        <p key={idx} className="text-sm leading-relaxed" style={{ fontSize: "14px", lineHeight: "1.6", color: "#cbd5e1", margin: "6px 0" }}>
          {formatInline(block)}
        </p>
      );
    });
  }


  async function applyReclaim(licenceId: string, kind: "unassign" | "decommission") {
    try {
      const payload = kind === "unassign"
        ? { assigned_to_person_id: null, status: "available" }
        : { status: "revoked" };
        
      const res = await fetch(`${apiBaseUrl}/licences/${licenceId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await responseError(res));

      await loadAllContext();
      setToast(`Licence successfully ${kind === "unassign" ? "unassigned" : "decommissioned"}.`);
    } catch (err) {
      console.error(err);
      setMessage(`Reclaim failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  async function handleReclaimItem(rec: Recommendation, itemId: string) {
    if (!rec.reclaim) return;
    const lic = allLicences.find((l) => l.id === itemId);
    const ok = window.confirm(
      rec.reclaim.kind === "unassign"
        ? `Revoke "${lic?.licence_name}" and return the seat to the available pool?`
        : `Decommission "${lic?.licence_name}"? This removes it from active spend.`
    );
    if (!ok) return;
    await applyReclaim(itemId, rec.reclaim.kind);
  }

  async function handleReclaimAll(rec: Recommendation) {
    if (!rec.reclaim || !rec.items) return;
    const ok = window.confirm(`Apply "${rec.reclaim.label}" to all ${rec.items.length} licences in this recommendation?`);
    if (!ok) return;
    const ids = rec.items.map((i) => i.id);
    for (const id of ids) {
      await applyReclaim(id, rec.reclaim.kind);
    }
  }

  useEffect(() => {
    if (!extractOpen) return;
    setExtractHealthChecked(false);
    fetch(`${apiBaseUrl}/api/health`)
      .then((r) => r.json())
      .then((h) => setExtractHealth(h))
      .catch(() => setExtractHealth(null))
      .finally(() => setExtractHealthChecked(true));
  }, [extractOpen]);

  function matchVendorId(name?: string) {
    if (!name) return "";
    const n = name.trim().toLowerCase();
    if (!n) return "";
    const exact = allVendors.find((v) => String(v.name).trim().toLowerCase() === n);
    if (exact) return exact.id;
    const partial = allVendors.find(
      (v) =>
        String(v.name).toLowerCase().includes(n) || n.includes(String(v.name).toLowerCase())
    );
    return partial?.id ?? "";
  }

  async function handleRunExtract() {
    if (!extractDoc && !extractText.trim()) {
      setToast("Attach a document or paste contract text first.");
      return;
    }
    setExtracting(true);
    try {
      const res = await fetch(`${apiBaseUrl}/api/extract`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dataUrl: extractDoc?.dataUrl,
          filename: extractDoc?.name,
          text: extractText.trim() || undefined,
          vendors: allVendors.map((v) => v.name),
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setToast(data?.detail || "Extraction failed.");
        return;
      }
      const f = data.fields ?? {};
      const matchVendor = matchVendorId(f.vendor);
      
      const nextPrefill = {
        organisation_id: selectedOrgId || "",
        vendor_id: matchVendor || "",
        subscription_id: "",
        title: f.title || "",
        contract_number: f.contract_number || "",
        contract_type: f.contract_type || "SaaS",
        start_date: f.start_date || "",
        end_date: f.end_date || "",
        value: Number(f.value) || 0,
        currency_code: f.currency_code || "AED",
        auto_renew: !!f.auto_renew,
        notice_period_days: Number(f.notice_period_days) || 0,
        owner: f.owner || "",
        status: f.status || "draft",
        notes: f.notes || "",
        document_name: extractDoc?.name || "",
        document_data: extractDoc?.dataUrl || ""
      };
      
      setContractDocName(extractDoc?.name || "");
      setContractDocData(extractDoc?.dataUrl || "");
      
      setExtractOpen(false);
      setEditingRecord(nextPrefill);
      setToast(`Extracted with Gemini — review and save.`);
    } catch (err) {
      console.error(err);
      setToast("Couldn't reach the extraction service. Ensure API server is running.");
    } finally {
      setExtracting(false);
    }
  }

  if (moduleKey === "copilot") {
    return (
      <AppShell title={config.title} description={config.description}>
        {toast ? (
          <div className={`toast-message toast-message--${toastType}`} role="status">
            <strong>{toastTitle || "Notification"}</strong>
            <span>{toast}</span>
            <button type="button" onClick={() => { setToast(""); setToastTitle(""); setToastType("info"); }}>Close</button>
          </div>
        ) : null}
        
        <div className="module-table-card" style={{ marginTop: "24px", display: "flex", flexDirection: "column", height: "calc(100vh - 250px)", minHeight: "450px", overflow: "hidden" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderBottom: "1px solid rgba(217, 244, 250, 0.16)", padding: "12px 20px" }}>
            <span style={{ fontSize: "14px", fontWeight: "bold", color: "#14b8a6", display: "flex", alignItems: "center", gap: "8px" }}>
              <svg style={{ height: "16px", width: "16px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
              Active Session
            </span>
            <button
              onClick={handleCopilotClear}
              style={{
                background: "transparent",
                border: "1px solid rgba(239, 68, 68, 0.3)",
                color: "#fca5a5",
                fontSize: "12px",
                padding: "4px 10px",
                borderRadius: "4px",
                cursor: "pointer"
              }}
            >
              Clear Chat
            </button>
          </div>

          <div style={{ flex: 1, overflowY: "auto", padding: "20px", display: "flex", flexDirection: "column", gap: "16px" }}>
            {copilotMessages.map((m) => (
              <div
                key={m.id}
                style={{
                  display: "flex",
                  gap: "12px",
                  alignItems: "start",
                  flexDirection: m.role === "user" ? "row-reverse" : "row"
                }}
              >
                <div
                  style={{
                    height: "32px",
                    width: "32px",
                    borderRadius: "8px",
                    border: "1px solid",
                    borderColor: m.role === "user" ? "rgba(49, 195, 234, 0.35)" : "rgba(20, 184, 166, 0.3)",
                    background: m.role === "user" ? "rgba(49, 195, 234, 0.1)" : "rgba(20, 184, 166, 0.15)",
                    color: m.role === "user" ? "#31c3ea" : "#14b8a6",
                    fontWeight: "bold",
                    fontSize: "12px",
                    display: "grid",
                    placeItems: "center",
                    flexShrink: 0
                  }}
                >
                  {m.role === "user" ? "U" : "AI"}
                </div>
                <div style={{ maxWidth: "75%", display: "flex", flexDirection: "column", gap: "4px", alignItems: m.role === "user" ? "flex-end" : "flex-start" }}>
                  <div
                    style={{
                      borderRadius: "12px",
                      border: "1px solid rgba(217, 244, 250, 0.08)",
                      background: m.role === "user" ? "rgba(49, 195, 234, 0.1)" : "rgba(6, 23, 36, 0.6)",
                      padding: "10px 14px",
                      color: "#f1f5f9"
                    }}
                  >
                    {renderMarkdown(m.content)}
                  </div>
                  <span style={{ fontSize: "10px", color: "#64748b" }}>{m.timestamp}</span>
                </div>
              </div>
            ))}

            {copilotLoading && (
              <div style={{ display: "flex", gap: "12px", alignItems: "start" }}>
                <div
                  style={{
                    height: "32px",
                    width: "32px",
                    borderRadius: "8px",
                    border: "1px solid rgba(20, 184, 166, 0.3)",
                    background: "rgba(20, 184, 166, 0.15)",
                    color: "#14b8a6",
                    fontWeight: "bold",
                    fontSize: "12px",
                    display: "grid",
                    placeItems: "center",
                    flexShrink: 0
                  }}
                >
                  AI
                </div>
                <div
                  style={{
                    borderRadius: "12px",
                    border: "1px solid rgba(217, 244, 250, 0.08)",
                    background: "rgba(6, 23, 36, 0.6)",
                    padding: "10px 14px",
                    color: "#94a3b8",
                    display: "flex",
                    alignItems: "center",
                    gap: "8px"
                  }}
                >
                  <span style={{ display: "inline-block", width: "12px", height: "12px", border: "2px solid #14b8a6", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 1s linear infinite" }} />
                  Thinking...
                </div>
              </div>
            )}
          </div>

          {copilotMessages.length === 1 && !copilotLoading && (
            <div style={{ borderTop: "1px solid rgba(217, 244, 250, 0.16)", background: "rgba(255, 255, 255, 0.01)", padding: "12px 20px" }}>
              <p style={{ fontSize: "10px", fontWeight: "bold", textTransform: "uppercase", color: "#94a3b8", marginBottom: "8px", display: "flex", alignItems: "center", gap: "4px" }}>
                <svg style={{ height: "12px", width: "12px", color: "#14b8a6" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z" />
                </svg>
                Try asking
              </p>
              <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
                {[
                  "Summarize upcoming renewals",
                  "Highlight licensing and access risks",
                  "Analyze budget allocations vs tracked spend",
                  "Find duplicate subscriptions and potential savings"
                ].map((s, idx) => (
                  <button
                    key={idx}
                    onClick={() => handleCopilotSend(s)}
                    style={{
                      background: "rgba(6, 23, 36, 0.4)",
                      border: "1px solid rgba(255, 255, 255, 0.08)",
                      color: "#e2e8f0",
                      fontSize: "12px",
                      padding: "6px 12px",
                      borderRadius: "6px",
                      cursor: "pointer"
                    }}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div style={{ borderTop: "1px solid rgba(217, 244, 250, 0.16)", background: "rgba(6, 23, 36, 0.8)", padding: "16px" }}>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                handleCopilotSend(copilotInput);
              }}
              style={{ display: "flex", gap: "10px" }}
            >
              <input
                type="text"
                value={copilotInput}
                onChange={(e) => setCopilotInput(e.target.value)}
                placeholder="Ask the AI Co-pilot a query..."
                disabled={copilotLoading}
                style={{
                  flex: 1,
                  borderRadius: "8px",
                  border: "1px solid rgba(255, 255, 255, 0.1)",
                  background: "rgba(4, 15, 24, 0.8)",
                  padding: "10px 14px",
                  color: "#fff",
                  outline: "none"
                }}
              />
              <button
                type="submit"
                disabled={!copilotInput.trim() || copilotLoading}
                style={{
                  border: "1px solid rgba(20, 184, 166, 0.3)",
                  background: "rgba(20, 184, 166, 0.15)",
                  color: "#14b8a6",
                  padding: "10px 16px",
                  borderRadius: "8px",
                  cursor: "pointer",
                  fontWeight: "bold"
                }}
              >
                Send
              </button>
            </form>
          </div>
        </div>
      </AppShell>
    );
  }

  if (moduleKey === "insights") {
    const insightsResult = computeInsights(scopedSubscriptions, scopedVendors);
    const optimisationsResult = computeOptimisations(
      scopedLicences,
      scopedBudgets,
      scopedPayments,
      scopedSubscriptions,
      scopedEmployees,
      scopedVendors,
      selectedOrgCurrency,
      fxRates
    );

    const dupSaving = insightsResult.findings
      .filter((f) => f.potentialAnnualSaving)
      .reduce((a, f) => {
        const saving = f.potentialAnnualSaving ?? 0;
        return a + toOrgCurrency(saving, f.currency ?? selectedOrgCurrency, fxRates, selectedOrgCurrency);
      }, 0);

    const totalActions = optimisationsResult.recommendations.length + insightsResult.findings.length;

    const severityColors: Record<string, { bg: string, text: string, border: string }> = {
      high: { bg: "rgba(239, 68, 68, 0.15)", text: "#fca5a5", border: "rgba(239, 68, 68, 0.3)" },
      medium: { bg: "rgba(245, 158, 11, 0.15)", text: "#fde047", border: "rgba(245, 158, 11, 0.3)" },
      low: { bg: "rgba(59, 130, 246, 0.15)", text: "#93c5fd", border: "rgba(59, 130, 246, 0.3)" }
    };

    return (
      <AppShell title={config.title} description={config.description}>
        {toast ? (
          <div className={`toast-message toast-message--${toastType}`} role="status">
            <strong>{toastTitle || "Notification"}</strong>
            <span>{toast}</span>
            <button type="button" onClick={() => { setToast(""); setToastTitle(""); setToastType("info"); }}>Close</button>
          </div>
        ) : null}

        <div style={{ display: "grid", gridTemplateColumns: showDashboardBudget ? "repeat(4, 1fr)" : "repeat(3, 1fr)", gap: "16px", marginTop: "24px" }}>
          <div className="dashboard-card" style={{ minHeight: "110px", padding: "16px" }}>
            <span style={{ fontSize: "13px", color: "#94a3b8" }}>Total recommendations</span>
            <strong style={{ display: "block", fontSize: "28px", color: totalActions ? "#fca5a5" : "#fff", marginTop: "8px" }}>
              {totalActions}
            </strong>
          </div>
          <div className="dashboard-card" style={{ minHeight: "110px", padding: "16px" }}>
            <span style={{ fontSize: "13px", color: "#94a3b8" }}>Idle licences</span>
            <strong style={{ display: "block", fontSize: "28px", color: "#fde047", marginTop: "8px" }}>
              {optimisationsResult.idleLicences}
            </strong>
          </div>
          {showDashboardBudget ? (
          <div className="dashboard-card" style={{ minHeight: "110px", padding: "16px" }}>
            <span style={{ fontSize: "13px", color: "#94a3b8" }}>Budget alerts</span>
            <strong style={{ display: "block", fontSize: "28px", color: "#fde047", marginTop: "8px" }}>
              {optimisationsResult.budgetAlerts}
            </strong>
          </div>
          ) : null}
          <div className="dashboard-card" style={{ minHeight: "110px", padding: "16px" }}>
            <span style={{ fontSize: "13px", color: "#94a3b8" }}>Duplicate products</span>
            <strong style={{ display: "block", fontSize: "28px", color: "#fff", marginTop: "8px" }}>
              {insightsResult.duplicateCount}
            </strong>
          </div>
        </div>

        <div style={{ marginTop: "32px", marginBottom: "16px", display: "flex", alignItems: "center", gap: "8px" }}>
          <svg style={{ height: "18px", width: "18px", color: "#14b8a6" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
          <div>
            <h2 style={{ fontSize: "18px", fontWeight: "bold", color: "#fff" }}>Spend & Licence Optimisation</h2>
            <p style={{ fontSize: "12px", color: "#64748b", marginTop: "2px" }}>
              {optimisationsResult.recommendations.length} recommendations from idle seats, expiring licences, access risks, and budget utilisation
            </p>
          </div>
        </div>

        {optimisationsResult.recommendations.length === 0 ? (
          <div className="dashboard-card" style={{ padding: "24px", textAlign: "center", color: "#94a3b8" }}>
            No optimisation opportunities found in this scope. Seats are assigned, licences are current, and budgets are within range.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            {optimisationsResult.recommendations.map((rec) => {
              const colors = severityColors[rec.severity] || severityColors.low;
              return (
                <div key={rec.id} className="dashboard-card" style={{ padding: "0", overflow: "hidden", display: "block", minHeight: "auto" }}>
                  <div style={{ padding: "20px", display: "flex", flexWrap: "wrap", justifyContent: "space-between", alignItems: "start", gap: "16px" }}>
                    <div style={{ flex: 1, minWidth: "250px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                        <h4 style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", margin: 0 }}>{rec.title}</h4>
                        <span style={{ fontSize: "10px", background: colors.bg, color: colors.text, border: `1px solid ${colors.border}`, padding: "2px 6px", borderRadius: "999px", textTransform: "capitalize" }}>
                          {rec.severity}
                        </span>
                        <span style={{ fontSize: "10px", background: "rgba(255, 255, 255, 0.05)", color: "#cbd5e1", border: "1px solid rgba(255, 255, 255, 0.1)", padding: "2px 6px", borderRadius: "999px", textTransform: "capitalize" }}>
                          {rec.category}
                        </span>
                      </div>
                      <p style={{ fontSize: "13px", color: "#94a3b8", marginTop: "6px", lineHeight: "1.5" }}>{rec.summary}</p>
                    </div>

                    <div style={{ display: "flex", flexDirection: "column", alignItems: "end", gap: "8px" }}>
                      {rec.metric && (
                        <div style={{ textAlign: "right" }}>
                          <span style={{ fontSize: "10px", textTransform: "uppercase", color: "#64748b" }}>{rec.metric.label}</span>
                          <p style={{ fontSize: "18px", fontWeight: "bold", color: rec.metric.tone === "danger" ? "#fca5a5" : rec.metric.tone === "warning" ? "#fde047" : "#fff", margin: "2px 0 0" }}>
                            {rec.metric.value}
                          </p>
                        </div>
                      )}
                      {rec.reclaim && rec.items && rec.items.length > 1 && (
                        <button
                          onClick={() => handleReclaimAll(rec)}
                          style={{
                            border: "1px solid rgba(20, 184, 166, 0.3)",
                            background: "rgba(20, 184, 166, 0.15)",
                            color: "#14b8a6",
                            padding: "6px 12px",
                            borderRadius: "6px",
                            fontSize: "12px",
                            fontWeight: "bold",
                            cursor: "pointer"
                          }}
                        >
                          {rec.reclaim.label} All
                        </button>
                      )}
                    </div>
                  </div>

                  {rec.items && rec.items.length > 0 && (
                    <div style={{ borderTop: "1px solid rgba(255, 255, 255, 0.08)" }}>
                      {rec.items.map((it) => (
                        <div key={it.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 20px", borderBottom: "1px solid rgba(255, 255, 255, 0.04)" }}>
                          <div>
                            <span style={{ fontSize: "13px", fontWeight: "600", color: "#fff" }}>{it.label}</span>
                            {it.sub && <span style={{ display: "block", fontSize: "11px", color: "#64748b", marginTop: "2px" }}>{it.sub}</span>}
                          </div>
                          <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                            {it.right && (
                              <span style={{ fontSize: "13px", fontWeight: "500", color: it.rightTone === "danger" ? "#fca5a5" : it.rightTone === "warning" ? "#fde047" : "#cbd5e1" }}>
                                {it.right}
                              </span>
                            )}
                            {rec.reclaim && (
                              <button
                                onClick={() => handleReclaimItem(rec, it.id)}
                                title={rec.reclaim.label}
                                style={{
                                  border: "1px solid rgba(20, 184, 166, 0.2)",
                                  background: "rgba(20, 184, 166, 0.08)",
                                  color: "#14b8a6",
                                  padding: "4px 8px",
                                  borderRadius: "4px",
                                  fontSize: "12px",
                                  cursor: "pointer"
                                }}
                              >
                                {rec.reclaim.label}
                              </button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  <div style={{ display: "flex", gap: "8px", alignItems: "start", background: "rgba(255, 255, 255, 0.02)", padding: "12px 20px", borderTop: "1px solid rgba(255, 255, 255, 0.08)" }}>
                    <svg style={{ height: "16px", width: "16px", color: "#14b8a6", flexShrink: 0, marginTop: "2px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                    </svg>
                    <p style={{ fontSize: "13px", color: "#cbd5e1", margin: 0 }}>
                      <strong style={{ color: "#fff" }}>Recommendation: </strong>
                      {rec.recommendation}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div style={{ marginTop: "40px", marginBottom: "16px", display: "flex", alignItems: "center", gap: "8px" }}>
          <svg style={{ height: "18px", width: "18px", color: "#14b8a6" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2" />
          </svg>
          <div>
            <h2 style={{ fontSize: "18px", fontWeight: "bold", color: "#fff" }}>Duplicate & Redundant Software</h2>
            <p style={{ fontSize: "12px", color: "#64748b", marginTop: "2px" }}>
              {insightsResult.findings.length} findings across {insightsResult.analysedCount} analysed subscriptions
              {dupSaving > 0 ? ` · ${formatValue(dupSaving)} ${selectedOrgCurrency}/yr potentially recoverable` : ""}
            </p>
          </div>
        </div>

        {insightsResult.findings.length === 0 ? (
          <div className="dashboard-card" style={{ padding: "24px", textAlign: "center", color: "#94a3b8" }}>
            No duplicate products or overlapping tooling detected in this scope.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            {insightsResult.findings.map((f) => {
              const colors = severityColors[f.severity] || severityColors.low;
              return (
                <div key={f.id} className="dashboard-card" style={{ padding: "0", overflow: "hidden", display: "block", minHeight: "auto" }}>
                  <div style={{ padding: "20px", display: "flex", flexWrap: "wrap", justifyContent: "space-between", alignItems: "start", gap: "16px" }}>
                    <div style={{ flex: 1, minWidth: "250px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
                        <h4 style={{ fontSize: "16px", fontWeight: "bold", color: "#fff", margin: 0 }}>{f.title}</h4>
                        <span style={{ fontSize: "10px", background: colors.bg, color: colors.text, border: `1px solid ${colors.border}`, padding: "2px 6px", borderRadius: "999px", textTransform: "capitalize" }}>
                          {f.severity}
                        </span>
                        <span style={{ fontSize: "10px", background: "rgba(255, 255, 255, 0.05)", color: "#cbd5e1", border: "1px solid rgba(255, 255, 255, 0.1)", padding: "2px 6px", borderRadius: "999px" }}>
                          {f.type === "duplicate" ? "Duplicate" : "Category Overlap"}
                        </span>
                      </div>
                      <p style={{ fontSize: "13px", color: "#94a3b8", marginTop: "6px", lineHeight: "1.5" }}>{f.summary}</p>
                    </div>

                    {f.potentialAnnualSaving !== null ? (
                      <div style={{ textAlign: "right" }}>
                        <span style={{ fontSize: "10px", textTransform: "uppercase", color: "#64748b" }}>Potential Annual Saving</span>
                        <p style={{ fontSize: "18px", fontWeight: "bold", color: "#10b981", margin: "2px 0 0" }}>
                          {formatValue(f.potentialAnnualSaving)} <span style={{ fontSize: "12px", color: "#94a3b8" }}>{f.currency}</span>
                        </p>
                      </div>
                    ) : (
                      <div style={{ textAlign: "right" }}>
                        <span style={{ fontSize: "10px", textTransform: "uppercase", color: "#64748b" }}>Potential Saving</span>
                        <p style={{ fontSize: "14px", fontWeight: "500", color: "#94a3b8", margin: "2px 0 0" }}>
                          Mixed currencies — review
                        </p>
                      </div>
                    )}
                  </div>

                  <div className="table-wrap" style={{ borderTop: "1px solid rgba(255, 255, 255, 0.08)" }}>
                    <table className="module-table">
                      <thead>
                        <tr>
                          <th>Subscription</th>
                          <th>Vendor</th>
                          <th>Category</th>
                          <th>Billing</th>
                          <th>Amount</th>
                          <th>Annualised</th>
                        </tr>
                      </thead>
                      <tbody>
                        {f.items.map((it) => (
                          <tr key={it.subscriptionId}>
                            <td style={{ fontWeight: "bold", color: "#fff" }}>{it.name}</td>
                            <td>{it.vendorName}</td>
                            <td>{it.category}</td>
                            <td style={{ textTransform: "capitalize" }}>{it.billing_cycle}</td>
                            <td>
                              {formatValue(it.amount)} {it.currency_code}
                            </td>
                            <td>
                              {formatValue(it.annualised)} {it.currency_code}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <div style={{ display: "flex", gap: "8px", alignItems: "start", background: "rgba(255, 255, 255, 0.02)", padding: "12px 20px", borderTop: "1px solid rgba(255, 255, 255, 0.08)" }}>
                    <svg style={{ height: "16px", width: "16px", color: "#14b8a6", flexShrink: 0, marginTop: "2px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    </svg>
                    <p style={{ fontSize: "13px", color: "#cbd5e1", margin: 0 }}>
                      <strong style={{ color: "#fff" }}>Recommendation: </strong>
                      {f.recommendation}
                    </p>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </AppShell>
    );
  }

  if (moduleKey === "tool-requests") {
    const employeeSubmissionTypes = getAllowedWorkflowTypes("subscriptions", userRoles);
    const showEmployeeSubmission = canSubmitEmployeeSoftwareRequest(userRoles);
    const showInbox = canManageToolRequestInbox(userRoles);

    if (showEmployeeSubmission && !showInbox) {
      return (
        <AppShell title="Software Request" description="Submit an employee software request for line manager, finance, and IT approval.">
          {message ? <p className="module-message">{message}</p> : null}
          {toast ? (
            <div className={`toast-message toast-message--${toastType}`} role="status">
              <strong>{toastTitle || "Notification"}</strong>
              <span>{toast}</span>
              <button type="button" onClick={() => { setToast(""); setToastTitle(""); setToastType("info"); }}>Close</button>
            </div>
          ) : null}
          <section className="module-layout full-layout">
            <section className="module-table-card tool-requests-card">
              <div className="module-toolbar">
                <div>
                  <p className="eyebrow">Employee submission</p>
                  <h2>Request software access</h2>
                </div>
                <div className="toolbar-actions">
                  <button type="button" onClick={() => setEmployeeSoftwareRequestOpen(true)}>
                    <Icon name="add" />
                    <span>New request</span>
                  </button>
                </div>
              </div>
              <p className="tool-requests-intro">
                Submit an employee software request. Your line manager will review it first, then Finance and IT will complete procurement or licence assignment.
              </p>
            </section>
          </section>
          {employeeSoftwareRequestOpen ? (
            <WorkflowSubmissionModal
              moduleKey="subscriptions"
              title="Employee Software Request"
              user={user ?? undefined}
              selectedOrgId={selectedOrgId}
              allowedWorkflowTypes={employeeSubmissionTypes}
              organisations={allOrganisations}
              vendors={scopedVendors}
              subscriptions={scopedSubscriptions}
              employees={scopedEmployees}
              licences={allLicences}
              budgets={allBudgets}
              vendorCatalogue={vendorCatalogue}
              fxRates={fxRates}
              roleEmails={roleMailboxEmails}
              apiBaseUrl={apiBaseUrl}
              onClose={() => setEmployeeSoftwareRequestOpen(false)}
              onSubmit={(payload, workflowType, emailOverrides) =>
                submitWorkflowRequest(payload, workflowType, emailOverrides, "subscriptions")
              }
            />
          ) : null}
        </AppShell>
      );
    }

    if (!showInbox) {
      return (
        <AppShell title="Access denied" description="You do not have permission to view this area.">
          <section className="workspace-panel">
            <p>You do not have permission to perform this action.</p>
          </section>
        </AppShell>
      );
    }

    const inboxRecords = [...scopedRecords].sort(
      (a, b) => new Date(String(b.created_at || "")).getTime() - new Date(String(a.created_at || "")).getTime()
    );
    const activeRecord = toolRequestModalRecord;
    const canActOnRequest = activeRecord
      ? ["new", "under_review"].includes(String(activeRecord.status || "").toLowerCase())
      : false;

    return (
      <AppShell title={config.title} description={config.description}>
        {message ? <p className="module-message">{message}</p> : null}
        {toast ? (
          <div className={`toast-message toast-message--${toastType}`} role="status">
            <strong>{toastTitle || "Notification"}</strong>
            <span>{toast}</span>
            <button type="button" onClick={() => { setToast(""); setToastTitle(""); setToastType("info"); }}>Close</button>
          </div>
        ) : null}

        <section className="module-layout full-layout">
          <section className="module-table-card tool-requests-card">
            <div className="module-toolbar">
              <div>
                <p className="eyebrow">Employee Inbox</p>
                <h2>{loading ? "Loading..." : `${inboxRecords.length} request${inboxRecords.length === 1 ? "" : "s"}`}</h2>
              </div>
              <div className="toolbar-actions">
                <button aria-label="Refresh" title="Refresh" type="button" onClick={() => void loadRecords()}>
                  <Icon name="refresh" />
                </button>
              </div>
            </div>

            <p className="tool-requests-intro">
              Review employee software requests submitted outside the procurement workflow. Convert valid requests into subscription workflows for approval and procurement.
            </p>

            <div className="table-wrap">
              <table className="module-table tool-requests-table">
                <thead>
                  <tr>
                    <th>Requester</th>
                    <th>Department</th>
                    <th>Tool</th>
                    <th>Date</th>
                    <th>Status</th>
                    <th>View</th>
                  </tr>
                </thead>
                <tbody>
                  {inboxRecords.length === 0 ? (
                    <tr>
                      <td colSpan={6}>No tool requests in this organisation scope.</td>
                    </tr>
                  ) : (
                    inboxRecords.map((record) => (
                      <tr key={String(record.id)} className="tool-request-row">
                        <td>
                          <strong>{String(record.requester_name || "-")}</strong>
                          <span className="tool-request-email">{String(record.requester_email || "")}</span>
                        </td>
                        <td>{formatValue(record.department)}</td>
                        <td>
                          <strong>{String(record.requested_tool || "-")}</strong>
                          {record.estimated_amount ? (
                            <span className="tool-request-cost">
                              {formatValue(record.estimated_amount)} {String(record.currency_code || "AED")}
                            </span>
                          ) : null}
                        </td>
                        <td>{formatWorkflowDateOnly(record.created_at)}</td>
                        <td>
                          <span className={getToolRequestStatusClass(record.status)}>
                            {getToolRequestStatusLabel(record.status)}
                          </span>
                        </td>
                        <td>
                          <button
                            type="button"
                            className="tool-request-view-btn"
                            title="View request"
                            aria-label="View request"
                            onClick={() => setToolRequestModalRecord(record)}
                          >
                            ✉️
                          </button>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </section>

        {activeRecord ? (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setToolRequestModalRecord(null)}
          >
            <div
              className="modal-panel tool-request-modal"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <div>
                  <p className="eyebrow">Employee Tool Request</p>
                  <h2>{String(activeRecord.requested_tool || "Software Request")}</h2>
                </div>
                <button type="button" onClick={() => setToolRequestModalRecord(null)}>
                  Close
                </button>
              </div>

              <div className="tool-request-modal-body">
                <div className="tool-request-email-view">
                  <div className="tool-request-email-meta">
                    <div><strong>From:</strong> {String(activeRecord.requester_email || "-")}</div>
                    <div><strong>To:</strong> IT Admin</div>
                    <div><strong>Subject:</strong> {String(activeRecord.email_subject || `Request for ${activeRecord.requested_tool}`)}</div>
                  </div>
                  <div className="tool-request-email-message">
                    <strong>Message</strong>
                    <pre>{String(activeRecord.message_body || "No message provided.")}</pre>
                  </div>
                </div>

                <div className="workflow-details-grid">
                  <div className="workflow-details-item">
                    <span>Requester</span>
                    <strong>{String(activeRecord.requester_name || "-")}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Department</span>
                    <strong>{formatValue(activeRecord.department)}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Requested Tool</span>
                    <strong>{String(activeRecord.requested_tool || "-")}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Status</span>
                    <strong>{getToolRequestStatusLabel(activeRecord.status)}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Vendor</span>
                    <strong>{formatValue(activeRecord.vendor_name)}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Estimated Cost</span>
                    <strong>
                      {activeRecord.estimated_amount
                        ? `${formatValue(activeRecord.estimated_amount)} ${String(activeRecord.currency_code || "AED")}`
                        : "Not provided"}
                    </strong>
                  </div>
                </div>

                {activeRecord.business_justification ? (
                  <div className="workflow-details-alert workflow-details-alert--info">
                    <strong>Business Justification:</strong> {String(activeRecord.business_justification)}
                  </div>
                ) : null}

                {activeRecord.rejection_reason ? (
                  <div className="workflow-details-alert workflow-details-alert--danger">
                    <strong>Rejection Reason:</strong> {String(activeRecord.rejection_reason)}
                  </div>
                ) : null}

                {activeRecord.info_request_message ? (
                  <div className="workflow-details-alert workflow-details-alert--info">
                    <strong>Information Requested:</strong> {String(activeRecord.info_request_message)}
                  </div>
                ) : null}

                {activeRecord.workflow_request_id ? (
                  <div className="workflow-details-link">
                    <a href="/workflows">View linked workflow in Workflows board</a>
                  </div>
                ) : null}

                <section className="workflow-details-section" style={{ marginTop: "20px" }}>
                  <h3>Sent Emails</h3>
                  {renderSentEmailMailbox(
                    emailLogs,
                    "No emails have been sent for this tool request yet. An email is delivered to IT Admin when a new request is submitted.",
                  )}
                </section>
              </div>

              <div className="modal-actions tool-request-modal-actions">
                <button type="button" onClick={() => setToolRequestModalRecord(null)}>
                  Close
                </button>
                {canActOnRequest ? (
                  <>
                    <button
                      type="button"
                      className="tool-request-action-btn tool-request-action-btn--info"
                      onClick={() => handleToolRequestAction(activeRecord.id, "request-info")}
                    >
                      Request More Information
                    </button>
                    <button
                      type="button"
                      className="tool-request-action-btn tool-request-action-btn--reject"
                      onClick={() => handleToolRequestAction(activeRecord.id, "reject")}
                    >
                      Reject Request
                    </button>
                    <button
                      type="button"
                      className="tool-request-action-btn tool-request-action-btn--primary"
                      onClick={() => handleToolRequestAction(activeRecord.id, "create-workflow")}
                    >
                      Create Workflow
                    </button>
                  </>
                ) : null}
              </div>
            </div>
          </div>
        ) : null}
      </AppShell>
    );
  }

  if (moduleKey === "settings") {
    const smtpReady = Boolean(
      emailStatus?.smtpReady || emailStatus?.gmailConfigured || emailStatus?.outlookConfigured
    );
    const deliveryLabel = String(emailStatus?.deliveryMode || emailStatus?.provider || "unknown").toUpperCase();

    return (
      <AppShell title={config.title} description={config.description}>
        {message ? <p className="module-message">{message}</p> : null}
        <section className="module-layout compact-layout">
          {showEmailSettings ? (
          <section className="module-table-card settings-card">
            <div className="module-toolbar">
              <div>
                <p className="eyebrow">Email delivery</p>
                <h2>Gmail SMTP</h2>
              </div>
            </div>
            <div className="settings-details">
              <span>Mode</span>
              <strong>{deliveryLabel}</strong>
              <span>SMTP host</span>
              <strong>{String(emailStatus?.host || "Not configured")}</strong>
              <span>From address</span>
              <strong>{String(emailStatus?.fromAddress || "Not set")}</strong>
              <span>Authentication</span>
              <strong>{emailStatus?.authConfigured ? "Configured" : "Missing SMTP_USER / SMTP_PASS"}</strong>
              <span>Delivery enabled</span>
              <strong>{emailStatus?.emailDeliveryEnabled ? "Yes" : "No"}</strong>
              <span>Ready to send</span>
              <strong>{smtpReady && emailStatus?.emailDeliveryEnabled ? "Yes" : "No — set SMTP_PASS in infra/.env and restart API"}</strong>
            </div>
            <p style={{ marginTop: "14px", color: "var(--muted)", fontSize: "13px", lineHeight: 1.55 }}>
              Workflow emails are sent from <code>derisknotification@gmail.com</code> via Gmail SMTP.
              Demo recipients remain the Outlook role mailboxes configured in <code>infra/.env</code>.
              {emailStatus?.setupNote ? ` ${String(emailStatus.setupNote)}` : null}
            </p>
            <form
              className="module-form"
              style={{ marginTop: "18px" }}
              onSubmit={handleTestEmail}
            >
              <label>
                <span>Send test email to demo recipient</span>
                {demoRecipientOptions.length ? (
                  <select
                    value={testEmailTo}
                    onChange={(event) => setTestEmailTo(event.target.value)}
                  >
                    {demoRecipientOptions.map((option) => (
                      <option key={String(option.email)} value={String(option.email)}>
                        {String(option.label || option.role)} — {String(option.email)}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="email"
                    value={testEmailTo}
                    onChange={(event) => setTestEmailTo(event.target.value)}
                    placeholder="deriskemployee1@outlook.com"
                  />
                )}
              </label>
              <button type="submit" disabled={testEmailLoading || !smtpReady || !emailStatus?.emailDeliveryEnabled}>
                {testEmailLoading ? "Sending..." : "Send test email"}
              </button>
            </form>
          </section>
          ) : null}

          <form className="module-form" onSubmit={handlePasswordChange}>
            <div>
              <p className="eyebrow">Security</p>
              <h2>Change password</h2>
            </div>
            <label>
              <span>Current password</span>
              <input name="current_password" type="password" required />
            </label>
            <label>
              <span>New password</span>
              <input name="new_password" type="password" minLength={8} required />
            </label>
            <button type="submit">Update password</button>
          </form>
          <section className="module-table-card settings-card">
            <div className="module-toolbar">
              <div>
                <p className="eyebrow">Account</p>
                <h2>{user?.name ?? "Signed in user"}</h2>
              </div>
            </div>
            <div className="settings-details">
              <span>Email</span>
              <strong>{user?.email}</strong>
              <span>Roles</span>
              <strong>{user?.roles?.join(", ")}</strong>
            </div>
          </section>
        </section>
      </AppShell>
    );
  }

  function renderInfoReviewModal(approveAction: string) {
    if (!infoReviewModal) return null;
    const wf = infoReviewModal;
    const payload = (wf.payload || {}) as Record<string, unknown>;
    const infoResponse = String(payload.info_response || "");
    const infoRequest = String(wf.info_request_message || "");
    const softName = getWorkflowSoftwareName(wf, allSubscriptions);

    return (
      <div className="modal-backdrop" role="presentation" onClick={() => setInfoReviewModal(null)}>
        <div className="modal-panel" style={{ maxWidth: "540px" }} onClick={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <div>
              <p className="eyebrow">Information Request Response</p>
              <h2>{softName}</h2>
            </div>
            <button type="button" onClick={() => setInfoReviewModal(null)}>Close</button>
          </div>
          <div style={{ padding: "20px 24px", display: "flex", flexDirection: "column", gap: "16px" }}>
            <div style={{ fontSize: "12px", color: "rgba(255,255,255,0.45)" }}>
              Requested by <strong style={{ color: "#fff" }}>{String(wf.requested_by_email || wf.requested_by || "—")}</strong>
              {" · "}#{String(wf.id).slice(0, 8).toUpperCase()}
            </div>
            {infoRequest && (
              <div style={{
                background: "rgba(251,191,36,0.08)", border: "1px solid rgba(251,191,36,0.25)",
                borderRadius: "8px", padding: "12px 16px", fontSize: "13px",
              }}>
                <div style={{ color: "#fbbf24", fontWeight: 700, marginBottom: "6px" }}>What you asked:</div>
                <div style={{ color: "rgba(255,255,255,0.8)" }}>{infoRequest}</div>
              </div>
            )}
            {infoResponse ? (
              <div style={{
                background: "rgba(56,189,248,0.07)", border: "1px solid rgba(56,189,248,0.25)",
                borderRadius: "8px", padding: "12px 16px", fontSize: "13px",
              }}>
                <div style={{ color: "#38bdf8", fontWeight: 700, marginBottom: "6px" }}>Their response:</div>
                <div style={{ color: "rgba(255,255,255,0.85)", lineHeight: 1.6 }}>{infoResponse}</div>
              </div>
            ) : (
              <div style={{ color: "rgba(255,255,255,0.35)", fontSize: "13px", fontStyle: "italic" }}>
                No response text found in this workflow's payload.
              </div>
            )}
          </div>
          <div className="modal-actions">
            <button type="button" onClick={() => setInfoReviewModal(null)}>Cancel</button>
            <button
              type="button"
              className="k-btn k-btn--reject"
              onClick={async () => {
                const reason = window.prompt("Enter rejection reason:");
                if (reason === null) return;
                try {
                  await fetch(`${apiBaseUrl}/workflow-requests/${wf.id}/reject`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ actor_user_id: user?.id, actor_email: user?.email, actor_roles: userRoles, rejection_reason: reason }),
                  });
                  await loadAllContext();
                  setInfoReviewModal(null);
                  setToast("Workflow rejected.");
                  setToastType("danger");
                  setToastTitle("Rejected");
                } catch { setToast("Failed to reject."); setToastType("error"); setToastTitle("Error"); }
              }}
            >
              Reject
            </button>
            <button
              type="button"
              className="k-btn k-btn--approve"
              onClick={async () => {
                try {
                  const action = approveAction === "line-manager-approve" ? "line-manager-approve" : approveAction === "complete" ? "complete" : "approve";
                  await fetch(`${apiBaseUrl}/workflow-requests/${wf.id}/${action}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ actor_user_id: user?.id, actor_email: user?.email, actor_roles: userRoles }),
                  });
                  await loadAllContext();
                  setInfoReviewModal(null);
                  setToast("Workflow approved.");
                  setToastType("success");
                  setToastTitle("Approved");
                } catch { setToast("Failed to approve."); setToastType("error"); setToastTitle("Error"); }
              }}
            >
              Approve
            </button>
          </div>
        </div>
      </div>
    );
  }

  function renderPurchaseConfirmModal() {
    if (!purchaseConfirmRecord) return null;
        const payload = (purchaseConfirmRecord.payload || {}) as Record<string, unknown>;
        const itemName = getWorkflowSoftwareName(purchaseConfirmRecord, allSubscriptions);
        const validationError = validateActivationForm(activationForm);
        const canConfirm = !validationError;

        return (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={closeProcurementModal}
          >
            <div
              className="modal-panel purchase-confirm-modal"
              onClick={(event) => event.stopPropagation()}
            >
              <div className="modal-header">
                <div>
                  <p className="eyebrow">{procurementStep === "redirect" ? "Step 1 of 2 — Purchase" : "Step 2 of 2 — Activation"}</p>
                  <h2>{procurementStep === "redirect" ? "Go to Vendor Site" : "Confirm Purchase & Activation"}</h2>
                </div>
                <button type="button" onClick={closeProcurementModal}>
                  Close
                </button>
              </div>

              <div className="purchase-confirm-body">
                <div className="workflow-details-alert workflow-details-alert--info">
                  <strong>{itemName}</strong>
                  {payload.amount ? (
                    <>
                      {" "}
                      · {formatValue(payload.amount)} {String(payload.currency_code || "AED")}
                    </>
                  ) : null}
                  {payload.department ? <> · {String(payload.department)}</> : null}
                </div>

                {procurementStep === "redirect" && (
                  <div style={{ display: "flex", flexDirection: "column", gap: "16px", padding: "8px 0" }}>
                    <p style={{ margin: 0, fontSize: "14px", color: "#cbd5e1", lineHeight: 1.6 }}>
                      Click the button below to open the vendor&apos;s purchasing page in a new tab.
                      Complete the purchase there, then return here and click <strong style={{ color: "#fff" }}>I&apos;ve completed the purchase</strong>.
                    </p>
                    {procurementUrlLoading ? (
                      <div style={{ display: "flex", alignItems: "center", gap: "10px", color: "#94a3b8", fontSize: "13px" }}>
                        <span style={{ display: "inline-block", width: "16px", height: "16px", border: "2px solid #94a3b8", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
                        Looking up vendor purchase page…
                      </div>
                    ) : procurementVendorUrl ? (
                      <a
                        href={procurementVendorUrl.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{
                          display: "inline-flex", alignItems: "center", gap: "8px",
                          background: "var(--brand-blue)", color: "#fff", fontWeight: 700,
                          padding: "10px 20px", borderRadius: "8px", textDecoration: "none",
                          fontSize: "14px", width: "fit-content",
                        }}
                      >
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
                        </svg>
                        {procurementVendorUrl.label || "Open vendor purchase page"}
                      </a>
                    ) : (
                      <p style={{ margin: 0, fontSize: "13px", color: "#f59e0b" }}>
                        Could not find a purchase URL automatically. Please navigate to the vendor site manually.
                      </p>
                    )}
                  </div>
                )}

                {procurementStep === "credentials" && <><div className="modal-form-grid">
                  <label style={{ gridColumn: "1 / -1" }}>
                    <span>Activation Method</span>
                    <select
                      value={activationForm.activation_method}
                      onChange={(event) =>
                        setActivationForm((prev) => ({
                          ...prev,
                          activation_method: event.target.value as ActivationMethod | "",
                        }))
                      }
                      required
                    >
                      <option value="">Select activation method</option>
                      {activationMethodOptions.map((option) => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>



                {activationForm.activation_method === "license_key" ? (
                  <div className="modal-form-grid activation-method-fields">
                    <label>
                      <span>License Key</span>
                      <input
                        value={activationForm.license_key}
                        onChange={(event) =>
                          setActivationForm((prev) => ({ ...prev, license_key: event.target.value }))
                        }
                        placeholder="XXXX-YYYY-ZZZZ"
                        required
                      />
                    </label>
                    <label>
                      <span>Activation Code <em style={{ fontWeight: 400, fontStyle: "normal", color: "var(--muted)" }}>(optional)</em></span>
                      <input
                        value={activationForm.activation_code}
                        onChange={(event) =>
                          setActivationForm((prev) => ({ ...prev, activation_code: event.target.value }))
                        }
                      />
                    </label>
                  </div>
                ) : null}

                {activationForm.activation_method === "company_account" ? (
                  <div className="modal-form-grid activation-method-fields">
                    <label>
                      <span>Username</span>
                      <input
                        value={activationForm.username}
                        onChange={(event) =>
                          setActivationForm((prev) => ({ ...prev, username: event.target.value }))
                        }
                        placeholder="user@example.com"
                        required
                      />
                    </label>
                    <label>
                      <span>Password</span>
                      <input
                        type="password"
                        value={activationForm.password}
                        onChange={(event) =>
                          setActivationForm((prev) => ({ ...prev, password: event.target.value }))
                        }
                        placeholder="••••••••"
                        required
                      />
                    </label>
                  </div>
                ) : null}

                {validationError ? (
                  <div className="workflow-details-alert workflow-details-alert--danger">
                    {validationError}
                  </div>
                ) : null}
                </>}
              </div>

              <div className="modal-actions purchase-confirm-actions">
                <button type="button" onClick={closeProcurementModal}>
                  Cancel
                </button>
                {procurementStep === "redirect" ? (
                  <button
                    type="button"
                    style={{ background: "#10b981", borderColor: "#10b981", color: "#fff", fontWeight: 700 }}
                    onClick={() => setProcurementStep("credentials")}
                  >
                    ✓ I&apos;ve completed the purchase
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={!canConfirm}
                    onClick={() => submitPurchaseConfirmation(purchaseConfirmRecord.id, activationForm)}
                    style={{
                      background: canConfirm ? "var(--brand-blue)" : undefined,
                      borderColor: canConfirm ? "var(--brand-blue)" : undefined,
                      opacity: canConfirm ? 1 : 0.55,
                      cursor: canConfirm ? "pointer" : "not-allowed",
                    }}
                  >
                    Confirm Purchase & Activate
                  </button>
                )}
              </div>
            </div>
          </div>
        );
  };

  function renderOffboardingConfirmModal() {
    if (!offboardingConfirmRecord) return null;
        const obPayload = (offboardingConfirmRecord.payload || {}) as Record<string, unknown>;
        const obEmail = String(obPayload.offboarded_employee_email || "");
        const obName = String(obPayload.offboarded_employee_name || obEmail || "");
        const obLicences = obEmail
          ? allLicences.filter((l) => {
              const emp = scopedEmployees.find((e) => String(e.id) === String(l.assigned_to_person_id));
              return emp && String(emp.work_email || "").toLowerCase() === obEmail.toLowerCase() && !["revoked","expired"].includes(String(l.status));
            })
          : [];

        async function completeOffboarding() {
          setOffboardingCompleting(true);
          try {
            // Step 1: it_confirmed (only if still in submitted state)
            if (String(offboardingConfirmRecord!.status) !== "it_confirmed") {
              const r1 = await fetch(`${apiBaseUrl}/workflow-requests/${String(offboardingConfirmRecord!.id)}/validate-budget`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ actor_user_id: user?.id, actor_email: user?.email, actor_roles: userRoles }),
              });
              if (!r1.ok) throw new Error(await r1.text());
            }
            // Step 2: complete
            const r2 = await fetch(`${apiBaseUrl}/workflow-requests/${String(offboardingConfirmRecord!.id)}/complete`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ actor_user_id: user?.id, actor_email: user?.email, actor_roles: userRoles, offboarded_employee_email: obEmail }),
            });
            if (!r2.ok) throw new Error(await r2.text());
            setOffboardingConfirmRecord(null);
            triggerToast("OFFBOARDING COMPLETE", `${obName} has been offboarded. All licences revoked and login deactivated.`, "success");
            await loadRecords();
            await loadAllContext();
            await refreshDashboardData();
          } catch (err) {
            triggerToast("ERROR", err instanceof Error ? err.message : "Offboarding failed.", "danger");
          } finally {
            setOffboardingCompleting(false);
          }
        }

        return (
          <div className="modal-backdrop" onClick={() => !offboardingCompleting && setOffboardingConfirmRecord(null)}>
            <div className="modal-panel" style={{ maxWidth: "520px" }} onClick={(e) => e.stopPropagation()}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
                <div>
                  <p style={{ fontSize: "11px", textTransform: "uppercase", color: "#f59e0b", fontWeight: 700, letterSpacing: "0.08em", margin: 0 }}>IT Action Required</p>
                  <h2 style={{ fontSize: "22px", fontWeight: 800, color: "#fff", margin: "4px 0 0" }}>Confirm Offboarding</h2>
                </div>
                <button className="modal-close" onClick={() => setOffboardingConfirmRecord(null)} disabled={offboardingCompleting}>Close</button>
              </div>

              <div style={{ background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.25)", borderRadius: "8px", padding: "14px 16px", marginBottom: "20px" }}>
                <p style={{ margin: 0, fontSize: "14px", color: "#fff" }}>
                  <strong>{obName}</strong>{obEmail && obName !== obEmail ? ` (${obEmail})` : ""} is being offboarded.
                </p>
                <p style={{ margin: "6px 0 0", fontSize: "13px", color: "#94a3b8" }}>
                  Confirm all software access has been removed before completing. Clicking the button below will automatically revoke all remaining licences, deactivate the employee record, and disable their login.
                </p>
              </div>

              <div style={{ marginBottom: "20px" }}>
                <p style={{ fontSize: "13px", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.06em", margin: "0 0 10px" }}>
                  Active licences to be revoked ({obLicences.length})
                </p>
                {obLicences.length === 0 ? (
                  <div style={{ padding: "12px 16px", background: "rgba(16,185,129,0.08)", border: "1px solid rgba(16,185,129,0.2)", borderRadius: "8px", fontSize: "13px", color: "#34d399" }}>
                    No active licences found for this employee.
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                    {obLicences.map((l) => {
                      const sub = allSubscriptions.find((s) => String(s.id) === String(l.subscription_id));
                      return (
                        <div key={String(l.id)} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "10px 14px", background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)", borderRadius: "6px" }}>
                          <div>
                            <span style={{ fontSize: "14px", fontWeight: 600, color: "#fff" }}>{String(l.licence_name || "Licence")}</span>
                            {sub && <span style={{ fontSize: "12px", color: "#94a3b8", marginLeft: "8px" }}>{String(sub.name || "")}</span>}
                          </div>
                          <span style={{ fontSize: "11px", padding: "2px 8px", borderRadius: "999px", background: "rgba(239,68,68,0.15)", color: "#fca5a5", border: "1px solid rgba(239,68,68,0.3)", fontWeight: 600 }}>
                            {String(l.status || "active")}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>

              <div style={{ display: "flex", gap: "12px", justifyContent: "flex-end" }}>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setOffboardingConfirmRecord(null)}
                  disabled={offboardingCompleting}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={completeOffboarding}
                  disabled={offboardingCompleting}
                  style={{ padding: "10px 20px", borderRadius: "8px", background: offboardingCompleting ? "rgba(239,68,68,0.4)" : "rgba(239,68,68,0.85)", color: "#fff", border: "none", fontWeight: 700, fontSize: "14px", cursor: offboardingCompleting ? "not-allowed" : "pointer" }}
                >
                  {offboardingCompleting ? "Completing offboarding…" : "✓ All Licences Have Been Removed"}
                </button>
              </div>
            </div>
          </div>
        );
  };

  function renderFinanceConfirmModal() {
    if (!financeConfirmRecord) return null;
        const getBudgetAlertDetails = (record: AnyRecord) => {
          const payload = (record.payload || {}) as Record<string, any>;
          const department = payload.department || "";
          const requestAmount = Number(payload.amount) || 0;
          const currency = payload.currency_code || "AED";
          const orgId = record.organisation_id || selectedOrgId;

          const currentYear = new Date().getFullYear();
          const deptBudgets = allBudgets.filter(
            (b) =>
              String(b.department || "").toLowerCase().trim() === String(department).toLowerCase().trim() &&
              (orgId ? b.organisation_id === orgId : true) &&
              b.status === "approved"
          );
          const deptBudget = deptBudgets.find((b) => Number(b.fiscal_year) === currentYear) || deptBudgets[0];

          const allocatedAmount = deptBudget ? Number(deptBudget.allocated_amount) : 0;
          const budgetCurrency = deptBudget ? String(deptBudget.currency_code) : currency;

          const activeSubs = allSubscriptions.filter(
            (s) =>
              String(s.department || "").toLowerCase().trim() === String(department).toLowerCase().trim() &&
              (orgId ? s.organisation_id === orgId : true) &&
              ["active", "trial", "pending_renewal"].includes(String(s.status || ""))
          );
          const currentSpend = activeSubs.reduce(
            (sum, s) => sum + toOrgCurrency(Number(s.amount || 0), String(s.currency_code || budgetCurrency), fxRates, budgetCurrency),
            0
          );
          const normalizedRequestAmount = toOrgCurrency(requestAmount, currency, fxRates, budgetCurrency);
          const totalAfterPurchase = currentSpend + normalizedRequestAmount;
          const remainingBefore = allocatedAmount - currentSpend;
          const remainingAfter = allocatedAmount - totalAfterPurchase;
          const exceedsBudget = totalAfterPurchase > allocatedAmount;

          return {
            department,
            allocatedAmount,
            budgetCurrency,
            currentSpend,
            requestAmount: normalizedRequestAmount,
            rawRequestAmount: requestAmount,
            currency,
            remainingBefore,
            remainingAfter,
            exceedsBudget,
            hasBudget: !!deptBudget,
          };
        };

        const stats = getBudgetAlertDetails(financeConfirmRecord);
        const payload = (financeConfirmRecord.payload || {}) as Record<string, any>;
        const itemName = payload.name || payload.title || payload.full_name || financeConfirmRecord.activated_entity_id || "(No Title)";
        const requester = String(financeConfirmRecord.requested_by_email || "");
        
        const utilizationPercent = stats.allocatedAmount > 0
          ? Math.min(100, Math.round(((stats.currentSpend + stats.requestAmount) / stats.allocatedAmount) * 100))
          : 0;

        const progressClass = utilizationPercent > 90 
          ? "dept-progress-fill--danger" 
          : utilizationPercent > 75 
          ? "dept-progress-fill--warning" 
          : "dept-progress-fill--normal";

        return (
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={() => setFinanceConfirmRecord(null)}
          >
            <div
              className="modal-panel"
              onClick={(event) => event.stopPropagation()}
              style={{ width: "min(600px, 95vw)" }}
            >
              <div className="modal-header">
                <div>
                  <p className="eyebrow">Finance Review & Budget Validation</p>
                  <h2>Approve Budget & Validate</h2>
                </div>
                <button
                  type="button"
                  onClick={() => setFinanceConfirmRecord(null)}
                >
                  Close
                </button>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: "16px", padding: "20px 0" }}>
                <div style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.08)", paddingBottom: "12px" }}>
                  <h4 style={{ margin: 0, fontSize: "14px", color: "#fff" }}>Purchase Details</h4>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", marginTop: "8px", fontSize: "13px", color: "#cbd5e1" }}>
                    <div><strong>Software Item:</strong> {itemName}</div>
                    <div><strong>Requester:</strong> {requester}</div>
                    <div><strong>Department:</strong> {stats.department || "-"}</div>
                    <div><strong>Cost:</strong> {formatValue(stats.rawRequestAmount)} {stats.currency}</div>
                  </div>
                </div>

                {(() => {
                  const totalAllocated = departmentStats.reduce((sum, d) => sum + d.allocatedAmount, 0);
                  const totalSpent = departmentStats.reduce((sum, d) => sum + d.totalCost, 0);
                  const totalRemaining = totalAllocated - totalSpent;
                  const totalUtilization = totalAllocated > 0 ? Math.round((totalSpent / totalAllocated) * 100) : 0;
                  return (
                    <div style={{ borderBottom: "1px solid rgba(255, 255, 255, 0.08)", paddingBottom: "12px" }}>
                      <h4 style={{ margin: 0, fontSize: "14px", color: "#fff" }}>Organisation-Wide Budget Summary</h4>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", marginTop: "8px", fontSize: "13px", color: "#cbd5e1" }}>
                        <div><strong>Total Allocated:</strong> {formatValue(totalAllocated)} {selectedOrgCurrency}</div>
                        <div><strong>Total Tracked Spend:</strong> {formatValue(totalSpent)} {selectedOrgCurrency}</div>
                        <div><strong>Total Remaining:</strong> <span style={{ color: totalRemaining < 0 ? "#ef4444" : "#34d399" }}>{formatValue(totalRemaining)} {selectedOrgCurrency}</span></div>
                        <div><strong>Total Utilization:</strong> {totalUtilization}%</div>
                      </div>
                    </div>
                  );
                })()}

                <div>
                  <h4 style={{ margin: 0, fontSize: "14px", color: "#fff", marginBottom: "8px" }}>Department Budget Verification</h4>
                  
                  <div className="budget-summary-grid">
                    <div className="budget-summary-box">
                      <span className="budget-summary-title">Allocated Budget</span>
                      <p className="budget-summary-amount" style={{ color: "#fff" }}>
                        {stats.hasBudget ? `${formatValue(stats.allocatedAmount)} ${stats.budgetCurrency}` : "No budget active"}
                      </p>
                    </div>
                    <div className="budget-summary-box">
                      <span className="budget-summary-title">Total Department Spend</span>
                      <p className="budget-summary-amount" style={{ color: "var(--brand-cyan)" }}>
                        {formatValue(stats.currentSpend)} {stats.budgetCurrency}
                      </p>
                    </div>
                    <div className="budget-summary-box">
                      <span className="budget-summary-title">Remaining Before Purchase</span>
                      <p className="budget-summary-amount" style={{ color: stats.remainingBefore < 0 ? "#fca5a5" : "#34d399" }}>
                        {stats.hasBudget ? `${formatValue(stats.remainingBefore)} ${stats.budgetCurrency}` : "-"}
                      </p>
                    </div>
                    <div className="budget-summary-box">
                      <span className="budget-summary-title">Remaining After Purchase</span>
                      <p className="budget-summary-amount" style={{ color: stats.remainingAfter < 0 ? "#fca5a5" : "#34d399" }}>
                        {stats.hasBudget ? `${formatValue(stats.remainingAfter)} ${stats.budgetCurrency}` : "-"}
                      </p>
                    </div>
                  </div>

                  {stats.hasBudget && (
                    <div className="dept-progress-container" style={{ marginTop: "12px" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "11px", color: "var(--muted)", fontWeight: "bold" }}>
                        <span>Utilization after purchase</span>
                        <span>{utilizationPercent}%</span>
                      </div>
                      <div className="dept-progress-bar">
                        <div className={`dept-progress-fill ${progressClass}`} style={{ width: `${utilizationPercent}%` }} />
                      </div>
                    </div>
                  )}
                  
                  {stats.hasBudget ? (
                    stats.exceedsBudget ? (
                      <div className="budget-alert-banner budget-alert-banner--red">
                        <svg style={{ height: "16px", width: "16px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                        </svg>
                        <strong>WARNING: This purchase will EXCEED the department budget allocated for this year.</strong>
                      </div>
                    ) : (
                      <div className="budget-alert-banner budget-alert-banner--green">
                        <svg style={{ height: "16px", width: "16px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                        </svg>
                        <strong>APPROVED: Within the department allocated budget.</strong>
                      </div>
                    )
                  ) : (
                    <div className="budget-alert-banner budget-alert-banner--red" style={{ background: "rgba(245, 158, 11, 0.1)", borderColor: "rgba(245, 158, 11, 0.3)", color: "#fde047" }}>
                      <svg style={{ height: "16px", width: "16px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                      </svg>
                      <strong>NOTICE: No approved budget found for this department. Proceed with caution.</strong>
                    </div>
                  )}
                </div>
              </div>

              <div className="modal-actions" style={{ borderTop: "1px solid rgba(255, 255, 255, 0.08)", paddingTop: "16px" }}>
                <button
                  type="button"
                  onClick={() => setFinanceConfirmRecord(null)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    const recId = financeConfirmRecord.id;
                    setFinanceConfirmRecord(null);
                    
                    try {
                      const response = await fetch(`${apiBaseUrl}/workflow-requests/${recId}/validate-budget`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                          actor_user_id: user?.id ?? null,
                          actor_email: user?.email ?? null,
                          actor_roles: user?.roles ?? [],
                        }),
                      });
                      if (!response.ok) throw new Error(await responseError(response));
                      const responseData = await response.json();
                      setMessage("");
                      triggerToast(
                        "BUDGET VALIDATED",
                        "The budget has been validated and request is awaiting purchase confirmation.",
                        "success"
                      );
                      if (responseData?.warning) {
                        triggerToast("WARNING", responseData.warning, "warning");
                      }
                      patchWorkflowRecord(recId, {
                        status: String(responseData?.status || "finance_approved"),
                      });
                      await refreshWorkflowBoard();
                      await refreshDashboardData();
                    } catch (error) {
                      setMessage(`Workflow action failed. ${error instanceof Error ? error.message : "Error occurred."}`);
                    }
                  }}
                  style={{
                    background: "var(--brand-blue)",
                    borderColor: "var(--brand-blue)"
                  }}
                >
                  Approve Budget
                </button>
              </div>
            </div>
          </div>
        );
  };

  function renderWorkflowDetailsModal() {
    if (!workflowDetailsRecord) return null;

    const record = workflowDetailsRecord;
    const payload = (record.payload || {}) as Record<string, unknown>;
    const emailContacts = getWorkflowEmailContacts(allUsers);
    const requesterEmail = String(record.requested_by_email || "-");
    const budgetInfo = getWorkflowBudgetInfo(record, allBudgets, allSubscriptions, selectedOrgId, fxRates);
    const activatedSubscription = record.activated_entity_id
      ? allSubscriptions.find((s) => String(s.id) === String(record.activated_entity_id))
      : null;
    const licenseCount = record.activated_entity_id
      ? allLicences.filter((l) => String(l.subscription_id) === String(record.activated_entity_id)).length
      : Number(payload.license_count || payload.seat_count || 0) || "-";
    const relatedPayment = record.activated_entity_id
      ? allPayments.find((p) => String(p.subscription_id) === String(record.activated_entity_id))
      : null;
    const invoiceNumber = String(
      payload.invoice_number || payload.invoice_no || relatedPayment?.reference || relatedPayment?.payment_reference || "-"
    );
    const softwareName = getWorkflowSoftwareName(record, allSubscriptions);
    const requesterName = resolveRequesterDisplay(record, allUsers);
    const isCompleted = ["completed", "finance_closed"].includes(String(record.status || "").toLowerCase());
    const subscriptionStatus = formatSubscriptionStatus(
      activatedSubscription?.status ||
        (isCompleted ? "active" : payload.status || "-")
    );
    const renewalDate = activatedSubscription?.renewal_date || payload.renewal_date;
    const purchaseDate = record.completed_at;
    const purchasedBy = resolveUserDisplay(allUsers, record.completed_by, "finance");
    const assignedEmployee = String(record.assigned_employee_name || requesterName || "-");
    const activationStatus = formatCategoryLabel(record.activation_status || (isCompleted ? "ready" : "-"));
    const activationRows = buildActivationInformationRows(record);
    const toolPurposeNotes = String(payload.notes || record.notes || "").trim();

    const submittedFormRows = Object.entries(payload)
      .filter(([key, value]) => value !== null && value !== undefined && String(value).trim() !== "")
      .map(([key, value]) => [
        key.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase()),
        String(value),
      ] as [string, string]);

    const sections = [
      {
        title: "Submitted Form",
        rows: submittedFormRows.length
          ? submittedFormRows
          : [["Form data", "No structured submission payload recorded."]],
      },
      {
        title: "Software Information",
        rows: [
          ["Software Name", softwareName],
          ["Vendor", resolveVendorName(allVendors, payload.vendor_id || activatedSubscription?.vendor_id)],
          ["Category", resolveCategoryName(payload.category, activatedSubscription)],
          ["Billing Type", formatCategoryLabel(payload.billing_cycle || activatedSubscription?.billing_cycle)],
          ...(showFinancialSpend
            ? [["Cost", payload.amount || activatedSubscription?.amount ? `${formatValue(payload.amount || activatedSubscription?.amount)} ${payload.currency_code || activatedSubscription?.currency_code || "AED"}` : "-"]]
            : []),
          ["Department", formatValue(payload.department || activatedSubscription?.department)],
          ["Tool Purpose / Notes", toolPurposeNotes || "Not provided"],
        ],
      },
      {
        title: "Workflow Information",
        rows: [
          ["Workflow ID", String(record.id || "-")],
          ["Workflow Type", workflowTypeLabel(record.workflow_type)],
          ["Current Status", getWorkflowStatusLabel(record.status, record.workflow_type, record.pre_info_request_status)],
          ["Workflow Stage", String(record.workflow_stage || getWorkflowStatusLabel(record.status, record.workflow_type, record.pre_info_request_status))],
          ["Requester", requesterName],
          ["Requester Email", requesterEmail],
          ["Requester Role", String(payload.requester_role || "-")],
          ["Current Approver", String(record.current_approver_name || "-")],
          ["Current Approver Email", String(record.current_approver_email || "-")],
          ["Final Receiver Email", roleMailboxEmails.master_admin || roleMailboxEmails.it_admin || "-"],
          ["Created Date", formatWorkflowDate(record.created_at)],
          ["Last Updated", formatWorkflowDate(record.updated_at)],
          ["Line Manager Approver", resolveUserDisplay(allUsers, record.line_manager_approved_by)],
          ["Master Approver", resolveUserDisplay(allUsers, record.master_approved_by, "master")],
          ...(showProcurementDetails
            ? [
                ["Finance Approver", resolveUserDisplay(allUsers, record.finance_approved_by, "finance")],
                ["Purchase Confirmed By", resolveUserDisplay(allUsers, record.completed_by, "finance")],
              ]
            : []),
        ],
      },
      ...(showDashboardBudget
        ? [
            {
              title: "Budget Information",
              rows: [
                ["Budget Name", budgetInfo.budgetName],
                ["Budget Amount", budgetInfo.budgetAmount],
                ["Budget Status", budgetInfo.budgetStatus],
              ],
            },
          ]
        : []),
      ...(showProcurementDetails
        ? [
            {
              title: "Procurement Information",
              rows: [
                ["Purchase Status", getPurchaseStatus(record)],
                ["Purchased By", purchasedBy],
                ["Purchase Date", purchaseDate ? formatWorkflowDateOnly(purchaseDate) : "-"],
                ["Invoice Number", invoiceNumber === "-" ? "Not recorded" : invoiceNumber],
              ],
            },
          ]
        : []),
      {
        title: "Subscription Information",
        rows: [
          ["Subscription Status", subscriptionStatus],
          ["Renewal Date", renewalDate ? formatWorkflowDateOnly(renewalDate) : "-"],
          ["License Count", formatValue(licenseCount)],
        ],
      },
      ...(record.activation_method
        ? [
            {
              title: "Activation Information",
              rows: activationRows,
            },
          ]
        : []),
      {
        title: "Email Notifications",
        rows: [
          ["Requester Email", requesterEmail],
          ["Approver Emails", emailContacts.approverEmails.join(", ")],
          ...(showProcurementDetails ? [["Finance Emails", emailContacts.financeEmails.join(", ")]] : []),
          ["Delivery Status", getEmailDeliveryStatus(emailLogs)],
        ],
      },
      {
        title: "Audit Trail",
        rows: [
          ["Workflow Created", formatWorkflowDate(record.created_at)],
          ["Approved", formatWorkflowDate(record.master_approved_at)],
          ...(showProcurementDetails
            ? [
                ["Finance Approved", formatWorkflowDate(record.finance_approved_at)],
                ["Purchase Confirmed", formatWorkflowDate(record.completed_at)],
              ]
            : []),
          ["Completed", isCompleted ? formatWorkflowDate(record.completed_at) : "-"],
        ],
      },
    ];

    return (
      <div
        className="modal-backdrop"
        role="presentation"
        onClick={() => setWorkflowDetailsRecord(null)}
      >
        <div
          className="modal-panel workflow-details-modal"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="modal-header">
            <div>
              <p className="eyebrow">Workflow Details</p>
              <h2>{softwareName}</h2>
            </div>
            <button type="button" onClick={() => setWorkflowDetailsRecord(null)}>
              Close
            </button>
          </div>

          <div className="workflow-details-body">
            {isCompleted ? (
              <section className="workflow-details-completion">
                <h3>Purchase & Activation Summary</h3>
                <div className="workflow-details-grid workflow-details-grid--highlight">
                  <div className="workflow-details-item">
                    <span>Assigned Employee</span>
                    <strong>{assignedEmployee}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Activation Method</span>
                    <strong>{getActivationMethodLabel(record.activation_method)}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Activation Status</span>
                    <strong>{activationStatus}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Subscription Status</span>
                    <strong>{subscriptionStatus}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Purchase Date</span>
                    <strong>{purchaseDate ? formatWorkflowDateOnly(purchaseDate) : "-"}</strong>
                  </div>
                  <div className="workflow-details-item">
                    <span>Purchased By</span>
                    <strong>{purchasedBy}</strong>
                  </div>
                </div>
              </section>
            ) : null}

            {record.rejection_reason ? (
              <div className="workflow-details-alert workflow-details-alert--danger">
                <strong>Rejection Reason:</strong> {String(record.rejection_reason)}
              </div>
            ) : null}

            {wfAiReview && (
              <section style={{ marginBottom: "20px" }}>
                <h3 style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "12px" }}>
                  <span>AI Review</span>
                  <span style={{ fontSize: "11px", fontWeight: 400, color: "var(--text-secondary)", background: "var(--surface-2)", padding: "2px 8px", borderRadius: "4px" }}>
                    Powered by Gemini
                  </span>
                </h3>
                {wfAiReview.loading ? (
                  <div style={{ display: "flex", alignItems: "center", gap: "10px", color: "var(--text-secondary)", fontSize: "13px" }}>
                    <span style={{ width: "16px", height: "16px", border: "2px solid var(--text-secondary)", borderTopColor: "transparent", borderRadius: "50%", display: "inline-block", animation: "spin 0.8s linear infinite" }} />
                    Analysing request...
                  </div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
                      <span style={{
                        padding: "6px 16px", borderRadius: "20px", fontWeight: 700, fontSize: "13px",
                        background: wfAiReview.recommendation === "approve" ? "rgba(16,185,129,0.15)" : wfAiReview.recommendation === "reject" ? "rgba(239,68,68,0.15)" : "rgba(245,158,11,0.15)",
                        color: wfAiReview.recommendation === "approve" ? "#10b981" : wfAiReview.recommendation === "reject" ? "#ef4444" : "#f59e0b",
                        border: `1px solid ${wfAiReview.recommendation === "approve" ? "rgba(16,185,129,0.4)" : wfAiReview.recommendation === "reject" ? "rgba(239,68,68,0.4)" : "rgba(245,158,11,0.4)"}`,
                        textTransform: "capitalize",
                      }}>
                        {wfAiReview.recommendation === "approve" ? "✓" : wfAiReview.recommendation === "reject" ? "✗" : "⚠"} {wfAiReview.recommendation}
                      </span>
                      <span style={{ fontSize: "13px", color: "var(--text-secondary)" }}>
                        Confidence: <strong style={{ color: "var(--text-primary)" }}>{wfAiReview.confidence}%</strong>
                      </span>
                    </div>
                    {wfAiReview.reasons.length > 0 && (
                      <ul style={{ margin: 0, paddingLeft: "18px", fontSize: "13px", color: "var(--text-primary)", display: "flex", flexDirection: "column", gap: "4px" }}>
                        {wfAiReview.reasons.map((r, i) => <li key={i}>{r}</li>)}
                      </ul>
                    )}
                    {wfAiReview.concerns.length > 0 && (
                      <div style={{ background: "rgba(245,158,11,0.08)", border: "1px solid rgba(245,158,11,0.3)", borderRadius: "6px", padding: "10px 14px" }}>
                        <p style={{ margin: "0 0 6px", fontSize: "12px", fontWeight: 600, color: "#f59e0b" }}>Concerns</p>
                        <ul style={{ margin: 0, paddingLeft: "16px", fontSize: "13px", color: "var(--text-primary)", display: "flex", flexDirection: "column", gap: "4px" }}>
                          {wfAiReview.concerns.map((c, i) => <li key={i}>{c}</li>)}
                        </ul>
                      </div>
                    )}
                    <p style={{ margin: 0, fontSize: "11px", color: "var(--text-secondary)" }}>AI recommendation — final decision remains with you.</p>
                  </div>
                )}
              </section>
            )}

            {sections.map((section) => (
              <section key={section.title} className="workflow-details-section">
                <h3>{section.title}</h3>
                <div className="workflow-details-grid">
                  {section.rows.map(([label, value]) => (
                    <div key={label} className="workflow-details-item">
                      <span>{label}</span>
                      <strong>{value}</strong>
                    </div>
                  ))}
                </div>
              </section>
            ))}

            <section className="workflow-details-section">
              <h3>Workflow Timeline</h3>
              {workflowStatusHistory.length ? (
                <div className="workflow-details-grid">
                  {workflowStatusHistory.map((entry) => (
                    <div key={String(entry.id)} className="workflow-details-item" style={{ gridColumn: "1 / -1" }}>
                      <span>
                        {String(entry.from_status || "start")} → {String(entry.to_status)} ·{" "}
                        {entry.created_at ? formatWorkflowDate(entry.created_at) : "-"}
                      </span>
                      <strong>{String(entry.notes || entry.actor_email || "-")}</strong>
                    </div>
                  ))}
                </div>
              ) : (
                <p style={{ color: "var(--muted)", fontStyle: "italic", fontSize: "13px" }}>No status history recorded yet.</p>
              )}
            </section>

            <section className="workflow-details-section">
              <h3>Email History</h3>
              {renderSentEmailMailbox(
                emailLogs,
                "No email previews stored yet. Previews are generated automatically at each workflow stage.",
              )}
            </section>

            {isCompleted && record.activated_entity_id ? (
              <div className="workflow-details-link">
                <a href={`/${record.requested_module}`}>
                  View Created {String(record.requested_module || "record").replace(/_/g, " ").toUpperCase()}
                </a>
              </div>
            ) : null}
          </div>

          <div className="modal-actions workflow-details-actions">
            {canRunWorkflowAction(record, "line-manager-approve") && (
              <button
                className="k-btn k-btn--approve"
                type="button"
                onClick={() => { setWorkflowDetailsRecord(null); handleWorkflowAction(record.id, "line-manager-approve"); }}
              >
                <Icon name="approve" /> Manager Approve
              </button>
            )}
            {canRunWorkflowAction(record, "approve") && (
              <button
                className="k-btn k-btn--approve"
                type="button"
                onClick={() => { setWorkflowDetailsRecord(null); handleWorkflowAction(record.id, "approve"); }}
              >
                <Icon name="approve" /> Approve
              </button>
            )}
            {canRunWorkflowAction(record, "validate-budget") && (
              <button
                className="k-btn k-btn--approve"
                type="button"
                onClick={() => {
                  setWorkflowDetailsRecord(null);
                  // Open the confirm modal directly — the dashboard's scopedRecords
                  // don't contain workflow rows, so handleWorkflowAction can't find it.
                  if (String(record.workflow_type) === "employee_offboarding") {
                    setOffboardingConfirmRecord(record);
                  } else {
                    setFinanceConfirmRecord(record);
                  }
                }}
              >
                <Icon name="approve" /> {String(record.workflow_type) === "employee_offboarding" ? "Confirm Licences Checked" : "Validate Budget"}
              </button>
            )}
            {canRunWorkflowAction(record, "complete") && String(record.workflow_type) !== "employee_offboarding" && (
              <button
                className="k-btn k-btn--complete"
                type="button"
                onClick={() => { setWorkflowDetailsRecord(null); openProcurementModal(record); }}
              >
                <Icon name="close" /> Complete Procurement
              </button>
            )}
            {canRunWorkflowAction(record, "reject") && (
              <button
                className="k-btn k-btn--reject"
                type="button"
                onClick={() => { setWorkflowDetailsRecord(null); handleWorkflowAction(record.id, "reject"); }}
              >
                <Icon name="reject" /> Reject
              </button>
            )}
            <button type="button" onClick={() => setWorkflowDetailsRecord(null)}>
              Close
            </button>
          </div>
        </div>
      </div>
    );
  };

  return (
    <AppShell title={config.title} description={config.description}>
      {message ? <p className="module-message">{message}</p> : null}
      {toast ? (
        <div className={`toast-message toast-message--${toastType}`} role="status">
          <strong>{toastTitle || "Notification"}</strong>
          <span>{toast}</span>
          <button type="button" onClick={() => { setToast(""); setToastTitle(""); setToastType("info"); }}>Close</button>
        </div>
      ) : null}

      {/* ── Subscription Creation Log (shown above audit trail) ── */}
      {moduleKey === "audit-logs" ? (
        <div className="sub-creation-log">
          <div className="sub-creation-log-header">
            <div>
              <h2>Subscription Creation Log</h2>
              <p>Software assets created through completed approval workflows — separate from workflow event audit trail.</p>
            </div>
            <span className="emp-section-count">
              {subLogLoading ? "…" : `${subscriptionCreationLog.length} record${subscriptionCreationLog.length !== 1 ? "s" : ""}`}
            </span>
          </div>
          {subLogLoading ? (
            <div className="emp-empty">Loading subscription creation log…</div>
          ) : subscriptionCreationLog.length === 0 ? (
            <div className="emp-empty">No subscriptions created through workflows yet.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Subscription</th>
                  <th>Vendor</th>
                  <th>Organisation</th>
                  <th>Created By</th>
                  <th>Workflow ID</th>
                  <th>Cost</th>
                  <th>Created Date</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {subscriptionCreationLog.map((row) => (
                  <tr key={String(row.workflow_id)}>
                    <td>
                      <span className="sub-name">{String(row.subscription_name || "—")}</span>
                      {row.assigned_user ? (
                        <span className="sub-meta">Assigned: {String(row.assigned_user)}</span>
                      ) : null}
                    </td>
                    <td>{String(row.vendor_name || "—")}</td>
                    <td>{String(row.organisation_name || "—")}</td>
                    <td>{String(row.created_by || "—")}</td>
                    <td>
                      <span style={{ fontFamily: "monospace", fontSize: "0.76rem", color: "var(--brand-cyan)" }}>
                        #{String(row.workflow_id || "").slice(0, 8).toUpperCase()}
                      </span>
                    </td>
                    <td>
                      {row.amount ? `${formatValue(row.amount)} ${String(row.currency_code || "")}` : "—"}
                    </td>
                    <td>{row.created_date ? formatWorkflowDateOnly(row.created_date) : "—"}</td>
                    <td>
                      <span className={`status-badge status-badge--${String(row.subscription_status || "").replace(/_/g, "-")}`}>
                        {formatSubscriptionStatus(row.subscription_status)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}

      {/* ── Diagnostics Panel ── */}
      {moduleKey === "diagnostics" ? (
        <DiagnosticsPanel apiBaseUrl={apiBaseUrl} user={user} />
      ) : null}

      <section className="module-layout full-layout" style={moduleKey === "diagnostics" ? { display: "none" } : {}}>
        <section className="module-table-card">
          <div className="module-toolbar">
            <div>
              <p className="eyebrow">Records</p>
              <h2>{loading ? "Loading..." : `${scopedRecords.length} item${scopedRecords.length === 1 ? "" : "s"}`}</h2>
            </div>
            <div className="toolbar-actions">
              {moduleKey === "contracts" && (
                <button
                  type="button"
                  onClick={() => {
                    setExtractText("");
                    setExtractDoc(null);
                    setExtractOpen(true);
                  }}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "6px",
                    background: "rgba(20, 184, 166, 0.15)",
                    color: "#14b8a6",
                    border: "1px solid rgba(20, 184, 166, 0.3)"
                  }}
                >
                  <svg style={{ height: "14px", width: "14px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                  <span>AI Extract</span>
                </button>
              )}
              {config.exportXlsx ? (
                <button type="button" onClick={handleExportXlsx} title="Export XLSX">
                  <Icon name="download" />
                  <span>Export</span>
                </button>
              ) : null}
              {canBulkUpload ? (
                <>
                  <button type="button" onClick={handleDownloadTemplate} title="Download bulk upload template">
                    <Icon name="download" />
                    <span>Template</span>
                  </button>
                  <label className="upload-button" title="Upload XLSX bulk file">
                    <Icon name="upload" />
                    <span>Upload</span>
                    <input accept=".xlsx" type="file" onChange={handleBulkUpload} />
                  </label>
                </>
              ) : null}
              {moduleKey === "users" && legacyDemoUsers.length > 0 && (isMasterAdmin || isItAdmin) ? (
                <button
                  type="button"
                  className="legacy-demo-cleanup-btn"
                  title="Remove all @derisk360.local and @demo.derisk360.com accounts"
                  onClick={() => void handleCleanupLegacyDemoUsers()}
                >
                  <Icon name="archive" />
                  <span>Remove {legacyDemoUsers.length} demo account{legacyDemoUsers.length === 1 ? "" : "s"}</span>
                </button>
              ) : null}
              {canMutate ? (
                <>
                  {isMasterAdmin && isSoftwareModule ? (
                    <div className="master-create-toggle" role="group" aria-label="Create mode">
                      <span className="master-create-toggle-label">Create as</span>
                      <button
                        type="button"
                        className={`master-create-toggle-btn${masterSoftwareCreateMode === "direct" ? " master-create-toggle-btn--active" : ""}`}
                        onClick={() => setMasterSoftwareCreateMode("direct")}
                        title="Create subscription or licence records immediately without a workflow"
                      >
                        Direct
                      </button>
                      <button
                        type="button"
                        className={`master-create-toggle-btn${masterSoftwareCreateMode === "workflow" ? " master-create-toggle-btn--active" : ""}`}
                        onClick={() => setMasterSoftwareCreateMode("workflow")}
                        title="Submit through the standard approval workflow like other roles"
                      >
                        Workflow
                      </button>
                    </div>
                  ) : null}
                  <button
                  type="button"
                  className={isSoftwareModule && canDirectCreateSoftware ? "master-direct-create-btn" : undefined}
                  onClick={() => {
                    if (isSoftwareModule && !canDirectCreateSoftware && !canSubmitSoftwareWorkflowRequest) {
                      triggerToast(
                        "ACCESS DENIED",
                        "You do not have permission to perform this action.",
                        "danger"
                      );
                      return;
                    }
                    setWorkflowFormDraft(selectedOrgId ? { organisation_id: selectedOrgId } : {});
                    setIsModalOpen(true);
                  }}
                >
                  <Icon name="add" />
                  <span>
                    {isSoftwareModule
                      ? canDirectCreateSoftware
                        ? "Add directly"
                        : "Submit request"
                      : "Add"}
                  </span>
                </button>
                </>
              ) : null}
              <button aria-label="Refresh" title="Refresh" type="button" onClick={() => void loadRecords()}>
                <Icon name="refresh" />
              </button>
            </div>
          </div>


          {moduleKey === "users" && legacyDemoUsers.length > 0 ? (
            <p style={{ margin: "0 0 16px", color: "rgba(253, 224, 71, 0.92)", fontSize: "13px" }}>
              {legacyDemoUsers.length} legacy demo account{legacyDemoUsers.length === 1 ? "" : "s"} still active (
              @derisk360.local / @demo.derisk360.com). Use <strong>Remove demo accounts</strong> to keep only your real Outlook users.
            </p>
          ) : null}

          {moduleKey === "workflows" ? (
            <div className="kanban-board">
              {(["submitted", "line_manager_approved", "finance_approved", "completed", "rejected"] as const).map((colStatus) => {
                const isOver = activeDragColumn === colStatus;
                const colRecords = scopedRecords.filter((r) => {
                  const stat = String(r.status || "").toLowerCase();
                  const needsLineManager = requiresLineManager(String(r.workflow_type || ""));
                  // For info_requested, use pre_info_request_status to assign to correct column
                  const effectiveStat = stat === "info_requested"
                    ? String(r.pre_info_request_status || "submitted").toLowerCase()
                    : stat;
                  if (colStatus === "submitted") {
                    if (!needsLineManager) return false;
                    return effectiveStat === "submitted" || effectiveStat === "reopened" || effectiveStat === "master_approved";
                  }
                  const isOffboarding = String(r.workflow_type) === "employee_offboarding";
                  if (colStatus === "line_manager_approved") {
                    if (needsLineManager) {
                      return effectiveStat === "line_manager_approved";
                    }
                    return (
                      !isOffboarding &&
                      (effectiveStat === "line_manager_approved" ||
                      effectiveStat === "master_approved" ||
                      ((effectiveStat === "submitted" || effectiveStat === "reopened") && !needsLineManager))
                    );
                  }
                  if (colStatus === "finance_approved") {
                    if (isOffboarding) {
                      return effectiveStat === "submitted" || effectiveStat === "reopened" || effectiveStat === "it_confirmed";
                    }
                    return effectiveStat === "finance_approved";
                  }
                  if (colStatus === "completed") {
                    return stat === "completed" || stat === "finance_closed";
                  }
                  return stat === colStatus;
                });

                const sortedColRecords = [...colRecords].sort((a, b) => {
                  const dateA = new Date(String(a.created_at || ""));
                  const dateB = new Date(String(b.created_at || ""));
                  return dateB.getTime() - dateA.getTime();
                });

                const totalPages = Math.ceil(sortedColRecords.length / 5);
                const currentPage = Math.min(kanbanPages[colStatus] || 0, Math.max(0, totalPages - 1));
                const paginatedRecords = sortedColRecords.slice(currentPage * 5, (currentPage + 1) * 5);

                const colTitle = {
                  submitted: "Waiting for Approval",
                  line_manager_approved: "Waiting for Budget Approval",
                  finance_approved: "Awaiting Procurement / IT",
                  completed: "Completed",
                  rejected: "Rejected",
                }[colStatus];

                const colThemeClass = `kanban-column--${colStatus}`;

                return (
                  <div
                    key={colStatus}
                    className={`kanban-column ${colThemeClass} ${isOver ? "kanban-column--dragover" : ""}`}
                    onDragLeave={() => setActiveDragColumn(null)}
                    onDragOver={(e) => {
                      e.preventDefault();
                      setActiveDragColumn(colStatus);
                    }}
                    onDrop={(e) => handleColumnDrop(e, colStatus)}
                  >
                    <div className="kanban-column-header">
                      <h3>{colTitle}</h3>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        <span className="kanban-column-count">{colRecords.length}</span>
                        {colRecords.length > 5 && (
                          <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                            <button
                              type="button"
                              disabled={currentPage === 0}
                              onClick={() => setKanbanPages((prev) => ({ ...prev, [colStatus]: currentPage - 1 }))}
                              style={{
                                background: "rgba(255, 255, 255, 0.08)",
                                border: "1px solid rgba(255, 255, 255, 0.15)",
                                borderRadius: "4px",
                                padding: "1px 6px",
                                fontSize: "11px",
                                color: currentPage === 0 ? "rgba(255,255,255,0.25)" : "#fff",
                                cursor: currentPage === 0 ? "not-allowed" : "pointer",
                              }}
                            >
                              ◀
                            </button>
                            <span style={{ fontSize: "11px", color: "rgba(255, 255, 255, 0.6)", minWidth: "24px", textAlign: "center" }}>
                              {currentPage + 1}/{totalPages}
                            </span>
                            <button
                              type="button"
                              disabled={currentPage + 1 >= totalPages}
                              onClick={() => setKanbanPages((prev) => ({ ...prev, [colStatus]: currentPage + 1 }))}
                              style={{
                                background: "rgba(255, 255, 255, 0.08)",
                                border: "1px solid rgba(255, 255, 255, 0.15)",
                                borderRadius: "4px",
                                padding: "1px 6px",
                                fontSize: "11px",
                                color: currentPage + 1 >= totalPages ? "rgba(255,255,255,0.25)" : "#fff",
                                cursor: currentPage + 1 >= totalPages ? "not-allowed" : "pointer",
                              }}
                            >
                              ▶
                            </button>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="kanban-cards">
                      {paginatedRecords.length === 0 ? (
                        <div className="kanban-empty-lane">
                          {records.length > 0 ? "No records in this column. Try changing your Organisation Scope (top right)." : "No records found."}
                        </div>
                      ) : (
                        paginatedRecords.map((record) => {
                          const payload = (record.payload || {}) as Record<string, unknown>;
                          const softwareName = getWorkflowSoftwareName(record, allSubscriptions);
                          const formattedDate = formatWorkflowDate(record.created_at);
                          const requesterName = resolveRequesterDisplay(record, allUsers);
                          const statusLabel = getWorkflowStatusLabel(record.status, record.workflow_type, record.pre_info_request_status);

                          const isMyRequest = String(record.requested_by_email || "").toLowerCase() === String(user?.email || "").toLowerCase();
                          const isCancellable = isMyRequest && ["submitted", "reopened", "info_requested"].includes(String(record.status));
                          const cardEmailLogs = allEmailLogs.filter((l) => String(l.workflowId) === String(record.id));
                          const latestStageLog = [...cardEmailLogs].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()).find((l) => l.workflowStage);
                          const isOffboardingCard = String(record.workflow_type) === "employee_offboarding";
                          const offboardEmail = isOffboardingCard ? String((payload as Record<string,unknown>).offboarded_employee_email || "") : "";
                          const offboardName = isOffboardingCard ? String((payload as Record<string,unknown>).offboarded_employee_name || offboardEmail || "") : "";
                          const offboardLicences = isOffboardingCard && offboardEmail
                            ? allLicences.filter((l) => {
                                const emp = scopedEmployees.find((e) => String(e.id) === String(l.assigned_to_person_id));
                                return emp && String(emp.work_email || "").toLowerCase() === offboardEmail.toLowerCase() && !["revoked","expired"].includes(String(l.status));
                              })
                            : [];

                          return (
                            <div
                              key={String(record.id)}
                              className="kanban-card kanban-card--compact"
                              draggable={true}
                              style={{ position: "relative" }}
                              onDragStart={(e) => {
                                e.dataTransfer.setData("text/plain", String(record.id));
                              }}
                            >
                              {isMyRequest && (
                                <span title="Your request" style={{ position: "absolute", top: "8px", right: "8px", background: "rgba(16,185,129,0.18)", color: "#34d399", borderRadius: "50%", width: "20px", height: "20px", display: "flex", alignItems: "center", justifyContent: "center", fontSize: "11px", fontWeight: 800, border: "1px solid rgba(16,185,129,0.4)" }}>✓</span>
                              )}
                              <h4 className="kanban-card-title">{isOffboardingCard ? `Offboarding: ${offboardName || softwareName}` : softwareName}</h4>
                              {isOffboardingCard && offboardLicences.length > 0 && (
                                <div style={{ margin: "6px 0 2px", padding: "8px", background: "rgba(239,68,68,0.08)", borderRadius: "6px", border: "1px solid rgba(239,68,68,0.2)" }}>
                                  <p style={{ fontSize: "11px", color: "#fca5a5", fontWeight: 600, margin: "0 0 4px" }}>Active licences to revoke ({offboardLicences.length})</p>
                                  {offboardLicences.map((l) => (
                                    <div key={String(l.id)} style={{ fontSize: "11px", color: "rgba(255,255,255,0.6)", padding: "1px 0" }}>• {String(l.licence_name || l.id)}</div>
                                  ))}
                                </div>
                              )}
                              {isOffboardingCard && offboardLicences.length === 0 && String(record.status) !== "completed" && (
                                <div style={{ margin: "6px 0 2px", padding: "6px 8px", background: "rgba(16,185,129,0.08)", borderRadius: "6px", border: "1px solid rgba(16,185,129,0.2)", fontSize: "11px", color: "#34d399" }}>
                                  No active licences found — safe to complete.
                                </div>
                              )}

                              <div className="kanban-card-amount">
                                {payload.amount ? (
                                  <>
                                    <strong>{formatValue(payload.amount)}</strong> {String(payload.currency_code || "AED")}
                                  </>
                                ) : (
                                  <strong>-</strong>
                                )}
                              </div>

                              <div className="kanban-card-status" style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                                <span className={getWorkflowStatusBadgeClass(record.status)}>{statusLabel}</span>
                                {(() => {
                                  const lastEmail = allEmailLogs.find((log) => String(log.workflowId) === String(record.id));
                                  
                                  let lastEmailText = "Pending";
                                  let lastEmailBadgeColor = "rgba(255, 255, 255, 0.4)";
                                  let lastEmailDotColor = "#94a3b8";

                                  if (lastEmail) {
                                    if (lastEmail.status === "SENT") {
                                      lastEmailText = "Sent";
                                      lastEmailBadgeColor = "rgba(16, 185, 129, 0.35)";
                                      lastEmailDotColor = "#34d399";
                                    } else if (lastEmail.status === "FAILED") {
                                      lastEmailText = "Failed";
                                      lastEmailBadgeColor = "rgba(239, 68, 68, 0.35)";
                                      lastEmailDotColor = "#f87171";
                                    } else if (lastEmail.status === "MOCK_MODE") {
                                      lastEmailText = "Mock Mode";
                                      lastEmailBadgeColor = "rgba(245, 158, 11, 0.35)";
                                      lastEmailDotColor = "#fbbf24";
                                    }
                                  }

                                  return (
                                    <div
                                      style={{
                                        display: "inline-flex",
                                        alignItems: "center",
                                        gap: "5px",
                                        background: lastEmailBadgeColor,
                                        borderRadius: "4px",
                                        padding: "2px 6px",
                                        fontSize: "11px",
                                        fontWeight: "600",
                                        color: "#ef4444"
                                      }}
                                    >
                                      <span style={{ width: "6px", height: "6px", borderRadius: "50%", background: lastEmailDotColor }} />
                                      <span>Email: {lastEmailText}</span>
                                    </div>
                                  );
                                })()}
                              </div>

                              <div className="kanban-card-meta">
                                <span className="kanban-card-user" title={String(record.requested_by_email)}>
                                  👤 {requesterName}
                                </span>
                                <span className="kanban-card-date">📅 {formattedDate}</span>
                              </div>

                              <div
                                className="kanban-card-actions"
                                onPointerDown={(event) => event.stopPropagation()}
                              >
                                <button
                                  className="k-btn k-btn--details"
                                  type="button"
                                  onClick={() => setWorkflowDetailsRecord(record)}
                                >
                                  Details
                                </button>
                                {canRunWorkflowAction(record, "line-manager-approve") && (
                                  <button
                                    className="k-btn k-btn--approve"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "line-manager-approve")}
                                  >
                                    <Icon name="approve" /> Manager Approve
                                  </button>
                                )}
                                {canRunWorkflowAction(record, "approve") && (
                                  <button
                                    className="k-btn k-btn--approve"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "approve")}
                                  >
                                    <Icon name="approve" /> Approve
                                  </button>
                                )}
                                {canRunWorkflowAction(record, "validate-budget") && (
                                  <button
                                    className="k-btn k-btn--approve"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "validate-budget")}
                                  >
                                    <Icon name="approve" /> {String(record.workflow_type) === "employee_offboarding" ? "Confirm Licences Checked" : "Validate Budget"}
                                  </button>
                                )}
                                {canRunWorkflowAction(record, "request-info") && (
                                  <button
                                    className="k-btn k-btn--reopen"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "request-info")}
                                  >
                                    <Icon name="wait" /> Request Info
                                  </button>
                                )}
                                {canRunWorkflowAction(record, "complete") && String(record.workflow_type) !== "employee_offboarding" && (
                                  <button
                                    className="k-btn k-btn--complete"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "complete")}
                                  >
                                    <Icon name="close" /> Complete Procurement
                                  </button>
                                )}
                                {String(record.workflow_type) === "employee_offboarding" && String(record.status) === "it_confirmed" && (isItAdmin || isMasterAdmin) && (
                                  <button
                                    className="k-btn k-btn--complete"
                                    type="button"
                                    onClick={() => setOffboardingConfirmRecord(record)}
                                  >
                                    <Icon name="close" /> Complete Offboarding
                                  </button>
                                )}
                                {canRunWorkflowAction(record, "reject") && (
                                  <button
                                    className="k-btn k-btn--reject"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "reject")}
                                  >
                                    <Icon name="reject" /> Reject
                                  </button>
                                )}
                                {canRunWorkflowAction(record, "reopen") && (
                                  <button
                                    className="k-btn k-btn--reopen"
                                    type="button"
                                    onClick={() => handleWorkflowAction(record.id, "reopen")}
                                  >
                                    <Icon name="reopen" /> Reopen
                                  </button>
                                )}
                                {isCancellable && (
                                  <button
                                    className="k-btn k-btn--reject"
                                    type="button"
                                    title="Cancel your request"
                                    onClick={() => {
                                      const rid = String(record.id);
                                      const confirmed = window.confirm("Cancel this request?");
                                      if (!confirmed) return;
                                      fetch(`${apiBaseUrl}/workflow-requests/${rid}/cancel`, {
                                        method: "POST",
                                        headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({ actor_email: user?.email, actor_roles: userRoles }),
                                      }).then(() => refreshWorkflowBoard());
                                    }}
                                  >
                                    <Icon name="reject" /> Cancel Request
                                  </button>
                                )}
                                {isMasterAdmin ? (
                                  <button
                                    className="k-btn k-btn--reject"
                                    type="button"
                                    title="Remove workflow"
                                    onClick={() => handleRemoveWorkflow(record.id)}
                                  >
                                    <Icon name="archive" /> Remove
                                  </button>
                                ) : null}
                              </div>
                            </div>
                          );
                        })
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div>
              {moduleKey === "budgets" && (
                <div style={{ marginBottom: "32px" }}>
                  {/* Total Budget Summary Bar */}
                  {(() => {
                    const totalAllocated = departmentStats.reduce((sum, d) => sum + d.allocatedAmount, 0);
                    const totalSpent = departmentStats.reduce((sum, d) => sum + d.totalCost, 0);
                    const totalRemaining = totalAllocated - totalSpent;
                    const utilizationPercent = totalAllocated > 0 ? Math.min(100, Math.round((totalSpent / totalAllocated) * 100)) : 0;
                    
                    const progressClass = utilizationPercent > 90 
                      ? "dept-progress-fill--danger" 
                      : utilizationPercent > 75 
                      ? "dept-progress-fill--warning" 
                      : "dept-progress-fill--normal";

                    return (
                      <div className="dashboard-grid" style={{ marginBottom: "24px" }} aria-label="Budget summary bar">
                        <article className="dashboard-card" style={{ background: "linear-gradient(135deg, rgba(16, 185, 129, 0.1), rgba(16, 185, 129, 0.02))", border: "1px solid rgba(16, 185, 129, 0.2)" }}>
                          <span>Total Allocated Budget</span>
                          <strong style={{ color: "#34d399" }}>{formatValue(totalAllocated)} {selectedOrgCurrency}</strong>
                          <p>Across {departmentStats.filter((d) => d.hasBudget).length} department{departmentStats.filter((d) => d.hasBudget).length !== 1 ? "s" : ""}</p>
                        </article>
                        <article className="dashboard-card" style={{ background: "linear-gradient(135deg, rgba(239, 68, 68, 0.05), rgba(239, 68, 68, 0.02))", border: "1px solid rgba(239, 68, 68, 0.2)" }}>
                          <span>Total Tracked Spend</span>
                          <strong style={{ color: "#fca5a5" }}>{formatValue(totalSpent)} {selectedOrgCurrency}</strong>
                          <p>Active subscriptions cost</p>
                        </article>
                        <article className="dashboard-card" style={{ background: "linear-gradient(135deg, rgba(49, 195, 234, 0.05), rgba(49, 195, 234, 0.02))", border: "1px solid rgba(49, 195, 234, 0.2)" }}>
                          <span>Total Remaining Budget</span>
                          <strong style={{ color: totalRemaining < 0 ? "#ef4444" : "var(--brand-cyan)" }}>{formatValue(totalRemaining)} {selectedOrgCurrency}</strong>
                          <p>{totalRemaining < 0 ? "Budget Deficit" : "Available Funds"}</p>
                        </article>
                        <article className="dashboard-card" style={{ display: "flex", flexDirection: "column", justifyContent: "center" }}>
                          <span>Overall Budget Utilization</span>
                          <strong style={{ fontSize: "1.6rem" }}>{utilizationPercent}%</strong>
                          <div className="dept-progress-bar" style={{ marginTop: "8px", background: "rgba(255,255,255,0.1)" }}>
                            <div className={`dept-progress-fill ${progressClass}`} style={{ width: `${utilizationPercent}%` }} />
                          </div>
                        </article>
                      </div>
                    );
                  })()}

                  {/* Department Governance Cards Grid */}
                  <div className="dept-analytics-grid">
                    {[...departmentStats].sort((a, b) => b.allocatedAmount - a.allocatedAmount).map((stat) => {
                      const percent = stat.allocatedAmount > 0
                        ? Math.min(100, Math.round((stat.totalCost / stat.allocatedAmount) * 100))
                        : 0;

                      const progressClass = percent > 90
                        ? "dept-progress-fill--danger"
                        : percent > 75
                        ? "dept-progress-fill--warning"
                        : "dept-progress-fill--normal";

                      const isEditing = budgetEditState?.id === stat.budgetId;

                      return (
                        <div key={stat.department} className="dept-analytics-card">
                          <div className="dept-card-header">
                            <h3>{stat.department}</h3>
                            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                              <span className="kanban-column-count">{stat.softwareCount} App{stat.softwareCount !== 1 ? "s" : ""}</span>
                              {(isFinance || isMasterAdmin) && stat.hasBudget && !isEditing && (
                                <button
                                  title="Edit budget"
                                  style={{ background: "none", border: "none", cursor: "pointer", color: "var(--brand-cyan)", padding: "2px 6px", borderRadius: "6px", fontSize: "13px" }}
                                  onClick={() => {
                                    const b = stat.budgetRaw as any;
                                    setBudgetEditState({
                                      id: String(b.id),
                                      department: String(b.department || stat.department),
                                      allocated_amount: String(b.allocated_amount || ""),
                                      currency_code: String(b.currency_code || selectedOrgCurrency),
                                      status: String(b.status || "approved"),
                                      notes: String(b.notes || ""),
                                      fiscal_year: String(b.fiscal_year || new Date().getFullYear()),
                                    });
                                  }}
                                >✏️</button>
                              )}
                            </div>
                          </div>

                          {/* ── Inline edit form ── */}
                          {isEditing && budgetEditState && (
                            <div style={{ display: "flex", flexDirection: "column", gap: "8px", background: "rgba(255,255,255,0.04)", borderRadius: "8px", padding: "12px" }}>
                              <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Department</label>
                              <input
                                style={{ padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px" }}
                                value={budgetEditState.department}
                                onChange={(e) => setBudgetEditState((s) => s ? { ...s, department: e.target.value } : s)}
                              />
                              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
                                <div>
                                  <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Allocated Amount</label>
                                  <input
                                    type="number"
                                    style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px" }}
                                    value={budgetEditState.allocated_amount}
                                    onChange={(e) => setBudgetEditState((s) => s ? { ...s, allocated_amount: e.target.value } : s)}
                                  />
                                </div>
                                <div>
                                  <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Currency</label>
                                  <select
                                    style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(30,30,40,0.9)", color: "#fff", fontSize: "13px" }}
                                    value={budgetEditState.currency_code}
                                    onChange={(e) => setBudgetEditState((s) => s ? { ...s, currency_code: e.target.value } : s)}
                                  >
                                    {["AED","USD","GBP","EUR","INR","SAR","QAR","KWD"].map((c) => <option key={c} value={c}>{c}</option>)}
                                  </select>
                                </div>
                              </div>
                              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
                                <div>
                                  <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Fiscal Year</label>
                                  <input
                                    type="number"
                                    style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px" }}
                                    value={budgetEditState.fiscal_year}
                                    onChange={(e) => setBudgetEditState((s) => s ? { ...s, fiscal_year: e.target.value } : s)}
                                  />
                                </div>
                                <div>
                                  <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Status</label>
                                  <select
                                    style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(30,30,40,0.9)", color: "#fff", fontSize: "13px" }}
                                    value={budgetEditState.status}
                                    onChange={(e) => setBudgetEditState((s) => s ? { ...s, status: e.target.value } : s)}
                                  >
                                    <option value="draft">Draft</option>
                                    <option value="approved">Approved</option>
                                    <option value="locked">Locked</option>
                                    <option value="closed">Closed</option>
                                  </select>
                                </div>
                              </div>
                              <div>
                                <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Notes</label>
                                <textarea
                                  rows={2}
                                  style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px", resize: "vertical", boxSizing: "border-box" }}
                                  value={budgetEditState.notes}
                                  onChange={(e) => setBudgetEditState((s) => s ? { ...s, notes: e.target.value } : s)}
                                />
                              </div>
                              <div style={{ display: "flex", gap: "8px", marginTop: "4px" }}>
                                <button
                                  disabled={budgetSaving}
                                  style={{ flex: 1, padding: "7px", borderRadius: "6px", background: "var(--brand-cyan)", color: "#000", fontWeight: 700, fontSize: "12px", border: "none", cursor: "pointer" }}
                                  onClick={async () => {
                                    setBudgetSaving(true);
                                    try {
                                      const res = await fetch(`${apiBaseUrl}/budgets/${budgetEditState.id}`, {
                                        method: "PATCH",
                                        headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({
                                          department: budgetEditState.department,
                                          allocated_amount: Number(budgetEditState.allocated_amount),
                                          currency_code: budgetEditState.currency_code,
                                          status: budgetEditState.status,
                                          notes: budgetEditState.notes || null,
                                          fiscal_year: Number(budgetEditState.fiscal_year),
                                        }),
                                      });
                                      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || "Failed to save budget"); }
                                      const updatedBudgets = await fetch(`${apiBaseUrl}/budgets?organisation_id=${selectedOrgId}`).then((r) => r.json());
                                      setAllBudgets(updatedBudgets);
                                      await loadRecords({ silent: true });
                                      setBudgetEditState(null);
                                      setToast("Budget updated."); setToastType("success"); setToastTitle("Saved");
                                    } catch (e) { setToast(e instanceof Error ? e.message : "Failed to save budget."); setToastType("error"); setToastTitle("Error"); }
                                    setBudgetSaving(false);
                                  }}
                                >{budgetSaving ? "Saving…" : "Save"}</button>
                                <button
                                  disabled={budgetSaving}
                                  style={{ padding: "7px 12px", borderRadius: "6px", background: "rgba(239,68,68,0.15)", color: "#fca5a5", fontWeight: 700, fontSize: "12px", border: "1px solid rgba(239,68,68,0.3)", cursor: "pointer" }}
                                  onClick={async () => {
                                    if (!confirm(`Remove budget for "${stat.department}"? This cannot be undone.`)) return;
                                    setBudgetSaving(true);
                                    try {
                                      await fetch(`${apiBaseUrl}/budgets/${budgetEditState.id}`, { method: "DELETE" });
                                      const updatedBudgets2 = await fetch(`${apiBaseUrl}/budgets`).then((r) => r.json());
                                      setAllBudgets(updatedBudgets2);
                                      await loadRecords({ silent: true });
                                      setBudgetEditState(null);
                                      setToast("Budget removed."); setToastType("success"); setToastTitle("Removed");
                                    } catch { setToast("Failed to remove budget."); setToastType("error"); setToastTitle("Error"); }
                                    setBudgetSaving(false);
                                  }}
                                >Remove</button>
                                <button
                                  style={{ padding: "7px 12px", borderRadius: "6px", background: "rgba(255,255,255,0.06)", color: "rgba(255,255,255,0.6)", fontSize: "12px", border: "1px solid rgba(255,255,255,0.1)", cursor: "pointer" }}
                                  onClick={() => setBudgetEditState(null)}
                                >Cancel</button>
                              </div>
                            </div>
                          )}

                          {!isEditing && (
                            <>
                          <div className="dept-card-stats">
                            <div className="dept-stat-item">
                              <span className="dept-stat-label">Budget</span>
                              <span className="dept-stat-val" style={{ color: "#fff" }}>
                                {stat.hasBudget ? `${formatValue(stat.allocatedAmount)}` : "-"}
                              </span>
                            </div>
                            <div className="dept-stat-item">
                              <span className="dept-stat-label">Spent</span>
                              <span className="dept-stat-val" style={{ color: stat.totalCost > 0 ? "var(--brand-cyan)" : "#fff" }}>
                                {formatValue(stat.totalCost)}
                              </span>
                            </div>
                            <div className="dept-stat-item">
                              <span className="dept-stat-label">Remaining</span>
                              <span className="dept-stat-val" style={{ color: stat.budgetLeft < 0 ? "#fca5a5" : "#34d399" }}>
                                {stat.hasBudget ? `${formatValue(stat.budgetLeft)}` : "-"}
                              </span>
                            </div>
                          </div>

                          {stat.hasBudget && (
                            <div className="dept-progress-container">
                              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "10px", color: "#fff", fontWeight: "800" }}>
                                <span>Utilization</span>
                                <span>{percent}%</span>
                              </div>
                              <div className="dept-progress-bar">
                                <div className={`dept-progress-fill ${progressClass}`} style={{ width: `${percent}%` }} />
                              </div>
                            </div>
                          )}

                          <div style={{ marginTop: "6px" }}>
                            <span style={{ fontSize: "11px", color: "#fff", fontWeight: "800", display: "block", marginBottom: "4px" }}>
                              Active Catalog
                            </span>
                            <div className="dept-app-list">
                              {stat.subscriptions.map((sub: any) => (
                                <div key={String(sub.id)} className="dept-app-item">
                                  <span className="dept-app-name" title={String(sub.name)}>{String(sub.name)}</span>
                                  <span className="dept-app-cost">{formatValue(sub.amount)} {sub.currency_code}</span>
                                </div>
                              ))}
                              {stat.subscriptions.length === 0 && (
                                <div style={{ fontSize: "11px", fontStyle: "italic", color: "#fff", fontWeight: 700, padding: "10px 0", textAlign: "center" }}>
                                  No active subscriptions.
                                </div>
                              )}
                            </div>
                          </div>
                            </>
                          )}
                        </div>
                      );
                    })}

                    {/* ── Add Department Budget card ── */}
                    {(isFinance || isMasterAdmin) && (
                      <div className="dept-analytics-card" style={{ border: "1px dashed rgba(217,244,250,0.25)", background: "rgba(255,255,255,0.02)" }}>
                        {!budgetAddOpen ? (
                          <button
                            style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: "10px", width: "100%", flex: 1, background: "none", border: "none", cursor: "pointer", color: "rgba(255,255,255,0.4)", minHeight: "120px" }}
                            onClick={() => { setBudgetAddFields({ department: "", allocated_amount: "", currency_code: selectedOrgCurrency, status: "approved", notes: "", fiscal_year: String(new Date().getFullYear()) }); setBudgetAddOpen(true); }}
                          >
                            <span style={{ fontSize: "32px", lineHeight: 1 }}>＋</span>
                            <span style={{ fontSize: "13px", fontWeight: 600 }}>Add Department Budget</span>
                          </button>
                        ) : (
                          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                            <div className="dept-card-header">
                              <h3 style={{ fontSize: "0.95rem" }}>New Budget</h3>
                            </div>
                            <div>
                              <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Department *</label>
                              <input
                                placeholder="e.g. Legal"
                                style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px", boxSizing: "border-box" }}
                                value={budgetAddFields.department}
                                onChange={(e) => setBudgetAddFields((s) => ({ ...s, department: e.target.value }))}
                              />
                            </div>
                            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
                              <div>
                                <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Amount *</label>
                                <input
                                  type="number"
                                  placeholder="0.00"
                                  style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px", boxSizing: "border-box" }}
                                  value={budgetAddFields.allocated_amount}
                                  onChange={(e) => setBudgetAddFields((s) => ({ ...s, allocated_amount: e.target.value }))}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Currency</label>
                                <select
                                  style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(30,30,40,0.9)", color: "#fff", fontSize: "13px" }}
                                  value={budgetAddFields.currency_code}
                                  onChange={(e) => setBudgetAddFields((s) => ({ ...s, currency_code: e.target.value }))}
                                >
                                  {["AED","USD","GBP","EUR","INR","SAR","QAR","KWD"].map((c) => <option key={c} value={c}>{c}</option>)}
                                </select>
                              </div>
                            </div>
                            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px" }}>
                              <div>
                                <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Fiscal Year</label>
                                <input
                                  type="number"
                                  style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px", boxSizing: "border-box" }}
                                  value={budgetAddFields.fiscal_year}
                                  onChange={(e) => setBudgetAddFields((s) => ({ ...s, fiscal_year: e.target.value }))}
                                />
                              </div>
                              <div>
                                <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Status</label>
                                <select
                                  style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(30,30,40,0.9)", color: "#fff", fontSize: "13px" }}
                                  value={budgetAddFields.status}
                                  onChange={(e) => setBudgetAddFields((s) => ({ ...s, status: e.target.value }))}
                                >
                                  <option value="draft">Draft</option>
                                  <option value="approved">Approved</option>
                                  <option value="locked">Locked</option>
                                </select>
                              </div>
                            </div>
                            <div>
                              <label style={{ fontSize: "11px", color: "#fff", fontWeight: 800, textTransform: "uppercase" }}>Notes</label>
                              <textarea
                                rows={2}
                                placeholder="Optional description…"
                                style={{ width: "100%", padding: "6px 10px", borderRadius: "6px", border: "1px solid rgba(217,244,250,0.2)", background: "rgba(255,255,255,0.06)", color: "#fff", fontSize: "13px", resize: "vertical", boxSizing: "border-box" }}
                                value={budgetAddFields.notes}
                                onChange={(e) => setBudgetAddFields((s) => ({ ...s, notes: e.target.value }))}
                              />
                            </div>
                            <div style={{ display: "flex", gap: "8px", marginTop: "4px" }}>
                              <button
                                disabled={budgetSaving || !budgetAddFields.department.trim() || !budgetAddFields.allocated_amount}
                                style={{ flex: 1, padding: "7px", borderRadius: "6px", background: "var(--brand-cyan)", color: "#000", fontWeight: 700, fontSize: "12px", border: "none", cursor: "pointer", opacity: (!budgetAddFields.department.trim() || !budgetAddFields.allocated_amount) ? 0.5 : 1 }}
                                onClick={async () => {
                                  if (!selectedOrgId) return;
                                  setBudgetSaving(true);
                                  try {
                                    const res = await fetch(`${apiBaseUrl}/budgets`, {
                                      method: "POST",
                                      headers: { "Content-Type": "application/json" },
                                      body: JSON.stringify({
                                        organisation_id: selectedOrgId,
                                        department: budgetAddFields.department.trim(),
                                        allocated_amount: Number(budgetAddFields.allocated_amount),
                                        currency_code: budgetAddFields.currency_code,
                                        status: budgetAddFields.status,
                                        notes: budgetAddFields.notes || null,
                                        fiscal_year: Number(budgetAddFields.fiscal_year),
                                      }),
                                    });
                                    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.detail || "Failed to create budget"); }
                                    const updatedBudgets3 = await fetch(`${apiBaseUrl}/budgets?organisation_id=${selectedOrgId}`).then((r) => r.json());
                                    setAllBudgets(updatedBudgets3);
                                    await loadRecords({ silent: true });
                                    setBudgetAddOpen(false);
                                    setToast(`Budget added for ${budgetAddFields.department}.`); setToastType("success"); setToastTitle("Budget Created");
                                  } catch (e) { setToast(e instanceof Error ? e.message : "Failed to create budget."); setToastType("error"); setToastTitle("Error"); }
                                  setBudgetSaving(false);
                                }}
                              >{budgetSaving ? "Saving…" : "Add Budget"}</button>
                              <button
                                style={{ padding: "7px 12px", borderRadius: "6px", background: "rgba(255,255,255,0.06)", color: "rgba(255,255,255,0.6)", fontSize: "12px", border: "1px solid rgba(255,255,255,0.1)", cursor: "pointer" }}
                                onClick={() => setBudgetAddOpen(false)}
                              >Cancel</button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>

                </div>
              )}
              {/* ── HR Admin custom employees table ── */}
              {moduleKey === "employees" && (isHrAdmin || isMasterAdmin || isItAdmin) && (() => {
                const lineManagers = allUsers.filter((u) =>
                  Array.isArray(u.roles) && (u.roles as string[]).includes("line_manager")
                );
                return (
                  <div className="table-wrap">
                    <table className="module-table">
                      <thead>
                        <tr>
                          <th>Employee</th>
                          <th>Department</th>
                          <th>Job Title</th>
                          <th>Line Manager</th>
                          <th>Status</th>
                          <th style={{ minWidth: "150px", width: "150px" }}>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {scopedRecords.map((emp) => {
                          const assignedLm = lineManagers.find(
                            (lm) => String(lm.work_email || "").toLowerCase() === String(emp.line_manager_email || "").toLowerCase()
                          );
                          const lmLabel = assignedLm
                            ? String(assignedLm.full_name || assignedLm.work_email)
                            : emp.line_manager_email
                            ? String(emp.line_manager_email)
                            : null;
                          return (
                            <tr key={String(emp.id)}>
                              <td style={{ minWidth: "180px" }}>
                                <span style={{ fontWeight: 700, color: "#fff", display: "block" }}>{String(emp.full_name || "—")}</span>
                                <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.5)", display: "block", marginTop: "2px" }}>{String(emp.work_email || "—")}</span>
                                {emp.employee_number && String(emp.employee_number) !== "-" ? (
                                  <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.35)", fontFamily: "monospace" }}>{String(emp.employee_number)}</span>
                                ) : null}
                              </td>
                              <td style={{ whiteSpace: "nowrap" }}>{String(emp.department || "—")}</td>
                              <td style={{ whiteSpace: "nowrap" }}>{String(emp.job_title || "—")}</td>
                              <td style={{ minWidth: "200px" }}>
                                {lmLabel ? (
                                  <span style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                                    <span style={{
                                      fontSize: "12px", fontWeight: 600, padding: "3px 10px", borderRadius: "999px",
                                      background: "rgba(49,195,234,0.12)", color: "var(--brand-cyan)",
                                      border: "1px solid rgba(49,195,234,0.3)", whiteSpace: "nowrap",
                                    }}>
                                      {lmLabel}
                                    </span>
                                  </span>
                                ) : (
                                  <span style={{ color: "rgba(255,255,255,0.3)", fontSize: "13px" }}>Not assigned</span>
                                )}
                                <div style={{ marginTop: "6px" }}>
                                  <select
                                    style={{
                                      fontSize: "12px", padding: "4px 8px", borderRadius: "6px",
                                      background: "rgba(255,255,255,0.06)", border: "1px solid rgba(217,244,250,0.2)",
                                      color: "rgba(255,255,255,0.7)", cursor: "pointer", width: "100%",
                                    }}
                                    value={String(emp.line_manager_email || "")}
                                    onChange={async (e) => {
                                      const val = e.target.value;
                                      try {
                                        await fetch(`${apiBaseUrl}/employees/${emp.id}`, {
                                          method: "PATCH",
                                          headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ line_manager_email: val || null }),
                                        });
                                        await loadRecords({ silent: true });
                                        setToast(`Line manager updated for ${String(emp.full_name)}`);
                                        setToastType("success");
                                        setToastTitle("Team assignment updated");
                                      } catch {
                                        setToast("Failed to update line manager.");
                                        setToastType("error");
                                        setToastTitle("Error");
                                      }
                                    }}
                                  >
                                    <option value="">— Unassigned —</option>
                                    {lineManagers.map((lm) => (
                                      <option key={String(lm.id)} value={String(lm.work_email || "")}>
                                        {String(lm.full_name || lm.work_email)}
                                      </option>
                                    ))}
                                  </select>
                                </div>
                              </td>
                              <td>
                                <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: "6px" }}>
                                  <span className={
                                    String(emp.status) === "active"
                                      ? "status-badge status-badge--approved"
                                      : "status-badge status-badge--rejected"
                                  }>
                                    {String(emp.status || "").toUpperCase()}
                                  </span>
                                  {(isHrAdmin || isMasterAdmin || isItAdmin) && String(emp.status || "") !== "inactive" && (
                                    <button
                                      aria-label="Offboard"
                                      title="Start offboarding workflow"
                                      type="button"
                                      style={{ fontSize: "11px", padding: "4px 10px", borderRadius: "6px", background: "rgba(245,158,11,0.12)", color: "#fde047", border: "1px solid rgba(245,158,11,0.3)", cursor: "pointer", whiteSpace: "nowrap" }}
                                      onClick={() => setOffboardingEmployee(emp)}
                                    >
                                      Offboard
                                    </button>
                                  )}
                                </div>
                              </td>
                              <td style={{ minWidth: "80px", whiteSpace: "nowrap" }}>
                                <div className="table-actions" style={{ flexWrap: "nowrap", gap: "6px", alignItems: "center" }}>
                                  <button
                                    aria-label="Edit"
                                    className="icon-action"
                                    title="Edit employee"
                                    type="button"
                                    onClick={() => {
  const r = openRecordForEdit(emp, moduleKey);
  setEditingRecord(r);
  setEditFormCurrency(String(r.currency_code || ""));
}}
                                  >
                                    <Icon name="edit" />
                                  </button>
                                  <button
                                    aria-label="Archive"
                                    className="icon-action danger"
                                    title="Archive employee"
                                    type="button"
                                    onClick={() => handleDelete(emp.id)}
                                  >
                                    <Icon name="archive" />
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                );
              })()}

              {/* ── Employee offboarding modal ── */}
              {offboardingEmployee ? (
                <WorkflowSubmissionModal
                  moduleKey="employees"
                  title={`Offboard ${String(offboardingEmployee.full_name || offboardingEmployee.work_email || "Employee")}`}
                  user={user ?? undefined}
                  selectedOrgId={selectedOrgId}
                  allowedWorkflowTypes={["employee_offboarding"]}
                  organisations={allOrganisations}
                  vendors={scopedVendors}
                  subscriptions={scopedSubscriptions}
                  employees={scopedEmployees}
                  licences={allLicences}
                  budgets={allBudgets}
                  vendorCatalogue={vendorCatalogue}
                  fxRates={fxRates}
                  roleEmails={roleMailboxEmails}
                  apiBaseUrl={apiBaseUrl}
                  onClose={() => setOffboardingEmployee(null)}
                  onSubmit={(payload, workflowType, emailOverrides) =>
                    submitWorkflowRequest({ ...payload, offboarded_employee_email: payload.offboarded_employee_email || String(offboardingEmployee.work_email || ""), offboarded_employee_name: payload.offboarded_employee_display_name || String(offboardingEmployee.full_name || "") }, workflowType, emailOverrides, "employees")
                  }
                />
              ) : null}

              {/* ── Custom Renewals page ── */}
              {moduleKey === "renewals" && (() => {
                const allRenewals = computeRenewals(
                  scopedSubscriptions,
                  scopedLicences,
                  scopedVendors,
                  scopedContracts,
                ).sort((a, b) => {
                  const da = daysUntil(a.renewal_date) ?? 99999;
                  const db = daysUntil(b.renewal_date) ?? 99999;
                  return da - db;
                });
                const ITEMS_PER_PAGE = 10;
                const totalPages = Math.max(1, Math.ceil(allRenewals.length / ITEMS_PER_PAGE));
                const safePage = Math.min(renewalsPageIdx, totalPages - 1);
                const pageItems = allRenewals.slice(safePage * ITEMS_PER_PAGE, (safePage + 1) * ITEMS_PER_PAGE);
                const urgencyLabel: Record<string, string> = { overdue: "Overdue", red: "< 30 days", amber: "30–60 days", green: "60–90 days" };
                const typeIcon: Record<string, string> = { subscription: "🔁", licence: "🪪", vendor: "🏢", contract: "📄" };
                return (
                  <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                    {allRenewals.length === 0 ? (
                      <p className="renewal-board-empty">No renewals found.</p>
                    ) : (
                      <>
                        <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: "10px" }}>
                          {pageItems.map((r) => {
                            const days = daysUntil(r.renewal_date);
                            const urg = urgencyFor(r.renewal_date) ?? "green";
                            return (
                              <article key={r.id} className={`renewal-card renewal-card--${urg}`} style={{ width: "100%" }}>
                                <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
                                  <span className={`renewal-badge renewal-badge--${urg}`} style={{ flexShrink: 0 }}>{urgencyLabel[urg]}</span>
                                  <span className="renewal-type-icon" style={{ flexShrink: 0 }}>{typeIcon[r.type] ?? "📋"}</span>
                                  <strong className="renewal-card-name" style={{ flex: 1, margin: 0 }}>{r.name}</strong>
                                  {r.vendor_name && r.vendor_name !== "-" && (
                                    <span className="renewal-card-vendor" style={{ margin: 0, flexShrink: 0 }}>{r.vendor_name}</span>
                                  )}
                                  <span className="renewal-card-date" style={{ flexShrink: 0 }}>📅 {r.renewal_date}</span>
                                  <span className={`renewal-days renewal-days--${urg}`} style={{ flexShrink: 0 }}>
                                    {days === null ? "" : days < 0 ? `${Math.abs(days)}d overdue` : days === 0 ? "Due today" : `${days}d left`}
                                  </span>
                                  {showRenewalAmounts && r.amount != null && r.amount > 0 && (
                                    <span className="renewal-card-amount" style={{ margin: 0, flexShrink: 0 }}>
                                      {new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(r.amount)}{" "}
                                      <span>{r.currency_code}</span>
                                    </span>
                                  )}
                                  <button
                                    type="button"
                                    className="k-btn"
                                    style={{ flexShrink: 0, fontSize: "0.75rem", padding: "4px 10px" }}
                                    onClick={() => {
                                      setWorkflowFormDraft({
                                        organisation_id: selectedOrgId ?? "",
                                        workflow_type: "renewal_request",
                                        request_type: "Renewal Request",
                                        subscription_id: r.type === "subscription" ? String(r.id) : "",
                                        vendor_id: r.type === "vendor" ? String(r.id) : "",
                                        name: String(r.name ?? ""),
                                        renewal_date: String(r.renewal_date ?? ""),
                                        amount: r.amount != null ? String(r.amount) : "",
                                        currency_code: String(r.currency_code ?? ""),
                                      });
                                      setIsModalOpen(true);
                                    }}
                                  >
                                    🔄 Renew
                                  </button>
                                </div>
                              </article>
                            );
                          })}
                        </div>
                        {totalPages > 1 && (
                          <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: "16px", marginTop: "8px" }}>
                            <button
                              className="k-btn"
                              disabled={safePage === 0}
                              style={{ opacity: safePage === 0 ? 0.4 : 1, minWidth: "36px", padding: "6px 12px" }}
                              type="button"
                              onClick={() => setRenewalsPageIdx(Math.max(0, safePage - 1))}
                            >◀</button>
                            <span style={{ fontSize: "0.8rem", color: "var(--muted)", fontWeight: 600 }}>
                              {safePage + 1} / {totalPages} · {allRenewals.length} renewals
                            </span>
                            <button
                              className="k-btn"
                              disabled={safePage >= totalPages - 1}
                              style={{ opacity: safePage >= totalPages - 1 ? 0.4 : 1, minWidth: "36px", padding: "6px 12px" }}
                              type="button"
                              onClick={() => setRenewalsPageIdx(Math.min(totalPages - 1, safePage + 1))}
                            >▶</button>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                );
              })()}

              {/* ── Renewals workflow modal ── */}
              {moduleKey === "renewals" && isModalOpen ? (
                <WorkflowSubmissionModal
                  moduleKey="renewals"
                  title="Renewal Request"
                  user={user ?? undefined}
                  selectedOrgId={selectedOrgId}
                  allowedWorkflowTypes={["renewal_request"]}
                  organisations={allOrganisations}
                  vendors={scopedVendors}
                  subscriptions={scopedSubscriptions}
                  employees={scopedEmployees}
                  licences={allLicences}
                  budgets={allBudgets}
                  vendorCatalogue={vendorCatalogue}
                  fxRates={fxRates}
                  roleEmails={roleMailboxEmails}
                  apiBaseUrl={apiBaseUrl}
                  initialDraft={workflowFormDraft}
                  onClose={() => setIsModalOpen(false)}
                  onSubmit={(payload, workflowType, emailOverrides) =>
                    submitWorkflowRequest(payload, workflowType, emailOverrides, "renewals")
                  }
                />
              ) : null}

              {/* ── Recycle Bin grouped by module ── */}
              {moduleKey === "recycle-bin" && (() => {
                const PAGE_SIZE = 5;
                const moduleOrder = ["subscriptions","licences","vendors","contracts","budgets","payments","employees","organisations","workflow_requests"];
                const moduleLabelMap: Record<string, string> = {
                  subscriptions: "Subscriptions", licences: "Licences", vendors: "Vendors",
                  contracts: "Contracts", budgets: "Budgets", payments: "Payments",
                  employees: "Employees", organisations: "Organisations",
                  workflow_requests: "Workflow Requests",
                };
                const grouped: Record<string, AnyRecord[]> = {};
                for (const r of scopedRecords) {
                  const mod = String(r.module || "other");
                  if (!grouped[mod]) grouped[mod] = [];
                  grouped[mod].push(r);
                }
                const presentModules = moduleOrder.filter((m) => grouped[m]?.length);
                const otherModules = Object.keys(grouped).filter((m) => !moduleOrder.includes(m) && grouped[m]?.length);
                const allModules = [...presentModules, ...otherModules];

                if (allModules.length === 0) {
                  return <p style={{ color: "rgba(255,255,255,0.4)", padding: "48px 0", textAlign: "center", fontSize: "14px" }}>The recycle bin is empty.</p>;
                }

                const btnBase: React.CSSProperties = {
                  display: "flex", alignItems: "center", gap: "5px", padding: "5px 14px",
                  borderRadius: "6px", fontSize: "12px", fontWeight: 600, cursor: "pointer", border: "1px solid",
                };

                return (
                  <div style={{ display: "flex", flexDirection: "column", gap: "40px" }}>
                    {allModules.map((mod) => {
                      const items = grouped[mod];
                      const label = moduleLabelMap[mod] ?? mod.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
                      const pageIdx = recycleBinPages[mod] ?? 0;
                      const totalPages = Math.ceil(items.length / PAGE_SIZE);
                      const safePage = Math.min(pageIdx, totalPages - 1);
                      const pageItems = items.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
                      const setPage = (p: number) => setRecycleBinPages((prev) => ({ ...prev, [mod]: p }));

                      return (
                        <div key={mod} style={{ background: "rgba(255,255,255,0.02)", border: "1px solid rgba(255,255,255,0.07)", borderRadius: "12px", overflow: "hidden" }}>
                          {/* Section header */}
                          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 20px", borderBottom: "1px solid rgba(255,255,255,0.07)", background: "rgba(255,255,255,0.03)" }}>
                            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                              <h3 style={{ fontSize: "12px", fontWeight: 700, color: "rgba(255,255,255,0.5)", textTransform: "uppercase", letterSpacing: "0.1em", margin: 0 }}>{label}</h3>
                              <span style={{ fontSize: "11px", background: "rgba(255,255,255,0.1)", borderRadius: "20px", padding: "2px 9px", color: "rgba(255,255,255,0.45)", fontWeight: 600 }}>{items.length}</span>
                            </div>
                            {totalPages > 1 && (
                              <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                                <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.35)" }}>
                                  {safePage + 1} / {totalPages}
                                </span>
                                <button type="button" disabled={safePage === 0}
                                  onClick={() => setPage(safePage - 1)}
                                  style={{ ...btnBase, color: safePage === 0 ? "rgba(255,255,255,0.2)" : "rgba(255,255,255,0.6)", borderColor: "rgba(255,255,255,0.12)", background: "rgba(255,255,255,0.04)" }}>
                                  ← Prev
                                </button>
                                <button type="button" disabled={safePage >= totalPages - 1}
                                  onClick={() => setPage(safePage + 1)}
                                  style={{ ...btnBase, color: safePage >= totalPages - 1 ? "rgba(255,255,255,0.2)" : "rgba(255,255,255,0.6)", borderColor: "rgba(255,255,255,0.12)", background: "rgba(255,255,255,0.04)" }}>
                                  Next →
                                </button>
                              </div>
                            )}
                          </div>

                          {/* Table */}
                          <table style={{ width: "100%", borderCollapse: "collapse" }}>
                            <thead>
                              <tr style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}>
                                <th style={{ padding: "10px 20px", textAlign: "left", fontSize: "11px", fontWeight: 700, color: "rgba(255,255,255,0.35)", textTransform: "uppercase", letterSpacing: "0.07em", width: "40%" }}>Name</th>
                                <th style={{ padding: "10px 20px", textAlign: "left", fontSize: "11px", fontWeight: 700, color: "rgba(255,255,255,0.35)", textTransform: "uppercase", letterSpacing: "0.07em", width: "15%" }}>Status</th>
                                <th style={{ padding: "10px 20px", textAlign: "left", fontSize: "11px", fontWeight: 700, color: "rgba(255,255,255,0.35)", textTransform: "uppercase", letterSpacing: "0.07em", width: "20%" }}>Deleted</th>
                                {isMasterAdmin && <th style={{ padding: "10px 20px", textAlign: "right", fontSize: "11px", fontWeight: 700, color: "rgba(255,255,255,0.35)", textTransform: "uppercase", letterSpacing: "0.07em", width: "25%" }}>Actions</th>}
                              </tr>
                            </thead>
                            <tbody>
                              {pageItems.map((record, idx) => {
                                const displayName = String(record.name || record.title || record.full_name || record.licence_name || record.fiscal_year || record.reference || record.id);
                                const deletedAt = record.deleted_at || record.updated_at;
                                const isLast = idx === pageItems.length - 1;
                                return (
                                  <tr key={String(record.id)} style={{ borderBottom: isLast ? "none" : "1px solid rgba(255,255,255,0.04)" }}>
                                    <td style={{ padding: "13px 20px", fontSize: "13px", fontWeight: 500, color: "rgba(255,255,255,0.85)" }}>{displayName}</td>
                                    <td style={{ padding: "13px 20px" }}>
                                      <span className="status-badge status-badge--rejected" style={{ fontSize: "10px" }}>
                                        {String(record.status || "—").toUpperCase()}
                                      </span>
                                    </td>
                                    <td style={{ padding: "13px 20px", fontSize: "12px", color: "rgba(255,255,255,0.4)" }}>
                                      {deletedAt ? new Date(String(deletedAt)).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—"}
                                    </td>
                                    {isMasterAdmin && (
                                      <td style={{ padding: "13px 20px" }}>
                                        <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
                                          <button type="button"
                                            style={{ ...btnBase, color: "var(--brand-teal)", borderColor: "rgba(49,195,234,0.35)", background: "rgba(49,195,234,0.08)", fontWeight: "700" }}
                                            onClick={() => handleRestore(String(record.module), record.id)}>
                                            <Icon name="reopen" /> Restore
                                          </button>
                                          <button type="button"
                                            style={{ ...btnBase, color: "var(--brand-danger)", borderColor: "rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.08)" }}
                                            onClick={() => handleDeleteForever(String(record.module), record.id)}>
                                            <Icon name="reject" /> Delete Forever
                                          </button>
                                        </div>
                                      </td>
                                    )}
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>

                          {/* Footer page info */}
                          {totalPages > 1 && (
                            <div style={{ padding: "10px 20px", borderTop: "1px solid rgba(255,255,255,0.06)", background: "rgba(255,255,255,0.02)", fontSize: "11px", color: "rgba(255,255,255,0.3)", textAlign: "right" }}>
                              Showing {safePage * PAGE_SIZE + 1}–{Math.min((safePage + 1) * PAGE_SIZE, items.length)} of {items.length}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                );
              })()}

              {moduleKey !== "budgets" && moduleKey !== "employees" && moduleKey !== "renewals" && moduleKey !== "recycle-bin" && (
              <div className="table-wrap">
              <table className="module-table">
                <thead>
                  <tr>
                    {config.tableFields.map((field) => (
                      <th key={field}>{field.replaceAll("_", " ")}</th>
                    ))}
                    {showActions ? <th>Action</th> : null}
                    {moduleKey === "vendors" ? <th>Subscriptions</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {scopedRecords.map((record) => {
                    const isVendorExpanded = moduleKey === "vendors" && catalogueVendorId === String(record.id);
                    const vendorCatalogueItems = moduleKey === "vendors" ? vendorCatalogue.filter((c) => String(c.vendor_id) === String(record.id)) : [];
                    return (<Fragment key={String(record.id)}>
                    <tr>
                      {config.tableFields.map((field) => {
                        if (field === "status") {
                          const statusVal = String(record[field] || "");
                          const lowerVal = statusVal.toLowerCase().replace(/_/g, "-");
                          const label = statusVal.toUpperCase().replace(/_/g, " ");
                          let statusClass = "status-badge";
                          if (lowerVal === "master-approved" || lowerVal.includes("master-approved")) {
                            statusClass += " status-badge--master-approved";
                          } else if (lowerVal.includes("approved")) {
                            statusClass += " status-badge--approved";
                          } else if (lowerVal.includes("rejected")) {
                            statusClass += " status-badge--rejected";
                          } else if (lowerVal.includes("submitted")) {
                            statusClass += " status-badge--submitted";
                          } else if (lowerVal.includes("reopened")) {
                            statusClass += " status-badge--reopened";
                          } else if (lowerVal.includes("completed")) {
                            statusClass += " status-badge--completed";
                          }
                          const assignedEmp = moduleKey === "licences" && record.assigned_to_person_id
                            ? allEmployees.find((e) => String(e.id) === String(record.assigned_to_person_id))
                            : null;
                          return (
                            <td key={field}>
                              <span className={statusClass}>{label}</span>
                              {assignedEmp && (
                                <span style={{ display: "block", fontSize: "11px", color: "var(--brand-cyan)", marginTop: "4px", fontWeight: 600 }}>
                                  {String(assignedEmp.full_name || assignedEmp.work_email || "")}
                                </span>
                              )}
                            </td>
                          );
                        }
                        if (field === "created_at" || field.endsWith("_at")) {
                          const val = record[field];
                          if (!val) return <td key={field}>-</td>;
                          const formatted = new Date(String(val)).toLocaleString("en-US", {
                            month: "short",
                            day: "numeric",
                            year: "numeric",
                            hour: "numeric",
                            minute: "2-digit",
                          });
                          return <td key={field}>{formatted}</td>;
                        }
                        if (moduleKey === "users" && field === "roles") {
                          const roles = Array.isArray(record.roles) ? (record.roles as string[]) : [];
                          return (
                            <td key={field}>
                              {roles.length ? (
                                roles.map((role) => (
                                  <span className="user-role-badge" key={role}>
                                    {formatRoleLabel(role)}
                                  </span>
                                ))
                              ) : (
                                "-"
                              )}
                            </td>
                          );
                        }
                        if (moduleKey === "users" && field === "user_status") {
                          const statusVal = String(record.user_status || "unknown");
                          const lowerVal = statusVal.toLowerCase();
                          const statusClass =
                            lowerVal === "active"
                              ? "status-badge status-badge--approved"
                              : "status-badge status-badge--rejected";
                          return (
                            <td key={field}>
                              <span className={statusClass}>{statusVal.toUpperCase()}</span>
                            </td>
                          );
                        }
                        if (moduleKey === "users" && field === "must_change_password") {
                          return <td key={field}>{record.must_change_password ? "Yes" : "No"}</td>;
                        }
                        return <td key={field}>{formatValue(record[field])}</td>;
                      })}
                      {!showActions ? null : moduleKey === "workflows" ? (
                        <td>
                          <div className="table-actions">
                            <button aria-label="Approve" className="icon-action" disabled={!canRunWorkflowAction(record, "approve")} title="Approve" type="button" onClick={() => handleWorkflowAction(record.id, "approve")}>
                              <Icon name="approve" />
                            </button>
                            <button aria-label="Finance close" className="icon-action" disabled={!canRunWorkflowAction(record, "complete")} title="Finance close" type="button" onClick={() => handleWorkflowAction(record.id, "complete")}>
                              <Icon name="close" />
                            </button>
                            <button aria-label="Reject" className="icon-action danger" disabled={!canRunWorkflowAction(record, "reject")} title="Reject" type="button" onClick={() => handleWorkflowAction(record.id, "reject")}>
                              <Icon name="reject" />
                            </button>
                            <button aria-label="Reopen" className="icon-action" disabled={!canRunWorkflowAction(record, "reopen")} title="Reopen" type="button" onClick={() => handleWorkflowAction(record.id, "reopen")}>
                              <Icon name="reopen" />
                            </button>
                          </div>
                        </td>
                      ) : moduleKey === "recycle-bin" ? (
                        <td>
                          <div className="table-actions">
                            {isMasterAdmin && (
                              <>
                                <button
                                  aria-label="Restore"
                                  className="k-btn"
                                  style={{ color: "var(--brand-teal)", borderColor: "var(--brand-teal)", background: "rgba(49, 195, 234, 0.08)", fontWeight: "700" }}
                                  title="Restore record"
                                  type="button"
                                  onClick={() => handleRestore(String(record.module), record.id)}
                                >
                                  <Icon name="reopen" /> Restore
                                </button>
                                <button
                                  aria-label="Delete Forever"
                                  className="k-btn k-btn--reject"
                                  style={{
                                    color: "var(--brand-danger)",
                                    borderColor: "var(--brand-danger)",
                                    background: "rgba(239, 68, 68, 0.08)",
                                    marginLeft: "8px",
                                  }}
                                  title="Delete permanently"
                                  type="button"
                                  onClick={() => handleDeleteForever(String(record.module), record.id)}
                                >
                                  <Icon name="reject" /> Delete Forever
                                </button>
                              </>
                            )}
                          </div>
                        </td>
                      ) : (
                        <td>
                          <div className="table-actions">
                            {canMutate ? (
                              <>
                                {(moduleKey === "contracts" && record.document_data) ? (
                                  <button
                                    aria-label="Download Document"
                                    className="icon-action"
                                    title="Download attached document"
                                    type="button"
                                    onClick={() => downloadFromUrl(record.document_data as string, (record.document_name as string) || `${record.title}.pdf`)}
                                  >
                                    <Icon name="download" />
                                  </button>
                                ) : !config.readOnly ? (
                                  <button
                                    aria-label="Download record details"
                                    className="icon-action"
                                    title="Download as Excel"
                                    type="button"
                                    onClick={() => {
                                      const label = (record.name || record.title || record.full_name || record.licence_name || record.fiscal_year || record.id) as string;
                                      downloadFromUrl(`${apiBaseUrl}/exports/${moduleKey}/${record.id}.xlsx`, `${moduleKey}-${String(label).replace(/\s+/g, "-")}.xlsx`);
                                    }}
                                  >
                                    <Icon name="download" />
                                  </button>
                                ) : null}
                                <button aria-label="Edit" className="icon-action" title="Edit" type="button" onClick={() => {
  const r = openRecordForEdit(record, moduleKey);
  setEditingRecord(r);
  setEditFormCurrency(String(r.currency_code || ""));
}}>
                                  <Icon name="edit" />
                                </button>
                                {canResetPassword ? (
                                  <button aria-label="Reset temporary password" className="icon-action" title="Reset temporary password" type="button" onClick={() => handleResetPassword(record.id)}>
                                    <Icon name="key" />
                                  </button>
                                ) : null}
                                <button aria-label={moduleKey === "users" ? "Remove user" : "Archive"} className="icon-action danger" title={moduleKey === "users" ? "Remove user" : "Archive"} type="button" onClick={() => handleDelete(record.id)}>
                                  <Icon name="archive" />
                                </button>
                              </>
                            ) : null}
                          </div>
                        </td>
                      )}
                      {moduleKey === "vendors" && (
                        <td>
                          <button
                            type="button"
                            className="k-btn"
                            style={{ whiteSpace: "nowrap" }}
                            onClick={() => {
                              setCatalogueVendorId(isVendorExpanded ? null : String(record.id));
                              setCatalogueAddFields({ name: "", price: "", currency_code: "AED", scrape_url: "" });
                              setCatalogueEditId(null);
                            }}
                          >
                            {isVendorExpanded ? "Hide Subscriptions" : "View Subscriptions"}
                          </button>
                        </td>
                      )}
                    </tr>
                    {/* Inline catalogue expand row */}
                    {isVendorExpanded && (
                      <tr>
                        <td colSpan={config.tableFields.length + (showActions ? 2 : 1)} style={{ padding: 0, background: "rgba(49,195,234,0.04)", borderLeft: "3px solid var(--brand-cyan)" }}>
                          <div style={{ padding: "16px 24px" }}>
                            <p style={{ fontSize: "0.75rem", color: "var(--brand-cyan)", fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", margin: "0 0 12px" }}>
                              Subscriptions — {String(record.name || "")}
                            </p>
                            {vendorCatalogueItems.length === 0 && !catalogueAddFields.name ? (
                              <p style={{ fontSize: "13px", color: "rgba(255,255,255,0.4)", margin: "0 0 12px", fontStyle: "italic" }}>No catalogue entries yet.</p>
                            ) : null}
                            <div style={{ display: "flex", flexDirection: "column", gap: "8px", marginBottom: "14px" }}>
                              {vendorCatalogueItems.map((item) => {
                                const isEditing = catalogueEditId === String(item.id);
                                const inStyle: React.CSSProperties = { background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.15)", borderRadius: "6px", color: "#fff", padding: "5px 8px", fontSize: "13px" };
                                return (
                                  <div key={String(item.id)} style={{ display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap", padding: "8px 12px", background: "rgba(255,255,255,0.04)", borderRadius: "8px", border: "1px solid rgba(255,255,255,0.07)" }}>
                                    {isEditing ? (
                                      <div style={{ display: "flex", flexDirection: "column", gap: "8px", width: "100%" }}>
                                        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                                          <input style={{ ...inStyle, flex: 2, minWidth: "120px" }} placeholder="Name" value={catalogueEditFields.name} onChange={(e) => setCatalogueEditFields((p) => ({ ...p, name: e.target.value }))} />
                                          <input type="number" style={{ ...inStyle, width: "90px" }} placeholder="Price" value={catalogueEditFields.price} onChange={(e) => setCatalogueEditFields((p) => ({ ...p, price: e.target.value }))} />
                                          <select style={{ ...inStyle }} value={catalogueEditFields.currency_code} onChange={(e) => setCatalogueEditFields((p) => ({ ...p, currency_code: e.target.value }))}>
                                            {["AED","USD","GBP","EUR","INR","SAR","QAR","KWD"].map((c) => <option key={c} value={c}>{c}</option>)}
                                          </select>
                                        </div>
                                        <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                                          <input style={{ ...inStyle, flex: 1, minWidth: "200px" }} placeholder="Pricing URL (e.g. https://figma.com/pricing)" value={catalogueEditFields.scrape_url} onChange={(e) => setCatalogueEditFields((p) => ({ ...p, scrape_url: e.target.value }))} />
                                          <button type="button" className="k-btn k-btn--approve" style={{ fontSize: "12px", padding: "4px 12px" }} onClick={async () => {
                                            await fetch(`${apiBaseUrl}/vendor-catalogue/${item.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: catalogueEditFields.name, price: Number(catalogueEditFields.price), currency_code: catalogueEditFields.currency_code, scrape_url: catalogueEditFields.scrape_url || null }) });
                                            setVendorCatalogue(await fetch(`${apiBaseUrl}/vendor-catalogue`).then((r) => r.json()));
                                            setCatalogueEditId(null);
                                          }}>Save</button>
                                          <button type="button" style={{ fontSize: "12px", padding: "4px 10px", background: "none", border: "1px solid rgba(255,255,255,0.15)", borderRadius: "6px", color: "rgba(255,255,255,0.5)", cursor: "pointer" }} onClick={() => setCatalogueEditId(null)}>Cancel</button>
                                        </div>
                                      </div>
                                    ) : (
                                      <>
                                        <span style={{ flex: 2, fontSize: "13px", fontWeight: 600 }}>{String(item.name)}</span>
                                        <span style={{ fontSize: "13px", color: "var(--brand-cyan)", fontWeight: 700 }}>
                                          {Number(item.price).toLocaleString(undefined, { minimumFractionDigits: 2 })} {String(item.currency_code)}<span style={{ fontSize: "11px", color: "rgba(255,255,255,0.4)", fontWeight: 400 }}>/mo</span>
                                          {item.last_scraped_at ? <span style={{ display: "block", fontSize: "10px", color: "rgba(255,255,255,0.3)", fontWeight: 400 }}>verified {new Date(String(item.last_scraped_at)).toLocaleDateString()}</span> : null}
                                        </span>
                                        {item.scrape_url ? (
                                          <a
                                            href={String(item.scrape_url)}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                            title={String(item.scrape_url)}
                                            style={{ display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px", fontWeight: 600, color: "var(--brand-cyan)", background: "rgba(20,184,166,0.1)", border: "1px solid rgba(20,184,166,0.3)", borderRadius: "6px", padding: "4px 10px", textDecoration: "none", whiteSpace: "nowrap" }}
                                          >
                                            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                                            Open Site
                                          </a>
                                        ) : (
                                          <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.25)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: "6px", padding: "4px 10px", whiteSpace: "nowrap" }}>No URL</span>
                                        )}
                                        <button type="button" className="icon-action" title="Edit" onClick={() => { setCatalogueEditId(String(item.id)); setCatalogueEditFields({ name: String(item.name), price: String(item.price), currency_code: String(item.currency_code), scrape_url: String(item.scrape_url || "") }); }}><Icon name="edit" /></button>
                                        <button type="button" className="icon-action danger" title="Delete" onClick={async () => {
                                          await fetch(`${apiBaseUrl}/vendor-catalogue/${item.id}`, { method: "DELETE" });
                                          setVendorCatalogue(await fetch(`${apiBaseUrl}/vendor-catalogue`).then((r) => r.json()));
                                        }}><Icon name="archive" /></button>
                                      </>
                                    )}
                                  </div>
                                );
                              })}
                            </div>
                            {/* Add new entry */}
                            {(() => {
                              const inpStyle: React.CSSProperties = { background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.15)", borderRadius: "6px", color: "#fff", padding: "6px 10px", fontSize: "13px" };
                              return (
                                <div style={{ paddingTop: "10px", borderTop: "1px solid rgba(255,255,255,0.07)", display: "flex", flexDirection: "column", gap: "8px" }}>
                                  <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                                    <input placeholder="Subscription name (e.g. Figma Professional)" style={{ ...inpStyle, flex: 2, minWidth: "160px" }} value={catalogueAddFields.name} onChange={(e) => setCatalogueAddFields((p) => ({ ...p, name: e.target.value }))} />
                                    <input type="number" placeholder="Price" style={{ ...inpStyle, width: "110px" }} value={catalogueAddFields.price} onChange={(e) => setCatalogueAddFields((p) => ({ ...p, price: e.target.value }))} />
                                    <select style={{ ...inpStyle, padding: "6px 8px", fontWeight: "700", color: "#ffffff", WebkitTextFillColor: "#ffffff", appearance: "auto" }} value={catalogueAddFields.currency_code} onChange={(e) => setCatalogueAddFields((p) => ({ ...p, currency_code: e.target.value }))}>
                                      {["AED","USD","GBP","EUR","INR","SAR","QAR","KWD"].map((c) => <option key={c} value={c} style={{ fontWeight: "700", color: "#000" }}>{c}</option>)}
                                    </select>
                                  </div>
                                  <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
                                    <input placeholder="Pricing URL (e.g. https://figma.com/pricing)" style={{ ...inpStyle, flex: 1, minWidth: "200px" }} value={catalogueAddFields.scrape_url} onChange={(e) => setCatalogueAddFields((p) => ({ ...p, scrape_url: e.target.value }))} />
                                    <button type="button" className="k-btn" style={{ fontSize: "12px", padding: "5px 14px", whiteSpace: "nowrap" }}
                                      disabled={!catalogueAddFields.scrape_url.trim() || catalogueFetchingAdd}
                                      onClick={async () => {
                                        setCatalogueFetchingAdd(true);
                                        try {
                                          const res = await fetch(`${apiBaseUrl}/vendor-catalogue/fetch-price`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: catalogueAddFields.name, vendor_name: String(record.name || ""), url: catalogueAddFields.scrape_url }) });
                                          if (res.ok) {
                                            const data = await res.json();
                                            setCatalogueAddFields((p) => ({ ...p, price: String(data.original_price ?? data.price), currency_code: data.original_currency ?? data.currency_code }));
                                          } else {
                                            const err = await res.json();
                                            alert(err.detail || "Could not fetch price from that URL.");
                                          }
                                        } finally { setCatalogueFetchingAdd(false); }
                                      }}>{catalogueFetchingAdd ? "Fetching…" : "Fetch Price"}</button>
                                    <button type="button" className="k-btn k-btn--approve" style={{ fontSize: "12px", padding: "5px 14px" }}
                                      disabled={!catalogueAddFields.name.trim() || !catalogueAddFields.price}
                                      onClick={async () => {
                                        await fetch(`${apiBaseUrl}/vendor-catalogue`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vendor_id: String(record.id), name: catalogueAddFields.name, price: Number(catalogueAddFields.price), currency_code: catalogueAddFields.currency_code, scrape_url: catalogueAddFields.scrape_url || null }) });
                                        setVendorCatalogue(await fetch(`${apiBaseUrl}/vendor-catalogue`).then((r) => r.json()));
                                        setCatalogueAddFields({ name: "", price: "", currency_code: "AED", scrape_url: "" });
                                      }}>+ Add</button>
                                  </div>
                                </div>
                              );
                            })()}
                          </div>
                        </td>
                      </tr>
                    )}
                    </Fragment>);
                  })}
                  {!loading && scopedRecords.length === 0 ? (
                    <tr>
                      <td colSpan={config.tableFields.length + (showActions ? 1 : 0)}>No records found.</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
              </div>
              )}
            </div>
          )}
        </section>
      </section>

      {(isModalOpen || editingRecord) && canMutate && !isWorkflowSoftwareCreate ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={(event) => {
            if (event.target === event.currentTarget) {
              closeRecordModal();
            }
          }}
        >
          <form className="modal-panel" onClick={(event) => event.stopPropagation()} onSubmit={editingRecord?.id ? handleEdit : handleCreate}>
            <div className="modal-header">
              <div>
                <p className="eyebrow">{editingRecord?.id ? "Edit" : usesWorkflow ? "Workflow request" : "Direct create"}</p>
                <h2>{editingRecord?.id ? `Edit ${config.title}` : config.title}</h2>
                {moduleKey === "users" && !editingRecord?.id ? (
                  <p style={{ margin: "8px 0 0", color: "rgba(255,255,255,0.62)", fontSize: "13px", maxWidth: "520px" }}>
                    Any work email address works (Outlook, Gmail, or your company domain). A temporary password is generated automatically unless you set one below.
                  </p>
                ) : null}
                {isWorkflowSoftwareCreate ? (
                  <p style={{ margin: "8px 0 0", color: "rgba(255,255,255,0.62)", fontSize: "13px", maxWidth: "520px" }}>
                    Submitting this form creates a workflow request for Master Admin approval. No record is created until the workflow completes.
                  </p>
                ) : null}
              </div>
              <button type="button" onClick={closeRecordModal}>
                Close
              </button>
            </div>

            <div className="modal-form-grid">
              {/* ── Custom subscription request form (workflow only) ── */}
              {isWorkflowSoftwareCreate && moduleKey === "subscriptions" && !editingRecord?.id ? (() => {
                // Live exchange rates fetched from DB (updated daily)
                const FX = fxRates;

                function convertPrice(unitPriceUsd: number, months: number, toCurrency: string): string {
                  const rate = FX[toCurrency] ?? 1;
                  return (unitPriceUsd * months * rate).toFixed(2);
                }

                const selectedVendorId = workflowFormDraft.vendor_id ?? "";
                const catalogueItems = vendorCatalogue.filter((c) => String(c.vendor_id) === selectedVendorId);
                const selectedCatalogueItem = catalogueItems.find((c) => String(c.name) === workflowFormDraft.name);
                // _unit_price_usd stores the catalogue monthly price in USD so we can recompute on currency/month change
                const unitPriceUsd = Number(workflowFormDraft._unit_price_usd || 0);
                const months = Number(workflowFormDraft._duration_months || 12);
                const selectedCurrency = workflowFormDraft.currency_code || "AED";

                const subInputStyle: React.CSSProperties = {
                  background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.15)",
                  borderRadius: "8px", color: "#fff", padding: "8px 12px", fontSize: "13px", width: "100%",
                };

                return (
                  <>
                    {/* Organisation */}
                    <label>
                      <span>Organisation</span>
                      <select style={subInputStyle} required value={workflowFormDraft.organisation_id ?? ""}
                        onChange={(e) => updateWorkflowDraft("organisation_id", e.target.value)}>
                        <option value="">Select organisation</option>
                        {allOrganisations.map((o) => <option key={String(o.id)} value={String(o.id)}>{String(o.name)}</option>)}
                      </select>
                    </label>

                    {/* Vendor — first so catalogue dropdown is scoped */}
                    <label>
                      <span>Vendor</span>
                      <select style={subInputStyle} required value={selectedVendorId}
                        onChange={(e) => {
                          updateWorkflowDraft("vendor_id", e.target.value);
                          updateWorkflowDraft("name", "");
                          updateWorkflowDraft("amount", "");
                          updateWorkflowDraft("_unit_price_usd", "");
                          updateWorkflowDraft("currency_code", "AED");
                        }}>
                        <option value="">Select vendor</option>
                        {allVendors.map((v) => <option key={String(v.id)} value={String(v.id)}>{String(v.name)}</option>)}
                      </select>
                    </label>

                    {/* Software / subscription name — scoped dropdown if catalogue items exist */}
                    <label>
                      <span>Subscription / Software name</span>
                      {catalogueItems.length > 0 ? (
                        <select style={subInputStyle} required value={workflowFormDraft.name ?? ""}
                          onChange={(e) => {
                            const item = catalogueItems.find((c) => String(c.name) === e.target.value);
                            updateWorkflowDraft("name", e.target.value);
                            if (item) {
                              // Treat catalogue price as USD base; catalogue currency_code is 'USD' per our seed
                              const basePriceUsd = Number(item.price) / (FX[String(item.currency_code)] ?? 1);
                              updateWorkflowDraft("_unit_price_usd", String(basePriceUsd));
                              updateWorkflowDraft("amount", convertPrice(basePriceUsd, months, selectedCurrency));
                              updateWorkflowDraft("currency_code", selectedCurrency);
                            } else {
                              updateWorkflowDraft("_unit_price_usd", "");
                              updateWorkflowDraft("amount", "");
                            }
                          }}>
                          <option value="">Select subscription</option>
                          {catalogueItems.map((c) => (
                            <option key={String(c.id)} value={String(c.name)}>
                              {String(c.name)} — {Number(c.price).toLocaleString(undefined, { minimumFractionDigits: 2 })} {String(c.currency_code)}/mo
                            </option>
                          ))}
                          <option value="__other__">Other (enter manually)</option>
                        </select>
                      ) : (
                        <input style={subInputStyle} required placeholder="e.g. Microsoft Teams"
                          value={workflowFormDraft.name ?? ""}
                          onChange={(e) => updateWorkflowDraft("name", e.target.value)} />
                      )}
                    </label>

                    {/* Free-text name if "Other" chosen */}
                    {workflowFormDraft.name === "__other__" && (
                      <label>
                        <span>Enter software name</span>
                        <input style={subInputStyle} required placeholder="Software name"
                          value={workflowFormDraft._custom_name ?? ""}
                          onChange={(e) => {
                            updateWorkflowDraft("_custom_name", e.target.value);
                          }} />
                      </label>
                    )}

                    {/* Duration in months — drives renewal_date and total cost */}
                    <label>
                      <span>Subscription duration</span>
                      <select style={subInputStyle} value={String(months)}
                        onChange={(e) => {
                          const m = Number(e.target.value);
                          updateWorkflowDraft("_duration_months", String(m));
                          const end = new Date();
                          end.setMonth(end.getMonth() + m);
                          updateWorkflowDraft("renewal_date", end.toISOString().split("T")[0]);
                          if (unitPriceUsd) {
                            updateWorkflowDraft("amount", convertPrice(unitPriceUsd, m, selectedCurrency));
                          }
                        }}>
                        {[1,2,3,6,9,12,18,24,36].map((m) => (
                          <option key={m} value={String(m)}>{m} month{m !== 1 ? "s" : ""}</option>
                        ))}
                      </select>
                    </label>

                    {/* Total cost — auto-computed (unit × months × fx), currency changes recompute */}
                    <label>
                      <span>
                        Total cost
                        {selectedCatalogueItem && unitPriceUsd
                          ? ` (${months} month${months !== 1 ? "s" : ""} × ${(unitPriceUsd * (FX[selectedCurrency] ?? 1)).toFixed(2)} ${selectedCurrency}/mo)`
                          : ""}
                      </span>
                      <div style={{ display: "flex", gap: "8px" }}>
                        <input type="number" style={{ ...subInputStyle, flex: 1 }} required placeholder="0.00"
                          value={workflowFormDraft.amount ?? ""}
                          onChange={(e) => {
                            updateWorkflowDraft("amount", e.target.value);
                            updateWorkflowDraft("_unit_price_usd", ""); // manual override clears auto-compute
                          }} />
                        <select style={{ ...subInputStyle, width: "90px" }} value={selectedCurrency}
                          onChange={(e) => {
                            const newCur = e.target.value;
                            updateWorkflowDraft("currency_code", newCur);
                            if (unitPriceUsd) {
                              updateWorkflowDraft("amount", convertPrice(unitPriceUsd, months, newCur));
                            }
                          }}>
                          {["AED","USD","GBP","EUR","INR","SAR","QAR","KWD"].map((c) => <option key={c} value={c}>{c}</option>)}
                        </select>
                      </div>
                    </label>

                    {/* Billing cycle */}
                    <label>
                      <span>Billing cycle</span>
                      <select style={subInputStyle} value={workflowFormDraft.billing_cycle ?? "annual"}
                        onChange={(e) => updateWorkflowDraft("billing_cycle", e.target.value)}>
                        <option value="monthly">Monthly</option>
                        <option value="quarterly">Quarterly</option>
                        <option value="annual">Annual</option>
                      </select>
                    </label>

                    {/* Department */}
                    <label>
                      <span>Department</span>
                      <select style={subInputStyle} value={workflowFormDraft.department ?? ""}
                        onChange={(e) => updateWorkflowDraft("department", e.target.value)}>
                        <option value="">Select department</option>
                        {requestDepartmentOptions.map((d) => (
                          <option key={d} value={d}>{d}</option>
                        ))}
                      </select>
                    </label>

                    {/* Business justification */}
                    <label style={{ gridColumn: "1 / -1" }}>
                      <span>Business justification</span>
                      <textarea rows={3} style={{ ...subInputStyle, resize: "vertical" }}
                        placeholder="Describe how this tool will be used and why it is needed."
                        value={workflowFormDraft.notes ?? ""}
                        onChange={(e) => updateWorkflowDraft("notes", e.target.value)} />
                    </label>
                  </>
                );
              })() : config.createFields.map((field) => {
                const value = editingRecord?.[field.name];
                const sourceRecords =
                  field.source === "roles" && moduleKey === "users"
                    ? (relations[field.source] ?? []).filter((record) => record.can_login !== false)
                    : field.source
                      ? relations[field.source] ?? []
                      : [];

                // Vendor autofill: make fields controlled via vendorDraft when adding a new vendor
                const isVendorAdd = moduleKey === "vendors" && !editingRecord?.id;
                const controlledValue = isWorkflowSoftwareCreate
                  ? workflowFormDraft[field.name] ?? (value ? String(value) : "")
                  : isVendorAdd
                  ? vendorDraft[field.name] ?? (value ? String(value) : "")
                  : undefined;
                const onChangeVendor = isVendorAdd
                  ? (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
                      setVendorDraft((p) => ({ ...p, [field.name]: e.target.value }))
                  : undefined;

                // FX conversion: when editing a record, make the currency_code select controlled
                // so changing it auto-converts the amount input via direct DOM write.
                const amountFieldName = MODULE_AMOUNT_FIELD[moduleKey];
                const isEditMode = !!editingRecord?.id;
                const isCurrencyField = field.name === "currency_code" && !!amountFieldName && isEditMode && !isWorkflowSoftwareCreate && !isVendorAdd;

                return (
                  <Fragment key={field.name}>
                    <label className={field.type === "checkbox" ? "checkbox-field" : ""}>
                      <span>{field.label}</span>
                      {field.type === "textarea" ? (
                        <textarea
                          name={field.name}
                          rows={3}
                          required={field.required}
                          placeholder={field.placeholder}
                          value={(isWorkflowSoftwareCreate || isVendorAdd) ? controlledValue : undefined}
                          defaultValue={!(isWorkflowSoftwareCreate || isVendorAdd) && value ? String(value) : undefined}
                          onChange={
                            isWorkflowSoftwareCreate
                              ? (event) => updateWorkflowDraft(field.name, event.target.value)
                              : onChangeVendor
                          }
                        />
                      ) : field.type === "select" ? (
                        <select
                          name={field.name}
                          required={field.required}
                          value={isCurrencyField ? editFormCurrency : (isWorkflowSoftwareCreate || isVendorAdd) ? controlledValue : undefined}
                          defaultValue={!isCurrencyField && !(isWorkflowSoftwareCreate || isVendorAdd) && value ? String(value) : undefined}
                          onChange={
                            isCurrencyField
                              ? (e) => {
                                  const newCur = e.target.value;
                                  const amtInput = e.target.form?.elements.namedItem(amountFieldName!) as HTMLInputElement | null;
                                  if (amtInput && editFormCurrency && newCur) {
                                    const current = parseFloat(amtInput.value);
                                    if (!isNaN(current)) {
                                      const inUsd = current / (fxRates[editFormCurrency] ?? 1);
                                      amtInput.value = (inUsd * (fxRates[newCur] ?? 1)).toFixed(2);
                                    }
                                  }
                                  setEditFormCurrency(newCur);
                                }
                              : isWorkflowSoftwareCreate
                              ? (event) => updateWorkflowDraft(field.name, event.target.value)
                              : onChangeVendor
                          }
                        >
                          <option value="">Select</option>
                          {field.source
                            ? sourceRecords.map((record) => (
                                <option value={String(field.source === "roles" ? record.code : record.id)} key={String(field.source === "roles" ? record.code : record.id)}>
                                  {recordLabel(record, field.source ?? "")}
                                </option>
                              ))
                            : field.options?.map((option) => (
                                <option value={option} key={option}>
                                  {option}
                                </option>
                              ))}
                        </select>
                      ) : field.type === "checkbox" ? (
                        <input
                          name={field.name}
                          type="checkbox"
                          checked={
                            isWorkflowSoftwareCreate
                              ? workflowFormDraft[field.name] === "true" ||
                                (workflowFormDraft[field.name] === undefined && (typeof value === "boolean" ? value : true))
                              : undefined
                          }
                          defaultChecked={
                            !isWorkflowSoftwareCreate ? (typeof value === "boolean" ? value : true) : undefined
                          }
                          onChange={
                            isWorkflowSoftwareCreate
                              ? (event) => updateWorkflowDraft(field.name, event.target.checked ? "true" : "false")
                              : undefined
                          }
                        />
                      ) : (
                        <input
                          name={field.name}
                          type={field.type ?? "text"}
                          required={field.required}
                          placeholder={field.placeholder}
                          value={(isWorkflowSoftwareCreate || isVendorAdd) ? controlledValue : undefined}
                          defaultValue={!(isWorkflowSoftwareCreate || isVendorAdd) && value ? String(value) : undefined}
                          onChange={
                            isWorkflowSoftwareCreate
                              ? (event) => updateWorkflowDraft(field.name, event.target.value)
                              : onChangeVendor
                          }
                        />
                      )}
                    </label>
                    {/* Autofill button — shown after the name field when adding a vendor */}
                    {isVendorAdd && field.name === "name" && (
                      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "-6px" }}>
                        <button
                          type="button"
                          className="k-btn"
                          style={{ fontSize: "12px", padding: "5px 14px", whiteSpace: "nowrap" }}
                          disabled={!(vendorDraft.name || "").trim() || vendorAutofilling}
                          onClick={async () => {
                            setVendorAutofilling(true);
                            try {
                              const res = await fetch(`${apiBaseUrl}/vendors/autofill`, {
                                method: "POST",
                                headers: { "Content-Type": "application/json" },
                                body: JSON.stringify({ name: vendorDraft.name }),
                              });
                              if (res.ok) {
                                const data = await res.json();
                                if (data.not_found) {
                                  setVendorDraft((p) => ({
                                    ...p,
                                    legal_name: "Not found", website_url: "Not found",
                                    contact_name: "Not found", contact_email: "Not found",
                                  }));
                                } else {
                                  setVendorDraft((p) => ({
                                    ...p,
                                    ...(data.vendor_name  ? { name:          data.vendor_name }  : {}),
                                    legal_name:    data.legal_name    || "",
                                    website_url:   data.website_url   || "",
                                    contact_name:  data.contact_name  || "",
                                    contact_email: data.contact_email || "",
                                  }));
                                }
                              } else {
                                const err = await res.json().catch(() => ({}));
                                alert(err.detail || "Autofill failed — please fill in the details manually.");
                              }
                            } finally { setVendorAutofilling(false); }
                          }}
                        >
                          {vendorAutofilling ? "Looking up…" : "Autofill"}
                        </button>
                        {vendorAutofilling && (
                          <span style={{ fontSize: "12px", color: "rgba(255,255,255,0.4)" }}>Searching online for vendor details…</span>
                        )}
                      </div>
                    )}
                  </Fragment>
                );
              })}
              {isWorkflowSoftwareCreate && softwareToolSummary ? renderSoftwareToolSummary(softwareToolSummary) : null}
              {moduleKey === "contracts" && (
                <label className="file-upload-field" style={{ display: "grid", gap: "7px" }}>
                  <span style={{ color: "rgba(255, 255, 255, 0.72)", fontSize: "13px", fontWeight: "800" }}>Contract Document</span>
                  <div style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "4px" }}>
                    <input
                      type="file"
                      accept=".pdf,.txt,.md"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (file) {
                          const b64 = await fileToBase64(file);
                          setContractDocName(file.name);
                          setContractDocData(`data:${file.type};base64,${b64}`);
                        }
                      }}
                      style={{
                        border: "1px solid rgba(217, 244, 250, 0.18)",
                        borderRadius: "8px",
                        background: "rgba(6, 23, 36, 0.9)",
                        color: "#fff",
                        padding: "8px 12px",
                        outline: "none",
                        width: "auto"
                      }}
                    />
                    {!!(contractDocName || editingRecord?.document_name) && (
                      <span style={{ fontSize: "12px", color: "#94a3b8" }}>
                        Attached: {contractDocName || (editingRecord?.document_name as string)}
                      </span>
                    )}
                  </div>
                </label>
              )}
            </div>

            {isWorkflowSoftwareCreate ? (
              <section className="workflow-email-preview">
                <p className="eyebrow">Notification preview</p>
                <p className="workflow-email-preview-note">
                  Preview only — emails are not sent until the notification service is enabled.
                </p>
                {renderWorkflowEmailPreview()}
              </section>
            ) : null}

            <div className="modal-actions">
              <button type="button" onClick={closeRecordModal}>
                Cancel
              </button>
              <button type="submit">
                {editingRecord?.id ? "Submit update" : usesWorkflow ? "Submit Request" : "Create"}
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {isModalOpen && canSubmitSoftwareWorkflowRequest && isWorkflowSoftwareCreate ? (
        <WorkflowSubmissionModal
          moduleKey={moduleKey}
          title={config.title}
          user={user ?? undefined}
          selectedOrgId={selectedOrgId}
          allowedWorkflowTypes={allowedSoftwareWorkflowTypes}
          organisations={allOrganisations}
          vendors={scopedVendors}
          subscriptions={scopedSubscriptions}
          employees={scopedEmployees}
          lineManagers={allUsers.filter((u) => Array.isArray(u.roles) && (u.roles as string[]).includes("line_manager"))}
          licences={allLicences}
          budgets={allBudgets}
          vendorCatalogue={vendorCatalogue}
          fxRates={fxRates}
          roleEmails={roleMailboxEmails}
          apiBaseUrl={apiBaseUrl}
          onClose={closeRecordModal}
          onSubmit={(payload, workflowType, emailOverrides) =>
            submitWorkflowRequest(payload, workflowType, emailOverrides)
          }
        />
      ) : null}

      {extractOpen && (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!extracting) setExtractOpen(false);
          }}
        >
          <div
            className="modal-panel"
            onClick={(event) => event.stopPropagation()}
            style={{ width: "min(600px, 95vw)" }}
          >
            <div className="modal-header">
              <div>
                <p className="eyebrow">AI Extraction</p>
                <h2>Extract contract with AI</h2>
              </div>
              <button
                type="button"
                onClick={() => setExtractOpen(false)}
                disabled={extracting}
              >
                Close
              </button>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: "16px", padding: "20px 0" }}>
              <p style={{ fontSize: "13px", color: "#94a3b8", margin: 0 }}>
                Attach a contract (PDF or text) or paste its text. Gemini reads it and pre-fills a new contract record for you to review before saving.
              </p>

              {extractHealthChecked && !extractHealth && (
                <div style={{ border: "1px solid rgba(245, 158, 11, 0.3)", background: "rgba(245, 158, 11, 0.1)", color: "#fde047", padding: "8px 12px", borderRadius: "6px", fontSize: "13px" }}>
                  Extraction service isn’t reachable. Make sure the API backend is running.
                </div>
              )}
              {extractHealthChecked && extractHealth && !extractHealth.configured && (
                <div style={{ border: "1px solid rgba(245, 158, 11, 0.3)", background: "rgba(245, 158, 11, 0.1)", color: "#fde047", padding: "8px 12px", borderRadius: "6px", fontSize: "13px" }}>
                  No Gemini API key configured on the server. Please add your API key.
                </div>
              )}
              {extractHealthChecked && extractHealth?.configured && (
                <div style={{ border: "1px solid rgba(20, 184, 166, 0.3)", background: "rgba(20, 184, 166, 0.1)", color: "#14b8a6", padding: "8px 12px", borderRadius: "6px", fontSize: "13px" }}>
                  Connected · using Gemini.
                </div>
              )}

              <div>
                <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                  <label
                    style={{
                      border: "1px solid rgba(49, 195, 234, 0.28)",
                      background: "rgba(49, 195, 234, 0.14)",
                      color: "#fff",
                      cursor: "pointer",
                      fontWeight: "bold",
                      padding: "8px 12px",
                      borderRadius: "8px",
                      fontSize: "13px",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "6px"
                    }}
                  >
                    <svg style={{ height: "14px", width: "14px" }} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                    </svg>
                    <span>Attach document</span>
                    <input
                      type="file"
                      accept=".pdf,.txt,.md"
                      style={{ display: "none" }}
                      onChange={async (e) => {
                        const f = e.target.files?.[0];
                        if (!f) return;
                        e.target.value = "";
                        if (f.size > 8_000_000) {
                          setToast("File over 8MB — paste the text instead.");
                          return;
                        }
                        const b64 = await fileToBase64(f);
                        setExtractDoc({ name: f.name, dataUrl: `data:${f.type};base64,${b64}` });
                      }}
                    />
                  </label>
                  <span style={{ fontSize: "13px", color: "#94a3b8", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "200px" }}>
                    {extractDoc ? extractDoc.name : "No file selected"}
                  </span>
                </div>
                <p style={{ fontSize: "11px", color: "#64748b", marginTop: "4px", margin: "4px 0 0" }}>
                  Supports PDF, plain text, and Markdown files.
                </p>
              </div>

              <label style={{ display: "grid", gap: "6px" }}>
                <span style={{ fontSize: "13px", fontWeight: "bold", color: "rgba(255, 255, 255, 0.72)" }}>Or paste contract text</span>
                <textarea
                  style={{
                    width: "100%",
                    minHeight: "140px",
                    borderRadius: "8px",
                    border: "1px solid rgba(217, 244, 250, 0.18)",
                    background: "rgba(6, 23, 36, 0.9)",
                    color: "#fff",
                    padding: "10px",
                    outline: "none",
                    fontFamily: "inherit"
                  }}
                  value={extractText}
                  onChange={(e) => setExtractText(e.target.value)}
                  placeholder="Paste the contract text here..."
                />
              </label>
            </div>

            <div className="modal-actions">
              <button
                type="button"
                onClick={() => setExtractOpen(false)}
                disabled={extracting}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleRunExtract}
                disabled={extracting || (extractHealthChecked && !extractHealth?.configured)}
                style={{
                  background: "var(--brand-blue)",
                  borderColor: "var(--brand-blue)"
                }}
              >
                {extracting ? "Extracting..." : "Extract"}
              </button>
            </div>
          </div>
        </div>
      )}

      {renderPurchaseConfirmModal()}

      {renderWorkflowDetailsModal()}

      {renderOffboardingConfirmModal()}

      {renderFinanceConfirmModal()}


    </AppShell>
  );
}
