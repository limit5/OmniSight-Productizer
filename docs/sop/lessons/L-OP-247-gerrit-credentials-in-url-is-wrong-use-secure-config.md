---
id: L-OP-247
ticket: OP-247
title: Gerrit credentials in URL is wrong; use `secure.config`
date: 2026-05-06
tags: [gerrit, git, runner]
legacy_lesson: 13
---

# Gerrit credentials in URL is wrong; use `secure.config`

**Situation**: Same OP-247 wire-up. My first replication.config put credentials inline in the URL: `https://oauth2:${GITLAB_TOKEN}@sora.services:...`. Even if `${GITLAB_TOKEN}` HAD substituted, this is **the wrong place**:
- URLs in `replication.config` are stored in the world-readable section
- `replication list --detail` (which any project member can run) shows the URL — exposing the token
- The "blessed" path is `etc/secure.config` which Gerrit reads with restricted permissions

**Fix**: Plain URL in `replication.config`, credentials in `etc/secure.config`:
```ini
# secure.config (chmod 600 typical)
[remote "gitlab-mirror"]
  username = oauth2
  password = glpat-xxxxxxxxxxxxxxxx
```
Gerrit auto-pairs the `[remote "gitlab-mirror"]` blocks across the two files.

**Verification**: After moving creds to secure.config, `replication list --detail` shows the URL without any password leak. Replication still works because Gerrit uses secure.config username + password as HTTP basic auth.

**Generalisation**: Credentials live in **dedicated secrets files** (per the tool's documented contract), never in URLs or general config. URL-inline auth is convenient for quick scripts but a leak vector in any config that gets `cat`'d or queried via API.
