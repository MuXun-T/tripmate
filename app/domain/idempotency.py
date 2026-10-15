import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.errors import DomainError

_KEY_RE = re.compile(r"^[A-Za-z0-9._~:+/=-]{1,200}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    tenant_id: UUID
    user_id: UUID
    route: str
    operation: str
    raw_key: str

    def key_hash(self) -> str:
        if not _KEY_RE.fullmatch(self.raw_key):
            raise DomainError(
                "INVALID_ARGUMENT", "Idempotency-Key has invalid characters or length", 400
            )
        canonical = canonical_json(
            {
                "tenant_id": str(self.tenant_id),
                "user_id": str(self.user_id),
                "route": self.route,
                "operation": self.operation,
                "key": self.raw_key,
            }
        )
        return sha256_hex(canonical)


def request_hash(request: Any) -> str:
    return sha256_hex(canonical_json(request))
