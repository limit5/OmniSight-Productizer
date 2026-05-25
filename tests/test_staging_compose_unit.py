r"""[OP-971] AUDIT-19a — staging compose systemd wrapper + env contract.

Pins the three artifacts that activate the OP-767/878 staging compose
under a managed lifecycle on the 5a host:

  * deploy/systemd/omnisight-staging-compose.service — the user unit:
    wraps `docker compose … up -d --wait`, runs the env-contract guard
    as ExecStartPre, curls /healthz as ExecStartPost, and carries the
    cgroup quota (MemoryMax=30% / CPUQuota=30% / IOWeight=50) so staging
    can't take down prod on the co-tenanted box.
  * infra/staging/verify-env-contract.sh — the guard: refuses to start
    if the staging env points at prod (prod Postgres DSN, sk_live_ Stripe
    key, prod Anthropic key, non-staging Slack webhook, missing
    environment=staging) with a clear EnvContractViolation message.
  * infra/staging/.env.template — placeholders only, no real secrets.
  * docs/operations/staging-environment-runbook.md — the runbook.

The behaviour tests actually execute verify-env-contract.sh against
temp env files, so a regression in the guard logic fails here at PR time
rather than the first time someone fat-fingers .env.local on the host.

Cost: a handful of bash subprocesses, stdlib + pytest only, no docker /
systemd required.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT = REPO_ROOT / "deploy" / "systemd" / "omnisight-staging-compose.service"
VERIFY = REPO_ROOT / "infra" / "staging" / "verify-env-contract.sh"
ENV_TEMPLATE = REPO_ROOT / "infra" / "staging" / ".env.template"
RUNBOOK = REPO_ROOT / "docs" / "operations" / "staging-environment-runbook.md"
GITIGNORE = REPO_ROOT / ".gitignore"

STAGING_COMPOSE_REL = "deploy/staging/docker-compose.yml"


def _service_block(unit_text: str) -> str:
    m = re.search(r"^\[Service\]\s*\n(.*?)(?=^\[|\Z)", unit_text, re.S | re.M)
    assert m, "unit file is missing a [Service] section"
    return m.group(1)


def _directives(block: str, key: str) -> list[str]:
    """All values for `key=...` in a block (a systemd key may repeat)."""
    return re.findall(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)


def _run_verify(tmp_path: Path, env_lines: str, extra_env: dict[str, str] | None = None,
                env_file_env_key: str = "OMNISIGHT_STAGING_ENV_FILE"):
    env_file = tmp_path / ".env"
    env_file.write_text(env_lines)
    env = {
        "PATH": "/usr/bin:/bin",
        env_file_env_key: str(env_file),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(VERIFY)],
        capture_output=True, text=True, env=env, timeout=30,
    )


# A staging env file that passes every contract check.
GOOD_ENV = (
    "environment=staging\n"
    "OMNISIGHT_ENV=staging\n"
    "POSTGRES_DB=omnisight_staging\n"
    "STAGING_POSTGRES_PORT=55432\n"
    "OMNISIGHT_DATABASE_URL=postgresql+asyncpg://omnisight:pw@postgres:5432/omnisight_staging\n"
    "STRIPE_SECRET_KEY=sk_test_abc123\n"
    "ANTHROPIC_API_KEY=sk-ant-test-xyz\n"
    "SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T0/B0/staging-hook\n"
)


# ─────────────────────────────────────────────────────────────────────
# File presence
# ─────────────────────────────────────────────────────────────────────


def test_artifacts_exist():
    for p in (UNIT, VERIFY, ENV_TEMPLATE, RUNBOOK):
        assert p.exists(), f"missing required artifact {p.relative_to(REPO_ROOT)}"
    assert VERIFY.stat().st_mode & 0o111, "verify-env-contract.sh must be executable"
    # script must be syntactically valid bash
    r = subprocess.run(["bash", "-n", str(VERIFY)], capture_output=True, text=True)
    assert r.returncode == 0, f"verify-env-contract.sh bash -n failed: {r.stderr}"


def test_env_template_is_tracked_not_ignored():
    # .gitignore globs `.env*`; the template must be explicitly un-ignored
    # or it can never be committed (ticket "Files touched" lists it as NEW).
    text = GITIGNORE.read_text()
    assert "!infra/staging/.env.template" in text, (
        ".gitignore must negate-ignore infra/staging/.env.template "
        "(it is matched by the `.env*` glob otherwise and can't be committed)"
    )


# ─────────────────────────────────────────────────────────────────────
# systemd unit contract
# ─────────────────────────────────────────────────────────────────────


def test_unit_execstart_brings_up_staging_compose_with_wait():
    block = _service_block(UNIT.read_text())
    starts = _directives(block, "ExecStart")
    assert starts, "unit must declare ExecStart"
    es = starts[0]
    assert "docker compose" in es and STAGING_COMPOSE_REL in es, (
        f"ExecStart must run `docker compose -f …/{STAGING_COMPOSE_REL}`; got {es!r}"
    )
    assert "up -d" in es and "--wait" in es, (
        f"ExecStart must use `up -d --wait` so `systemctl start` blocks on healthchecks; got {es!r}"
    )


def test_unit_execstop_brings_compose_down():
    block = _service_block(UNIT.read_text())
    stops = _directives(block, "ExecStop")
    assert stops, "unit must declare ExecStop"
    assert "docker compose" in stops[0] and "down" in stops[0] and STAGING_COMPOSE_REL in stops[0], (
        f"ExecStop must run `docker compose -f …/{STAGING_COMPOSE_REL} down`; got {stops[0]!r}"
    )


def test_unit_execstartpre_runs_env_contract_guard():
    block = _service_block(UNIT.read_text())
    pres = _directives(block, "ExecStartPre")
    assert any("verify-env-contract.sh" in p for p in pres), (
        f"unit must run infra/staging/verify-env-contract.sh as ExecStartPre; got {pres!r}"
    )


def test_unit_execstartpost_checks_healthz():
    block = _service_block(UNIT.read_text())
    posts = _directives(block, "ExecStartPost")
    assert any("healthz" in p.lower() for p in posts), (
        f"unit must curl the staging /healthz endpoint as ExecStartPost; got {posts!r}"
    )


@pytest.mark.parametrize(
    "key,expected",
    [("MemoryMax", "30%"), ("CPUQuota", "30%"), ("IOWeight", "50")],
)
def test_unit_cgroup_quota(key, expected):
    block = _service_block(UNIT.read_text())
    vals = _directives(block, key)
    assert vals == [expected], (
        f"unit must declare {key}={expected} (staging may never starve prod on the "
        f"co-tenanted host); got {vals!r}"
    )


def test_unit_is_user_level_not_root():
    block = _service_block(UNIT.read_text())
    # user units install under default.target, not multi-user.target, and
    # must not pin User=root (it shares the operator's docker access).
    install = re.search(r"^\[Install\]\s*\n(.*?)(?=^\[|\Z)", UNIT.read_text(), re.S | re.M)
    assert install and "WantedBy=default.target" in install.group(1), (
        "unit must be user-level: [Install] WantedBy=default.target"
    )
    assert "User=root" not in block, "staging unit must not run as root"


def test_unit_exports_environment_staging():
    block = _service_block(UNIT.read_text())
    env_dirs = _directives(block, "Environment")
    joined = " ".join(env_dirs)
    assert "environment=staging" in joined or "OMNISIGHT_ENV=staging" in joined, (
        "unit must export environment=staging so the compose project and the "
        f"contract check agree; got {env_dirs!r}"
    )


def test_unit_documents_cgroup_and_v1_fallback():
    text = UNIT.read_text()
    assert "cgroup v2" in text.lower(), "unit comment must explain the cgroup v2 requirement"
    assert "CgroupV1Fallback" in text, "unit comment must reference the CgroupV1Fallback path"


# ─────────────────────────────────────────────────────────────────────
# verify-env-contract.sh behaviour
# ─────────────────────────────────────────────────────────────────────


def test_contract_passes_on_good_staging_env(tmp_path):
    r = _run_verify(tmp_path, GOOD_ENV, extra_env={"environment": "staging"})
    assert r.returncode == 0, f"good staging env should pass; rc={r.returncode} stderr={r.stderr}"
    assert "env contract OK" in r.stderr


def test_contract_rejects_prod_postgres_dsn(tmp_path):
    bad = "environment=staging\nOMNISIGHT_DATABASE_URL=postgresql+asyncpg://omnisight:pw@pg-primary:5432/omnisight\n"
    r = _run_verify(tmp_path, bad, extra_env={"environment": "staging"})
    assert r.returncode == 1, f"prod DSN must hard-fail; rc={r.returncode}"
    assert "EnvContractViolation" in r.stderr and "staging" in r.stderr.lower()


def test_contract_rejects_missing_environment_marker(tmp_path):
    # GOOD_ENV minus the environment= line, and we don't export it either.
    body = "\n".join(l for l in GOOD_ENV.splitlines() if not l.startswith("environment=")
                      and not l.startswith("OMNISIGHT_ENV=")) + "\n"
    r = _run_verify(tmp_path, body)
    assert r.returncode == 1, f"missing environment=staging must hard-fail; rc={r.returncode}"
    assert "EnvContractViolation" in r.stderr and "environment=staging" in r.stderr


def test_contract_rejects_stripe_live_key(tmp_path):
    bad = GOOD_ENV.replace("sk_test_abc123", "sk_live_realmoney")
    r = _run_verify(tmp_path, bad, extra_env={"environment": "staging"})
    assert r.returncode == 1, f"sk_live_ Stripe key must hard-fail; rc={r.returncode}"
    assert "EnvContractViolation" in r.stderr and "STRIPE_SECRET_KEY" in r.stderr


def test_contract_rejects_prod_anthropic_key(tmp_path):
    bad = GOOD_ENV.replace("ANTHROPIC_API_KEY=sk-ant-test-xyz", "ANTHROPIC_API_KEY=sk-ant-api03-prod-key")
    r = _run_verify(tmp_path, bad, extra_env={"environment": "staging"})
    assert r.returncode == 1, f"prod-looking Anthropic key must hard-fail; rc={r.returncode}"
    assert "EnvContractViolation" in r.stderr and "Anthropic" in r.stderr


def test_contract_rejects_non_staging_slack_webhook(tmp_path):
    bad = GOOD_ENV.replace("staging-hook", "prod-alerts")
    r = _run_verify(tmp_path, bad, extra_env={"environment": "staging"})
    assert r.returncode == 1, f"non-staging Slack webhook must hard-fail; rc={r.returncode}"
    assert "EnvContractViolation" in r.stderr


def test_contract_accepts_staging_port_dsn_without_staging_dbname(tmp_path):
    # DSN whose db name is NOT *-staging but which connects on a staging port.
    env = ("environment=staging\n"
           "OMNISIGHT_DATABASE_URL=postgresql+asyncpg://omnisight:pw@db:55432/omnisight\n")
    r = _run_verify(tmp_path, env, extra_env={"environment": "staging"})
    assert r.returncode == 0, f"staging-port DSN should pass; rc={r.returncode} stderr={r.stderr}"


def test_contract_documents_error_codes(tmp_path):
    text = VERIFY.read_text()
    for code in ("EnvContractViolation", "PortCollisionWithProd", "CgroupV1Fallback"):
        assert code in text, f"verify-env-contract.sh must reference the {code} error code"


# ─────────────────────────────────────────────────────────────────────
# .env.template — placeholders only, no real secrets
# ─────────────────────────────────────────────────────────────────────


def test_env_template_has_no_real_secrets():
    text = ENV_TEMPLATE.read_text()
    assert "environment=staging" in text
    assert "POSTGRES_DB=omnisight_staging" in text
    # Inspect only KEY=VALUE assignment lines (comments may mention sk_live_
    # etc. when explaining what the contract check rejects).
    assignments = dict(
        re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text, re.M)
    )
    stripe = assignments.get("STRIPE_SECRET_KEY", "")
    assert stripe == "" or stripe.startswith("sk_test_"), (
        f"STRIPE_SECRET_KEY in the template must be empty or an sk_test_ placeholder; got {stripe!r}"
    )
    assert not stripe.startswith("sk_live_"), ".env.template must never carry a live Stripe key"
    for k in ("ANTHROPIC_API_KEY", "OMNISIGHT_ANTHROPIC_API_KEY"):
        v = assignments.get(k, "")
        assert not re.match(r"sk-ant-api\d", v), f".env.template must not embed a real Anthropic key in {k}"


# ─────────────────────────────────────────────────────────────────────
# Runbook
# ─────────────────────────────────────────────────────────────────────


def test_runbook_covers_lifecycle_and_error_codes():
    text = RUNBOOK.read_text()
    assert "OP-971" in text
    assert "omnisight-staging-compose" in text
    assert "verify-env-contract.sh" in text
    for needle in ("systemctl --user start", "systemctl --user stop", "/healthz",
                   "EnvContractViolation", "PortCollisionWithProd", "CgroupV1Fallback",
                   "systemd-cgls"):
        assert needle in text, f"runbook must document {needle!r}"


# ─────────────────────────────────────────────────────────────────────
# B1b (OP-1711): overlay-digest gate wiring — the deploy script must export
# the actual running backend digest, and the compose must pass it into BOTH
# backends, so the /readyz overlay-digest gate compares SUBSTANCE not shape.
# ─────────────────────────────────────────────────────────────────────

STAGING_COMPOSE = REPO_ROOT / STAGING_COMPOSE_REL
STAGING_DEPLOY = REPO_ROOT / "scripts" / "staging_deploy.sh"
RUNNING_DIGEST_ENV = "OMNISIGHT_RUNNING_IMAGE_DIGEST_BACKEND"


def _compose_service_env(compose_text: str, service: str) -> list[str]:
    """Return the `environment:` list entries for a top-level compose service."""
    # Grab the service block (from `  <service>:` to the next 2-space key).
    m = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", compose_text, re.S | re.M)
    assert m, f"compose is missing service {service!r}"
    block = m.group(1)
    env_m = re.search(r"^    environment:\n(.*?)(?=^    \S|\Z)", block, re.S | re.M)
    assert env_m, f"service {service!r} has no environment block"
    return re.findall(r"^      - (.+?)\s*$", env_m.group(1), re.M)


@pytest.mark.parametrize("service", ["backend-a", "backend-b"])
def test_b1b_compose_passes_running_digest_to_both_backends(service):
    text = STAGING_COMPOSE.read_text()
    entries = _compose_service_env(text, service)
    matches = [e for e in entries if e.startswith(f"{RUNNING_DIGEST_ENV}=")]
    assert matches, (
        f"{service} env must pass {RUNNING_DIGEST_ENV} so the /readyz overlay-digest "
        f"gate (B1a) has a running-digest to compare; got {entries!r}"
    )
    # Must default-empty (fail-OPEN on the legacy/no-bundle path), never hard-fail bring-up.
    assert matches[0].endswith(":-}") or matches[0].endswith("=${%s:-}" % RUNNING_DIGEST_ENV), (
        f"{service} {RUNNING_DIGEST_ENV} must use an empty default (${{{RUNNING_DIGEST_ENV}:-}}) "
        f"so an un-injected deploy can't brick readiness; got {matches[0]!r}"
    )


def test_b1b_deploy_script_exports_running_digest_from_bundle():
    text = STAGING_DEPLOY.read_text()
    assert f"export {RUNNING_DIGEST_ENV}=" in text, (
        f"staging_deploy.sh must export {RUNNING_DIGEST_ENV} before `compose up`"
    )
    # The exported value must come from the verified candidate bundle's backend
    # digest (the digest verify_pulled_digests proved equal to what was pulled),
    # not a re-stated tag — that is what makes the gate compare real substance.
    assert "bundle_image_digest" in text and 'bundle_image_digest "$CANDIDATE_BUNDLE" backend' in text, (
        f"{RUNNING_DIGEST_ENV} must be sourced from the candidate bundle's backend digest"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
