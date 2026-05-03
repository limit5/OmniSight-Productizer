# OmniSight-Productizer 深度審計報告 — 2026-05-03

> 審計於 SC 系列 + KS 系列大量 commit 後進行；目的是評估系統當前 production-readiness、找出阻塞 GA 的問題清單。

## 審計範圍

8 個維度全部覆蓋：

1. 不完整實作 / 規格 vs 實作落差（stub / NotImplementedError / 半實作）
2. 測試覆蓋缺口 + 弱 assertion / mock 蓋過 test
3. 跨 worker mutable state（違反 SOP cross-worker safety 規則）
4. Security / safety / 操作風險（shell injection / SQL inj / 邏輯洩漏）
5. Frontend 互動 / a11y / i18n / XSS
6. Infra / CI / docker / deploy 安全與可用性
7. API contract / pydantic schema / OpenAPI drift
8. 跨 module 契約 + dependency 衛生 + supply chain

執行方法：4 個第一輪 Explore agent + 5 個第二輪 Explore agent 並行 + bash baseline scan。

## Baseline 自動掃描結果

```
Production code:    305,163 行
Test code:          360,774 行
TODO/FIXME/XXX/HACK: 1735 個 marker（production code）
NotImplementedError: 1167 個（production code）
Skipped/xfail tests: 154 個
9 個 production 檔 > 2000 行（最大 routers/tenant_projects.py 3902）
58 個 alembic migration（3 個沒 downgrade）
0 自動化 quality 工具（pip-audit / ruff / mypy 都沒裝在 backend/.venv）
```

## 嚴重度總計

| 嚴重度 | 數量 | 修復估計 |
|---|---|---|
| 🚨 BLOCKER | 22 | 5-8 day |
| ⚠️ DEFECT | 35+ | 10-15 day |
| 🟡 DEBT | 70+ | 30-50 day |
| 🔵 COSMETIC | 50+ | 5-10 day |
| **Total findings** | **~150+** | **~50-80 day** |

---

## 健康分數

| 維度 | 分數 | 最差項 |
|---|---|---|
| Backend correctness | 6/10 | shell=True on LLM payload (B4) |
| Test coverage（quantity） | 7/10 | 360k 行 test，量足 |
| Test coverage（quality） | 5/10 | 13 個 critical 模組 0 test |
| Cross-worker safety | 4/10 | 3 BLOCKER race + 7 DEFECT lazy cache |
| API contract enforcement | 5/10 | 50+ endpoint untyped + extra="allow" |
| Frontend a11y / i18n | 3/10 | 0 i18n + 廣泛 a11y 缺 |
| Infra / deploy 安全 | 6/10 | staging socket 暴露 + HSTS 缺 |
| Dependency 衛生 | 5/10 | anthropic 5+ 版落後 + 11 floating |
| Cross-module 一致性 | 5/10 | 25 dead import + 1768 local import |
| Documentation drift | 6/10 | HANDOFF "deployed-active" 誇大 |
| **整體 weighted** | **5.2/10** | **介於 dev-OK 跟 production-ready 之間** |

---

# 🚨 BLOCKER (22 條)

## B1-B3: Cross-worker in-memory state

```
backend/print_pipeline.py:527    _ipp_jobs: dict = {}              ← Job ID 跨 worker 撞號
backend/print_pipeline.py:1077   _queue_jobs: dict = {}            ← print queue worker 不一致
backend/telemetry_backend.py:571 _TELEMETRY_CERTS: list = []       ← cert registry 各 worker 不同 subset
```

**修法**：搬到 SharedKV (Redis) / SQLite WAL / DB row。違反 SOP step 1 acceptable answer #1（"不共享因每 worker 從同一來源推導" 不成立——這些 list/dict 是 mutable accumulator）。

## B4-B7: Shell injection 潛在 RCE

```
backend/agents/runner_handlers.py:133  subprocess.run(payload["command"], shell=True)
                                       ← LLM-controlled payload 直接 RCE
backend/skill_registry.py:243          shell=True on manifest.hooks.validate_cmd
backend/skill_registry.py:320          shell=True on manifest.hooks.install
backend/skill_registry.py:366          shell=True on manifest.hooks.enumerate_cmd
```

**修法**：runner_handlers 改 `shlex.split` + `shell=False`；skill_registry 至少 `shlex.quote` 或 allowlist。

## B8-B10: SQL inj / DDL string interp

```
backend/enterprise_web_stack.py:1393                 apply_rls() f-string interp tenant_id
backend/db.py:238                                    ALTER TABLE f-string（hardcoded tuple 但 anti-pattern）
backend/alembic/versions/0106_ks_envelope_tables.py:327  op.execute(f"DROP TABLE...")
```

**修法**：parameterized query；DDL 用 sqlalchemy operations 不用字串 interp。

## B11: Frontend XSS

```
components/omnisight/project-report-panel.tsx:44-66
  markdownToHtml() → dangerouslySetInnerHTML 沒 sanitizer (DOMPurify) ← XSS string vector
```

**修法**：引入 DOMPurify 或 marked 內建 sanitize。

## B12-B14: Alembic migration 沒 downgrade

```
backend/alembic/versions/0007_session_audit_enhancements.py    downgrade = pass
backend/alembic/versions/0008_account_lockout.py               downgrade = pass
backend/alembic/versions/0009_workflow_run_version.py          downgrade = pass
```

**修法**：實作 DROP/REVERT 對應每個 upgrade op；prod rollback 必經此路徑。

## B15-B17: Infra 安全 / 可用性

```
docker-compose.staging.yml:28,60   /var/run/docker.sock 直接掛（RCE → 全主機）
                                   prod 已用 docker-socket-proxy；staging 還露 raw socket
docker-compose.staging.yml:90      NODE_OPTIONS=--max-old-space-size=4096 + 沒 mem_limit
                                   DoS 一次燒 4GB 才 OOM-kill
deploy/reverse-proxy/Caddyfile:220 Missing HSTS header on main HTTPS listener
                                   即便 OMNISIGHT_PUBLIC_HOSTNAME 設 prod FQDN，operator 拿不到 HSTS 保護
```

## B18-B20: API contract / __init__.py

```
backend/__init__.py:10        BuildArtifact from backend.deploy import 但符號不存在 → import error
backend/__init__.py:22        WebVital from backend.observability import 但符號不存在
backend/models.py:458         SystemInfoResponse model_config = {"extra": "allow"}    ← schema 失效
backend/routers/auth.py:126   LoginRequest extra="allow"                              ← security boundary
```

## B21: Privacy regression（已 xfail 隱藏）

```
backend/tests/test_sse_scope_regression.py:351  test_user_scope_does_not_leak_across_users
  → SSE frame 跨 user 洩漏，user privacy 破，但 xfail 隱藏中
```

## B22: Supply chain skew

```
anthropic==0.95.0 (backend)         ← 5+ major 版本落後
ai@6.0.154 (frontend, anthropic 3.0+)  ← backend 不支援新 prompt features
```

---

# ⚠️ DEFECT (35+ 條)

## D1-D7: Lazy cache 沒 cross-worker sync

```
backend/bootstrap.py:662                    _gate_cache
backend/forecast.py:155                     _PRICING_CACHE
backend/sensor_fusion.py:497                _SF_CACHE
backend/telemetry_backend.py:536            _TELEMETRY_CACHE
backend/codeowners.py:39                    _rules
backend/prompt_loader.py:205                _task_skills_cache
backend/agents/llm.py:1221                  _OLLAMA_TOOL_COMPAT_CACHE
```

**修法**：加 mtime check + 失效；或走 SharedKV 同 Z 系列模式。

## D8-D12: Stub 混在 production endpoint

```
backend/orchestrator_gateway.py:234       _gerrit_status_stub() 被 GET /status 用
backend/enterprise_web_stack.py:988       _export_xlsx_stub() 回 tab-separated，Content-Type 標 XLSX
backend/enterprise_web_stack.py:1517      _preview_xlsx_stub() 同模式
backend/agents/sub_agent.py               run_in_background 假 raise，feature 假裝有
backend/agents/runner_handlers.py         bash_handler run_in_background 同
```

## D13-D17: NotImplementedError 沒 ABC 強制

```
backend/build_adapters.py:448                    BuildAdapter._build()
backend/app_store_connect.py:Transport.request   raise but no @abstractmethod
backend/hmi_components.py                        HALComponent.render_html/js/hal_endpoints
backend/web/framework_adapter.py:602             _AdapterBase._render_files() with pragma:no-cover
backend/queue_backend.py                         _UnimplementedAdapter __init__ 直接 raise
```

## D18-D24: Pydantic / API contract drift

```
backend/routers/sensor_fusion.py:73-325                25 endpoints 回 dict[str, Any] 沒 response_model
backend/routers/workflow.py:45,62,73,82,98,114,133     7 stateful endpoints 同
backend/routers/payment.py / motion_control.py         12+ endpoints 同
backend/routers/artifacts.py:97                        DELETE 204 但實際 return None implicit
backend/routers/auth.py:156                            login() bare dict
backend/models.py:90-91                                Agent class Config (V1) vs ConfigDict (V2) 混用
backend/models.py:701                                  ProviderInfo.env_var: Optional[str] = "" 邊界曖昧
```

## D25-D29: Operational / deploy hazards

```
docker-compose.prod.yml:699-700   Grafana 預設 admin:admin，env var 未設 startup 不 fail
docker-compose.test.yml:45        測試 DB 密碼 plaintext 在 docker logs 裡可見
scripts/backup_prod_db.sh:182     DLP scan gate 不驗 script 是否存在
scripts/deploy-prod.sh:63         git fetch 接受任意 ref，沒 GPG signature 驗
docker-compose.prod.yml:493       cloudflared digest 沒自動 re-pin 機制
```

## D30-D32: Frontend 互動失誤

```
components/omnisight/cloudflare-tunnel-setup.tsx:272         Modal 沒 keyboard Escape + focus trap
components/omnisight/api-key-management-panel.tsx:160-174    <input> 沒 label/aria-label
components/omnisight/integration-settings.tsx:1529-1535      setInterval 沒 cleanup 驗證
```

## D33-D35: Imports / circular workaround

```
backend/vision_to_ui.py:755    invoke_chat 在函數內 import (circular workaround)
backend/llm_adapter.py:103-114 build_chat_model max_tokens=None 被 hardcode 4096 silent
backend/llm_adapter.py:325-344 invoke_chat 接 bind_tools 但內部丟掉 contract 不一致
```

---

# 🟡 DEBT (70+ 條)

## DT1-DT13: 完全沒測試的 production module

```
backend/account_linking.py            ← OAuth flow state machine
backend/agents/_shell_safe.py         ← 安全敏感（防注入）
backend/agents/batch_dispatcher.py
backend/agents/batch_eligibility.py
backend/agents/cost_guard.py          ← 成本控管
backend/agents/external_tool_registry.py
backend/agents/hd_pcb_si.py
backend/agents/mcp_integration.py
backend/agents/postgres_stores.py
backend/agents/rate_limiter.py        ← 限流
backend/agents/tool_dispatcher.py     ← agent runtime 核心
backend/agents/tools_patch.py
backend/agents/tool_schemas.py
```

## DT14-DT18: 大檔（refactor 候選）

```
backend/routers/tenant_projects.py    3902
backend/db.py                         3548
backend/routers/bootstrap.py          3351
backend/depth_sensing.py              3215
backend/routers/invoke.py             2923
backend/routers/system.py             2541
backend/agents/tools.py               2443
backend/onvif_device.py               2389
backend/auth.py                       2193
```

## DT19-DT24: Mock 蓋過真實 test

```
backend/tests/test_ssh_runner.py:334               13 mocks，沒打真 SSHClient
backend/tests/test_require_super_admin.py:294      11 mocks
backend/tests/test_honeypot.py:723/733/750         整 settings module mock
backend/tests/test_oauth_refresh_hook.py:609       5+ mocks event loop
backend/tests/test_anthropic_mode_manager.py:51,71  validate() 沒 assert
backend/tests/test_api_keys_legacy_migration.py:117 migration 無 assert
```

## DT25-DT35: Frontend i18n + a11y 全面欠缺

```
0 i18n machinery (no next-intl / react-i18next)        40+ component 硬編 user-facing 字串
20 untested components (no .test.tsx)                  workspace-chat / user-menu / api-key-mgmt 等
icon-only buttons 系統性用 title="" 而非 aria-label   integration-settings.tsx 等多處
modal overlays 沒鍵盤可達                              user-menu / notification-center 等
```

## DT36-DT45: Imports / re-export 衛生

```
backend/__init__.py:15-25         25+ dead imports from non-existent submodules
1768x local imports (function 內) 廣泛 circular avoidance
10x TYPE_CHECKING blocks         指示 module 結構脆弱
backend/sandbox_prewarm.py        15+ "from backend.X import _Y" 函數內 import
backend/security/llm_firewall.py  audit 在 function 內 import
backend/decryption_audit.py       audit 在 module 層 import — 不一致
```

## DT46-DT50: SOC2 / compliance HANDOFF 誇大

```
HANDOFF.md  10+ row 寫 deployed-active 但 gate 條件描述含「待 …」未滿足
TODO.md     Phase 1/2 從 [D] 改 [x] 含「audit correction」inline comment
            audit trail 模糊
```

## DT51-DT60: Dependency 衛生

```
backend/requirements.in    redis>=5.0.0 跟 redis[hiredis]>=5.0.0 重複宣告
backend/requirements.in    11 個 floating version range (>=)，違 AGENTS Rule 11 重現性
backend/requirements.txt   pytest/respx/httpx 等 test deps 進 production lockfile
backend/requirements.txt   paramiko==4.0.0 是 ghost (.in 沒寫，.txt 留著)
psycopg2-binary==2.9.10    stale (2.9.15 stable, 2024-01 release)
pnpm-lock.yaml             stale (4-27 lock vs 今日 package.json 變動)
無 pip-audit / ruff / mypy 在 venv，沒 automated quality 工具
無 license audit (top 30 deps 沒查 GPL/AGPL 相容性)
```

## DT61-DT70: Migration / DB 衛生

```
58 alembic migrations 沒強制 enforcement 必 downgrade
0057_oauth_tokens 被 codex 加 idx 但有耦合 TODO.md 的 comment 警告
跨 module type alias 漂移風險 (UserId 等可能多處定義)
__init__.py 25+ dead re-export
__all__ exports 跟實際 def 沒驗證一致
```

---

# 🔵 COSMETIC (50+ 條)

```
backend/imaging_pipeline.py:978-1204  C++ 風 TODO comment 嵌在 Python 內
backend/ipcam_rtsp_server.py:631      MD5 (RTSP digest auth, spec-defined)
backend/onvif_device.py:699           SHA1 (XML signature digest)
HANDOFF.md                            "Production status / Next gate" prose-only，沒 machine-readable
1735 個 TODO/FIXME marker 散在 backend/，無中央 issue tracker
1167 個 NotImplementedError 在 production code（多數合理但無 audit）
335 個 bare pass 在 production code
1.5:1 frontend test:component ratio (130 components / 199 tests，但分布不均)
```

---

# 修復路線

```
Wave 1 — Production blockers (5-8 day)
  B1-B22  全 22 個 BLOCKER

Wave 2 — Defects (10-15 day)
  D1-D35  全 35+ 個 DEFECT

Wave 3 — Debt (30-50 day)
  DT1-DT70 全 70+ 個 DEBT

Wave 4 — Cosmetic (5-10 day)
  剩餘 marker / hash / dead comment / etc
```

---

# 沒涵蓋的維度

- Memory leak / performance regression（需要 runtime profiling）
- 前端 visual regression（需要 chromatic / loki 等工具）
- Real CVE scan（需要 pip-audit 安裝）
- API spec 完整 OpenAPI export 比對
- LLM cost / latency baseline

這些需要工具引入或實際 runtime 才能補。

---

# 後續

對應 TODO.md 加 `Priority FX — Audit Findings Fix` 拆 7 sub-epic（FX.1-FX.7），由訂閱版 runner + codex runner 並行修復。

詳細 sub-epic 結構見 TODO.md。
