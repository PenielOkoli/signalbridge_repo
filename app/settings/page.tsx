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

const EXCHANGE_SETUP_GUIDES: Record<ExchangeId, { title: string; steps: string[]; passphrase: string; safety: string }> = {
  bybit: {
    title: "Bybit key setup",
    steps: [
      "Open Bybit, then go to API Management.",
      "Create a new API key for futures/contract trading.",
      "Allow trading permissions, disable withdrawals, and add an IP whitelist if your account asks for one."
    ],
    passphrase: "Bybit keys do not usually need a passphrase.",
    safety: "Use read + trade permissions only. Never enable withdrawals."
  },
  binanceusdm: {
    title: "Binance USD-M key setup",
    steps: [
      "Open Binance, then go to API Management.",
      "Create an API key and enable futures trading access.",
      "Do not enable withdrawal permissions."
    ],
    passphrase: "Binance USD-M keys do not use a passphrase.",
    safety: "Use futures trading only and keep withdrawals off."
  },
  okx: {
    title: "OKX key setup",
    steps: [
      "Open OKX, then go to API Management.",
      "Create a trading API key for derivatives or futures.",
      "Copy the passphrase shown during setup and keep withdrawal access off."
    ],
    passphrase: "OKX requires a passphrase.",
    safety: "Choose trading permission only and save the passphrase immediately."
  },
  bitget: {
    title: "Bitget key setup",
    steps: [
      "Open Bitget, then go to API Management.",
      "Create a futures API key for trading only.",
      "Save the passphrase if Bitget shows one and keep withdrawals disabled."
    ],
    passphrase: "Bitget usually requires a passphrase.",
    safety: "Trading on, withdrawals off, passphrase saved if shown."
  },
  bingx: {
    title: "BingX key setup",
    steps: [
      "Open BingX, then go to API Management.",
      "Create a futures trading API key.",
      "Allow trading, block withdrawals, and copy any passphrase BingX provides."
    ],
    passphrase: "BingX may require a passphrase depending on the account type.",
    safety: "If BingX asks for IP restrictions, turn them on."
  },
  kucoinfutures: {
    title: "KuCoin Futures key setup",
    steps: [
      "Open KuCoin, then go to API Management.",
      "Create a futures API key with trading access.",
      "Keep withdrawal permissions off and save the API passphrase."
    ],
    passphrase: "KuCoin Futures requires a passphrase.",
    safety: "Trading only; passphrase required; never give withdrawal permission."
  },
  mexc: {
    title: "MEXC key setup",
    steps: [
      "Open MEXC, then go to API Management.",
      "Create an API key for futures trading.",
      "Disable withdrawals and copy any passphrase shown during setup."
    ],
    passphrase: "MEXC may require a passphrase.",
    safety: "Use futures permissions only."
  },
  gateio: {
    title: "Gate.io key setup",
    steps: [
      "Open Gate.io, then go to API Management.",
      "Create an API key and allow futures trading.",
      "Keep withdrawal permissions off and save the passphrase if Gate.io provides one."
    ],
    passphrase: "Gate.io may require a passphrase.",
    safety: "Gate.io keys should be trade-only and withdrawal disabled."
  },
  phemex: {
    title: "Phemex key setup",
    steps: [
      "Open Phemex, then go to API Management.",
      "Create a futures trading API key.",
      "Disable withdrawals and keep the passphrase from the key creation screen."
    ],
    passphrase: "Phemex requires a passphrase.",
    safety: "Save the passphrase when you create the key because it is usually shown once."
  },
  coinex: {
    title: "CoinEx key setup",
    steps: [
      "Open CoinEx, then go to API Management.",
      "Create a futures API key for trading only.",
      "Do not enable withdrawals, and copy the passphrase if CoinEx gives you one."
    ],
    passphrase: "CoinEx may require a passphrase.",
    safety: "Trade-only access is enough for SignalBridge."
  }
};

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
            {errorMessage ? <div className="rounded-xl border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">{errorMessage}</div> : null}

            <section className="grid gap-4 lg:grid-cols-4">
              <StepCard number="1" title="Telegram" done={runtime?.telegram.auth_state === "authenticated"} />
              <StepCard number="2" title="Channels" done={config.telegram.monitored_chats.length > 0} />
              <StepCard number="3" title="Exchange key" done={Boolean(config.exchange.api_key_set && config.exchange.api_secret_set)} />
              <StepCard
                number="4"
                title="Risk"
                done={
                  (config.risk.risk_mode === "fixed_usdt" ? config.risk.fixed_usdt_risk > 0 : config.risk.balance_risk_percent > 0) &&
                  config.risk.max_leverage > 0
                }
              />
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
                <div className="flex items-center gap-2 rounded-lg border border-line bg-field px-3">
                  <Search className="h-4 w-4 text-ink-3" />
                  <input value={chatSearch} onChange={(event) => setChatSearch(event.target.value)} placeholder="Search Telegram channels" className="h-11 w-full bg-transparent text-sm text-ink-1 outline-none placeholder:text-ink-3" />
                </div>
                <button type="button" onClick={() => void loadTelegramChats()} className="simple-secondary-button" disabled={busy !== null || runtime?.telegram.auth_state !== "authenticated"}>
                  Load channels
                </button>
              </div>
              <div className="thin-scrollbar mt-4 max-h-80 overflow-auto rounded-xl border border-line bg-field">
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
                  <div className="px-4 py-8 text-center text-sm text-ink-3">No channels loaded.</div>
                )}
              </div>
            </SimplePanel>

            <section className="grid gap-4 xl:grid-cols-2">
              <SimplePanel icon={<KeyRound className="h-5 w-5" />} title="3. Connect exchange" right={config.exchange.mode}>
                <div className="mb-3 rounded-xl border border-line bg-field px-4 py-3 text-sm leading-6 text-ink-2">
                  <p className="font-medium text-ink-1">Before you paste anything here:</p>
                  <p className="mt-1">Create the API key inside your exchange account, give it trading access only, and keep withdrawals disabled.</p>
                </div>
                <ExchangeSetupGuide exchangeId={config.exchange.exchange_id} />
                <div className="grid gap-3 md:grid-cols-2">
                  <Select label="Exchange" value={config.exchange.exchange_id} onChange={(value) => setConfig({ ...config, exchange: { ...config.exchange, exchange_id: value as ExchangeId } })} options={EXCHANGE_OPTIONS} />
                  <Select label="Mode" value={config.exchange.mode} onChange={(value) => setConfig({ ...config, exchange: { ...config.exchange, mode: value as "testnet" | "mainnet" } })} options={[{ label: "Testnet", value: "testnet" }, { label: "Mainnet", value: "mainnet" }]} />
                  <Field label={`Exchange API key ${config.exchange.api_key_set ? "(already saved)" : ""}`} value={secrets.exchangeApiKey} onChange={(value) => setSecrets({ ...secrets, exchangeApiKey: value })} helper="Find this in your exchange’s API management / developer settings." />
                  <Field label={`Exchange API secret ${config.exchange.api_secret_set ? "(already saved)" : ""}`} type="password" value={secrets.exchangeApiSecret} onChange={(value) => setSecrets({ ...secrets, exchangeApiSecret: value })} helper="This is shown once when you create the API key. Store it securely." />
                  <Field label={`Passphrase ${config.exchange.api_password_set ? "(already saved)" : ""}`} type="password" value={secrets.exchangeApiPassword} onChange={(value) => setSecrets({ ...secrets, exchangeApiPassword: value })} placeholder="Only needed by some exchanges" helper="Leave blank unless your exchange explicitly gives you a passphrase." />
                  <Field label="Default leverage" value={String(config.exchange.default_leverage)} onChange={(value) => setConfig({ ...config, exchange: { ...config.exchange, default_leverage: Number(value || 0) } })} />
                </div>
              </SimplePanel>

              <SimplePanel icon={<SlidersHorizontal className="h-5 w-5" />} title="4. Choose risk rules" right={config.risk.risk_mode.replace("_", " ")}>
                <div className="grid gap-3 md:grid-cols-2">
                  <Select label="Risk mode" value={config.risk.risk_mode} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, risk_mode: value as "fixed_usdt" | "balance_percent" } })} options={[{ label: "Fixed USDT", value: "fixed_usdt" }, { label: "Balance percent", value: "balance_percent" }]} />
                  <Field label="Max leverage" value={String(config.risk.max_leverage)} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, max_leverage: Number(value || 0) } })} />
                  {config.risk.risk_mode === "fixed_usdt" ? (
                    <Field label="Risk per trade in USDT" value={String(config.risk.fixed_usdt_risk)} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, fixed_usdt_risk: Number(value || 0) } })} />
                  ) : (
                    <Field label="Risk per trade in %" value={String(config.risk.balance_risk_percent)} onChange={(value) => setConfig({ ...config, risk: { ...config.risk, balance_risk_percent: Number(value || 0) } })} />
                  )}
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
      <div className="mb-8 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="simple-brand-mark">
            <Cable className="h-5 w-5" />
          </div>
          <div>
            <div className="font-display font-semibold text-ink-1">SignalBridge</div>
            <div className="text-xs text-ink-3">My trading bot</div>
          </div>
        </div>
        <svg className="signal-pulse-line h-4 w-9" viewBox="0 0 60 20" fill="none" aria-hidden="true">
          <path d="M0 10 H16 L21 3 L27 17 L32 10 H60" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
        </svg>
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
  return <span className="rounded-full border border-line bg-wash px-3 py-2 text-sm font-semibold text-ink-1">{value}</span>;
}

function StepCard({ number, title, done }: { number: string; title: string; done: boolean }) {
  return (
    <div className="simple-panel p-4">
      <div className={`flex h-9 w-9 items-center justify-center rounded-full text-sm font-bold ${done ? "bg-accent text-accent-ink" : "bg-panel-2 text-ink-2"}`}>
        {done ? <ShieldCheck className="h-5 w-5" /> : number}
      </div>
      <div className="mt-3 font-display font-semibold text-ink-1">{title}</div>
      <div className="mt-1 text-sm text-ink-3">{done ? "Done" : "Needs setup"}</div>
    </div>
  );
}

function SimplePanel({ icon, title, right, children }: { icon: ReactNode; title: string; right: string; children: ReactNode }) {
  return (
    <section className="simple-panel">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <div className="flex items-center gap-2 font-display font-semibold text-ink-1">
          {icon}
          {title}
        </div>
        <span className="text-sm text-ink-3">{right}</span>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-line bg-field px-3 py-2">
      <div className="text-xs text-ink-3">{label}</div>
      <div className="mono-num mt-1 text-sm text-ink-1">{value}</div>
    </div>
  );
}

function ExchangeSetupGuide({ exchangeId }: { exchangeId: ExchangeId }) {
  const guide = EXCHANGE_SETUP_GUIDES[exchangeId];

  return (
    <div className="mb-4 rounded-xl border border-accent/20 bg-accent/5 p-4">
      <div className="text-sm font-semibold text-accent">{guide.title}</div>
      <div className="mt-1 text-xs uppercase tracking-[0.18em] text-accent/70">Recommended sequence</div>
      <ol className="mt-2 space-y-2 text-sm leading-6 text-ink-2">
        {guide.steps.map((step, index) => (
          <li key={step} className="flex gap-2">
            <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/20 text-xs font-semibold text-accent">{index + 1}</span>
            <span>{step}</span>
          </li>
        ))}
      </ol>
      <div className="mt-3 rounded-lg border border-accent/20 bg-accent/5 px-3 py-2 text-xs leading-5 text-accent">
        {guide.safety}
      </div>
      <div className="mt-3 rounded-lg border border-line bg-field px-3 py-2 text-xs leading-5 text-ink-2">
        {guide.passphrase}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, type = "text", placeholder = "", helper = "" }: { label: string; value: string; onChange: (value: string) => void; type?: string; placeholder?: string; helper?: string }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink-2">{label}</span>
      <input value={value} type={type} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="input-field h-11 text-sm" />
      {helper ? <span className="mt-1 block text-xs leading-5 text-ink-3">{helper}</span> : null}
    </label>
  );
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: Array<{ label: string; value: string }> }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink-2">{label}</span>
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