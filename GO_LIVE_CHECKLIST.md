# SignalBridge Go-Live Checklist

> Current release boundary: this codebase is a secure **single-owner workspace**.
> Do not enable public signup or advertise per-user accounts until the multi-tenant
> migration in `PRODUCTION_LAUNCH_PLAN.md` is complete. OAuth alone does not
> isolate exchange accounts, Telegram sessions, signals, or logs.

## Product Defaults
- Keep the AI parser API key server-side only. Set `SIGNALBRIDGE_PARSER_API_KEY`, `OPENAI_API_KEY`, or `GROQ_API_KEY` in the backend environment.
- Do not ask users for parser/API model credentials in the dashboard.
- Keep the dashboard bearer token server-side only. The Next.js app should call the backend through its `/api/bridge/...` proxy.
- Keep Telegram app credentials server-side only. Users should only enter their phone number, SMS code, optional 2FA password, and selected signal channels.
- Keep exchange keys user-owned and locally encrypted.
- Start every user on exchange testnet/sandbox mode where the selected exchange supports it.

## Security
- Never commit `config.json`, `master.key`, `users.json`, `*.session`, `.env`, or logs.
- Set `SIGNALBRIDGE_AUTH_SECRET` to a strong production-only value before launch.
- Keep `SIGNALBRIDGE_COOKIE_SECURE=true` in production so dashboard sessions are HTTPS-only.
- Keep `ALLOW_PUBLIC_SIGNUP=false` until workspace isolation is deployed.
- Use exchange API keys with withdrawal permission disabled.
- Restrict `CORS_ORIGINS` to the deployed dashboard origin.
- Set `TRUSTED_HOSTS` to the public API hostname and use a reverse proxy that enforces HTTPS.
- Do not trust `X-Forwarded-For` unless the proxy strips inbound copies and sets it itself; then set `SIGNALBRIDGE_TRUST_PROXY_HEADERS=true`.
- Use HTTPS in front of the FastAPI bridge before public launch.
- Rotate `API_BEARER_TOKEN` before sharing a production build.
- Rotate the Telegram API hash shown in earlier local templates because it was exposed in a working copy; never commit a real Telegram API hash again.

## User Experience
- First-run flow should be: connect Telegram, choose channels, connect exchange, set risk, start bot.
- Hide advanced controls until the user has a working basic setup.
- Use plain-language errors like "Telegram is not connected" instead of raw stack traces.
- Show testnet/mainnet mode clearly before the user starts the bot.

## Reliability
- Run the backend as a managed service using systemd, Docker, or a cloud VM process manager.
- Persist logs outside the app root in production.
- Add health checks for `/health` and alert when the bot stops unexpectedly.
- Telegram listener reconnects with exponential backoff; monitor repeated reconnect warnings.
- Test with real Telegram signal examples before enabling mainnet.
- Prove backup and restore of configuration, encrypted tenant data, and database before onboarding anyone.

## Historical Channel Replay
- Use `history_replay.py` for dry-run parser testing against one Telegram channel's message history.
- Example: `python history_replay.py --chat echo_trader2025 --limit 200 --output tmp/echo_replay.jsonl`
- The replay tool uses the existing Telegram session and parser API key. It does not place trades or call the exchange.
- Review the JSONL output for parse rate, ignored messages, failed parses, and incorrect signal extraction before enabling mainnet.
