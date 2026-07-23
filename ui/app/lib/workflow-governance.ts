export const WORKFLOW_TYPES = {
  employee_software_request: "Employee Software Request",
  new_subscription_request: "New Subscription Request",
  license_assignment_request: "License Assignment Request",
  hr_onboarding_request: "HR Onboarding — Licence Request",
  renewal_request: "Renewal Request",
  generic_procurement: "Procurement Workflow",
  employee_offboarding: "Employee Offboarding",
} as const;

export type WorkflowType = keyof typeof WORKFLOW_TYPES;

export const SUBSCRIPTION_REQUEST_TYPES: WorkflowType[] = [
  "new_subscription_request",
  "renewal_request",
];

export function resolveWorkflowType(
  moduleKey: string,
  payload: Record<string, unknown> = {},
  explicit?: string
): WorkflowType {
  if (explicit && explicit in WORKFLOW_TYPES) {
    return explicit as WorkflowType;
  }
  if (typeof payload.workflow_type === "string" && payload.workflow_type in WORKFLOW_TYPES) {
    return payload.workflow_type as WorkflowType;
  }
  if (moduleKey === "renewals") {
    return "renewal_request";
  }
  if (moduleKey === "subscriptions") {
    return "new_subscription_request";
  }
  if (moduleKey === "licences") {
    return "license_assignment_request";
  }
  if (moduleKey === "employees" || payload.tool_requested || payload.business_justification) {
    return "employee_software_request";
  }
  return "generic_procurement";
}

export function workflowTypeLabel(value: unknown): string {
  const key = String(value || "generic_procurement") as WorkflowType;
  return WORKFLOW_TYPES[key] || "Procurement Workflow";
}

export function requiresLineManager(workflowType: string): boolean {
  return workflowType === "employee_software_request";
}

export function canLineManagerApprove(roles: string[] = []): boolean {
  return roles.includes("master_admin") || roles.includes("line_manager");
}

export type RoleDashboardSection = {
  title: string;
  description: string;
  items: string[];
};

export function getRoleDashboardSections(roles: string[] = []): RoleDashboardSection[] {
  if (roles.includes("master_admin")) {
    return [
      {
        title: "Master Admin Overview",
        description: "Full governance visibility across all workflows and modules.",
        items: ["All workflows", "Direct subscription/license create", "Override approvals"],
      },
    ];
  }
  if (roles.includes("finance")) {
    return [
      {
        title: "Finance Dashboard",
        description: "Budget validation, procurement queue, and payment tracking.",
        items: ["Budget utilization", "Pending approvals", "Procurement queue", "Payments"],
      },
    ];
  }
  if (roles.includes("line_manager")) {
    return [
      {
        title: "Line Manager Dashboard",
        description: "Review employee software requests from your team.",
        items: ["Pending approvals", "Team requests"],
      },
    ];
  }
  if (roles.includes("it_admin")) {
    return [
      {
        title: "IT Dashboard",
        description: "Software procurement, subscriptions, licenses, and vendors.",
        items: ["Tool requests", "Subscriptions", "Licenses", "Vendor management"],
      },
    ];
  }
  if (roles.includes("hr_admin")) {
    return [
      {
        title: "HR Dashboard",
        description: "Employee records and licence assignment workflows.",
        items: ["Employee records", "Licence assignment requests"],
      },
    ];
  }
  if (roles.includes("auditor")) {
    return [
      {
        title: "Auditor Dashboard",
        description: "Read-only visibility into reports, workflows, and audit history.",
        items: ["Reports", "Audit logs", "Workflow history"],
      },
    ];
  }
  return [
    {
      title: "My Workspace",
      description: "Track your requests and operational updates.",
      items: ["My requests", "Current status", "Recent notifications"],
    },
  ];
}
