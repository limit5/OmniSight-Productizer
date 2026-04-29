---
audience: architect
status: accepted
date: 2026-04-29
priority: KS — multi-tenant secret management (3-tier × 3-phase)
related:
  - docs/design/as-auth-security-shared-library.md
  - docs/security/as-installer-threat-model.md
  - docs/security/as-migration-discipline.md
  - docs/design/hd-daily-scenarios-and-platform-pipeline.md (HD.21.5 self-hosted edition)
  - TODO.md (Priority KS section, Priority I multi-tenancy foundation)
---

# ADR — Priority KS: Multi-Tenant Secret Management（3-Tier × 3-Phase）

> **One-liner**：把目前 single Fernet master key 的設計演進成 **3 tier 客戶能自選機密管理強度**（Envelope / CMEK / BYOG proxy）+ **3 phase 我方依商務節奏 ship**（multi-tenant 上線前必過 Phase 1、中型 enterprise 簽約前 Phase 2、銀行政府客戶詢盤時 Phase 3）。**核心目標**：multi-tenant 第一天前不要在 secret management 留資安債。

---

## 1. 背景與問題陳述

### 1.1 現況快照

OmniSight 的 secret 儲存目前透過 **AS Token Vault**（`backend/security/token_vault.py`，AS.2.1 落地）：
- **單一 master Fernet key**（AS.0.4 §3 invariant）— 從 env var `OMNISIGHT_FERNET_MASTER_KEY` 讀
- **Per-user envelope binding**（防 DB row swap）
- **`key_version` column 預留**（AS.2.1 為未來 KMS rotation 留 hook）
- 覆蓋：oauth_tokens / customer api_keys / future provider keys

這個設計**單租戶下夠用**、但 multi-tenant 上線後會變成資安炸彈：
- master key compromise = 全租戶全 key 全洩
- 沒有 per-tenant 隔離（DEK / KEK 分離）
- 沒有 KMS audit trail（誰 / 何時 / 為何 decrypt 不可追）
- 沒有客戶側可 revoke 的機制（合規客戶必問）
- 沒有 spend anomaly detector（key leak 後客戶要靠 provider 帳單才知道）

### 1.2 真實場景（為何不能拖）

**N 客戶 × 9 provider × 平均 2 把 key** ≈ 數百到數千把 key、每把都有花錢能力（Anthropic / OpenAI 預設無花費上限）。一次 breach + leak：

- **直接金錢損失**：客戶被燒爆、ToS 規定 key holder 全責、客戶提告連帶
- **品牌死亡**：early-stage SaaS 一次 breach 就終結
- **法律風險**：GDPR / CCPA / 中國個保法 / SOC 2 全踩雷
- **內部威脅**：rogue 員工 / 離職員工 / 被 social engineering 員工
- **備份洩漏**：DB backup 上 S3、bucket misconfig、key 隨備份外流
- **Log 洩漏**：log aggregator (Datadog / Sentry / Splunk) 不小心吞 key

### 1.3 OmniSight 上下游約束

- **AS Token Vault 已 ship**：演進、不重做
- **Priority I（multi-tenancy foundation, line 1098）**：multi-tenant 真正啟用
- **HD.21.5 self-hosted edition**：air-gapped / customer VPC 部署
- **N10 ledger**：tamper-evident audit chain
- **PEP gateway**：metering / billing
- **既有 9 個 LLM provider**（Anthropic / OpenAI / Google / xAI / Groq / DeepSeek / Together / OpenRouter / Ollama）

---

## 2. 決策（Decision）

採用 **3 tier × 3 phase 模型**：

- **3 tier**：客戶依規模 / 監管需求自選 secret management 強度
- **3 phase**：我方依商務節奏 ship、不一次寫完才上線

### 2.1 三層 Tier 模型

| Tier | 對象 | 機制 | 客戶體感 | 覆蓋率（預估）|
|------|------|------|----------|-------------|
| **Tier 1（預設）** | 個人 / startup / 小團隊 | **Envelope encryption**（per-tenant DEK + master KEK in KMS） | 與現況一樣零摩擦 | ~95% |
| **Tier 2** | 中大型企業 / 受監管產業 | **CMEK**（客戶帶 KMS key、可隨時 revoke） | onboarding 多 5-10 min wizard | ~4% |
| **Tier 3** | 銀行 / 政府 / 醫療 / 軍工 / air-gapped | **BYOG proxy**（客戶 VPC 跑 omnisight-proxy） | SRE 1-2 day 部署 + 永久 ops | ~1% |

### 2.2 三階段 Phase 模型

| Phase | 對應 Tier | 時程估算 | 觸發條件 | 是否硬阻塞 |
|-------|-----------|---------|---------|-----------|
| **Phase 1** | Tier 1 envelope | ~3 週 | **BP 完工後 → HD 開工前**（multi-tenant 上線 + HD.17 多客戶 NDA 隔離前置） | **是**（single Fernet master key 在 multi-tenant 場景是資安炸彈） |
| **Phase 2** | Tier 2 CMEK | ~3 週 | **HD 之後 commercial-driven** — 第一個中型 enterprise 詢盤要求 CMEK 時暫停 HD 排程、提前 pull forward | 否（商務驅動、可 pull forward） |
| **Phase 3** | Tier 3 BYOG proxy | ~2 週（與 HD.21.5 共享 image） | **HD 之後 commercial-driven** — 第一個銀行 / 政府 / 軍工詢盤；缺席期客戶可走 **HD.21.5.2 self-hosted edition** 當 fallback | 否（商務驅動、可 pull forward） |

### 2.2.1 排程定位（2026-04-29 operator confirmed）

```
AS (done) → W11-W16 (in progress) → FS → SC → BP → KS.1 → HD → [KS.2 / KS.3 by commercial trigger]
                                                    ↑
                                       3 週、sequential 必過
                                       multi-tenant + HD.17 前置
```

- **KS.1 = sequential 必過**：BP 完工後 / HD 開工前、~3 週、無例外
- **KS.2 = HD 後 commercial-driven**：mid-market enterprise 詢盤觸發；security questionnaire 通常會問 CMEK、沒有 = 失單；Phase 2 提前 ~3 週 cost 換 unblock deal
- **KS.3 = HD 後 commercial-driven**：銀行 / 政府 / 軍工詢盤；缺席期 fallback = **HD.21.5.2 self-hosted edition**（整套 OmniSight 進客戶 VPC、與 KS.3 BYOG 是兩條獨立路徑、僅共享 proxy container image 為實作便利）
- **KS.4 cross-cutting**：跨 phase incrementally ship、不阻塞任一 phase

### 2.2.2 BP 期間 multi-tenant 政策（明文）

**BP 期間（路線 (b) BP 5.5 週）不開 multi-tenant 收費入口**：

- 防 KS.1 沒到位、就有 paying tenant 暴露於 single Fernet master key 風險
- BP 期間 OmniSight 維持 **single-tenant / 內部 dogfood / 邀請制 early-access**
- 邀請制 early-access tenant 簽 explicit risk acceptance form：明示「pre-multi-tenant secret management、breach 風險高於 GA、可隨時退出」
- BP 完工 + KS.1 ship 後、才打開 self-service signup + 收費

此政策進 N10 audit ledger、由 operator + 法律 / 業務共同 sign-off。

### 2.3 為什麼不走 zero-knowledge / 客戶端加密

考慮過：瀏覽器 passphrase 衍生 key、server 只看密文。否決：
- **Background scheduled agent 跑不了**（key 不在 server）
- **多裝置同步要靠重派 passphrase**、體驗炸
- **OmniSight 是 agent 驅動平台**、非純互動式 SaaS（如 1Password / Bitwarden 是純互動）

zero-knowledge 留給未來「個人 vault」這類純互動子模組考慮、不是 LLM key 主存儲方案。

### 2.4 為什麼不走 OAuth delegation

主流 LLM provider（Anthropic / OpenAI / Google AI Studio / xAI / Groq / DeepSeek / Together / OpenRouter）**沒有暴露 OAuth API endpoint**。即便未來他們 ship、OAuth refresh token rotation 仍要儲存在我方、本質仍是 secret management 問題、只是把 key 換成 refresh token。所以 OAuth 不能取代 KS、最多是 KS 的補充入口（已在 AS.1.x 處理過用戶層 OAuth）。

### 2.5 為什麼不走純 aggregator（OpenRouter / Portkey / Helicone）

考慮過：所有客戶都走 aggregator、我方不存任何 provider key。否決：
- aggregator 抽成 5-10%、客戶不接受（嵌入式產業客戶毛利薄）
- 9 家 provider 中 OpenRouter 自己也是其中一個、不能取代直連
- 客戶可能已有 vendor 直接合約 / 議價 / credit 池

aggregator 留給「Tier 0」入門 fast-path：客戶連 KMS 都不想設、可全部走 OpenRouter pass-through、由 OmniSight 統一付費 + bill-back。本 ADR 不展開、未來考慮。

---

## 3. 架構概觀

```
┌─────────────────────────────────────────────────────────────────────┐
│             KS (Multi-Tenant Secret Management)                     │
└─────────────────────────────────────────────────────────────────────┘

Phase 1 (Tier 1) — Envelope Encryption + KMS Master Key
┌──────────────────────────────────────────────────────────────────────┐
│  per-tenant DEK (data encryption key)                                │
│        │                                                              │
│        ▼ encrypted with                                              │
│  master KEK (key encryption key) — in AWS KMS / GCP KMS / Vault     │
│        │                                                              │
│        ▼ decrypt-on-use via IAM                                      │
│  plaintext provider key — in memory only, never persisted plain     │
│        │                                                              │
│        ▼ every decryption                                            │
│  Decryption Audit Log (who / when / which key / which request)      │
│  → N10 ledger (tamper-evident chain)                                │
│                                                                       │
│  + Spend Anomaly Detector (rate threshold → throttle + alert)       │
│  + Log Secret Scrubber (custom logger filter + CI gitleaks)         │
│  + Backup Pipeline DLP                                              │
└──────────────────────────────────────────────────────────────────────┘

Phase 2 (Tier 2) — CMEK (Customer-Managed Encryption Keys)
┌──────────────────────────────────────────────────────────────────────┐
│  Customer's AWS KMS / GCP KMS / Vault key (CMK)                      │
│        │                                                              │
│        ▼ OmniSight has IAM role with Encrypt + Decrypt only         │
│  per-tenant DEK encrypted under customer CMK                        │
│        │                                                              │
│        ▼ customer disable CMK → all data unreadable instantly       │
│        ▼ customer audit via own CloudTrail                          │
└──────────────────────────────────────────────────────────────────────┘

Phase 3 (Tier 3) — BYOG (Bring Your Own Gateway)
┌──────────────────────────────────────────────────────────────────────┐
│  Customer's VPC                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  omnisight-proxy container                                    │    │
│  │   - holds provider keys locally (key never leaves customer) │    │
│  │   - mTLS + signed nonce auth                                │    │
│  │   - forwards LLM requests, streams response back            │    │
│  └─────────────────────────────────────────────────────────────┘    │
│        ▲                                                              │
│        │ mTLS                                                         │
│  OmniSight SaaS backend                                              │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.1 Tier 自由切換

- **Tier 1 → Tier 2 升級**：tenant 在 settings 觸發 wizard、systems 重新加密所有 tenant 資料（per-tenant DEK 不變、上層 wrap 從 master KEK 換成客戶 CMK）
- **Tier 2 → Tier 1 降級**：撤回我方 IAM 對客戶 CMK 的依賴、re-encrypt 回 master KEK
- **Tier 2 → Tier 3 升級**：部署 proxy + key migration runbook（key 從 OmniSight 端 export 給客戶 → 客戶導入 proxy → OmniSight 端清除）
- **Tier 3 → 任何 tier 降級**：拒絕（zero-trust exit、客戶要重 onboard）

---

## 4. KMS Adapter 抽象設計

支援 4 個 KMS backend、客戶 / operator 可選：

```python
class KMSAdapter(Protocol):
    def encrypt(self, plaintext: bytes, key_id: str, context: dict[str, str]) -> bytes:
        ...
    def decrypt(self, ciphertext: bytes, key_id: str, context: dict[str, str]) -> bytes:
        ...
    def describe_key(self, key_id: str) -> KeyMetadata:
        """Used to detect customer-side disable / revoke (Phase 2)."""
        ...

# Implementations
class AwsKmsAdapter(KMSAdapter): ...   # boto3 + IAM assume-role
class GcpKmsAdapter(KMSAdapter): ...   # google-cloud-kms
class VaultTransitAdapter(KMSAdapter): ...   # HashiCorp Vault Transit secret engine
class LocalFernetAdapter(KMSAdapter): ...   # dev / single-tenant fallback
```

**選用準則**：
- **AWS / GCP 客戶** → 用對應雲 KMS（CMEK 場景必選）
- **多雲 / 雲中立** → HashiCorp Vault Transit（self-hosted Vault 也可）
- **dev / single-tenant** → LocalFernetAdapter（與現況等價、退路）
- **Self-hosted edition** → 預設 Vault Transit、operator 也可用 LocalFernet

---

## 5. UX 對照矩陣

| 動作 | 現況 | Phase 1 | Phase 2 | Phase 3 |
|------|------|---------|---------|---------|
| 首次 onboarding | 註冊 | 無變化 | + 5-7 click wizard 設 IAM/KMS | + 1-2 day SRE 部署 proxy |
| 加 API key | 貼 key → save | 無變化 | 無變化 | 在 proxy config 設、UI 不貼 |
| 日常用 agent | 點 invoke | 無變化 | 無變化 | + 10-50 ms latency |
| Key rotation | UI 改 → save | UI 改 + audit log | 同 Phase 1 | 改 proxy config |
| 緊急 revoke | UI 刪（DB 副本仍存） | UI 刪 + DEK 銷毀 | 客戶 KMS disable、瞬間失能 | 客戶 proxy 拔 key |
| Audit / 合規 | 翻 DB log | 完整 decryption audit | + 客戶 KMS CloudTrail | 客戶 proxy 完整流量 log |
| Spend 異常 | 帳單炸了才知 | 即時 anomaly alert | 同 + KMS rate limit | 同 + proxy enforce |
| 多人協作 | role 控管 | 同 + 每筆 decryption 記名 | 同 | key 不在我方、只有 proxy 流量 |

### 5.1 Day-in-the-life

#### Tier 1 — ACME 公司 Alice（startup CTO）

```
Day 0: 註冊 → 加 Anthropic key → 完成（5 分鐘、與現況一樣）
Day 30: 同事 leak key 到 GitHub
   14:00 Anthropic email 偵測到
   14:01 OmniSight Spend Anomaly Detector 偵測 token rate 跳 50x → throttle + Slack alert
   14:02 Alice 收 alert、進 UI、Rotate key
   14:03 砍 Anthropic 舊 key、產新、貼 OmniSight、save
   ✅ 偵測 → rotate < 3 分鐘
Quarter-end: 下載 audit log、看到「2026-01-15 14:23 user=bob decrypted=anthropic_key for=invoke_agent_42 from=192.168.1.5」每筆都在
```

#### Tier 2 — 金融 startup Carol（CTO）

```
Day 0 onboarding (一次性 5-10 分鐘):
   Step 1 選 KMS provider (AWS)
   Step 2 wizard 顯示精準 IAM policy JSON 給 Carol 在 AWS Console 貼上
   Step 3 把 KMS key ARN 貼回 wizard、verify 連線
   Step 4 ✅ done
Day 60 NDA 撤、要切斷:
   1. Carol 在自家 AWS Console → KMS → Disable
   2. 不需通知 OmniSight、不需我方動任何東西
   3. 從 disable 那秒起、所有 tenant 資料變磚、所有新請求 403 + 明確錯誤
   ✅ 客戶法務拿 KMS disable timestamp 當合規證據
```

#### Tier 3 — 銀行 SecOps Dan

```
Day 0 onboarding (1-2 day):
   1. 簽約、拿 omnisight-proxy image
   2. SRE 部署到行內 VPC + outbound 白名單 + inbound mTLS + provider keys 進 proxy
   3. 在 OmniSight UI 註冊 proxy URL + cert
   ✅ done (含 IT change-control 流程)
Daily: 員工 Eve 點 invoke
   OmniSight backend → 行內 proxy (mTLS) → Anthropic → response 回流
   Eve 體感與 Phase 1/2 一樣 (多 ~30ms 肉眼難察)
Day 200 audit:
   Dan 從 proxy log 拿完整流量
   OmniSight 端只有 metadata（時間 / model / token count）
   ✅ "sensitive data never left bank infra"
Day 365 終止合約:
   Dan 拔 proxy + 撤白名單
   OmniSight 從第一秒完全失聯
   ✅ Zero-trust exit
```

---

## 6. 與 AS Token Vault 的演進關係

KS 不是新建系統、是 AS Token Vault 的**第二代演進**：

| 元件 | AS Token Vault（現況） | KS Phase 1 |
|------|----------------------|-----------|
| Encryption | Single Fernet | Envelope (per-tenant DEK + master KEK) |
| Master key 來源 | env var | KMS (AWS / GCP / Vault) |
| key_version column | 預留欄位（AS.2.1 §） | **正式啟用**為 KEK rotation 索引 |
| Audit | partial（AS.1.4 oauth_audit 涵蓋 OAuth 路徑） | full（每筆 decryption 寫 N10） |
| Anomaly detection | 無 | 有（per-tenant rate threshold） |
| Compat | — | 雙讀雙寫 30 天、之後 deprecate single Fernet |

**Migration 策略**：
- KS.1.3 雙讀雙寫期間：寫只走新 envelope、讀 fallback 到舊 Fernet
- 30 天後：所有 row 已 re-encrypt、deprecate 舊 Fernet 路徑
- AS.0.4 §3 invariant 升級成「single master Fernet key OR envelope (DEK + KEK)、二擇一不混用」、新 invariant 寫進 AS migration discipline 文件

---

## 7. 與其他 Priority 整合

| Priority | KS 整合點 |
|----------|-----------|
| **Priority I（multi-tenancy foundation, line 1098）** | KS.1 是 I 上線前硬阻塞、I 啟用前必須先 ship envelope encryption |
| **Priority AS** | KS Phase 1 改造 AS Token Vault（演進、不重做） |
| **Priority HD.21.5** | KS Phase 3 omnisight-proxy 與 HD.21.5 self-hosted edition 共享 container image + 部署 SOP |
| **Priority N10 audit ledger** | KS 每筆 decryption / KEK rotation / CMEK revoke 寫 N10 hash chain |
| **Priority Z（LLM provider observability）** | KS spend anomaly detector 補 Z 的 rate-limit 觀測 — Z 看「provider 那邊還剩什麼」、KS 看「我這端燒得異常」 |
| **Priority T（billing / payment gateway）** | spend anomaly threshold 與 T 的 budget alarm 整合 |
| **Priority SC（security compliance）** | KS 的 SOC 2 / GDPR / 法規一致性 retest 走 SC pipeline |

---

## 8. Migration / Schema

| Migration | 內容 | 落地時機 |
|-----------|------|---------|
| 0106 | `kms_keys` / `tenant_deks` / `decryption_audits` / `spend_thresholds` / `kek_rotations` | Phase 1 |
| 0107 | `cmek_configs` / `tier_assignments` / `cmek_revoke_events` | Phase 2 |
| 0108 | `proxy_registrations` / `proxy_health_checks` / `proxy_mtls_certs` | Phase 3 |
| 0109-0115 | 預留 — 細節擴充 / 第三方 KMS adapter / Tier 0 aggregator pass-through 等 | 未來 |

合計 KS 0106-0115（10 slots 預留）。

### 8.1 Single-Knob Rollback

每個 Phase 各自有獨立 env knob、彼此正交：
- `OMNISIGHT_KS_ENVELOPE_ENABLED=false` → Phase 1 退回 single Fernet（migration 雙寫期間有效）
- `OMNISIGHT_KS_CMEK_ENABLED=false` → Phase 2 隱藏 Tier 2 wizard、所有 tenant 退回 Tier 1
- `OMNISIGHT_KS_BYOG_ENABLED=false` → Phase 3 隱藏 Tier 3 註冊、proxy 模式不可選

---

## 9. Test Strategy

### 9.1 Phase 1 必過項

- DEK / KEK 分離：master KEK compromise 測試（模擬 master key 洩漏 + 確認 per-tenant DEK 仍需個別 unwrap）
- Envelope round-trip：encrypt → store → fetch → decrypt 與 plaintext byte-equal
- 雙讀雙寫遷移：任何時間點 hard-restart、雙寫 row 都能 read（新／舊路徑各驗一次）
- KEK rotation：rotate 後新 row 用新 KEK、舊 row decrypt 仍通
- Decryption audit log：每次 decrypt 都寫 N10、leak ledger 比對全 match
- Spend anomaly detector：注入「rate spike 50x」事件、驗 throttle + alert 在 60 sec 內觸發
- Log secret scrubber：注入含 `sk-ant-xxx` 字串的 log line、確認 sink（Datadog / Sentry）看到 `[REDACTED]` 而非 raw key
- Backup DLP：模擬 backup pipeline 餵入含 secret 的 row、驗 backup encrypt + DLP 攔截

### 9.2 Phase 2 必過項

- AWS KMS adapter live test（CI sandbox account）
- GCP KMS adapter live test
- Vault Transit live test
- CMEK revoke E2E：disable 客戶側 KMS、驗 OmniSight 60 sec 內偵測 + 所有 tenant 請求 graceful 403
- Tier 升級 / 降級 re-encrypt 路徑

### 9.3 Phase 3 必過項

- mTLS handshake（valid cert / expired cert / self-signed）
- 簽名 nonce replay attack 防護
- Proxy unreachable graceful 失敗（不退回我方直連、嚴格 zero-trust）
- p95 latency overhead < 50 ms
- Proxy ↔ SaaS 串流 LLM response（streaming 不破）

### 9.4 Compat Regression

- KS 全套 disable（三 knob 全 false）→ 退回 single Fernet、既有 AS / OAuth / customer secret 0 回歸

---

## 10. Risk Register（R46-R50）

| ID | 風險 | Mitigation |
|----|------|-----------|
| **R46** | Master KEK compromise（Phase 1）— KMS misconfig / IAM credential leak → 全租戶全洩 | KEK 季度 rotation；IAM least-privilege；KMS admin dual-control（Anthropic 內部 break-glass workflow 模式）；KMS audit log 進 N10 |
| **R47** | KMS vendor lock-in（Phase 2）— 客戶被綁 AWS / GCP | Multi-adapter abstraction (AWS / GCP / Vault) + Vault Transit 為 cloud-neutral fallback、客戶可雙寫 |
| **R48** | CMEK revoke degrades poorly — 客戶 disable key 中、in-flight transactions 中斷 | Graceful degrade：(a) in-flight 完成 / (b) 新請求 60 sec 內偵測到 + 403 + 友善錯誤 + 復原 runbook |
| **R49** | BYOG proxy MITM — 攻擊者中間人 SaaS↔proxy | mTLS + cert pinning + signed nonce、handshake fail 直接 close（不 fallback） |
| **R50** | Audit log integrity tampering — insider 改 decryption audit 紀錄 | N10 hash chain integration、append-only、off-site immutable backup（S3 Object Lock / Glacier）|

---

## 11. Cross-Cutting Defense-in-Depth（KS.4）

不分 tier 都要做：

1. **Log secret scrubber**：custom logger filter + CI pre-commit `gitleaks` / `trufflehog` 掃描
2. **Backup pipeline DLP**：encryption + DLP scan，避免 backup leak
3. **Memory zeroization**：Python `ctypes.memset` 用後抹除（best-effort、防 memory dump）
4. **Quarterly third-party pentest**：每季外部 pentest、不只內部覺得安全
5. **Bug bounty**：HackerOne / Bugcrowd（GA 後啟動）
6. **Incident response runbook**：24h SOP（rotate / notify / forensics / blameless postmortem）
7. **SOC 2 Type II 準備**：規模到一定門檻走認證
8. **GDPR / DSAR 對齊**：tenant data deletion 完整 purge DEK + audit trail metadata

---

## 12. Rollout 三階段

### 12.1 Phase 1 — Tier 1 Envelope Encryption（**BP 完工後 → HD 開工前 sequential 必過、~3 週**）

- Week 1：KMS adapter 抽象 + AWS / GCP / Vault 三家 adapter + LocalFernet fallback
- Week 2：Per-tenant DEK + envelope wrap/unwrap + AS Token Vault 接管 + 雙讀雙寫
- Week 3：Decryption audit log + spend anomaly detector + log scrubber + backup DLP + 完整 test

**Day 1 結束**：所有新建 tenant 已走 envelope、舊 tenant 雙寫漸進升級、operator 看不出差別。

**BP 完工 + KS.1 ship 完成 = multi-tenant gate 解鎖** — 此時可打開 self-service signup + 收費入口、HD.17 多客戶 NDA 隔離也可開工。

### 12.2 Phase 2 — Tier 2 CMEK（**HD 之後 commercial-driven、~3 週**）

**觸發條件**：第一個 mid-market enterprise 詢盤要求 CMEK（security questionnaire 通常必問）。

- Week 1：AWS KMS / GCP KMS / Vault Transit live integration + CMEK revoke 偵測
- Week 2：Tenant Settings Wizard（5-step IAM policy generator）+ Tier upgrade flow
- Week 3：CMEK revoke graceful degrade + audit log 連客戶 SIEM + 完整 E2E test

**Pull-forward 政策**：HD 期間若收到 mid-market enterprise 詢盤 → **暫停 HD 排程**、Phase 2 提前 ship（3 週 cost、unblock 一個 deal 值得）。HD 工作 resume 後從原進度繼續、不重做。

### 12.3 Phase 3 — Tier 3 BYOG Proxy（**HD 之後 commercial-driven、~2 週**）

**觸發條件**：第一個銀行 / 政府 / 軍工 / 國防詢盤。

**缺席期 fallback**：客戶可走 **HD.21.5.2 self-hosted edition**（整套 OmniSight 進客戶 VPC）。HD.21.5.2 與 KS.3 是**兩條獨立路徑**：
- HD.21.5.2 = 整套 OmniSight 在客戶 VPC 跑（最 paranoid 客戶選這條）
- KS.3 = 只把 LLM key 放客戶 VPC、SaaS 主體仍在我方（要 SaaS 體驗但不放心 key）

僅共享 proxy container image 為實作便利、不互依賴。所以 Phase 3 缺席期 OmniSight 仍能服務 air-gapped 客戶。

- Week 1：omnisight-proxy container（distroless base、< 100MB）+ mTLS + 簽名 nonce + proxy config schema
- Week 2：Proxy ↔ SaaS protocol（streaming-aware）+ HD.21.5 self-hosted edition 對齊 + audit log + p95 latency budget 驗證

**Pull-forward 政策**：HD 期間若收到 bank / gov 詢盤而客戶不接受 HD.21.5.2 整套 self-host（要保留 SaaS 體驗）→ **暫停 HD、Phase 3 提前 ship**（2 週 cost）。

---

## 13. Open Questions

1. **Tier 0 aggregator pass-through 要不要做**：客戶連 KMS 都不想設、可全走 OpenRouter / Portkey、由 OmniSight 統一付費 + bill-back。是否在 KS.6 開新 phase？決策推遲到 KS.1 完工後依市場反饋。
2. **HSM（FIPS 140-2 Level 3）支援**：金融 / 政府客戶可能要 HSM 而非軟體 KMS。AWS CloudHSM / Azure Dedicated HSM 整合是 Phase 2.5？決策推遲到第一個 HSM 詢盤。
3. **Tier 升級的 re-encrypt 工作量**：tenant 持有大量 secret 時 Tier 1 → Tier 2 升級可能要小時級 background job。是否預設 throttle / progress UI？決策推遲到 Phase 2 開工。
4. **omnisight-proxy 是否走 WASM 沙盒**：超敏感客戶可能要求 proxy 內 LLM payload 也走 WASM 沙盒（中間人篡改更難）。Phase 4？決策推遲。
5. **Customer revoke + ongoing session**：客戶 disable CMEK 時，**正在執行的 long-running agent 怎麼處理**？硬中斷 vs 完成當前 step vs grace period。決策推遲到 Phase 2 wizard 設計時與客戶訪談。

---

## 14. 參考文件

- `docs/design/as-auth-security-shared-library.md`（AS Token Vault 主 ADR、KS Phase 1 演進對象）
- `docs/security/as-installer-threat-model.md`（AS 安全模型、KS 繼承）
- `docs/security/as-migration-discipline.md`（migration 規律、KS 同步遵守）
- `docs/design/hd-daily-scenarios-and-platform-pipeline.md`（HD.21.5 self-hosted edition、與 KS Phase 3 共享 image）
- `TODO.md` Priority KS section（同步維護）
- `TODO.md` Priority I（multi-tenancy foundation、KS Phase 1 是其硬阻塞）

---

## 15. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-29
- **Status**: Accepted（Phase 1 待排程於 Priority I 之前；Phase 2 / 3 商務驅動）
- **Next review**: Phase 1 完工後、Priority I 啟動前
