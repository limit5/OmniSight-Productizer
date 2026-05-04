#!/usr/bin/env python3
"""auto-runner-sdk — agentic TODO runner via Anthropic native API.

Same TODO.md / HANDOFF.md / SOP contract as auto-runner.py (the CLI
version), but drives Claude through the Anthropic SDK with full tool-use
loop, prompt caching (90% off after turn 1), and CostGuard tracking.

Differences vs. auto-runner.py:
  * Uses ANTHROPIC_API_KEY (subscription-independent)
  * Per-item cost is observable + accumulated via CostGuard
  * Prompt cache: SOP/TODO/HANDOFF system blocks reused across turns
    within a single item — typical inner-loop cache hit ≥90%

Run modes (compatible with the CLI version):
  * No env: process every pending [ ] in TODO.md
  * OMNISIGHT_RUNNER_FILTER=B13,Q-prep: only matching sections
  * OMNISIGHT_SDK_MODEL=claude-sonnet-4-6: override default opus
  * OMNISIGHT_SDK_MAX_ITERATIONS=80: bump tool-loop ceiling per item
  * Ctrl+C once: graceful stop after current item
  * Ctrl+C twice: force exit
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Make `from backend...` imports resolve when invoked from project root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from backend.agents.anthropic_native_client import (  # noqa: E402
    DEFAULT_MODEL_OPUS,
    AnthropicClient,
    RunResult,
)
from backend.agents.cost_guard import (  # noqa: E402
    CostActual,
    CostGuard,
    InMemoryCostStore,
    estimate_cost,
)
from backend.agents.mcp_integration import (  # noqa: E402
    RemoteMCPRegistry,
    build_registry_from_env,
)
from backend.agents.project_memory import (  # noqa: E402
    load_all_memory,
    parse_ignored_paths,
    render_operator_summary as render_memory_operator_summary,
    render_for_prompt as render_memory_for_prompt,
)
from backend.agents.runner_handlers import make_runner_dispatcher  # noqa: E402
from backend.agents.skills_loader import (  # noqa: E402
    SkillRegistry,
    load_default_scopes,
    make_skill_handler,
    render_catalog_for_prompt,
)
from backend.agents.sub_agent import (  # noqa: E402
    list_default_subagent_types,
    make_agent_tool_handler,
)


# ─── Graceful shutdown ───────────────────────────────────────────


_shutdown_requested = False
_ctrl_c_count = 0


def _sigint_handler(signum: int, frame: Any) -> None:
    """First Ctrl+C: flag shutdown, finish current item. Second: force exit."""
    global _shutdown_requested, _ctrl_c_count
    _ctrl_c_count += 1
    if _ctrl_c_count == 1:
        _shutdown_requested = True
        print(
            "\n\n🛑 [優雅停機] 收到 Ctrl+C，等待當前任務完成後停止流水線..."
        )
        print("   (再按一次 Ctrl+C 強制立即終止)\n")
    else:
        print("\n\n💥 [強制終止] 收到第二次 Ctrl+C，立即停止。")
        sys.exit(1)


signal.signal(signal.SIGINT, _sigint_handler)


# ─── Config ──────────────────────────────────────────────────────


BASE_DIR = _HERE
TODO_FILE = BASE_DIR / "TODO.md"
HANDOFF_FILE = BASE_DIR / "HANDOFF.md"
SOP_FILE = BASE_DIR / "docs" / "sop" / "implement_phase_step.md"
# Memory rule files (CLAUDE.md / AGENTS.md / OMNISIGHT.md / WARP.md) are
# discovered dynamically by backend.agents.project_memory — no constants
# needed here. Kept for backward compat: scripts that imported CLAUDE_FILE.
CLAUDE_FILE = BASE_DIR / "CLAUDE.md"

MODEL_NAME = os.environ.get("OMNISIGHT_SDK_MODEL", DEFAULT_MODEL_OPUS)
MAX_ITERATIONS = int(os.environ.get("OMNISIGHT_SDK_MAX_ITERATIONS", "80"))
MAX_TOKENS = int(os.environ.get("OMNISIGHT_SDK_MAX_TOKENS", "16000"))
MAX_RETRIES = int(os.environ.get("OMNISIGHT_SDK_MAX_RETRIES", "2"))
COOLDOWN_S = int(os.environ.get("OMNISIGHT_SDK_COOLDOWN", "5"))
SECTION_COOLDOWN_S = int(os.environ.get("OMNISIGHT_SDK_SECTION_COOLDOWN", "10"))
DAILY_BUDGET_USD = float(os.environ.get("OMNISIGHT_SDK_DAILY_BUDGET", "0") or 0)
# Per-item soft cap. Items costing more than this are flagged
# retryable=False — preventing the "$10 → retry → $10 → still broken"
# pattern that burned $25 on W14.5. Set to 0 to disable. Default $8
# leaves headroom for large items but stops runaway retries.
MAX_PER_ITEM_USD = float(os.environ.get("OMNISIGHT_SDK_MAX_PER_ITEM_USD", "8") or 0)

# Phase 7 — what to do when an item fails / is deferred.
#   stop     (default): write structured stop reason to HANDOFF.md and
#                       exit the pipeline cleanly. Operator decides next
#                       step (manual fix / subscription CLI / decompose
#                       TODO). Safe under implicit dependencies — W15.2
#                       won't run when W15.1 failed.
#   continue           : original best-effort batch behaviour. Keep
#                       going. Only safe when caller knows items are
#                       truly independent (e.g., BP.K.1-8 separate
#                       frontend components, or BP.W3.1 27 skill packs).
#
# A `section` mode (skip remaining items in current section, advance to
# next matching section) is intentionally NOT implemented in v1 — it
# adds runtime exclusion-set complexity for a use case we don't have
# real demand for. Add when an empirical need surfaces.
FAIL_BEHAVIOR = os.environ.get("OMNISIGHT_SDK_FAIL_BEHAVIOR", "stop").strip().lower()
if FAIL_BEHAVIOR not in {"stop", "continue"}:
    print(
        f"⚠️ OMNISIGHT_SDK_FAIL_BEHAVIOR={FAIL_BEHAVIOR!r} 不是合法值"
        " (stop|continue)，回退到 'stop'"
    )
    FAIL_BEHAVIOR = "stop"

# Tools the agent loop is allowed to call. Wired in runner_handlers +
# skills_loader + sub_agent (Skill / Agent are registered dynamically at
# startup once the registry / dispatcher are ready).
RUNNER_TOOLS: list[str] = [
    "Read", "Write", "Edit", "Bash", "Grep", "Glob", "Skill", "Agent",
]

# OMNISIGHT_SDK_DISABLE_SKILLS=1 silences the loader entirely (drops "Skill"
# from RUNNER_TOOLS, no catalog injection). Useful for benchmark / size
# audits where a deterministic prompt is needed.
SKILLS_DISABLED = os.environ.get("OMNISIGHT_SDK_DISABLE_SKILLS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# OMNISIGHT_SDK_DISABLE_SUBAGENTS=1 drops "Agent" from RUNNER_TOOLS so the
# parent loop cannot spawn sub-agents. Useful when budget is tight or
# when the LLM has been observed over-using sub-agents on simple tasks.
SUBAGENTS_DISABLED = os.environ.get(
    "OMNISIGHT_SDK_DISABLE_SUBAGENTS", ""
).strip().lower() in {"1", "true", "yes", "on"}

# Operator UI for WP.5.7: omit specific rule files without editing repo
# files. Relative paths resolve against BASE_DIR; ~/ resolves to home.
RULE_IGNORE_ENV = "OMNISIGHT_RULE_IGNORE"
RULE_IGNORE_PATHS = parse_ignored_paths(
    os.environ.get(RULE_IGNORE_ENV),
    project_root=BASE_DIR,
)


# ─── Track filter (parallel-worker support) ──────────────────────


RUNNER_FILTER_RAW = os.environ.get("OMNISIGHT_RUNNER_FILTER", "").strip()
RUNNER_FILTER = (
    {p.strip().upper() for p in RUNNER_FILTER_RAW.split(",") if p.strip()}
    if RUNNER_FILTER_RAW
    else set()
)

# Force a single specific pending item (substring match against the line).
# Useful for dry-runs and when an earlier section item is too large but a
# later item in the same section is genuinely standalone. When set, the
# scanner returns the FIRST pending line whose stripped form contains this
# substring, regardless of position.
TARGET_ITEM_SUBSTR = os.environ.get("OMNISIGHT_SDK_TARGET_ITEM", "").strip()


def _section_matches_filter(section_title: str) -> bool:
    """Same matcher as auto-runner.py — see that file for the rule precedence."""
    if not RUNNER_FILTER:
        return True
    m = re.match(r"###\s+([A-Za-z][\w.-]*?)(?=\s|$)", section_title)
    if not m:
        return False
    section_id = m.group(1).rstrip(".").upper()
    for f in RUNNER_FILTER:
        if section_id == f:
            return True
        if "." not in f and section_id.startswith(f + "."):
            return True
        if len(f) == 1 and section_id.startswith(f):
            rest = section_id[len(f) :]
            if rest and rest[0].isdigit():
                return True
    return False


# ─── TODO scanning ───────────────────────────────────────────────


def get_next_pending_item() -> tuple[str | None, str | None, str | None]:
    """Find next ``- [ ]`` line, returning (section_title, item_line, ctx).

    When ``OMNISIGHT_SDK_TARGET_ITEM`` is set, returns the first pending
    line whose stripped form contains that substring. Otherwise behaves
    like auto-runner.py — first pending in first matching section.
    """
    if not TODO_FILE.exists():
        print(f"❌ 找不到 {TODO_FILE} 檔案！")
        sys.exit(1)
    lines = TODO_FILE.read_text(encoding="utf-8").splitlines(keepends=True)

    current_section: str | None = None
    section_lines: list[str] = []

    for line in lines:
        if line.startswith("### "):
            if current_section and _section_matches_filter(current_section):
                hit = _find_pending(section_lines)
                if hit:
                    return current_section, hit, "".join(section_lines)
            current_section = line.strip()
            section_lines = []
            continue
        if line.startswith("## "):
            if current_section and _section_matches_filter(current_section):
                hit = _find_pending(section_lines)
                if hit:
                    return current_section, hit, "".join(section_lines)
            current_section = None
            section_lines = []
            continue
        if current_section is not None:
            section_lines.append(line)

    if current_section and _section_matches_filter(current_section):
        hit = _find_pending(section_lines)
        if hit:
            return current_section, hit, "".join(section_lines)
    return None, None, None


def _find_pending(lines: list[str]) -> str | None:
    """Find a pending ``- [ ]`` line. Honours TARGET_ITEM_SUBSTR if set."""
    if TARGET_ITEM_SUBSTR:
        for line in lines:
            stripped = line.strip()
            if (
                stripped.startswith("- [ ]")
                and TARGET_ITEM_SUBSTR in stripped
            ):
                return stripped
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            return stripped
    return None


# Keep the old name for backward-compat with any callers / tests.
_find_first_pending = _find_pending


def _mark_item_failed(item_line: str) -> None:
    """Flip a failed ``- [ ]`` to ``- [!]`` so we don't loop on it forever."""
    try:
        content = TODO_FILE.read_text(encoding="utf-8")
        failed = item_line.replace("- [ ]", "- [!]", 1)
        if failed != item_line and item_line in content:
            content = content.replace(item_line, failed, 1)
            TODO_FILE.write_text(content, encoding="utf-8")
            print(f"📝 已將失敗項目標記為 [!]：{failed[:60]}")
    except OSError as e:
        print(f"⚠️ 標記失敗項目時出錯：{e}")


# ─── Phase 7 — stop-on-failure structured handoff ────────────────


def _collect_remaining_pending(
    *, current_section_title: str, current_item_line: str
) -> tuple[list[str], list[tuple[str, str]]]:
    """Return (remaining_in_section, remaining_other_sections).

    `remaining_in_section`: pending item strings (still ``- [ ]``) in
    the same ### section as the failure, AFTER the failed item, that
    pass the RUNNER_FILTER. These are the items most likely blocked by
    the failure (intra-section dependency).

    `remaining_other_sections`: list of ``(section_title, item_line)``
    pairs in OTHER matching sections still pending. Lower likelihood
    of dependency on this failure but worth surfacing for ops review.
    """
    if not TODO_FILE.exists():
        return [], []
    lines = TODO_FILE.read_text(encoding="utf-8").splitlines()

    in_section: list[str] = []
    other: list[tuple[str, str]] = []
    cur_sec: str | None = None
    in_target = False
    past_failed_item = False

    for line in lines:
        if line.startswith("### "):
            cur_sec = line.strip()
            in_target = cur_sec == current_section_title
            if not in_target:
                past_failed_item = False
            continue
        if line.startswith("## "):
            cur_sec = None
            in_target = False
            past_failed_item = False
            continue
        stripped = line.strip()
        if cur_sec is None:
            continue
        if not _section_matches_filter(cur_sec):
            continue
        if in_target:
            if stripped == current_item_line:
                past_failed_item = True
                continue
            if past_failed_item and stripped.startswith("- [ ]"):
                in_section.append(stripped)
        else:
            if stripped.startswith("- [ ]"):
                other.append((cur_sec, stripped))
    return in_section, other


_HANDOFF_STOP_HEADING = "## ⏸️ Runner 自動停工"


def _write_handoff_stop_block(
    *,
    section_title: str,
    item_line: str,
    failure_reason: str,
    cumulative_usd: float,
    completed_count: int,
    remaining_in_section: list[str],
    remaining_other_sections: list[tuple[str, str]],
) -> None:
    """Append a structured ``## ⏸️ Runner 自動停工`` block to HANDOFF.md.

    Always APPENDED (never replaces existing content) so prior task
    history is preserved. The block has a stable heading prefix so
    operators can grep for `## ⏸️ Runner 自動停工` to enumerate all
    pipeline stops over time.
    """
    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    short_section = section_title.lstrip("#").strip()

    blocked_section_lines = "\n".join(f"  - {it}" for it in remaining_in_section)
    if not blocked_section_lines:
        blocked_section_lines = "  - (本 section 無其他 pending 項)"

    other_section_lines = (
        "\n".join(
            f"  - [{s.lstrip('#').strip()[:60]}] {it[:80]}"
            for s, it in remaining_other_sections[:15]
        )
        or "  - (其他 section 無 pending 項或不在 RUNNER_FILTER 內)"
    )
    if len(remaining_other_sections) > 15:
        other_section_lines += (
            f"\n  - …（還有 {len(remaining_other_sections) - 15} 項未列出）"
        )

    block = f"""

{_HANDOFF_STOP_HEADING} — {iso}

**停工項目**: `{item_line[:120]}`
**所屬區塊**: {short_section[:120]}
**停工原因**: {failure_reason}
**本批次累計花費**: {_format_usd(cumulative_usd)}
**本批次已完成**: {completed_count} 顆
**FAIL_BEHAVIOR**: `stop`（default）— runner 主動退出，等人工介入

### 同 section 內被擋住的後續項
{blocked_section_lines}

### 其他符合 RUNNER_FILTER 的 section 待跑項
{other_section_lines}

### 為什麼停而不是繼續

當前 runner 採用 stop-on-failure 策略：失敗的項目可能阻擋下游（例
如 W15.2 vite_error_relay 依賴 W15.1 plugin 介面，W15.1 沒成功的話
W15.2 跑也是白跑）。直接停工讓人類判斷下一步比 runner 自作主張安全。

### 下一步建議（操作者選一）

1. **改用訂閱版 CLI 跑這項**：
   `python3 auto-runner.py`（訂閱版有完整 Agent / Skill / MCP 工具，
   月費已付不另外燒）。注意需手動把 `[!]` 改回 `[ ]` 才會被 picked。
2. **手動拆 TODO**：把卡住的項目在 TODO 裡拆成 2-5 顆獨立 sub-item，
   然後重跑 API runner。
3. **跳過 + 標 `[O]`**：人工確認此項需 operator-blocked，標 `[O]`
   後重跑 runner，會自動跳過。
4. **強制繼續**：明確知道後續項目獨立時，
   `OMNISIGHT_SDK_FAIL_BEHAVIOR=continue` 重跑。**只有確定獨立才用，
   不然會浪費錢**。
5. **整批回退**：看 `git log --oneline` 找 baseline，`git reset --hard
   <SHA>` 拋棄整批。
"""
    try:
        existing = (
            HANDOFF_FILE.read_text(encoding="utf-8")
            if HANDOFF_FILE.exists()
            else ""
        )
        HANDOFF_FILE.write_text(existing + block, encoding="utf-8")
        print(f"📜 [HANDOFF] 已附加停工原因區塊")
    except OSError as e:
        print(f"⚠️ 寫 HANDOFF 失敗：{e}")


# ─── Cost helpers ────────────────────────────────────────────────


def _format_usd(usd: float) -> str:
    return f"${usd:.4f}"


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s ({seconds:.1f}s)"
    return f"{s}s ({seconds:.1f}s)"


# ─── Item runner ─────────────────────────────────────────────────


async def run_one_item(
    *,
    client: AnthropicClient,
    cost_guard: CostGuard,
    section_title: str,
    item_line: str,
    section_context: str,
    sop_text: str,
    todo_text: str,
    handoff_text: str,
    memory_block: str = "",
    mcp_servers: list[dict] | None = None,
    skills_catalog: str = "",
    tools: list[str] | None = None,
) -> tuple[bool, RunResult | None, bool]:
    """Drive Claude through one TODO item.

    Returns:
      (success, run_result, retryable)

      ``retryable=False`` for **structural** failures where retrying the
      same prompt would burn money for the same outcome:

        * ``stop_reason=max_tokens``: LLM's final turn was cut off
          mid-response. Same prompt → same long response → same cut-off.
          Caller should mark the item failed-skip-without-retry.
        * ``stop_reason=max_iterations_exceeded``: tool loop exhausted
          MAX_ITERATIONS. Task likely needs decomposition; more rounds
          won't help.
        * Item cost exceeded :data:`MAX_PER_ITEM_USD`: cap as a guard
          against the "$10 → retry → $10 → still broken" failure mode
          that burned $25 on W14.5.

      ``retryable=True`` for transient / unknown failures (e.g., API
      hiccups) where one more attempt has positive expected value.
    """
    print(f"\n{'=' * 60}")
    print(f"🚀 [自動調度] 區塊: {section_title}")
    truncated = item_line[:80] + ("..." if len(item_line) > 80 else "")
    print(f"📌 [執行項目] {truncated}")
    print(f"{'=' * 60}\n")

    # System prompt = SOP + pointers. Embedding the full HANDOFF + TODO
    # would burn ~700k tokens per first-turn call (~$13 on opus-4-7); the
    # LLM can Read those files on demand via the Read tool, paying only
    # for what it actually needs.
    _ = todo_text  # not embedded — LLM reads on demand
    _ = handoff_text  # not embedded — LLM reads on demand
    # Phase 4 multi-rule memory: CLAUDE.md / AGENTS.md / OMNISIGHT.md /
    # WARP.md from project root, plus user-level ~/.claude/CLAUDE.md /
    # AGENTS.md. Built upstream by load_all_memory + render_for_prompt;
    # placed BEFORE SOP so its L1 constraints win on conflict.
    memory_section = f"{memory_block}\n\n" if memory_block.strip() else ""
    skills_block = f"\n{skills_catalog}\n" if skills_catalog else ""
    system_text = (
        f"# 執行環境\n"
        f"- 專案根目錄（PROJECT_ROOT）：`{BASE_DIR}`\n"
        f"- **所有檔案路徑必須在這個 root 之下**。Read/Write/Edit/Bash/Grep/Glob 工具會拒絕 root 之外的路徑。\n"
        f"- 你也可以直接傳相對路徑（例如 `TODO.md`、`backend/agents/state.py`），會被 resolve 成 PROJECT_ROOT 之下。\n"
        f"- Bash 的 cwd 已經固定在 PROJECT_ROOT；不要 `cd` 到別處。\n\n"
        f"{memory_section}"
        f"# 專案 SOP\n{sop_text}\n\n"
        "# 可用上下文檔案\n"
        "- `TODO.md`（PROJECT_ROOT）— 全部任務清單。當前任務的區塊已放在你的 user prompt 內。\n"
        "  若需查其他區塊（例如 cross-reference 上下游 task），用 Read tool。\n"
        "- `HANDOFF.md`（PROJECT_ROOT）— 過往 task 的交接記錄。**檔案上萬行，預設不要全讀**。\n"
        "  若你的任務明確需要參考 prior context，用 Read tool 配合 offset/limit 撈相關段落即可。\n"
        "- 專案 source code — 用 Read / Grep / Glob 探索。\n"
        f"{skills_block}"
    )

    prompt = (
        "你現在處於「全自動化無人值守」模式。\n\n"
        "**你只需要完成以下【單一項目】，不要做其他項目：**\n\n"
        f"➤ {item_line}\n\n"
        "此項目屬於以下區塊（僅供上下文參考，不要執行其他項目）：\n"
        f"{section_title}\n{section_context}\n\n"
        "【⚙️ 嚴格執行準則】：\n"
        "1. **最高指導原則：在進行任何思考與修改前，請務必先讀取並嚴格遵守 SOP 中的所有規則。**\n"
        "2. **只完成上方標記 ➤ 的那一個項目**。其他項目不要動。\n"
        "3. 這是真實執行階段，請直接讀寫檔案、修改程式碼、建立資料夾或執行必要指令。\n"
        "4. 如果遇到缺少的檔案，請參考專案上下文自行推導並建立。\n"
        "5. **【狀態標記鐵律】**：完成後，你「必須」開啟 TODO.md 進行狀態標記：\n"
        "   - 若你已由 AI 完成該項目，請將對應的 `- [ ]` 改為 `- [x]`。\n"
        "   - **若該項目需要人類實體操作 (Operator-blocked)，請將它從 `- [ ]` 改為 `- [O]`。**\n"
        "   - **只標記你剛完成的那一項，不要改動其他項目。**\n"
        "6. 請將本次的進度與最新狀態更新至 HANDOFF.md 中。\n"
        "7. 更新完後，請務必將更動後的內容 commit 到 Git，確保版本控制的完整性。\n"
        "8. 絕對不要詢問我任何問題或要求人類確認（你已經擁有最高權限）。\n"
        "9. 完成後，直接輸出「✅ 項目完成」並結束。\n\n"
        "【🛡️ 反 max_tokens 截斷準則】（Phase 5 強化規則）：\n"
        "  * 若任務涉及**多個 subsystem**（例如同時要動 backend + frontend + alembic + tests / "
        "    > 5 檔同改 / > 200 行新 code），**先**呼叫 Agent tool 用 `subagent_type=\"Plan\"` "
        "    出實作計畫拆成 1-3 個小 commit，再開始動手。\n"
        "  * 不要試圖在**單一回應內**寫完一個大模組 + 對應測試 + alembic — output 會被 "
        "    `max_tokens` 截斷，而且重試也會被截。**分多輪 tool_use** 才是對的。\n"
        "  * 若你開始懷疑這個 task 對 single-shot 太大 → 大膽用 Plan sub-agent。\n\n"
        "可用工具：Read / Write / Edit / Bash / Grep / Glob"
        + (" / Skill" if "Skill" in (tools or RUNNER_TOOLS) else "")
        + (" / Agent" if "Agent" in (tools or RUNNER_TOOLS) else "")
        + " — 路徑限專案根目錄之下。\n"
    )

    started = time.time()
    try:
        result = await client.run_with_tools(
            prompt=prompt,
            tools=tools or RUNNER_TOOLS,
            system=system_text,
            model=MODEL_NAME,
            max_tokens=MAX_TOKENS,
            max_iterations=MAX_ITERATIONS,
            enable_cache=True,
            on_tool_call="log",
            mcp_servers=mcp_servers,
        )
    except Exception as e:  # noqa: BLE001 - external boundary
        elapsed = time.time() - started
        print(f"\n❌ [系統錯誤] {type(e).__name__}: {e}")
        print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
        # Genuinely transient (API hiccup, connection reset) → retryable.
        return False, None, True

    elapsed = time.time() - started
    usage = result.usage
    actual_cost = estimate_cost(
        model=MODEL_NAME,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_creation_tokens=usage.cache_creation_input_tokens,
    )
    await cost_guard.record_estimate(actual_cost)
    await cost_guard.record_actual(
        CostActual(
            call_id=actual_cost.call_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_creation_tokens=usage.cache_creation_input_tokens,
            cost_usd=actual_cost.cost_usd_estimated,
        )
    )

    cache_pct = 0.0
    if usage.input_tokens + usage.cache_read_input_tokens > 0:
        cache_pct = (
            usage.cache_read_input_tokens
            / (usage.input_tokens + usage.cache_read_input_tokens)
            * 100
        )

    print(
        f"\n📊 [tokens] in={usage.input_tokens} out={usage.output_tokens} "
        f"cache_read={usage.cache_read_input_tokens} "
        f"cache_create={usage.cache_creation_input_tokens} "
        f"(cache hit {cache_pct:.0f}%)"
    )
    print(
        f"💰 [本項花費] {_format_usd(actual_cost.cost_usd_estimated)} | "
        f"⏱️ [耗時] {_fmt_duration(elapsed)} | "
        f"🔁 [iterations] {result.iterations} | "
        f"🛑 [stop] {result.stop_reason}"
    )

    # Tool-call summary so operators can see Phase 2/3 effectiveness:
    # which tools the LLM actually picked, and how often. Differentiates
    # Skill / Agent (Phase 2-3 features) from the basic 6 host tools.
    if result.tool_calls:
        by_name: dict[str, int] = {}
        for tc in result.tool_calls:
            name = tc.get("name", "?")
            by_name[name] = by_name.get(name, 0) + 1
        summary = ", ".join(
            f"{k}={v}" for k, v in sorted(by_name.items(), key=lambda kv: -kv[1])
        )
        print(f"🔧 [tool calls] {summary}")

    success = (
        result.stop_reason == "end_turn"
        and "✅ 項目完成" in result.final_text
    )

    # Phase 5: classify failure as retryable or structural. Structural
    # failures (max_tokens / max_iterations / over-budget) won't improve
    # on retry — they reflect that THIS task is too big for the current
    # config. Retrying just burns money for the same outcome (cf. W14.5
    # which lost $25 on two identical max_tokens retries).
    retryable = True
    item_cost = actual_cost.cost_usd_estimated

    if success:
        print(f"\n✅ [項目完成] {item_line[:60]}")
    elif result.stop_reason == "max_tokens":
        print(
            f"\n⚠️ [回應被截斷] stop_reason=max_tokens — LLM 單次回應超過 "
            f"max_tokens={MAX_TOKENS}。重試會再被截，**不重試**。\n"
            f"   救法：用 Plan sub-agent 拆解任務，或調高 OMNISIGHT_SDK_MAX_TOKENS。"
        )
        retryable = False
    elif result.stop_reason == "max_iterations_exceeded":
        print(
            f"\n⚠️ [iterations 用盡] {MAX_ITERATIONS} 輪未收 ✅。任務需拆解，"
            f"重試多輪也救不了，**不重試**。"
        )
        retryable = False
    else:
        print(f"\n❌ [項目異常] 未收到 ✅ 項目完成 標記（stop={result.stop_reason}）")

    if (
        not success
        and MAX_PER_ITEM_USD > 0
        and item_cost > MAX_PER_ITEM_USD
    ):
        print(
            f"\n💸 [單項超預算] 本項花費 {_format_usd(item_cost)} > "
            f"MAX_PER_ITEM_USD {_format_usd(MAX_PER_ITEM_USD)}，"
            f"**不重試**避免炸更多。"
        )
        retryable = False

    return success, result, retryable


# ─── Main loop ───────────────────────────────────────────────────


async def main() -> None:
    print("🤖 OmniSight-Productizer SDK 流水線啟動 (Anthropic native API)")
    print(
        f"⚙️ 設定：model={MODEL_NAME} max_iter={MAX_ITERATIONS} "
        f"max_tokens={MAX_TOKENS} retries={MAX_RETRIES}"
    )
    if RUNNER_FILTER:
        print(
            f"🏷️ Track filter：只處理 {', '.join(sorted(RUNNER_FILTER))} 系列"
        )
    else:
        print("🏷️ Track filter：無（處理所有項目）")
    if DAILY_BUDGET_USD > 0:
        print(f"💵 Daily budget cap: {_format_usd(DAILY_BUDGET_USD)}")
    if MAX_PER_ITEM_USD > 0:
        print(
            f"💸 Per-item soft cap: {_format_usd(MAX_PER_ITEM_USD)} "
            "(超過 → 不重試，避免炸更多)"
        )
    if FAIL_BEHAVIOR == "stop":
        print(
            "🛑 Fail behavior: stop "
            "(任一項失敗即停工 + 寫 HANDOFF 等人工介入)"
        )
    else:
        print(
            "🏃 Fail behavior: continue "
            "(失敗會跳下一項 — 確定 items 獨立才用)"
        )
    print(
        "⚠️ 警告：系統將自動執行程式碼與系統指令，按 Ctrl+C 可隨時中斷。\n"
    )

    if not SOP_FILE.exists():
        print(f"⚠️ [警告] 找不到指定的 SOP 檔案！\n   {SOP_FILE}")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY 未設定。請先在 .env 或環境設定後重試。")
        sys.exit(1)

    # Memory layer banner (just the count + scope split — actual content
    # is loaded fresh per item to honour operator mid-pipeline edits).
    _initial_memory = load_all_memory(BASE_DIR, ignored_paths=RULE_IGNORE_PATHS)
    if _initial_memory:
        print(
            render_memory_operator_summary(
                _initial_memory,
                project_root=BASE_DIR,
                ignore_env_var=RULE_IGNORE_ENV,
            )
        )
    else:
        print(
            render_memory_operator_summary(
                _initial_memory,
                project_root=BASE_DIR,
                ignore_env_var=RULE_IGNORE_ENV,
            )
        )

    dispatcher = make_runner_dispatcher()

    # Skills: load 3-scope registry, register Skill tool handler on the
    # SAME dispatcher so AnthropicClient's tool loop can resolve it.
    active_tools = list(RUNNER_TOOLS)
    if SKILLS_DISABLED:
        skill_registry = SkillRegistry()
        active_tools = [t for t in active_tools if t != "Skill"]
        skills_catalog = ""
        print("🎒 Skills: disabled via OMNISIGHT_SDK_DISABLE_SKILLS")
    else:
        skill_registry = load_default_scopes(BASE_DIR)
        if len(skill_registry) > 0:
            dispatcher.register("Skill", make_skill_handler(skill_registry))
            skills_catalog = render_catalog_for_prompt(skill_registry)
            print(
                f"🎒 Skills: {len(skill_registry)} loaded "
                f"(project / home / bundled scopes)"
            )
        else:
            # Empty registry → drop Skill tool to avoid the LLM calling a
            # tool that has no entries.
            active_tools = [t for t in active_tools if t != "Skill"]
            skills_catalog = ""
            print("🎒 Skills: none found")

    client = AnthropicClient(
        default_model=MODEL_NAME,
        max_tokens_default=MAX_TOKENS,
        dispatcher=dispatcher,
    )

    # Sub-agent (Agent tool): handler needs the client reference, so
    # register AFTER client construction. Closure captures client; the
    # sub-agent re-uses the same dispatcher → inherits sandboxed handlers.
    if SUBAGENTS_DISABLED:
        active_tools = [t for t in active_tools if t != "Agent"]
        print("🧩 Sub-agents: disabled via OMNISIGHT_SDK_DISABLE_SUBAGENTS")
    else:
        _subagent_call_count = {"n": 0}

        def _on_subagent(info: dict) -> None:
            _subagent_call_count["n"] += 1
            print(
                f"   🧩 sub-agent #{_subagent_call_count['n']} "
                f"({info['subagent_type']} on {info['model']}, "
                f"max_iter={info['max_iterations']}): "
                f"{info['description'][:70]}"
            )

        dispatcher.register(
            "Agent",
            make_agent_tool_handler(
                client=client,
                parent_system_suffix=(
                    f"# Inherited execution context\n"
                    f"PROJECT_ROOT: {BASE_DIR}\n"
                    f"All tool paths must be inside PROJECT_ROOT — sandboxed.\n"
                ),
                on_dispatch=_on_subagent,
            ),
        )
        types_avail = ", ".join(list_default_subagent_types())
        print(f"🧩 Sub-agents: enabled ({types_avail})")
    cost_store = InMemoryCostStore()
    cost_guard = CostGuard(store=cost_store)

    # Build MCP server registry from env tokens — if no
    # OMNISIGHT_MCP_*_TOKEN vars are set the registry is empty and
    # ``mcp_servers_payload`` is an empty list (treated as None downstream).
    mcp_registry: RemoteMCPRegistry = build_registry_from_env()
    mcp_servers_payload: list[dict] = mcp_registry.to_anthropic_mcp_servers()
    if mcp_servers_payload:
        names = [s["name"] for s in mcp_servers_payload]
        print(f"🔌 MCP servers active: {', '.join(names)}")
    else:
        print(
            "🔌 MCP servers: none configured "
            "(set OMNISIGHT_MCP_*_TOKEN to enable Figma/Gmail/Calendar/Drive)"
        )

    pipeline_start = time.time()
    completed = 0
    failed = 0
    skipped: list[str] = []
    last_section: str | None = None
    cumulative_usd = 0.0

    while True:
        section_title, item_line, section_context = get_next_pending_item()
        if not section_title:
            break

        if last_section and last_section != section_title:
            print(
                f"\n📦 [切換區塊] {last_section[:40]}... → "
                f"{section_title[:40]}..."
            )
            time.sleep(SECTION_COOLDOWN_S)
        last_section = section_title

        # Re-read SOP/TODO/HANDOFF every item — TODO/HANDOFF mutate as we work.
        # Memory layer (CLAUDE/AGENTS/OMNISIGHT/WARP, project + user) is
        # walked fresh per item too — operators can edit mid-pipeline.
        try:
            sop_text = SOP_FILE.read_text(encoding="utf-8")
            todo_text = TODO_FILE.read_text(encoding="utf-8")
            handoff_text = (
                HANDOFF_FILE.read_text(encoding="utf-8")
                if HANDOFF_FILE.exists()
                else "(HANDOFF.md not yet created)"
            )
            memory_files = load_all_memory(BASE_DIR, ignored_paths=RULE_IGNORE_PATHS)
            memory_block = render_memory_for_prompt(memory_files)
        except OSError as e:
            print(f"❌ 讀取 SOP/TODO/HANDOFF/memory 失敗: {e}")
            sys.exit(1)
        print(
            render_memory_operator_summary(
                memory_files,
                project_root=BASE_DIR,
                ignore_env_var=RULE_IGNORE_ENV,
            )
        )

        success = False
        run_result: RunResult | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"\n🔄 [重試 {attempt}/{MAX_RETRIES}] {item_line[:60]}")
                await asyncio.sleep(COOLDOWN_S)
            success, run_result, retryable = await run_one_item(
                client=client,
                cost_guard=cost_guard,
                section_title=section_title,
                item_line=item_line,
                section_context=section_context,
                sop_text=sop_text,
                todo_text=todo_text,
                handoff_text=handoff_text,
                memory_block=memory_block,
                mcp_servers=mcp_servers_payload or None,
                skills_catalog=skills_catalog,
                tools=active_tools,
            )
            if success:
                break
            if not retryable:
                # Phase 5: structural failures (max_tokens / max_iterations
                # / over-budget) do NOT retry — same prompt produces same
                # outcome, just burns more tokens. Move on to next item.
                print(
                    f"\n⏭️ [跳過重試] 失敗類型對重試無 ROI（同樣 prompt → "
                    f"同樣結果）。直接進下一項。"
                )
                break

        if success:
            completed += 1
        else:
            failed += 1
            skipped.append(f"[{section_title}] {item_line[:80]}")
            _mark_item_failed(item_line)

            # Recompute cumulative for the stop-reason block (Phase 7).
            current_total = sum(
                est.cost_usd_estimated
                for est in cost_store._estimates.values()  # noqa: SLF001
            )

            if FAIL_BEHAVIOR == "stop":
                # Phase 7: write structured stop reason + clean exit so
                # operator can decide next step (manual fix / subscription
                # CLI / decompose TODO / etc) without burning more $ on
                # downstream items whose preconditions may be unmet.
                if run_result is not None:
                    reason = (
                        f"stop_reason={run_result.stop_reason}"
                        + (
                            f" / 重試 {MAX_RETRIES} 次仍失敗"
                            if run_result.stop_reason
                            in {"end_turn"}  # generic fail, exhausted retries
                            else ""
                        )
                    )
                else:
                    reason = "系統錯誤（API exception）"

                in_section, other = _collect_remaining_pending(
                    current_section_title=section_title,
                    current_item_line=item_line,
                )
                _write_handoff_stop_block(
                    section_title=section_title,
                    item_line=item_line,
                    failure_reason=reason,
                    cumulative_usd=current_total,
                    completed_count=completed,
                    remaining_in_section=in_section,
                    remaining_other_sections=other,
                )
                print(
                    f"\n🛑 [停工] FAIL_BEHAVIOR=stop。"
                    f"等待人工介入，看 HANDOFF.md 最新區塊了解下一步。"
                )
                cumulative_usd = current_total
                break  # exit the while-loop cleanly
            else:
                # FAIL_BEHAVIOR=continue: original best-effort batch behaviour
                print(
                    f"\n⏭️ [跳過] 失敗已標 [!]，FAIL_BEHAVIOR=continue 進下一項。"
                )

        # Recompute cumulative so we have an honest figure for budget gate.
        cumulative_usd = sum(
            est.cost_usd_estimated
            for est in cost_store._estimates.values()  # noqa: SLF001
        )
        print(f"\n💰 [累計花費] {_format_usd(cumulative_usd)}")

        if DAILY_BUDGET_USD > 0 and cumulative_usd >= DAILY_BUDGET_USD:
            print(
                f"\n🛑 [預算上限] 累計 {_format_usd(cumulative_usd)} "
                f"≥ daily cap {_format_usd(DAILY_BUDGET_USD)}，停止流水線。"
            )
            break

        if _shutdown_requested:
            print("\n🛑 [優雅停機完成] 當前任務已結束，流水線安全停止。")
            break

        print(f"\n⏳ 冷卻 {COOLDOWN_S}s 後執行下一項...")
        await asyncio.sleep(COOLDOWN_S)

    total_elapsed = time.time() - pipeline_start
    print(f"\n🎉 [流水線結束]")
    print(f"📊 統計：完成 {completed} / 失敗跳過 {failed}")
    print(f"💰 總花費：{_format_usd(cumulative_usd)}")
    print(f"⏱️ 流水線總耗時：{_fmt_duration(total_elapsed)}")
    if skipped:
        print("⚠️ 跳過的項目：")
        for s in skipped:
            print(f"   - {s}")


if __name__ == "__main__":
    asyncio.run(main())
