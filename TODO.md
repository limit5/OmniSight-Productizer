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
- [ ] Define dataclass `HardwareProfile` with fields: SoC, MCU, DSP, NPU, sensor, codec, USB, display, memory_map, peripherals
- [ ] JSON schema + pydantic validation
- [ ] Migration: extend ParsedSpec optionally embedding HardwareProfile
- [ ] Unit test: round-trip serialize/deserialize

### C3. L4-CORE-02 Datasheet PDF → HardwareProfile parser (#212)
- [ ] PDF text extraction (reuse Phase 67-E RAG)
- [ ] Structured extraction prompt per HardwareProfile field
- [ ] Confidence per field (≥0.7 auto-accept, else clarify)
- [ ] Fallback: operator form-fills missing fields
- [ ] Unit test: sample datasheets (Hi3516 / RK3566 / ESP32-S3)

### C4. L4-CORE-03 Embedded product planner agent (#213)
- [ ] Input: HardwareProfile + ProductSpec + selected skill_pack
- [ ] Output: full DAG (BSP → kernel → drivers → protocol layer → UI → OTA → tests)
- [ ] Use skill pack's `tasks.yaml` as template source
- [ ] Handle dependency resolution between tasks
- [ ] Unit test: fixture spec → expected DAG task count / topology

### C5. L4-CORE-05 Skill pack framework (#214)
- [ ] Define skill manifest schema (`skill.yaml`)
- [ ] Registry: `configs/skills/<name>/` convention
- [ ] Lifecycle hooks: install / validate / enumerate
- [ ] CLI: `omnisight skill list / install / validate`
- [ ] Contract test: every skill must provide 5 artifacts (tasks/scaffolds/tests/hil/docs)

### C6. L4-CORE-06 Document suite generator (#215)
- [ ] Extend REPORT-01 with per-product-class templates
- [ ] Templates: datasheet.md.j2 / user_manual.md.j2 / compliance.md.j2 / api_doc.md.j2 / sbom.json.j2 / eula.md.j2 / security.md.j2
- [ ] Merge compliance-cert fields from relevant L4-CORE-09/10/18
- [ ] PDF export via weasyprint
- [ ] Unit test per product class

### C7. L4-CORE-07 HIL plugin API (#216)
- [ ] Define plugin protocol: `measure()` / `verify()` / `teardown()`
- [ ] Camera family plugin (focus/WB/stream-latency)
- [ ] Audio family plugin (SNR/AEC metrics)
- [ ] Display family plugin (uniformity/touch latency)
- [ ] Registry: skill pack declares required HIL plugins
- [ ] Integration test: mock HIL plugin lifecycle

### C8. L4-CORE-08 Protocol compliance harness (#217)
- [ ] Wrapper for ODTT (ONVIF Device Test Tool) — headless mode or subprocess
- [ ] Wrapper for USB-IF USBCV
- [ ] Wrapper for UAC test suite
- [ ] Normalized report schema (pass/fail per test case + evidence)
- [ ] Output → audit_log
- [ ] Smoke test per wrapper

### C9. L4-CORE-09 Safety & compliance framework (#223)
- [ ] Rule library: ISO 26262 ASIL A-D / IEC 60601 SW-A/B/C / DO-178 DAL A-E / IEC 61508 SIL 1-4
- [ ] Each rule is a DAG validator + required artifact list
- [ ] Artifacts: hazard analysis, risk file, software classification, traceability matrix
- [ ] CLI: `omnisight compliance check --standard iso26262 --asil B`
- [ ] Unit test: gate rejects DAG missing required artifact

### C10. L4-CORE-10 Radio certification pre-compliance (#224)
- [ ] Test recipe library: FCC Part 15 / CE RED / NCC LPD / SRRC SRD
- [ ] Conducted + radiated emissions stub runners
- [ ] SAR test hook (operator-uploads SAR result file)
- [ ] Per-region cert artifact generator
- [ ] Unit test: sample radio spec → correct cert checklist

### C11. L4-CORE-11 Power / battery profiling (#225)
- [ ] Sleep-state transition detector (entry/exit event trace)
- [ ] Current profiling sampler (external shunt ADC integration)
- [ ] Battery lifetime model (capacity × avg draw × duty cycle)
- [ ] Dashboard: mAh/day per feature toggle
- [ ] Unit test: synthetic current trace → correct lifetime estimate

### C12. L4-CORE-12 Real-time / determinism track (#226)
- [ ] RT-linux build profile (`PREEMPT_RT` kernel config)
- [ ] RTOS build profile (FreeRTOS / Zephyr)
- [ ] `cyclictest` harness + percentile latency report
- [ ] Scheduler trace capture (`trace-cmd` / `bpftrace`)
- [ ] Threshold gate: fails build if P99 > declared budget

### C13. L4-CORE-13 Connectivity sub-skill library (#227)
- [ ] BLE sub-skill (GATT + pairing + OTA profile)
- [ ] WiFi sub-skill (STA/AP + provisioning + enterprise auth)
- [ ] 5G sub-skill (modem AT / QMI + dual-SIM)
- [ ] Ethernet sub-skill (basic + VLAN + PoE detection)
- [ ] CAN sub-skill (SocketCAN + diagnostics)
- [ ] Modbus / OPC-UA sub-skills (industrial)
- [ ] Registry + composition: skill packs opt-in per sub-skill

### C14. L4-CORE-14 Sensor fusion library (#228)
- [ ] IMU drivers (MPU6050 / LSM6DS3 / BMI270)
- [ ] GPS NMEA parser + UBX protocol
- [ ] Barometer driver (BMP280 / LPS22)
- [ ] EKF implementation (9-DoF orientation)
- [ ] Calibration routines (bias/scale/alignment)
- [ ] Unit test against known trajectory fixture

### C15. L4-CORE-15 Security stack (#229)
- [ ] Secure boot chain: bootloader → kernel → rootfs signature verify
- [ ] TEE binding (OP-TEE / TrustZone abstraction)
- [ ] Remote attestation: TPM / SE / fTPM
- [ ] SBOM signing with sigstore/cosign
- [ ] Key management SOP (`docs/operations/key-management.md`)
- [ ] Threat model per product class

### C16. L4-CORE-16 OTA framework (#230)
- [ ] A/B slot partition scheme
- [ ] Delta update (bsdiff / zchunk / RAUC)
- [ ] Rollback trigger on boot-fail (watchdog + count)
- [ ] Signature verification (ed25519 + cert chain)
- [ ] Server side: update manifest + phased rollout
- [ ] Integration test: flash → reboot → rollback path

### C17. L4-CORE-17 Telemetry backend (#231)
- [ ] Client SDK: crash dump + usage event + perf metric
- [ ] Ingestion endpoint (batched POST + retry queue)
- [ ] Storage: partitioned table with retention policy
- [ ] Privacy: PII redaction + opt-in flag
- [ ] Dashboard: fleet health + crash rate + adoption
- [ ] Unit test: SDK offline queue flushes on reconnect

### C18. L4-CORE-18 Payment / PCI compliance framework (#239)
- [ ] PCI-DSS control mapping (req 1-12 → product artifacts)
- [ ] PCI-PTS physical security rule set
- [ ] EMV L1 (hardware) / L2 (kernel) / L3 (acceptance) test stubs
- [ ] P2PE (point-to-point encryption) key injection flow
- [ ] HSM integration abstraction (Thales / Utimaco / SafeNet)
- [ ] Cert artifact generator

### C19. L4-CORE-19 Imaging / document pipeline (#240)
- [ ] Scanner ISP path (CIS/CCD → 8/16-bit grey/RGB)
- [ ] OCR integration (Tesseract / PaddleOCR / vendor SDK)
- [ ] TWAIN driver template (Windows)
- [ ] SANE backend template (Linux)
- [ ] ICC color profile embedding

### C20. L4-CORE-20 Print pipeline (#241)
- [ ] IPP/CUPS backend wrapper
- [ ] PDL interpreters: PCL / PostScript / PDF (via Ghostscript)
- [ ] Color management: ICC profile per paper/ink combo
- [ ] Print queue + spooler integration
- [ ] Unit test: round-trip PDF → raster → PDL → output

### C21. L4-CORE-21 Enterprise web stack pattern (#242) — **highest leverage for Layer C**
- [ ] Auth: Next-Auth + optional SSO plug (LDAP/SAML/OIDC)
- [ ] RBAC: role/permission schema + policy middleware
- [ ] Audit: every write → audit_log (reuse Phase 53 hash chain)
- [ ] Reports: tabular + chart via Tremor / shadcn
- [ ] i18n: next-intl scaffold with zh/en bundles
- [ ] Multi-tenant: tenant_id column + row-level security
- [ ] Import/export: CSV/XLSX/JSON round-trip
- [ ] Workflow engine: state machine + approval chain
- [ ] Reference implementation (acts as template for SW-WEB-*)

### C22. L4-CORE-22 Barcode/scanning SDK abstraction (#243)
- [ ] Unified `BarcodeScanner` interface
- [ ] Vendor adapters: Zebra SNAPI / Honeywell SDK / Datalogic SDK / Newland SDK
- [ ] Symbology support: UPC/EAN/Code128/QR/DataMatrix/PDF417/Aztec
- [ ] Decode modes: HID wedge / SPP / API
- [ ] Unit test with pre-captured frame samples

### C23. L4-CORE-23 Depth / 3D sensing pipeline (#253)
- [ ] ToF sensor driver abstraction (Sony IMX556 / Melexis MLX75027)
- [ ] Structured light capture + decoder
- [ ] Stereo rectification + disparity (OpenCV SGBM)
- [ ] Point cloud: PCL + Open3D wrappers
- [ ] ICP registration + SLAM hooks
- [ ] Unit test: known scene → expected point count + bounds

### C24. L4-CORE-24 Machine vision & industrial imaging framework (#254)
- [ ] GenICam driver abstraction
- [ ] GigE Vision transport (aravis or mvIMPACT)
- [ ] USB3 Vision transport
- [ ] Hardware trigger + encoder sync
- [ ] Multi-camera calibration (checkerboard + bundle adjustment)
- [ ] Line-scan support
- [ ] PLC integration (Modbus/OPC-UA via CORE-13)

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
