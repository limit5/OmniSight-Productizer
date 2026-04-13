# OmniSight Productizer

![Next.js](https://img.shields.io/badge/Next.js-16.2-black?logo=next.js)
![React](https://img.shields.io/badge/React-19.2-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.7-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4.2-06B6D4?logo=tailwindcss&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.1-1C3C3C?logo=langchain&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Isolation-2496ED?logo=docker&logoColor=white)

![Anthropic](https://img.shields.io/badge/Anthropic-Claude-D97706?logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT-412991?logo=openai&logoColor=white)
![Google](https://img.shields.io/badge/Google-Gemini-4285F4?logo=google&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-Local-ffffff?logo=ollama&logoColor=black)

![Tests](https://img.shields.io/badge/Tests-523-brightgreen)
![Tools](https://img.shields.io/badge/Tools-29_Sandboxed-green)
![Agents](https://img.shields.io/badge/Roles-19_Skills-blueviolet)
![API](https://img.shields.io/badge/API-~80_Endpoints-blue)

Full-stack autonomous development command center for embedded AI cameras (UVC/RTSP). Multi-agent orchestration with isolated workspaces, real-time streaming UI, dual-track simulation, and Docker-containerized cross-compilation.

## Architecture

```
Browser (Windows/Linux)
    |
Next.js (WSL2:3000)              Frontend — Sci-Fi FUI dashboard (18 components)
    | rewrites proxy
FastAPI (WSL2:8000)               Backend — Multi-agent engine (14 routers, ~80 endpoints)
    |
    +-- LangGraph Pipeline        Orchestrator → Conversation/Specialist → Tool Executor → Summarizer
    +-- 8 LLM Providers           Anthropic, OpenAI, Google, xAI, Groq, DeepSeek, Together, Ollama
    +-- 29 Sandboxed Tools        File, Git, Bash, Simulation, Platform, Review, Report
    +-- EventBus → SSE            Real-time push + event persistence + replay API
    +-- WorkspaceManager          git worktree per agent (CODEOWNERS enforcement)
    +-- ContainerManager          Docker cross-compilation (aarch64/armv7/riscv64 + vendor SDK mount)
    +-- Dual-Track Simulation     algo (x86 + Valgrind) / hw (mock sysfs + QEMU)
    +-- 4-Tier Notifications      L1 log → L2 Slack → L3 Jira → L4 PagerDuty (DLQ + retry)
    +-- NPI Lifecycle             8 phases × 3 tracks × 4 business models (Timeline + Gantt)
    +-- Slash Commands            22 commands with autocomplete (/status, /build, /simulate, ...)
    +-- SQLite WAL                11 tables, event_log, debug_findings, integrity check
```

## Quick Start

```bash
# 1. Backend
cd OmniSight-Productizer
pip install -r backend/requirements.txt
python3 -m uvicorn backend.main:app --port 8000

# 2. Frontend
npm install
npm run dev

# 3. Browser
open http://localhost:3000
```

### Environment Variables

Copy `.env.example` to `.env` and set your LLM API key:

```bash
OMNISIGHT_LLM_PROVIDER=anthropic
OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-...

# Or use local Ollama (no key needed)
# OMNISIGHT_LLM_PROVIDER=ollama
```

Without an API key the system runs in rule-based fallback mode — all features work, agents produce template responses instead of LLM-generated ones.

## Key Features

### Multi-Agent Pipeline
- **8 specialist agents**: firmware, software, validator, reporter, reviewer, general, custom, devops
- **19 role skill files** with domain-specific prompts (BSP, ISP, HAL, algorithm, SDET, etc.)
- **Conversational AI**: intent detection routes questions to conversation node (no tools), tasks to specialists
- **Self-healing**: error_check → retry (3x) → loop detection → human escalation
- **Verification loop**: simulation [FAIL] → auto-fix code → re-verify (2x max)

### Simulation & Verification
- **Dual-track**: algo (data-driven replay + Valgrind) / hw (mock sysfs + QEMU cross-run)
- **simulate.sh**: unified test runner with JSON report, coverage enforcement, cmake toolchain support
- **4 platform profiles**: aarch64, armv7, riscv64, vendor-example (extensible for any SoC)

### DevOps Integration
- **Gerrit**: AI reviewer (patchset-created → auto-review), Code-Review -1 → auto-fix task
- **GitHub/GitLab/Jira**: bidirectional webhook sync (HMAC/token validation, 5s debounce)
- **CI/CD triggers**: GitHub Actions, Jenkins, GitLab CI (fire-and-forget on merge)
- **CODEOWNERS**: file-level ownership enforcement + pre-merge conflict detection

### NPI Lifecycle
- **8 phases**: PRD → EIV → POC → HVT → EVT → DVT → PVT → MP
- **3 tracks**: Engineering, Design, Market (OBM mode)
- **4 business models**: ODM, OEM, JDM, OBM
- **Gantt chart**: horizontal progress bars with Timeline/Gantt toggle

### Reliability & Recovery
- **Token budget**: 3-tier (80% warn → 90% downgrade → 100% freeze) + daily auto-reset
- **Provider failover**: chain with 5min circuit breaker cooldown + health UI
- **Watchdog**: 30min task timeout, 2hr stuck detection, dynamic reallocation
- **Startup cleanup**: reset stuck agents/simulations, orphan containers, git locks
- **Event persistence**: DLQ with retry (3x exponential backoff), event_log table, replay API
- **Debug blackboard**: cross-agent error tracking, loop detection, /system/debug API

### Slash Commands
Type `/` in any input field for autocomplete:

| Category | Commands |
|----------|----------|
| System | `/status` `/info` `/debug` `/logs` `/devices` |
| Dev | `/build` `/test` `/simulate` `/review` `/platform` |
| Agent | `/spawn` `/agents` `/tasks` `/assign` `/invoke` |
| Provider | `/provider` `/switch` `/budget` |
| NPI | `/npi` `/sdks` |
| Tools | `/help` `/clear` `/refresh` |

## Project Structure

```
OmniSight-Productizer/
├── app/                    # Next.js pages + error boundary
├── components/omnisight/   # 18 FUI components
├── hooks/                  # use-engine (SSE + state), use-mobile
├── lib/                    # api.ts, slash-commands.ts, i18n
├── backend/
│   ├── agents/             # graph.py, nodes.py, llm.py, tools.py, state.py
│   ├── routers/            # 14 API routers
│   ├── docker/             # Dockerfile.agent (aarch64 + Valgrind + QEMU)
│   ├── tests/              # 29 test files, 370 tests
│   ├── slash_commands.py   # 22 / command handlers
│   ├── codeowners.py       # File ownership parser
│   ├── notifications.py    # L1-L4 tiered dispatch + DLQ
│   └── ...
├── configs/
│   ├── platforms/          # 4 platform profiles (aarch64, armv7, riscv64, vendor-example)
│   ├── roles/              # 19 role skill files
│   ├── skills/             # 4 Anthropic-format task skills
│   ├── models/             # 7 LLM model rule files
│   ├── templates/          # 2 Jinja2 report templates
│   ├── CODEOWNERS          # File → agent type mapping
│   └── hardware_manifest.yaml
├── scripts/simulate.sh     # Dual-track simulation runner
├── test_assets/            # Ground truth test data
├── docs/
│   ├── design/             # 10 system design documents
│   └── sop/                # Implementation SOP
└── HANDOFF.md              # Complete project state document
```

## Development Phases

28 phases completed (Phase 1-29), covering:
- Core infrastructure (Phase 1-12)
- NPI + Artifact + Simulation (Phase 13-15)
- Error recovery + Conversational AI (Phase 16-19)
- Multi-agent patterns: 5 patterns at 80-98% coverage (Phase 20-24)
- Provider UI + Webhooks + Handoff viz + NPI Gantt (Phase 25-27)
- SoC SDK integration + Slash commands (Phase 28-29)

See [HANDOFF.md](HANDOFF.md) for detailed phase history and future roadmap.

## License

Proprietary. All rights reserved.
