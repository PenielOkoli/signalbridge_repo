"use client";

import { ArrowRight, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";

import { ThemeToggle } from "./theme-toggle";

type AuthMode = "login" | "signup";

export function AuthPanel({ mode }: { mode: AuthMode }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [message, setMessage] = useState("");
  const [signupEnabled, setSignupEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const isSignup = mode === "signup";

  useEffect(() => {
    let cancelled = false;

    async function loadAuthState() {
      try {
        const response = await fetch("/api/bridge/auth/state", {
          cache: "no-store",
          credentials: "include"
        });
        if (!response.ok) return;
        const body = (await response.json()) as { signup_enabled?: boolean };
        if (!cancelled) {
          setSignupEnabled(body.signup_enabled ?? true);
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
    if (isSignup && !signupEnabled) {
      setMessage("An owner account already exists for this SignalBridge workspace. Log in with that account.");
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

      window.location.assign("/dashboard");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Authentication failed.");
    } finally {
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
              <span className="block text-xs font-bold text-zinc-500">Trading signal copier</span>
            </span>
          </Link>

          <div className="max-w-2xl">
            <p className="landing-eyebrow">Account access</p>
            <h1 className="mt-4 text-5xl font-black leading-none sm:text-7xl">
              Your signals should not wait for you.
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-8 text-zinc-500">
              Access the owner console for this SignalBridge workspace, connect Telegram, add exchange keys, and keep execution guarded by your rules.
            </p>
          </div>

          <div className="grid gap-3 text-sm font-bold text-zinc-500 sm:grid-cols-3">
            <span className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-emerald-signal" />
              Minimal API permissions
            </span>
            <span className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-emerald-signal" />
              Channel-level control
            </span>
            <span className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-emerald-signal" />
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
            <p className="text-sm font-black uppercase tracking-[0.16em] text-emerald-signal">
              {isSignup ? "Owner setup" : "Welcome back"}
            </p>
            <h2 className="mt-3 text-3xl font-black">{isSignup ? "Create owner account" : "Log in to SignalBridge"}</h2>
            <p className="mt-3 leading-7 text-zinc-500">
              {isSignup
                ? "The first account owns this local SignalBridge workspace. Additional accounts do not create separate exchange workspaces."
                : "Use the owner account for this SignalBridge workspace."}
            </p>
          </div>

          {isSignup && !signupEnabled ? (
            <div className="mt-7 rounded-lg border border-gold-signal/30 bg-gold-signal/10 px-4 py-3 text-sm font-bold text-gold-signal">
              An owner account already exists. This deployment is currently single-workspace, so use the login page.
            </div>
          ) : null}

          <form className="mt-7 grid gap-4" onSubmit={handlePasswordAuth}>
            {isSignup ? (
              <label className="grid gap-2">
                <span className="text-xs font-black uppercase tracking-[0.14em] text-zinc-500">Name</span>
                <input className="auth-input" value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" />
              </label>
            ) : null}
            <label className="grid gap-2">
              <span className="text-xs font-black uppercase tracking-[0.14em] text-zinc-500">Email</span>
              <input className="auth-input" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" inputMode="email" />
            </label>
            <label className="grid gap-2">
              <span className="text-xs font-black uppercase tracking-[0.14em] text-zinc-500">Password</span>
              <input className="auth-input" value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete={isSignup ? "new-password" : "current-password"} />
            </label>

            {message ? <p className="rounded-lg border border-fuchsia-500/30 bg-fuchsia-500/10 px-3 py-2 text-sm font-bold text-fuchsia-300">{message}</p> : null}

            <button type="submit" className="primary-cta mt-1 w-full" disabled={busy || (isSignup && !signupEnabled)}>
              {busy ? "Working..." : isSignup ? "Create account" : "Log in"} <ArrowRight className="h-4 w-4" />
            </button>
          </form>

          <p className="mt-7 text-center text-sm text-zinc-500">
            {isSignup ? (
              <>
                Already have an account?{" "}
                <Link href="/login" className="font-black text-emerald-signal">
                  Log in
                </Link>
              </>
            ) : signupEnabled ? (
              <>
                Setting up this workspace?{" "}
                <Link href="/signup" className="font-black text-emerald-signal">
                  Create owner account
                </Link>
              </>
            ) : (
              "This workspace already has an owner account."
            )}
          </p>
        </div>
      </section>
    </main>
  );
}
