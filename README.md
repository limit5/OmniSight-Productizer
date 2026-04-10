# OmniSight Productizer

![Next.js](https://img.shields.io/badge/Next.js-16.2-black?logo=next.js)
![React](https://img.shields.io/badge/React-19.2-61DAFB?logo=react&logoColor=white)
![TypeScript](https://img.shields.io/badge/TypeScript-5.7-3178C6?logo=typescript&logoColor=white)
![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-4.2-06B6D4?logo=tailwindcss&logoColor=white)
![Vercel AI SDK](https://img.shields.io/badge/AI_SDK-6.0-000?logo=vercel&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Pydantic](https://img.shields.io/badge/Pydantic-2.11-E92063?logo=pydantic&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.1-1C3C3C?logo=langchain&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-29.4-2496ED?logo=docker&logoColor=white)

![Anthropic](https://img.shields.io/badge/Anthropic-Claude-D97706?logo=anthropic&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o-412991?logo=openai&logoColor=white)
![Google](https://img.shields.io/badge/Google-Gemini-4285F4?logo=google&logoColor=white)
![Meta](https://img.shields.io/badge/Meta-Llama_3-0467DF?logo=meta&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-Local-ffffff?logo=ollama&logoColor=black)

![Platform](https://img.shields.io/badge/Platform-WSL2_|_Linux-FCC624?logo=linux&logoColor=black)
![Architecture](https://img.shields.io/badge/Cross--Compile-aarch64-FF6600?logo=arm&logoColor=white)
![Agents](https://img.shields.io/badge/Agents-5_Specialists-blueviolet)
![Tools](https://img.shields.io/badge/Tools-17_Sandboxed-green)
![API](https://img.shields.io/badge/API-41_Endpoints-blue)
![SSE](https://img.shields.io/badge/Streaming-SSE_Real--Time-orange)

Full-stack autonomous development command center for embedded AI cameras (UVC/RTSP).
Multi-agent orchestration with isolated workspaces, real-time streaming UI, and Docker-containerized cross-compilation.

## Architecture

```
Browser (Windows)
    |
Next.js (WSL2:3000)          Frontend — Sci-Fi FUI dashboard
    | rewrites proxy
FastAPI (WSL2:8000)           Backend — Multi-agent engine
    |
    +-- LangGraph Pipeline    Orchestrator -> Specialist -> Tool Executor -> Summarizer
    +-- 8 LLM Providers       Anthropic (default), Google, OpenAI, xAI, Groq, DeepSeek, Together, Ollama
    +-- 17 Tools               6 File + 8 Git + 1 Bash + 1 Push + 1 Search (sandboxed)
    +-- EventBus -> SSE       Real-time push to all UI panels
    +-- WorkspaceManager      git worktree per agent (isolated branches)
    +-- ContainerManager      Docker aarch64 cross-compilation per agent
    +-- System Monitor        Live CPU/RAM/Disk/USB/Spec/Log/Token from /proc + lsusb + git
```

## Quick Start

```bash
# 1. Backend
cd OmniSight-Productizer
python3 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/python -m uvicorn backend.main:app --reload --port 8000

# 2. Frontend
npm install
npm run dev

# 3. Browser
open http://localhost:3000
```

### Environment Variables

Copy `.env.example` to `.env` and set your LLM API key:

```bash
# Required: at least one LLM provider key
OMNISIGHT_LLM_PROVIDER=anthropic
OMNISIGHT_ANTHROPIC_API_KEY=sk-ant-...

# Or use local Ollama (no key needed)
# OMNISIGHT_LLM_PROVIDER=ollama
```

Without an API key the system runs in rule-based fallback mode — all features work, agents just produce template responses instead of LLM-generated ones.

## Tech Stack

### Frontend
- Next.js 16.2 / React 19 / TypeScript 5.7
- Tailwind CSS 4.2 + custom FUI theme
- Vercel AI SDK 6.0 with 8 provider packages
- 14 custom components (Sci-Fi FUI design)
- SSE subscription for real-time state updates

### Backend
- Python 3.12 / FastAPI 0.115 / Pydantic 2.11
- LangGraph 1.1 (stateful agent orchestration)
- LangChain Core 1.2 + 6 provider integrations
- SSE-Starlette (Server-Sent Events)
- Docker SDK for containerized builds

### Infrastructure
- WSL2 (Ubuntu 24.04) on Windows
- Docker with `omnisight-agent:latest` image (aarch64 cross-compiler)
- Git worktree for per-agent isolation

## System Components

### LangGraph Agent Pipeline

```
START -> Orchestrator (intent routing)
             |
             +-> Firmware Agent   (UVC/RTSP drivers, kernel modules, I2C/SPI, Makefile)
             +-> Software Agent   (algorithms, SDK, C/C++, build systems)
             +-> Validator Agent  (test suites, coverage, QA, benchmarks)
             +-> Reporter Agent   (compliance docs, FCC/CE, reports)
             +-> General Agent    (catch-all)
             |
        Tool Calls? --yes--> Tool Executor (17 tools, sandboxed)
             |                     |
             +<--------------------+
             |
        Summarizer -> END
```

### Isolated Workspaces (Layer 1: git worktree)

When an agent receives a task, the system automatically provisions an isolated workspace:

```
Main Repo (.git)
    |
    +-- master                     Human developers (untouchable by agents)
    +-- .agent_workspaces/
        +-- firmware-alpha/        branch: agent/firmware-alpha/task-1
        +-- validator-gamma/       branch: agent/validator-gamma/task-2
        +-- reporter-delta/        branch: agent/reporter-delta/task-3
```

- Worktrees share the `.git` object store (instant creation, minimal disk)
- Each agent has its own git identity
- Push restricted to `agent/*` branches only
- Finalize generates diff summary + commit history

### Docker Containers (Layer 2: build isolation)

```bash
# Build the agent image (includes aarch64-linux-gnu-gcc)
curl -X POST http://localhost:8000/api/v1/workspaces/container/build-image

# Start a container for an agent
curl -X POST http://localhost:8000/api/v1/workspaces/container/start/firmware-alpha
```

- Ubuntu 22.04 + build-essential + aarch64 cross-compiler
- Workspace bind-mounted to `/workspace`
- Network isolation (`--network none`)
- Container destroyed after cleanup, artifacts persist in worktree

### INVOKE (Singularity Sync)

The lightning button performs context-aware global orchestration:

```
INVOKE pressed
    |
    +-- Input has text? --> Route through LangGraph pipeline
    |
    +-- Input empty? --> Analyze system state:
         +-- Error agents?      --> Auto retry
         +-- Unassigned tasks?  --> Auto assign to matching agents + provision workspaces
         +-- All complete?      --> Generate summary report
         +-- All nominal?       --> Health check
    |
    Results stream to REPORTER VORTEX via SSE
```

### Real-Time Event System

Every backend action publishes to both the SSE event bus and the log buffer:

| Event | Source | REPORTER VORTEX Color |
|-------|--------|-----------------------|
| `[AGENT]` | Agent status changes | Blue |
| `[TOOL]` | Tool execution (start/done/error) | Green |
| `[PIPELINE]` | LangGraph phase transitions | Purple |
| `[TASK]` | Task assignment/completion | Orange |
| `[WORKSPACE]` | Worktree lifecycle | Cyan |
| `[DOCKER]` | Container start/stop | Sky blue |
| `[INVOKE]` | Global orchestration actions | Yellow |
| warn level | Any warning | Orange (full line) |
| error level | Any error | Red (full line) |

## API Reference

### Core (9 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Health check |
| GET/POST | `/api/v1/agents` | List / create agents |
| GET/PATCH/DELETE | `/api/v1/agents/{id}` | Agent CRUD |
| GET/POST | `/api/v1/tasks` | List / create tasks |
| GET/PATCH/DELETE | `/api/v1/tasks/{id}` | Task CRUD |

### Chat & Invoke (6 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/chat` | Sync LangGraph chat |
| POST | `/api/v1/chat/stream` | SSE streaming chat |
| GET/DELETE | `/api/v1/chat/history` | Chat history |
| POST | `/api/v1/invoke` | Sync invoke |
| POST | `/api/v1/invoke/stream` | SSE streaming invoke |

### System (9 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/system/info` | Host info (CPU, RAM, disk, kernel) |
| GET | `/api/v1/system/status` | Header status summary |
| GET | `/api/v1/system/devices` | USB/storage/network devices |
| GET | `/api/v1/system/spec` | Hardware spec from manifest YAML |
| PUT | `/api/v1/system/spec` | Update spec field |
| GET | `/api/v1/system/repos` | Git repositories |
| GET | `/api/v1/system/logs` | System log buffer |
| GET | `/api/v1/system/tokens` | LLM token usage |
| DELETE | `/api/v1/system/tokens` | Reset token counters |

### Providers (3 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/providers` | List 8 LLM providers |
| POST | `/api/v1/providers/switch` | Switch active provider |
| GET | `/api/v1/providers/test` | Test current provider |

### Tools & Events (3 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/tools` | List 17 available tools |
| GET | `/api/v1/tools/by-agent/{type}` | Tools for agent type |
| GET | `/api/v1/events` | Persistent SSE event stream |

### Workspaces & Containers (9 endpoints)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/workspaces` | List active workspaces |
| GET | `/api/v1/workspaces/containers` | List running containers |
| GET | `/api/v1/workspaces/{id}` | Workspace details |
| POST | `/api/v1/workspaces/provision` | Create isolated workspace |
| POST | `/api/v1/workspaces/finalize/{id}` | Commit + diff summary |
| POST | `/api/v1/workspaces/cleanup/{id}` | Remove workspace |
| POST | `/api/v1/workspaces/container/start/{id}` | Start Docker container |
| POST | `/api/v1/workspaces/container/stop/{id}` | Stop container |
| POST | `/api/v1/workspaces/container/build-image` | Build agent image |

## Supported LLM Providers

| Provider | Backend (LangGraph) | Frontend (AI SDK) | Default Model |
|----------|-------|----------|---------------|
| Anthropic | langchain-anthropic | @ai-sdk/anthropic | claude-sonnet-4-20250514 |
| Google Gemini | langchain-google-genai | @ai-sdk/google | gemini-1.5-pro |
| OpenAI | langchain-openai | @ai-sdk/openai | gpt-4o |
| xAI (Grok) | langchain-openai (compat) | @ai-sdk/xai | grok-3-mini |
| Groq | langchain-groq | @ai-sdk/groq | llama-3.3-70b-versatile |
| DeepSeek | langchain-openai (compat) | @ai-sdk/deepseek | deepseek-chat |
| Together.ai | langchain-together | @ai-sdk/togetherai | Meta-Llama-3.1-70B |
| Ollama | langchain-ollama | ollama-ai-provider | llama3.1 |

Switch at runtime: `POST /api/v1/providers/switch {"provider": "groq"}`

## Security

- File I/O sandboxed to workspace root (path escape = PermissionError)
- Bash commands checked against dangerous patterns (rm -rf /, mkfs, dd, curl|bash)
- Git push restricted to `agent/*` branches (force push blocked)
- Docker containers run with `--network none` by default
- Per-agent git identity isolation
- Tool access scoped per agent type

## Project Structure

```
OmniSight-Productizer/
+-- app/                          Next.js App Router
|   +-- page.tsx                  Main dashboard (useEngine hook)
|   +-- api/chat/route.ts        Direct LLM chat route
+-- components/omnisight/         14 FUI components
+-- hooks/use-engine.ts           Unified state + SSE subscription
+-- lib/
|   +-- api.ts                    Backend API client (REST + SSE)
|   +-- providers.ts              AI SDK provider registry
+-- backend/
|   +-- main.py                   FastAPI entry point
|   +-- config.py                 Settings (8 providers, Docker, etc.)
|   +-- models.py                 Pydantic models
|   +-- events.py                 EventBus + log integration
|   +-- workspace.py              git worktree manager
|   +-- container.py              Docker container manager
|   +-- agents/
|   |   +-- graph.py              LangGraph topology
|   |   +-- nodes.py              7 agent nodes
|   |   +-- llm.py                Multi-provider LLM factory
|   |   +-- tools.py              17 sandboxed tools
|   |   +-- state.py              Graph state schema
|   +-- routers/                  9 API routers (41 endpoints)
|   +-- docker/Dockerfile.agent   aarch64 cross-compile image
+-- configs/                      SSOT (Single Source of Truth)
|   +-- hardware_manifest.yaml    Hardware spec (drives all agents)
|   +-- client_spec.json          Customer requirements
+-- test_fixtures/                E2E testing only
|   +-- hardware_manifest.yaml    Sample spec for testing
|   +-- mock_compile.sh           Build simulation script
+-- .env.example                  Environment variable template
```
