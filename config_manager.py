"""
SignalBridge secure local configuration manager.

This module is intentionally small and dependency-light. It owns the local
config.json/master.key pair used by the VM worker and API bridge. Secrets are
encrypted before they are persisted, and decrypted values are never included in
model repr output.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_MASTER_KEY_PATH = Path("master.key")
SCHEMA_VERSION = 2
SECRET_PREFIX = "sb:v1:"


class ConfigManagerError(RuntimeError):
    """Base error for configuration failures."""


class ConfigFileNotFoundError(ConfigManagerError):
    """Raised when config.json is required but does not exist."""


class ConfigParseError(ConfigManagerError):
    """Raised when config.json is not valid JSON or not an object."""


class ConfigValidationError(ConfigManagerError):
    """Raised when config.json fails schema validation."""


class MasterKeyError(ConfigManagerError):
    """Raised when master.key is missing, corrupt, or unusable."""


class SecretDecryptionError(ConfigManagerError):
    """Raised when an encrypted secret cannot be decrypted with master.key."""


class ExchangeId(str, Enum):
    """CCXT-backed exchanges supported by SignalBridge futures execution."""

    BYBIT = "bybit"
    BINGX = "bingx"
    BINANCE_USDM = "binanceusdm"
    OKX = "okx"
    BITGET = "bitget"
    KUCOIN_FUTURES = "kucoinfutures"
    MEXC = "mexc"
    GATEIO = "gateio"
    PHEMEX = "phemex"
    COINEX = "coinex"


class ExchangeMode(str, Enum):
    """Supported exchange environments."""

    TESTNET = "testnet"
    MAINNET = "mainnet"


BybitMode = ExchangeMode


class RiskMode(str, Enum):
    """Supported position-sizing strategies."""

    FIXED_USDT = "fixed_usdt"
    BALANCE_PERCENT = "balance_percent"


class ModelProvider(str, Enum):
    """Supported hosted model providers for signal parsing."""

    OPENAI = "openai"
    GROQ = "groq"


class SecretModel(BaseModel):
    """Pydantic base model that hides field values from repr by default."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class SecurityConfig(SecretModel):
    """Security settings for the local FastAPI bridge."""

    api_bearer_token: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        min_length=32,
        repr=False,
        description="Static bearer token required by the dashboard/API bridge.",
    )


class TelegramConfig(SecretModel):
    """Telegram client settings for the phone-number authentication flow."""

    api_id: int | None = Field(
        default=None,
        ge=1,
        description="Telegram API ID from https://my.telegram.org.",
    )
    api_hash: str = Field(default="", repr=False)
    phone_number: str = Field(default="")
    session_name: str = Field(default="signalbridge")
    monitored_chats: list[str] = Field(
        default_factory=list,
        description="Optional Telegram usernames, channel IDs, or invite-style names to monitor.",
    )

    @field_validator("api_hash")
    @classmethod
    def validate_api_hash(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Fa-f0-9]{32}", value):
            raise ValueError("telegram.api_hash must be a 32-character hex string")
        return value

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        if value and not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("telegram.phone_number must use E.164 format, e.g. +15551234567")
        return value

    @field_validator("session_name")
    @classmethod
    def validate_session_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("telegram.session_name may contain only letters, numbers, dots, underscores, and hyphens")
        return value

    @field_validator("monitored_chats")
    @classmethod
    def validate_monitored_chats(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for chat in value:
            clean_chat = chat.strip()
            if not clean_chat:
                continue
            if len(clean_chat) > 128:
                raise ValueError("telegram.monitored_chats entries must be 128 characters or less")
            if clean_chat not in seen:
                normalized.append(clean_chat)
                seen.add(clean_chat)
        return normalized


class ExchangeConfig(SecretModel):
    """Exchange execution settings.

    API credential fields must contain encrypted values at rest. Use
    ConfigManager.encrypt_secret before assigning real credentials. Some
    venues, such as OKX, Bitget, and KuCoin Futures, require the optional
    password/passphrase value.
    """

    exchange_id: ExchangeId = Field(default=ExchangeId.BYBIT)
    mode: ExchangeMode = Field(default=ExchangeMode.TESTNET)
    encrypted_api_key: str = Field(default="", repr=False)
    encrypted_api_secret: str = Field(default="", repr=False)
    encrypted_api_password: str = Field(default="", repr=False)
    default_leverage: int = Field(default=3, ge=1, le=125)

    @field_validator("encrypted_api_key", "encrypted_api_secret", "encrypted_api_password")
    @classmethod
    def validate_encrypted_secret_shape(cls, value: str) -> str:
        if value and not value.startswith(SECRET_PREFIX):
            raise ValueError(f"encrypted secrets must start with {SECRET_PREFIX!r}")
        return value


BybitConfig = ExchangeConfig


class OpenAIConfig(SecretModel):
    """Hosted parser model settings."""

    provider: ModelProvider = Field(default=ModelProvider.GROQ)
    model: str = Field(default="openai/gpt-oss-120b", min_length=1)
    encrypted_api_key: str = Field(default="", repr=False)
    request_timeout_seconds: int = Field(default=20, ge=1, le=120)

    @field_validator("encrypted_api_key")
    @classmethod
    def validate_encrypted_api_key_shape(cls, value: str) -> str:
        if value and not value.startswith(SECRET_PREFIX):
            raise ValueError(f"encrypted secrets must start with {SECRET_PREFIX!r}")
        return value


class RiskConfig(SecretModel):
    """Risk controls used later by the execution engine."""

    risk_mode: RiskMode = Field(default=RiskMode.FIXED_USDT)
    fixed_usdt_risk: float = Field(default=25.0, gt=0)
    balance_risk_percent: float = Field(default=1.0, gt=0, le=100)
    max_leverage: int = Field(default=10, ge=1, le=125)
    daily_trade_limit: int | None = Field(default=None, ge=1)
    max_take_profit_orders: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_risk_mode_settings(self) -> "RiskConfig":
        if self.risk_mode == RiskMode.FIXED_USDT and self.fixed_usdt_risk <= 0:
            raise ValueError("fixed_usdt_risk must be greater than zero when risk_mode is fixed_usdt")
        if self.risk_mode == RiskMode.BALANCE_PERCENT and self.balance_risk_percent <= 0:
            raise ValueError(
                "balance_risk_percent must be greater than zero when risk_mode is balance_percent"
            )
        return self


class AppConfig(SecretModel):
    """Top-level SignalBridge configuration."""

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    bot_should_run: bool = Field(default=False)


class RuntimeSecrets(SecretModel):
    """Decrypted secrets for dependency injection into runtime services."""

    exchange_api_key: str = Field(default="", repr=False)
    exchange_api_secret: str = Field(default="", repr=False)
    exchange_api_password: str = Field(default="", repr=False)
    openai_api_key: str = Field(default="", repr=False)


class ConfigManager:
    """Read, validate, encrypt, decrypt, and atomically write local config."""

    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        master_key_path: str | Path = DEFAULT_MASTER_KEY_PATH,
    ) -> None:
        self.config_path = Path(config_path)
        self.master_key_path = Path(master_key_path)
        self._fernet: Fernet | None = None

    def config_exists(self) -> bool:
        """Return True when config.json exists."""

        return self.config_path.is_file()

    def initialize_empty_config(self) -> AppConfig:
        """Create a valid in-memory config with safe defaults.

        This method does not write config.json. Call save_config when the caller
        is ready to persist the initialized config.
        """

        self._get_fernet()
        return AppConfig()

    def load_config(self) -> AppConfig:
        """Load and validate config.json."""

        if not self.config_exists():
            raise ConfigFileNotFoundError(f"Config file does not exist: {self.config_path}")

        try:
            raw_text = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigParseError(f"Unable to read config file: {self.config_path}") from exc

        try:
            raw_data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ConfigParseError(f"Config file is not valid JSON: {self.config_path}") from exc

        if not isinstance(raw_data, dict):
            raise ConfigParseError("Config file root must be a JSON object")

        try:
            return AppConfig.model_validate(self._normalize_payload(raw_data))
        except ValidationError as exc:
            raise ConfigValidationError(str(exc)) from exc

    def save_config(self, config: AppConfig) -> None:
        """Atomically write config.json with encrypted-at-rest fields intact."""

        validated_config = AppConfig.model_validate(config)
        self._get_fernet()
        self._ensure_secret_fields_are_encrypted(validated_config)
        payload = validated_config.model_dump(mode="json")
        encoded = json.dumps(payload, indent=2, sort_keys=True)

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.config_path.name}.",
            suffix=".tmp",
            dir=str(self.config_path.parent),
            text=True,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
                temp_file.write(encoded)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())

            os.replace(temp_name, self.config_path)
            self._harden_file_permissions(self.config_path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise ConfigManagerError(f"Unable to write config file: {self.config_path}") from exc

    def encrypt_secret(self, value: str) -> str:
        """Encrypt a secret string for storage.

        Empty values stay empty so optional credentials do not become noisy
        encrypted blobs in config.json.
        """

        if value == "":
            return ""
        token = self._get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
        return f"{SECRET_PREFIX}{token}"

    def decrypt_secret(self, value: str) -> str:
        """Decrypt a secret created by encrypt_secret."""

        if value == "":
            return ""
        if not value.startswith(SECRET_PREFIX):
            raise SecretDecryptionError("Encrypted secret is missing the SignalBridge secret prefix")

        token = value.removeprefix(SECRET_PREFIX)
        try:
            return self._get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise SecretDecryptionError("Encrypted secret could not be decrypted with the current master.key") from exc

    def decrypt_runtime_secrets(self, config: AppConfig) -> RuntimeSecrets:
        """Return decrypted credentials for runtime services.

        The returned model also hides values in repr, but callers should still
        avoid logging or serializing it.
        """

        return RuntimeSecrets(
            exchange_api_key=self.decrypt_secret(config.exchange.encrypted_api_key),
            exchange_api_secret=self.decrypt_secret(config.exchange.encrypted_api_secret),
            exchange_api_password=self.decrypt_secret(config.exchange.encrypted_api_password),
            openai_api_key=self.resolve_parser_api_key(config),
        )

    def resolve_parser_api_key(self, config: AppConfig) -> str:
        """Return the deployment-owned parser key.

        In production, the parser key must come from environment variables.
        Legacy config.json fallback remains available for local development only.
        """

        env_key = self._parser_api_key_from_env(config.openai)
        if env_key:
            return env_key
        if self._is_production_env():
            raise ConfigValidationError(
                "parser API key must be provided through environment variables in production"
            )
        return self.decrypt_secret(config.openai.encrypted_api_key)

    def parser_api_key_configured(self, config: AppConfig) -> bool:
        """Return True when a parser key is available without exposing it."""

        if self._parser_api_key_from_env(config.openai):
            return True
        if self._is_production_env():
            return False
        return bool(config.openai.encrypted_api_key)

    def resolve_telegram_api_id(self, config: AppConfig) -> int | None:
        """Return the deployment-owned Telegram API ID, falling back to config."""

        env_value = self._first_env_value("SIGNALBRIDGE_TELEGRAM_API_ID", "TELEGRAM_API_ID")
        if env_value:
            try:
                return int(env_value)
            except ValueError as exc:
                raise ConfigValidationError("SIGNALBRIDGE_TELEGRAM_API_ID must be an integer") from exc
        return config.telegram.api_id

    def resolve_telegram_api_hash(self, config: AppConfig) -> str:
        """Return the deployment-owned Telegram API hash, falling back to config."""

        return self._first_env_value("SIGNALBRIDGE_TELEGRAM_API_HASH", "TELEGRAM_API_HASH") or config.telegram.api_hash

    def resolve_telegram_session_name(self, config: AppConfig) -> str:
        """Return the deployment-owned Telegram session name, falling back to config."""

        return (
            self._first_env_value("SIGNALBRIDGE_TELEGRAM_SESSION_NAME", "TELEGRAM_SESSION_NAME")
            or config.telegram.session_name
        )

    def telegram_app_configured(self, config: AppConfig) -> bool:
        """Return True when the backend has Telegram app credentials available."""

        return bool(self.resolve_telegram_api_id(config) and self.resolve_telegram_api_hash(config))

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(self._load_or_create_master_key())
        return self._fernet

    def _load_or_create_master_key(self) -> bytes:
        if not self.master_key_path.exists():
            return self._create_master_key()

        try:
            key = self.master_key_path.read_bytes().strip()
            Fernet(key)
        except (OSError, ValueError) as exc:
            raise MasterKeyError(f"master.key is corrupt or unreadable: {self.master_key_path}") from exc

        return key

    def _create_master_key(self) -> bytes:
        key = Fernet.generate_key()
        self.master_key_path.parent.mkdir(parents=True, exist_ok=True)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY

        try:
            fd = os.open(str(self.master_key_path), flags, 0o600)
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(key)
                key_file.write(b"\n")
                key_file.flush()
                os.fsync(key_file.fileno())
            self._harden_file_permissions(self.master_key_path)
        except FileExistsError:
            return self._load_or_create_master_key()
        except OSError as exc:
            raise MasterKeyError(f"Unable to create master.key: {self.master_key_path}") from exc

        return key

    @staticmethod
    def _harden_file_permissions(path: Path) -> None:
        """Best-effort owner-only permissions on POSIX; harmless on Windows."""

        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows ACL hardening is environment-specific; later deployment
            # automation can apply an explicit service-account ACL if needed.
            pass

    @staticmethod
    def _parser_api_key_from_env(openai_config: OpenAIConfig) -> str:
        provider = str(getattr(openai_config.provider, "value", openai_config.provider))
        provider_specific = {
            ModelProvider.GROQ.value: ("GROQ_API_KEY",),
            ModelProvider.OPENAI.value: ("OPENAI_API_KEY",),
        }
        env_names = (
            "SIGNALBRIDGE_PARSER_API_KEY",
            "PARSER_API_KEY",
            *provider_specific.get(provider, ()),
        )

        for env_name in env_names:
            value = os.getenv(env_name, "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _first_env_value(*names: str) -> str:
        for name in names:
            value = os.getenv(name, "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _is_production_env() -> bool:
        return os.getenv("ENVIRONMENT", "").strip().lower() in {"prod", "production"}

    @staticmethod
    def _ensure_secret_fields_are_encrypted(config: AppConfig) -> None:
        secret_values: Iterable[tuple[str, str]] = (
            ("exchange.encrypted_api_key", config.exchange.encrypted_api_key),
            ("exchange.encrypted_api_secret", config.exchange.encrypted_api_secret),
            ("exchange.encrypted_api_password", config.exchange.encrypted_api_password),
            ("openai.encrypted_api_key", config.openai.encrypted_api_key),
        )
        invalid_fields = [
            field_name
            for field_name, value in secret_values
            if value and not value.startswith(SECRET_PREFIX)
        ]
        if invalid_fields:
            joined_fields = ", ".join(invalid_fields)
            raise ConfigValidationError(f"Secret fields must be encrypted before save: {joined_fields}")

    @staticmethod
    def _normalize_payload(raw_data: dict[str, Any]) -> dict[str, Any]:
        """Prepare older config payloads for current schema validation."""

        normalized = dict(raw_data)
        normalized["schema_version"] = SCHEMA_VERSION
        if "exchange" not in normalized and isinstance(normalized.get("bybit"), dict):
            legacy_bybit = dict(normalized["bybit"])
            legacy_bybit.setdefault("exchange_id", ExchangeId.BYBIT.value)
            legacy_bybit.setdefault("encrypted_api_password", "")
            normalized["exchange"] = legacy_bybit
        normalized.pop("bybit", None)
        return normalized


def load_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Convenience helper for scripts that only need to read config.json."""

    return ConfigManager(config_path=config_path).load_config()


def _redact_config_for_display(config: AppConfig) -> dict[str, Any]:
    """Return a non-secret diagnostic view for CLI/debug use."""

    data = config.model_dump(mode="json")
    data["security"]["api_bearer_token"] = "***"
    data["telegram"]["api_hash"] = "***" if config.telegram.api_hash else ""
    data["telegram"]["phone_number"] = "***" if config.telegram.phone_number else ""
    data["exchange"]["encrypted_api_key"] = "***" if config.exchange.encrypted_api_key else ""
    data["exchange"]["encrypted_api_secret"] = "***" if config.exchange.encrypted_api_secret else ""
    data["exchange"]["encrypted_api_password"] = "***" if config.exchange.encrypted_api_password else ""
    data["openai"]["encrypted_api_key"] = "***" if config.openai.encrypted_api_key else ""
    return data


if __name__ == "__main__":
    manager = ConfigManager()
    config = manager.load_config() if manager.config_exists() else manager.initialize_empty_config()
    print(json.dumps(_redact_config_for_display(config), indent=2, sort_keys=True))
