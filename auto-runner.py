import os
import sys
import signal
import subprocess
import time

# ── 優雅停機 ──
_shutdown_requested = False
_ctrl_c_count = 0


def _sigint_handler(signum, frame):
    """
    第一次 Ctrl+C：設 flag，等當前任務完成後停止。
    第二次 Ctrl+C：強制立即終止（緊急用）。
    """
    global _shutdown_requested, _ctrl_c_count
    _ctrl_c_count += 1
    if _ctrl_c_count == 1:
        _shutdown_requested = True
        print("\n\n🛑 [優雅停機] 收到 Ctrl+C，等待當前任務完成後停止流水線...")
        print("   (再按一次 Ctrl+C 強制立即終止)\n")
    else:
        print("\n\n💥 [強制終止] 收到第二次 Ctrl+C，立即停止。")
        sys.exit(1)


signal.signal(signal.SIGINT, _sigint_handler)

# 1. 動態取得 auto_runner.py 所在的資料夾絕對路徑（也就是您的專案根目錄）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 2. 透過 os.path.join 安全地組合出絕對路徑
TODO_FILE = os.path.join(BASE_DIR, "TODO.md")
HANDOFF_FILE = os.path.join(BASE_DIR, "HANDOFF.md")
SOP_FILE = os.path.join(BASE_DIR, "docs", "sop", "implement_phase_step.md")

# ── 可調參數 ──
TASK_TIMEOUT_S = 1800       # 單一項目最長執行時間（30 分鐘）
MAX_RETRIES = 2             # 同一項目最多重試次數
COOLDOWN_S = 5              # 項目間冷卻秒數
SECTION_COOLDOWN_S = 10     # 跨 ### 區塊的冷卻秒數

# ── Track filter（平行化用）──
# 設定 OMNISIGHT_RUNNER_FILTER 環境變數來限制此 runner 只處理特定 Priority 系列。
#
# 支援兩種粒度：
#   字母級：  OMNISIGHT_RUNNER_FILTER=L,G,H,R     → 跑 L/G/H/R 所有子項
#   子項級：  OMNISIGHT_RUNNER_FILTER=B13,B14      → 只跑 B13 和 B14
#             OMNISIGHT_RUNNER_FILTER=B13           → 只跑 B13
#             OMNISIGHT_RUNNER_FILTER=S2-0,S2-1     → 只跑 S2-0 和 S2-1
#
# 混用也可以：
#   OMNISIGHT_RUNNER_FILTER=B13,G,H   → B13 + G 系列全部 + H 系列全部
#
# 範例（平行 B13 + B14）：
#   Terminal 1: OMNISIGHT_RUNNER_FILTER=B13 python3 auto-runner.py
#   Terminal 2: OMNISIGHT_RUNNER_FILTER=B14 python3 auto-runner.py
#
# 不設定 = 處理所有項目（預設行為）
RUNNER_FILTER_RAW = os.environ.get("OMNISIGHT_RUNNER_FILTER", "").strip()
RUNNER_FILTER = set(
    p.strip().upper() for p in RUNNER_FILTER_RAW.split(",") if p.strip()
) if RUNNER_FILTER_RAW else set()


def _section_matches_filter(section_title):
    """
    檢查 section 標題是否匹配 RUNNER_FILTER。

    匹配規則（依精確度遞減嘗試）：
      1. 完整子項號碼匹配：「B13」匹配 "### B13. FUI Error..."
      2. 帶連字號的子項：「S2-0」匹配 "### S2-0. API 隱形..."
      3. 字母級匹配：「B」匹配所有 "### B*." sections
      4. 無法解析 → 接受（安全 fallback）
    """
    if not RUNNER_FILTER:
        return True  # 無 filter = 全部接受

    import re
    # 從 "### B13. FUI..." 或 "### S2-0. API..." 提取完整編號
    m = re.match(r"###\s+([A-Za-z]\S*?)\.", section_title)
    if not m:
        return True  # 無法解析 → 接受

    section_id = m.group(1).upper()  # e.g. "B13", "S2-0", "L10", "G1"

    for f in RUNNER_FILTER:
        # 完整匹配：filter="B13" matches section_id="B13"
        if section_id == f:
            return True
        # 前綴匹配：filter="B" matches section_id="B13", "B14", "B2"
        # 但 filter="B1" 只 matches "B1", "B10"-"B19"（不匹配 "B2"）
        if len(f) == 1 and section_id.startswith(f):
            return True

    return False


def get_next_pending_item():
    """
    從 TODO.md 中找出下一個未完成的單一項目。
    回傳 (section_title, item_line, section_context)：
      - section_title: ### 標題（提供上下文）
      - item_line: 第一個 '- [ ]' 的完整行文字
      - section_context: 該 ### 區塊的全部內容（讓 AI 了解上下文，但只做一項）
    如果沒有待辦項目，回傳 (None, None, None)。
    """
    if not os.path.exists(TODO_FILE):
        print(f"❌ 找不到 {TODO_FILE} 檔案！")
        sys.exit(1)

    with open(TODO_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_section = None
    section_lines = []

    for line in lines:
        # 遇到新的 ### 標題
        if line.startswith("### "):
            # 先檢查上一個 section 有沒有未完成項目
            if current_section and _section_matches_filter(current_section):
                first_pending = _find_first_pending(section_lines)
                if first_pending:
                    return current_section, first_pending, "".join(section_lines)

            current_section = line.strip()
            section_lines = []
            continue

        # 遇到 ## 標題（更高層級），結束當前 section
        if line.startswith("## "):
            if current_section and _section_matches_filter(current_section):
                first_pending = _find_first_pending(section_lines)
                if first_pending:
                    return current_section, first_pending, "".join(section_lines)
            current_section = None
            section_lines = []
            continue

        # 累積當前 section 的行
        if current_section is not None:
            section_lines.append(line)

    # 檢查最後一個 section
    if current_section and _section_matches_filter(current_section):
        first_pending = _find_first_pending(section_lines)
        if first_pending:
            return current_section, first_pending, "".join(section_lines)

    return None, None, None


def _find_first_pending(lines):
    """在一組行中找出第一個 '- [ ]' 項目的完整文字。"""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            return stripped
    return None


def run_claude_item(section_title, item_line, section_context):
    """
    呼叫 Claude 執行單一項目，有 timeout 保護。
    """
    print(f"\n{'=' * 60}")
    print(f"🚀 [自動調度] 區塊: {section_title}")
    print(f"📌 [執行項目] {item_line[:80]}{'...' if len(item_line) > 80 else ''}")
    print(f"{'=' * 60}\n")

    prompt = f"""你現在處於「全自動化無人值守」模式。

**你只需要完成以下【單一項目】，不要做其他項目：**

➤ {item_line}

此項目屬於以下區塊（僅供上下文參考，不要執行其他項目）：
{section_title}
{section_context}

【⚙️ 嚴格執行準則】：
1. **最高指導原則：在進行任何思考與修改前，請務必先讀取並嚴格遵守 {SOP_FILE} 檔案中的所有規則。**
2. **只完成上方標記 ➤ 的那一個項目**。其他項目不要動。
3. 這是真實執行階段，請直接讀寫檔案、修改程式碼、建立資料夾或執行必要指令。
4. 如果遇到缺少的檔案，請參考專案上下文自行推導並建立。
5. **【狀態標記鐵律】**：完成後，你「必須」開啟 {TODO_FILE} 進行狀態標記：
   - 若你已由 AI 完成該項目，請將對應的 `- [ ]` 改為 `- [x]`。
   - **若該項目需要人類實體操作 (Operator-blocked)，請將它從 `- [ ]` 改為 `- [O]`。**
   - **只標記你剛完成的那一項，不要改動其他項目。**
6. 請將本次的進度與最新狀態更新至 {HANDOFF_FILE} 中。
7. 更新完後，請務必將更動後的內容commit到 Git，確保版本控制的完整性。
8. 絕對不要詢問我任何問題或要求人類確認（你已經擁有最高權限）。
9. 完成後，直接輸出「✅ 項目完成」並結束。
"""

    command = ["claude", "-p", prompt, "--dangerously-skip-permissions"]

    start_time = time.time()
    try:
        # start_new_session=True 讓 Claude 子程序不會收到父程序的 SIGINT，
        # 這樣 Ctrl+C 只影響 auto-runner，Claude 可以自然完成當前工作。
        process = subprocess.Popen(
            command,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            start_new_session=True,
        )
        exit_code = process.wait(timeout=TASK_TIMEOUT_S)
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
        # 終止整個子程序 session group
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait()
        return False
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"\n❌ [系統錯誤] {e}")
        print(f"⏱️ [耗時] {_fmt_duration(elapsed)}")
        return False


def _fmt_duration(seconds):
    """將秒數格式化為 Xm Ys 或 Xs 的可讀字串。"""
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s ({seconds:.1f}s)"
    return f"{s}s ({seconds:.1f}s)"


def main():
    print("🤖 OmniSight-Productizer 全自動化流水線啟動...")
    print(f"⚙️ 設定：每項 timeout={TASK_TIMEOUT_S}s / 重試={MAX_RETRIES}次 / 冷卻={COOLDOWN_S}s")
    if RUNNER_FILTER:
        print(f"🏷️ Track filter：只處理 {', '.join(sorted(RUNNER_FILTER))} 系列")
    else:
        print("🏷️ Track filter：無（處理所有項目）")
    print("⚠️ 警告：系統將自動執行程式碼與系統指令，按 Ctrl+C 可隨時中斷。\n")
    pipeline_start = time.time()

    # 啟動前檢查 SOP 檔案是否存在
    if not os.path.exists(SOP_FILE):
        print(f"⚠️ [警告] 找不到指定的 SOP 檔案！")
        print(f"🔍 系統正在尋找的絕對路徑為：\n   {SOP_FILE}")
        print("💡 請檢查路徑大小寫是否正確，或者檔案是否真的放在該位置。")
        sys.exit(1)

    completed_count = 0
    failed_count = 0
    skipped_items = []
    last_section = None

    while True:
        section_title, item_line, section_context = get_next_pending_item()

        if not section_title:
            total_elapsed = time.time() - pipeline_start
            print(f"\n🎉 [大功告成] TODO.md 中所有 '- [ ]' 項目皆已處理！")
            print(f"📊 統計：完成 {completed_count} / 失敗跳過 {failed_count}")
            print(f"⏱️ 流水線總耗時：{_fmt_duration(total_elapsed)}")
            if skipped_items:
                print(f"⚠️ 跳過的項目：")
                for s in skipped_items:
                    print(f"   - {s}")
            break

        # 跨區塊時多等一下
        if last_section and last_section != section_title:
            print(f"\n📦 [切換區塊] {last_section[:40]}... → {section_title[:40]}...")
            time.sleep(SECTION_COOLDOWN_S)
        last_section = section_title

        # 重試邏輯
        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                print(f"\n🔄 [重試 {attempt}/{MAX_RETRIES}] {item_line[:60]}")
                time.sleep(COOLDOWN_S)

            success = run_claude_item(section_title, item_line, section_context)
            if success:
                break

        if success:
            completed_count += 1
        else:
            failed_count += 1
            skipped_items.append(f"[{section_title}] {item_line[:80]}")
            print(f"\n⏭️ [跳過] 重試 {MAX_RETRIES} 次仍失敗，跳過此項目繼續下一個。")
            # 把失敗的項目標記為注釋，避免無限重試同一項
            _mark_item_failed(item_line)

        # ── 優雅停機檢查：當前任務已完成，若收到過 Ctrl+C 則在此停下 ──
        if _shutdown_requested:
            total_elapsed = time.time() - pipeline_start
            print(f"\n🛑 [優雅停機完成] 當前任務已結束，流水線安全停止。")
            print(f"📊 統計：完成 {completed_count} / 失敗跳過 {failed_count}")
            print(f"⏱️ 流水線總耗時：{_fmt_duration(total_elapsed)}")
            if skipped_items:
                print(f"⚠️ 跳過的項目：")
                for s in skipped_items:
                    print(f"   - {s}")
            break

        print(f"\n⏳ 冷卻 {COOLDOWN_S}s 後執行下一項...")
        time.sleep(COOLDOWN_S)


def _mark_item_failed(item_line):
    """
    將失敗的項目從 - [ ] 改為 - [!] 避免無限重試。
    人工可稍後檢查 [!] 項目決定重做或標記 [O]。
    """
    try:
        with open(TODO_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        # 只替換第一個出現的（避免同名行誤改）
        original = item_line
        failed_mark = item_line.replace("- [ ]", "- [!]", 1)
        if original != failed_mark:
            content = content.replace(original, failed_mark, 1)
            with open(TODO_FILE, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"📝 已將失敗項目標記為 [!]：{failed_mark[:60]}")
    except Exception as e:
        print(f"⚠️ 標記失敗項目時出錯：{e}")


if __name__ == "__main__":
    main()
