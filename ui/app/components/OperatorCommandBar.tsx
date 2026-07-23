"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

type PlanStep = {
  tool: string;
  params?: Record<string, unknown>;
  description?: string;
};

type OperatorPlan = {
  steps: PlanStep[];
  questions: string[];
  warnings: string[];
};

type StepResult = {
  index: number;
  tool: string;
  description?: string;
  status: string;
  verification?: string;
  error?: string;
};

type LoggedInUser = {
  id?: string;
  email?: string;
  roles?: string[];
};

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function OperatorCommandBar() {
  const [user, setUser] = useState<LoggedInUser | null>(null);
  const [orgId, setOrgId] = useState("");
  const [open, setOpen] = useState(false);
  const [instruction, setInstruction] = useState("");
  const [questionAnswers, setQuestionAnswers] = useState<Record<number, string>>({});
  const [planId, setPlanId] = useState<string | null>(null);
  const [plan, setPlan] = useState<OperatorPlan | null>(null);
  const [planStatus, setPlanStatus] = useState("");
  const [stepResults, setStepResults] = useState<StepResult[]>([]);
  const [executeStatus, setExecuteStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const storedUser = sessionStorage.getItem("slmct_user");
    if (storedUser) setUser(JSON.parse(storedUser));
    const storedOrg = sessionStorage.getItem("slmct_selected_org_id");
    if (storedOrg) setOrgId(storedOrg);

    const onOrgChange = () => {
      setOrgId(sessionStorage.getItem("slmct_selected_org_id") || "");
    };
    window.addEventListener("slmct_org_changed", onOrgChange);
    return () => window.removeEventListener("slmct_org_changed", onOrgChange);
  }, []);

  const canUseOperator = useMemo(() => {
    const roles = user?.roles ?? [];
    return roles.some((role) =>
      ["master_admin", "finance", "it_admin", "hr_admin", "line_manager", "employee"].includes(role),
    );
  }, [user]);

  if (!canUseOperator) return null;

  const resetResults = () => {
    setPlanId(null);
    setPlan(null);
    setPlanStatus("");
    setStepResults([]);
    setExecuteStatus("");
    setQuestionAnswers({});
    setError("");
  };

const LOOKUP_ANSWER = /^(find(\s+it|\s+the\s+price|\s+the\s+url|\s+them)?|look\s*it\s*up|lookup|autofill|search)$/i;

  const inferAnswerFields = (questions: string[], answers: Record<number, string>) => {
    const fields: Record<string, string> = {};
    questions.forEach((question, index) => {
      const answer = (answers[index] || "").trim();
      if (!answer || LOOKUP_ANSWER.test(answer)) return;
      const lower = question.toLowerCase();
      if (lower.includes("vendor")) fields.vendor = answer;
      else if (lower.includes("price") || lower.includes("amount") || lower.includes("cost")) fields.amount = answer;
      else if (lower.includes("department")) fields.department = answer;
      else if (lower.includes("fiscal year") || lower.includes("year")) fields.fiscal_year = answer;
      else if (lower.includes("currency")) fields.currency_code = answer;
      else if (lower.includes("billing")) fields.billing_cycle = answer;
    });
    return fields;
  };

  const buildAnswersPayload = () => {
    if (!plan?.questions?.length) return { answers: undefined as Record<string, string> | undefined, answer_fields: undefined as Record<string, string> | undefined };
    const answers: Record<string, string> = {};
    plan.questions.forEach((question, index) => {
      const answer = (questionAnswers[index] || "").trim();
      if (answer) answers[question] = answer;
    });
    const answer_fields = inferAnswerFields(plan.questions, questionAnswers);
    return {
      answers: Object.keys(answers).length ? answers : undefined,
      answer_fields: Object.keys(answer_fields).length ? answer_fields : undefined,
    };
  };

  const buildInstruction = () => instruction.trim();

  const handlePlan = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!instruction.trim() || !orgId || loading) return;
    setLoading(true);
    setError("");
    setStepResults([]);
    setExecuteStatus("");
    try {
      const { answers, answer_fields } = buildAnswersPayload();
      const res = await fetch(`${apiBaseUrl}/api/operator/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instruction: buildInstruction(),
          answers,
          answer_fields,
          organisation_id: orgId,
          actor_user_id: user?.id ?? null,
          actor_roles: user?.roles ?? [],
          actor_email: user?.email ?? null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Planning failed");
      setPlanId(data.plan_id);
      setPlan(data.plan);
      setPlanStatus(data.status || "");
      if (!(data.plan?.questions?.length)) {
        setQuestionAnswers({});
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleExecute = async () => {
    if (!planId || loading) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${apiBaseUrl}/api/operator/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_id: planId,
          confirmed: true,
          organisation_id: orgId,
          actor_user_id: user?.id ?? null,
          actor_roles: user?.roles ?? [],
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Execution failed");
      setExecuteStatus(data.status || "");
      setStepResults(data.step_results || []);
      setPlanStatus(data.plan?.status || data.status || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <button type="button" className="operator-fab" onClick={() => setOpen(true)} title="AI Operator">
        AI Operator
      </button>

      {open ? (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal-panel operator-panel" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h2>AI Operator</h2>
              <button type="button" onClick={() => setOpen(false)} aria-label="Close">
                ×
              </button>
            </div>

            <p className="operator-help">
              Describe what you want done. The operator will look up prices, vendor details, and URLs on its own when
              you say &quot;find&quot; or &quot;fill out details&quot; — then plan steps and execute only after you confirm.
              It never approves workflows on its own.
            </p>

            <form onSubmit={handlePlan} className="operator-form">
              <label>
                <span>Instruction</span>
                <textarea
                  rows={3}
                  value={instruction}
                  onChange={(e) => setInstruction(e.target.value)}
                  placeholder='e.g. "Create a 30000 AED Marketing budget for 2027"'
                  disabled={loading}
                />
              </label>
              <div className="modal-actions">
                <button type="submit" disabled={loading || !instruction.trim()}>
                  {loading ? "Working…" : plan ? "Re-plan" : "Generate plan"}
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => {
                    resetResults();
                    setInstruction("");
                  }}
                  disabled={loading}
                >
                  Clear
                </button>
              </div>
            </form>

            {error ? <p className="operator-error">{error}</p> : null}

            {plan ? (
              <div className="operator-plan">
                <p className="eyebrow">Plan {planStatus ? `· ${planStatus}` : ""}</p>

                {plan.warnings?.length ? (
                  <div className="operator-warnings">
                    <strong>Warnings</strong>
                    <ul>
                      {plan.warnings.map((w) => (
                        <li key={w}>{w}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {plan.questions?.length ? (
                  <div className="operator-questions">
                    <strong>Questions</strong>
                    {plan.questions.map((q, index) => (
                      <label key={q}>
                        <span>{q}</span>
                        <input
                          type="text"
                          value={questionAnswers[index] || ""}
                          onChange={(e) =>
                            setQuestionAnswers((prev) => ({
                              ...prev,
                              [index]: e.target.value,
                            }))
                          }
                        />
                      </label>
                    ))}
                    <button type="button" onClick={() => void handlePlan()} disabled={loading}>
                      Submit answers &amp; re-plan
                    </button>
                  </div>
                ) : null}

                {plan.steps?.length ? (
                  <>
                    <ol className="operator-steps">
                      {plan.steps.map((step, index) => (
                        <li key={`${step.tool}-${index}`}>
                          <strong>{step.description || step.tool}</strong>
                          <span>{step.tool}</span>
                        </li>
                      ))}
                    </ol>
                    <div className="modal-actions">
                      <button
                        type="button"
                        onClick={() => void handleExecute()}
                        disabled={loading || !!plan.questions?.length}
                      >
                        Confirm &amp; execute
                      </button>
                      <button type="button" className="secondary" onClick={resetResults} disabled={loading}>
                        Cancel plan
                      </button>
                    </div>
                  </>
                ) : null}
              </div>
            ) : null}

            {stepResults.length ? (
              <div className="operator-results">
                <p className="eyebrow">Execution {executeStatus ? `· ${executeStatus}` : ""}</p>
                <ul>
                  {stepResults.map((step) => (
                    <li key={step.index} className={step.status === "passed" ? "pass" : "fail"}>
                      <strong>{step.description || step.tool}</strong>
                      <span>{step.status}</span>
                      {step.verification ? <small>{step.verification}</small> : null}
                      {step.error ? <small>{step.error}</small> : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
