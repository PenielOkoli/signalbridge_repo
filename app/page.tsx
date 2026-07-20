import {
  ArrowRight,
  Bot,
  Building2,
  CheckCircle2,
  Edit3,
  LockKeyhole,
  MoveUp,
  PlayCircle,
  Reply,
  Send,
  ShieldCheck,
  SlidersHorizontal,
  Touchpad,
  Zap
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { ThemeToggle } from "./components/theme-toggle";

const exchanges = [
  ["Bybit", "Ready"],
  ["Binance USD-M", "Ready"],
  ["OKX", "Passphrase"],
  ["Bitget", "Passphrase"],
  ["BingX", "Ready"],
  ["KuCoin Futures", "Passphrase"],
  ["MEXC", "Ready"],
  ["Gate.io", "Ready"]
];

const features = [
  {
    icon: <Edit3 className="h-5 w-5" />,
    title: "Edited messages",
    signal: "Buy ETH @ 3000",
    update: "Buy ETH @ 3050",
    result: "Pending order amended automatically"
  },
  {
    icon: <Reply className="h-5 w-5" />,
    title: "Contextual replies",
    signal: "Original: Long SOL...",
    update: "Close this trade now",
    result: "Position closed at market"
  },
  {
    icon: <MoveUp className="h-5 w-5" />,
    title: "Protective updates",
    signal: "BTC long active",
    update: "Move SL to entry",
    result: "Stop loss moved to break even"
  }
];

export default function LandingPage() {
  return (
    <main className="marketing-shell">
      <header className="marketing-nav">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-4 sm:px-6 lg:px-8">
          <Link href="/" className="flex items-center gap-3" aria-label="SignalBridge home">
            <span className="brand-mark">SB</span>
            <span className="brand-copy text-lg font-bold tracking-tight text-emerald-signal">SignalBridge</span>
          </Link>

          <nav className="hidden items-center gap-7 md:flex">
            {["Features", "Exchanges", "Pricing", "Docs"].map((item) => (
              <a key={item} href={`#${item.toLowerCase()}`} className="text-xs font-semibold uppercase tracking-[0.08em] text-zinc-500 transition-colors hover:text-emerald-signal">
                {item}
              </a>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            <ThemeToggle variant="ghost" />
            <Link href="/login" className="hidden rounded-lg border border-white/10 px-4 py-2 text-xs font-semibold uppercase tracking-[0.08em] text-zinc-300 transition-colors hover:border-emerald-signal md:inline-flex">
              Log in
            </Link>
            <Link href="/signup" className="rounded-lg bg-emerald-signal px-4 py-2 text-xs font-semibold uppercase tracking-[0.08em] text-emerald-dim transition-opacity hover:opacity-90">
              Start free
            </Link>
          </div>
        </div>
      </header>

      <section className="mx-auto grid min-h-[680px] w-full max-w-7xl grid-cols-1 items-center gap-10 px-4 py-16 sm:px-6 lg:grid-cols-2 lg:px-8">
        <div className="z-10 flex flex-col gap-6">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-xs font-semibold text-zinc-300">
              <Touchpad className="h-3.5 w-3.5" />
              No-code setup
            </span>
            <span className="inline-flex items-center gap-2 rounded-full border border-emerald-signal/20 bg-emerald-signal/10 px-3 py-1.5 text-xs font-semibold text-emerald-signal">
              <Zap className="h-3.5 w-3.5" />
              Institutional-grade execution
            </span>
          </div>

          <div>
            <h1 className="max-w-3xl text-5xl font-semibold leading-[1.05] tracking-[-0.02em] text-zinc-100 sm:text-6xl">
              Trade Telegram signals <span className="text-emerald-signal">while you sleep.</span>
            </h1>
            <p className="mt-5 max-w-xl text-base leading-8 text-zinc-400">
              Connect your favorite Telegram channels to your exchange in under 5 minutes. SignalBridge executes
              trades, handles edits, and applies your custom risk rules automatically.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <Link href="/signup" className="inline-flex min-h-12 items-center justify-center rounded-xl bg-emerald-signal px-6 text-lg font-semibold text-emerald-dim shadow-[0_0_20px_rgba(78,222,163,0.22)] transition-colors hover:bg-[#6ffbbe]">
              Start free <ArrowRight className="ml-2 h-5 w-5" />
            </Link>
            <Link href="#features" className="inline-flex min-h-12 items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/[0.03] px-6 text-lg font-semibold text-zinc-200 transition-colors hover:bg-white/[0.07]">
              <PlayCircle className="h-5 w-5" />
              See how it works
            </Link>
          </div>
        </div>

        <div className="relative min-h-[420px] overflow-hidden rounded-xl border border-white/10 bg-panel p-6 shadow-2xl">
          <div className="absolute inset-0 opacity-[0.04] flow-grid" />
          <div className="relative z-10 flex h-full flex-col items-center justify-center gap-8">
            <FlowNode
              icon={<Send className="h-6 w-6" />}
              tone="gold"
              label="1. Signal received"
              title={'"Buy BTC/USDT @ 64.2k"'}
            />
            <div className="h-12 w-px bg-gradient-to-b from-white/20 to-emerald-signal/50" />
            <FlowNode
              icon={<Bot className="h-6 w-6" />}
              tone="green"
              label="2. SignalBridge decides"
              title="Risk rules applied automatically"
              active
            />
            <div className="h-12 w-px bg-gradient-to-b from-emerald-signal/50 to-white/20" />
            <FlowNode
              icon={<Building2 className="h-6 w-6" />}
              tone="zinc"
              label="3. Trade placed"
              title="Order filled on exchange"
            />
          </div>
        </div>
      </section>

      <section className="mx-auto flex w-full max-w-7xl flex-col items-center justify-center gap-5 border-y border-white/10 px-4 py-8 opacity-80 sm:px-6 md:flex-row lg:px-8">
        <span className="text-center text-xs font-semibold uppercase tracking-[0.18em] text-zinc-500">Built around safety</span>
        <div className="flex flex-wrap items-center justify-center gap-6">
          <TrustBadge icon={<ShieldCheck className="h-5 w-5" />} label="Trading-only keys" />
          <TrustBadge icon={<LockKeyhole className="h-5 w-5" />} label="No withdrawals" />
          <TrustBadge icon={<CheckCircle2 className="h-5 w-5" />} label="Every action logged" />
        </div>
      </section>

      <section id="features" className="mx-auto w-full max-w-7xl px-4 py-20 sm:px-6 lg:px-8">
        <div className="mb-8 max-w-2xl">
          <h2 className="text-3xl font-semibold tracking-[-0.01em] text-zinc-100">Automation that actually understands</h2>
          <p className="mt-3 text-base leading-8 text-zinc-400">
            Standard bots fail when admins edit messages or reply to old setups. SignalBridge follows the conversation
            like a trader would.
          </p>
        </div>

        <div className="grid gap-6 md:grid-cols-3">
          {features.map((feature) => (
            <article key={feature.title} className="rounded-xl border border-white/10 bg-panel p-6 transition-colors hover:border-white/20">
              <div className="mb-5 inline-flex h-10 w-10 items-center justify-center rounded-lg border border-white/10 bg-white/[0.04] text-zinc-200">
                {feature.icon}
              </div>
              <h3 className="text-xl font-medium text-zinc-100">{feature.title}</h3>
              <div className="mt-5 rounded-lg border border-white/10 bg-[#09100c] p-4 font-mono text-[13px] leading-5">
                <p className="text-zinc-500 line-through">{feature.signal}</p>
                <p className="mt-2 text-gold-signal">{feature.update}</p>
                <div className="mt-3 flex items-center gap-2 border-t border-white/10 pt-3 text-emerald-signal">
                  <CheckCircle2 className="h-4 w-4" />
                  {feature.result}
                </div>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section id="exchanges" className="mx-auto w-full max-w-7xl border-t border-white/10 px-4 py-20 sm:px-6 lg:px-8">
        <div className="mb-8 flex flex-col justify-between gap-4 md:flex-row md:items-end">
          <div>
            <h2 className="text-3xl font-semibold tracking-[-0.01em] text-zinc-100">Supported exchanges</h2>
            <p className="mt-3 text-base text-zinc-400">Execute through low-latency API connections.</p>
          </div>
          <Link href="/signup" className="inline-flex w-max items-center gap-2 rounded-lg border border-emerald-signal/30 px-4 py-2 text-sm font-semibold text-emerald-signal">
            Connect account <ArrowRight className="h-4 w-4" />
          </Link>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {exchanges.map(([name, status]) => (
            <div key={name} className="flex items-center justify-between rounded-xl border border-white/10 bg-panel p-4 transition-colors hover:bg-white/[0.04]">
              <div className="flex items-center gap-3">
                <div className="flex h-8 w-8 items-center justify-center rounded bg-gold-signal/15 text-xs font-bold text-gold-signal">
                  {name.slice(0, 1)}
                </div>
                <span className="text-sm font-semibold text-zinc-200">{name}</span>
              </div>
              <span className="rounded bg-emerald-signal/10 px-2 py-1 text-[10px] font-bold uppercase text-emerald-signal">{status}</span>
            </div>
          ))}
        </div>
      </section>

      <section id="pricing" className="mx-auto w-full max-w-7xl px-4 pb-20 sm:px-6 lg:px-8">
        <div className="rounded-xl border border-emerald-signal/30 bg-emerald-signal/10 p-8 md:flex md:items-center md:justify-between">
          <div>
            <h2 className="text-3xl font-semibold tracking-[-0.01em] text-zinc-100">Start with testnet. Go live when you trust it.</h2>
            <p className="mt-3 max-w-2xl text-zinc-400">
              Set up Telegram, connect an exchange, and watch the execution log before risking real capital.
            </p>
          </div>
          <Link href="/signup" className="mt-6 inline-flex min-h-12 items-center rounded-xl bg-emerald-signal px-6 font-semibold text-emerald-dim md:mt-0">
            Start free <ArrowRight className="ml-2 h-5 w-5" />
          </Link>
        </div>
      </section>
    </main>
  );
}

function FlowNode({
  icon,
  tone,
  label,
  title,
  active = false
}: {
  icon: ReactNode;
  tone: "gold" | "green" | "zinc";
  label: string;
  title: string;
  active?: boolean;
}) {
  const toneClass = {
    gold: "bg-gold-signal/15 text-gold-signal",
    green: "bg-emerald-signal/15 text-emerald-signal",
    zinc: "bg-white/[0.05] text-zinc-200"
  }[tone];

  return (
    <div className={`w-full max-w-md rounded-lg border bg-panel-2 p-4 shadow-lg transition-transform hover:scale-[1.02] ${active ? "active-node border-emerald-signal/50" : "border-white/10"}`}>
      <div className="flex items-center gap-4">
        <div className={`flex h-12 w-12 shrink-0 items-center justify-center rounded-full ${toneClass}`}>{icon}</div>
        <div>
          <p className={`mb-1 text-xs font-semibold uppercase tracking-[0.08em] ${active ? "text-emerald-signal" : "text-zinc-500"}`}>{label}</p>
          <p className="text-base font-medium text-zinc-100">{title}</p>
        </div>
      </div>
    </div>
  );
}

function TrustBadge({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="flex items-center gap-2 text-zinc-300">
      <span className="text-emerald-signal">{icon}</span>
      <span className="text-sm font-semibold">{label}</span>
    </div>
  );
}
