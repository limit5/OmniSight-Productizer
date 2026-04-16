---
model_id: claude-opus-4-7
provider: anthropic
family: claude
context_window: 200000
strengths: [deep-reasoning, architecture-design, complex-refactoring, code-review, advanced-software-engineering]
---

# Claude Opus — Agent Behavior Rules

## Prompt Style
- Leverage deep reasoning for architectural decisions
- Consider multiple approaches before committing to implementation
- Document trade-offs explicitly in code comments and commit messages

## Code Generation
- Prioritize correctness and maintainability over conciseness
- Design for testability — prefer dependency injection and interfaces
- Consider edge cases and failure modes proactively
- Write comprehensive error messages that aid debugging

## Tool Calling
- Plan multi-step operations before executing
- Read related files to understand full context before modifications
- Verify changes don't break existing interfaces

## Architecture Decisions
- When creating new modules, check for existing patterns in the codebase
- Prefer composition over inheritance
- Keep interfaces narrow and focused
