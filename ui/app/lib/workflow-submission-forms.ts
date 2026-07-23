import {
  WorkflowType,
  WORKFLOW_TYPES,
  workflowTypeLabel,
} from "./workflow-governance";

export type SubmissionField = {
  name: string;
  label: string;
  type?: "text" | "email" | "number" | "date" | "textarea" | "select";
  required?: boolean;
  placeholder?: string;
  helpText?: string;
  source?: "organisations" | "vendors" | "subscriptions" | "employees" | "line_managers";
  options?: string[];
  section: "request" | "details" | "justification";
  readOnly?: boolean;
  wide?: boolean;
};

export type SubmissionSection = {
  id: "request" | "details" | "justification";
  title: string;
  description: string;
  step: number;
};

export const SUBMISSION_SECTIONS: SubmissionSection[] = [
  {
    id: "request",
    step: 1,
    title: "Request Information",
    description: "Tell us who is submitting this request and which organisation it belongs to.",
  },
  {
    id: "details",
    step: 2,
    title: "Software / Licence Details",
    description: "Provide the product, subscription, or licence information needed to process your request.",
  },
  {
    id: "justification",
    step: 3,
    title: "Business Need",
    description: "Help approvers understand why this software is required and how it will be used.",
  },
];

const FIELD_HELP: Record<string, string> = {
  workflow_type: "Choose the type of governance request that matches what you need.",
  request_type: "The workflow category for this submission.",
  requester_name: "Your full name as the person submitting this request.",
  requester_email: "We will send workflow updates to this email address.",
  requester_role: "Your role determines which requests you can submit.",
  organisation_id: "Select the organisation this request applies to.",
  department: "The team or department that will use or pay for this software.",
  vendor_id: "The software vendor or publisher, if known.",
  name: "The subscription or product name as it should appear in the catalogue.",
  tool_requested: "Select or enter the software required for your work.",
  category: "Software category such as Collaboration, Security, or Design.",
  subscription_id: "Select the existing subscription this request relates to.",
  assigned_to_person_id: "The employee who will receive the licence or access.",
  licence_name: "A descriptive name for this licence assignment.",
  why_needed: "Describe the business need, who will use it, expected benefit, and any urgency or constraints.",
  urgency: "How quickly this request needs to be fulfilled.",
  amount: "Estimated cost for budgeting and finance approval.",
  currency_code: "Currency for the estimated or actual cost.",
  required_tools: "List the tools the new employee needs on day one.",
};

function applyFieldHelp(fields: SubmissionField[]): SubmissionField[] {
  return fields.map((field) => ({
    ...field,
    helpText: field.helpText || FIELD_HELP[field.name],
    wide:
      field.wide ??
      (field.type === "textarea" ||
        ["name", "tool_requested", "licence_name", "notes"].includes(field.name)),
  }));
}

const departmentOptions = [
  "Software Engineering",
  "Human Resources",
  "Finance & Accounts",
  "Marketing",
  "Sales",
  "IT",
  "Product",
  "Operations",
];

const currencyOptions = ["AED", "INR", "GBP", "USD", "EUR", "SAR", "QAR"];
const urgencyOptions = ["standard", "high", "critical"];

function requestInformationFields(
  moduleKey: string,
  workflowType: WorkflowType,
  allowedWorkflowTypes: WorkflowType[]
): SubmissionField[] {
  const typeOptions = allowedWorkflowTypes.length ? allowedWorkflowTypes : [workflowType];
  const showTypeSelect = ["subscriptions", "licences"].includes(moduleKey) && typeOptions.length > 1;

  return [
    showTypeSelect
      ? {
          name: "workflow_type",
          label: "Request Type",
          type: "select",
          options: typeOptions,
          section: "request",
          required: true,
        }
      : {
          name: "request_type",
          label: "Request Type",
          type: "text",
          section: "request",
          required: true,
          readOnly: true,
        },
    {
      name: "requester_name",
      label: "Requester Name",
      type: "text",
      section: "request",
      required: true,
    },
    {
      name: "requester_email",
      label: "Requester Email",
      type: "email",
      section: "request",
      required: true,
    },
    {
      name: "requester_role",
      label: "Requester Role",
      type: "text",
      section: "request",
      required: true,
      readOnly: true,
    },
    {
      name: "organisation_id",
      label: "Organisation",
      type: "select",
      source: "organisations",
      section: "request",
      required: true,
    },
    {
      name: "department",
      label: "Department",
      type: "select",
      options: departmentOptions,
      section: "request",
      required: true,
    },
  ];
}

const businessJustificationFields: SubmissionField[] = [
  {
    name: "why_needed",
    label: "Why do you need this?",
    type: "textarea",
    section: "justification",
    required: true,
    placeholder: "Describe the business need, who will use it, expected benefit, and any urgency or constraints.",
    wide: true,
    helpText: "Include the problem you're solving, who benefits, and any relevant deadlines or alternatives you've considered.",
  },
  {
    name: "urgency",
    label: "Urgency",
    type: "select",
    options: urgencyOptions,
    section: "justification",
    required: true,
  },
];

const subscriptionDetailFields: SubmissionField[] = [
  { name: "vendor_id", label: "Vendor", type: "select", source: "vendors", section: "details" },
  { name: "name", label: "Software Name", type: "text", section: "details", required: true, placeholder: "e.g. Slack, Notion, Adobe Creative Cloud", helpText: "The subscription or product name as it should appear in the catalogue." },
  { name: "quantity", label: "Number of Seats", type: "number", section: "details", required: true, placeholder: "e.g. 5", helpText: "How many seats are being purchased. Each seat will appear as an available licence ready to assign to employees." },
  { name: "_duration_months", label: "Subscription Duration", type: "select", options: ["1","2","3","6","9","12","18","24","36"], section: "details" },
  {
    name: "billing_cycle",
    label: "Billing Cycle",
    type: "select",
    options: ["monthly", "quarterly", "annual"],
    section: "details",
  },
  { name: "amount", label: "Estimated Cost", type: "number", section: "details", helpText: "Estimated cost for budgeting and finance approval." },
  {
    name: "currency_code",
    label: "Currency",
    type: "select",
    options: currencyOptions,
    section: "details",
    helpText: "Currency for the estimated or actual cost.",
  },
];

const licenceDetailFields: SubmissionField[] = [
  {
    name: "organisation_id",
    label: "Organisation",
    type: "select",
    source: "organisations",
    section: "details",
    required: true,
  },
  {
    name: "subscription_id",
    label: "Subscription",
    type: "select",
    source: "subscriptions",
    section: "details",
    required: true,
  },
  {
    name: "assigned_to_person_id",
    label: "Assigned Employee",
    type: "select",
    source: "employees",
    section: "details",
    required: true,
  },
  { name: "licence_name", label: "Licence Name", type: "text", section: "details", required: true },
  { name: "assigned_at", label: "Assigned Date", type: "date", section: "details" },
  { name: "_duration_months", label: "Licence Duration", type: "select", options: ["1","2","3","6","9","12","18","24","36"], section: "details" },
];

export function getSubmissionFields(
  moduleKey: string,
  workflowType: WorkflowType,
  allowedWorkflowTypes: WorkflowType[] = [],
  requesterRole = "User"
): SubmissionField[] {
  const requestFields = requestInformationFields(moduleKey, workflowType, allowedWorkflowTypes);


  if (workflowType === "renewal_request") {
    return applyFieldHelp([
      { name: "request_type", label: "Request Type", type: "text", section: "request", required: true, readOnly: true },
      { name: "requester_name", label: "Requester Name", type: "text", section: "request", required: true },
      { name: "requester_email", label: "Requester Email", type: "email", section: "request", required: true },
      { name: "organisation_id", label: "Organisation", type: "select", source: "organisations", section: "request", required: true },
      { name: "subscription_id", label: "Subscription", type: "select", source: "subscriptions", section: "details", required: true },
      { name: "vendor_id", label: "Vendor", type: "select", source: "vendors", section: "details" },
      { name: "renewal_date", label: "Renewal Date", type: "date", section: "details", required: true },
      { name: "amount", label: "Renewal Cost", type: "number", section: "details", required: true },
      { name: "currency_code", label: "Currency", type: "select", options: currencyOptions, section: "details", required: true },
      { name: "notes", label: "Notes (optional)", type: "textarea", section: "justification", wide: true },
    ]);
  }

  if (workflowType === "employee_offboarding") {
    return applyFieldHelp([
      ...requestFields.filter((f) => f.name !== "department"),
      {
        name: "offboarded_employee_name",
        label: "Departing Employee",
        type: "select",
        source: "employees",
        section: "details",
        required: true,
      },
      {
        name: "offboarded_employee_email",
        label: "Departing Employee Email",
        type: "email",
        section: "details",
        required: true,
        readOnly: true,
        helpText: "Auto-filled from the selected employee. All active licences for this email will be revoked on completion.",
      },
      {
        name: "last_working_date",
        label: "Last Working Date",
        type: "date",
        section: "details",
        required: true,
      },
      {
        name: "justification_notes",
        label: "Notes",
        type: "textarea",
        section: "justification",
        placeholder: "Add any handover notes or additional context.",
        wide: true,
      },
    ]);
  }

  if (workflowType === "hr_onboarding_request") {
    return applyFieldHelp([
      ...requestFields,
      // New employee details
      { name: "new_employee_full_name", label: "New Employee Full Name", type: "text", section: "details", required: true, placeholder: "e.g. John Smith" },
      { name: "new_employee_email", label: "New Employee Work Email", type: "email", section: "details", required: true, placeholder: "e.g. john.smith@company.com" },
      { name: "new_employee_job_title", label: "Job Title", type: "text", section: "details", required: true, placeholder: "e.g. Software Engineer" },
      { name: "new_employee_line_manager_email", label: "Line Manager", type: "select", source: "line_managers", section: "details", required: true, helpText: "The line manager who will approve this employee's future software requests." },
      { name: "new_employee_temp_password", label: "Temporary Portal Password", type: "text", section: "details", required: true, placeholder: "They will be asked to change this on first login", helpText: "A temporary password for the employee's first login to the portal. They will be prompted to change it." },
      // Licence to assign
      {
        name: "subscription_id",
        label: "Subscription to Assign Licence From",
        type: "select",
        source: "subscriptions",
        section: "details",
        required: true,
      },
      { name: "licence_name", label: "Licence Name", type: "text", section: "details", required: true },
      { name: "assigned_at", label: "Start Date", type: "date", section: "details" },
      { name: "_duration_months", label: "Licence Duration", type: "select", options: ["1","2","3","6","9","12","18","24","36"], section: "details" },
      ...businessJustificationFields,
    ]);
  }

  if (moduleKey === "licences" || workflowType === "license_assignment_request") {
    return applyFieldHelp([...requestFields, ...licenceDetailFields, ...businessJustificationFields]);
  }

  if (workflowType === "employee_software_request") {
    // Employee requests are always 1 seat — hide the quantity field
    return applyFieldHelp([
      ...requestFields,
      ...subscriptionDetailFields.filter((f) => f.name !== "quantity"),
      ...businessJustificationFields,
    ]);
  }

  return applyFieldHelp([...requestFields, ...subscriptionDetailFields, ...businessJustificationFields]);

}

export function buildDefaultSubmissionDraft(
  moduleKey: string,
  workflowType: WorkflowType,
  user?: { name?: string; email?: string },
  organisationId?: string,
  requesterRole = "User"
): Record<string, string> {
  const defaultRenewal = (() => {
    const d = new Date();
    d.setMonth(d.getMonth() + 12);
    return d.toISOString().split("T")[0];
  })();
  const isSubNew = workflowType === "employee_software_request" || workflowType === "new_subscription_request";
  const isLicenceRequest = workflowType === "license_assignment_request" || workflowType === "hr_onboarding_request";
  return {
    workflow_type: workflowType,
    request_type: workflowTypeLabel(workflowType),
    requester_name: user?.name || "",
    requester_email: user?.email || "",
    requester_role: requesterRole,
    organisation_id: organisationId || "",
    department: "",
    why_needed: "",
    urgency: "standard",
    ...(isSubNew ? { _duration_months: "12", renewal_date: defaultRenewal, currency_code: "AED", billing_cycle: "annual" } : {}),
    ...(isLicenceRequest ? { _duration_months: "12", expires_at: defaultRenewal } : {}),
  };
}

export function getSubmissionFormHeading(
  _moduleKey: string,
  workflowType: WorkflowType
): { title: string; description: string } {
  const descriptions: Record<WorkflowType, string> = {
    new_subscription_request:
      "Submit this form to request approval for a new software subscription. No subscription record will be created until the workflow is approved.",
    license_assignment_request:
      "Submit this form to request a licence assignment. No licence record will be created until approvers sign off.",
    hr_onboarding_request:
      "Submit this form to request a software licence for a new employee being onboarded. Finance will validate the budget, then IT will activate the licence. The employee will be notified automatically on completion.",
    employee_software_request:
      "Submit this form to request software access for your work. Your line manager, Finance, and IT will review before access is granted.",
    renewal_request:
      "Submit this form to request renewal approval for an existing subscription.",
    generic_procurement:
      "Submit this form to start a procurement approval workflow. No records are created until approvers sign off.",
    employee_offboarding:
      "Submit this form to initiate employee offboarding. IT will confirm that all software licences have been revoked before the workflow is marked complete. All active licence assignments for the departing employee will be automatically revoked on completion.",
  };

  return {
    title: workflowTypeLabel(workflowType),
    description: descriptions[workflowType] || descriptions.generic_procurement,
  };
}

export type RoutingPreview = {
  requester: string;
  requesterEmail: string;
  nextApprover: string;
  nextApproverEmail: string;
  finalReceiver: string;
  finalReceiverEmail: string;
  currentStage: string;
  nextStage: string;
};

export function getApprovalRoutingPreview(
  workflowType: WorkflowType,
  requesterName: string,
  requesterEmail: string,
  roleEmails: Record<string, string> = {}
): RoutingPreview {
  const financeEmail = roleEmails.finance || "deriskfinance@outlook.com";
  const lineManagerEmail = roleEmails.line_manager || "deriskline@outlook.com";

  if (workflowType === "employee_software_request") {
    return {
      requester: requesterName || requesterEmail || "-",
      requesterEmail: requesterEmail || "-",
      nextApprover: "Line Manager",
      nextApproverEmail: lineManagerEmail,
      finalReceiver: "Finance Manager",
      finalReceiverEmail: financeEmail,
      currentStage: "Submitted",
      nextStage: "Submitted → Line Manager Approval → Budget Approval",
    };
  }

  const itEmail = roleEmails.it_admin || "deriskit@outlook.com";

  if (workflowType === "employee_offboarding") {
    return {
      requester: requesterName || requesterEmail || "-",
      requesterEmail: requesterEmail || "-",
      nextApprover: "IT Administrator",
      nextApproverEmail: itEmail,
      finalReceiver: "IT Administrator",
      finalReceiverEmail: itEmail,
      currentStage: "Submitted",
      nextStage: "Submitted → IT Confirmation → Licences Revoked",
    };
  }

  return {
    requester: requesterName || requesterEmail || "-",
    requesterEmail: requesterEmail || "-",
    nextApprover: "Finance Manager",
    nextApproverEmail: financeEmail,
    finalReceiver: "IT Administrator",
    finalReceiverEmail: itEmail,
    currentStage: "Submitted",
    nextStage: "Submitted → Budget Approval → IT Procurement → Activation",
  };
}

export type WorkflowFlowStep = {
  label: string;
  name: string;
  email?: string;
  state: "done" | "current" | "upcoming";
};

export function getWorkflowFlowSteps(
  workflowType: WorkflowType,
  routing: RoutingPreview,
  roleEmails: Record<string, string> = {}
): WorkflowFlowStep[] {
  const financeEmail = roleEmails.finance || routing.nextApproverEmail;
  const itEmail = roleEmails.it_admin || "deriskit@outlook.com";
  const lineManagerEmail = roleEmails.line_manager || routing.nextApproverEmail;

  const completedStep: WorkflowFlowStep = {
    label: "Completed",
    name: "Request fulfilled",
    state: "upcoming",
  };

  if (workflowType === "employee_software_request") {
    return [
      {
        label: "Employee",
        name: routing.requester,
        email: routing.requesterEmail,
        state: "done",
      },
      {
        label: "Line Manager",
        name: "Line Manager",
        email: lineManagerEmail,
        state: "current",
      },
      {
        label: "Finance",
        name: "Finance Manager",
        email: financeEmail,
        state: "upcoming",
      },
      {
        label: "IT",
        name: "IT Administrator",
        email: itEmail,
        state: "upcoming",
      },
      completedStep,
    ];
  }

  if (workflowType === "employee_offboarding") {
    return [
      {
        label: "HR / Requester",
        name: routing.requester,
        email: routing.requesterEmail,
        state: "done",
      },
      {
        label: "IT",
        name: "IT Administrator",
        email: itEmail,
        state: "current",
      },
      { ...completedStep, label: "Licences Revoked", name: "Offboarding complete" },
    ];
  }

  return [
    {
      label: "Requester",
      name: routing.requester,
      email: routing.requesterEmail,
      state: "done",
    },
    {
      label: "Finance",
      name: "Finance Manager",
      email: routing.nextApproverEmail,
      state: "current",
    },
    {
      label: "IT",
      name: "IT Administrator",
      email: itEmail,
      state: "upcoming",
    },
    completedStep,
  ];
}

export function defaultSubmissionEmailSubject(moduleKey: string, workflowType: WorkflowType): string {
  if (workflowType === "license_assignment_request" || moduleKey === "licences") {
    return "New Licence Request Submitted";
  }
  if (workflowType === "employee_software_request") {
    return "Software Request Awaiting Approval";
  }
  if (workflowType === "employee_offboarding") {
    return "Employee Offboarding Request Submitted";
  }
  return "New Subscription Request Submitted";
}

export function buildDefaultSubmissionEmailBody(
  draft: Record<string, string>,
  workflowType: WorkflowType,
  routing: RoutingPreview
): string {
  const itemName =
    draft.name?.trim() ||
    draft.licence_name?.trim() ||
    draft.tool_requested?.trim() ||
    draft.required_tools?.trim() ||
    "Requested item";

  return [
    `Requester name: ${draft.requester_name || "-"}`,
    `Requester email: ${draft.requester_email || "-"}`,
    `Request type: ${workflowTypeLabel(workflowType)}`,
    `Tool/Subscription/Licence: ${itemName}`,
    `Business justification: ${draft.why_needed || draft.business_problem || "Not provided"}`,
    `Current workflow stage: ${routing.currentStage}`,
    `Action required from: ${routing.nextApproverEmail}`,
  ].join("\n");
}
