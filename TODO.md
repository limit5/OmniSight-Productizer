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
- [ ] G-code interpreter (subset: G0/G1/G28/M104/M109/M140)
- [ ] Stepper driver abstraction (TMC2209 / A4988 / DRV8825)
- [ ] Heater + PID loop (hotend + bed)
- [ ] Endstop handling + homing
- [ ] Thermal runaway safety shutoff
- [ ] Unit test: G-code sequence → expected motion trace

---

## 🅓 Priority D — L4 Layer B (per-product skill packs)

Each pack must deliver 5 artifacts (DAG tasks / code scaffolds / integration
tests / HIL recipes / doc templates) per framework contract.

### D1. SKILL-UVC (pilot, #218)
- [ ] UVC 1.5 descriptor scaffold (H.264 + still image + extension unit)
- [ ] gadget-fs/functionfs binding
- [ ] UVCH264 payload generator
- [ ] USB-CV compliance test recipe
- [ ] Datasheet + user manual templates
- [ ] **First skill done — validates CORE-05 framework**

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
C18..C25 as demanded by prioritized skill packs

### Phase 4 — Skill pack parallel sprint (6-10 weeks, 3-person team)
D2..D28 parallelized, prioritized by demand:
- Team α: imaging family (D2 IPCam, D5 doorbell, D6 dashcam, D19/20/21 scanner/printer/MFP)
- Team β: audio + display family (D3, D4, D10, D11, D13, D14)
- Team γ: industrial + safety-critical (D15 medical, D16 drone, D17 industrial-PC, D22 barcode, D23-D25 payment family, D26-D28 3D/MV)
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

---

## Totals

| Layer | Range |
|---|---|
| A (infrastructure) | 105-149 day |
| B (skill packs) | 160-225 day |
| C (software tracks) | 129-187 day |
| META | 4-8 day |
| **Total** | **~398-569 day** |

3-person team parallelized: **~7-10 months wall-clock**.
