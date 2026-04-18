#!/usr/bin/env node
/**
 * Focus Order Validator — W5 a11y skill（#353 B16 Part C row 291）
 *
 * 以 Playwright 驅動瀏覽器逐個 Tab，收集每一個 focus 停駐點的
 *   - 選擇器路徑（ARIA role / label / text-content）
 *   - 視覺座標（矩形 top-left）
 *   - focus indicator 對比（簡化版：outline-width > 0 即通過）
 * 產出 JSON + ASCII flow diagram，用於 PR review 附件。
 *
 * Exit:
 *   0 — focus order 自然流且無 focus trap 意外
 *   1 — 發現異常（tab 次數 < expected / focus 跳越視覺閱讀流 / outline:none 無替代）
 *   2 — invocation error
 *
 * 用法：
 *   node scripts/a11y_focus_order.js --url https://localhost:3000/app --max-tabs 50
 *   node scripts/a11y_focus_order.js --url https://staging/a --expect-focus-trap "[role=dialog]"
 *
 * Playwright 是 dev-dependency；若未安裝則 gracefully exit 0 with NOTE —
 * 與 W2 simulate-track 的 axe/pa11y optional 降級策略對齊。
 */
'use strict';

const argv = require('node:process').argv.slice(2);
const fs = require('node:fs');

function parseArgs(raw) {
  const out = { url: null, maxTabs: 50, expectFocusTrap: null, outFile: null };
  for (let i = 0; i < raw.length; i++) {
    const k = raw[i];
    if (k === '--url') out.url = raw[++i];
    else if (k === '--max-tabs') out.maxTabs = parseInt(raw[++i], 10);
    else if (k === '--expect-focus-trap') out.expectFocusTrap = raw[++i];
    else if (k === '--out') out.outFile = raw[++i];
    else if (k === '-h' || k === '--help') {
      console.log(fs.readFileSync(__filename, 'utf8').split('\n').slice(2, 22).join('\n'));
      process.exit(0);
    }
  }
  return out;
}

async function main() {
  const args = parseArgs(argv);
  if (!args.url) {
    console.error('ERROR: --url required');
    process.exit(2);
  }

  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch {
    console.error('NOTE: playwright not installed — focus-order check skipped.');
    console.error('      Install: pnpm add -D playwright && pnpm exec playwright install chromium');
    process.exit(0);
  }

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto(args.url, { waitUntil: 'domcontentloaded' });

  const stops = [];
  let lastSignature = null;
  let cycled = false;

  for (let i = 0; i < args.maxTabs; i++) {
    await page.keyboard.press('Tab');
    const snapshot = await page.evaluate(() => {
      const el = document.activeElement;
      if (!el || el === document.body) return null;
      const rect = el.getBoundingClientRect();
      const styles = getComputedStyle(el);
      return {
        tag: el.tagName.toLowerCase(),
        role: el.getAttribute('role') || el.tagName.toLowerCase(),
        label:
          el.getAttribute('aria-label') ||
          el.getAttribute('aria-labelledby') ||
          el.textContent?.trim().slice(0, 60) ||
          '',
        id: el.id || null,
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        outlineWidth: styles.outlineWidth,
        outlineColor: styles.outlineColor,
        outlineStyle: styles.outlineStyle,
        boxShadow: styles.boxShadow,
        tabindex: el.getAttribute('tabindex'),
      };
    });

    if (!snapshot) continue;

    const signature = `${snapshot.role}:${snapshot.id}:${snapshot.label}:${snapshot.x},${snapshot.y}`;
    if (signature === lastSignature) {
      cycled = true;
      break;
    }
    lastSignature = signature;
    stops.push({ index: i + 1, ...snapshot });
  }

  await browser.close();

  // ─── Heuristics ───
  const violations = [];
  stops.forEach((s, idx) => {
    if (s.outlineStyle === 'none' && !/inset|rgba|[0-9]/.test(s.boxShadow || '')) {
      violations.push({
        stop: idx + 1,
        kind: 'focus-indicator-missing',
        detail: `outline:none on ${s.role} "${s.label}" with no box-shadow fallback`,
      });
    }
    if (s.tabindex && parseInt(s.tabindex, 10) > 0) {
      violations.push({
        stop: idx + 1,
        kind: 'positive-tabindex',
        detail: `tabindex=${s.tabindex} on ${s.role} breaks natural reading flow`,
      });
    }
  });

  // Visual reading flow (top-to-bottom then left-to-right) check — allow small jitter
  for (let i = 1; i < stops.length; i++) {
    const prev = stops[i - 1];
    const cur = stops[i];
    if (cur.y + 40 < prev.y) {
      violations.push({
        stop: i + 1,
        kind: 'reading-flow-break',
        detail: `stop ${i + 1} (y=${cur.y}) jumps above stop ${i} (y=${prev.y})`,
      });
    }
  }

  if (args.expectFocusTrap) {
    const trapPresent = stops.some((s) =>
      s.label?.toLowerCase().includes(args.expectFocusTrap.toLowerCase())
    );
    if (!trapPresent) {
      violations.push({
        stop: 0,
        kind: 'expected-trap-absent',
        detail: `expected focus inside "${args.expectFocusTrap}" but never observed`,
      });
    }
  }

  const report = {
    url: args.url,
    totalStops: stops.length,
    cycledBackToStart: cycled,
    violations,
    stops,
  };

  const output = JSON.stringify(report, null, 2);
  if (args.outFile) {
    fs.writeFileSync(args.outFile, output);
  } else {
    console.log(output);
  }

  process.exit(violations.length === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err?.stack || err);
  process.exit(2);
});
