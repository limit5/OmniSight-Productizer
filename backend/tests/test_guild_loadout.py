"""BP.B.10 -- Guild/loadout registry contract coverage."""

from __future__ import annotations

import pytest

from backend.agents import tools as agent_tools


LOADOUT_KEYS = tuple(sorted(agent_tools.AGENT_TOOLS))

EXPECTED_LOADOUT_NAMES: dict[str, frozenset[str]] = {
    "architect": frozenset(
        {
            "WebSearch",
            "add_task_comment",
            "create_pr",
            "get_next_task",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "list_directory",
            "read_file",
            "read_yaml",
            "run_bash",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "custom": frozenset(
        {
            "add_task_comment",
            "android_skill_search",
            "check_evk_connection",
            "create_pr",
            "deploy_to_evk",
            "get_next_task",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "image_generate",
            "list_directory",
            "list_uvc_devices",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "devops": frozenset(
        {
            "add_task_comment",
            "check_evk_connection",
            "create_pr",
            "deploy_to_evk",
            "get_next_task",
            "get_platform_config",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "list_directory",
            "list_uvc_devices",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "firmware": frozenset(
        {
            "add_task_comment",
            "check_evk_connection",
            "create_pr",
            "deploy_to_evk",
            "get_next_task",
            "get_platform_config",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "list_directory",
            "list_uvc_devices",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "run_simulation",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "general": frozenset(
        {
            "add_task_comment",
            "android_skill_search",
            "check_evk_connection",
            "create_pr",
            "deploy_to_evk",
            "get_next_task",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "image_generate",
            "list_directory",
            "list_uvc_devices",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "intel": frozenset(
        {
            "WebSearch",
            "add_task_comment",
            "create_pr",
            "get_next_task",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "list_directory",
            "read_file",
            "read_yaml",
            "run_bash",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "manufacturing": frozenset(
        {
            "add_task_comment",
            "get_next_task",
            "list_directory",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "run_simulation",
            "search_in_files",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "mechanical": frozenset(
        {
            "add_task_comment",
            "get_next_task",
            "list_directory",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "run_simulation",
            "search_in_files",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "reporter": frozenset(
        {
            "add_task_comment",
            "create_pr",
            "generate_artifact_report",
            "get_next_task",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "list_directory",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "search_in_files",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "reviewer": frozenset(
        {
            "add_task_comment",
            "gerrit_get_diff",
            "gerrit_post_comment",
            "gerrit_submit_review",
            "get_next_task",
            "git_branch",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_status",
            "list_directory",
            "read_file",
            "read_yaml",
            "search_in_files",
            "summarize_state",
        }
    ),
    "software": frozenset(
        {
            "add_task_comment",
            "android_skill_search",
            "create_pr",
            "get_next_task",
            "get_platform_config",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "image_generate",
            "list_directory",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "run_simulation",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
    "validator": frozenset(
        {
            "add_task_comment",
            "check_evk_connection",
            "create_pr",
            "deploy_to_evk",
            "get_next_task",
            "get_platform_config",
            "git_add",
            "git_add_remote",
            "git_branch",
            "git_checkout_branch",
            "git_commit",
            "git_diff",
            "git_diff_staged",
            "git_log",
            "git_push",
            "git_remote_list",
            "git_status",
            "list_directory",
            "list_uvc_devices",
            "read_file",
            "read_yaml",
            "register_build_artifact",
            "run_bash",
            "run_simulation",
            "save_solution",
            "search_in_files",
            "search_past_solutions",
            "summarize_state",
            "update_task_status",
            "write_file",
            "write_yaml",
        }
    ),
}

CATEGORY_TOOL_NAMES: dict[str, frozenset[str]] = {
    "deploy": frozenset(tool.name for tool in agent_tools.DEPLOY_TOOLS),
    "episodic": frozenset(tool.name for tool in agent_tools.EPISODIC_TOOLS),
    "git_mutation": frozenset(
        {
            "create_pr",
            "git_add",
            "git_add_remote",
            "git_checkout_branch",
            "git_commit",
            "git_push",
        }
    ),
    "image": frozenset(tool.name for tool in agent_tools.IMAGE_TOOLS),
    "mcp": frozenset(tool.name for tool in agent_tools.MCP_TOOLS),
    "platform": frozenset(tool.name for tool in agent_tools.PLATFORM_TOOLS),
    "report": frozenset(tool.name for tool in agent_tools.REPORT_TOOLS),
    "review": frozenset(tool.name for tool in agent_tools.REVIEW_TOOLS),
    "simulation": frozenset(tool.name for tool in agent_tools.SIMULATION_TOOLS),
    "web_search": frozenset(tool.name for tool in agent_tools.WEB_SEARCH_TOOLS),
}

EXPECTED_CATEGORY_LOADOUTS: dict[str, frozenset[str]] = {
    "deploy": frozenset({"custom", "devops", "firmware", "general", "validator"}),
    "episodic": frozenset(
        {
            "architect",
            "custom",
            "devops",
            "firmware",
            "general",
            "intel",
            "software",
            "validator",
        }
    ),
    "git_mutation": frozenset(
        {
            "architect",
            "custom",
            "devops",
            "firmware",
            "general",
            "intel",
            "reporter",
            "software",
            "validator",
        }
    ),
    "image": frozenset({"custom", "general", "software"}),
    "mcp": frozenset({"custom", "general", "software"}),
    "platform": frozenset({"devops", "firmware", "software", "validator"}),
    "report": frozenset({"reporter"}),
    "review": frozenset({"reviewer"}),
    "simulation": frozenset(
        {"firmware", "manufacturing", "mechanical", "software", "validator"}
    ),
    "web_search": frozenset({"architect", "intel"}),
}

CATEGORY_CASES = tuple(
    (loadout_key, category_name)
    for loadout_key in LOADOUT_KEYS
    for category_name in sorted(CATEGORY_TOOL_NAMES)
)


def _loadout_names(loadout_key: str) -> frozenset[str]:
    return frozenset(tool.name for tool in agent_tools.AGENT_TOOLS[loadout_key])


@pytest.mark.parametrize("loadout_key", LOADOUT_KEYS)
def test_guild_loadout_matches_expected_tool_names(loadout_key: str) -> None:
    assert _loadout_names(loadout_key) == EXPECTED_LOADOUT_NAMES[loadout_key]


@pytest.mark.parametrize("loadout_key", LOADOUT_KEYS)
def test_guild_loadout_has_no_duplicate_tool_names(loadout_key: str) -> None:
    names = [tool.name for tool in agent_tools.AGENT_TOOLS[loadout_key]]

    assert len(names) == len(set(names))


@pytest.mark.parametrize(("loadout_key", "category_name"), CATEGORY_CASES)
def test_guild_loadout_category_membership(
    loadout_key: str,
    category_name: str,
) -> None:
    loadout_names = _loadout_names(loadout_key)
    category_names = CATEGORY_TOOL_NAMES[category_name]

    if loadout_key in EXPECTED_CATEGORY_LOADOUTS[category_name]:
        assert category_names <= loadout_names
    else:
        assert category_names.isdisjoint(loadout_names)


@pytest.mark.parametrize("tool_name", sorted(agent_tools.TOOL_MAP))
def test_tool_map_tool_is_reachable_from_a_guild_loadout(tool_name: str) -> None:
    all_loadout_names = frozenset(
        tool.name
        for loadout_tools in agent_tools.AGENT_TOOLS.values()
        for tool in loadout_tools
    )

    assert tool_name in all_loadout_names


def test_guild_loadout_fixture_count_matches_bp_b10_target() -> None:
    collected_case_count = (
        len(LOADOUT_KEYS)
        + len(LOADOUT_KEYS)
        + len(CATEGORY_CASES)
        + len(agent_tools.TOOL_MAP)
    )

    assert 170 <= collected_case_count <= 210
