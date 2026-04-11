---
model_id: gpt
provider: openai
family: gpt
context_window: 128000
strengths: [code-generation, instruction-following, function-calling]
---

# GPT Series — Agent Behavior Rules

## Prompt Style
- Use clear markdown structure with numbered steps
- Be specific and unambiguous in instructions
- Break complex tasks into explicit sequential steps

## Code Generation
- Follow language-specific best practices
- Include type hints and JSDoc/docstrings
- Test edge cases explicitly

## Tool Calling
- Use function calling format for tool invocations
- Validate parameters before calling tools
- Handle tool errors gracefully with retry logic

## Considerations
- Verify output completeness — may truncate long responses
- For large files, work in targeted sections rather than full rewrites
