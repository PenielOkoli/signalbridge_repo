export type LogEntry = {
  id: string;
  timestamp: string;
  level: "debug" | "info" | "warning" | "error" | "trade";
  message: string;
  context: Record<string, unknown>;
};

export type StatusResponse = {
  bridge: {
    status: "online";
    version: string;
    timestamp: string;
    log_entries: number;
    activity_log_path: string | null;
  };
  bot: {
    state: "stopped" | "starting" | "running" | "stopping" | "error";
    running: boolean;
    started_at: string | null;
    last_error: string | null;
    can_start: boolean;
    trades_last_24h: number;
    monitored_chat_count: number;
  };
  telegram: {
    auth_state: "unknown" | "unauthenticated" | "code_sent" | "password_required" | "authenticated" | "error";
    configured: boolean;
    app_configured: boolean;
    phone_number_set: boolean;
    session_file_present: boolean;
    monitored_chats: string[];
    code_sent_at: string | null;
  };
  config: {
    ready_for_auth: boolean;
    ready_for_trading: boolean;
    telegram_configured: boolean;
    exchange_id: ExchangeId;
    exchange_mode: "testnet" | "mainnet";
    exchange_credentials_configured: boolean;
    openai_configured: boolean;
    risk_mode: "fixed_usdt" | "balance_percent";
    daily_trade_limit: number | null;
    max_take_profit_orders: number;
  };
};

export type ExchangePosition = {
  symbol: string;
  side: "buy" | "sell";
  contracts: number;
  entry_price: number | null;
  mark_price: number | null;
  leverage: number | null;
  liquidation_price: number | null;
  unrealized_pnl: number | null;
  notional_usdt: number | null;
};

export type ExchangeOrder = {
  order_id: string;
  symbol: string;
  side: string;
  order_type: string;
  status: string;
  amount: number | null;
  remaining: number | null;
  price: number | null;
  trigger_price: number | null;
  reduce_only: boolean;
  timestamp: string | null;
};

export type ExchangeStateResponse = {
  exchange_id: ExchangeId;
  mode: "testnet" | "mainnet";
  source: "live_runtime" | "on_demand";
  fetched_at: string;
  total_open_positions: number;
  total_open_orders: number;
  free_usdt: number | null;
  total_usdt: number | null;
  open_positions: ExchangePosition[];
  open_orders: ExchangeOrder[];
};

export type TelegramChatOption = {
  peer_id: string;
  source_value: string;
  title: string;
  username: string | null;
  kind: "channel" | "group";
  member_count_hint: number | null;
};

export type TelegramChatsResponse = {
  chats: TelegramChatOption[];
  selected: string[];
};

export type ConfigResponse = {
  schema_version: number;
  security: {
    api_bearer_token_set: boolean;
  };
  telegram: {
    app_configured: boolean;
    phone_number: string;
    monitored_chats: string[];
  };
  exchange: {
    exchange_id: ExchangeId;
    mode: "testnet" | "mainnet";
    default_leverage: number;
    api_key_set: boolean;
    api_secret_set: boolean;
    api_password_set: boolean;
  };
  openai: {
    provider: "openai" | "groq";
    model: string;
    request_timeout_seconds: number;
    api_key_set: boolean;
  };
  risk: {
    risk_mode: "fixed_usdt" | "balance_percent";
    fixed_usdt_risk: number;
    balance_risk_percent: number;
    max_leverage: number;
    daily_trade_limit: number | null;
    max_take_profit_orders: number;
    enabled_symbols: string[];
  };
};

export type ExchangeId =
  | "bybit"
  | "bingx"
  | "binanceusdm"
  | "okx"
  | "bitget"
  | "kucoinfutures"
  | "mexc"
  | "gateio"
  | "phemex"
  | "coinex";

export type BridgeConnection = {
  apiUrl: string;
  token: string;
};

export function readBridgeConnection(defaultApiUrl = "", defaultToken = ""): BridgeConnection {
  return {
    apiUrl: defaultApiUrl,
    token: defaultToken
  };
}

export function persistBridgeConnection(connection: BridgeConnection) {
  void connection;
}

export function normalizeApiUrl(apiUrl: string) {
  return apiUrl.trim().replace(/\/$/, "");
}

export async function fetchBridgeJson<T>(
  connection: BridgeConnection,
  path: string,
  init?: RequestInit
): Promise<T> {
  const apiUrl = normalizeApiUrl(connection.apiUrl);
  const useDirectBridge = Boolean(apiUrl && connection.token);
  const url = useDirectBridge ? `${apiUrl}${path}` : `/api/bridge${path}`;

  const response = await fetch(url, {
    cache: "no-store",
    credentials: "include",
    ...init,
    headers: {
      ...(useDirectBridge ? { Authorization: `Bearer ${connection.token}` } : {}),
      ...(init?.headers ?? {})
    }
  });

  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
      window.location.assign("/login");
    }
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) {
        detail = body.detail;
      }
    } catch {
      // Ignore JSON parse failures and fall back to HTTP status text.
    }
    throw new Error(detail);
  }

  return (await response.json()) as T;
}

export function formatUtcTimestamp(value: string | null) {
  if (!value) {
    return "-";
  }
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    month: "short",
    day: "2-digit",
    hour12: false,
    timeZone: "UTC"
  }).format(new Date(value));
}

export function splitLines(value: string) {
  return value
    .split(/\r?\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}
