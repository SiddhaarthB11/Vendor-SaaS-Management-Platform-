"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type UserLike = {
  id?: string;
  email?: string;
  roles?: string[];
};

type DevtoolsJob = {
  id: string;
  kind: string;
  status: string;
  params?: Record<string, unknown>;
  report?: Record<string, unknown>;
  error?: string;
  created_at?: string;
  finished_at?: string;
};

type PlantedBreak = {
  id?: string;
  suite?: string;
  feature?: string;
  path?: string;
};

type PanelStatus = {
  watch: { enabled: boolean; interval_sec: number; last_run_job_id?: string | null };
  latest_judge?: DevtoolsJob | null;
  latest_autofix?: DevtoolsJob | null;
  planted_breaks?: {
    active?: boolean;
    count?: number;
    breaks?: PlantedBreak[];
    catalog_size?: number;
    planted_at?: string;
  };
  capabilities?: {
    gemini_configured?: boolean;
    autofix_ready?: boolean;
    autofix?: {
      ready?: boolean;
      fixer_mode?: string;
      git_available?: boolean;
      api_writable?: boolean;
      devtools_present?: boolean;
    };
    repo_root?: string;
  };
  suites?: { name: string; path: string; slow: boolean }[];
};

function statusColor(status: string) {
  if (status === "completed" || status === "pass") return "#22c55e";
  if (status === "running" || status === "queued") return "#f59e0b";
  if (status === "failed" || status === "fail") return "#ef4444";
  return "#94a3b8";
}

export default function DevtoolsControlPanel({
  apiBaseUrl,
  user,
}: {
  apiBaseUrl: string;
  user: UserLike | null;
}) {
  const [status, setStatus] = useState<PanelStatus | null>(null);
  const [activeJob, setActiveJob] = useState<DevtoolsJob | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const actorPayload = {
    actor_user_id: user?.id,
    actor_email: user?.email,
    actor_roles: user?.roles ?? ["master_admin"],
  };

  const fetchStatus = useCallback(async () => {
    try {
      setError("");
      const r = await fetch(`${apiBaseUrl}/api/devtools/status`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data: PanelStatus = await r.json();
      setStatus(data);
      const latest =
        data.latest_autofix?.status === "queued" || data.latest_autofix?.status === "running"
          ? data.latest_autofix
          : data.latest_judge?.status === "queued" || data.latest_judge?.status === "running"
            ? data.latest_judge
            : null;
      if (latest) {
        setActiveJob(latest);
        setBusy(latest.kind);
      } else {
        setBusy("");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [apiBaseUrl]);

  const pollJob = useCallback(
    (jobId: string) => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const r = await fetch(`${apiBaseUrl}/api/devtools/jobs/${jobId}`);
          if (!r.ok) return;
          const job: DevtoolsJob = await r.json();
          setActiveJob(job);
          if (job.status === "completed" || job.status === "failed" || job.status === "cancelled") {
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = null;
            setBusy("");
            void fetchStatus();
          }
        } catch {
          /* ignore poll errors */
        }
      }, 2500);
    },
    [apiBaseUrl, fetchStatus]
  );

  useEffect(() => {
    if (activeJob && (activeJob.status === "queued" || activeJob.status === "running")) {
      pollJob(activeJob.id);
    }
  }, [activeJob?.id, activeJob?.status, pollJob]);

  useEffect(() => {
    void fetchStatus();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchStatus]);

  const cancelActiveJob = async () => {
    const jobId =
      activeJob?.id ||
      status?.latest_autofix?.id ||
      status?.latest_judge?.id;
    if (!jobId) return;
    setBusy("cancel");
    try {
      await postJson(`/api/devtools/jobs/${jobId}/cancel`, {});
      setBusy("");
      setActiveJob(null);
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy("");
    }
  };

  const postJson = async (path: string, body: Record<string, unknown>) => {
    setError("");
    const r = await fetch(`${apiBaseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, ...actorPayload }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    return data as DevtoolsJob;
  };

  const startJudge = async (opts: { quick?: boolean; full?: boolean }) => {
    setBusy("judge");
    try {
      const job = await postJson("/api/devtools/judge/start", {
        quick: opts.quick ?? !opts.full,
        full: opts.full ?? false,
      });
      setActiveJob(job);
      pollJob(job.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy("");
    }
  };

  const startAutofix = async () => {
    setBusy("autofix");
    try {
      const job = await postJson("/api/devtools/autofix/start", {
        quick: true,
        max_attempts: 5,
      });
      setActiveJob(job);
      pollJob(job.id);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy("");
    }
  };

  const plantBreaks = async () => {
    setBusy("plant");
    try {
      setError("");
      const r = await fetch(`${apiBaseUrl}/api/devtools/plant-breaks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...actorPayload, count: 5 }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      if (!data.ok && data.message) setError(String(data.message));
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const restoreBreaks = async () => {
    setBusy("restore");
    try {
      await postJson("/api/devtools/restore-breaks", {});
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const [mergeResult, setMergeResult] = useState<{ merged: boolean; message: string; branch: string } | null>(null);

  const mergeBranch = async (branch: string) => {
    setBusy("merge");
    setMergeResult(null);
    try {
      const data = await postJson("/api/devtools/merge", { branch });
      setMergeResult(data as unknown as { merged: boolean; message: string; branch: string });
      if ((data as unknown as { merged?: boolean }).merged) {
        await fetchStatus();
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const toggleWatch = async (enabled: boolean) => {
    setBusy("watch");
    try {
      await postJson("/api/devtools/watch", {
        enabled,
        interval_sec: status?.watch?.interval_sec ?? 300,
        quick: true,
      });
      await fetchStatus();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const report = activeJob?.report as Record<string, unknown> | undefined;
  const nestedJudge = report?.judge as Record<string, unknown> | undefined;
  const judgeSummary = (report?.summary ?? nestedJudge?.summary) as Record<string, unknown> | undefined;
  const failures = (report?.failures ?? nestedJudge?.failures) as
    | { suite: string; check: string; detail: string; planted_break_id?: string }[]
    | undefined;
  const plantedCorrelation = (report?.planted_breaks ?? nestedJudge?.planted_breaks) as
    | {
        detected_count?: number;
        count?: number;
        detection_rate?: number;
        correlations?: {
          break_id?: string;
          suite: string;
          feature?: string;
          run_status: string;
          detected: boolean;
        }[];
      }
    | undefined;
  const phase = report?.phase as string | undefined;
  const branch = report?.branch as string | undefined;
  const attempts = report?.attempts as Record<string, unknown>[] | undefined;

  const phaseLabel: Record<string, string> = {
    starting: "Starting…",
    branch: "Creating autofix branch",
    judge: "Running judge",
    judge_done: "Judge complete",
    fixer: "AI fixer editing code",
    fixer_done: "Fixer finished",
    review: "Reviewer checking diff",
    review_rejected: "Fix rejected — retrying",
    commit: "Committing fix",
    attempt_done: "Attempt complete",
  };

  return (
    <section
      style={{
        marginBottom: 32,
        padding: 24,
        borderRadius: 12,
        border: "1px solid rgba(99, 102, 241, 0.35)",
        background: "linear-gradient(135deg, rgba(99,102,241,0.08), rgba(15,23,42,0.4))",
      }}
    >
      <p className="eyebrow">Autonomous AI Testing</p>
      <h2 style={{ margin: "4px 0 8px" }}>AI Testing Control Panel</h2>
      <p style={{ color: "var(--text-secondary)", marginBottom: 20, maxWidth: 720 }}>
        Run diagnostics, plant intentional breaks to demo autofix, or start the full loop — judge, AI code
        fix, reviewer, commit, repeat until green. Jobs run in the background; refresh or wait for live
        progress below.
      </p>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginBottom: 20 }}>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void plantBreaks()}
          style={{ background: "rgba(251,146,60,0.15)", border: "1px solid rgba(251,146,60,0.45)", color: "#fdba74" }}
        >
          {busy === "plant" ? "Planting breaks…" : "💥 Plant 5 random breaks"}
        </button>
        <button
          type="button"
          disabled={!!busy || !status?.planted_breaks?.active}
          onClick={() => void restoreBreaks()}
          style={{ background: "rgba(148,163,184,0.12)", border: "1px solid rgba(148,163,184,0.35)", color: "#e2e8f0" }}
        >
          {busy === "restore" ? "Restoring…" : "↩ Restore breaks"}
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void startJudge({ quick: true })}
          style={{ background: "rgba(34,197,94,0.15)", border: "1px solid rgba(34,197,94,0.4)", color: "#86efac" }}
        >
          {busy === "judge" ? "Judge running…" : "▶ Run Judge (Quick)"}
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void startJudge({ full: true })}
          style={{ background: "rgba(59,130,246,0.15)", border: "1px solid rgba(59,130,246,0.4)", color: "#93c5fd" }}
        >
          Run Judge (Full + Copilot Eval)
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void startAutofix()}
          style={{ background: "rgba(168,85,247,0.15)", border: "1px solid rgba(168,85,247,0.4)", color: "#d8b4fe" }}
        >
          {busy === "autofix" ? "Autofix running…" : "🔧 Start Autofix (fix code)"}
        </button>
        <button
          type="button"
          disabled={!!busy}
          onClick={() => void toggleWatch(!status?.watch?.enabled)}
          style={{
            background: status?.watch?.enabled ? "rgba(239,68,68,0.15)" : "rgba(148,163,184,0.12)",
            border: `1px solid ${status?.watch?.enabled ? "rgba(239,68,68,0.4)" : "rgba(148,163,184,0.3)"}`,
            color: status?.watch?.enabled ? "#fca5a5" : "#cbd5e1",
          }}
        >
          {status?.watch?.enabled ? "⏹ Stop Watch" : "👁 Enable Watch (5 min)"}
        </button>
        <button type="button" disabled={!!busy} onClick={() => void fetchStatus()}>
          Refresh status
        </button>
        {(activeJob?.status === "queued" ||
          activeJob?.status === "running" ||
          status?.latest_judge?.status === "queued" ||
          status?.latest_autofix?.status === "running") ? (
          <button
            type="button"
            disabled={!!busy}
            onClick={() => void cancelActiveJob()}
            style={{ background: "rgba(239,68,68,0.15)", border: "1px solid rgba(239,68,68,0.4)", color: "#fca5a5" }}
          >
            Cancel stuck job
          </button>
        ) : null}
      </div>

      {error ? (
        <p style={{ color: "#f87171", marginBottom: 12 }}>
          {error}
          {error.includes("already running") ? (
            <span> Click <strong>Refresh status</strong> or <strong>Cancel stuck job</strong>, then retry.</span>
          ) : null}
        </p>
      ) : null}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12, marginBottom: 20 }}>
        <div style={{ padding: 12, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>Watch</div>
          <div style={{ fontWeight: 600, color: status?.watch?.enabled ? "#22c55e" : "#94a3b8" }}>
            {status?.watch?.enabled ? "ON" : "OFF"}
          </div>
        </div>
        <div style={{ padding: 12, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>Latest judge</div>
          <div style={{ fontWeight: 600, color: statusColor(status?.latest_judge?.status ?? "") }}>
            {status?.latest_judge?.status ?? "—"}
          </div>
        </div>
        <div style={{ padding: 12, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>Gemini</div>
          <div style={{ fontWeight: 600 }}>{status?.capabilities?.gemini_configured ? "Ready" : "Not configured"}</div>
        </div>
        <div style={{ padding: 12, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>Planted breaks</div>
          <div
            style={{
              fontWeight: 600,
              color: status?.planted_breaks?.active ? "#fb923c" : "#94a3b8",
            }}
          >
            {status?.planted_breaks?.active
              ? `${status.planted_breaks.count} active`
              : "None"}
          </div>
        </div>
        <div style={{ padding: 12, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>Autofix</div>
          <div
            style={{
              fontWeight: 600,
              color: status?.capabilities?.autofix_ready ? "#22c55e" : "#f59e0b",
            }}
          >
            {status?.capabilities?.autofix_ready
              ? `Ready (${status?.capabilities?.autofix?.fixer_mode ?? "auto"})`
              : "Not ready"}
          </div>
        </div>
      </div>

      {status?.planted_breaks?.active && (status.planted_breaks.breaks?.length ?? 0) > 0 ? (
        <div
          style={{
            marginBottom: 20,
            padding: 14,
            borderRadius: 8,
            background: "rgba(251,146,60,0.08)",
            border: "1px solid rgba(251,146,60,0.35)",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 8, color: "#fdba74" }}>Active test breaks</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: "var(--text-secondary)", fontSize: 14 }}>
            {(status.planted_breaks.breaks ?? []).map((b) => (
              <li key={b.id}>
                <strong>{b.suite}</strong> — {b.feature}{" "}
                <span style={{ opacity: 0.7 }}>({b.path})</span>
              </li>
            ))}
          </ul>
          <p style={{ margin: "10px 0 0", fontSize: 13, color: "var(--text-secondary)" }}>
            Run <strong>Judge</strong> to see failures, then <strong>Autofix</strong> or fix manually. Use{" "}
            <strong>Restore breaks</strong> to undo without autofix.
          </p>
        </div>
      ) : null}

      {activeJob ? (
        <div style={{ marginBottom: 16, padding: 16, borderRadius: 8, background: "var(--surface-2)", border: "1px solid var(--border)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <strong>
              Active job: {activeJob.kind} · {activeJob.id.slice(0, 8)}…
            </strong>
            <span style={{ color: statusColor(activeJob.status), fontWeight: 600 }}>{activeJob.status}</span>
          </div>
          {activeJob.error ? <p style={{ color: "#f87171" }}>{activeJob.error}</p> : null}
          {phase ? (
            <p style={{ margin: "8px 0", fontSize: 14, color: "#c4b5fd" }}>
              Phase: {phaseLabel[phase] ?? phase}
              {branch ? ` · branch ${branch}` : ""}
            </p>
          ) : null}
          {judgeSummary ? (
            <p style={{ margin: "8px 0", fontSize: 14 }}>
              Suites: {String(judgeSummary.passed)}/{String(judgeSummary.total_suites)} passed ·{" "}
              {String(judgeSummary.total_check_failures ?? 0)} failures
            </p>
          ) : null}
          {plantedCorrelation && (plantedCorrelation.count ?? 0) > 0 ? (
            <p style={{ margin: "8px 0", fontSize: 14, color: "#fdba74" }}>
              Planted breaks: {plantedCorrelation.detected_count}/{plantedCorrelation.count} detected by judge
              {typeof plantedCorrelation.detection_rate === "number"
                ? ` (${Math.round(plantedCorrelation.detection_rate * 100)}%)`
                : ""}
            </p>
          ) : null}
          {plantedCorrelation?.correlations && plantedCorrelation.correlations.length > 0 ? (
            <div style={{ maxHeight: 120, overflow: "auto", fontSize: 12, marginBottom: 8 }}>
              {plantedCorrelation.correlations.map((c) => (
                <div key={c.break_id ?? c.suite} style={{ marginBottom: 4 }}>
                  <span style={{ color: c.detected ? "#86efac" : "#fbbf24" }}>
                    {c.detected ? "✓" : c.run_status === "not_run" ? "○" : "?"}
                  </span>{" "}
                  <strong>{c.suite}</strong> — {c.feature ?? c.break_id}
                  {c.run_status === "not_run" ? " (suite not run)" : ""}
                </div>
              ))}
            </div>
          ) : null}
          {failures && failures.length > 0 ? (
            <div style={{ maxHeight: 200, overflow: "auto", fontSize: 13 }}>
              {failures.slice(0, 15).map((f, i) => (
                <div key={i} style={{ marginBottom: 6 }}>
                  <span style={{ color: "#f87171" }}>[{f.suite}]</span> {f.check}: {f.detail?.slice(0, 120)}
                  {f.planted_break_id ? (
                    <span style={{ color: "#fdba74", marginLeft: 6 }}>← {f.planted_break_id}</span>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}
          {attempts && attempts.length > 0 ? (
            <div style={{ marginTop: 12 }}>
              <strong>Autofix attempts</strong>
              {attempts.map((a, i) => (
                <div key={i} style={{ fontSize: 13, marginTop: 8, padding: 8, background: "rgba(0,0,0,0.2)", borderRadius: 6 }}>
                  <div>Attempt {String(a.attempt)} — judge: {String(a.judge_overall)}</div>
                  {(a.review as { approve?: boolean; reason?: string })?.reason ? (
                    <div style={{ marginTop: 4, color: "var(--text-secondary)" }}>
                      Review: {(a.review as { approve?: boolean; reason?: string }).reason}
                    </div>
                  ) : null}
                  {typeof a.fixer_output === "string" && a.fixer_output ? (
                    <div style={{ marginTop: 4, color: "var(--text-secondary)", fontSize: 12 }}>
                      {(a.fixer_output as string).slice(0, 200)}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}
          {typeof report?.note === "string" ? (
            <p style={{ marginTop: 12, fontSize: 13, color: "var(--text-secondary)" }}>{report.note}</p>
          ) : null}
          {(report?.cleanup as { total_rows?: number; status?: string })?.total_rows !== undefined ? (
            <p style={{ marginTop: 8, fontSize: 13, color: "#86efac" }}>
              Cleanup: removed {(report?.cleanup as { total_rows?: number }).total_rows} diagnostic row(s)
            </p>
          ) : null}
          {activeJob.kind === "autofix" &&
          activeJob.status === "completed" &&
          report?.overall === "pass" &&
          branch ? (
            <div
              style={{
                marginTop: 16,
                padding: 14,
                borderRadius: 8,
                background: "rgba(34,197,94,0.08)",
                border: "1px solid rgba(34,197,94,0.35)",
              }}
            >
              <p style={{ margin: "0 0 10px", fontSize: 14, color: "#86efac" }}>
                All tests passing on <strong>{branch}</strong>. Review the diff, then merge into main —
                this is the only step that changes your main branch, and it only happens when you click it.
              </p>
              <button
                type="button"
                disabled={!!busy}
                onClick={() => void mergeBranch(branch)}
                style={{ background: "rgba(34,197,94,0.18)", border: "1px solid rgba(34,197,94,0.5)", color: "#bbf7d0" }}
              >
                {busy === "merge" ? "Merging…" : `✓ Merge ${branch} into main`}
              </button>
              {mergeResult && mergeResult.branch === branch ? (
                <p style={{ margin: "10px 0 0", fontSize: 13, color: mergeResult.merged ? "#86efac" : "#fca5a5" }}>
                  {mergeResult.merged ? "✓ " : "✗ "}
                  {mergeResult.message}
                </p>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
