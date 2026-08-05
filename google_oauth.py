"""
Google OpenID Connect helpers for SignalBridge.

This module builds the authorization redirect, verifies the callback, and
returns a normalized Google identity profile that can be mapped to a workspace
membership in the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any
from urllib import error, parse, request

from authlib.jose import JsonWebKey, jwt


GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
GOOGLE_SCOPES = "openid email profile"
STATE_COOKIE_NAME = "signalbridge_oauth_state"
STATE_TTL_SECONDS = 600


class GoogleOAuthError(RuntimeError):
    """Raised when Google sign-in cannot be completed."""


@dataclass(slots=True)
class GoogleIdentityProfile:
    subject: str
    email: str
    email_verified: bool
    name: str
    given_name: str | None = None
    family_name: str | None = None
    picture: str | None = None


@dataclass(slots=True)
class OAuthStatePayload:
    state: str
    nonce: str
    code_verifier: str
    next_path: str
    created_at: int

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OAuthStatePayload":
        return cls(
            state=str(payload.get("state", "")),
            nonce=str(payload.get("nonce", "")),
            code_verifier=str(payload.get("code_verifier", "")),
            next_path=str(payload.get("next_path", "/dashboard")),
            created_at=int(payload.get("created_at", 0)),
        )


class GoogleOAuthService:
    def __init__(self) -> None:
        self.client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        self.redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
        self.public_base_url = os.getenv("SIGNALBRIDGE_PUBLIC_BASE_URL", "").strip().rstrip("/")
        self.allowed_emails = _parse_csv_env("GOOGLE_OAUTH_ALLOWED_EMAILS")
        self.allowed_domains = _parse_csv_env("GOOGLE_OAUTH_ALLOWED_DOMAINS")

    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def build_authorize_url(self, next_path: str) -> tuple[str, str]:
        self._ensure_enabled()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(64)
        state_payload = OAuthStatePayload(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            next_path=_normalize_next_path(next_path),
            created_at=int(time.time()),
        )
        state_cookie = _encode_state_payload(state_payload)
        auth_url = self._authorization_endpoint()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
            "access_type": "offline",
            "include_granted_scopes": "true",
        }
        return f"{auth_url}?{parse.urlencode(params)}", state_cookie

    def parse_state_cookie(self, state_cookie: str) -> OAuthStatePayload:
        payload = _decode_state_payload(state_cookie)
        if int(time.time()) - payload.created_at > STATE_TTL_SECONDS:
            raise GoogleOAuthError("Google sign-in request expired; try again")
        return payload

    async def exchange_code(self, code: str, state_cookie: str) -> GoogleIdentityProfile:
        self._ensure_enabled()
        state = self.parse_state_cookie(state_cookie)
        token_response = await _post_form(
            self._token_endpoint(),
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
                "code_verifier": state.code_verifier,
            },
        )

        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise GoogleOAuthError("Google sign-in did not return an identity token")

        claims = self._verify_id_token(id_token, state.nonce)
        email = str(claims.get("email") or "").strip().lower()
        if not email:
            raise GoogleOAuthError("Google account email is missing")
        if not bool(claims.get("email_verified")):
            raise GoogleOAuthError("Google account email is not verified")
        self._enforce_allowlist(email)

        display_name = str(claims.get("name") or claims.get("given_name") or email.split("@")[0]).strip()
        return GoogleIdentityProfile(
            subject=str(claims["sub"]),
            email=email,
            email_verified=True,
            name=display_name,
            given_name=_optional_str(claims.get("given_name")),
            family_name=_optional_str(claims.get("family_name")),
            picture=_optional_str(claims.get("picture")),
        )

    def oauth_error_message(self) -> str:
        if self.enabled():
            return ""
        return "Google OAuth is not configured on the backend"

    def next_path_from_state(self, state_cookie: str) -> str:
        return self.parse_state_cookie(state_cookie).next_path

    def _ensure_enabled(self) -> None:
        if not self.enabled():
            raise GoogleOAuthError(self.oauth_error_message())

    def _authorization_endpoint(self) -> str:
        return str(self._google_configuration()["authorization_endpoint"])

    def _token_endpoint(self) -> str:
        return str(self._google_configuration()["token_endpoint"])

    def _jwks_uri(self) -> str:
        return str(self._google_configuration()["jwks_uri"])

    def _verify_id_token(self, id_token: str, expected_nonce: str) -> dict[str, Any]:
        key_set = JsonWebKey.import_key_set(self._google_jwks())
        claims = jwt.decode(id_token, key_set)
        claims.validate()
        audience = claims.get("aud")
        if isinstance(audience, str):
            audiences = {audience}
        else:
            audiences = {str(item) for item in audience or []}
        if self.client_id not in audiences:
            raise GoogleOAuthError("Google identity token audience is invalid")
        issuer = str(claims.get("iss") or "")
        if issuer not in GOOGLE_ISSUERS:
            raise GoogleOAuthError("Google identity token issuer is invalid")
        if str(claims.get("nonce") or "") != expected_nonce:
            raise GoogleOAuthError("Google identity token nonce is invalid")
        return dict(claims)

    @lru_cache(maxsize=1)
    def _google_configuration(self) -> dict[str, Any]:
        return _fetch_json("https://accounts.google.com/.well-known/openid-configuration")

    @lru_cache(maxsize=1)
    def _google_jwks(self) -> dict[str, Any]:
        return _fetch_json(self._jwks_uri())

    def _enforce_allowlist(self, email: str) -> None:
        if self.allowed_emails and email not in self.allowed_emails:
            raise GoogleOAuthError("this Google account is not allowed to sign in")
        if self.allowed_domains:
            domain = email.split("@")[-1]
            if domain not in self.allowed_domains:
                raise GoogleOAuthError("this Google account domain is not allowed to sign in")


async def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    payload = parse.urlencode(data).encode("utf-8")
    request_obj = request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    def _send() -> dict[str, Any]:
        try:
            with request.urlopen(request_obj, timeout=15) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GoogleOAuthError(f"Google token exchange failed: {detail}") from exc
        except Exception as exc:
            raise GoogleOAuthError("Google token exchange failed") from exc

    from asyncio import to_thread

    return await to_thread(_send)


def _fetch_json(url: str) -> dict[str, Any]:
    try:
        with request.urlopen(url, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise GoogleOAuthError(f"failed to load Google OAuth metadata from {url}") from exc


def _encode_state_payload(payload: OAuthStatePayload) -> str:
    serialized = json.dumps(asdict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_state_secret().encode("utf-8"), serialized, hashlib.sha256).digest()
    return f"{_b64(serialized)}.{_b64(signature)}"


def _decode_state_payload(token: str) -> OAuthStatePayload:
    try:
        payload_raw, signature_raw = token.split(".", 1)
        payload_bytes = _b64decode(payload_raw)
        expected_signature = hmac.new(_state_secret().encode("utf-8"), payload_bytes, hashlib.sha256).digest()
        supplied_signature = _b64decode(signature_raw)
        if not hmac.compare_digest(expected_signature, supplied_signature):
            raise GoogleOAuthError("Google sign-in state is invalid")
        data = json.loads(payload_bytes)
        return OAuthStatePayload.from_dict(data)
    except GoogleOAuthError:
        raise
    except Exception as exc:
        raise GoogleOAuthError("Google sign-in state is invalid") from exc


def _state_secret() -> str:
    secret = os.getenv("SIGNALBRIDGE_OAUTH_STATE_SECRET", "").strip()
    if secret:
        return secret
    secret = os.getenv("SIGNALBRIDGE_AUTH_SECRET", "").strip()
    if secret:
        return secret
    return "signalbridge-oauth-dev-secret"


def _normalize_next_path(next_path: str) -> str:
    candidate = (next_path or "/dashboard").strip()
    if not candidate.startswith("/"):
        return "/dashboard"
    if candidate.startswith("//"):
        return "/dashboard"
    return candidate


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64(digest)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _parse_csv_env(name: str) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
