"""
Production security guardrails for SignalBridge.

These checks are intentionally strict only when ENVIRONMENT/SIGNALBRIDGE_ENV is
set to production. Local development remains usable, but a public deployment
will fail fast if critical secrets or network boundaries are unsafe.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque

from fastapi import HTTPException, Request, status


PRODUCTION_VALUES = {"prod", "production", "live"}
PLACEHOLDER_MARKERS = ("change-me", "replace-with", "your-", "example", "changeme")
LOCAL_ORIGINS = ["http://127.0.0.1:3000", "http://localhost:3000"]


class ProductionSecurityError(RuntimeError):
    """Raised when a production deployment is missing mandatory safeguards."""


def is_production() -> bool:
    value = os.getenv("SIGNALBRIDGE_ENV", os.getenv("ENVIRONMENT", "")).strip().lower()
    return value in PRODUCTION_VALUES


def validate_production_environment() -> None:
    """Fail fast when production environment variables are unsafe."""

    if not is_production():
        return

    problems: list[str] = []
    _require_secret("API_BEARER_TOKEN", problems, min_length=32)
    _require_secret("SIGNALBRIDGE_AUTH_SECRET", problems, min_length=48)
    _require_secret("SIGNALBRIDGE_PARSER_API_KEY", problems, min_length=16, alternatives=("OPENAI_API_KEY", "GROQ_API_KEY"))
    _require_secret("SIGNALBRIDGE_TELEGRAM_API_HASH", problems, min_length=32, alternatives=("TELEGRAM_API_HASH",))
    _require_int("SIGNALBRIDGE_TELEGRAM_API_ID", problems, alternatives=("TELEGRAM_API_ID",))

    cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
    if not cors_origins:
        problems.append("CORS_ORIGINS must be set to the deployed dashboard origin")
    if "*" in cors_origins:
        problems.append("CORS_ORIGINS must not contain '*' in production")
    for origin in cors_origins:
        if not origin.startswith("https://"):
            problems.append(f"CORS_ORIGINS entry must use https:// in production: {origin}")

    if os.getenv("SIGNALBRIDGE_COOKIE_SECURE", "").strip().lower() not in {"1", "true", "yes", "on"}:
        problems.append("SIGNALBRIDGE_COOKIE_SECURE=true is required in production")

    api_url = os.getenv("SIGNALBRIDGE_API_URL", "").strip()
    if api_url and not api_url.startswith("https://"):
        problems.append("SIGNALBRIDGE_API_URL must use https:// in production")

    # Per-user workspace isolation (Telegram session, config, master key, logs)
    # was completed in commit 03847c2 (fix(security): isolate Telegram sessions
    # per workspace). ALLOW_PUBLIC_SIGNUP is now safe to enable; this check is
    # intentionally left in place as a reminder to re-audit isolation if the
    # workspace bootstrapping logic changes again.


    if not trusted_hosts_from_env():
        problems.append("TRUSTED_HOSTS must list the public API hostname(s)")

    if problems:
        joined = "; ".join(problems)
        raise ProductionSecurityError(f"unsafe production configuration: {joined}")


def cors_origins_from_env() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if not raw:
        return LOCAL_ORIGINS
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or LOCAL_ORIGINS


def trusted_hosts_from_env() -> list[str] | None:
    raw = os.getenv("TRUSTED_HOSTS", "").strip()
    if not raw:
        return None if not is_production() else []
    return [host.strip() for host in raw.split(",") if host.strip()]


def apply_security_headers(request: Request, headers: dict[str, str]) -> None:
    headers["X-Content-Type-Options"] = "nosniff"
    headers["X-Frame-Options"] = "DENY"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    headers["Cache-Control"] = "no-store"
    if is_production() or request.url.scheme == "https":
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


@dataclass(slots=True)
class RateLimit:
    max_requests: int
    window_seconds: int


class InMemoryRateLimiter:
    """Small per-process rate limiter for sensitive local API endpoints."""

    def __init__(self) -> None:
        self._events: dict[str, Deque[float]] = defaultdict(deque)

    def check(self, request: Request, scope: str, limit: RateLimit) -> None:
        now = time.monotonic()
        key = f"{scope}:{client_ip(request)}"
        events = self._events[key]
        cutoff = now - limit.window_seconds
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= limit.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many attempts; wait before retrying",
            )
        events.append(now)


def client_ip(request: Request) -> str:
    # Forwarded headers are client-controlled unless a reverse proxy removes and
    # re-adds them. Only trust them after that proxy boundary is deliberately
    # configured, otherwise a caller could evade rate limits by forging XFF.
    forwarded_for = request.headers.get("x-forwarded-for", "") if _env_flag("SIGNALBRIDGE_TRUST_PROXY_HEADERS") else ""
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def _require_secret(name: str, problems: list[str], *, min_length: int, alternatives: tuple[str, ...] = ()) -> None:
    resolved_name, value = _first_env(name, *alternatives)
    if not value:
        names = ", ".join((name, *alternatives))
        problems.append(f"one of {names} must be set")
        return
    lowered = value.lower()
    if len(value) < min_length:
        problems.append(f"{resolved_name} must be at least {min_length} characters")
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        problems.append(f"{resolved_name} still contains a placeholder value")


def _require_int(name: str, problems: list[str], *, alternatives: tuple[str, ...] = ()) -> None:
    resolved_name, value = _first_env(name, *alternatives)
    if not value:
        names = ", ".join((name, *alternatives))
        problems.append(f"one of {names} must be set")
        return
    try:
        int(value)
    except ValueError:
        problems.append(f"{resolved_name} must be an integer")


def _first_env(*names: str) -> tuple[str, str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return name, value
    return names[0], ""


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}
