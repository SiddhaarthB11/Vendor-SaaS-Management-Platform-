"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);

    try {
      const baseUrl = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
      const response = await fetch(`${baseUrl}/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      if (!response.ok) {
        let detail = "Invalid username or password.";
        try {
          const data = await response.json();
          if (typeof data?.detail === "string") detail = data.detail;
        } catch {
          /* ignore */
        }
        setError(detail);
        return;
      }

      const data = await response.json();
      sessionStorage.setItem("slmct_user", JSON.stringify(data.user));
      router.push("/dashboard");
    } catch {
      setError("Unable to reach the login service.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="brand-panel" aria-label="Derisk360 subscription analytics preview">
        <nav className="brand-nav" aria-label="Product">
          <div className="brand-mark" aria-label="Derisk360">
            <img src="/derisk-logo-png.avif" alt="Derisk360" />
          </div>
          <span className="environment-pill">Internal</span>
        </nav>

        <div className="hero-copy">
          <p className="eyebrow">SLMCT Platform</p>
          <h1>Subscription, licences, and cost control across the Derisk360 group.</h1>
          <p>
            Track software spend by organisation, department, vendor, owner, renewal date,
            and allocated budget from one secure workspace.
          </p>
        </div>

        <div className="security-board" aria-label="Internal application security notice">
          <div className="security-board-icon" aria-hidden="true">
            <span />
          </div>
          <div>
            <span className="board-kicker">Security notification</span>
            <h2>Authorised Derisk360 internal use only.</h2>
            <p>
              You are accessing an organisational internal application for managing software
              subscriptions, licences, budgets, and cost tracking across Derisk360 entities.
            </p>
            <p>
              Access is monitored and protected. Use of this system is subject to Derisk360
              internal policies, approved business purposes, and role-based permissions.
            </p>
          </div>
        </div>
      </section>

      <section className="auth-panel" aria-label="Sign in">
        <div className="auth-card">
          <div className="auth-heading">
            <p className="eyebrow">Secure access</p>
            <h2>Sign in to SLMCT</h2>
            <p>Use your Derisk360 work account to continue.</p>
          </div>

          <form className="login-form" onSubmit={handleSubmit}>
            <label>
              Username
              <input
                type="text"
                placeholder="Enter your username"
                autoComplete="new-password"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>

            <label>
              Password
              <input
                type="password"
                placeholder="Enter password"
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </label>

            {error ? <p className="form-error" role="alert">{error}</p> : null}

            <div className="form-options">
              <label className="check-row">
                <input type="checkbox" />
                <span>Remember this device</span>
              </label>
              <a href="#">Forgot password?</a>
            </div>

            <button type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Signing in..." : "Sign in"}
            </button>
          </form>

          <div className="security-note">
            <strong>Protected workspace</strong>
            <span>2FA and role-based access will be enforced for production use.</span>
          </div>
        </div>
      </section>
    </main>
  );
}
