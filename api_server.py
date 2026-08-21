"""
SignalBridge local API bridge.

The FastAPI app exposes redacted configuration, runtime status, logs, bot
control, and Telegram phone-auth endpoints. Sensitive values remain encrypted
on the VM filesystem and are never returned to the dashboard.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from auth_manager import (
    AUTH_COOKIE_NAME,
    AuthError,
    AuthManager,
    InvalidCredentialsError,
    PasswordResetError,
    SessionClaims,
    SessionValidationError,
    SignupDisabledError,
    session_idle_timeout_seconds,
)
from password_reset_mailer import PasswordResetDeliveryError, PasswordResetMailer
from bot_runtime import RuntimeConfigurationError, RuntimeSupervisorError, TelegramAuthError
from database import create_all_tables, create_async_engine_from_env, create_session_maker
from google_oauth import GoogleOAuthError, GoogleOAuthService, STATE_COOKIE_NAME
from config_manager import AppConfig, ConfigManager, ConfigManagerError, ConfigValidationError
from workspace_services import WorkspaceServiceRegistry, WorkspaceServices
from workspace_repository import WorkspaceRepository
from production_security import (
    InMemoryRateLimiter,
    RateLimit,
    apply_security_headers,
    cors_origins_from_env,
    trusted_hosts_from_env,
    validate_production_environment,
)


class ApiServerError(RuntimeError):
    """Base API bridge error."""


class TelegramConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone_number: str = ""
    monitored_chats: list[str] = Field(default_factory=list)


ExchangeIdPayload = Literal[
    "bybit",
    "bingx",
    "binanceusdm",
    "okx",
    "bitget",
    "kucoinfutures",
    "mexc",
    "gateio",
    "phemex",
    "coinex",
]


class ExchangeConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    exchange_id: ExchangeIdPayload = "bybit"
    mode: Literal["testnet", "mainnet"] = "testnet"
    default_leverage: int = Field(default=3, ge=1, le=125)
    api_key: str | None = Field(default=None, repr=False)
    api_secret: str | None = Field(default=None, repr=False)
    api_password: str | None = Field(default=None, repr=False)


class OpenAIConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["openai", "groq"] = "openai"
    model: str = "gpt-4o-mini"
    request_timeout_seconds: int = Field(default=20, ge=1, le=120)
    api_key: str | None = Field(default=None, repr=False)


class RiskConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    risk_mode: Literal["fixed_usdt", "balance_percent"] = "fixed_usdt"
    fixed_usdt_risk: float = Field(default=25.0, gt=0)
    balance_risk_percent: float = Field(default=1.0, gt=0, le=100)
    max_leverage: int = Field(default=10, ge=1, le=125)
    daily_trade_limit: int | None = Field(default=None, ge=1)
    max_take_profit_orders: int = Field(default=1, ge=1, le=10)


class SecurityConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    api_bearer_token: str | None = Field(default=None, min_length=32, repr=False)


class ConfigUpdatePayload(BaseModel):
    """Dashboard update payload.

    Secret fields use null to keep the existing encrypted value, an empty string
    to clear it, or a non-empty plaintext value to replace it.
    """

    model_config = ConfigDict(extra="forbid")

    security: SecurityConfigPayload = Field(default_factory=SecurityConfigPayload)
    telegram: TelegramConfigPayload = Field(default_factory=TelegramConfigPayload)
    exchange: ExchangeConfigPayload = Field(default_factory=ExchangeConfigPayload)
    openai: OpenAIConfigPayload = Field(default_factory=OpenAIConfigPayload)
    risk: RiskConfigPayload = Field(default_factory=RiskConfigPayload)


class TelegramVerifyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(min_length=1)
    password: str | None = Field(default=None, repr=False)


class TelegramPasswordVerifyPayload(BaseModel):
    # Telegram passwords may intentionally begin or end with a space, so do
    # not apply the general payload whitespace normalisation here.
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=512, repr=False)


class AuthSignupPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=512, repr=False)


class AuthLoginPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=512, repr=False)


class PasswordResetRequestPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=254)


class PasswordResetConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: str = Field(min_length=1, max_length=512, repr=False)
    password: str = Field(min_length=8, max_length=512, repr=False)


def create_app(
    config_manager: ConfigManager | None = None,
    workspace_registry: WorkspaceServiceRegistry | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Create the secured API app used by uvicorn and tests."""

    manager = config_manager or ConfigManager()
    auth_manager = AuthManager()
    password_reset_mailer = PasswordResetMailer()
    rate_limiter = InMemoryRateLimiter()
    bearer = HTTPBearer(auto_error=False)
    registry = workspace_registry or WorkspaceServiceRegistry()
    google_oauth = GoogleOAuthService()
    workspace_repository = _workspace_repository_from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        validate_production_environment()
        if workspace_repository is not None and _env_flag("SIGNALBRIDGE_BOOTSTRAP_DATABASE"):
            engine = create_async_engine_from_env()
            try:
                await create_all_tables(engine)
            finally:
                await engine.dispose()
        try:
            yield
        finally:
            await registry.shutdown_all()

    app = FastAPI(title="SignalBridge API", version="1.0.0", lifespan=lifespan)
    app.state.config_manager = manager
    app.state.auth_manager = auth_manager
    app.state.password_reset_mailer = password_reset_mailer
    app.state.workspace_registry = registry
    app.state.google_oauth = google_oauth
    app.state.workspace_repository = workspace_repository

    trusted_hosts = trusted_hosts_from_env()
    if trusted_hosts is not None:
        if not trusted_hosts:
            raise RuntimeError("TRUSTED_HOSTS must be configured in production")
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or cors_origins_from_env(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-SignalBridge-User-Activity"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        apply_security_headers(request, response.headers)
        return response

    def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        expected = _expected_bearer_token(manager)
        supplied = credentials.credentials if credentials else ""
        if not expected or supplied != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def require_session(request: Request, response: Response) -> SessionClaims:
        token = request.cookies.get(AUTH_COOKIE_NAME, "")
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login required")
        try:
            claims = auth_manager.verify_session(token)
        except SessionValidationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        # Renew only after a valid request caused by recent browser activity.
        # Background dashboard polling still validates the session, but cannot
        # keep it alive after the user stops using the site.
        user = auth_manager.get_user_by_id(claims.sub)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="session user no longer exists")
        if request.headers.get("x-signalbridge-user-activity") == "1":
            _set_session_cookie(response, auth_manager.issue_session(user, workspace_id=claims.workspace_id))
        return claims

    async def require_workspace_context(claims: SessionClaims = Depends(require_session)) -> tuple[SessionClaims, WorkspaceServices]:
        workspace_id = claims.workspace_id or auth_manager.workspace_id_for_user(claims.sub)
        if not workspace_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="workspace membership is required")
        services = await registry.get(workspace_id)
        return claims, services

    protected = [Depends(require_token), Depends(require_session)]

    def limit_auth_attempts(request: Request) -> None:
        rate_limiter.check(request, "auth", RateLimit(max_requests=10, window_seconds=60))

    def limit_telegram_code_attempts(request: Request) -> None:
        rate_limiter.check(request, "telegram-code", RateLimit(max_requests=3, window_seconds=300))

    def limit_telegram_verify_attempts(request: Request) -> None:
        rate_limiter.check(request, "telegram-verify", RateLimit(max_requests=5, window_seconds=300))

    def limit_password_reset_requests(request: Request) -> None:
        rate_limiter.check(request, "password-reset-request", RateLimit(max_requests=3, window_seconds=3600))

    def limit_password_reset_confirmations(request: Request) -> None:
        rate_limiter.check(request, "password-reset-confirm", RateLimit(max_requests=5, window_seconds=900))

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "workspace_count": registry.workspace_count()}

    @app.get("/auth/state", dependencies=[Depends(require_token)])
    async def auth_state() -> dict[str, Any]:
        return {
            "signup_enabled": auth_manager.signup_enabled(),
            "workspace_mode": "multi_tenant",
            "google_oauth_enabled": google_oauth.enabled(),
            "google_oauth_error": google_oauth.oauth_error_message(),
        }

    @app.get("/auth/google/start", dependencies=[Depends(require_token)])
    async def google_start(next: str = "/dashboard") -> Response:
        try:
            authorize_url, state_cookie = google_oauth.build_authorize_url(next)
        except GoogleOAuthError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

        response = Response(status_code=status.HTTP_302_FOUND)
        response.headers["Location"] = authorize_url
        response.set_cookie(
            key=STATE_COOKIE_NAME,
            value=state_cookie,
            max_age=600,
            httponly=True,
            secure=_cookie_secure(),
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/auth/google/callback")
    async def google_callback(code: str, state: str, response: Response, request: Request) -> Response:
        if not state:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google sign-in state is missing")

        state_cookie = request.cookies.get(STATE_COOKIE_NAME, "")
        if not state_cookie:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google sign-in state cookie is missing")

        try:
            state_payload = google_oauth.parse_state_cookie(state_cookie)
            if state_payload.state != state:
                raise GoogleOAuthError("Google sign-in state does not match the callback")
            profile = await google_oauth.exchange_code(code, state_cookie)
            if workspace_repository is None:
                raise GoogleOAuthError("Google OAuth requires DATABASE_URL to be configured")
            principal = await workspace_repository.resolve_google_identity(profile)
            session_token = auth_manager.issue_session(principal.user, workspace_id=principal.workspace.id)
        except GoogleOAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        _set_session_cookie(response, session_token)
        response.delete_cookie(key=STATE_COOKIE_NAME, path="/")
        await _bootstrap_workspace(principal.workspace.id, principal.workspace.slug, registry)
        redirect_to = state_payload.next_path
        response.status_code = status.HTTP_302_FOUND
        response.headers["Location"] = redirect_to
        return response

    @app.post("/auth/signup", dependencies=[Depends(require_token), Depends(limit_auth_attempts)])
    async def signup(payload: AuthSignupPayload, response: Response) -> dict[str, Any]:
        try:
            user = auth_manager.create_user(payload.email, payload.password, payload.name)
            session_token = auth_manager.issue_session(user, workspace_id=user.workspace_id)
        except SignupDisabledError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except (AuthError, ValueError, ValidationError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        _set_session_cookie(response, session_token)
        workspace_services = await registry.get(user.workspace_id or auth_manager.workspace_id_for_user(user.id) or user.id)
        workspace_services.log_store.append("info", "workspace user created", email=user.email, workspace_id=user.workspace_id)
        return {"user": auth_manager.public_user(user), "signup_enabled": auth_manager.signup_enabled()}

    @app.post("/auth/login", dependencies=[Depends(require_token), Depends(limit_auth_attempts)])
    async def login(payload: AuthLoginPayload, response: Response) -> dict[str, Any]:
        try:
            user = auth_manager.authenticate(payload.email, payload.password)
            session_token = auth_manager.issue_session(user, workspace_id=user.workspace_id)
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        _set_session_cookie(response, session_token)
        workspace_services = await registry.get(user.workspace_id or auth_manager.workspace_id_for_user(user.id) or user.id)
        workspace_services.log_store.append("info", "workspace user logged in", email=user.email, workspace_id=user.workspace_id)
        return {"user": auth_manager.public_user(user), "signup_enabled": auth_manager.signup_enabled()}

    @app.post("/auth/forgot-password", dependencies=[Depends(require_token), Depends(limit_password_reset_requests)])
    async def forgot_password(payload: PasswordResetRequestPayload) -> dict[str, bool]:
        # Always return the same result so this endpoint cannot be used to find
        # out whether a particular email has an account.
        if not password_reset_mailer.configured():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="password recovery is not configured; contact support",
            )
        try:
            token = auth_manager.create_password_reset_token(payload.email)
            if token:
                password_reset_mailer.send_password_reset(payload.email.strip().lower(), token)
        except PasswordResetDeliveryError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="password recovery email could not be sent; try again later",
            ) from exc
        except (AuthError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="password recovery could not be started") from exc
        return {"accepted": True}

    @app.post("/auth/reset-password", dependencies=[Depends(require_token), Depends(limit_password_reset_confirmations)])
    async def reset_password(payload: PasswordResetConfirmPayload) -> dict[str, bool]:
        try:
            auth_manager.reset_password(payload.token, payload.password)
        except PasswordResetError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except (AuthError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        return {"reset": True}

    @app.get("/auth/me", dependencies=[Depends(require_token)])
    async def me(claims: SessionClaims = Depends(require_session)) -> dict[str, Any]:
        return {"user": auth_manager.public_user(claims), "signup_enabled": auth_manager.signup_enabled()}

    @app.post("/auth/logout", dependencies=[Depends(require_token)])
    async def logout(response: Response) -> dict[str, Any]:
        _clear_session_cookie(response)
        return {"ok": True}

    @app.get("/status", dependencies=protected)
    async def get_status(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        return await services.runtime.get_status()

    @app.get("/logs", dependencies=protected)
    async def get_logs(limit: int = 200, workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        return {"logs": [entry.model_dump(mode="json") for entry in services.log_store.list(limit)]}

    @app.get("/exchange/state", dependencies=protected)
    async def get_exchange_state(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.get_exchange_snapshot()
        except RuntimeConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except RuntimeSupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/config", dependencies=protected)
    async def get_config(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        config = _load_or_initialize(services.config_manager)
        return _redacted_config_response(services.config_manager, config)

    @app.post("/config", dependencies=protected)
    async def update_config(payload: ConfigUpdatePayload, workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        current = _load_or_initialize(services.config_manager)
        try:
            updated = _merge_config(services.config_manager, current, payload)
            services.config_manager.save_config(updated)
            await services.runtime.on_config_updated(current, updated)
        except (ConfigManagerError, ValidationError, ValueError, RuntimeSupervisorError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

        services.log_store.append("info", "configuration updated")
        return _redacted_config_response(services.config_manager, updated)

    @app.post("/bot/start", dependencies=protected)
    async def start_bot(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.start_bot()
        except RuntimeConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except RuntimeSupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/bot/stop", dependencies=protected)
    async def stop_bot(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.stop_bot()
        except RuntimeSupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/telegram/request-code", dependencies=[Depends(require_token), Depends(require_session), Depends(limit_telegram_code_attempts)])
    async def request_telegram_code(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.request_telegram_code()
        except RuntimeConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except TelegramAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post(
        "/telegram/verify-code",
        dependencies=[Depends(require_token), Depends(require_session), Depends(limit_telegram_verify_attempts)],
    )
    async def verify_telegram_code(payload: TelegramVerifyPayload, workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.verify_telegram_code(payload.code)
        except TelegramAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post(
        "/telegram/verify-password",
        dependencies=[Depends(require_token), Depends(require_session), Depends(limit_telegram_verify_attempts)],
    )
    async def verify_telegram_password(payload: TelegramPasswordVerifyPayload, workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.verify_telegram_password(payload.password)
        except TelegramAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/telegram/chats", dependencies=protected)
    async def list_telegram_chats(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.list_available_telegram_chats()
        except RuntimeConfigurationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
        except TelegramAuthError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post("/telegram/logout", dependencies=protected)
    async def logout_telegram(workspace: tuple[SessionClaims, WorkspaceServices] = Depends(require_workspace_context)) -> dict[str, Any]:
        _, services = workspace
        try:
            return await services.runtime.logout_telegram()
        except RuntimeSupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return app


def _workspace_repository_from_env() -> WorkspaceRepository | None:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return None
    engine = create_async_engine_from_env()
    session_maker = create_session_maker(engine)
    return WorkspaceRepository(session_maker)


async def _bootstrap_workspace(workspace_id: str, workspace_slug: str, registry: WorkspaceServiceRegistry) -> None:
    services = await registry.get(workspace_id)
    if not services.config_manager.config_exists():
        services.log_store.append("info", "workspace initialized after Google sign-in", workspace_id=workspace_id, workspace_slug=workspace_slug)


def _load_or_initialize(manager: ConfigManager) -> AppConfig:
    if manager.config_exists():
        return manager.load_config()
    config = manager.initialize_empty_config()
    manager.save_config(config)
    return config


def _merge_config(manager: ConfigManager, current: AppConfig, payload: ConfigUpdatePayload) -> AppConfig:
    data = current.model_dump(mode="json")

    if payload.security.api_bearer_token:
        if not _env_flag("ALLOW_DASHBOARD_TOKEN_ROTATION"):
            raise ConfigValidationError("dashboard API token is managed by deployment environment")
        data["security"]["api_bearer_token"] = payload.security.api_bearer_token

    data["telegram"]["phone_number"] = payload.telegram.phone_number
    data["telegram"]["monitored_chats"] = payload.telegram.monitored_chats
    data["exchange"]["exchange_id"] = payload.exchange.exchange_id
    data["exchange"]["mode"] = payload.exchange.mode
    data["exchange"]["default_leverage"] = payload.exchange.default_leverage
    data["openai"]["provider"] = payload.openai.provider
    data["openai"]["model"] = payload.openai.model
    data["openai"]["request_timeout_seconds"] = payload.openai.request_timeout_seconds
    data["risk"] = payload.risk.model_dump(mode="json")

    if payload.exchange.api_key is not None:
        data["exchange"]["encrypted_api_key"] = manager.encrypt_secret(payload.exchange.api_key)
    if payload.exchange.api_secret is not None:
        data["exchange"]["encrypted_api_secret"] = manager.encrypt_secret(payload.exchange.api_secret)
    if payload.exchange.api_password is not None:
        data["exchange"]["encrypted_api_password"] = manager.encrypt_secret(payload.exchange.api_password)
    if payload.openai.api_key is not None:
        if not _env_flag("ALLOW_DASHBOARD_PARSER_KEY_UPDATE"):
            raise ConfigValidationError("parser API key is managed by deployment environment")
        data["openai"]["encrypted_api_key"] = manager.encrypt_secret(payload.openai.api_key)

    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc


def _redacted_config_response(manager: ConfigManager, config: AppConfig) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "security": {"api_bearer_token_set": bool(config.security.api_bearer_token)},
        "telegram": {
            "app_configured": manager.telegram_app_configured(config),
            "phone_number": config.telegram.phone_number,
            "monitored_chats": config.telegram.monitored_chats,
        },
        "exchange": {
            "exchange_id": config.exchange.exchange_id,
            "mode": config.exchange.mode,
            "default_leverage": config.exchange.default_leverage,
            "api_key_set": bool(config.exchange.encrypted_api_key),
            "api_secret_set": bool(config.exchange.encrypted_api_secret),
            "api_password_set": bool(config.exchange.encrypted_api_password),
        },
        "openai": {
            "provider": config.openai.provider,
            "model": config.openai.model,
            "request_timeout_seconds": config.openai.request_timeout_seconds,
            "api_key_set": manager.parser_api_key_configured(config),
        },
        "risk": config.risk.model_dump(mode="json"),
    }


def _expected_bearer_token(manager: ConfigManager) -> str:
    env_token = os.getenv("API_BEARER_TOKEN", "").strip()
    if env_token:
        return env_token
    raise ConfigValidationError("API_BEARER_TOKEN must be provided through the environment")


def _set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=session_token,
        max_age=_session_cookie_max_age(),
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _session_cookie_max_age() -> int:
    return session_idle_timeout_seconds()


def _cookie_secure() -> bool:
    raw = os.getenv("SIGNALBRIDGE_COOKIE_SECURE", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return os.getenv("ENVIRONMENT", "").strip().lower() in {"prod", "production"}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


app = create_app()
