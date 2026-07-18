"""OP-2602 — U6-0 P-ID-C: runner ExecutionContext population (dormant).

Populate-only: the two launcher entry points construct a SERVER-DERIVED
service principal and set it on the client; nothing consumes it until
T7b. These tests assert the constructed context carries only fixed
server constants (never model/ticket-body text), pins the internal
``omnisight-self`` tenant, and mints a fresh request_id per top-level
invocation / per item.

The auto-runner script (``auto-runner-sdk.py``) cannot be imported by an
import statement (hyphenated name), so its identity contract is asserted
by mirroring the exact ``for_service`` call of its set-site plus a
source-level scan for the pinned constants.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

from backend.agents import runner_tenant
from backend.agents.execution_context import ExecutionContext, for_server_runner

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_s1_launcher() -> Any:
    name = "s1_launcher_pidc_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        REPO_ROOT / "scripts" / "run_s1_via_anthropic_sdk.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── run_s1 helper: server-derived fields ───


def test_run_s1_helper_builds_server_derived_service_principal() -> None:
    launcher = _load_s1_launcher()
    ctx = launcher._build_runner_execution_context(
        "OP-1234", request_id_factory=lambda: "fixed",
    )
    assert isinstance(ctx, ExecutionContext)
    assert ctx.principal_type == "service"
    assert ctx.actor_id == "s1-jira-runner"
    assert ctx.tenant_id == "omnisight-self"
    assert ctx.request_id == "OP-1234:fixed"
    assert ctx.authorization_source == "jira_runner"
    assert ctx.roles == ()
    assert ctx.session_id is None
    assert ctx.message_id is None


def test_run_s1_service_name_is_fixed_module_constant() -> None:
    # The actor is a FIXED server constant, not a model-configurable value.
    launcher = _load_s1_launcher()
    assert launcher._RUNNER_SERVICE_NAME == "s1-jira-runner"
    ctx = launcher._build_runner_execution_context("OP-9")
    assert ctx.actor_id == launcher._RUNNER_SERVICE_NAME


def test_run_s1_tenant_is_omnisight_self_constant_not_t_default() -> None:
    # Regression against a t-default reintroduction: internal runner work
    # must pin the self tenant, never inherit a customer-tenant default.
    launcher = _load_s1_launcher()
    ctx = launcher._build_runner_execution_context("OP-1")
    assert ctx.tenant_id == runner_tenant.OMNISIGHT_SELF_TENANT
    assert ctx.tenant_id != "t-default"


def test_run_s1_request_id_fresh_per_invocation() -> None:
    # Unrelated attempts must not share a grant-binding dimension: the
    # default factory mints a fresh uuid4 per top-level invocation, and
    # the id is NOT the bare stable ticket key.
    launcher = _load_s1_launcher()
    a = launcher._build_runner_execution_context("OP-1234")
    b = launcher._build_runner_execution_context("OP-1234")
    assert a.request_id != b.request_id
    assert a.request_id != "OP-1234"
    assert a.request_id.startswith("OP-1234:")


def test_run_s1_dry_run_client_accepts_execution_context() -> None:
    launcher = _load_s1_launcher()
    client = launcher._DryRunClient()
    assert client.execution_context is None
    ctx = launcher._build_runner_execution_context("OP-2")
    client.execution_context = ctx
    assert client.execution_context is ctx


# ─── auto-runner (todo-runner) identity contract ───


def _build_todo_runner_context() -> ExecutionContext:
    # Mirrors the run_one_item set-site in auto-runner-sdk.py exactly
    # (the script is not importable — hyphenated module name). AA-1: the
    # set-site now uses the factory-owned for_server_runner("todo").
    return for_server_runner(
        runner_kind="todo",
        tenant_id=runner_tenant.OMNISIGHT_SELF_TENANT,
        request_id=uuid.uuid4().hex,
        roles=(),
    )


def test_todo_runner_identity_is_service_self_tenant() -> None:
    ctx = _build_todo_runner_context()
    assert ctx.principal_type == "service"
    assert ctx.actor_id == "todo-runner"
    assert ctx.tenant_id == "omnisight-self"
    assert ctx.tenant_id == runner_tenant.OMNISIGHT_SELF_TENANT
    assert ctx.authorization_source == "todo_runner"
    assert ctx.roles == ()


def test_todo_runner_each_item_gets_distinct_request_id() -> None:
    # Per ITEM, not process-wide: two constructions ⇒ two ids.
    a = _build_todo_runner_context()
    b = _build_todo_runner_context()
    assert a.request_id != b.request_id


def test_auto_runner_source_pins_constants_at_set_site() -> None:
    # The script cannot be imported; assert the source carries the pinned
    # server constants so a drift (e.g. t-default, model-derived actor)
    # cannot land silently.
    src = (REPO_ROOT / "auto-runner-sdk.py").read_text(encoding="utf-8")
    # AA-1: the trusted (source, actor_id) pair is now FACTORY-OWNED via
    # for_server_runner("todo") — a stronger pin than a call-site string, so a
    # drift (t-default, model-derived actor, smuggled source) cannot land.
    assert '_RUNNER_SERVICE_NAME = "todo-runner"' in src
    assert "for_server_runner(" in src
    assert 'runner_kind="todo"' in src
    assert "tenant_id=runner_tenant.OMNISIGHT_SELF_TENANT" in src
    assert "request_id=uuid.uuid4().hex" in src
    # The trusted source string is no longer a free literal at the set-site.
    assert 'authorization_source="todo_runner"' not in src
    assert '"t-default"' not in src
