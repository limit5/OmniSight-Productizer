#!/usr/bin/env bash
# Screen Reader testing protocol launcher — W5 a11y skill (#353 B16 Part C row 291).
#
# 目的：提供「結構化」的螢幕閱讀器測試腳本，對照 WCAG 2.2 AA 四階段
# （Setup → Navigation → Interactive → Dynamic Content），
# 產出 `data/a11y-sr-report-<timestamp>.md` 供 PR review 附件。
#
# 支援 driver：
#   - macOS VoiceOver（以 `osascript` 啟動；執行者自己戴耳機跑）
#   - Windows NVDA（以 `nvda.exe --launcher` 啟動；docs/ci 只給 skeleton）
#   - 純文字模式（--dry-run，只產模板不啟 SR，適合遠端 agent）
#
# 用法：
#   scripts/a11y_screen_reader_protocol.sh --url https://localhost:3000/app --dry-run
#   scripts/a11y_screen_reader_protocol.sh --url https://staging/app --driver voiceover
#
set -euo pipefail

URL=""
DRIVER="dry-run"  # voiceover | nvda | dry-run
OUT_DIR="${OUT_DIR:-data}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --driver) DRIVER="$2"; shift 2 ;;
    --dry-run) DRIVER="dry-run"; shift ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$URL" ]]; then
  echo "ERROR: --url required" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
REPORT="$OUT_DIR/a11y-sr-report-$TS.md"

cat > "$REPORT" <<EOF
# Screen Reader Testing Protocol Report

- **URL**: $URL
- **Driver**: $DRIVER
- **Timestamp**: $TS
- **Operator**: $(whoami)@$(hostname)
- **Reference**: WCAG 2.2 AA § 1.3.1 / 2.4.3 / 2.4.6 / 4.1.2 / 4.1.3

## Phase 1 — Setup

- [ ] Screen reader 啟動且 focus ring 可視
- [ ] 瀏覽器切換至無障礙模式（Chrome: chrome://accessibility/ tree 可見）
- [ ] 系統語音語速調至測試者可辨識水準（預設約 180 wpm）
- [ ] 開發者工具 ARIA inspector 打開（axe DevTools / Chrome a11y pane）

## Phase 2 — Navigation Testing

- [ ] Heading 跳讀（VO: VO+Cmd+H / NVDA: H）— 階層連續不跳級（h1→h2→h3）
- [ ] Landmark 跳讀（VO: VO+U → Landmarks / NVDA: D）— main / nav / header / footer 齊全
- [ ] Skip-to-content link 於第一次 Tab 出現 + Enter 能跳過 nav
- [ ] Tab order 與視覺閱讀流一致（無 tabindex>0 強制重排）
- [ ] Focus indicator 對比 ≥ 3:1（手動觀察）

## Phase 3 — Interactive Component Testing

| 元件 | 預期語音 | 實測語音 | Pass? | Ticket |
|---|---|---|---|---|
| 主要 CTA button | "<label>, button" |  |  |  |
| Primary link | "<label>, link" |  |  |  |
| Form input (required) | "<label>, edit text, required" |  |  |  |
| Select / combobox | "<label>, combo box, <current>, menu popup" |  |  |  |
| Modal dialog | "<title>, dialog" + focus trap |  |  |  |
| Disclosure / accordion | "<label>, button, expanded/collapsed" |  |  |  |
| Custom widget (tab / slider) | 明確 role + state |  |  |  |

## Phase 4 — Dynamic Content Testing（對應 ARIA Live Region Checklist）

- [ ] Toast / snackbar — 出現時 SR 自動念出（\`role="status"\` 或 \`aria-live="polite"\`）
- [ ] Loading / progress — 變更時有 announcement（非僅視覺 spinner）
- [ ] Error / validation — 聚焦至錯誤欄 + 念出錯誤訊息（\`aria-live="assertive"\` 或 focus move）
- [ ] Route change (SPA) — 新頁面 title 被念出（route announcer pattern）
- [ ] Polling / async update — 非關鍵更新走 polite、關鍵走 assertive

## Findings

| Severity | Component | Description | WCAG | Fix |
|---|---|---|---|---|
|  |  |  |  |  |

## Sign-off

- [ ] 所有 Phase 2 / 3 / 4 核取框打勾或對應 Findings 有 ticket
- [ ] Findings 中 Severity=critical/serious 已進 R1 ChatOps queue
- [ ] 報告 attach 至 PR 或 Gerrit change

EOF

echo "Protocol report template generated: $REPORT"

case "$DRIVER" in
  voiceover)
    if [[ "$(uname -s)" != "Darwin" ]]; then
      echo "ERROR: VoiceOver driver only supported on macOS" >&2
      exit 2
    fi
    echo "Launching VoiceOver + opening $URL in default browser ..."
    osascript -e 'tell application "VoiceOver Utility" to activate' || true
    open "$URL"
    ;;
  nvda)
    echo "NOTE: NVDA launcher must be started by operator on Windows host." >&2
    echo "      After activation, open browser and navigate to: $URL" >&2
    ;;
  dry-run)
    echo "Dry-run mode — template only, no SR launched. Operator should fill in the report manually."
    ;;
  *)
    echo "unknown driver: $DRIVER" >&2
    exit 2
    ;;
esac
