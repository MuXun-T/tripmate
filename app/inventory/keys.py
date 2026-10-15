from uuid import UUID


class KeyCodec:
    def __init__(self, prefix: str, environment: str) -> None:
        self.prefix = prefix.strip(":")
        self.environment = environment

    def _root(self, tenant_id: UUID) -> str:
        return f"{self.prefix}:v1:{self.environment}:{{{tenant_id}}}"

    def inventory(self, tenant_id: UUID, slot_id: UUID) -> str:
        return f"{self._root(tenant_id)}:inventory:{slot_id}"

    def hold(self, tenant_id: UUID, reservation_id: UUID) -> str:
        return f"{self._root(tenant_id)}:hold:{reservation_id}"

    def expiry(self, tenant_id: UUID) -> str:
        return f"{self._root(tenant_id)}:holds:expiry"

    def conversation(self, tenant_id: UUID, user_id: UUID, conversation_id: UUID) -> str:
        return f"{self._root(tenant_id)}:memory:conversation:{user_id}:{conversation_id}"

    def stream(self, tenant_id: UUID, domain: str) -> str:
        allowed = {
            "reservations",
            "inventory",
            "knowledge",
            "runs",
            "reminders",
            "trips",
            "preferences",
        }
        if domain not in allowed:
            raise ValueError("unsupported stream domain")
        return f"{self._root(tenant_id)}:stream:{domain}"

    @staticmethod
    def same_hash_tag(*keys: str) -> bool:
        tags: list[str] = []
        for key in keys:
            start = key.find("{")
            end = key.find("}", start + 1)
            if start < 0 or end < 0:
                return False
            tags.append(key[start + 1 : end])
        return len(set(tags)) <= 1
