# mcp_integration

**Purpose**: Declares which remote MCP servers (Figma, Gmail, Google Calendar, Google Drive) an operator has access to and serializes them into the Anthropic Messages API's top-level `mcp_servers=[]` slot. The Anthropic SDK handles tool discovery and dispatch — this module is config + registry only.

**Key types / public surface**:
- `MCPServerConfig` — frozen dataclass for one server; `to_anthropic_payload()` produces the SDK shape.
- `RemoteMCPRegistry` — in-memory per-operator registry; `to_anthropic_mcp_servers(only_names=...)` builds the request payload.
- `build_default_server_config(name, ...)` — factory that pulls URL + description from the static catalog.
- `DEFAULT_REMOTE_MCP_CATALOG` — the 4 claude.ai-managed MCP entries with sample tool names.
- `parse_mcp_tool_name` / `is_mcp_tool` — split `mcp__<server>__<method>` names for dispatch routing.

**Key invariants**:
- `MCPServerConfig.__repr__` redacts `authorization_token`; tokens arrive already-decrypted from the AS Token Vault, never logged.
- `name` must match the prefix Anthropic emits in tool_use blocks (`mcp__<name>__<method>`) — the registry key and the wire identifier are the same string.
- `to_anthropic_mcp_servers()` sorts output by name for deterministic test/log diffs and silently drops disabled servers.
- Registry is in-memory only by design; PG-backed storage and dynamic tool schema mirroring are deliberately deferred (see module docstring).

**Cross-module touchpoints**:
- Standalone — no backend imports. Docstring references `backend/security/oauth_client.py` (AS.1.x) as the future source of decrypted tokens passed in by callers.
- Intended consumer is the Anthropic Messages API caller (agent dispatch layer); not visible from this file which exact module wires the registry into requests.
