---
id: L-OP-693
ticket: OP-693
title: `echo >>` to .env without trailing-newline guard concatenates with last token
date: 2026-05-07
tags: [gerrit, runner]
legacy_lesson: 20
---

# `echo >>` to .env without trailing-newline guard concatenates with last token

**Situation**: OP-693 SP-D added `OMNISIGHT_GERRIT_ENABLED=true` to .env via `echo "..." >> .env`. The .env's last line (`OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN=eyJ...`) lacked a trailing newline, so `echo` appended directly to that line: the resulting line read `...OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN=eyJ...gAzOMNISIGHT_GERRIT_ENABLED=true`. Both vars corrupted: cloudflared's running container kept a copy of the pre-edit token in memory (didn't re-read .env) and stayed alive, but a subsequent restart would've broken cloudflared. Backend rolling restart picked up the corrupted .env on the env_file load and silently misread BOTH values.

**Fix**: Always check + ensure trailing newline before appending to .env / config files via `echo >>`. Pattern:
```bash
[ -z "$(tail -c 1 .env)" ] || echo "" >> .env  # add newline if last char isn't already
printf '%s=%s\n' "$KEY" "$VALUE" >> .env
```

Or use Python's pathlib for safer text editing: `text = p.read_text(); if not text.endswith('\n'): text += '\n'; text += new_line + '\n'; p.write_text(text)`.

**Verification**: Repaired with regex split — `re.compile(r'^(OMNISIGHT_CLOUDFLARE_TUNNEL_TOKEN=[A-Za-z0-9+/=]+)(OMNISIGHT_GERRIT_ENABLED=true)$', re.M).sub(r'\1\n\2\n', text)`. Backend restart re-read corrected .env; cloudflared restart_count stayed 0 (its token was loaded at startup before .env was touched, and the in-memory copy is not affected by re-reads); subsequent restart picked up the fixed token.

**Generalisation**: Any tool that appends to a config file via shell redirection MUST guard against missing trailing newlines. Production secrets in .env are particularly risky because a corrupted long token (like a JWT or OAuth token) can produce VERY hard-to-debug failures (the whole token becomes one giant invalid string, which fails parsing in different layers depending on consumer).
