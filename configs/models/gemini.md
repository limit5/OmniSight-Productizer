---
model_id: gemini
provider: google
family: gemini
context_window: 1000000
strengths: [long-context, multimodal, code-generation, reasoning]
---

# Gemini Series — Agent Behavior Rules

## Prompt Style
- Leverage long context window for comprehensive file analysis
- Use structured output formats when returning data
- Be explicit about reasoning steps

## Code Generation
- Follow project conventions discovered through file reading
- Test generated code paths with available tools
- Prefer incremental modifications over full rewrites

## Tool Calling
- Can process large codebases in context
- Read multiple files to understand full dependency graph
- Use search extensively before making changes

## Considerations
- Strong at multimodal tasks (image analysis, diagram understanding)
- Gemini Thinking variant: allow extended reasoning for complex problems
- Gemini Fast variant: optimize for speed on straightforward tasks
