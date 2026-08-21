"use client";

import { RadioTower, Search, ShieldCheck, Unlock } from "../components/icons";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ThemeToggle } from "../components/theme-toggle";
import {
  type BridgeConnection,
  type ConfigResponse,
  type StatusResponse,
  type TelegramChatOption,
  type TelegramChatsResponse,
  fetchBridgeJson,
  readBridgeConnection
} from "../lib/signalbridge";

const emptyConfig: ConfigResponse = {
  schema_version: 2,
  security: { api_bearer_token_set: false },
  telegram: { app_configured: false, phone_number: "", monitored_chats: [] },
  exchange: {
    exchange_id: "bybit",
    mode: "testnet",
    default_leverage: 3,
    api_key_set: false,
    api_secret_set: false,
    api_password_set: false
  },
  openai: { provider: "groq", model: "openai/gpt-oss-20b", request_timeout_seconds: 20, api_key_set: false },
  risk: {
    risk_mode: "fixed_usdt",
    fixed_usdt_risk: 25,
    balance_risk_percent: 1,
    max_leverage: 10,
    daily_trade_limit: null,
    max_take_profit_orders: 1
  }
};

type Step = "telegram" | "channels";

export default function OnboardingPage() {
  const defaultApiUrl = process.env.NEXT_PUBLIC_SIGNALBRIDGE_API_URL ?? "";
  const [connection, setConnection] = useState<BridgeConnection>({ apiUrl: defaultApiUrl, token: "" });
  const [config, setConfig] = useState<ConfigResponse>(emptyConfig);
  const [runtime, setRuntime] = useState<StatusResponse | null>(null);
  const [step, setStep] = useState<Step>("telegram");
  const [telegramCode, setTelegramCode] = useState("");
  const [telegramPassword, setTelegramPassword] = useState("");
  const [chatSearch, setChatSearch] = useState("");
  const [availableChats, setAvailableChats] = useState<TelegramChatOption[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const hydrate = useCallback(async (connectionOverride: BridgeConnection = connection) => {
    try {
      const [configResponse, statusResponse] = await Promise.all([
        fetchBridgeJson<ConfigResponse>(connectionOverride, "/config"),
        fetchBridgeJson<StatusResponse>(connectionOverride, "/status")
      ]);
      setConfig(configResponse);
      setRuntime(statusResponse);
      if (statusResponse.telegram.auth_state === "authenticated") {
        setStep("channels");
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to reach SignalBridge.");
    }
  }, [connection]);

  useEffect(() => {
    setConnection(readBridgeConnection(defaultApiUrl, ""));
  }, [defaultApiUrl]);

  useEffect(() => {
    void hydrate(connection);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connection]);

  async function saveTelegramFields(fields: Partial<ConfigResponse["telegram"]>) {
    const response = await fetchBridgeJson<ConfigResponse>(connection, "/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        security: { api_bearer_token: null },
        telegram: { phone_number: config.telegram.phone_number, monitored_chats: config.telegram.monitored_chats, ...fields },
        exchange: {
          exchange_id: config.exchange.exchange_id,
          mode: config.exchange.mode,
          default_leverage: config.exchange.default_leverage,
          api_key: null,
          api_secret: null,
          api_password: null
        },
        openai: {
          provider: config.openai.provider,
          model: config.openai.model,
          request_timeout_seconds: config.openai.request_timeout_seconds,
          api_key: null
        },
        risk: config.risk
      })
    });
    setConfig(response);
    return response;
  }

  async function requestCode() {
    setBusy("request-code");
    setErrorMessage(null);
    try {
      await saveTelegramFields({ phone_number: config.telegram.phone_number });
      const response = await fetchBridgeJson<StatusResponse>(connection, "/telegram/request-code", { method: "POST" });
      setRuntime(response);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to request a Telegram code.");
    } finally {
      setBusy(null);
    }
  }

  async function verifyCode() {
    setBusy("verify-code");
    setErrorMessage(null);
    try {
      const response = await fetchBridgeJson<StatusResponse>(connection, "/telegram/verify-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: telegramCode })
      });
      setRuntime(response);
      setTelegramCode("");
      if (response.telegram.auth_state === "authenticated") {
        setTelegramPassword("");
        await loadTelegramChats();
        setStep("channels");
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "That code didn't work.");
    } finally {
      setBusy(null);
    }
  }

  async function verifyTelegramPassword() {
    setBusy("verify-password");
    setErrorMessage(null);
    try {
      const response = await fetchBridgeJson<StatusResponse>(connection, "/telegram/verify-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: telegramPassword })
      });
      setRuntime(response);
      setTelegramPassword("");
      await loadTelegramChats();
      setStep("channels");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "That password didn't work.");
    } finally {
      setBusy(null);
    }
  }

  async function loadTelegramChats() {
    setBusy("load-chats");
    setErrorMessage(null);
    try {
      const response = await fetchBridgeJson<TelegramChatsResponse>(connection, "/telegram/chats");
      setAvailableChats(response.chats);
      setConfig((current) => ({
        ...current,
        telegram: {
          ...current.telegram,
          monitored_chats: response.selected.length ? response.selected : current.telegram.monitored_chats
        }
      }));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to load Telegram channels.");
    } finally {
      setBusy(null);
    }
  }

  function toggleMonitoredChat(sourceValue: string) {
    setConfig((current) => {
      const selected = current.telegram.monitored_chats.includes(sourceValue)
        ? current.telegram.monitored_chats.filter((item) => item !== sourceValue)
        : [...current.telegram.monitored_chats, sourceValue];
      return { ...current, telegram: { ...current.telegram, monitored_chats: selected } };
    });
  }

  async function finishOnboarding() {
    setBusy("finish");
    setErrorMessage(null);
    try {
      await saveTelegramFields({ monitored_chats: config.telegram.monitored_chats });
      window.location.assign("/dashboard");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Could not save your channel selection.");
    } finally {
      setBusy(null);
    }
  }

  const filteredChats = availableChats.filter((chat) => {
    const needle = chatSearch.trim().toLowerCase();
    if (!needle) return true;
    return [chat.title, chat.username ?? "", chat.source_value, chat.kind].some((value) => value.toLowerCase().includes(needle));
  });

  const authState = runtime?.telegram.auth_state ?? "unknown";
  const codeRequested = authState === "code_sent";
  const passwordRequired = authState === "password_required";
  const stepNumber = step === "telegram" ? 1 : 2;

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
            <p className="landing-eyebrow">Step {stepNumber} of 2</p>
            <h1 className="mt-4 text-5xl font-black leading-none sm:text-6xl">
              {step === "telegram" ? "Connect Telegram" : "Pick your channels"}
            </h1>
            <p className="mt-6 max-w-xl text-lg leading-8 text-ink-3">
              {step === "telegram"
                ? "SignalBridge reads signals from a Telegram account you control. Connect it once here -- you can always adjust it later in Settings."
                : "Choose which channels SignalBridge should watch for trading signals. You can add or remove channels any time in Settings."}
            </p>
          </div>

          <div className="grid gap-3 text-sm font-bold text-ink-3 sm:grid-cols-2">
            <span className={`flex items-center gap-2 ${step === "telegram" ? "text-accent" : ""}`}>
              <ShieldCheck className="h-4 w-4" />
              1. Connect Telegram
            </span>
            <span className={`flex items-center gap-2 ${step === "channels" ? "text-accent" : ""}`}>
              <ShieldCheck className="h-4 w-4" />
              2. Choose channels
            </span>
          </div>
        </div>
      </aside>

      <section className="auth-main">
        <div className="mb-5 flex justify-end">
          <ThemeToggle variant="ghost" />
        </div>
        <div className="auth-card">
          {errorMessage ? (
            <div className="mb-5 rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm font-bold text-danger">{errorMessage}</div>
          ) : null}

          {step === "telegram" ? (
            <>
              <div className="mb-6 flex items-center gap-2 text-sm font-black uppercase tracking-[0.16em] text-accent">
                <RadioTower className="h-4 w-4" />
                Connect Telegram
              </div>
              <label className="grid gap-2">
                <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">Phone number</span>
                <input
                  className="auth-input"
                  value={config.telegram.phone_number}
                  onChange={(event) => setConfig({ ...config, telegram: { ...config.telegram, phone_number: event.target.value } })}
                  placeholder="+15551234567"
                  inputMode="tel"
                />
              </label>

              {passwordRequired ? (
                <div className="mt-4 grid gap-4">
                  <div className="rounded-lg border border-accent/30 bg-accent/10 px-4 py-3 text-sm font-bold text-ink-2">
                    Telegram accepted your code and needs your two-factor password to finish signing in.
                  </div>
                  <label className="grid gap-2">
                    <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">Telegram 2FA password</span>
                    <input className="auth-input" type="password" value={telegramPassword} onChange={(event) => setTelegramPassword(event.target.value)} autoComplete="current-password" />
                  </label>
                  <button type="button" onClick={verifyTelegramPassword} className="primary-cta w-full" disabled={busy !== null || !telegramPassword}>
                    {busy === "verify-password" ? "Verifying..." : "Verify 2FA password"} <ArrowRight className="h-4 w-4" />
                  </button>
                </div>
              ) : codeRequested ? (
                <div className="mt-4 grid gap-4">
                  <label className="grid gap-2">
                    <span className="text-xs font-black uppercase tracking-[0.14em] text-ink-3">SMS code</span>
                    <input className="auth-input" value={telegramCode} onChange={(event) => setTelegramCode(event.target.value)} placeholder="Code from Telegram" />
                  </label>
                  <button type="button" onClick={verifyCode} className="primary-cta w-full" disabled={busy !== null || !telegramCode}>
                    {busy === "verify-code" ? "Verifying..." : "Verify code"} <ArrowRight className="h-4 w-4" />
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={requestCode}
                  className="primary-cta mt-6 w-full"
                  disabled={busy !== null || config.telegram.phone_number.trim().length < 6}
                >
                  <Unlock className="h-4 w-4" />
                  {busy === "request-code" ? "Sending..." : "Request code"}
                </button>
              )}
            </>
          ) : (
            <>
              <div className="mb-6 flex items-center gap-2 text-sm font-black uppercase tracking-[0.16em] text-accent">
                <RadioTower className="h-4 w-4" />
                Choose signal channels
              </div>
              <div className="flex items-center gap-2 rounded-lg border border-line bg-field px-3">
                <Search className="h-4 w-4 text-ink-3" />
                <input
                  value={chatSearch}
                  onChange={(event) => setChatSearch(event.target.value)}
                  placeholder="Search Telegram channels"
                  className="h-11 w-full bg-transparent text-sm text-ink-1 outline-none placeholder:text-ink-3"
                />
              </div>
              <div className="thin-scrollbar mt-4 max-h-72 overflow-auto rounded-xl border border-line bg-field">
                {filteredChats.length ? (
                  <div className="divide-y divide-line">
                    {filteredChats.map((chat) => {
                      const checked = config.telegram.monitored_chats.includes(chat.source_value);
                      return (
                        <label key={chat.peer_id} className="flex cursor-pointer items-center gap-3 px-4 py-3 hover:bg-hover">
                          <input type="checkbox" checked={checked} onChange={() => toggleMonitoredChat(chat.source_value)} className="h-5 w-5 accent-accent" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate font-semibold text-ink-1">{chat.title}</span>
                            <span className="block truncate text-sm text-ink-3">{chat.username ? `@${chat.username}` : chat.source_value}</span>
                          </span>
                        </label>
                      );
                    })}
                  </div>
                ) : (
                  <div className="px-4 py-8 text-center text-sm text-ink-3">
                    {busy === "load-chats" ? "Loading channels..." : "No channels found."}
                  </div>
                )}
              </div>
              <button type="button" onClick={finishOnboarding} className="primary-cta mt-6 w-full" disabled={busy !== null}>
                {busy === "finish" ? "Saving..." : "Finish setup"} <ArrowRight className="h-4 w-4" />
              </button>
            </>
          )}

          <p className="mt-7 text-center text-sm text-ink-3">
            Not now?{" "}
            <Link href="/dashboard" className="font-black text-accent">
              Skip -- I&apos;ll finish this in Settings
            </Link>
          </p>
        </div>
      </section>
    </main>
  );
}
