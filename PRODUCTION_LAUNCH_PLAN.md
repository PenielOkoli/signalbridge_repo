# SignalBridge Production Launch Plan

## Decision

Do not open public signup from the current branch. It has one `config.json`, one
`master.key`, one Telegram session, one log stream, and one `BotSupervisor`.
Adding OAuth at this point would create separate logins that control the same
trading workspace.

Launch the current code only as a private, single-owner testnet beta. Build the
multi-tenant foundation below before inviting independent customers or allowing
mainnet trading.

## Recommended Production Stack

| Concern | Recommended platform | Why |
| --- | --- | --- |
| Dashboard | Vercel | Native, zero-downtime Next.js deployment. |
| Telegram and execution workers | Google Compute Engine VM, Docker Compose, systemd | Long-lived Telethon sessions and local worker state need a stable process and persistent disk. |
| Reverse proxy and TLS | Caddy on the VM | Automatic HTTPS, simple proxy configuration, and one controlled public API entry point. |
| Primary database | Cloud SQL for PostgreSQL, same GCP region as the worker | Managed backups, private networking, and a durable source of truth for tenant data. |
| Application secrets | Google Secret Manager plus Cloud KMS | IAM-controlled secret access, versioning, audit logs, and envelope-key management. |
| Distributed queue and rate limits | Memorystore for Redis | Shared rate limits, job queues, idempotency, and worker coordination. |
| OAuth | Google OpenID Connect through Auth.js or a managed provider such as Clerk | Google sign-in first; backend validates signed identity tokens and maps them to a workspace. |
| Error monitoring | Sentry | Exception alerts with scrubbed request context. |

For a lean private beta, Vercel plus one Compute Engine VM with a persistent
disk is enough. Cloud Run is not the primary worker choice because Telegram
listeners need an always-running process and durable session handling.

## Target Isolation Model

Every query, runtime action, secret, session, log record, and exchange request
must carry a `workspace_id` derived from the authenticated identity. Never
accept this ID from a browser without membership validation.

```text
OAuth identity -> user -> workspace membership -> workspace_id
                                              -> encrypted exchange credentials
                                              -> encrypted Telegram session
                                              -> settings, logs, trades, audit events
                                              -> dedicated worker runtime
```

Create these database tables before enabling public accounts:

- `users`: application user, OAuth subject, email, verified-at, disabled-at.
- `oauth_accounts`: provider name and provider subject; unique together.
- `workspaces`: tenant boundary and billing state.
- `workspace_memberships`: user, workspace, role (`owner`, `operator`, `viewer`).
- `workspace_secrets`: encrypted exchange credentials and encrypted Telegram session, each with a key version.
- `workspace_settings`: phone number, selected channels, risk controls, and parser policy.
- `trade_events`, `orders`, `positions`, `audit_events`, `runtime_events`: all keyed and indexed by `workspace_id`.

Use an envelope-encryption design: Cloud KMS protects a data-encryption key;
the per-workspace data key encrypts that workspace's exchange credentials and
Telegram session. Store ciphertext, nonce, key version, and authenticated
encryption metadata in the database. The browser never receives decrypted
secrets.

## OAuth Design

1. Use Google OpenID Connect first; collect only `openid`, `email`, and `profile`.
2. Require verified email, state/nonce/PKCE validation, and an allowlist during beta.
3. On first login, create a user plus one workspace transactionally.
4. Issue short-lived, HTTP-only, Secure, SameSite=Lax session cookies.
5. The FastAPI API verifies the identity token/JWKS and resolves membership
   server-side on every protected request.
6. Remove the static `users.json` authentication and the dashboard's shared
   bearer-token model after the migration. The bridge token remains an internal
   service credential, never a user credential.
7. Add MFA for owner accounts before mainnet and enforce recent re-authentication
   for exchange-key changes, Telegram logout, and mainnet activation.

## Required Code Refactor

1. Replace `ConfigManager` file storage with a `WorkspaceRepository` backed by
   PostgreSQL. Keep a migration/import path for the existing local config.
2. Replace global `BotSupervisor` with `RuntimeManager[workspace_id]`; one
   workspace gets one serial execution queue and one encrypted Telegram session.
3. Make all FastAPI routes resolve the workspace from the authenticated session;
   remove global `/config`, `/logs`, `/status`, and `/exchange/state` behavior.
4. Add database-level tenant scoping as defense in depth. If using Supabase,
   enable PostgreSQL Row Level Security. If using Cloud SQL directly, use a
   repository layer that rejects unscoped queries and integration tests that
   prove cross-workspace reads and writes fail.
5. Introduce Redis-backed distributed rate limiting, idempotency keys, replay
   protection, and a durable job queue for signal processing.
6. Add immutable audit events for sign-in, credential change, bot start/stop,
   signal parsed, order submitted, order rejected, and risk-limit refusal.
7. Add test suites for tenant isolation, OAuth callback state handling, exchange
   credential encryption, and a full testnet trade lifecycle.

## Launch Sequence

1. Deploy the current app privately on testnet only; keep public signup off.
2. Build and test the tenant data model and workspace-scoped runtime.
3. Add Google OAuth and invite-only onboarding. Do not ship public signup yet.
4. Run a private beta with isolated testnet accounts; replay real historical
   signals and review every parser/execution mismatch.
5. Add alerting, backups, restore drills, audit trails, per-user rate limits,
   and account deletion/export processes.
6. Complete legal review: terms, privacy notice, risk disclosure, supported
   jurisdictions, exchange terms, and data-retention policy.
7. Permit mainnet per workspace only after an explicit confirmation, withdrawal-
   disabled exchange keys, a kill switch, position limits, and a tested incident
   response procedure.

## Non-Negotiable Mainnet Controls

- Default all new workspaces to testnet.
- Require explicit, fresh confirmation for mainnet; never copy testnet keys or
  settings into mainnet automatically.
- Enforce a per-workspace daily loss limit, maximum notional, maximum leverage,
  symbol allowlist, and global emergency stop.
- Use exchange API keys without withdrawal permissions and restrict keys by IP
  when the exchange supports it.
- Do not log signal text, API keys, auth tokens, phone numbers, or two-factor
  codes without a redaction policy and a retention limit.
- Back up PostgreSQL and encryption-key metadata; test restoration every month.

## Platform References

- [Vercel's Next.js platform](https://vercel.com/frameworks/nextjs)
- [Google Compute Engine Persistent Disk](https://cloud.google.com/compute/docs/disks/persistent-disks)
- [Google Secret Manager overview](https://cloud.google.com/secret-manager/docs/overview)
- [Supabase PostgreSQL Row Level Security](https://supabase.com/docs/guides/database/postgres/row-level-security)
- [Next.js authentication guidance](https://nextjs.org/docs/app/guides/authentication)
- [Upstash rate limiting overview](https://upstash.com/docs/redis/sdks/ratelimit-ts/overview)
