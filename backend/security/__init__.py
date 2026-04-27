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

AS.1.1 (auth shared lib):
  - oauth_client — protocol primitives (PKCE / state / nonce /
    refresh rotation / auto-refresh middleware). Provider-agnostic;
    vendor catalogs land in AS.1.3. TS twin will live at
    `templates/_shared/oauth-client/` (AS.1.2).
"""

from .prompt_hardening import (
    INJECTION_GUARD_PRELUDE,
    harden_user_message,
    looks_like_injection,
)
from .secret_filter import redact

# Re-export pure submodules by name (cheap — constants + functions, no IO).
from . import oauth_client  # noqa: F401
from . import password_generator  # noqa: F401

__all__ = [
    "INJECTION_GUARD_PRELUDE",
    "harden_user_message",
    "looks_like_injection",
    "oauth_client",
    "password_generator",
    "redact",
]
