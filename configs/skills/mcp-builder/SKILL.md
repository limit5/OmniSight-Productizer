---
name: mcp-builder
description: Build MCP (Model Context Protocol) servers for extending tool capabilities. Use when tasks mention MCP, tool integration, external service connector, or API bridge.
keywords: [mcp, model-context-protocol, tool, server, integration, api, connector, bridge]
---

# MCP Server Development

Build MCP servers that enable LLM agents to interact with external services.

## Workflow

### Phase 1: Research & Planning
- Understand the target API/service documentation
- Design tool names with clear, descriptive prefixes
- Plan input/output schemas (use Pydantic for Python, Zod for TypeScript)
- Decide: stdio (local) or streamable HTTP (remote)

### Phase 2: Implementation
- Establish project structure with proper entry point
- Build core infrastructure: API client, error handling, pagination
- Implement tools with comprehensive input validation
- Error messages should guide agents toward solutions

### Phase 3: Testing
- Verify each tool individually with sample inputs
- Test error paths: invalid inputs, network failures, auth errors
- Validate JSON schema compliance

### Phase 4: Integration
- Register MCP server in project configuration
- Test end-to-end with LLM agent invocation
- Document available tools and their parameters

## Key Principles
- Prefer comprehensive API coverage over high-level workflow tools
- Error messages must include actionable next steps
- Use stateless JSON with streamable HTTP for remote servers
- Use stdio for local implementations
- TypeScript has superior SDK support; Python via `mcp` package

## Project Template
```
my-mcp-server/
├── src/
│   ├── index.ts        # Entry point
│   ├── tools/          # Tool implementations
│   └── client.ts       # API client wrapper
├── package.json
└── README.md
```
