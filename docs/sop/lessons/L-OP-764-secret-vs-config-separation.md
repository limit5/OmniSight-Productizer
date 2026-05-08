---
id: L-OP-764
ticket: OP-764
title: Keep secrets and non-secret config in separate stores from day one
date: 2026-05-08
tags: [config, secrets, deploy]
---

# Keep secrets and non-secret config in separate stores from day one

**Situation**: Pre-OP-764, every OmniSight host carried a hand-edited
``.env`` that mixed feature flags, hostnames, and timeouts (legitimate
config) with API keys, OAuth client secrets, webhook HMAC secrets, and
the bootstrap admin password (real secrets). Deploying a new staging
host meant ``scp .env`` from prod, hand-edit the few values that
differed, ship. That pattern (1) put plaintext secrets on every host,
(2) silently drifted between environments because each host's ``.env``
was edited independently, and (3) gave us no audit trail when a key
rotated.

**Fix**: OP-764 split the world into two pipes that meet at boot:

  * Non-secret config lives in ``config/<env>.yaml``, checked into the
    repo. Reviewers see every env-specific knob in PRs.
  * Secrets live in a vault — Fernet-encrypted file or HashiCorp Vault
    — fronted by ``backend.secrets_provider`` with a single
    :data:`SECRET_FIELDS` registry that anchors the split.
  * ``backend.config._apply_env_overlay()`` runs at module import,
    BEFORE ``Settings()``, and writes both halves into ``os.environ``
    only for keys not already set so a deploy ``--override`` still
    wins. The overlay refuses to apply a YAML entry whose key is in
    ``SECRET_FIELDS`` — accidental commits never reach Settings.

**Verification**:
``backend/tests/test_secrets_provider.py`` pins SECRET_FIELDS membership
and the file-vault round-trip + encryption canary;
``backend/tests/test_config_multi_env_overlay.py::test_synthetic_staging_spinup_no_dotenv_required``
is the headline AC: a fresh process with only ``OMNISIGHT_ENV=staging``
plus a populated vault produces a fully-configured Settings without any
``.env`` involvement;
``backend/tests/test_migrate_dotenv_to_vault.py`` covers the legacy
``.env`` → vault + yaml round-trip including the conflict-without-force
refusal path.

**Generalisation**: When a single store mixes secrets with feature
flags, every operation on the store has the same blast radius as a
secret rotation. Split them at the lowest layer that touches both —
config loading — and pin the split in a registry that callers consult
instead of re-deciding. The registry is the load-bearing piece: a
new Settings field that carries a credential MUST land both as a
class attribute and as a SECRET_FIELDS entry, or the next migration
will silently leak it into checked-in YAML.
