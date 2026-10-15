import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from app.domain.errors import DomainError

ISSUER = "tripmate"
AUDIENCE = "tripmate-api"
_HEADER = {"alg": "HS256", "typ": "JWT"}


def issue_bearer_token(
    tenant_id: UUID,
    user_id: UUID,
    secret: str,
    *,
    expires_in_seconds: int = 3600,
    now: datetime | None = None,
) -> str:
    instant = int((now or datetime.now(UTC)).timestamp())
    header = _encode(_canonical_json(_HEADER))
    payload = _encode(
        _canonical_json(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "tenant_id": str(tenant_id),
                "user_id": str(user_id),
                "iat": instant,
                "nbf": instant,
                "exp": instant + expires_in_seconds,
            }
        )
    )
    signing_input = f"{header}.{payload}"
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return f"{signing_input}.{_encode(signature.digest())}"


def verify_bearer_token(token: str, secret: str, *, now: datetime | None = None) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise _unauthorized()
    header_part, payload_part, signature_part = parts
    signing_input = f"{header_part}.{payload_part}"
    expected = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    if not hmac.compare_digest(_encode(expected.digest()), signature_part):
        raise _unauthorized()
    try:
        header = json.loads(_decode(header_part))
        payload = cast(dict[str, Any], json.loads(_decode(payload_part)))
        if header != _HEADER:
            raise _unauthorized()
        instant = int((now or datetime.now(UTC)).timestamp())
        if payload.get("iss") != ISSUER or payload.get("aud") != AUDIENCE:
            raise _unauthorized()
        issued_at = int(payload["iat"])
        not_before = int(payload["nbf"])
        expires = int(payload["exp"])
        if issued_at > instant + 30 or not_before > instant + 30 or expires <= instant:
            raise _unauthorized()
        if expires <= issued_at:
            raise _unauthorized()
        payload["tenant_id"] = UUID(payload["tenant_id"])
        payload["user_id"] = UUID(payload["user_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _unauthorized() from exc
    return payload


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _unauthorized() -> DomainError:
    return DomainError("UNAUTHORIZED", "A valid bearer token is required", 401)
