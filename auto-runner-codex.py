"""auto-runner-codex.py — Codex (OpenAI GPT-class) subscription CLI runner.

Sibling of auto-runner.py (Claude Code subscription) and
auto-runner-sdk.py (Anthropic API). Drives OpenAI's `codex` CLI through
the same TODO / HANDOFF / SOP contract, with codex-specific differences
captured in coordination.md and AGENTS.md:

  * TODO marker uses ``[x][G]`` / ``[!][G]`` etc. (G = GPT/Codex).
  * HANDOFF entries are headed ``## [Codex/GPT-5.5]``.
  * Commit messages add a ``[Tier-A]`` or ``[Tier-B]`` line before the
    Co-Authored-By trailers.
  * Tier B tasks run from the ``codex-work`` worktree to keep them off
    main until human review.

Usage (interactive sub):

    # Default Tier B (worktree). RUNNER_FILTER scopes to a TODO section.
    OMNISIGHT_CODEX_FILTER=FS python3 auto-runner-codex.py

    # Tier A (rare — pattern-replication tasks pre-approved by human).
    OMNISIGHT_CODEX_TIER=A OMNISIGHT_CODEX_FILTER=BP.D.7 python3 auto-runner-codex.py

    # Force a specific item (dry-run pattern same as auto-runner-sdk).
    OMNISIGHT_CODEX_TARGET_ITEM='FS.4.1 Resend' python3 auto-runner-codex.py

Companion docs:

  * AGENTS.md            — Codex's L1 rule layer (must be present)
  * coordination.md      — section ownership + Tier rules + worktree layout
  * docs/operations/codex-collaboration.md — operator how-to
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# Make `from backend...` imports resolve when invoked from project root.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from backend.agents.project_memory import (  # noqa: E402
    load_all_memory,
    parse_ignored_paths,
    render_operator_summary as render_memory_operator_summary,
)

# ── 優雅停機 ──
_shutdown_requested = False
_ctrl_c_count = 0


def _sigint_handler(signum, frame):
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


# ── 環境路徑 ──

# This script lives at the project root; resolve to that.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TODO_FILE = os.path.join(BASE_DIR, "TODO.md")
HANDOFF_FILE = os.path.join(BASE_DIR, "HANDOFF.md")
SOP_FILE = os.path.join(BASE_DIR, "docs", "sop", "implement_phase_step.md")
AGENTS_FILE = os.path.join(BASE_DIR, "AGENTS.md")
COORDINATION_FILE = os.path.join(BASE_DIR, "coordination.md")
# Operator UI for WP.5.7: omit specific rule files without editing repo
# files. Relative paths resolve against BASE_DIR; ~/ resolves to home.
RULE_IGNORE_ENV = "OMNISIGHT_RULE_IGNORE"
RULE_IGNORE_PATHS = parse_ignored_paths(
    os.environ.get(RULE_IGNORE_ENV),
    project_root=Path(BASE_DIR),
)

# Worktree path for Tier B work (created via `git worktree add`).
WORKTREE_DIR = os.environ.get(
    "OMNISIGHT_CODEX_WORKTREE",
    os.path.normpath(os.path.join(BASE_DIR, "..", "OmniSight-codex-worktree")),
)


# ── Live repo state probes (governance lesson #3 mitigation, 2026-05-05) ──
#
# Codex has shipped 2 alembic regressions in 2 consecutive runs (BP.Q
# revision label "0186" + WP.7 broken chain `down_revision="0186"`).
# Root cause: TODO.md drafts often contain stale alembic numbers from
# when the epic was written months ago. Codex treats those literals as
# authoritative instead of probing the live filesystem.
#
# Mitigation: scan ``backend/alembic/versions/`` at runner startup and
# inject "current head + next free number + chain rule" into every
# codex prompt. Codex sees the truth even if TODO.md is stale.
#
# Probe runs against BASE_DIR (master/main worktree) not WORK_CWD; for
# Tier B the codex worktree may have been ahead-of-main mid-epic but
# alembic head should be derived from main since that's the canonical
# chain target.
def _current_alembic_head() -> tuple[str, str] | None:
    """Return ``(current_head, next_free)`` as 4-digit zero-padded
    strings, or None if the alembic directory cannot be scanned.

    Scans only filenames matching ``NNNN_*.py`` — does NOT parse
    ``revision = "..."`` inside files (the file-name convention is
    enforced by every existing migration in the tree, and parsing
    contents would mean importing files which has side-effects in
    some alembic stacks).
    """
    versions_dir = Path(BASE_DIR) / "backend" / "alembic" / "versions"
    if not versions_dir.is_dir():
        return None
    nums: list[int] = []
    for entry in versions_dir.iterdir():
        name = entry.name
        if not name.endswith(".py"):
            continue
        head = name[:4]
        if head.isdigit():
            nums.append(int(head))
    if not nums:
        return None
    current = max(nums)
    return f"{current:04d}", f"{current + 1:04d}"


# ── 可調參數 ──
TASK_TIMEOUT_S = int(os.environ.get("OMNISIGHT_CODEX_TIMEOUT_S", "1800"))
MAX_RETRIES = int(os.environ.get("OMNISIGHT_CODEX_MAX_RETRIES", "2"))
COOLDOWN_S = int(os.environ.get("OMNISIGHT_CODEX_COOLDOWN", "5"))
SECTION_COOLDOWN_S = int(os.environ.get("OMNISIGHT_CODEX_SECTION_COOLDOWN", "10"))

# codex CLI invocation — overridable for new versions / system installs.
# Default model alias; codex-cli respects --model on each invocation.
CODEX_BIN = os.environ.get("OMNISIGHT_CODEX_BIN", "codex")
CODEX_MODEL = os.environ.get("OMNISIGHT_CODEX_MODEL", "")  # empty = use codex default
# Approval mode: --yolo gives full-access auto (matches the "no permission
# prompts" semantics auto-runner.py uses for `claude -p`). Operators who
# want stricter behaviour can set OMNISIGHT_CODEX_APPROVAL=auto or "" to
# disable, and supply their own flags via OMNISIGHT_CODEX_EXTRA_FLAGS.
CODEX_APPROVAL = os.environ.get("OMNISIGHT_CODEX_APPROVAL", "yolo")
CODEX_EXTRA_FLAGS = os.environ.get("OMNISIGHT_CODEX_EXTRA_FLAGS", "").strip()


# ── Tier 與 worktree 路徑解析 ──

# Tier:
#   B (default) → cwd = worktree (codex-work branch), keeps changes off main.
#   A           → cwd = main checkout (main). Caller must have pre-vetted
#                 the task as Tier A per coordination.md.
TIER = os.environ.get("OMNISIGHT_CODEX_TIER", "B").strip().upper()
if TIER not in {"A", "B"}:
    print(f"⚠️ OMNISIGHT_CODEX_TIER={TIER!r} 不合法 (A|B)，回退到 'B'")
    TIER = "B"


def _resolve_cwd_for_tier(tier: str) -> str:
    if tier == "A":
        return BASE_DIR
    if not os.path.isdir(WORKTREE_DIR):
        print(
            f"❌ Tier B 需要 worktree 但找不到 {WORKTREE_DIR}\n"
            "   先用以下指令建立：\n"
            f"   git -C {BASE_DIR} branch codex-work main\n"
            f"   git -C {BASE_DIR} worktree add {WORKTREE_DIR} codex-work"
        )
        sys.exit(1)
    return WORKTREE_DIR


WORK_CWD = _resolve_cwd_for_tier(TIER)


# ── Track filter（同 auto-runner / auto-runner-sdk 的語意）──

RUNNER_FILTER_RAW = os.environ.get("OMNISIGHT_CODEX_FILTER", "").strip()
RUNNER_FILTER = (
    {p.strip().upper() for p in RUNNER_FILTER_RAW.split(",") if p.strip()}
    if RUNNER_FILTER_RAW
    else set()
)

# Lock to a single item by substring match (same shape as
# auto-runner-sdk.OMNISIGHT_SDK_TARGET_ITEM).
TARGET_ITEM_SUBSTR = os.environ.get("OMNISIGHT_CODEX_TARGET_ITEM", "").strip()


def _section_matches_filter(section_title: str) -> bool:
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
            rest = section_id[len(f):]
            if rest and rest[0].isdigit():
                return True
    return False


# ── TODO 掃描（codex 認 [G] suffix 為自己的、留 [C] 給 Claude）──


def get_next_pending_item():
    """Find next ``- [ ]`` line that isn't already tagged for the other agent.

    A line marked ``- [ ] item description`` (no agent tag yet) is fair
    game. A line tagged ``- [x][C]`` or ``- [!][C]`` etc. is Claude's
    business and we leave it alone; same for ``[x][G]`` etc. on the
    Codex side (already-done by previous run).
    """
    if not os.path.exists(TODO_FILE):
        print(f"❌ 找不到 {TODO_FILE} 檔案！")
        sys.exit(1)

    with open(TODO_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_section = None
    section_lines: list[str] = []

    for line in lines:
        if line.startswith("### "):
            if current_section and _section_matches_filter(current_section):
                hit = _find_first_pending(section_lines)
                if hit:
                    return current_section, hit, "".join(section_lines)
            current_section = line.strip()
            section_lines = []
            continue

        if line.startswith("## "):
            if current_section and _section_matches_filter(current_section):
                hit = _find_first_pending(section_lines)
                if hit:
                    return current_section, hit, "".join(section_lines)
            current_section = None
            section_lines = []
            continue

        if current_section is not None:
            section_lines.append(line)

    if current_section and _section_matches_filter(current_section):
        hit = _find_first_pending(section_lines)
        if hit:
            return current_section, hit, "".join(section_lines)

    return None, None, None


_PENDING_RE = re.compile(r"^\s*-\s*\[ \]\s+(.*)$")


def _find_first_pending(lines: list[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        # A pending bullet is `- [ ] ...` with NO agent tag yet.
        # `- [~][G]` (reserved by codex), `- [x][G]` (done), `- [!][G]`
        # (failed) all start with something other than `- [ ]` and are
        # therefore correctly skipped. Same for `[C]` Claude-tagged.
        if stripped.startswith("- [ ]"):
            if TARGET_ITEM_SUBSTR and TARGET_ITEM_SUBSTR not in stripped:
                continue
            return stripped
    return None


# ── Runner-managed TODO marker contract (Tier B fix, 2026-05-03) ──
#
# Why the runner — not codex — owns the TODO marker:
#
# Tier B runs codex in a separate worktree on `codex-work` branch. That
# worktree has its OWN TODO.md (a snapshot at the branch HEAD), distinct
# from the main checkout's TODO.md. If codex updates the worktree's
# TODO.md and commits, those changes land on `codex-work`, NOT on
# main. The runner reads main/TODO.md every iteration → never sees
# the [G] marker codex wrote in the worktree → infinite loop dispatching
# the same already-completed item.
#
# Fix: runner takes full ownership of main/TODO.md marker writes.
#   * BEFORE dispatching, runner reserves the item with `- [~][G]` so
#     parallel runners (Claude or other codex instances) see it claimed
#     and skip it.
#   * AFTER codex returns success, runner flips `- [~][G]` → `- [x][G]`.
#   * AFTER codex returns failure, runner flips `- [~][G]` → `- [!][G]`.
#
# Codex's prompt is updated to TELL it not to touch TODO.md at all in
# Tier B mode. Code commits on `codex-work` are the work product;
# main/TODO.md is the coordination state.


def _replace_marker_in_master_todo(
    item_line: str,
    new_prefix: str,
    *,
    label: str,
) -> str | None:
    """Replace the leading ``- [X]...`` of a single line in main TODO.

    Returns the new line on success, None on failure / no match. Caller
    typically uses the returned string as the canonical reference for
    later flips (``[~][G]`` → ``[x][G]`` etc.).
    """
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"⚠️ 讀 main TODO 失敗：{e}")
        return None

    if item_line not in content:
        # Could happen if a parallel runner already mutated this line.
        # Don't try to recover — just log and bail.
        print(
            f"⚠️ 在 main TODO 找不到原始 marker（可能已被其他 runner 改動）：\n"
            f"   {item_line[:80]}"
        )
        return None

    # item_line starts with one of: "- [ ]", "- [~][G]", "- [x][G]", "- [!][G]".
    # Replace the leading marker with `new_prefix`.
    for old_prefix in ("- [ ]", "- [~][G]", "- [x][G]", "- [!][G]"):
        if item_line.startswith(old_prefix):
            new_line = new_prefix + item_line[len(old_prefix):]
            break
    else:
        print(f"⚠️ 無法解析的 marker prefix：{item_line[:40]}")
        return None

    if new_line == item_line:
        return new_line  # idempotent no-op
    new_content = content.replace(item_line, new_line, 1)
    try:
        with open(TODO_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        print(f"⚠️ 寫 main TODO 失敗：{e}")
        return None
    print(f"📝 [{label}] {new_prefix} ← {item_line[:60]}")
    return new_line


def _reserve_item_for_codex(item_line: str) -> str | None:
    """Pre-flight: flip ``- [ ]`` → ``- [~][G]`` so parallel runners skip it."""
    return _replace_marker_in_master_todo(item_line, "- [~][G]", label="reserved")


def _flip_reservation_to_done(reserved_line: str) -> str | None:
    """Post-success: flip ``- [~][G]`` → ``- [x][G]``."""
    return _replace_marker_in_master_todo(reserved_line, "- [x][G]", label="done")


def _flip_reservation_to_failed(reserved_line: str) -> str | None:
    """Post-failure: flip ``- [~][G]`` → ``- [!][G]``."""
    return _replace_marker_in_master_todo(reserved_line, "- [!][G]", label="failed")


# ── codex 命令組 ──


def _build_codex_command() -> list[str]:
    """Build the codex exec invocation."""
    cmd = [CODEX_BIN, "exec", "--cd", WORK_CWD]
    # --yolo (full-auto) is the codex equivalent of claude's
    # --dangerously-skip-permissions. Skip if operator explicitly disabled.
    if CODEX_APPROVAL.lower() == "yolo":
        cmd.append("--yolo")
    elif CODEX_APPROVAL.lower() and CODEX_APPROVAL.lower() != "":
        cmd.extend(["--approval-mode", CODEX_APPROVAL])
    if CODEX_MODEL:
        cmd.extend(["--model", CODEX_MODEL])
    if CODEX_EXTRA_FLAGS:
        cmd.extend(CODEX_EXTRA_FLAGS.split())
    return cmd


# ── Item runner ──


def run_codex_item(section_title: str, item_line: str, section_context: str) -> bool:
    print(f"\n{'=' * 60}")
    print(f"🚀 [自動調度] 區塊: {section_title}")
    print(
        f"📌 [執行項目] {item_line[:80]}{'...' if len(item_line) > 80 else ''}"
    )
    print(f"🪧 [Tier] {TIER} (cwd={WORK_CWD})")
    print(f"{'=' * 60}\n")

    # codex's prompt mirrors auto-runner.py + adds codex-specific
    # discipline reminders that AGENTS.md spells out in detail.

    # Live-state injection (governance lesson #3, 2026-05-05): probe
    # alembic head from the filesystem so codex doesn't trust stale
    # numbers in TODO.md drafts. Probe is best-effort — if the scan
    # fails (no versions/ dir, etc.), we degrade silently and codex
    # falls back to its own discovery; that's safer than blocking.
    alembic_state = _current_alembic_head()
    live_state_block = ""
    if alembic_state is not None:
        current_head, next_free = alembic_state
        live_state_block = f"""

【🔴 LIVE REPO STATE — auto-injected, takes precedence over TODO.md drafts】
Current alembic head (scanned from `ls backend/alembic/versions/`): **{current_head}**
Next free migration number: **{next_free}**

If your task creates a new alembic migration, USE `revision = "{next_free}"`
with `down_revision = "{current_head}"`. **Ignore any literal alembic
number written in the TODO.md item description** — those drafts were
authored months ago and carry stale numbering (e.g. TODO might say
"alembic 0118" but the live head is {current_head}, so the next free
number is {next_free}, NOT 0118 and NOT 0186 and NOT whatever TODO says).

If your task does NOT touch alembic, ignore this block.
"""

    prompt = f"""你現在是 OpenAI Codex (codex-cli) 在 OmniSight-Productizer 專案中
進行「全自動化無人值守」開發。**這個專案有兩個 LLM 並行協作**：你（Codex）跟
Claude (Opus)。協作規則在 {COORDINATION_FILE}，你的特定規則在 {AGENTS_FILE}。
{live_state_block}
**你只需要完成以下【單一項目】，不要做其他項目：**

➤ {item_line}

此項目屬於以下區塊（僅供上下文參考，不要執行其他項目）：
{section_title}
{section_context}

【⚙️ 嚴格執行準則 — 請打開並遵守 AGENTS.md 的所有規則】：
1. **最高指導原則**：在進行任何思考與修改前，請務必先讀取並嚴格遵守
   {AGENTS_FILE} 與 {SOP_FILE} 兩份文件中的所有規則。
2. **只完成上方標記 ➤ 的那一個項目**。其他項目不要動。
3. **嚴守 AGENTS.md Rule 1 (先抄、後問、不發明)**：找專案內最接近的既有
   pattern，鏡像它。如果你覺得你有更好的設計，**不要實作**，把建議寫進
   commit message 末尾的 `<!-- codex-suggestion: ... -->`。
4. **嚴守 AGENTS.md Rule 2 (scope 紀律)**：只做指定的，不做順便的。發現的
   無關 bug 或改進機會 → 寫進 HANDOFF.md `[codex-found]: ...`，繼續做你
   被指定的事。
5. **嚴守 AGENTS.md Rule 3 (不確定就退)**：不確定就做更少、不要做更多；
   不確定 pattern 就抄既有的、不要發明；真的卡住就在 HANDOFF.md 寫
   `[codex-blocked]: ...` 並停止。
6. 這是真實執行階段，請直接讀寫檔案、修改程式碼、建立資料夾或執行必要指令。
7. 如果遇到缺少的檔案，請參考專案上下文自行推導並建立。
8. **【TODO.md 寫入鐵律 — Tier B 重要】**：在 Tier B (worktree) 模式下，你
   **完全不要改動 TODO.md**。原因：你的 worktree 跟 main 是不同 working
   tree，TODO.md 的 main 副本由 runner 統一管理。runner 已經在你開工前
   把那行從 `- [ ]` 標成 `- [~][G]`（reserved），完工後 runner 會根據你
   的 exit code 自動翻成 `- [x][G]`（成功）或 `- [!][G]`（失敗）。
   - **不要在 worktree 內 edit TODO.md**（你改的是 codex-work 那份的副本，
     完全沒被 runner 看見，會造成過去版本的「無限 loop bug」復發）
   - **不要把 TODO.md 加進你的 commit**
   - 唯一例外：Tier A 模式時 (cwd 為 main)，你才照舊 `- [ ]` → `- [x][G]`
     自己標。
9. **HANDOFF.md 寫入規範**：HANDOFF.md 你**可以**寫，因為它在 worktree
   內你寫進去的內容會跟你的 code 一起 commit 到 codex-work，後續 merge 時
   一併進入 main。你寫的條目 heading 必須以 **`## [Codex/GPT-5.5]`** 開頭，
   後面跟日期跟 item ID。例：`## [Codex/GPT-5.5] 2026-05-02 FS.4.1 完工`。
   **不要改動 Claude 寫的條目** (heading 是 `## [Claude/Opus]`)。
10. 更新完後，請務必將更動後的內容 commit 到 Git。**commit message 末尾必須
    在 Co-Authored-By trailers 之前加一行 Tier marker**：
       [Tier-{TIER}]
    然後加三行 Co-Authored-By：
       Co-Authored-By: GPT-5.5 (codex-cli) <noreply@openai.com>
       Co-Authored-By: <env git user> ← 用 `git config user.name` / `user.email`
       Co-Authored-By: <global git user> ← 用 `git config --global user.name` / `user.email`
11. 絕對不要詢問我任何問題或要求人類確認（你已擁有 --yolo 權限）。
12. 完成後，直接輸出「✅ 項目完成」並結束。

**不要做這些（會搞砸協作）**：
  * 改動 TODO.md（Tier B 模式 runner 管，見上面鐵律 8）
  * 改動標記為 `[C]` 的 TODO 條目（那是 Claude 的工作 — Tier A 也禁）
  * 改動 heading 為 `[Claude/Opus]` 的 HANDOFF 條目
  * 改動 CLAUDE.md 或 docs/operations/runner-strategy.md（Claude 主管）
  * 改動 coordination.md 或 AGENTS.md（這是規則文件，需人類同意）
  * 為了「順便也做了 X」擴大 commit scope
"""

    cmd = _build_codex_command()
    print(f"💬 [codex cmd] {' '.join(cmd)}")

    start_time = time.time()
    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            start_new_session=True,
        )
        process.communicate(input=prompt, timeout=TASK_TIMEOUT_S)
        exit_code = process.returncode
        elapsed = time.time() - start_time

        if exit_code == 0:
            print(f"\n✅ [項目完成] {item_line[:60]}")
            print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
            return True
        else:
            print(f"\n❌ [項目異常] Exit Code: {exit_code}")
            print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
            return False

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        print(f"\n⏰ [超時] 項目執行超過 {TASK_TIMEOUT_S}s，強制終止。")
        print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
        if process is not None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
        return False
    except FileNotFoundError:
        print(
            f"\n❌ [系統錯誤] 找不到 codex CLI ({CODEX_BIN!r}). 安裝指引：\n"
            "   npm install -g @openai/codex   或   brew install --cask codex\n"
            "   裝完跑 `codex` → Sign in with ChatGPT (Plus/Pro/Business 訂閱)"
        )
        return False
    except Exception as e:  # noqa: BLE001
        elapsed = time.time() - start_time
        print(f"\n❌ [系統錯誤] {type(e).__name__}: {e}")
        print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
        return False


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s ({seconds:.1f}s)"
    return f"{s}s ({seconds:.1f}s)"


# ── Main ──


def main() -> None:
    print("🤖 OmniSight-Productizer Codex (GPT-class) 流水線啟動 (subscription CLI)")
    print(
        f"⚙️ 設定：codex_bin={CODEX_BIN} model={CODEX_MODEL or '<codex default>'} "
        f"approval={CODEX_APPROVAL or '<default>'} timeout={TASK_TIMEOUT_S}s "
        f"retries={MAX_RETRIES}"
    )
    print(f"🪧 Tier: {TIER}")
    print(f"📂 Working dir: {WORK_CWD}")
    if RUNNER_FILTER:
        print(
            f"🏷️ Track filter：只處理 {', '.join(sorted(RUNNER_FILTER))} 系列"
        )
    else:
        print("🏷️ Track filter：無（處理所有 [G]-eligible 項目）")
    if TARGET_ITEM_SUBSTR:
        print(f"🎯 Target item lock: substring={TARGET_ITEM_SUBSTR!r}")
    memory_files = load_all_memory(Path(BASE_DIR), ignored_paths=RULE_IGNORE_PATHS)
    print(
        render_memory_operator_summary(
            memory_files,
            project_root=Path(BASE_DIR),
            ignore_env_var=RULE_IGNORE_ENV,
        )
    )
    print(
        "⚠️ 警告：codex 將以 --yolo 模式執行 shell + 檔案編輯，按 Ctrl+C 可隨時中斷。\n"
    )

    # Sanity checks before going hot.
    if not os.path.exists(SOP_FILE):
        print(f"❌ 找不到 SOP {SOP_FILE}")
        sys.exit(1)
    if not os.path.exists(AGENTS_FILE):
        print(f"❌ 找不到 AGENTS.md ({AGENTS_FILE}) — codex 必讀文件")
        sys.exit(1)
    if not os.path.exists(COORDINATION_FILE):
        print(f"❌ 找不到 coordination.md ({COORDINATION_FILE}) — 協作規則必讀")
        sys.exit(1)
    if not shutil.which(CODEX_BIN):
        print(
            f"❌ 找不到 codex CLI ({CODEX_BIN!r}) on PATH. 安裝：\n"
            "   npm install -g @openai/codex   或   brew install --cask codex"
        )
        sys.exit(1)

    pipeline_start = time.time()
    completed_count = 0
    failed_count = 0
    skipped_items: list[str] = []
    last_section = None

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
        memory_files = load_all_memory(Path(BASE_DIR), ignored_paths=RULE_IGNORE_PATHS)
        print(
            render_memory_operator_summary(
                memory_files,
                project_root=Path(BASE_DIR),
                ignore_env_var=RULE_IGNORE_ENV,
            )
        )

        # Tier B fix (2026-05-03): runner OWNS main/TODO.md marker.
        # Reserve the item with [~][G] BEFORE dispatching codex so:
        #   (a) the next iteration of THIS runner won't re-pick it (the
        #       infinite-loop bug that wasted hours on FS.1.1 before)
        #   (b) parallel Claude runners reading the same main/TODO see
        #       it claimed and skip
        # Codex's prompt (Rule 8) tells it NOT to touch TODO.md in
        # Tier B mode — so changes made in worktree's TODO.md are
        # invisible-but-harmless; the main copy is the truth.
        if TIER == "B":
            reserved_line = _reserve_item_for_codex(item_line)
            if reserved_line is None:
                # Could not reserve — likely raced with another runner.
                # Skip this iteration; next scan will skip the now-tagged
                # item naturally.
                print("⚠️ 無法 reserve item（可能被其他 runner 改了），跳下一輪")
                time.sleep(COOLDOWN_S)
                continue
        else:
            # Tier A: codex runs in main; old contract — codex owns
            # TODO marker itself.
            reserved_line = item_line

        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"\n🔄 [重試 {attempt}/{MAX_RETRIES}] {item_line[:60]}")
                time.sleep(COOLDOWN_S)
            success = run_codex_item(section_title, item_line, section_context)
            if success:
                break

        if success:
            completed_count += 1
            if TIER == "B":
                _flip_reservation_to_done(reserved_line)
        else:
            failed_count += 1
            skipped_items.append(f"[{section_title}] {item_line[:80]}")
            print(
                f"\n⏭️ [跳過] 重試 {MAX_RETRIES} 次仍失敗，跳過此項目繼續下一個。"
            )
            if TIER == "B":
                _flip_reservation_to_failed(reserved_line)
            else:
                # Tier A still uses the legacy direct-mark behaviour.
                _replace_marker_in_master_todo(item_line, "- [!][G]", label="failed")

        if _shutdown_requested:
            break

        print(f"\n⏳ 冷卻 {COOLDOWN_S}s 後執行下一項...")
        time.sleep(COOLDOWN_S)

    total_elapsed = time.time() - pipeline_start
    print("\n🎉 [流水線結束]")
    print(f"📊 統計：完成 {completed_count} / 失敗跳過 {failed_count}")
    print(f"⏱️ 總耗時：{_fmt_duration(total_elapsed)}")
    if skipped_items:
        print("⚠️ 跳過的項目：")
        for s in skipped_items:
            print(f"   - {s}")


if __name__ == "__main__":
    main()
