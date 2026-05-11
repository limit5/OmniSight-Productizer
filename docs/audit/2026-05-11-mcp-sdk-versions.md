# MCP SDK Security Audit — OP-846

**Date**: 2026-05-11
**Scope**: OmniSight-Productizer plus sister checkout `../omnisight-ai-core`
**Question**: Are any local MCP consumers pinned to an official MCP SDK version affected by the April 2026 MCP STDIO RCE disclosure?

## Operator Brief

- **Official MCP SDK exposure: no pinned vulnerable SDK found.** Required manifest grep found no `mcp`, `model-context-protocol`, or `@modelcontextprotocol` SDK dependency in OmniSight-Productizer or `../omnisight-ai-core`.
- **Runtime MCP exposure still exists outside SDK pins.** OmniSight has URL-based Anthropic MCP forwarding and one local STDIO launcher for `android-skills-mcp`; the local launcher is an execution surface if its registry command/package source is compromised.
- **Worst case**: attacker-controlled MCP STDIO configuration or package resolution can execute host commands under the backend worker account; current evidence does not show a pinned vulnerable official SDK requiring a bump ticket.

## Sources Checked

- The Hacker News / OX disclosure summary: <https://thehackernews.com/2026/04/anthropic-mcp-design-vulnerability.html> reported that the flaw is architectural, affects official MCP SDKs across Python, TypeScript, Java, and Rust, and enables arbitrary command execution via MCP STDIO configuration.
- Cloud Security Alliance research note, 2026-04-23: <https://labs.cloudsecurityalliance.org/research/csa-research-note-mcp-rce-design-vulnerability-20260423-csa/> describes MCP STDIO command configuration as the trust boundary and recommends auditing all deployed STDIO entries.
- GitHub Advisory Database `GHSA-345p-7cg4-v4c7` / `CVE-2026-25536`: <https://github.com/advisories/GHSA-345p-7cg4-v4c7> lists npm `@modelcontextprotocol/sdk` affected `>=1.10.0, <=1.25.3`, patched in `1.26.0`.
- Aikido Intel `AIKIDO-2026-10472`: <https://intel.aikido.dev/cve/AIKIDO-2026-10472> lists Python `mcp` example-code command injection range `1.23.0 - 1.26.0`, fixed in `1.27.0`; pre-CVE at audit time.

## Commands Run

```bash
grep -RInE 'mcp|model-context-protocol|@modelcontextprotocol|modelcontextprotocol' \
  package.json pyproject.toml docs-site/requirements.txt \
  backend/requirements.in backend/requirements.txt \
  packages/omnisight-vite-plugin/package.json \
  backend/requirements-dev.txt backend/requirements-dev.in

find ../omnisight-ai-core -path '*/.git' -prune -o -path '*/redis_data' -prune -o \
  -type f \( -name 'requirements*.txt' -o -name 'requirements*.in' \
  -o -name 'pyproject.toml' -o -name 'package*.json' \) -print

find ../omnisight-ai-core -path '*/.git' -prune -o -path '*/redis_data' -prune -o \
  -type f -print0 | xargs -0 grep -RInE \
  'mcp|model-context-protocol|@modelcontextprotocol|modelcontextprotocol'

grep -RInE 'mcp|model-context-protocol|@modelcontextprotocol|modelcontextprotocol' \
  pnpm-lock.yaml package.json pyproject.toml backend/requirements.txt \
  backend/requirements.in backend/requirements-dev.txt backend/requirements-dev.in \
  docs-site/requirements.txt packages/omnisight-vite-plugin/package.json \
  configs/mcp_servers.json

python3 -m pip_audit -r backend/requirements.txt
pnpm audit --json
```

## SDK Version Inventory

| Repository | File | Pinned MCP SDK version | Known-vulnerable range? | Patch version available? | Verdict |
|---|---|---:|---|---|---|
| OmniSight-Productizer | `backend/requirements.in` | none | no | n/a | Verified no official Python `mcp` SDK pin. |
| OmniSight-Productizer | `backend/requirements.txt` | none | no | n/a | Verified no official Python `mcp` SDK pin; `anthropic==0.97.0` is present but is not `modelcontextprotocol/python-sdk`. |
| OmniSight-Productizer | `backend/requirements-dev.in` | none | no | n/a | No MCP dependency line. |
| OmniSight-Productizer | `backend/requirements-dev.txt` | none | no | n/a | No MCP dependency line. |
| OmniSight-Productizer | `docs-site/requirements.txt` | none | no | n/a | No MCP dependency line. |
| OmniSight-Productizer | `pyproject.toml` | none | no | n/a | No MCP dependency line. |
| OmniSight-Productizer | `package.json` | none | no | n/a | No `@modelcontextprotocol/sdk` dependency. |
| OmniSight-Productizer | `packages/omnisight-vite-plugin/package.json` | none | no | n/a | No `@modelcontextprotocol/sdk` dependency. |
| OmniSight-Productizer | `pnpm-lock.yaml` | none | no | n/a | No transitive `@modelcontextprotocol/sdk` lock entry found. |
| `../omnisight-ai-core` | `requirements*.txt`, `requirements*.in`, `pyproject.toml`, `package*.json` | none | no | n/a | No matching manifest files in tracked checkout. |

## Runtime MCP Consumers

| Surface | Evidence | Transport | SDK pin? | Exposure verdict |
|---|---|---|---|---|
| Remote managed MCP registry | `backend/agents/mcp_integration.py:1-30`, `:132-202`, `:281-360` | URL / Anthropic Messages `mcp_servers=[]` | none local | No local official MCP SDK version to bump. Risk shifts to Anthropic SDK behavior and remote managed MCP services. |
| JIRA MCP wrapper | `backend/agents/mcp_integration.py:180-202` | URL, operator-overridable `OMNISIGHT_MCP_JIRA_URL` | none local | No local official MCP SDK version to bump. Keep writes in `jira_dispatch` as already enforced by OP-813. |
| Local Android skills MCP | `configs/mcp_servers.json:1-11`, `backend/agents/tools.py:1922-2001`, `:2088-2136` | STDIO subprocess via `npx -y android-skills-mcp` | none local | Not an official SDK pin, but it is an unpinned STDIO package launch. Treat as a supply-chain execution surface, not a CVE-confirmed SDK-version exposure. |
| `../omnisight-ai-core` gateway | `git ls-files` plus manifest/content grep | none found | none | The sister checkout has no dependency manifest and no MCP/model-context-protocol text match in tracked non-binary files. |

## Vulnerable-Version Check

No local dependency matches the known official SDK vulnerable ranges:

| Advisory | Package | Vulnerable range | Fixed version | Local match |
|---|---|---:|---:|---|
| `CVE-2026-25536` / `GHSA-345p-7cg4-v4c7` | npm `@modelcontextprotocol/sdk` | `>=1.10.0, <=1.25.3` | `1.26.0` | none |
| `AIKIDO-2026-10472` pre-CVE | PyPI `mcp` | `1.23.0 - 1.26.0` | `1.27.0` | none |
| April 2026 OX / Hacker News disclosure | official MCP SDK pattern across Python / TypeScript / Java / Rust | architectural STDIO trust-boundary flaw | no protocol-level universal patch reported | no pinned official SDK; one local STDIO package launch remains a non-SDK exposure surface |

SCA tooling did not report MCP SDK advisories:

- `python3 -m pip_audit -r backend/requirements.txt` completed and reported three non-MCP findings: `langchain-core` `CVE-2026-44843`, `mako` `CVE-2026-44307`, `python-multipart` `CVE-2026-42561`.
- `pnpm audit --json` completed and reported two non-MCP findings: `next` `GHSA-q4gf-8mx6-v5v3`, `postcss` `GHSA-qx2v-qp2m-jg93` / `CVE-2026-41305`.

## Remediation Plan

### Vulnerable Consumers

None found for official MCP SDK packages. No "[bump MCP SDK on X]" follow-up ticket is required from the version inventory.

### Safe Consumers

- `backend/agents/mcp_integration.py`: verified safe from the specific official MCP SDK version-bump requirement because it does not import or pin `mcp` / `@modelcontextprotocol/sdk`; it serializes URL MCP server descriptors for the existing Anthropic SDK.
- `../omnisight-ai-core`: verified safe from the specific official MCP SDK version-bump requirement because no MCP dependency manifest or MCP source text was found in the tracked checkout.

### Non-SDK Risk To Track Separately

`configs/mcp_servers.json` launches `npx -y android-skills-mcp` without a package version pin. This is not the official SDK CVE exposure requested by OP-846, but it is the kind of STDIO command/package execution surface highlighted by the April 2026 disclosure. Recommended separate follow-up: pin or vendor-review `android-skills-mcp`, require an allowlisted package/version, and document rollback as "disable the `android_skill_search` tool or remove the `android-skills` registry entry."

## Final Verdict

OmniSight-Productizer and `../omnisight-ai-core` do not currently pin a vulnerable official MCP SDK version. The audit closes with no MCP SDK bump ticket required. The remaining security concern is local STDIO package execution via `android-skills-mcp`, which should be handled as a separate MCP server supply-chain hardening ticket rather than an SDK-version remediation.
