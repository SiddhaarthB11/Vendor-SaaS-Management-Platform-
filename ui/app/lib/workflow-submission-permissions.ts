import { WorkflowType } from "./workflow-governance";

const IT_SUBSCRIPTION_TYPES: WorkflowType[] = [
  "new_subscription_request",
  "renewal_request",
];

const IT_LICENCE_TYPES: WorkflowType[] = ["license_assignment_request"];

const HR_LICENCE_TYPES: WorkflowType[] = ["license_assignment_request", "hr_onboarding_request"];

const HR_OFFBOARDING_TYPES: WorkflowType[] = ["employee_offboarding"];
const IT_OFFBOARDING_TYPES: WorkflowType[] = ["employee_offboarding"];
const MASTER_OFFBOARDING_TYPES: WorkflowType[] = ["employee_offboarding"];

const EMPLOYEE_SUBSCRIPTION_TYPES: WorkflowType[] = ["employee_software_request"];

const MASTER_SUBSCRIPTION_TYPES: WorkflowType[] = [
  "employee_software_request",
  "new_subscription_request",
  "renewal_request",
];

const MASTER_LICENCE_TYPES: WorkflowType[] = ["license_assignment_request"];

export function canDirectCreateSoftwareRecords(roles: string[] = []): boolean {
  return roles.includes("master_admin");
}

export function getAllowedWorkflowTypes(
  moduleKey: string,
  roles: string[] = [],
  options?: { masterUsesWorkflow?: boolean }
): WorkflowType[] {
  if (moduleKey === "employees") {
    if (roles.includes("master_admin")) return MASTER_OFFBOARDING_TYPES;
    if (roles.includes("hr_admin")) return HR_OFFBOARDING_TYPES;
    if (roles.includes("it_admin")) return IT_OFFBOARDING_TYPES;
    return [];
  }
  if (!["subscriptions", "licences"].includes(moduleKey)) return [];
  if (roles.includes("master_admin")) {
    if (options?.masterUsesWorkflow) {
      return moduleKey === "subscriptions" ? MASTER_SUBSCRIPTION_TYPES : MASTER_LICENCE_TYPES;
    }
    return [];
  }
  if (roles.includes("employee")) {
    return moduleKey === "subscriptions" ? EMPLOYEE_SUBSCRIPTION_TYPES : [];
  }
  if (roles.includes("it_admin")) {
    return moduleKey === "subscriptions" ? IT_SUBSCRIPTION_TYPES : IT_LICENCE_TYPES;
  }
  if (roles.includes("hr_admin")) {
    return moduleKey === "licences" ? HR_LICENCE_TYPES : [];
  }
  return [];
}

export function canSubmitSoftwareWorkflow(
  moduleKey: string,
  roles: string[] = [],
  options?: { masterUsesWorkflow?: boolean }
): boolean {
  return getAllowedWorkflowTypes(moduleKey, roles, options).length > 0;
}

export function canSubmitWorkflowType(
  moduleKey: string,
  roles: string[] = [],
  workflowType: WorkflowType,
  options?: { masterUsesWorkflow?: boolean }
): boolean {
  return getAllowedWorkflowTypes(moduleKey, roles, options).includes(workflowType);
}

export function getDefaultWorkflowType(
  moduleKey: string,
  roles: string[] = [],
  options?: { masterUsesWorkflow?: boolean }
): WorkflowType | null {
  const allowed = getAllowedWorkflowTypes(moduleKey, roles, options);
  return allowed[0] ?? null;
}

export function canSubmitEmployeeSoftwareRequest(roles: string[] = []): boolean {
  return roles.includes("employee");
}

export function canManageToolRequestInbox(roles: string[] = []): boolean {
  return roles.includes("master_admin") || roles.includes("it_admin");
}

export function getPrimaryRoleLabel(roles: string[] = []): string {
  if (roles.includes("master_admin")) return "Master Admin";
  if (roles.includes("it_admin")) return "IT Admin";
  if (roles.includes("hr_admin")) return "HR Admin";
  if (roles.includes("finance")) return "Finance";
  if (roles.includes("line_manager")) return "Line Manager";
  if (roles.includes("employee")) return "Employee";
  if (roles.includes("auditor")) return "Auditor";
  return roles[0] || "User";
}
