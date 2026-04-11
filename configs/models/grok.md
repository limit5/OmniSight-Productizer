---
model_id: grok
provider: xai
family: grok
context_window: 131072
strengths: [fast-inference, code-generation, real-time-knowledge]
---

# Grok Series — Agent Behavior Rules

## Prompt Style
- Direct and concise instructions work best
- Use concrete examples when describing expected output
- Break complex tasks into discrete steps

## Code Generation
- Follow existing patterns in the codebase
- Include error handling for system operations
- Verify generated code compiles/runs via tool calls

## Tool Calling
- Execute tools sequentially for dependent operations
- Validate assumptions with read operations before writes
- Use bash tools to verify build results

## Considerations
- Strong at rapid iteration and quick fixes
- Good for time-sensitive debugging tasks
