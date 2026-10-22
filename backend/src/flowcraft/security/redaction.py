"""Automatic secret redaction for stored run data and logs.

Two mechanisms, applied before anything is persisted or logged:
1. Key-name heuristics (authorization, api_key, token, secret, password...).
2. Value-pattern scrubbing (known API key shapes) so secrets echoed inside
   free-text model output or error messages do not leak into history.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|auth|token|secret|password|passwd|credential|"
    r"access[_-]?key|private[_-]?key|cookie|session)",
    re.IGNORECASE,
)

# Well-known credential shapes. Deliberately conservative (long + prefixed).
VALUE_PATTERNS = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),                # Anthropic-style
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                    # OpenAI-style
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                     # GitHub PAT
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),             # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                         # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                   # Google API key
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}", re.IGNORECASE),
]

MAX_TEXT_CHARS = 4000  # truncate huge payloads stored in history


def scrub_text(text: str) -> str:
    out = text
    for pat in VALUE_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


def redact(value: Any, *, key_hint: str | None = None) -> Any:
    """Recursively redact sensitive keys and scrub string values."""
    if key_hint and SENSITIVE_KEY_RE.search(key_hint):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        s = scrub_text(value)
        if len(s) > MAX_TEXT_CHARS:
            s = s[:MAX_TEXT_CHARS] + f"...[truncated {len(s) - MAX_TEXT_CHARS} chars]"
        return s
    return value
