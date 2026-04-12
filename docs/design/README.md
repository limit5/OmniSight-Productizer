# System Design Documents

This directory contains all design specifications and architecture documents for the OmniSight Productizer system. Each document describes a subsystem's requirements, architecture, and implementation guidelines.

## Document Index

| Document | Subsystem | Related Phase |
|----------|-----------|---------------|
| [code-review-git-repo.md](code-review-git-repo.md) | Gerrit Code Review + Git repository management | Phase 8 |
| [dual-track-simulation.md](dual-track-simulation.md) | HAL abstraction + dual-track simulation (algo + hw mock/QEMU) | Phase 15 |
| [issue_tracking_system.md](issue_tracking_system.md) | External issue tracker sync (GitHub/GitLab/Jira) | Phase 11 |
| [npi-lifecycle.md](npi-lifecycle.md) | NPI lifecycle management (8 phases x 3 tracks x 4 business models) | Phase 13 |
| [organization_role_map.md](organization_role_map.md) | Agent role hierarchy and skill mapping | Phase 7 |
| [rust_token_killer.md](rust_token_killer.md) | RTK output compression for LLM token optimization | Phase 12 |
| [tiered-notification-routing-system.md](tiered-notification-routing-system.md) | 4-tier notification routing (L1-L4) | Phase 10 |
| [multi-agent-patterns.md](multi-agent-patterns.md) | 5 multi-agent collaboration patterns (Generator-Verifier, Orchestrator-Subagent, Agent Teams, Message Bus, Shared State) | Phase 20-24 |
| [soc-sdk-integration-development-automation.md](soc-sdk-integration-development-automation.md) | SoC SDK/EVK 三軌並行整合（Infra + Software + Hardware → HVT 匯集） | Phase 28 |
| [tiered-memory-architecture.md](tiered-memory-architecture.md) | AI Agent 分層記憶（L1 核心規則 + L2 工作記憶 + L3 經驗向量 DB） | Phase 32 |

## Adding New Documents

Place new design documents in this directory with descriptive filenames. Update this index when adding or renaming documents.
