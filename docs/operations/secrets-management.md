# Secrets management & multi-environment config (OP-764 D3)

This runbook covers the post-OP-764 way OmniSight backends pick up
configuration: per-environment YAML for non-secret defaults plus a
pluggable secrets provider for everything that would compromise an
external system if leaked.

## Why we changed the model

Pre-OP-764, every host carried a hand-edited `.env` file copied from
prod. That gave us:

- Plaintext secrets on every dev / staging / prod host.
- Drift — a knob added to prod's `.env` rarely landed on staging.
- No audit trail — who rotated `OMNISIGHT_ANTHROPIC_API_KEY` last week?

Post-OP-764:

- **Single source of truth** for non-secrets is `config/<env>.yaml`,
  checked into the repo. Reviewers see every env-specific knob in PRs.
- **Single source of truth** for secrets is the vault (file-backed
  Fernet store *or* HashiCorp Vault / AWS SSM / sealed-secrets via
  the same provider interface).
- `.env` is **bypassed** when the new system has a value — see the
  precedence rules below — but is preserved as a transitional
  fallback so phased migrations don't break.

## Resolution order at boot

`backend.config._apply_env_overlay()` runs BEFORE
`Settings()` is instantiated. It writes into `os.environ` only when
the var is **not already set**. Pydantic-settings then reads
`os.environ` at the highest priority.

Final precedence (lowest → highest):

1. `Settings` field default (last-resort fallback).
2. Vault secret value (when `OMNISIGHT_SECRETS_BACKEND` ≠ `env`).
3. `config/<env>.yaml` value for non-secret fields.
4. `.env` value (legacy; overridden by the overlay above).
5. Process environment — shell, `docker --env`, deploy `--override`.

The overlay refuses to apply a YAML entry whose key is in
`backend.secrets_provider.SECRET_FIELDS`. That's the central
guard-rail: a secret accidentally committed to YAML is logged at
`ERROR` and ignored — it never reaches `Settings`.

## File layout

```
config/
├── dev.yaml         # local dev defaults (debug=true, runc, …)
├── staging.yaml     # staging fleet (auth=strict, runsc, low budget)
└── prod.yaml        # production fleet (auth=strict, runsc, real budget)

backend/secrets_provider.py
                     # SECRET_FIELDS registry + provider impls

scripts/migrate_dotenv_to_vault.py
                     # one-shot legacy .env → vault + yaml split

data/secrets.vault   # Fernet-encrypted JSON dict (file backend only)
```

## Selecting an environment

The deploy / runtime entry points pass `OMNISIGHT_ENV`:

```sh
OMNISIGHT_ENV=staging python -m uvicorn backend.main:app
```

`OMNISIGHT_ENV=production` is treated as an alias for `prod` so
existing prod hosts that set the longer name continue to work
without renaming `config/prod.yaml`.

For one-shot per-deploy overrides:

```sh
OMNISIGHT_ENV=staging \
  OMNISIGHT_FRONTEND_ORIGIN=https://staging.example.com \
  python -m uvicorn backend.main:app
```

A real env var always wins over both YAML and vault, so an operator
testing a config change can override anything for one boot without
editing files. Wrapping that pattern in your deploy CLI:

```sh
deploy.sh --env staging --override OMNISIGHT_FRONTEND_ORIGIN=https://staging.example.com
```

…just translates to the env-var form above.

## Selecting a secrets backend

`OMNISIGHT_SECRETS_BACKEND` chooses the provider. Default is `env`
(legacy behaviour: read from `os.environ`).

| Backend | Why pick it | Required env |
|---------|-------------|--------------|
| `env`   | Legacy, transitional | none |
| `file`  | Single host, air-gap, dev | `OMNISIGHT_SECRETS_FILE` (default `data/secrets.vault`) |
| `hvac`  | HashiCorp Vault | `OMNISIGHT_VAULT_ADDR`, `OMNISIGHT_VAULT_TOKEN` (or `VAULT_TOKEN`), optional `OMNISIGHT_VAULT_MOUNT`/`OMNISIGHT_VAULT_PATH` |

Adding a new backend (AWS SSM, sealed-secrets, GCP Secret Manager,
…) is a single subclass of `SecretsProvider` plus a branch in
`make_secrets_provider`. Keep the read interface narrow — `get` and
`list_keys` is enough for the boot overlay.

## Migrating an existing host

```sh
# 1. Pick a backend on the new host.
export OMNISIGHT_SECRETS_BACKEND=file
export OMNISIGHT_SECRETS_FILE=/var/lib/omnisight/secrets.vault

# 2. Dry-run to see the planned split.
python scripts/migrate_dotenv_to_vault.py \
    --dotenv /etc/omnisight/.env \
    --env staging \
    --dry-run --verbose

# 3. Run it for real.
python scripts/migrate_dotenv_to_vault.py \
    --dotenv /etc/omnisight/.env \
    --env staging

# 4. Verify the backend boots without the .env.
mv /etc/omnisight/.env /etc/omnisight/.env.pre-op764
OMNISIGHT_ENV=staging python -m uvicorn backend.main:app
```

The migration script:

- Refuses to overwrite a non-empty existing YAML value or vault row
  whose value differs unless `--force` is passed.
- Round-trips every secret it writes (read-back-and-compare) and
  exits non-zero on mismatch — catches a misconfigured Fernet key
  or Vault permission error before the deploy proceeds.
- Never deletes the source `.env` — operators verify the split
  first, then remove the file by hand.

## Adding a new secret

When you introduce a new `Settings` field that carries a credential:

1. Add the field on `backend.config.Settings` as you normally would.
2. Add the snake_case name to
   `backend.secrets_provider.SECRET_FIELDS`.
3. Do **not** add a default in `config/*.yaml` — secrets are vault-only.
4. Document the rotation drill in your team's runbook.

The migration script and the boot overlay both consult the registry,
so a missing entry silently leaks the secret into the next yaml
migration. Step 2 is the load-bearing step.

## Rotation drill (file backend)

```sh
python -c '
from backend.secrets_provider import FileSecretsProvider
p = FileSecretsProvider()
p.put("anthropic_api_key", "sk-ant-NEW...")
'
systemctl restart omnisight-backend
```

The vault file is Fernet-encrypted with the same key as
`backend.secret_store` — rotating that key requires re-encrypting
the vault payload (decrypt with the old key, write with the new one).

## Rotation drill (HashiCorp Vault)

```sh
vault kv patch secret/omnisight/staging anthropic_api_key="sk-ant-NEW..."
systemctl restart omnisight-backend
```

The boot overlay re-reads on every process start; there is no live
hot-reload — that is intentionally out of scope. Operators bounce
the process for a deterministic cut-over.
