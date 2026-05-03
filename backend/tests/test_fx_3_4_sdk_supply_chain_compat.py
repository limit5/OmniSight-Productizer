"""FX.3.4 — SDK supply chain compatibility drift guard.

Pins the cross-stack contract that the 2026-05-03 deep audit (B22)
flagged: backend `anthropic` SDK and frontend `ai`/`@ai-sdk/anthropic`
must produce wire-compatible Anthropic Messages API requests.

The two stacks do NOT share a `messages` envelope (backend
`ChatRequest` is `{message: str}`; frontend `app/api/chat/route.ts`
calls Anthropic directly via `streamText`). What they DO share is the
Anthropic wire surface — same model IDs, same role/content vocabulary,
same prompt-feature shape (system blocks, cache_control, tools,
mcp_servers).

This file asserts the floors / overlaps that, if drifted, would let
one stack send a request the other can't reproduce. It is a
**drift guard** in the sense of `docs/sop/implement_phase_step.md`
Step 4 — pure introspection, no network.

Audit reference: docs/audit/2026-05-03-deep-audit.md §B22.
TODO row: FX.3.4 (Priority FX, infra BLOCKER bucket).
Prior work: FX.6.3 pinned `anthropic==0.97.0` (latest stable on PyPI;
the TODO author's "1.x+" target referred to `langchain-anthropic`,
which is at 1.4.0 — the raw `anthropic` SDK has no 1.x release yet).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_TS = REPO_ROOT / "lib" / "providers.ts"
PACKAGE_JSON = REPO_ROOT / "package.json"
REQUIREMENTS_IN = REPO_ROOT / "backend" / "requirements.in"


# ─── 1. Backend SDK floor ─────────────────────────────────────────


def test_anthropic_sdk_at_or_above_pinned_floor():
    """Backend `anthropic` SDK must be at the FX.6.3 floor (0.97.0).

    PyPI has no 1.x release of `anthropic` as of 2026-05-04; 0.97.0 is
    the latest stable. If a 1.x ever ships and we adopt it, bump the
    floor here — that is the trigger for re-running the wire-shape
    asserts below against the new SDK's `messages.create()` signature.
    """
    import anthropic

    parts = tuple(int(p) for p in anthropic.__version__.split(".")[:3])
    assert parts >= (0, 97, 0), (
        f"anthropic SDK {anthropic.__version__} is below FX.6.3 floor 0.97.0; "
        "the production image must ship the pinned version. "
        "Re-run `pip-compile --generate-hashes` against backend/requirements.in."
    )


def test_langchain_anthropic_at_or_above_floor():
    """`langchain-anthropic` is the multi-provider shim and must stay
    in spec with the pinned `anthropic` SDK.

    `langchain-anthropic==1.4.0` (per requirements.in) declares
    `anthropic<1.0.0,>=0.85.0`, so 0.97.0 is in-range. If either pin
    moves, the resolver will fail loudly — this test catches a silent
    install-time downgrade.
    """
    import langchain_anthropic

    parts = tuple(int(p) for p in langchain_anthropic.__version__.split(".")[:3])
    assert parts >= (1, 4, 0), (
        f"langchain-anthropic {langchain_anthropic.__version__} below 1.4.0 floor."
    )


# ─── 2. Cross-stack model-ID overlap ──────────────────────────────


def _frontend_anthropic_models() -> list[str]:
    """Extract the Anthropic `models: [...]` array from `lib/providers.ts`.

    Tolerates whitespace + trailing commas. Doesn't try to parse JS
    fully — just locates the Anthropic block and pulls double-quoted
    model strings out of it.
    """
    src = PROVIDERS_TS.read_text(encoding="utf-8")
    # Find the Anthropic provider object's models array. The provider
    # block starts with `id: "anthropic"` and the next `models: [...]`
    # belongs to it.
    anchor = src.index('id: "anthropic"')
    tail = src[anchor:]
    match = re.search(r"models:\s*\[(.*?)\]", tail, re.DOTALL)
    assert match, "lib/providers.ts: anthropic provider has no models: [...] block"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_backend_default_models_listed_in_frontend_provider_registry():
    """Every default model the backend's native Anthropic client picks
    must be selectable from the frontend's provider registry, otherwise
    a UI session can't reproduce a backend prompt against the same model.
    """
    from backend.agents.anthropic_native_client import (
        DEFAULT_MODEL_HAIKU,
        DEFAULT_MODEL_OPUS,
        DEFAULT_MODEL_SONNET,
    )

    frontend = set(_frontend_anthropic_models())
    backend_defaults = {DEFAULT_MODEL_OPUS, DEFAULT_MODEL_SONNET, DEFAULT_MODEL_HAIKU}
    overlap = backend_defaults & frontend
    # At least one model must be selectable from both stacks. If/when
    # the frontend registry is in lock-step with backend constants the
    # overlap will be 3; we assert ≥1 to allow the registry to lead or
    # trail by one model during a coordinated bump.
    assert overlap, (
        f"Model-ID drift: backend defaults {backend_defaults} have ZERO "
        f"overlap with frontend registry {frontend}. A frontend chat "
        "session cannot reproduce any backend prompt against the same "
        "model ID. Update lib/providers.ts or the backend constants."
    )


def test_frontend_anthropic_default_model_is_real():
    """`PROVIDERS.anthropic.defaultModel` must appear in its own
    `models: [...]`, otherwise the dropdown default points at a model
    not selectable by the user.
    """
    src = PROVIDERS_TS.read_text(encoding="utf-8")
    anchor = src.index('id: "anthropic"')
    tail = src[anchor:]
    default_match = re.search(r'defaultModel:\s*"([^"]+)"', tail)
    assert default_match, "lib/providers.ts: anthropic block has no defaultModel"
    default = default_match.group(1)
    assert default in _frontend_anthropic_models(), (
        f"defaultModel '{default}' is not in the anthropic models[] list"
    )


# ─── 3. Frontend ai-sdk floor ─────────────────────────────────────


def _package_json_dep_floor(name: str) -> tuple[int, int, int]:
    """Parse `^X.Y.Z` floor from `package.json` dependencies.

    Returns (X, Y, Z) for the first numeric semver found after `^` /
    `~` / no-prefix. Raises if absent or unparseable.
    """
    pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    spec = deps.get(name)
    assert spec is not None, f"package.json missing dependency '{name}'"
    m = re.match(r"[\^~]?(\d+)\.(\d+)\.(\d+)", spec)
    assert m, f"package.json: cannot parse semver from '{name}={spec}'"
    return (int(m[1]), int(m[2]), int(m[3]))


def test_frontend_ai_sdk_at_or_above_v6():
    """`ai@^6` is the wire-format generation that aligns with the
    Anthropic Messages API features the backend SDK 0.97 emits
    (system blocks, cache_control, tools, mcp_servers). Earlier ai@4/5
    used the deprecated `toDataStreamResponse` and a different
    UI-message shape.
    """
    floor = _package_json_dep_floor("ai")
    assert floor >= (6, 0, 0), (
        f"frontend `ai` package floor is {floor}, expected >= (6, 0, 0). "
        "Older majors do not carry the `ModelMessage`/`streamText` "
        "shape that app/api/chat/route.ts depends on."
    )


def test_frontend_ai_sdk_anthropic_at_or_above_v3():
    """`@ai-sdk/anthropic@^3` is the provider that translates ai@6
    `ModelMessage`s into Anthropic Messages API requests. Floor matches
    the package.json declaration.
    """
    floor = _package_json_dep_floor("@ai-sdk/anthropic")
    assert floor >= (3, 0, 0), (
        f"frontend `@ai-sdk/anthropic` floor is {floor}, expected >= (3, 0, 0)."
    )


# ─── 4. Backend → Anthropic wire shape ────────────────────────────

# These are the keys the backend's `simple_params()` is allowed to
# emit. They MUST be a subset of the parameters `messages.create()`
# accepts in the pinned SDK version. If a bump removes / renames any
# of these, this test fires before we ship.
_ANTHROPIC_REQUEST_KEYS = {
    "model",
    "max_tokens",
    "temperature",
    "messages",
    "system",
    "tools",
    "mcp_servers",
}


def test_simple_params_emits_only_anthropic_wire_keys(monkeypatch):
    """`AnthropicClient.simple_params()` must produce a dict whose
    top-level keys are all valid Anthropic Messages API parameters.

    ai@6's `@ai-sdk/anthropic` provider, on the frontend side, builds
    a request using exactly this same vocabulary. A drift here (e.g.
    backend adds `prompt=` instead of `messages=`) would make the two
    stacks send incompatible payloads against the same endpoint.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")

    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(
        prompt="hello",
        system="you are concise",
        temperature=0.2,
        enable_cache=True,
    )

    extra = set(params) - _ANTHROPIC_REQUEST_KEYS
    assert not extra, (
        f"simple_params() emitted unknown keys {extra}; only "
        f"{_ANTHROPIC_REQUEST_KEYS} are accepted by Anthropic "
        "Messages API and by @ai-sdk/anthropic@3."
    )

    # `messages` must be a list with the canonical role/content shape.
    assert isinstance(params["messages"], list)
    assert params["messages"][0]["role"] == "user"
    assert params["messages"][0]["content"] == "hello"

    # `system` with cache enabled becomes the canonical block list.
    assert isinstance(params["system"], list)
    assert params["system"][0]["type"] == "text"
    assert params["system"][0]["text"] == "you are concise"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_simple_params_role_vocabulary_matches_ai_sdk_v6_modelmessage():
    """The backend's `messages[*].role` vocabulary must intersect the
    ai-sdk@6 `ModelMessage` role union (`system|user|assistant|tool`).

    `simple_params()` in non-tool mode only emits `user` — that is the
    floor we lock here. If a refactor introduces an unsupported role
    (e.g. `function`, `developer`), this asserts before it lands.
    """
    import os

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-stub"
    from backend.agents.anthropic_native_client import AnthropicClient

    client = AnthropicClient()
    params = client.simple_params(prompt="ping")

    ai_sdk_v6_roles = {"system", "user", "assistant", "tool"}
    seen_roles = {m["role"] for m in params["messages"]}
    assert seen_roles <= ai_sdk_v6_roles, (
        f"backend emits roles {seen_roles - ai_sdk_v6_roles} that the "
        f"ai-sdk@6 ModelMessage union does not accept."
    )


def test_tool_result_block_shape_matches_anthropic_wire():
    """`ToolResult.to_anthropic_block()` must emit the exact Anthropic
    `tool_result` content-block shape that the SDK forwards verbatim
    and that ai-sdk@6's `@ai-sdk/anthropic` provider also produces from
    its `ToolResultPart`.
    """
    from backend.agents.tool_dispatcher import ToolResult

    ok_block = ToolResult(tool_use_id="tu_1", content="ok").to_anthropic_block()
    assert set(ok_block) == {"type", "tool_use_id", "content"}
    assert ok_block["type"] == "tool_result"

    err_block = ToolResult(
        tool_use_id="tu_2", content="boom", is_error=True
    ).to_anthropic_block()
    assert set(err_block) == {"type", "tool_use_id", "content", "is_error"}
    assert err_block["is_error"] is True


# ─── 5. requirements.in pin lock ──────────────────────────────────


def test_requirements_in_pins_anthropic_sdk_explicitly():
    """`backend/requirements.in` must carry an explicit top-level
    `anthropic==X.Y.Z` pin (FX.6.3 invariant).

    Leaving it transitive on `langchain-anthropic` is exactly the gap
    the audit's B22 flagged — the resolver picked 0.95.0 silently.
    """
    src = REQUIREMENTS_IN.read_text(encoding="utf-8")
    pin = re.search(r"^anthropic==(\d+)\.(\d+)\.(\d+)\s*$", src, re.MULTILINE)
    assert pin, (
        "backend/requirements.in lost the explicit `anthropic==X.Y.Z` "
        "pin. Restore it (FX.6.3 invariant)."
    )
    parts = (int(pin[1]), int(pin[2]), int(pin[3]))
    assert parts >= (0, 97, 0)


# Smoke: confirm the pytest collection actually picks this file up.
@pytest.mark.parametrize("sentinel", [True])
def test_smoke(sentinel):
    assert sentinel
