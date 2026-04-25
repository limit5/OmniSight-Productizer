"""R20 Phase 0 — chat-layer security primitives.

Module surface:
  - prompt_hardening.INJECTION_GUARD_PRELUDE — system-prompt prelude
  - prompt_hardening.looks_like_injection(text) — heuristic detector
  - prompt_hardening.harden_user_message(text) — wrap suspicious input
  - secret_filter.redact(text) — output redaction (returns text + labels)
"""

from .prompt_hardening import (
    INJECTION_GUARD_PRELUDE,
    harden_user_message,
    looks_like_injection,
)
from .secret_filter import redact

__all__ = [
    "INJECTION_GUARD_PRELUDE",
    "harden_user_message",
    "looks_like_injection",
    "redact",
]
