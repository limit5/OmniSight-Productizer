---
model_id: claude-mythos
provider: anthropic
family: claude
context_window: 1000000
strengths: [ultra-long-context, cross-file-analysis, large-codebase-reasoning]
---

# Claude Mythos — Agent Behavior Rules

## Prompt Style
- Leverage ultra-long context for comprehensive codebase analysis
- Cross-reference multiple files simultaneously for consistency checks
- Provide detailed analysis spanning entire module boundaries

## Code Generation
- Same quality standards as Claude Opus
- Can process entire subsystems in a single pass
- Ideal for large-scale refactoring and cross-file dependency analysis

## Tool Calling
- Read multiple related files in sequence to build full picture
- Use search_in_files extensively to find all references before refactoring
- Verify cross-module consistency after changes

## Unique Capabilities
- Full codebase comprehension in context
- Suitable for architecture-level reviews and migration tasks
