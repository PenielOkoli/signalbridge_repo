"""
Local authentication and session management for SignalBridge.

This module intentionally avoids storing users in the trading config. User
records live in users.json with salted PBKDF2 password hashes, while browser
sessions are short-lived HMAC-signed JWT cookies.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


AUTH_COOKIE_NAME = "signalbridge_session"
# A session is renewed while the user is actively making authenticated requests.
# If the browser is inactive for this duration, a new login is required.
DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS = 60 * 10
# Existing records carry their own iteration count and remain valid. New local
# owner passwords use a stronger work factor while OAuth is being introduced.
PASSWORD_HASH_ITERATIONS = 600_000
USER_STORE_VERSION = 2
PASSWORD_RESET_TTL_SECONDS = 60 * 60


class AuthError(RuntimeError):
    """Base class for controlled authentication failures."""


class InvalidCredentialsError(AuthError):
    """Raised when login credentials cannot be verified."""


class SessionValidationError(AuthError):
    """Raised when a browser session is missing, expired, or invalid."""


class SignupDisabledError(AuthError):
    """Raised when public signup is disabled after the first account."""


class PasswordResetError(AuthError):
    """Raised when a password-reset link is invalid or expired."""


class AuthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuthUser(AuthModel):
    id: str
    email: str
    name: str
    workspace_id: str | None = None
    password_hash: str = Field(repr=False)
    created_at: int
    password_changed_at: int = 0

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        clean = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", clean):
            raise ValueError("email address is invalid")
        return clean

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        clean = value.strip()
        if len(clean) < 2:
            raise ValueError("name must contain at least 2 characters")
        return clean


class PublicUser(AuthModel):
    id: str
    email: str
    name: str
    workspace_id: str | None = None


class PasswordResetToken(AuthModel):
    user_id: str
    token_hash: str = Field(repr=False)
    created_at: int
    expires_at: int


class UserStore(AuthModel):
    version: int = USER_STORE_VERSION
    users: list[AuthUser] = Field(default_factory=list)
    password_reset_tokens: list[PasswordResetToken] = Field(default_factory=list)


class SessionClaims(AuthModel):
    sub: str
    email: str
    name: str
    workspace_id: str | None = None
    iat: int
    exp: int


class AuthManager:
    """Own local user storage, password verification, and JWT sessions."""

    def __init__(self, users_path: str | Path = "users.json", signing_secret: str | None = None) -> None:
        self.users_path = Path(users_path)
        self.signing_secret = signing_secret or _resolve_signing_secret()

    def signup_enabled(self) -> bool:
        raw = os.getenv("ALLOW_PUBLIC_SIGNUP", "").strip().lower()
        if not raw:
            return True
        return raw in {"1", "true", "yes", "on"}

    def public_user(self, user: AuthUser | SessionClaims) -> dict[str, str | None]:
        return {
            "id": user.sub if isinstance(user, SessionClaims) else user.id,
            "email": user.email,
            "name": user.name,
            "workspace_id": user.workspace_id,
        }

    def get_user_by_id(self, user_id: str) -> AuthUser | None:
        """Return the current user record for safely renewing a session."""

        return next((user for user in self._load_store().users if user.id == user_id), None)

    def create_user(self, email: str, password: str, name: str) -> AuthUser:
        if not self.signup_enabled():
            raise SignupDisabledError("signup is disabled after the first account")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")

        store = self._load_store()
        clean_email = email.strip().lower()
        if any(user.email == clean_email for user in store.users):
            raise ValueError("an account with this email already exists")

        user = AuthUser(
            id=uuid4().hex,
            email=clean_email,
            name=name,
            workspace_id=uuid4().hex,
            password_hash=_hash_password(password),
            created_at=int(time.time()),
        )
        store.users.append(user)
        self._save_store(store)
        return user

    def authenticate(self, email: str, password: str) -> AuthUser:
        clean_email = email.strip().lower()
        store = self._load_store()
        for user in store.users:
            if user.email == clean_email and _verify_password(password, user.password_hash):
                if not user.workspace_id:
                    user.workspace_id = uuid4().hex
                    self._save_store(store)
                return user
        raise InvalidCredentialsError("invalid email or password")

    def create_password_reset_token(self, email: str) -> str | None:
        """Create one short-lived reset token for an existing local account.

        Only the SHA-256 digest is stored, so a filesystem disclosure cannot be
        used to reset an account. Callers must return the same response whether
        this method finds a user or not to avoid account enumeration.
        """

        store = self._load_store()
        now = int(time.time())
        self._discard_expired_reset_tokens(store, now)
        clean_email = email.strip().lower()
        user = next((candidate for candidate in store.users if candidate.email == clean_email), None)
        if user is None:
            self._save_store(store)
            return None

        token = secrets.token_urlsafe(32)
        store.password_reset_tokens = [item for item in store.password_reset_tokens if item.user_id != user.id]
        store.password_reset_tokens.append(
            PasswordResetToken(
                user_id=user.id,
                token_hash=_hash_reset_token(token),
                created_at=now,
                expires_at=now + PASSWORD_RESET_TTL_SECONDS,
            )
        )
        self._save_store(store)
        return token

    def reset_password(self, token: str, new_password: str) -> AuthUser:
        if len(new_password) < 8:
            raise ValueError("password must be at least 8 characters")
        if not token or len(token) > 512:
            raise PasswordResetError("password-reset link is invalid or expired")

        store = self._load_store()
        now = int(time.time())
        self._discard_expired_reset_tokens(store, now)
        token_hash = _hash_reset_token(token)
        matching = next(
            (item for item in store.password_reset_tokens if hmac.compare_digest(item.token_hash, token_hash)),
            None,
        )
        if matching is None:
            self._save_store(store)
            raise PasswordResetError("password-reset link is invalid or expired")

        user = next((candidate for candidate in store.users if candidate.id == matching.user_id), None)
        if user is None:
            store.password_reset_tokens = [item for item in store.password_reset_tokens if item is not matching]
            self._save_store(store)
            raise PasswordResetError("password-reset link is invalid or expired")

        user.password_hash = _hash_password(new_password)
        user.password_changed_at = now
        # Reset links are single-use and changing the password invalidates every
        # other outstanding recovery link for that account.
        store.password_reset_tokens = [item for item in store.password_reset_tokens if item.user_id != user.id]
        self._save_store(store)
        return user

    def issue_session(self, user: AuthUser, ttl_seconds: int | None = None, workspace_id: str | None = None) -> str:
        now = int(time.time())
        ttl = ttl_seconds or session_idle_timeout_seconds()
        issued_at = max(now, user.password_changed_at + 1)
        resolved_workspace_id = workspace_id or getattr(user, "workspace_id", None) or self.workspace_id_for_user(user.id)
        claims = {
            "sub": user.id,
            "email": user.email,
            "name": user.name,
            "workspace_id": resolved_workspace_id,
            "iat": issued_at,
            "exp": issued_at + ttl,
        }
        return _encode_jwt(claims, self.signing_secret)

    def verify_session(self, token: str) -> SessionClaims:
        payload = _decode_jwt(token, self.signing_secret)
        try:
            claims = SessionClaims.model_validate(payload)
        except ValidationError as exc:
            raise SessionValidationError("session payload is invalid") from exc

        now = int(time.time())
        if claims.exp <= now:
            raise SessionValidationError("session has expired")
        if not claims.workspace_id:
            workspace_id = self.workspace_id_for_user(claims.sub)
            if not workspace_id:
                raise SessionValidationError("session user is not assigned to a workspace")
            claims.workspace_id = workspace_id
        user = next((candidate for candidate in self._load_store().users if candidate.id == claims.sub), None)
        if user is None:
            raise SessionValidationError("session user no longer exists")
        if user.password_changed_at and claims.iat <= user.password_changed_at:
            raise SessionValidationError("session was revoked after a password change")
        return claims

    def workspace_id_for_user(self, user_id: str) -> str | None:
        for user in self._load_store().users:
            if user.id == user_id:
                return user.workspace_id
        return None

    def _load_store(self) -> UserStore:
        if not self.users_path.exists():
            return UserStore()
        try:
            raw = json.loads(self.users_path.read_text(encoding="utf-8"))
            return UserStore.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise AuthError(f"failed to load user store: {exc}") from exc

    def _save_store(self, store: UserStore) -> None:
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        store.version = USER_STORE_VERSION
        serialized = json.dumps(store.model_dump(mode="json"), indent=2, sort_keys=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.users_path.parent, delete=False) as handle:
            handle.write(serialized)
            handle.write("\n")
            temp_path = Path(handle.name)
        try:
            temp_path.replace(self.users_path)
            _restrict_file_permissions(self.users_path)
        except OSError as exc:
            with suppress(OSError):
                temp_path.unlink(missing_ok=True)
            raise AuthError(f"failed to save user store: {exc}") from exc

    @staticmethod
    def _discard_expired_reset_tokens(store: UserStore, now: int) -> None:
        store.password_reset_tokens = [item for item in store.password_reset_tokens if item.expires_at > now]


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_HASH_ITERATIONS,
        _b64url_encode(salt),
        _b64url_encode(digest),
    )


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = _b64url_decode(salt_raw)
        expected = _b64url_decode(digest_raw)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _encode_jwt(payload: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = "{}.{}".format(
        _b64url_json(header),
        _b64url_json(payload),
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def _decode_jwt(token: str, secret: str) -> dict[str, Any]:
    try:
        header_raw, payload_raw, signature_raw = token.split(".", 2)
        signing_input = f"{header_raw}.{payload_raw}"
        expected_signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
        supplied_signature = _b64url_decode(signature_raw)
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise SessionValidationError("session signature is invalid")
        header = json.loads(_b64url_decode(header_raw))
        if header.get("alg") != "HS256":
            raise SessionValidationError("session algorithm is unsupported")
        payload = json.loads(_b64url_decode(payload_raw))
        if not isinstance(payload, dict):
            raise SessionValidationError("session payload is invalid")
        return payload
    except SessionValidationError:
        raise
    except Exception as exc:
        raise SessionValidationError("session token is invalid") from exc


def _b64url_json(value: dict[str, Any]) -> str:
    return _b64url_encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _resolve_signing_secret() -> str:
    secret = os.getenv("SIGNALBRIDGE_AUTH_SECRET", "").strip()
    if secret:
        return secret

    master_key = Path("master.key")
    if master_key.exists():
        return master_key.read_text(encoding="utf-8").strip()

    generated = secrets.token_urlsafe(48)
    os.environ["SIGNALBRIDGE_AUTH_SECRET"] = generated
    return generated


def session_idle_timeout_seconds() -> int:
    """Read the rolling idle-session timeout, retaining the prior env name."""

    raw = os.getenv("SIGNALBRIDGE_SESSION_IDLE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        raw = os.getenv("SIGNALBRIDGE_SESSION_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS
    try:
        return max(300, int(raw))
    except ValueError:
        return DEFAULT_SESSION_IDLE_TIMEOUT_SECONDS


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _restrict_file_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
