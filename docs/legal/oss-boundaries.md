---
audience: legal + architect
status: accepted
date: 2026-04-30
priority: HD (community integration)
related:
  - docs/design/hd-hardware-design-verification.md
  - docs/design/hd-daily-scenarios-and-platform-pipeline.md
  - TODO.md (Priority HD HD.1.2 / HD.1.3 / HD.1.12 / R57)
---

# OSS License Boundary Discipline — OmniSight HD

> **One-liner**：HD.1 系列要借力 ~12 個社群 OSS 專案（KiCAD-MCP / Altium-Schematic-Parser / altium2kicad / OpenOrCadParser / gerbonara / pygerber / OdbDesign / ODBPy / vision-parse / SKiDL / pyFDT / ldparser / kicad-skip 等）省 32-42 工程週、但其中包含 **GPL-2.0** 與 **AGPL-3.0** copyleft license — 若不嚴守邊界、SaaS 整體會被傳染、商業授權崩潰。本文件定義紀律與執行機制。

---

## 1. 為何這份文件存在

OmniSight 是商業 SaaS、需保留客戶資料 / 商務邏輯不被 viral copyleft license 強制 release source。但 HD 借力的最佳社群專案中：

- `thesourcerer8/altium2kicad`（GPL-2.0）— Altium PcbDoc 唯一可用 binary parser（reverse-engineering 工作量數月）
- `nam20485/OdbDesign`（AGPL-3.0）— ODB++ 唯一 active mature parser

不借力 = 多花 **~10-14 工程週**。借力但邊界不清 = **整套 SaaS 被傳染**。本文件選後者中段：**借力 + 嚴守 process boundary**。

---

## 2. 三層 License Boundary 策略

| License Tier | 整合方式 | 商務影響 | 範例 |
|--------------|----------|---------|------|
| **MIT / Apache / BSD / WTFPL / Public Domain** | **直接 link / vendor**（首選） | 保留 attribution、無傳染 | KiCAD-MCP-Server (MIT) / Altium-Schematic-Parser (MIT) / OpenOrCadParser (MIT) / gerbonara (Apache-2.0) / pygerber (MIT) / ODBPy (Apache-2.0) / vision-parse (MIT) / SKiDL (MIT) / pyFDT (Apache-2.0) / ldparser (MIT) / Zephyr python-devicetree (Apache-2.0) |
| **LGPL-2.1 / LGPL-3.0** | **動態 link OK、靜態 link 須 source release** | 動態 link 商務無妨、避免靜態 vendor | kicad-skip (LGPL-2.1) |
| **GPL-2.0 / GPL-3.0** | **subprocess / 命令列工具 only、絕不 link / vendor 進 source tree** | 邊界對 = 商務無妨、邊界錯 = 整套 SaaS 被迫 release source | altium2kicad (GPL-2.0、Perl 工具走 subprocess) |
| **AGPL-3.0** | **Docker sidecar + REST only、process boundary 即 license boundary、SaaS over network 也會觸發 viral** | 邊界對 = 商務無妨、邊界錯 = 即便不 distribute binary 也須 release source（AGPL §13） | OdbDesign (AGPL-3.0、自帶 Docker REST 設計) |
| **Proprietary / Commercial** | 商務簽約 + 明示 redistribution 範圍 | 視合約而定 | Altium / Cadence / Mentor / Siemens 官方 SDK（不在借力範圍） |

---

## 3. GPL Boundary 操作細節（altium2kicad 案例）

**戰略**：把 altium2kicad 視為**獨立的 CLI 工具**、透過 subprocess 呼叫、結果是**純資料**（KiCad 檔案）、資料無 license traction。

**邊界執行**：
- altium2kicad 進 `third_party/altium2kicad/`、git submodule 鎖 commit、**不 install 進 OmniSight Python venv**
- OmniSight backend 走 `subprocess.run(["perl", "third_party/altium2kicad/altium2kicad.pl", input_file, output_dir])`
- altium2kicad 的 stdout / stderr 視為純文字結果、不視為「source code」
- altium2kicad 產出的 `.kicad_sch` / `.kicad_pcb` 是**客戶資料 + 標準格式檔**、不繼承 GPL（KiCad 檔本身不是 altium2kicad 的衍生作品、是經 altium2kicad 處理的資料）
- container image 內 altium2kicad 副本、build script 必須**獨立步驟 install**（與 OmniSight Python wheel 分離）、Dockerfile 註記「third-party GPL tool, isolated subprocess only」

**法律備援**：每次 release 前 legal review 確認 altium2kicad 仍純 subprocess、未被任何 import / link / vendoring。

---

## 4. AGPL Boundary 操作細節（OdbDesign 案例）

**戰略**：OdbDesign 自帶 Docker REST 設計、SaaS 與其透過 HTTP 通訊、**process boundary = network boundary = license boundary**。

**邊界執行**：
- OdbDesign 跑在獨立 Docker container（`docker run nam20485/odbdesign:latest`）
- OmniSight backend 走 `httpx.post("http://odb-sidecar:8080/parse", files={...})`
- container image 由 OdbDesign upstream 提供、我方**不 fork 不修改**（純 consumer）
- 客戶 self-hosted 部署若要免 OdbDesign（純 commercial license 客戶），可走 `ODBPy` Apache fallback（HD.1.12c）
- **AGPL §13 觸發條件**：若我方修改 OdbDesign source、即便僅內部 deploy、也須對 OdbDesign 修改部分 release source。**規避策略**：永不修改 upstream image、需求改進走 upstream PR（OdbDesign 是 active 專案、PR 通常被 merge）
- container 跑在 OmniSight infra、OdbDesign 本身不 expose 給客戶端（避免客戶端的 AGPL §13 義務 cascade）

**法律備援**：legal review 每季抽檢 OdbDesign image SHA、確認無 fork drift；OdbDesign upstream license 若改變（如轉 commercial）走 N10 ledger alert + 切 ODBPy fallback。

---

## 5. CI Enforcement

### 5.1 License Scanner

CI 加以下其一（評估後選定）：
- **FOSSA**（commercial、最 thorough、付費）
- **ScanCode Toolkit**（Apache-2.0、自架、社群 standard）
- **`licensee`**（MIT、GitHub 內部用、輕量）

跑點：
- PR pre-merge：scan diff、新 dep 進來 → license 必標、未知 license 則 block
- nightly：scan whole repo、drift 警報
- release pre-tag：full audit、結果歸檔 N10 ledger

### 5.2 GPL / AGPL Block Patterns

```yaml
# .github/workflows/license-check.yml
- name: GPL/AGPL guard
  run: |
    # 在 backend/ frontend/ 兩 source tree 內
    # 若出現 GPL-2.0 / GPL-3.0 / AGPL-3.0 標記 → fail
    licensee detect --json . | jq '
      .matched_files[] |
      select(.license.spdx_id |
        test("^(GPL|AGPL)-[0-9]"))
    ' && exit 1 || exit 0
```

允許目錄白名單：
- `third_party/altium2kicad/` — GPL-2.0 OK（subprocess only、有 Dockerfile 隔離標記）
- `third_party/odb-design-image-pin/` — AGPL-3.0 OK（純 image 引用、無 source vendor）

### 5.3 第三方 dep registry

`third_party/dependencies.yml`：
```yaml
dependencies:
  - name: KiCAD-MCP-Server
    upstream: https://github.com/mixelpixx/KiCAD-MCP-Server
    license: MIT
    integration: docker_sandbox
    pin: <commit-sha>
    last_audit: 2026-04-30
  - name: altium2kicad
    upstream: https://github.com/thesourcerer8/altium2kicad
    license: GPL-2.0
    integration: subprocess_cli
    boundary_note: 嚴禁 import / link、僅 subprocess.run
    pin: <commit-sha>
    last_audit: 2026-04-30
  - name: OdbDesign
    upstream: https://github.com/nam20485/OdbDesign
    license: AGPL-3.0
    integration: docker_sidecar_rest
    boundary_note: 嚴禁 fork upstream image、所有改進走 upstream PR
    pin: <docker-tag>
    last_audit: 2026-04-30
  # ... 其他 ~10 個 dep
```

---

## 6. 商務 / 銷售面影響

### 6.1 簽約時對客戶說明

OmniSight 商務合約 license 條款須包含：

> 「OmniSight 整合多家 open-source 工具作為輔助 backend、嚴守 license 邊界。GPL-2.0（altium2kicad）、AGPL-3.0（OdbDesign）等 copyleft component 均透過 subprocess / network process 隔離、其 source 義務不 cascade 到 OmniSight 主 codebase。客戶取得 OmniSight 服務不會繼承 copyleft 義務。完整 third-party license inventory 詳見 `docs/legal/oss-boundaries.md`。」

### 6.2 self-hosted edition（HD.21.5.2）

self-hosted 客戶會拿到 OmniSight container images。images 內含的 third-party tools：
- MIT / Apache / BSD：與主 binary 一起 distribute、附 LICENSE 檔即可
- LGPL：附 LICENSE + 指引「如要替換、走標準 LGPL §6 替換流程」
- **GPL（altium2kicad）**：附 LICENSE + 完整 source tarball（GPL §3 義務、不影響我方 OmniSight source）
- **AGPL（OdbDesign）**：客戶自行 pull upstream image、我方 distribution **不 ship** AGPL component（避免客戶側 AGPL §13 義務、或客戶須自行 evaluate）

### 6.3 客戶 NDA / IP 隔離

客戶 schematic / PCB 上傳 → 經 GPL / AGPL tool subprocess 處理 → 輸出 KiCad 檔。這個資料流：
- 客戶資料：客戶持有、處理過程加密（KS.1 envelope）
- subprocess 環境：sandbox（distroless + no outbound network）
- 處理結果：純資料、無 license 傳染
- audit log：N10 ledger 紀錄誰 / 何時 / 對哪檔 / 走哪 backend

---

## 7. 紀律 review

- **季度 license audit**：scanner output + manual spot-check + N10 ledger 紀錄
- **新 dep 加入 review**：3 人簽核（engineer + architect + legal）
- **upstream license change alert**：每月自動 check upstream `LICENSE` 檔 SHA、變動 → alert
- **annual third-party pentest**：第三方 pentest 同時驗 license 合規（KS.4.4 整合）

---

## 8. Sign-off

- **Owner**: Agent-software-beta + nanakusa sora
- **Date**: 2026-04-30
- **Status**: Accepted
- **Next review**: HD.1.2 / HD.1.12 開工前、第一個 GPL / AGPL subprocess 落地時必過 legal review
