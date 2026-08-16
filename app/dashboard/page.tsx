"use client";

import {
  Activity,
  Bot,
  Cable,
  KeyRound,
  Play,
  RadioTower,
  RefreshCw,
  Settings,
  ShieldCheck,
  Square,
  Terminal,
  TriangleAlert
} from "../components/icons";
import Link from "next/link";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";

import { ThemeToggle } from "../components/theme-toggle";
import {
  type BridgeConnection,
  type ExchangeStateResponse,
  type LogEntry,
  type StatusResponse,
  fetchBridgeJson,
  formatUtcTimestamp,
  readBridgeConnection
} from "../lib/signalbridge";

const fallbackLogs: LogEntry[] = [
  {
    id: "awaiting-bridge",
    timestamp: "",
    level: "warning",
    message: "Waiting for the local SignalBridge service",
    context: {}
  }
];

export default function DashboardPage() {
  const defaultApiUrl = process.env.NEXT_PUBLIC_SIGNALBRIDGE_API_URL ?? "";
  // SECURITY: Never expose the bearer token to the frontend.
  // The proxy route (/api/bridge) handles authentication server-side.
  const [connection, setConnection] = useState<BridgeConnection>({ apiUrl: defaultApiUrl, token: "" });
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [exchangeState, setExchangeState] = useState<ExchangeStateResponse | null>(null);
  const [exchangeError, setExchangeError] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>(fallbackLogs);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<"start" | "stop" | "refresh" | null>(null);

  useEffect(() => {
    setConnection(readBridgeConnection(defaultApiUrl, ""));
  }, [defaultApiUrl]);

  useEffect(() => {
    let cancelled = false;

    async function refresh() {
      try {
        const [statusResponse, logResponse] = await Promise.all([
          fetchBridgeJson<StatusResponse>(connection, "/status"),
          fetchBridgeJson<{ logs: LogEntry[] }>(connection, "/logs?limit=30")
        ]);
        if (cancelled) return;
        setStatus(statusResponse);
        setLogs(logResponse.logs.length ? logResponse.logs : fallbackLogs);
        setErrorMessage(statusResponse.bot.last_error);

        try {
          const exchangeResponse = await fetchBridgeJson<ExchangeStateResponse>(connection, "/exchange/state");
          if (!cancelled) {
            setExchangeState(exchangeResponse);
            setExchangeError(null);
          }
        } catch (error) {
          if (!cancelled) {
            setExchangeState(null);
            setExchangeError(error instanceof Error ? error.message : "Exchange data is unavailable.");
          }
        }
      } catch (error) {
        if (cancelled) return;
        setStatus(null);
        setExchangeState(null);
        setExchangeError(null);
        setLogs(fallbackLogs);
        setErrorMessage(error instanceof Error ? error.message : "Unable to reach SignalBridge.");
      }
    }

    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [connection]);

  async function refreshNow() {
    setBusy("refresh");
    try {
      const [statusResponse, logResponse] = await Promise.all([
        fetchBridgeJson<StatusResponse>(connection, "/status"),
        fetchBridgeJson<{ logs: LogEntry[] }>(connection, "/logs?limit=30")
      ]);
      setStatus(statusResponse);
      setLogs(logResponse.logs.length ? logResponse.logs : fallbackLogs);
      setErrorMessage(statusResponse.bot.last_error);

      try {
        const exchangeResponse = await fetchBridgeJson<ExchangeStateResponse>(connection, "/exchange/state");
        setExchangeState(exchangeResponse);
        setExchangeError(null);
      } catch (error) {
        setExchangeState(null);
        setExchangeError(error instanceof Error ? error.message : "Exchange data is unavailable.");
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Refresh failed.");
    } finally {
      setBusy(null);
    }
  }

  async function invoke(path: "/bot/start" | "/bot/stop") {
    setBusy(path === "/bot/start" ? "start" : "stop");
    try {
      const response = await fetchBridgeJson<StatusResponse>(connection, path, { method: "POST" });
      setStatus(response);
      setErrorMessage(response.bot.last_error);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "Bot command failed.");
    } finally {
      setBusy(null);
    }
  }

  const recentLogs = useMemo(
    () =>
      [...logs]
        .sort((left, right) => safeTime(right.timestamp) - safeTime(left.timestamp))
        .slice(0, 6),
    [logs]
  );

  const setupItems = [
    {
      label: "Telegram account",
      ready: Boolean(status?.config.telegram_configured && status?.telegram.session_file_present),
      detail: status?.telegram.auth_state === "authenticated" ? "Connected" : "Not connected"
    },
    {
      label: "Signal channels",
      ready: Boolean((status?.bot.monitored_chat_count ?? 0) > 0),
      detail: `${status?.bot.monitored_chat_count ?? 0} selected`
    },
    {
      label: "Exchange keys",
      ready: Boolean(status?.config.exchange_credentials_configured),
      detail: formatExchangeId(status?.config.exchange_id)
    },
    {
      label: "Signal reader",
      ready: Boolean(status?.config.parser_operational),
      detail: status?.config.parser_operational ? "Managed in backend" : "Backend parser unavailable"
    }
  ];
  const completedSteps = setupItems.filter((item) => item.ready).length;
  const readyForTrading = Boolean(status?.config.ready_for_trading);
  const botRunning = Boolean(status?.bot.running);
  const pageState = !status ? "Connecting..." : botRunning ? "Bot running" : readyForTrading ? "Ready to start" : "Finish setup";
  const openPositions = exchangeState?.open_positions ?? [];
  const livePnl = sumNullable(openPositions.map((position) => position.unrealized_pnl));
  const realizedPnl = sumNullable(openPositions.map((position) => position.realized_pnl));

  return (
    <main className="app-shell">
      <div className="simple-app-frame">
        <AppSidebar active="Home" />

        <section className="min-w-0">
          <header className="simple-page-header">
            <div>
              <p className="simple-eyebrow">SignalBridge</p>
              <h1 className="simple-title">Home</h1>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <ThemeToggle variant="ghost" />
              <button type="button" onClick={refreshNow} className="simple-icon-button" aria-label="Refresh" disabled={busy === "refresh"}>
                <RefreshCw className={`h-4 w-4 ${busy === "refresh" ? "animate-spin" : ""}`} />
              </button>
              <Link href="/settings" className="simple-secondary-button">
                <Settings className="h-4 w-4" />
                Settings
              </Link>
            </div>
          </header>

          <div className="simple-page">
            <section className="simple-hero-card">
              <div className="flex flex-col gap-5 lg:flex-row lg:items-center lg:justify-between">
                <div className="max-w-2xl">
                  <div className={`simple-status-dot ${botRunning ? "bg-emerald-signal" : readyForTrading ? "bg-gold-signal" : "bg-red-400"}`} />
                  <h2 className="mt-4 text-3xl font-bold text-zinc-50">{pageState}</h2>
                  <p className="mt-2 max-w-xl text-base leading-7 text-zinc-400">
                    {botRunning
                      ? "SignalBridge is watching your selected Telegram channels and can place trades."
                      : readyForTrading
                        ? "Everything important is connected. You can start the bot when you are ready."
                        : "Complete the setup checklist before starting the trading bot."}
                  </p>
                  {errorMessage ? <p className="mt-3 rounded-lg border border-red-400/30 bg-red-400/10 px-3 py-2 text-sm text-red-200">{errorMessage}</p> : null}
                  <p className="mt-3 rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm text-zinc-400">
                    Single workspace mode: this dashboard shows the Telegram session and exchange account configured on this SignalBridge server.
                  </p>
                </div>

                <div className="flex flex-col gap-2 sm:flex-row lg:flex-col">
                  {botRunning ? (
                    <button type="button" onClick={() => invoke("/bot/stop")} className="simple-danger-button" disabled={busy !== null}>
                      <Square className="h-4 w-4" />
                      Stop bot
                    </button>
                  ) : readyForTrading ? (
                    <button type="button" onClick={() => invoke("/bot/start")} className="simple-primary-button" disabled={busy !== null || !status?.bot.can_start}>
                      <Play className="h-4 w-4" />
                      Start bot
                    </button>
                  ) : (
                    <Link href="/settings" className="simple-primary-button">
                      <Settings className="h-4 w-4" />
                      Finish setup
                    </Link>
                  )}
                </div>
              </div>
            </section>

            <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
              <div className="simple-panel">
                <PanelTitle icon={<ShieldCheck className="h-5 w-5" />} title="Setup checklist" right={`${completedSteps}/4 done`} />
                <div className="grid gap-3 p-4 sm:grid-cols-2">
                  {setupItems.map((item) => (
                    <ChecklistItem key={item.label} {...item} />
                  ))}
                </div>
              </div>

              <div className="simple-panel">
                <PanelTitle icon={<Activity className="h-5 w-5" />} title="Today" right="Live status" />
                <div className="grid gap-px bg-white/10">
                  <SimpleRow label="Trades" value={String(status?.bot.trades_last_24h ?? 0)} />
                  <SimpleRow label="Bot" value={status?.bot.state ?? "offline"} />
                  <SimpleRow label="Exchange" value={`${formatExchangeId(status?.config.exchange_id)} ${status?.config.exchange_mode ?? ""}`} />
                  <SimpleRow label="Live PnL" value={formatMoney(livePnl)} tone={pnlTone(livePnl)} />
                  <SimpleRow label="Realized PnL" value={formatMoney(realizedPnl)} tone={pnlTone(realizedPnl)} />
                  <SimpleRow label="Balance" value={displayNumber(exchangeState?.free_usdt)} />
                </div>
              </div>
            </section>

            <section className="grid gap-4 lg:grid-cols-2">
              <div className="simple-panel">
                <PanelTitle icon={<Terminal className="h-5 w-5" />} title="Recent activity" right={`${recentLogs.length} items`} />
                <div className="divide-y divide-white/10">
                  {recentLogs.map((entry) => (
                    <LogLine key={entry.id} entry={entry} />
                  ))}
                </div>
              </div>

              <div className="simple-panel">
                <PanelTitle icon={<KeyRound className="h-5 w-5" />} title="Exchange account" right={exchangeState ? "Connected" : "Not loaded"} />
                {exchangeError ? <p className="border-b border-red-400/20 bg-red-400/10 px-4 py-3 text-sm text-red-200">Exchange data unavailable: {exchangeError}</p> : null}
                <div className="grid gap-px bg-white/10">
                  <SimpleRow label="Open positions" value={String(exchangeState?.total_open_positions ?? 0)} />
                  <SimpleRow label="Open orders" value={String(exchangeState?.total_open_orders ?? 0)} />
                  <SimpleRow label="Live PnL" value={formatMoney(livePnl)} tone={pnlTone(livePnl)} />
                  <SimpleRow label="Realized PnL" value={formatMoney(realizedPnl)} tone={pnlTone(realizedPnl)} />
                  <SimpleRow label="Free USDT" value={displayNumber(exchangeState?.free_usdt)} />
                  <SimpleRow label="Total USDT" value={displayNumber(exchangeState?.total_usdt)} />
                </div>
              </div>
            </section>

            <section className="simple-panel">
              <PanelTitle icon={<KeyRound className="h-5 w-5" />} title="Open positions" right={`${openPositions.length} live`} />
              {openPositions.length ? (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[720px] border-collapse text-left">
                    <thead>
                      <tr className="border-b border-white/10 bg-[#11131a] text-xs uppercase text-zinc-500">
                        <th className="px-4 py-3 font-semibold">Symbol</th>
                        <th className="px-4 py-3 font-semibold">Side</th>
                        <th className="px-4 py-3 font-semibold">Size</th>
                        <th className="px-4 py-3 font-semibold">Entry</th>
                        <th className="px-4 py-3 font-semibold">Mark</th>
                        <th className="px-4 py-3 text-right font-semibold">Live PnL</th>
                        <th className="px-4 py-3 text-right font-semibold">Realized PnL</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/10">
                      {openPositions.map((position) => (
                        <tr key={`${position.symbol}-${position.side}`} className="bg-[#11131a]">
                          <td className="mono-num px-4 py-3 text-sm font-semibold text-zinc-100">{position.symbol}</td>
                          <td className={`px-4 py-3 text-sm font-bold uppercase ${position.side === "buy" ? "text-emerald-signal" : "text-red-300"}`}>{position.side}</td>
                          <td className="mono-num px-4 py-3 text-sm text-zinc-200">{displayNumber(position.contracts)}</td>
                          <td className="mono-num px-4 py-3 text-sm text-zinc-200">{displayNumber(position.entry_price)}</td>
                          <td className="mono-num px-4 py-3 text-sm text-zinc-200">{displayNumber(position.mark_price)}</td>
                          <td className={`mono-num px-4 py-3 text-right text-sm font-bold ${pnlTextClass(position.unrealized_pnl)}`}>
                            {formatMoney(position.unrealized_pnl)}
                          </td>
                          <td className={`mono-num px-4 py-3 text-right text-sm font-bold ${pnlTextClass(position.realized_pnl)}`}>
                            {formatMoney(position.realized_pnl)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="px-4 py-8 text-center text-sm text-zinc-500">No open positions yet.</div>
              )}
            </section>
          </div>
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

function PanelTitle({ icon, title, right }: { icon: ReactNode; title: string; right: string }) {
  return (
    <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
      <div className="flex items-center gap-2 font-semibold text-zinc-100">
        {icon}
        {title}
      </div>
      <span className="text-sm text-zinc-500">{right}</span>
    </div>
  );
}

function ChecklistItem({ label, ready, detail }: { label: string; ready: boolean; detail: string }) {
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 p-4">
      <div className="flex items-start gap-3">
        <div className={`mt-1 flex h-6 w-6 items-center justify-center rounded-full ${ready ? "bg-emerald-signal text-emerald-dim" : "bg-zinc-800 text-zinc-400"}`}>
          {ready ? <ShieldCheck className="h-4 w-4" /> : <TriangleAlert className="h-4 w-4" />}
        </div>
        <div>
          <div className="font-semibold text-zinc-100">{label}</div>
          <div className="mt-1 text-sm text-zinc-500">{detail}</div>
        </div>
      </div>
    </div>
  );
}

function SimpleRow({ label, value, tone = "neutral" }: { label: string; value: string; tone?: "neutral" | "profit" | "loss" }) {
  const valueClass = {
    neutral: "text-zinc-100",
    profit: "text-emerald-signal",
    loss: "text-red-300"
  }[tone];

  return (
    <div className="grid grid-cols-[150px_minmax(0,1fr)] gap-px bg-white/10">
      <div className="bg-[#11131a] px-4 py-3 text-sm text-zinc-500">{label}</div>
      <div className={`mono-num truncate bg-[#11131a] px-4 py-3 text-sm font-semibold uppercase ${valueClass}`}>{value}</div>
    </div>
  );
}

function LogLine({ entry }: { entry: LogEntry }) {
  const chat = typeof entry.context?.chat === "string" ? entry.context.chat : null;
  return (
    <div className="grid gap-2 px-4 py-3 sm:grid-cols-[130px_90px_100px_minmax(0,1fr)]">
      <span className="mono-num text-xs text-zinc-500">{formatUtcTimestamp(entry.timestamp)}</span>
      <span className={`text-xs font-bold uppercase ${levelClass(entry.level)}`}>{entry.level}</span>
      <span className="text-xs text-zinc-400 truncate">{chat || "-"}</span>
      <span className="text-sm text-zinc-200">{entry.message}</span>
    </div>
  );
}

function safeTime(value: string) {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

function displayNumber(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function formatMoney(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} USDT`;
}

function sumNullable(values: Array<number | null | undefined>) {
  const numericValues = values.filter((value): value is number => typeof value === "number" && !Number.isNaN(value));
  if (!numericValues.length) {
    return null;
  }
  return numericValues.reduce((total, value) => total + value, 0);
}

function pnlTone(value: number | null | undefined): "neutral" | "profit" | "loss" {
  if (value === null || value === undefined || Number.isNaN(value) || value === 0) {
    return "neutral";
  }
  return value > 0 ? "profit" : "loss";
}

function pnlTextClass(value: number | null | undefined) {
  return {
    neutral: "text-zinc-100",
    profit: "text-emerald-signal",
    loss: "text-red-300"
  }[pnlTone(value)];
}

function formatExchangeId(value: string | undefined) {
  return (
    {
      bybit: "Bybit",
      bingx: "BingX",
      binanceusdm: "Binance",
      okx: "OKX",
      bitget: "Bitget",
      kucoinfutures: "KuCoin",
      mexc: "MEXC",
      gateio: "Gate.io",
      phemex: "Phemex",
      coinex: "CoinEx"
    }[value ?? ""] ?? "-"
  );
}

function levelClass(level: LogEntry["level"]) {
  return {
    debug: "text-zinc-500",
    info: "text-zinc-300",
    warning: "text-gold-signal",
    error: "text-red-400",
    trade: "text-emerald-signal"
  }[level];
}
