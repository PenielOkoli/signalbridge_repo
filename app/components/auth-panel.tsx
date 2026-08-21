"use client";

import { ArrowRight, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

import { ThemeToggle } from "./theme-toggle";

type AuthMode = "login" | "signup" | "forgot-password" | "reset-password";

export function AuthPanel({ mode }: { mode: AuthMode }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resetToken, setResetToken] = useState("");
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [signupEnabled, setSignupEnabled] = useState(true);
  const [googleOauthEnabled, setGoogleOauthEnabled] = useState(false);
  const [googleOauthError, setGoogleOauthError] = useState("");
  const [busy, setBusy] = useState(false);
  const isSignup = mode === "signup";
  const isForgotPassword = mode === "forgot-password";
  const isResetPassword = mode === "reset-password";
  const isRecovery = isForgotPassword || isResetPassword;

  useEffect(() => {
    if (isResetPassword) {
      setResetToken(new URLSearchParams(window.location.search).get("token") ?? "");
    }
  }, [isResetPassword]);

  useEffect(() => {
    if (mode === "login" && new URLSearchParams(window.location.search).get("passwordReset") === "1") {
      setMessage("Password updated. You can now log in with your new password.");
    }
  }, [mode]);

  useEffect(() => {
    let cancelled = false;

    async function loadAuthState() {
      try {
        const response = await fetch("/api/bridge/auth/state", {
          cache: "no-store",
          credentials: "include"
        });
        if (!response.ok) return;
        const body = (await response.json()) as { signup_enabled?: boolean; google_oauth_enabled?: boolean; google_oauth_error?: string };
        if (!cancelled) {
          setSignupEnabled(body.signup_enabled ?? true);
          setGoogleOauthEnabled(body.google_oauth_enabled ?? false);
          setGoogleOauthError(body.google_oauth_error ?? "");
        }
      } catch {
        // Keep signup enabled client-side; the backend remains authoritative.
      }
    }

    void loadAuthState();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handlePasswordAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isForgotPassword) {
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        setMessage("Enter a valid email address.");
        return;
      }
      await submitRecoveryRequest();
      return;
    }
    if (isResetPassword) {
      if (!resetToken) {
        setMessage("This password-reset link is invalid or incomplete. Request a new link.");
        return;
      }
      if (password.length < 8) {
        setMessage("Password must be at least 8 characters.");
        return;
      }
      if (password !== confirmPassword) {
        setMessage("Passwords do not match.");
        return;
      }
      await submitNewPassword();
      return;
    }
    if (isSignup && !signupEnabled) {
      setMessage("Signups are disabled for this deployment. Log in with an existing account.");
      return;
    }
    if (isSignup && name.trim().length < 2) {
      setMessage("Enter your name to create an account.");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      setMessage("Enter a valid email address.");
      return;
    }
    if (password.length < 8) {
      setMessage("Password must be at least 8 characters.");
      return;
    }

    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(`/api/bridge/auth/${isSignup ? "signup" : "login"}`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email,
          password,
          ...(isSignup ? { name } : {})
        })
      });

      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const body = (await response.json()) as { detail?: string };
          detail = body.detail ?? detail;
        } catch {
          // Keep the status text fallback.
        }
        setMessage(detail);
        return;
      }

      window.location.assign(isSignup ? "/onboarding" : "/dashboard");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Authentication failed.");
    } finally {
      setBusy(false);
    }
  }

  async function submitRecoveryRequest() {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/bridge/auth/forgot-password", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email })
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        setMessage(body.detail ?? "Password recovery is unavailable right now.");
        return;
      }
      setMessage("If an account exists for that email, a password-reset link has been sent. Check your inbox and spam folder.");
    } catch {
      setMessage("Password recovery is unavailable right now.");
    } finally {
      setBusy(false);
    }
  }

  async function submitNewPassword() {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/bridge/auth/reset-password", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: resetToken, password })
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        setMessage(body.detail ?? "Could not reset the password.");
        return;
      }
      window.location.assign("/login?passwordReset=1");
    } catch {
      setMessage("Could not reset the password. Request a new link and try again.");
    } finally {
      setBusy(false);
    }
  }

  async function handleGoogleSignIn() {
    setBusy(true);
    setMessage("");
    try {
      window.location.assign(`/api/bridge/auth/google/start?next=${encodeURIComponent("/dashboard")}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Google sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <aside className="auth-aside">
        <div className="relative z-10 flex h-full flex-col justify-between gap-10">
          <Link href="/" className="flex items-center gap-3">
            <span className="brand-mark">SB</span>
            <span>
              <span className="block text-sm font-black uppercase tracking-[0.22em]">SignalBridge</span>
              <span className="block text-xs font-bold text-ink-3">Trading signal copier</span>
            </span>
          </Link>

          <div className="max-w-2xl">
            <p className="landing-eyebrow">Account access</p>
            <h1 className="mt-4 text-5xl font-black leading-none sm:text-7xl">
              Your signals should not wait for you.
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-8 text-ink-3">
              Access your SignalBridge workspace, connect Telegram, add exchange keys, and keep execution guarded by your rules.
            </p>
          </div>

          <div className="grid gap-3 text-sm font-bold text-ink-3 sm:grid-cols-3">
            <span className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-accent" />
              Minimal API permissions
            </span>
            <span className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-accent" />
              Channel-level control
            </span>
            <span className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-accent" />
              Full execution log
            </span>
          </div>
        </div>
      </aside>

      <section className="auth-main">
        <div className="mb-5 flex justify-end">
          <ThemeToggle variant="ghost" />
        </div>
        <div className="auth-card">
          <div>
            <p className="text-sm font-black uppercase tracking-[0.16em] text-accent">
              {isSignup ? "Workspace setup" : isRecovery ? "Account recovery" : "Welcome back"}
            </p>
            <h2 className="mt-3 text-3xl font-black">
              {isSignup ? "Create account" : isForgotPassword ? "Reset your password" : isResetPassword ? "Choose a new password" : "Log in to SignalBridge"}
            </h2>
            <p className="mt-3 leading-7 text-ink-3">
              {isSignup
                ? "Create a new account to open its own SignalBridge workspace, Telegram session, and exchange settings."
                : isForgotPassword
                  ? "Enter your account email and we'll send a single-use reset link that expires in one hour."
                  : isResetPassword
                    ? "Set a new password for your SignalBridge account."
                    : "Use your account to open your own SignalBridge workspace."}
            </p>
          </div>

          {isSignup && !signupEnabled ? (
            <div className="mt-7 rounded-lg border border-warn/30 bg-warn/10 px-4 py-3 text-sm font-bold text-warn">
              Public signup is disabled for this deployment. Use an existing account to continue.
            </div>
          ) : null}

          {!isRecovery && googleOauthEnabled ? (
            <button type="button" onClick={handleGoogleSignIn} className="secondary-cta mt-7 w-full" disabled={busy}>
              Continue with Google
            </button>
          ) : !isRecovery && googleOauthError ? (
            <div className="mt-7 rounded-lg border border-panel-2 bg-field px-4 py-3 text-sm font-bold text-ink-2">
              {googleOauthError}
            </div>
          ) : null}

          <form className="mt-7 grid gap-4" onSubmit={handlePasswordAuth}>
            {isSignup ? (
              <label className="grid gap-2">
                <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">Name</span>
                <input className="auth-input" value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" />
              </label>
            ) : null}
            {!isResetPassword ? <label className="grid gap-2">
              <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">Email</span>
              <input className="auth-input" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" inputMode="email" />
            </label> : null}
            {!isForgotPassword ? <label className="grid gap-2">
              <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">Password</span>
              <input className="auth-input" value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete={isSignup ? "new-password" : "current-password"} />
            </label> : null}
            {isResetPassword ? <label className="grid gap-2">
              <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">Confirm new password</span>
              <input className="auth-input" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} type="password" autoComplete="new-password" />
            </label> : null}

            {message ? <p className="rounded-lg border border-fuchsia-500/30 bg-fuchsia-500/10 px-3 py-2 text-sm font-bold text-fuchsia-300">{message}</p> : null}

            <button type="submit" className="primary-cta mt-1 w-full" disabled={busy || (isSignup && !signupEnabled)}>
              {busy ? "Working..." : isSignup ? "Create account" : isForgotPassword ? "Send reset link" : isResetPassword ? "Set new password" : "Log in"} <ArrowRight className="h-4 w-4" />
            </button>
          </form>

          <p className="mt-7 text-center text-sm text-ink-3">
            {isSignup ? (
              <>
                Already have an account?{" "}
                <Link href="/login" className="font-black text-accent">
                  Log in
                </Link>
              </>
            ) : isRecovery ? (
              <Link href="/login" className="font-black text-accent">
                Back to log in
              </Link>
            ) : signupEnabled ? (
              <>
                Need a separate workspace?{" "}
                <Link href="/signup" className="font-black text-accent">
                  Create account
                </Link>
              </>
            ) : (
              "Public signup is disabled for this deployment."
            )}
          </p>
          {!isSignup && !isRecovery ? (
            <p className="mt-4 text-center text-sm text-ink-3">
              <Link href="/forgot-password" className="font-black text-accent">Forgot your password?</Link>
            </p>
          ) : null}
        </div>
      </section>
    </main>
  );
}
