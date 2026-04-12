---
name: webapp-testing
description: Test web applications using Playwright automation. Use when tasks mention testing UI, frontend, web app, browser testing, or E2E.
keywords: [playwright, browser, e2e, ui-test, web-test, frontend-test, selenium, cypress, webapp, web]
---

# Web Application Testing

Test local or remote web applications through Playwright scripts.

## Decision Framework

1. **Static HTML?** → Read file directly with `read_file`
2. **Dev server running?** → Navigate directly
3. **Need to start server?** → Use `run_bash("npm run dev &")` then test

## Workflow

### Phase 1: Reconnaissance
- Navigate to target URL
- Wait for `networkidle` before inspecting DOM
- Capture page state: title, visible elements, console errors

### Phase 2: Element Discovery
- Use descriptive selectors: text content > role > CSS > ID
- Verify elements exist before interacting
- Log selector strategy for reproducibility

### Phase 3: Interaction & Verification
- Click, type, select — always wait for response
- Assert expected outcomes (text, URL, element state)
- Capture screenshots on failure

### Phase 4: Report
- Summarize: total tests, passed, failed
- Include failure details with selectors and expected vs actual

## Key Principles

- Always wait for `networkidle` on dynamic apps before DOM inspection
- Use synchronous Playwright (`sync_playwright()`)
- Include appropriate waits before actions
- Treat test scripts as black-box tools — run with `--help` first
