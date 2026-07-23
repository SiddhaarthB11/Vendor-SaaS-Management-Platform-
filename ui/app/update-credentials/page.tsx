"use client";

import { useEffect, useState } from "react";

const apiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type TokenInfo = {
  valid: boolean;
  assigned_name: string;
  assigned_email: string;
  username: string;
  expires_at: string | null;
};

export default function UpdateCredentialsPage() {
  const [token, setToken] = useState<string | null>(null);
  const [info, setInfo] = useState<TokenInfo | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const t = params.get("token");
    setToken(t);
    if (!t) {
      setError("No token provided. Please use the link from your email.");
      setLoading(false);
      return;
    }
    fetch(`${apiBaseUrl}/workflow-requests/credential-token/${t}`)
      .then(async (res) => {
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.detail ?? "Invalid or expired link.");
        }
        return res.json();
      })
      .then((data: TokenInfo) => setInfo(data))
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const res = await fetch(`${apiBaseUrl}/workflow-requests/credential-token/${token}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_password: newPassword }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail ?? "Failed to update password.");
      }
      setSuccess(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main style={{
      minHeight: "100vh",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
      padding: "24px",
    }}>
      <div style={{
        background: "rgba(30,41,59,0.95)",
        border: "1px solid rgba(255,255,255,0.1)",
        borderRadius: "16px",
        padding: "40px",
        maxWidth: "440px",
        width: "100%",
        boxShadow: "0 25px 50px rgba(0,0,0,0.5)",
      }}>
        <div style={{ marginBottom: "28px", textAlign: "center" }}>
          <div style={{ fontSize: "32px", marginBottom: "8px" }}>🔑</div>
          <h1 style={{ color: "#f8fafc", fontSize: "22px", fontWeight: 700, margin: 0 }}>
            Update Licence Password
          </h1>
          <p style={{ color: "rgba(255,255,255,0.5)", fontSize: "14px", margin: "8px 0 0" }}>
            Derisk360 SLMCT Platform
          </p>
        </div>

        {loading && (
          <p style={{ color: "rgba(255,255,255,0.6)", textAlign: "center" }}>Verifying link…</p>
        )}

        {!loading && error && !success && (
          <div style={{
            background: "rgba(239,68,68,0.15)",
            border: "1px solid rgba(239,68,68,0.4)",
            borderRadius: "8px",
            padding: "16px",
            color: "#fca5a5",
            textAlign: "center",
          }}>
            {error}
          </div>
        )}

        {!loading && info && !success && (
          <>
            <div style={{
              background: "rgba(20,184,166,0.1)",
              border: "1px solid rgba(20,184,166,0.3)",
              borderRadius: "8px",
              padding: "14px 16px",
              marginBottom: "24px",
            }}>
              <p style={{ color: "rgba(255,255,255,0.7)", fontSize: "13px", margin: 0 }}>
                Updating password for <strong style={{ color: "#f8fafc" }}>{info.assigned_name || info.assigned_email}</strong>
                {info.username && info.username !== "—" && (
                  <><br />Username: <strong style={{ color: "#f8fafc" }}>{info.username}</strong></>
                )}
              </p>
            </div>

            <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
              <div>
                <label style={{ color: "rgba(255,255,255,0.7)", fontSize: "13px", fontWeight: 600, display: "block", marginBottom: "6px" }}>
                  New Password
                </label>
                <input
                  type="password"
                  value={newPassword}
                  onChange={e => setNewPassword(e.target.value)}
                  required
                  minLength={8}
                  placeholder="At least 8 characters"
                  style={{
                    width: "100%",
                    padding: "10px 14px",
                    borderRadius: "8px",
                    border: "1px solid rgba(255,255,255,0.15)",
                    background: "rgba(255,255,255,0.05)",
                    color: "#f8fafc",
                    fontSize: "14px",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>
              <div>
                <label style={{ color: "rgba(255,255,255,0.7)", fontSize: "13px", fontWeight: 600, display: "block", marginBottom: "6px" }}>
                  Confirm New Password
                </label>
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={e => setConfirmPassword(e.target.value)}
                  required
                  placeholder="Re-enter password"
                  style={{
                    width: "100%",
                    padding: "10px 14px",
                    borderRadius: "8px",
                    border: "1px solid rgba(255,255,255,0.15)",
                    background: "rgba(255,255,255,0.05)",
                    color: "#f8fafc",
                    fontSize: "14px",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>
              {error && (
                <p style={{ color: "#fca5a5", fontSize: "13px", margin: 0 }}>{error}</p>
              )}
              <button
                type="submit"
                disabled={submitting}
                style={{
                  background: submitting ? "rgba(20,184,166,0.5)" : "#14b8a6",
                  color: "#fff",
                  border: "none",
                  borderRadius: "8px",
                  padding: "12px",
                  fontSize: "15px",
                  fontWeight: 700,
                  cursor: submitting ? "not-allowed" : "pointer",
                  marginTop: "4px",
                }}
              >
                {submitting ? "Updating…" : "Update Password"}
              </button>
            </form>
          </>
        )}

        {success && (
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: "48px", marginBottom: "16px" }}>✅</div>
            <h2 style={{ color: "#f8fafc", fontSize: "18px", fontWeight: 700, margin: "0 0 8px" }}>
              Password Updated
            </h2>
            <p style={{ color: "rgba(255,255,255,0.6)", fontSize: "14px", margin: 0 }}>
              Your licence account password has been updated successfully. You can now close this page.
            </p>
          </div>
        )}
      </div>
    </main>
  );
}
