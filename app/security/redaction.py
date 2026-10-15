import hashlib
import re

_REDACTIONS = (
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[email]"),
    (re.compile(r"(?<!\w)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\w)"), "[phone]"),
    (re.compile(r"(?i)\b(?:bearer\s+)?[A-Za-z0-9_-]{32,}\b"), "[credential]"),
)


def redact_message(value: str, *, limit: int = 4000) -> str:
    redacted = value[:limit]
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
