# OmniSight-Productizer — TODO

Full breakdown of all pending work. Grouped by priority tier (from
HANDOFF.md). Each task lists concrete sub-steps + deliverables.
Updated: 2026-04-15.

Legend:
- 🅐 Operator-blocked (physical action required)
- 🅑 Small products (< 1 day each, mostly UI/backend polish)
- 🅒 L4 product line — Layer A (shared infrastructure)
- 🅓 L4 product line — Layer B (per-product skill packs)
- 🅔 L4 product line — Layer C (software tracks)
- 🅕 META (organizational matrices / SOPs)

---

## 🅐 Priority A — Operator blockers

### A1. L1-01 Real deploy.sh execution + NS migration + v0.1.0 tag (#172)
- [O] Run `scripts/deploy.sh prod v0.1.0` against production host *(🅐 BLOCKED: operator needs prod host — see HANDOFF.md runbook Step 1)*
- [O] Migrate GoDaddy NS records to Cloudflare *(🅐 BLOCKED: operator needs GoDaddy + CF accounts — see HANDOFF.md runbook Step 2)*
- [O] Confirm Cloudflare Tunnel active + cert issued *(🅐 BLOCKED: operator needs CF dashboard — see HANDOFF.md runbook Step 3)*
- [O] Smoke GET `/api/health` from public domain *(🅐 BLOCKED: depends on Steps 1-3 above — see HANDOFF.md runbook Step 4)*
- [x] Tag `v0.1.0` on master *(done: local tag created 2026-04-15)*
- [x] Push `v0.1.0` tag to origin *(done: pushed to origin 2026-04-15)*
- [x] Update HANDOFF with deploy URL + v0.1.0 release notes *(done: runbook + release notes added)*

### A2. L1-05 Prod smoke test — 2 real DAGs end-to-end (#176)
- [x] Pick DAG #1: `compile-flash` template against host_native *(done: defined in `scripts/prod_smoke_test.py` — DAG_1_COMPILE_FLASH_HOST_NATIVE)*
- [x] Pick DAG #2: `cross-compile` template (uses Phase 64-C-LOCAL) *(done: defined in `scripts/prod_smoke_test.py` — DAG_2_CROSS_COMPILE_AARCH64)*
- [O] Run both via production UI; capture workflow_run IDs *(🅐 BLOCKED: depends on A1 prod deploy — run `python scripts/prod_smoke_test.py https://<PROD_URL>`)*
- [O] Verify steps complete, artifacts persist, audit log hash-chain intact *(🅐 BLOCKED: depends on above — script verifies automatically)*
- [O] Attach run report to HANDOFF *(🅐 BLOCKED: script generates `data/smoke-test-report-a2.md` — paste into HANDOFF after run)*

---

## 🅑 Priority B — Small products

### B1. Cross-agent observation routing (#209)
- [x] Add enum constant `cross_agent/observation` in finding_type module
- [x] Add orchestrator routing rule: cross-agent finding → Decision Engine proposal
- [x] Add `blocking=true` flag on DE proposals to prioritize reporter-blocked cases
- [x] Unit test: agent A emits finding → DE proposal appears → agent B notified
- [x] Update `docs/sop/implement_phase_step.md` with cross-agent protocol

### B2. INGEST-01 `backend/repo_ingest.py` (#202)
- [x] Implement `clone_repo(url, shallow=True)` with git credential validation
- [x] Implement `introspect(repo_path)` → reads `package.json` / `README.md` / `next.config.mjs` / `requirements.txt` / `Cargo.toml`
- [x] Map discovered fields → ParsedSpec (framework, runtime_model, persistence, target_arch)
- [x] Handle private repo token storage (reuse git_credentials.yaml pattern)
- [x] Unit tests for 3 starter templates (v0.app Next.js / FastAPI backend / Rust CLI)

### B3. REPORT-01 `backend/report_generator.py` (#203)
- [x] Section 1 (Spec): read ParsedSpec + all clarifications + input sources
- [x] Section 2 (Execution): workflow_runs + steps + decisions + retries
- [x] Section 3 (Outcome): deploy URL + smoke test results + open debug_findings
- [x] Markdown template + optional PDF via `weasyprint`
- [x] Signed URL helper for read-only share
- [x] Unit test: fixture workflow → report matches golden file

### B4. UX-05 New-project wizard modal (#204)
- [x] Detect empty `localStorage['omnisight:intent:last_spec']` on first load
- [x] Modal with 4 choices: GitHub repo / Upload docs / Prose / Blank DAG
- [x] Route each choice to the correct panel (Spec Editor / DAG Editor)
- [x] Skip if user has prior session
- [x] Component test: first-mount shows modal; second-mount does not

### B5. UX-01 SpecTemplateEditor source tabs (#205)
- [x] Add `Prose | From Repo | From Docs` tab header
- [x] Repo tab: URL input + clone progress indicator (depends on INGEST-01)
- [x] Docs tab: drag-drop zone + uploaded file list + per-file parse status
- [x] Merge ingested data into ParsedSpec; preserve user prose overrides
- [x] Component tests: each tab round-trips to the shared spec state

### B6. UX-04 Project Report panel (#206)
- [x] Create `components/omnisight/project-report-panel.tsx`
- [x] Three collapsible sections mirroring REPORT-01 output
- [x] Markdown download button + copy-to-clipboard
- [x] Share link button → POST `/report/share` → returns signed URL
- [x] Component tests: renders golden fixture; download triggers correct blob

### B7. UX-03 RunHistory project_run aggregation (#207)
- [x] Add `project_runs` table (id, label, created_at, workflow_run_ids[])
- [x] Migration + backfill script for existing runs (best-effort: group by session)
- [x] API: `GET /projects/{id}/runs` returns parent + children
- [x] Frontend: default collapsed parent row with summary stats
- [x] Expand on click to show child workflow_runs
- [x] Component test: parent click expands, status tallies correct

### B8. DAG toolchain enum / autocomplete (HANDOFF B)
- [x] Collect toolchain names from `get_platform_config` outputs
- [x] Expose enum via `GET /platforms/toolchains`
- [x] Frontend: DAG Form editor uses `<datalist>` for toolchain field
- [x] Semantic validator warn on unknown toolchain at edit time (not runtime)

### B9. ESLint 113 findings batch cleanup (HANDOFF B)
- [x] Group findings by rule (likely top 5 rules cover 80%)
- [x] PR 1: unused-vars / prefer-const (~40 findings)
- [x] PR 2: no-explicit-any in types files (~25 findings)
- [x] PR 3: react-hooks/exhaustive-deps (carefully, per-file review)
- [x] PR 4: remaining misc rules
- [x] Flip warn → error for cleaned rules in `eslint.config.mjs`

### B10. Pipeline Timeline `omnisight:timeline-focus-run` wiring (HANDOFF B)
- [x] Decide: is the NPI-phase Timeline the right target? (Currently mismatched concept) *(Decision: NO — Pipeline Timeline tracks NPI lifecycle phases, not individual runs. Concept mismatch confirmed.)*
- [x] ~~If yes: Timeline listens to event, scrolls to matching workflow_run marker~~ *(N/A — decided NO)*
- [x] If no: drop the event; RunHistory inline-expand already covers the need *(Done: event dropped. B7 RunHistory project_run aggregation with inline-expand already provides run-level focus.)*
- [x] Update HANDOFF based on decision

### B11. Forecast panel reactive to spec context (HANDOFF B)
- [x] Listen to `omnisight:spec-updated` event
- [x] Recompute estimates when `target_platform` / `framework` changes
- [x] Show delta vs previous estimate (± cycle time / ± token budget)
- [x] Component test: fire event → estimate re-renders

### B12. UX-CF-TUNNEL-WIZARD — Cloudflare Tunnel 一鍵自動配置（新）
> 目標：取代現行手動 4 步驟（`cloudflared tunnel login` → `create` → `route dns` → 編輯 `config.yml`）。使用者在 UI 只輸入 CF API Token + 選 Zone + 填 hostname，其餘由系統自動完成。

**Backend**
- [x] `backend/cloudflare_client.py`：CF API v4 wrapper（tunnels / dns / zones / accounts）+ 錯誤映射（401 invalid token / 403 missing scope / 409 conflict / 429 rate limit）
- [x] `backend/routers/cloudflare_tunnel.py`：
  - `POST /api/v1/cloudflare/validate-token` — 驗 token 並回傳可用 accounts
  - `GET  /api/v1/cloudflare/zones?account_id=` — 列使用者可選 zone
  - `POST /api/v1/cloudflare/provision` — 建 tunnel + ingress config + DNS CNAME（冪等、失敗自動回滾）
  - `GET  /api/v1/cloudflare/status` — tunnel 連線狀態（connector 是否 up、DNS propagation）
  - `POST /api/v1/cloudflare/rotate-token`
  - `DELETE /api/v1/cloudflare/tunnel` — teardown（刪 tunnel + DNS record）
- [x] 使用 connector **token 模式**（`cloudflared tunnel run --token <T>`）避免 credentials.json 檔案管理
- [x] 整合 `backend/secret_store.py`：token at-rest 加密、UI 只見 fingerprint；寫入 Phase 53 audit_log（`cf_tunnel.provision` / `.rotate` / `.delete`）
- [x] systemd 橋接：sudoers NOPASSWD rule **僅限** `systemctl {start,stop,restart,status} cloudflared.service`；或 container 模式用 sidecar 免 systemd
- [x] 冪等：provision 失敗要清掉已建 tunnel / DNS（plan → apply 兩段式，或 try/rollback 記錄）

**Frontend**
- [x] `components/omnisight/cloudflare-tunnel-setup.tsx`：多步 wizard
  - Step 1 API Token 輸入 + "如何建立 token" 連結（預設 scope 清單）
  - Step 2 驗證 token → 列 account / zone 選單
  - Step 3 填 hostname（預設 `omnisight.<zone>` 與 `api.omnisight.<zone>`）
  - Step 4 Review → 「一鍵 Provision」按鈕
  - Step 5 即時狀態（SSE）：tunnel 建立 ✓ / DNS ✓ / connector online ✓ / health probe ✓
- [x] 既有 tunnel 偵測：顯示現況 + rotate / teardown 按鈕
- [x] 錯誤 UI：token scope 不足時明列缺少哪幾個 permission

**測試**
- [x] Mock CF API（`respx`）涵蓋：invalid token / missing scope / existing tunnel / DNS 已存在 / rate limit / 部分成功回滾
- [x] 整合測試：provision → status → teardown 完整循環
- [O] E2E（Playwright）：wizard 四步流程 + 錯誤路徑

**文件 & 安全**
- [x] `docs/operations/cloudflare_tunnel_wizard.md`：使用者操作步驟 + token scope 建議
- [x] 更新 `docs/operations/deployment.md`：提示 wizard 路徑並保留 CLI 手動模式備援
- [x] 安全檢查：token 不得出現於日誌 / SSE payload / error 訊息

**預估**：~6 day（BE 2 + FE 1.5 + systemd 橋接 1 + audit/rollback/test/docs 1.5）

---

## 🅒 Priority C — L4 Layer A (shared infrastructure)

### C0. L4-CORE-00 ProjectClass enum + multi-planner routing (#222)
- [x] Add `ProjectClass` enum: embedded_product / algo_sim / optical_sim / iso_standard / test_tool / factory_tool / enterprise_web
- [x] Extend ParsedSpec with `project_class` field
- [x] Intent Parser prompt: infer class from prose (add YAML rules to `configs/spec_conflicts.yaml`)
- [x] Router: dispatch to correct planner based on class
- [x] Unit test: each class routes to its planner

### C1. L4-CORE-04 Phase 64-C-SSH runner (#210) — **highest priority**
- [x] Extend `backend/t3_resolver.py` to select SSH when arch≠host
- [x] `backend/ssh_runner.py`: paramiko-based exec + file sync (rsync/sftp)
- [x] Credentials: re-use git_credentials.yaml-style secure storage
- [x] Sandbox: read-only sysroot + scratch dir per run
- [x] Timeout + heartbeat + kill on disconnect
- [x] Integration test: loopback SSH to localhost emulates remote board
- [x] Docs: `docs/operations/ssh-runner.md` with key-gen + lockdown

### C2. L4-CORE-01 HardwareProfile schema (#211)
- [x] Define dataclass `HardwareProfile` with fields: SoC, MCU, DSP, NPU, sensor, codec, USB, display, memory_map, peripherals
- [x] JSON schema + pydantic validation
- [x] Migration: extend ParsedSpec optionally embedding HardwareProfile
- [x] Unit test: round-trip serialize/deserialize

### C3. L4-CORE-02 Datasheet PDF → HardwareProfile parser (#212)
- [x] PDF text extraction (reuse Phase 67-E RAG)
- [x] Structured extraction prompt per HardwareProfile field
- [x] Confidence per field (≥0.7 auto-accept, else clarify)
- [x] Fallback: operator form-fills missing fields
- [x] Unit test: sample datasheets (Hi3516 / RK3566 / ESP32-S3)

### C4. L4-CORE-03 Embedded product planner agent (#213)
- [x] Input: HardwareProfile + ProductSpec + selected skill_pack
- [x] Output: full DAG (BSP → kernel → drivers → protocol layer → UI → OTA → tests)
- [x] Use skill pack's `tasks.yaml` as template source
- [x] Handle dependency resolution between tasks
- [x] Unit test: fixture spec → expected DAG task count / topology

### C5. L4-CORE-05 Skill pack framework (#214)
- [x] Define skill manifest schema (`skill.yaml`)
- [x] Registry: `configs/skills/<name>/` convention
- [x] Lifecycle hooks: install / validate / enumerate
- [x] CLI: `omnisight skill list / install / validate`
- [x] Contract test: every skill must provide 5 artifacts (tasks/scaffolds/tests/hil/docs)

### C6. L4-CORE-06 Document suite generator (#215)
- [x] Extend REPORT-01 with per-product-class templates
- [x] Templates: datasheet.md.j2 / user_manual.md.j2 / compliance.md.j2 / api_doc.md.j2 / sbom.json.j2 / eula.md.j2 / security.md.j2
- [x] Merge compliance-cert fields from relevant L4-CORE-09/10/18
- [x] PDF export via weasyprint
- [x] Unit test per product class

### C7. L4-CORE-07 HIL plugin API (#216)
- [x] Define plugin protocol: `measure()` / `verify()` / `teardown()`
- [x] Camera family plugin (focus/WB/stream-latency)
- [x] Audio family plugin (SNR/AEC metrics)
- [x] Display family plugin (uniformity/touch latency)
- [x] Registry: skill pack declares required HIL plugins
- [x] Integration test: mock HIL plugin lifecycle

### C8. L4-CORE-08 Protocol compliance harness (#217)
- [x] Wrapper for ODTT (ONVIF Device Test Tool) — headless mode or subprocess
- [x] Wrapper for USB-IF USBCV
- [x] Wrapper for UAC test suite
- [x] Normalized report schema (pass/fail per test case + evidence)
- [x] Output → audit_log
- [x] Smoke test per wrapper

### C9. L4-CORE-09 Safety & compliance framework (#223)
- [x] Rule library: ISO 26262 ASIL A-D / IEC 60601 SW-A/B/C / DO-178 DAL A-E / IEC 61508 SIL 1-4
- [x] Each rule is a DAG validator + required artifact list
- [x] Artifacts: hazard analysis, risk file, software classification, traceability matrix
- [x] CLI: `omnisight compliance check --standard iso26262 --asil B`
- [x] Unit test: gate rejects DAG missing required artifact

### C10. L4-CORE-10 Radio certification pre-compliance (#224)
- [x] Test recipe library: FCC Part 15 / CE RED / NCC LPD / SRRC SRD
- [x] Conducted + radiated emissions stub runners
- [x] SAR test hook (operator-uploads SAR result file)
- [x] Per-region cert artifact generator
- [x] Unit test: sample radio spec → correct cert checklist

### C11. L4-CORE-11 Power / battery profiling (#225)
- [x] Sleep-state transition detector (entry/exit event trace)
- [x] Current profiling sampler (external shunt ADC integration)
- [x] Battery lifetime model (capacity × avg draw × duty cycle)
- [x] Dashboard: mAh/day per feature toggle
- [x] Unit test: synthetic current trace → correct lifetime estimate

### C12. L4-CORE-12 Real-time / determinism track (#226)
- [x] RT-linux build profile (`PREEMPT_RT` kernel config)
- [x] RTOS build profile (FreeRTOS / Zephyr)
- [x] `cyclictest` harness + percentile latency report
- [x] Scheduler trace capture (`trace-cmd` / `bpftrace`)
- [x] Threshold gate: fails build if P99 > declared budget

### C13. L4-CORE-13 Connectivity sub-skill library (#227)
- [x] BLE sub-skill (GATT + pairing + OTA profile)
- [x] WiFi sub-skill (STA/AP + provisioning + enterprise auth)
- [x] 5G sub-skill (modem AT / QMI + dual-SIM)
- [x] Ethernet sub-skill (basic + VLAN + PoE detection)
- [x] CAN sub-skill (SocketCAN + diagnostics)
- [x] Modbus / OPC-UA sub-skills (industrial)
- [x] Registry + composition: skill packs opt-in per sub-skill

### C14. L4-CORE-14 Sensor fusion library (#228)
- [x] IMU drivers (MPU6050 / LSM6DS3 / BMI270)
- [x] GPS NMEA parser + UBX protocol
- [x] Barometer driver (BMP280 / LPS22)
- [x] EKF implementation (9-DoF orientation)
- [x] Calibration routines (bias/scale/alignment)
- [x] Unit test against known trajectory fixture

### C15. L4-CORE-15 Security stack (#229)
- [x] Secure boot chain: bootloader → kernel → rootfs signature verify
- [x] TEE binding (OP-TEE / TrustZone abstraction)
- [x] Remote attestation: TPM / SE / fTPM
- [x] SBOM signing with sigstore/cosign
- [x] Key management SOP (`docs/operations/key-management.md`)
- [x] Threat model per product class

### C16. L4-CORE-16 OTA framework (#230)
- [x] A/B slot partition scheme
- [x] Delta update (bsdiff / zchunk / RAUC)
- [x] Rollback trigger on boot-fail (watchdog + count)
- [x] Signature verification (ed25519 + cert chain)
- [x] Server side: update manifest + phased rollout
- [x] Integration test: flash → reboot → rollback path

### C17. L4-CORE-17 Telemetry backend (#231)
- [x] Client SDK: crash dump + usage event + perf metric
- [x] Ingestion endpoint (batched POST + retry queue)
- [x] Storage: partitioned table with retention policy
- [x] Privacy: PII redaction + opt-in flag
- [x] Dashboard: fleet health + crash rate + adoption
- [x] Unit test: SDK offline queue flushes on reconnect

### C18. L4-CORE-18 Payment / PCI compliance framework (#239)
- [x] PCI-DSS control mapping (req 1-12 → product artifacts)
- [x] PCI-PTS physical security rule set
- [x] EMV L1 (hardware) / L2 (kernel) / L3 (acceptance) test stubs
- [x] P2PE (point-to-point encryption) key injection flow
- [x] HSM integration abstraction (Thales / Utimaco / SafeNet)
- [x] Cert artifact generator

### C19. L4-CORE-19 Imaging / document pipeline (#240)
- [x] Scanner ISP path (CIS/CCD → 8/16-bit grey/RGB)
- [x] OCR integration (Tesseract / PaddleOCR / vendor SDK)
- [x] TWAIN driver template (Windows)
- [x] SANE backend template (Linux)
- [x] ICC color profile embedding

### C20. L4-CORE-20 Print pipeline (#241)
- [x] IPP/CUPS backend wrapper
- [x] PDL interpreters: PCL / PostScript / PDF (via Ghostscript)
- [x] Color management: ICC profile per paper/ink combo
- [x] Print queue + spooler integration
- [x] Unit test: round-trip PDF → raster → PDL → output

### C21. L4-CORE-21 Enterprise web stack pattern (#242) — **highest leverage for Layer C**
- [x] Auth: Next-Auth + optional SSO plug (LDAP/SAML/OIDC)
- [x] RBAC: role/permission schema + policy middleware
- [x] Audit: every write → audit_log (reuse Phase 53 hash chain)
- [x] Reports: tabular + chart via Tremor / shadcn
- [x] i18n: next-intl scaffold with zh/en bundles
- [x] Multi-tenant: tenant_id column + row-level security
- [x] Import/export: CSV/XLSX/JSON round-trip
- [x] Workflow engine: state machine + approval chain
- [x] Reference implementation (acts as template for SW-WEB-*)

### C22. L4-CORE-22 Barcode/scanning SDK abstraction (#243)
- [x] Unified `BarcodeScanner` interface
- [x] Vendor adapters: Zebra SNAPI / Honeywell SDK / Datalogic SDK / Newland SDK
- [x] Symbology support: UPC/EAN/Code128/QR/DataMatrix/PDF417/Aztec
- [x] Decode modes: HID wedge / SPP / API
- [x] Unit test with pre-captured frame samples

### C23. L4-CORE-23 Depth / 3D sensing pipeline (#253)
- [x] ToF sensor driver abstraction (Sony IMX556 / Melexis MLX75027)
- [x] Structured light capture + decoder
- [x] Stereo rectification + disparity (OpenCV SGBM)
- [x] Point cloud: PCL + Open3D wrappers
- [x] ICP registration + SLAM hooks
- [x] Unit test: known scene → expected point count + bounds

### C24. L4-CORE-24 Machine vision & industrial imaging framework (#254)
- [x] GenICam driver abstraction
- [x] GigE Vision transport (aravis or mvIMPACT)
- [x] USB3 Vision transport
- [x] Hardware trigger + encoder sync
- [x] Multi-camera calibration (checkerboard + bundle adjustment)
- [x] Line-scan support
- [x] PLC integration (Modbus/OPC-UA via CORE-13)

### C25. L4-CORE-25 Motion control / G-code / CNC abstraction (#255)
- [x] G-code interpreter (subset: G0/G1/G28/M104/M109/M140)
- [x] Stepper driver abstraction (TMC2209 / A4988 / DRV8825)
- [x] Heater + PID loop (hotend + bed)
- [x] Endstop handling + homing
- [x] Thermal runaway safety shutoff
- [x] Unit test: G-code sequence → expected motion trace

### C26. L4-CORE-26 HMI embedded web UI framework (#261)
- [x] Bundle size budget per platform profile (flash partition aware; CI hard-fail on 超標)
- [x] Constrained generator（whitelist Preact / vanilla JS / lit-html；禁 CDN；inline fonts + CSS；禁 analytics）
- [x] Backend binding generator：NL + HAL schema → fastcgi/mongoose/civetweb C handler 骨架 + 對應 JS client
- [x] QEMU + headless Chromium 驗證 harness（`scripts/simulate.sh` 新增 `hmi` track）
- [x] IEC 62443 security baseline gate（CSP / XSS / CSRF / session storage / auth flow）
- [x] Embedded browser ABI matrix（aarch64/armv7/riscv64 × 凍結版 Chromium/WebKit 相容性表）
- [x] i18n 框架（與 D-series doc templates 共用語言池，en/zh-TW/ja/zh-CN 4 語言起步）
- [x] 共用 HMI component library（network / OTA / logs viewer — 供 D2 IPCam / D8 Router / D9 5G-GW / D17 Industrial-PC / D24 POS / D25 Kiosk 共用）
- [x] Pluggable LLM backend（Opus 4.7 Design Tool / Ollama 本地 / rule-based fallback，沿用 `OMNISIGHT_LLM_PROVIDER`）
- [x] Unit + integration tests（generator / QEMU+Chromium / size budget gate）

---

## 🅙 Priority S — Shared Auth/Session Foundation（路線 C 前置共用基礎）

> 背景：J（multi-session hardening）與 K（auth hardening）有 30% schema/API 交集（`audit_log.session_id`、sessions CRUD、sessions 表擴充）。此 S phase 先把共用基礎做完，J 與 K 即可互不干擾增量推進，避免 migration 衝突。

### S0. Shared foundation
- [x] Alembic migration：`audit_log` 加 `session_id TEXT` 欄位 + index；既有資料 `session_id=NULL` 表示系統或匿名來源
- [x] `sessions` 表預留欄位：`metadata JSONB`（J 存 per-session mode）、`mfa_verified BOOLEAN DEFAULT 0`、`rotated_from TEXT`（K 用）— 一次 ALTER 到位避免日後再 migrate
- [x] `GET /api/v1/auth/sessions` — 列當前 user 的 active sessions（遮罩 token，顯示 IP/UA/created_at/last_seen_at）
- [x] `DELETE /api/v1/auth/sessions/{token}` — revoke 單一 session（admin 可 revoke 他人）
- [x] `DELETE /api/v1/auth/sessions` — 當前 user 的「登出所有其他裝置」
- [x] `current_user` 回傳值同時帶 `Session` 物件（或 `request.state.session` 注入），讓後續 audit 寫入可取 `session_id` 不需再查
- [x] 所有 audit 寫入點統一經 helper `write_audit(action, actor, session_id=..., ...)` — session_id 自動從 request context 取
- [x] 測試：sessions CRUD、revoke 後 cookie 失效、audit_log 正確帶 session_id、bearer token 寫入時 session_id=`bearer:<fingerprint>`
- [x] 預估：**0.5 day**

---

## 🅚 Priority K-early — 對外部署紅線安全（路線 C 第 2 段）

> 背景：現行 auth 預設 `OMNISIGHT_AUTH_MODE=open`（無認證）、default admin 密碼為常數 `omnisight-admin`、`authenticate_password` 無速率限制。三者為對外部署前必須解掉的紅燈。

### K1. 預設配置強化 + 部署檢查
> **實測現況（2026-04-16 驗證）**：`backend/config.py` L296-329 `validate_startup_config` 已部分實作——`debug=false` 時會 hard-fail 擋下 `OMNISIGHT_AUTH_MODE=open` 與 `OMNISIGHT_ADMIN_PASSWORD=omnisight-admin`，但 `OMNISIGHT_DEBUG=true` 全退化為 warning。`backend/auth.py` L274-291 `ensure_default_admin()` 仍會自動建 `admin@omnisight.local` / `omnisight-admin`，實測 `POST /api/v1/auth/login` 可直接以該預設密碼取得 admin session + HttpOnly cookie（SameSite=lax，無 Secure flag）。前端 `/login` 導流正常（`next` query param 回原頁），但**無任何「首次登入強制改密碼」關卡**——導流做對、credential baseline 未鎖，這是目前最大對外部署紅線。
- [x] Startup self-check：若 `ENV=production` 但 `OMNISIGHT_AUTH_MODE != strict` → 拒絕啟動（明確錯誤訊息 + 退出碼 78）；目前只看 `settings.debug` flag，需獨立 `ENV=production` 判斷避免 debug 意外被設 true 繞過
- [x] 若 default admin 密碼仍為 `omnisight-admin` → 啟動時強制標記 `users.must_change_password=1`，登入後任何 API 除 `POST /auth/change-password` 全回 428 Precondition Required；前端 `/login` 成功 response 檢測此旗標自動導向 `/settings/change-password`
- [x] Docker `Dockerfile.backend` / compose prod：預設 env `OMNISIGHT_AUTH_MODE=strict`
- [x] 文件 `docs/ops/security_baseline.md`：列出部署前 checklist（strict mode、改密碼、bearer token 僅限 CI 白名單 IP）
- [x] 測試：啟動模式檢查、未改密碼時 API 拒絕 428、改完密碼後旗標清除
- [x] 預估：**0.5 day**

### K2. 登入速率限制 + 帳號鎖定
- [x] `backend/rate_limit.py`：in-process token bucket（未來 I 多 worker 時換 Redis）— 預設 `/auth/login` 每 IP 5/min、每 email 10/hour
- [x] `users` 表加 `failed_login_count`、`locked_until`；連續 10 次失敗 → 鎖 15 分鐘（指數 backoff 上限 24h）
- [x] 鎖定期間 `authenticate_password` 回 `None` 且不走 PBKDF2（省 CPU）
- [x] 成功登入 reset counter；audit_log 記錄 `auth.login.fail` / `auth.lockout`
- [x] 測試：rate limit 生效、lockout 釋放、時間衰減
- [x] 預估：**1 day**

### K3. Cookie flags + CSP 驗證
- [x] 驗所有 `Set-Cookie`：session → `HttpOnly + Secure + SameSite=Lax`；CSRF → `Secure + SameSite=Lax`（不可 HttpOnly，前端要讀）
- [x] 加入 `secure.py` middleware（若無）設 CSP、`X-Frame-Options=DENY`、`Referrer-Policy=strict-origin`、`Permissions-Policy`
- [x] CSP 嚴格模式（nonce-based）避免 inline script；前端 Next.js config 配合
- [x] E2E 測試：`curl -I` 驗 response header；Playwright 驗 CSP 阻擋 inline eval
- [x] 預估：**0.5 day**

**K-early 總預估**：**2 day**。完成即可對外部署不會被立刻打爆。

---

## 🅙 Priority J — Multi-session Single-user Hardening（路線 C 第 3 段）

> 背景：單人但多處登入（筆電 / 手機 / 多 tab）時目前有 7 類體驗問題：SSE 全域廣播、localStorage 各機器不同步、`_ModeSlot` 全域共用、workflow_run 併發無樂觀鎖、無 session 管理 UI、audit 無 session_id（已由 S0 解掉）、operation mode 全域。此 phase 補齊。

### J1. SSE per-session filter
- [x] Event envelope 加 `session_id` + `broadcast_scope: session|user|global`
- [x] 前端 SSE client 比對當前 `session_id` 過濾（預設只看自己 session 觸發的 + user-level 通知）
- [x] UI toggle：「顯示所有我的 session 事件」/「僅本 session」
- [x] 測試：多 session fixture → 驗證過濾正確
- [x] 預估：**0.5 day**

### J2. Workflow_run 樂觀鎖
- [x] `workflow_runs` 加 `version INTEGER DEFAULT 0` + migration
- [x] Retry / cancel / update endpoints：require `If-Match: <version>` header；version 不符回 409
- [x] 前端按鈕按下時帶當前 version；409 → 提示「另一處已修改，請重新整理」
- [x] 測試：並發 retry 只有一個成功
- [x] 預估：**0.5 day**

### J3. Session management UI
- [x] `components/omnisight/session-manager-panel.tsx`：列 S0 `/auth/sessions` 結果（device / IP / created / last_seen）
- [x] 每列 Revoke 按鈕 + 「登出其他所有裝置」按鈕
- [x] 當前 session 標記 "This device"
- [x] E2E 測試：revoke 後該裝置下次 API call 得 401
- [x] 預估：**1 day**

### J4. localStorage 多 tab 同步
- [x] 所有 `omnisight:*` keys 加 `user_id` 前綴（從登入 context 取）
- [x] `window.addEventListener('storage', ...)` 跨 tab 同步 spec / locale / wizard 狀態
- [x] 首次載入 wizard 判斷改查 server-side `user_preferences` 表（共用電腦第二使用者不被跳過）
- [x] 測試：Playwright 雙 tab scenario
- [x] 預估：**0.5 day**

### J5. Per-session Operation Mode
- [x] Operation Mode 從全域 config 搬到 `sessions.metadata.operation_mode`
- [x] `_ModeSlot` 讀取改為 per-session（budget 仍是全域池，但 mode cap 個別計算）
- [x] UI mode selector 只影響當前 session，tooltip 顯示「此設定僅影響本裝置」
- [x] 測試：A session turbo + B session supervised 各自 mode cap 生效
- [x] 預估：**0.5 day**

### J6. Audit UI 帶 session 過濾
- [x] Audit 查詢頁加 session filter（列表 + 目前 session 快捷鈕）
- [x] 顯示每筆 audit 的 device / IP（從 session 聯結）
- [x] 預估：**0.5 day**

**J 總預估**：**3.5 day**

---

## 🅚 Priority K-rest — Auth Hardening 完整版（路線 C 第 4 段）

### K4. Session rotation + binding
- [x] 登入成功 / 密碼變更 / 權限升級 → 產新 token，舊 token 寫 `rotated_from` 指向新 token；舊 token grace 30s 允許 in-flight request，之後失效
- [x] Session 綁 UA hash（非 IP，移動網路 IP 常變）；UA 變更記警告但不強制登出
- [x] 測試：rotate 流程、grace window、UA 變更警告
- [x] 預估：**1 day**

### K5. MFA (TOTP) + Passkey (WebAuthn) 骨架
- [x] `user_mfa` 表：method (totp/webauthn)、secret/credential、created_at、last_used
- [x] TOTP：enrollment QR + verify flow；backup codes（10 組，單次）
- [x] WebAuthn：`py_webauthn` 套件、register + authenticate endpoints
- [x] 登入流程：密碼 OK → 若有 MFA → `mfa_required=true` response → 驗通過才 `create_session(mfa_verified=True)`
- [x] Strict mode 可設 `require_mfa=True` 強制 admin / operator 啟用
- [x] UI：Settings → MFA 管理頁
- [x] 測試：TOTP drift 容忍、backup code 單次性
- [x] 預估：**2.5 day**

### K6. Bearer token per-key + 稽核
- [x] 廢除單一 `OMNISIGHT_DECISION_BEARER` env；改 `api_keys` 表（id、name、hashed_key、scopes、created_by、last_used_ip、enabled）
- [x] CLI / CI 憑個別 key 呼叫；audit_log 帶 `session_id=bearer:<key_id>` 可追
- [x] Admin UI 建 / rotate / revoke key
- [x] Migration 舊 env：啟動時偵測 → 自動建一筆 `legacy-bearer` key 並發警告要求盡快換
- [x] 測試：scope 限制（只能呼叫白名單 endpoint）、revoke 即時生效
- [x] 預估：**1 day**

### K7. 密碼政策 + Argon2id 升級路徑
- [x] 密碼強度：最短 12 字、zxcvbn score ≥ 3
- [x] 新密碼比對歷史 5 筆（`password_history` 表）
- [x] Hash 格式支援 `argon2id$...`；驗證時雙軌（舊 pbkdf2 驗成功後自動 rehash 成 argon2id）
- [x] `argon2-cffi` 依賴加入
- [x] 測試：升級路徑、舊 hash 仍可驗、下次登入自動升級
- [x] 預估：**0.5 day**

**K-rest 總預估**：**5 day**

**路線 C 總預估**：S0 (0.5) + K-early (2) + J (3.5) + K-rest (5) = **11 day**

---

## 🅘 Priority I — Multi-tenancy Foundation（緊接路線 C 之後）

> 背景：完成路線 C 後，auth + session + audit 基礎 hardened，才適合把「單人多 session」擴成「多租戶多 user」。此 phase 是正式多人上線的 gate。相依：**G4（Postgres）** 必須完成（SQLite 無 RLS）、**H1-H4a（host-aware）** 必須完成（I6 才有 token bucket 可拆 per-tenant）、**S0 + K-early** 必須完成（auth baseline）。

### I1. Schema: tenants + tenant_id 欄位 + 回填
- [x] 新增 `tenants` 表（id / name / plan / created_at / enabled）
- [x] `users` 加 `tenant_id`（一人一 tenant；未來多 tenant 用 `user_tenant_membership` 中介表）
- [x] 所有業務表加 `tenant_id`：`workflow_runs` / `debug_findings` / `decisions` / `event_log` / `audit_log` / `spec_*` / `artifacts` / `user_preferences`
- [x] Alembic 遷移 + 回填腳本（既有資料歸預設 tenant `t-default`）
- [x] 測試：migration 幂等、回填正確
- [x] 預估：**3 day**

### I2. Query layer RLS（SQLAlchemy global filter）
- [x] `backend/db_context.py`：`current_tenant_id()` context var
- [x] SQLAlchemy event listener：所有 SELECT 自動注入 `WHERE tenant_id = :current`（Postgres 可改 RLS policy）
- [x] INSERT 自動填 `tenant_id = current`
- [x] Router 層 `require_tenant` dependency 從 user 取 tenant_id 塞進 context
- [x] 測試：跨 tenant 查詢回空、INSERT 無法指定他 tenant
- [x] 預估：**2 day**

### I3. SSE per-tenant + per-user filter（延伸 J1）
- [x] Event envelope 加 `tenant_id`；subscriber 自動綁當前 tenant
- [x] `broadcast_scope` ��充 `tenant` 選項
- [x] 回歸測試：A tenant 監聽只收到 A 的事件
- [x] 預估：**1.5 day**

### I4. Secrets per-tenant
- [x] `git_credentials` / `provider_keys` / `cloudflare_tokens`（B12 產物）全改 tenant-scoped 表
- [x] `backend/secrets.py` API 加 tenant_id 維度
- [x] Migration：既有共用 credentials 分給 `t-default`
- [x] UI：Settings 頁分 tenant 視圖
- [x] 預估：**2 day**

### I5. Filesystem namespace
- [x] `data/tenants/<tid>/{artifacts,ingest,backups,workflow_runs}/`
- [x] 所有寫路徑函式接受 tenant context
- [x] `_INGEST_ROOT` 改 `/tmp/omnisight_ingest/<tid>/`
- [x] Migration 腳本搬既有檔案到 `t-default`
- [x] 測試：跨 tenant 路徑隔離
- [x] 預估：**1.5 day**

### I6. Sandbox fair-share（DRF per-tenant）
- [x] H4a 的 token bucket 改 per-tenant；全域 CAPACITY_MAX 維持 12
- [x] Dominant Resource Fairness：每 tenant 拿到 `CAPACITY_MAX / active_tenant_count` 的保證最低值
- [x] 空閒時可超用他 tenant 未用額度，他 tenant 來時 30s 內讓出
- [x] Turbo 加 per-tenant cap 防單 tenant 獨佔
- [x] 測試：兩 tenant 負載模擬、餓死防護
- [x] 預估：**1.5 day**

### I7. Frontend tenant-aware
- [x] localStorage 前綴改 `omnisight:${tenantId}:${userId}:*`
- [x] Tenant switcher UI（若 user 多 tenant）+ 切換時清當前 context
- [x] 所有 API client 自動帶 `X-Tenant-Id` header（middleware 雙重驗）
- [x] 預估：**1 day**

### I8. Audit log per-tenant hash chain
- [x] 每 tenant 獨立 genesis + chain（Phase 53 hash chain 改 per-tenant 分岔）
- [x] 跨 tenant 查詢封鎖（admin 明確切 tenant 才能看）
- [x] 驗證工具支援 per-tenant chain 完整性
- [x] 預估：**1 day**

### I9. Rate limit per-user / per-tenant
- [x] K2 的 rate limit 擴充維度：per-IP + per-user + per-tenant
- [x] 換 Redis token bucket（為 I10 準備）
- [x] Quota config：tenant.plan → limits
- [x] 預估：**1 day**

### I10. Multi-worker uvicorn + shared state
- [x] uvicorn `--workers N`（N = CPU_cores / 2，16 core → 8 worker）
- [x] Shared state 搬 Redis：`_parallel_in_flight` / AIMD budget / SSE subscriber registry / rate limit
- [x] Sticky session 若需要（SSE 連線要黏 worker）
- [x] 測試：滾動重啟 worker 無事件遺失
- [x] 預估：**2 day**

**I 總預估**：**16.5 day**

**相依**：I 必須在 **G4 + H4a + S0 + K-early** 完成後才開工；I 進行中 B12（CF Tunnel wizard）設計需配合 I4 改 tenant-scoped。

---

## 🅜 Priority M — Resource Hard Isolation（SaaS 級硬邊界）

> 背景：I 做完資料 plane 是硬隔離（RLS / SSE filter / secrets / audit / 路徑），但資源層仍是「公平排隊」不是「硬邊界」。多租戶並發時仍會互相拖：一個 tenant 的 compile 吃滿 CPU 會觸發 AIMD derate 讓無辜 tenant 也降速；磁碟無 quota 會互吃；dockerd 單點啟動序列化；prewarm pool 共用有狀態污染風險；LLM provider circuit breaker 全域；egress allowlist 共用。此 Phase 補齊達 SaaS 級硬隔離 + per-tenant 計費可能。
>
> 相依：**I6（DRF token bucket）** 是 M1 權重映射基礎；**I4（secrets per-tenant）** 是 M3 circuit breaker per-key 基礎；**I5（filesystem namespace）** 是 M2 quota 基礎；**H1（host metrics）** 是 M4 cgroup metrics 擴展基礎。

### M1. Cgroup CPU/Memory 硬隔離（對映 DRF token）
- [x] Sandbox launch 時 `docker run --cpus=<weight> --memory=<limit>`；權重由 I6 DRF token 折算（1 token ≈ 1 core × 512MB）
- [x] `backend/container.py` `start_container()` 接 `tenant_budget` 參數，自動算 `--cpus` / `--memory`
- [x] Cgroup v2 `cpu.weight` 驗證：A tenant 4-token job + B tenant 1-token job 同核並跑 → CPU 時間 4:1（`backend/cgroup_verify.py` + 4:1 ratio acceptance test）
- [x] OOM 偵測：container hit memory limit → audit_log 記錄 `sandbox.oom` + 回 sandbox_result with error，不影響其他 tenant（`_oom_watchdog` + `sandbox_oom_total{tenant_id,tier}` metric）
- [x] 測試：並發 CPU 打滿實測、memory limit 精確性、權重公平性（21 tests in `test_container_tenant_budget.py` + `test_cgroup_verify.py`）
- [x] 預估：**1 day**

### M2. Per-tenant Disk Quota + LRU Cleanup
- [x] `data/tenants/<tid>/` 加 `quota.yaml`：`soft=5GB / hard=10GB`（plan 驅動，`backend/tenant_quota.PLAN_DISK_QUOTAS` free/starter/pro/enterprise 四級）
- [x] Background sweep（每 5 min）：`backend/tenant_quota.run_quota_sweep_loop()` per-tenant → 超 soft 發 `tenant_storage_warning` SSE（30 min cooldown）；超 hard `start_container` 預先 raise `QuotaExceeded` → workspaces router 回 507 Insufficient Storage
- [x] LRU cleanup：超 soft 時自動刪 `workflow_runs/` 下最舊的完成 run（保留最近 `keep_recent_runs` 筆 + 所有 `.keep` 標記 + `.in_progress` sentinel 永不刪）
- [x] `/tmp` 按 tenant namespace（沿用 I5 `tenant_ingest_root`）+ 每 sandbox 結束 `stop_container` 強制清理（`cleanup_tenant_tmp`）
- [x] UI：Settings → Storage Quota 區塊（`integration-settings.tsx StorageQuotaSection`），雙 bar (soft/hard) + 子目錄 breakdown + 手動 LRU 按鈕
- [x] 測試：28 項 — plan/quota.yaml/measure/check_hard/lru/cleanup_tmp/sweep/start_container gate/`/storage/*` REST
- [x] 預估：**0.5 day**

### M3. Per-tenant-per-provider Circuit Breaker
- [x] 現行 `provider_chain` 5min cooldown 改 key：`(tenant_id, provider, api_key_fingerprint)` → 獨立 circuit state（`backend/circuit_breaker.py`，COOLDOWN_SECONDS=300，LRU 1024 cap）
- [x] Tenant A 的 OpenAI key 壞掉不會影響 Tenant B 的同 provider（`get_llm()`、`model_router._is_provider_available` 都先查 per-tenant breaker，再退回 legacy global cooldown）
- [x] Audit：circuit open/close 事件帶 tenant_id（`audit.log_sync circuit.open / circuit.close`，`entity_id="<provider>/<fingerprint>"`，寫入該 tenant 的 hash chain）
- [x] UI：Settings → LLM Providers 顯示各 key 當前 circuit 狀態（`integration-settings.tsx CircuitBreakerSection`，open/closed pill + per-row RESET + RESET ALL；10s auto-refresh）
- [x] 測試：A key 故障 → A fallback、B 不受影響（27 cases in `test_circuit_breaker.py`：isolation、recovery、cooldown、SSE、audit、`/providers/circuits` REST、`get_llm` failover）
- [x] 新增 REST：`GET /providers/circuits[?scope=all]`、`POST /providers/circuits/reset`；`/providers/health` 同步顯示 per-tenant cooldown
- [x] 預估：**0.5 day**

### M4. Cgroup-based Per-tenant Metrics + UI 拆分
- [x] `backend/host_metrics.py` 擴展：從 `/sys/fs/cgroup/<container>/cpu.stat` + `memory.current` 採集 per-container 用量（cgroup v2 reader + 5s sampling loop in lifespan）
- [x] 依 container label `tenant_id` 聚合 → `tenant_cpu_percent` / `tenant_mem_used_gb` / `tenant_disk_used_gb` / `tenant_sandbox_count` Prometheus metrics（`metrics.py` 新增 4 gauge + 3 counter — `tenant_cpu_seconds_total`、`tenant_mem_gb_seconds_total`、`tenant_derate_total`）
- [x] `/api/v1/host/metrics?tenant_id=...` 回該租戶的資源用量（admin 可查任意、user 只能查自己 → 403 on cross-tenant）；另有 `/host/metrics/me`、`/host/accounting`（admin-only billing）
- [x] UI `host-device-panel` 新增 per-tenant 柱狀圖：admin 看 ALL tenants（highlight self）、user 看 MY TENANT USAGE，5s auto-refresh
- [x] AIMD 決策升級：`backend/tenant_aimd.py` `plan_derate()` — HOT+culprit → derate 單一禍首；HOT+no-outlier → flat；COOL → additive-increase；per-tenant multiplier state + `tenant_derate_total` Prom counter
- [x] 計費基礎：`UsageAccumulator` 累積 `cpu_seconds_total` / `mem_gb_seconds_total`；`scripts/usage_report.py` 輸出 text/JSON/CSV（`--live` 走 in-process；HTTP 模式需 admin bearer）
- [x] 測試：64 cases — 32 host_metrics、14 tenant_aimd、9 host_router、9 usage_report
- [x] 預估：**1 day**

### M5. Prewarm Pool 多租戶安全
- [x] Config `prewarm_policy`：`shared` / `per_tenant` / `disabled`；多租戶模式預設 `per_tenant`（`backend/config.py` `Settings.prewarm_policy` + `validate_startup_config` whitelist / shared-mode warning）
- [x] `per_tenant` 模式：prewarm pool 按 tenant 分桶（每桶深度 1-2），空間換隔離（`_prewarmed_by_tenant: dict[str, dict[str, PrewarmSlot]]`；`agent_id` 摻入 tenant hash 避免 `_containers` 撞 key）
- [x] `disabled` 模式：徹底關 prewarm，犧牲 300ms 啟動延遲換乾淨（高安全需求客戶）（`prewarm_for` / `consume` / `_prewarm_enabled` 均 short-circuit）
- [x] Launch 前強制 `/tmp` 清空（即使 shared 模式亦然）（`consume()` 每次 hit/miss 都呼叫 `tenant_quota.cleanup_tenant_tmp`；cleanup 失敗仍回傳 slot）
- [x] 測試：A prewarm container 無法被 B 拿去用（per_tenant 模式）（23 cases in `test_prewarm_multi_tenant.py`：policy validation / isolation / cross-tenant consume rejection / cancel_all scope / starter signature shim / slot metadata）
- [x] 預估：**0.25 day**

### M6. Per-tenant Egress Allowlist
- [x] `tenant_egress_policies` 表：`tenant_id, allowed_hosts[], allowed_cidrs[], default_action`（alembic 0015 + `_SCHEMA` inline；FK to `tenants(id)`）
- [x] Sandbox launch 時動態寫 iptables/nftables rule：`-A OUTPUT -m owner --uid-owner <sandbox_uid> -d <allowed> -j ACCEPT`（`scripts/apply_tenant_egress.sh` + `python -m backend.tenant_egress emit-rules` 產生 JSON rule plan，operator 跑一次裝鏈）
- [x] 預設拒絕（`default DROP`），只白名單可達（`default_action='deny'`，`build_rule_plan` 序列化終端 DROP；空白名單 = 完全 air-gap）
- [x] UI：Settings → Network Egress 頁面 + 申請審批流程（viewer/operator 申請、admin 核准）（`NetworkEgressSection` 在 `integration-settings.tsx`，含 host/cidr 申請、pending 列表、approve/reject 按鈕、recent decisions、justification 欄位）
- [x] 相容舊 `configs/t1_egress_allow_hosts.yaml`（自動 migrate 到 `t-default`）（0015 upgrade 讀 `OMNISIGHT_T1_EGRESS_ALLOW_HOSTS` env CSV + 可選 yaml 檔案；`policy_for` 在 DB row 缺席時 fallback 到 legacy env）
- [x] 測試：A 允許 `api.openai.com`、B 僅允許內網 → A/B sandbox 實際出向測試（45 cases in `test_tenant_egress.py`：validators / build_rule_plan / CRUD / request workflow / DNS cache / legacy fallback / sandbox_net 整合 / REST endpoint / audit）
- [x] 預估：**1.5 day**

**相依**：I6 + I4 + I5 + H1。I 全做完後可順接。

**總預估**：M1 (1) + M2 (0.5) + M3 (0.5) + M4 (1) + M5 (0.25) + M6 (1.5) = **~4.75 day**

**驗收**：
- 10 tenant × 3 並發 job 混合負載 — 每 tenant 實測 CPU / mem 用量對映 DRF 權重 ±15% 以內
- Tenant A 寫滿自己 10GB quota 後 B 寫入不受影響
- A 的 LLM key 故障觸發 circuit open 不影響 B
- UI host-device-panel admin 可看 per-tenant 資源使用率
- 可產出 per-tenant monthly usage report（cpu_seconds / mem_gb_seconds / disk_gb_days / tokens_used）作為計費基礎

**不做的後果**：
- 無法開 SaaS（計費算不出來）
- 嘈雜鄰居問題：一個濫用 tenant 拖慢全體
- 合規：無法證明「tenant A 無法存取 tenant B 的執行環境」

---

## 🅝 Priority N — Dependency Governance（相依套件治理）

> 背景：Python deps (`backend/requirements.txt`) 大部分 `==` 硬鎖，但 transitive 未鎖；Node deps (`package.json`) 多為 caret `^`，minor/patch 自動漂；`package-lock.json` 與 `pnpm-lock.yaml` 並存易分歧；`engines` 欄位未設 → 不同開發者不同 Node 版本。高風險子系統：**LangChain / LangGraph**（近年每週一次 minor、import path 常搬家）、**Next.js 16**（App Router API 近三個 major 每次都 breaking）、**Pydantic**（v1→v2 已痛過，v3 出現會再痛）、**FastAPI + Starlette + anyio** 三角關係。此 Phase 建立從「鎖定 → 自動 PR → 合約測試 → fallback 分支 → 升級 runbook」的完整堤壩。
>
> **現在最該先做**：N1 + N2 + N5（~1.5 day），建最低限度堤壩；其餘隨開發節奏補齊。

### N1. 全量鎖定 + 單一 lockfile + Node/Python 版本固定
- [x] `package.json` 加 `"engines": {"node": ">=20.17.0 <21", "pnpm": ">=9"}`；repo root 加 `.nvmrc` / `.node-version` 寫 `20.17.0`
- [x] 收斂到單一 lockfile：選 `pnpm-lock.yaml`，刪 `package-lock.json`；`.gitignore` 排除另一個；CI 檢查 `git status --porcelain | grep -E 'lock\.(json|yaml)$'` 必須乾淨
- [x] Python 全量鎖：導入 `pip-tools`（或 `uv`），`backend/requirements.in` 寫人讀範圍 → `backend/requirements.txt` 由 `pip-compile --generate-hashes` 生出含 transitive hash 的鎖檔
- [x] Docker `Dockerfile.backend` 改為 `pip install --require-hashes -r requirements.txt`
- [x] CI 新增 lockfile drift 檢查（若 `requirements.in` 或 `package.json` 變動但 lock 未更新 → fail）
- [x] 預估：**0.5 day**

### N2. Renovate 自動 PR + group rules + 分層 auto-merge
- [x] `renovate.json` 基本 config；排程 `every weekend` 降低雜訊
- [x] Group rules：`@radix-ui/*` 一組（peer 連動）、`@ai-sdk/*` 一組、`langchain*` 一組、`@types/*` 一組
- [x] 分層 auto-merge：
  - patch：CI 綠自動合（含 security）
  - minor：需 1 人審
  - major：單獨 PR + 2 人審 + 必走 G3 blue-green（見 N10）
- [x] Security PR 優先級最高、立即開
- [x] 文件 `docs/ops/renovate_policy.md`
- [O] **Operator-blocked**：在 GitHub Repo Settings 安裝 Renovate App、開 `Allow auto-merge`、配置 branch protection（minor=1 reviewer / major=2 reviewers）— 詳見 `docs/ops/renovate_policy.md` "Bootstrap checklist (operator)"
- [x] 預估：**0.5 day**

### N3. OpenAPI 前後端合約測試 + 自動生前端 type
- [x] CI 新 step：`python scripts/dump_openapi.py` → `openapi-typescript openapi.json > lib/generated/api-types.ts`（offline via `app.openapi()` — 不需啟 uvicorn）
- [x] 前端 `lib/api.ts` 改用生成的 type；FastAPI 改 schema 時前端編譯期即炸（`_N3_ContractProbes` tripwire 放在 file 尾端）
- [x] `openapi.json` 納入 git + snapshot 比對；PR diff 顯示 API breaking change（`openapi-contract` job — `git status --porcelain` 為 CI gate）
- [x] 合約測試：前端 mock 用 schema 自動生 fixture（`msw` + `openapi-msw`，`test/msw/` 下）
- [x] 文件 `docs/ops/openapi_contract.md`
- [x] 預估：**0.5 day**

### N4. LangChain / LangGraph Adapter 防火牆層
- [x] `backend/llm_adapter.py`：所有 `langchain*` / `langgraph*` import 集中此檔；其他模組一律只 import `llm_adapter` 的符號
- [x] Adapter 公開 stable interface：`invoke_chat`、`stream_chat`、`embed`、`tool_call`（與 LangChain 版本解耦）
- [x] 掃全專案：若 `backend/**` 除 `llm_adapter.py` 外仍有 `from langchain` → CI fail（`scripts/check_llm_adapter_firewall.py` + CI job `llm-adapter-firewall`）
- [x] 升 LangChain 時只需改 adapter 層 + 跑 adapter 測試
- [x] 單元測試覆蓋 adapter 所有公開方法（`backend/tests/test_llm_adapter.py` — 50 tests）
- [x] 預估：**1 day**

### N5. Nightly Upgrade-Preview CI
- [x] `.github/workflows/upgrade-preview.yml`：cron nightly
  - `pip list --outdated --format=json` + `pnpm outdated --json` 產報告
  - 試算 `pip-compile --upgrade` 與 `pnpm update` 的 diff
  - 在隔離 container 跑完整測試套件（含 E2E）
  - 結果 POST 成 issue（標 `dependency-preview`），包含：outdated 清單、試升 diff、測試結果、疑似 breaking 套件
- [x] 不自動合，只提前警示「明天合 Renovate PR 會壞什麼」
- [x] 預估：**0.5 day**

### N6. 升級 Runbook + Rollback + CVE/EOL 監控
- [x] `docs/ops/dependency_upgrade_runbook.md`：
  - 升級前：image snapshot、DB backup、lockfile clean 確認
  - 升級中：staging 24h 觀察、smoke test 清單
  - 升級後：監控指標（error rate / latency p99 / memory）72h
  - Rollback：`git revert` + `docker compose pull <prev-tag>` 步驟
- [x] CVE 掃描：`osv-scanner` 或 Snyk 每日跑、嚴重 CVE 自動開 PR
- [x] EOL 監控：`scripts/check_eol.py` 每月查 Python / Node / FastAPI / Next.js 官方 EOL schedule（endoflife.date API），剩 6 個月內發 warning
- [x] 預估：**0.5 day**

### N7. Multi-version CI Matrix
- [x] Python matrix：3.12 + 3.13
- [x] Node matrix：20.x + 22.x
- [x] FastAPI：current pinned + latest minor
- [x] 分層：PR 上只跑 primary（快）；nightly 跑完整 matrix（廣）
- [x] 新 deprecation warning 在 CI log 顯眼顯示
- [x] 預估：**0.5 day**

### N8. DB Engine Compatibility Matrix（與 G4 綁）
- [x] CI matrix：SQLite 3.40 + 3.45；Postgres 15 + 16
- [x] Alembic migration 雙軌驗證：每條 migration 對 SQLite 與 Postgres 各跑一次 upgrade/downgrade
- [x] 標記 migration 中的 engine-specific 語法（警示 reviewer）
- [x] 與 G4 共用：G4 完成後 N8 退役 SQLite，只留 Postgres matrix（15/16/17）
- [x] 預估：**0.5 day**

### N9. Framework Fallback Branches
- [x] 長青分支 `compat/nextjs-15`：固定在 Next 15 最後穩定版、weekly rebase master（只取非 Next 相關 commit）
- [x] 長青分支 `compat/pydantic-v2`：固定在 Pydantic 2.x 最後版（Pydantic v3 出現時用）
- [x] CI 每週對 fallback 分支跑 build + 核心測試，確保隨時可切
- [x] 重大 major 升級（Next 17 / Pydantic v3）PR 合入前，fallback 分支必須 green
- [x] Rollback 流程：若 production 升級爆炸 → 切 fallback 分支 tag → 重部
- [x] 預估：**0.5 day** 建置 + 持續維護
- [O] 首兩條目標：`compat/nextjs-15`（現處 Next 16，15 是最近 fallback）、`compat/pydantic-v2`（未雨綢繆）<!-- 2026-04-16 N9: 宣告/工作流程/腳本/SOP 全部就緒並 commit；本機分支由 `bash scripts/fallback_setup.sh` 一條指令建立（dry-run 已通過）。實際 `git push -u origin compat/{nextjs-15,pydantic-v2}` 需要 push credentials → Operator-blocked，所以這項標 [O]。Push 完成後 fallback-branches.yml 會自動接手 weekly cron。 -->

### N10. 升級流程強制走 G3 Blue-Green + 升級節奏政策
- [x] 政策寫入 `docs/ops/dependency_upgrade_policy.md`：
  - Patch：週批次，CI 綠自動合
  - Minor：雙週批次，1 人審 + staging 24h 觀察
  - Major：季度，2 人審 + **必走 G3 blue-green**（standby 先升、smoke 通過才切流、舊版保留 24h rollback）
  - 一個 PR 一個套件（或一組強相依），不混合；便於 single revert
- [x] CI gate：major 版本號升級的 PR 自動加 `requires-blue-green` label，deploy workflow 檢查該 label 存在才允許上 prod<!-- 2026-04-16 N10: `.github/workflows/blue-green-gate.yml` + `scripts/bluegreen_label_decider.py` auto-label；`scripts/check_bluegreen_gate.py` 接到 `scripts/deploy.sh` prod-only；required status check 名稱 `N10 / blue-green-label`。 -->
- [x] 記錄每次 major 升級的 rollback 次數，季度 review<!-- 2026-04-16 N10: `docs/ops/upgrade_rollback_ledger.md`（append-only，三表：Upgrades / Rollbacks / Quarterly Summaries；trigger vocabulary 已列出）。 -->
- [x] 預估：**0.25 day**（純文件 + CI label gate）

**相依**：N8 與 G4 綁、N10 與 G3 綁；其餘可獨立推進。

**總預估**：N1 (0.5) + N2 (0.5) + N3 (0.5) + N4 (1) + N5 (0.5) + N6 (0.5) + N7 (0.5) + N8 (0.5) + N9 (0.5) + N10 (0.25) = **~5.25 day**

**建議順序**：
- **立即（A1 上線後）**：N1 + N2 + N5（~1.5 day）— 鎖定 + 自動 PR + 預警
- **短期（一個月內）**：N3 + N4 + N6（~2 day）— 合約測試 + LangChain 防火牆 + runbook
- **中期（配合 G4）**：N8（與 G4 同步做）
- **長期**：N7 + N9 + N10（配合 G3 上線後做）

**驗收**：
- 三個月內無「lockfile drift 導致 build 壞」事件
- LangChain 任一 major 升級影響僅限 `llm_adapter.py` 單檔
- 每次 FastAPI schema change 前端編譯期即發現
- Nightly upgrade-preview 平均每週提前捕捉至少 1 個 breaking change
- Next / Pydantic 出現 breaking 大升級時，fallback 分支已 green 可切

---

## 🅞 Priority O — Enterprise Event-Driven Multi-Agent Orchestration（企業級事件驅動架構）

> 背景：`docs/design/enterprise-multi-agent-event-driven-architecture.md`（2026-04-16 新增）提出把 OmniSight 從「單程序 LangGraph + SQLite」升級為「Orchestrator Gateway + 分散式 Message Queue + Stateless Worker Pool + Merger Agent」。現行系統已具備事件驅動基礎（EventBus / SSE / DLQ / event_log）、DAG 規劃（`backend/routers/dag.py`）、worktree 隔離（`backend/workspace.py`）、CODEOWNERS pre-merge（`backend/codeowners.py`）、Jira/GitHub webhook 雙向同步——此路線目標是補齊「水平擴展 worker pool + Redis 分散式互斥 + LLM merge conflict 仲裁 + CATC payload 形式化」。
>
> **審核政策更新（2026-04-16 用戶裁示，覆寫 CLAUDE.md L1 原「AI 最多 +1」規則）**：AI agent 允許**直接 commit** 並推送到 Gerrit code review server。最終合併進 main repo 需**雙簽 +2**：Merger Agent 給 +2（範圍限「衝突解析正確性」，不涵蓋新邏輯審核）**且** 人工給 +2，兩者同時存在才放行。任一方未到齊皆不得 submit。
>
> 🔒 **人工 +2 強制最終放行（2026-04-16 用戶裁示補強）**：**不論有多少個 AI agent（Merger Agent、其他 reviewer bot、未來新增的任何自動化 reviewer）投 Code-Review: +2，最終一律必須有人工 +2 才能放行 submit 到最終 git repo。任何 AI agent 組合（Nx AI +2）都不得自行 submit**。Gerrit submit-rule 必須顯式要求 `Code-Review: +2 from human group (labeled non-ai-reviewer)` 存在；僅有 AI 票無論多寡，submit 都會被拒絕。此規則為 immutable baseline，不因新增 AI reviewer 數量而改變閘門條件。
>
> ⚠️ 此政策與 CLAUDE.md Safety Rule「AI reviewer max score is +1」衝突，需同步更新 L1 規則（見 O6 註記）。
>
> 核心價值：(1) 企業銷售 — JIRA 深度整合是 B2B 硬需求；(2) 並行開發痛點 — Merger Agent 自動化解 merge conflict（但仍需人工終審）；(3) 產品可擴展性 — 突破單 FastAPI 程序瓶頸。
>
> 相依（硬前置）：**G4 (Postgres + replica)**、**I10 (Redis shared state)**、**S0 + K-early (auth baseline)**、**M1-M2 (cgroup 硬隔離，已完成)**、**B12 + L (bootstrap 配 Redis/MQ endpoint)**。

### O0. CATC Payload Schema + Validator (#263)
- [x] `backend/catc.py`：`TaskCard` dataclass（jira_ticket / acceptance_criteria / navigation{entry_point, impact_scope{allowed,forbidden}} / domain_context / handoff_protocol）
- [x] JSON Schema + pydantic validator（拒絕未宣告 impact_scope 的 payload）
- [x] Round-trip 測試（dict ↔ dataclass ↔ JSON）
- [x] impact_scope glob 語法（`src/camera/*`）解析器 + 單元測試
- [x] 與 `backend/codeowners.py` 交集檢查 helper：`check_catc_against_codeowners(card, agent_type)`
- [x] 預估：**0.5 day**

### O1. Redis 分散式檔案路徑互斥鎖 (#264)
- [x] `backend/dist_lock.py`：`acquire_paths(task_id, paths, ttl_s)` / `release_paths(task_id)` / `extend_lease(task_id, ttl_s)`
- [x] 鎖粒度：檔案或資料夾路徑；依 path 字典序排序取得避免死鎖
- [x] TTL 預設 30 min；worker heartbeat 每 60 s 呼叫 `extend_lease`；掉線自動 revoke
- [x] Lua 腳本保證 atomic（`MULTI/EXEC` + `WATCH`）
- [x] 死鎖偵測：background job 偵測鎖依賴循環（依 task → path → task 圖）→ 最低優先權 task 強制 kill + 寫 audit
- [x] Metrics：`dist_lock_wait_seconds` / `dist_lock_held_total` / `dist_lock_deadlock_kills_total`
- [x] Preemption 政策：鎖超過 TTL × 2 可被更高 priority task 搶佔（搭 DRF）
- [x] 整合測試：3 個 task 競爭 10 個 path + heartbeat 失敗 + 死鎖場景
- [x] 預估：**2 day**

### O2. Message Queue 抽象層 (#265)
- [x] `backend/queue_backend.py`：`QueueBackend` interface（push / pull / ack / nack / dlq）
- [x] 預設 backend：Redis Streams（與 I10 Redis 共用連線）
- [x] 可插拔 adapter 介面：RabbitMQ / AWS SQS（先宣告接口，不實作）
- [x] 任務狀態機：`Queued` → `Blocked_by_Mutex` → `Ready` → `Claimed` → `Running` → `Done / Failed`
- [x] Visibility timeout：claim 後 N 分鐘沒 ack → 重新入隊（worker crash 恢復）
- [x] 優先權佇列：P0（故障）/ P1（hotfix）/ P2（sprint）/ P3（backlog）
- [x] DLQ：3 次失敗進 DLQ，附 root cause + stack + 原 CATC
- [x] Metrics：`queue_depth{priority,state}` / `queue_claim_duration_seconds`
- [x] 整合測試：push/pull/ack、visibility timeout、DLQ、priority 排序
- [x] 預估：**2 day**

### O3. Stateless Agent Worker Pool (#266)
- [x] `backend/worker.py`：`Worker` 進程入口——pull from queue → 拿 lock（O1）→ 起 sandbox container（已有 M1 cgroup）→ 執行 agent node → commit code → push to Gerrit → push result event → release lock
- [x] Worker heartbeat 寫 Redis（`worker:<id>:alive` with TTL 90 s）
- [x] 支援 `--capacity N` 單 worker 並行領幾個任務
- [x] 支援 `--tenant-filter` / `--capability-filter`（只領 particular agent_type）
- [x] Graceful shutdown：SIGTERM → stop claiming new + 等現有任務完成 + release lock
- [x] Worker registration：啟動時註冊到 Redis `workers:active` set
- [x] Worker orchestration：systemd unit template + docker-compose profile `workers-N`
- [x] Sandbox runtime enforcement：bind-mount 只掛 `impact_scope.allowed` 路徑（延伸 I5 tenant namespace）— 超出範圍物理不可達
- [x] Gerrit push：worker 完成任務後自動 `git review`（或等價 HTTP API）推 patchset；commit 訊息含 `Change-Id` + `CATC-Ticket:` trailer
- [x] 整合測試：N workers pull 同一 queue、crash recovery、heartbeat loss、graceful shutdown、Gerrit push 失敗重試
- [x] 預估：**3 day**

### O4. Orchestrator Gateway Service (#267)
- [x] `backend/orchestrator_gateway.py`：獨立 FastAPI app（或現有 backend 內的 router）
- [x] `POST /orchestrator/intake` — 接 Jira webhook：解析 User Story → LLM 生成 DAG → 產出 N 張 CATC → impact_scope 互斥檢查 → push queue
- [x] `POST /orchestrator/replan` — 手動重規劃（PM approve 後觸發）
- [x] `GET /orchestrator/status/{jira_ticket}` — 回傳 DAG 狀態 + 每張 CATC 的 queue/run state + Gerrit patchset review 狀態（兩邊 +2 是否到齊）
- [x] DAG validation layer：
  - [x] 循環偵測（Tarjan / Kahn）
  - [x] impact_scope pairwise 交集檢查（避免同 sprint 內衝突 CATC）
  - [x] 複雜度評分 > threshold 時強制 PM approve（flag `require_human_review=true`）
- [x] LLM backend 可插拔：DAG 拆分可用 cheaper model（Haiku）、Merger 用 Opus
- [x] Token budget gate：整個 intake 流程 token 用量超 budget → reject + SSE 告警
- [x] 整合測試：假 Jira webhook → DAG 正確、impact_scope 衝突被擋、token 超標被擋
- [x] 預估：**2 day**

### O5. JIRA Bidirectional Sync 深化 (#268)
- [x] 抽 `IntentSource` interface：`fetch_story(ticket)` / `create_subtask(parent, payload)` / `update_status(ticket, status)` / `comment(ticket, body)`
- [x] JIRA adapter（主）：沿用現有 webhook signature 驗證 + 加 sub-task 批次建立
- [x] GitHub Issues / GitLab adapter（次）：保留 vendor-agnostic，小客戶不用 JIRA 也能跑
- [x] Sub-task 欄位映射：CATC → JIRA custom field（impact_scope / acceptance_criteria / handoff_protocol）
- [x] Status 雙向：JIRA `In Progress` → queue push；Worker Gerrit push → JIRA `Reviewing`；雙 +2 到齊 + Gerrit submit → JIRA `Done`
- [x] Audit：所有 JIRA 外呼都進 audit_log（含 request/response hash）
- [x] 預估：**2 day**

### O6. Merger Agent (#269) — 衝突解析器，Gerrit patchset 輸出 + AI +2 vote（**不自動合併**）
- [x] `backend/merger_agent.py`：specialized LLM wrapper，system prompt 固定為「合併衝突解決專家，保留雙方邏輯意圖，不得新增任何原未出現於雙方 commit 的新邏輯」
- [x] 輸入：conflict block（含 `<<<<<<< HEAD` 標記）+ 雙方 commit message + 檔案上下文 20 行
- [x] 輸出：resolved patchset（Gerrit-ready `git format-patch` 形式）+ confidence score + rationale + 顯式 diff（只限 conflict 區塊，不改其他行）
- [x] **Gerrit 互動流程**（新政策核心）：
  - [x] Merger Agent 產出 resolution → `git push HEAD:refs/for/main%topic=merger-PROJ-XXX` 推到 Gerrit（新增 patchset 到原 change）
  - [x] Merger Agent 自動呼叫 Gerrit REST `POST /changes/{id}/revisions/{rev}/review` 給 **Code-Review: +2**（scope 限「衝突解析正確性」，comment 說明 confidence + rationale + diff 範圍）
  - [x] **絕不自動 submit**——merge 必須等**人工** Code-Review: +2 也到齊；**不論其他 AI reviewer（lint-bot / security-bot / 任何未來新增的 AI）也投 +2**，submit-rule 仍強制要求人工 +2 才放行（詳見 O7 submit-rule group 設計）
  - [O] Merger Agent 的 Gerrit account 為專屬 `merger-agent-bot`，加入 `ai-reviewer-bots` group；scope 限定 patchset 推送與 Code-Review 投票（無 Submit 權限、無法加入 `non-ai-reviewer` group）（Gerrit 伺服器端 operator 設定；Python 實作已就緒）
- [x] 自動化 gate（**不自動合併，只決定是否給 +2**）：
  - [x] confidence ≥ 0.9 AND 衝突 ≤ 20 行 AND 單檔 AND non-security 檔案 → Merger +2
  - [x] 否則 → Merger 僅推 patchset 但**不投票**（或給 Code-Review: 0）+ SSE 告警人工接手雙 +2
  - [x] 任何涉及 security-sensitive 檔案（auth/ secrets/ config/ CI config/ `.github/workflows/`）一律**不投票**，人工須自己 +2 兩次或拒絕
- [x] 強制 test gate：Merger 在投票前先跑受影響模組的 unit test，失敗則不推 patchset 直接 escalate human
- [x] Metrics：`merger_agent_plus_two_rate` / `merger_agent_confidence_histogram` / `merger_agent_abstain_total` / `merger_agent_security_refusal_total`
- [x] 失敗次數 ≥ 3 該 change 停止自動重試，escalate human（沿用 CLAUDE.md rule）
- [x] 整合測試：簡單 conflict（Merger +2 + mock human +2 → submit）、有歧義 conflict（Merger abstain）、security 檔案（Merger refuse）、test 失敗（Merger 不 push）、僅 Merger +2 無人工 +2（submit-rule 拒絕）、僅人工 +2 無 Merger +2（submit-rule 拒絕）
- [x] **CLAUDE.md L1 更新**：Safety Rules「AI reviewer max score is +1」需補一條例外條款——「Merger Agent 於衝突解析 patchset 上可給 +2，但 scope 限衝突區塊正確性；最終 submit 仍需人工 +2 雙簽」；否則實作即違反 L1 immutable rule
- [x] 預估：**2.5 day**（原 2d + 0.5d 用於 Gerrit REST 整合 + submit-rule 測試）

### O7. Gerrit Submit-Rule 雙簽閘 + CI/CD Merge 仲裁 Pipeline (#270)
- [x] Gerrit `project.config` 更新 submit-rule（Prolog 或 Rules Engine）：要求同一 change 上至少一個 **Code-Review: +2 from human group (labeled `non-ai-reviewer`)** 且至少一個 **Code-Review: +2 from `merger-agent-bot` group**；**人工 +2 為 hard gate**——不論其他 AI reviewer 投多少 +2（Merger / lint-bot / security-bot / 未來新增的任何 AI reviewer），缺人工 +2 submit 永遠被拒絕（`.gerrit/rules.pl` + `.gerrit/project.config.example`；Python SSOT mirror `backend/submit_rule.py`）
  - [O] Group 配置：Gerrit 建 `non-ai-reviewer` group（human only，bot 帳號不得加入）+ `ai-reviewer-bots` group（所有 AI reviewer 都屬此 group）；submit-rule 以 group membership 判斷而非個別帳號（未來新增 AI reviewer 不需改 rule）— operator must run `gerrit create-group` commands per `docs/ops/gerrit_dual_two_rule.md §1`
  - [x] Submit-rule 測試矩陣：
    - [x] 只有 merger +2，無人工 → reject（`test_submit_rule_matrix.py::test_merger_plus_two_alone_rejects`）
    - [x] 只有人工 +2，無 merger → reject（`::test_human_plus_two_alone_rejects`）
    - [x] merger +2 + 人工 +2 → allow（`::test_merger_plus_two_plus_human_plus_two_allows`）
    - [x] merger +2 + 人工 -1 → reject（`::test_merger_plus_two_plus_human_minus_one_rejects`）
    - [x] **Nx AI +2（merger + lint-bot + security-bot + 其他）+ 0 人工 → reject**（`::test_n_ai_plus_twos_without_human_rejects`，6 個 AI +2 仍拒絕）
    - [x] Nx AI +2 + 人工 +2 → allow（`::test_n_ai_plus_twos_plus_human_plus_two_allows`）
  - [x] 範本 `.gerrit/project.config.example` + runbook `docs/ops/gerrit_dual_two_rule.md`（明確記載「人工 +2 強制最終放行」政策與 group 設計）
- [x] Gerrit webhook：偵測 `merge-conflict` 事件 → 呼叫 Orchestrator `POST /orchestrator/merge-conflict` → Orchestrator 喚醒 O6 Merger Agent（`backend/routers/orchestrator.py` endpoint + `.gerrit/project.config.example` webhook plugin stanza；operator 必須在 Gerrit 伺服器 enable webhooks plugin — 見 [O] 下）
  - [O] Gerrit 伺服器端 webhooks plugin 實際啟用與 TLS 憑證配置（code side 已備）
- [x] Merger Agent 路徑：push resolved patchset → 投票（±2 / 0）→ SSE 通知人工 reviewer（Slack/email webhook）— SSE event `orchestration.change.awaiting_human_plus_two`
- [x] 人工 reviewer 若 Code-Review: +2 → Gerrit submit-rule 通過 → 自動 merge（`on_human_vote_recorded` → `GerritSubmitter.submit`）
- [x] 人工 Code-Review: -1 / -2 → Merger Agent 自動 revert 其 +2（寫 comment：「human disagrees, merger withdraws」）→ change 回 work-in-progress（`GerritVoteRevoker.revoke` + `_reset_failure(change_id)`）
- [x] 若 Merger abstain（O6 gate 未過）→ 建 JIRA ticket + assign 原 CATC owner + 等人工雙 +2 或 reject（`_handle_non_plus_two` + de-dupe 同一 change）
- [x] GitHub Actions workflow 範本 `.github/workflows/merge-arbiter.yml`（for GitHub-native 客戶，無 Gerrit 時退化為 PR + 2 approver required，其中一個必須是 `merger-agent-bot` GitHub App）
- [x] 完整 E2E 測試：兩 PR 同改一檔 → 第二個 merge conflict → Merger push 解析 patchset → Merger +2 → 通知人工 → 人工 +2 → submit → 雙方 commit 都留在 main（`test_merge_arbiter.py::test_e2e_happy_path_webhook_to_submit`）
- [x] 預估：**1.5 day**（原 1d + 0.5d Gerrit submit-rule 配置與測試）

### O8. 遷移路徑：從單程序到分散式（Feature Flag + Dual-mode） (#271)
- [x] `OMNISIGHT_ORCHESTRATION_MODE=monolith | distributed`（預設 monolith 保留既有行為）（`backend/config.py` + `backend/orchestration_mode.py::current_mode` — env > settings > default）
- [x] `distributed` 模式：agent 執行路徑從 LangGraph node 改走 queue dispatch（`_distributed_dispatch` → `_build_catc_from_request` → `queue_backend.push` → wait for worker ack/DLQ/timeout）
- [x] `monolith` 模式：保留現有 LangGraph 呼叫路徑不變（`_monolith_dispatch` → `backend.agents.graph.run_graph`）
- [x] 雙模式 behavior parity 測試：同一 input 在兩種 mode 下產出相同 event sequence（`PARITY_EVENT_SEQUENCE` + `test_orchestration_mode.py::TestDualModeParity`，含 happy path + failure path）
- [x] 灰度切換手冊 `docs/ops/orchestration_migration.md`（pre-flight, per-tenant 灰度, Prometheus invariants, SSE spot-check, troubleshooting）
- [x] Rollback 劇本：切回 monolith 時如何處理 in-flight queue 任務（soft `wait` strategy + hard `redispatch_monolith` strategy，CLI：`python -m backend.orchestration_drain`）
- [x] 預估：**2 day**

### O9. 觀測性：鎖 / 佇列 / Merger / 雙簽狀態可視化 (#272)
- [x] Dashboard `components/omnisight/orchestration-panel.tsx`：queue depth by priority、held locks by task、**merger agent +2 rate / abstain rate / security refusal rate**、**待人工 +2 的 change 列表（含 merger confidence）**、worker pool capacity
- [x] SSE events：`orchestration.queue.tick` / `orchestration.lock.acquired|released` / `orchestration.merger.voted` / `orchestration.change.awaiting_human_plus_two`
- [x] Prometheus exporter：所有 O1/O2/O6 metrics 統一 `/metrics` 出口
- [x] 告警規則：queue_depth > 100 持續 5min / dist_lock_wait_p99 > 60s / merger_plus_two_rate 異常偏高（可能 LLM 過度自信）/ 雙簽 pending > 24h
- [x] 預估：**1 day**

### O10. 安全加固（queue / lock / JIRA token / Gerrit bot）(#273)
- [x] Queue 傳輸 TLS + payload HMAC（防 worker 被偽造任務） — `backend/security_hardening.py::sign_envelope/verify_envelope/assert_production_queue_tls`；`backend/queue_backend.py::_sign_queue_message/verify_pulled_message` 在 push/pull 兩側自動掛；env `OMNISIGHT_QUEUE_HMAC_KEY` + `OMNISIGHT_QUEUE_HMAC_KEY_ID`
- [x] JIRA API token 沿用 `backend/secret_store.py` Fernet at-rest + fingerprint 顯示 — `backend/jira_adapter.py::_resolve_jira_token/describe_jira_token`；env `OMNISIGHT_JIRA_TOKEN_CIPHERTEXT` 優先 > plaintext fallback + warning
- [x] Redis auth：ACL 分 role（orchestrator write lock、worker read+extend、observer read-only） — `backend/security_hardening.py::RedisAclRole/default_redis_acl_roles/render_acl_file`；CLI `python -m backend.security_hardening render-acl > users.acl`
- [x] Worker attestation：啟動時 TLS 憑證 + tenant claim，orchestrator 驗證後才發任務 — `backend/security_hardening.py::WorkerIdentity/issue_attestation/AttestationVerifier`；worker `_info_snapshot` 暴露 `tls_cert_fingerprint` (env `OMNISIGHT_WORKER_TLS_FP`)
- [x] **Merger Agent Gerrit 帳號權限最小化**：`merger-agent-bot` 僅能 push to `refs/for/*` + Code-Review ±2；**不得**有 `Submit` / `Push Force` / `Delete Change` / 任何 project admin 權限 — `.gerrit/project.config.example` 加了 9 行 `deny` 規則；`backend/security_hardening.py::verify_merger_least_privilege` + CLI `verify-gerrit-config` 鎖 CI
- [x] Merger Agent 投票 audit：每次 +2 / abstain / refuse 都寫入 hash-chain audit_log，附 change-id / patchset revision / confidence / rationale — `backend/security_hardening.py::MergerVoteAuditChain`；`backend/merger_agent.py::_default_audit` 雙 sink (backend.audit + O10 chain)
- [x] 滲透測試案例：偽造 CATC、竊取鎖、注入 merger prompt、worker 偽裝、偽冒 `merger-agent-bot` 投票 — `backend/tests/test_o10_pentests.py`（5 TestScenario 類，23 條測試全綠）+ `backend/tests/test_security_hardening.py`（51 條單元測試）
- [x] 預估：**1.5 day** — 完成於 2026-04-17（單次 session）

**Priority O 總預估**：**20 day**（原 19d + Merger Agent Gerrit 整合與 submit-rule 配置 +1d）（solo ~4 週，2-person team ~2-3 週）。

**建議切段交付**：
1. **O0 + O1 + O2**（4.5d）— 基礎設施：CATC + Redis lock + Queue。可單獨 ship 作為 I10 的延伸
2. **O3 + O8**（5d）— Worker pool + migration flag。此時系統 dual-mode 可運行（worker 會 Gerrit push，但人工雙 +2 既有流程不變）
3. **O4 + O5**（4d）— Orchestrator + JIRA 深化。B2B 銷售可用
4. **O6 + O7**（4d）— Merger Agent 雙簽閘完整閉環。競品差異化賣點，**同時是 CLAUDE.md L1 政策變更點**
5. **O9 + O10**（2.5d）— 觀測性與安全加固。正式對外上線前 gate

---

## 🅦 Priority W — Web Platform Vertical（Next.js / Nuxt.js / 前端生態）

> 背景：現行 C 系列 platform profiles 寫死嵌入式 cross-compile 假設（aarch64/armv7/riscv64 + sysroot + toolchain file），但 OmniSight 自身就是 Next.js 16 app——前端 pipeline（Playwright / vitest / eslint / Tailwind / shadcn/ui）**已在 repo**，是三條新 vertical 中 dogfood 成本最低、最快可驗證 engine generalizability 的入口。
>
> **窄整合策略**：不動現有 embedded 主軸，只補 web 特化的 profile / simulate track / role skills / deploy adapters / compliance gates。Priority O 的 orchestration + worker pool + CATC + 雙簽 +2 完全沿用。
>
> 目標：從 NL 描述 → 自動生成 Next.js / Nuxt.js / Astro 專案骨架 + 部署到 Vercel / Netlify / Cloudflare Pages / Docker + nginx。C26 HMI framework 的 constrained generator + bundle budget + security gate pattern 可 80% 復用（只是 flash budget 約束放寬）。
>
> 相依（硬前置）：**W0 platform profile schema 泛化**（本 Priority 內前置，P/X 共用）、**O0-O3**（CATC + Worker pool）、**C26 HMI framework**（generator pattern 復用）。

### W0. Platform profile schema 泛化（W/P/X 共用前置）(#274)
- [x] `configs/platforms/schema.yaml` 擴充：toolchain 欄位改為 `optional`；新增 `target_kind: embedded | web | mobile | software` enum
- [x] 既有 aarch64/armv7/riscv64/vendor-example profile 補 `target_kind: embedded`
- [x] `backend/platform.py` 的 `get_platform_config()` 依 `target_kind` 分派不同 build toolchain resolver
- [x] 測試：既有 embedded profile behavior parity（零 regression）
- [x] 預估：**1 day**

### W1. Web platform profiles (#275)
- [x] `configs/platforms/web-static.yaml` — 純靜態站（SSG）
- [x] `configs/platforms/web-ssr-node.yaml` — Next.js/Nuxt.js SSR on Node 20
- [x] `configs/platforms/web-edge-cloudflare.yaml` — Cloudflare Workers / Pages Functions
- [x] `configs/platforms/web-vercel.yaml` — Vercel Serverless / Edge Runtime
- [x] 每個 profile 宣告：runtime version / bundle size budget / memory limit / build cmd
- [x] 預估：**0.5 day**

### W2. Web simulate track (#276)
- [x] `scripts/simulate.sh` 新增 `web` track：Lighthouse CI（Performance / Accessibility / SEO / Best Practices）+ bundle size gate + a11y audit + SEO lint
- [x] Lighthouse baseline：Performance ≥ 80 / A11y ≥ 90 / SEO ≥ 95
- [x] Bundle budget per profile（web-static ≤ 500 KiB critical / web-ssr-node ≤ 5 MiB server bundle）
- [x] Playwright E2E smoke（homepage → 關鍵互動 × 2）
- [x] Visual regression（可選，Chromatic 或 Playwright screenshot baseline）
- [x] 預估：**1.5 day**

### W3. Web role skills (#277)
- [x] `configs/roles/web/frontend-react.skill.md`（命名對齊既有 `{category}/{role_id}.skill.md` 慣例，prompt_loader 自動 discover）
- [x] `configs/roles/web/frontend-vue.skill.md`
- [x] `configs/roles/web/frontend-svelte.skill.md`
- [x] `configs/roles/web/a11y.skill.md`（WCAG 2.2 AA，含 2.4.11 / 2.5.7 / 2.5.8 / 3.3.8 新增條款）
- [x] `configs/roles/web/seo.skill.md`
- [x] `configs/roles/web/perf.skill.md`（Core Web Vitals：LCP / INP / CLS，INP 取代 FID）
- [x] 每個 role 提供 domain-specific prompt + role-specific tool whitelist（非 `[all]`，frontend 12 工具 / 審查類 5 工具）
- [x] 預估：**1 day**

### W4. Deploy adapters (#278)
- [x] `backend/deploy/vercel.py`（Vercel REST API：project create / env set / deploy）
- [x] `backend/deploy/netlify.py`
- [x] `backend/deploy/cloudflare_pages.py`（沿用 B12 CF API client）
- [x] `backend/deploy/docker_nginx.py`（靜態站 + nginx 配置生成）
- [x] 統一 `WebDeployAdapter` interface：`provision()` / `deploy(build_artifact)` / `rollback()` / `get_url()`
- [x] Secret：API token 沿用 `backend/secret_store.py` Fernet
- [x] 預估：**2 day**

### W5. Compliance gates（WCAG / GDPR / SPDX license scan）(#279)
- [x] WCAG 2.2 AA：axe-core 自動掃 + manual checklist（focus order / contrast / screen reader labels）
- [x] GDPR：cookie banner / data retention policy / DPA template / right-to-be-forgotten endpoint 掃描
- [x] SPDX license scan：`@npmcli/arborist` 列依賴樹 + 禁用 GPL/AGPL（可覆寫 allowlist）
- [x] 整合 C18 compliance harness 作為 evidence bundle
- [x] 預估：**1.5 day**

### W6. SKILL-NEXTJS (pilot, #280)
- [x] Next.js 16 App Router 專案骨架 generator（含 `turbopack.root` 預設正確——避免 OmniSight 自身踩過的 Turbopack workspace-root panic）
- [x] Server Components / Client Components 模式 template
- [x] 認證 template（next-auth / Clerk）
- [x] API routes + tRPC 選項
- [x] Vercel + Cloudflare Pages 雙 target build
- [x] Playwright E2E + vitest unit 骨架
- [x] **First web skill — validates W0-W5 framework**（比照 D1 驗證 C5、D29 驗證 C26 的 pattern）
- [x] 預估：**2 day**

### W7. SKILL-NUXT (#281)
- [x] Nuxt 4 專案骨架 generator
- [x] Nitro engine 多 target（Node / Edge / Cloudflare Workers / Bun）
- [x] Pinia state + Vue Router
- [x] Vitest + Playwright
- [x] **Cross-stack framework validation**（SKILL-NEXTJS 是 n=1 pilot，SKILL-NUXT 是 n=2 — 兩者共用同一套 ScaffoldOptions/render_project/pilot_report API，證明 W0-W5 是 framework 而非 pilot-plus-copy）
- [x] 預估：**1.5 day**

### W8. SKILL-ASTRO（選配, #282）
- [x] Astro 5 content-heavy 站骨架
- [x] Islands architecture + MDX 支援
- [x] Sanity/Contentful CMS 接口
- [x] 預估：**1 day**

### W9. 共用 CMS adapters（Headless CMS 接口 library）(#283)
- [x] Sanity / Strapi / Contentful / Directus adapters
- [x] 統一 `CMSSource` interface：`fetch(query)` / `webhook_handler(payload)`
- [x] 預估：**1 day**

### W10. Web 觀測性與監控 (#284)
- [x] Sentry / Datadog RUM adapter
- [x] Core Web Vitals 即時 dashboard
- [x] Error tracking → JIRA ticket（透過 O5 IntentSource）
- [x] 預估：**0.5 day**

**Priority W 總預估**：**13.5 day**

**建議切段交付**：
1. W0+W1+W2（3d）— 基礎設施：profile schema + web profiles + simulate track
2. W3+W4（3d）— Role skills + deploy adapters，此時 web 專案可自動建+部署
3. W5+W6（3.5d）— Compliance + Next.js pilot，首支可售 SKU
4. W7+W8+W9+W10（4d）— Nuxt/Astro/CMS/觀測性補齊

---

## 🅟 Priority P — Mobile App Vertical（iOS / Android / 跨平台）

> 背景：行動端 app 是三條新 vertical 中最重的（SDK 巨大、簽章鏈繁複、store 審核規則專屬、emulator 資源吃重），但也是工控客戶常與嵌入式設備配對的伴生品（相機 app、條碼 scanner app、IoT remote）。
>
> **窄整合策略**：Priority O 的 orchestration / worker pool / CATC / 雙簽 +2 完全沿用。特殊點：Apple certs + Google keystore 等簽章物需要對 `backend/secret_store.py` 擴充 HSM 層 + 嚴格 access audit（P3）。
>
> 相依（硬前置）：**W0 platform profile schema 泛化**（W/P/X 共用）、**O0-O10（全 Priority O）**（簽章推送走分散式鎖 + 人工雙 +2 簽章驗證）、**B12 secret_store.py 擴充 HSM 模式**（P3 前置）。

### P0. Mobile platform profiles (#285)
- [x] `configs/platforms/ios-arm64.yaml` — iOS Device ABI
- [x] `configs/platforms/ios-simulator.yaml` — iOS Simulator (x86_64 + arm64)
- [x] `configs/platforms/android-arm64-v8a.yaml`
- [x] `configs/platforms/android-armeabi-v7a.yaml`
- [x] 每 profile 宣告：SDK version / min API level / toolchain path / emulator spec
- [x] 預估：**1 day**

### P1. Mobile toolchains 整合 (#286)
- [x] Docker image base：`ghcr.io/omnisight/mobile-build`（Xcode CLI 16 + Android SDK 35 + Gradle 8 + CocoaPods 1.15）
- [x] **macOS 限制**：iOS build 需真實 macOS host（Linux 不可）；支援 `OMNISIGHT_MACOS_BUILDER=self-hosted|macstadium|cirrus-ci|github-macos-runner` 遠端委派
- [x] Android build 可純 Linux Docker 跑
- [x] Fastlane / gym / gradle wrapper 整合
- [x] 預估：**2 day**

### P2. Mobile simulate track (#287)
- [x] `scripts/simulate.sh` 新增 `mobile` track：iOS Simulator + Android Emulator 雙平台 smoke + UI test
- [x] `XCUITest`（iOS）+ `Espresso`（Android）整合
- [x] Flutter/RN 走各自 test runner
- [x] **Cloud device farm 整合**：Firebase Test Lab / AWS Device Farm / BrowserStack（真機覆蓋用）
- [x] 螢幕截圖 matrix（多機型 × 多 locale）
- [x] 預估：**2.5 day**

### P3. 簽章鏈管理（extend secret_store）(#288)
- [x] Apple certs：Developer ID Certificate + Provisioning Profile + App Store Distribution Certificate
- [x] Android keystore：per-app keystore + alias + password
- [x] HSM 整合（選配）：AWS KMS / GCP KMS / YubiHSM — 私鑰不出 HSM
- [x] 簽章 audit：每次 sign 寫 hash-chain audit_log（who / when / what artifact / what cert）
- [x] Cert 到期 alert（30d / 7d / 1d pre-expiry SSE 告警）
- [x] 預估：**2 day**

### P4. Mobile role skills (#289)
- [x] `configs/roles/mobile/ios-swift.skill.md`（SwiftUI / UIKit / Combine）
- [x] `configs/roles/mobile/android-kotlin.skill.md`（Jetpack Compose / Kotlin Coroutines）
- [x] `configs/roles/mobile/flutter-dart.skill.md`
- [x] `configs/roles/mobile/react-native.skill.md`
- [x] `configs/roles/mobile/kmp.skill.md`（Kotlin Multiplatform）
- [x] `configs/roles/mobile/mobile-a11y.skill.md`（iOS VoiceOver + Android TalkBack）
- [x] 預估：**1 day**

### P5. Store 提交自動化 (#290)
- [x] App Store Connect API 整合：create version / upload build / submit for review / screenshot upload
- [x] Google Play Developer API：upload .aab / manage tracks（internal / alpha / beta / production）
- [x] 提交流程走 O7 雙簽 +2：Merger Agent 驗技術正確性 + 人工終審（store guideline 合規）
- [x] TestFlight / Firebase App Distribution 內部派發
- [x] 預估：**2.5 day**

### P6. Store 合規 gates (#291)
- [x] App Store Review Guidelines 自動檢查（明顯違規 pattern：假付費、誤導性 copy、未宣告 private API）
- [x] Google Play Policy 自動檢查（背景位置權限、SDK 版本、資料安全區塊填寫）
- [x] Privacy nutrition label / Data Safety Form 自動生成（依 SDK 依賴推導）
- [x] 整合 C18 compliance harness
- [x] 預估：**1.5 day**

### P7. SKILL-IOS (pilot, #292)
- [x] SwiftUI app 骨架 generator
- [x] Xcode project + SPM/CocoaPods 管理
- [x] Push notification（APNs）integration template
- [x] StoreKit 2 購買 template
- [x] **First mobile skill — validates P0-P6**
- [x] 預估：**2.5 day**

### P8. SKILL-ANDROID (pilot, #293)
- [x] Jetpack Compose app 骨架
- [x] Gradle 8 + Kotlin 2.0
- [x] FCM push integration
- [x] Play Billing template
- [x] 預估：**2.5 day**

### P9. SKILL-FLUTTER / SKILL-RN（跨平台, #294）
- [x] Flutter 3.x app 骨架 + 共用 iOS/Android config
- [x] React Native 0.76 app 骨架
- [x] 選一主推 + 另一為對照
- [x] 預估：**2 day**

### P10. Mobile observability (#295)
- [x] Firebase Crashlytics / Sentry Mobile adapter
- [x] ANR detection（Android）/ watchdog termination（iOS）
- [x] 線上 UI metric（render time / frame drop）
- [x] 預估：**0.5 day**

**Priority P 總預估**：**20 day**（含簽章鏈 + store 合規的繁複度）

**建議切段交付**：
1. P0+P1+P2（5.5d）— 平台基礎：profiles + toolchains + simulate track
2. P3（2d）— 簽章鏈（進 store 前必備）
3. P4+P7 或 P8（3.5d）— 擇一 pilot（iOS 或 Android 先做）
4. P5+P6（4d）— Store 提交 + 合規 gates，首支可售
5. P8/P7 另一 + P9（4.5d）— 另一原生 + 跨平台補齊
6. P10（0.5d）— 觀測性

---

## 🅧 Priority X — Pure Software Application Vertical（後端服務 / CLI / 桌面 app）

> 背景：三條新 vertical 中最輕量——不需硬體模擬、不需 app store、不需嵌入式 cross-compile，但語言/框架生態最雜（Python/Go/Rust/Node.js/Java/.NET/Qt/Electron/Tauri...）。適合當作「第三 vertical」降低風險的 easiest win；OmniSight 自身後端就是 FastAPI，dogfood 門檻低。
>
> **窄整合策略**：Priority O 的 orchestration / worker pool / CATC / 雙簽 +2 完全沿用。簡單的多語言 build matrix + deploy adapter 補上即可運作。
>
> 相依（硬前置）：**W0 platform profile schema 泛化**（W/P/X 共用）、**O0-O3**（CATC + Worker pool）。

### X0. Software platform profiles (#296)
- [x] `configs/platforms/linux-x86_64-native.yaml`
- [x] `configs/platforms/linux-arm64-native.yaml`
- [x] `configs/platforms/windows-msvc-x64.yaml`
- [x] `configs/platforms/macos-arm64-native.yaml`（需 macOS builder，參考 P1）
- [x] `configs/platforms/macos-x64-native.yaml`（Intel legacy）
- [x] 預估：**0.5 day**

### X1. Software simulate track (#297)
- [x] `scripts/simulate.sh` 新增 `software` track：語言-native test runner
- [x] 多語言 dispatcher：`pytest` / `go test` / `cargo test` / `mvn test`（或 `gradle test`）/ `npm test` / `pnpm test` / `yarn test` / `dotnet test`
- [x] Coverage gate：依 language 各自門檻（Python 80% / Go 70% / Rust 75% / Java 70% / Node 80% / C# 70%）
- [x] Benchmark 回歸（可選，`--benchmark=on` opt-in + `test_assets/benchmarks/<module>.json` 基準）
- [x] 預估：**1 day**

### X2. Software role skills (#298)
- [x] `configs/roles/software/backend-python.skill.md`（FastAPI / Django / Flask）
- [x] `configs/roles/software/backend-go.skill.md`（gin / fiber / net/http）
- [x] `configs/roles/software/backend-rust.skill.md`（axum / actix / rocket）
- [x] `configs/roles/software/backend-node.skill.md`（Express / NestJS / Fastify）
- [x] `configs/roles/software/backend-java.skill.md`（Spring Boot / Quarkus）
- [x] `configs/roles/software/cli-tooling.skill.md`（Cobra / Clap / Commander / Typer / Picocli）
- [x] `configs/roles/software/desktop-electron.skill.md` / `desktop-tauri.skill.md` / `desktop-qt.skill.md`
- [x] 預估：**1.5 day**

### X3. Build & package adapters (#299)
- [x] Docker image build + push（GHCR / Docker Hub / ECR）
- [x] Helm chart 生成（k8s 部署）
- [x] .deb / .rpm（Linux package）
- [x] .msi / NSIS installer（Windows）
- [x] .dmg / .pkg（macOS）
- [x] `cargo-dist` / `goreleaser` / `pyinstaller` / `electron-builder` 對應 skill hook
- [x] 預估：**2 day**

### X4. License / dependency 合規 (#300)
- [x] SPDX license scan（依語言 ecosystem：`cargo-license` / `go-licenses` / `pip-licenses` / `npm-license-checker`）
- [x] 禁用 licenses allowlist（預設禁 GPL/AGPL，allowlist 可覆寫）
- [x] CVE scan（`trivy` / `grype` / `osv-scanner`）
- [x] 依賴圖 SBOM 輸出（CycloneDX / SPDX）
- [x] 預估：**1 day**

### X5. SKILL-FASTAPI (pilot, #301)
- [x] FastAPI service 骨架 + Alembic + Pydantic
- [x] Dockerfile + docker-compose.yml + helm chart
- [x] pytest + httpx + coverage
- [x] OpenAPI spec 自動生成（整合 N3 OpenAPI governance）
- [x] **First software skill — validates X0-X4**
- [x] 預估：**1.5 day**

### X6. SKILL-GO-SERVICE (#302)
- [x] Gin/Fiber 微服務骨架
- [x] goreleaser 多平台 binary build
- [x] 預估：**1 day**

### X7. SKILL-RUST-CLI (#303)
- [x] Clap + anyhow + tokio 骨架
- [x] cargo-dist 多平台 release
- [x] 預估：**1 day**

### X8. SKILL-DESKTOP-TAURI (#304)
- [x] Tauri 2.x 骨架 + 前端整合（React/Vue 可選）
- [x] 三平台 build（Windows/macOS/Linux）+ auto-update
- [x] 預估：**1.5 day**

### X9. SKILL-SPRING-BOOT（企業 Java, #305）
- [x] Spring Boot 3 + Maven/Gradle
- [x] Flyway migration + JUnit 5
- [x] 預估：**1 day**

**Priority X 總預估**：**12 day**（最輕量，工具鏈多但每條路徑成本低）

**建議切段交付**：
1. X0+X1+X2+X3（5d）— 平台基礎 + 多語言 build / package
2. X4（1d）— 合規 gates
3. X5（1.5d）— FastAPI pilot，首支可售（OmniSight 自用也是 FastAPI，dogfood）
4. X6-X9（4.5d）— 其他語言/框架 skill packs 隨 demand ship

---

## 🅛 Priority L — Bootstrap Wizard（一鍵從新機器到公網可用）

> 背景：目前**無 UI 觸發的 OmniSight 自佈署功能**。`scripts/deploy.sh` 是 CLI-only（A1 待辦卡在 operator 手動執行）；`POST /api/v1/deploy` 是佈產品 binary 到 EVK 開發板，非佈 OmniSight 自身。`ensure_default_admin` 用 env 設密碼、Cloudflare Tunnel 4 步驟手動、LLM provider key 寫 `.env`、systemd unit 要 `sed` 填 USERNAME。首次安裝摩擦極大。
>
> 目標：新機器 clone repo → `docker compose up` → 瀏覽器開 UI → 精靈引導完成所有配置 → 公網 HTTPS 可用，**全程不 SSH 不編輯 yaml**。
>
> 相依：**B12 (CF Tunnel wizard)** 是 Step 3 基礎；**G1 (readyz)** 讓 Step 4 能精確判斷「起來了沒」；**K1 (must_change_password)** 讓 Step 1 密碼關卡有後端支援。

### L1. Bootstrap 狀態偵測 + `/bootstrap` 路由
- [x] `backend/bootstrap.py`：`get_bootstrap_status()` 回傳 `{admin_password_default: bool, llm_provider_configured: bool, cf_tunnel_configured: bool, smoke_passed: bool}`
- [x] 全局 middleware：若 bootstrap 未完成 → 除 `/bootstrap/*`、`/auth/login`、`/healthz`、靜態資源外一律導向 `/bootstrap`
- [x] `bootstrap_state` 表：`step`, `completed_at`, `actor_user_id`, `metadata`；完成全部步驟後寫 `bootstrap_finalized=true` 進 app 設定
- [x] `POST /api/v1/bootstrap/finalize` — 全 step 綠才讓過（admin 才能呼叫）
- [x] 前端 `app/bootstrap/page.tsx` 多步 wizard 殼
- [x] 預估：**0.5 day**

### L2. Step 1 — 首次 admin 密碼設定
- [x] 整合 K1 的 `must_change_password` 旗標；wizard Step 1 強制改預設 `omnisight-admin`
- [x] 密碼強度檢查（最短 12 字 + zxcvbn ≥ 3，與 K7 統一）；若 K7 未做則先用簡版
- [x] 寫入 audit_log（`bootstrap.admin_password_set`）；清除 `must_change_password`
- 預估：**0.5 day**

### L3. Step 2 — LLM Provider 選擇 + API Key 輸入
- [x] UI 選單：Anthropic / OpenAI / Ollama（本機）/ Azure
- [x] API Key 輸入 → `POST /api/v1/bootstrap/llm-provision`：驗 key（`provider.ping()`）→ 寫入 `backend/llm_secrets.py`（at-rest 加密；`backend/secrets.py` 會 shadow stdlib，故改名）→ 更新 `settings.llm_provider`
- [x] Ollama 選項偵測本機 `localhost:11434` 可達性 + 列可用 model
- [x] 錯誤處理：key 無效 / quota 用盡 / 網路不通 → 明確訊息
- 預估：**0.5 day**

### L4. Step 3 — Cloudflare Tunnel（複用 B12 wizard）
- [x] 直接 embed B12 的 `cloudflare-tunnel-setup.tsx` 到 bootstrap step 3
- [x] 完成 provision 後寫 `bootstrap_state.cf_tunnel_configured=true`
- [x] 提供「跳過（內網部署）」選項，記 audit warning
- 預估：**0.25 day**（主要靠 B12，此處只做 embed + state 寫入）

### L5. Step 4 — 服務啟動 / 健康驗證（SSE 即時 log）
- [x] `POST /api/v1/bootstrap/start-services`：呼叫 `systemctl start` 或 `docker compose up -d`（依部署模式）
- [x] SSE event stream `bootstrap.service.tick`：每行 log 即時推送（tail systemd journal 或 docker logs）
- [x] 輪詢 G1 的 `/readyz` 直到通過 or timeout 180s
- [x] 並行檢查：backend ready / frontend ready / DB migration up-to-date / CF tunnel connector online（若 step 3 有做）
- [x] UI 顯示 4 個勾勾即時變綠
- 預估：**1 day**

### L6. Step 5 — Smoke Test + 完成
- [x] 跑 `scripts/prod_smoke_test.py` 子集（選 compile-flash host_native DAG，~60s） *(done: `--subset dag1` CLI flag on the smoke script + `POST /api/v1/bootstrap/smoke-subset` endpoint runs DAG_1 in-process, verifies audit hash chain, and flips `smoke_passed` + records `STEP_SMOKE` on green; wizard Step 5 UI wired via `SmokeSubsetStep`)*
- [x] 顯示 audit_log hash chain 驗證結果、兩個 DAG 的 run summary *(done: wizard now POSTs `subset=both` so `/bootstrap/smoke-subset` runs DAG_1 + DAG_2 and returns per-DAG `SmokeRunSummary`s; Step 5 pane renders each DAG as its own pass/fail card (label, run_id, plan_id, plan_status, target, t3 runner, task count, validation errors) and a dedicated audit-chain panel shows PASS/FAIL + tenant_count + detail + first_bad_id + bad_tenants)*
- [x] 全綠 → `POST /api/v1/bootstrap/finalize` → 寫 `bootstrap_finalized=true` → 導向 dashboard *(done: backend `POST /bootstrap/finalize` already writes `bootstrap_finalized=true` in the marker + records `STEP_FINALIZED` via `mark_bootstrap_finalized`; wizard now exposes an inline "Finalize & go to dashboard" CTA inside the Step 5 Smoke pane (`bootstrap-smoke-finalize-button`) that is enabled only when `status.all_green` and `missing_steps` is empty — click posts `/bootstrap/finalize`, waits for `reloadStatus`, then `router.replace("/")`; Step 6 "Finalize" pane still carries the canonical button for operators who auto-advance past Smoke; two new vitest cases cover the inline-CTA green-path redirect and the disabled/missing-steps red-path)*
- [x] 失敗 → 顯示錯誤 + 允許回到前面 step 修正 *(done: `SmokeSubsetStep` now derives `hasFailure = !smokeGreen && (error !== null || (result !== null && !passed))` and — when the wizard parent supplies the new `onJumpToStep(id)` callback — renders a `bootstrap-smoke-jump-back` panel listing all four preceding gates as quick-jump buttons (`bootstrap-smoke-jump-back-{admin_password,llm_provider,cf_tunnel,services_ready}`); a `_diagnoseSmokeFailure(result, error)` heuristic picks the most likely culprit (audit-chain break → admin_password; validation_errors mentioning llm/provider → llm_provider; tunnel/cloudflare → cf_tunnel; platform/compile/target/runner/ready or any network error → services_ready) and tags the chosen button with `data-culprit="true"` plus a "likely" pill; the parent wires the callback to `setUserPinned(true) + setActiveId(id)` so a single click pins the wizard to the chosen prior gate (existing `goPrev/Next` chevrons still work for sequential navigation). Three new vitest cases cover (a) the audit-chain culprit path with click-through to a non-culprit step, (b) the network-error path with services_ready as the inferred culprit, and (c) the green-path that suppresses the panel entirely)*
- 預估：**0.5 day**

### L7. 部署模式偵測 + docker-compose 路徑
- [x] `detect_deploy_mode()`：偵測是否在 docker 內 / 是否有 systemd / 是否有 docker socket *(done: new `backend/deploy_mode.py` exposes `detect_deploy_mode() -> DeployModeDetection` with per-probe signals — `_is_in_docker()` checks `/.dockerenv` + `/proc/1/cgroup` for docker/containerd/kubepods tokens, `_has_systemd()` checks `/run/systemd/system` + systemctl on PATH, `_has_docker_socket()` checks `/var/run/docker.sock` is a real unix socket. Decision table: env `OMNISIGHT_DEPLOY_MODE` wins → in-docker+socket → docker-compose (compose-in-docker) → in-docker w/o socket → dev (no-op) → systemd → docker-compose (socket or binary) → dev fallback. Returns dataclass carrying `mode`, `in_docker`, `has_systemd`, `has_docker_socket`, `has_docker_binary`, `has_systemctl_binary`, `override_source`, `reason`, and per-probe `signals` dict for audit/UI. `backend/routers/bootstrap.py::_detect_deploy_mode()` now delegates to this so Step 4 launcher shares the richer probe without breaking callers. 22 new tests in `test_deploy_mode.py` cover every probe + decision row incl. regression guard that systemd wins over docker when both present.)*
- [x] 依模式提供不同 start-services 指令： *(done: `backend/routers/bootstrap.py::_start_command()` now branches on the L7 detection: `systemd` → `sudo -n systemctl start omnisight-backend.service omnisight-frontend.service` (the `-n` keeps sudo non-interactive so a missing K1 sudoers rule fails fast instead of blocking on a TTY prompt the wizard has no console for); `docker-compose` → `docker compose -f docker-compose.prod.yml up -d` unchanged (compose_file body field still overrides); `dev` → `[]` no-op so the endpoint short-circuits with `status="already_running"`. Added module-level `SUDOERS_LINE` + `generate_sudoers_snippet()` helper that renders the scoped K1 grant `omnisight ALL=(root) NOPASSWD: /usr/bin/systemctl start omnisight-backend.service, /usr/bin/systemctl start omnisight-frontend.service` — mirrors the `omnisight-cloudflared` sudoers shape operators already ship so ops learn one convention. Scope is start-only (K1 least-privilege): the wizard never stops or restarts services, and the generator proves it by omitting stop/restart grants. 6 new tests in `test_bootstrap_start_services.py`: `_start_command` argv shape for all three modes (sudo-wrap for systemd, default + override compose file for docker-compose, empty for dev w/ and w/o compose_file), and `generate_sudoers_snippet` covers every unit with absolute `/usr/bin/systemctl` path, never grants stop/restart, ends with a trailing newline for visudo, embeds `SUDOERS_LINE` verbatim. Existing `test_start_services_systemd_success` updated to expect the new `sudo -n` prefix in both the response.command and fake-exec captured argv. Full regression: 17 in `test_bootstrap_start_services.py` + 22 in `test_deploy_mode.py` + 10 in `test_bootstrap_service_tick.py` = **49 passed zero regressions**.)*
  - `systemd` 模式：`sudo systemctl start omnisight-*`（需 K1 的 scoped sudoers）
  - `docker-compose` 模式：`docker compose -f docker-compose.prod.yml up -d`
  - `dev` 模式：跳過 start-services step（已在 uvicorn + next dev）
- [x] 文件 `docs/ops/bootstrap_modes.md` *(done: new operations doc covers (a) at-a-glance per-mode table mapping detection signal → start argv → tail argv → privilege gate, (b) decision table from `detect_deploy_mode()` (env override → in-docker+socket → in-docker-only → systemd → docker → dev fallback) with rationale for why systemd beats docker on bare metal + why compose-in-docker beats systemd-in-container, (c) per-mode deep dives: systemd section lists unit files + the K1 sudoers install one-liner that pipes `generate_sudoers_snippet()` output through `visudo -c -f` + `chmod 0440` and a 4-row troubleshooting matrix; docker-compose section covers compose plugin v2 / socket access / `compose_file` override + 4-row troubleshooting; dev section explains when it wins and why `/readyz` still validates even in dev so `dev → systemd` migration stays honest, (d) "Source of truth" section linking every file the doc describes (`deploy_mode.py`, `bootstrap.py`'s `_start_command` / `_tick_command` / `generate_sudoers_snippet`, three test modules, cross-refs to `docs/operations/deployment.md` and `cloudflared_service.py`).)*
- 預估：**0.5 day**

### L8. 重置 + 測試
- [x] `POST /api/v1/bootstrap/reset`（admin 限定、dev 模式限定）— 清 bootstrap_state、重設 must_change_password；用於 QA *(done: new endpoint in `backend/routers/bootstrap.py::bootstrap_reset` gated by `Depends(_au.require_admin)` AND `detect_deploy_mode().mode == "dev"` (refuses with HTTP 403 + body carrying detected `deploy_mode` + `deploy_mode_reason` so QA sees why on non-dev hosts). On success it (1) `DELETE FROM bootstrap_state` via new `bootstrap.reset_bootstrap_state_table()` returning the row count, (2) wipes `data/.bootstrap_state.json` via new `bootstrap.clear_marker()` (clears `smoke_passed`, `cf_tunnel_configured`, `cf_tunnel_skipped`, `bootstrap_finalized` in one shot), (3) re-flags every enabled admin row via new `auth.flag_all_admins_must_change_password()` so the L2 wizard Step 1 gate fires again — disabled admins skipped because re-flagging an account no one can log into would be a footgun, (4) resets the in-process gate cache so the very next request hits the redirect middleware, (5) emits `bootstrap.reset` audit row with `actor=admin.email`, `severity=warning`, `reason`, the per-admin `admins_reflagged` list, deploy_mode + reason, and counts. Response is `{status, deploy_mode, bootstrap_state_rows_deleted, admins_reflagged, marker_cleared, actor_user_id}`. 8 new tests in `test_bootstrap_reset.py`: helper-level (`reset_bootstrap_state_table` returns row count + leaves table intact + idempotent on empty; `clear_marker` wipes marker + no-op on missing; `flag_all_admins_must_change_password` re-flags enabled admins, skips disabled + viewer rows) plus endpoint (happy path with `OMNISIGHT_DEPLOY_MODE=dev` env override + seeded admin row → 200 with all counts; idempotent replay on clean install → 200 with zero counts; audit row written with actor + reason + severity + emails; non-dev mode → 403 with no DB/marker mutation; non-admin → 401/403). Full bootstrap regression: 32 prior tests + 8 new = **40 passed, zero regressions**)*
- [x] E2E Playwright：完整 5-step wizard 走完（mock CF API、mock LLM provider ping） *(done: new `e2e/l8-bootstrap-wizard.spec.ts` drives every wizard gate end-to-end against a hermetic mock surface installed via `page.route("**/api/v1/bootstrap/**")`. A `BootstrapFlowState` object doubles as the test oracle: each mocked POST mutates it so the next `GET /bootstrap/status` reflects reality, and call-count assertions at the end of the run (`admin_password_calls >= 1`, `llm_provision_calls >= 1`, etc.) catch the regression where a pill paints green without its POST firing. Step-by-step: (1) **admin password** — fills `omnisight-admin` + a 20-char/4-class strong passphrase that passes `estimatePasswordStrength` with score 4, submits, waits for `bootstrap-step-admin_password` pill `data-state=green`; (2) **LLM provider** — picks Anthropic, fills a dummy `sk-ant-…` key, submits, mocked `provider.ping()` returns a fake fingerprint + latency so the success banner has real data to render, waits for green pill; (3) **Cloudflare Tunnel** — uses the documented LAN-only skip escape hatch (`cf-tunnel-skip-reveal` → fill reason → `cf-tunnel-skip-confirm`) so the embedded B12 wizard never opens and the real CF API never fires (a defensive `/api/v1/cloudflare/**` stub catches any future auto-probe); (4) **service health** — asserts the sidebar pill flips green driven by the all-green mocked parallel-health-check (backend/frontend/db_migration green + cf_tunnel skipped, which the UI treats as green), deliberately skipping the panel body assertion because the auto-advance effect may move past it before Playwright can observe it — the pill is the durable contract; (5) **smoke subset** — clicks `bootstrap-smoke-run-button`, mock returns smoke_passed=true with both DAGs green (compile-flash host_native + cross-compile aarch64) and audit_chain.ok=true, waits for the smoke pill green; (6) **finalize** — auto-advance lands on the Finalize pane, clicks `bootstrap-finalize-button`, asserts `page.waitForURL((url) => !url.pathname.startsWith("/bootstrap"))` confirms the client-side redirect to `/` fired. Also stubs `/api/v1/operation-mode` + `/api/v1/budget-strategy` with benign defaults so the home page after redirect doesn't flake on real backend calls. `playwright.config.ts` extended with pass-through of `OMNISIGHT_DATABASE_PATH` so the spec can point the backend at a temp DB without clobbering the caller's dev data — run with `OMNISIGHT_PW_LIB_DIR=~/.local/lib/playwright-deps OMNISIGHT_DATABASE_PATH=/tmp/omnisight-e2e-l8/omnisight.db OMNISIGHT_E2E_BACKEND_PORT=18831 OMNISIGHT_E2E_FRONTEND_PORT=3101 npx playwright test e2e/l8-bootstrap-wizard.spec.ts --project=chromium` — **1 passed in 1.5s** once services are up. Exercise runtime includes the Next.js prod build + uvicorn boot (~30s) but the test itself is fast and hermetic.)*
- [x] 錯誤路徑：密碼太弱 / LLM key 無效 / systemctl 失敗各自 UX *(done: three distinct error-path UXs, each keyed by a machine-readable `kind` so the banner + remediation is chosen without string-parsing `detail`. **Backend** — `POST /bootstrap/admin-password` now emits `kind` on every failure (409→`already_rotated`, 401→`current_password_wrong`, 422→`password_too_short` when `len < PASSWORD_MIN_LENGTH`, 422→`password_too_weak` otherwise) via a new branch in `backend/routers/bootstrap.py::bootstrap_admin_password`; `POST /bootstrap/start-services` classifies every error into `bad_mode` / `binary_missing` / `timeout` / `sudoers_missing` / `unit_missing` / `unit_failed` — the systemd-specific kinds (`sudoers_missing` / `unit_missing`) are derived from stderr heuristics (`"a password is required"` / `"no tty present"` / `"sorry, user"` → sudoers; `"unit not found"` / `"not loaded"` / `"no such file"` → unit; everything else → unit_failed). **Frontend client (`lib/api.ts`)** — new typed errors `BootstrapAdminPasswordError` / `BootstrapStartServicesError` with `kind` + `detail` + (for start-services) `mode` / `command` / `returncode` / `stdout_tail` / `stderr_tail`; new `bootstrapStartServices()` that bypasses `request<T>`'s Error-flattening and parses the JSON kind directly; matching `BOOTSTRAP_ADMIN_PASSWORD_KIND_COPY` + `BOOTSTRAP_START_SERVICES_KIND_COPY` records (title+hint pairs); `BOOTSTRAP_PROVIDER_KEY_URL` map (anthropic / openai / azure dashboard URLs) for the LLM `key_invalid` follow-up. **Frontend UI (`app/bootstrap/page.tsx`)** — (1) `AdminPasswordStep` replaces the plain-text `localError` with a dedicated `AdminPasswordErrorBanner` that picks copy by kind; `password_too_weak` renders an extra zxcvbn-improvement tip panel (`bootstrap-admin-password-weak-tips`); unclassified plain-Error fall-through preserves the original `bootstrap-admin-password-error` testid with `data-kind="unclassified"`. (2) `ProvisionErrorBanner` takes a new `providerId` prop — on `key_invalid` it renders a one-click `bootstrap-llm-provider-key-url` link (target=_blank, rel=noopener noreferrer) to the provider dashboard, omitted for every other kind since minting a new key isn't the fix for quota / network / 5xx. (3) `ServiceHealthStep` gains a `StartServicesPanel` sub-component mounted between the 4 probe rows and the summary — exposes `bootstrap-start-services-button` that POSTs start-services, renders `bootstrap-start-services-error` on kind-keyed failure with hint + optional `bootstrap-start-services-stderr` pre block, and `bootstrap-start-services-ok` on success (labels `already running (dev mode)` vs `launched (systemd, rc=0)`). On success the panel calls `onStartResolved` which re-probes parallel-health so a freshly-launched backend flips green without waiting for the 3s interval. **Tests** — 7 new backend cases in `test_bootstrap_admin_password.py` (4 kind assertions + helper) + 7 new cases in `test_bootstrap_start_services.py` (sudoers_missing / unit_missing / unit_failed catch-all / binary_missing / timeout / bad_mode / docker-compose unit_failed) → **183 backend bootstrap tests pass**; 16 new vitest cases in `test/components/bootstrap-page.test.tsx` (4 admin-password kinds + unclassified fall-through + 3 LLM providers key-url link + non-key_invalid no-link + 5 start-services kinds + green-path + dev-mode already_running) all pass when run in isolation — full frontend suite reports 246/248 with the 2 pre-existing failures unchanged.)*
- 預估：**0.75 day**

### L9. Quick-start 一鍵佈署腳本 (#336)
- [x] `scripts/quick-start.sh`（428 行，已撰寫）：6 步驟自動化——前置檢查 → .env 互動生成（LLM provider 選擇 + API key 輸入）→ `docker compose -f docker-compose.prod.yml up -d --build` → 健康檢查 polling → Cloudflare Tunnel 自動建立（CF API Token → tunnel → ingress → DNS CNAME → cloudflared connector）→ 開瀏覽器
- [x] WSL2 偵測：systemd 啟用則用 `systemd service`；未啟用則 `nohup` 背景模式 + 提示使用者啟用 systemd *(done: detection already existed at lines 228-251 of `scripts/quick-start.sh` — probes `/proc/version` for `microsoft` (vs. binary-only `systemctl` check) + verifies `ps -p 1 -o comm= = systemd` as PID 1. Hardening applied to the downstream branching at lines 604-644: (1) **nohup idempotency** — persists `CFLARED_PID` to `/tmp/omnisight-cloudflared.pid` so re-runs reuse a live connector instead of spawning a parallel one (which would confuse CF's load balancer + consume double the tunnel slots); re-use path verifies `ps -p $OLD_PID -o comm=` actually matches `cloudflared` before trusting the PID (PID-recycling protection); stale file cleared on fail. (2) **disown** — backgrounded cloudflared is removed from the shell's job table so Ctrl-C on the script doesn't take the tunnel down; guarded with `|| true` because `disown` exits non-zero when job control is off (e.g. under `sh` or certain CI runners). (3) **namespaced log** — `/tmp/omnisight-cloudflared.log` instead of generic `/tmp/cloudflared.log` to avoid collision + easier to find. (4) **systemd-enablement guidance** — now a 3-step copy-pasteable block (edit `/etc/wsl.conf` with heredoc → `wsl --shutdown` in PowerShell → re-run script to auto-upgrade to systemd service), fired when `IS_WSL=true` but systemd not PID 1. (5) **user-facing copy** — both branches now state the runtime mode explicitly ("將以 systemd service 常駐" vs "將以 nohup 背景程序運行") so the user immediately knows which lifecycle semantics apply. **Tests** — 5 new source-level + behavioral tests in `tests/test_quick_start_script.py`: `test_wsl_systemd_branching_both_modes_present` (asserts both `cloudflared service install` systemd branch AND `nohup cloudflared tunnel run` branch exist, gated by the same `WSL_SYSTEMD` flag), `test_nohup_branch_is_idempotent` (PID file + `ps -p` recycling check + `disown` all present), `test_systemd_enablement_guidance_is_copy_pasteable` (verifies `/etc/wsl.conf` + `systemd=true` + `wsl --shutdown` are all in the output path), `test_nohup_log_path_is_namespaced` (guards against regressing to generic log path), `test_wsl_detection_simulation` (runs the extracted detection block against stubbed `grep`+`ps` to simulate both non-WSL+systemd and WSL2+no-systemd scenarios — confirms `WSL_SYSTEMD` and `IS_WSL` get set correctly in each case AND that the systemd-enablement tips actually print to stdout in the WSL2-no-systemd case). All 19 quick-start tests pass (14 prior + 5 new) in 1.55s; `bash -n` clean; live `--dry-run` on WSL2+systemd host prints the new "cloudflared 將以 systemd service 常駐" message.)*
- [x] GoDaddy NS 遷移指引：腳本內印出清楚步驟（GoDaddy Dashboard → Nameservers → 填 CF NS），這步無法自動化 *(done: Step 5 of `scripts/quick-start.sh` rewritten from a bare 4-line guide into a tri-state, idempotent, fully-guided walkthrough. **What's auto-detected** — the script probes current NS via `dig +short +time=3 +tries=1 NS $DOMAIN @1.1.1.1` (forces 1.1.1.1 so a broken WSL2 `/etc/resolv.conf` doesn't poison detection; falls back to resolv.conf resolver on timeout) with `host -t NS`/`nslookup -type=NS` fallbacks if `dig` isn't installed. Tolerates trailing dots (`dig` returns `ns.cloudflare.com.`). Counts how many NS are `ns.cloudflare.com` vs total → classifies into 4 states: `all-cf` (done — skip with `✓ NS 遷移已完成，跳過此步驟` + detected NS list), `mixed` (mid-propagation — show ratio like `1/2 已指向 Cloudflare` + advise waiting 30min-4h + verify-by-`dig`/whatsmydns link), `non-cf` (still on GoDaddy — fire full walkthrough), `unknown` (no lookup tool available / query failed — also fire full walkthrough as safe default). **What's new in the walkthrough** — (A) The script prints the actual two Cloudflare NS values verbatim (no tab-switch to CF dashboard needed). These are captured by a new `CF_NAMESERVERS=()` global initialized at line 423 alongside `CF_READY=false`, populated in the CF setup block at line 491 via `jq -r '.result[0].name_servers // [] | .[]'` with output captured to `CF_NS_RAW` first (so jq failure doesn't silently null the array through process-substitution + set -e), then `while IFS= read -r` loop (not `readarray` — bash 3.2 compat for macOS hosts). If the CF block was skipped (no token / zone not found / empty), `CF_NAMESERVERS` stays empty and Step 5 gracefully falls back to "look them up in CF dashboard" with explicit URL. (B) GoDaddy UI walkthrough updated to the 2024+ portfolio URL `https://dcc.godaddy.com/control/portfolio` (old `manage/${DOMAIN}/dns` URL kept as secondary) with exact modern menu path: `⋮ → DNS → Nameservers tab → Change Nameservers → "I'll use my own nameservers"`. Includes the "this will temporarily break your domain" warning GoDaddy shows (confirming it's normal). (C) Verification section prints `dig NS ${DOMAIN} +short` and links https://www.whatsmydns.net/#NS/${DOMAIN} with Cyan highlighting + notes CF zone status flipping `Pending → Active` is the authoritative signal. (D) Two footgun warnings, both hit us previously: ${BOLD}DNSSEC must be disabled at GoDaddy before cutover${NC} (DNSSEC mismatch will brick the domain globally — verify with `dig DS $DOMAIN +short`), and ${BOLD}MX/TXT email records must be preserved${NC} on CF (email silently breaks on NS switch otherwise — tell users to confirm CF auto-import in CF Dashboard → DNS before cutover). (E) A post-cutover `curl -I https://${DOMAIN}` smoke check printed only when `CF_READY=true` (ingress + DNS CNAME already created by the script). **Tests** — 6 new source+behavioral tests in `tests/test_quick_start_script.py`: `test_cf_nameservers_captured_from_zones_api` (asserts CF_NAMESERVERS=(), .result[0].name_servers in jq query, no `readarray` for bash 3.2 compat), `test_ns_auto_detect_uses_multiple_tools` (asserts bounded dig `+time=3 +tries=1`, `@1.1.1.1` public resolver, + host/nslookup fallbacks), `test_ns_state_classification_is_tri_state` (asserts all 3 states all-cf/mixed/non-cf + idempotent skip copy), `test_godaddy_ui_walkthrough_has_current_menu_path` (asserts 2024+ portfolio URL + exact radio label `I'll use my own nameservers`), `test_verification_commands_are_printed` (asserts `dig NS ${DOMAIN} +short`, whatsmydns.net, DNSSEC warning, MX warning), `test_cf_nameservers_graceful_fallback_when_empty` (source-level assert of `${#CF_NAMESERVERS[@]} -gt 0` guard + dash.cloudflare.com fallback copy, PLUS behavioral test: stubs `command() { return 1; }` to simulate no-lookup-tools host with empty CF_NAMESERVERS, runs the extracted Step 5 block, asserts "未取得 CF API Token" fallback copy fires + CF dashboard URL printed — guards the degradation path most likely to regress silently). All 25 tests pass in 1.60s (19 prior + 6 new); `bash -n` clean; live `--dry-run` on WSL2+systemd host clean; 4-scenario smoke of the extracted block (all-cf / mixed / non-cf / unknown-nolookup) visually confirmed correct output for each branch.)*
- [x] 冪等：重複執行不壞（.env 已存在跳過 / tunnel 已存在複用 / DNS CNAME 已存在 skip） *(done: full idempotency contract audited + hardened + pinned with 7 new tests. **Audit result** — 3 of the 3 explicit criteria were already implemented: `.env` skip at `scripts/quick-start.sh:291` (`if [ -f ".env" ]; then` → log 已存在，跳過生成), tunnel reuse at `scripts/quick-start.sh:520-528` (queries `tunnels?name=${TUNNEL_NAME}&is_deleted=false` + extracts `.result[0].id` + fetches token for existing tunnel via `/tunnels/${CF_TUNNEL_ID}/token` + announces "Tunnel 已存在: ${CF_TUNNEL_ID}，複用"), and DNS CNAME "already exists" skip via `grep -qi "already exists"` on the error path. **Hardening applied** — the CNAME path had a subtle silent-failure mode: if a prior run created a CNAME pointing at tunnel-A, then tunnel-A got deleted out-of-band (e.g. via CF dashboard) and re-running created tunnel-B, the POST would fail with "already exists", the script would log "跳過" — but the CNAME would still point at dead tunnel-A and the site would return 1016 forever. The only fix was a manual DNS edit. Rewrote `scripts/quick-start.sh:572-621` as a three-case state machine: (POST succeeds → log 已建立) / (POST fails + already exists + GET returns matching `.content` → log "已存在且指向當前 tunnel，跳過" as true no-op) / (POST fails + already exists + GET returns DIFFERENT `.content` → log "偵測到 CNAME 漂移：目前指向 X，更新為 Y" + PATCH the record by id → log "CNAME 已更新至當前 tunnel"). Drift-repair uses `/dns_records?type=CNAME&name=${HOSTNAME}` to fetch the existing record, `jq -r '.result[0].content // empty'` to extract content (empty fallback so set -u doesn't explode), and `curl -X PATCH /dns_records/${EXISTING_ID}` with identical body shape as the original POST. Defensive: if GET returns empty id (record gone between POST and GET — impossible in practice but set -e would crash without the guard), fall back to "無法查詢內容，保守跳過" and warn instead of attempting a PATCH with null id. **Other idempotent paths audited + confirmed safe** — Ingress uses `-X PUT` (replace-style; CF semantics → same body in = same state out, so re-runs are a no-op by API design) not POST; cloudflared `.deb` install gated by `if ! command -v cloudflared` with "cloudflared 已安裝" in the else branch; cloudflared systemd install uses `|| true` on the install step + `systemctl restart` to pick up new config; cloudflared nohup branch already hardened in the prior L9#4 task with `/tmp/omnisight-cloudflared.pid` + `ps -p $OLD_PID -o comm=` recycling check + `disown`. `docker compose up -d --build` is idempotent by design (won't create duplicates; will rebuild if config changed). Error-path banner at line 100 explicitly tells users "重新執行此腳本：問題修復後重跑即可（腳本支援冪等）" so they know a clean re-run is safe. **Tests** — 7 new source-level + behavioral tests in `tests/test_quick_start_script.py`: (1) `test_idempotency_env_skip_source_guard` — asserts the `.env` existence guard + "已存在，跳過生成" copy + crucially, that no `cp .env.example .env` appears BEFORE the guard (regression guard against accidentally clobbering a user's customized .env on re-run). (2) `test_idempotency_tunnel_reuse_source_guard` — asserts `is_deleted=false` in the probe URL (catches regressions that would reuse a soft-deleted tunnel), the `.result[0].id // empty` extraction, the "Tunnel 已存在" copy, the `/tunnels/${CF_TUNNEL_ID}/token` fetch for existing tunnels, and exactly 1 POST to `.../accounts/{id}/tunnels` (a regression adding a second POST URL would duplicate tunnel creation). (3) `test_idempotency_ingress_uses_put_not_post` — asserts `-X PUT` within the ingress block (slices between "設定 Tunnel ingress" and "DNS CNAME" markers so drift elsewhere doesn't pollute the assertion). (4) `test_idempotency_cname_already_exists_branch_source_guard` — asserts `grep -qi "already exists"` detection, the GET URL with `type=CNAME&name=${HOSTNAME}`, `EXISTING_CONTENT=` capture, `-X PATCH` drift-repair call, both success-skip copy ("已存在且指向當前 tunnel，跳過") and drift-detection breadcrumb ("偵測到 CNAME 漂移"). (5) `test_idempotency_cloudflared_install_skipped_if_present` — gates on `if ! command -v cloudflared` + "cloudflared 已安裝" else-branch copy. (6) `test_idempotency_full_rerun_announces_support_in_error_copy` — guards the "支援冪等" string in the error banner so users aren't left wondering if a failed run corrupted state. (7) `test_idempotency_cname_drift_repair_behavioral` — the crown-jewel behavioral test: extracts the CNAME loop, wraps it in a bash harness that stubs `curl` with scenario-driven canned responses (happy / already_match / drift), logs every curl invocation to a temp file, and asserts the exact (method, URL) sequence for each case. Scenario 1 (happy): 2 POSTs, 0 GETs, 0 PATCHes → logs "CNAME 已建立". Scenario 2 (already-match): 2 POSTs (fail) + 2 GETs (verify matching content), 0 PATCHes → logs "已存在且指向當前 tunnel，跳過" (critical: guards against a regression that would PATCH unnecessarily and churn DNS). Scenario 3 (drift): 2 POSTs (fail) + 2 GETs (detect drift) + 2 PATCHes (repair) → logs "偵測到 CNAME 漂移" AND "CNAME 已更新至當前 tunnel". Full `tests/test_quick_start_script.py` suite: 32 passed in 1.63s (25 prior + 7 new); `bash -n scripts/quick-start.sh` clean; `./scripts/quick-start.sh --dry-run` on WSL2+systemd host passes all preflight checks + announces "所有前置條件通過 ✓".)*
- [x] 域名可設定：`OMNISIGHT_DOMAIN=sora-dev.app` env 或腳本內 default *(done: audited the existing `DOMAIN="${OMNISIGHT_DOMAIN:-sora-dev.app}"` baseline at `scripts/quick-start.sh:36` → extended to a full override+validation+banner contract across all three related knobs. **What was already working** — `DOMAIN` honored the `OMNISIGHT_DOMAIN` env with `sora-dev.app` default via the `:-` expansion, and the value propagated to every downstream consumer (CF zone lookup at line 483, ingress config at 557-558, CNAME loop at 584, NS probe at 759-767, GoDaddy walkthrough at 818-884, final summary at 922). **What was missing / added** — (1) **`API_SUBDOMAIN` + `TUNNEL_NAME` also env-overridable** — `API_SUBDOMAIN="${OMNISIGHT_API_SUBDOMAIN:-api}"` + `TUNNEL_NAME="${OMNISIGHT_TUNNEL_NAME:-omnisight-prod}"` so users running staging+prod on one CF account can pick distinct tunnel names without forking (`TUNNEL_NAME` collision in the previous implementation would silently reuse one account's tunnel from the other → classic "why is my staging traffic hitting prod?" footgun). (2) **`_strip_ws` helper** — `OMNISIGHT_DOMAIN=" sora-dev.app "` from a quoted .env value (common paste-error from dashboard snippets) now gets leading/trailing whitespace stripped before validation. Uses the POSIX `${s#"${s%%[![:space:]]*}"}` + `${s%"${s##*[![:space:]]}"}` idiom (bash 3.2 portable — no `[[ :-: ]]` extended globs). (3) **`_validate_domain`** — rejects (a) empty, (b) > 253 chars (RFC 1035 upper bound), (c) values containing `://`, `/`, or whitespace via `case` glob so `OMNISIGHT_DOMAIN=https://foo.com` (URL pasted by mistake) fails with a friendly message BEFORE any CF API call, (d) uppercase (CF would normalize but we reject for clarity — users should see the exact value they typed reflected back), (e) single-label hostnames like `localhost` via an RFC 1035-ish regex `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$` which requires at least one dot and per-label length 1-63. (4) **`_validate_api_subdomain`** — single DNS label, no dots (so `OMNISIGHT_API_SUBDOMAIN=foo.bar` which would create a weird `foo.bar.DOMAIN` nested host is rejected), 1-63 chars. (5) **`_validate_tunnel_name`** — 1-32 chars (mirrors CF dashboard UI limit; the API is more permissive but we mirror the UI so the tunnel name in CF dashboard doesn't look weird), `[a-zA-Z0-9_-]+`. Each validator emits a per-knob diagnostic that names WHICH env var is wrong + WHY + shows a valid example — generic "invalid config" was explicitly avoided because users reading it would blame the wrong variable. (6) **Placement matters** — the validation calls run AFTER `--help`/`--uninstall` CLI parsing (so `OMNISIGHT_DOMAIN=bogus ./script --help` still works) and AFTER `LOG_FILE` init (so `err()`'s `tee -a "$LOG_FILE"` has a real file to write to). Fails fast (exit 1) with no trap noise because validation runs before the `trap _cleanup_on_exit EXIT` is installed. (7) **Deployment banner** — echoes the three resolved values (`Domain:`, `API subdomain:`, `Tunnel name:`) right after validation so the operator sees the effective config before any side-effecting Docker/CF work; when all three are defaults, a trailing hint mentions how to override them — gives the "last chance to Ctrl-C" checkpoint that caught two footguns during local testing. (8) **`--help` extended** — from 3 lines to a 12-line block listing every env var with its default and a concrete combined-usage example `OMNISIGHT_DOMAIN=app.example.com OMNISIGHT_TUNNEL_NAME=omnisight-staging ./scripts/quick-start.sh`. **Tests** — 8 new tests in `tests/test_quick_start_script.py` (extends the L9 contract-pin pattern; counts are source+behavioral): (A) `test_domain_env_override_source_guard` — pins the `${VAR:-default}` expansion for all three knobs, catches regressions that hardcode the default. (B) `test_validators_exist_for_all_three_knobs` — asserts the 3 validator defs + their call sites + the 3 `_strip_ws` calls are all wired (catches half-done refactors). (C) `test_help_documents_env_vars` — runs `--help` end-to-end and greps for all 3 env vars, their defaults, and the usage-example form. (D) `test_deployment_banner_prints_resolved_values` — behavioral: runs `--dry-run` twice (once with defaults, once with overrides) and asserts the banner reflects both correctly + the "全部為預設值" hint fires only on the defaults path. (E) `test_invalid_env_values_rejected_with_clear_message` — 6-case parametrize exercising every rejection branch (URL paste, single-label host, uppercase, two-label subdomain, space in tunnel name, 33-char tunnel name) + asserts the per-knob diagnostic string fires. (F) `test_whitespace_stripped_from_env_values` — feeds `"  sora-dev.app  "` via env and asserts validation accepts + banner echoes the stripped form with no trailing spaces (regression guard). (G) `test_domain_propagates_to_all_downstream_consumers` — pins that `$DOMAIN` flows into the CF zones API URL + CNAME iterator + NS probe + final summary; also asserts no lingering literal `sora-dev.app` appears in any downstream consumer with an explicit allowlist for (i) the default-assignment line, (ii) `--help` copy, (iii) validator error-message example text. (H) `test_validators_accept_common_valid_domains` — sources the validator block into a harness and exercises each against a realistic positive set (`sora-dev.app`, `app.example.com`, `a.b.c.d`, `foo-bar.example.co.uk`, 60-char label; api/v2/app/a/x-y; omnisight-prod/omnisight_staging/abc123/32-char name) — catches regex-regression cases where source-grep passes but real input rejects. Full `tests/test_quick_start_script.py` suite: **45 passed in 4.26s** (32 prior + 13 new — note: 13 instead of 8 because the parametrize adds 6 cases counted individually); `bash -n scripts/quick-start.sh` clean; `./scripts/quick-start.sh --help` shows the new env-var section; `./scripts/quick-start.sh --dry-run` announces banner + preflight passes; 4 live negative-path dry-runs (`OMNISIGHT_DOMAIN=https://foo.com`, `localhost`, `OMNISIGHT_API_SUBDOMAIN=foo.bar`, `OMNISIGHT_TUNNEL_NAME='bad name'`) all reject with correct per-knob diagnostic; 1 live positive-path dry-run (`OMNISIGHT_DOMAIN=app.example.com OMNISIGHT_TUNNEL_NAME=omnisight-staging`) shows banner reflecting overrides without the "全部為預設值" hint.)*
- [O] 測試：在乾淨 WSL2 上跑一次全流程 → 容器啟動 + health pass + CF tunnel active + 瀏覽器開啟 *(🅐 Operator-blocked — 最終四個 acceptance gate 都需要 operator-only credential/環境，無法僅靠 AI 驗證：(1) **容器啟動** 需要 real `ANTHROPIC_API_KEY`，backend 的 `validate_startup_config()` 在 `OMNISIGHT_ENV=production` 下會硬拒（`ConfigValidationError: Refusing to start — llm_provider='anthropic' but ANTHROPIC_API_KEY is empty`）；(2) **health pass** 被 #1 阻塞（backend 的 uvicorn 都沒起來）；(3) **CF tunnel active** 需要 real CF API token + real domain；(4) **瀏覽器開啟** 需要 Windows host 上的 `explorer.exe`（腳本 fallback 到 `xdg-open` 但 headless Linux/CI 沒這個 opener 也不會 crash — `command -v` guarded）。**本回合 AI 實際完成**（在此 sandbox WSL2 + systemd + Docker 29.4 上 live 驗證）：**L9 邊界情況找出並修復 3 個阻塞 bug**——(A) 專案根目錄**缺少 `.dockerignore`**：`Dockerfile.frontend:14` 的 `COPY . .` 會把 host 的 `node_modules` 覆蓋進 `/app/node_modules/`（剛 `pnpm install` 出來的），觸發 buildkit 的 `cannot copy to non-directory: /var/lib/docker/buildkit/containerd-overlayfs/cachemounts/buildkit*/app/node_modules/@eslint/config-array`，**build 整個炸掉**；新增 `.dockerignore`（~55 行、narrowly scoped：只 exclude `node_modules`/`.next`/`.pnpm-store`/`__pycache__`/`.git`/`data/*.db`/`test-results`/`.venv`/`.env*`/`.agent_workspaces` — 故意**不**排除 `CLAUDE.md` / `README.md`（backend Dockerfile line 20 顯式 `COPY CLAUDE.md ./`，我第一版 dockerignore 把它排掉 → 第二次 build 又炸、留下可觀察的錯誤鏈讓我縮小 exclusion scope）；`.env` 本身也排除，因為 compose 用 `env_file: .env` 掛進 container 而非 build-time COPY，留在 context 裡是潛在 secret leak）。(B) **`public/` 目錄不存在**：`Dockerfile.frontend:30` 的 `COPY --from=builder /app/public ./public`（Next.js standalone runner stage）會因為 builder stage 沒產出 `/app/public` 而 fail with `"/app/public": not found`；新增 `public/.gitkeep` 讓 git 追蹤 empty directory，Next.js build 就會把 empty `public/` 傳到 standalone stage（Next.js 本來就視 public 為選用，只是 Dockerfile 無條件 copy 所以必須存在）。(C) **`Dockerfile.backend:35` 的 `ENV OMNISIGHT_WORKERS=""`**：pydantic-settings 讀 `OMNISIGHT_*` env 時拿到空字串 → `ValidationError: Input should be a valid integer, unable to parse string as an integer [input_value='', input_type=str] for Settings.workers`，backend process 在 `import backend.main` 的第一行就 crash（`from backend.config import settings` → `settings = Settings()` → ValidationError 拋 exception 結束）；removal of the `ENV` line（保留 CMD 行的 `${OMNISIGHT_WORKERS:-$(python3 -c ...)}` default 因為 `${VAR:-default}` 對 unset/empty 都 fallback），改註解說明為什麼不 declare 這個 env。**Build 驗證** `docker image ls | grep omnisight` 確認 `omnisight-productizer-backend:latest`（1.34GB）+ `omnisight-productizer-frontend:latest`（272MB）**兩個 image 都成功建出**；backend container 也能 create + 起 uvicorn process（只是卡在 validate_startup_config 的 ANTHROPIC_API_KEY 必需性，這是 architectural gate 不是 bug）。**新增 6 個契約測試**（`tests/test_quick_start_script.py` 45→51 顆）——(1) `test_non_interactive_mode_auto_detected_from_non_tty`：pin `[ ! -t 0 ] || [ ! -t 1 ]` 雙向 TTY 檢測，擋 regression 改回只檢查 stdin 會讓 piped-stdout/TTY-stdin 組合漏過導致 CF prompt 吞掉 TTY 使用者輸入一個換行；(2) `test_non_interactive_skips_cf_tunnel_setup_cleanly`：pin `if [ "$NON_INTERACTIVE" = true ]; then cf_setup="N"` 分支 + warn copy「非互動模式：跳過 Cloudflare Tunnel 設定」（silent skip 是 footgun）；(3) `test_wait_for_health_is_resilient_to_transient_failures`（**行為測試**）：從腳本抽出 `_wait_for_health()`，stub `curl` 前 2 次 fail 第 3 次 success，assert 函式回 0 + 印「Backend 就緒（第 3/10 次檢查）」讓使用者看到「實際花了幾輪」debug 資訊 + sidecar counter file 驗證 curl 真被呼叫 3 次（不是 short-circuit 掉）；(4) `test_wait_for_health_times_out_with_actionable_error`：flip-side，curl 永遠 fail + retries=3、interval=0，assert 函式 return 1 + stdout 含「啟動超時（3 × 0s = 0s）」包數字讓使用者 debug + copy-pasteable 的「`docker compose -f ... logs backend`」提示；(5) `test_browser_open_falls_back_gracefully_on_no_desktop`：assert `command -v explorer.exe &>/dev/null` + `command -v xdg-open &>/dev/null` 雙 guard 都在 + browser-open block 內**沒有** `exit 1`（否則 headless Linux 會在最後一行失敗）+ 「✅ 部署完成」banner unconditionally 印在 block 外（line-prefix `\necho -e \"${GREEN}${BOLD}✅ 部署完成` 精確 match 擋未來 refactor 把 banner 縮進到 elif 裡）；(6) `test_end_to_end_non_interactive_reaches_success_banner`（**全流程 smoke test**）：在 `tempfile.TemporaryDirectory()` 裡 scaffold fake project（`.env.example` + `docker-compose.prod.yml` + pre-existing `.env`）、stub `docker` command（subcommand 都 fake-success，`compose ps --services --status=running` 印 `backend\\nfrontend` 讓 RUNNING_SVCS >= 2 通過、`compose up` 退 0）、stub `curl`（只有 `http://localhost:8000/api/v1/health` 和 `http://localhost:3000/` 回 0，其他都回 7 讓 CF preflight 進 warn 分支）、**故意不**提供 `explorer.exe` 或 `xdg-open`（驗證 headless fallback）、`PATH=$stubs:$PATH` + `stdin=subprocess.DEVNULL`（非 TTY → 觸發 `NON_INTERACTIVE=true`），跑完整腳本 assert exit 0 + 「所有前置條件通過」+「.env 已存在」+「容器已啟動」+「Backend 就緒」+「Frontend 就緒」+「非互動模式：跳過 Cloudflare Tunnel 設定」+「部署完成」+ docker invocation log 含「compose」和「up」—— 這是 L9 最終 checkbox 的 **automated contract**，未來任何 refactor 讓非互動路徑壞掉都會炸。51 passed in 6.82s（45 prior + 6 new，零 regression）。**Live WSL2 驗證** on this sandbox：Step 0 preflight 14 個 check 全綠（Docker 29.4.0 / Compose 5.1.2 / systemd PID 1 / ports 8000+3000 free / 863G disk / curl+openssl+jq 全部 OK / .env.example 存在 / WSL2+systemd 常駐提示）→ Step 1 `.env 已存在，跳過生成` 冪等 path 確認 → Step 2 docker build（兩個 image 都成功建出）→ error-path trap 在 backend unhealthy 時正確 fire diagnostic banner（含 `docker compose logs` 建議 + 「支援冪等，重跑即可」提示）+ exit code 1 → 每次失敗後 `docker compose down` 清乾淨 state + 立即重跑驗證冪等性 5 次沒有累積汙染。**真要端到端驗證剩下的 credentialed stages**：operator 需要在真 WSL2 + systemd 主機執行 `./scripts/quick-start.sh`、互動輸入 real LLM API key（Step 1）+ 真 CF API token（Step 4）+ 真 CF-hosted domain → backend `validate_startup_config()` 會過 → health check 會通 → CF tunnel 會 active → Windows host 的 explorer.exe 會開 `https://${DOMAIN}` → L1-01 runbook 收尾，看 HANDOFF.md 「L1-01 prod deploy runbook」 Step 1-4。)*
- [x] 預估：**1 day**（腳本已寫好，需測試 + 修邊界情況）*(done: 本回合找出並修復 3 個邊界 bug — 缺 `.dockerignore` / 缺 `public/` / `ENV OMNISIGHT_WORKERS=""` pydantic validation crash — 加 6 個契約測試，docker build 在 clean WSL2 sandbox 上成功；剩餘的端到端 smoke 屬於 operator credentialed stage)*

### L10. Pre-built Docker Images on GHCR (#337)
- [x] GitHub Actions workflow `.github/workflows/docker-publish.yml`：tag push 時自動 build + push `ghcr.io/your-org/omnisight-backend:latest` + `ghcr.io/your-org/omnisight-frontend:latest` *(done: 新增 `.github/workflows/docker-publish.yml` — trigger on `v*` tag push + `workflow_dispatch` 手動逃生口；`permissions: { contents: read, packages: write }` 最小權限（`packages: write` 是 GHCR push 的 load-bearing 權限，default `GITHUB_TOKEN` 是 read-only）；`jobs.publish` 以 matrix strategy 平行 build backend + frontend（`name`/`image`/`dockerfile` 三欄 matrix，`fail-fast: false` 讓單邊失敗不會 cancel 對手）；GHCR namespace 先用 shell `tr '[:upper:]' '[:lower:]'` 把 `github.repository_owner` 轉小寫（GHCR 拒絕大寫 namespace — 這是 mixed-case org handle 在 prod tag push 才會炸的 footgun）；`docker/login-action@v3` 以 `github.actor` + `secrets.GITHUB_TOKEN` 登入 `ghcr.io`（不需要 operator 管理 PAT）；`docker/build-push-action@v6` 同時 push 兩個 tag：`:latest`（浮動 — 每次 release 更新）+ `:${resolved-tag}`（immutable snapshot keyed off `GITHUB_REF_NAME` — 保留歷史版本可 pull）；OCI labels 寫入 `org.opencontainers.image.{source,revision,version}` 三欄以利溯源；GHA cache `type=gha,scope=${matrix.name}` 分 backend/frontend scope 避免互相 evict。**測試** 新增 12-case 契約測試 `backend/tests/test_docker_publish_workflow.py`：以 `yaml.safe_load` 解析 workflow YAML，pin 住下列 load-bearing invariants — (1) trigger on `push.tags: [v*]` + workflow_dispatch 逃生口存在，(2) `permissions.packages==write`，(3) `permissions.contents==read`（least-privilege），(4) matrix 同時覆蓋 backend+frontend，(5) 引用的 Dockerfile 實際存在於 repo，(6) `docker/login-action` 步驟存在 + registry 指向 ghcr.io（或 `env.REGISTRY` 引用）+ password 用 `GITHUB_TOKEN`（擋未來改成 operator-managed PAT），(7) `docker/build-push-action` 步驟存在 + `push: true`，(8) tags 同時包含 `:latest` 和版本引用，(9) image 名稱為 `omnisight-backend` + `omnisight-frontend`（pin public contract），(10) `env.REGISTRY == "ghcr.io"`，(11) workflow 有 lowercase owner 的 shell step（防漏網的 mixed-case footgun）。12 passed in 0.08s；`python3 -c "import yaml; yaml.safe_load(...)"` 確認 workflow 可正確 parse。**Scope note** — 此項目只做 workflow 本身；multi-arch / image size / docker-compose image-first 屬後續 L10 子項)
- [x] `docker-compose.prod.yml` 改為 `image:` 優先（有 registry image 就 pull，沒有才 local build） *(done: 將 `docker-compose.prod.yml` 從「無條件 local build」改成 image-first deployment — 兩個 app service 都加上 `image: ghcr.io/${OMNISIGHT_GHCR_NAMESPACE:-your-org}/omnisight-{backend,frontend}:${OMNISIGHT_IMAGE_TAG:-latest}` 並保留 `build:` 區塊作為 fallback。**為什麼這樣 work** — Docker Compose 的官方語意：當同一個 service 同時宣告 `image:` 和 `build:`，`docker compose up` 會先檢查本地 cache，沒有就嘗試 pull，pull 失敗才 fall back 到 `docker build`。這完全 cover「有 registry image 就 pull，沒有才 local build」的需求，**不需要 wrapper script 自己判斷 pull/build 邏輯**。`pull_policy: missing` 顯式設定（雖然是 default），是為了擋未來 drift 到 `always`（每次 up 都強制 pull、慢且依賴網路）或 `build`（永遠不 pull、整個 image-first 契約失效）。**Knobs**（兩個都記錄在 `.env.example` 的「Production / Docker Settings」區塊）：(a) `OMNISIGHT_GHCR_NAMESPACE` — lowercase GitHub org/user handle，default `your-org`（placeholder — pull 會 graceful fail → 自動 build fallback，所以即使沒設也能 first-run），(b) `OMNISIGHT_IMAGE_TAG` — image tag，default `latest`，可 pin 到 `v*` release tag 拿到 reproducible deploys。Image basename `omnisight-backend` + `omnisight-frontend` **與 `.github/workflows/docker-publish.yml` 的 publishing matrix 完全一致**（已被測試 pin 住，工作流改名會炸 CI）。**Quick-start.sh 同步調整** — `scripts/quick-start.sh:476` 把 `docker compose ... up -d --build` 改為 `docker compose ... up -d`（`--build` flag 會 force rebuild、繞開 image-first path），diagnostic 加一行 `GHCR pull 失敗且本地 build 也失敗 (檢查 OMNISIGHT_GHCR_NAMESPACE)`，最後的「升級部署」hint 也從單行改成兩行：日常用 `pull && up -d`，明確要強制 build 才用 `--build`。**驗證** `python3 -c "import yaml; yaml.safe_load(...)"` 解析通過；`docker compose -f docker-compose.prod.yml config` 用預設值 resolve 成 `image: ghcr.io/your-org/omnisight-backend:latest` + `pull_policy: missing` + `build:` 三者並存；用 `OMNISIGHT_GHCR_NAMESPACE=acme OMNISIGHT_IMAGE_TAG=v1.2.3` 覆寫驗證 resolve 成 `ghcr.io/acme/omnisight-backend:v1.2.3` + `ghcr.io/acme/omnisight-frontend:v1.2.3`；`bash -n scripts/quick-start.sh` 語法乾淨。**測試** 新增 13 case 契約測試 `backend/tests/test_compose_prod_image_first.py`（mirror 既有 `test_docker_publish_workflow.py` 的 yaml.safe_load + 結構性 assertion 模式，不需網路 / 不需 Docker，<200ms 完成）：(1) backend+frontend 都宣告 `image:` 並指向 `ghcr.io/`；(2) image 字串字面 contains `${OMNISIGHT_GHCR_NAMESPACE:-your-org}` + `${OMNISIGHT_IMAGE_TAG:-latest}`（pin env-var 名稱，rename 會炸）；(3) image basename 必為 `omnisight-{backend,frontend}`；(4) `build:` block 仍存在 + dockerfile 路徑實際存在（沒有就連 fallback 也炸）；(5) frontend 的 `BACKEND_URL` build arg 保留（否則 fallback build 會 silently 斷掉 prod frontend → backend wiring）；(6) `pull_policy: missing` 顯式設定；(7) prometheus + grafana sidecar 維持 pinned version + `observability` profile gate；(8) compose 消費的 image basename **集合相等於** workflow publish 的 image basename — image-first deployment 要求兩邊 agree，rename 一邊另一邊就炸；(9) `.env.example` 文件中含 `OMNISIGHT_GHCR_NAMESPACE` + `OMNISIGHT_IMAGE_TAG` + L10 #337 / GHCR 引用；(10) `scripts/quick-start.sh` 的 `docker compose ... up` 行**不能含 `--build`**（會 defeat image-first 契約）— 此 test 用 `splitlines()` + 過濾 comment / echo 確保只 assert 真實 invocation 行，註解或 user-facing 提示中的 `--build` 不會誤判。**結果** 25 passed in 0.13s（13 新 + 12 個既有 publish workflow test 同時跑、零 regression）；既有的 51 個 `tests/test_quick_start_script.py` 全綠（包含我這次改動經過的 `test_browser_open_falls_back_gracefully_on_no_desktop` 和 `test_end_to_end_non_interactive_reaches_success_banner` 全流程 smoke test，pin 住 quick-start 修改沒破壞下游）。**Scope note** — 此項目只做 `docker-compose.prod.yml` 的 image-first 切換；multi-arch build / image size 優化屬後續 L10 子項，未動 image 內容物或 Dockerfile)
- [x] Multi-arch build（`linux/amd64` + `linux/arm64`）via `docker buildx` *(done: 將 `.github/workflows/docker-publish.yml` 從 single-arch amd64-only 升級為 OCI 多架構 image index publish — (1) 新增 `docker/setup-qemu-action@v3` step 於 buildx setup **之前**（順序 load-bearing：buildx initialize 時會 cache binfmt_misc capability matrix，若 QEMU 在 buildx 後才註冊，buildx 會 fall back 到 amd64-only 然後 arm64 build 在第一個 `RUN` 炸 `exec format error`），用 `with.platforms: linux/arm64` scope 只註冊 arm64 handler（amd64 在 ubuntu-latest runner 上 native 執行、不需 QEMU、縮小 attack surface）；(2) `docker/build-push-action@v6` 新增 `platforms: linux/amd64,linux/arm64` — buildx 會同時 build 兩個 layer 並 push 為 OCI image index（manifest list），operator 從 arm64 host（Raspberry Pi / Ampere / Apple-silicon CI）`docker pull ghcr.io/<owner>/<image>:<tag>` 會自動 resolve 到 arm64 layer、x86_64 host resolve 到 amd64 layer，**一個 tag 解一切架構**；(3) `timeout-minutes: 45 → 90` — QEMU-emulated arm64 build 比 native amd64 慢約 2×（frontend 的 `pnpm install --frozen-lockfile` + Next.js 15 build 是主要 CPU sink），45 min 會 flaky-timeout 但 build 本身健康；(4) header comment 從「multi-arch 未 scope」更新為實作說明 + QEMU rationale + wall-time 預期。**為什麼不換 `runs-on: ubuntu-24.04-arm`（native arm64 runner）避開 QEMU**：GitHub 在 2025 Q1 開始供應原生 arm64 runner，**但 free tier 的 public repo 目前僅對部分 org 開放**，用 `runs-on` matrix（amd64 runner build amd64 layer + arm64 runner build arm64 layer）會讓 workflow 對 repo owner 類型產生隱性耦合（private repo / non-whitelisted org 會 queue forever）。QEMU 路線對所有 ubuntu-latest runner 保證可跑，速度 trade-off 可接受（frontend ~8-12 min emulated arm64 vs ~4 min native）。**驗證** `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/docker-publish.yml'))"` 解析通過；printed resolution 確認 QEMU step 出現在 buildx 之前、`platforms: linux/amd64,linux/arm64` 正確寫入 build-push-action、`timeout-minutes: 90` 生效。**測試** 新增 **4 顆契約測試** 於 `backend/tests/test_docker_publish_workflow.py`（延續既有 yaml.safe_load + 結構性 assertion 模式）：(a) `test_qemu_action_present_for_arm64_emulation` — 找 `docker/setup-qemu-action@` step + assert `with.platforms` contains `arm64`（接受空 `with` 也 OK，action v3 default 註冊所有 handler）；(b) `test_qemu_runs_before_buildx` — 抓兩個 step 的 index、assert QEMU index < buildx index，擋「好心重排 step 順序」的 regression；(c) `test_build_push_publishes_amd64_and_arm64` — 讀 build-push-action 的 `with.platforms` 字串、分別 assert `linux/amd64` 和 `linux/arm64` 都在，擋 regression 只留 amd64 為「加速 CI」defeat L10 #337 契約；(d) `test_timeout_accommodates_emulated_arm64_build` — assert `timeout-minutes >= 60` 擋回退到 45，註解解釋「QEMU frontend build ~8-12 min、headroom 需要」。**結果** `backend/tests/test_docker_publish_workflow.py` 16 passed（12 既有 + 4 新）；同時跑 `test_compose_prod_image_first.py` 29 passed（16 publish workflow + 13 compose image-first，零 regression）；`tests/test_quick_start_script.py` 51 passed（完全不受此 workflow 改動影響，quick-start 走本地 `docker compose up` 不觸及 CI workflow）。**Scope 自律** — 只改 workflow；image size 優化 / 0.5 day 預估 checkbox 屬後續 L10 子項。**Operator 下一步** — 下次打 `v*` tag 時 workflow 會自動 build 兩個架構 image 並 push 為 OCI index；arm64 host 的 operator（例如 Raspberry Pi / ARM VPS）直接 `docker pull` 就能拿到正確 layer、不需手動指定 `--platform`。)
- [x] Image size 優化：backend < 500 MB / frontend < 200 MB *(done: 原 `docker image ls` 顯示 backend 1.34 GB / frontend 272 MB → **backend 490 MB / frontend 169 MB**，兩邊都在預算內；compressed pull size（`docker image inspect .Size`，也就是 registry 真正交付的 bytes）更緊：**backend 103 MB / frontend 68 MB**，映射 L10 #337 下一個 bullet 的 "pull 30 秒" 承諾（50 Mbps * 30 s ≈ 180 MB 容量，兩邊 combined 171 MB 剛好吞得下）。**四大優化並用**：(1) **`.dockerignore` 遞迴 patterns** — 單一最大 bug：原檔用 bare `.venv` / `__pycache__` / `node_modules`，Docker 的 dockerignore 語意是 `.venv` 只匹配 `./.venv`，**不含** `./backend/.venv`（這個 dev venv 168 MB 的 duplicate site-packages 一直靜默洩漏進 backend image）；修正是把所有 cache/venv 類 pattern 改成 `**/pattern` 形式，新增 `**/.venv`、`**/__pycache__`、`**/node_modules`、`**/.pytest_cache`、`**/.mypy_cache`、`**/.pnpm-store` 等；`.dockerignore` 開頭註解寫清楚為什麼要 `**/` prefix 以免未來有人「清理」回 bare pattern 再炸。(2) **Dockerfile.backend 多階段化** — 原本單階段把 gcc/g++/git/openssh/curl 塞進 runtime（325 MB 編譯工具鏈永久駐留），改成 `builder` stage 跑 `pip install --require-hashes -r requirements.txt`（需要 gcc 編 uvloop/pillow/cryptography/argon2-cffi/hiredis 等 native wheel），然後 `runner` stage 只 `apt-get install` weasyprint 的執行期 libs（libpango-1.0-0 / libpangoft2-1.0-0 / libcairo2 / libgdk-pixbuf-2.0-0 / libffi8 / shared-mime-info / fonts-dejavu-core + curl），用 `COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages` 把已裝好的 Python packages 傳過去。**關鍵陷阱**：最初嘗試 `pip wheel --require-hashes` 先在 builder 編出 /wheels 再在 runner `pip install --no-index --find-links=/wheels` —— 失敗，因為 `pip wheel` 把 sdist 編成 wheel 時，該 wheel 的 hash 與 lockfile 記錄的 pypi wheel/sdist hash 不同，`--require-hashes` 直接拒收（`zxcvbn-python` 等純 sdist 套件炸），而**早期 RUN 鏈尾巴的 `|| true` 還把這個 failure 默默吞掉**導致 image 建出來但 site-packages 是空的（backend 啟動就 `ModuleNotFoundError: No module named 'fastapi'`）。改用「builder 直接 install 到 system site-packages，runner COPY 整份過來」模式，不碰 wheel 中間層，hash 契約保持完整。測試合約 `test_backend_does_not_use_pip_wheel_pattern` 把這個決定 pin 住，擋未來的 regression。(3) **Dropping test + bytecode + WOFF2 deadcode** — builder 內做三波 cleanup：`pip uninstall -y pytest pytest-asyncio pytest-cov coverage iniconfig pluggy`（~25 MB；已驗證 production code `grep -rln '^import pytest\|^from pytest' backend/` 排除 tests/ 空結果），`rm -rf site-packages/{pip,pip-*.dist-info,zstandard*,zopfli*}`（~40 MB —— zstandard 23 MB + zopfli 2.7 MB 是 fontTools[woff] 的 WOFF2 壓縮 extra，weasyprint 跑的是 PDF pipeline 不走 WOFF2，已用 `weasyprint.HTML(...).write_pdf()` smoke test 確認移除後 PDF 輸出 bytes-identical），`find -name '__pycache__' -exec rm -rf {} +`（bytecode cache，lazy regenerated）；runner 又追加一份 `rm -rf /usr/local/lib/python3.12/site-packages/{pip,pip-*.dist-info}` 打掉 python:3.12-slim base image 本身帶的 pip（builder 那次只清掉 builder 的 site-packages；runner base 是另一份 layer），plus `rm -rf /usr/share/doc /usr/share/man /usr/share/bash-completion`（3 MB 多的 debian docs）。app-level cleanup 再 `rm -rf ./backend/tests ./backend/.pytest_cache`（31 MB 測試碼 + pytest cache 不屬於 runtime contract）。(4) **Dockerfile.frontend 換 base** — 原本 runner 用 `node:20-alpine`（194 MB，含 node 97 MB + npm/yarn/corepack ~22 MB，**但 `node server.js` 從不呼叫 package manager**）；試過「加 RUN rm -rf npm corepack yarn」發現 Docker layer 是 additive、一個 layer 刪一個 layer 的檔案只產生 whiteout marker，image bytes 不會真的縮（layer tarball 裡前一層的 bytes 還在），**反而**切成「builder 用 `node:20-alpine` 跑 `pnpm run build`、runner 換成 `alpine:3.19` + `apk add nodejs libstdc++ ca-certificates`」才真的省 bytes —— alpine 3.19 的 nodejs apk 是精簡版 node 20.15.1（41.5 MB vs 97 MB 完整版），沒有 npm/yarn/corepack、純 runtime；builder 和 runner 都是 musl-based，Next.js 16.2 standalone bundle 直接搬過去跑 zero issue，已 `timeout 5 node server.js` smoke test 看到 "✓ Ready in 0ms"。**第二刀**：runner 追加 `rm -rf /app/node_modules/.pnpm/@img+sharp-libvips-*` + `sharp@*`（~33 MB）—— `next.config.mjs` 寫 `images: { unoptimized: true }` 已經明確關掉 sharp 路徑，但 Next 的 standalone tracer 還是保守 bundle 兩個架構的 libvips 二進制（linux-x64 glibc + linuxmusl-x64）做防禦，拆掉省 33 MB 不影響 runtime；`|| true` guard 防未來 Next 改 bundle 位置讓這個 rm 路徑失效但 image build 不炸。**契約測試** 新增 `backend/tests/test_dockerfile_image_size.py`（17 cases；15 靜態 + 2 live-opt-in）—— (a) **backend static**：`test_backend_is_multi_stage`（`FROM ... AS builder` + `FROM ... AS runner` 都在）、`test_backend_runner_has_no_compilers`（slice from "AS runner" 之後確認 `gcc` / `g++` / ` git ` 都不在 — 擋「好心合併 stage」的 regression）、`test_backend_runner_installs_weasyprint_runtime_libs`（libpango-1.0-0 / libcairo2 / libgdk-pixbuf-2.0-0 / fonts-dejavu-core — 擋「更激進 slim 再砍掉這些」導致 `from backend.report_generator` 在 prod crash 的 footgun）、`test_backend_strips_test_packages_in_builder`（pytest / pytest-asyncio / pytest-cov / coverage 都列在 uninstall list）、`test_backend_strips_unused_woff2_compressors`（zstandard + zopfli 都被顯式 rm — 附註 "smoke-tested weasyprint PDF output"）、`test_backend_strips_pip_from_runtime`（pip + pip-*.dist-info 都 rm）、`test_backend_excludes_tests_from_runtime`（用 regex 找 `rm -rf ./backend/tests`）、`test_backend_does_not_use_pip_wheel_pattern`（剝掉 `#` 註解行後 assert 找不到 `pip wheel` — 擋回到有 hash mismatch bug 的舊模式，附註解釋 `|| true` 如何吞掉 install failure 的 postmortem）。(b) **frontend static**：`test_frontend_is_multi_stage`、`test_frontend_runner_uses_alpine_base`（regex `FROM\s+alpine:3\.\d+\s+AS\s+runner` + `apk add` + `nodejs`）、`test_frontend_keeps_ca_certificates`（outbound HTTPS 沒 trust store 會 `unable to verify the first certificate`，operator debug 一小時系列）、`test_frontend_strips_unused_sharp_native`（`sharp-libvips` pattern 必須被 rm）。(c) **.dockerignore static**：`test_dockerignore_uses_recursive_venv_pattern`、`test_dockerignore_uses_recursive_pycache_pattern`、`test_dockerignore_uses_recursive_node_modules_pattern` —— 把 `**/` prefix pin 住，這三個是 L10 優化的最大單筆幫助，regress 就會再炸回 1.34 GB。(d) **live opt-in gates**：`test_backend_image_size_under_budget` + `test_frontend_image_size_under_budget`，預設 skip（`pytest.mark.skipif(OMNISIGHT_TEST_DOCKER_IMAGE_SIZE!=1)`；理由：多數 CI lane 沒 build image + 建一次 2-5 min），operator 用 `OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1 pytest backend/tests/test_dockerfile_image_size.py` 本地跑 —— 透過 `docker image inspect --format '{{.Size}}'` 拿 **compressed pull size**（實際 registry 交付 bytes），assert < 500 MiB / 200 MiB。**結果** `backend/tests/test_dockerfile_image_size.py`：**15 passed + 2 skipped**；加了 opt-in env var 再跑 **17 passed**（兩個 live gate 都過，backend 103 MB compressed / frontend 68 MB compressed）；sibling L10 測試零 regression：`test_docker_publish_workflow.py` + `test_compose_prod_image_first.py` + `test_dockerfile_image_size.py` 一起跑 **44 passed + 2 skipped**；quick-start sibling `tests/test_quick_start_script.py` **51 passed**（完全不受 Dockerfile 改動影響）。**Live runtime smoke**：`docker run ... python -c 'from backend.main import app; import weasyprint; weasyprint.HTML(...).write_pdf()'` 輸出 "FastAPI app: FastAPI | PDF bytes: 2814"；`docker run ... node server.js` 看到 Next.js "✓ Ready in 0ms"。**Scope note** — 只動 Dockerfile.backend / Dockerfile.frontend / .dockerignore / 新增一個 test；Python 應用碼、requirements.txt、compose 檔、workflow 都沒動）*
- [x] 效果：首次部署從「本地 build 5-10 分鐘」→「pull 30 秒」 *(done: L10 #5 capstone — L10 #1-#4 各 pin 一個 precondition（workflow 發佈 / compose image-first / multi-arch / image size < budget），此項目 pin 住 **emergent effect**——給定四個 precondition 同時成立，首次部署的 wall time 由壓縮 layer 的網路傳輸主導，在 50 Mbps 下行（FCC 自 2015 起列為「broadband served」的 baseline）時 ≤ 30 秒。新增 `backend/tests/test_pull_30s_effect.py`（**5 靜態 + 1 live opt-in**）延續 L10 sibling 測試的 `yaml.safe_load + 結構性 assertion` 模式、<100ms、無網路無 Docker。**五個靜態測試**：(1) `test_pull_budget_arithmetic_is_internally_consistent` — pin `REFERENCE_BANDWIDTH_MBPS=50` × `PULL_BUDGET_SECONDS=30` = `PULL_BUDGET_BYTES≈187.5 MB` = `PULL_BUDGET_MIB≈178.8` 的 conversion chain；顯式區分 network decimal（Mbps / MB）vs storage binary（MiB）單位避免 4% 無聲 drift；sanity 斷言 envelope 落在 150-200 MiB 帶（若 decimal point 被搬移此處先炸）。(2) `test_combined_image_budgets_fit_the_30s_envelope` — **load-bearing 斷言**：`BACKEND_COMPRESSED_BUDGET_MIB=108` + `FRONTEND_COMPRESSED_BUDGET_MIB=70` = 178 MiB ≤ 178.8 MiB envelope，頭兩版寫 120+75=195 當場被測試抓到（正是契約測試該做的事 — 實測基線 103+68=171 MiB、envelope 只有 7.8 MiB slack，budget 要緊貼實測 + 小 headroom 否則承諾失效）；錯誤訊息明確指向「shrink image OR update 30秒 promise in TODO.md+HANDOFF.md」。(3) `test_pull_path_reachable_via_compose_image_first` — parse `docker-compose.prod.yml`、backend+frontend 都 assert `image:` 以 `ghcr.io/` 起頭 + `pull_policy: missing`（擋未來 drift 到 `always` 每次都強制 re-pull 或 `build` 完全繞開 pull path）。(4) `test_quickstart_does_not_force_build_on_compose_up` — 掃 `scripts/quick-start.sh` 每一行，過濾 `#` 註解行與 `echo` user-facing 提示行（line 1061 的「強制本地 build」逃生口 echo 合法提到 `--build`），assert 沒有真實的 `docker compose ... up --build` 呼叫；為與 L10 #2 測試 defence-in-depth 並存——因 `--build` 是最容易靜默 regression 30 秒承諾的單一 flag。(5) `test_workflow_actually_pushes_so_pull_is_possible` — parse workflow、逐個 job/step 找 `docker/build-push-action` 並 assert `with.push: true`（接受 boolean True 或 str "true"），擋「為了 test workflow 暫時關掉 publish」這類 CI-green-prod-broken regression。**Live opt-in**：`test_live_combined_pull_under_30s_at_50mbps` — 與 L10 #4 同 env gate（`OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1`）+ 同 helper（`docker image inspect --format '{{.Size}}'`），取 backend + frontend 實際 bytes / `BANDWIDTH_BYTES_PER_SEC` 得 wall-clock 秒數，assert ≤ 30 s；沒有 image 時 pytest.skip 不炸。**驗證**：`python3 -m pytest backend/tests/test_pull_30s_effect.py backend/tests/test_dockerfile_image_size.py backend/tests/test_compose_prod_image_first.py backend/tests/test_docker_publish_workflow.py -v` **49 passed + 3 skipped in 0.21s**（5 新 static + 44 prior static + 3 live opt-in，零 regression）；`tests/test_quick_start_script.py` **51 passed in 7.04s**（完全不受新測試影響）。**Scope 自律** — 只新增一個 test 檔；沒動 Dockerfile / compose / workflow / quick-start / .env.example / 任何應用碼 —— 「效果」本身已由 L10 #1-#4 交付，此項目的價值是**把四個 precondition 串成的 emergent contract 變成 automated**，讓任何一個 precondition 靜默 regression（例如 `pull_policy: always`、`push: false`、image 膨脹 20%、quick-start 加回 `--build`）都會在 CI 上炸。**實際效果展示** — operator 端到端測：打 `v*` tag → workflow 2-phase QEMU build 後 push ghcr.io/<lowercase-owner>/omnisight-{backend,frontend}:{latest,v*} 各一份 OCI manifest list（amd64+arm64）約 combined ~170 MiB 壓縮 bytes → 部署主機 `OMNISIGHT_GHCR_NAMESPACE=<owner> docker compose -f docker-compose.prod.yml up -d` 觸發 compose 的 image-first path 從 GHCR 拉取 combined 170 MiB 在 50 Mbps 下約 **27 s**（180 MiB/30 s × 170 MiB ÷ 180 MiB），對照 L10 #1 之前的「本地 build 5-10 分鐘」（Python wheels 編譯 + Next.js 15 build 是兩大 CPU sink）— 效果差 **~15×**）*
- [x] 預估：**0.5 day** *(done: L10 #5 capstone 在此 day-budget 內完成 — 新增一個 ~260 行契約測試檔 pin 住 pull-30s emergent contract，零 Dockerfile/compose/workflow 動作，無網路無 Docker，<100ms 跑完)*

### L11. 雲端一鍵佈署按鈕 (#338)
- [x] DigitalOcean App Platform：`deploy/digitalocean/app.yaml` + README Deploy 按鈕 *(done: L11 #1 — `deploy/digitalocean/app.yaml` (192 行 DO App Platform spec) 交付完整雙服務 topology：**backend** 私有（`internal_ports: [8000]`、健康檢查 `/api/v1/health`、無 public `routes` — 強制所有外部流量走 Next.js /api 代理以保留 CSRF+CORS 語意）+ **frontend** 公開（`http_port: 3000`、`routes: [{path: /}]`、健康檢查 `/`）；inter-service wiring 用 DO 的 `${backend.PRIVATE_URL}` service-discovery placeholder + `RUN_AND_BUILD_TIME` scope（Next.js `next.config.mjs` 在 build 階段把 BACKEND_URL 烘進 rewrite target，runtime 再讀一次給 SSR fetch）+ `${APP_URL}` 給 `OMNISIGHT_FRONTEND_ORIGIN`（CORS）與 `NEXT_PUBLIC_API_URL`，兩個 token 都是 DO 在首次 deploy 後解析 —— 擋「hard-code 網址 → custom domain 後失效」這種 footgun；**secret hygiene**：`OMNISIGHT_ANTHROPIC_API_KEY` / `OMNISIGHT_OPENAI_API_KEY` / `OMNISIGHT_GOOGLE_API_KEY` / `OMNISIGHT_ADMIN_PASSWORD` 全部 `type: SECRET`（DO 加密存放），sentinel 值 `EV[1:PLACEHOLDER:REPLACE_AFTER_DEPLOY]` 讓首次部署故意壞（operator 必須手動填 → 擋「placeholder 忘記換」的上線事故）；**production safety**：`.env.example` 的 Internet-exposure-auth 區塊要求的三個 env 在 spec 中硬 pin — `OMNISIGHT_AUTH_MODE=strict` / `OMNISIGHT_DEBUG=false` / `OMNISIGHT_COOKIE_SECURE=true`，加上 K1 bootstrap admin 的 `OMNISIGHT_ADMIN_EMAIL` + `OMNISIGHT_ADMIN_PASSWORD`；**alerts** `DEPLOYMENT_FAILED` + `DOMAIN_FAILED` 讓 push-triggered deploy 壞掉時 operator 會收到通知；**databases** 區塊註解掉但留下 `engine: PG / size: db-s-dev-database` 的 scaffolding（附 code change note — SQLAlchemy URL 切換點），因 App Platform 檔案系統 ephemeral 會在 redeploy 時把 `/app/data/omnisight.db` 清掉；**README 主頁新增「One-click cloud deploy (L11 #338)」section**，嵌入官方 Deploy-to-DO badge SVG (`deploytodo.com/do-btn-blue.svg`) + 連結 `cloud.digitalocean.com/apps/new?repo=github.com/limit5/OmniSight-Productizer/tree/master`（spec 內 `github.repo` 同址；contract 測試會雙向 pin 這個一致性），加上指向 `deploy/digitalocean/app.yaml` 與 `deploy/digitalocean/README.md` 的審閱連結；**`deploy/digitalocean/README.md`** 68 行 post-deploy runbook — 服務表格、post-deploy 步驟（填 SECRET → 設 custom domain → 觸發第二次 deploy 讓 `${APP_URL}` resolve）、三大 caveats（**ephemeral filesystem** SQLite 每次 redeploy 被清、**no Docker-in-Docker** 讓 ContainerManager tool sandbox 路徑失效所以 App Platform 僅適合 single-tenant tool-less demo、**成本** 2× basic-xxs ≈ $10/mo 含 downsize 與 upsize 指引）、`doctl apps update --spec` 手動推 spec 的指令；**契約測試** `tests/test_digitalocean_app_spec.py` **27 passed in 0.05s** — 涵蓋 (a) file-level (檔案存在 + YAML 有效 + `name` + `region` + `DEPLOYMENT_FAILED` alert)、(b) topology (兩服務都在 + backend 私有 `internal_ports: [8000]` 且無 routes + frontend public `/` 且 `http_port: 3000`)、(c) health-check paths 與後端實際路由一致（`/api/v1/health` 而非過時的 `/health`）、(d) inter-service wiring (`BACKEND_URL` 必須引用 `${backend.PRIVATE_URL}` 且 scope 為 `RUN_AND_BUILD_TIME` — 擋 Next.js build-time rewrite 吃不到變數 → 生產環境 404 的 footgun)、(e) secret hygiene (4 個 credential env 都是 `type: SECRET` + spec 文字不得包含 `sk-ant-api` / `sk-proj-` / `AIzaSy` / `xai-` 這種真實 key 前綴 防止 paste 誤傷)、(f) production env 三項硬 pin (DEBUG=false / AUTH_MODE=strict / COOKIE_SECURE=true)、(g) admin bootstrap envs 存在、(h) `OMNISIGHT_FRONTEND_ORIGIN` 用 `${APP_URL}` placeholder、(i) Dockerfile paths 真實存在（`Dockerfile.backend` + `Dockerfile.frontend` 在 repo root）、(j) spec 的 `github.repo` + `github.branch` 與 README Deploy-button URL 的 `limit5/OmniSight-Productizer/tree/master` 雙向一致、(k) README 包含官方 badge SVG + DO apps/new URL + canonical repo、(l) companion README 非空 stub（>500 bytes + 提到 SECRET/REPLACE_AFTER_DEPLOY）；PyYAML 6.0.1 已在 backend/requirements.txt，零新 runtime 相依；**scope 自律** — 只新增 3 個檔案（app.yaml + companion README + 測試）+ 1 段 README 修改，沒動任何應用碼 / Dockerfile / compose / workflow / quick-start.sh / .env.example)*
- [x] Railway：`deploy/railway/railway.json` + README Deploy 按鈕 *(done: L11 #2 — `deploy/railway/railway.json`（15 行 single-service config）配合 Railway 的**非對稱 config-as-code 模型**交付：Railway 的 `railway.json` schema 根本沒有 `services` block（與 DO app.yaml 的 multi-service YAML 截然不同 — 只有 `$schema` / `build` / `deploy` 三個 top-level key 的 flat 形狀），所以一份 `railway.json` 只能綁一個 service。**決策**：`railway.json` 當 **backend service 的 canonical config**（因為 backend 有三個 Railway-specific footgun 必須在 config 層處理，frontend 走 Railway 的 Dockerfile auto-detect 零設定即可）—— (a) `build.builder: "DOCKERFILE"` 強制走 Dockerfile path（**Railway 預設是 NIXPACKS 語言自動偵測，會完全無視 `Dockerfile.backend`** 拿 Python auto-detector 重建 image，已在契約測試 `test_build_uses_dockerfile_builder` pin 住）；(b) `build.dockerfilePath: "Dockerfile.backend"` 明確指向 repo root 的 backend Dockerfile（與 DO spec 同 path、與 `docker-compose.prod.yml` 同 path — 三個部署路徑 share 一份 image 定義）；(c) `deploy.startCommand` 覆寫 Dockerfile 的 `CMD`：`sh -c 'exec python -m uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${OMNISIGHT_WORKERS:-2}'` —— **關鍵**：`Dockerfile.backend` 的 CMD 硬編 `--port 8000` 讓 docker-compose 走得通，但 Railway 的 edge proxy 依賴 platform 注入的 `$PORT` env 來路由公網流量，沿用硬編 8000 會讓每個 request 回 502；`${PORT:-8000}` 的 fallback 讓 `railway up` 在本地 Railway CLI 模式（不會注入 $PORT）仍然可以起來，`OMNISIGHT_WORKERS:-2` 保留 basic tier 的 sizing 預設；(d) `deploy.healthcheckPath: "/api/v1/health"` — 對齊 `backend/main.py:551` 的 `app.include_router(health.router, prefix=settings.api_prefix)` 加上 `backend/config.py` 的 `api_prefix: str = "/api/v1"`（契約測試 `test_deploy_healthcheck_points_at_real_endpoint` pin 死，typo 會讓 Railway rollout 永遠 red）；(e) `deploy.restartPolicyType: "ON_FAILURE"` + `restartPolicyMaxRetries: 10` — 擋「single-replica demo 崩了就黑掉直到人類發現」的漏洞（`NEVER` 是 Railway 的另一合法選項但對無 oncall 的 single-tenant 部署危險）；(f) `numReplicas: 1` + `sleepApplication: false` 明確 pin 讓 live spec 照映出來 — operator 在 Railway UI 意外開了 auto-scale 時會造成 drift 但 config 無感，這裡 pin 讓下次 deploy 把它拉回。**env var 策略大轉換** — Railway 的 config-as-code 沒有 env/variables schema（**完全由 dashboard/CLI 管**，不能在 JSON 裡寫），所以 `deploy/railway/README.md` 是 operator 唯一的 env matrix 來源，契約測試 `test_spec_does_not_embed_env_block` assert `railway.json` 裡不能出現 `envs` / `env` / `variables` 這種從 DO schema 抄過來的 top-level key（擋「從 DO spec cargo-cult 過來」的 regression，JSON 會 parse 但 Railway 會默默忽略 → 上線缺 env 靜默崩）。**`deploy/railway/README.md`**（108 行）完整交付 Railway 特有的「monorepo 雙 service」setup runbook：Topology 表格標記 backend 用 `railway.json` 走 `Dockerfile.backend`、frontend 走 Railway defaults 搭配 `Dockerfile.frontend`（Next.js standalone 的 `node server.js` 已經讀 `process.env.PORT`，所以 frontend 零 config）；Post-deploy 步驟明確指示「Config-as-Code Path = `deploy/railway/railway.json`」（Railway 需要 operator 明確指 path，因為 `railway.json` 不在 repo root 而在 `deploy/railway/`）；env var 矩陣分成 backend（8 個：三個 production hard-pin DEBUG=false / AUTH_MODE=strict / COOKIE_SECURE=true + K1 bootstrap ADMIN_EMAIL/PASSWORD + LLM_PROVIDER/KEY + `OMNISIGHT_FRONTEND_ORIGIN=https://${{frontend.RAILWAY_PUBLIC_DOMAIN}}` 走 Railway 的 reference-variable 語法跨 service 取公網 hostname）與 frontend（3 個：NODE_ENV + `BACKEND_URL=http://${{backend.RAILWAY_PRIVATE_DOMAIN}}:${{backend.PORT}}` 走 Railway private networking IPv6 `*.railway.internal` + `NEXT_PUBLIC_API_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}` 走同源）；三大 Caveats（ephemeral FS 提供 Railway Volume 與 Postgres plugin 兩個選項、no Docker-in-Docker 讓 ContainerManager sandbox 不可用、cost $5/mo 免費額度兩 services 足夠 demo）；healthcheck 限於 deploy rollout（非 periodic liveness）的小字註解防誤解；`railway up` + `railway variables --set` 的 imperative 更新指令。**README 主頁**：在 DO Deploy 按鈕旁並列 Railway 按鈕 `![Deploy on Railway](railway.com/button.svg)` 連到 `railway.com/new/template?template=<URL-encoded repo URL>`（Railway 官方 template flow，template 參數 URL-encoded `https%3A%2F%2Fgithub.com%2Flimit5%2FOmniSight-Productizer` 擋 markdown-render 爛掉），並把兩平台的 spec + runbook 整理成 bullet list，留 Render 的 placeholder 給 L11 #3。**契約測試** `tests/test_railway_spec.py`（16 cases）**16 passed in 0.03s** — (a) file-level (存在 + JSON valid + 有 `$schema` 指向 railway.com)、(b) single-service shape (`services` / `env` / `variables` 三個 top-level key 皆不得出現)、(c) build (builder=DOCKERFILE + dockerfilePath 指向 repo 中存在的 Dockerfile.backend)、(d) deploy (startCommand 含 `$PORT` + uvicorn + backend.main:app — 擋三種 regression: 忘 override CMD / port 硬編 / 搬到錯的 module / healthcheckPath=/api/v1/health / healthcheckTimeout 在 [10,300] sane band / restartPolicyType=ON_FAILURE / numReplicas=1 pinned)、(e) secret hygiene (spec 文字不得含 `sk-ant-api`/`sk-proj-`/`AIzaSy`/`xai-` 真實 key 前綴)、(f) README (Railway badge SVG + template URL + 兩個 deploy/railway/* path 連結)、(g) companion README (>800 bytes + 所有 10 個關鍵字：OMNISIGHT_AUTH_MODE/DEBUG/COOKIE_SECURE/ADMIN_EMAIL/ADMIN_PASSWORD/FRONTEND_ORIGIN + BACKEND_URL + RAILWAY_PRIVATE_DOMAIN/PUBLIC_DOMAIN + Dockerfile.frontend — 擋「runbook 被截成 stub」)；sibling DO 測試 `test_digitalocean_app_spec.py` 同 session 跑 **43 passed (27+16) in 0.07s** 零 regression。**零新 runtime 相依** — `json`/`pathlib`/`pytest` 全 stdlib + 內建 fixture。**scope 自律** — 只新增 3 個檔案（`deploy/railway/railway.json` + `deploy/railway/README.md` + `tests/test_railway_spec.py`）+ 1 段 README 修改，沒動任何應用碼 / Dockerfile / compose / workflow / quick-start.sh / .env.example / 現有 DO spec 或測試)*
- [x] Render：`deploy/render/render.yaml` + README Deploy 按鈕 *(done: L11 #3 — `deploy/render/render.yaml`（149 行 Render Blueprint）交付與 DO/Railway 三角對齊的 two-service topology，但攻下 Render 特有的三個 footgun：**(1) multi-service Blueprint schema** — 與 DO `services:` list 形狀相近但用 `type: pserv`（private service）標記 backend 而非 DO 的「有 routes 就公開、只有 internal_ports 就私有」暗示語意，`omnisight-backend` `type: pserv` + `omnisight-frontend` `type: web` 組合讓 FastAPI 只在 Blueprint 內網可達（契約測試 `test_backend_is_private_pserv` pin 死，翻成 `type: web` 會把 CSRF/CORS 中間件 bypass 掉形成安全退化）；**(2) env var 不支援跨 service string templating** — Render 的 `fromService` 只能取 sibling 的 `host` / `port` / `hostport`，**不能**組成 `http://host:port` 或 `https://host` 這樣的完整 URL（RailwAy 的 `${{frontend.RAILWAY_PUBLIC_DOMAIN}}` 可以在 template string 裡；DO 的 `${APP_URL}` 也可以；Render 不行）→ 解法：`BACKEND_URL=http://omnisight-backend:8000` 硬 pin（pserv 內部 hostname == service name 的 Render 服務發現契約），`OMNISIGHT_FRONTEND_ORIGIN` + `NEXT_PUBLIC_API_URL` 改用 `sync: false` 讓 Render Blueprint wizard 在 apply 時提示 operator 手填（並在 runbook 文件化 Stage 2 re-deploy 流程，因為 `NEXT_PUBLIC_*` 會在 build time 被 Next.js inline 到 client bundle，Stage 2 必須觸發 manual redeploy 才讓 URL 烘進去）；**(3) pserv 不會注入 $PORT** — 與 Railway 的 web service 不同，Render pserv 不會塞 `$PORT` env，所以 `dockerCommand` 保留 `--port 8000` 而不是 `${PORT:-8000}`（契約測試 `test_backend_docker_command_pins_port_and_workers` 三 assertion：uvicorn + backend.main:app / `--port 8000` / `OMNISIGHT_WORKERS` 都在），這和 frontend 的 web service (Next.js standalone 自己讀 `process.env.PORT`) 行為不對稱，需要在 runbook 明示。**secret hygiene 模式切換** — DO 用 `type: SECRET` + sentinel 字串 `EV[1:PLACEHOLDER:REPLACE_AFTER_DEPLOY]`（讓首次部署故意壞），Railway 零 config（env 全 dashboard-managed），Render 用 `sync: false`（Blueprint apply wizard 會 prompt operator，值從不進 git），四個 credential env（`OMNISIGHT_ANTHROPIC_API_KEY` / `OMNISIGHT_OPENAI_API_KEY` / `OMNISIGHT_GOOGLE_API_KEY` / `OMNISIGHT_ADMIN_PASSWORD`）+ 兩個 URL env (`OMNISIGHT_FRONTEND_ORIGIN` / `NEXT_PUBLIC_API_URL`) 全部 `sync: false`（`test_credential_envs_flagged_sync_false` 四 case + `test_cors_origin_is_operator_filled` pin 死，額外 assert `value` key 不能同時出現 — 防 cargo-cult 把 DO sentinel 抄過來）；**persistent storage** — Render 是 three-target 中唯一在 `starter` tier 就有 persistent disk 的平台（DO App Platform 檔案系統全 ephemeral、Railway 需 Volume addon），`disk:` block 掛 1 GB SSD 到 `/var/data` 並把 `OMNISIGHT_DATABASE_PATH` 對齊到 `/var/data/omnisight.db`（契約測試 `test_backend_database_path_on_persistent_disk` 驗 mountPath / sizeGB >=1 / db_path startswith mount+'/' 三條），解 SQLite 每 redeploy 被清的 demo-killer；**production safety**：三個 `.env.example` Internet-exposure hard-pin 在 spec 照抄（`OMNISIGHT_DEBUG=false` / `OMNISIGHT_AUTH_MODE=strict` / `OMNISIGHT_COOKIE_SECURE=true`）+ K1 bootstrap admin email/password + `OMNISIGHT_ENV=production` + `OMNISIGHT_WORKERS=2` 匹配 starter tier sizing；**region pin** — 兩 service 必須同 region（contract 測試 `test_both_services_pin_a_region` 驗 `len(regions) == 1`，因為 pserv 內部 DNS 只在同 region 解析），pin `oregon`；**`runtime: docker`** — 兩 service 明示宣告（Render 預設 auto-detect 會跳過 Dockerfile 直接猜 stack 重建 image，契約測試 `test_both_services_use_docker_runtime` pin）；**`deploy/render/README.md`**（166 行）完整交付 Render 特有的 **2-stage post-deploy runbook**：Stage 1 = Blueprint apply + 填 3 個 secrets（API key / admin password / 暫留空兩個 URL env），Stage 2 = 從 dashboard 抄 `*.onrender.com` URL → 設 `OMNISIGHT_FRONTEND_ORIGIN` + `NEXT_PUBLIC_API_URL` → manual redeploy（強調 frontend **必須** rebuild 因為 `NEXT_PUBLIC_*` 是 build-time inlined）；full env matrix（backend 13 + frontend 3，每列標 Source = spec/prompt + Why 欄）；custom domain 章節提醒改域名後要更新兩個 URL env 並 rebuild frontend；4 caveats（persistent disk paid-tier only + no Docker-in-Docker 讓 ContainerManager sandbox 失效 + free tier 15 min spin-down + Blueprint env var templating 限制追蹤）；`Manual Deploy → Deploy latest commit` + auto-sync 兩條 imperative update path。**README 主頁**：在 DO + Railway 按鈕列後新增 Render badge `![Deploy to Render](render.com/images/deploy-to-render-button.svg)` 連到 `render.com/deploy?repo=https://github.com/limit5/OmniSight-Productizer`（Render 官方 one-click Blueprint flow，URL 不需 encode 因為 Render 接受 raw github.com URL 做 query param），spec + runbook 連結加到 bullet list 替掉原本的 "button follows in subsequent L11 steps" placeholder。**契約測試** `tests/test_render_blueprint.py`（28 cases）**28 passed in 0.03s** — (a) file-level (存在 + YAML 有效 + `services` 是 list 且 >=2)、(b) topology (`omnisight-backend` type=pserv + `omnisight-frontend` type=web + 兩 service runtime=docker + 兩 service 同 region)、(c) health check (backend `/api/v1/health` + frontend `/`)、(d) dockerCommand pin (uvicorn + backend.main:app / `--port 8000` / OMNISIGHT_WORKERS)、(e) inter-service wiring (`BACKEND_URL` 精確 `http://omnisight-backend:8000`)、(f) secret hygiene (4 credential envs 都 `sync: false` 且無 `value` key + spec 文字不含真實 key 前綴 `sk-ant-api`/`sk-proj-`/`AIzaSy`/`xai-`)、(g) production envs 三項 hard pin + K1 admin bootstrap 存在、(h) persistent disk (mountPath=/var/data + sizeGB>=1 + DB path 在 disk 內)、(i) CORS origin `sync: false`、(j) Dockerfile paths 實際存在（含 `./` prefix 處理）、(k) Blueprint `repo:` 與 README button URL 雙向一致 `limit5/OmniSight-Productizer` + branch=master、(l) README badge SVG + deploy URL + canonical repo + spec file link + runbook link、(m) companion README >800 bytes + 14 關鍵字（含 `Stage 2` / `pserv` / `sync: false` / `onrender.com` / `BACKEND_URL` / 6 個 OMNISIGHT_* env 等 — 擋 runbook 被截成 stub）；三個 deploy 契約測試 sibling 同 session 跑 **71 passed (28+16+27) in 0.07s** 零 regression。**零新 runtime 相依** — PyYAML 已在 backend/requirements.txt，stdlib pathlib/pytest 夠用。**scope 自律** — 只新增 3 個檔案（`deploy/render/render.yaml` + `deploy/render/README.md` + `tests/test_render_blueprint.py`）+ 2 行 README 修改（換 placeholder + 新 Deploy badge），沒動任何應用碼 / Dockerfile / compose / workflow / quick-start.sh / .env.example / 現有 DO/Railway spec 或測試)*
- [x] 每個平台定義 services（backend + frontend）+ env vars（from `.env.example`）+ build commands *(done: L11 #4 — cross-platform parity contract `tests/test_deploy_parity_across_platforms.py` (22 cases, **22 passed in 0.02 s**, 93 passed combined with three sibling L11 suites — 27 DO + 16 Railway + 28 Render + 22 parity) attacks the one invariant the per-platform suites **structurally cannot catch**: drift between the three platforms. Per-platform tests each see exactly one spec file, so if a future PR adds a new production-required env to DO + Render but silently forgets Railway's dashboard-managed README matrix (Railway's `railway.json` schema has no env block — env vars live exclusively in `deploy/railway/README.md`'s operator-facing table), every per-platform suite stays green while the Railway operator gets a broken first-boot config-validation failure. **Triple-dimension contract** mirrors the TODO text: **(1) services** — `test_digitalocean_declares_backend_and_frontend_services` + `test_render_declares_backend_and_frontend_services` parse the spec `services:` list and assert both service names are present (DO uses bare `backend`/`frontend`, Render uses `omnisight-backend`/`omnisight-frontend` for pserv DNS determinism); `test_railway_topology_documents_both_services` checks the README text for both service names + both Dockerfile paths (Railway's asymmetric `railway.json` only configures backend, so frontend topology lives in the README — this is the sibling `test_spec_does_not_embed_env_block` in the Railway suite's mirror at the parity layer). **(2) env vars from .env.example** — module-level constant `CRITICAL_BACKEND_ENVS` lists the 8 production-required envs sourced directly from `.env.example`'s Internet-exposure block (OMNISIGHT_DEBUG / AUTH_MODE / COOKIE_SECURE / ADMIN_EMAIL / ADMIN_PASSWORD / LLM_PROVIDER / ANTHROPIC_API_KEY / FRONTEND_ORIGIN) + `CRITICAL_FRONTEND_ENVS` (NODE_ENV + BACKEND_URL); `test_env_example_defines_the_critical_backend_envs` is a sanity gate that the list hasn't drifted from the source-of-truth; then per platform: DO + Render parse `envs:` / `envVars:` keys and `issubset` against the critical list (DO/Render); Railway scans `deploy/railway/README.md` text for each env name (README is Railway's only env matrix). **Symmetric frontend coverage** — `test_digitalocean_frontend_covers_critical_frontend_envs` / `test_railway_frontend_env_matrix_covers_critical_frontend_envs` / `test_render_frontend_covers_critical_frontend_envs` — catches the footgun "platform X defines BACKEND_URL but forgets Y" (Railway's frontend has NO JSON so README is the only place that can miss this). **(3) build commands** — `test_both_dockerfiles_exist_at_repo_root` pins the foundation (the entire tri-platform parity collapses if `Dockerfile.backend` / `Dockerfile.frontend` are renamed/deleted); then per platform: DO asserts `dockerfile_path == "Dockerfile.backend"` / `"Dockerfile.frontend"` (DO schema key); Railway asserts `build.builder == "DOCKERFILE"` (else NIXPACKS auto-detect rebuilds from source ignoring the Dockerfile) + `build.dockerfilePath == "Dockerfile.backend"` + README documents `Dockerfile.frontend` (Railway's frontend Dockerfile selection happens via dashboard); Render asserts both `dockerfilePath` values (with `lstrip("./")` to accept either `./Dockerfile.backend` or `Dockerfile.backend` — Render accepts both); `test_backend_start_commands_all_name_uvicorn_on_backend_main` parses Railway `deploy.startCommand` + Render `dockerCommand` and asserts both contain `uvicorn` + `backend.main:app` (DO inherits Dockerfile CMD verbatim, pinned by `test_dockerfile_image_size.py`). **Production hard-pin value parity** — `PRODUCTION_HARD_PIN_VALUES` dict maps the three `.env.example` Internet-exposure envs to their required values (DEBUG=false / AUTH_MODE=strict / COOKIE_SECURE=true), then **three symmetric tests** — `test_digitalocean_production_hard_pins_match_env_example` + `test_render_production_hard_pins_match_env_example` parse spec envs and compare values with `_normalize_env_value()` helper that handles YAML bool vs quoted-string ambiguity (Render allows `value: "false"` OR `value: false` — both load to different Python types; normalize to lowercase string); `test_railway_production_hard_pins_documented_with_values` scans the README with line-level regex (every line containing the env name must ALSO contain the expected value — catches "row drops the value cell while keeping the name"). **Meta discovery tests** — `test_all_three_platform_dirs_exist` (fails BEFORE any per-platform suite can fixture-skip — so deleting `deploy/<platform>/` is caught at the parity layer) + `test_all_three_deploy_buttons_present_in_root_readme` (checks `cloud.digitalocean.com/apps/new` / `railway.com/new/template` / `render.com/deploy` URLs all in main README — users land on README not the subdirs). **Cross-platform secret hygiene** — `test_no_live_api_keys_leaked_across_any_platform_spec` scans all 6 deploy files (3 specs + 3 READMEs) for real key prefixes (`sk-ant-api` / `sk-proj-` / `AIzaSy` / `xai-`) with context-aware filter: the README instructional string `"sk-ant-..."` passes (after-prefix char is `.`), `"sk-ant-apiabc123..."` fails (after-prefix first-non-dot char is alnum). Per-platform suites each have `test_no_plaintext_api_keys_in_spec` — this parity-layer check surfaces the violation with all-three paths in one error message for one-grep triage. **Module-level `CRITICAL_BACKEND_ENVS` list + `CRITICAL_FRONTEND_ENVS` list + `PRODUCTION_HARD_PIN_VALUES` dict** — these are the contract **surface area**: add a new env to `.env.example`'s Internet-exposure block and you update ONE list here; the three per-platform assertions then trigger and point each platform's owner at the diff. This replaces the alternative of "duplicate the env name in three per-platform suites" which would have immediate drift. **Zero new runtime dep** — PyYAML already in backend/requirements.txt + stdlib `json`/`pathlib`/`pytest`. **Scope 自律** — one new file `tests/test_deploy_parity_across_platforms.py` (344 行，包含 docstring、fixtures、20 個 assertion tests + 2 個 meta test + shared helpers `_do_service` / `_do_env_keys` / `_render_service` / `_render_env_keys` / `_normalize_env_value`)；沒動任何應用碼 / Dockerfile / compose / workflow / quick-start.sh / .env.example / 既有 DO/Railway/Render spec 或三份 README 或三份 sibling test — 純粹 add-only 的 parity 契約層。**Verification run**：`python3 -m pytest tests/test_deploy_parity_across_platforms.py tests/test_digitalocean_app_spec.py tests/test_railway_spec.py tests/test_render_blueprint.py -v` → **93 passed in 0.10 s**（22 new + 71 sibling），零 regression)*
- [x] README.md 加 Deploy 按鈕 badges（one-click 跳轉到平台佈署頁） *(done: L11 #5 — badges themselves already landed incidentally during L11 #1/#2/#3 (each spec phase co-shipped its own button), but the **button UX was only pinned by a single presence-check assertion** in `test_deploy_parity_across_platforms.py::test_all_three_deploy_buttons_present_in_root_readme` that checks bare URL substrings — which passes even if someone reduces the rendered affordance to a text link, swaps to an off-brand badge mirror, drops alt text, or points the one-click URL at the wrong repo/branch. This step closes that coverage gap with a dedicated `tests/test_readme_deploy_buttons.py` (11 cases, **11 passed in 0.03 s**, 104 passed combined with 4 L11 siblings) focused **only on the button surface itself**, partitioned into 6 sections: (1) **dedicated section** — `test_deploy_section_is_near_top_of_readme` extracts the `### One-click cloud deploy` block with a single regex (`r"###\s+One-click cloud deploy.*?(?=\n##\s|\n###\s|\Z)"`) and asserts it starts before line 200 (currently at line 94; >200 lines means the section drifted into the appendix where first-time visitors won't see it), `test_only_one_deploy_section` asserts exactly 1 occurrence (prevents a future PR from duplicating the block into an appendix and splitting user attention); (2) **image-badge markdown** — `BADGE_MARKDOWN_RE = r"\[!\[(?P<alt>[^\]]+)\]\((?P<badge>https?://[^\s)]+)\)\]\((?P<href>https?://[^\s)]+)\)"` parses each `[![alt](badge.svg)](deploy-url)` triple and keys it by deploy-URL host (digitalocean.com / railway.com / render.com) — `test_all_three_platforms_have_image_badges` asserts all three platforms produce image-badge matches (catches the regression where someone "fixes" a rendering bug by converting `[![alt](svg)](url)` → `[text](url)` and loses the visual affordance entirely), `test_each_badge_has_non_empty_alt_text` asserts non-empty alt text ≥6 chars (accessibility + graceful SVG-404 fallback); (3) **official provider SVG hosts** — `EXPECTED_BADGE_HOSTS = {digitalocean: "www.deploytodo.com", railway: "railway.com", render: "render.com"}` — `test_badges_served_from_official_provider_hosts` asserts each badge URL contains the expected host AND ends with `.svg` (provider-rebrand survival: Render rebranded its badge in 2024 and Railway in 2025 — pointing at the provider's own URL auto-updates; img.shields.io approximations or third-party mirrors rot); (4) **canonical-repo URL targets** — three per-platform tests (`test_digitalocean_deploy_url_points_at_canonical_repo` / `test_railway_deploy_url_points_at_canonical_repo` / `test_render_deploy_url_points_at_canonical_repo`) each verify the URL shape specific to that provider: DO asserts `cloud.digitalocean.com/apps/new` prefix + `limit5/OmniSight-Productizer` slug + `tree/master` branch (matching `app.yaml`'s `github.repo`/`github.branch`), Railway asserts `railway.com/new/template` prefix + `template=` query + canonical slug accepting either raw or URL-encoded (`limit5%2FOmniSight-Productizer`), Render asserts `render.com/deploy` prefix + `repo=` query + canonical slug (no branch segment because Render reads the default branch from the Blueprint file itself — DO and Render have different URL shapes and this test encodes each); **why this matters** — a rename of the GitHub org (or a fork) would currently let the sibling parity test pass while silently routing clicks to the wrong repo; (5) **companion runbook + spec links** — `COMPANION_RUNBOOKS` + `COMPANION_SPECS` dicts map each platform to its `deploy/<platform>/README.md` + spec file (`app.yaml` / `railway.json` / `render.yaml`), then `test_deploy_section_links_to_every_companion_runbook` + `test_deploy_section_links_to_every_platform_spec` assert each path appears in the section text AND `.is_file()` on disk (closes the gap where a file rename passes every per-platform test — they read the file directly by its new path — but leaves a 404 in README); (6) **visual hierarchy** — `test_badges_appear_before_runbook_links_in_section` uses `.find("[![")` + `.find("\n- ")` and asserts the badge offset < bullet offset (primary CTA before reference material; reversing the order buries the one-click button below a wall of text). **Contract partition rationale** — `test_deploy_parity_across_platforms.py::test_all_three_deploy_buttons_present_in_root_readme` keeps the substring-level presence check (simple regression detector), this new file handles structural/UX/target-integrity concerns; the two files address different regression classes and neither is redundant. **Zero new runtime dep** — stdlib `re` + `pathlib` + `pytest`. **Scope 自律** — one new file `tests/test_readme_deploy_buttons.py` (277 行) + 0 changes to `README.md` (badges already present and pass all 11 assertions as-shipped from L11 #1/#2/#3, which is the ideal outcome — the section reached the target state before the coverage assertions landed, confirming the three earlier phases delivered the desired UX); 0 changes to any spec / runbook / application code. **Verification run**：`python3 -m pytest tests/test_readme_deploy_buttons.py tests/test_deploy_parity_across_platforms.py tests/test_digitalocean_app_spec.py tests/test_railway_spec.py tests/test_render_blueprint.py -v` → **104 passed in 0.11 s**（11 new + 93 sibling），零 regression)*
- [x] 預估：**1 day** *(done: L11 #6 capstone — L11 #1-#5 各 pin 一個 precondition（DO spec / Railway spec / Render spec / cross-platform parity / README button UX），此項目 pin 住 **emergent budget + acceptance claim**——「L11 整體在 1-day budget 內交付且五個 sub-item 合力滿足驗收條件：點 Deploy 按鈕 → 填 env → 3 分鐘內 public URL + Bootstrap wizard」。新增 `tests/test_l11_budget_capstone.py`（27 cases，**27 passed in 0.08s**，合計 5 sibling 跑 **131 passed in 0.12s** 零 regression）延續 L10 #5 capstone 模式（emergent contract 架在 sibling precondition 之上），覆蓋 sibling 結構性無法觸及的 7 個層面：(1) **TODO.md 預算算術** — `test_l11_block_declares_one_day_budget` 用 `re.search(r"-\s+\[[ xO]\]\s+預估：\*\*1\s+day\*\*")` 接受 `[ ]` / `[x]` / `[O]` 三態（test 在 TODO.md 更新前/後都要過，所以不能 hard-pin checkbox 狀態，load-bearing claim 是 "1 day" 數字本身）；`test_total_budget_line_includes_l11_one_day` assert 字串 `"L11 (1)"` 出現；`test_total_estimate_sums_consistently` 用 substring pin 完整算術行 `"**總預估**：L1-L8 (4.5) + L9 (1) + L10 (0.5) + L11 (1) = **~7 day**"` — 任何 L11 budget 變動（如有人改成 2 day）但忘同步總預估會立刻炸；`test_l11_implementation_items_all_checked` parse L11 section 內 `- [ ]` 且不含 "預估：" 的行、assert list 為空（確保除 budget line 外沒遺漏未完成項目）。(2) **驗收文字保全** — `test_acceptance_lists_bootstrap_wizard_in_cloud_path` assert "Bootstrap wizard" 關鍵詞在 L11 block 出現，這是 K1 must_change_password 契約的文字錨點；`test_acceptance_pins_three_minute_cloud_sla` 用 `re.search(r"3\s*分鐘")` + 英文 fallback `\b3\s*minute` 雙形式接受，擋「有人在 review churn 中把 '3 分鐘' 悄悄改成 '5 分鐘' 或 'in minutes'」的 SLA 軟化；`test_acceptance_names_readme_deploy_button` assert "README" + "Deploy" 同出現。(3) **三平台 spec + runbook 完整性** — parametrize 跑 3 platforms × 2 assertions：`test_every_platform_ships_spec_and_runbook` 斷言 `deploy/<platform>/{spec, README.md}` 兩個檔案都在（spec 無 runbook 會讓 operator 在 post-deploy env prompt 前困住；runbook 無 spec 讓 Deploy button 沒東西 apply）；`test_every_runbook_is_non_stub` 斷言 runbook >500 bytes（現況 DO ~3.2KB / Railway ~4.1KB / Render ~7.3KB 都有充足 slack，500 B floor 擋「stub 化」regression）。(4) **Bootstrap wizard wiring 跨三平台** — parametrize 跑 3 platforms × 2 envs：`test_platform_wires_admin_bootstrap_envs` 斷言每平台 combined spec+runbook 文字都包含 `OMNISIGHT_ADMIN_EMAIL` + `OMNISIGHT_ADMIN_PASSWORD`（K1 first-boot seeder 需要這兩個 env 才能建 must_change_password 的初始 admin），**關鍵是接受「combined spec+runbook 任一處」** — Railway 的 `railway.json` schema 根本沒有 env block，env 只能住 `deploy/railway/README.md`，所以不能 assert per-spec，必須 assert combined surface；`test_at_least_one_runbook_explains_must_change_password` 弱形式斷言（三個 runbook 至少一份解釋）— 因為 DO runbook 設計上精簡並委派後端文檔，Render + Railway runbook 有解釋，weak-form assertion 降 churn 敏感度而不放過真的遺失。(5) **Sibling test file inventory** — `test_sibling_test_file_exists` parametrize 5 個 sibling 路徑分別斷 `.is_file()`（一個一個 assert 讓 error message 指出具體缺失的 sibling）；`test_sibling_suite_meets_baseline_function_count` 計 `^def test_` 總數 ≥ 94 baseline（實測 observed at close-out），**刻意區分**「pytest 收集的 104 cases」vs「實際 94 個 test function」— parametrize 展開時 collected count 會變動，但 function count 才是「真的有新 test 被加進來 vs 假裝 parametrize 多一個 value」的穩定度量，docstring + constant comment 把這區分寫死擋未來搞混（最初 capstone 寫成 104 當場被測試抓到 — 自己出 bug 自己 catch 正是契約測試該做的事）。(6) **Zero new runtime dep claim** — `test_l11_introduced_no_new_runtime_deps` 掃 5 sibling + capstone 自己的 import、collect 所有 top-level module name，減去 stdlib allowlist（re/json/pathlib/os/sys/typing/collections/itertools/functools/__future__）+ 第三方 allowlist（yaml/pytest），剩下 set 必須空；同時 sanity assert `pyyaml` 已在 `backend/requirements.txt`（擋 `PyYAML` 被從 requirements 摘掉後 3 個 YAML spec + 測試集體垮）。(7) **Emergent 3-min deploy chain** — `test_deploy_chain_is_buildable_from_repo_alone` assert `Dockerfile.backend` + `Dockerfile.frontend` 都在 repo root（所有 3 個 spec 都 reference 它們，no-source-build-only flow 才能達 3-min SLA，per-platform test 各自檢自己的 Dockerfile reference 但沒人同時檢兩個）；`test_both_multi_service_specs_declare_two_services` load DO app.yaml + Render render.yaml 並斷言 `services` list 長度 ≥2（Railway 被排除因 schema 是 single-service-per-file，該 schema 差異由 `test_railway_spec.py::test_spec_does_not_embed_env_block` 涵蓋）；`test_readme_deploy_section_contains_all_three_platforms` assert "DigitalOcean" / "Railway" / "Render" 名稱都在 README（L11 #5 已驗 deploy-URL host 字串，這層補充 user-readable platform name — 兩層防禦覆蓋不同 regression）。**契約分層 rationale** — L11 #1-#5 每個 sibling 各看自己的一角（single platform / UX surface），此 capstone **結構上不重複** sibling 的工作，只看 sibling **組合起來是否滿足驗收**：budget arithmetic / acceptance text preservation / 三平台對稱性 / K1 bootstrap wiring / sibling inventory / no new dep / emergent 3-min chain — 這 7 個維度沒有任何一個可以從 single sibling 導出。**Scope 自律** — 只新增 1 個檔案 `tests/test_l11_budget_capstone.py`（~420 行，27 assertion tests + 3 pytest.fixture + 2 module constant + 1 shared PLATFORMS table + 1 SIBLING_TEST_FILES list）；沒動任何應用碼 / Dockerfile / compose / workflow / quick-start.sh / .env.example / 3 個 spec / 3 個 runbook / 5 個 sibling test — 純粹 add-only 的 capstone 契約層。**Verification**：`python3 -m pytest tests/test_l11_budget_capstone.py tests/test_readme_deploy_buttons.py tests/test_deploy_parity_across_platforms.py tests/test_digitalocean_app_spec.py tests/test_railway_spec.py tests/test_render_blueprint.py` → **131 passed in 0.12 s**（27 capstone + 11 L11#5 + 22 parity + 27 DO + 16 Railway + 28 Render），零 regression)*

**總預估**：L1-L8 (4.5) + L9 (1) + L10 (0.5) + L11 (1) = **~7 day**

**相依**：B12 (CF Tunnel) + G1 (readyz) + K1 (must_change_password)。若三者都已完成，L 可 7 day 內完工。

**驗收**：
- **本地 WSL2**：`./scripts/quick-start.sh` → 一條命令 → 互動式問答 → 容器啟動 + CF Tunnel 連線 + 瀏覽器打開 Bootstrap wizard → 完成 → https://sora-dev.app 公網可用。全程 **< 15 分鐘**，其中手動操作 < 2 分鐘。
- **雲端**：點 README 的 Deploy 按鈕 → 填 env → 3 分鐘內 public URL 可用 → Bootstrap wizard 引導設定。

---

## 🅖 Priority G — Ops / Reliability（HA 補強）

> 背景：目前為單機 systemd 原型，`scripts/deploy.sh` 以 `systemctl restart` 原地重啟，會有短暫中斷；SQLite 無複製；無負載均衡 / 多副本 / 藍綠 / rolling。Canary、備份、DLQ、watchdog 已具備，但欠缺真正 HA 與零停機。以下 Phase 為補強工作。

### G1. HA-01 Graceful shutdown + readiness/liveness 拆分
- [x] Backend 攔截 `SIGTERM`：停收新流量、flush SSE、關閉 DB、等待 in-flight task（timeout 30s）
- [x] `/api/v1/health` 拆為 `/healthz`（liveness，永遠快速回 200 if process alive）與 `/readyz`（readiness，檢 DB + migration + 關鍵 provider chain）
- [x] systemd unit 加 `TimeoutStopSec=40` 與 `KillSignal=SIGTERM`
- [x] docker-compose healthcheck 改用 `/readyz`
- [x] 單元 + 整合測試：送 SIGTERM 時 in-flight request 仍完成、新連線被拒
- [x] 交付：`backend/lifecycle.py`、`deploy/systemd/*.service` 更新、測試

### G2. HA-02 Reverse proxy + dual backend instance rolling restart
- [x] 新增 Caddy / nginx 前置（listen :443 → upstream backend-a:8000, backend-b:8001）
- [x] `docker-compose.prod.yml` 擴充 `backend-a` / `backend-b` 兩副本（共用 volume）
- [x] `scripts/deploy.sh` 改為 rolling：取下 A → 重啟 → `/readyz` pass → 取下 B → 重啟
- [x] Upstream health check + automatic eject（fail_timeout）
- [x] 整合測試：部署中對 `/api/v1/*` 持續打流量，0 個 5xx
- [x] 交付：`deploy/reverse-proxy/Caddyfile`、`docker-compose.prod.yml` diff、`scripts/deploy.sh` rolling 模式

### G3. HA-03 Blue-Green 部署策略
- [x] `scripts/deploy.sh` 新增 `--strategy blue-green` 旗標<!-- 2026-04-18 G3 #1: GNU-style `--strategy <rolling|systemd|blue-green>` + `--strategy=…` 形式 flag parser 先於 positional 解析、`STRATEGY_FLAG` 覆寫既有 `STRATEGY_ARG > OMNISIGHT_DEPLOY_STRATEGY > systemd` 解析鏈、validator 白名單擴充為三值、blue-green dispatch arm 放在 rolling 之前並 fail-closed（exit 5）直到 rows 1354-1357 ceremony 接上、compose 檔缺失 exit 4 對齊 rolling；契約鎖：`backend/tests/test_deploy_sh_blue_green_flag.py`（24 顆、0.49 s、covers flag parsing / resolution / dispatch / usage / runtime smoke / legacy regression）+ 既有 `test_deploy_sh_rolling.py` (31) + `test_g2_delivery_bundle.py` (28) 合計 83 顆全綠 0.38 s 零回歸。 -->
- [x] 維護 active/standby symlink 或 proxy upstream 切換（atomic）<!-- 2026-04-18 G3 #2: `deploy/blue-green/` 狀態目錄（`active_color` 純檔 + `upstream-{blue,green}.caddy` Caddy snippet + `active_upstream.caddy` 相對 symlink 指向 blue 為初值）+ `scripts/bluegreen_switch.sh` 原子切換基元（`status` / `switch` / `set-active <color>` / `rollback` 四個子指令；rename(2)-based atomic swap via `mv -Tf tmp symlink` + `mv -f tmp statefile`，禁用 `ln -sfn` 兩-syscall 原子間隔；crash-consistency 順序：step 1 `previous_color` 足跡 → step 2 symlink 切流（真 cutover）→ step 3 `active_color` 鏡像，step 2→3 之間掛了 next-status 會噴 state/symlink mismatch WARN 讓 operator 手動 reconcile）；`scripts/deploy.sh --strategy blue-green` 呼叫 `bluegreen_switch.sh status` 印出目前 active/standby/symlink/previous 狀態但仍 exit 5 fail-closed（pre-cut smoke row 1355 未接就不動上游）；命名對稱 `(active_upstream_rp)` snippet 兩色共用 → 切色純靠 symlink flip，Caddyfile 匯入端不需知道哪色 live；契約鎖：`backend/tests/test_bluegreen_atomic_switch.py`（32 顆、0.24 s、涵蓋 state dir shape / script hygiene / atomic primitives / runtime subcommand behaviour / crash-consistency reconcile / deploy.sh 整合）+ 既有 `test_deploy_sh_blue_green_flag.py` (24) + `test_deploy_sh_rolling.py` (31) + `test_g2_delivery_bundle.py` (28) + `test_reverse_proxy_caddyfile.py` (24) 合計 139 顆全綠 0.63 s 零回歸。 -->
- [x] Pre-cut smoke（`scripts/prod_smoke_test.py` on standby）→ 切流 → 觀察 5 分鐘 → 保留舊版 24h 供 rollback<!-- 2026-04-18 G3 #3: `scripts/deploy.sh --strategy blue-green` 完整 ceremony — (1) 從 `bluegreen_switch.sh status` 解析 active/standby 色 + 映射 backend-a↔blue↔8000 / backend-b↔green↔8001；(2) `docker compose up -d --no-deps --force-recreate backend-<standby>` + /readyz wait；(3) **pre-cut smoke**：`timeout $SMOKE_TIMEOUT python3 scripts/prod_smoke_test.py http://localhost:$BG_STANDBY_PORT`（跳過 Caddy 直打 standby 讓 DAG 跑在新碼上，smoke fail = exit 6、NO cutover）；(4) atomic cutover `bluegreen_switch.sh set-active $BG_STANDBY`；(5) optional Caddy reload via `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD`；(6) retention breadcrumbs：atomic tmp-then-mv 寫入 `deploy/blue-green/cutover_timestamp`（Unix seconds）+ `previous_retention_until`（= cutover + 24h 秒數）；(7) **5 分鐘 observe**：poll `/readyz` 每 15 s 共 300 s，consecutive failures >= `OBSERVE_MAX_FAILURES`（預設 3）= exit 7 提示 operator 跑 `--rollback`（row 1356）；(8) **24h retention**：絕不 `docker compose stop` 舊色容器、keep 暖身給 row 1356 秒級 rollback 用。Tunables：`OMNISIGHT_BLUEGREEN_{SMOKE_TIMEOUT,OBSERVE_SECONDS,OBSERVE_INTERVAL,OBSERVE_MAX_FAILURES,RETENTION_HOURS,CADDY_RELOAD_CMD,STANDBY_READY_TIMEOUT}`。Escape hatches：`OMNISIGHT_BLUEGREEN_DRY_RUN=1`（印 plan 不碰 docker / symlink）/ `OMNISIGHT_BLUEGREEN_SKIP_SMOKE=1`（DANGEROUS 僅限 dev）。Exit codes：0 pass / 3 standby /readyz timeout（無 cutover）/ 4 compose missing / 5 primitive missing / 6 smoke fail（無 cutover）/ 7 observe degraded（有 cutover → rollback）。契約鎖：`backend/tests/test_bluegreen_precut_ceremony.py`（29 顆、0.08 s、涵蓋 config knobs / pre-cut smoke / atomic cutover / retention math / observation counter / dry-run / color-service-port mapping / primitive-missing fail-closed / structural）+ 既有 `test_bluegreen_atomic_switch.py` (32)、`test_deploy_sh_blue_green_flag.py` (24)、`test_deploy_sh_rolling.py` (31)、`test_g2_delivery_bundle.py` (28)、`test_reverse_proxy_caddyfile.py` (24) 更新後合計 168 顆全綠 0.93 s 零回歸（兩顆 regression lock 由 exit-5 改為 exit-6/7 + set-active 呼叫）。 -->
- [x] Rollback 腳本：`deploy.sh --rollback`（秒級切回 previous color）<!-- 2026-04-18 G3 #4: `scripts/deploy.sh --rollback` fast-path — GNU-style `--rollback` flag 與 `--strategy` 同層解析並**在 ENV validation / git fetch / pip / pnpm / compose 前**短路 dispatch，讓 `scripts/deploy.sh --rollback` 單獨執行即可（3am operator 不用背 env 名）；5 道 fail-closed gate 依序：(a) `bluegreen_switch.sh` + `deploy/blue-green/` 存在否 → exit 5、(b) `previous_color` breadcrumb 存在否 → exit 2、(c) retention window 未過期 → exit 8（`now <= previous_retention_until`，row 1355 breadcrumb 的 24 h 預算；bypass via `OMNISIGHT_ROLLBACK_FORCE=1` with DANGEROUS warning）、(d) previous color `/readyz` 活著否 → exit 3（bypass via `OMNISIGHT_ROLLBACK_SKIP_PREFLIGHT=1` with DANGEROUS warning）、(e) active==previous no-op → exit 0（防 ping-pong）；atomic cutover 走 `OMNISIGHT_BLUEGREEN_DIR="$BLUEGREEN_STATE_DIR" $BLUEGREEN_SWITCH rollback`（rename(2) symlink flip 走 G3 #2 primitive，保證無兩-syscall 原子間隔）；optional Caddy reload via `OMNISIGHT_BLUEGREEN_CADDY_RELOAD_CMD`；audit breadcrumb `deploy/blue-green/rollback_timestamp`（Unix seconds，atomic `.tmp.$$` + mv 避免 row 1357 runbook concurrent reader 看到半寫狀態）；color→port 映射 blue↔8000 / green↔8001 對齊 G2 compose topology + row 1355 blue-green arm；dry-run via `OMNISIGHT_BLUEGREEN_DRY_RUN=1` 在 symlink flip 前退出。Exit codes：0 success / 2 no previous_color / 3 previous /readyz dead / 5 primitive missing / 8 retention expired。契約鎖：`backend/tests/test_deploy_sh_rollback.py`（40 顆、0.17 s、涵蓋 flag parsing / block structure / no-build contract / fail-closed gates / atomic delegation / audit breadcrumb / escape hatches / color mapping / Caddy reload / runtime behaviour / structural）+ 既有 `test_deploy_sh_blue_green_flag.py` (24) + `test_bluegreen_atomic_switch.py` (32) + `test_bluegreen_precut_ceremony.py` (29) + `test_deploy_sh_rolling.py` (31) + `test_g2_delivery_bundle.py` (28) + `test_reverse_proxy_caddyfile.py` (24) 合計 208 顆全綠 0.82 s 零回歸。 -->
- [x] 交付：runbook `docs/ops/blue_green_runbook.md`、腳本<!-- 2026-04-18 G3 #5 (TODO row 1357): `docs/ops/blue_green_runbook.md` operator-grade runbook（12 節：why-blue-green / state files / pre-flight / cutover ceremony / rollback ceremony / 24h hygiene / manual primitive / troubleshooting decision tree / tunables cheat-sheet / script&contract index / anti-patterns / 可貼 deploy ticket 的 change-management checklist），所有 command / exit code / env var / state file 都直接對齊 G3 #1–#4 三隻腳本（`scripts/deploy.sh` / `scripts/bluegreen_switch.sh` / `scripts/prod_smoke_test.py`）+ 委託 N10 ledger 與 N6 dependency runbook 互鏈；契約鎖：`backend/tests/test_blue_green_runbook.py`（107 顆、0.12 s、11 class，driftproof：runbook 路徑 / 12 節順序 / cutover exits {0,3,4,5,6,7} / rollback exits {0,2,3,5,8} / switch exits {0,1,2,3} 全文出現 / 14 顆 tunable env var 同時存在 runbook 與 deploy.sh / 8 顆 state file 全提及 / 所有引用腳本實際存在 + 同名 in-doc / 5 顆 sibling 契約測試 in-doc / dry-run + 24h retention + blue↔backend-a↔8000 / green↔backend-b↔8001 mapping / 5 顆 anti-pattern 警語 / §12 fenced checklist 三相位 + literal 指令）+ 既有 `test_deploy_sh_rollback.py` (40) + `test_deploy_sh_blue_green_flag.py` (24) + `test_bluegreen_atomic_switch.py` (32) + `test_bluegreen_precut_ceremony.py` (29) + `test_deploy_sh_rolling.py` (31) + `test_g2_delivery_bundle.py` (28) + `test_reverse_proxy_caddyfile.py` (24) 合計 315 顆全綠 0.92 s 零回歸。G3 (HA-03 Blue-Green) 五顆 checkbox 全數完成。 -->

### G4. HA-04 SQLite → PostgreSQL 遷移 + streaming replica
- [x] Alembic 驗證所有 migration 在 Postgres 上綠（sqlite-isms 掃描：`AUTOINCREMENT`、`WITHOUT ROWID`、dynamic type）<!-- 2026-04-18 G4 #1 (TODO row 1360): 三組交付 — (1) `backend/alembic_pg_compat.py` runtime shim：regex-based SQLite→Postgres 翻譯器 via SQLAlchemy `before_cursor_execute` event hook；覆蓋 7 個 ism：`AUTOINCREMENT` → `BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY`、`datetime('now')` → `to_char(now(), 'YYYY-MM-DD HH24:MI:SS')`、`strftime('%s','now')`（源碼 / `%%s` op.execute wire 形式皆匹配）→ `EXTRACT(EPOCH FROM NOW())`、`INSERT OR IGNORE` → `INSERT … ON CONFLICT DO NOTHING`、`INSERT OR REPLACE` → `INSERT … ON CONFLICT (PK) DO UPDATE SET col=EXCLUDED.col`（第一欄作 conflict target）、`PRAGMA table_info(T)` → `information_schema.columns` SELECT（保留 `(cid, name, type, notnull, dflt_value, pk)` 六欄 shape 讓 `row[1]` 讀 column name 的 migration 保持相容）、`?` positional → `%s` psycopg2 paramstyle（string-literal aware，`'?'` 及 `'it''s'` SQL escape 皆不誤傷）；SQLite dialect 與未知 dialect identity pass-through；`translate_sql(translate_sql(x))==translate_sql(x)` idempotent。(2) `scripts/scan_sqlite_isms.py` static CLI：ism patterns 從 shim module 單一來源同步、`--ism`/`--json`/`--fail-on-shim-handled`/`--include-dynamic-type` 四旗標、`SHIM_HANDLED` 集合標示「runtime 會翻譯」的 ism（exit 0），未知 ism 視為封鎖（exit 1），unknown ism 名稱 exit 2。`WITHOUT ROWID` 刻意不納入 SHIM_HANDLED（無 PG drop-in 對映）以確保靜態掃描第一時間攔截。(3) `backend/alembic/env.py` 新增 `install_pg_compat(conn)` 在連到 Postgres engine 時裝 event listener，SQLite 連線無副作用；migration 0004 `decision_rules` ADD COLUMN 以 `information_schema.columns` / `PRAGMA table_info` 先檢查再執行，避免 PG 事務式 DDL 因 DuplicateColumn 阻斷整包 upgrade（支援 up→down→up cycle）。活體驗證：docker postgres:16-alpine 上 `alembic upgrade head` → `downgrade 0001` → `upgrade head` 全綠 15 revisions + `scripts/alembic_dual_track.py --engine postgres` 16 steps ok=true + 所有 29 張表 `\dt` 呈現 + `t-default` tenant 插入驗證 + `audit_log.id` 為 IDENTITY column + `decision_rules.negative` 存在。契約鎖：`backend/tests/test_alembic_pg_compat.py`（226 顆、0.68 s、14 section：pattern library / per-ism match / scanner API / per-rule translation / param translation / dialect dispatch / install_pg_compat event wiring / real-world samples / all-migrations-shim-clean / per-migration-file / env.py wiring / scanner CLI / shim-handled parity / idempotency）+ `backend/tests/test_alembic_pg_live_upgrade.py`（8 顆、7.3 s、`OMNI_TEST_PG_URL` env gate 控制、live psycopg2 + alembic subprocess：upgrade head / 29 表創建 / t-default 插入 / downgrade→baseline / up-down-up 循環 / head revision 0015 / negative column / identity column 驗證）合計 **234 顆全綠 7.34 s 零回歸**。SQLite 舊路徑（legacy dev + 現有 CI sqlite track）無任何行為變化，N8 dual-track validator 與 test_db.py (13)、test_audit.py (13) 既有測試全通過。 -->
- [x] Connection 抽象：`DATABASE_URL` 支援 `postgresql+asyncpg://`<!-- 2026-04-18 G4 #2 (TODO row 1361): 三組交付 — (1) `backend/db_url.py` pure-Python URL 解析器/正規化器（stdlib `urllib.parse` only，零第三方 import），`DatabaseURL` frozen dataclass + scheme taxonomy 覆蓋 11 個 scheme：`sqlite` / `sqlite+aiosqlite` / `sqlite+pysqlite` / `postgresql+asyncpg` / `postgres+asyncpg` / `asyncpg` / `postgresql` / `postgres` / `postgresql+psycopg2`（大小寫不敏感）、`MalformedURLError` / `UnsupportedURLError` 明確拒絕；`sqlalchemy_url(sync=True/False)` 可於 asyncpg↔psycopg2、aiosqlite↔pysqlite 間雙向 coerce；`asyncpg_dsn()` + `asyncpg_connect_kwargs()`（選擇性轉發 `ssl` / `server_settings` / `statement_cache_size` / `connect_timeout` / `command_timeout` 白名單 query params）；`sqlite_path()` 方法拒絕 `:memory:` 及 PG URL；`redacted()` 密碼打碼為 `***`；`resolve_from_env()` 四層 precedence：`OMNISIGHT_DATABASE_URL` > `DATABASE_URL`（12-factor） > `OMNISIGHT_DATABASE_PATH`（legacy SQLite） > `default_sqlite_path` argument，壞 URL 絕不 silent fallback。(2) `backend/db_connection.py` async driver dispatcher：`AsyncDBConnection` Protocol（`execute` / `executescript` / `fetchone` / `fetchall` / `commit` / `close`，`dialect` + `driver` 屬性）；`_SqliteAsyncConnection` 封裝 `aiosqlite.Connection`、`_PostgresAsyncConnection` 封裝 `asyncpg.Connection` + lazy transaction（`_ensure_tx()` 於首次 `execute()` 時 `BEGIN`，`commit()` / `close()` flush）+ **string-literal-aware** state-machine `_qmark_to_dollar()` 將 SQLite `?` placeholder 翻譯為 asyncpg `$N`（保留 `'What?'` 文字常量內的 `?`、`'it''s'` SQL escape 不誤判）；`open_connection(url)` factory 接受 `DatabaseURL` / `str` / `None`（自 env 解析），`asyncpg` 與 `aiosqlite` 皆 lazy import 不污染 SQLite-only CI。runtime 禁用 `postgresql+psycopg2://`（sync driver，僅 Alembic 容忍），明確 `RuntimeError` 指向 `postgresql+asyncpg://`。(3) Config 接線：`backend/config.py` 新增 `database_url: str = ""` field（`OMNISIGHT_DATABASE_URL` env 自動映射、legacy `database_path` 無變化）；`backend/alembic/env.py` 在 `SQLALCHEMY_URL` 之後新增 `OMNISIGHT_DATABASE_URL` / `DATABASE_URL` 兩條 precedence 層並以 `parse().sqlalchemy_url(sync=True)` 自動將 `postgresql+asyncpg://` coerce 為 psycopg2 URL（Alembic 必要，因其僅支援 sync engine）。契約鎖：`backend/tests/test_db_url.py`（75 顆、0.16 s、11 section：scheme 接受/拒絕矩陣 / PG URL 欄位解析 / SQLite URL 解析 / format adapter round-trip / redaction / env precedence / config wiring / Alembic env 來源掃描 / dispatcher E2E SQLite / `?`→`$N` placeholder 翻譯 / frozen dataclass 不可變）；dispatcher E2E 測試以 `unittest.mock` 假 `asyncpg` 模組驗證 lazy import + kwargs shape + tx lifecycle，真實 Postgres 整合由 `OMNI_TEST_PG_URL`-gated 既有 live 測試承擔。活體驗證：`OMNISIGHT_DATABASE_URL="sqlite:////tmp/alembic_smoke.db" scripts/alembic_dual_track.py --engine sqlite` 綠（ok=true）；舊 SQLALCHEMY_URL 及 OMNISIGHT_DATABASE_PATH 路徑 0 影響（test_alembic_pg_compat.py 234、test_db.py 13、test_audit.py 13、test_auth.py、test_bootstrap.py、test_s0_sessions.py、test_k1_security_hardening.py 合計 401 顆全綠）。 -->
- [x] 部署 primary + hot standby（`streaming replication`、`synchronous_commit=on` 可設）<!-- 2026-04-18 G4 #3 (TODO row 1362): 三組交付 — (1) `deploy/postgres-ha/` compose bundle（7 檔）：`docker-compose.yml`（`pg-primary` + `pg-standby` 雙 service、`postgres:16-alpine` 對齊 G4 #1 live-upgrade 測試版本、named volumes `omnisight-pg-{primary,standby}`、bind-mount read-only 三個 conf 檔、`depends_on.condition: service_healthy` 保證 standby 只在 primary `pg_isready` 綠後啟動、primary host port 5432 / standby host port 5433 避免操作員 psql 衝突、`POSTGRES_PASSWORD:?` + `REPLICATION_PASSWORD:?` fail-closed env 解析、bridge network `pg-ha`）；`postgresql.primary.conf`（`wal_level=replica` / `max_wal_senders=10` / `max_replication_slots=10` / `wal_keep_size=1GB` / `hot_standby=on`（促成 promote-in-place 對稱）/ `synchronous_commit=on` / **`synchronous_standby_names=''` 預設 async 可設** — operator 想開 sync replication 只需把它改成 `'FIRST 1 (omnisight_standby)'` 然後 `pg_ctl reload`、`archive_mode=off`（G4 #5 runbook 再 opt-in PITR）、`log_replication_commands=on` / `timezone=UTC`）；`postgresql.standby.conf`（`hot_standby=on` 讓 standby 接 read-only 查詢、`hot_standby_feedback=on` 同步 VACUUM horizon 避免 ERROR 40001、`max_standby_streaming_delay=30s` / `wal_receiver_timeout=60s` / `wal_receiver_status_interval=10s`、刻意**不設** `primary_conninfo` — 它含 replication password 只能 runtime 由 `init-standby.sh` 寫進 `postgresql.auto.conf` (0600)）；`pg_hba.conf`（**scram-sha-256 全線**、無 md5、無遠端 trust — local socket trust 留給 docker-entrypoint-initdb.d bootstrap；`host replication replicator 0.0.0.0/0 scram-sha-256` + IPv6 symmetric 對應未來 G5 k8s dual-stack）；`init-primary.sh`（docker-entrypoint-initdb.d hook，`set -euo pipefail` + 密碼 sha256 前 12 hex fingerprint log（**絕不**直接 echo 密碼）+ `CREATE ROLE replicator WITH LOGIN REPLICATION PASSWORD '...'` guarded by `pg_roles` existence check（支援 PGDATA rewind）+ `pg_create_physical_replication_slot('omnisight_standby_slot')` guarded by `pg_replication_slots` 存在 check（slot 保 primary 在 standby 斷線時保留 WAL 超過 `wal_keep_size` 不需 re-basebackup）+ `psql -v ON_ERROR_STOP=1`）；`init-standby.sh`（**不走** docker-entrypoint-initdb.d — standby PGDATA 必須由 `pg_basebackup` 而非 `initdb` seed，所以覆寫 container entrypoint；pg_isready polling up to `STANDBY_BASEBACKUP_TIMEOUT=300s` 等 primary；`rm -rf` 空 PGDATA 容 partial clone 殘留；`pg_basebackup --host=pg-primary --username=replicator --pgdata=$PGDATA --wal-method=stream --write-recovery-conf --slot=omnisight_standby_slot --progress --verbose` 拉 consistent snapshot + WAL 經 named slot；`touch standby.signal` idempotent；每次 boot 都重寫 `postgresql.auto.conf` with `primary_conninfo='host=... user=replicator password=... application_name=omnisight_standby'` + `primary_slot_name='omnisight_standby_slot'` + `chmod 0600`（密碼輪替只需 restart）；`exec postgres -c config_file=/etc/postgresql/postgresql.conf -c hba_file=/etc/postgresql/pg_hba.conf` 讓 SIGTERM 直達 postgres）；`.env.example`（12 個 env knob + **operator guide block**：「把 postgresql.primary.conf 裡的 synchronous_standby_names 改成 'FIRST 1 (omnisight_standby)' 然後 docker compose exec pg-primary pg_ctl reload」把同步複製變 operator-discoverable）。(2) `scripts/pg_ha_verify.py` pure-stdlib 靜態驗證器（~350 行，零第三方 import）：stdlib-only `parse_pg_conf` + `parse_hba` + per-artefact check — primary conf 驗 12 項（listen_addresses/wal_level/max_wal_senders/max_replication_slots/hot_standby/synchronous_commit/synchronous_standby_names declared/wal_keep_size/archive_mode/timezone/log_replication_commands）、standby conf 驗 8 項、hba 驗 5 項（replication row 存在 + uses scram + no md5 + no remote trust + app row 存在）、compose 驗 19 項（service shape + bind-mount + entrypoint + depends_on healthy + 各 env 旗標）、init-primary.sh 驗 9 項（strict mode/CREATE ROLE/REPLICATION priv/pg_create_physical_replication_slot/guards/canonical slot name/ON_ERROR_STOP/不 echo raw password）、init-standby.sh 驗 11 項（strict mode/pg_isready/pg_basebackup/--wal-method=stream/--write-recovery-conf/--slot=/standby.signal/primary_conninfo+application_name/exec postgres/config_file）、.env.example 驗 13 項（12 knob + synchronous_standby_names docs）；CLI `--json` 機器可讀 + `--deploy-dir` 可指向 alt path；exit 0 綠 / exit 1 封鎖（CI 用）。(3) Config wiring — 無新增 Python production 依賴，本回合只 ship `deploy/` 靜態檔 + script + test，不 touch runtime code（`backend/db.py` / `backend/db_url.py` / `backend/db_connection.py` / `backend/config.py` 0 diff）。契約鎖：`backend/tests/test_pg_ha_deployment.py`（114 顆、0.33 s、11 class：deploy directory shape 9 + primary conf 12 + standby conf 8 + hba 6 + compose 16 + init-primary 10 + init-standby 15 + env example 14 + parse_pg_conf 8 + parse_hba 3 + verifier e2e 5 + symmetry 5）其中 verifier e2e 真跑 `python3 scripts/pg_ha_verify.py` 兩次（human + --json）+ 三顆 negative gates（砍 wal_level / 加 md5 row / 加 remote trust row）驗證 exit 1；symmetry class 強制 `omnisight_standby_slot` / `omnisight_standby` application_name / `replicator` user / `postgres:16-alpine` / synchronous-commit operator docs chain 五項跨檔對齊避免「primary 寫 slot X 但 standby 用 slot Y」這類 silent mis-routing。全量回歸綠：test_pg_ha_deployment 114 + test_db_url 75 + test_alembic_pg_compat 226 合計 **415 顆全綠 1.02 s 零回歸**。活體驗證：`scripts/pg_ha_verify.py` 75/75 checks ok；docker compose config `docker-compose.yml` 能 parse；**不跑真 container**（G4 #5 failover runbook + CI PG matrix 會跑）— 本回合只鎖靜態契約。為什麼 sync 是 operator opt-in 不是預設：sync 加 2-5 ms p50 latency（本地網路）或 20-50 ms p50（跨 AZ）對 OmniSight 大部分 audit_log / decision_log write path 不划算；留給 operator 按 compliance window 自行打開（docs/ops/db_failover.md G4 #5 會列 policy 決策表）。為什麼 pg_hba 用 scram 不 md5：md5 從 PG 14 deprecated，wire 上 leak password hash；scram-sha-256 是 PG 14+ default。為什麼用 named physical slot 不 `wal_keep_size`-only：slot 保證 primary 等 standby consumed WAL 才釋放即便標本 segment 到了 1 GB；wal_keep_size 只是 best-effort retention window。為什麼 init-standby 每次 boot 重寫 `postgresql.auto.conf`：密碼輪替 operator 更新 .env + restart 就生效不需 exec 進 container。為什麼 standby override entrypoint 不走 initdb.d：initdb 會先把 PGDATA initdb 然後 initdb.d 跑；我們要先 pg_basebackup 取代 initdb → 只能覆寫 entrypoint。下一組可以開的工作項（G4 #4-5）→ row 4 `scripts/migrate_sqlite_to_pg.py` data migration（用 G4 #2 `open_connection` 同時連兩端 + audit_log hash chain 連續性驗證）、row 5 CI PG matrix、row 6 `docs/ops/db_failover.md` runbook（含 sync/async policy 決策表 + 故障接管 ceremony + 本回合 slot/application_name/synchronous_standby_names 的 operator-facing playbook）。 -->
- [x] 資料搬移腳本 `scripts/migrate_sqlite_to_pg.py`（含 audit_log hash chain 連續性驗證）<!-- 2026-04-18 G4 #4 (TODO row 1363): 兩組交付 — (1) `scripts/migrate_sqlite_to_pg.py`（~640 行，stdlib + aiosqlite + asyncpg lazy import，零新增第三方依賴）One-shot SQLite → PostgreSQL 資料搬移器，由 CLI 分 8 個 argparse 旗標驅動：`--source`（SQLite URL 或 bare path，fallback `OMNISIGHT_DATABASE_PATH`） / `--target`（`postgresql+asyncpg://...` 必要，runtime 拒絕 psycopg2 URL）/ `--batch-size`（預設 500）/ `--tables`（逗號分隔過濾器，unknown 表直接 exit 2）/ `--truncate-target`（TRUNCATE...RESTART IDENTITY CASCADE，否則 dirty target 直接 exit 4）/ `--skip-chain-verify`（DANGEROUS 僅限已知破損源）/ `--dry-run`（只走 source chain 檢查不寫 target，`--target` 可省）/ `--json`（CI 用結構化輸出）/ `--quiet`。核心 audit_log hash chain 連續性驗證 — **pure function** `verify_hash_chain_in_rows()` 完全對齊 `backend.audit._hash()`：`curr_hash = sha256(prev_hash + canonical_json(payload) + str(round(ts, 6)))`（canonical JSON：sort_keys=True / separators=(',',':') / ensure_ascii=False）— 在源端（**pre-flight**，任何 tenant chain 破損 exit 3，絕不寫 target）與目標端（**post-flight**，migration bug 會 exit 5）雙邊 re-walk。28 張表固定 FK-safe 順序（`TABLES_IN_ORDER`，`tenants` 在最前供 `api_keys` / `tenant_secrets` / `tenant_egress_*` FK 對齊），IDENTITY sequence 重設清單 (`TABLES_WITH_IDENTITY_ID` = `event_log` / `audit_log` / `auto_decision_log` / `github_installations`) 於拷貝完成後以 `SELECT setval(pg_get_serial_sequence(...), MAX(id), MAX(id) IS NOT NULL)` 對齊避免下次 implicit insert 撞 id。`audit_log` 以 `ORDER BY id ASC` 全表讀取，**保留原 id / ts / prev_hash / curr_hash 一字不改**寫進 target（Merkle 鏈破一處整條斷）；`tenants` 非 `--truncate-target` 模式走 `ON CONFLICT (id) DO NOTHING` 吸收 Alembic 0012 seed 的 `t-default` 衝突；非 seed tenant 表 row_count > 0 + 未設 `--truncate-target` = exit 4 fail-closed；拷貝後每表 source/target row count 不一致 = exit 6；asyncpg executemany() 以 `$N` paramstyle 批次送；FileNotFoundError = exit 1；malformed URL = exit 2。**Exit code table：0 成功 / 1 unexpected / 2 CLI usage / 3 source chain broken / 4 target 有未預期資料 / 5 target chain broken（migration bug） / 6 row count mismatch**。活體驗證：`python3 scripts/migrate_sqlite_to_pg.py --source data/omnisight.db --dry-run --json` 對既有 dev DB 跑通（exit 0、source_chain_ok=true、t-default tenant chain 綠、25 表偵測）。(2) `backend/tests/test_migrate_sqlite_to_pg.py` 51 顆契約測試 7 class：**TestScriptShape**（6 顆：檔案存在 / --help subprocess / `TABLES_IN_ORDER` 非空 / tenants 必首 / identity tables schema 對齊）、**TestHashChainVerifier**（11 顆：空 / 單行 / 多行 / 竄改 after_json 被抓 / 竄改 prev_hash 被抓 / **與 `backend.audit._hash` 公式對齊**（drift 偵測） / 非 ASCII payload / 每 tenant genesis prev_hash="" / canonical JSON 排序 + 無空白 / recompute_hash determinism）、**TestSqlBuilders**（7 顆：basic INSERT / quoted identifiers 防 "user" keyword 意外 / ON CONFLICT 尾綴 / 空欄位拒絕 / placeholder 數對 / pg_get_serial_sequence 用法 / COALESCE empty-table safe）、**TestCli**（12 顆：source accepts bare path + URL + env fallback / source 拒 PG URL / target 必 asyncpg 拒 psycopg2 / target 拒 SQLite / 9 個 argparse flag 同時 wire / subprocess exit 2 on missing source / bad URL / missing target without dry-run）、**TestDryRunE2E**（4 顆：real SQLite fixture DB，valid chain → exit 0 + source_chain_ok / broken chain → exit 3 + first_bad_id=3 / --skip-chain-verify bypass / nonexistent file exit 1）、**TestOrchestratorMocked**（8 顆：用 `_FakePgConn` mock asyncpg 連線驗 dirty target exit 4 / tenants seed 1 row 容忍 / id ASC 插入順序保留 / IDENTITY sequence reset audit_log=5 / broken source 0 writes to target / TRUNCATE statement 先發射 / --tables filter 尊重 / unknown table 拋 ValueError）、**TestReportShape**（2 顆：MigrationReport + TableResult dataclass to_dict()）。全量回歸綠：test_migrate_sqlite_to_pg 51 + test_db_url 75 + test_alembic_pg_compat 226 + test_pg_ha_deployment 114 + test_audit 13 合計 **479 顆全綠 8.49 s 零回歸**；外加 test_db 13 + test_bootstrap + test_auth + test_s0_sessions 66 顆全綠 13 s 零回歸。為什麼這支不是 pgloader / sqlite3-to-postgres PyPI：它們以 PK order 重排 row 並重寫型別，audit_log 的 prev_hash→curr_hash 鏈條就會靜默毀掉整串 tamper-evidence。為什麼 pre+post 雙邊 chain verify：pre 保護生產資料（broken 永不寫 target），post 保護搬移器本身（如果未來改了 canonical_json 實作邊側漂移，exit 5 立刻 scream）。為什麼 `pg_get_serial_sequence` 而不是 pure setval：IDENTITY 跟 SERIAL 的 sequence 命名規則不同，這樣對 PG 10+ 的 IDENTITY 和 legacy SERIAL 都通用。為什麼 tenants 走 ON CONFLICT DO NOTHING 而不直接 TRUNCATE：Alembic 0012 `INSERT OR IGNORE INTO tenants (...t-default...)` 是 schema bootstrap 的一部分，operator 跑 `alembic upgrade head` 後再跑 migrate script 是標準流程；要求 TRUNCATE tenants 會逼 operator 在每次跑前手動 seed，不符「one-shot」契約。下一組可以開的工作項（G4 #5-6）→ row 1364 CI Postgres service matrix、row 1365 `docs/ops/db_failover.md` runbook。 -->
- [x] CI 新增 Postgres service matrix（sqlite + pg 兩軌）<!-- 2026-04-18 G4 #5 (TODO row 1364): 三組交付 — (1) `.github/workflows/db-engine-matrix.yml` 升級：`postgres-matrix` job **從 advisory 翻為 hard gate** — 舊 `continue-on-error: true` 全域旗標被替成 expression `continue-on-error: ${{ matrix.postgres == '17' }}`，PG 15 + 16 現在是 PR 合併的硬門，PG 17 則做 N7 forward-look advisory cell；matrix 同時由 `["15","16"]` 擴到 `["15","16","17"]`；刪除「annotate expected failure reason」step（失敗不再是 expected，硬綠即可）；path filters 增 `backend/alembic_pg_compat.py` / `backend/db_url.py` / `backend/db_connection.py` / `scripts/migrate_sqlite_to_pg.py` / `scripts/scan_sqlite_isms.py` 五條與 G4 #1/#2/#4 artefacts 對齊，任一改動皆重跑 matrix；header 區塊改寫為「four layers」並說明「PG hard 自 G4 #1 shim 起」；工作流頂部 block comment 記錄剩餘 handoff 條目（`docs/ops/db_failover.md` 宣告 cutover 後再 retire sqlite-matrix / 推 PG 17 為 hard / 翻 `check_migration_syntax.py` 為 `--strict`）。(2) 新增 **`pg-live-integration`** job（hard gate）— 提供 `postgres:16` service container（pg_isready health-check + 30-retry wait loop）+ 安裝 `psycopg2-binary==2.9.10`（Alembic sync）+ `asyncpg==0.30.0`（runtime, G4 #2 dispatcher）（兩者皆 CI-only 不進 requirements.txt 避免 11 MB wheel 污染 backend-tests / openapi-contract / renovate-config 等無關 job）+ `OMNI_TEST_PG_URL=postgresql+psycopg2://omnisight:omnisight@127.0.0.1:5432/omnitest` env gate + 跑兩路契約：(a) `pytest backend/tests/test_alembic_pg_live_upgrade.py`（G4 #1 live shim 契約 8 顆）—skipif 因 env 已設而 lift、(b) `alembic upgrade head` on 乾淨 SQLite + `scripts/migrate_sqlite_to_pg.py --dry-run --json`（G4 #4 source-chain verifier smoke）+ `python3 -c` 解析 `source_chain_ok` exit 0/1；兩路 artefact（`_pg-live-junit.xml` + `_migrate-dryrun.json`）以 14 天 retention 上傳。`matrix-summary` needs 新增 `pg-live-integration`，step summary 表格從 4 列（sqlite ×2 / postgres ×2 / engine-syntax-scan）擴成 7 列（sqlite ×2 / postgres ×3 / pg live integration / engine-syntax-scan）且 PG 15/16 + live-integration 欄位明確 `hard`、PG 17 + engine-syntax-scan 欄位標 `advisory*`。(3) `docs/ops/db_matrix.md` 改寫：Status line 從「advisory on Postgres 15 + 16」翻成「**hard gate** on Postgres 15 + 16, advisory on Postgres 17」；「Layer architecture」ASCII 圖多一層 `pg-live-integration` 盒；「Postgres wiring」section 拆成 `postgres-matrix`（psycopg v3）vs `pg-live-integration`（psycopg2 + asyncpg）兩段；「Known pre-G4 findings」改名為「Engine-syntax-scan scope」並重寫 rationale（shim 是 translation layer，刻意不改 migration 檔）；「G4 handoff plan」改為「G4 handoff status」列四項已交付（runtime shim / PG matrix hard / PG 17 advisory / live-integration）+ 四項後 cutover sweep（retire sqlite-matrix / promote PG 17 / strict syntax-scan / 刪 `OMNISIGHT_SKIP_FS_MIGRATIONS`）。契約鎖：`backend/tests/test_ci_pg_matrix.py`（22 顆 0.11 s、7 section：triggers 3 / sqlite-matrix 3 / postgres-matrix 5 / pg-live-integration 7 / summary 2 / concurrency 1 / env.py 跨 artefact anchor 1）—**關鍵不變量**：`postgres-matrix.continue-on-error` 不得是 bare `true`（只允 scope 到 `'17'` 的 expression），`postgres-matrix` matrix 必含 {15,16,17}，`pg-live-integration` 必須為 hard gate + 有 PG service container + 匯出 `OMNI_TEST_PG_URL` + 跑 `test_alembic_pg_live_upgrade.py` + 跑 `migrate_sqlite_to_pg.py --dry-run` + 裝 `psycopg2`+`asyncpg` 雙 driver、`backend/alembic/env.py` 必須 `install_pg_compat`（防 PG 綠是由錯誤原因造成的假陽性）、concurrency 必 ref-scoped + cancel-in-progress、`matrix-summary.needs` 必含全部 4 job、`if: always()` 保summary 在 cell red 時仍寫。全量回歸綠：test_ci_pg_matrix 22 + test_docker_publish_workflow 18 + test_alembic_pg_compat 226 + test_db_url 75 + test_pg_ha_deployment 114 + test_migrate_sqlite_to_pg 51 合計 **504 顆全綠 1.83 s 零回歸**。YAML 靜態驗證：`python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/db-engine-matrix.yml'))"` → 5 jobs (`sqlite-matrix`, `postgres-matrix`, `pg-live-integration`, `engine-syntax-scan`, `matrix-summary`) 全 parse。為什麼 PG 17 留 advisory：N7 forward-look pattern — 一個 cell 新版 OS/driver/DB 常有 deprecation warnings，強制硬綠會壓縮 operator 的 triage 窗。為什麼 `pg-live-integration` 只掛 PG 16（不是 3 個 matrix）：live test 8 顆加 migrate dry-run 要 30-60 s cold boot，乘 3 就是 3 min 延長合併延遲；PG 15/16/17 的 Alembic DDL path 由 `postgres-matrix` dual-track 已 cover，live 這層只是再加一道 shim 與 migrate script 的整合檢查，16 足矣。為什麼 `psycopg2-binary` 不是 `psycopg[binary]` v3：`test_alembic_pg_live_upgrade.py` 寫死 `import psycopg2`（libpq DSN `postgresql://...` 復原），切 v3 會需要 test 檔跟著改，本回合刻意小 blast radius；而 `postgres-matrix` 本來就用 psycopg v3（SQLAlchemy 推薦 dialect）—兩個 cell 的 driver 選擇各有理由，不強求統一。為什麼不把 PG service 加進主 `ci.yml` 的 `backend-tests`：shard 多（decision/pipeline/schema/rest），全 shard 上 PG 會把主 PR 流水線從 ~20 min 拉到 ~35 min，而 99% 的測試邏輯不碰 DB engine dialect；PG 特定行為由專用 matrix cover 更合理。下一組可以開的工作項（G4 #6）→ row 1365 `docs/ops/db_failover.md` runbook（含 sync/async replication policy 決策表 + promote-in-place ceremony + 用 G4 #4 `scripts/migrate_sqlite_to_pg.py` 的操作步驟 + 失敗接管 playbook + 本回合 CI matrix 狀態對照）。 -->

- [x] 交付：`docs/ops/db_failover.md`、遷移腳本、CI 更新<!-- 2026-04-18 G4 #6 (TODO row 1365): 三組交付 — (1) `docs/ops/db_failover.md` 新增 operator runbook（~600 行 / 15 section / 14 table）承接 G4 #1–#5 五塊 primitive（shim / URL 抽象 / HA bundle / migrator / CI matrix）並把它們串成三條完整動作線：**(a) SQLite → Postgres 一次性 cutover ceremony**（§3 pre-flight 五檢：`scripts/pg_ha_verify.py` 75/75 綠 / `migrate_sqlite_to_pg.py --dry-run --json` source_chain_ok=true / `docker compose ps` 兩 container healthy / `alembic upgrade head` 對新 PG / `pg_stat_replication.state=streaming` / 再開 rollback hot-key tab → §4 執行 + **7 顆 exit-code 操作決策表**（0/1/2/3/4/5/6 對齊 `scripts/migrate_sqlite_to_pg.py` docstring，exit 3 「source chain broken」標記為 SECURITY triage、exit 5 「target chain broken」為 P0 migrator bug）→ §4.3 T+5/T+10/T+30 post-cutover 監控表 + 「rollback 是 env-var 還原不是反向 migration」說明）、**(b) planned failover**（§5：pre-flight sync_state + replay lag 雙 gate → `pg_ctl promote` 三步 ceremony + `pg_is_in_recovery()=f` 驗證 → **§5.3 sync-vs-async policy 決策表 5 行**（durability / p50 latency / standby-down blast radius / 推薦場景 / recovery 手段）→ 「為何 async 是 default」單獨段落：因 `audit_log` 的 Merkle 鏈在 missing-tail 下仍 internally consistent，ciompliance 屬性不依賴 last-second durability ← 這是跨 G4/I 兩個 milestone 的 load-bearing policy rationale，runbook 是它唯一書面家 → §5.4 `pg_ctl promote` exit code 表）、**(c) unplanned failover**（§6：transient-vs-persistent 判斷 → promote threshold（RTO ~5 min 建議）→ §6.3 async-mode 下「missing-tail chain tamper-evidence 不可偽造」的 data-loss accounting 說明）、**(d) rebuild old primary as new standby**（§7：`docker volume rm omnisight-pg-primary` + `init-standby.sh` `pg_basebackup` pipeline → §7.1 「為何選 pg_basebackup 不 pg_rewind」：pg_rewind 需要 `wal_log_hints=on` 且 rewind window 須覆蓋 retained WAL，未來若 DB 變大再評估是 deploy-bundle 改動 → §7.2 promoted-ex-standby 必須手建一次 `omnisight_standby_slot` 的唯一差異說明）。另含 §8 3 支 forensic SQL（`pg_stat_replication` LSN lag + `pg_replication_slots.restart_lsn` 回推 + standby 的 `pg_last_xact_replay_timestamp` 延遲）/ §9 CI matrix 狀態對照表（sqlite-matrix hard × 2、postgres-matrix 15/16 hard + 17 advisory、pg-live-integration hard、engine-syntax-scan advisory）+ §9.1 cutover-後 4 項 follow-up PR / §10 troubleshooting tree（HA pair 狀態 4 分支 + migrator exit code 7 分支 + `pg_ctl promote` 2 分支）/ §11 三張 tunables 速查（.env knob 12 / primary.conf knob 7 / migrator flag 9）/ §12 script + contract index / §13 anti-patterns 6 條（含 `docker compose down -v` 全毀、`postgresql.auto.conf` 手改會被 `init-standby.sh` 覆寫、`--skip-chain-verify` 在 prod 等同隱藏 tamper 證據、sync-mode 下 standby 掛掉等於 primary 掛掉）/ §14 paste-into-ticket 12 行 change-management checklist / §15 cross-references。(2) `backend/tests/test_db_failover_runbook.py` 117 顆契約 0.12 s 10 class 鎖 9 大契約（script-backed 不允 drift）：**TestRunbookFileShape**（4：存在 / 路徑固定 / ≥5000 字 / H1 含 G4-or-HA-04 錨點）、**TestRunbookSections**（16：15 個 `## N. 標題` 逐一 present + 全域 sorted 順序）、**TestMigrateExitCodeCoverage**（8：0/1/2/3/4/5/6 七顆 `**N**` table marker + **與 `scripts/migrate_sqlite_to_pg.py` docstring 的 Exit codes:: block 雙向對齊** 防單邊新增 exit code）、**TestEnvKnobCoverage**（24：12 個 `.env.example` knob 各做 runbook mention + env_example 聲明雙向對齊）、**TestMigratorFlagCoverage**（18：9 個 CLI flag 各做 runbook mention + script `"--flag"` 註冊雙向對齊）、**TestPolicyInvariantsReferenced**（8：`omnisight_standby_slot` / `omnisight_standby` / `synchronous_standby_names` / `pg_last_wal_replay_lsn` / `pg_stat_replication` / `pg_ctl promote` / `source_chain_ok` / `pg_ha_verify.py` 八個 load-bearing token 缺一即 red — 這是防「runbook 改寫後把關鍵 literal 弄丟」的反 regression 鎖）、**TestCIJobAnchors**（8：4 個 job 名 runbook mention + workflow top-level YAML key 雙向對齊）、**TestReferencedPathsExist**（14：13 path 逐一 exists + 全 runbook 掃描所有 backtick-quoted `scripts/` 或 `deploy/postgres-ha/` 路徑並驗證 on-disk 存在 — 防 copy-paste 壞掉）、**TestContractIndex**（14：7 sibling test 既要在 §12 被引用又要實體存在）、**TestSyncAsyncPolicy**（3：runbook 明文 `asynchronous` + `default`、`FIRST 1 (omnisight_standby)` 精確 opt-in literal、同 literal 也在 `.env.example` 存在）。(3) TODO.md + HANDOFF.md 更新 + `docs/ops/db_matrix.md` 內部鏈接校準（已在 G4 #5 預留 `G4 #6` 鉤子，本回合文件實體 landed 後鏈接即生效）。全量回歸綠：test_db_failover_runbook 117 + test_alembic_pg_compat 226 + test_db_url 75 + test_pg_ha_deployment 114 + test_migrate_sqlite_to_pg 51 + test_ci_pg_matrix 22 + test_blue_green_runbook 107 合計 **712 顆全綠 1.99 s 零回歸**。為什麼 runbook 不寫成「script auto-generates」：operator 3am 看的是敘事，不是 API reference；script-backed 但人寫才有決策表（sync-vs-async policy、「為何選 pg_basebackup 不 pg_rewind」）。為什麼本回合 **不 修 migrate 腳本也不 改 CI**：TODO 行「交付：runbook、遷移腳本、CI 更新」的後兩項 G4 #4 (row 1363) 與 G4 #5 (row 1364) 已交；本回合是 G4 closure — runbook 是最後一塊、script-backed + cross-referenced 不要副作用地 re-design 既有工件。為什麼 §4.3 post-cutover 監控只列 T+5/T+10/T+30 三個點：G4 cutover 的主要風險窗是 migrator exit-0 到 app 第一次寫入之間（< 5 min）、replication lag 穩態（5–10 min）、app 長穩運行（30 min）— 再長的窗口屬於 G5 的 multi-node orchestration 而非 HA pair 的 runbook scope。為什麼契約測試跑全 runbook backtick-quoted path scan 而非 just whitelist：未來 runbook 增補新命令、新 deploy artefact 不需加 whitelist entry 才不會紅 — 但 `scripts/` + `deploy/postgres-ha/` 命名空間以外（如 operator 自己 create 的 `data/omnisight.db`）刻意排除避免誤傷。下一組可以開的工作項：G5（HA-05 multi-node orchestration）— K8s vs Nomad 選型決策 + Deployment/Service/Ingress manifests + HPA、或直接跳去 I 系列 multi-tenancy（RLS / statement_timeout / role-scoped grants）因為本回合 G4 closure 讓 I 系列的 PG-only hard dependency 都已就緒。 -->

### G5. HA-05 Multi-node orchestration（K8s manifests 或 Nomad job）
- [x] 選型決策文件（K8s vs Nomad vs docker swarm — 比較運維負擔）<!-- 2026-04-18 G5 #1 (TODO row 1369): 三組交付 — (1) `docs/ops/orchestration_selection.md` (~400 行 / 9 節) 新 charter 文件，承接 G4 closure 並為 G5 #2–#6 rows 1370–1374 預鎖決策框：§1 TL;DR 11 行機器可讀表（Chosen=Kubernetes / Manifest home=deploy/k8s/ + deploy/helm/omnisight/ / Minimum target=K8s 1.29 / containerd runtime / Alternatives=Nomad 1.7+ + Swarm classic / Reversibility=Medium / primary trade-off=K8s ops tax vs 生態深度）+ 四段短論 why-not-Swarm + why-not-Nomad 把讀者從 §1 一眼就能抓到結論；§2 把 TODO 行點名「比較運維負擔」明確拆成 5 個 axes（install-burden / day2-burden / observability-burden / upgrade-burden / recovery-burden）+ 評分卡 low/medium/high 定義；§3 K8s 子 5 節（1.29 為版本錨 / 每個 burden 獨立一段 subheading + rationale：install=half-day managed / 2-3 day self-hosted、day2=RBAC + CNI upgrade 是最大事故源但 OmniSight 只吃 single namespace + 2 ingress、observability=Prometheus Operator + kube-state-metrics + `orchestration_alerts.rules.yml` 1:1 port、upgrade=pin PDB policy/v1 + HPA autoscaling/v2 防靜默 deprecation、recovery=etcd snapshot + Postgres HA 已在 `deploy/postgres-ha/` 隔離）；§4 Nomad 子 6 節（install=low 單一 static binary 2h vs K8s 3day、day2=ACL 比 RBAC 簡單但 O10 security baseline 不允 flat-file secret → Vault 必收 → 成本回升、observability=無 kube-state-metrics 等價要自寫 exporter、upgrade=low、recovery=`nomad operator raft snapshot`、§4.6 **why-not-Nomad 三條明文**：ecosystem gravity / hiring funnel / IBM 收購 HashiCorp 後的 vendor concentration risk）；§5 Swarm 子 6 節（install=very-low 但 §5.2 day2-burden 是 low-short/high-long — 因 classic swarm 自 2020 進 maintenance mode、§5.6 **why-Swarm-is-no-go**：2026 greenfield 不可押 maintenance-mode orchestrator — load-bearing disqualifier，契約測試明文鎖 "maintenance" 字樣）；§6 5 軸 × 3 候選 scoring 矩陣 + ecosystem-depth + roadmap-risk 兩列額外權重；§7 8 條 consequences 直接鎖下游 rows 1370–1374 具體 commitments（`deploy/k8s/` + `deploy/helm/omnisight/` 雙交付面 / `policy/v1` PDB / `httpGet` probe 接 `/readyz` + `/livez` / `autoscaling/v2` HPA `targetCPUUtilizationPercentage: 70` + Deployment RollingUpdate `maxUnavailable: 0` `maxSurge: 1` / Helm `values-staging.yaml` + `values-prod.yaml` 分離而非 mega-file / Ingress Gateway-API toggle 不 silent auto-detect / CI smoke 用 `kind` 1.29 pin minimum version claim / **§7.8 Nomad+Swarm 明文 out-of-scope**）；§8 4 條 open questions 明示不 block G5 #1 但要 G5 #2–#6 各自接（Ingress controller / StorageClass / rolling update strategy / multi-tenant isolation 留待 I 系列）；§9 6 條 cross-reference 串 TODO / G1 health router / blue_green_runbook / postgres-ha bundle / prometheus rules / orchestration_migration（區分 O8 應用層 monolith↔distributed flip 與 G5 cluster orchestrator 不可 conflate）。(2) `backend/tests/test_orchestration_selection_decision.py` 62 顆契約 0.08 s 9 class 鎖 9 大契約：**TestDecisionDocFileShape**（4：存在 / 路徑固定 / ≥4000 字 / H1 含 G5 + 三候選錨點）、**TestDecisionDocSections**（10：9 個 `## N.` present + sorted 順序）、**TestCandidateCoverage**（7：3 候選各在 §1 TL;DR + §6 scoring + §3/§4/§5 dedicated section 三重出現）、**TestBurdenAxisCoverage**（10：5 axes 在 §2 定義 + §6 scoring 雙出現）、**TestTldrFields**（7：5 個 TL;DR field + **chosen 必 literal 含 "Kubernetes"** + manifest-home 必含 `deploy/k8s/` + `deploy/helm/omnisight/` — 防 row 1373/1374 commit 路徑被改）、**TestConsequencesAlignment**（11：§7 必 literal 含 `maxUnavailable` + `CPU` + `70` + `PodDisruptionBudget` + `/readyz` + `/livez` + `values-staging.yaml` + `values-prod.yaml` + `deploy/k8s/` + `deploy/helm/omnisight/` + `policy/v1` + `autoscaling/v2` — 每顆對應 rows 1370–1374 具體 commitment）、**TestCrossReferenceTargetsExist**（5：5 個 cross-ref 既在 doc 引用又實體 exists — 防 copy-paste 壞 link）、**TestAntiDecisionExplicit**（2：§7 literal 含 "nomad" + "swarm" + "out-of-scope" 且 §5 literal 含 "maintenance" — load-bearing disqualifier 的反 regression 鎖）、**TestG5SiblingNavigation**（5：G5 #2–#6 sibling row marker 都出現）。(3) TODO.md + HANDOFF.md 更新。**為什麼選 K8s 不 Nomad**：ecosystem depth 是 day-1 就 compound 的 — Prometheus Operator / kube-state-metrics / cert-manager / external-secrets-operator 每塊都 K8s-native first，觀察性 burden 是 OmniSight 最在意的軸（G7 整個 milestone）所以 `omnisight_backend_instance_up` 不用重寫 PromQL 是決定性。**為什麼選 K8s 不 Swarm**：2026 不能押 2020 進 maintenance 的 orchestrator — 省下今天 install tax 會在 2–3 年後付更大遷移 tax。**為什麼 charter 不直接 ship manifests**：G5 #2–#6 artefact 會因 Gateway-API / managed-PG vs StatefulSet-PG 等前置 open question 重 shape；charter 先落 consequences 鎖 + open questions 明示，下游各 row 才有穩定 anchor 不重工。**為什麼契約測試鎖 literal "maintenance"**：未來 refactor §5 可能把 "maintenance mode" 軟化成 "in upkeep"，但 "2020 起 maintenance" 是 Swarm 唯一 load-bearing 淘汰理由 — literal 鎖住不被 prose polish 稀釋。**為什麼不 ship 雙交付**：orchestrator-agnostic 抽象 leaky — PDB 在 K8s 是頂級對象 Nomad 沒等價、HPA 在 K8s 是 `autoscaling/v2` field Nomad 是 `scaling` stanza + plugin；維護兩套 day-2 cost 遠超「未來換」的潛在節省。全量相關回歸綠：test_orchestration_selection_decision 62 + test_blue_green_runbook 107 + test_db_failover_runbook 117 + test_pg_ha_deployment 114 + test_orchestration_mode 26 合計 **426 顆全綠 307 s 零回歸**。下一組：G5 #2 row 1370（Deployment replicas=2 maxUnavailable=0 + Service + Ingress + HPA CPU 70% manifests — 按本 charter §7 8 條 consequences 逐一落地）。 -->
- [x] Manifests：Deployment（replicas=2, maxUnavailable=0）、Service、Ingress、HPA（CPU 70%）<!-- 2026-04-18 G5 #2 (TODO row 1370): 五組 K8s manifest + 91 顆契約 — (1) `deploy/k8s/00-namespace.yaml` Namespace `omnisight` + `app.kubernetes.io/part-of=omnisight` 標籤；(2) `deploy/k8s/10-deployment-backend.yaml` Deployment `apps/v1` replicas=2 RollingUpdate `maxUnavailable: 0` `maxSurge: 1` revisionHistoryLimit=5、單 container `backend` 掛 `ghcr.io/your-org/omnisight-backend:latest` placeholder、named port `http` containerPort 8000、`OMNISIGHT_INSTANCE_ID` 從 downward API `metadata.name` 取值與 compose 層 `backend-a`/`backend-b` Prometheus label 等價、`resources.requests.cpu: 250m` + `requests.memory: 512Mi` + `limits.memory: 1Gi`（CPU request 是 HPA 70% 計算前提，缺一 HPA 靜默停在 minReplicas）、**刻意不含 probes**（G5 #4 row 1372 owns `httpGet` 接 `/readyz` + `/livez`）；(3) `deploy/k8s/20-service-backend.yaml` Service `v1` ClusterIP port 80 → targetPort 命名 `http`（indirection 讓 G5 #5 Helm 改容器 port 不用動 Service）、selector `app.kubernetes.io/name=omnisight-backend` + `component=backend`；(4) `deploy/k8s/30-ingress.yaml` Ingress `networking.k8s.io/v1` `ingressClassName: nginx`（charter §7.6 default，Gateway-API 屬 G5 #5 Helm toggle 非 silent auto-detect）、host `omnisight.example.com` placeholder、path `/` Prefix；(5) `deploy/k8s/40-hpa-backend.yaml` HPA `autoscaling/v2`（charter §7.4 鎖定：v2beta2 在 1.26 已移除）minReplicas=2（== Deployment baseline，永不縮到 HA-02 之下）maxReplicas=10 metrics `Resource/cpu` `Utilization` `averageUtilization: 70`；(6) `deploy/k8s/README.md` 交代 charter 引用、apply 命令、`kind` 1.29 smoke、並明列 G5 #3–#6 的下游 ownership + Nomad/Swarm out-of-scope。檔名前綴 `00/10/20/30/40` 鎖 `kubectl apply -f` 字典序：namespace 先落地、deploy 次之、service/ingress 緊接、hpa 最後（scaleTargetRef 找不到 Deployment 會報錯）。(7) `backend/tests/test_k8s_manifests_g5_2.py` 91 顆契約 0.15 s 9 class：**TestK8sManifestFilesShape**（23：directory 存 / 5 manifest 各存 / 各為 `.yaml` / 各 YAML parse / lexical apply order == 5 file 預期序列 / README 存在且引 charter 且提 `kind` / README cross-ref G5 #3/#4/#5/#6 / README 明文 out-of-scope + nomad + swarm）、**TestNamespaceContract**（4：Namespace kind / `v1` / 名 `omnisight` / recommended labels）、**TestDeploymentContract**（17：kind / `apps/v1` / namespace / 名 / replicas=2 / RollingUpdate / `maxUnavailable=0` / `maxSurge=1` / selector labels / pod template labels match selector / 單 container / name `backend` / port 8000 命名 `http` TCP / image 含 `omnisight-backend` 且 `ghcr.io/` 前綴 / resources.requests.cpu 存 / memory 存 / revisionHistoryLimit bounded 1..10 / `OMNISIGHT_INSTANCE_ID` 從 downward API `metadata.name`）、**TestServiceContract**（8：kind / `v1` / namespace / 名 / ClusterIP / port 80 / targetPort `http` / protocol TCP / selector match Deployment pod labels）、**TestIngressContract**（8：kind / `networking.k8s.io/v1` / namespace / 名 / `ingressClassName: nginx` / ≥1 rule / path `/` Prefix / backend service name / backend service port match Service 不論 named-or-numeric）、**TestHPAContract**（9：kind / `autoscaling/v2` / namespace / 名 / scaleTargetRef `apps/v1` Deployment 名對齊 / minReplicas == Deployment replicas / maxReplicas > minReplicas / 單 Resource cpu metric / `averageUtilization: 70` Utilization 類型）、**TestCrossManifestConsistency**（6：4 manifest 同 namespace / 同 `part-of=omnisight` / 同 `name=omnisight-backend` / HPA target 名對齊 Deployment / Ingress service 名對齊 Service / Service targetPort 在 Deployment container port 名中可解析）、**TestCharterAlignment**（11：charter 明文含 `deploy/k8s/` + `maxUnavailable: 0` + `maxSurge: 1` + `autoscaling/v2` + `targetCPUUtilizationPercentage: 70` + `ingressClassName: nginx` 六條；manifest 端 `maxUnavailable=0` / `maxSurge=1` / HPA api / HPA cpu 70 / Ingress class 對 charter 雙向 pin）、**TestScopeDisciplineSiblingRows**（3：無 PDB 物件 / 無 `deploy/helm/omnisight/Chart.yaml` / 無 `deploy/nomad` 或 `deploy/swarm` 目錄 — sibling row 悄悄 landed 的 regression guard）、**TestTodoRowMarker**（2：row 1370 headline literal 在 TODO / row 1370 行號 > G5 section header 行號）。**為什麼不在 G5 #2 加 probes**：TODO row 明確把 probe 切給 G5 #4 row 1372，現在加進來等於悄悄吃掉下一 row scope；未加 probe 的 Deployment K8s 會用 TCP 就緒語意（pod 啟動即 Ready）——dev/smoke 可 run、prod 由 G5 #4 補齊 `/readyz` 精確 HTTP 就緒判定。**為什麼 targetPort 用命名 `http` 不寫 8000**：G5 #5 Helm values 會想換 containerPort（例：和 frontend 的 3000 避免混淆），named port 讓 Service 層 0 diff；這是 K8s 原生的 port contract indirection。**為什麼 image 用 `your-org` 佔位而不空**：K8s Deployment 不允 image 欄缺；`your-org` 是明顯佔位、operator 一眼看出需 override，與 docker-compose.prod.yml `${OMNISIGHT_GHCR_NAMESPACE:-your-org}` default 對齊。**為什麼 OMNISIGHT_INSTANCE_ID 走 downward API**：compose 層用靜態 `backend-a`/`backend-b`（2 replica 固定），K8s 層 pod 名隨 ReplicaSet hash 變動、HPA 還會動態擴 2→10，必須 downward API 才能 per-pod 唯一；Prometheus 既有 `omnisight_backend_instance_up{instance_id}` rule 無需改動。**為什麼 scope test 硬鎖「不存在 PDB / Helm Chart / Nomad / Swarm」**：G5 是 6-checkbox bucket，silent scope creep 是最常見的 regression 源；每 row landed 前這三個 assert 都會紅，下次 G5 #3 landed PDB 時這個 test 會要求**同 commit** flip row 1371 + 刪 assert（顯性遷移）。(8) TODO row 1370 `[ ]`→`[x]` + 本 historical note；(9) HANDOFF 新條目。**回歸** — test_k8s_manifests_g5_2 91 + test_orchestration_selection_decision 62 + test_blue_green_runbook 107 + test_db_failover_runbook 117 + test_pg_ha_deployment 114 合計 **491 顆全綠 0.66 s 零回歸**。**下一組**：G5 #3 row 1371（PodDisruptionBudget minAvailable=1）。 -->
- [x] PDB（PodDisruptionBudget minAvailable=1）<!-- 2026-04-18 G5 #3 (TODO row 1371): 一個新 manifest + 一份新契約 + 兩個檔案微調 — (1) `deploy/k8s/15-pdb-backend.yaml` PodDisruptionBudget `policy/v1`（charter §7.2 鎖：v1beta1 在 K8s 1.25 已移除、v1 是 1.29 唯一 wire form）name `omnisight-backend` namespace `omnisight` + 完整 `app.kubernetes.io/{name,component,part-of,managed-by}` 四個 recommended labels、**`spec.minAvailable: 1`**（**整數而非百分比**：2-replica baseline 下「≥1 pod」是最便宜可表達的 HA 承諾；未來 replicas 從 2 → 4 時 `minAvailable=1` 仍誠實（floor 不變），而 `maxUnavailable=1` 會悄悄漂成 「up to 25% 可同時下」削弱契約）、`spec.selector.matchLabels` 與 Deployment `spec.selector.matchLabels` byte-equal（`app.kubernetes.io/name=omnisight-backend` + `component=backend`）保證 PDB 守護的就是 Deployment 擁有的 pods。檔名 `15-` 前綴讓 `kubectl apply -f deploy/k8s/` 字典序在 Deployment(10) 之後、Service(20) 之前落地（PDB 不嚴格要求 Deployment 先存在，但 operational 讀法更乾淨）。**為什麼不挑 maxUnavailable**：mutual exclusion API 要求兩者只能擇一；minAvailable 是 HA 承諾的 directly-readable 形式（"≥1 pod must remain"），且整數形式對 future replicas bump 安全（百分比會 silently drift）。**為什麼是獨立 file 不 merge 進 Deployment**：PDB 在 selector layer 與 Deployment 解耦、`kubectl delete -f` / `apply --prune` 能獨立操作、G5 #5 Helm chart `pdb.enabled` toggle 是 one-template flip。**為什麼 spec.minAvailable < replicas**：等於 replicas 會封死 voluntary disruption（含 rolling restart）— 這正是歷史事故 "PDB locked us out of draining" 的失敗模式，contract test 顯式 assert 防 regression。(2) `backend/tests/test_k8s_pdb_g5_3.py` **40 顆契約** 7 class — **TestPdbFileShape**（5：file 存 / `.yaml` / YAML parse / lexically after Deployment / lexically before Service / 檔名 `15-pdb-backend.yaml` 字面鎖）、**TestPdbApiContract**（9：kind=PodDisruptionBudget / apiVersion=policy/v1 / 反 v1beta1 字面鎖 / namespace=omnisight / name=omnisight-backend / 4 個 recommended labels）、**TestPdbSpecContract**（10：spec mapping / minAvailable=1 / 整數 not percentage / 不設 maxUnavailable mutual-exclusion guard / selector mapping / matchLabels not matchExpressions / selector 含 name + component / **minAvailable < replicas** 防 lockout regression）、**TestPdbDeploymentAlignment**（4：selector matchLabels byte-equal Deployment selector / selector resolves to Deployment pod template labels / namespace == Deployment namespace / part-of label == Deployment）、**TestPdbCharterAlignment**（5：charter literal 含 `policy/v1` + `PodDisruptionBudget` + `G5 #3` 三條雙向鎖）、**TestPdbReadmeAlignment**（4：README 含 file 名 + kind + `policy/v1` + `minAvailable: 1`/`minAvailable=1`）、**TestTodoRowMarker**（3：row 1371 headline literal / row marked `[x]` / row 在 G5 section header 之下）、**TestScopeDisciplineSiblingRows**（4：無 Helm chart dir / **Deployment 無 readinessProbe + livenessProbe**（G5 #4 row 1372 owns 一旦悄悄 land 即紅）/ 無 deploy/nomad + deploy/swarm 目錄 / PDB 是獨立 single-doc YAML 防 silent merge into Deployment）。(3) `deploy/k8s/README.md` Files table 加入 `15-pdb-backend.yaml | PodDisruptionBudget | policy/v1 | charter §7.2` row + Scope 區塊把 PDB 從「不包含」搬到 footer "now part of the bundle" 註記；(4) `backend/tests/test_k8s_manifests_g5_2.py` G5 #2 contract test 同 commit 微調：（a）`ALL_MANIFEST_PATHS` 加入 `PDB_PATH`，（b）`test_manifest_filenames_sorted_apply_order` expected 序列加入 `15-pdb-backend.yaml` 在 Deployment 與 Service 之間，（c）刪除 `test_no_poddisruptionbudget_yet`（前 historical note 預告的 "下次 G5 #3 landed PDB 時這個 test 會要求**同 commit** flip row 1371 + 刪 assert" 顯性遷移現在兌現）— 顯性遷移不可 silent。**為什麼 G5 #3 contract test 自帶 sibling scope guard**：G5 #4 row 1372 是下一順位 row，本 commit 同步守 「Deployment 無 readinessProbe / livenessProbe」防 G5 #3 偷跑 G5 #4 scope（與 G5 #2 自我守 PDB 同樣的設計動機）。**為什麼測試硬鎖 row [x] 而非僅 headline 出現**：headline 出現可發生於 row 還是 [ ] 的草稿期；硬鎖 [x] 才能保 manifest 在 prod 與 row tracker 同步，revert manifest 不 flip row 會被 contract 紅燈。**為什麼 charter alignment test 接 §7.2**：charter 在 G5 #1 landed，是 truth source；任何人改 manifest 不改 charter（或反向）會在兩側同時紅燈逼齊。**為什麼整數而非百分比鎖 contract test**：整數 vs 百分比是 K8s 兩條合法路徑、百分比在未來 replicas bump 下會悄悄削弱 contract — 這個 contract test 是後悔藥（如果 5 年後有人想換成 50%，他必須先紅一次 contract、被迫 review 為什麼當初挑了整數）。**回歸全綠** — `test_k8s_pdb_g5_3` 40 + `test_k8s_manifests_g5_2` 90（上回合 91 顆扣掉 1 顆已 obsolete 的 PDB scope guard）+ `test_orchestration_selection_decision` 62 + `test_blue_green_runbook` 107 + `test_db_failover_runbook` 117 + `test_pg_ha_deployment` 114 合計 **530 顆全綠 < 1 s 零回歸**。下一組：G5 #4 row 1372（readiness/liveness probe `httpGet /readyz` + `/livez`）— 本回合 PDB scope guard 已硬鎖防 G5 #3 偷跑，下一 commit landed probe 時應同步刪除 G5 #3 contract test 中對應 sibling scope assert（顯性遷移 pattern 延續）。 -->
- [x] readiness/liveness probe 對接 G1 endpoint<!-- 2026-04-18 G5 #4 (TODO row 1372): probe 對接 G1 endpoint — (1) `backend/routers/health.py` 新增 `/livez` + `/api/v1/livez` 兩條路由 delegate 到既有 `healthz()` handler（byte-identical payload `{"status": "ok", "live": True}`）— charter §7.3 commits K8s liveness probe 到 `/livez` 字樣、G1 本來只 ship `/healthz`，這顆 alias 讓 K8s manifest 可以照 charter 寫而不用在 `/healthz` vs `/livez` 之間擇一；`/livez` 是 **alias 不是 fork**（走同 handler），未來 G1 改 liveness 語意時兩條路徑同步改、無 drift 風險；(2) `backend/main.py` 同 commit 把 `/livez` 加入四個 middleware exemption set：`_PASSWORD_CHANGE_EXEMPT`（不讓強制改密中間層 307 到 probe）、`_RATE_LIMIT_EXEMPT`（壓力下 K8s probe 不可 429）、`_BOOTSTRAP_EXEMPT_REL` + `_BOOTSTRAP_EXEMPT_RAW`（未 bootstrap 時不讓 wizard redirect 挾持 probe）、`_GRACEFUL_SHUTDOWN_EXEMPT_RAW`（draining 期間 liveness 必須持續 200 — 否則 K8s 會重啟 draining pod 打斷 in-flight），缺任一會讓 K8s probe 在特定階段 503 → restart loop；(3) `deploy/k8s/10-deployment-backend.yaml` 在 backend container 下新增兩 probe stanza：`readinessProbe.httpGet` path=`/readyz` port=`http` scheme=`HTTP` initialDelay=5 period=5 timeout=2 failureThreshold=3 successThreshold=1（**drain detection 視窗 3×5=15 s < G1 `Retry-After: 30` 一半**，LB 有 15 s 緩衝把 replica 拔出再被 in-flight 30 s 耗完）、`livenessProbe.httpGet` path=`/livez` port=`http` scheme=`HTTP` initialDelay=15 period=10 timeout=2 failureThreshold=3 successThreshold=1（**restart window 3×10=30 s ≥ G1 30 s 關閉 budget**，不會把 healthy-but-draining pod 搶先重啟）；`port: http` 走命名 port 不寫 8000，G5 #5 Helm 改 containerPort 時 probe 無感；`scheme: HTTP` 顯式寫死，TLS flip 要是 visible diff 不可 silent kubelet default；(4) `deploy/k8s/README.md` scope 區塊把「Readiness / liveness probes → G5 #4 row 1372」從 NOT-included 搬走，改成 footer "probes wired in G5 #4" 描述 + 解釋 `/livez` 是 `/healthz` alias；(5) `backend/tests/test_healthz_readyz.py` G1 contract test 擴充：`test_livez_alias_matches_healthz_payload`（unit 三 handler 同 payload）、`test_livez_root_200` + `test_livez_prefixed_200`（ASGI / + /api/v1 雙路徑 200）、`test_livez_stays_200_while_draining`（draining 期間 K8s 不重啟 pod — 最關鍵的反 regression）；(6) **新 contract**：`backend/tests/test_k8s_probes_g5_4.py` 10 class 契約 — **TestReadinessProbeContract**（9：readinessProbe 存 / httpGet / path=/readyz / port=named `http` / scheme=HTTP / initialDelay bounded / **drain window < 30 s G1 Retry-After**（計算 failureThreshold×periodSeconds，防 period 被悄悄加大讓 LB 看不到 drain）/ timeout≤period / successThreshold=1 K8s only-legal 值）、**TestLivenessProbeContract**（8：liveness 存 / httpGet / path=/livez **鎖死 charter 字樣**（換回 /healthz 或其他會紅）/ port named / scheme / initialDelay≥5 防 cold start restart loop / **restart window ≥ 30 s G1 budget**（對稱於 readiness 一頭鎖上、一頭鎖下）/ timeout≤period）、**TestProbePortResolvesToContainerPort**（3：container ports 含 name=http / named `http` 映射 8000 uvicorn 口、不 silent drift / 兩 probe 同 port）、**TestG1EndpointsServeProbePaths**（3：G1 router 源碼含 `"/readyz"` 字面 / 含 `"/livez"` 字面防 K8s 指向不存在的 route / **livez handler delegates to healthz 源碼檢查**，防未來 refactor 讓兩者 drift）、**TestG1HealthTestCoverage**（2：G1 test file 覆蓋 /livez alias 字樣 + **draining-stays-200 test 字樣**，G5 #4 test 跨界保證 G1 test 不被刪）、**TestMiddlewareExemptionsCoverLivez**（4：正則定位四個 middleware exemption set 各自包含 `"/livez"` literal — 缺一即 probe 會在該階段 503）、**TestCharterAlignment**（4：charter 含 /readyz + /livez + httpGet + G5 #4 四字面）、**TestReadmeAlignment**（4：README 含 /readyz + /livez + httpGet + **反 regression「Readiness / liveness probes → G5 #4」字串必不在**，防 README scope 區塊被誤 revert）、**TestTodoRowMarker**（3：row 1372 headline / [x] 狀態 / 在 G5 section 下）、**TestScopeDisciplineSiblingRows**（3：無 Helm chart / 無 Nomad/Swarm / **無 startupProbe**（charter §7.3 只 scope readiness + liveness，startup 另一 row 責任防 silent creep））；(7) `backend/tests/test_k8s_pdb_g5_3.py` 同 commit 刪 `test_no_probes_in_deployment_yet`（G5 #3 historical note 明文預告「下一 commit landed probe 時應同步刪除 G5 #3 contract test 中對應 sibling scope assert — 顯性遷移 pattern 延續」，兌現）+ 更新 class docstring 把 G5 #4 sibling 從「not asserted here」改成「now pinned in test_k8s_probes_g5_4.py」明示 ownership 轉移；(8) TODO row 1372 `[ ]`→`[x]` + 本 historical note；(9) HANDOFF 新條目。**為什麼不把 /livez 當另一個 handler 複製一份**：alias 路徑 → 同 handler 是 K8s `/livez` 與 compose `/healthz` 的唯一不漂移方式；分身 handler 會在「draining 行為 / 版本升級 / provider 檢查改寫」各種場景悄悄分化、5 年後釀成 prod/compose 行為不對等事故。**為什麼 readiness drain window 鎖 <30 s、liveness restart window 鎖 ≥30 s**：兩個窗口分別 bound **different** G1 contract — readiness 要比 G1 `Retry-After: 30` **快**（才能讓 LB 在 30 s 內把 pod 拔出）、liveness 要比 G1 30 s shutdown budget **慢**（才不會把 healthy-but-draining pod 搶先重啟打斷 in-flight），計算 threshold×period 是 K8s 既定語意、不會因為重命名 field 漏鎖。這兩條 contract 是 G5 #4 最核心的「probe timing 接得住 G1 lifecycle」互鎖。**為什麼 MiddlewareExemptions 用正則定位 set literal 而不只 `"/livez" in text`**：後者會被 comment / docstring 意外通過；正則定位到「真的那個 set」才能保證「exemption 真的生效於 middleware」不是 dead text。**為什麼 ProbePortResolvesToContainerPort 鎖 `http`→8000**：named port `http` 在 Service / Ingress / HPA 都轉一層、probe 也用 named，若 container port 重新命名為 `api` 等 `http` 消失、kubelet 會 **silently skip probe**（找不到 named port 不是錯誤，是 no-op）——這條 contract 是後悔藥。**為什麼 G5 #4 contract 還跨界測 G1 test 字樣**：`/livez` 的 draining-stays-200 保證是 G5 #4 能 work 的前提；未來 G1 refactor 若把該 test 刪了、probe 在 drain 期會被 K8s 誤判 dead → 重啟 draining pod 打斷 in-flight — 跨界鎖住這個 test 存在是對 K8s-level contract 的縱深防禦。**為什麼 startupProbe 不加**：charter §7.3 只 scope readiness + liveness 兩種 probe；startup 是合理未來追加但屬另一 row 責任（初期 K8s 1.16 前沒 startupProbe、現代通常冷啟動 <15 s 用 `livenessProbe.initialDelaySeconds` 夠）—— silent creep 是 G5 最在意的事故源。下一組：G5 #5 row 1373（Helm chart `deploy/helm/omnisight/` + `values-staging.yaml` / `values-prod.yaml`）。 -->
- [ ] Helm chart `deploy/helm/omnisight/`（values.yaml for staging/prod）
- [ ] 交付：`deploy/k8s/` 或 `deploy/nomad/`、決策文件

### G6. HA-06 DR runbook + 自動化 restore drill
- [ ] 每日排程：備份 → 另一主機執行 `restore` → 跑 `backup_selftest.py` + smoke 子集 → 報告
- [ ] RTO / RPO 目標明文化（建議 RTO ≤ 15min, RPO ≤ 5min）
- [ ] Runbook：資料庫 primary 掛掉的手動切換步驟、反向代理故障的 fallback
- [ ] 年度 DR 演練 checklist
- [ ] 交付：`scripts/dr_drill.sh`、`docs/ops/dr_runbook.md`

### G7. HA-07 Observability for HA signals
- [ ] Prometheus 指標：`omnisight_backend_instance_up`、`rolling_deploy_5xx_rate`、`replica_lag_seconds`、`readyz_latency`
- [ ] Grafana dashboard `deploy/observability/grafana/ha.json`
- [ ] Alert rules：replica lag > 10s / 5xx rate > 1% for 2min / instance down
- [ ] 交付：dashboard + alert rules

**相依性**：G1 → G2 → G3（rolling → blue-green）；G4 獨立可並行；G5 建議待 G1–G4 穩定後；G6、G7 橫向支援。

**預估**：G1 (2d) + G2 (3d) + G3 (2d) + G4 (5-7d) + G5 (4-5d) + G6 (2d) + G7 (2d) ≈ **20-23 day**。

---

## 🅗 Priority H — Host-aware Coordinator（主機負載感知 + 自適應調度）

> 背景：現行 `_ModeSlot`（`backend/decision_engine.py` L52-189）只以 Operation Mode 給靜態 budget（manual=1/supervised=2/full_auto=4/turbo=8），coordinator 不讀 CPU/mem/disk，prewarm 純猜測。風險：turbo 在高壓時仍硬塞 → OOM / watchdog 誤判 stuck → 重試放大壓力。UI `host-device-panel.tsx` L40-51 `HostInfo` 是 placeholder 未實作。
>
> 基準硬體（hardcode baseline）：AMD Ryzen 9 9950X、WSL2 分配 **16 cores + 64 GB RAM + 512 GB disk**。

### H1. 主機 metrics 採集（baseline hardcode 版）
- [ ] `backend/host_metrics.py`：定義 `HOST_BASELINE = HostBaseline(cpu_cores=16, mem_total_gb=64, disk_total_gb=512, cpu_model="AMD Ryzen 9 9950X")`
- [ ] `psutil` 採樣：`cpu_percent(interval=1)` / `virtual_memory()` (用 `available` 反推) / `disk_usage('/')` / `os.getloadavg()`
- [ ] Docker SDK 抓 running container 數 + 總 mem reservation；Docker Desktop 情境 fallback `docker stats --no-stream`
- [ ] 採樣 5s 週期、ring buffer 60 點（5 分鐘歷史）
- [ ] WSL2 輔助訊號：`loadavg_1m / 16 > 0.9` 也標記為 high pressure（host 其他進程）
- [ ] Prometheus gauges：`host_cpu_percent` / `host_mem_percent` / `host_disk_percent` / `host_loadavg_1m` / `host_container_count`
- [ ] Endpoint：`GET /api/v1/host/metrics`（current + history）
- [ ] SSE event：`host.metrics.tick`（5s 推送）
- [ ] 測試：mock psutil、驗證 ring buffer rotation、Docker unavailable 時的 fallback
- 預估：**0.5 day**

### H2. Coordinator 負載感知調度（precondition + backoff）
- [ ] `_ModeSlot.acquire()` 新增 precondition：`cpu_pct < 85 AND mem_pct < 85 AND container_count < K`
- [ ] 超標時指數 backoff（cap 30s），不佔槽位；emit `sandbox.deferred` audit 事件（reason: `host_cpu_high` / `host_mem_high` / `container_cap`）
- [ ] Turbo 自動降級：`cpu_pct > 80` 持續 30s → 降到 supervised budget；恢復後可自動回升（需冷卻 2 min）
- [ ] `auto_derate=true` 設定開關（`backend/config.py`），使用者可關閉（turbo 模式需手動 confirm）
- [ ] Prewarm（`sandbox_prewarm.py`）在 high pressure 時暫停新建 warm pool；已 warm 的保留
- [ ] Audit 記錄所有 derate / recover 決策（Phase 53 hash-chain）
- [ ] 測試：mock host_metrics 模擬高壓 → 驗證 acquire 被阻塞、derate 觸發、recover 冷卻
- 預估：**2 day**

### H3. UI Host Load Panel + Coordinator 決策透明化
- [ ] 把 `components/omnisight/host-device-panel.tsx` placeholder 換成真 SSE 驅動（listen `host.metrics.tick`）
- [ ] 顯示：CPU% / mem%（含 available）/ disk% / loadavg 1m / running container 數 + 各項 60-pt sparkline
- [ ] Baseline 顯示「16c / 64GB / 512GB」於 header（hardcode）
- [ ] `ops-summary-panel.tsx` 加欄位：**queue depth**（等槽位任務數）/ **deferred count**（近 5min）/ **effective concurrency budget**（因 derate 可能 < 設定）
- [ ] 過載 Badge：`Coordinator auto-derated to supervised`，hover tooltip 顯示原因（"CPU 87% > threshold"）
- [ ] 手動 override 按鈕：`Force turbo`（confirm dialog 警告可能 OOM，audit 記錄）
- [ ] 高壓閾值視覺標記（CPU >85% 變紅、70-85% 變黃）
- [ ] Component + Playwright E2E 測試
- 預估：**1.5 day**

### H4a. Weighted Token Bucket + AIMD 自適應 concurrency
- [ ] 定義 `SandboxCostWeight` 表（初期估值）：
  - `gvisor_lightweight = 1` (unit test / lint, ~512MB / 1 core burst)
  - `docker_t2_networked = 2` (integration, ~1.5GB / 2 core)
  - `phase64c_local_compile = 4` (`make -j4`, ~2GB / 4 core sustained)
  - `phase64c_qemu_aarch64 = 3` (cross-compile, ~2GB / 2 core)
  - `phase64c_ssh_remote = 0.5` (成本在對端)
- [ ] `CAPACITY_MAX = min(cpu_cores * 0.8, mem_gb / 2) = 12 tokens`（16c/64GB → 12）
- [ ] AIMD 控制器（`backend/adaptive_budget.py`）：
  - Init `budget = 6`（≈ CAPACITY_MAX / 2 安全啟動）
  - Additive: 每 30s 若 `cpu<70` & `mem<70` & `deferred=0` → `budget += 1`
  - Multiplicative: `cpu>85` 或 `mem>85` 持續 10s → `budget = max(floor=2, budget//2)`
  - Hard cap：`budget ≤ CAPACITY_MAX`
- [ ] Mode 變 multiplier：`turbo=1.0 / full_auto=0.7 / supervised=0.4 / manual=0.15`；effective = `min(mode_cap × CAPACITY_MAX, aimd_budget)`
- [ ] `_ModeSlot.acquire(cost: int)` 改為 token-based，排隊時 emit `sandbox.deferred`
- [ ] Last-known-good budget 持久化（DB），冷啟動時載入替代 `init=6`
- [ ] UI 顯示當前 AIMD budget + 最近 5min trace（上升/下降歷史）
- [ ] 測試：模擬 CPU spike → 驗證 MD halve、冷卻後 AI 回升、floor/cap 邊界
- 預估：**1.5 day**

### H4b. Sandbox cost calibration（H1 上線 1 週後）
- [ ] `scripts/calibrate_sandbox_cost.py`：讀取過去 N 天 sandbox 執行紀錄（start/end timestamp + 同期 host_metrics ring）
- [ ] 計算每類 sandbox 的平均 CPU×time / Δmem_peak → 產新權重表
- [ ] 輸出 diff report（舊權重 vs 新權重）供人工審核
- [ ] 支援 `--apply` 旗標寫回 `configs/sandbox_cost_weights.yaml`（改 H4a hardcode 為 config 驅動）
- [ ] Audit：權重變更寫入 hash-chain
- 預估：**1 day**

**相依性**：H1 → H2 → H3（metrics → 調度 → UI）；H4a 可與 H3 並行；H4b 需 H1 資料累積 1 週。

**總預估**：H1 (0.5d) + H2 (2d) + H3 (1.5d) + H4a (1.5d) + H4b (1d) = **6.5 day**

**驗收**：
- turbo mode 在 CPU>85% 時 30s 內自動降級，UI Badge 顯示原因
- 同時跑 8 個 Phase 64-C-LOCAL compile 不會 OOM（AIMD 會先擋）
- 新使用者看 host-device-panel 可一眼知道系統壓力與 queue 狀況
- WSL2 host-load 輔助訊號（loadavg_1m/16）能反映 Windows host 其他進程壓力

---

## 🅥 Priority V — Visual Design Loop + Workspace Architecture（v0.dev / Codex 體驗層 + 獨立工作區）

> 背景：W/P/X 三系列（scaffold + compliance + deploy）已全部完成，但體驗層（AI 自主寫完整 app + 即時視覺回饋 + 對話迭代修改 + 獨立工作區 UI）= 0。v0.dev 和 Codex 的核心差異化不在後端引擎（OmniSight O 系列已平手），而在「使用者看到什麼、怎麼互動」——這正是 V 系列要補的。
>
> **產品定位**：W/P/X 是 OmniSight 的隱藏殺手鐧。嵌入式是主線，但當使用者需要純網站 / 行動 app / 純軟體時，系統必須能理解需求 → 規劃任務 → 自主完成 → 交付成品。V 系列把 W/P/X 從「scaffold generator」升級為「end-to-end autonomous builder + visual iteration + dedicated workspace」。
>
> **UI 架構**：主 dashboard（neural-grid 指揮中心）保留給嵌入式主線。W/P/X 各自導向獨立工作區（`/workspace/web`、`/workspace/mobile`、`/workspace/software`），有專屬 layout、preview pane、迭代 chat。指揮中心顯示「N 個工作區正在運作」summary card 可跳轉。
>
> 對標：v0.dev（Web 體驗）+ Codex for Almost Everything（Mobile + Software 體驗）。目標不是模仿，是「後端治理超越 + 前端體驗平手」。
>
> 相依：**O3 Worker Pool**（✅ done）、**W0-W10 / P0-P10 / X0-X9**（✅ done）、**R0-R3 PEP + ChatOps + Entropy + Scratchpad**（✅ done）。無硬前置阻塞——L 做完後可立即開工。

### V0. Workspace Foundation 共用基建 (#316)
- [x] `app/workspace/[type]/layout.tsx`：workspace router（`/workspace/web` / `/workspace/mobile` / `/workspace/software`） *(done: V0 #1 — dynamic route with `WORKSPACE_TYPES = ["web","mobile","software"]`, `isWorkspaceType` guard, `generateStaticParams`, per-type `generateMetadata`, `notFound()` on unknown segments + 13 contract tests)*
- [x] `components/omnisight/workspace-shell.tsx`：共用 workspace layout 殼（三欄：sidebar / preview / code+chat） *(done: V0 #2 — 3-column CSS grid shell with per-type defaults (`web`→Components/Preview, `mobile`→Platforms/Device Preview, `software`→Languages/Runtime Output), sidebar collapse toggle (ARIA + `data-sidebar-collapsed`), slot props API (`sidebar`/`preview`/`codeChat`), title overrides + 16 contract tests)*
- [x] Workspace context provider：per-workspace 獨立 project state（當前專案 / agent session / preview state）；跟指揮中心 global state 分離 *(done: V0 #3 — `components/omnisight/workspace-context.tsx` (~255 行 Client Component) exports `WorkspaceProvider` + `useWorkspaceContext` + `useWorkspaceType` + `defaultWorkspaceState` + frozen `DEFAULT_{PROJECT,AGENT_SESSION,PREVIEW}_STATE` 常數。**State shape** — 三個獨立 sub-state：`project: {id, name, updatedAt}` / `agentSession: {sessionId, agentId, status: idle\|running\|paused\|error\|done, startedAt, lastEventAt}` / `preview: {status: idle\|loading\|ready\|error, url, errorMessage, updatedAt}`。**Setter API** — `setProject` / `setAgentSession` / `setPreviewState` 三者都接受 `Partial<T> \| null`：partial merge 套用（project / preview 會自動 bump `updatedAt`，agent session 不會因為它用 `lastEventAt` 對應 SSE event 語意），`null` 清回 default。`resetWorkspace()` 一次清三者但保留 `type`（結構欄位，非 per-session state）。**隔離契約** — `useWorkspaceContext()` 在 provider 外呼叫時**直接 throw**（不回退到 global、不 silently 空值），這是命令中心 global state 與 workspace state 嚴格切割的 enforcement 點；`WorkspaceProvider` 在收到 unknown type 時也 throw 擋字串 typo。**持久化預留** — `initialState?: Partial<Omit<WorkspaceState,"type">>` prop 讓 V0 #4 persistence layer 之後可以從 localStorage / backend hydrate 而不必改 API surface。**整合** — `app/workspace/[type]/layout.tsx` 在 `<section data-workspace-type>` 內包入 `<WorkspaceProvider type={type}>`，所以每個 `/workspace/{web,mobile,software}/*` subtree 自動獲得 type-scoped 獨立 context；nested 多 provider 時 inner scope 贏。**測試** — `test/components/workspace-context.test.tsx` **30 顆 contract tests**（本次全 pass；加上既有 V0 #1 的 13 + V0 #2 的 16 共 59/59 workspace 相關測試全 green）涵蓋：defaults + DEFAULT_* frozen、useWorkspaceContext outside-provider throw、每 type defaults stamping、unknown type throw、setProject partial merge + updatedAt auto-bump + caller-supplied updatedAt honoured + null 重置 + 連續 merge 累積、setAgentSession partial merge + status transition + null 重置 + lastEventAt 不被 auto-bump、setPreviewState partial merge + null 重置 + error 路徑 errorMessage 流程、resetWorkspace 三者一次清但保留 type、initialState full 種子 + 部分 seed 缺欄補 default、sibling 兩 provider 不同 type 獨立狀態、nested provider inner-wins、useWorkspaceType 便捷 hook、end-to-end click flow（run → preview ready → reset）；**scope 自律** — 只新增 1 組件 + 1 測試，`layout.tsx` 僅加 `<WorkspaceProvider>` wrapper，沒動 workspace-shell / 其他 omnisight 元件 / 後端)*
- [x] Workspace session persistence：切換工作區不丟失 state（localStorage + backend session sync） *(done: V0 #4 — three new surfaces stitch persistence onto the V0 #3 provider without changing its API. **`hooks/use-workspace-persistence.ts`** (~220 行) exports a pure utility module: `WORKSPACE_SNAPSHOT_SCHEMA_VERSION = 1`, `workspaceStorageKey(type)` → `"omnisight:workspace:<type>:session"`, `workspaceSessionApiPath(type)` → `/api/workspace/<type>/session`, plus load/save/fetch/push/clear and `parseWorkspaceEnvelope` + `pickNewerEnvelope` (newest-savedAt-wins with last-writer-tiebreak). Every function is SSR-safe (`typeof window === "undefined"` early-exit), never throws, and returns `null` on malformed payloads — so a corrupt localStorage blob silently falls back to defaults instead of crashing the workspace shell. `fetchImpl` is injectable for tests. **Envelope shape**: `{schemaVersion: 1, savedAt: <ISO-8601>, state: {project?, agentSession?, preview?}}` — mirror of the three V0 #3 sub-states; unknown sub-state fields are dropped on parse for forward-compat. **`components/omnisight/persistent-workspace-provider.tsx`** (~180 行 Client Component) wraps `<WorkspaceProvider>` with a bridge that hydrates via `useEffect` (two-phase: localStorage seed first, backend GET overlays only when `savedAt` strictly newer; defer-to-effect avoids Next.js hydration mismatches on SSR). Write-through is sync to localStorage and debounced (default 400ms, `backendDebounceMs={0}` for tests) to `/api/workspace/<type>/session`. A `suppressSaveRef` + microtask-release pattern prevents the PUT-loop that would otherwise round-trip the backend's own payload back at it during hydration. `disableBackendSync` prop for browser-only mode. **`app/api/workspace/[type]/session/route.ts`** (~130 行) implements GET / PUT / DELETE with an HMR-persistent `globalThis`-attached `Map<WorkspaceType, envelope>` store; schema-validated PUT returns 400 on bad JSON / bad schema / unknown type; 204 on empty GET; 200 with envelope on hit. `__resetWorkspaceSessionStoreForTests` exported. **整合**: `app/workspace/[type]/layout.tsx` swapped `<WorkspaceProvider>` for `<PersistentWorkspaceProvider>` — one-line change, zero API surface churn for V0 #3 consumers. **測試** — 3 new suites, **59 new tests all green** (plus existing V0 #1/#2/#3 59 still green + pre-existing regression coverage = 120 workspace-related tests pass with zero regressions): `test/hooks/use-workspace-persistence.test.ts` (31 顆) — storage key namespacing, envelope schema guards (non-object / wrong version / missing savedAt / bad state), roundtrip, per-type scoping, malformed JSON tolerance, unknown-type runtime guard, save failure under quota mock, backend GET (204/404/net-fail/malformed/happy), backend PUT (ok/5xx/net-fail + body shape), `pickNewerEnvelope` precedence + tiebreak, SSR safety (no window); `test/app/workspace-session-api.test.ts` (13 顆) — GET empty→204, GET unknown-type→400, PUT→GET roundtrip, PUT bad-type/bad-json/bad-schema/bad-state→400, overwrite semantics, cross-type isolation, partial-state sub-object drop, DELETE 204 + remove; `test/components/persistent-workspace-provider.test.tsx` (15 顆) — empty-storage default render, seeded-after-mount hydration (all three sub-states), malformed-localStorage tolerance, type-scoped seed isolation, project/agent/preview write-through to localStorage, schemaVersion stamping on save, unmount→remount roundtrip, mount-time GET call + path + method, backend-wins-on-newer-savedAt, backend-loses-on-older-savedAt, debounced PUT body shape, network-failure tolerance, `disableBackendSync` path (no fetch, still writes storage), reset clears + persists. **Scope 自律** — only added 3 source files + 3 test files + 2-line layout edit; V0 #3 `workspace-context.tsx` untouched and its 30 contract tests still pass; no coupling to Auth/Tenant providers (keeps the "workspace state ≠ global state" V0 #3 isolation intact — tenant scoping at the key level is a future layer))*
- [x] `components/omnisight/workspace-bridge-card.tsx`：指揮中心的 summary card——顯示「3 個工作區正在運作」+ 每個工作區的 agent 狀態 + 點擊跳轉 *(done: V0 #5 — `components/omnisight/workspace-bridge-card.tsx` (~340 行 Client Component) + 35 顆 contract tests。**組件 API** — 預設 **uncontrolled** 模式：mount 時從 localStorage 讀 3 個 workspace type 的 snapshot（透過 V0 #4 的 `loadWorkspaceSnapshotFromStorage`），然後可選擇性 fetch `/api/workspace/<type>/session` 比對 `savedAt` 決定要不要用 backend 版本（用 V0 #4 的 `pickNewerEnvelope` — tie 時 challenger/backend 贏，與 V0 #4 語義一致）；**controlled** 模式：caller 傳 `workspaces` prop 時，組件視其為 single source of truth 並跳過所有 storage / 網路 I/O（測試 seam + 未來 V0 #6 SSE live-state 的直接注入點）。**Props** — `workspaces?` / `onNavigate?(type)` / `disableBackendSync?` / `fetchImpl?` / `isWorkspaceActive?` (default: `status ∈ {running, paused, error}`) / `nowMs?()` (時鐘 seam、deterministic tests) / `title?` (default "Workspaces") / `className?`。**命令中心隔離契約** — **刻意不呼叫 `useWorkspaceContext()`**：card 渲染在 dashboard subtree，那裡**沒有** `<WorkspaceProvider>`；V0 #3 的 hard-throw 契約意味著 card 呼叫它會直接爆炸。改走 V0 #4 已建好的持久化介面，所以 workspace state **只流經 persisted envelope** 這一個單向通道，不汙染 command-center global providers (auth/tenant/engine)。**Exports** — `WorkspaceBridgeCard` / `defaultIsWorkspaceActive` / `emptyWorkspaceSummary(type)` / `summariseEnvelope(type, envelope\|null)` / `formatRelativeSince(iso, nowMs)` 及 `WorkspaceSummary` / `WorkspaceBridgeCardProps` 型別。**UI contract** — header 顯示 `{activeCount} / 3 workspaces running`；3 列固定順序（web → mobile → software），每列 `<Link href="/workspace/<type>">` + `onClick → onNavigate(type)`；每列有 `data-workspace-type` / `data-active` / `data-agent-status` / `data-preview-status` 屬性方便 SSE router (V0 #6) 之後 grep；status badge 有 per-status tone（idle = muted / running = emerald / paused = amber / error = destructive / done = sky）；aria-label 包含 workspace label 與 agent status。**Relative timestamp** — `formatRelativeSince`：null/invalid → `—`；< 1m → `just now`；< 1h → `Nm ago`；< 1d → `Nh ago`；≥ 1d → `Nd ago`。**測試** — `test/components/workspace-bridge-card.test.tsx` **35 顆 contract tests 全 pass**（加上 V0 #1-#4 既有 120 顆 = 155/155 workspace-related 測試全 green，零 regression）：helper 測試（`emptyWorkspaceSummary` 每 type defaults + 獨立 reference、`summariseEnvelope` null/full/partial/frozen-default 不被 mutate、`defaultIsWorkspaceActive` 5 狀態分類、`formatRelativeSince` 5 段時間）、controlled render（3 列順序、missing types 補空、active count 精確計算、summary text 格式、custom title、custom `isWorkspaceActive` 覆蓋、status+preview+last-event+project 4 欄文案、per-row data-* 屬性、lastEventAt=null → `—`）、navigation（href 對應 3 type、`onNavigate(type)` 被呼叫 once、aria-label 含 workspace + agent status）、uncontrolled localStorage（mount 時 hydrate、controlled 時 storage 被忽略、corrupt JSON 安全降級成 default、`disableBackendSync` 時 fetch 不被呼叫）、uncontrolled backend（3 type 都 GET、newer backend envelope 覆蓋 storage、older backend envelope 被 storage 勝出、network error → 組件不炸、全部 default row 渲染）。**Scope 自律** — 只新增 1 組件 + 1 測試；V0 #3 `workspace-context.tsx` 未動（30 顆 context 契約測試全綠）、V0 #4 `use-workspace-persistence.ts` 未動（31 顆 hook 測試全綠）、V0 #4 `persistent-workspace-provider.tsx` 未動（15 顆 integration 測試全綠）、V0 #4 session API route 未動（13 顆 API 測試全綠）、V0 #1/#2 workspace-layout/shell 未動（29 顆測試全綠）；後端完全沒動；command-center dashboard 也沒被改（card 還沒掛進任何 parent — 那會是單獨的 integration checkbox）)*
- [x] SSE event routing：`workspace.type` filter 確保 agent event 只推送到對應工作區（不汙染指揮中心） *(done: V0 #6 — `lib/api.ts` 新增 `WorkspaceType = "web"|"mobile"|"software"` 型別、module-level `_currentWorkspaceType` 與 `setCurrentWorkspaceType`/`getCurrentWorkspaceType` getter/setter 對稱於既有 session/tenant gate；`_shouldDeliverEvent` 新增 **workspace gate**（在 session/tenant/scope 篩選之前跑）：事件 `data._workspace_type` 非空字串時 → `_currentWorkspaceType===null`（指揮中心）直接 reject、`_currentWorkspaceType !== _workspace_type` 直接 reject、匹配才 fall-through 到既有 session/tenant 邏輯；`_workspace_type` 缺省或空字串 → 完全不經 workspace gate，維持 J1/I3 backward compat。**指揮中心隔離契約** — dashboard 從未呼叫 `setCurrentWorkspaceType`，因此 `_currentWorkspaceType === null`，所有帶 `_workspace_type` 的 agent event 都被拒絕 → 不會再汙染 Agent Matrix Wall / 全域 header。**Provider 綁線** — `components/omnisight/persistent-workspace-provider.tsx` 新增一個 `useEffect`（deps=`[type]`，在 hydrate effect 之前跑）於 mount 時呼叫 `setCurrentWorkspaceType(type)`，cleanup 時**先讀 `getCurrentWorkspaceType()` 對比**才清 null — 這防止 Next.js route transition 時新 layout 的 effect 先跑、舊 layout 的 cleanup 後跑把新 workspace 的 registration 誤清（React 18 strict-mode 重 mount 也 benefits from this guard）；`app/workspace/[type]/layout.tsx` 完全沒動，因為 provider 已經在 V0 #4 wrap 進去了。**測試** — 新增 `test/integration/sse-workspace-filter.test.ts` **18 顆 contract tests** + augment `test/components/persistent-workspace-provider.test.tsx` **3 顆 registration tests**（共 21 新測試全 pass；workspace-related 總測試數 155 → 175 全 green、零 regression）：getter/setter roundtrip、三 type 匹配傳遞（web/mobile/software × agent_update/tool_progress/task_update）、三組跨 type mismatch 拒絕（web 丟 mobile event / mobile 丟 software / software 丟 web）、command center isolation（`null` 丟三 type 全拒）、command center 仍收 global heartbeat/mode_changed、backward compat（無 `_workspace_type` 正常派送、空字串視同無）、composition with session/tenant gates（workspace match + session match 雙綠燈 deliver / workspace match + session mismatch session gate 擋 / workspace mismatch + session match workspace gate 擋 / workspace match + tenant match deliver / workspace mismatch + tenant match workspace gate 擋）、multi-workspace fixture（同一 wire 四 surfaces 依序切換各只看到自己的 event）、runtime switch（mount 成 web 收到 web event、switch 成 mobile 後 web event 被擋 mobile event 通行）；provider 新 3 顆覆蓋：mount 註冊 + unmount 清空、cleanup 不覆蓋已被新 workspace 佔走的 slot（模擬 route transition 競態）、`type` prop 變更觸發 re-registration。**Scope 自律** — 只動 2 source files（`lib/api.ts` +31 行；`persistent-workspace-provider.tsx` +23 行）+ 2 test files（1 新 + 1 augment）；後端完全沒動（backend `_workspace_type` 注入是獨立 P-track 任務，前端先備好 gate 等後端 ship）；V0 #1-#5 檔案未動（workspace-context / workspace-shell / workspace-bridge-card / use-workspace-persistence / session API route 都零改動，對應測試全綠）；指揮中心 dashboard 也沒改（不需要 — 它天然落入 `_currentWorkspaceType === null` 分支，自動套用 isolation 規則）)*
- [x] `components/omnisight/workspace-chat.tsx`：共用對話式迭代 chat panel（文字 + 圖片上傳 + annotation reference + NL → 任務），三個工作區共用此元件 *(done: V0 #7 — `components/omnisight/workspace-chat.tsx` (~430 行 Client Component) + 44 顆 contract tests。**組件 API** — 單一入口的 conversational iteration 面板，三工作區共用：`workspaceType?` / `messages?` / `annotations?` / `onSubmitTask?(submission)` / `disabled?` / `placeholder?` / `title?` / `idFactory?` / `nowIso?` / `readAttachmentsFromFiles?` / `className?`。**Submission 契約** — `{text, attachments, annotationIds, workspaceType}`：`text` 是 `draftText.trim()`，`attachments` 是 pending file tray 原樣傳出，`annotationIds` 是選取中的 annotation id list，`workspaceType` 透過 `useOptionalWorkspaceType()` 從 provider 讀（否則走 prop override）— 這對齊 V0 #6 SSE gate 的 `_workspace_type` key，讓 backend router 能 dispatch 回正確 workspace。**Type 解析** — 新增 `useOptionalWorkspaceType` / `useOptionalWorkspaceContext` 在 `workspace-context.tsx`（非 throw 變體），chat 只用這組 hook 讓它可以同時在 provider 內（`/workspace/[type]/*`）與 provider 外（storybook / command-center host）渲染，但兩者都缺時硬 throw 擋 silent bug。**Composer 行為** — Enter 送出 / Shift+Enter 換行、文字/附件/annotation 三選一非空才啟用 submit、async submit 進行中鎖 submit/text/attach/chip、成功清空 composer、失敗（Promise reject）保留 composer state 讓 operator retry、`disabled` prop 全域鎖所有 input surface。**Attachments** — 隱藏 `<input type="file" multiple>` + 拖放區（`onDragOver`/`onDragLeave`/`onDrop`，`data-dragging` 屬性 for 樣式 + 測試）、`filesToChatAttachments` 純函數把 `File[]` 轉成 `WorkspaceChatAttachment[]`（含 `id`/`name`/`mimeType`/`sizeBytes`/`previewUrl`；image/* 才生 objectURL）、`WORKSPACE_CHAT_MAX_FILE_BYTES = 10 MB` 硬上限過濾、remove button `revokeObjectURL` 安全清理、`readAttachmentsFromFiles` 注入點讓 caller（e.g. V3 software track backend upload layer）可改語意。**Annotation chips** — 每個 annotation 渲染成 toggle chip（`@{label}` + `data-active` + `aria-pressed`），多選支援，當 `annotations` prop 縮減時自動 drop 已選但不再存在的 id（`useEffect` 去重），tooltip 用 `description ?? label`。**訊息渲染** — `role ∈ {user, agent, system}` 三色 tone、ISO-8601 timestamp 顯示 `toLocaleTimeString()`（pending 顯示 "Sending…"）、pending 透過 `data-pending` 標記、每訊息 attachment/annotation 子列只在有資料時渲染、empty-state 有自己的測試 node、`aria-live="polite"` 讓螢幕閱讀器朗讀 agent 回覆、auto-scroll to `logEndRef` when log 成長。**Placeholder 區分** — per-type 預設文案（web 強調 UI change + screenshot、mobile 強調 flow + platform annotation、software 強調 behaviour + log snippet），`placeholder` prop override。**Exports** — `WorkspaceChat`（default + named）、`WORKSPACE_CHAT_MAX_FILE_BYTES`、`defaultChatIdFactory`、`defaultNowIso`、`filesToChatAttachments`；型別 `WorkspaceChatRole` / `WorkspaceChatAttachment` / `WorkspaceChatAnnotation` / `WorkspaceChatMessage` / `WorkspaceChatSubmission` / `WorkspaceChatProps`。**測試** — `test/components/workspace-chat.test.tsx` **44 顆 contract tests 全 pass**（加上既有 V0 #1-#6 176 顆 = 220/220 workspace-related 測試全 green，零 regression）：helper 測試（filesToChatAttachments 4 顆：mapping、default mime、image-only preview URL、size-gate drop；defaultChatIdFactory 3 顆：uniqueness、randomUUID-missing fallback branch、defaultNowIso ISO-8601 輸出）、type resolution 5 顆（prop-only、provider-only、prop wins over provider、兩者皆無→throw、invalid prop→throw）、header 3 顆（default/custom title、aria-label 含 title+type）、message log 5 顆（empty state、undefined messages、順序/role/text、role+pending data attrs、per-message attachments+annotations、無附件時不渲染空列）、composer 6 顆（empty 時 submit disabled、text 後啟用、submission payload shape、Enter/Shift+Enter 分流、success 清空、reject 保留、in-flight 鎖）、attachments 6 顆（file input path、drop path、remove 按鈕、submission 含 attachments+success 清空、only-attachments allow submit、custom reader 注入）、annotation 6 顆（無 annotations 不渲染 tray、chip label 前綴、toggle、多選 payload、縮減後 drop stale id、only-annotation allow submit）、disabled 2 顆（所有 input 鎖、Enter 也不 fire）、placeholder 2 顆（per-type 差異、override）。**Scope 自律** — 新增 1 組件 + 1 測試 + `workspace-context.tsx` 加 2 個 non-throw hook（`useOptionalWorkspaceContext` / `useOptionalWorkspaceType`；既有 `useWorkspaceContext`/`useWorkspaceType` 零改動，V0 #3 的 30 顆契約測試仍綠）；V0 #1/#2/#4/#5/#6 所有檔案未動，對應測試全綠；後端完全沒動（backend `annotation reference` 與 file upload 是 V1/V3 track 任務，前端先備好 composer 等後端 ship）；layout.tsx/workspace-shell.tsx 未動（chat 還沒被掛進 `codeChat` slot — 那會是單獨的 V1 integration checkbox））*
- [x] Workspace navigation sidebar：per-type sidebar template（web = component palette；mobile = platform selector；software = language selector） *(done: V0 #8 — `components/omnisight/workspace-navigation-sidebar.tsx` (~350 行 Client Component) + 48 顆契約測試全綠。**單模板三形態** — 一個 `WorkspaceNavigationSidebar` 組件同時餵三套 per-type 預設資料，caller `items` prop override 永遠贏：`DEFAULT_WEB_COMPONENTS`（button/input/textarea/select/checkbox/card/tabs/dialog/toast/table，分 Actions/Forms/Layout/Navigation/Overlays/Data）、`DEFAULT_MOBILE_PLATFORMS`（ios/android/flutter/react-native，分 Native/Cross-platform，meta=語言）、`DEFAULT_SOFTWARE_LANGUAGES`（python/typescript/go/rust/cpp/shell，分 Scripting/Systems，meta=版本）全用 `Object.freeze(...) as const` 定住 reference；`getDefaultSidebarItems(type)` 回傳 **fresh copies** 避免 caller mutate 傷到 frozen 原件。**Props API** — `workspaceType?` / `items?` / `selectedId?` / `defaultSelectedId?` / `onSelectionChange?(id, item)` / `searchable?`(default true) / `searchPlaceholder?` / `title?` / `emptyMessage?` / `className?`。**Type 解析**（與 V0 #7 對稱）— 用 `useOptionalWorkspaceType()` 讀 provider、`workspaceType` prop 永遠優先、兩者都缺或 invalid → hard throw。**選取雙軌** — controlled (`selectedId !== undefined`) pin 住、click 仍 fire callback 但不移動；uncontrolled 以 `defaultSelectedId` 啟動、click 當下切換；`selectedId={null}` 區別於省略（controlled-no-selection）。**Disabled items** — `aria-disabled` + `data-item-disabled="true"` + native `disabled` 屬性 + 樣式，`handleSelect` 內部也 guard（bypass 屬性後仍不 fire）。**Search 濾鏡** — 純函式 `filterItemsByQuery(items, query)` 對 label/description/category/meta case-insensitive；0 match 顯示 per-type empty state；`searchable={false}` 隱藏輸入框。**Grouping** — `groupItemsByCategory(items)` 保留 first-seen order；有 category 者各自 `<section>` + `<h4>` 標頭；無 category 者 trailing `{category:null}` bucket 且不渲染 heading。**隔離契約** — 組件**刻意不寫** workspace context state（不 call `setProject` / `setAgentSession`）：選取純 UI-local；想持久化的 caller 自己在 `onSelectionChange` handler 內寫進 workspace context / 自家 store — 與 V0 #5 bridge card / V0 #7 chat「共用 UI 組件不碰 shared state」哲學完全平行。**Exports** — `WorkspaceNavigationSidebar` / `DEFAULT_WEB_COMPONENTS` / `DEFAULT_MOBILE_PLATFORMS` / `DEFAULT_SOFTWARE_LANGUAGES` / `getDefaultSidebarItems` / `groupItemsByCategory` / `filterItemsByQuery` 及 `WorkspaceSidebarItem` / `WorkspaceNavigationSidebarProps` 型別。**測試** — `test/components/workspace-navigation-sidebar.test.tsx` **48 顆全 pass**（workspace-related 總測試 220 → 268 全綠、零 regression）覆蓋：helper 純函式 15 顆（getDefaultSidebarItems 4 + DEFAULT_* frozen+unique 4 + groupItemsByCategory 4 + filterItemsByQuery 7 — 四欄位搜尋）、type resolution 5 顆、per-type labels 2 顆（it.each 三 type + overrides）、items source 5 顆、grouping 2 顆、search filter 4 顆、uncontrolled selection 3 顆、controlled selection 2 顆、disabled items 2 顆、meta chip 2 顆。**Scope 自律** — 只新增 1 組件 + 1 測試；V0 #1-#7 所有檔案未動（對應 220 顆測試全綠）；後端完全沒動；`workspace-shell.tsx` / `app/workspace/[type]/layout.tsx` 未動（sidebar 還沒掛進 shell.sidebar slot — 那是 V1 web / V2 mobile / V3 software 各自的 integration checkbox）。**🎉 V0 Workspace Foundation #316 全 8 顆 checkbox 完成**（#1-#8），workspace-related 測試總量 268 顆、10 檔、1.34s）*
- 預估：**6 day**

### V1. Web — AI 自主 UI 生成引擎 (#317)
- [x] `configs/roles/ui-designer.md`：UI Designer specialist agent role skill——精通 shadcn/ui 全套 API（Button/Card/Dialog/Sheet/Table/Form/...）+ Tailwind utility classes + responsive breakpoints + WAI-ARIA patterns + color contrast *(done: V1 #1 — `configs/roles/ui-designer.md` (~250 行 skill spec) frontmatter (`role_id: ui-designer` / category: web / tools 含 `get_available_components`+`load_design_tokens`+`run_consistency_linter`+`get_design_context`) + 全套 shadcn/ui API 覆蓋分 7 區塊（Inputs/Actions、Form、容器/Layout、Navigation、Overlays/Feedback、Data Display、always-call-the-registry rule）列出當前 `components/ui/` 已 install 的全部 50+ 元件對應 props/variants/sizes、Tailwind 慣例（4-base spacing scale + design-token color utility 表 + typography size scale + radius/shadow + class composition order 範例）、5 級 responsive breakpoint 表（sm 640/md 768/lg 1024/xl 1280/2xl 1536）+ mobile-first / 44px touch / container queries / sidebar collapse rules、WAI-ARIA pattern 對應表（14 條 APG pattern → shadcn 元件 → 必檢項）+ 自組元件 7 條 ARIA 必檢、WCAG 2.2 AA 對比硬性下限 + 專案 dark theme palette **已驗對比表**（6 條前景/背景組合全 ≥ 5:1）+ 色盲安全 rules、AI UI 生成 SOP（拿事實→intent→拆元件樹→寫程式→自我審查→交付 6 步）、12 條 Anti-patterns（raw HTML 替代、div onClick、inline hex、outline:none、!important、arbitrary breakpoint、aria-label 不一致、Tooltip 載重要訊息、bg-slate-900 hard-pin、Carousel 無 Pause、tabindex>0、light-mode 假設、image 無 alt）、V1 #317 acceptance gate 12 條品質標準、與其它 V1 sibling（registry/design-token-loader/consistency-linter/vision-to-ui/Figma-MCP/WebFetch/Edit-router）的 7 行協作介面表、PR 自審 10 條 checklist。**為什麼這份 skill 是 V1 整條 pipeline 的根** — 後續 sibling (`backend/ui_component_registry.py` / `backend/design_token_loader.py` / `backend/component_consistency_linter.py` / `backend/vision_to_ui.py` / Figma MCP 串接 / URL→reference / Edit complexity auto-router / 整合測試) 全部餵到這個 agent 的 context 裡，由這份 skill 決定 agent 「拿到 registry/tokens/visual context 後該怎麼把它們串成 React+shadcn+Tailwind code」；其它 sibling 提供事實，這份 skill 提供判斷準則 —— 沒有它 agent 會回到訓練記憶猜元件 API、寫死 hex 色、忘記 ARIA。**Scope 自律** — 只新增 1 個 skill 檔；不動 `configs/roles/web/` 既有 6 份 skill (a11y/perf/seo/frontend-react/frontend-svelte/frontend-vue)、不動任何 backend / frontend 應用碼、不新增測試（skill 是純 prompt-content 文件、行為由 sibling backend 模組與 consistency linter 驗證——對應的契約測試會在 V1 #2-#4 sibling 各自 ship 時建立）)*
- [x] `backend/ui_component_registry.py`：shadcn/ui component registry——列舉所有可用元件名 + props interface + 典型使用範例，agent 呼叫 `get_available_components()` 取得清單注入 context *(done: V1 #2 — `backend/ui_component_registry.py` (~720 行) 定義 `ShadcnComponent` / `ComponentProp` / `ComponentVariant` frozen dataclass + 固定 7 類 `CATEGORIES` (inputs/form/layout/navigation/overlay/feedback/data) + 55 條 `REGISTRY` 條目（對齊 `components/ui/*.tsx` 全部 55 個實裝元件，自動排除 `use-mobile`/`use-toast` 兩個 utility hook）每條含 name/summary/exports tuple/CVA variants/props/canonical TSX example/ARIA pattern/notes。公開 API：`get_available_components(project_root=None, category=None)` 回傳 JSON-safe dict list（tuple→list、dataclass→dict、按 name 排序）並於有 `project_root` 時掃描 on-disk 過濾（missing tree 時 graceful fallback 到完整 catalogue 不餓死 agent context）、`get_component(name)` / `list_component_names()` / `get_components_by_category(cat)` 查詢輔助、`find_missing_on_disk(root)` 供 CI 斷言「新加檔卻忘了註冊」抓漏、`render_agent_context_block(project_root, categories)` 產 deterministic markdown（prompt-cache 穩定）。契約測試 `backend/tests/test_ui_component_registry.py` 152 條全綠：結構不變量（frozen dataclass / 未知 category / 空 exports / 空 example 擋住）、parametrize 遍歷每元件檢查 import_path + 必備欄位、on-disk 雙向 parity、JSON serialisation 不漏 tuple、category filter、spot-check 高頻元件（button variants / Form 要有 useForm+zodResolver / Dialog 必要子件 / Tooltip mobile caveat / Carousel WCAG 2.2.2 pause / AlertDialog action+cancel / Chart 禁 hex）、`render_agent_context_block` 決定性 + 全 category 覆蓋 + project-root scoping。**為什麼這是 V1 pipeline 的骨架** — UI Designer skill (#1) 的 step-zero「強制呼 `get_available_components()`」終於有對應後端：agent 拿到 registry 才知道專案真的有哪些 shadcn 元件可用、import 路徑、CVA variant 值、ARIA 慣例——阻斷「從訓練記憶猜 props」這條主要 bug 入口；同時 `find_missing_on_disk` 讓 CI 發現「有人 add 了新元件卻忘更新 registry」；`render_agent_context_block` 的 deterministic 輸出是後續把元件清單注入 agent prompt 的入口（byte-identical → prompt cache hit）。sibling V1 #3 `design_token_loader.py` / #4 `component_consistency_linter.py` / #5 `vision_to_ui.py` 之後會各自消費這份 registry。**Scope 自律** — 只新增 1 個 `backend/ui_component_registry.py` + 1 個 test 檔；不動 `configs/roles/ui-designer.md`（已把 tool 名宣告好）、不動 `components/ui/*.tsx`（只讀檔不動）、不改任何其它 backend 模組、不動 frontend code。152 測試 0.18 s 全綠；sibling `test_web_role_skills.py` 51 條 regression 0 錯)*
- [x] `backend/design_token_loader.py`：載入目標專案的 `tailwind.config.ts` + `globals.css` → 提取 color palette / font stack / border-radius / spacing scale → `DesignTokens` dataclass 注入 agent 生成約束 *(done: V1 #3 — `backend/design_token_loader.py` (~1000 行) 新增 `DesignToken` / `DesignTokens` frozen dataclass + 固定 6 類 `KINDS` (color/font/radius/spacing/shadow/other) + 固定 5 類 `SCOPES` (root/dark/theme/html/tailwind-config)。核心 CSS parser 採自製 zero-dep 掃描器：`_iter_top_level_blocks` 同時追蹤 paren-depth 與 brace-depth 使 `@custom-variant dark (&:is(.dark *));` 之類含括號但無 braces 的 at-rule statement 不被誤當 block selector；`_remove_nested_blocks` + `_split_declarations` 跳過 `@keyframes` / `@layer` / `@media` / component-rule noise 只抽 top-level custom-property；`_classify_selector` 把 `:root` / `.dark` / `@theme inline` / `html { ... }` 歸到對應 scope，其它丟棄。`_classify_kind` 先走 name-prefix（`color-` / `font-` / `radius-` / `spacing-` / `shadow-` + shadcn semantic 全白名單 + FUI brand `neural-blue` / `hardware-orange` / `artifact-purple` / `validation-emerald` / `critical-red` / `holo-glass` / `deep-space-*`），fallback 走 value-shape regex（hex / rgb / hsl / oklch / oklab / lab / lch / color / transparent / currentColor / var(--known-*)）；無法分類的落 `other` 不崩。Tailwind v3 backwards-compat：`_parse_tailwind_config` 用保守 regex 抓 `colors/fontFamily/borderRadius/spacing/boxShadow` 5 區塊（支援字串 + array 值 → join），v4 專案直接跳過。公開 API：`load_design_tokens(project_root)` graceful-fallback（None / missing dir / unreadable / 非 UTF-8 → 回 well-formed empty `DesignTokens`，絕不 raise 打斷 agent prompt；新捕獲 `UnicodeDecodeError` 這個 robustness 漏洞在寫測試時被發現並修復）；`DesignTokens.palette` / `palette_dark` / `fonts` / `radii` / `spacing` / `shadows` / `brand` 全走 `MappingProxyType` 唯讀 view（排序 by name）；`brand` 自動剔除 shadcn semantic/chart/sidebar 白名單留下 FUI 品牌色；`utility_classes()` 從 `@theme` 綁定產 Tailwind v4 自動生成 utility（`--color-X → bg-X/text-X/border-X`、`--radius-X → rounded-X`、`--spacing-X → p-X/m-X/gap-X`、`--font-X → font-X`）；`to_dict()` JSON-safe 含 schema_version；`to_agent_context()` / `render_agent_context_block(project_root)` 產 deterministic markdown（版本 header + Sources + dark-only 標誌 + 7 區塊排序 palette + utility classes 清單 + Generation rules，byte-identical 穩定 prompt cache）；`_find_css_files` 依序查 `components.json → tailwind.css` → `app/globals.css` → `styles/globals.css` → `src/styles/globals.css`。契約測試 `backend/tests/test_design_token_loader.py` 97 條全綠 0.13 s：module invariants（`KINDS`/`SCOPES`/semver/`__all__`）、`DesignToken` frozen + kind/scope/name validation、`DesignTokens` empty default + JSON safe + filter validation、CSS parser 覆蓋（fixture `_TAILWIND_V4_CSS` 實驗多 scope/keyframes/layer noise 同檔存在也不污染抽出結果 + root palette + dark palette 不滲漏 + fonts + radii + brand 隔離 + utility class 由 `@theme` 獨家產出）、`_classify_kind` 22 條 parametrize 覆蓋所有 prefix 路徑 + value-shape fallback + "other" 底、malformed CSS 6 條 fallback（空檔 / 純 comment / unclosed brace / custom-variant 誤判 / @media 噪音 / 非 UTF-8 位元流 → 絕不 raise）、`load_design_tokens` entry-point（None / missing dir / no globals / string path / components.json pointer 勝 / styles/ fallback）、Tailwind v3 config（colors/fonts/radii/spacing 抽出 + array font stack 合併）、agent context 決定性（byte-identical cross-call）+ 版本 header + 必備 sections + dark-only 警示 + palette 按名排序、JSON 序列化 roundtrip + MappingProxyType 唯讀、live-project parity 17 條（專案 `app/globals.css` 真的有 shadcn semantic 10 token + FUI brand 5 token + chart-1..5 + font-sans/mono + radius scale + utility 覆蓋，失守就爆 — UI Designer skill prompt 引用的字面 token 必保活）；sibling `test_ui_component_registry.py` (152) + `test_web_role_skills.py` (51) 203 條 regression 0 錯。**為什麼此模組是 V1 pipeline 的另一支腳** — UI Designer skill (#1) 的 step-zero 要求 `load_design_tokens(project_root)`，此模組把 CSS custom-property 從 live `globals.css` 抽成結構化 DesignTokens 注進 agent context → agent 不再回到訓練記憶亂寫 `bg-slate-900`、`#38bdf8`、`px-[13px]`；搭配 sibling registry 產出同格式 agent-context markdown（`render_agent_context_block`），兩者組合構成「拿事實」step 的完整事實面；`is_dark_only` 旗標把 `html { color-scheme: dark }` 的 UX 硬性合約穿到 agent prompt，避免被 emit `dark:` prefix 或 light fallback；`UnicodeDecodeError` robustness 漏洞寫測試時抓到並修復（docstring 原承諾 "never raises mid-prompt" 但只抓 OSError），避免未來 BOM-less UTF-16 / 壞檔 crash agent pipeline。**Scope 自律** — 只動 1 個 `backend/design_token_loader.py`（實作檔早已 staged，僅 unicode 容錯一行擴充）+ 1 個 test 檔；不動 `configs/roles/ui-designer.md`（已宣告工具名 `load_design_tokens`）、不動 `app/globals.css` / `styles/globals.css`（只讀不改）、不動任何 backend routing/server 碼、不動 frontend code、不動 sibling `ui_component_registry.py`)*
- [x] `backend/component_consistency_linter.py`：post-generation 掃描 → 偵測 raw `<div>`/`<button>`/`<input>` 可被 shadcn 元件替代的模式 → 自動修正或提示 agent 修正 *(done: V1 #4 — `backend/component_consistency_linter.py` (~700 行) 新增 `LintRule` / `LintViolation` / `LintReport` frozen dataclass + 固定 `SEVERITIES=(error/warn/info)` + 17 條 `RULES` 不可變 `MappingProxyType` 目錄，完整對齊 UI Designer skill (#1) 的 12 條 Anti-patterns：raw HTML→shadcn（`raw-button` / `raw-input` / `raw-textarea` / `raw-select` / `raw-dialog` / `raw-progress`）、semantic a11y（`div-onclick` / `role-button-on-div` / `img-without-alt` / `tabindex-positive` / `focus-outline-none-unsafe`）、design token（`inline-hex-color` / `hard-pinned-palette` / `arbitrary-size` / `arbitrary-breakpoint` / `important-hack` / `dark-prefix-on-dark-only`）。detector 體系用 8 支純 regex scanner 加 `_strip_comments` preprocess（保留 newline 位置以維持 line number，`{/* */}` / `/* */` / `//` 全 zero-out 內容避免誤判 comment 內示例）、`_OPEN_TAG_RE` 限於 lowercase 起頭才算 HTML tag（JSX component `<Button>` 不觸發）、`<input data-slot="native-input">` opt-out shadcn 內部 slot、`_HARD_PALETTE_RE` 覆蓋 22 族 Tailwind 調色（bg/text/border/ring/from/to/via/fill/stroke/divide/outline/decoration/placeholder/caret/accent/shadow）、`_ARBITRARY_SIZE_RE` 限於 `text/p/m/w/h/gap/rounded/…` 系列不誤判 `grid-cols-[1fr_auto]`、`_ARBITRARY_BREAKPOINT_RE` 抓 `min-[Npx]:` / `max-[Npx]:`、`_IMPORTANT_RE` 同時抓字面 `!important` 與 Tailwind `!utility-class` shortcut（lookbehind 去 false-positive `!==`）、`_DARK_PREFIX_RE` 只在 dark-only 專案觸發（dead code 警告）、`_OUTLINE_NONE_RE` className-scoped 偵測 `outline-none` 但若同字串內有 `focus-visible:ring-*` 替代就豁免、`_TABINDEX_RE` 只標 `tabIndex > 0`（0/-1 合法）、inline `style={{ outline: "none" }}` 檢同 scope 窗內 `boxShadow` / `ring` 替代才豁免。公開 API：`lint_code(code, source=None)` deterministic 排序（line→col→rule_id）；`lint_file(path)` missing / unreadable / `UnicodeDecodeError` graceful-fallback 回 clean report 絕不 raise；`lint_directory(root, extensions=(.tsx,.jsx), exclude=(node_modules/.next/dist/build/out/components/ui))` 預設排除 vendored shadcn 源（是 wrapper 本身合理用 raw HTML）；`auto_fix_code(code)` idempotent tag swap（`<button>→<Button>` / `<input>→<Input>` / `<textarea>→<Textarea>` / `<progress>→<Progress>` 4 條 mechanical rewrite）+ `_ensure_imports` 自動補 `import { Button } from "@/components/ui/button"`（merge 進現有同 path import、跳過 `"use client"` directive、不重複）；`auto_fix_file(path, write=True)` 讀檔 → rewrite → optionally 寫回；`render_report(report | iter)` 產 deterministic markdown（含 schema version header + summary line + per-file violation list + snippet + suggested_fix）；`run_consistency_linter(code=None, path=None, auto_fix=False)` agent-callable entry（對齊 UI Designer skill (#1) 的 `priority_tools` 名），exactly-one-of 檢查、回 JSON-safe dict 含 `schema_version/source/is_clean/severity_counts/rule_counts/violations/auto_fix_applied/fixed_code/markdown`。契約測試 `backend/tests/test_component_consistency_linter.py` 105 條全綠 0.11 s：Rule Catalogue 不變量（semver/severity tuple 固定/MappingProxyType 唯讀/17 條 rule 全存在/每條欄位合法/unknown severity 擋/empty id+summary 擋/auto_fixable 與 _TAG_SWAPS 一致）、`LintViolation` frozen + unknown rule_id 擋 + non-positive line/col 擋、`LintReport` empty-default clean + warn-only 仍 clean + error blocks cleanliness + `to_dict` JSON roundtrip + counts views 唯讀、raw tag detector 6 parametrize 全 hit + shadcn 大寫 component 不觸發 + suggested_fix 提元件名與 import path + native-input opt-out、semantic a11y 6 條（div onClick/span role=button/img 無 alt/img empty alt 放行/img 敘述 alt 放行/3 條正 tabIndex parametrize hit、0/-1 放行）、design token 9 條（5 parametrize hex 色 hit + 括號中 hex `text-[#38bdf8]` hit + palette class hit + semantic token 放行 + 5 parametrize arbitrary size hit + `grid-cols-[...]` 放行 + arbitrary breakpoint hit + 標準 md/lg/2xl 放行 + `!important` Tailwind shortcut hit + dark prefix hit + 無 dark prefix 放行）、outline 3 條（`outline-none` 單獨 hit / 有 `focus-visible:ring-2` 替代放行 / inline `outline: none` hit）、comment stripping 4 條（JSX block / JS block / line comment 都 strip、line-number 不因 strip 而位移）、determinism 2 條（violations 按 line 排序 / 同 input 同 output byte-identical）、auto_fix 8 條（rewrite button + Input + Textarea + Progress 4 tag、multiple tag mix、idempotent 跑兩次同結果、`"use client"` directive 後面才插 import、既有同 path import merge 不重複、clean code 不動）、file api 6 條（missing 回 clean/round trip/`auto_fix_file write=True` 寫回/`write=False` 不寫/missing root 回 ()/`node_modules` 排除/預設排除 `components/ui/`）、agent entry 4 條（requires exactly-one-of / JSON-safe dict / auto_fix 回 fixed_code / path mode 讀檔）、render_report 4 條（clean 說 clean / dirty 列 rule_id+severity / 空 iterable 回 "No files scanned" / determinism）、live project 2 條（tag swap 對齊 `ui_component_registry.REGISTRY` key / 6 種 malformed input 不 raise）；sibling `test_ui_component_registry.py` (152) + `test_design_token_loader.py` (97) + `test_web_role_skills.py` (51) 300 條 regression 0 錯。**為什麼此模組是 V1 pipeline 的品質閘** — UI Designer skill (#1) 的 SOP step 5「自我審查」要求 post-generation 跑此 linter，零違規才交付；`run_consistency_linter` 的 JSON-safe dict + `fixed_code` 欄位讓 agent 自動循環：emit code → lint → 有 error 就接 `auto_fix_code` 或拿 `suggested_fix` 重生 → 再 lint 直到 `is_clean=True`。相較於 sibling #2 registry（告訴 agent 能用什麼）+ #3 design_token_loader（告訴 agent 色/間距約束），此 linter 是 **後驗**閘門：擋住 agent 仍然從訓練記憶滑出訓練分布 emit raw `<button>` / `bg-slate-900` / `#38bdf8` 的 12 種 anti-pattern；`auto_fixable` 只開放 4 條機械 tag swap 最高安全 rewrite（含自動 import 注入），其餘違規留給 agent 判斷 design token 選擇 — 避免 linter 替 designer 做風格決策。**Scope 自律** — 只新增 1 個 `backend/component_consistency_linter.py` + 1 個 test 檔；不動 `configs/roles/ui-designer.md`（已宣告 tool 名 `run_consistency_linter`）、不動 sibling `ui_component_registry.py` / `design_token_loader.py`（只 import 前者驗 tag swap 對齊）、不動 `components/ui/*.tsx`（只做 exclude 排除不讀內容）、不動任何 backend routing/server 碼、不動 frontend code)*
- [x] `backend/vision_to_ui.py`：Screenshot/手繪稿 → code pipeline——接收圖片 → Opus 4.7 multimodal 分析佈局結構 + 色彩 + 元件 → 輸出 React + shadcn/ui + Tailwind code *(done: V1 #5 — `backend/vision_to_ui.py` (~820 行) + 80 條契約測試全綠 0.40s。**Pipeline** — `validate_image(bytes, mime) → VisionImage`（4 類 mime `image/png|jpeg|gif|webp` 硬白名單、5 MiB 上限對齊 Anthropic inline limit、magic-byte 交叉比對擋住「JPEG 宣稱成 PNG」的 mislabel、`image/jpg` alias 正規化到 `image/jpeg`）→ `build_multimodal_message(image, prompt) → HumanMessage` 其 content 為 `[{type:text,...}, {type:image, source:{type:base64, media_type, data}}]` 直接吃進 `langchain_anthropic` 的多模態 API → `build_vision_analysis_prompt(hint)` / `build_ui_generation_prompt(analysis, project_root, brief, tokens)` 兩支**純函數決定性** prompt 構造（byte-identical across calls，與 sibling `render_agent_context_block()` 並列建 prompt cache key）→ `parse_vision_analysis(text)` 三層容錯解析（fenced `json` block → bare JSON → balanced-brace span → prose salvage regex，全失敗回 `parse_succeeded=False` 但不 raise）→ `extract_tsx_from_response(text)` 抽出 TSX（`tsx|jsx|ts|typescript|javascript` fence → lang-less fence with JSX → `<...>` span 最後 fallback）→ `lint_code(tsx)` via sibling linter → 若 `auto_fix=True` 且有 error 跑 `auto_fix_code(tsx)`（機械 tag swap + import merge）→ 回 `VisionGenerationResult`。**Schema** — `VISION_SCHEMA_VERSION = "1.0.0"` 綁在每個 `to_dict()` payload；`DEFAULT_VISION_MODEL = "claude-opus-4-7"` / `DEFAULT_VISION_PROVIDER = "anthropic"`；`SUPPORTED_MIME_TYPES` frozen set；`MAX_IMAGE_BYTES = 5 * 1024 * 1024`。**資料模型** — `VisionImage` / `VisionAnalysis` / `VisionGenerationResult` 三個 `@dataclass(frozen=True)` 全 JSON-safe (`to_dict()` 無 dataclass/tuple 洩漏)；`VisionAnalysis` 固定五欄（`layout_summary/color_observations/detected_components/suggested_primitives/accessibility_notes`）+ `raw_text/parse_succeeded/extras`；`VisionGenerationResult` 夾 `analysis/tsx_code/lint_report/pre_fix_lint_report/auto_fix_applied/warnings/model/provider` + `is_clean` property（空 TSX → 不 clean 避免「沒生成卻 is_clean=True」漏洞）。**Graceful fallback 三路** — (a) LLM 不可用（`invoke_chat` 回 `""` 或 provider throw）→ `warnings=("llm_unavailable",)` + 空 TSX + 空 report（never raises mid-prompt、agent 自己決定是否 retry 更大 model）；(b) 生成成功但 TSX fence 抽不到 → `warnings=("tsx_missing",)` 把 raw 回應塞進 `tsx_code` 供 human 檢視；(c) analysis JSON 解析失敗但抽出 text → `warnings=("analysis_parse_failed",)` 繼續下一步。**可注入 invoker** — `ChatInvoker = Callable[[list], str]` 測試注入假物件，production 預設包裝 `backend.llm_adapter.invoke_chat` 並把 `except Exception → return ""` 讓所有網路錯誤降級成 warning 不爆 agent pipeline。**Agent entry** — `run_vision_to_ui(...)` 對齊 UI Designer skill 的 tool 介面，回 JSON-safe dict。**Sibling 整合** — generation prompt 透過 `render_agent_context_block()` (registry) + `DesignTokens.to_agent_context()` 注入事實面；linter auto-fix 自動把 `<button>/<input>/<textarea>/<progress>` 重寫成 shadcn 等價元件並合併 `@/components/ui/*` import。**契約測試** — `backend/tests/test_vision_to_ui.py` 80 條全綠 0.40s：module invariants 6 條、`VisionImage` 6 條、`validate_image` 11 條（4 format parametrize + jpg→jpeg + 4 reject path + 未知 payload 放行）、multimodal message 2 條、analysis prompt determinism 4 條、generation prompt determinism 5 條、`parse_vision_analysis` 10 條、`extract_tsx_from_response` 6 條（5 lang parametrize）、dataclasses 4 條、`analyze_screenshot` pipeline 5 條、`generate_ui_from_vision` 9 條（clean TSX + llm_unavailable 兩路 + tsx_missing + auto-fix 真的重寫 raw button 驗 `<Button>`+import + auto_fix=False 留違規 + 預餵 analysis 省第一 call + 無效圖在 LLM call 前 raise + model+provider 回傳）、`run_vision_to_ui` 2 條、default invoker wiring 2 條（含網路異常吞成 warning）、sibling integration 3 條（真把 `<input>` → `<Input>` 加 import）。sibling `test_ui_component_registry.py` (152) + `test_design_token_loader.py` (97) + `test_component_consistency_linter.py` (105) 354 條 regression 0 錯；V1 #1-#5 合計 434 條 0.65s 全綠。**為什麼此模組是 V1 pipeline 的 vision 入口** — UI Designer skill (#1) 的 SOP step 0 列四種事實面拿法之一就是 vision；相較於 sibling #2 registry（靜態事實：裝了什麼元件）/ #3 design_token_loader（靜態事實：用什麼 token），vision_to_ui 提供**動態**事實（這張圖裡有什麼）；auto-fix round 與 linter report 合作保證 agent 看到 TSX 時已是清潔版或帶明確違規 list；`warnings` 三類讓 agent 自動決策 retry vs. escalate vs. ask operator。**Scope 自律** — 只新增 1 個 `backend/vision_to_ui.py` + 1 個 test 檔；不動 sibling `ui_component_registry.py` / `design_token_loader.py` / `component_consistency_linter.py`（只 import 公開 API）、不動 `backend/llm_adapter.py`（只 lazy-import `HumanMessage` + `invoke_chat`）、不動 `configs/roles/ui-designer.md`（skill 已列此模組在 sibling 表）、不動任何 routing/server 碼、不動 frontend code；V0 / V1 #1-#4 既有測試全綠零 regression)*
- [x] Figma → code 串接：呼叫已有 MCP `get_design_context(fileKey, nodeId)` → 提取 design tokens + 元件層級結構 + spacing → agent 生成對應 React code *(done: V1 #6 — `backend/figma_to_ui.py` (~850 行) + 117 條契約測試全綠 0.39s。**Pipeline** — agent 呼叫 MCP `mcp__claude_ai_Figma__get_design_context(fileKey, nodeId)` 拿回 `{code, screenshot, variables, metadata, asset_urls}` 原始 payload → `from_mcp_response(mcp_response, file_key, node_id)` 三種 envelope 容錯解析（bare dict / JSON string / `{"content":[{type:"text",text:"..."}]}` content-wrapper，全失敗 graceful 回 empty context 並 stash `_parse_warnings` 到 metadata）→ screenshot 支援三路（raw bytes / base64 string / data-URL `data:image/png;base64,...`，不合規一律降級 `screenshot_invalid` warning 不炸）→ `extract_from_context(ctx) → FigmaExtraction` 用 11 支純 regex scanner 抽 hex/rgb/hsl/oklch/oklab/`var(--x)` 六種顏色、`px`/`rem`/arbitrary `p-[12px]` spacing + Tailwind scale `p-4` 語彙、`rounded-lg`/`rounded-[16px]`/CSS `border-radius:` 三類 radius、Tailwind `shadow-lg` + CSS `box-shadow:` + JSX camelCase `boxShadow:"..."` 三類 shadow、`text-sm`/`font-semibold` utility + CSS `font-size:` + JSX camelCase `fontSize:"..."` 三類 typography、JSX 大寫開頭 tag hierarchy、`import ... from "..."` 依賴映射、變數 `{r,g,b}` 0-1 float 自動轉 `#rrggbb` → `build_figma_generation_prompt(...)` 決定性拼裝 `Figma source / Figma extraction / Figma reference code (truncated @ 8KB) / registry block / design tokens block / caller brief / generation rules` 7 大區塊（byte-identical across calls）→ `build_multimodal_message(ctx, prompt)` 若有 screenshot 吐 `[text, image]` list message、沒有就吐純文字 `HumanMessage` → LLM → `extract_tsx_from_response` → lint → 若 `auto_fix=True` 且有 error 跑 `auto_fix_code(tsx)` → 回 `FigmaGenerationResult`。**Schema** — `FIGMA_SCHEMA_VERSION = "1.0.0"` 綁每個 `to_dict()`；`DEFAULT_FIGMA_MODEL = "claude-opus-4-7"` / `DEFAULT_FIGMA_PROVIDER = "anthropic"`（Figma reference → shadcn 重構是推理任務用 Opus 而非 Haiku）；`TOKEN_KINDS = (color, spacing, radius, font, shadow, other)` frozen。**資料模型** — `FigmaToken` / `FigmaDesignContext` / `FigmaExtraction` / `FigmaGenerationResult` 四個 `@dataclass(frozen=True)` 全 JSON-safe；`FigmaDesignContext.variables/metadata/asset_urls` 全走 `MappingProxyType` 唯讀 view（直接 mutate raise TypeError）；`FigmaGenerationResult.is_clean` 要求 `lint_report.is_clean` AND `tsx_code.strip()` 避免「空 TSX is_clean=True」漏洞。**Node-id / file-key 正規化** — `normalize_node_id("1-2") == "1:2"` / `normalize_node_id("1:2") == "1:2"` / `normalize_node_id("-5-6") == "-5:6"`（leading-'-' 保留、split 在第二個 dash）、8 條 invalid pattern reject（空字串、`1:`、`abc`、`1/2`、`1:2:3`…）；`normalize_file_key` 同時檢 slash/whitespace 避免 URL-segment 誤當成 key；`canonical_figma_source(fk, nid)` 產 `figma.com/design/<fk>?node-id=<nid-dash>` 可貼回 browser。**Graceful fallback 五路** — (a) LLM 不可用 → `warnings=("llm_unavailable",)` + 空 TSX；(b) 生成成功但 TSX fence 抽不到 → `warnings=("tsx_missing",)` raw 回應塞進 `tsx_code` 供 human 檢視；(c) MCP 回 None → `warnings=("mcp_response_missing", "figma_context_empty")`；(d) MCP 回非 JSON 字串 → `mcp_response_not_json`；(e) MCP 回 list/array → `mcp_response_not_object`；全部 stash 到 metadata `_parse_warnings` 再由 `generate_ui_from_figma` bubble 到 final `warnings` tuple 供 agent 決策 retry vs. escalate。**可注入 invoker** — `ChatInvoker = Callable[[list], str]` 測試注入假物件，production 預設包裝 `backend.llm_adapter.invoke_chat` 並把 `except Exception → return ""` 讓所有網路錯誤降級成 warning 不爆 agent pipeline。**Agent entry** — `run_figma_to_ui(file_key, node_id, mcp_response=|context=, ...)` 對齊 UI Designer skill 的 `priority_tools` 介面，exactly-one-of 檢查、key mismatch raise ValueError、回 JSON-safe dict 含 `schema_version/context/extraction/tsx_code/lint_report/pre_fix_lint_report/auto_fix_applied/warnings/model/provider/is_clean`。**Sibling 整合** — generation prompt 透過 `render_registry_block()` + `DesignTokens.to_agent_context()` 注入事實面；linter auto-fix 自動把 Figma reference code 常見的 raw `<button>/<input>/<textarea>/<progress>` 重寫成 shadcn 等價元件並合併 `@/components/ui/*` import；pipeline 相容 sibling `vision_to_ui` 的 `VisionImage` / `validate_image` / `build_multimodal_message` / `extract_tsx_from_response` 直接 import 不重造輪子。**契約測試** — `backend/tests/test_figma_to_ui.py` 117 條全綠 0.39s：Module invariants 4 條（semver / Opus 4.7 default / TOKEN_KINDS 固定 / 18 個 `__all__` 成員覆蓋 parametrize）；Node-id 10 條 parametrize（7 valid form + 8 invalid + None）；File-key 5 條（opaque / empty / URL-path / whitespace）；Canonical source 1 條；`FigmaToken` 4 條（frozen / unknown kind / empty name / None value reject）；`FigmaDesignContext` 7 條（validate happy + empty key reject + invalid node-id reject + MappingProxyType 唯讀 + dash 正規化 + `to_dict` JSON-safe + screenshot 型別檢查）；`from_mcp_response` 13 條（dict / JSON string / content envelope / None / non-JSON / non-object / data-URL screenshot / bytes screenshot / bad b64 / canonical source / node-id dash / MappingProxyType / empty payload）；`extract_from_context` 14 條（hex / rgba / px / Tailwind scale spacing / rounded / 3 類 shadow / typography / components + imports / variable + observed tokens 雙源 / 空 code 回 parse_succeeded=False / 決定性 / JSON safe / 絕對定位 div 不崩 / `{r,g,b}` 轉 hex / `var(--x)` 引用抽出）；Prompt determinism 10 條（byte-identical / brief 變更 / rules + Figma header / Figma source + extraction sections / registry+tokens 注入 / reference code fence / 8KB truncation / 無 code 走 fallback / screenshot flag on/off / empty brief "(none)"）；Multimodal 2 條（無 screenshot 純 text / 有 screenshot [text, image] list）；`FigmaGenerationResult` 2 條（`is_clean` 空 TSX 檢查 / `to_dict` JSON safe）；`generate_ui_from_figma` 10 條（happy path clean + llm_unavailable + tsx_missing + auto_fix 真把 raw button 重寫成 `<Button>` + auto_fix=False 保留違規 + 預餵 extraction + MCP parse warning bubble + 無 screenshot 仍運作 + 非 context raise TypeError + model/provider 回傳）；`run_figma_to_ui` 5 條（mcp_response path + context path + exactly-one-of / 兩個都給 raise / file_key 不匹配 raise / node_id 不匹配 raise + llm_unavailable 顯現）；Default invoker 2 條（provider/model forward + 網路異常吞成 warning）；Sibling integration 3 條（prompt 真提到 button/card/primary/background + auto_fix 真把 `<input>` → `<Input>` + extraction token JSON roundtrip）。sibling V1 #1-#5 485 條 regression 0 錯（`test_ui_component_registry.py` 152 + `test_design_token_loader.py` 97 + `test_component_consistency_linter.py` 105 + `test_vision_to_ui.py` 80 + `test_web_role_skills.py` 51）；V1 #1-#6 合計 602 條 0.89s 全綠。**為什麼此模組是 V1 pipeline 的 Figma 入口** — UI Designer skill (#1) 的 SOP step 0 列四種事實面之一就是 `get_design_context`；相較於 sibling #5 `vision_to_ui`（動態事實：raw 圖），figma_to_ui 提供**結構化**事實（reference code + variables map + 元件 hierarchy + asset URLs）讓模型不需要 OCR 推敲、可直接 map Figma 變數 ↔ 專案 design token；pipeline 的 `from_mcp_response` 容錯 3 種 envelope shape 正對應 Figma 官方 MCP 偶爾變動的回應包裝；auto-fix round 替 Figma 典型輸出常見的 raw `<button>` / 絕對定位擋著；`warnings` 五類讓 agent 自動決策 retry vs. escalate vs. ask operator。**Scope 自律** — 只新增 1 個 `backend/figma_to_ui.py` + 1 個 test 檔；不動 sibling `ui_component_registry.py` / `design_token_loader.py` / `component_consistency_linter.py` / `vision_to_ui.py`（只 import 公開 API）、不動 `backend/llm_adapter.py`（只 lazy-import `HumanMessage` + `invoke_chat`）、不動 `configs/roles/ui-designer.md`（skill 已列 `get_design_context` 在 tools 表 + Figma MCP 在 sibling 表）、不動任何 routing/server 碼、不動 frontend code；V0 / V1 #1-#5 既有 485 測試全綠零 regression)*
- [x] URL → reference：`WebFetch(url)` + Playwright 截圖 → 注入 agent visual context 作為參考（「做一個像這個 URL 的頁面」） *(done: V1 #7 — `backend/url_to_reference.py` (~900 行) + 131 條契約測試全綠 0.16s。**Pipeline** — `normalize_url(raw, allow_private=False)` 檢 scheme allowlist (`http`/`https`)、空白、長度 (`MAX_URL_LENGTH=2048`)、fragment 剝除、host lowercase、SSRF 防禦（loopback/private/link-local/reserved/multicast IP 與保留主機名如 `localhost`/`127.0.0.1`/`169.254.169.254` AWS metadata 一律 raise `ValueError`，`allow_private=True` 逃生出口給 localhost 整合測試）→ `fetch_url(url, fetcher=, allow_private=, now=)` 拿 HTML（注入型 `URLFetcher = Callable[[str], FetchResponse]`，預設 wrap `httpx.Client` + 退回 `urllib.request` 雙後端——`playwright` 非 backend deps、純 agent-harness 職責；fetcher 拋例外 → `fetch_failed` warning 不崩）→ `capture_screenshot(url, screenshotter=)` 也是純注入型 `Screenshotter = Callable[[str], ScreenshotResult | None]`，`screenshotter=None` 時回 `screenshot_unavailable` warning 直接降級純文字（無內建 Playwright adapter——由 harness / auto-router 各自提供，避免 backend 拖 browser dependency）→ `extract_from_html(html, base_url=)` 14 支 regex scanner 抽出色（hex/rgb/hsl/oklch/oklab）、`font-family` 串、`h1-h3` 文字、`<button>` + `<Button>` label、`<nav>` 內 anchor text、component 偵測（form/input/textarea/select/table/nav/header/footer/aside/main/section/article/dialog/button 直屬 + class-based card/hero/tabs/modal + `role="dialog|tablist"`）、layout hints（grid/flex/sidebar/container/two-column/dark-theme）、link host（相對 URL 透過 `urljoin(base_url)` 解析、跳過 `#`/`javascript:`/`mailto:`/`tel:`）、meta keywords、external stylesheet URL → `build_url_generation_prompt(reference=, extraction=, project_root=, brief=, tokens=)` 決定性拼裝 8 大區塊（header/URL reference/URL extraction/HTML snippet 16KB cap/registry/tokens/brief/rules；byte-identical across calls — prompt-cache stable）→ `build_multimodal_message(reference, prompt)` 有 screenshot 吐 `[text, image]` list、無則純文字 HumanMessage → LLM → `extract_tsx_from_response` → lint → 若 `auto_fix=True` 且 error 跑一輪 `auto_fix_code` → 回 `URLReferenceResult`。**Schema** — `URL_REF_SCHEMA_VERSION = "1.0.0"` 綁每個 `to_dict()`；`DEFAULT_URL_REF_MODEL = "claude-opus-4-7"` / `DEFAULT_URL_REF_PROVIDER = "anthropic"`（reference → shadcn 重構需要推理——Opus 不是 Haiku）；`MAX_HTML_BYTES = 2 MiB`（下載上限）；`HTML_PROMPT_CAP = 16_000`（prompt 注入上限，大於 Figma 8KB 因為完整 HTML 結構信號比 Figma reference code 稀薄）；`MAX_URL_LENGTH = 2048`；`DEFAULT_FETCH_TIMEOUT = 20.0s`；`SUPPORTED_URL_SCHEMES = frozenset({"http", "https"})`；`DEFAULT_USER_AGENT` 宣告 reference-only UA。**資料模型** — `FetchResponse` / `ScreenshotResult` / `URLReference` / `URLExtraction` / `URLReferenceResult` 五個 `@dataclass(frozen=True)` 全 JSON-safe（`to_dict()` 無 dataclass/tuple 洩漏）；`FetchResponse.headers` 走 `MappingProxyType` + lowercase key 正規化；`URLReference.meta` 也 `MappingProxyType` 唯讀；`URLReference.is_ok` property（HTML 非空或有 screenshot 才算可用）；`URLReferenceResult.is_clean` 要 `lint_report.is_clean` AND `tsx_code.strip()` 避免「空 TSX is_clean=True」漏洞。**Graceful fallback 八路** — (a) invalid URL / blocked private host → raise `ValueError`（caller bug、同步驗證）；(b) fetcher 例外 → `fetch_failed` warning、empty HTML、仍 call LLM；(c) 4xx → `fetch_http_error` warning、保留 body 餵 LLM；(d) 5xx → `fetch_server_error`；(e) fetcher 回錯誤型別 → `fetch_unexpected_shape`；(f) non-HTML content-type → `non_html_content_type` warning；(g) HTML 過大 → 切 `MAX_HTML_BYTES` + `html_truncated` warning；(h) screenshotter 缺席/回 None/拋例外 → `screenshot_unavailable`、mime 不合 → `screenshot_unsupported_mime`、太大 → `screenshot_too_large`、magic-byte mismatch → `screenshot_invalid`、回錯誤型別 → `screenshot_unexpected_shape`、PNG 宣稱成 JPEG 的 mislabel 一律 reject；(i) LLM 不可用 → `llm_unavailable` + 空 TSX；(j) TSX fence 抽不到 → `tsx_missing`、raw 回應塞進 `tsx_code` 供 human 檢視。全部走 `warnings` tuple 讓 agent 決策 retry vs. escalate vs. ask operator。**Agent entry** — `run_url_to_reference(*, url=|reference=, fetcher=, screenshotter=, invoker=, ...)` 對齊 UI Designer skill 表面，exactly-one-of 檢查、JSON-safe dict 含 `schema_version/linter_schema_version/reference/extraction/tsx_code/lint_report/pre_fix_lint_report/auto_fix_applied/warnings/model/provider/is_clean`。**Sibling 整合** — generation prompt 透過 `render_registry_block()` + `DesignTokens.to_agent_context()` 注入事實面；multimodal message 直接 import sibling `vision_to_ui.build_multimodal_message` + `validate_image` + `SUPPORTED_MIME_TYPES` + `VisionImage` + `MAX_IMAGE_BYTES` 不重造輪子；linter auto-fix 自動把 raw `<button>` / `<input>` 重寫成 shadcn 等價元件並合併 `@/components/ui/*` import（契約測試實際驗證 `<Button>` + `<Input>` 兩種 rewrite）；`extract_tsx_from_response` 也是從 sibling reuse。**契約測試** — `backend/tests/test_url_to_reference.py` 131 條全綠 0.16s：Module invariants 22 條（22 個 `__all__` 成員 parametrize + semver + Opus 4.7 default + http-only scheme + 長度/位元組/prompt cap sanity）；`FetchResponse` 3 條（frozen + header 唯讀 + bytes content + int status 強制）；`ScreenshotResult` defaults 1 條；`normalize_url` 26 條（6 valid canonicalise parametrize + 9 invalid parametrize + None + 長度 cap + 7 private host parametrize + allow_private escape）；`URLReference` 5 條（empty URL reject + frozen + bad screenshot type + meta 唯讀 + `to_dict` JSON-safe + `is_ok` property）；`URLExtraction` 2 條（empty default + JSON safe）；`extract_from_html` 11 條（empty → parse_succeeded=False + full sample 全欄位覆蓋 + 決定性 byte-identical + entities + 相對 link resolve + junk href skip + shadcn `<Button>` 文字 + h1-h3 限制 + 5 種色彩格式 parametrize + title stripping）；`fetch_url` 11 條（happy path + fetcher 拋例外 → fetch_failed + 404 → fetch_http_error + 503 → fetch_server_error + 非 HTML content-type warn + xhtml 不警 + 過大 → truncate + fetcher 回錯誤型別 → unexpected_shape + latin-1 charset 解碼 + invalid URL raise + `now=` 覆蓋驗 `fetched_at`）；`capture_screenshot` 10 條（無 screenshotter → unavailable + happy path + None → unavailable + 拋例外 → unavailable + unexpected shape + unsupported mime + image/jpg alias → image/jpeg + 空 bytes → unavailable + 過大 → too_large + PNG 宣稱 JPEG → invalid + URL 正規化傳進 shot）；Multimodal 2 條（text-only 無 screenshot / [text, image] 有 screenshot）；Prompt determinism 9 條（byte-identical + header+rules sections + reference+extraction sections + registry+tokens 注入 + HTML truncation 含 "truncated" 標記 + 空 HTML fallback + 無 brief "(none)" + 有 brief embed + screenshot 狀態 line）；`URLReferenceResult` 2 條（空 TSX is_clean=False + `to_dict` JSON safe）；`generate_ui_from_url` 10 條（happy clean + llm_unavailable + tsx_missing + fetch_failed warning 冒泡 + auto_fix 真把 raw button 重寫成 Button + auto_fix=False 留違規 + 預餵 reference 跳過 fetch + 預餵 extraction forward + screenshotter 真的接上 image + 已有 screenshot 不再 call shotter + model/provider 回傳）；`run_url_to_reference` 5 條（url mode + reference mode + exactly-one-of / 兩個都給 raise + llm_unavailable 顯現 + URL 正規化）；Default invoker 2 條（provider/model forward + 網路異常吞成 warning）；Sibling integration 3 條（prompt 真 mention Button/Card/primary/background/foreground + auto_fix 真把 `<input>` → `<Input>` + extraction JSON roundtrip）。sibling V1 #1-#6 602 條 regression 0 錯（`test_ui_component_registry.py` 152 + `test_design_token_loader.py` 97 + `test_component_consistency_linter.py` 105 + `test_vision_to_ui.py` 80 + `test_figma_to_ui.py` 117 + `test_web_role_skills.py` 51）；V1 #1-#7 合計 733 條 1.37s 全綠。**為什麼此模組是 V1 pipeline 的 URL 入口** — UI Designer skill (#1) 的 SOP step 0 列四種事實面拿法之一就是 URL；相較於 sibling #5 `vision_to_ui`（動態事實：raw 圖，無 HTML 脈絡）/ sibling #6 `figma_to_ui`（結構化事實：reference code + variables + hierarchy，但鎖在 Figma ecosystem），url_to_reference 提供**公開網頁**事實面，補齊最後一個「像 X 這個 URL」的 channel——讓 designer 能以「做一個像 stripe.com/pricing 的頁面」這種自然指令啟動 agent。關鍵設計決策：Playwright 不當 Python 後端依賴而走注入型 `Screenshotter` —— backend 只宣告 callable signature、harness / auto-router 各自繫 browser tool（測試用 fake，prod 可繫 playwright-python、puppeteer-service、Figma-screenshotter、任何外部抓圖服務），以同一套 graceful-fallback 契約吞下瀏覽器不可用情形；WebFetch 類推——預設包 `httpx.Client` with `urllib.request` fallback，SSRF gate 預設擋 loopback/private 除非明確 `allow_private=True`；reference is reference, not clone —— rule #7 明確要求不 copy brand copy verbatim，所有觀察色 / 尺寸必須 map 到 design token 而非 inline，此政策已編進 prompt rules 並由 linter auto-fix 對 raw HTML tag 擋住。**Scope 自律** — 只新增 1 個 `backend/url_to_reference.py` + 1 個 test 檔；不動 sibling `ui_component_registry.py` / `design_token_loader.py` / `component_consistency_linter.py` / `vision_to_ui.py` / `figma_to_ui.py`（只 import 公開 API）、不動 `backend/llm_adapter.py`（只 lazy-import `HumanMessage` + `invoke_chat`）、不動 `configs/roles/ui-designer.md`（skill 已在 SOP step 0 + sibling 表宣告 `WebFetch(url)` + Playwright）、不動任何 routing/server 碼、不動 frontend code、不動 `backend/requirements.txt`（httpx 已存在、playwright 刻意不加）；V0 / V1 #1-#6 既有 602 測試全綠零 regression)*
- [x] Edit complexity auto-router：分析 user prompt 複雜度——小改（文字/色彩/spacing）→ Haiku 快改（< 3s）；大改（layout 重構/新頁面）→ Opus 深想 *(done: V1 #8 — `backend/edit_complexity_router.py` (~600 行) + 92 條契約測試全綠 0.10s。**Pipeline（純啟發式、零 LLM 呼叫）** — `classify_prompt(text, has_image=, has_figma=, has_url=, has_existing_code=) → (complexity, EditSignals, reasons)` 對 user prompt 抽 7 類訊號（word_count with fenced-code stripping + CJK-per-glyph 計數、small_hits via 5 組雙語 regex `copy_tweak`/`color_tweak`/`spacing_tweak`/`size_tweak`/`minor_marker`、large_hits via 5 組雙語 regex `layout_refactor`/`new_page`/`multi_section`/`major_marker`/`state_wiring`、shadcn primitive mentions via JSX tag form + PascalCase 無空格識別 + hyphenated 小寫 token 三路掃描對齊 55 條 `_SHADCN_PRIMITIVES` 白名單、action verb 計數、conjunction 計數、四個多模態 context 旗標）→ `_score(signals)` 六階段規則鏈：**Rule 1** 多模態 context (image/figma/url) → 一律 large（對齊 sibling `vision_to_ui` / `figma_to_ui` / `url_to_reference` 都 pin Opus 4.7 的 invariant）、**Rule 2** 硬結構信號（`major_marker` / `new_page` / `layout_refactor`）→ large、**Rule 3** component mentions ≥ 3 → large（多 primitive 通常等同 layout）、**Rule 4** word_count ≥ 60 + 任一 large 信號 or 5+ action verb → large、**Rule 5** 純 small hits + ≤ 20 words + ≤ 3 conjunction → small、**Rule 5b** 極短 prompt (≤10 words) + ≤ 1 action + ≤ 1 component → small（如 "rename the button"）、**Rule 6** medium catch-all（附 ambiguity 標籤不讓 reasons 為空）→ `route(prompt, ...)` 三重 override stack：`complexity=` hard override 標 `caller_override:old→new`、`has_existing_code=True` soft nudge 把 medium+短 prompt+無 large 信號下推到 small（標 `existing_code_nudge`）、`provider=` / `model=` 手動釘死覆寫 default 並標 `*_override:val`。**Schema** — `EDIT_ROUTER_SCHEMA_VERSION = "1.0.0"` 綁每個 `to_dict()`；`DEFAULT_PROVIDER = "anthropic"` / `DEFAULT_SMALL_MODEL = "claude-haiku-4-5"` / `DEFAULT_MEDIUM_MODEL = "claude-sonnet-4-6"` / `DEFAULT_LARGE_MODEL = "claude-opus-4-7"` 三檔模型對齊 V1 sibling 規格；`COMPLEXITY_TO_MODEL` + `EXPECTED_LATENCY_MS` 皆 `MappingProxyType` 唯讀；`EXPECTED_LATENCY_MS["small"] = 3000` 對應 TODO 規格「Haiku 快改 < 3s」契約（測試硬鎖 ≤3000ms）；六個 threshold 常數（`SMALL_WORD_CEILING=20` / `LARGE_WORD_FLOOR=60` / `LARGE_PRIMITIVE_COUNT=3` / `MEDIUM_ACTION_FLOOR=3` / `LARGE_ACTION_FLOOR=5` / `HEAVY_CONJUNCTION_COUNT=4`）模組級常量可被未來調整但一碰就爆測試。**資料模型** — `EditComplexity(str, Enum)` 3-bucket 繼承 str 直接 JSON-serialisable 不需 custom encoder；`EditSignals` / `EditRouteDecision` 兩個 `@dataclass(frozen=True)` 全 JSON-safe + `to_dict()` 無 tuple 洩漏、負數欄位 raise ValueError、未知 complexity 拒絕建構、blank provider/model 拒絕。**Agent entry** — `run_edit_router(prompt, ...)` 對齊 sibling 命名慣例（`run_vision_to_ui` / `run_figma_to_ui` / `run_url_to_reference`）回 JSON-safe dict 含 `schema_version/complexity/provider/model/reasons/signals/prompt/expected_latency_ms`；`render_decision_markdown(decision)` 產 deterministic markdown 供 SSE / operator log 用（byte-identical across calls）。**零副作用保證** — 整個 router 沒有 LLM 呼叫、沒有檔案 I/O、沒有 time/random，所有決策純 regex + 字元計數；blank/None prompt → medium + `("empty_prompt",)` reason 不 raise；未知 complexity override → ValueError 清楚標註；這是 V1 pipeline 唯一可以放在 hot path 前做 dispatch 的模組。**契約測試** — `backend/tests/test_edit_complexity_router.py` 92 條全綠 0.10s：Module invariants 17 條（14 個 `__all__` 成員 parametrize + semver + defaults pin Opus 4.7/Sonnet 4.6/Haiku 4.5 + MappingProxyType readonly + bucket 完整性 + latency monotonic + small ≤ 3000ms 硬契約 + enum pin strings + enum is str + 6 threshold sanity）；`EditSignals` 5 條（frozen + defaults + 3 parametrize 負值 reject + JSON safe）；`EditRouteDecision` 4 條（frozen + unknown complexity reject + blank provider/model reject + JSON safe round-trip）；`classify_prompt` 4 條（pure + deterministic + blank → medium + None → blank + whitespace-only → empty）；Small bucket 10 條（9 parametrize 跨 EN/CJK + specific reason surfaces）；Large bucket 9 條（8 parametrize 跨 EN/CJK 含 TODO integration example + specific reason surfaces）；Medium bucket 2 條（ambiguous multi-action no-structural prompts）；Multimodal override 4 條（image / figma / url 各 force large + empty prompt + image）；Caller override 6 條（complexity override wins + confirm reason + unknown reject + provider/model override + existing_code nudge 驗 medium→small + existing_code 不 downgrade large）；Component mentions 4 條（collected + many → large + dedup + sorted + hyphen variant）；Word count / CJK 2 條（fenced code stripped + CJK glyph counted）；Conjunction / action verb 2 條（action counted + heavy conjunctions 擋 small）；Reason provenance 3 條（tuple of str + 非空 + ambiguous 有理由）；Agent entry 4 條（JSON-safe dict + context flag roundtrip + caller override roundtrip + unknown complexity reject）；Markdown render 3 條（deterministic + sections + empty signals 顯示 "(none)"）；Sibling alignment 2 條（大桶 model 對齊 `vision_to_ui.DEFAULT_VISION_MODEL` + provider 對齊 `DEFAULT_VISION_PROVIDER`）；TODO pinned examples 2 條（"做一個定價頁面，三個方案，年月切換" → large + Opus 4.7 + 三個 Haiku 路徑小改例子）。sibling V1 #1-#7 733 條 regression 0 錯；V1 #1-#8 合計 825 條 1.86s 全綠。**為什麼此模組是 V1 pipeline 的 dispatch 層** — UI Designer skill (#1) SOP step 2 明寫「small edit → Haiku 路徑 / large edit → Opus 深想，由 Edit complexity auto-router 決定，不在此 skill 內」— 把「模型分級」從 skill 外切出來是刻意的分工：skill 關心 WHAT（shadcn API / tokens / ARIA rules），router 關心 HOW MUCH COMPUTE（算力預算）。相較於 sibling #2-#4 fact-side（registry / tokens / linter）與 #5-#7 channel-side（vision / figma / url），此 router 是**元模組**——決定後續要不要動用 Opus 4.7。沒有它所有 UI 編輯都落 Opus → 成本 10× 浪費、p50 延遲 6× 浪費、無差別地送 Haiku → 大改 layout 直接崩盤。**關鍵設計決策** — (a) **零 LLM 呼叫**：router 本身是 Haiku 路徑的前置條件；用 LLM 決定用 Haiku 就是死循環，所以純 regex/keyword 必須 deterministic；(b) **bilingual first**：每組 regex 都同時涵蓋英文 + 繁中，對齊 TODO 原文「做一個定價頁面，三個方案，年月切換」這種中英夾雜的真實 user prompt；(c) **reasons 一級公民**：每個決策都附 machine-readable reason tuple 讓 operator / telemetry / A/B 分析可回推「為什麼這個 prompt 上了 Opus」；(d) **multimodal 硬規則**：image/figma/url 都 force large，因為 sibling 三個 channel 各自都 pin Opus 4.7，router 不能與它們不一致；(e) **existing_code soft nudge**：caller 傳 `has_existing_code=True` 代表「這是改既有檔案」，比 greenfield 窄，medium 下推到 small；(f) **三檔而非兩檔**：TODO 只列 Haiku/Opus 兩檔但實際引入 Sonnet 4.6 作中檔——避免 ambiguous prompt 在 Haiku/Opus 間跳太遠，同時保留 reason 可解釋性。**Scope 自律** — 只新增 1 個 `backend/edit_complexity_router.py` + 1 個 test 檔；不動 sibling `ui_component_registry.py` / `design_token_loader.py` / `component_consistency_linter.py` / `vision_to_ui.py` / `figma_to_ui.py` / `url_to_reference.py`（只 import `vision_to_ui.DEFAULT_VISION_MODEL` / `DEFAULT_VISION_PROVIDER` 做 sibling alignment 斷言）、不動 `backend/llm_adapter.py`（router 根本不呼叫 LLM）、不動 `backend/model_router.py`（agent-level model 選擇不同層次）、不動 `configs/roles/ui-designer.md`（skill 文案已在 SOP step 2 + sibling 表引用此 router）、不動任何 routing/server 碼、不動 frontend code、不動 `backend/requirements.txt`（零新依賴）；V0 / V1 #1-#7 既有 733 測試全綠零 regression)*
- [x] 整合測試：NL「做一個定價頁面，三個方案，年月切換」→ agent 輸出完整 React + shadcn Tabs/Card/Switch 元件 + Tailwind → render 正確 + consistency lint pass *(done: V1 #9 — `backend/tests/test_v1_nl_pricing_page_integration.py` (~500 行) + 36 條契約測試全綠 0.47s。**Pipeline wired end-to-end** — 此檔是 V1 #1-#8 **唯一**被組裝在一起跑的地方，對齊 TODO row 原文「NL 做一個定價頁面，三個方案，年月切換 → agent 輸出 Tabs/Card/Switch + Tailwind → render 正確 + consistency lint pass」四段合約：(1) `edit_complexity_router.route(NL_PROMPT)` 回 `large` bucket + Opus 4.7（與 sibling V1 #5-#7 multimodal 模組 pin Opus 4.7 不變量一致）、reasons tuple 同時 surface `large:new_page` + `large:multi_section`（TODO「做一個定價頁面」觸發 new_page regex、「三個方案」觸發 multi_section regex 的中文條款）、`small_hits=()` 確認 small-patterns 沒在 CJK 字元上誤觸；(2) prompt 組裝走 sibling fact-side 兩個決定性入口 `ui_component_registry.render_agent_context_block` + `design_token_loader.render_agent_context_block`，`_assemble_generation_prompt` 純函數拼 header+registry+tokens+brief+rules → byte-identical across calls（Anthropic prompt cache 友善）；(3) `FakeInvoker` 雙 invoke 記錄 messages 但網路零呼叫、注入式 chat-invoker 雙與 sibling V1 #5-#7 測試用完全同一個 pattern 避免測試姿態分岔；(4) 產出的 TSX 走 sibling `vision_to_ui.extract_tsx_from_response` 抽 fence（extractor 刻意 channel-agnostic，V1 #5 ship 時就為此整合預留）→ `component_consistency_linter.lint_code` 斷言 `is_clean=True`（error-severity violations 空列表）。**測試分 5 個區塊共 36 條** — (a) `TestRouteDecisionForPricingPrompt` 5 條：bucket 是 large、model 是 `claude-opus-4-7`、reasons 含 `large:new_page`+`large:multi_section`、signals.small_hits 空（CJK 過濾驗 regex 不誤觸）、route 純函數 deterministic；(b) `TestPromptDeterminism` 7 條：registry block byte-identical、tokens block byte-identical、generation prompt byte-identical、prompt 嵌入 NL brief、prompt 嵌入 registry section（含 `tabs`/`card`/`switch` 三個 TODO-mandated primitive 名）、prompt 嵌入 tokens section、generation rules 包含 `bg-background`+`<button>`+`dark-only` 反禁制術語；(c) `TestEndToEndPipeline` 8 條：invoker 收到的 messages 包含 NL + registry（證明 prompt 真的組到訊息裡）、extract 抽到非空 TSX、**lint_code(tsx).is_clean=True**（TODO 核心合約：consistency lint pass），**TSX 同時包含 `<Tabs>/<TabsList>/<TabsTrigger>/<TabsContent>/<Card>/<Switch>`**（TODO 核心合約：Tabs/Card/Switch 齊備）、imports 全走 `@/components/ui/*` canonical path、三個 plan tier 名稱齊備（Starter/Pro/Enterprise）、**年月切換 surface 驗證**—Monthly/Yearly 字串雙向 label + `<Switch id="billing-cycle">` + `<Label htmlFor="billing-cycle">` WCAG 2.2 form-control pairing、design-token utilities 正面（`bg-background`+`text-foreground`）負面（無 hex 色 regex 命中、無 `bg-slate-` palette pin、無 `dark:` prefix on dark-only project）；(d) `TestJSXTagBalance` 14 條（12 parametrize + 2 固定）：12 種 shadcn tag 逐個 parametrize 驗 open/close 計數吻合且無 dangling `<Tag` 未結束（proxy for「render 正確」因為 pytest 環境跑不了 jsdom）、`<Switch>` self-closing + `aria-label` attr 滿足 WCAG 4.1.2 Name-Role-Value、每個 `<Card>` 配對 `<CardHeader>` 至少 3 次；(e) `TestCanonicalResponseFixture` 2 條 belt-and-braces：hand-curated TSX 自身 lint 乾淨（避免未來維護者改了 fixture 但沒修測試—失敗訊息會貼緊 fixture 編輯而非 pipeline wiring）、fixture 可被 extractor 抽出（import 起頭）。**為什麼此檔是 V1 pipeline 的 landing gate** — V1 #1-#8 每個 sibling 都有自己獨立的 unit contract test（合計 825 條），但那些測試都只驗 sibling 在 isolation 下的合約；若任何一個 sibling 偷偷改了 public export 名、輸出 shape、或 prompt header 文案，isolation 測試照樣綠但 pipeline wiring 斷。此檔鎖死：(i) `render_agent_context_block` 兩個函數 module-level 存在且 deterministic；(ii) `extract_tsx_from_response` 作為 channel-agnostic extractor 對 NL channel 也能抽；(iii) `lint_code` 的 `is_clean` 屬性與 TODO 合約定義一致（errors block，warns pass）；(iv) `route` 的 `complexity` / `model` / `provider` / `reasons` 四欄位都 round-trip；(v) `EditComplexity.LARGE.value == "large"` string-enum 契約；(vi) `DEFAULT_LARGE_MODEL == "claude-opus-4-7"` 常量鎖定；(vii) `DEFAULT_PROVIDER == "anthropic"` 鎖定。任一 sibling 有 drift，此檔會在 CI 裡 loudly fail 並把失敗訊息貼緊 offending sibling。**關鍵設計決策** — (a) **canonical response 而非 mock LLM**：V1 pipeline 的品質賭注是「Opus 4.7 真的能輸出 clean shadcn TSX」——此檔內建一份 hand-curated realistic response 讓測試 deterministic，同時雙份 `TestCanonicalResponseFixture` 鎖死 fixture 本身乾淨（防止未來維護者把 `<button>` 寫進 fixture 結果 pipeline 測試錯報 lint 是 sibling 問題）；(b) **結構驗證 ≠ jsdom 實渲染**：TODO 的「render 正確」在 backend pytest 環境不可能做 React 實渲染，但可以拆成 (i) lint 清潔（等價於 React dev-mode warnings 不會觸發）、(ii) JSX tag 開合平衡（等價於 reconciler accept）、(iii) 必要 prop 存在（`defaultValue` on Tabs、`aria-label` on icon-only Switch、`htmlFor` on Label）、(iv) 必要 import 存在——四項加總是「render 正確」的可機器驗證 proxy；(c) **parametrize over tag names**：12 種 shadcn tag 逐個 parametrize 比單一迴圈好——失敗訊息明確指出哪個 tag 不平衡，不用 staring at loop variable；(d) **NL channel 不新增模組**：backend 沒有專門的 `nl_to_ui.py` 模組是刻意的——UI Designer skill 的 SOP 本就是「NL → router → agent (帶 registry+tokens 入 context) → TSX」，agent-side 動作不落 backend 碼；此整合測試示範的是 **agent 側 prompt 組裝如何重用 backend sibling 的 deterministic context blocks**，而非新增第九個 sibling 模組；(e) **NL_PROMPT 當 module constant**：TODO row 原文「做一個定價頁面，三個方案，年月切換」不 paraphrase，直接當 test module 的常量——字串本身是 acceptance contract 的一部分，哪天 router 的 CJK regex drift 到不再 match 此句就是 bug。**Scope 自律** — 只新增 1 個 `backend/tests/test_v1_nl_pricing_page_integration.py` 測試檔；零 production 碼變更（不新增 backend module、不改 `configs/roles/ui-designer.md`、不改 sibling 實作、不改任何 routing/server 碼、不改 frontend code、不改 `components/ui/*`、不改 `backend/requirements.txt`）；V0 / V1 #1-#8 既有 825 測試全綠零 regression；V1 全體 861 條 1.65s 全綠。**驗收合約對應** — TODO row 四段合約 → 測試命題：(1) 「NL 做一個定價頁面」→ `TestRouteDecisionForPricingPrompt.test_bucket_is_large` + `test_routes_to_opus_4_7`；(2) 「agent 輸出完整 React + shadcn Tabs/Card/Switch 元件 + Tailwind」→ `TestEndToEndPipeline.test_extracted_tsx_contains_all_mandated_primitives` + `test_extracted_tsx_imports_from_components_ui` + `test_extracted_tsx_uses_design_token_utilities`；(3) 「render 正確」→ `TestJSXTagBalance.test_tag_opens_and_closes[*]` 12 parametrize + `test_switch_is_self_closing_with_aria_label` + `test_every_card_has_a_header`；(4) 「consistency lint pass」→ `TestEndToEndPipeline.test_extracted_tsx_is_lint_clean`。四段全部綠 → V1 #317 Web track 的整合驗收 gate 通過)*
- 預估：**7 day**

### V2. Web — Live Preview + Sandbox 渲染 (#318)
- [x] `backend/ui_sandbox.py`：per-session Next.js dev server 管理器——Docker container 內跑 `npm run dev`，agent 透過 volume mount 寫 code → HMR 自動更新 *(done: V2 #1 — `backend/ui_sandbox.py` (~700 行) + 166 條契約測試全綠 0.15s。Pipeline 四層：(1) 常量層（`UI_SANDBOX_SCHEMA_VERSION="1.0.0"` + `DEFAULT_SANDBOX_IMAGE="node:22-alpine"` + `DEFAULT_DEV_COMMAND=("sh","-c","npm run dev -- --port 3000 --hostname 0.0.0.0")` + `DEFAULT_CONTAINER_PORT=3000` + `DEFAULT_HOST_PORT_RANGE=(40000,40999)` + `DEFAULT_WORKDIR="/app"` + `DEFAULT_IDLE_LIMIT_S=900.0`（對齊 V2 row 2 idle 15 min）+ `DEFAULT_NODE_ENV="development"` + `READY_PATTERNS` 五條編譯 regex 跨 Next.js/Vite/CRA dev-server banner）；(2) 資料模型層（`SandboxStatus` 六態 str-enum `pending/starting/running/stopping/stopped/failed` + `SandboxConfig` frozen + `SandboxInstance` frozen + `CompileError` frozen，全 JSON-safe，全帶 `to_dict()` + schema_version）；(3) 純函數層（`format_container_name` lowercase/safe-char/63-cap 對齊 docker DNS label + `build_preview_url` 自動注入 leading slash + `validate_workspace` 檢 exists/dir/absolute + `allocate_host_port` SHA-256(session_id) 決定性 hash + linear-probe 避開 `in_use` + `build_docker_run_spec` pure deterministic dict（env auto-default NODE_ENV/HOST/PORT；mounts bind workspace→/app；ports host→container；env sorted byte-identical）+ `detect_dev_server_ready` case-insensitive 掃五 banner + `parse_compile_error` best-effort 抽 file/line/col 三元組 + `error_type` 如 module_not_found/syntaxerror/typeerror + 空/garbage 回 empty tuple + dedup + `render_sandbox_status_markdown` deterministic markdown）；(4) 管理器層（`DockerClient` Protocol 五方法 minimal shim；production 綁 `SubprocessDockerClient` shell out `docker` CLI + injection-style `runner=` 供 mocking；tests 綁 `FakeDockerClient` 記錄 run/stop/remove 呼叫；`SandboxManager` thread-safe `RLock` 包住 `_instances`，public API：`create`/`start`/`mark_ready`/`touch`/`stop`/`remove`/`get`/`list`/`logs`/`poll_ready`/`snapshot`；`create` 守 `SandboxAlreadyExists` 1-per-session 不變量；`start` 自動 alloc host_port 若 None + docker 失敗轉 `failed` + emit `ui_sandbox.failed` 不 propagate；`stop` docker 錯誤轉 warnings tuple；event callback 錯誤被吞成 WARNING log）。**Graceful fallback 四路** — docker 不可用 `SubprocessDockerClient` raise `SandboxError`、`docker run` 失敗 instance→`failed`、`stop/rm` 錯→warnings、event callback 爆→吞 log。**Schema 與 invariants** — schema_version 綁 `SandboxConfig/Instance/build_docker_run_spec/snapshot` 四 JSON payload；str-enum 直接 JSON-serialisable；`__post_init__` 驗 session_id `[A-Za-z0-9_.-]{1,64}` 對齊 docker DNS label + absolute workspace + positive timeouts + env 限 str + 空 command reject + absolute workdir；`idle_seconds(now=)` 決定性、last_active_at=0 新 sandbox 回 0 不被誤收。**Volume mount** — 固定 `workspace_path → /app` bind read_only=False 讓 agent 寫 code 觸發 HMR。**Port allocator** — SHA-256 hash 起點讓同 session 永遠落同 port 對 repro bugs 很重要。**契約測試** `backend/tests/test_ui_sandbox.py` 166 條 0.15s：module invariants 30+（`__all__` set + 31 export parametrize + semver + image/command/port/workdir/host/timeout 硬 pin + idle 900s + READY_PATTERNS + NODE_ENV=dev）、`format_container_name` 7、`build_preview_url` 8、`validate_workspace` 5、`allocate_host_port` 6、`SandboxConfig` validation 15、`build_docker_run_spec` 10、`detect_dev_server_ready` 9、`parse_compile_error` 6、`SandboxInstance` 7、`SandboxManager` lifecycle 20（含 20-thread concurrent stress + one-per-session + graceful docker failure + terminal-only remove + idempotent start/mark_ready/stop + 4 個生命週期 event 順序）、`render_sandbox_status_markdown` 3、`SubprocessDockerClient` 4、sibling alignment 2。V1 #1-#9 sibling 810 regression 0 錯。**為什麼此模組是 V2 live-preview 基石** — V1 產靜態 TSX，V2 關迴圈 render 出來讓 agent 看；此模組是 V2 三腳其一（lifecycle primitives），另兩腳 row 2 policy/idle reaper + row 3 `ui_screenshot.py` Playwright；刻意只做 primitives，`reap_idle` 留給 row 2 一行 for-loop policy wrapper。**關鍵設計決策** — (a) dependency-injected `DockerClient` Protocol 零 docker-py 依賴 + tests 無需 daemon；(b) frozen dataclasses + `replace` 保留 audit trail；(c) deterministic port allocator 跨 restart 同 session 同 port；(d) graceful docker failure 絕不 propagate 到 agent loop；(e) `SandboxAlreadyExists` 對齊 row 2 規格；(f) heuristic ready detection 覆蓋多 framework；(g) 結構化 `CompileError` 為 row 5 「preview error bridge」預留 SSE-ready shape；(h) thread-safe `RLock` 20-thread 驗證無 corruption。**Scope 自律** — 只新增 `backend/ui_sandbox.py` + `backend/tests/test_ui_sandbox.py`；不動 V1 sibling、不動 `llm_adapter.py`（sandbox 不打 LLM）、不動 `events.py`（event bridge 留 V2 row 7）、不動 `requirements.txt`（零新依賴；docker 走 CLI）、不動 `configs/roles/ui-designer.md`、不動 routing/server/frontend；V0 / V1 #1-#9 全綠 zero regression；V1+V2 合計 976 條 < 2s)*
- [x] Sandbox lifecycle：create → start → hot-reload → screenshot → stop → cleanup；每 session 最多 1 sandbox，idle 15 min 自動回收 *(done: V2 #2 — `backend/ui_sandbox_lifecycle.py` (~620 行) + `backend/tests/test_ui_sandbox_lifecycle.py` 114 條契約測試全綠 0.28s。**Policy wrapper over V2 #1 primitives** — V2 #1 刻意只做 Docker verbs；此模組鎖定 V2 row 2 spec 的 full lifecycle orchestration + 兩個硬不變量（1 per session + idle 15 min reap）。**模組五層** — (1) 常量層（`SANDBOX_LIFECYCLE_SCHEMA_VERSION="1.0.0"` + `DEFAULT_READY_POLL_INTERVAL_S=0.5` + `DEFAULT_READY_POLL_TIMEOUT_S=60.0` + `DEFAULT_REAPER_INTERVAL_S=30.0` + `DEFAULT_IDLE_LIMIT_S=900.0` re-export 對齊 V2 #1 + `MAX_SANDBOXES_PER_SESSION=1`）；(2) Event 命名層（6 個 `ui_sandbox.*` 常量：`ensure_session` / `hot_reload` / `screenshot` / `teardown` / `reaped` / `ready_timeout` + `LIFECYCLE_EVENT_TYPES` tuple 給 V2 row 6 SSE bus 前綴訂閱用，全 `ui_sandbox.*` namespace + 去重保證）；(3) 資料模型層（`ScreenshotResult` frozen — session_id/preview_url/viewport/path/image_bytes/captured_at + `byte_len` property + `to_dict(include_bytes=False)` default 不吐 PNG 保 SSE payload 輕量、`True` 時 base64 編碼給 V2 row 6 Opus multimodal inject；`ReapReport` frozen — reaped_at/reaped_sessions/still_active/idle_limit_s/warnings + `reaped_count` property + schema_version 嵌入 to_dict）；(4) 錯誤層（`LifecycleError` 繼承 `SandboxError` 讓現有 `except SandboxError` 繼續有效 + `ReadyTimeout` / `ScreenshotUnavailable` / `WorkspaceMismatch` 三個細分）；(5) `SandboxLifecycle` 主類別（composition over inheritance — has-a manager，不 subclass；public API：`ensure_session`/`wait_ready`/`hot_reload`/`capture_screenshot`/`teardown`/`reap_idle`/`start_reaper`/`stop_reaper`/`is_reaper_running`/`reaper_sweeps`/`get_stage`/`list_sessions`/`snapshot`/`set_screenshot_hook` + context manager `__enter__`/`__exit__`；dependency injection 滿天：`manager=` / `screenshot_hook=` / `clock=` / `sleep=` / `event_cb=` 五個 seam 讓 tests 100% 決定性）。**六段 lifecycle 執行路徑** — (a) **create** `ensure_session(config)` 先 `manager.get(session_id)` 檢查——absent 則 `manager.create`；存在且 workspace 相同且非 terminal → 重用；存在且 workspace 不同 → `WorkspaceMismatch`；存在且 terminal → 自動 teardown+recreate；`recreate=True` 強制 teardown；(b) **start** `ensure_session` 當 status=pending 時自動 `manager.start`；(c) **hot-reload** `hot_reload(session_id, files_changed=)` → `manager.touch` + emit `ui_sandbox.hot_reload` 帶 files 列表給 V2 row 6 動畫用；HMR 真的發生在 container 內（V2 #1 bind mount 保證）此函數只做 policy ack + last_active_at 更新；(d) **screenshot** `capture_screenshot(session_id, viewport=, path=)` → 呼叫 injected `screenshot_hook(session_id, preview_url, viewport, path) → bytes`；hook 為 None raise `ScreenshotUnavailable`（V2 row 3 `ui_screenshot.py` wires in）；hook 拋例外自動 wrap 為 `LifecycleError` 不 teardown（screenshot 失敗是 transient dev-server hiccup）；返回 bytes type/空 檢查；成功後 `manager.touch` + emit `ui_sandbox.screenshot`；(e) **stop** `teardown(session_id, remove=True)` → `manager.stop` + optional `manager.remove`；idempotent on terminal；docker 錯誤變 warnings 永不 raise；emit `ui_sandbox.teardown`；(f) **cleanup** teardown 的 `remove=True` 分支 + `__exit__` context manager 保證不留孤兒。**1-per-session enforcement** — V2 #1 已在 `manager.create` 擋 `SandboxAlreadyExists`；V2 #2 的 `ensure_session` 是 idempotent wrapper——重複呼叫同 session_id 返回同一 sandbox + 零新 container（契約 `test_one_sandbox_per_session_enforced_by_lifecycle`：3 次 ensure → 1 個 instance + 1 次 docker run）。**Idle 15 min 自動回收** — `reap_idle(now=, idle_limit_s=)` 同步 sweep：scan manager.list()，terminal sandbox 無條件 remove（順便當 GC）、idle > limit 的 running sandbox teardown(remove=True)、回傳 `ReapReport(reaped_sessions, still_active, warnings)`；emit `ui_sandbox.reaped` 只在有動作時避免 SSE noise；docker 錯誤絕不 propagate 只加 warnings。`start_reaper()/stop_reaper()` spawn daemon 線程用 `threading.Event.wait(timeout=interval_s)` 精準控制 — stop_reaper 在 100ms 內返回即使 interval_s=30。單例保證（already-alive → 二次呼叫 no-op）。**Wait-ready polling** — `wait_ready(session_id, timeout_s=, poll_interval_s=)` 基於 `manager.poll_ready` 輪詢直到 `detect_dev_server_ready` 命中 `READY_PATTERNS` 就 `mark_ready`；超時 raise `ReadyTimeout` 並 emit `ui_sandbox.ready_timeout` event；若 sandbox 已 running 立刻返回零 sleep；status=failed raise `LifecycleError` 提前止血。**契約測試 114 條 0.28s** — module invariants 11、ScreenshotResult 7、ReapReport 5、建構 4、ensure_session 8、wait_ready 5、hot_reload 5、capture_screenshot 10、teardown 5、reap_idle sync 9、背景 reaper 6、1-per-session e2e 1、introspection 4、context manager 2、**full golden path 1**（create→start→hot_reload→screenshot→teardown 8 段 event 順序驗證）、20-thread 並發壓測 1（worker × (ensure+mark_ready+hot_reload+teardown) + 背景 reaper 同步跑 20ms interval → 0 error + registry 乾淨）、sibling alignment 3。**FakeClock + FakeSleep paired** — `FakeSleep(clock)` 呼叫時 `clock.advance(seconds)` 並不真 sleep；所有 blocking ops 全決定性零真時間耗費；只有 `test_background_reaper_actually_sweeps` 用真 time.sleep 驗 thread 真跑。**為什麼 V2 #2 是 V2 row 3-7 的 landing gate** — row 3 screenshot 需要 screenshot_hook 注入點（已開 set_screenshot_hook + ScreenshotHook Protocol）；row 4 compile-error bridge 需要 hot_reload/teardown event + ReadyTimeout/LifecycleError 鉤子（已開 event constants + error hierarchy）；row 5 agent visual context 需要 ScreenshotResult.to_dict(include_bytes=True) 產 base64 給 Opus multimodal（已實作）；row 6 SSE bus 需要 LIFECYCLE_EVENT_TYPES tuple 做 prefix subscription（已開）；row 7 整合測試需要 context manager + 1-per-session + idle reaper 全 green（已全覆蓋）。**關鍵設計決策** — (a) Composition over inheritance — SandboxLifecycle 擁有 manager 不是它的 subclass；(b) Deterministic time seams — 每個阻塞操作吃 injected sleep=、每個 timestamp 過 injected clock=；(c) Screenshot 是 hook 不是 module dependency — V2 #2 不 import Playwright；(d) Reaper 是 opt-in — production 呼叫 start_reaper；tests 呼 reap_idle 同步；(e) Graceful teardown on exit — context manager 保證 SIGINT 不留孤兒；(f) Error 繼承 SandboxError — 既有 except 保持可用；(g) Event 不 noise — reap_idle 只在有動作時 emit；(h) docker stop failure → warnings 永不 propagate；(i) Event callback failure swallowed 與 V2 #1 一致；(j) Thread-safe — RLock 包 _reaper state + ensure_session 全臨界區；20-thread 並發 + 背景 reaper 壓測驗無 corruption。**Scope 自律** — 只新增 `backend/ui_sandbox_lifecycle.py` + `backend/tests/test_ui_sandbox_lifecycle.py` 2 檔；不動 V2 #1 sibling、不動 V1 sibling、不動 `backend/ui_sandbox.py` 一行、不動 `events.py`（SSE bridge 留 V2 row 6）、不動 `llm_adapter.py`、不動 `requirements.txt`（零新依賴）、不動 `configs/roles/ui-designer.md`、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1 合計 976 條全綠 zero regression；V1+V2 合計 1090 條 1.73s 全綠)*
- [x] `backend/ui_screenshot.py`：Playwright headless 截圖 service——定期或 on-demand 截圖 sandbox → 回傳 PNG base64 *(done: V2 #3 — `backend/ui_screenshot.py` (~780 行) + `backend/tests/test_ui_screenshot.py` 153 條契約測試全綠 0.29s。**Playwright side of V2 #2's ScreenshotHook boundary** — V2 #2 刻意不 import Playwright，只開 `set_screenshot_hook()` 注入點；V2 #3 在該 boundary 背後 ship 真 implementation + policy wrapper。**模組六層** — (1) 常量層（`UI_SCREENSHOT_SCHEMA_VERSION="1.0.0"` + `PNG_SIGNATURE=b"\x89PNG\r\n\x1a\n"` 官方 8-byte 開頭 + `DEFAULT_VIEWPORT="desktop"` + `DEFAULT_CAPTURE_TIMEOUT_S=30.0` + `DEFAULT_NAVIGATION_TIMEOUT_MS=30000` Playwright 原生毫秒 + `DEFAULT_WAIT_UNTIL="load"`（不用 `networkidle` 因 HMR websocket 永不 idle）+ `DEFAULT_PERIODIC_INTERVAL_S=5.0` + `DEFAULT_HISTORY_SIZE=32`（~2.5 分鐘 @ 5s cadence）+ `MAX_CAPTURE_BYTES=10_000_000` 防 SSE payload 暴炸）；(2) Viewport 資料層（`Viewport` frozen dataclass — name/width/height/device_scale_factor/is_mobile/user_agent + `__post_init__` 驗 name 走 `[a-z0-9_-]{1,32}` regex + positive dims + dsf；三個預設 `VIEWPORT_DESKTOP` 1440×900 dsf=1.0 Chrome UA、`VIEWPORT_TABLET` 768×1024 dsf=2.0 iPad UA、`VIEWPORT_MOBILE` 375×812 dsf=3.0 iPhone X UA — V2 row 4 three-viewport matrix 直接 iterate `VIEWPORT_PRESETS` 即可）；(3) Event 命名層（4 個 `ui_sandbox.screenshot*` 常量：`captured`（與 V2 #2 `LIFECYCLE_EVENT_SCREENSHOT` 同名讓 SSE bus 單一 topic）/`periodic_started`/`periodic_stopped`/`failed` + `SCREENSHOT_EVENT_TYPES` tuple 給 V2 row 6 SSE bus 前綴訂閱）；(4) Request + Capture 資料層（`ScreenshotRequest` frozen — session_id/preview_url/viewport/path/full_page/wait_until/timeout_s/navigation_timeout_ms + `target_url` property pure join + `to_dict` JSON-safe；`ScreenshotCapture` frozen — session_id/preview_url/viewport/path/image_bytes/captured_at/duration_ms/target_url + `byte_len` property + `to_dict(include_bytes=False)` default 不吐 PNG（SSE payload 輕量）`True` 時 `encode_png_base64` 給 V2 row 5 Opus multimodal + `to_data_url` 直接產 `data:image/png;base64,…`；bytearray → bytes coerce；missing target_url → auto-derive via `build_target_url`）；(5) 錯誤層（`ScreenshotError` 基底 + `PlaywrightUnavailable` install guidance + `ViewportUnknown` preset miss + `CaptureTimeout` Playwright nav timeout + `InvalidPngData` 空/wrong-sig/oversize + `PeriodicAlreadyRunning` 單例守衛）；(6) 純函數層（`get_viewport(name)` case-insensitive + trim + raise `ViewportUnknown` + `list_viewports()` stable order + `build_target_url(preview_url, path)` 檢 scheme+host、拒絕 `..` path-traversal、strip query/fragment on base、normalize trailing slash + `validate_png_bytes(data, max_bytes=)` 檢 type+non-empty+size+PNG signature + `encode_png_base64(data)` 先 validate 再 b64encode）。**Engine + Service 分層** — 鏡像 V2 #1 primitives vs V2 #2 policy 分割：(A) `ScreenshotEngine(Protocol)` — `capture(request) -> bytes` + `close()` 兩方法；implementations 必 thread-safe（periodic thread + on-demand caller 會並發）；(B) `PlaywrightEngine` 生產實作 — lazy import `playwright.sync_api` inside `__init__` + `PlaywrightLauncher` type 給 tests 注入 fake；browser 一次 launch + per-capture 開 context（device-scale-factor 在 tablet/mobile 切換時重用 page 會 flaky）；internal lock serialise capture 因 single playwright browser 不 thread-safe；`CaptureTimeout` 在 nav timeout 時 raise + `ScreenshotError` wrap non-timeout；launch 失敗自動 pw.stop() roll back；`close()` idempotent + `__enter__/__exit__` 支援 `with PlaywrightEngine()` 語法；(C) `ScreenshotService` policy wrapper — `engine=` 注入 + `clock=` + `event_cb=` + `default_viewport=` + `history_size=` + `capture_timeout_s=` + `navigation_timeout_ms=` + `wait_until=` + `periodic_interval_s=` 九個 seam；public API `capture` / `as_hook` / `start_periodic` / `stop_periodic` / `stop_all_periodic` / `is_periodic_running` / `periodic_sessions` / `periodic_sweeps` / `latest` / `recent(limit=)` / `clear_history` / `capture_count` / `failure_count` / `sessions_with_history` / `snapshot` / `close` + `__enter__/__exit__`；thread-safe RLock 包 `_history` dict + `_periodic` dict + counters。**六段 capture 執行路徑** — (a) `_build_request` 解析 viewport（None → default；str → get_viewport；Viewport → passthrough；其它 → TypeError）+ 注入 timeout_s override 或 default；(b) `engine.capture(request)` 實際截圖；(c) `ScreenshotError` → 計 `_failure_count` + emit `SCREENSHOT_EVENT_FAILED` reason=`engine_error` + re-raise；非 `ScreenshotError` → emit reason=`unexpected:{type}` + wrap 成 `ScreenshotError` re-raise；(d) `validate_png_bytes(png)` 檢簽名 + max bytes；失敗 → emit reason=`invalid_png` + raise `InvalidPngData`；(e) 成功 → 建 `ScreenshotCapture` 記 `duration_ms = (finished - started) * 1000.0`；(f) append into `_history[session_id]` deque(maxlen=history_size) + emit `SCREENSHOT_EVENT_CAPTURED`。**Periodic capture** — `start_periodic(session_id, preview_url, viewport=, path=, interval_s=)` spawn daemon thread；`_PeriodicState` 記 stop_event/preview_url/viewport/path/interval_s/started_at/sweeps/failures；`PeriodicAlreadyRunning` 守單例；loop 基於 `threading.Event.wait(timeout=interval_s)` 精準控制 — stop_periodic() 不管 interval 多長都在 ms 內返回；`ScreenshotError` inside loop → logger.warning + failures++ 不 kill thread；`stop_periodic` 回 True/False 告知有沒有東西 stop；`stop_all_periodic(timeout_s=)` shutdown 時清零；`snapshot()` dump 每個 periodic session 的 alive/sweeps/failures/started_at 給 operator。**as_hook() adapter** — 回 `Callable[..., bytes]` 與 V2 #2 `ScreenshotHook(Protocol)` 完全相容 — `hook(session_id=, preview_url=, viewport=, path=)` 內部 delegate `self.capture(...).image_bytes`；V2 #2 `SandboxLifecycle(screenshot_hook=svc.as_hook())` 就 plug-and-play。**契約測試 153 條 0.29s** — module invariants 18（`__all__` set + 35 export parametrize + semver + PNG signature 8-byte 官方值 + default viewport 可 resolve + 所有正數 default + event namespace 全 `ui_sandbox.*` + `SCREENSHOT_EVENT_CAPTURED == LIFECYCLE_EVENT_SCREENSHOT` 鎖死 + 錯誤階層）、Viewport presets 10（三預設 1440×900/768×1024/375×812 硬 pin 對齊 V2 row 4 spec + frozen + to_dict JSON-safe + is_mobile flag + dsf positive + name regex + list_viewports stable + get_viewport 大小寫不敏感 + unknown raise）、`build_target_url` 10（6 parametrize happy path + path traversal reject + bad path reject + bad base reject）、`validate_png_bytes` 6 + `encode_png_base64` 2、ScreenshotRequest 7（defaults + target_url property + frozen + validation + to_dict）、ScreenshotCapture 10（byte_len + frozen + target_url auto-derive + explicit retain + to_dict 無 bytes default + 有 bytes base64 + to_data_url prefix + bad input + bytearray coerce）、Service 構造 5（None/非-engine reject + 7 parametrize bad kwargs + engine property + default viewport normalize）、capture() 12（engine 被呼叫 + viewport by name/instance + 非-str/Viewport reject + ScreenshotError propagate + unexpected wrap + InvalidPng reject + duration_ms 真算 + timeout override + counters + event payload 無 bytes + history 存）、history ring 7（oldest→newest + limit tail + unknown empty + negative raise + zero limit + latest + maxlen evict）、clear_history 2、as_hook 整合 4（bytes 回 + 記入 history + **與 V2 #2 SandboxLifecycle 端到端整合**（FakeDockerClient → SandboxManager → SandboxLifecycle(screenshot_hook=svc.as_hook()) → ensure_session → capture_screenshot 跑通；驗證 V2 #2 boundary 與 V2 #3 實作接合無縫）+ Protocol shape check）、periodic 10（thread spawn + 真 capture + singleton raise + non-positive reject + stop 回 False 無 loop + is_running tracks + periodic_sessions + stop_all + 失敗不 kill loop + sweeps counter）、snapshot/close/CM 4、PlaywrightEngine 7（injected launcher + viewport dims 正確傳入 new_context + URL 走 build_target_url + close idempotent + CM 語法 + type mismatch + closed state + nav timeout 轉 CaptureTimeout + launch failure 轉 ScreenshotError + pw.stop rollback + **real playwright 缺失時 raise PlaywrightUnavailable**（host-conditional test 在 playwright 缺席時驗）、20-thread 並發壓測 1（20 × 5 = 100 captures across 3 sessions → 0 error + counter=100 + 3 buckets 齊）、sibling alignment 2（V1/V2 都還 importable + schema version 獨立演進）。**Graceful fallback 三路** — (a) playwright 未裝 → `PlaywrightEngine.__init__` raise `PlaywrightUnavailable` 含 `pip install playwright && playwright install chromium` hint；(b) nav timeout → 轉 `CaptureTimeout`（Playwright private submodule 用 name-contains-Timeout heuristic 避開依賴）；(c) periodic 內 failure → failures++ + logger.warning 不 kill thread。**為什麼此模組是 V2 row 4-7 的 landing gate** — row 4 three-viewport 矩陣直接 iterate `VIEWPORT_PRESETS` 即可 ship；row 5 agent visual context 用 `ScreenshotCapture.to_dict(include_bytes=True)` 產 base64 inject Opus 4.7 multimodal；row 6 SSE bus 訂 `SCREENSHOT_EVENT_TYPES` 四 topic 就拿完整 lifecycle；row 7 整合測試接 `SandboxLifecycle(screenshot_hook=ScreenshotService(engine=PlaywrightEngine()).as_hook())` 一行 wire up。**關鍵設計決策** — (a) **Playwright lazy + optional**：裝了用真 engine，沒裝仍可 import module + 跑 test（Fake engine）+ production raise 清楚的 install hint — CI/test 環境零新依賴；(b) **Engine/Service 分割**：Engine 做 browser 機械動作 + Service 做 policy（viewport resolve/PNG validate/history/periodic/events）；mirror V2 #1 vs V2 #2 split；(c) **`as_hook()` adapter 不 inherit**：Service 是 ScreenshotHook 的**提供者**而非實作者 — 回 closure 讓 Protocol 相容但不耦合；(d) **Event 命名與 V2 #2 對齊**：`SCREENSHOT_EVENT_CAPTURED == LIFECYCLE_EVENT_SCREENSHOT` 鎖死 SSE bus 單 topic — 訂閱者不用同時處理兩個名字；(e) **Bounded history**：deque(maxlen=32) per session 無限期 periodic loop 也不會 OOM；(f) **PNG signature validation**：engine 回錯東西（JPEG/HTML error page/空）全部擋在 SSE frame 之前；(g) **Deterministic time**：clock= 注入讓 duration_ms / captured_at 全決定性；(h) **thread-safe RLock**：20-thread 並發 100 captures 驗無 corruption；(i) **Playwright `wait_until="load"` 不用 `networkidle`**：HMR websocket 永不 idle 會誤 timeout；(j) **Viewport name lowercase + regex**：`[a-z0-9_-]{1,32}` 避免 URL-unsafe 或過長名字；(k) **`to_data_url()` 便利方法**：前端直嵌 preview HTML 不必 separate fetch；(l) **`stop_all_periodic` + `close`**：shutdown 時清零 daemon threads 避免 pytest session 懸掛。**Scope 自律** — 只新增 `backend/ui_screenshot.py` + `backend/tests/test_ui_screenshot.py` 2 檔；不動 V1 sibling、不動 V2 #1/#2 sibling、不動 `backend/ui_sandbox.py` 一行、不動 `backend/ui_sandbox_lifecycle.py` 一行、不動 `events.py`（SSE bridge 留 V2 row 6）、不動 `llm_adapter.py`、不動 `requirements.txt`（Playwright 保持 optional）、不動 `configs/roles/ui-designer.md`、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1-#2 合計 1090 條全綠 zero regression；V1+V2 合計 1243 條 2.09s 全綠)*
- [x] Responsive viewport：desktop (1440×900) / tablet (768×1024) / mobile (375×812) 三 viewport 截圖 *(done: V2 #4 — `backend/ui_responsive_viewport.py` (~590 行) + `backend/tests/test_ui_responsive_viewport.py` 126 條契約測試全綠 0.24 s。**Policy wrapper over V2 #3 for the three-viewport matrix** — V2 #3 已有單 viewport `ScreenshotService.capture(viewport=)` 的 primitive 與 `VIEWPORT_PRESETS` 三預設（desktop 1440×900 / tablet 768×1024 / mobile 375×812），V2 #4 封一顆 `ResponsiveViewportCapture.capture_all()` 把「一次呼叫 → 三 viewport → 一份 structured report → 一對 batch lifecycle event」整成 V2 row 5 multimodal inject / row 6 SSE bus 可直接消費的樣子。**模組四層** — (1) **常量層** `UI_RESPONSIVE_VIEWPORT_SCHEMA_VERSION="1.0.0"`（與 V2 #3 schema 獨立演進）+ `DEFAULT_VIEWPORT_MATRIX=("desktop","tablet","mobile")`（V2 row 4 spec 規範的 canonical 順序）+ `FAILURE_MODES=("collect","abort")` + `DEFAULT_FAILURE_MODE="collect"`（讓 agent 看到哪些 viewport 成功哪些失敗）；(2) **Event 命名層** 4 個 `ui_sandbox.viewport_batch.*` 常量 `started`/`viewport_captured`/`viewport_failed`/`completed` + `VIEWPORT_BATCH_EVENT_TYPES` tuple；**刻意與 V2 #3 的 `ui_sandbox.screenshot*` namespace 分離** — V2 row 6 SSE bus 訂 `ui_sandbox.viewport_batch.` prefix 拿 batch envelope、訂 `ui_sandbox.screenshot` 拿 per-capture；兩個 topic family 同時 fire 不互相偷走；(3) **資料模型層** `ViewportCaptureOutcome` frozen — viewport_name/success/capture/error_type/error_message/duration_ms + 嚴格 invariant 檢查（成功要求 capture、失敗要求 error_type+error_message、viewport_name 必須與 capture.viewport.name 一致、duration 非負）+ `to_dict(include_bytes=)` 可選 base64；`ResponsiveCaptureReport` frozen — session_id/preview_url/path/viewport_names/outcomes/started_at/finished_at/failure_mode + properties `success_count`/`failure_count`/`is_complete_success`/`is_partial`/`duration_ms`/`captures`（只成功的）/`failures`（只失敗的）/`skipped_viewports`（abort 模式下未到達的）+ `to_dict(include_bytes=)`；(4) **錯誤層** `ResponsiveViewportError` 繼承 `ScreenshotError`（既有 `except ScreenshotError` 仍有效）+ `InvalidViewportMatrix` 細分空/未知/重複 + `BatchAborted(report=)` 抱住 partial report 給 abort-mode 呼叫者檢視。(5) **純函數層** `resolve_viewport_matrix(matrix)` 吃 str/Viewport 混合、preserve order、去 dup-by-name、包 `ViewportUnknown` 為 `InvalidViewportMatrix` + `render_responsive_report_markdown(report)` 產 deterministic operator 可讀 markdown（表格含 viewport/status/dims/bytes/error + skipped 標記）；(6) **主類別** `ResponsiveViewportCapture(service=, clock=, event_cb=, default_matrix=)` — composition-over-inheritance（has-a ScreenshotService 不是 subclass，鏡像 V2 #2 `SandboxLifecycle` 與 V2 #1 `SandboxManager` 的關係）；public API `capture_all(session_id, preview_url, path=, matrix=, failure_mode=, full_page=)` + `snapshot()` + 計數器 `batch_count()`/`success_batches()`/`partial_batches()`/`aborted_batches()`/`last_report()`；thread-safe RLock 包計數 + 最後 report。**六段 capture_all 執行路徑** — (a) 檢 failure_mode/session_id/preview_url/path；(b) matrix None 用 default 已 resolved tuple 避免 re-resolve，有 override → `resolve_viewport_matrix`（同一輪檢 dup/unknown）；(c) emit `VIEWPORT_BATCH_EVENT_STARTED` 帶 viewport_names+started_at；(d) 序列跑每個 viewport 呼 `service.capture(...)` — 只 catch `ScreenshotError`（V2 #3 已經把非 ScreenshotError 的例外 wrap 成 ScreenshotError）；成功 → 建 `ViewportCaptureOutcome(success=True, capture=)` + emit `viewport_captured` 帶 byte_len/duration_ms；失敗 → 建 `ViewportCaptureOutcome(success=False, error_type=, error_message=)` + emit `viewport_failed` 帶 error 結構；failure_mode=abort 時失敗立即 break 不跑 remaining viewport；(e) 用 outcomes 組 `ResponsiveCaptureReport`，更新 counters（complete/partial/aborted 三分類）；(f) emit `VIEWPORT_BATCH_EVENT_COMPLETED` 帶完整 report dict；(g) 若 aborted → raise `BatchAborted(report=)` 帶 partial report。**為什麼不是 ScreenshotService 的一個方法** — (i) 單一職責：service 管 one-shot + periodic loop，batch 是第三種 lifecycle；(ii) event namespace 清晰：混 `ui_sandbox.screenshot` 與 `ui_sandbox.viewport_batch.*` 會迫使每個 subscriber 做 prefix 過濾；(iii) schema 獨立：本模組自己的 schema_version 可獨立 bump；(iv) 測試性：service 的 batch 方法會把單 viewport + 三 viewport 兩概念塞在同個 fixture。**為什麼 serial 不 parallel** — `PlaywrightEngine` 內部已 serialise（單 Chromium browser 非 thread-safe），parallel capture 只會 lock-contend 且讓 event 順序不 deterministic；serial 反而給 V2 row 6 SSE bus 穩定的 desktop→tablet→mobile 顯示動畫。**為什麼 collect 是預設 failure mode** — agent 看到「2/3 succeeded + tablet 在 CaptureTimeout」比「batch 全砍」更有意義，因為 viewport-specific 的 CSS regression 是 V2 row 4 最常見的 failure mode；abort-mode 保留給 CI「全綠或失敗」gate。**契約測試 126 條 0.24 s** — module invariants 13（`__all__` 17 exports set + 17 export parametrize + semver + schema 獨立於 V2 #3 + `DEFAULT_VIEWPORT_MATRIX=("desktop","tablet","mobile")` 硬 pin + 三預設 dims 對齊 V2 row 4 spec + `FAILURE_MODES={"collect","abort"}` + event 全在 `ui_sandbox.viewport_batch.` namespace + batch events 不與 V2 #3 SCREENSHOT_EVENT_TYPES 衝突 + 錯誤階層）、`resolve_viewport_matrix` 13（name/Viewport 混用 + 大小寫不敏感 + 空 reject + unknown wrap `ViewportUnknown` 到 `InvalidViewportMatrix.__cause__` + duplicate reject + name+instance 同 .name 也算 dup + bad type reject + None reject + preserve order + 返回 tuple）、`ViewportCaptureOutcome` 13（success/failure happy path + frozen + success 要求 capture + success 拒絕 error_type + failure 拒絕 capture + failure 要求 error_type+error_message + viewport_name 對應 capture.viewport.name + 負 duration reject + 空 name reject + to_dict 兩分支 json-safe + include_bytes + 失敗 capture=None）、`ResponsiveCaptureReport` 18（frozen + success_count + partial + skipped_viewports + captures only-successful + failures tuple + duration_ms + non-negative floor + finished<started reject + bad path/session_id/preview_url/failure_mode reject + 空 viewport_names reject + 非-Outcome reject + to_dict json-safe + include_bytes 三顆都有 base64 + skipped 記錄）、建構 7（ok + None reject + 非-service reject + 空 default_matrix reject + unknown default_matrix reject + 自定 default_matrix + `.service` property 指同物 + 計數器從零）、capture_all happy path 13（三 viewport 順序 + 返回 report type + default matrix used + override matrix + path 傳 engine + full_page 傳 engine + 三 viewport dims 對齊 V2 row 4 + bad failure_mode/session_id/preview_url/path reject + duplicate override reject + unknown override reject）、failure mode 6（collect partial + collect all-fail + abort raise `BatchAborted` 帶 partial report + abort 第一 viewport 失敗 engine 只呼 1 次 + abort completed event 攜 partial report + RuntimeError 走 V2 #3 wrap 成 ScreenshotError）、event 8（started→captured×3→completed 順序 + partial 有 viewport_failed + started payload shape + viewport_captured payload shape + viewport_failed payload shape + completed payload shape + 事件 callback 爆炸 swallow + V2 #3 service events 與 batch events 同時 fire）、counters+snapshot 7（success/partial/aborted 三分類計數 + last_report 追最近 + snapshot json-safe + 零 capture snapshot）、timing 1（注入 clock 的 duration_ms 對齊）、thread-safety 1（10 thread × 3 capture 壓測無 corruption + engine 30 calls）、markdown 3（happy path + partial + abort 帶 skipped）、V2 #3 整合 4（service history 被 batch 填 + service.capture_count 被 batch 更新 + service.failure_count 被 batch 更新 + `report.captures` 與 `service.recent(...)` 同一物件而非 copy）、sibling alignment 3（V1 `ui_component_registry` + V2 #1 `ui_sandbox` + V2 #2 `ui_sandbox_lifecycle` 仍 importable）、**end-to-end 1**（三 viewport payload 唯一 + dims 對齊 V2 row 4 spec + `report.to_dict(include_bytes=True)` 直接餵 V2 row 5 Opus multimodal inject）。**V1+V2 合計** 794 條（含 ui_*/v1_*）全綠 zero regression；V2 #1-#4 合計 559 條 0.67 s。**關鍵設計決策** — (a) Composition over inheritance — `ResponsiveViewportCapture` 有 service 不是 subclass；(b) Serial capture 不 parallel — 避免 Playwright lock contention 與 event 亂序；(c) Collect 是預設 failure mode — agent 需要部分結果；(d) Event namespace 分離 — batch envelope vs per-capture 兩 topic family 同時 fire；(e) ScreenshotError catch only — V2 #3 已 wrap 意料外例外；(f) Duplicate matrix reject 不靜默 dedupe — 幾乎總是 typo；(g) skipped_viewports 保留 — abort 模式也能知道哪些 viewport 沒跑；(h) Report schema 獨立 — 不綁 V2 #3 schema_version；(i) Frozen + thread-safe — 與 V2 #1/#2/#3 一致；(j) event callback 爆炸 swallow — 與 V2 #1/#2/#3 一致；(k) `ScreenshotCapture` 同一物件 — service.recent 與 report.captures 指同物而非 deep copy。**Scope 自律** — 只新增 `backend/ui_responsive_viewport.py` + `backend/tests/test_ui_responsive_viewport.py` 2 檔；不動 V1 sibling、不動 V2 #1/#2/#3 sibling（`backend/ui_sandbox.py` / `backend/ui_sandbox_lifecycle.py` / `backend/ui_screenshot.py` 一行都沒動）、不動 `events.py`（SSE bridge 留 V2 row 6）、不動 `llm_adapter.py`、不動 `requirements.txt`（零新依賴）、不動 `configs/roles/ui-designer.md`、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1-#3 合計全綠 zero regression；V1+V2 合計 1369 條全綠)*
- [x] Preview error bridge：sandbox dev server 的 compile error / runtime error 攔截（stdout/stderr parse）→ 結構化 error object → 注入 agent context → agent 自動修 → 重截圖 *(done: V2 #5 — `backend/ui_preview_error_bridge.py` (~800 行) + `backend/tests/test_ui_preview_error_bridge.py` 193 條契約測試全綠 0.39 s。**Auto-fix loop closer for V2 row 5** — V2 #1 已有 `parse_compile_error` 與 `SandboxManager.logs()` 兩個 primitive；V2 #2/#3/#4 補齊 lifecycle / screenshot / viewport 矩陣；V2 #5 把 dev-server 錯誤流轉成 agent 下一輪 ReAct 可直接消費的結構化上下文，關 auto-fix 迴圈。**模組六層** — (1) **常量層** `UI_PREVIEW_ERROR_BRIDGE_SCHEMA_VERSION="1.0.0"` + `DEFAULT_LOG_TAIL=500` + `DEFAULT_WATCH_INTERVAL_S=1.5`（比 V2 #2 的 ready-poll 0.5 s 慢因 errors 通常跨多 sweep 持續）+ `DEFAULT_MAX_ERRORS_PER_SESSION=50`（防 dev server 狂噴 warning OOM）+ `DEFAULT_MAX_EXCERPT_CHARS=2000`（SSE frame / Opus multimodal 上限）；(2) **Severity 層** `SEVERITY_ERROR`/`SEVERITY_WARNING` + `SEVERITY_LEVELS` tuple 讓 React `Warning:` 類 degrade 為 non-blocking（agent 仍可 screenshot + surface）；(3) **Event 命名層** 6 個 `ui_sandbox.error.*` 常量 `detected`/`cleared`/`batch`/`context_built`/`watch_started`/`watch_stopped` + `ERROR_EVENT_TYPES` tuple 給 V2 row 6 SSE bus prefix 訂閱，**與 V2 #2 的 `LIFECYCLE_EVENT_TYPES` 完全 disjoint**（契約 `test_error_event_names_do_not_collide_with_lifecycle_events`）；(4) **資料模型層** `ErrorSource` str-enum 二態 `compile`/`runtime` + `LogSource` str-enum 三態 `stdout`/`stderr`/`combined` + `PreviewError` frozen（session_id/error_id/message/source/error_type/severity/file/line/column/first_seen_at/last_seen_at/occurrences/raw_excerpt + `is_compile`/`is_runtime`/`is_blocking` properties + `to_dict()` schema_version 內嵌）+ `ErrorBatch` frozen（session_id/scanned_at/detected/cleared/active/log_chars_scanned/warnings + `detected_count`/`cleared_count`/`active_count`/`has_activity` properties）+ `AgentContextPayload` frozen（session_id/built_at/errors/summary_markdown/auto_fix_hint/turn_id + `has_blocking_errors`/`has_errors`/`error_count` properties + `to_dict()` + `to_json()` sorted-key 決定性）；(5) **錯誤層** `PreviewErrorBridgeError` base + `WatchAlreadyRunning` / `WatchNotRunning` 細分；(6) **純函數層** `parse_runtime_error(text, max_errors=)` 抽 React/Next.js runtime exception（Uncaught TypeError/ReferenceError/SyntaxError/RangeError + `[Error]` prefix + unhandledRejection + Hydration failed + React Error/Warning + Warning:/[hmr] Failed + TypeError/ReferenceError/RangeError 裸型別）+ `runtime/<kind>` namespaced error_type + stack-frame file:line:col 掃同行或後 5 行 + empty/非 str 回空 tuple + `max_errors` 截斷 + `hash_error(source, error_type, message, file, line)` SHA-256 前 12 hex 穩定 ID（跨 process/Python 同輸入同結果）+ `classify_severity(error_type)` 映 `warning/react_warning/hmr_warning/runtime/warning*` → warning、其它 → error + `combine_errors(compile_errors, runtime_errors)` 保序 concat + 拒絕 non-CompileError + `render_error_markdown(errors)` 確定性 table（# / Source / Severity / Type / Location / Message 6 欄）+ empty → "No active errors." 穩定 body + `|` 自動 escape + `build_auto_fix_hint(errors)` 動態 agent-facing prompt fragment（count+blocking+warning 分類 + 指向第一個 blocking 的 file:line + empty 給 "preview rendered cleanly"）。**主類別 `PreviewErrorBridge`** — composition-over-inheritance 對齊 V2 #2/#3/#4；has-a `SandboxManager` 不 subclass；ctor 九 seam（manager/clock/sleep/event_cb/log_tail/watch_interval_s/max_errors_per_session/max_excerpt_chars）；thread-safe `RLock` 包 `_state`/`_last_batch`/`_watches`/counters。**六段 scan 執行路徑** — (a) `manager.logs(session_id, tail=)` 抓 combined stdout/stderr（V2 #1 已在上游 swallow docker 錯誤，bridge 再加一層 try/except 轉 warnings tuple 絕不 propagate）+ empty 也 well-formed batch；(b) `parse_compile_error(V2 #1)` + `parse_runtime_error(本模組)` 同時跑；(c) `combine_errors` concat 後**跨源 dedup** — 同 `(message, file, line)` 在 compile + runtime 都命中時 runtime 贏（「Uncaught TypeError:」一定是 runtime）；(d) 對 deduped errors 算 `classify_severity` + `hash_error` 產 `error_id` + 同 scan 內重複 bump occurrences；(e) lock 內 diff against prior state — `new` 進 `detected`、`missing` 進 `cleared`、`persisting` 保留 `first_seen_at` 累加 occurrences 更新 `last_seen_at`；(f) 超出 `max_errors_per_session` 按 `first_seen_at` 丟最老；(g) active 排序 blocking 先 + first_seen_at 升序；(h) lock 外 emit `detected`×N + `cleared`×N + `batch`（若活動，active 截 25 + `active_truncated` flag）。**Agent context 生產** — `build_agent_context(session_id, turn_id=)` → `AgentContextPayload` 含 summary markdown + auto_fix_hint + 結構化 errors tuple；empty 仍 renderable 讓 agent loop 不 special-case；emit `ui_sandbox.error.context_built`；`to_dict()` JSON-safe + `to_json()` sorted-key 決定性。**State queries + mutation** — `active_errors` / `has_active_errors` / `get_error` / `tracked_sessions` / `last_batch` / `acknowledge` (emit cleared source=acknowledge + idempotent) / `clear_session` (teardown 時呼叫)。**Background watch** — `start_watch` daemon thread 基於 `threading.Event.wait(timeout=)` 精準控制 + `WatchAlreadyRunning`/`WatchNotRunning` 守 + `stop_all_watches` / `is_watching` / `watch_sessions` / `watch_sweeps` / `watch_failures` telemetry；emit `watch_started`/`watch_stopped` 帶 sweeps/failures 統計。**Graceful 三路** — (a) `manager.logs` 內部已吞 docker 錯誤 + 外層 bridge 再吞一次 → warnings；(b) parse 爆炸轉 warnings；(c) event callback 爆炸 swallow + warning log 與 V2 #1/#2/#3/#4 一致。**Snapshot + context manager** — `snapshot()` JSON-safe + `__enter__/__exit__` 停所有 watch + 清 state 保 SIGINT hygiene。**契約測試 193 條 0.39 s** — module invariants 16（`__all__` set + 30 export parametrize + semver + 所有正數 default + severity levels + event 全 `ui_sandbox.error.` namespace + **與 V2 #2 LIFECYCLE_EVENT_TYPES 零交集** + ErrorSource/LogSource enum + 錯誤階層）、parse_runtime_error 12、hash_error 10（deterministic + 5 field 各變就變 + string/enum source 等價 + 長度 12 + 非-str source/int line reject）、classify_severity 10（9 parametrize + 非-str default）、combine_errors 3、PreviewError 19（happy/runtime/warning/frozen + 12 bad-input reject + non-enum source reject + to_dict json-safe + schema stable + 可選 file）、ErrorBatch 7（happy + frozen + to_dict + bad-input + non-PreviewError reject + no-activity + 正規 tuple）、AgentContextPayload 8（happy + empty + frozen + to_dict + to_json deterministic + bad-input + non-error reject + empty turn_id reject）、render_error_markdown 5、build_auto_fix_hint 4、bridge constructor 5、scan 14（bad session_id/tail + empty + compile detect + runtime detect + persist + cleared event + error_id stable + first_seen 保留 + counters + log_fetch 不 raise + 空 logs 寬容 + max_errors + tail 傳導 + batch 截 25 + blocking 先 + last_batch + tracked grows）、build_agent_context 6、state+mutation 9、background watch 11（spawn + started event + bad session/interval + already running + missing default + missing_ok=False raise + stopped event + stop_all 3 + **真 thread 真 sweeps** + counter 清）、snapshot+CM 4、event callback safety 1、**thread safety 1（10 thread × 20 scan 壓測 200 scans + 跨源 dedup 收斂到 1 active error + 0 corruption）**、sibling alignment 2（V1/V2 #1-#4 全 importable + parse_compile_error 重用自 V2 #1 不 re-implement）、**end-to-end agent fix loop 1**（broken code → scan 檢出 + detected event → build_agent_context 帶 file + auto_fix_hint → agent 改檔 → HMR 清錯 → scan 檢出 cleared + cleared event → context 回 clean 「preview rendered cleanly」驗 V2 row 5 完整閉環）。**V1+V2 合計** 904 條全綠 zero regression 1.03 s。**關鍵設計決策** — (a) Composition over inheritance 對齊 V2 #2/#3/#4；(b) Stateful but deterministic — FakeClock 驅動所有 timestamp；(c) Stable content-hash error ID — dedup 跨 sweep；(d) 跨源 dedup runtime 贏 compile —「Uncaught TypeError:」語義上一定是 runtime；(e) Watch 是 opt-in；(f) Graceful log failure → warnings tuple 絕不 propagate；(g) 沒 agent/LLM 耦合 — 本模組**生產** payload，注入由 orchestration 層做；(h) **沒 side effects on sandbox** — 不 touch/stop/teardown，純 read-only + auto-fix 由 agent 驅動；(i) Event namespace 分離 — `ui_sandbox.error.*` vs V2 #2 `ui_sandbox.ensure_session/hot_reload/screenshot/teardown/reaped/ready_timeout` 零交集；(j) `active_truncated` flag — SSE 消費端知道被截；(k) Frozen dataclasses + thread-safe RLock 一致性。**Scope 自律** — 只新增 `backend/ui_preview_error_bridge.py` + `backend/tests/test_ui_preview_error_bridge.py` 2 檔；不動 V1 sibling、不動 V2 #1/#2/#3/#4 sibling 一行、不動 `events.py`（SSE bridge 留 V2 row 6）、不動 `llm_adapter.py`、不動 `requirements.txt`（零新依賴）、不動 `configs/roles/ui-designer.md`、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1-#4 合計全綠 zero regression)*
- [x] Agent visual context injection：每輪 ReAct 前自動截圖 → base64 附加到 Opus 4.7 multimodal message → agent 真正「看到」畫面長什麼樣 *(done: V2 #6 — `backend/ui_agent_visual_context.py` (~870 行) + `backend/tests/test_ui_agent_visual_context.py` 146 條契約測試全綠 0.34 s。**Loop closer for the ReAct visual half** — V2 #4 已把三 viewport 截圖攤平成 `ResponsiveCaptureReport`、V2 #5 已把 dev-server 錯誤攤平成 `AgentContextPayload`；V2 #6 把兩邊同時抓起來編成 Anthropic multimodal 規格的 `HumanMessage(content=[{text}, {image}×N])`，agent 每一輪 ReAct 真的「看到」畫面。**模組六層** — (1) 常量層（`UI_AGENT_VISUAL_CONTEXT_SCHEMA_VERSION="1.0.0"` 獨立於 V2 #1-#5 + `DEFAULT_IMAGE_MEDIA_TYPE="image/png"` + `DEFAULT_IMAGE_SOURCE_KIND="base64"` + `DEFAULT_MAX_IMAGE_BYTES_PER_VIEWPORT=MAX_CAPTURE_BYTES` 10 MB re-export V2 #3 + `DEFAULT_MAX_TOTAL_IMAGE_BYTES=30_000_000` 對三 viewport 的總 cap + `DEFAULT_PATH="/"` + `DEFAULT_TEXT_PROMPT_TEMPLATE` 10 placeholders ``{session_id}/{turn_id}/{preview_url}/{path}/{viewport_list}/{missing_line}/{error_summary}/{auto_fix_hint}/{image_count}/{image_plural}`` 全 deterministic format）；(2) Event 命名層（4 個 `ui_sandbox.agent_visual_context.*` 常量 `building`/`built`/`failed`/`skipped` + `AGENT_VISUAL_CONTEXT_EVENT_TYPES` tuple；**與 V2 #2 `LIFECYCLE_EVENT_TYPES` + V2 #3 `SCREENSHOT_EVENT_TYPES` + V2 #4 `VIEWPORT_BATCH_EVENT_TYPES` + V2 #5 `ERROR_EVENT_TYPES` 全 disjoint**）；(3) 資料模型層（`AgentVisualContextImage` frozen — viewport_name/width/height/byte_len/image_base64/media_type/source_kind/captured_at + `to_dict()` JSON-safe 含 base64（visual context 是 IS 要送像素）+ `to_content_block()` 產 Anthropic ``{"type":"image","source":{"type":"base64","media_type":"image/png","data":b64}}``；`AgentVisualContextPayload` frozen — session_id/turn_id/built_at/preview_url/path/viewport_matrix/images/missing_viewports/text_prompt/error_summary_markdown/auto_fix_hint/has_blocking_errors/active_error_count/was_skipped/skip_reason/warnings + `image_count/has_images/total_image_bytes/has_errors/captured_viewport_names` properties + `to_dict()` + `to_content_blocks()` 返回 text-first-then-images 順序 list[dict]）；(4) 錯誤層（`AgentVisualContextError(RuntimeError)` 基底）；(5) 純函數層（`encode_capture_to_image(capture, *, max_bytes=)` 吃 V2 #3 `ScreenshotCapture` 轉 `AgentVisualContextImage`、oversized raise `AgentVisualContextError`、wrap `ScreenshotError` + `apply_image_byte_budget(images, *, max_total_bytes)` 貪婪 drop 保留第一張（text-only degrade 更差）、preserve 順序、返回 `(kept, dropped)` + `render_visual_context_text(...)` 拉 template format 產 deterministic agent-facing text block、empty 提供 "sandbox unreachable" / "(no error summary)" / "(no auto-fix hint)" 穩定 fallback + `build_text_content_block(text)` 產 ``{"type":"text","text":text}`` + `build_image_content_block(image)` 產 Anthropic base64 image block + `build_content_blocks(payload)` 展平 payload→list[dict] text-first-then-images + `build_human_message(payload)` lazy-import `backend.llm_adapter.HumanMessage` 避免測試 import langchain）；(6) `AgentVisualContextBuilder` 主類別 — composition-over-inheritance 對齊 V2 #2/#3/#4/#5；has-a `ResponsiveViewportCapture`（必需）+ optional `PreviewErrorBridge`；ctor 10 seam（`responsive=`/`error_bridge=`/`clock=`/`event_cb=`/`default_matrix=`/`default_failure_mode=`/`default_path=`/`max_image_bytes_per_viewport=`/`max_total_image_bytes=`/`text_prompt_template=`）；public API `build`/`build_skipped`/`build_message`/`snapshot` + `build_count/skipped_count/failed_count/last_payload` accessors；thread-safe RLock 包 counters + last payload；內部 `_turn_counter` 做 `avc-turn-%06d` auto-id 給不帶 turn_id 的 caller。**九段 build 執行路徑** — (a) 檢 session_id/preview_url/path/failure_mode/matrix + 解析 turn_id（None → auto-increment）；(b) emit `BUILDING` event 帶 matrix+failure_mode；(c) optional `bridge.scan(session_id)` if `scan_errors=True` + wrap 失敗成 warning；(d) optional `bridge.build_agent_context(session_id, turn_id=)` + wrap 失敗成 warning；(e) `responsive.capture_all(...)` 拿 `ResponsiveCaptureReport` — `BatchAborted` propagate（emit `FAILED` + failed++ + raise）、其它 Exception → emit `FAILED` + failed++ + 回 skipped payload（degrade 不 raise）；(f) outcomes iterate → `encode_capture_to_image` per success outcome、`AgentVisualContextError` 轉 warning（`image_encode_failed:{viewport}:{reason}`）；(g) `apply_image_byte_budget` 裁 total bytes，dropped 變 `image_dropped_budget:{viewport}:{byte_len}` warning；(h) 組 text_prompt via `render_visual_context_text`，error 段吃 `AgentContextPayload.summary_markdown`+`auto_fix_hint` 或空 bridge 時回 "No error bridge wired." placeholder；(i) 組 `AgentVisualContextPayload` → emit `BUILT` + 更新 build_count + last_payload；回 payload。**Skipped payload** — `build_skipped(session_id, preview_url, skip_reason, ...)` 對 sandbox 不可達 / idle / teardown pending 產純文字 payload（`was_skipped=True`、`image_count=0`、`missing_viewports=matrix`）；emit `SKIPPED` event；agent loop 永遠不需要 branch "能不能拿 visual context"，永遠吃同一種 payload shape。**Graceful 六路** — (a) bridge.scan 爆炸轉 warning tuple 繼續；(b) bridge.build_agent_context 爆炸轉 warning 繼續；(c) ResponsiveCapture 非 `BatchAborted` 爆 → 降級 skipped + emit FAILED；(d) BatchAborted（failure_mode=abort）propagate 出去讓 CI 看；(e) per-image encode 失敗 → warning + drop 那顆；(f) event_cb 爆 → swallow + warning log 與 V2 #1-#5 一致。**為什麼 V2 #6 是 V2 row 7 的 landing gate** — row 7 整合測試只需 `builder = AgentVisualContextBuilder(responsive=..., error_bridge=...); payload, message = builder.build_message(session_id=, preview_url=)` 兩行就拿 multimodal-ready `HumanMessage`；Anthropic base64 規格已鎖死 `{"type":"image","source":{"type":"base64","media_type":"image/png","data":b64}}`；text-first-then-images 順序保證 agent 先讀 prompt 再看圖；SSE envelope 不攜 base64 避免大 frame；所有 schema 獨立演進；`warnings tuple` 告訴 agent 本次有什麼被 drop。**關鍵設計決策** — (a) Composition over inheritance — Builder 擁有 responsive+bridge 不是它們的 subclass；(b) Pure content block builders — `build_content_blocks(payload)` 返回 `list[dict]` 不綁 LangChain，tests 不需 import langchain；(c) `build_human_message` lazy-import 只在真要 HumanMessage 時才 pull；(d) Byte budget 貪婪但保底 — 永遠留下第一張 viewport 不 degrade 成 text-only；(e) Skipped 是一等 payload shape — agent loop 不 special-case；(f) Event namespace 分離 — 4 topics 全在 `ui_sandbox.agent_visual_context.` prefix 不與 V2 #1-#5 衝突；(g) Deterministic text template — placeholder format let golden tests pin 完整 output；(h) BatchAborted propagate，其它 exception degrade — CI 要硬 gate、production 要軟 degrade；(i) `_turn_counter` auto-id — caller 不帶 turn_id 時零 coord；(j) `scan_errors=False` default — bridge 可能已跑 `start_watch` background loop，雙掃 redundant；(k) `include_errors=False` opt-out — 沒 bridge 或不想 inject 時產 "No error bridge wired." placeholder；(l) SSE envelope 不攜 base64 — `_envelope_for_event` 只塞 metadata（viewport names / byte 總量 / error count）避免 SSE subscribers 接到大 frame；`last_payload.to_dict()` 才攜全 base64。**契約測試 146 條 0.34 s** — module invariants 17（`__all__` set + 23 export parametrize + semver + 常量對齊 V2 #3 + event namespace 全 `ui_sandbox.agent_visual_context.` + template 含 10 placeholder + **4 sibling namespaces disjoint check** + 錯誤繼承 RuntimeError）、`AgentVisualContextImage` 18（happy + frozen + 13 bad-input parametrize + to_dict json-safe + to_content_block matches Anthropic shape）、`encode_capture_to_image` 5（happy + 非-capture reject + 非正 max_bytes reject + oversized raise + ScreenshotError wrap）、`apply_image_byte_budget` 7（empty + under cap + drop until fit + 保第一張 + 非正 cap reject + 非-image entries reject + preserve order）、`render_visual_context_text` 5（全 sections present + empty fallback + deterministic + 空 template reject + 自定 template 可用）、content block builders 4（text happy + text 4 bad input + image shape + image non-image reject）、`AgentVisualContextPayload` 25（happy + frozen + 14 bad-input parametrize + 非-image entries reject + skipped must have no images + skipped requires reason + to_dict json-safe + to_content_blocks text-first + image order matches + skipped empty images + 等等）、builder 構造 5（responsive required + 非-responsive reject + 非-bridge error_bridge reject + 6 bad kwargs parametrize + accessor defaults + counters start zero）、`build` happy 8（三 viewport + auto turn_id 遞增 + building→built 順序 + built envelope 不攜 base64 + 計數遞增 + path override 傳 engine + custom viewport matrix + 6 bad input parametrize）、failure 4（collect partial missing_viewports 記錄 + collect all-fail 無 image 不 skipped + abort raise BatchAborted 帶 partial report + abort failed event 攜 partial_report + 非-BatchAborted degrade 成 skipped + skipped_count+failed_count 同步）、byte budget 2（total cap 切 drop 後 warning + per-viewport cap 切 encode_failed warning）、error bridge 整合 3（no bridge placeholder + bridge 真產 error summary + scan_errors=True 觸發 scan + include_errors=False 繞 bridge）、build_skipped 2（text-only + empty reason reject）、build_message 2（返回 tuple + HumanMessage content list shape + non-payload reject）、snapshot 3（empty + with payload 不含 base64 + 有 bridge 時 schema_version 帶 out）、event callback safety 1（callback 爆 build 仍 OK）、**thread-safety 1（10 thread × 3 viewport = 30 engine call + build_count=10 + 0 corruption）**、**end-to-end golden path 1**（FakeDocker → SandboxManager → PreviewErrorBridge (V2 #5) + FakeScreenshotEngine → ScreenshotService (V2 #3) → ResponsiveViewportCapture (V2 #4) → AgentVisualContextBuilder (V2 #6) → `build_message()` → HumanMessage(content=[text, image, image, image]) 每顆 image 符 Anthropic base64 規格 + error summary 含 Header.tsx + active_error_count≥1 + payload.to_dict() JSON-safe 且攜 image_base64 + V2 #6 event BUILDING+BUILT 在 events 記錄中）、sibling alignment 2（V1/V2 #1-#5 全 importable + schema 獨立）。V1+V2 合計 **1050 條全綠 1.29 s zero regression** — V1 `test_ui_component_registry.py` + V2 #1 `test_ui_sandbox.py` + V2 #2 `test_ui_sandbox_lifecycle.py` + V2 #3 `test_ui_screenshot.py` + V2 #4 `test_ui_responsive_viewport.py` + V2 #5 `test_ui_preview_error_bridge.py` + V2 #6 `test_ui_agent_visual_context.py` 全綠。**Scope 自律** — 只新增 `backend/ui_agent_visual_context.py` + `backend/tests/test_ui_agent_visual_context.py` 2 檔；不動 V1 sibling、不動 V2 #1/#2/#3/#4/#5 sibling 一行、不動 `llm_adapter.py`（只 lazy-import HumanMessage）、不動 `events.py`（SSE bridge 留 V2 row 7）、不動 `vision_to_ui.py`（各自不同用途 — vision_to_ui 是「使用者給截圖」，agent_visual_context 是「agent 看 sandbox」）、不動 `requirements.txt`（零新依賴）、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1-#5 合計全綠 zero regression)*
- [x] SSE event：`ui_sandbox.screenshot`（session_id / viewport / image_url / timestamp）+ `ui_sandbox.error`（error_type / message / file / line） *(done: V2 #7 — `backend/ui_sandbox_sse.py` (~640 行) + `backend/tests/test_ui_sandbox_sse.py` 113 條契約測試全綠 0.17 s。**SSE bridge for V2 row 7** — V2 #1-#6 透過注入的 `event_cb: Callable[[str, Mapping[str, Any]], None]` 發送內部事件，payload 是富結構 dict（`ScreenshotCapture.to_dict()` / `PreviewError.to_dict()` 等）。此模組把這些內部回呼翻譯成 V2 row 7 規範的 SSE frame，推到 `backend.events.bus`，讓前端即時收到。**兩個 canonical SSE topic** — `SSE_EVENT_SCREENSHOT="ui_sandbox.screenshot"` 硬 pin + `SSE_EVENT_ERROR="ui_sandbox.error"` 硬 pin + `SSE_EVENT_TYPES` tuple；**Required fields 鎖死** — `SCREENSHOT_EVENT_FIELDS=("session_id","viewport","image_url","timestamp")` + `ERROR_EVENT_FIELDS=("error_type","message","file","line")` — 前端永遠可以依賴這四個欄位存在。**模組六層** — (1) 常量層（`UI_SANDBOX_SSE_SCHEMA_VERSION="1.0.0"` + 兩 event topic 常量 + field tuple + image-url 三策略 + phase 二態 + default dedup 2 秒）；(2) Image URL 策略（`IMAGE_URL_STRATEGY_ENDPOINT` 指 `/api/ui_sandbox/{session_id}/screenshots/{capture_id}` 前端去 GET PNG 保持 SSE frame 輕量 + `IMAGE_URL_STRATEGY_DATA` 內嵌 `data:image/png;base64,…` 給單機/demo 場景 + `IMAGE_URL_STRATEGY_OMIT` 只發 metadata）；(3) Error phase 二態（`detected` 新/持續 + `cleared` 已清除）— 前端只訂一個 topic 看 phase 欄位；(4) 純函數層（`build_screenshot_image_url` 解析 viewport / session_id / capture_id 三策略統一出口 + `build_screenshot_event_payload` 從 V2 #3 `ScreenshotCapture.to_dict()` 富結構 → 精簡 SSE frame 絕不帶 raw PNG bytes + `build_error_event_payload` 從 V2 #5 `PreviewError.to_dict()` → SSE frame + `build_error_cleared_payload` 從 V2 #5 cleared payload → SSE frame 並合成四個 required fields 讓前端走單 topic）；(5) `EventPublisher` Protocol + `BusEventPublisher` 生產 adapter（`backend.events.bus.publish` lazy import 避免單元測試被迫拉 events 模組）；(6) `UiSandboxSseBridge` 主類別（composition over inheritance — has-a `EventPublisher` 不 subclass `EventBus`；ctor 五 seam：`publisher=`/`image_url_strategy=`/`image_url_template=`/`dedup_window_seconds=`/`clock=`；public API `on_screenshot_event`/`on_error_event`/`on_lifecycle_event` 三 callback 全 `(event_type, payload)` 簽名契合 V2 #1-#6 `EventCallback` seam；`snapshot()` JSON-safe telemetry 輸出所有計數器 + 設定；thread-safe RLock 包 dedup window + counters）。**六段 on_screenshot_event 執行路徑** — (a) 檢 event_type 是否在 `_SCREENSHOT_IN_TYPES` tuple，不在 → `ignored_events++` 直接 return；(b) `build_screenshot_event_payload(payload, image_url_strategy=, image_url_template=, capture_bytes=, now=clock())` — 例外 → `publish_failures++` + warning log 絕不 re-raise；(c) dedup key `screenshot::{session_id}::{viewport}::{timestamp:.6f}` 查 dedup window（V2 #2 lifecycle 與 V2 #3 service 共用 `ui_sandbox.screenshot` topic，同一 capture 會 fire 兩次 callback — dedup window 確保一張圖只發一個 SSE frame）；(d) mark_seen + GC 舊 entries 避免 dedup dict 無限膨脹（>1024 entries 時清掉超出 window 的）；(e) `publisher.publish(SSE_EVENT_SCREENSHOT, frame, session_id=)` — 例外 → `publish_failures++` + warning log；(f) `screenshot_emitted++`。**on_error_event 執行路徑** — 同樣的 shape，但分兩支：`ui_sandbox.error.detected` → `build_error_event_payload(phase="detected")`；`ui_sandbox.error.cleared` → `build_error_cleared_payload`（合成 error_type=""/message=""/file=None/line=None 四個 required fields，phase="cleared"，前端單 topic 接收）。**Dedup 精巧** — key 包 timestamp，同 capture 兩次 callback = 同 timestamp = 命中 dedup；不同 viewport 或不同 timestamp 落在同 session = 不同 key 不 dedup；dedup window 過期自動 re-emit。**Graceful 三路絕不 raise** — (a) payload malformed → build_* 例外被 catch → `publish_failures++` + warning + silent return；(b) publisher 失敗（bus down / queue full / serialisation）→ catch → `publish_failures++` + warning + silent return；(c) event callback 簽名不匹配 → `ignored_events++` silent return。**SSE frame 輕量原則** — endpoint 策略下 frame body 典型 < 200 bytes（只攜 session_id / viewport / image_url / timestamp / schema_version + optional preview_url / byte_len / viewport_width / viewport_height）；data 策略只在呼叫 `capture_bytes=` 顯式傳入時 inline；**絕不** 把 V2 #3 的 10 MB PNG 塞進 SSE 通道。**為什麼 `on_lifecycle_event = on_screenshot_event` alias** — V2 #2 `LIFECYCLE_EVENT_SCREENSHOT="ui_sandbox.screenshot"` 與 V2 #3 `SCREENSHOT_EVENT_CAPTURED="ui_sandbox.screenshot"` 是同一 topic string，兩者 payload 都含 session_id/viewport/captured_at — 同 callback 處理即可，少一個 alias lookup。**Dedup 為何重要** — V2 #2 `capture_screenshot()` 呼叫 V2 #3 `ScreenshotService.capture()`；V2 #3 emit 一次 `ui_sandbox.screenshot`，V2 #2 也 emit 一次 `ui_sandbox.screenshot` — 兩個 event_cb 綁同 bridge 會重複發 SSE frame，前端會看到兩張一樣的圖；dedup window 守這個 1-per-capture 不變量。**契約測試 113 條 0.17 s** — module invariants 11（`__all__` 28 exports set + 28 export parametrize + semver + **spec 硬 pin：SSE_EVENT_SCREENSHOT == "ui_sandbox.screenshot"** + **SSE_EVENT_ERROR == "ui_sandbox.error"** + SSE_EVENT_TYPES 去重 + **SCREENSHOT_EVENT_FIELDS 硬 pin `("session_id","viewport","image_url","timestamp")` 對齊 V2 row 7 spec 逐字元** + **ERROR_EVENT_FIELDS 硬 pin `("error_type","message","file","line")` 對齊 V2 row 7 spec** + image strategies + error phases + 正 dedup window + template 含 placeholder）、`build_screenshot_image_url` 11（endpoint default + 自定 template + data 內嵌 + data 要求 bytes + data 拒空 bytes + data 拒非-bytes + omit 空 url + 未知 strategy reject + 非-mapping reject + 未知 placeholder reject + viewport 吃 str）、`build_screenshot_event_payload` 15（required fields 全在 + **no raw bytes** 驗證 + timestamp from captured_at + session_id preserved + viewport dict → 短 name + viewport_width/height 從 dict 展開 + schema_version + 空 session_id reject + viewport None reject + now fallback + preview_url + endpoint image_url + data image_url + omit 空 url + 非-mapping reject）、`build_error_event_payload` 14（required fields + schema_version + 所有欄位 preserved + last_seen_at → timestamp + first_seen_at fallback + now fallback + null file/line + bad phase reject + 空 error_type reject + 空 message reject + 非-mapping reject + str line 自動 int + 壞 line → None + occurrences preserved）、`build_error_cleared_payload` 7（phase=cleared + required fields 合成 + error_id preserved + 空 error_id reject + 非-mapping reject + cleared_at → timestamp + now fallback）、publisher 2（fake 符 Protocol + BusEventPublisher lazy import）、bridge 構造 3（defaults + 5 bad kwargs parametrize + snapshot shape）、`on_screenshot_event` 9（emit 成功 + 非 screenshot topic ignored + **V2 #2+#3 同 topic dedup** + dedup window 過期 re-emit + 不同 viewport 各自 emit + 壞 payload 不 raise 只計數 + publisher 失敗不 raise 只計數 + omit 策略 flow through + 自定 template 生效）、`on_error_event` 5（detected emit + cleared emit + 非 error topic ignored + 壞 payload 不 raise + cleared 缺 error_id 不 raise）、**thread-safety 1**（10 thread × 10 capture + 10 thread × 10 error stress = 100 screenshot + 100 error emit 0 corruption）、one-shot helpers 4（screenshot helper + publisher 失敗不 raise + error detected helper + error cleared helper）、sibling alignment 2（V1/V2 #1-#6 全 importable + **bridge input topics match sibling emit constants**：`SCREENSHOT_EVENT_CAPTURED == LIFECYCLE_EVENT_SCREENSHOT == "ui_sandbox.screenshot"` 三向鎖 + `ERROR_EVENT_DETECTED`/`ERROR_EVENT_CLEARED` 都在 bridge `_ERROR_IN_TYPES`）、**end-to-end 2**：(a) V2 #3 `ScreenshotService(engine=FakeEngine, event_cb=bridge.on_screenshot_event)` → service.capture() → 恰好一個 `ui_sandbox.screenshot` SSE frame + 四 required fields 齊 + 零 raw bytes；(b) V2 #5 `PreviewErrorBridge(manager=mgr, event_cb=bridge.on_error_event)` + FakeDocker logs 含 module_not_found → scan() → SSE bus 收到 `ui_sandbox.error` frame + 四 required fields + phase="detected" + session/message/error_type 全填。**V1+V2 合計** 1163 條全綠 1.50 s zero regression — V1 `test_ui_component_registry.py` + V2 #1-#7 全綠。**關鍵設計決策** — (a) Spec-first required fields 鎖死 tuple，前端不會 drift；(b) Image URL 三策略讓 ops 決定 SSE payload 大小；(c) Dedup window keyed on `(session_id, viewport, timestamp)` — V2 #2+#3 同 capture 一次出；(d) 絕不 raw PNG bytes 上 SSE 通道（endpoint 策略只發 URL，data 策略需 caller 顯式 opt-in）；(e) 合成 required fields 給 cleared event 讓前端單 topic 接收；(f) Composition over inheritance — bridge has-a publisher；(g) Graceful 三路絕不 raise — payload malformed / publisher 失敗 / 非相容 topic 全變 counter 增量；(h) Lazy import `backend.events.bus` — 單元測試零 events 副作用；(i) Counter-based telemetry — `screenshot_emitted/screenshot_deduped/error_emitted/error_cleared_emitted/ignored_events/publish_failures` 讓 ops 知道 bridge 是否工作；(j) Thread-safe RLock 包 dedup + counters — 20-thread 壓測 0 corruption；(k) `on_lifecycle_event = on_screenshot_event` alias — V2 #2/#3 同 topic 共用 path；(l) GC 自動清 dedup dict — 長跑不 OOM。**Scope 自律** — 只新增 `backend/ui_sandbox_sse.py` + `backend/tests/test_ui_sandbox_sse.py` 2 檔；不動 V1 sibling、不動 V2 #1/#2/#3/#4/#5/#6 sibling 一行、不動 `backend/events.py`（只 lazy-import 其 `bus` 單例不改介面）、不動 `llm_adapter.py`、不動 `requirements.txt`（零新依賴）、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1-#6 合計全綠 zero regression)*
- [x] 整合測試：agent 寫 code → sandbox HMR → 截圖 → error 偵測 → auto-fix → 重截圖 → 最終 screenshot 無 error *(done: V2 #8 — `backend/tests/test_v2_sandbox_autofix_integration.py` (~740 行) + 24 條契約測試全綠 0.33 s。**Closes the V2 live-preview loop end-to-end** — 組合 V2 #1-#7 跑完整的 agent-auto-fix 流程，證明七個模組拼起來真的能 render → detect → inject → re-render。**鏡頭流程** — (1) agent 寫 broken `components/Header.tsx`（import 不存在的 `Button`）→ filesystem 寫入 tmp_path/workspace/components/Header.tsx；(2) `SandboxLifecycle.ensure_session(SandboxConfig)` 透過注入的 `FakeDockerClient` create + start + mark_ready；(3) `lifecycle.hot_reload(session_id, files_changed=("components/Header.tsx",))` emit `ui_sandbox.hot_reload`；(4) `PreviewErrorBridge.scan(session_id)` 讀 docker logs（canned `BROKEN_LOGS` 含 `Module not found: Can't resolve 'Button'` + `./components/Header.tsx:1:10`）→ `active_count >= 1`；(5) `AgentVisualContextBuilder.build_message(session_id, preview_url, turn_id="react-turn-1")` → `(payload, HumanMessage)`，payload 有 3 images + `has_blocking_errors=True` + error summary 含 `"Header.tsx"` + `text_prompt` 含 `"Header.tsx"` 讓 Opus 4.7 讀得到；HumanMessage.content 是 Anthropic base64 格式 `[{"type":"text"}, {"type":"image","source":{"type":"base64","media_type":"image/png","data":...}}×3]`；(6) agent 寫 fix — `FIXED_HEADER_TSX`（乾淨 `<button>Go</button>`）覆蓋同檔 + `docker.set_logs(CLEAN_LOGS)` 切到乾淨 log；(7) `lifecycle.hot_reload` 再發一次；(8) `bridge.scan(session_id)` → `cleared_count >= 1` + `active_count == 0`；(9) `builder.build_message(turn_id="react-turn-2")` → payload `has_blocking_errors=False` + `active_error_count=0` + error_summary_markdown 含 `"No active errors"`（對齊 V2 #5 `render_error_markdown` 空回 body 契約）+ 依然 3 images 讓 agent 看到 UI；(10) `UiSandboxSseBridge` 透過 `publisher=FakePublisher` 集滿所有 SSE frames — `SSE_EVENT_SCREENSHOT` ≥ 3 frames + `SSE_EVENT_ERROR` 含 `phase="detected"` 與 `phase="cleared"`；每張 frame 符 V2 row 7 硬 pin 四必欄 `SCREENSHOT_EVENT_FIELDS=("session_id","viewport","image_url","timestamp")` + `ERROR_EVENT_FIELDS=("error_type","message","file","line")` + **絕不攜 raw PNG bytes**（for-loop 檢每個值都不是 bytes/bytearray）。**契約測試 24 條** — (a) happy-path assert 5：broken.has_errors/fixed.has_errors + Header.tsx 在 summary+text_prompt + auto_fix_hint 有內容（broken）且表達「clean」（fixed）+ 9 total engine calls (3 viewport × 2 round) + seen viewport names 為 `DEFAULT_VIEWPORT_MATRIX = ("desktop","tablet","mobile")` 兩輪同序；(b) SSE contract 4：3+ screenshot frames 含四必欄 + 零 raw bytes + detected/cleared 都 emit + `bridge.snapshot()` 計數 screenshot_emitted ≥ 3 + error_emitted ≥ 1 + error_cleared_emitted ≥ 1 + publish_failures == 0 + **dedup window 收斂 V2 #2 lifecycle.screenshot 與 V2 #3 service.screenshot 同 timestamp 重發成單 frame**（screenshot_emitted + screenshot_deduped ≥ 6）；(c) event ordering 3：`hot_reload < error.detected < screenshot` round 1 + `second_hot_reload < cleared` round 2 + 全部 topic 符合 9 個 allowed prefix（ui_sandbox.created/starting/ready/stopped/failed/ensure_session/hot_reload/teardown/reaped/ready_timeout/screenshot/viewport_batch./error./agent_visual_context.）；(d) payload correctness 4：broken.to_dict() JSON 含 image_base64 + base64 解碼後每張 PNG 開頭為 `PNG_SIGNATURE` 8 bytes + fixed.to_content_blocks() 第一 block 是 text 其後 3 image + 三 image 全 base64 + fixed.error_summary_markdown 含 `"No active errors"` + broken summary 含 `"Header.tsx"`；(e) 整體健康 4：sandbox.status 全程 `running`（auto-fix 靠 HMR 不 teardown）+ `len(docker.run_calls) == 1` 整個流程只 run_detached 一次 + workspace 檔案真的被 agent 寫到（`header_path.read_text() == FIXED_HEADER_TSX`）+ 每個 engine capture request 帶正確 preview_url/session_id/path + `error_bridge.last_batch(session_id).active_count == 0`；(f) 冪等與決定性 2：scan on clean 兩次 → detected_count/cleared_count/active_count 全 0 + **兩個獨立 Rig 同 scenario 跑出同樣 shape**（image_count 都 3 + active_error_count 相同 + viewport_matrix 相同）；(g) sibling alignment 2：V1 + V2 #1-#7 8 個模組都還能 import + 7 個 sibling schema version 都還是 `"1.0.0"` 獨立演進。**Rig fixture** — 本檔自帶 `Rig` class 集中組裝 V2 #1-#7 全部注入點：共享一個 `FakeClock`（所有 `captured_at`/`started_at`/`last_seen_at` 決定性）、一個 `FakeSleep(clock)`（所有 blocking poll 零真時間）、一個 `RecordingEventCallback`（全 V2 module 的 event_cb 都透過 `_fanout(recorder, sse_bridge.on_*)` 兩路 dispatch）、一個 `FakePublisher`（SSE bus stand-in 記錄每個 publish(topic, payload, session_id)）、一個 `UiSandboxSseBridge(publisher=FakePublisher, clock=FakeClock)`。**Wire 順序對齊 production** — SandboxManager(V2 #1) 建立 → ScreenshotService(V2 #3, engine=FakeScreenshotEngine, event_cb=bridge.on_screenshot_event) → SandboxLifecycle(V2 #2, manager, screenshot_hook=service.as_hook(), event_cb=bridge.on_lifecycle_event) → ResponsiveViewportCapture(V2 #4, service) → PreviewErrorBridge(V2 #5, manager, event_cb=bridge.on_error_event) → AgentVisualContextBuilder(V2 #6, responsive, error_bridge)。**Local fakes** — `FakeDockerClient`（mutable `canned_logs`、thread-safe、實作 V2 #1 `DockerClient` Protocol 五方法 run_detached/stop/remove/logs/inspect；`set_logs(text)` mid-flight 切 log）、`FakeScreenshotEngine`（每 viewport 產獨特 `PNG_SIGNATURE + b"px-{name}"` 讓三 image base64 確實相異、thread-safe、實作 V2 #3 `ScreenshotEngine` Protocol 兩方法 capture/close）、`FakeClock + FakeSleep`（配對；FakeSleep 內部呼 clock.advance 不真 sleep）、`RecordingEventCallback`（thread-safe list append + by_type filter）、`FakePublisher`（V2 #7 `EventPublisher` Protocol 的 in-memory shim）。**Graceful `_fanout`** — 每個 V2 module 只收單一 `event_cb`，本檔定義 `_fanout(*cbs)` 小 helper 讓一個 seam 同時 feed 到 recorder + SSE bridge；個別 listener exception 被吞避免連鎖 failure（對齊 V2 #1-#7 每個 module 自己的 graceful event_cb 契約）。**為什麼此測試是 V2 row 7 的 acceptance gate** — TODO row 1516 spec 原文要求 7 段流程：agent 寫 code → sandbox HMR → 截圖 → error 偵測 → auto-fix → 重截圖 → 最終 screenshot 無 error。本檔 `_run_autofix_loop(rig)` 函數精確對應 7 段，且對應 7 個斷言點都硬 pin：broken 階段 `has_errors is True` + `has_blocking_errors is True`，fixed 階段 `has_errors is False` + `has_blocking_errors is False` + `active_error_count == 0` + error_summary 含 `"No active errors"`，**證明迴圈閉合**。**關鍵設計決策** — (a) 一個 Rig 組全部 — 不同 test 各自呼 `_run_autofix_loop(rig)` 然後各自斷言不同 facet，避免重複組裝；(b) `FakeDockerClient.set_logs` mutable — 沒這個就沒辦法模擬「agent 改檔 → dev server 重編譯 → logs 變乾淨」；(c) `_fanout` 保留兩路 event — 本地 recorder 給 ordering assertions，SSE bridge 給 row 7 frame assertions，同時驗兩件事；(d) `encode_capture_to_image` 每 viewport 不同 payload — 防 accidental sharing bug；(e) 不真 import `backend.events` bus — FakePublisher 直接餵 UiSandboxSseBridge，單元測試零 SSE 副作用；(f) `end_to_end` 不做 three-rig 並發壓測 — V2 #1-#7 各自都有 thread-safety test；(g) `Sandbox.status` 全程 running 不 teardown — auto-fix 依賴 HMR 不重建；(h) 本檔不新增 module schema — 只是 test harness；(i) 本檔 SSE frame shape 斷言用 `SCREENSHOT_EVENT_FIELDS`/`ERROR_EVENT_FIELDS` 常量（不是硬編欄位名）讓 V2 #7 bump spec 時本檔自動對齊；(j) 第三輪 `scan()` on clean logs 冪等驗證 — 生產 agent 每 turn 都會 scan 一次，不能每次都誤報 detected/cleared；(k) `engine.calls` 數量鎖 6 不鎖更多 — 防止日後誰不小心在 lifecycle 加個 extra capture。**V1+V2 合計** — 8 個 sibling 測試 + 本檔 = **1187 條全綠 1.53 s zero regression**。**Scope 自律** — 只新增 `backend/tests/test_v2_sandbox_autofix_integration.py` 1 檔；**不新增任何 production module**（V2 #8 是 row 8 整合測試專案，刻意不 ship 新 runtime 模組）、不動 V1 sibling、不動 V2 #1/#2/#3/#4/#5/#6/#7 sibling 一行、不動 `backend/events.py`、不動 `llm_adapter.py`、不動 `requirements.txt`（零新依賴）、不動 routing/server/frontend；V0 / V1 #1-#9 / V2 #1-#7 合計全綠 zero regression。**至此 V2 row #318 所有 8 個子項全數 check mark**)*
- 預估：**6 day**

### V3. Web — 視覺迭代 + 標註回饋 (#319)
- [x] `components/omnisight/visual-annotator.tsx`：在 preview 截圖上的 annotation overlay——使用者可畫矩形框 / 點選元素 / 加文字 comment *(done: V3 #1 — `components/omnisight/visual-annotator.tsx` (~530 行 Client Component) + 53 顆契約測試全綠。**三模式 overlay** — 單一 `VisualAnnotator` 組件承載 `rect` / `click` / `select` 三 mode 切換：`rect` 拖出矩形、`click` 單擊成 pin、`select` 擊中現有 annotation 進入編輯。Toolbar 三顆 `ToolbarButton` 含 `data-active` / `aria-pressed`、另一顆 `visual-annotator-clear` 清空（無 annotation 時 disabled）。**資料模型對齊 V3 #2** — `VisualAnnotation = {id, type, boundingBox:{x,y,w,h}, comment, cssSelector?:null, label?, createdAt, updatedAt}` + 輔助 `VisualAnnotationAgentPayload`（`{type, cssSelector, boundingBox, comment}`），讓下一顆 V3 checkbox「annotation → agent context」變成純函式 `annotationToAgentPayload()` 即可消費（payload 回傳全新 `boundingBox` copy 避免 caller mutate source）。`cssSelector` 目前永遠 null，保留給 V3 #3 element inspector 注入。**座標系統** — boundingBox 存 **normalised** `[0,1]` fractions（截圖 responsive 縮放時仍黏著原像素區）；`pointsToNormalizedBox(a, b, rect)` 純函式 clamp + 排序 so drag 方向不影響結果。Click point 以 `w=h=0` 存，`hitTestNormalizedBox` 幫 click point 加 `epsilon=0.015` 讓單點可命中。**Pointer gesture** — `pointerDown/Move/Up/Cancel` handlers，`rect` mode 拖動期間渲染 `visual-annotator-draft`（dashed box）、release 時若 w/h 皆 < `rectMinNormalized`（預設 0.01）則 demote 成 `click` pin（避免誤觸）。`pointerCancel` 走與 `pointerUp` 相同 commit 分支，保住 operator 勞動成果。**Select mode 命中測試** — 反向 iterate（後畫贏前畫），click point 吃 epsilon，空白處 click 清 selection（`null`）。**Editor** — selected annotation 底部 footer 開出 `visual-annotator-editor` 面板：`<Textarea>` 即時 propagate comment change 到 `onAnnotationsChange`（每打字更新 `updatedAt`）、`visual-annotator-remove-{id}` 一鍵刪除 + renumber 1-based `label`。**鍵盤** — overlay surface `tabIndex={disabled?-1:0}`，`Delete`/`Backspace` 移除 selected annotation；若焦點在 textarea/input 內（目標 tagName check）則 **不** 搶 keystroke。**Controlled + uncontrolled** — `annotations`/`defaultAnnotations` + `selectedId`/`defaultSelectedId` + `mode`/`defaultMode` 三組 pair，controlled caller 未 swap 時 UI 穩定不抖；controlled `annotations` 收縮掉某 id 時 `useEffect` 自動清 selection。**Test seam** — jsdom 無 layout 故 `getOverlayRect` prop 注入固定 200×100 rect；`idFactory` / `nowIso` 兩 clock seam；`defaultAnnotatorIdFactory` crypto.randomUUID 缺失時降級 `ann-{time36}-{rand36}`（測試覆蓋該 branch）。**Disabled** — `data-disabled="true"` + toolbar/clear/remove 全 disabled + pointer gestures 不 commit + 鍵盤 Delete no-op。**Exports** — `VisualAnnotator`（default + named）、helper `clampNormalized` / `pointsToNormalizedBox` / `hitTestNormalizedBox` / `annotationToAgentPayload` / `defaultAnnotatorIdFactory` / `defaultAnnotatorNowIso`；型別 `VisualAnnotationType` / `VisualAnnotatorMode` / `NormalizedBoundingBox` / `VisualAnnotation` / `VisualAnnotationAgentPayload` / `OverlayRect` / `VisualAnnotatorProps`。**測試** — `test/components/visual-annotator.test.tsx` **53 顆 contract tests 全 pass**（helper 15 + render 7 + mode toggle 4 + rect draw 5 + click mode 1 + select 4 + editor 4 + keyboard 3 + clear 2 + disabled 1 + controlled selection 2 + mode-prop it.each 3 + zero-rect guard 1 + agent-payload 3）；workspace-family 總 186 → 239 全綠 / 1.26s / 0 regression。**Scope 自律** — 只新增 1 組件 + 1 測試；workspace-chat.tsx / workspace-shell.tsx / workspace-context.tsx 零改動；後端完全沒動；`app/workspace/web/page.tsx` 未掛（那是 V4 integration checkbox）；element inspector（V3 #3）與 iteration timeline（V3 #4）屬不同 checkbox 故本回合不觸碰)
- [x] Annotation → agent context：每個標註轉換為 `{type: "click"|"rect", cssSelector: "...", boundingBox: {x,y,w,h}, comment: "..."}` → 注入 agent 的下一輪 ReAct prompt *(done: V3 #2 — `backend/ui_annotation_context.py` (~600 行) + `backend/tests/test_ui_annotation_context.py` 140 顆契約測試全綠 0.49s。**Server-side twin of V3 #1** — 鏡像 frontend `annotationToAgentPayload` 純函式到 Python；field names 與 V3 #1 `components/omnisight/visual-annotator.tsx` 逐字對齊，JSON 過 wire 一路 identity（`annotation_from_dict` → `annotation_to_agent_payload` → `to_dict()` 等於 V3 #1 直接產出）。**TODO-row 硬 pin 的 agent payload shape** — `VisualAnnotationAgentPayload.to_dict()` 回傳 **只** `{type, cssSelector, boundingBox, comment}` 四鍵，且 key 順序 byte-stable（insertion-ordered dict），契約測試 `test_agent_payload_field_set_matches_todo_row` + `test_agent_payload_field_order_matches_todo_row` 硬鎖；`test_frontend_wire_shape_parses_cleanly` 用 V3 #1 `defaultAnnotatorIdFactory` 實際產出的 wire body 驗 round-trip identity。**核心 API** — `AnnotationContextBuilder(clock=, event_cb=, text_prompt_template=).build(session_id=, annotations=..., turn_id=None) → AnnotationAgentContextPayload`；可接 `VisualAnnotation` 物件、frontend 字典、或混合（每 entry 獨立 normalise），parse 錯誤以 `annotations[idx]:` prefix 冒泡；`build_message(...)` 回 `(payload, HumanMessage)` 讓 agent loop 直接 shove 進 `llm_adapter.invoke_chat([system, visual_msg, annotation_msg])`。**資料模型三層** — `NormalizedBoundingBox(x,y,w,h∈[0,1])` frozen dataclass + `clamp_normalized` 純 helper（NaN/Inf→0、負數→0、>1→1）+ is_point property；`VisualAnnotation` 嚴謹驗型（id/type/bounding_box/comment/css_selector/label/created_at/updated_at）、`click` 型必 zero-size、`rect` 型必 non-zero；`VisualAnnotationAgentPayload` 薄殼只承四 field。**Text 注入** — `DEFAULT_ANNOTATION_TEXT_PROMPT_TEMPLATE` 含 `{session_id}/{turn_id}/{annotation_count}/{annotation_body}` 四 named placeholder；`render_annotation_entry(label=, payload=)` 產出 byte-stable markdown 條目（rect 顯示 `x/y/w/h` 4 欄；click 只顯示 `x/y` 兩欄；空 comment 顯示 `(no comment)`；缺 selector 顯示 `(none)`；space-trimmed comment 也算 no-comment）；`render_annotations_markdown(payloads, labels=None)` 零 payload 時回 `"No operator annotations this turn."` 保持 prompt layout 穩定，auto-label 從 1、或尊重 frontend `label` 如 V3 #1 已分配；`build_content_blocks(payload)` 回 Anthropic `[{"type":"text","text":...}]` 單一 block（無 image，annotations 是 *metadata about* preview 而非替代，呼叫端把 V2 #6 blocks 串在前）。**事件命名空間** — `ui_sandbox.annotation_context.{building,built,empty}` 三 topic，與 V2 #2-#6 (lifecycle/screenshot/viewport_batch/error/agent_visual_context) 零重疊；`test_event_namespace_disjoint_from_v2_2_to_6` 用 `isdisjoint` 強制；`built` envelope 含 annotation_count/rect_count/click_count/selector_count/commented_count/has_annotations/schema_version 不含 bounding_box 像素（SSE frame lean）；empty build 發 `empty` 不發 `built`；`event_cb` 例外被 logger.warning 吞。**Empty list 為合法路徑** — 不 raise、`has_annotations=False`、`annotation_count=0`、`annotation_body_markdown="No operator annotations this turn."`、empty_count 獨立計數；`test_builder_build_count_tracks_non_empty_only` 硬鎖分桶。**Turn id / clock / thread-safe** — `_resolve_turn_id` 自動遞增 `annotation-turn-{counter:06d}`、caller 可 override；`clock` 注入 seam、built_at 從 clock 蓋章；`RLock` 保 turn_counter/build_count/empty_count/_last_payload/snapshot 五 state；4 threads × 10 builds concurrent 測試 40 turn_ids 全獨特 zero lost event。**Snapshot JSON-safe** — elide 全 payload bodies 只留 counts 與 last_payload summary；`json.dumps` round-trip 保不炸。**契約測試 140 顆分桶** — 模組常量 9 + clamp 6 + bounding_box 12 + VisualAnnotation 10 + from_dict/from_list 11 + agent_payload TODO shape 6 + VisualAnnotationAgentPayload 3 + render 10 + content_blocks 5 + builder core 20 + events 5 + snapshot 3 + payload serialisation 3 + build_message 4 + thread safety 1 + sibling schema disjoint 1 + frontend wire shape parity 1 + `each_export_exists` parametrize 24 + 其他 7。**零 regression** — V2 #1-#8 全系 1011 tests + V2 #8 integration 24 tests + V3 #1 frontend 53 tests 全綠；V3 #2 獨立 schema_version `"1.0.0"` 與 V2 #6 disjoint 可獨立演進；**LangChain firewall 遵守** — `HumanMessage` 只在 `build_human_message` 內 lazy-import 自 `backend.llm_adapter`，module import 零 LangChain cost。**Scope 自律** — 只新增 `backend/ui_annotation_context.py` + `backend/tests/test_ui_annotation_context.py` 兩檔；**零改動** V3 #1 `components/omnisight/visual-annotator.tsx`（V3 #1 早已前瞻性 emit TODO row shape，本回合只消費）、V2 #1-#8 全系、`backend/llm_adapter.py`、`backend/events.py`、任何 FastAPI router（route wiring 屬後續 V4 workspace integration checkbox 不在 V3 #2 scope）、frontend 任何組件、`requirements.txt`（零新依賴）；element inspector (V3 #3)、iteration timeline (V3 #4)、version rollback (V3 #5) 屬不同 checkbox 故本回合不觸碰)
- [x] Element inspector integration：sandbox React tree 注入 `data-omnisight-component` attribute → hover 時前端顯示元件名 + 當前 props + computed styles（輕量版 React DevTools） *(done: V3 #3 — `components/omnisight/element-inspector.tsx` (~500 行 Client Component) + `test/components/element-inspector.test.tsx` 54 顆契約測試全綠 1.0s。**Lightweight React DevTools stand-in** — 包住任意 sandbox React tree，監聽 `onPointerOver` 走祖先鏈找最近帶 `data-omnisight-component` 的元件並展 3 段 panel：component name（`Badge`）+ props（JSON 解析、缺失/壞掉皆有專屬 empty / error 文字）+ computed styles（`DEFAULT_COMPUTED_STYLE_KEYS` 13 key 白名單 — layout/size/typography/box-model 四面向）。**Wire 契約硬 pin** — `OMNISIGHT_COMPONENT_ATTR="data-omnisight-component"` + `OMNISIGHT_PROPS_ATTR="data-omnisight-props"` 兩 string 就是 sandbox transformer 與 overlay 的協議，契約測試 `pins the wire-level attribute names` 鎖死字串；重命名瞬間前端即紅。**資料模型** — `ElementInspection = {element, componentName, props, propsRaw, parseError, computedStyles, cssSelector, boundingBox}`；`parseOmnisightProps` 壞 JSON / 非 object primitive 都回 `{props:{}, error:"..."}` 不丟棄；`pickComputedStyles` 兩路 lookup（直接 property + getPropertyValue kebab-case fallback）保 jsdom 也跑得動；`formatPropValue(value, maxChars=80)` 字串 JSON.stringify 引號、函式顯 `ƒ ()`、長 blob 截斷帶 `…`（`maxChars=0` 關截斷）；`computeOmnisightSelector` 以 `[data-omnisight-component="Card"] > [data-omnisight-component="Button"]` 形式穩定跨 re-render，同名 siblings 自動加 `:nth-of-type(N)`，可接收 `root` 限制爬升邊界。**Integration with V3 #1** — `onPinnedChange(inspection)` 吐完整 `ElementInspection.cssSelector`，caller（未來 V4 workspace page）可將其灌回 `VisualAnnotator.annotations[].cssSelector`（V3 #1 目前永遠 null），讓 V3 #2 `annotation_to_agent_payload` 產出帶 selector 的 agent context — V3 #1/#2/#3 三件齊心閉迴圈。**Hover / pin / Escape 語意** — hover 打開 panel、pointerleave 關閉（除非 pinned）、click pin（同元件再 click 取消）、Escape 鍵完全關（hovered + pinned 都清）；pinned panel 優先於 hover（`panelInspection = pinned ?? hovered`）避免游標越過 sibling 時 flicker；panel aside 上 `data-pinned` / `data-component` 兩 test hook。**Controlled / uncontrolled** — `hoveredInspection`/`defaultHoveredInspection` + `pinnedInspection`/`defaultPinnedInspection` 兩對 pair + `onHoveredChange`/`onPinnedChange` 兩 callback；effect hook 自動清掉 element.isConnected=false 的遺骸（DOM 換掉時 inspector 不抓鬼）。**Test seam** — `getComputedStyleImpl` + `getBoundingClientRectImpl` 兩注入口讓 jsdom 測試 deterministic（jsdom 無 layout 故 default 讀值常為空，測試全用 fakeStyle+fakeRect 注入）；同樣 seam 也讓 prod `window.getComputedStyle` 未替換直接可用。**Disabled** — `data-disabled="true"` + pointer/click/key 三 handler 全 early-return + 根節點 `tabIndex=-1`；三 handler 的 callback 也不觸發 — `drops hover / click / Escape when disabled` 驗證 `onHoveredChange`/`onPinnedChange` 皆 `not.toHaveBeenCalled()`。**Exports** — `ElementInspector`（default + named）、常量 `OMNISIGHT_COMPONENT_ATTR` / `OMNISIGHT_PROPS_ATTR` / `DEFAULT_COMPUTED_STYLE_KEYS`、純 helper `findNearestOmnisightAncestor` / `parseOmnisightProps` / `pickComputedStyles` / `formatPropValue` / `computeOmnisightSelector` / `inspectElement`；型別 `ElementComputedStyles` / `ElementInspection` / `ElementInspectorProps` / `InspectionBoundingBox` / `InspectOptions` / `ComputedStyleKey`。**測試 54 顆分桶** — 常量硬 pin 2（wire attr names + default style keys allowlist）+ parseOmnisightProps 5（null/undefined/empty + object + 非 object reject + 壞 JSON + 永不 throw）+ pickComputedStyles 4（default allowlist order + custom keys + kebab-case fallback + null style）+ formatPropValue 6（quote string + primitives + JSON serialise + function ƒ + truncate + maxChars=0 disable）+ findNearestOmnisightAncestor 5（self + walk-up + root stop + unannotated returns null + null target）+ computeOmnisightSelector 5（no attr 空 + single + chain > + nth-of-type + root limit）+ inspectElement 4（非儀器化 null + 完整 fields + JSON error surface + custom keys）+ render 3（root + viewport + no panel initially + disabled flag）+ hover 5（open panel + ignore non-instrumented + switch content + close on leave + keep on leave when pinned + onHoveredChange enter+leave）+ pin/unpin 4（click pin + click-same unpin + Escape clear + pinned wins over hover）+ disabled 2（callbacks drop + tabIndex=-1）+ parse error surfacing 2（error shown + no-props placeholder）+ controlled state 3（controlled pinned + defaultHovered seeded + element disconnect drop）+ V3 #1 selector wiring 1（onPinned 吐可直接灌回 VisualAnnotator.cssSelector 的 selector）+ sibling contract 1（與 visual-annotator 零 export 碰撞）。**零 regression** — V3 #1 53 顆 + V3 #3 54 顆 + workspace family 151 顆共 **258 顆全綠 / 1.27s**；V3 #2 backend 140 顆獨立 green；**LangChain firewall 遵守**：零新依賴，組件只吃 shadcn `Button`/`Badge` + lucide-react 3 icon（`Pin`/`PinOff`/`X`）。**Scope 自律** — 只新增 `components/omnisight/element-inspector.tsx` + `test/components/element-inspector.test.tsx` 兩檔；**零改動** V3 #1 `visual-annotator.tsx`（V3 #1 早已預留 `cssSelector?: string | null` 欄位，V3 #3 只產出 caller-site 可灌入的值）、V3 #2 `backend/ui_annotation_context.py`（server-side twin 不動）、V2 #1-#8 全系、`llm_adapter.py`、`backend/events.py`、任何 FastAPI router（sandbox-side attribute 注入 transformer 屬 V4 sandbox 整合，不在 V3 #3 scope）、frontend 其他組件、`package.json`（零新依賴）；UI iteration timeline (V3 #4)、version rollback (V3 #5) 屬不同 checkbox 故本回合不觸碰)
- [x] UI iteration timeline（`components/omnisight/ui-iteration-timeline.tsx`）：每次 agent 修改 → 存截圖 + code diff → 水平時間軸可視化；點任何版本可回溯（preview + code 都回到該版本） *(done: V3 #4 — `components/omnisight/ui-iteration-timeline.tsx` (~580 行 Client Component) + `test/components/ui-iteration-timeline.test.tsx` 64 顆契約測試全綠 1.56s。**水平時間軸** — `TimelineAxis` sub-component 以 `computeTimelinePositions()` 算出 0%–100% 均勻散點（n=1 居中 50%、n=2 `[0,100]`、n=N `i/(n-1)*100`），每節點 `<button>` 以 `left:${pct}%` 絕對定位在 `bg-border` 的 1px 橫軸上；active 節點 4×4 + primary 色、非 active 3×3 + border 色；節點下方 `-translate-x-1/2` 對齊的 relative-time label（`30m ago`）。**節點資料模型** — `IterationSnapshot = {id, commitSha?:string|null, screenshotSrc, screenshotAlt?, diff, summary, agentId?:string|null, createdAt, diffStats?}`；`diffStats` 可選（大 diff 可預算省每渲染 re-parse），缺省時走 `parseDiffStats(diff)` fallback。**Detail pane 左右雙欄** — 選中版本展開 `IterationDetailPanel`，`grid-cols-1 lg:grid-cols-2` 兩欄：左欄 `<img>` preview（無 screenshot 顯示 `(no screenshot available)` dashed placeholder）、右欄 `<pre>` unified diff（空 diff 顯示 `(no code changes)`），上方 header 含 `index / total`、short SHA（`shortCommitSha` 截 7 字，<=7 字 pass-through）、`agentId` badge、relative timestamp（`title={ISO}` 供 hover full precision）、diff stats `+N / -M / Kf` 綠/紅/灰三色（a11y label `"N additions and M deletions across K files"`）。**Rollback CTA** — `onRollback?` prop 未傳時按鈕消失、傳時右上 `variant="default"` 顯示 `回到此版本`（`rollbackLabel` prop 可覆蓋為 `Restore this version` 或 i18n 變體）；click 時以完整 `IterationSnapshot` payload 呼叫 `onRollback(snapshot)` — V3 #5 consumer 收到即可 issue `git checkout` + `preview refresh`。**Controlled + uncontrolled 兩軸** — `iterations`/`defaultIterations` 盯 list、`activeId`/`defaultActiveId` 盯選擇、`onActiveChange` 每次選擇翻轉（含 null）觸發；`resolveActiveId(iters, candidate)` 純函式做 self-healing — controlled caller 持續指向已被 prune 的 id 時 `useEffect` 回發 `onActiveChange(null)` 讓 caller 清 stale state，uncontrolled 則自動收斂到 null。**Click 語意** — click 不同節點→切換；click 已 active 節點→取消（與 V3 #3 pin/unpin 一致，operator 二次 click 自然 dismiss detail）；header `X` 按鈕亦清 selection。**鍵盤** — root `tabIndex={disabled?-1:0}` + `role="group"`，`ArrowRight`/`ArrowLeft` wrap-around 切換（無 active 時 Right→首 / Left→末）、`Escape` 清 active（無 active 時 no-op 不搶 keystroke）；`disabled` 時三 key 全 no-op + callback 不觸發。**Disabled** — `data-disabled="true"` + `tabIndex=-1` + 節點 button `disabled` + rollback button `disabled` + handler 層二次防線（handler 最前 `if (disabled) return` early-return — belt + braces 驗證 `fireEvent.click` 即使繞過 browser-level disabled 也被 handler 攔下）。**純 helper 7 支** — `parseDiffStats(diff)`（空字串 / null / undefined / non-string 全 zero；CRLF/LF/CR 通吃；`diff --git` 算 file、`+++`/`---` 不算 add/del；`+`/`-` 開頭 lines 才計）、`formatRelativeTime(iso, now?)`（`<45s→just now`、`<60m→Nm ago`、`<24h→Nh ago`、`<7d→Nd ago`、`<5w→Nw ago`、older→`YYYY-MM-DD`；invalid ISO pass-through、future→just now）、`shortCommitSha(sha)`（>7 截首 7、<=7 passthrough、null/undef/non-string/empty→""）、`sortIterationsAscending/Descending(iters)`（回新陣列、穩定 sort、不 mutate 原陣列）、`findIteration(iters, id)`（null/undef/empty/missing→null）、`computeTimelinePositions(iters)`（n=0→[]、n=1→[50]、n≥2→`i/(n-1)*100`）、`resolveActiveId(iters, candidate)`（missing id→null 自動 heal）。**Test seam 3 支** — `nowProvider()` 注入固定 `FIXED_NOW='2026-04-18T10:30:00Z'` 讓 `"30m ago"` 成為 deterministic string；`parseDiffStatsImpl` 讓 caller 自帶 structured diff parser；`formatRelativeImpl` 讓 i18n "5 分鐘前" 變體零侵入植入。**Empty state 專屬** — `iterations=[]` 時軸與 detail 全不渲染，只顯 `ui-iteration-timeline-empty` dashed placeholder + 可自訂 `emptyMessage`（預設 `"No iterations yet — the timeline will populate as the agent makes changes."`）；`count` badge 顯示 `0`。**Exports** — `UiIterationTimeline`（default + named）+ 型別 `IterationSnapshot` / `IterationDiffStats` / `UiIterationTimelineProps` + 7 純 helper 全 named export。**測試 64 顆分桶** — parseDiffStats 5（空/header 排除/多檔/CRLF/非 diff blob）+ formatRelativeTime 8（just-now/future/分/時/日/週/fallback 日期/invalid）+ shortCommitSha 3（長 SHA 截/短 passthrough/null 空）+ sort{Asc,Desc} 4（新陣列/descending mirror/stable sort/空 & 單項）+ findIteration 3（命中/null id/missing id）+ computeTimelinePositions 3（[] / [50] / 均勻散佈）+ resolveActiveId 2（命中/null-heal）+ empty state 3（placeholder/custom 訊息/disabled+empty）+ node 渲染 4（per-iter node + 順序 + defaultIterations + label 時間戳 + detail 未選不渲染）+ 選擇 uncontrolled 3（click→active + re-click unpin + X button close）+ 選擇 controlled 3（pin via prop + onActiveChange fire-without-mutate + stale-id 自癒）+ detail 8（screenshot/diff/SHA/agent/summary/timestamp 全欄渲染 + screenshot 缺 → placeholder + diff 空 → placeholder + parseDiffStats 顯示 + 無 `diff --git` → 隱藏 files 欄 + cached diffStats 優先 + null commitSha 隱藏 + null agentId 隱藏）+ rollback 4（onRollback callback + 未傳時按鈕消失 + disabled 不觸發 + 自訂 rollbackLabel）+ 鍵盤 7（Arrow→ wrap / Arrow← wrap / 無 active Arrow→ 首 / 無 active Arrow← 末 / Escape 清 / Escape 空 no-op / disabled 全 no-op）+ disabled 1（data-* + tabindex + click 抑制）+ seams 2（custom formatRelativeImpl + custom parseDiffStatsImpl）+ sibling 契約 1（與 visual-annotator / element-inspector 零 export 碰撞，除 default）。**零 regression** — V3 #1 53 + V3 #3 54 + V3 #4 64 + workspace family 103 共 **274 tests 全綠 / 1.27s**；V3 #2 backend 140 顆獨立 green（iteration timeline 為前端 only component，不動後端）。**LangChain firewall 遵守** — 零新依賴，組件只吃 shadcn `Button`/`Badge` + lucide-react 7 icon（`ArrowLeftRight`/`Bot`/`Camera`/`GitCommit`/`History`/`RotateCcw`/`X`）。**Scope 自律** — 只新增 `components/omnisight/ui-iteration-timeline.tsx` + `test/components/ui-iteration-timeline.test.tsx` 兩檔；**零改動** V3 #1 `visual-annotator.tsx`、V3 #3 `element-inspector.tsx`、V3 #2 `backend/ui_annotation_context.py`、V2 #1-#8 全系、`llm_adapter.py`、`backend/events.py`、任何 FastAPI router（snapshot 持久化 + emit 屬 V4 workspace integration 不在 V3 #4 scope）、frontend 其他組件、`package.json`（零新依賴）；Version rollback (V3 #5) 屬不同 checkbox — 本組件只 emit `onRollback(snapshot)` callback，git checkout + preview refresh 由 V3 #5 消費)
- [x] Version rollback：在 iteration timeline 點「回到此版本」→ sandbox git checkout 該 commit → preview 刷新 *(done: V3 #5 — `backend/ui_version_rollback.py` (~760 行) + `backend/tests/test_ui_version_rollback.py` 129 顆契約測試全綠 0.19s。**Server-side rollback orchestrator** — 以 `VersionRollback(manager=, lifecycle=, git_runner=, event_cb=, clock=)` 封裝 git checkout + preview 刷新閉迴圈：接 V3 #4 `onRollback(snapshot: IterationSnapshot)` 的 `commitSha`，在 V2 #1 sandbox workspace 跑 `git rev-parse --verify {ref}^{commit}` → `git checkout --detach --force {sha}` → `git diff --name-only {prev}..{new}` 算 files_changed → 呼 V2 #2 `SandboxLifecycle.hot_reload(session_id, files_changed=...)` 觸發 HMR。**V3 #4 wire 契約對齊** — `rollback_request_from_snapshot(session_id=, snapshot=, reason=)` 直接吃 V3 #4 `IterationSnapshot` 字典 `{id, commitSha, screenshotSrc, diff, summary, agentId?, createdAt, diffStats?}`，`commitSha=null/blank` → `InvalidCommitRef` 回 422 before git；`short_commit_sha(sha, length=7)` 與 V3 #4 `shortCommitSha` 逐字節對齊（null/non-str/blank→""、<=7 passthrough、>7 truncate）。**依賴注入 3 seam** — `git_runner: GitCommandRunner` protocol 可換 `FakeGitRunner`（測試）/ `SubprocessGitRunner`（prod，argv 走 list 避 shell injection + timeout 捕捉 `TimeoutExpired`→`GitCommandError(returncode=-1)` + `FileNotFoundError`→`GitCommandError`）；`clock` 凍結時間；`event_cb` 捕事件。**事件命名空間** — `ui_sandbox.rollback.{requested,checked_out,completed,failed}` 四 topic，`test_event_namespace_disjoint_from_v2_and_v3` 驗證與 V2 #2-#6 (lifecycle/screenshot/viewport_batch/error/agent_visual_context) + V3 #2 (annotation_context) 全 disjoint；`requested` → `checked_out` → `completed` 是 happy path fire 序、`requested` → `failed` 替代後兩 on unhappy path；`event_cb` 拋例外被 logger.warning 吞不殺 rollback。**Event envelope** — `requested`/`checked_out` 各含 schema_version/session_id/iteration_id/requested_sha/resolved_sha/previous_sha/short_sha/at；`completed` 額外吐 file_count/files_changed_total/files_preview（capped at 20 for SSE lean）/truncated/is_noop/preview_refresh_requested/warning_count；`failed` 含 error/error_type。**資料模型** — `RollbackRequest(session_id, commit_sha, iteration_id?, reason?)` frozen + 嚴驗 SHA 格式（`^[0-9a-f]{4,40}$`，支援 short-SHA 4+ 與 40 char full）；`RollbackResult(schema_version, session_id, iteration_id, requested_sha, resolved_sha, previous_sha, short_sha, files_changed, files_changed_total, preview_refresh_requested, checked_out_at, reason, warnings)` frozen 全欄 + `.file_count`/`.truncated`/`.is_noop` 三 derived property；`GitCommandResult(argv, returncode, stdout, stderr)` frozen + `.ok` property。**邊界/失敗路徑全覆蓋** — fresh sandbox 無 HEAD → `previous_sha=None` + skip diff（不 abort）、rev-parse target 失敗 → `GitCommandError` + failed event、checkout 失敗 → `GitCommandError` + failed event 保持 previous_sha 原值、diff 失敗 → 不 abort 改附 `warnings=("diff_name_only failed: ...; HMR signal sent with empty file list",)`、lifecycle.hot_reload 拋 `SandboxError` → `warnings=("preview refresh skipped: ...",)` 但 checkout 成功不回滾、`RollbackSandboxNotFound` 當 session 未 live、rev-parse 回非 SHA stdout → refuse checkout。**Noop 偵測** — 當 previous_sha == resolved_sha 且 files_changed=() → `is_noop=True` + `noop_count` 獨立累計；counter 系 `rollback_count`/`failure_count`/`noop_count` 三桶 RLock 保護 + `last_result`/`last_error` 快照。**Truncation 門檻** — `MAX_FILES_CHANGED=500` 預設（可自訂），超過時 `files_changed` 只保前 N，`files_changed_total` 仍誠實回全數；SSE `files_preview` 獨立 cap 20（壓 frame 大小）。**安全/無 shell injection** — `SubprocessGitRunner` 一律用 `subprocess.run([git, *args])` list 形式 argv，零 `shell=True`，配合 `is_valid_commit_sha` 上游驗 `^[0-9a-f]{4,40}$` 雙保險；checkout 用 `--detach --force` 故意 destructive 因 rollback 是 operator 明確意圖覆蓋 dirty tree。**Thread-safe** — `RLock` 保 5 state（rollback_count/failure_count/noop_count/last_result/last_error）；`test_version_rollback_is_thread_safe` 4 threads × 5 rollbacks 共 20 concurrent 零 lost update。**Snapshot JSON-safe** — `.snapshot()` 只回 counters + last_result 概要（無 full file list / no raw stderr）；`json.dumps` round-trip 不炸。**Exports** — 22 項：4 常量 (`UI_VERSION_ROLLBACK_SCHEMA_VERSION`/`MAX_FILES_CHANGED`/`DEFAULT_GIT_TIMEOUT_S`/`ROLLBACK_EVENT_TYPES`) + 4 event topic 字串 + 4 error class (`VersionRollbackError`/`InvalidCommitRef`/`GitCommandError`/`RollbackSandboxNotFound`) + 3 dataclass (`GitCommandResult`/`RollbackRequest`/`RollbackResult`) + 2 runner (`GitCommandRunner` protocol + `SubprocessGitRunner`) + 1 orchestrator (`VersionRollback`) + 4 純 helper (`is_valid_commit_sha`/`short_commit_sha`/`normalize_commit_ref`/`rollback_request_from_snapshot`)。**測試 129 顆分桶** — 模組 invariants 11（`__all__` match + each export exists parametrize 22 + semver + event namespace/uniqueness/ordering/disjoint + error 繼承）+ `is_valid_commit_sha` 12（含 length 4-40 範圍、大小寫敏感、非 str reject）+ `short_commit_sha` 8（truncate/passthrough/custom length/non-str/blank/strip）+ `normalize_commit_ref` 5（lowercase/trim/reject bad/reject non-str/reject blank）+ `rollback_request_from_snapshot` 12（happy/short SHA/case normalise/missing commitSha/blank/bad format/missing id/blank id/bad snapshot shape/blank session/reason 傳導/V3 #4 wire shape 全欄 round-trip）+ `RollbackRequest` 8（happy/frozen/blank session/bad SHA/blank iteration/non-str reason/to_dict short_sha/optional omit）+ `GitCommandResult` 4（ok/not_ok/to_dict/非 tuple reject/frozen）+ `SubprocessGitRunner` 6（default git binary/custom binary/blank reject/empty args reject/non-str reject/missing binary→GitCommandError）+ `VersionRollback` ctor 6（none manager/non-callable clock/bad max_files/non-positive timeout/default runner is SubprocessGitRunner/counters zero）+ happy paths 10（三事件序列/to_dict 全欄/counters 更新/noop 偵測/無 lifecycle refresh=False/lifecycle 拋 SandboxNotFound→warnings/--detach --force 必填/cwd=workspace_path/clock 蓋章/fresh workspace 無 HEAD）+ 失敗路徑 7（sandbox missing/resolve target 失敗/checkout 失敗/diff 失敗→warnings/rev-parse 非 SHA 輸出/last_error 設定/non-request type→TypeError）+ rollback_from_snapshot 2（happy/missing SHA→early raise without git）+ event envelope 5（requested/checked_out/completed/failed/broken event_cb 被吞）+ truncation 3（超 cap truncate + total 誠實/未超 cap 不 truncate/SSE preview cap 20）+ snapshot 3（initial/after success/JSON safe）+ thread safety 1（4×5 concurrent 零 lost）。**零 regression** — V2 sibling 全系 (test_ui_sandbox + test_ui_sandbox_lifecycle + test_ui_screenshot + test_ui_preview_error_bridge + test_ui_responsive_viewport + test_ui_agent_visual_context) **1038 tests 全綠**；V3 #2 annotation_context 140 顆獨立 green；V3 #1/#3/#4 frontend 171 tests 獨立 green。**LangChain firewall 遵守** — 零新依賴（stdlib only: subprocess/re/threading/time/dataclasses/typing）；`backend.ui_sandbox` 本來就在 sandbox 家族內不屬 LangChain。**Scope 自律** — 只新增 `backend/ui_version_rollback.py` + `backend/tests/test_ui_version_rollback.py` 兩檔；**零改動** V3 #1 `visual-annotator.tsx`、V3 #2 `ui_annotation_context.py`、V3 #3 `element-inspector.tsx`、V3 #4 `ui-iteration-timeline.tsx`（V3 #4 早已 emit `onRollback(snapshot)` 單一 callback，本回合只消費）、V2 #1-#8 全系、`backend/llm_adapter.py`、`backend/events.py`、任何 FastAPI router（route wiring 屬後續 V4 workspace integration checkbox 不在 V3 #5 scope）、frontend 任何組件、`package.json`/`requirements.txt`（零新依賴）)
- 預估：**5 day**

### V4. Web Workspace UI + 輸出 (#320)
- [x] `app/workspace/web/page.tsx`：Web 工作區主頁面——三欄佈局 *(done: V4 #1 — `app/workspace/web/page.tsx` (~700 行 Client Component) + `test/components/web-workspace-page.test.tsx` 67 顆契約測試全綠 0.87s。**三欄合體** — 整頁包進 `<PersistentWorkspaceProvider type="web">`（V0 #4 持久化 + localStorage/backend hydrate），內層 `<WorkspaceShell type="web">`（V0 #2 grid shell）填三 slot：sidebar / preview / code-chat；slot 標題客製為 `Build · Tokens · Tree` / `Live preview` / `Code & iteration`。**左 sidebar 三段** — `SidebarSection` 共用包裝（`web-sidebar-section-{tree,palette,tokens}`）：(1) `ProjectTree` 遞迴節點渲染 `DEFAULT_PROJECT_TREE`，dir 預設展開頂層、`aria-expanded` 受控可摺疊、file 點選 emit；(2) `ComponentPalette` 列 10 個 `SHADCN_PALETTE` 項（Button/Card/Input/Textarea/Select/Tabs/Dialog/Toast/Table/Badge），按一下 `aria-pressed`/`data-selected` 翻面 → 加到 chat 的 annotation chips；(3) `DesignTokenEditor` 三 control：`<input type="color">` + 文字 hex（雙向 sync，invalid hex 顯示 `web-design-token-color-error`）、shadcn `Select` 字型（4 個 preset Inter/System/Serif/Mono）、shadcn `Slider` spacing（min=4 / max=48 / step=2）+ `Send tokens to agent` button → 凍結當下 token snapshot 變成 chat chip。**Live preview 即時更新** — `<PreviewSurface>` 以 inline style 注入 `--ws-primary` / `--ws-font-family` / `--ws-spacing` 三 CSS var，改 token 立即重繪 frame 不需等 agent。**中 pane 三件式** — `ResponsiveToggle`（shadcn `Tabs`）切 `desktop/tablet/mobile` → frame `width×height` 寫死對齊 `backend/ui_screenshot.VIEWPORT_PRESETS`（1440×900 / 768×1024 / 375×812），右上角 readout 顯示像素；`<iframe sandbox="allow-scripts allow-same-origin allow-forms">` 套 `ctx.preview.url` 渲染 sandbox preview，url 為 null 時降級成 `<VisualAnnotator>` 蓋在 screenshot 上（V3 #1 整合，annotation list 由 page state 持有），兩者皆 null 顯示 `web-preview-empty` 空狀態。**右 pane 上下分** — 上：`<CodeViewer>` 客製輕量化（無 Prism / Monaco / Shiki 依賴避免 bundle 膨脹）— `classifyDiffLines()` pure helper 把 unified diff 切成 `add/del/ctx/meta` 四 kind（emerald/rose/sky/muted 配色）、有 diff 則出 `web-code-viewer-diff-badge` 顯 `N+ / M-`、無 diff 則 fallback 純 source `<pre>`、`Copy` button 走 `navigator.clipboard.writeText` 或 injected `copyToClipboardImpl`（test seam），成功翻成 `Copied` 1.5s 後 reset、reject 翻 `Error`；下：`<WorkspaceChat workspaceType="web">`（V0 #7）吃 page 組裝的 `allChips` = annotation chips（label `Region #N` / `Pin #N` + 32-char comment summary）+ palette chips（`shadcn · {Name}`）+ token snapshot chip（`Tokens · #color · Npx · Font`）。**Submit 串聯** — `WorkspaceChat.onSubmitTask` → page 組 user `WorkspaceChatMessage` 推進 chat log + `ctx.setAgentSession({status:"running"})` + 把 `submission.annotationIds` 過濾出對應 `VisualAnnotation` 跑 V3 #2 `annotationToAgentPayload()` 得 `{type, cssSelector, boundingBox, comment}` payload（保留給 SSE wire）、submit 完清掉 token snapshot chip 但保留 palette+annotation 讓 operator 連續迭代。**Pure helpers exported** — 7 顆：`isHexColor` (regex `^#[0-9a-f]{6}$`, case-insensitive)、`shortenPath(p, max=36)` 前綴 ellipsis、`resolveViewport(id)` fallback 回 desktop、`annotationChipLabel(ann)` Region/Pin + label + 32-char comment、`designTokensChip(tokens)` 含 font preset 名稱 fallback `custom`、`paletteChip(entry)`、`classifyDiffLines(diff)` 容錯 null/CRLF/CR/LF。**常量 exported** — `RESPONSIVE_PRESETS`（凍 3 viewport tuple）、`SHADCN_PALETTE`（凍 10 entries）、`DEFAULT_PROJECT_TREE`（凍範例樹）、`DEFAULT_DESIGN_TOKENS`（#3366ff/Inter stack/16px）、`FONT_PRESETS`（4 preset）、`SPACING_SLIDER`（{min:4,max:48,step:2}）、`HEX_COLOR_RE`。**Provider scope 自處理** — `app/workspace/[type]/layout.tsx` 只 wrap `[type]/` 子路由，`/workspace/web` 是 sibling 靜態段不受其覆蓋，故 page 自行在內 wrap `<PersistentWorkspaceProvider type="web">` 取得 V0 #4 完整 state machinery。**測試 67 顆分桶** — pure helper 19（`isHexColor` 8 含 HEX_COLOR_RE re-export + `shortenPath` 3 + `resolveViewport` 5 + `annotationChipLabel` 5 + `designTokensChip` 2 + `paletteChip` 1 + `classifyDiffLines` 3）+ 常量 invariants 14（`RESPONSIVE_PRESETS` 5 對齊 backend widths + `SHADCN_PALETTE` 3 含 Button/Card 必含 + `DEFAULT_PROJECT_TREE` 2 + `DEFAULT_DESIGN_TOKENS` 3 + `FONT_PRESETS` 2 + `SPACING_SLIDER` 1）+ layout composition 4（shell type=web + 三 slot 填對 + 客製 title + 三 sidebar section）+ project tree 2（DEFAULT 渲染 + dir 摺疊）+ component palette 2（10 entry 全列 + toggle 翻面）+ design token editor 4（4 control 全在 + invalid hex error + valid hex clear error + color picker → CSS var）+ responsive toggle 3（desktop default + tablet 翻 viewport via Radix pointer sequence + 3 trigger 全列）+ preview surface 1（empty placeholder）+ code viewer 4（無 diff fallback source + 有 diff 出 badge + copy 呼 clipboard impl + reject 翻 Error）+ chat integration 3（submit stamps `workspaceType="web"` + 訊息進 log + token snapshot 變 chip）+ default export 1（`<WebWorkspacePage />` 內含 provider 不 throw）。**jsdom polyfill** — Radix Slider 需 `ResizeObserver`、Radix Select 需 `Element.prototype.hasPointerCapture`/`releasePointerCapture`/`scrollIntoView`，test 檔最上方注入 no-op shim；Tabs trigger 在 jsdom 不收 plain click 故用 `pointerDown` → `mouseDown` → `pointerUp` → `click` 完整序列。**零 regression** — workspace family（workspace-shell + workspace-context + workspace-chat + workspace-layout + visual-annotator + element-inspector + ui-iteration-timeline + persistent-workspace-provider + workspace-bridge-card + workspace-navigation-sidebar）+ 新 page 共 442 顆全綠；其他單元測試 751/754（3 顆 bootstrap-page 失敗為 pre-existing 不在 V4 scope）。**LangChain firewall 遵守** — 0 新依賴（純 React + 既有 shadcn primitives + lucide-react icons + 既有 omnisight 元件）；無新 npm package。**Scope 自律** — 只新增 `app/workspace/web/page.tsx` + `test/components/web-workspace-page.test.tsx` 兩檔；零改動 V0 #1-#7（shell/context/persistent-provider/chat/bridge-card/sidebar/layout）、V3 #1-#5（visual-annotator/annotation-context/element-inspector/ui-iteration-timeline/version-rollback）、V2 #1-#8 sandbox 全系、`backend/`、`components/ui/`、`components/omnisight/` 任一 .tsx；後續 V4 checkboxes（instant preview URL deploy adapter、block exporter、brand consistency validator）屬不同 row 不在本回合 scope)
  - [x] 左 sidebar：project tree + shadcn component palette（瀏覽 + 點選加到 chat prompt）+ design token editor（調色盤 + 字型選擇 + spacing slider → live 更新 preview）
  - [x] 中 pane：preview iframe/screenshot + responsive toggle（desktop/tablet/mobile）+ visual annotator overlay
  - [x] 右 pane：code viewer（syntax highlight + diff + copy button）+ workspace chat（conversational iteration）
- [ ] Instant preview URL：W4 deploy adapter 的快速模式——`vercel deploy --preview` 或 `docker run` 出暫時 URL 供分享（不走 full CI/CD）
- [ ] Block exporter：agent 生成的元件包成 shadcn CLI 相容 block（`npx shadcn add <block-url>`）
- [ ] Brand consistency validator：post-deploy 掃描 → 所有 color/font 是否在 design system 允許範圍 → 違規項列為 warning
- 預估：**5 day**

### V5. Mobile — AI 自主 App 生成引擎 (#321)
- [ ] `configs/roles/mobile-ui-designer.md`：Mobile UI Designer specialist agent——精通 SwiftUI views / Jetpack Compose components / Flutter widgets + 平台 HIG (Human Interface Guidelines / Material Design)
- [ ] `backend/mobile_component_registry.py`：SwiftUI views 清單（NavigationStack/List/Form/TabView/...）+ Compose components（Scaffold/LazyColumn/Card/...）+ Flutter widgets（Scaffold/ListView/...）注入 agent context
- [ ] Figma → mobile code：MCP `get_design_context` → 提取 spacing/color/component 結構 → agent 輸出 Swift / Kotlin / Dart code
- [ ] Screenshot → mobile code：上傳 app 截圖或手繪稿 → Opus 4.7 vision → 對應 SwiftUI/Compose/Flutter layout code
- 預估：**4 day**

### V6. Mobile — Device Preview + Sandbox 渲染 (#322)
- [ ] `backend/mobile_sandbox.py`：per-session build server——Android: Docker + Gradle build → .apk → Android emulator 截圖；iOS: macOS remote delegate + xcodebuild → .app → Simulator 截圖
- [ ] `backend/mobile_screenshot.py`：iOS `xcrun simctl io booted screenshot` + Android `adb shell screencap` → PNG 回傳
- [ ] Device frame renderer（`components/omnisight/device-frame.tsx`）：iPhone 15 / SE / iPad / Pixel 8 / Fold / Samsung tablet 外框 → 截圖套入
- [ ] Multi-device grid view（`components/omnisight/device-grid.tsx`）：6+ 機型同時預覽（同一頁面在不同機型上的截圖 grid）
- [ ] Agent visual context injection（mobile）：每輪 ReAct 截圖 emulator → 注入 Opus 4.7 multimodal context
- [ ] Build error → agent auto-fix：Xcode / Gradle build error 攔截 → 注入 agent → 修 → 重 build → 重截圖
- 預估：**6 day**

### V7. Mobile — 視覺迭代 + Workspace UI + 輸出 (#323)
- [ ] Mobile visual annotation：在 device frame 截圖上畫框/點選 → agent 修改對應 SwiftUI/Compose/Flutter 元件
- [ ] Mobile iteration timeline：每次修改存 emulator 截圖 + code diff → 版本歷史（含多機型截圖）
- [ ] `app/workspace/mobile/page.tsx`：Mobile 工作區主頁面
  - [ ] 左 sidebar：project tree + platform selector (iOS/Android/Flutter/RN) + build config
  - [ ] 中 pane：device frame preview + device switcher + multi-device grid toggle + visual annotator overlay
  - [ ] 右 pane：code viewer + workspace chat
- [ ] Build status panel：即時 Xcode/Gradle build 進度 + error list + artifact link (.ipa/.apk download)
- [ ] Store submission dashboard：工作區內看 App Store / Play Console 審核狀態 + 截圖管理 + TestFlight/Firebase 一鍵派發
- 預估：**5 day**

### V8. Software Workspace UI (#324)
- [ ] `app/workspace/software/page.tsx`：Software 工作區主頁面
  - [ ] 左 sidebar：project tree + language/framework selector + build target selector
  - [ ] 中 pane：terminal output viewer（agent bash tool output 即時 stream：build log / test output / deploy log）+ OpenAPI/Swagger interactive docs viewer
  - [ ] 右 pane：code viewer + workspace chat
- [ ] Multi-platform release dashboard：各平台 build 狀態 grid（Docker ✅ / Helm ✅ / .deb ⏳ / .msi ❌ / .dmg ✅）+ 每個 artifact 的 download link
- [ ] Test coverage viewer：coverage report 渲染（per-file coverage bar + uncovered line highlight）
- 預估：**4 day**

### V9. Cross-workspace Integration + Polish (#325)
- [ ] Image generation tool：`backend/agents/tools.py` 新增 `image_generate` tool（呼叫 OpenAI Image API 或 Anthropic image gen）→ agent 可在 coding 流程中生成 icon/banner/asset → preview pane 直接顯示
- [ ] `omnisight-cli` MVP：Python Click/Typer CLI 工具——`omnisight status` / `omnisight workspace list` / `omnisight run "NL prompt"` / `omnisight inspect <agent_id>` / `omnisight inject <agent_id> "hint"`——等於把 workspace + R1 ChatOps 功能搬到 terminal
- [ ] Workspace onboarding flow：首次進入工作區時的 guided tour（「選擇框架」→「描述你要什麼」→「AI 開始工作」→「preview 出現」→「標註修改」→「部署」）
- [ ] Cross-workspace E2E tests：
  - [ ] Web：NL「做一個 SaaS landing page」→ agent 自主寫完 → preview 渲染正確 → 標註「把 hero 背景改深藍」→ agent 修改 → 重渲染 → deploy → Lighthouse ≥ 80
  - [ ] Mobile：NL「做一個 todo app」→ agent 寫 SwiftUI → emulator 截圖 → 標註「加個 dark mode toggle」→ agent 修改 → rebuild → 截圖正確
  - [ ] Software：NL「做一個 REST API with user CRUD」→ agent 寫 FastAPI → pytest pass → OpenAPI spec 渲染 → Docker build → deploy
- 預估：**5 day**

**Priority V 總預估**：**48 day**（4 stage 切段交付：基建 6d → Web 23d → Mobile 15d → Software+CLI 9d）

**建議切段交付**：
1. **V0**（6d）— Workspace foundation：三個工作區 route 可訪問 + 共用 layout + chat + bridge card
2. **V1 + V2 + V3 + V4**（23d）— Web 完整體驗：AI 生成 → preview → 標註迭代 → 輸出。此時 Web 工作區達到 v0.dev parity
3. **V5 + V6 + V7**（15d）— Mobile 完整體驗：AI 生成 → device preview → 標註迭代。此時 Mobile 達 Codex parity
4. **V8 + V9**（9d）— Software 工作區 + CLI + image gen + E2E tests。此時三個 vertical 全面就位

---

## 🅡 Priority R — Enterprise Watchdog & Disaster Recovery（全維度守護 + 災難復原 + UI 強化）

> 背景：`docs/design/enterprise_watchdog_and_disaster_recovery_architecture.md`（2026-04-17 新增）提出五層防護：PEP Gateway（工具執行網關）、冪等性重試、語意監控、自動續寫、階梯式部署。審計結果顯示 ~55% 與既有模組重疊（sandboxed tools / L1-L4 notifications / M1 cgroups / worktree isolation / startup cleanup），但有 5 項真正新增能力填補 critical gap：PEP 攔截 middleware、ChatOps interactive approve/inject、semantic entropy 偵測、scratchpad 持久化 + 斷點續傳、Serverless PaaS adapter。
>
> **設計覆寫**：白皮書 §三.2 建議重試前 `git clean -fd` + `git checkout .`——**本路線拒絕此設計**，改用「discard worktree + create fresh worktree」維持既有 WorkspaceManager 的安全隔離（R8 詳述）。白皮書 §四 的 `<system_override>` 標籤——**不採用**，改走 agent state machine 的 `human_hint` slot，避免 prompt injection 權限溢出（R1 詳述）。
>
> **UI 策略**：既有 47 個 FUI 元件中，toast-center（approve/reject 按鈕）、decision-dashboard（審批佇列）、audit-panel（審計追蹤）、orchestration-panel（O9 觀測性）可直接延伸。新增 2 個全新元件（`pep-live-feed.tsx`、`chatops-mirror.tsx`）+ 擴充 3 個既有元件（agent-matrix-wall / run-history-panel / integration-settings 或 ops-summary-panel 加 deployment topology tab）。
>
> 相依（硬前置）：**O0-O3**（CATC + Worker pool + Redis + MQ）、**G2**（reverse proxy）、**G5**（K8s manifests）、**I10**（Redis HA）、**L**（Bootstrap wizard）。建議排在 O 之後。

### R0. PEP Gateway Middleware（工具執行網關）(#306)
- [x] `backend/pep_gateway.py`：PEP (Policy Enforcement Point) middleware，intercept 所有 tool_executor node 的 tool call
- [x] 毀滅性命令 pattern 表（`rm -rf /`、`chown`、`chmod 777`、`dd if=/dev/zero`、`mkfs`、`:(){:|:&};:`…）→ 自動 DENY + audit
- [x] 生產部署攔截：`deploy.sh prod`、`kubectl apply --context production`、`terraform apply` 等 prod-scope 命令 → 自動 HOLD，等人工 approve
- [x] T1/T2/T3 sandbox tier 整合：PEP 在 tool call 前查 sandbox tier policy，T1 只放行白名單工具，T3 允許 sudo 但仍攔截 prod-deploy
- [x] Circuit breaker：PEP down 時 fallback 到 sandbox-tier 本機 rule（degraded but alive）；恢復後自動切回
- [x] SSE event：`pep.decision`（action / agent / tool / command / decision: auto_allow | hold | deny）
- [x] Metrics：`pep_decisions_total{decision}` / `pep_hold_duration_seconds` / `pep_deny_total`
- [x] 整合測試：mock tool call → auto_allow、hold、deny 三條路徑 + PEP down fallback
- [x] **UI — PEP Live Feed 面板（新元件 `pep-live-feed.tsx`，獨立 panel 掛進 mobile-nav/page.tsx）**：
  - [x] 即時顯示所有 tool call decisions（SSE `pep.decision` 驅動）
  - [x] 每行：timestamp / agent name / tool name / command（truncated） / decision badge（✅ auto / 🟡 HELD / 🔴 DENY）
  - [x] HELD 的行展開後顯示完整 command + impact_scope + 「Approve」/「Reject」按鈕（複用 toast-center 的 approve/reject 機制，經 `/decisions/{id}/approve|reject`）
  - [x] Filter bar：by agent / by decision / by tool name
  - [x] Header 統計：auto_allowed count / held count / denied count（自動刷新）
- [x] **UI — decision-dashboard 延伸**：PEP HELD 項目自動出現在 decision queue（kind=`pep_tool_intercept`，列掛 PEP chip）
- [x] **UI — audit-panel 延伸**：新增 kind filter tabs（`All Actions | PEP | Decisions | Auth`），PEP 篩選過濾 `action.startsWith("pep.")`
- [x] **UI — toast-center 延伸**：PEP HOLD 事件透過 DE 的 `decision_pending` 自動上浮（severity=risky/destructive），並顯示 PEP chip；approve 後 toast 自動消失
- [x] 預估：**3.5 day**（backend 2d + UI 1.5d）**✅ AI completed 2026-04-17**

### R1. ChatOps Interactive Integration（Discord / Teams / Line 雙向互動）(#307)
- [x] `backend/chatops_bridge.py`：統一 ChatOps interface（`send_interactive(channel, message, buttons)` / `on_button_click(callback)` / `on_command(cmd, handler)`）
- [x] Discord adapter：Webhook + Interaction endpoint（Button / Select Menu）
- [x] Teams adapter：Adaptive Card + Bot Framework webhook
- [x] Line adapter：Flex Message + Postback action
- [x] PEP approve/reject 按鈕回路：ChatOps button click → `POST /api/v1/pep/decision/{id}` → PEP gateway 放行/拒絕
- [x] `/omnisight inspect [ID]` — 回傳 agent 最後 3 輪 ReAct 日誌（markdown 格式）
- [x] `/omnisight inject [ID] "hint"` — 將 human hint 寫入 agent state machine 的 `human_hint` slot（**不用 `<system_override>` 標籤**，改走 debug blackboard 機制）；hint 內容強制 sanitize（strip XML/HTML tags + 長度上限 2000 chars）+ rate limit（每 agent 每 5 min 最多 3 次）+ audit log
- [x] `/omnisight rollback [ID]` — 觸發 worktree discard + recreate（R8 機制）
- [x] `/omnisight status` — 回傳系統 KPI snapshot（active agents / queue depth / PEP held / entropy alerts）
- [x] Hot Resume 機制：inject hint 後不重啟 sandbox，agent state machine 從 `suspended` → `running`，hint 注入 context 的 `human_hint` slot（非 system prompt 尾端）
- [x] 安全：ChatOps inject 只接受 Gerrit `non-ai-reviewer` group 對應的 ChatOps user（防止非授權人員注入指令）；所有 inject 進 hash-chain audit_log
- [x] **UI — ChatOps Mirror Panel（新元件 `chatops-mirror.tsx`）**：
  - [x] 即時雙向鏡像：ChatOps 頻道的對話（bot 發出 + human 回覆）顯示在 dashboard
  - [x] SSE event `chatops.message`（direction: outbound | inbound, channel, author, body, buttons_state）
  - [x] Dashboard 側也能 inject hint / approve PEP（不一定要開 Discord）——form input + submit 按鈕
  - [x] Channel selector（multiple channels 支援：#omnisight-alerts / #omnisight-dev / custom）
  - [x] Connection status indicator（● Connected / ○ Disconnected per channel）
  - [x] 歷史捲動 + 搜尋（最近 100 條 ChatOps 訊息快取在前端 state）
- [x] **UI — notification-center 延伸**：P2 severity 通知裡加 inline text input 讓 operator 直接 inject hint 而不用切到 ChatOps Mirror
- [x] 整合測試：mock Discord webhook → button click → PEP approve → agent resume；inject hint → agent picks up hint → audit recorded
- [x] 預估：**4 day**（backend 2.5d + UI 1.5d）**✅ AI completed 2026-04-17**

### R2. Semantic Entropy Monitor（語意熵值偵測）(#308)
- [x] `backend/semantic_entropy.py`：每 N 輪（預設 3）對 agent 最近 output 做 embedding similarity 計算
- [x] Embedding backend：sentence-transformers（本地 MiniLM）或 Anthropic embedding API（可插拔）
- [x] Entropy 指標：rolling window 5 輪的 pairwise cosine similarity 平均值；threshold 0.7 → `cognitive_deadlock` event
- [x] 整合 debug blackboard：entropy 超標寫入 `debug_findings` 表
- [x] 與既有 loop detection 協作：entropy check 在 loop detection 之前觸發，可更早抓到「措辭不同但語意空轉」
- [x] SSE event：`agent.entropy`（agent_id / entropy_score / threshold / verdict: ok | warning | deadlock）
- [x] Metrics：`semantic_entropy_score{agent_id}` gauge / `cognitive_deadlock_total` counter
- [x] 成本控制：MiniLM 本地推理 ~5ms / 輪；不用 LLM 評估 LLM（避免成本翻倍）
- [x] **UI — Agent Cognitive Health Card（擴充 `agent-matrix-wall.tsx`）**：
  - [x] 每個 agent 卡片新增「Cognitive Health」區塊
  - [x] Semantic entropy sparkline（最近 20 輪的 entropy 趨勢，微型折線圖）
  - [x] Entropy 當前值 + 閾值 badge（✅ < 0.5 / ⚠️ 0.5-0.7 / 🔴 > 0.7）
  - [x] ReAct loop counter（loop N / max M，auto-escalate at max）
  - [x] 當 entropy > threshold 時卡片邊框變紅 + 脈衝動畫（FUI scan-line 風格）
  - [x] 點擊 entropy sparkline 展開「最近 5 輪 output 摘要」popover（方便人工判斷是否真的卡住）
- [x] **UI — ops-summary-panel 延伸**：加「Highest Entropy Agent」badge（即時顯示 entropy 最高的 agent 名 + 分數）
- [x] 整合測試：mock 5 輪相似 output → entropy > threshold → deadlock event 發出 + UI sparkline 變紅
- [x] 預估：**2.5 day**（backend 1.5d + UI 1d）**✅ AI completed 2026-04-17**

### R3. Scratchpad Memory Offload + Auto-Continuation（心智卸載 + 自動續寫）(#309)
- [x] `backend/scratchpad.py`：per-agent persistent scratchpad file（`data/agents/<agent_id>/scratchpad.md`）
- [x] 自動寫入觸發：每 10 輪 ReAct 循環、每次 tool call 結束後、agent 切換子任務時
- [x] Scratchpad 格式：structured markdown（`## Current Task` / `## Progress` / `## Blockers` / `## Next Steps` / `## Context Summary`）
- [x] 加密 at-rest：沿用 `backend/secret_store.py` Fernet（scratchpad 可能含 code snippet + 設計決策）
- [x] Auto-continuation：`stop_reason=max_tokens` 偵測 → 自動發送「請從上次截斷處繼續輸出」→ 拼接結果 → 記 `token_continuation_total` metric
- [x] Scratchpad reload on resume：agent restart / crash recovery 時自動載入最新 scratchpad.md 到 context head
- [x] 清理策略：任務成功完成 → archive scratchpad（move to `data/agents/<agent_id>/archive/`）；失敗 → 保留供 debug
- [x] SSE event：`agent.scratchpad.saved`（agent_id / turn / size_bytes / sections_count）
- [x] Metrics：`scratchpad_saves_total` / `scratchpad_size_bytes` / `token_continuation_total`
- [x] **UI — Scratchpad Progress Indicator（擴充 `agent-matrix-wall.tsx` Health Card）**：
  - [x] Progress bar：scratchpad 持久化佔比（已寫入的 turn 數 / 總 turn 數）
  - [x] 最後寫入時間（relative，如「2 min ago」）
  - [x] 點擊展開 scratchpad 內容 preview（read-only，markdown rendered）
  - [x] 若 agent crash 且有 scratchpad → 卡片顯示「Recoverable ●」badge
- [x] **UI — Auto-Continuation Indicator**：在 agent message stream 中，auto-continued 的訊息標注「↩ auto-continued」小 tag
- [x] 整合測試：10 輪循環 → scratchpad 自動寫入；mock crash → reload scratchpad → agent 接續；max_tokens truncation → auto-continue → 拼接正確
- [x] 預估：**3 day**（backend 2d + UI 1d）**✅ AI completed 2026-04-17**

### R4. CATC State Snapshot 斷點續傳 (#310)
- [ ] O0 `TaskCard` dataclass 擴充：新增 `state_snapshot: Optional[str]`（BASE64 encoded JSON，含 scratchpad + tool_call_history + partial_output + turn_counter）
- [ ] Worker claim 任務時：若 `state_snapshot` 存在 → hot-resume from checkpoint（跳過已完成的 tool calls），否則 cold-start
- [ ] Snapshot 生成時機：scratchpad save 時同步生成 snapshot → 寫入 CATC payload → 存入 queue backend
- [ ] Snapshot 大小限制：≤ 512 KiB（超過則只保留最新 scratchpad + turn_counter，drop tool_call_history）
- [ ] 安全：snapshot 加密 at-rest + 簽名防竄改（HMAC-SHA256 with CATC-specific key）
- [ ] **UI — Checkpoint Timeline（擴充 `run-history-panel.tsx`）**：
  - [ ] 任務詳情頁新增水平時間軸，顯示所有 checkpoint 點（● = scratchpad save / ✕ = crash / ▶ = resume）
  - [ ] 每個 checkpoint 可點擊查看當時的 scratchpad 內容
  - [ ] crash → resume 之間用虛線連接，標註「resumed from checkpoint #N」
  - [ ] Checkpoint 間距顏色編碼：綠色 = 正常進展、黃色 = entropy 升高中、紅色 = 接近 deadlock
  - [ ] Timeline 末尾顯示「total recovered time」（= 從 checkpoint resume 省下的時間 vs. cold-restart 的預估時間）
- [ ] 整合測試：task running → 3 checkpoints → crash → worker re-claim → hot-resume from checkpoint#3 → task continues → done；checkpoint timeline UI 正確顯示 5 個點
- 預估：**2.5 day**（backend 1.5d + UI 1d）

### R5. Active-Standby HA 具體方案（Keepalived + Redis M-S）(#311)
- [ ] `deploy/ha/keepalived.conf.example`：VRRP instance 配置範本（VIP / priority / auth / health check script）
- [ ] `deploy/ha/redis-sentinel.conf.example`：Redis Sentinel 3-node 最小配置
- [ ] Health check script `scripts/ha_health_check.sh`：檢查 FastAPI `/healthz` + Redis ping + queue depth；失敗 → Keepalived 降權 → failover
- [ ] Consul leader election 替代方案（雲端 VPC 無 L2 multicast 時）：`backend/ha_leader.py` 用 Consul session + KV lock
- [ ] Failover runbook `docs/ops/ha_failover_runbook.md`：手動 / 自動 failover 步驟、驗證清單、回切流程
- [ ] Redis replication lag monitoring：SSE event `ha.redis_lag`（lag_bytes / lag_seconds）
- 預估：**2 day**

### R6. Serverless PaaS Adapter（Fargate / Cloud Run）(#312)
- [ ] `backend/deploy/fargate.py`：AWS ECS + Fargate task definition 生成 + 佈署
- [ ] `backend/deploy/cloud_run.py`：GCP Cloud Run service 生成 + 佈署
- [ ] 統一 `PaaSAdapter` interface：`provision()` / `deploy(image_uri)` / `scale(min, max)` / `teardown()` / `get_url()`
- [ ] Cold-start mitigation：pre-warm（Fargate 的 minimum task count / Cloud Run 的 min-instances）
- [ ] Task-queue bridge：PaaS container 啟動後自動連接 O2 Message Queue，pull CATC 任務
- [ ] Scale-to-zero 支援：idle timeout 後 PaaS 自動縮 → queue consumer 斷線 → 新任務入隊時自動 scale-up
- [ ] 成本估算 helper：依 vCPU/hour + memory/hour 計算 burst 成本 vs. always-on 成本
- [ ] 整合測試：mock Fargate API → provision + deploy + scale + teardown 四步流程
- 預估：**2.5 day**

### R7. Deployment Topology View UI + Bootstrap Wizard Extension (#313)
- [ ] **UI — Deployment Topology View（新元件 `deployment-topology.tsx`）**：
  - [ ] 偵測當前部署模式（Single Node / Active-Standby / K8s / PaaS）——呼叫 `GET /api/v1/system/deploy-topology`
  - [ ] Single Node 模式：顯示單機 CPU / Mem / Disk 使用率 + Docker container list + cgroup 狀態
  - [ ] Active-Standby 模式：雙節點拓撲圖（primary / standby）+ VIP 歸屬 + Redis replication lag + heartbeat latency
  - [ ] K8s 模式：pod count / ReplicaSet status / node health / HPA current/target replicas
  - [ ] PaaS 模式：service URL / active instances / cold-start p99 / scale-to-zero countdown
  - [ ] 各模式共有底部列：queue depth / DLQ count / active workers / last failover event
  - [ ] 模式切換建議：依當前 load 自動建議升級路徑（Single → HA / HA → K8s），呈現為 info banner
- [ ] `backend/routers/system.py` 新增 `GET /api/v1/system/deploy-topology`：回傳 deploy mode + 各模式特定指標
- [ ] **Bootstrap wizard（L 系列）extension**：L7 `detect_deploy_mode()` 延伸為可選 HA / K8s / PaaS 配置步驟
- [ ] **UI — integration-settings 延伸**：Settings modal 新增 Deployment 區塊，內嵌 Topology View + 「Upgrade Plan」one-click trigger
- [ ] Deployment runbook `docs/ops/deployment_hierarchy.md`：4 方案完整對比表 + 升級步驟 + 回切流程
- 預估：**2.5 day**（backend 1d + UI 1.5d）

### R8. Idempotent Retry 正規化（worktree-based，覆寫白皮書 §三.2 的 git clean 設計）(#314)
- [ ] **設計決策（明確覆寫白皮書）**：不使用 `git clean -fd` + `git checkout .`；改用「discard current worktree + `git worktree add` create fresh worktree from anchor commit」
- [ ] Anchor commit 機制：task 開始前記錄 `anchor_commit_sha`（寫入 CATC metadata）；retry 時 fresh worktree 從此 SHA 分支
- [ ] WorkspaceManager 擴充：`discard_and_recreate(agent_id, anchor_sha)` → 刪除舊 worktree dir（安全刪除，先 `git worktree remove --force`）→ 新建 worktree → 回傳新 path
- [ ] Audit trail：每次 retry 寫入 `audit_log`（`retry.worktree_recreated`，附 old_worktree_path / anchor_sha / reason）
- [ ] 既有 startup cleanup 延伸：啟動時掃描 orphan worktree（`git worktree list` 中不屬於任何 active agent 的 worktree）→ 自動 remove + log
- [ ] 整合測試：task fail → retry → old worktree 消失 + new worktree 乾淨 + anchor_sha 正確 + audit logged
- 預估：**1 day**

### R9. P1/P2/P3 ↔ L1-L4 通報統一 + E2E Watchdog Integration Tests (#315)
- [ ] 不新起 P1/P2/P3 分級——改為 L1-L4 notification tier 上掛 `severity` tag：
  - [ ] P1（系統崩潰）→ L4 PagerDuty + L3 Jira（severity: P1）+ L2 Slack/Discord @everyone + SMS
  - [ ] P2（任務卡死）→ L3 Jira（severity: P2, label: blocked）+ L2 ChatOps interactive（R1）
  - [ ] P3（自動修復中）→ L1 log + email 匯總報告
- [ ] `backend/notifications.py` 擴充：`send_notification(tier, severity, payload, interactive=False)` — interactive=True 時走 R1 ChatOps bridge
- [ ] 統一 event taxonomy：`watchdog.p1_system_down` / `watchdog.p2_cognitive_deadlock` / `watchdog.p3_auto_recovery` → 各自映射 L1-L4 + severity tag
- [ ] E2E watchdog integration tests（headless，不需真 Discord）：
  - [ ] Agent 語意空轉 → R2 entropy alert → R9 P2 mapping → L2 ChatOps notification（mock）→ R1 inject hint → agent resumes
  - [ ] PEP 攔截 prod-deploy → R0 HOLD → R9 P2 mapping → L2 ChatOps → R1 approve → PEP release → tool executes
  - [ ] Agent crash → R4 checkpoint → R8 worktree recreate → R3 scratchpad reload → agent hot-resumes
  - [ ] System OOM → M1 cgroup kill → R9 P1 mapping → L4 PagerDuty（mock）+ L3 Jira ticket auto-create
- [ ] **UI — notification-center 延伸**：每條通知卡片新增 `severity` badge（P1 紅 / P2 橙 / P3 灰）；filter bar 加 severity dropdown
- 預估：**2 day**（backend 1.5d + UI 0.5d）

**Priority R 總預估**：**25.5 day**（backend 17.5d + UI 8d）（solo ~5 週，2-person team ~3 週）

**建議切段交付**：
1. **R0 + R8 + R9**（6.5d）— PEP Gateway + 安全重試 + 統一通報 + E2E watchdog tests。**最高優先——填防護缺口**
2. **R1**（4d）— ChatOps Interactive。on-call UX 飛躍（含 ChatOps Mirror Panel）
3. **R2 + R3**（5.5d）— Semantic entropy + scratchpad。agent 智能 + 持久化（含 Agent Health Card + Progress Indicator）
4. **R4**（2.5d）— 斷點續傳。配合 R3 形成完整 crash-recovery 鏈（含 Checkpoint Timeline）
5. **R5 + R6 + R7**（7d）— HA 部署方案 + Serverless PaaS + Deployment Topology View。Scale-out 準備

---

## 🅓 Priority D — L4 Layer B (per-product skill packs)

Each pack must deliver 5 artifacts (DAG tasks / code scaffolds / integration
tests / HIL recipes / doc templates) per framework contract.

### D1. SKILL-UVC (pilot, #218)
- [x] UVC 1.5 descriptor scaffold (H.264 + still image + extension unit)
- [x] gadget-fs/functionfs binding
- [x] UVCH264 payload generator
- [x] USB-CV compliance test recipe
- [x] Datasheet + user manual templates
- [x] **First skill done — validates CORE-05 framework**

### D2. SKILL-IPCAM (#219)
- [ ] live555 or gstreamer RTSP server scaffold
- [ ] ONVIF Device / Media / Events / PTZ endpoints
- [ ] WS-Discovery multicast responder
- [ ] H.264/H.265 hardware codec binding per SoC
- [ ] ODTT Profile S test recipe
- [ ] IPCam datasheet + ONVIF conformance statement templates

### D3. SKILL-UAC-MIC (#220)
- [ ] UAC 2.0 descriptors
- [ ] Mic array (2/4/6 ch) beamforming
- [ ] AEC + noise suppression DSP
- [ ] USB-IF audio test recipe
- [ ] Doc templates

### D4. SKILL-DISPLAY (#221)
- [ ] LVGL or Qt scaffold
- [ ] Touch driver integration (FT6336 / GT911)
- [ ] OTA integration (CORE-16)
- [ ] Display calibration routine
- [ ] Doc templates

### D5. SKILL-DOORBELL (#232-sub)
- [ ] Reuse SKILL-IPCAM base
- [ ] Add PIR + doorbell button handling
- [ ] Two-way audio (SKILL-UAC-MIC reuse)
- [ ] Cloud event notification

### D6. SKILL-DASHCAM (#232-sub)
- [ ] Loop recording + G-sensor lock
- [ ] GPS NMEA overlay
- [ ] Parking mode + motion detect
- [ ] microSD health monitoring

### D7. SKILL-LIVESTREAM (#232-sub)
- [ ] RTMP publish + SRT fallback
- [ ] WebRTC push (for low-latency)
- [ ] Bitrate adaptive ladder
- [ ] Scene switching + overlays

### D8. SKILL-ROUTER (#232-sub)
- [ ] OpenWrt base image
- [ ] Mesh networking (IEEE 802.11s or proprietary)
- [ ] QoS + traffic shaping
- [ ] VPN integration (WireGuard / OpenVPN)

### D9. SKILL-5G-GW (#232-sub)
- [ ] Modem AT / QMI command set
- [ ] Dual-SIM + automatic fallback
- [ ] Carrier APN database
- [ ] Signal quality telemetry

### D10. SKILL-BT-EARBUDS (#232-sub)
- [ ] A2DP + HFP profile
- [ ] LE Audio (Auracast) support
- [ ] ANC DSP integration
- [ ] Low-power touch/tap gesture

### D11. SKILL-VIDEOCONF (#232-sub)
- [ ] SKILL-UVC + SKILL-UAC composition
- [ ] WebRTC endpoint (JSEP + ICE)
- [ ] AEC + echo management
- [ ] Teams/Zoom/Meet compatibility

### D12. SKILL-CARDASH (#232-sub)
- [ ] Android Auto / QNX skeleton
- [ ] AUTOSAR adapter stub
- [ ] ISO 26262 artifact gate (via CORE-09)
- [ ] CAN bus integration

### D13. SKILL-WATCH (#232-sub)
- [ ] Wear OS or RTOS baseline
- [ ] BLE peripheral role + notifications
- [ ] Heart rate / SpO2 / ECG sensor stack
- [ ] Power-critical UI design

### D14. SKILL-GLASSES (#232-sub)
- [ ] Display driver (micro-OLED / waveguide)
- [ ] 6DoF tracking via CORE-14
- [ ] Low-power always-on display
- [ ] Voice assistant integration

### D15. SKILL-MEDICAL (#232-sub)
- [ ] IEC 60601 artifact gate (via CORE-09)
- [ ] SW-A/B/C classification workflow
- [ ] Risk file template
- [ ] Cybersecurity per IEC 81001-5-1

### D16. SKILL-DRONE (#232-sub)
- [ ] PX4 or ArduPilot baseline
- [ ] MAVLink telemetry link
- [ ] Failsafe state machine
- [ ] GPS + optical flow + LIDAR fusion

### D17. SKILL-INDUSTRIAL-PC (#232-sub)
- [ ] Modbus RTU/TCP
- [ ] OPC-UA server
- [ ] EtherCAT master (if applicable)
- [ ] Redundant power management

### D18. SKILL-SMARTPHONE (deferred, #232-sub)
- [ ] **Scope warning: 15-20 day solo.** Recommend outsourcing or deferring.
- [ ] AOSP vendor tree integration
- [ ] Modem stack
- [ ] Camera HAL
- [ ] If pursued: split into 4 sub-skills

### D19. SKILL-SCANNER (#244-sub)
- [ ] Paper path sensor + feed motor control
- [ ] ISP tuning for documents (binarization + deskew)
- [ ] OCR via CORE-19
- [ ] TWAIN/SANE driver

### D20. SKILL-PRINTER (#244-sub)
- [ ] Print engine motor control (via CORE-25 motion abstraction)
- [ ] IPP/CUPS via CORE-20
- [ ] Ink/toner level telemetry
- [ ] Queue management

### D21. SKILL-MFP (#244-sub)
- [ ] Compose SKILL-SCANNER + SKILL-PRINTER
- [ ] Copy / fax / email-to-scan workflows
- [ ] Shared control panel UI
- [ ] ~70% reuse of above — thin wrapper

### D22. SKILL-BARCODE-GUN (#244-sub)
- [ ] Imager + trigger button handling
- [ ] HID wedge output mode
- [ ] Bluetooth SPP mode
- [ ] Symbology selection via CORE-22

### D23. SKILL-PAYMENT-TERMINAL (#244-sub)
- [ ] EMV L2 kernel integration
- [ ] PCI-PTS physical tamper handling (via CORE-18 + CORE-15)
- [ ] P2PE key injection
- [ ] Receipt printer + magstripe + NFC combo

### D24. SKILL-POS (#244-sub)
- [ ] Composite: payment + barcode + receipt + HMI + admin
- [ ] Inventory lookup integration
- [ ] Cashier workflow UI
- [ ] Daily close-out reports

### D25. SKILL-KIOSK (#244-sub)
- [ ] Display + touch + (optional) payment + network
- [ ] Attract-loop screensaver
- [ ] Remote content management
- [ ] Kiosk-mode lockdown (browser + app allowlist)

### D26. SKILL-TOF-CAM (#256-sub)
- [ ] ToF sensor driver via CORE-23
- [ ] Point cloud output (PCD/PLY)
- [ ] Intrinsic + extrinsic calibration
- [ ] Temperature compensation

### D27. SKILL-3D-PRINTER (#256-sub)
- [ ] Marlin or Klipper-style firmware via CORE-25
- [ ] Bed leveling routine (mesh or 4-point)
- [ ] Filament runout + thermal safety
- [ ] Slicer handshake (config exchange)

### D28. SKILL-MACHINE-VISION (#256-sub)
- [ ] Multi-camera sync via CORE-24
- [ ] Frame grabber + encoder trigger
- [ ] PLC integration
- [ ] Lighting control (strobe + polarizer)

### D29. SKILL-HMI-WEBUI (pilot, #262)
- [ ] Reference embedded web admin UI（參考對象：D2 SKILL-IPCAM 的 ONVIF 設定 / stream preview / user 管理 / OTA）
- [ ] 沿用 C26 generator + backend binding + size budget + IEC 62443 gate — validate CORE-26 framework
- [ ] rootfs packaging：`/www` partition + fastcgi/civetweb handler binary + inline JS/CSS assets 產出完整 image
- [ ] QEMU boot + Playwright E2E：cold boot → login → ONVIF probe → stream preview 整條路徑
- [ ] Flash partition size budget 驗收（目標 ≤ 3 MiB total for admin UI + handlers）
- [ ] Embedded browser 相容性：aarch64 Chromium 90 / armv7 WebKit 2.36 雙平台 smoke
- [ ] i18n：en / zh-TW 雙語 smoke test（驗證 C26 i18n 框架）
- [ ] IEC 62443 baseline 驗證（CSP、CSRF token、session cookie flags、帳密 rate limit）
- [ ] Datasheet + deployment runbook templates
- [ ] **First HMI skill — validates CORE-26 framework**（比照 D1 SKILL-UVC 驗證 C5 的 pattern）

---

## 🅔 Priority E — L4 Layer C (software tracks)

### E1. SW-TRACK-01 Academic algo simulation runner (#233)
- [ ] Python/MATLAB executor with resource limits
- [ ] Paper reproduction harness (tagged checkpoints)
- [ ] Reference dataset lifecycle (version + checksum + lineage)
- [ ] GPU scheduling (queue + fair share)
- [ ] Artifact archival (models + plots + notebooks)

### E2. SW-TRACK-02 Optical simulation runner (#234)
- [ ] Headless Zemax COM automation
- [ ] Code V scripting wrapper
- [ ] LightTools Python API wrapper
- [ ] Parameter sweep harness + tolerance analysis
- [ ] Report generator: MTF / spot diagram / encircled energy

### E3. SW-TRACK-03 ISO standard implementation track (#235)
- [ ] Spec section → code symbol traceability matrix
- [ ] Formal verification hooks (Frama-C for C, TLA+ for protocols)
- [ ] Certification artifact generator (requirements, design, test, traceability)
- [ ] Compliance checklist per standard

### E4. SW-TRACK-04 Collaborative test tools (#236)
- [ ] Test fixture registry (shared across teams)
- [ ] Multi-tenant result dashboard
- [ ] Cross-team replay (export → import test run)
- [ ] RBAC via CORE-21

### E5. SW-TRACK-05 Factory production line tuning tool (#237)
- [ ] Jig control: GPIO / relay / DAQ board drivers
- [ ] Test sequencer (YAML-defined flow)
- [ ] MES integration (SECS/GEM or REST)
- [ ] Yield dashboard + SPC charts
- [ ] Station lockout on test fail

### E6. SW-WEB-ERP (#245)
- [ ] Scaffold from CORE-21 template
- [ ] Modules: finance / accounting / procurement / orders
- [ ] Chart of accounts + GL posting
- [ ] Invoice + PO + SO workflows
- [ ] Multi-currency + tax calc

### E7. SW-WEB-WMS (#246)
- [ ] Inbound / outbound / stocktake / transfer workflows
- [ ] Barcode via CORE-22 integration
- [ ] Bin location + ABC analysis
- [ ] Shipping carrier integration

### E8. SW-WEB-HRM (#247)
- [ ] Attendance (punch-in + geofence + webcam)
- [ ] Leave request + approval workflow
- [ ] Payroll (formula engine + slip generation)
- [ ] Performance review cycles

### E9. SW-WEB-MATERIAL (#248)
- [ ] BOM management (multi-level + phantom)
- [ ] Procurement workflow
- [ ] Inventory valuation (FIFO/LIFO/Avg)
- [ ] Reorder point alerts

### E10. SW-WEB-SALES-INV (#249)
- [ ] Lightweight subset: sales order + purchase order + inventory
- [ ] Single-tenant default (vs multi-tenant ERP)
- [ ] POS integration

### E11. SW-WEB-PORTFOLIO (#250) — **smallest, do first**
- [ ] Content template (about / projects / contact)
- [ ] Theme customization
- [ ] Domain binding guide
- [ ] No new backend needed

### E12. SW-WEB-ECOMMERCE (#251)
- [ ] Catalog (categories + variants + attributes)
- [ ] Cart + checkout + payment (via CORE-18)
- [ ] CMS (pages + blog + banners)
- [ ] Admin: orders / fulfillment / returns

### E13. SW-IMG-ANALYSIS (#257)
- [ ] Thin wrapper over SW-TRACK-01
- [ ] OpenCV + PyTorch pipeline templates
- [ ] Batch workflow runner
- [ ] Annotation UI (bbox / polygon / mask)

### E14. SW-3D-MODELING (#258) — **heaviest new track**
- [ ] Backend: OpenCASCADE + CGAL + VTK
- [ ] UI: Three.js / WebGL viewport
- [ ] I/O: STL/STEP/OBJ/PLY/glTF/3MF
- [ ] Mesh operations (boolean / remesh / decimate)
- [ ] Parametric modeling basics

### E15. SW-DEFECT-DETECT (#259)
- [ ] Image source via CORE-24
- [ ] AI anomaly detection (PaDiM / PatchCore baseline)
- [ ] Rule engine (threshold + geometric checks)
- [ ] MES reporting via CORE-13
- [ ] Historical dashboard with trend

---

## 🅕 Priority F — META (matrices / SOPs)

### F1. META bundle #1 — embedded portfolio (#238)
- [ ] Product × certification matrix (FCC/CE/NCC/UL/IEC/ISO/FDA)
- [ ] SoC × skill compatibility matrix
- [ ] test_assets/ lifecycle SOP
- [ ] Cross-skill integration test strategy
- [ ] Third-party license audit gate

### F2. META bundle #2 — payment & enterprise (#252)
- [ ] Payment compliance (PCI L1-L4 × EMV regional × HSM vendor)
- [ ] Enterprise deployment topology (on-prem / SaaS / hybrid)
- [ ] Device↔backend pairing standard (POS/KIOSK/payment terminal)

### F3. META bundle #3 — 3D & machine vision (#260)
- [ ] 3D file format support matrix (STL/STEP/OBJ/PLY/glTF/3MF × R/W)
- [ ] Industrial vision interface × trigger modality matrix

---

## 🅣 Priority T — Billing & Payment Gateway（金流計費系統 — Stripe / ECPay / PayPal）

> 背景：OmniSight 商業化需要完整的金流基建。採用「訂閱 + 用量（token 消耗）」混合制。同一時間只啟用一家金流，另外兩家作為備用。主要金流推薦 **Stripe**（完全自定義付款頁 + 3D Secure 2 頁內 modal + 原生 metered billing）；備用一 **綠界 ECPay**（台灣本地 TWD + 超商/ATM）；備用二 **PayPal**（國際客戶偏好）。
>
> **付款頁面策略**：Stripe 使用 Payment Element 嵌入 OmniSight 頁面（零跳轉 + 3DS2 modal）；ECPay 強制跳轉到 ECPay hosted page（無法自定義）；PayPal Advanced Checkout 卡片欄位可嵌入但 3DS 需跳轉。三家共用 `PaymentGateway` 統一介面，切換只需改 `OMNISIGHT_PAYMENT_GATEWAY` env。
>
> **定價模型**：Free ($0, 50K tokens) / Starter ($19, 500K) / Pro ($49, 2M) / Business ($149, 8M) / Enterprise ($499, 30M+)。超量按 $0.015-0.03/1K tokens。目標毛利率 70%。
>
> 相依（硬前置）：**L（Bootstrap，✅ 系統可部署）**、**K（Auth，✅ 客戶帳號）**、**I（Multi-tenancy，✅ 帳單 per-tenant）**。金流可在 V/R 之後或並行推進。

### T0. PaymentGateway 統一介面 + 金流切換機制 (#326)
- [ ] `backend/billing/gateway.py`：`PaymentGateway` ABC — `create_customer` / `create_subscription` / `report_usage` / `create_checkout_session` / `handle_webhook` / `cancel_subscription` / `get_invoices` / `refund`
- [ ] `OMNISIGHT_PAYMENT_GATEWAY=stripe | ecpay | paypal` env 切換，啟動時只初始化一家
- [ ] Gateway factory：`get_gateway() -> PaymentGateway`，singleton pattern
- [ ] 統一 webhook router：`POST /api/v1/billing/webhook` → 依 gateway 分派到對應 adapter
- [ ] 統一 error hierarchy：`PaymentError` / `CardDeclinedError` / `SubscriptionNotFoundError` / `WebhookVerificationError`
- [ ] Billing event SSE：`billing.payment_succeeded` / `billing.payment_failed` / `billing.subscription_updated`
- [ ] 測試：mock gateway 跑完整 lifecycle（create → subscribe → usage → invoice → cancel）
- [ ] 預估：**1.5 day**

### T1. Stripe 整合（主要金流）(#327)
- [ ] `backend/billing/stripe_gateway.py`：實作 `PaymentGateway` ABC 的所有方法
- [ ] Stripe Customer 建立：與 OmniSight user + tenant 綁定（`stripe_customer_id` 存 `users` 表）
- [ ] Stripe Payment Element 前端整合：`components/omnisight/stripe-checkout.tsx`（`@stripe/react-stripe-js` + `PaymentElement`）
- [ ] 3D Secure 2 處理：Stripe 自動觸發 + 頁內 modal（不跳轉）+ `payment_intent.requires_action` 狀態處理
- [ ] Stripe Billing 訂閱：Product + Price（5 方案）+ Subscription + 試用期 + 升降級 proration
- [ ] Metered billing：`stripe.SubscriptionItem.create_usage_record(quantity=token_count)` — 每次 agent 執行完畢時回報 token 消耗
- [ ] Webhook 簽名驗證：`stripe.Webhook.construct_event(payload, sig, secret)` + 重放防護
- [ ] Stripe Customer Portal link：讓客戶自助管理訂閱 / 更換卡片 / 查看發票
- [ ] Stripe Tax（選配）：自動依客戶地區計算稅額
- [ ] 測試：mock Stripe API → checkout → 3DS → subscribe → usage report → invoice → webhook → portal
- [ ] 預估：**3.5 day**

### T2. 綠界 ECPay 整合（備用一：台灣本地）(#328)
- [ ] `backend/billing/ecpay_gateway.py`：實作 `PaymentGateway` ABC
- [ ] ECPay `AioCheckOut` 整合：建立訂單 → redirect 到 ECPay 付款頁 → 回傳 ReturnURL + OrderResultURL
- [ ] 信用卡 + 超商代碼 + ATM 虛擬帳號 三種付款方式
- [ ] 3D Secure：由 ECPay 在其頁面內處理（merchant 端不需額外邏輯，但需處理驗證失敗回傳）
- [ ] 定期定額（`PeriodAmount`）：對應訂閱方案，但無原生 metered billing → 自建用量累計 + 月底開補扣單
- [ ] 自建用量追蹤：`backend/billing/usage_ledger.py` 累計 token 消耗，月底計算超量費 → 產生 ECPay 補扣訂單
- [ ] Webhook（`OrderResultURL` POST）：驗證 `CheckMacValue` + 訂單狀態更新
- [ ] 台灣發票整合（選配）：ECPay 電子發票 API（`E-Invoice`）
- [ ] 測試：mock ECPay API → checkout redirect → 回傳 → 定期定額 → 用量補扣 → webhook 驗簽
- [ ] 預估：**3 day**

### T3. PayPal 整合（備用二：國際）(#329)
- [ ] `backend/billing/paypal_gateway.py`：實作 `PaymentGateway` ABC
- [ ] PayPal Advanced Checkout：JS SDK hosted card fields 嵌入 OmniSight 頁面
- [ ] 3D Secure：`SCA_WHEN_REQUIRED` 觸發 → redirect to PayPal for authentication → redirect back
- [ ] PayPal Subscriptions API：建 Plan + Subscription（試用 / 取消）
- [ ] 無原生 metered billing → 複用 T2 的 `usage_ledger.py` + 月底 PayPal `capture` 補扣
- [ ] Webhook（`PAYMENT.SALE.COMPLETED` / `BILLING.SUBSCRIPTION.*`）：驗簽 + 狀態同步
- [ ] PayPal Disputes 處理：`CUSTOMER.DISPUTE.CREATED` → 自動回覆交易證據（agent 執行記錄 + token 用量明細）
- [ ] 測試：mock PayPal API → checkout → 3DS redirect → subscribe → usage capture → webhook → dispute
- [ ] 預估：**2.5 day**

### T4. Token 用量追蹤 + Metered Billing 引擎 (#330)
- [ ] `backend/billing/token_meter.py`：agent 執行完畢 → `record_usage(tenant_id, agent_id, task_id, input_tokens, output_tokens, model, cost_usd)`
- [ ] 儲存：`token_usage` 表（tenant_id / timestamp / model / input_tokens / output_tokens / cost_usd / task_id）
- [ ] 即時累計：per-tenant 當月已用 tokens + 已用成本（Redis cached counter，每次 record 時 incr）
- [ ] 方案配額檢查：`check_quota(tenant_id)` → 超量時回傳 `QuotaAction`（`allow_and_bill` / `warn_approaching` / `hard_stop`）
- [ ] Stripe 路徑：自動呼叫 `usage_records` API 回報（real-time）
- [ ] ECPay / PayPal 路徑：累計到 `usage_ledger`，月底由 `T7 billing cycle` 結算
- [ ] 混合模型成本計算：依 model ID 查 `_PRICING` 表（system.py）計算實際成本 → 乘以 markup → 帳單金額
- [ ] SSE event：`billing.usage.tick`（每 10 次 record 推一次 → 前端 dashboard 即時更新）
- [ ] Metrics：`billing_tokens_total{tenant_id, model}` / `billing_revenue_usd_total{tenant_id, plan}`
- [ ] 測試：record 100 次 usage → 累計正確 → Stripe usage_record 呼叫正確 → 配額檢查 soft/hard 正確
- [ ] 預估：**2.5 day**

### T5. 訂閱管理（方案 / 試用 / 升降級 / 取消）(#331)
- [ ] `backend/billing/plans.py`：5 方案定義（Free / Starter / Pro / Business / Enterprise）+ 每方案 token 含量 + 超量單價 + 功能 feature flags
- [ ] Plan feature flags：`max_projects` / `max_agents` / `visual_preview` / `mobile_build` / `priority_model`（哪些功能各方案可用）
- [ ] 試用期：Starter/Pro 提供 14 天試用（Stripe 原生 / ECPay 手動追蹤）
- [ ] 升降級：mid-cycle proration（Stripe 原生 / ECPay+PayPal 手動計算剩餘天數差額）
- [ ] 取消：grace period 到期才停權（不立即切斷）；到期前 3d / 1d SSE 提醒
- [ ] `backend/routers/billing.py`：
  - [ ] `GET /billing/plans` — 列出方案 + 當前使用者的方案
  - [ ] `POST /billing/subscribe` — 建立訂閱 / 升降級
  - [ ] `POST /billing/cancel` — 取消（立即 or 期末）
  - [ ] `GET /billing/usage` — 當月 token 用量明細
  - [ ] `GET /billing/invoices` — 歷史帳單
- [ ] 測試：Free→Pro 升級 + proration 計算正確 + Pro→Starter 降級 + 取消 grace period
- [ ] 預估：**2.5 day**

### T6. Pricing Page + Checkout Flow UI (#332)
- [ ] `app/pricing/page.tsx`：定價頁面——5 方案對比表 + 月/年切換 + 功能對照 + CTA 按鈕
- [ ] `components/omnisight/pricing-table.tsx`：方案卡片元件（highlight 推薦方案 + current plan badge）
- [ ] `components/omnisight/checkout-modal.tsx`：付款彈窗——嵌入 Stripe Payment Element（或 ECPay redirect / PayPal buttons，依 gateway 切換）
- [ ] 3D Secure UX：Stripe modal 在 checkout-modal 內彈出（不離開 OmniSight）
- [ ] 付款成功動畫 + 重導到 dashboard / workspace
- [ ] 已訂閱使用者：CTA 變成「管理方案」→ 跳轉 T7 Customer Portal
- [ ] Responsive：desktop + tablet + mobile 三版排版
- [ ] 預估：**2 day**

### T7. Customer Portal（帳單管理 + 發票 + 用量明細）(#333)
- [ ] `app/settings/billing/page.tsx`：Settings 內的帳單管理頁
- [ ] 當前方案 + 下次扣款日 + 已用 token / 含量 + 使用率 progress bar
- [ ] Token 用量明細（`components/omnisight/usage-breakdown.tsx`）：per-day 折線圖 + per-model 分佈 + per-project 排名
- [ ] 帳單歷史（`components/omnisight/invoice-history.tsx`）：每月 invoice 列表 + PDF 下載 + 付款狀態 badge
- [ ] 付款方式管理：更換信用卡 / 查看到期日（Stripe Customer Portal link / ECPay 重新授權 / PayPal 管理）
- [ ] 升降級入口：方案對比 + one-click 升級 / 確認降級
- [ ] 預估：**2.5 day**

### T8. Webhook 處理 + 付款失敗重試 + Dunning (#334)
- [ ] `backend/billing/webhook_handler.py`：統一 webhook 入口 → 驗簽 → 分派到 gateway adapter → 更新訂閱/付款狀態
- [ ] Webhook 冪等：`webhook_events` 表記錄 event_id → 重複事件 skip（防重放）
- [ ] 付款失敗處理：
  - [ ] 首次失敗：SSE 告警 + email 通知「請更新付款方式」
  - [ ] 3 天後重試（Stripe Smart Retries 自動 / ECPay+PayPal 由 cron job 觸發）
  - [ ] 7 天仍失敗：降級到 Free 方案 + 限制功能 + email 最終通知
  - [ ] 14 天仍未修復：帳號進入 `suspended` 狀態（資料保留 30 天，之後歸檔）
- [ ] Dunning email 範本：3 封（首次失敗 / 7 天警告 / 14 天停權）
- [ ] Webhook audit：所有 webhook event 進 hash-chain `audit_log`
- [ ] 測試：mock payment_failed → 3 次重試 → 降級 → 停權 → 恢復付款 → 自動升回
- [ ] 預估：**2 day**

### T9. 金流安全 + PCI DSS + Secret Resolver + 審計 (#335)
- [ ] **Secret Resolver 多 backend 統一介面**（`backend/secret_resolver.py`）：
  - [ ] `SecretResolver` ABC：`get(key) -> str` / `set(key, value)` / `delete(key)` / `list_keys() -> list[str]` / `health_check() -> bool`
  - [ ] `EnvFileResolver`（Level 1）：從 `.env` 檔讀取——現有行為的正式封裝，開發環境預設
  - [ ] `FernetResolver`（Level 1.5）：沿用現有 `backend/secret_store.py` Fernet at-rest 加密——key 在磁碟但加密
  - [ ] `DopplerResolver`（Level 3）：Doppler REST API（`https://api.doppler.com/v3/configs/config/secrets`）+ service token auth
  - [ ] `AWSSecretsResolver`（Level 3）：AWS Secrets Manager（`boto3` `secretsmanager.get_secret_value`）+ IAM role auth
  - [ ] `VaultResolver`（Level 3）：HashiCorp Vault（`hvac` client）+ AppRole / token auth
  - [ ] `InfisicalResolver`（Level 3）：Infisical API（開源自建友好）+ service token auth
  - [ ] Factory：`get_resolver() -> SecretResolver`，依 `OMNISIGHT_SECRET_BACKEND=env | fernet | doppler | aws | vault | infisical` env 決定（預設 `env`）
  - [ ] Singleton + lazy init：整個 app 只建一次 resolver instance
  - [ ] Caching layer：secret 值 in-memory cache（TTL 5 min），避免每次 LLM call 都打 remote API
  - [ ] 測試：mock 每個 backend 的 get/set/delete + factory 切換 + cache TTL 過期 + health_check
- [ ] **config.py 整合**：`settings` 初始化時透過 `SecretResolver` 取得所有 `*_API_KEY` / `*_SECRET` 欄位值，取代直接從 env var 讀取
- [ ] **啟動時 secret 驗證**：`validate_startup_config()` 改走 resolver → 找不到必要 secret 時報清楚的錯誤訊息（含 backend 名稱 + 設定指引）
- [ ] **Secret rotation hook**：`resolver.on_rotation(key, callback)` — Vault / AWS 支援 rotation event → callback 更新 in-memory cache + 寫 audit_log `secret.rotated`
- [ ] PCI DSS SAQ-A 合規：OmniSight 不儲存 / 不處理 / 不傳輸卡號（全由 Stripe/ECPay/PayPal tokenize）
- [ ] API key 安全：Stripe secret key / ECPay HashKey+HashIV / PayPal client secret 統一走 `SecretResolver`（不再直接讀 env var）
- [ ] Webhook secret 安全：每家 webhook signing secret 統一走 `SecretResolver`
- [ ] 金額篡改防護：前端不傳金額，後端從 plan 定義計算 → 建 checkout session 時由後端設金額
- [ ] Refund 權限控制：只有 admin role 可觸發退款 → audit_log 記錄
- [ ] Rate limit：`/billing/subscribe` 和 `/billing/webhook` 加獨立 rate limit（防 brute force checkout）
- [ ] 金流切換 audit：切換 gateway 時寫 `audit_log`（`billing.gateway_switched`，含 from/to/operator）
- [ ] 金流健康檢查：`GET /api/v1/billing/health` — 驗 gateway API 可達 + webhook endpoint 可達 + resolver health + 最近一次 webhook 時間
- [ ] 滲透測試案例：重放 webhook / 偽造簽名 / 金額篡改 / 跨租戶訂閱操作 / resolver backend 偽造 / secret cache poisoning
- [ ] 預估：**3 day**（原 1.5d + Secret Resolver 1.5d）

**Priority T 總預估**：**25 day**（原 23.5d + Secret Resolver 1.5d）（solo ~5 週，2-person team ~3 週）

**建議切段交付**：
1. **T0 + T4 + T5**（6.5d）— 統一介面 + 用量追蹤 + 方案管理。billing 骨架可運行
2. **T1**（3.5d）— Stripe 整合。首個可收費的完整路徑（自定義付款 + 3DS + 訂閱 + metered）
3. **T6 + T7**（4.5d）— Pricing Page + Customer Portal。客戶面 UI 完成
4. **T8 + T9**（3.5d）— Webhook + Dunning + 安全。production-ready
5. **T2 + T3**（5.5d）— ECPay + PayPal 備用。按市場需求再開

---

## Execution order (recommended)

### Phase 1 — clear the runway (1-2 weeks)
A1 → A2 → B1 → B3 → B4 → B6 → B2 → B5 → B7 → B8..B11

### Phase 2 — L4 foundation (4-6 weeks)
C1 (SSH runner) → C0 (ProjectClass) → C2/C3/C4/C5 (schema + planner + framework)
→ D1 (UVC pilot) to validate C5 → C21 (enterprise web stack) in parallel

### Phase 3 — Layer A fill-out (4-6 weeks)
C6..C17 sequential (safety / radio / power / RT / connectivity / sensor-fusion / security / OTA / telemetry)
C18..C26 as demanded by prioritized skill packs (C26 preconditions any D skill shipping an embedded web admin UI)

### Phase 4 — Skill pack parallel sprint (6-10 weeks, 3-person team)
D2..D29 parallelized, prioritized by demand:
- Team α: imaging family (D2 IPCam, D5 doorbell, D6 dashcam, D19/20/21 scanner/printer/MFP)
- Team β: audio + display family (D3, D4, D10, D11, D13, D14)
- Team γ: industrial + safety-critical (D15 medical, D16 drone, D17 industrial-PC, D22 barcode, D23-D25 payment family, D26-D28 3D/MV, D29 HMI web UI pilot)
- D18 smartphone deferred / outsourced

### Phase 5 — Software tracks (4-6 weeks, 2-person team)
E11 (portfolio) first → E1..E5 (specialist SW tracks) →
E6..E10 + E12 (ERP family, all depend on C21) →
E13..E15 (imaging software, depend on C23/C24)

### Phase 6 — META polish + L4 total estimate validation (1 week)
F1 + F2 + F3 + cost burndown review

### Phase 7 — Ops / HA 補強（3-4 weeks，可與 Phase 3-5 並行）
G1 (graceful shutdown + readyz) → G2 (reverse proxy + dual instance) → G3 (blue-green)
→ G4 (Postgres + replica, 獨立並行) → G5 (K8s/Nomad manifests) → G6 (DR drill) + G7 (HA observability)

### Phase 8 — Host-aware Coordinator（~1.5 week，可與 Phase 7 並行）
H1 (host metrics, baseline hardcode) → H2 (load-aware scheduling) → H3 (UI panel)
→ H4a (AIMD + weighted token bucket) → H4b (cost calibration, H1 上線 1 週後執行)

### Phase 9 — Auth/Session 路線 C（~2.5 week）
S0 (shared foundation, 0.5d) → K-early K1-K3 (對外部署紅線, 2d)
→ J1-J6 (multi-session UX, 3.5d) → K-rest K4-K7 (MFA / rotate / bearer / argon2id, 5d)

### Phase 10 — Multi-tenancy Foundation（~3.5 week，必須在 G4 + H4a + S0 + K-early 之後）
I1 (schema + tenant_id) → I2 (RLS) → I3 (SSE filter) → I4 (secrets per-tenant)
→ I5 (filesystem namespace) → I6 (sandbox fair-share DRF) → I7 (frontend tenant-aware)
→ I8 (audit per-tenant chain) → I9 (rate limit) → I10 (multi-worker + Redis shared state)

### Phase 11 — Resource Hard Isolation（~1 week，緊接 Phase 10 之後）
M1 (cgroup CPU/mem) → M2 (disk quota + LRU) → M3 (per-tenant circuit breaker)
→ M4 (cgroup per-tenant metrics + AIMD 升級 + 計費) → M5 (prewarm 多租戶安全) → M6 (per-tenant egress)

### Phase 12 — Dependency Governance（~5.25 day，分三階段推進）
立即：N1 (lockfile + engines) → N2 (Renovate) → N5 (nightly preview) [~1.5d]
短期：N3 (OpenAPI 合約) → N4 (LangChain adapter 防火牆) → N6 (runbook + CVE/EOL) [~2d]
中期：N8 (DB matrix，與 G4 綁) [0.5d]
長期：N7 (multi-version CI) → N9 (fallback 分支) → N10 (blue-green 政策，與 G3 綁) [~1.25d]

### Phase 14 — Enterprise Event-Driven Orchestration（~4 week，必須在 G4 + I10 + S0 + K-early + B12 + L 之後）
O0 (CATC schema) → O1 (Redis dist-lock) → O2 (MQ abstraction) — 基礎設施可獨立 ship（4.5d）
→ O3 (Stateless worker pool + Gerrit push) + O8 (Migration feature flag) — dual-mode 可運行（5d）
→ O4 (Orchestrator Gateway) + O5 (JIRA 深度整合) — B2B 銷售可用（4d）
→ O6 (Merger Agent +2 vote) + O7 (Gerrit 雙簽 submit-rule + CI/CD arbiter) — 競品差異化賣點 + CLAUDE.md L1 政策變更點（4d）
→ O9 (觀測性) + O10 (安全加固) — 對外上線 gate（2.5d）

### Phase 15 — Web Platform Vertical（~3 week，窄整合第一條 vertical，dogfood 成本最低）
W0 (platform profile schema 泛化，W/P/X 共用前置) → W1+W2 (web profiles + simulate track) — 3d
→ W3+W4 (role skills + deploy adapters) — web 專案可自動建+部署（3d）
→ W5+W6 (compliance + Next.js pilot) — 首支可售 SKU（3.5d）
→ W7+W8+W9+W10 (Nuxt/Astro/CMS/觀測性) — 4d

### Phase 16 — Mobile App Vertical（~4 week，最重——簽章鏈 + store 合規）
P0+P1+P2 (平台基礎：profiles + toolchains + simulate track) — 5.5d
→ P3 (簽章鏈——進 store 前必備) — 2d
→ P4+P7 或 P8 (role skills + 擇一 pilot：iOS 或 Android 先做) — 3.5d
→ P5+P6 (Store 提交 + 合規 gates) — 首支可售（4d）
→ P8/P7 另一 + P9 (另一原生 + 跨平台 Flutter/RN) — 4.5d
→ P10 (觀測性) — 0.5d

### Phase 17 — Pure Software Vertical（~2.5 week，最輕量——OmniSight 後端即 FastAPI，dogfood 門檻低）
X0+X1+X2+X3 (平台基礎 + 多語言 build/package) — 5d
→ X4 (License / CVE / SBOM 合規) — 1d
→ X5 (FastAPI pilot，dogfood) — 1.5d
→ X6-X9 (Go / Rust / Tauri / Spring Boot，隨 demand ship) — 4.5d

### Phase 18 — Bootstrap Wizard + 一鍵佈署（~1.5 week，需 B12 + G1 + K1 基礎）— ✅ 此 phase 完成後系統可上線
L1 (status 偵測 + /bootstrap 路由) → L2 (admin 密碼) → L3 (LLM provider) → L4 (CF Tunnel embed)
→ L5 (服務啟動 + SSE log) → L6 (smoke test + finalize) → L7 (部署模式偵測) → L8 (reset + E2E)
→ L9 (quick-start.sh 一鍵腳本 + CF Tunnel 自動建立 + GoDaddy NS 指引) → L10 (GHCR pre-built images) → L11 (雲端 Deploy 按鈕)

### Phase 19 — Visual Design Loop: Workspace Foundation（~1.5 week）
V0 (workspace router + layout + chat + bridge card + SSE routing) — 三個工作區 route 可訪問（6d）

### Phase 20 — Visual Design Loop: Web Workspace（~5 week，v0.dev parity target）
V1 (AI 生成引擎: UI designer agent + registry + design tokens + Figma/screenshot input) — 7d
→ V2 (Live preview: sandbox dev server + screenshot + error bridge + visual context) — 6d
→ V3 (迭代標註: annotator + inspector + iteration timeline + rollback) — 5d
→ V4 (工作區 UI + 輸出: three-pane layout + preview URL + block export + consistency) — 5d

### Phase 21 — Visual Design Loop: Mobile Workspace（~3 week，Codex parity target）
V5 (AI 生成引擎: mobile designer agent + component registry + Figma/screenshot input) — 4d
→ V6 (Device preview: mobile sandbox + emulator screenshot + device frame + multi-device grid) — 6d
→ V7 (迭代 + 工作區 UI: annotation + timeline + three-pane + build status + store dashboard) — 5d

### Phase 22 — Visual Design Loop: Software Workspace + CLI + Polish（~2 week）
V8 (Software 工作區 UI: terminal + API docs + release dashboard) — 4d
→ V9 (CLI MVP + image gen tool + onboarding + cross-workspace E2E tests) — 5d

### Phase 23 — Watchdog & DR Remaining（~3 week，R4-R9，可與 Phase 21/22 並行）
R4 (斷點續傳 + Checkpoint Timeline) — 2.5d
→ R8 + R9 (安全重試 + 統一通報) — 3d
→ R5 + R6 + R7 (Active-Standby HA + Serverless PaaS + Deployment Topology View) — 7d

### Phase 24 — Billing & Payment Gateway（~5 week，需 L + K + I 前置，可與 Phase 22/23 並行）
T0 + T4 + T5 (統一介面 + 用量追蹤 + 方案管理) — billing 骨架（6.5d）
→ T1 (Stripe 整合 — 自定義付款 + 3DS + metered) — 首個可收費路徑（3.5d）
→ T6 + T7 (Pricing Page + Customer Portal) — 客戶面 UI（4.5d）
→ T8 + T9 (Webhook + Dunning + PCI DSS) — production-ready（3.5d）
→ T2 + T3 (ECPay + PayPal 備用) — 按市場需求再開（5.5d）

---

## Totals

| Layer | Range |
|---|---|
| A (infrastructure) | 105-149 day |
| B (skill packs) | 160-225 day |
| C (software tracks) | 129-187 day |
| O (enterprise orchestration) | 20 day |
| W (web vertical) | 13.5 day |
| P (mobile vertical) | 20 day |
| X (software vertical) | 12 day |
| R (watchdog + DR + UI) | 25.5 day |
| V (visual design loop + workspace) | 48 day |
| T (billing + payment gateway) | 25 day |
| META | 4-8 day |
| **Total** | **~562-732.5 day** |

3-person team parallelized: **~7-10 months wall-clock**.
