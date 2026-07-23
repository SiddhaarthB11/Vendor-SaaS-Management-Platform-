"use client";

import { FormEvent, Fragment, useEffect, useMemo, useState } from "react";
import {
  buildDefaultSubmissionDraft,
  buildDefaultSubmissionEmailBody,
  defaultSubmissionEmailSubject,
  getApprovalRoutingPreview,
  getSubmissionFields,
  getSubmissionFormHeading,
  getWorkflowFlowSteps,
  SUBMISSION_SECTIONS,
  SubmissionField,
} from "../lib/workflow-submission-forms";
import {
  getPrimaryRoleLabel,
} from "../lib/workflow-submission-permissions";
import { resolveWorkflowType, workflowTypeLabel, WorkflowType, WORKFLOW_TYPES } from "../lib/workflow-governance";

type AnyRecord = Record<string, unknown>;

export type SubmissionEmailOverride = {
  eventType: string;
  to: string;
  cc?: string;
  subject: string;
  body: string;
};

export type SubmissionResult = {
  workflowId: string;
  currentStage: string;
  approverEmail: string;
};

type WorkflowEmailPreview = {
  to: string;
  cc?: string;
  subject: string;
  body: string;
  eventType: string;
  templateLabel?: string;
  stage?: string;
  recipientRole?: string;
};

type Props = {
  moduleKey: string;
  title: string;
  user?: { id?: string; name?: string; email?: string; roles?: string[] };
  selectedOrgId?: string;
  allowedWorkflowTypes: WorkflowType[];
  organisations: AnyRecord[];
  vendors: AnyRecord[];
  subscriptions: AnyRecord[];
  employees: AnyRecord[];
  lineManagers?: AnyRecord[];
  licences?: AnyRecord[];
  budgets?: AnyRecord[];
  vendorCatalogue?: AnyRecord[];
  fxRates?: Record<string, number>;
  roleEmails?: Record<string, string>;
  apiBaseUrl: string;
  initialDraft?: Record<string, string>;
  onClose: () => void;
  onSubmit: (
    payload: Record<string, string>,
    workflowType: WorkflowType,
    emailOverrides: SubmissionEmailOverride[]
  ) => Promise<SubmissionResult>;
};

function recordLabel(record: AnyRecord, source: string): string {
  if (source === "organisations") return String(record.name || record.code || record.id);
  if (source === "employees") return String(record.full_name || record.work_email || record.id);
  if (source === "line_managers") return String(record.full_name || record.work_email || record.id);
  if (source === "subscriptions") return String(record.name || record.id);
  if (source === "vendors") return String(record.name || record.id);
  return String(record.name || record.id);
}

function buildSoftwareSummary(
  draft: Record<string, string>,
  subscriptions: AnyRecord[],
  vendors: AnyRecord[],
  licences: AnyRecord[] = []
) {
  const subscriptionId = draft.subscription_id?.trim();
  const softwareName = draft.name?.trim() || draft.tool_requested?.trim() || draft.licence_name?.trim();
  if (!subscriptionId && (!softwareName || softwareName.length < 2)) return null;

  const subscription = subscriptionId
    ? subscriptions.find((item) => String(item.id) === subscriptionId)
    : subscriptions.find((item) => String(item.name).toLowerCase() === softwareName?.toLowerCase());

  const vendor = draft.vendor_id
    ? vendors.find((item) => String(item.id) === draft.vendor_id)
    : subscription?.vendor_id
      ? vendors.find((item) => String(item.id) === String(subscription.vendor_id))
      : null;

  const businessUseCase =
    String(draft.notes || draft.why_needed || draft.business_problem || subscription?.notes || "").trim() ||
    "Describe the business use case in the form sections above.";

  const assignedLicences = subscriptionId
    ? licences.filter((item) => String(item.subscription_id) === subscriptionId && String(item.status) === "assigned")
        .length
    : null;
  const totalLicences = subscriptionId
    ? licences.filter((item) => String(item.subscription_id) === subscriptionId).length
    : null;

  return {
    title: String(subscription?.name || softwareName || "Selected tool"),
    description: businessUseCase,
    meta: [
      vendor?.name ? `Vendor: ${String(vendor.name)}` : "",
      draft.category || subscription?.category ? `Category: ${draft.category || subscription?.category}` : "",
      `Business use case: ${businessUseCase}`,
      draft.amount ? `Estimated cost: ${draft.amount}${draft.currency_code ? ` ${draft.currency_code}` : ""}` : "",
      assignedLicences != null && totalLicences != null
        ? `Licence availability: ${Math.max(totalLicences - assignedLicences, 0)} available of ${totalLicences}`
        : "",
    ].filter(Boolean),
  };
}

function textareaRows(field: SubmissionField): number {
  if (field.type !== "textarea") return 3;
  if (field.wide) return 8;
  if (field.name === "notes") return 6;
  return 5;
}

// Fallback rates used before live rates arrive from the API
const _FALLBACK_FX: Record<string, number> = { USD: 1, AED: 3.6725, GBP: 0.79, EUR: 0.92, INR: 83.5, SAR: 3.75, QAR: 3.64, KWD: 0.307 };

function toUsd(price: number, fromCurrency: string, rates: Record<string, number>): number {
  return price / (rates[fromCurrency] ?? 1);
}

function fromUsd(priceUsd: number, toCurrency: string, rates: Record<string, number>): number {
  return priceUsd * (rates[toCurrency] ?? 1);
}

export default function WorkflowSubmissionModal({
  moduleKey,
  title,
  user,
  selectedOrgId,
  allowedWorkflowTypes,
  organisations,
  vendors,
  subscriptions,
  employees,
  lineManagers = [],
  licences = [],
  budgets = [],
  vendorCatalogue = [],
  fxRates,
  roleEmails = {},
  apiBaseUrl,
  initialDraft,
  onClose,
  onSubmit,
}: Props) {
  const userRoles = user?.roles ?? [];
  const requesterRole = getPrimaryRoleLabel(userRoles);
  const initialWorkflowType = allowedWorkflowTypes[0] ?? resolveWorkflowType(moduleKey, {});

  const [draft, setDraft] = useState<Record<string, string>>(() => ({
    ...buildDefaultSubmissionDraft(moduleKey, initialWorkflowType, user, selectedOrgId, requesterRole),
    ...(initialDraft ?? {}),
  }));

  const workflowType = useMemo(() => {
    const explicit = draft.workflow_type as WorkflowType | undefined;
    if (explicit && allowedWorkflowTypes.includes(explicit)) {
      return explicit;
    }
    return allowedWorkflowTypes[0] ?? resolveWorkflowType(moduleKey, draft, draft.workflow_type);
  }, [allowedWorkflowTypes, draft, moduleKey]);

  const fields = useMemo(
    () => getSubmissionFields(moduleKey, workflowType, allowedWorkflowTypes, requesterRole),
    [allowedWorkflowTypes, moduleKey, requesterRole, workflowType]
  );

  useEffect(() => {
    setDraft((previous) => ({
      ...previous,
      workflow_type: workflowType,
      request_type: workflowTypeLabel(workflowType),
      requester_role: requesterRole,
    }));
  }, [requesterRole, workflowType]);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [successResult, setSuccessResult] = useState<SubmissionResult | null>(null);
  const [toolSummary, setToolSummary] = useState("");
  const [toolSummaryLoading, setToolSummaryLoading] = useState(false);
  const [emailPreview, setEmailPreview] = useState<WorkflowEmailPreview[]>([]);
  const [editableEmails, setEditableEmails] = useState<SubmissionEmailOverride[]>([]);
  const [emailPreviewLoading, setEmailPreviewLoading] = useState(false);
  const [emailPreviewError, setEmailPreviewError] = useState("");
  const [emailTemplatesLocked, setEmailTemplatesLocked] = useState(false);

  const scopedEmployees = useMemo(() => {
    const orgId = draft.organisation_id || selectedOrgId || "";
    if (!orgId) return employees;
    return employees.filter((item) => String(item.organisation_id || "") === String(orgId));
  }, [draft.organisation_id, employees, selectedOrgId]);

  const softwareSummary = useMemo(
    () => buildSoftwareSummary(draft, subscriptions, vendors, licences),
    [draft, subscriptions, vendors, licences]
  );

  const routingPreview = useMemo(
    () => getApprovalRoutingPreview(workflowType, draft.requester_name, draft.requester_email, roleEmails),
    [workflowType, draft.requester_name, draft.requester_email, roleEmails]
  );

  const flowSteps = useMemo(
    () => getWorkflowFlowSteps(workflowType, routingPreview, roleEmails),
    [routingPreview, roleEmails, workflowType]
  );

  const formHeading = useMemo(
    () => getSubmissionFormHeading(moduleKey, workflowType),
    [moduleKey, workflowType]
  );

  useEffect(() => {
    if (!successResult) return;
    const timer = window.setTimeout(() => onClose(), 3500);
    return () => window.clearTimeout(timer);
  }, [successResult, onClose]);

  // Auto-generate tool summary from name + vendor
  useEffect(() => {
    const toolName = (draft.name || draft.tool_requested || "").trim();
    if (!toolName || toolName.length < 2) {
      setToolSummary("");
      return;
    }
    const vendorRecord = vendors.find((v) => String(v.id) === draft.vendor_id);
    const vendorName = String(vendorRecord?.name || draft.vendor_name || "");
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setToolSummaryLoading(true);
      fetch(`${apiBaseUrl}/api/tool-summary`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tool_name: toolName, vendor_name: vendorName }),
      })
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => { if (!cancelled && data?.summary) setToolSummary(data.summary); })
        .catch(() => {})
        .finally(() => { if (!cancelled) setToolSummaryLoading(false); });
    }, 800);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [apiBaseUrl, draft.name, draft.tool_requested, draft.vendor_id, vendors]);

  useEffect(() => {
    if (!user?.email || successResult) return;
    let cancelled = false;
    setEmailPreviewLoading(true);
    setEmailPreviewError("");

    fetch(`${apiBaseUrl}/api/email/preview/workflow`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        actor_user_id: user.id ?? null,
        actor_email: user.email ?? null,
        requested_module: moduleKey,
        workflow_type: workflowType,
        payload: { ...draft, workflow_type: workflowType },
        event: "submitted",
      }),
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(await response.text());
        return response.json();
      })
      .then((data) => {
        if (cancelled) return;
        const previews = Array.isArray(data) ? data : [];
        setEmailPreview(previews);
        if (!emailTemplatesLocked) {
          setEditableEmails(
            previews.map((preview: WorkflowEmailPreview, index: number) => ({
              eventType: preview.eventType || `preview_${index}`,
              to: preview.to,
              cc: preview.cc,
              subject: preview.subject || defaultSubmissionEmailSubject(moduleKey, workflowType),
              body:
                preview.body ||
                buildDefaultSubmissionEmailBody(draft, workflowType, routingPreview),
            }))
          );
        }
      })
      .catch((previewError) => {
        if (!cancelled) {
          setEmailPreview([]);
          setEditableEmails([
            {
              eventType: "request_submitted",
              to: routingPreview.nextApproverEmail,
              cc: draft.requester_email,
              subject: defaultSubmissionEmailSubject(moduleKey, workflowType),
              body: buildDefaultSubmissionEmailBody(draft, workflowType, routingPreview),
            },
          ]);
          setEmailPreviewError(
            previewError instanceof Error ? previewError.message : "Could not load server email preview."
          );
        }
      })
      .finally(() => {
        if (!cancelled) setEmailPreviewLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl, draft, emailTemplatesLocked, moduleKey, routingPreview, successResult, user?.email, user?.id, workflowType]);

  useEffect(() => {
    const orgId = draft.organisation_id;
    if (!orgId) return;
    const employeeId = draft.assigned_to_person_id;
    if (!employeeId) return;
    const stillValid = scopedEmployees.some((item) => String(item.id) === employeeId);
    if (!stillValid) {
      setDraft((previous) => ({
        ...previous,
        assigned_to_person_id: "",
        employee_name: "",
        employee_email: "",
        who_will_use_it: "",
      }));
    }
  }, [draft.assigned_to_person_id, draft.organisation_id, scopedEmployees]);

  useEffect(() => {
    const employeeId = draft.assigned_to_person_id;
    if (!employeeId) return;
    const employee = scopedEmployees.find((item) => String(item.id) === employeeId);
    if (!employee) return;
    setDraft((previous) => ({
      ...previous,
      employee_name: String(employee.full_name || previous.employee_name || ""),
      employee_email: String(employee.work_email || previous.employee_email || ""),
      who_will_use_it: String(employee.full_name || previous.who_will_use_it || ""),
      department: String(previous.department || employee.department || ""),
      line_manager_email: String(previous.line_manager_email || employee.line_manager_email || ""),
    }));
  }, [draft.assigned_to_person_id, scopedEmployees]);


  function updateField(name: string, value: string) {
    if (name === "workflow_type" && value in WORKFLOW_TYPES) {
      if (!allowedWorkflowTypes.includes(value as WorkflowType)) {
        setError("You are not allowed to submit this request type.");
        return;
      }
      setDraft((previous) => ({
        ...previous,
        workflow_type: value,
        request_type: workflowTypeLabel(value),
      }));
      setError("");
      return;
    }
    if (name === "organisation_id") {
      setDraft((previous) => ({
        ...previous,
        organisation_id: value,
        assigned_to_person_id: "",
        employee_name: "",
        employee_email: "",
        who_will_use_it: "",
      }));
      setEmailTemplatesLocked(false);
      return;
    }
    if (name === "subscription_id" && isLicenceRequest) {
      const sub = subscriptions.find((s) => String(s.id) === value);
      setDraft((previous) => ({
        ...previous,
        subscription_id: value,
        licence_name: sub ? `${String(sub.name)} Seat` : previous.licence_name,
      }));
      return;
    }
    if (name === "offboarded_employee_name" && workflowType === "employee_offboarding") {
      const emp = employees.find((e) => String(e.id) === value);
      setDraft((previous) => ({
        ...previous,
        offboarded_employee_name: value,
        offboarded_employee_display_name: emp ? String(emp.full_name || emp.work_email || value) : value,
        offboarded_employee_email: emp ? String(emp.work_email || "") : "",
      }));
      return;
    }
    setDraft((previous) => ({ ...previous, [name]: value }));
    if (["workflow_type", "requester_email", "requester_name", "name", "tool_requested"].includes(name)) {
      setEmailTemplatesLocked(false);
    }
  }

  function resetEmailTemplates() {
    setEmailTemplatesLocked(false);
    setEditableEmails(
      emailPreview.map((preview, index) => ({
        eventType: preview.eventType || `preview_${index}`,
        to: preview.to,
        cc: preview.cc,
        subject: preview.subject || defaultSubmissionEmailSubject(moduleKey, workflowType),
        body: preview.body || buildDefaultSubmissionEmailBody(draft, workflowType, routingPreview),
      }))
    );
  }

  function updateEditableEmail(index: number, key: "subject" | "body", value: string) {
    setEmailTemplatesLocked(true);
    setEditableEmails((previous) =>
      previous.map((email, emailIndex) => (emailIndex === index ? { ...email, [key]: value } : email))
    );
  }

  function sourceRecords(field: SubmissionField): AnyRecord[] {
    if (field.source === "organisations") return organisations;
    if (field.source === "vendors") return vendors;
    if (field.source === "subscriptions") {
      if (isLicenceRequest) {
        return subscriptions.filter(
          (s) => s.vendor_id && String(s.status || "active") !== "revoked" && String(s.status || "active") !== "cancelled"
        );
      }
      return subscriptions;
    }
    if (field.source === "employees") return scopedEmployees;
    if (field.source === "line_managers") return lineManagers;
    return [];
  }

  function selectValue(field: SubmissionField): string {
    if (field.name === "workflow_type") {
      return draft.workflow_type || workflowType;
    }
    return draft[field.name] ?? "";
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!allowedWorkflowTypes.includes(workflowType)) {
      setError("You are not allowed to submit this request type.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const result = await onSubmit({ ...draft, workflow_type: workflowType }, workflowType, editableEmails);
      setSuccessResult(result);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : "Submission failed.");
    } finally {
      setSubmitting(false);
    }
  }

  // Departments scoped to the organisation chosen in the form (falls back to the
  // page-level selected org). Only departments with a non-closed budget are offered;
  // if the org has no budgets at all, the field's static list is used instead.
  const orgDepartmentOptions = useMemo(() => {
    const orgId = String(draft.organisation_id || selectedOrgId || "");
    if (!orgId) return [];
    return Array.from(
      new Set(
        budgets
          .filter((b) => String(b.organisation_id) === orgId && String(b.status) !== "closed")
          .map((b) => String(b.department || "").trim())
          .filter(Boolean)
      )
    ).sort();
  }, [budgets, draft.organisation_id, selectedOrgId]);

  const isSubscriptionNew = workflowType === "employee_software_request" || workflowType === "new_subscription_request";
  const isLicenceRequest = workflowType === "license_assignment_request" || workflowType === "hr_onboarding_request";

  // Live FX rates from parent (daily updated); fall back to static table if not yet loaded
  const rates = (fxRates && Object.keys(fxRates).length > 1) ? fxRates : _FALLBACK_FX;

  // For subscription_new: vendor catalogue items for the selected vendor
  const catalogueForVendor = isSubscriptionNew
    ? vendorCatalogue.filter((c) => String(c.vendor_id) === (draft.vendor_id ?? ""))
    : [];

  // Unit price in USD stored in draft._unit_price_usd; recomputed when catalogue item is selected
  const unitPriceUsd = Number(draft._unit_price_usd || 0);
  const durationMonths = Number(draft._duration_months || 12);
  const selectedCurrency = draft.currency_code || "AED";

  function handleCatalogueItemSelect(itemName: string) {
    const item = catalogueForVendor.find((c) => String(c.name) === itemName);
    if (item) {
      const usd = toUsd(Number(item.price), String(item.currency_code || "USD"), rates);
      const total = fromUsd(usd, selectedCurrency, rates) * durationMonths;
      setDraft((prev) => ({
        ...prev,
        name: itemName,
        _unit_price_usd: String(usd),
        amount: total.toFixed(2),
      }));
    } else {
      setDraft((prev) => ({ ...prev, name: itemName, _unit_price_usd: "", amount: "" }));
    }
  }

  function handleDurationChange(months: number) {
    const end = new Date();
    end.setMonth(end.getMonth() + months);
    const newAmount = unitPriceUsd ? fromUsd(unitPriceUsd, selectedCurrency, rates) * months : undefined;
    setDraft((prev) => ({
      ...prev,
      _duration_months: String(months),
      renewal_date: end.toISOString().split("T")[0],
      ...(newAmount !== undefined ? { amount: newAmount.toFixed(2) } : {}),
    }));
  }

  function handleCurrencyChange(newCur: string) {
    const newAmount = unitPriceUsd ? fromUsd(unitPriceUsd, newCur, rates) * durationMonths : undefined;
    setDraft((prev) => ({
      ...prev,
      currency_code: newCur,
      ...(newAmount !== undefined ? { amount: newAmount.toFixed(2) } : {}),
    }));
  }

  // Autofilled fields: suppress "Required" badge when the field already has a value
  // (either pre-populated from user context or marked readOnly)
  const AUTOFILL_FIELDS = new Set(["requester_name", "requester_email", "requester_role", "organisation_id", "request_type"]);
  function showRequired(field: SubmissionField): boolean {
    if (!field.required) return false;
    const val = draft[field.name];
    if ((field.readOnly || AUTOFILL_FIELDS.has(field.name)) && val && String(val).trim() !== "") return false;
    return true;
  }

  function renderField(field: SubmissionField) {
    const controlId = `request-field-${field.name}`;

    // Duration dropdown for licence requests — computes expires_at from assigned_at + months
    if (isLicenceRequest && field.name === "_duration_months") {
      return (
        <div key={field.name} className="request-form-field">
          <label htmlFor={controlId}>
            <span className="request-form-label">Licence Duration</span>
            <span className="request-form-help">Expiry date is calculated from the assigned date</span>
          </label>
          <select
            id={controlId}
            value={String(durationMonths)}
            onChange={(e) => {
              const months = Number(e.target.value);
              const base = draft.assigned_at ? new Date(draft.assigned_at) : new Date();
              base.setMonth(base.getMonth() + months);
              setDraft((prev) => ({
                ...prev,
                _duration_months: String(months),
                expires_at: base.toISOString().split("T")[0],
              }));
            }}
          >
            {[1,2,3,6,9,12,18,24,36].map((m) => (
              <option key={m} value={String(m)}>{m} month{m !== 1 ? "s" : ""}</option>
            ))}
          </select>
        </div>
      );
    }

    // Smart overrides for subscription_new
    if (isSubscriptionNew) {
      // Name field → catalogue dropdown when vendor selected, else free text
      if (field.name === "name") {
        return (
          <div key={field.name} className="request-form-field">
            <label htmlFor={controlId}>
              <span className="request-form-label">
                Software / Subscription Name
                {showRequired(field) ? <em className="request-form-required">Required</em> : null}
              </span>
              {field.helpText ? <span className="request-form-help">{field.helpText}</span> : null}
            </label>
            <select
              id={controlId}
              required={field.required}
              value={draft.name ?? ""}
              onChange={(e) => handleCatalogueItemSelect(e.target.value)}
            >
              <option value="">Select subscription</option>
              {catalogueForVendor.map((c) => (
                <option key={String(c.id)} value={String(c.name)}>
                  {String(c.name)} — {Number(c.price).toFixed(2)} {String(c.currency_code)}/mo
                </option>
              ))}
              <option value="__other__">Other (type manually)</option>
            </select>
            {draft.name === "__other__" && (
              <input
                style={{ marginTop: "8px" }}
                type="text"
                required
                placeholder="Enter software name"
                value={draft._custom_name ?? ""}
                onChange={(e) => setDraft((prev) => ({ ...prev, _custom_name: e.target.value }))}
              />
            )}
          </div>
        );
      }

      // Duration field → labeled select with month options
      if (field.name === "_duration_months") {
        const unitInCur = unitPriceUsd ? fromUsd(unitPriceUsd, selectedCurrency, rates) : null;
        return (
          <div key={field.name} className="request-form-field">
            <label htmlFor={controlId}>
              <span className="request-form-label">Subscription Duration</span>
              {unitInCur ? (
                <span className="request-form-help">
                  {unitInCur.toFixed(2)} {selectedCurrency}/mo × months = total cost
                </span>
              ) : null}
            </label>
            <select
              id={controlId}
              value={String(durationMonths)}
              onChange={(e) => handleDurationChange(Number(e.target.value))}
            >
              {[1,2,3,6,9,12,18,24,36].map((m) => (
                <option key={m} value={String(m)}>{m} month{m !== 1 ? "s" : ""}</option>
              ))}
            </select>
          </div>
        );
      }

      // Currency field → recomputes price on change
      if (field.name === "currency_code") {
        return (
          <div key={field.name} className="request-form-field">
            <label htmlFor={controlId}>
              <span className="request-form-label">Currency</span>
              {field.helpText ? <span className="request-form-help">{field.helpText}</span> : null}
            </label>
            <select
              id={controlId}
              value={selectedCurrency}
              onChange={(e) => handleCurrencyChange(e.target.value)}
            >
              {(field.options ?? []).map((opt) => <option key={opt} value={opt}>{opt}</option>)}
            </select>
          </div>
        );
      }

      // Amount field label shows breakdown when auto-computed
      if (field.name === "amount" && unitPriceUsd) {
        const unitInCur = fromUsd(unitPriceUsd, selectedCurrency, rates);
        return (
          <div key={field.name} className="request-form-field">
            <label htmlFor={controlId}>
              <span className="request-form-label">
                Total Cost
              </span>
              <span className="request-form-help">
                {durationMonths} month{durationMonths !== 1 ? "s" : ""} × {unitInCur.toFixed(2)} {selectedCurrency}/mo
              </span>
            </label>
            <input
              id={controlId}
              type="number"
              required={field.required}
              placeholder="0.00"
              value={draft.amount ?? ""}
              onChange={(e) => setDraft((prev) => ({ ...prev, amount: e.target.value, _unit_price_usd: "" }))}
            />
          </div>
        );
      }
    }

    return (
      <div
        key={field.name}
        className={`request-form-field${field.wide ? " request-form-field--wide" : ""}`}
      >
        <label htmlFor={controlId}>
          <span className="request-form-label">
            {field.label}
            {showRequired(field) ? <em className="request-form-required">Required</em> : null}
          </span>
          {field.helpText ? <span className="request-form-help">{field.helpText}</span> : null}
        </label>
        {field.type === "textarea" ? (
          <textarea
            id={controlId}
            rows={textareaRows(field)}
            required={field.required}
            placeholder={field.placeholder}
            value={draft[field.name] ?? ""}
            onChange={(event) => updateField(field.name, event.target.value)}
          />
        ) : field.type === "select" ? (
          <select
            id={controlId}
            required={field.required}
            value={selectValue(field)}
            onChange={(event) => updateField(field.name, event.target.value)}
          >
            <option value="">Select an option</option>
            {field.source
              ? sourceRecords(field).map((record) => (
                  <option key={String(record.id)} value={field.source === "line_managers" ? String(record.work_email || record.id) : String(record.id)}>
                    {recordLabel(record, field.source || "")}
                  </option>
                ))
              : (field.name === "department" && orgDepartmentOptions.length
                  ? orgDepartmentOptions
                  : field.options ?? []
                ).map((option) => (
                  <option key={option} value={option}>
                    {field.name === "workflow_type" ? workflowTypeLabel(option) : option}
                  </option>
                ))}
          </select>
        ) : (
          <input
            id={controlId}
            type={field.type ?? "text"}
            required={field.required}
            readOnly={field.readOnly || field.name === "request_type" || field.name === "requester_role"}
            placeholder={field.placeholder}
            value={draft[field.name] ?? ""}
            onChange={(event) => updateField(field.name, event.target.value)}
          />
        )}
      </div>
    );
  }

  if (successResult) {
    return (
      <div className="modal-backdrop" role="presentation">
        <div className="modal-panel request-form-modal request-success-panel" onClick={(event) => event.stopPropagation()}>
          <div className="request-success-icon" aria-hidden="true">
            ✓
          </div>
          <h2>Request Submitted Successfully</h2>
          <p className="request-success-lead">Your request is now in the approval workflow. Notifications have been sent.</p>
          <dl className="request-success-details">
            <div>
              <dt>Workflow ID</dt>
              <dd>{successResult.workflowId}</dd>
            </div>
            <div>
              <dt>Current Stage</dt>
              <dd>{successResult.currentStage}</dd>
            </div>
            <div>
              <dt>Current Approver</dt>
              <dd>{successResult.approverEmail}</dd>
            </div>
          </dl>
          <p className="request-success-note">Closing automatically…</p>
          <button type="button" className="request-form-secondary-btn" onClick={onClose}>
            Close now
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="modal-backdrop" role="presentation" onClick={(event) => event.target === event.currentTarget && onClose()}>
      <form className="modal-panel request-form-modal" onClick={(event) => event.stopPropagation()} onSubmit={handleSubmit}>
        <header className="request-form-header">
          <div className="request-form-header-content">
            <h2>{formHeading.title}</h2>
            <p className="request-form-intro">{formHeading.description}</p>
            <span className="request-form-type-pill">{workflowTypeLabel(workflowType)}</span>
          </div>
          <button type="button" className="request-form-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        <div className="request-form-body">
          {SUBMISSION_SECTIONS.map((section) => {
            const sectionFields = fields.filter((field) => field.section === section.id);
            if (!sectionFields.length) return null;
            return (
              <section key={section.id} className="request-form-section-card">
                <div className="request-form-section-heading">
                  <span className="request-form-step">{section.step}</span>
                  <div>
                    <h3>{section.title}</h3>
                    <p>{section.description}</p>
                  </div>
                </div>
                <div className="request-form-fields">{sectionFields.map(renderField)}</div>
              </section>
            );
          })}

          <section className="request-form-section-card">
            <div className="request-form-section-heading">
              <span className="request-form-step">4</span>
              <div>
                <h3>Tool Summary</h3>
                <p>A quick snapshot of the software or licence you are requesting.</p>
              </div>
            </div>
            {toolSummaryLoading ? (
              <div className="request-tool-summary request-tool-summary--loading" aria-live="polite">
                <span className="tool-summary-spinner" aria-hidden="true" />
                <span>Generating tool summary…</span>
              </div>
            ) : toolSummary ? (
              <div className="request-tool-summary" aria-live="polite">
                <div className="tool-summary-ai-label">AI-generated summary</div>
                <h4>{(draft.name || draft.tool_requested || "").trim()}</h4>
                <p>{toolSummary}</p>
                {softwareSummary?.meta.filter((m) => !m.startsWith("Business use case")).length ? (
                  <ul className="request-tool-summary-list">
                    {softwareSummary.meta
                      .filter((m) => !m.startsWith("Business use case"))
                      .map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                  </ul>
                ) : null}
              </div>
            ) : (
              <div className="request-tool-summary request-tool-summary--empty" aria-live="polite">
                <p>No summary yet.</p>
                <span>Enter the software name above and a summary will be generated automatically.</span>
              </div>
            )}
          </section>

          <section className="request-form-section-card">
            <div className="request-form-section-heading">
              <span className="request-form-step">5</span>
              <div>
                <h3>Workflow Preview</h3>
                <p>How your request will move through approvers after submission.</p>
              </div>
            </div>
            <div className="workflow-flow-preview workflow-flow-preview--horizontal">
              {flowSteps.map((step, index) => (
                <Fragment key={`${step.label}-${index}`}>
                  <div className={`workflow-flow-node workflow-flow-node--${step.state}`}>
                    <span className="workflow-flow-node-label">{step.label}</span>
                    <strong>{step.name}</strong>
                    {step.email ? <span className="workflow-flow-node-email">{step.email}</span> : null}
                  </div>
                  {index < flowSteps.length - 1 ? (
                    <div className="workflow-flow-arrow workflow-flow-arrow--horizontal" aria-hidden="true">
                      →
                    </div>
                  ) : null}
                </Fragment>
              ))}
            </div>
          </section>

          <section className="request-form-section-card">
            <div className="request-form-section-heading">
              <span className="request-form-step">6</span>
              <div>
                <h3>Email Preview</h3>
                <p>Review notification emails before you submit. You can edit the message body below.</p>
              </div>
            </div>
            <div className="request-form-section-actions">
              <button type="button" className="request-form-secondary-btn" onClick={resetEmailTemplates}>
                Reset template
              </button>
            </div>
            {emailPreviewLoading ? (
              <p className="request-form-muted">Loading email preview…</p>
            ) : (
              <>
                {emailPreviewError ? (
                  <p className="request-form-warning">Using local preview defaults: {emailPreviewError}</p>
                ) : null}
                {editableEmails.length ? (
                  <div className="email-preview-stack">
                    {editableEmails.map((preview, index) => (
                      <article key={`${preview.eventType}-${index}`} className="email-preview-card">
                        <header className="email-preview-card-header">
                          <div className="email-preview-card-title">
                            <span className="email-preview-card-icon" aria-hidden="true">
                              ✉
                            </span>
                            <span>Notification email</span>
                          </div>
                          <span className="email-preview-card-badge">Editable body</span>
                        </header>
                        <div className="email-preview-card-content">
                          <dl className="email-preview-card-meta">
                            <div>
                              <dt>To</dt>
                              <dd>{preview.to}</dd>
                            </div>
                            {preview.cc ? (
                              <div>
                                <dt>CC</dt>
                                <dd>{preview.cc}</dd>
                              </div>
                            ) : null}
                            <div>
                              <dt>Subject</dt>
                              <dd className="email-preview-card-subject-text">{preview.subject}</dd>
                            </div>
                          </dl>
                          <label className="email-preview-card-body">
                            <span>Body</span>
                            <textarea
                              rows={10}
                              value={preview.body}
                              onChange={(event) => updateEditableEmail(index, "body", event.target.value)}
                            />
                          </label>
                        </div>
                      </article>
                    ))}
                  </div>
                ) : (
                  <p className="request-form-muted">No preview available yet.</p>
                )}
              </>
            )}
          </section>
        </div>

        {error ? <p className="form-error request-form-error">{error}</p> : null}

        <footer className="request-form-footer">
          <button type="button" className="request-form-secondary-btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="request-form-primary-btn" disabled={submitting}>
            {submitting ? "Submitting request…" : "Submit Request"}
          </button>
        </footer>
      </form>
    </div>
  );
}
