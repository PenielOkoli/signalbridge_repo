"use client";

import {
  Activity,
  Cable,
  KeyRound,
  LogOut,
  RadioTower,
  Save,
  Search,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  Unlock
} from "../components/icons";
import Link from "next/link";
import type { FormEvent, ReactNode } from "react";
import { useCallback, useEffect, useState } from "react";

import { ThemeToggle } from "../components/theme-toggle";
import {
  type BridgeConnection,
  type ConfigResponse,
  type ExchangeId,
  type StatusResponse,
  type TelegramChatOption,
  type TelegramChatsResponse,
  fetchBridgeJson,
  formatUtcTimestamp,
  readBridgeConnection,
  splitLines
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
    max_take_profit_orders: 1,
    enabled_symbols: []
  }
};

const EXCHANGE_OPTIONS: Array<{ label: string; value: ExchangeId }> = [
  { label: "Bybit", value: "bybit" },
  { label: "Binance USD-M", value: "binanceusdm" },
  { label: "OKX", value: "okx" },
  { label: "Bitget", value: "bitget" },
  { label: "BingX", value: "bingx" },
  { label: "KuCoin Futures", value: "kucoinfutures" },
  { label: "MEXC", value: "mexc" },
  { label: "Gate.io", value: "gateio" },
  { label: "Phemex", value: "phemex" },
  { label: "CoinEx", value: "coinex" }
];

export default function SettingsPage() {
  const defaultApiUrl = process.env.NEXT_PUBLIC_SIGNALBRIDGE_API_URL ?? "";
  // SECURITY: Never expose the bearer token to the frontend.
  // The proxy route (/api/bridge) handles authentication server-side.
  const [connection, setConnection] = useState<BridgeConnection>({ apiUrl: defaultApiUrl, token: "" });
  const [config, setConfig] = useState<ConfigResponse>(emptyConfig);
  const [runtime, setRuntime] = useState<StatusResponse | null>(null);
  const [secrets, setSecrets] = useState({ exchangeApiKey: "", exchangeApiSecret: "", exchangeApiPassword: "" });
  const [telegramCode, setTelegramCode] = useState("");
  const [telegramPassword, setTelegramPassword] = useState("");
  const [chatSearch, setChatSearch] = useState("");
  const [availableChats, setAvailableChats] = useState<TelegramChatOption[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [statusText, setStatusText] = useState("Not connected");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const hydrate = useCallback(async (connectionOverride: BridgeConnection = connection) => {
    setBusy("load");
    try {
      const [configResponse, statusResponse] = await Promise.all([
        fetchBridgeJson<ConfigResponse>(connectionOverride, "/config"),
        fetchBridgeJson<StatusResponse>(connectionOverride, "/status")
      ]);
      setConfig(configResponse);
      setRuntime(statusResponse);
      setStatusText(statusResponse.config.ready_for_trading ? "Ready" : "Setup needed");
      setErrorMessage(null);
    } catch (error) {
      setStatusText("Offline");
      setErrorMessage(error instanceof Error ? error.message : "Unable to reach SignalBridge.");
    } finally {
      setBusy(null);
    }
  }, [connection]);

  const loadTelegramChats = useCallback(async (connectionOverride: BridgeConnection = connection) => {
    setBusy("load-chats");
    try {
      const response = await fetchBridgeJson<TelegramChatsResponse>(connectionOverride, "/telegram/chats");
      setAvailableChats(response.chats);
      setConfig((current) => ({
        ...current,
        telegram: {
          ...current.telegram,
          monitored_chats: response.selected.length ? response.selected : current.telegram.monitored_chats
        }
      }));
      setStatusText("Channels loaded");
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to load Telegram channels.");
    } finally {
      setBusy(null);
    }
  }, [connection]);

  useEffect(() => {
    setConnection(readBridgeConnection(defaultApiUrl, ""));
  }, [defaultApiUrl]);

  useEffect(() => {
    void hydrate(connection);
  }, [connection, hydrate]);

  function configPayload() {
    return {
      security: { api_bearer_token: null },
      telegram: {
        phone_number: config.telegram.phone_number,
        monitored_chats: config.telegram.monitored_chats
      },
      exchange: {
        exchange_id: config.exchange.exchange_id,
        mode: config.exchange.mode,
        default_leverage: config.exchange.default_leverage,
        api_key: secrets.exchangeApiKey || null,
        api_secret: secrets.exchangeApiSecret || null,
        api_password: secrets.exchangeApiPassword || null
      },
      openai: {
        provider: config.openai.provider,
        model: config.openai.model,
        request_timeout_seconds: config.openai.request_timeout_seconds,
        api_key: null
      },
      risk: config.risk
    };
  }

  async function saveCurrentConfig() {
    const response = await fetchBridgeJson<ConfigResponse>(connection, "/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(configPayload())
    });
    setConfig(response);
    setSecrets({ exchangeApiKey: "", exchangeApiSecret: "", exchangeApiPassword: "" });
    return response;
  }

  async function saveConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy("save");
    try {
      await saveCurrentConfig();
      await hydrate(connection);
      setStatusText("Saved");
      setErrorMessage(null);
    } catch (error) {
      setStatusText("Save failed");
      setErrorMessage(error instanceof Error ? error.message : "Save failed.");
    } finally {
      setBusy(null);
    }
  }

  async function requestCode() {
    setBusy("request-code");
    try {
      await saveCurrentConfig();
      const response = await fetchBridgeJson<StatusResponse>(connection, "/telegram/request-code", { method: "POST" });
      setRuntime(response);
      setStatusText("Code sent");
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Unable to request Telegram code.");
    } finally {
      setBusy(null);
    }
  }

  async function verifyCode() {
    setBusy("verify-code");
    try {
      const response = await fetchBridgeJson<StatusResponse>(connection, "/telegram/verify-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: telegramCode, password: telegramPassword || null })
      });
      setRuntime(response);
      setTelegramCode("");
      setTelegramPassword("");
      setStatusText("Telegram connected");
      setErrorMessage(null);
      void loadTelegramChats(connection);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Telegram code failed.");
    } finally {
      setBusy(null);
    }
  }

  async function logoutTelegram() {
    setBusy("logout");
    try {
      const response = await fetchBridgeJson<StatusResponse>(connection, "/telegram/logout", { method: "POST" });
      setRuntime(response);
      setAvailableChats([]);
      setStatusText("Telegram logged out");
      setErrorMessage(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Telegram logout failed.");
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

  const filteredChats = availableChats.filter((chat) => {
    const needle = chatSearch.trim().toLowerCase();
    if (!needle) return true;
    return [chat.title, chat.username ?? "", chat.source_value, chat.kind].some((value) => value.toLowerCase().includes(needle));
  });

  return (
    <main className="app-shell">
      <div className="simple-app-frame">
        <AppSidebar active="Settings" />

        <section className="min-w-0">
          <header className="simple-page-header">
            <div>
              <p className="simple-eyebrow">Bot setup</p>
              <h1 className="simple-title">Settings</h1>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <ThemeToggle variant="ghost" />
              <StatusBadge value={statusText} />
              <Link href="/dashboard" className="simple-secondary-button">
                <Activity className="h-4 w-4" />
                Home
              </Link>
            </div>
          </header>

          <form onSubmit={saveConfig} className="simple-page">
            {errorMessage ? <div className="rounded-xl border border-red-400/30 bg-red-400/10 px-4 py-3 text-sm text-red-200">{errorMessage}</div> : null}

            <section className="grid gap-4 lg:grid-cols-4">
              <StepCard number="1" title="Telegram" done={runtime?.telegram.auth_state === "authenticated"} />
              <StepCard number="2" title="Channels" done={config.telegram.monitored_chats.length > 0} />
              <StepCard number="3" title="Exchange" done={Boolean(config.exchange.api_key_set && config.exchange.api_secret_set)} />
              <StepCard number="4" title="Risk" done={config.risk.fixed_usdt_risk > 0 && config.risk.max_leverage > 0} />
            </section>

            <SimplePanel icon={<RadioTower className="h-5 w-5" />} title="1. Connect Telegram" right={(runtime?.telegram.auth_state ?? "unknown").replace("_", " ")}>
              <div className="grid gap-3 md:grid-cols-2">
                <Field label="Phone number" value={config.telegram.phone_number} onChange={(value) => setConfig({ ...config, telegram: { ...config.telegram, phone_number: value } })} placeholder="+15551234567" />
                <InfoRow label="Last code" value={formatUtcTimestamp(runtime?.telegram.code_sent_at ?? null)} />
                <Field label="SMS code" value={telegramCode} onChange={setTelegramCode} placeholder="Code from Telegram" />
                <Field label="2FA password" type="password" value={telegramPassword} onChange={setTelegramPassword} placeholder="Only if Telegram asks" />
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <button type="button" onClick={requestCode} className="simple-secondary-button" disabled={busy !== null || runtime?.telegram.auth_state === "code_sent" || runtime?.telegram.auth_state === "password_required" || runtime?.telegram.auth_state === "authenticated"}>
                  <Unlock className="h-4 w-4" />
                  {runtime?.telegram.auth_state === "code_sent" || runtime?.telegram.auth_state === "password_required" ? "Code sent" : "Request code"}
                </button>
                <button type="button" onClick={verifyCode} className="simple-primary-button" disabled={busy !== null || !telegramCode || runtime?.telegram.auth_state === "authenticated"}>
                  <ShieldCheck className="h-4 w-4" />
                  Verify code
                </button>
                <button type="button" onClick={logoutTelegram} className="simple-secondary-button" disabled={busy !== null}>
                  <LogOut className="h-4 w-4" />
                  Log out
                </button>
              </div>
            </SimplePanel>

            <SimplePanel icon={<RadioTower className="h-5 w-5" />} title="2. Choose signal channels" right={`${config.telegram.monitored_chats.length} selected`}>
              <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
                <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-[#07080c] px-3">
                  <Search className="h-4 w-4 text-zinc-500" />
                  <input value={chatSearch} onChange={(event) => setChatSearch(event.target.value)} placeholder="Search Telegram channels" className="h-11 w-full bg-transparent text-sm text-zinc-100 outline-none placeholder:text-zinc-600" />
                </div>
                <button type="button" onClick={() => void loadTelegramChats()} className="simple-secondary-button" disabled={busy !== null || runtime?.telegram.auth_state !== "authenticated"}>
                  Load channels
                </button>
              </div>
              <div className="thin-scrollbar mt-4 max-h-80 overflow-auto rounded-xl border border-white/10 bg-black/20">
                {filteredChats.length ? (
                  <div className="divide-y divide-white/10">
                    {filteredChats.map((chat) => {
                      const checked = config.telegram.monitored_chats.includes(chat.source_value);
                      return (
                        <label key={chat.peer_id} className="flex cursor-pointer items-center gap-3 px-4 py-3 hover:bg-white/[0.035]">
                          <input type="checkbox" checked={checked} onChange={() => toggleMonitoredChat(chat.source_value)} className="h-5 w-5 accent-emerald-signal" />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate font-semibold text-zinc-100">{chat.title}</span>
                            <span className="block truncate text-sm text-zinc-500">{chat.username ? `@${chat.username}` : chat.source_value}</span>
                          </span>
                        </label>
                      );
                    })}
                  </div>
                ) : (
                  <div className="px-4 py-8 text-center text-sm text-zinc-500">No channels loaded.</div>
                )}
              </div>
            </SimplePanel>

            <section className="grid gap-4 xl:grid-cols-2">
              <SimplePanel icon={<KeyRound className="h-5 w-5" />} title="3. Connect exchange" right={config.exchange.mode}>
                <div className="grid gap-3 md:grid-cols-2">
                  <Select label="Exchange" value={config.exchange.exchange_id} onChange={(value) => setConfig({ ...config, exchange: { ...config.exchange, exchange_id: value as ExchangeId } })} options={EXCHANGE_OPTIONS} />
                  <Select label="Mode" value={config.exchange.mode} onChange={(value) => setConfig({ ...config, exchange: { ...config.exchange, mode: value as "testnet" | "mainnet" } })} options={[{ label: "Testnet", value: "testnet" }, { label: "Mainnet", value: "mainnet" }]} />
                  <Field label={`API key ${config.exchange.api_key_set ? "(already saved)" : ""}`} value={secrets.exchangeApiKey} onChange={(value) => setSecrets({ ...secrets, exchangeApiKey: value })} />
                  <Field label={`API secret ${config.exchange.api_secret_set ? "(already saved)" : ""}`} type="password" value={secrets.exchangeApiSecret} onChange={(value) => setSecrets({ ...secrets, exchangeApiSecret: value })} />
                  <Field label={`Passphrase ${config.exchange.api_password_set ? "(already saved)" : ""}`} type="password" value={secrets.exchangeApiPassword} onChange={(value) => setSecrets({ ...secrets, exchangeApiPassword: value })} placeholder="Only for exchanges that need it" />
                  <Field label="Default leverage" value={String(config.exchange.default_leverage)} onChange={(value) => setConfig({ ...config, exchange: { ...config.exchange, default_leverage: Number(value || 0) } })} />
                </div>
              </SimplePanel>

              <SimplePanel icon={<SlidersHorizontal className="h-5 w-5" />} title="4. Choose risk rules" right={config.risk.risk_mode.replace("_", " ")}>
                <div className="grid gap-3 md:grid-cols-2">
                  <Select label="Risk mode" value={config.risk.risk_mode} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, risk_mode: value as "fixed_usdt" | "balance_percent" } })} options={[{ label: "Fixed USDT", value: "fixed_usdt" }, { label: "Balance percent", value: "balance_percent" }]} />
                  <Field label="Max leverage" value={String(config.risk.max_leverage)} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, max_leverage: Number(value || 0) } })} />
                  <Field label="Risk per trade in USDT" value={String(config.risk.fixed_usdt_risk)} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, fixed_usdt_risk: Number(value || 0) } })} />
                  <Field label="Risk per trade in %" value={String(config.risk.balance_risk_percent)} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, balance_risk_percent: Number(value || 0) } })} />
                  <TextArea label="Allowed trading symbols" value={config.risk.enabled_symbols.join("\n")} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, enabled_symbols: splitLines(value) } })} />
                </div>
              </SimplePanel>
            </section>

            <div className="sticky bottom-4 z-10 flex justify-end">
              <button type="submit" className="simple-primary-button shadow-terminal" disabled={busy !== null}>
                <Save className="h-4 w-4" />
                Save all settings
              </button>
            </div>
          </form>
        </section>
      </div>
    </main>
  );
}

function AppSidebar({ active }: { active: string }) {
  const links = [
    { label: "Home", icon: <Activity className="h-4 w-4" />, href: "/dashboard" },
    { label: "Settings", icon: <Settings className="h-4 w-4" />, href: "/settings" }
  ];

  return (
    <aside className="simple-sidebar">
      <div className="mb-8 flex items-center gap-3">
        <div className="simple-brand-mark">
          <Cable className="h-5 w-5" />
        </div>
        <div>
          <div className="font-semibold text-zinc-50">SignalBridge</div>
          <div className="text-xs text-zinc-500">My trading bot</div>
        </div>
      </div>
      <nav className="space-y-2">
        {links.map((link) => (
          <Link key={link.label} href={link.href} className="simple-nav-link" data-active={link.label === active}>
            {link.icon}
            {link.label}
          </Link>
        ))}
      </nav>
    </aside>
  );
}

function StatusBadge({ value }: { value: string }) {
  return <span className="rounded-full border border-white/10 bg-white/[0.045] px-3 py-2 text-sm font-semibold text-zinc-200">{value}</span>;
}

function StepCard({ number, title, done }: { number: string; title: string; done: boolean }) {
  return (
    <div className="simple-panel p-4">
      <div className={`flex h-9 w-9 items-center justify-center rounded-full text-sm font-bold ${done ? "bg-emerald-signal text-emerald-dim" : "bg-zinc-800 text-zinc-300"}`}>
        {done ? <ShieldCheck className="h-5 w-5" /> : number}
      </div>
      <div className="mt-3 font-semibold text-zinc-100">{title}</div>
      <div className="mt-1 text-sm text-zinc-500">{done ? "Done" : "Needs setup"}</div>
    </div>
  );
}

function SimplePanel({ icon, title, right, children }: { icon: ReactNode; title: string; right: string; children: ReactNode }) {
  return (
    <section className="simple-panel">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
        <div className="flex items-center gap-2 font-semibold text-zinc-100">
          {icon}
          {title}
        </div>
        <span className="text-sm text-zinc-500">{right}</span>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className="mono-num mt-1 text-sm text-zinc-200">{value}</div>
    </div>
  );
}

function Field({ label, value, onChange, type = "text", placeholder = "" }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-zinc-300">{label}</span>
      <input value={value} type={type} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="input-field h-11 text-sm" />
    </label>
  );
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: Array<{ label: string; value: string }> }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-zinc-300">{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)} className="input-field h-11 text-sm">
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function TextArea({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block md:col-span-2">
      <span className="mb-1 block text-sm font-medium text-zinc-300">{label}</span>
      <textarea value={value} onChange={(event) => onChange(event.target.value)} rows={4} className="thin-scrollbar w-full resize-y rounded-lg border border-white/10 bg-[#07080c] px-3 py-3 text-sm text-zinc-200 outline-none transition focus:border-emerald-signal/70 focus:ring-2 focus:ring-emerald-signal/15" placeholder="BTC/USDT:USDT" />
    </label>
  );
}
