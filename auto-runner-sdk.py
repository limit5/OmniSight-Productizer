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
from backend.agents.runner_handlers import make_runner_dispatcher  # noqa: E402


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

MODEL_NAME = os.environ.get("OMNISIGHT_SDK_MODEL", DEFAULT_MODEL_OPUS)
MAX_ITERATIONS = int(os.environ.get("OMNISIGHT_SDK_MAX_ITERATIONS", "80"))
MAX_TOKENS = int(os.environ.get("OMNISIGHT_SDK_MAX_TOKENS", "16000"))
MAX_RETRIES = int(os.environ.get("OMNISIGHT_SDK_MAX_RETRIES", "2"))
COOLDOWN_S = int(os.environ.get("OMNISIGHT_SDK_COOLDOWN", "5"))
SECTION_COOLDOWN_S = int(os.environ.get("OMNISIGHT_SDK_SECTION_COOLDOWN", "10"))
DAILY_BUDGET_USD = float(os.environ.get("OMNISIGHT_SDK_DAILY_BUDGET", "0") or 0)

# Tools the agent loop is allowed to call. Wired in runner_handlers.
RUNNER_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Grep", "Glob"]


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
) -> tuple[bool, RunResult | None]:
    """Drive Claude through one TODO item. Returns (success, run_result)."""
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
    system_text = (
        f"# 專案 SOP\n{sop_text}\n\n"
        "# 可用上下文檔案\n"
        "- `TODO.md`（專案根目錄）— 全部任務清單。當前任務的區塊已經放在你的 user prompt 內。\n"
        "  若需要查其他區塊（例如 cross-reference 到上下游 task），用 Read tool 開檔。\n"
        "- `HANDOFF.md`（專案根目錄）— 過往 task 的詳細交接記錄。預設**不要全讀**（檔案可能上萬行）。\n"
        "  若你的任務明確需要參考 prior context，用 Read tool 配合 offset/limit 撈相關段落即可。\n"
        "- 專案 source code — 用 Read / Grep / Glob 探索。\n"
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
        "可用工具：Read / Write / Edit / Bash / Grep / Glob — 路徑限專案根目錄之下。\n"
    )

    started = time.time()
    try:
        result = await client.run_with_tools(
            prompt=prompt,
            tools=RUNNER_TOOLS,
            system=system_text,
            model=MODEL_NAME,
            max_tokens=MAX_TOKENS,
            max_iterations=MAX_ITERATIONS,
            enable_cache=True,
            on_tool_call="log",
        )
    except Exception as e:  # noqa: BLE001 - external boundary
        elapsed = time.time() - started
        print(f"\n❌ [系統錯誤] {type(e).__name__}: {e}")
        print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
        return False, None

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

    success = (
        result.stop_reason == "end_turn"
        and "✅ 項目完成" in result.final_text
    )
    if success:
        print(f"\n✅ [項目完成] {item_line[:60]}")
    elif result.stop_reason == "max_iterations_exceeded":
        print(
            f"\n⚠️ [iterations 用盡] {MAX_ITERATIONS} 輪未收 ✅，視為失敗"
        )
    else:
        print("\n❌ [項目異常] 未收到 ✅ 項目完成 標記")
    return success, result


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
    print(
        "⚠️ 警告：系統將自動執行程式碼與系統指令，按 Ctrl+C 可隨時中斷。\n"
    )

    if not SOP_FILE.exists():
        print(f"⚠️ [警告] 找不到指定的 SOP 檔案！\n   {SOP_FILE}")
        sys.exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY 未設定。請先在 .env 或環境設定後重試。")
        sys.exit(1)

    dispatcher = make_runner_dispatcher()
    client = AnthropicClient(
        default_model=MODEL_NAME,
        max_tokens_default=MAX_TOKENS,
        dispatcher=dispatcher,
    )
    cost_store = InMemoryCostStore()
    cost_guard = CostGuard(store=cost_store)

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
        try:
            sop_text = SOP_FILE.read_text(encoding="utf-8")
            todo_text = TODO_FILE.read_text(encoding="utf-8")
            handoff_text = (
                HANDOFF_FILE.read_text(encoding="utf-8")
                if HANDOFF_FILE.exists()
                else "(HANDOFF.md not yet created)"
            )
        except OSError as e:
            print(f"❌ 讀取 SOP/TODO/HANDOFF 失敗: {e}")
            sys.exit(1)

        success = False
        run_result: RunResult | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"\n🔄 [重試 {attempt}/{MAX_RETRIES}] {item_line[:60]}")
                await asyncio.sleep(COOLDOWN_S)
            success, run_result = await run_one_item(
                client=client,
                cost_guard=cost_guard,
                section_title=section_title,
                item_line=item_line,
                section_context=section_context,
                sop_text=sop_text,
                todo_text=todo_text,
                handoff_text=handoff_text,
            )
            if success:
                break

        if success:
            completed += 1
        else:
            failed += 1
            skipped.append(f"[{section_title}] {item_line[:80]}")
            print(
                f"\n⏭️ [跳過] 重試 {MAX_RETRIES} 次仍失敗，跳過此項目繼續下一個。"
            )
            _mark_item_failed(item_line)

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
