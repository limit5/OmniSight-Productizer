"""Security primitives — auth-adjacent helpers, lazy-loaded.

R20 Phase 0 (chat-layer):
  - prompt_hardening.INJECTION_GUARD_PRELUDE — system-prompt prelude
  - prompt_hardening.looks_like_injection(text) — heuristic detector
  - prompt_hardening.harden_user_message(text) — wrap suspicious input
  - secret_filter.redact(text) — output redaction (returns text + labels)

AS.0.10 (auth shared lib):
  - password_generator — pure-functional auto-gen password core lib
    (Random / Diceware / Pronounceable). Importable submodule, no
    runtime side effects. TS twin lives at
    `templates/_shared/password-generator/`.
"""

from .prompt_hardening import (
    INJECTION_GUARD_PRELUDE,
    harden_user_message,
    looks_like_injection,
)
from .secret_filter import redact

# Re-export the password_generator submodule by name (cheap to import —
# pure constants + functions, no IO, no DB).
from . import password_generator  # noqa: F401

__all__ = [
    "INJECTION_GUARD_PRELUDE",
    "harden_user_message",
    "looks_like_injection",
    "password_generator",
    "redact",
]
