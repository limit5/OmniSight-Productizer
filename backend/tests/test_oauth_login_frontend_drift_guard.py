"""FX2.D9.7.12 — frontend OAuth catalog drift guard.

The login page owns the frontend button list in
``lib/auth/oauth-providers.ts`` while the backend login handler owns
``SUPPORTED_PROVIDERS``. This test keeps the frontend list a subset of
the backend set so a stale or speculative frontend button cannot route
users to a provider the backend rejects before the configured-state UI
has a chance to help.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_OAUTH_PROVIDERS_TS = REPO_ROOT / "lib/auth/oauth-providers.ts"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

olh = importlib.import_module("backend.security.oauth_login_handler")


def _frontend_oauth_provider_ids() -> set[str]:
    source = FRONTEND_OAUTH_PROVIDERS_TS.read_text(encoding="utf-8")
    match = re.search(
        r"export const OAUTH_PROVIDER_IDS = \[(?P<body>.*?)\] as const",
        source,
        flags=re.S,
    )
    assert match, "could not locate OAUTH_PROVIDER_IDS in frontend catalog"
    return set(re.findall(r'"([a-z][a-z0-9_-]*)"', match.group("body")))


def test_frontend_oauth_provider_ids_are_backend_supported_subset() -> None:
    frontend = _frontend_oauth_provider_ids()
    backend = set(olh.SUPPORTED_PROVIDERS)
    assert frontend, "frontend OAuth provider list should not be empty"
    assert frontend <= backend, (
        "frontend OAuth provider drift: "
        f"unsupported={sorted(frontend - backend)} "
        f"backend_supported={sorted(backend)}"
    )
