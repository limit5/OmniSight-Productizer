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
- [ ] Nuxt 4 專案骨架 generator
- [ ] Nitro engine 多 target（Node / Edge / Cloudflare Workers / Bun）
- [ ] Pinia state + Vue Router
- [ ] Vitest + Playwright
- [ ] 預估：**1.5 day**

### W8. SKILL-ASTRO（選配, #282）
- [ ] Astro 5 content-heavy 站骨架
- [ ] Islands architecture + MDX 支援
- [ ] Sanity/Contentful CMS 接口
- [ ] 預估：**1 day**

### W9. 共用 CMS adapters（Headless CMS 接口 library）(#283)
- [ ] Sanity / Strapi / Contentful / Directus adapters
- [ ] 統一 `CMSSource` interface：`fetch(query)` / `webhook_handler(payload)`
- [ ] 預估：**1 day**

### W10. Web 觀測性與監控 (#284)
- [ ] Sentry / Datadog RUM adapter
- [ ] Core Web Vitals 即時 dashboard
- [ ] Error tracking → JIRA ticket（透過 O5 IntentSource）
- [ ] 預估：**0.5 day**

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
- [ ] `configs/platforms/ios-arm64.yaml` — iOS Device ABI
- [ ] `configs/platforms/ios-simulator.yaml` — iOS Simulator (x86_64 + arm64)
- [ ] `configs/platforms/android-arm64-v8a.yaml`
- [ ] `configs/platforms/android-armeabi-v7a.yaml`
- [ ] 每 profile 宣告：SDK version / min API level / toolchain path / emulator spec
- [ ] 預估：**1 day**

### P1. Mobile toolchains 整合 (#286)
- [ ] Docker image base：`ghcr.io/omnisight/mobile-build`（Xcode CLI 16 + Android SDK 35 + Gradle 8 + CocoaPods 1.15）
- [ ] **macOS 限制**：iOS build 需真實 macOS host（Linux 不可）；支援 `OMNISIGHT_MACOS_BUILDER=self-hosted|macstadium|cirrus-ci|github-macos-runner` 遠端委派
- [ ] Android build 可純 Linux Docker 跑
- [ ] Fastlane / gym / gradle wrapper 整合
- [ ] 預估：**2 day**

### P2. Mobile simulate track (#287)
- [ ] `scripts/simulate.sh` 新增 `mobile` track：iOS Simulator + Android Emulator 雙平台 smoke + UI test
- [ ] `XCUITest`（iOS）+ `Espresso`（Android）整合
- [ ] Flutter/RN 走各自 test runner
- [ ] **Cloud device farm 整合**：Firebase Test Lab / AWS Device Farm / BrowserStack（真機覆蓋用）
- [ ] 螢幕截圖 matrix（多機型 × 多 locale）
- [ ] 預估：**2.5 day**

### P3. 簽章鏈管理（extend secret_store）(#288)
- [ ] Apple certs：Developer ID Certificate + Provisioning Profile + App Store Distribution Certificate
- [ ] Android keystore：per-app keystore + alias + password
- [ ] HSM 整合（選配）：AWS KMS / GCP KMS / YubiHSM — 私鑰不出 HSM
- [ ] 簽章 audit：每次 sign 寫 hash-chain audit_log（who / when / what artifact / what cert）
- [ ] Cert 到期 alert（30d / 7d / 1d pre-expiry SSE 告警）
- [ ] 預估：**2 day**

### P4. Mobile role skills (#289)
- [ ] `configs/roles/ios-swift.md`（SwiftUI / UIKit / Combine）
- [ ] `configs/roles/android-kotlin.md`（Jetpack Compose / Kotlin Coroutines）
- [ ] `configs/roles/flutter-dart.md`
- [ ] `configs/roles/react-native.md`
- [ ] `configs/roles/kmp.md`（Kotlin Multiplatform）
- [ ] `configs/roles/mobile-a11y.md`（iOS VoiceOver + Android TalkBack）
- [ ] 預估：**1 day**

### P5. Store 提交自動化 (#290)
- [ ] App Store Connect API 整合：create version / upload build / submit for review / screenshot upload
- [ ] Google Play Developer API：upload .aab / manage tracks（internal / alpha / beta / production）
- [ ] 提交流程走 O7 雙簽 +2：Merger Agent 驗技術正確性 + 人工終審（store guideline 合規）
- [ ] TestFlight / Firebase App Distribution 內部派發
- [ ] 預估：**2.5 day**

### P6. Store 合規 gates (#291)
- [ ] App Store Review Guidelines 自動檢查（明顯違規 pattern：假付費、誤導性 copy、未宣告 private API）
- [ ] Google Play Policy 自動檢查（背景位置權限、SDK 版本、資料安全區塊填寫）
- [ ] Privacy nutrition label / Data Safety Form 自動生成（依 SDK 依賴推導）
- [ ] 整合 C18 compliance harness
- [ ] 預估：**1.5 day**

### P7. SKILL-IOS (pilot, #292)
- [ ] SwiftUI app 骨架 generator
- [ ] Xcode project + SPM/CocoaPods 管理
- [ ] Push notification（APNs）integration template
- [ ] StoreKit 2 購買 template
- [ ] **First mobile skill — validates P0-P6**
- [ ] 預估：**2.5 day**

### P8. SKILL-ANDROID (pilot, #293)
- [ ] Jetpack Compose app 骨架
- [ ] Gradle 8 + Kotlin 2.0
- [ ] FCM push integration
- [ ] Play Billing template
- [ ] 預估：**2.5 day**

### P9. SKILL-FLUTTER / SKILL-RN（跨平台, #294）
- [ ] Flutter 3.x app 骨架 + 共用 iOS/Android config
- [ ] React Native 0.76 app 骨架
- [ ] 選一主推 + 另一為對照
- [ ] 預估：**2 day**

### P10. Mobile observability (#295)
- [ ] Firebase Crashlytics / Sentry Mobile adapter
- [ ] ANR detection（Android）/ watchdog termination（iOS）
- [ ] 線上 UI metric（render time / frame drop）
- [ ] 預估：**0.5 day**

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
- [ ] `configs/platforms/linux-x86_64-native.yaml`
- [ ] `configs/platforms/linux-arm64-native.yaml`
- [ ] `configs/platforms/windows-msvc-x64.yaml`
- [ ] `configs/platforms/macos-arm64-native.yaml`（需 macOS builder，參考 P1）
- [ ] `configs/platforms/macos-x64-native.yaml`（Intel legacy）
- [ ] 預估：**0.5 day**

### X1. Software simulate track (#297)
- [ ] `scripts/simulate.sh` 新增 `software` track：語言-native test runner
- [ ] 多語言 dispatcher：`pytest` / `go test` / `cargo test` / `mvn test` / `npm test` / `xUnit`
- [ ] Coverage gate：依 language 各自門檻（Python 80% / Go 70% / Rust 75% ...）
- [ ] Benchmark 回歸（可選）
- [ ] 預估：**1 day**

### X2. Software role skills (#298)
- [ ] `configs/roles/backend-python.md`（FastAPI / Django / Flask）
- [ ] `configs/roles/backend-go.md`（gin / fiber / net/http）
- [ ] `configs/roles/backend-rust.md`（axum / actix / rocket）
- [ ] `configs/roles/backend-node.md`（Express / NestJS / Fastify）
- [ ] `configs/roles/backend-java.md`（Spring Boot / Quarkus）
- [ ] `configs/roles/cli-tooling.md`（Cobra / Clap / Commander）
- [ ] `configs/roles/desktop-electron.md` / `desktop-tauri.md` / `desktop-qt.md`
- [ ] 預估：**1.5 day**

### X3. Build & package adapters (#299)
- [ ] Docker image build + push（GHCR / Docker Hub / ECR）
- [ ] Helm chart 生成（k8s 部署）
- [ ] .deb / .rpm（Linux package）
- [ ] .msi / NSIS installer（Windows）
- [ ] .dmg / .pkg（macOS）
- [ ] `cargo-dist` / `goreleaser` / `pyinstaller` / `electron-builder` 對應 skill hook
- [ ] 預估：**2 day**

### X4. License / dependency 合規 (#300)
- [ ] SPDX license scan（依語言 ecosystem：`cargo-license` / `go-licenses` / `pip-licenses` / `npm-license-checker`）
- [ ] 禁用 licenses allowlist（預設禁 GPL/AGPL，allowlist 可覆寫）
- [ ] CVE scan（`trivy` / `grype` / `osv-scanner`）
- [ ] 依賴圖 SBOM 輸出（CycloneDX / SPDX）
- [ ] 預估：**1 day**

### X5. SKILL-FASTAPI (pilot, #301)
- [ ] FastAPI service 骨架 + Alembic + Pydantic
- [ ] Dockerfile + docker-compose.yml + helm chart
- [ ] pytest + httpx + coverage
- [ ] OpenAPI spec 自動生成（整合 N3 OpenAPI governance）
- [ ] **First software skill — validates X0-X4**
- [ ] 預估：**1.5 day**

### X6. SKILL-GO-SERVICE (#302)
- [ ] Gin/Fiber 微服務骨架
- [ ] goreleaser 多平台 binary build
- [ ] 預估：**1 day**

### X7. SKILL-RUST-CLI (#303)
- [ ] Clap + anyhow + tokio 骨架
- [ ] cargo-dist 多平台 release
- [ ] 預估：**1 day**

### X8. SKILL-DESKTOP-TAURI (#304)
- [ ] Tauri 2.x 骨架 + 前端整合（React/Vue 可選）
- [ ] 三平台 build（Windows/macOS/Linux）+ auto-update
- [ ] 預估：**1.5 day**

### X9. SKILL-SPRING-BOOT（企業 Java, #305）
- [ ] Spring Boot 3 + Maven/Gradle
- [ ] Flyway migration + JUnit 5
- [ ] 預估：**1 day**

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
- [ ] `backend/bootstrap.py`：`get_bootstrap_status()` 回傳 `{admin_password_default: bool, llm_provider_configured: bool, cf_tunnel_configured: bool, smoke_passed: bool}`
- [ ] 全局 middleware：若 bootstrap 未完成 → 除 `/bootstrap/*`、`/auth/login`、`/healthz`、靜態資源外一律導向 `/bootstrap`
- [ ] `bootstrap_state` 表：`step`, `completed_at`, `actor_user_id`, `metadata`；完成全部步驟後寫 `bootstrap_finalized=true` 進 app 設定
- [ ] `POST /api/v1/bootstrap/finalize` — 全 step 綠才讓過（admin 才能呼叫）
- [ ] 前端 `app/bootstrap/page.tsx` 多步 wizard 殼
- [ ] 預估：**0.5 day**

### L2. Step 1 — 首次 admin 密碼設定
- [ ] 整合 K1 的 `must_change_password` 旗標；wizard Step 1 強制改預設 `omnisight-admin`
- [ ] 密碼強度檢查（最短 12 字 + zxcvbn ≥ 3，與 K7 統一）；若 K7 未做則先用簡版
- [ ] 寫入 audit_log（`bootstrap.admin_password_set`）；清除 `must_change_password`
- [ ] 預估：**0.5 day**

### L3. Step 2 — LLM Provider 選擇 + API Key 輸入
- [ ] UI 選單：Anthropic / OpenAI / Ollama（本機）/ Azure
- [ ] API Key 輸入 → `POST /api/v1/bootstrap/llm-provision`：驗 key（`provider.ping()`）→ 寫入 `backend/secrets.py`（at-rest 加密）→ 更新 `settings.llm_provider`
- [ ] Ollama 選項偵測本機 `localhost:11434` 可達性 + 列可用 model
- [ ] 錯誤處理：key 無效 / quota 用盡 / 網路不通 → 明確訊息
- [ ] 預估：**0.5 day**

### L4. Step 3 — Cloudflare Tunnel（複用 B12 wizard）
- [ ] 直接 embed B12 的 `cloudflare-tunnel-setup.tsx` 到 bootstrap step 3
- [ ] 完成 provision 後寫 `bootstrap_state.cf_tunnel_configured=true`
- [ ] 提供「跳過（內網部署）」選項，記 audit warning
- [ ] 預估：**0.25 day**（主要靠 B12，此處只做 embed + state 寫入）

### L5. Step 4 — 服務啟動 / 健康驗證（SSE 即時 log）
- [ ] `POST /api/v1/bootstrap/start-services`：呼叫 `systemctl start` 或 `docker compose up -d`（依部署模式）
- [ ] SSE event stream `bootstrap.service.tick`：每行 log 即時推送（tail systemd journal 或 docker logs）
- [ ] 輪詢 G1 的 `/readyz` 直到通過 or timeout 180s
- [ ] 並行檢查：backend ready / frontend ready / DB migration up-to-date / CF tunnel connector online（若 step 3 有做）
- [ ] UI 顯示 4 個勾勾即時變綠
- [ ] 預估：**1 day**

### L6. Step 5 — Smoke Test + 完成
- [ ] 跑 `scripts/prod_smoke_test.py` 子集（選 compile-flash host_native DAG，~60s）
- [ ] 顯示 audit_log hash chain 驗證結果、兩個 DAG 的 run summary
- [ ] 全綠 → `POST /api/v1/bootstrap/finalize` → 寫 `bootstrap_finalized=true` → 導向 dashboard
- [ ] 失敗 → 顯示錯誤 + 允許回到前面 step 修正
- [ ] 預估：**0.5 day**

### L7. 部署模式偵測 + docker-compose 路徑
- [ ] `detect_deploy_mode()`：偵測是否在 docker 內 / 是否有 systemd / 是否有 docker socket
- [ ] 依模式提供不同 start-services 指令：
  - `systemd` 模式：`sudo systemctl start omnisight-*`（需 K1 的 scoped sudoers）
  - `docker-compose` 模式：`docker compose -f docker-compose.prod.yml up -d`
  - `dev` 模式：跳過 start-services step（已在 uvicorn + next dev）
- [ ] 文件 `docs/ops/bootstrap_modes.md`
- [ ] 預估：**0.5 day**

### L8. 重置 + 測試
- [ ] `POST /api/v1/bootstrap/reset`（admin 限定、dev 模式限定）— 清 bootstrap_state、重設 must_change_password；用於 QA
- [ ] E2E Playwright：完整 5-step wizard 走完（mock CF API、mock LLM provider ping）
- [ ] 錯誤路徑：密碼太弱 / LLM key 無效 / systemctl 失敗各自 UX
- [ ] 預估：**0.75 day**

**總預估**：L1 (0.5) + L2 (0.5) + L3 (0.5) + L4 (0.25) + L5 (1) + L6 (0.5) + L7 (0.5) + L8 (0.75) = **~4.5 day**

**相依**：B12 (CF Tunnel) + G1 (readyz) + K1 (must_change_password)。若三者都已完成，L 可 4.5 day 內完工。若並行，可在 B12/K1 API 層出來後就開做 L1-L2。

**驗收**：在乾淨 WSL2 上 `git clone && docker compose up && 開瀏覽器` → 10 分鐘內完成所有配置、smoke test 綠、公網 HTTPS 可訪問 `/api/v1/health`，全程**零 SSH 零手動編輯 yaml**。

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

## 🅖 Priority G — Ops / Reliability（HA 補強）

> 背景：目前為單機 systemd 原型，`scripts/deploy.sh` 以 `systemctl restart` 原地重啟，會有短暫中斷；SQLite 無複製；無負載均衡 / 多副本 / 藍綠 / rolling。Canary、備份、DLQ、watchdog 已具備，但欠缺真正 HA 與零停機。以下 Phase 為補強工作。

### G1. HA-01 Graceful shutdown + readiness/liveness 拆分
- [ ] Backend 攔截 `SIGTERM`：停收新流量、flush SSE、關閉 DB、等待 in-flight task（timeout 30s）
- [ ] `/api/v1/health` 拆為 `/healthz`（liveness，永遠快速回 200 if process alive）與 `/readyz`（readiness，檢 DB + migration + 關鍵 provider chain）
- [ ] systemd unit 加 `TimeoutStopSec=40` 與 `KillSignal=SIGTERM`
- [ ] docker-compose healthcheck 改用 `/readyz`
- [ ] 單元 + 整合測試：送 SIGTERM 時 in-flight request 仍完成、新連線被拒
- [ ] 交付：`backend/lifecycle.py`、`deploy/systemd/*.service` 更新、測試

### G2. HA-02 Reverse proxy + dual backend instance rolling restart
- [ ] 新增 Caddy / nginx 前置（listen :443 → upstream backend-a:8000, backend-b:8001）
- [ ] `docker-compose.prod.yml` 擴充 `backend-a` / `backend-b` 兩副本（共用 volume）
- [ ] `scripts/deploy.sh` 改為 rolling：取下 A → 重啟 → `/readyz` pass → 取下 B → 重啟
- [ ] Upstream health check + automatic eject（fail_timeout）
- [ ] 整合測試：部署中對 `/api/v1/*` 持續打流量，0 個 5xx
- [ ] 交付：`deploy/reverse-proxy/Caddyfile`、`docker-compose.prod.yml` diff、`scripts/deploy.sh` rolling 模式

### G3. HA-03 Blue-Green 部署策略
- [ ] `scripts/deploy.sh` 新增 `--strategy blue-green` 旗標
- [ ] 維護 active/standby symlink 或 proxy upstream 切換（atomic）
- [ ] Pre-cut smoke（`scripts/prod_smoke_test.py` on standby）→ 切流 → 觀察 5 分鐘 → 保留舊版 24h 供 rollback
- [ ] Rollback 腳本：`deploy.sh --rollback`（秒級切回 previous color）
- [ ] 交付：runbook `docs/ops/blue_green_runbook.md`、腳本

### G4. HA-04 SQLite → PostgreSQL 遷移 + streaming replica
- [ ] Alembic 驗證所有 migration 在 Postgres 上綠（sqlite-isms 掃描：`AUTOINCREMENT`、`WITHOUT ROWID`、dynamic type）
- [ ] Connection 抽象：`DATABASE_URL` 支援 `postgresql+asyncpg://`
- [ ] 部署 primary + hot standby（`streaming replication`、`synchronous_commit=on` 可設）
- [ ] 資料搬移腳本 `scripts/migrate_sqlite_to_pg.py`（含 audit_log hash chain 連續性驗證）
- [ ] CI 新增 Postgres service matrix（sqlite + pg 兩軌）
- [ ] 交付：`docs/ops/db_failover.md`、遷移腳本、CI 更新

### G5. HA-05 Multi-node orchestration（K8s manifests 或 Nomad job）
- [ ] 選型決策文件（K8s vs Nomad vs docker swarm — 比較運維負擔）
- [ ] Manifests：Deployment（replicas=2, maxUnavailable=0）、Service、Ingress、HPA（CPU 70%）
- [ ] PDB（PodDisruptionBudget minAvailable=1）
- [ ] readiness/liveness probe 對接 G1 endpoint
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
- [ ] 預估：**0.5 day**

### H2. Coordinator 負載感知調度（precondition + backoff）
- [ ] `_ModeSlot.acquire()` 新增 precondition：`cpu_pct < 85 AND mem_pct < 85 AND container_count < K`
- [ ] 超標時指數 backoff（cap 30s），不佔槽位；emit `sandbox.deferred` audit 事件（reason: `host_cpu_high` / `host_mem_high` / `container_cap`）
- [ ] Turbo 自動降級：`cpu_pct > 80` 持續 30s → 降到 supervised budget；恢復後可自動回升（需冷卻 2 min）
- [ ] `auto_derate=true` 設定開關（`backend/config.py`），使用者可關閉（turbo 模式需手動 confirm）
- [ ] Prewarm（`sandbox_prewarm.py`）在 high pressure 時暫停新建 warm pool；已 warm 的保留
- [ ] Audit 記錄所有 derate / recover 決策（Phase 53 hash-chain）
- [ ] 測試：mock host_metrics 模擬高壓 → 驗證 acquire 被阻塞、derate 觸發、recover 冷卻
- [ ] 預估：**2 day**

### H3. UI Host Load Panel + Coordinator 決策透明化
- [ ] 把 `components/omnisight/host-device-panel.tsx` placeholder 換成真 SSE 驅動（listen `host.metrics.tick`）
- [ ] 顯示：CPU% / mem%（含 available）/ disk% / loadavg 1m / running container 數 + 各項 60-pt sparkline
- [ ] Baseline 顯示「16c / 64GB / 512GB」於 header（hardcode）
- [ ] `ops-summary-panel.tsx` 加欄位：**queue depth**（等槽位任務數）/ **deferred count**（近 5min）/ **effective concurrency budget**（因 derate 可能 < 設定）
- [ ] 過載 Badge：`Coordinator auto-derated to supervised`，hover tooltip 顯示原因（"CPU 87% > threshold"）
- [ ] 手動 override 按鈕：`Force turbo`（confirm dialog 警告可能 OOM，audit 記錄）
- [ ] 高壓閾值視覺標記（CPU >85% 變紅、70-85% 變黃）
- [ ] Component + Playwright E2E 測試
- [ ] 預估：**1.5 day**

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
- [ ] 預估：**1.5 day**

### H4b. Sandbox cost calibration（H1 上線 1 週後）
- [ ] `scripts/calibrate_sandbox_cost.py`：讀取過去 N 天 sandbox 執行紀錄（start/end timestamp + 同期 host_metrics ring）
- [ ] 計算每類 sandbox 的平均 CPU×time / Δmem_peak → 產新權重表
- [ ] 輸出 diff report（舊權重 vs 新權重）供人工審核
- [ ] 支援 `--apply` 旗標寫回 `configs/sandbox_cost_weights.yaml`（改 H4a hardcode 為 config 驅動）
- [ ] Audit：權重變更寫入 hash-chain
- [ ] 預估：**1 day**

**相依性**：H1 → H2 → H3（metrics → 調度 → UI）；H4a 可與 H3 並行；H4b 需 H1 資料累積 1 週。

**總預估**：H1 (0.5d) + H2 (2d) + H3 (1.5d) + H4a (1.5d) + H4b (1d) = **6.5 day**

**驗收**：
- turbo mode 在 CPU>85% 時 30s 內自動降級，UI Badge 顯示原因
- 同時跑 8 個 Phase 64-C-LOCAL compile 不會 OOM（AIMD 會先擋）
- 新使用者看 host-device-panel 可一眼知道系統壓力與 queue 狀況
- WSL2 host-load 輔助訊號（loadavg_1m/16）能反映 Windows host 其他進程壓力

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

### Phase 13 — Bootstrap Wizard 一鍵安裝（~1 week，需 B12 + G1 + K1 基礎）
L1 (status 偵測 + /bootstrap 路由) → L2 (admin 密碼) → L3 (LLM provider) → L4 (CF Tunnel embed)
→ L5 (服務啟動 + SSE log) → L6 (smoke test + finalize) → L7 (部署模式偵測) → L8 (reset + E2E)

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
| META | 4-8 day |
| **Total** | **~463.5-634.5 day** |

3-person team parallelized: **~7-10 months wall-clock**.
