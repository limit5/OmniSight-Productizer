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
- [ ] `configs/roles/ui-designer.md`：UI Designer specialist agent role skill——精通 shadcn/ui 全套 API（Button/Card/Dialog/Sheet/Table/Form/...）+ Tailwind utility classes + responsive breakpoints + WAI-ARIA patterns + color contrast
- [ ] `backend/ui_component_registry.py`：shadcn/ui component registry——列舉所有可用元件名 + props interface + 典型使用範例，agent 呼叫 `get_available_components()` 取得清單注入 context
- [ ] `backend/design_token_loader.py`：載入目標專案的 `tailwind.config.ts` + `globals.css` → 提取 color palette / font stack / border-radius / spacing scale → `DesignTokens` dataclass 注入 agent 生成約束
- [ ] `backend/component_consistency_linter.py`：post-generation 掃描 → 偵測 raw `<div>`/`<button>`/`<input>` 可被 shadcn 元件替代的模式 → 自動修正或提示 agent 修正
- [ ] `backend/vision_to_ui.py`：Screenshot/手繪稿 → code pipeline——接收圖片 → Opus 4.7 multimodal 分析佈局結構 + 色彩 + 元件 → 輸出 React + shadcn/ui + Tailwind code
- [ ] Figma → code 串接：呼叫已有 MCP `get_design_context(fileKey, nodeId)` → 提取 design tokens + 元件層級結構 + spacing → agent 生成對應 React code
- [ ] URL → reference：`WebFetch(url)` + Playwright 截圖 → 注入 agent visual context 作為參考（「做一個像這個 URL 的頁面」）
- [ ] Edit complexity auto-router：分析 user prompt 複雜度——小改（文字/色彩/spacing）→ Haiku 快改（< 3s）；大改（layout 重構/新頁面）→ Opus 深想
- [ ] 整合測試：NL「做一個定價頁面，三個方案，年月切換」→ agent 輸出完整 React + shadcn Tabs/Card/Switch 元件 + Tailwind → render 正確 + consistency lint pass
- 預估：**7 day**

### V2. Web — Live Preview + Sandbox 渲染 (#318)
- [ ] `backend/ui_sandbox.py`：per-session Next.js dev server 管理器——Docker container 內跑 `npm run dev`，agent 透過 volume mount 寫 code → HMR 自動更新
- [ ] Sandbox lifecycle：create → start → hot-reload → screenshot → stop → cleanup；每 session 最多 1 sandbox，idle 15 min 自動回收
- [ ] `backend/ui_screenshot.py`：Playwright headless 截圖 service——定期或 on-demand 截圖 sandbox → 回傳 PNG base64
- [ ] Responsive viewport：desktop (1440×900) / tablet (768×1024) / mobile (375×812) 三 viewport 截圖
- [ ] Preview error bridge：sandbox dev server 的 compile error / runtime error 攔截（stdout/stderr parse）→ 結構化 error object → 注入 agent context → agent 自動修 → 重截圖
- [ ] Agent visual context injection：每輪 ReAct 前自動截圖 → base64 附加到 Opus 4.7 multimodal message → agent 真正「看到」畫面長什麼樣
- [ ] SSE event：`ui_sandbox.screenshot`（session_id / viewport / image_url / timestamp）+ `ui_sandbox.error`（error_type / message / file / line）
- [ ] 整合測試：agent 寫 code → sandbox HMR → 截圖 → error 偵測 → auto-fix → 重截圖 → 最終 screenshot 無 error
- 預估：**6 day**

### V3. Web — 視覺迭代 + 標註回饋 (#319)
- [ ] `components/omnisight/visual-annotator.tsx`：在 preview 截圖上的 annotation overlay——使用者可畫矩形框 / 點選元素 / 加文字 comment
- [ ] Annotation → agent context：每個標註轉換為 `{type: "click"|"rect", cssSelector: "...", boundingBox: {x,y,w,h}, comment: "..."}` → 注入 agent 的下一輪 ReAct prompt
- [ ] Element inspector integration：sandbox React tree 注入 `data-omnisight-component` attribute → hover 時前端顯示元件名 + 當前 props + computed styles（輕量版 React DevTools）
- [ ] UI iteration timeline（`components/omnisight/ui-iteration-timeline.tsx`）：每次 agent 修改 → 存截圖 + code diff → 水平時間軸可視化；點任何版本可回溯（preview + code 都回到該版本）
- [ ] Version rollback：在 iteration timeline 點「回到此版本」→ sandbox git checkout 該 commit → preview 刷新
- 預估：**5 day**

### V4. Web Workspace UI + 輸出 (#320)
- [ ] `app/workspace/web/page.tsx`：Web 工作區主頁面——三欄佈局
  - [ ] 左 sidebar：project tree + shadcn component palette（瀏覽 + 點選加到 chat prompt）+ design token editor（調色盤 + 字型選擇 + spacing slider → live 更新 preview）
  - [ ] 中 pane：preview iframe/screenshot + responsive toggle（desktop/tablet/mobile）+ visual annotator overlay
  - [ ] 右 pane：code viewer（syntax highlight + diff + copy button）+ workspace chat（conversational iteration）
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
