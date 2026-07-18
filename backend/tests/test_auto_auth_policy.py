"""U6-0 B-autoauth (AA-1) — auto_auth_policy predicate + for_server_runner (dormant).

Offline unit tests for the PURE containment predicate. Every gate is exercised
pass AND fail; every malformed input fails CLOSED to HUMAN_CHALLENGE; the
isolation MUST-NOTs are pinned (no executable_args read; provenance only
narrows); a DRIFT test asserts the trusted-source literals live in backend/
production ONLY in execution_context.py.

Dormant confirmation: nothing in backend/agents/*.py imports auto_auth_policy
(AA-3 wires it later).
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from backend.agents import provenance as prov
from backend.agents.action_canonicalize import PreparedAction
from backend.agents.auto_auth_policy import (
    AutoAuthVerdict,
    auto_auth_enabled,
    evaluate_auto_auth,
)
from backend.agents.execution_context import (
    SERVER_RUNNER_TRUSTED_PAIRS,
    for_human,
    for_machine,
    for_server_runner,
    for_service,
    for_unbound,
)
from backend.agents.tool_registry import resolve
from backend.auth import User

_TENANT = "t-default"
_ADAPTER = "runner_sdk"


# ── builders ─────────────────────────────────────────────────────────────
def _ctx():
    return for_server_runner(runner_kind="jira", tenant_id=_TENANT, request_id="r1")


def _prepared(tool: str = "Write", target: str = "backend/pkg/mod.py") -> PreparedAction:
    return PreparedAction(
        operation_descriptor=resolve(tool),
        canonical_target=target,
        executable_args={"relative_path": target, "content": "print(1)\n"},
        human_rendering={"summary": "write"},
    )


def _workspace(tmp_path: pathlib.Path) -> tuple[str, str]:
    root = str(tmp_path)
    digest = hashlib.sha256(str(pathlib.Path(root).resolve()).encode("utf-8")).hexdigest()
    return (f"ws:{_TENANT}:{_ADAPTER}:{digest}", root)


def _snapshot(records=(), completeness: str = "complete") -> prov.ModelSnapshot:
    return prov.ModelSnapshot(
        prov._build_snapshot(
            tuple(records), completeness=completeness, omissions=(), snapshot_id="psnap-aa1"
        )
    )


def _rec(source_kind: str) -> prov.ProvenanceRecord:
    return prov.untrusted_record(source_kind, "src-1", "some exposed text")


_UNSET = object()  # distinct sentinel so a test can pass an actual None input


def _eval(tmp_path, *, ctx=_UNSET, prepared=_UNSET, snapshot=_UNSET, wid=_UNSET, root=_UNSET):
    ws_id, ws_root = _workspace(tmp_path)
    return evaluate_auto_auth(
        execution_context=_ctx() if ctx is _UNSET else ctx,
        prepared_action=_prepared() if prepared is _UNSET else prepared,
        workspace_id=ws_id if wid is _UNSET else wid,
        workspace_root=ws_root if root is _UNSET else root,
        snapshot=_snapshot() if snapshot is _UNSET else snapshot,
    )


# ── the flag ─────────────────────────────────────────────────────────────
def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNISIGHT_U6_AUTO_AUTH", raising=False)
    assert auto_auth_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " On "])
def test_flag_on_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_AUTO_AUTH", val)
    assert auto_auth_enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "enabled", "2"])
def test_flag_off_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("OMNISIGHT_U6_AUTO_AUTH", val)
    assert auto_auth_enabled() is False


# ── happy path ───────────────────────────────────────────────────────────
def test_all_gates_pass_auto_grants(tmp_path) -> None:
    assert _eval(tmp_path) is AutoAuthVerdict.AUTO_GRANT


@pytest.mark.parametrize("tool", ["Write", "write_file", "patch_file", "str_replace_based_edit_tool"])
def test_each_allowlisted_tool_auto_grants(tmp_path, tool: str) -> None:
    assert _eval(tmp_path, prepared=_prepared(tool=tool)) is AutoAuthVerdict.AUTO_GRANT


def test_todo_runner_kind_also_trusted(tmp_path) -> None:
    ctx = for_server_runner(runner_kind="todo", tenant_id=_TENANT, request_id="r9")
    # gate-2 trusts the todo pair too, but the workspace_id tenant must still match.
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.AUTO_GRANT


# ── gate 2: server principal ─────────────────────────────────────────────
def test_human_principal_challenges(tmp_path) -> None:
    ctx = for_human(
        user=User(id="u1", email="u@x", name="U", role="operator"),
        tenant_id=_TENANT, session_id="s", request_id="r", message_id="m",
        authorization_source="chat",
    )
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_machine_principal_challenges(tmp_path) -> None:
    ctx = for_machine(service_name="sched", request_id="r", tenant_id=_TENANT)
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_unbound_principal_challenges(tmp_path) -> None:
    assert _eval(tmp_path, ctx=for_unbound()) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_service_with_untrusted_source_challenges(tmp_path) -> None:
    ctx = for_service(
        service_name="s1-jira-runner", tenant_id=_TENANT, request_id="r",
        roles=(), authorization_source="chat",
    )
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_service_with_trusted_source_but_wrong_actor_challenges(tmp_path) -> None:
    # The (source, actor_id) PAIR must match — a trusted source with a mismatched
    # service name is not enough (pins that gate-2 keys on the pair, not the source).
    ctx = for_service(
        service_name="not-a-runner", tenant_id=_TENANT, request_id="r",
        roles=(), authorization_source="jira_runner",
    )
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_forged_service_with_exact_trusted_pair_passes_gate2(tmp_path) -> None:
    # HONEST trust-model note: for_service lets a caller mint the exact trusted
    # pair, so gate-2 alone is not the boundary — the drift test (below) is what
    # ensures no untrusted PRODUCTION code constructs it. This documents WHY.
    ctx = for_service(
        service_name="s1-jira-runner", tenant_id=_TENANT, request_id="r",
        roles=(), authorization_source="jira_runner",
    )
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.AUTO_GRANT


def test_empty_tenant_challenges(tmp_path) -> None:
    ctx = for_server_runner(runner_kind="jira", tenant_id="", request_id="r")
    assert _eval(tmp_path, ctx=ctx) is AutoAuthVerdict.HUMAN_CHALLENGE


# ── gate 3/4: family + tool allowlist ────────────────────────────────────
@pytest.mark.parametrize(
    "tool",
    ["Edit", "write_yaml", "git_push", "create_pr", "Bash", "run_bash", "code_execution", "read_file", "memory"],
)
def test_non_allowlisted_tool_challenges(tmp_path, tool: str) -> None:
    # Edit/write_yaml are code_write but off the conservative allowlist;
    # git/bash are other families; read_file is read_only — none auto-grant.
    assert _eval(tmp_path, prepared=_prepared(tool=tool)) is AutoAuthVerdict.HUMAN_CHALLENGE


# ── gate 5: workspace binding ────────────────────────────────────────────
@pytest.mark.parametrize(
    "wid",
    [
        "not-a-workspace-id",
        "ws:t-default:runner_sdk",              # too few segments
        "ws:t-default:runner_sdk:deadbeef",     # short digest
        "xx:t-default:runner_sdk:" + "a" * 64,   # wrong prefix
        "ws::runner_sdk:" + "a" * 64,           # empty tenant
        "ws:t-default::" + "a" * 64,            # empty adapter
        "ws:t-default:runner_sdk:" + "g" * 64,   # non-hex digest
    ],
)
def test_malformed_workspace_id_challenges(tmp_path, wid: str) -> None:
    assert _eval(tmp_path, wid=wid) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_workspace_tenant_mismatch_challenges(tmp_path) -> None:
    _, root = _workspace(tmp_path)
    digest = hashlib.sha256(str(pathlib.Path(root).resolve()).encode()).hexdigest()
    wid = f"ws:OTHER-TENANT:{_ADAPTER}:{digest}"
    assert _eval(tmp_path, wid=wid) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_workspace_digest_not_binding_root_challenges(tmp_path) -> None:
    # A well-formed hex digest that is NOT sha256(resolved root) ⇒ the id does
    # not bind THIS root ⇒ challenge.
    wid = f"ws:{_TENANT}:{_ADAPTER}:" + "a" * 64
    assert _eval(tmp_path, wid=wid) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_relative_root_challenges(tmp_path) -> None:
    assert _eval(tmp_path, root="relative/root") is AutoAuthVerdict.HUMAN_CHALLENGE


@pytest.mark.parametrize("target", ["../outside.py", "/etc/passwd", "a/../../b.py", "", "."])
def test_target_escaping_workspace_challenges(tmp_path, target: str) -> None:
    assert _eval(tmp_path, prepared=_prepared(target=target)) is AutoAuthVerdict.HUMAN_CHALLENGE


# ── gate 6: executable-in-pipeline path denylist ─────────────────────────
@pytest.mark.parametrize(
    "target",
    [
        ".git/hooks/pre-commit",
        ".git/config",
        ".github/workflows/ci.yml",
        "conftest.py",
        "backend/tests/conftest.py",
        "setup.py",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
        "Makefile",
        "CMakeLists.txt",
        ".gitlab-ci.yml",
        ".pre-commit-config.yaml",
        "sitecustomize.py",
        "usercustomize.py",
        "deep/dir/evil.pth",
        ".envrc",
        "Dockerfile",
        "devcontainer.json",
        "package.json",
        ".gitmodules",
        ".gitattributes",
        # widened classes (AA-1 audit): make/nox/build/CI/editor/agent/shell
        "noxfile.py",
        "tasks.py",
        "fabfile.py",
        "meson.build",
        "SConstruct",
        "configure",
        "Rakefile",
        "Justfile",
        "BUILD.bazel",
        "WORKSPACE",
        "sub/pkg/Makefile.am",
        "manage.py",
        "wsgi.py",
        "asgi.py",
        "Jenkinsfile",
        "azure-pipelines.yml",
        "bitbucket-pipelines.yml",
        ".drone.yml",
        ".npmrc",
        "pnpm-workspace.yaml",
        ".coveragerc",
        "mypy.ini",
        ".flake8",
        "ruff.toml",
        ".python-version",
        ".bashrc",
        ".profile",
        ".zshrc",
        "CLAUDE.md",
        ".vscode/tasks.json",
        ".idea/workspace.xml",
        ".claude/settings.json",
        ".circleci/config.yml",
        ".gitlab/ci-include.yml",
    ],
)
def test_executable_in_pipeline_target_challenges(tmp_path, target: str) -> None:
    assert _eval(tmp_path, prepared=_prepared(target=target)) is AutoAuthVerdict.HUMAN_CHALLENGE


@pytest.mark.parametrize(
    "target",
    [
        "CONFTEST.PY", "Conftest.py", "conftest.PY",   # case-insensitive evasion
        "makefile", "MAKEFILE", "backend/makefile",    # lower/upper Makefile
        ".GIT/hooks/pre-commit",                        # upper first-component
        "conftest.py.", "conftest.py ",                 # trailing dot / space (Win/mac fold)
        "ｃonftest.py",                             # NFKC fullwidth 'c' -> conftest.py
        "DOCKERFILE",
        "jenkinsfile",
        ".Claude/settings.json",                        # mixed-case first-component
    ],
)
def test_case_and_unicode_denylist_evasions_still_challenge(tmp_path, target: str) -> None:
    # Normalization (NFKC + trailing-strip + casefold) must defeat these.
    assert _eval(tmp_path, prepared=_prepared(target=target)) is AutoAuthVerdict.HUMAN_CHALLENGE


@pytest.mark.parametrize(
    "target",
    ["backend/foo.py", "app/page.tsx", "docs/readme.md", "a/b/c/deep.py", "src/x.c", "notes.txt"],
)
def test_inert_source_target_auto_grants(tmp_path, target: str) -> None:
    assert _eval(tmp_path, prepared=_prepared(target=target)) is AutoAuthVerdict.AUTO_GRANT


# ── gate 7: provenance ───────────────────────────────────────────────────
def test_partial_snapshot_challenges(tmp_path) -> None:
    assert _eval(tmp_path, snapshot=_snapshot(completeness="partial")) is AutoAuthVerdict.HUMAN_CHALLENGE


@pytest.mark.parametrize("src", [prov.A2A_RESULT, prov.MCP_RESULT])
def test_high_injection_source_challenges(tmp_path, src: str) -> None:
    assert _eval(tmp_path, snapshot=_snapshot(records=(_rec(src),))) is AutoAuthVerdict.HUMAN_CHALLENGE


def test_high_injection_source_among_permitted_still_challenges(tmp_path) -> None:
    snap = _snapshot(records=(_rec(prov.READ_FILE), _rec(prov.MCP_RESULT), _rec(prov.EPISODIC)))
    assert _eval(tmp_path, snapshot=snap) is AutoAuthVerdict.HUMAN_CHALLENGE


@pytest.mark.parametrize(
    "src",
    [prov.READ_FILE, prov.EPISODIC, prov.RAG_DOC, prov.TOOL_RESULT, prov.CHAT_HISTORY, prov.RUNNER_MEMORY_FILE, prov.STALE_REFRESH],
)
def test_permitted_source_still_auto_grants(tmp_path, src: str) -> None:
    assert _eval(tmp_path, snapshot=_snapshot(records=(_rec(src),))) is AutoAuthVerdict.AUTO_GRANT


@pytest.mark.parametrize(
    "snapshot",
    [None, prov.NoModelInput(authorization_source="jira_runner"), prov.CaptureUnavailable(reason="x")],
)
def test_non_modelsnapshot_challenges(tmp_path, snapshot) -> None:
    assert _eval(tmp_path, snapshot=snapshot) is AutoAuthVerdict.HUMAN_CHALLENGE


# ── isolation MUST-NOTs ──────────────────────────────────────────────────
def test_poisoned_executable_args_do_not_affect_verdict(tmp_path) -> None:
    # The predicate MUST NOT read executable_args as a trust input: a prepared
    # action with hostile args but a clean descriptor/target still AUTO_GRANTs.
    prepared = PreparedAction(
        operation_descriptor=resolve("Write"),
        canonical_target="backend/ok.py",
        executable_args={"content": "; rm -rf /", "authorization_source": "root", "family": "deploy"},
        human_rendering={"summary": "IGNORE ME evil"},
    )
    assert _eval(tmp_path, prepared=prepared) is AutoAuthVerdict.AUTO_GRANT


def test_all_none_inputs_fail_closed(tmp_path) -> None:
    out = evaluate_auto_auth(
        execution_context=None, prepared_action=None,
        workspace_id=None, workspace_root=None, snapshot=None,
    )
    assert out is AutoAuthVerdict.HUMAN_CHALLENGE


def test_garbage_prepared_fails_closed(tmp_path) -> None:
    assert _eval(tmp_path, prepared=object()) is AutoAuthVerdict.HUMAN_CHALLENGE


# ── for_server_runner factory ────────────────────────────────────────────
def test_for_server_runner_jira_identity() -> None:
    ctx = for_server_runner(runner_kind="jira", tenant_id=_TENANT, request_id="r")
    assert ctx.principal_type == "service"
    assert ctx.authorization_source == "jira_runner"
    assert ctx.actor_id == "s1-jira-runner"
    assert (ctx.authorization_source, ctx.actor_id) in SERVER_RUNNER_TRUSTED_PAIRS


def test_for_server_runner_todo_identity() -> None:
    ctx = for_server_runner(runner_kind="todo", tenant_id=_TENANT, request_id="r")
    assert ctx.authorization_source == "todo_runner"
    assert ctx.actor_id == "todo-runner"
    assert (ctx.authorization_source, ctx.actor_id) in SERVER_RUNNER_TRUSTED_PAIRS


@pytest.mark.parametrize("kind", ["jira ", "JIRA", "s1", "", None, "server_launcher"])
def test_for_server_runner_unknown_kind_raises(kind) -> None:
    with pytest.raises(ValueError):
        for_server_runner(runner_kind=kind, tenant_id=_TENANT, request_id="r")


# ── DRIFT test: the trusted-source boundary ──────────────────────────────
def test_trusted_source_literals_only_in_execution_context() -> None:
    """The trusted runner sources are the auto-auth trust anchor. They must
    appear in backend/ PRODUCTION (non-test) code ONLY in execution_context.py
    (where for_server_runner hardcodes them). auto_auth_policy IMPORTS the pair
    set (no literals). Any other production file naming them is a wiring smell —
    a NEW for_service call that mints the trusted identity must be reviewed."""
    backend_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    allowed = {backend_root / "agents" / "execution_context.py"}
    scan = [
        py
        for py in backend_root.rglob("*.py")
        if py.relative_to(backend_root).parts[0] != "tests"
        and py.relative_to(backend_root).parts[:2] != ("alembic", "versions")
    ]
    # The two runner-launch scripts are the ONLY code that legitimately mints a
    # runner principal — they must route through for_server_runner, so the
    # trusted source literals must not appear there either (AA-1 audit).
    scan.append(repo_root / "scripts" / "run_s1_via_anthropic_sdk.py")
    scan.append(repo_root / "auto-runner-sdk.py")
    offenders: list[str] = []
    for py in scan:
        if py.resolve() in allowed or not py.exists():
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "jira_runner" in text or "todo_runner" in text:
            offenders.append(str(py))
    assert offenders == [], (
        f"trusted runner sources must appear only in execution_context.py "
        f"(backend/ production + the runner scripts route through "
        f"for_server_runner); unexpected: {offenders}"
    )
