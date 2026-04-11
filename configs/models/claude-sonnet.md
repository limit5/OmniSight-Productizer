---
model_id: claude-sonnet-4
provider: anthropic
family: claude
context_window: 200000
strengths: [code-generation, tool-calling, structured-output, long-context]
---

# Claude Sonnet — Agent Behavior Rules

## Prompt Style
- Use structured sections with clear headers for complex tasks
- Leverage chain-of-thought reasoning for multi-step problems
- Be explicit about assumptions and decisions

## Code Generation
- Write production-quality code with proper error handling
- Use type annotations in Python, TypeScript strict mode conventions
- Prefer explicit over implicit — avoid magic values
- Include docstrings for public APIs

## Tool Calling
- Batch independent tool calls when possible for efficiency
- Verify file existence with `read_file` before writing
- Prefer read-then-modify pattern over blind writes
- Use `search_in_files` to understand context before changes

## Embedded Systems Specifics
- Always reference hardware_manifest.yaml for sensor/SoC specifications
- Verify cross-compile toolchain availability before build commands
- Check kernel config compatibility before driver modifications
