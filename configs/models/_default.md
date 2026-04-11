---
model_id: default
provider: any
family: generic
context_window: 128000
strengths: [general-purpose]
---

# Default Agent Behavior Rules

## General Guidelines
- Read existing files before making modifications
- Use tools to verify assumptions rather than guessing
- Commit changes with descriptive messages that explain the "why"
- Check for errors in tool output before proceeding
- When uncertain, ask for clarification rather than making assumptions

## Code Generation
- Follow existing code conventions in the project
- Include error handling for external operations (I/O, network, subprocess)
- Prefer modifying existing files over creating new ones

## Tool Usage
- Always read a file before writing to it
- Run `git status` before committing to verify staged changes
- Use `search_in_files` to find relevant code before modifying
- Check command exit codes in bash output
