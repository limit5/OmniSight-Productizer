"""U6-0 tool-metadata registry + resolver (dormant).

Authoritative per-tool classification for every model-callable surface —
LangChain ``backend.agents.tools.TOOL_MAP`` AND the SDK runner
``RUNNER_TOOLS`` (auto-runner-sdk.py) plus its dispatcher aliases
(``str_replace_based_edit_tool``/``bash``/``WebFetch``/``ToolSearch``).
The U6-0 kernel (T6, later) resolves every tool call against THIS table
— never trusting an adapter-supplied descriptor — and treats an
unlisted tool as protected/DENY.

Frozen design §2.B facts encoded here:
  * Classification is PER-TOOL, not per-tool-set. The tool-sets in
    ``backend/agents/tools.py`` (FILE_TOOLS, GIT_TOOLS, REVIEW_TOOLS,
    DEPLOY_TOOLS, EPISODIC_TOOLS, TASK_TOOLS) are MIXED — e.g.
    FILE_TOOLS carries both ``read_file`` (read) and ``write_file``
    (write). Each key below is classified on its own.
  * Keys are the tool's REGISTERED ``.name`` (the ``TOOL_MAP`` key),
    NOT the Python function name. The only current divergence: the
    function ``web_search`` is registered ``@tool("WebSearch")`` — its
    key is ``"WebSearch"``.
  * FAIL-CLOSED: ``run_bash``/``Bash``/``bash`` and any single-name
    multi-operation tool are ``mutating`` conservatively. Per-operation
    refinement (e.g. splitting bash into read vs write) is deferred to
    T6 with a prepared-action canonicalizer.
  * UNKNOWN ⇒ DENY: :func:`resolve` never returns a permissive default
    for an unlisted tool. It synthesises an
    ``OperationDescriptor(effect="mutating", family="__unknown_deny__")``
    so the kernel's default path is protected.

This module is ADDITIVE and DORMANT. Nothing consumes it in this
ticket. NB: an unrelated ``external_tool_registry.py`` exists — that
is the MCP-server registry (integration-type + license-tier metadata),
a different concept. This module is the per-tool AUTHORIZATION metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Effect = Literal["read_only", "mutating"]


@dataclass(frozen=True)
class OperationDescriptor:
    tool_name: str
    effect: Effect
    family: str


# ── Family strings (frozen design §2.B) ────────────────────────────────
# The kernel keys authorization decisions on ``family`` — new tools MUST
# reuse one of these labels (or add one deliberately, extending the
# kernel's policy table in the same change). ``external_comms`` covers
# outbound-message tools (Slack today; email / non-Gerrit PR comment-body
# posts as they land). ``skill_exec`` is a HIGH-RISK family reserved for
# ``*.skill`` executable-scoped file runs — the ``skills_loader`` runs
# only manifest-pinned, hash-verified skills as subprocesses (P-SKILL
# containment; still with the full parent environment), so they must NOT
# share the bare ``delegation`` family with sub-agent spawns (an enforce
# flip on ``code_write`` / ``deploy`` would otherwise be silently
# bypassable through a skill). The classification here is what the
# kernel will consult.
_READ_ONLY = "read_only"
_CODE_WRITE = "code_write"
_GERRIT_WRITE = "gerrit_write"
_TICKET_WRITE = "ticket_write"
_TASK_WRITE = "task_write"
_DEPLOY = "deploy"
_ARTIFACT_WRITE = "artifact_write"
_MEMORY_WRITE = "memory_write"
_DANGEROUS_PROPOSE = "dangerous_propose"
_DELEGATION = "delegation"
_EXTERNAL_COMMS = "external_comms"  # SlackPostMessage; future email / PR comment posts
_SKILL_EXEC = "skill_exec"  # manifest-pinned subprocess exec via *.skill files (see docstring)

_UNKNOWN_DENY = "__unknown_deny__"


def _op(name: str, effect: Effect, family: str) -> OperationDescriptor:
    return OperationDescriptor(tool_name=name, effect=effect, family=family)


# ── TOOL_METADATA ──────────────────────────────────────────────────────
# Every current model-callable tool, keyed by its REGISTERED name.
# Ordering below mirrors the tool-set groupings in
# ``backend/agents/tools.py`` (LangChain) followed by the SDK runner
# tools + dispatcher aliases. The parity test asserts that the LangChain
# ``TOOL_MAP`` keys are a subset of this table — a new @tool that isn't
# classified here MUST make the test fail loudly (the anti-drift guard).
TOOL_METADATA: dict[str, OperationDescriptor] = {
    # ── FILE_TOOLS (mixed: read + write) ────────────────────────────
    "read_file": _op("read_file", "read_only", _READ_ONLY),
    "write_file": _op("write_file", "mutating", _CODE_WRITE),
    # patch_file is defined in tools.py but not currently in TOOL_MAP;
    # tests inject it (and it may be registered later). Classified by its
    # true effect so the T7a guard never treats a real file-mutating tool
    # as unknown.
    "patch_file": _op("patch_file", "mutating", _CODE_WRITE),
    "list_directory": _op("list_directory", "read_only", _READ_ONLY),
    "read_yaml": _op("read_yaml", "read_only", _READ_ONLY),
    "write_yaml": _op("write_yaml", "mutating", _CODE_WRITE),
    "search_in_files": _op("search_in_files", "read_only", _READ_ONLY),

    # ── GIT_TOOLS (mixed: read + write) ─────────────────────────────
    "git_status": _op("git_status", "read_only", _READ_ONLY),
    "git_log": _op("git_log", "read_only", _READ_ONLY),
    "git_diff": _op("git_diff", "read_only", _READ_ONLY),
    "git_diff_staged": _op("git_diff_staged", "read_only", _READ_ONLY),
    "git_branch": _op("git_branch", "read_only", _READ_ONLY),
    "git_remote_list": _op("git_remote_list", "read_only", _READ_ONLY),
    "git_add": _op("git_add", "mutating", _CODE_WRITE),
    "git_commit": _op("git_commit", "mutating", _CODE_WRITE),
    "git_checkout_branch": _op("git_checkout_branch", "mutating", _CODE_WRITE),
    "git_push": _op("git_push", "mutating", _CODE_WRITE),
    "create_pr": _op("create_pr", "mutating", _CODE_WRITE),
    "git_add_remote": _op("git_add_remote", "mutating", _CODE_WRITE),

    # ── BASH_TOOLS — FAIL-CLOSED (single-name multi-op) ─────────────
    # run_bash is a shell entry point: could read OR write. Classified
    # ``mutating`` conservatively; per-operation refinement is a T6 job.
    "run_bash": _op("run_bash", "mutating", _CODE_WRITE),

    # ── REVIEW_TOOLS (Gerrit, mixed: read + write) ──────────────────
    "gerrit_get_diff": _op("gerrit_get_diff", "read_only", _READ_ONLY),
    "gerrit_post_comment": _op("gerrit_post_comment", "mutating", _GERRIT_WRITE),
    "gerrit_submit_review": _op("gerrit_submit_review", "mutating", _GERRIT_WRITE),

    # ── TASK_TOOLS + ORCHESTRATION_TOOLS ────────────────────────────
    "get_next_task": _op("get_next_task", "read_only", _READ_ONLY),
    "update_task_status": _op("update_task_status", "mutating", _TASK_WRITE),
    "add_task_comment": _op("add_task_comment", "mutating", _TASK_WRITE),
    "create_task": _op("create_task", "mutating", _TASK_WRITE),

    # ── REPORT_TOOLS / SIMULATION_TOOLS / ARTIFACT_TOOLS / IMAGE ────
    "generate_artifact_report": _op("generate_artifact_report", "mutating", _ARTIFACT_WRITE),
    "run_simulation": _op("run_simulation", "mutating", _ARTIFACT_WRITE),
    "register_build_artifact": _op("register_build_artifact", "mutating", _ARTIFACT_WRITE),
    "image_generate": _op("image_generate", "mutating", _ARTIFACT_WRITE),

    # ── PLATFORM_TOOLS / DEPLOY_TOOLS (mixed) ───────────────────────
    "get_platform_config": _op("get_platform_config", "read_only", _READ_ONLY),
    "check_evk_connection": _op("check_evk_connection", "read_only", _READ_ONLY),
    "list_uvc_devices": _op("list_uvc_devices", "read_only", _READ_ONLY),
    "deploy_to_evk": _op("deploy_to_evk", "mutating", _DEPLOY),

    # ── MEMORY_TOOLS / EPISODIC_TOOLS + save_solution ───────────────
    # save_solution is currently unbound from every guild (H0.5a,
    # OP-2592) — it's included here so the kernel already knows how to
    # classify it when the U6-0 provenance gate re-authorises it (T6+).
    "summarize_state": _op("summarize_state", "read_only", _READ_ONLY),
    "search_past_solutions": _op("search_past_solutions", "read_only", _READ_ONLY),
    "save_solution": _op("save_solution", "mutating", _MEMORY_WRITE),

    # ── MCP_TOOLS / WEB_SEARCH_TOOLS ────────────────────────────────
    "android_skill_search": _op("android_skill_search", "read_only", _READ_ONLY),
    # web_search is registered @tool("WebSearch") — key is the registered name.
    "WebSearch": _op("WebSearch", "read_only", _READ_ONLY),

    # ── SUPERVISOR_OBSERVE_TOOLS (all read-only) ────────────────────
    # supervisor_list_transitions is appended to SUPERVISOR_OBSERVE_TOOLS
    # at import time in tools.py — read-only ("what moves are legal?").
    "supervisor_quota_status": _op("supervisor_quota_status", "read_only", _READ_ONLY),
    "supervisor_recent_incidents": _op("supervisor_recent_incidents", "read_only", _READ_ONLY),
    "supervisor_delivery_summary": _op("supervisor_delivery_summary", "read_only", _READ_ONLY),
    "supervisor_ticket_detail": _op("supervisor_ticket_detail", "read_only", _READ_ONLY),
    "supervisor_guild_capabilities": _op("supervisor_guild_capabilities", "read_only", _READ_ONLY),
    "supervisor_release_status": _op("supervisor_release_status", "read_only", _READ_ONLY),
    "supervisor_list_transitions": _op("supervisor_list_transitions", "read_only", _READ_ONLY),

    # ── SORA_ACTION_TOOLS + SORA_PLANNING_TOOLS (JIRA writes) ───────
    "supervisor_rescue_ticket": _op("supervisor_rescue_ticket", "mutating", _TICKET_WRITE),
    "supervisor_requeue_ticket": _op("supervisor_requeue_ticket", "mutating", _TICKET_WRITE),
    "supervisor_strip_stale_labels": _op("supervisor_strip_stale_labels", "mutating", _TICKET_WRITE),
    "supervisor_comment_ticket": _op("supervisor_comment_ticket", "mutating", _TICKET_WRITE),
    "supervisor_transition_ticket": _op("supervisor_transition_ticket", "mutating", _TICKET_WRITE),
    "supervisor_set_labels": _op("supervisor_set_labels", "mutating", _TICKET_WRITE),
    "supervisor_link_blocks": _op("supervisor_link_blocks", "mutating", _TICKET_WRITE),

    # ── SORA_P5_PROPOSE_TOOLS ───────────────────────────────────────
    "propose_action": _op("propose_action", "mutating", _DANGEROUS_PROPOSE),
    "list_pending_actions": _op("list_pending_actions", "read_only", _READ_ONLY),

    # ── SDK runner tools (auto-runner-sdk.py RUNNER_TOOLS) ──────────
    # These are the Claude-Code-native names the SDK loop exposes.
    # Not enumerable via a single Python registry, so they're NOT
    # covered by the parity test — but they ARE covered here so the
    # T6 SDK adapter resolves against the same table.
    "Read": _op("Read", "read_only", _READ_ONLY),
    "Grep": _op("Grep", "read_only", _READ_ONLY),
    "Glob": _op("Glob", "read_only", _READ_ONLY),
    "WebFetch": _op("WebFetch", "read_only", _READ_ONLY),
    "ToolSearch": _op("ToolSearch", "read_only", _READ_ONLY),
    "Write": _op("Write", "mutating", _CODE_WRITE),
    "Edit": _op("Edit", "mutating", _CODE_WRITE),
    # dispatcher alias for Edit; classify identically.
    "str_replace_based_edit_tool": _op("str_replace_based_edit_tool", "mutating", _CODE_WRITE),
    # Bash / bash — FAIL-CLOSED single-name multi-op (see run_bash note).
    "Bash": _op("Bash", "mutating", _CODE_WRITE),
    "bash": _op("bash", "mutating", _CODE_WRITE),
    "Agent": _op("Agent", "mutating", _DELEGATION),
    # Skill runs a manifest-pinned, hash-verified ``*.skill`` executable
    # as a subprocess with the full parent environment
    # (backend/agents/skills_loader.py::_run_executable_skill, P-SKILL).
    # Classifying it as bare ``delegation`` would let an enforce flip on
    # code_write / deploy be silently bypassed through a skill call — so
    # it gets its own high-risk family (_SKILL_EXEC). Agent stays
    # ``delegation`` (sub-agent spawn, not arbitrary code exec).
    "Skill": _op("Skill", "mutating", _SKILL_EXEC),

    # ── Runner dispatcher extras (runner_handlers.py::_HANDLERS) ────
    # KnowledgeRetrieval is bound via bind_to_dispatcher on the runner
    # dispatcher — a read-only knowledge lookup, no side effect.
    "KnowledgeRetrieval": _op("KnowledgeRetrieval", "read_only", _READ_ONLY),

    # ── Default dispatcher extras (tool_dispatcher.py::_default_dispatcher) ─
    # SlackPostMessage posts to a Slack channel — an outbound message
    # to an external comms surface. FAMILY: external_comms.
    "SlackPostMessage": _op("SlackPostMessage", "mutating", _EXTERNAL_COMMS),

    # ── OP-828 built-in tool aliases (bind_built_in_tools*) ────────
    # ``code_execution`` names the DISPATCHER-REGISTERED emulation entry
    # (backend/agents/tool_dispatcher.py::ptc_sandbox_handler) that
    # tests exercise locally — the LIVE ``code_execution_20260120``
    # runs PROVIDER-SIDE inside Anthropic's PTC sandbox and never
    # reaches this dispatcher. Provider-side containment is P-PROV
    # (NOT this ticket); this classification only governs the local
    # emulation name so an enforce flip treats it as ``code_write``.
    "code_execution": _op("code_execution", "mutating", _CODE_WRITE),

    # ── OP-851 Memory Tool (bind_memory_tool → MEMORY_TOOL_NAME="memory") ──
    # Fail-closed name-level: memory tool exposes read (``view``) AND
    # write (``create``/``str_replace``/``insert``/``delete``/``rename``)
    # sub-commands, but ``resolve()`` is name-only today (the ``command``
    # arg is not inspected). Classified ``mutating`` / ``memory_write``
    # conservatively; per-command refinement is deferred to T8/T11 when
    # memory enforcement matters.
    "memory": _op("memory", "mutating", _MEMORY_WRITE),
}


_EXTERNAL_AGENT_PREFIX = "external_agent:"


def resolve(tool_name: str) -> OperationDescriptor:
    """Return the registry entry for ``tool_name`` — or a fail-closed
    synthetic descriptor if the tool is not classified.

    The unknown-tool descriptor carries
    ``effect="mutating"``/``family="__unknown_deny__"``: the U6-0 kernel
    treats it as protected, so an adapter that exposes a NEW tool without
    a registry entry cannot silently fall through to a permissive path.
    Every classification MUST land in :data:`TOOL_METADATA`; the parity
    test enforces this for the LangChain surface.

    Fallthrough #1 — dynamic A2A prefix. The outbound A2A node
    (``backend/agents/nodes.py::external_agent_node``) synthesises the
    per-call tool name as ``external_agent:<clean_agent_id>``. That id is
    a free-form registry key that MAY itself contain ``:`` (e.g.
    ``external_agent:team:bot``), so this branch matches on the prefix
    and requires a non-empty remainder — we do NOT ``split(":")``, which
    would truncate colon-in-id agent names to the first segment. Every
    such call is a delegation to another authority boundary → classified
    ``mutating`` / ``delegation``.
    """
    hit = TOOL_METADATA.get(tool_name)
    if hit is not None:
        return hit
    if (
        tool_name.startswith(_EXTERNAL_AGENT_PREFIX)
        and len(tool_name) > len(_EXTERNAL_AGENT_PREFIX)
    ):
        return OperationDescriptor(
            tool_name=tool_name,
            effect="mutating",
            family=_DELEGATION,
        )
    return OperationDescriptor(
        tool_name=tool_name,
        effect="mutating",
        family=_UNKNOWN_DENY,
    )
