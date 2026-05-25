#!/usr/bin/env bash
# scripts/deploy_line_parity.sh — [OP-1720] declarative cross-stage parity audit.
#
# PHASE 1 = declarative parity only. This is the standing, re-runnable
# successor to the 2026-05-25 deploy-line deep audit (OP-1709): instead of a
# periodic hand-audit it emits the STAGE-PARITY drift class (deep-audit #13
# staging .env=latest, #19 dev topology != staging, #23 staging placeholder
# digests, #34 staging PG co-tenant, the OP-1711/1719 prod-compose drift) as a
# green/red table + JSONL automatically, so cross-stage drift is caught at
# *promotion preflight* time rather than at the next major version push.
#
# It is normally invoked through the single entrypoint
#   scripts/deployment-audit.sh --cross-stage-parity [--live]
# (this file is the internal sibling it shells out to; it is also safe to run
# directly).
#
# STATIC-FIRST: every extractor reads committed repo artefacts (compose / .env
# / env-lock / systemd unit files). An OPTIONAL best-effort `--live` ANNOTATES
# rows from /readyz + /api/version + systemctl where reachable; live data is
# best-effort only — unreachable => the row is annotated `live=unknown`, NEVER a
# hard failure.
#
# Each dimension below is an EXACT extractor + an EXACT direction-aware verdict
# (prod/canary strictest, dev loosest). A WRONG-DIRECTION drift is a violation
# (RED) and makes this script exit non-zero, so it is usable both as a
# promotion-preflight gate AND an on-demand finding-generator. A right-direction
# difference (e.g. dev AUTH_MODE=open) is OK.
#
# MUST NOT (hard guardrails, per the OP-1720 ticket): REPORT ONLY — never
# remediate, never mutate compose/deploy behaviour (reads only), never create or
# materialise stages, never touch release-train semantics. Forward-compat /
# candidate-bundle preflight is PHASE 2 (the reserved JSONL `check_family`
# value `candidate_compat`; this phase implements only `parity`).
#
# Usage:
#   scripts/deploy_line_parity.sh                  # built-in stage config, real repo
#   scripts/deploy_line_parity.sh --live           # + best-effort live annotation
#   scripts/deploy_line_parity.sh --config FILE --root DIR   # fixture-driven (tests)
#   DEPLOY_PARITY_JSONL_LOG=/path/parity.jsonl scripts/deploy_line_parity.sh
#
# Config (TSV; `#` comments + blank lines ignored) — one row per stage:
#   <stage>  <compose>  <env_file|->  <lock|->  <gate_units(comma)|->  [live_base_url|-]
#   paths are resolved relative to --root (default: repo root).
#
# Exit: 0 = no wrong-direction violation · 1 = >=1 violation · 2 = bad usage.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec python3 - --repo "$REPO" "$@" <<'PYEOF'
import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone

try:
    import yaml  # PyYAML is present in the repo toolchain (see scripts using it).
except Exception:  # pragma: no cover - defensive
    sys.stderr.write("deploy-line-parity: PyYAML required to parse compose files\n")
    sys.exit(2)

ZERO_DIGEST = "sha256:" + ("0" * 64)
# Stage strictness rank — prod/canary strictest, dev loosest. The direction of
# every verdict is encoded against this rank.
RANK = {"dev": 1, "staging": 2, "canary": 3, "prod": 4}
APP_PREFIXES = ("backend", "frontend", "dag-executor", "bridge-daemon")

# ── built-in stage config (real repo artefacts) ──────────────────────────────
# canary shares prod's compose by design (ADR-0040 RT: canary == prod backend-a,
# weighted at the proxy); its distinguishing artefacts are the canary env-lock +
# canary gate units. staging maps to the systemd-wired, overlay-locked,
# release-train compose (deploy/staging), NOT the older ghcr-default root one.
BUILTIN_CONFIG = """\
dev\tdeploy/dev/docker-compose.yml\tdeploy/dev/.env.example\t-\t-\thttp://127.0.0.1:8020
staging\tdeploy/staging/docker-compose.yml\t-\tstaging.env.lock.json\tstaging-gate-canary.timer,staging-gate-smoke.timer\thttp://localhost:8010
canary\tdocker-compose.prod.yml\t.env.example\tcanary.env.lock.json\tomnisight-canary.timer,omnisight-canary-alert.service\thttp://localhost:8000
prod\tdocker-compose.prod.yml\t.env.example\tprod.env.lock.json\tomnisight-slo-monitor.service,omnisight-smoke-test.timer\thttp://localhost:8000
"""


def parse_args():
    ap = argparse.ArgumentParser(prog="deploy_line_parity.sh", add_help=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--root", default=None,
                    help="root for resolving config-relative paths (default: repo root)")
    ap.add_argument("--config", default=None,
                    help="TSV stage config (default: built-in real-repo mapping)")
    ap.add_argument("--systemd-dir", default="deploy/systemd",
                    help="dir (under root) holding gate unit files")
    ap.add_argument("--live", action="store_true",
                    help="best-effort live annotation (/readyz, /api/version, systemctl)")
    return ap.parse_args()


# ── config + artefact loading ─────────────────────────────────────────────────
def read_config(args):
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = BUILTIN_CONFIG
    stages = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        parts = [p.strip() for p in parts]
        while len(parts) < 6:
            parts.append("-")
        stage, compose, env_file, lock, gate_units, live_url = parts[:6]
        stages.append({
            "stage": stage,
            "compose": none_if_dash(compose),
            "env_file": none_if_dash(env_file),
            "lock": none_if_dash(lock),
            "gate_units": [] if gate_units in ("-", "") else gate_units.split(","),
            "live_url": none_if_dash(live_url),
        })
    return stages


def none_if_dash(v):
    return None if v in ("-", "", None) else v


def resolve(root, rel):
    if rel is None:
        return None
    if os.path.isabs(rel):
        return rel
    return os.path.join(root, rel)


def load_env_file(path):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                out[key] = val
    return out


def load_lock(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def norm_env(env):
    """Normalise a compose service `environment:` (list OR mapping) to a dict."""
    out = {}
    if isinstance(env, dict):
        for k, v in env.items():
            out[str(k)] = "" if v is None else str(v)
    elif isinstance(env, list):
        for item in env:
            if not isinstance(item, str):
                continue
            k, _, v = item.partition("=")
            out[k.strip()] = v
    return out


def load_compose(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:
        return None
    services = {}
    for name, svc in (doc.get("services") or {}).items():
        svc = svc or {}
        services[name] = {
            "image": str(svc.get("image", "")),
            "env": norm_env(svc.get("environment")),
            "env_file": svc.get("env_file"),
            "secrets": svc.get("secrets"),
            "build": svc.get("build"),
        }
    return {"services": services, "root_secrets": doc.get("secrets")}


# ── image-ref analysis ────────────────────────────────────────────────────────
def registry_kind(image):
    """GHCR / gitlab-cr / required(fail-closed) / unknown — from the image ref."""
    if "ghcr.io" in image:
        return "ghcr"
    if "sora.services" in image:
        return "gitlab-cr"
    if "registry required" in image or "OMNISIGHT_REGISTRY:?" in image:
        return "required"
    return "unknown"


def image_pin_kind(image, env_tag, lock_digest):
    """POSITIVE-evidence pin classification.

    Returns one of: mutable | digest | tag-immutable | unresolved.
    We only assert `mutable` (a violation candidate) on POSITIVE evidence of a
    floating tag — an unresolvable `${OMNISIGHT_IMAGE_TAG:?...}` is `unresolved`,
    never assumed mutable, so the audit never false-flags a fail-closed ref.
    """
    if ":latest" in image:
        return "mutable"
    uses_tag_var = "${OMNISIGHT_IMAGE_TAG" in image
    if uses_tag_var and env_tag is not None:
        if env_tag == "latest":
            return "mutable"
        if re.match(r"^v?\d+\.\d+", env_tag):
            return "tag-immutable"
        if env_tag:
            return "tag-other"
    if re.search(r"@sha256:[0-9a-f]{64}", image) and ZERO_DIGEST not in image:
        return "digest"
    if lock_digest and lock_digest != ZERO_DIGEST and re.match(r"^sha256:[0-9a-f]{64}$", lock_digest):
        # lock carries a real (non-placeholder) digest and the ref is digest-capable
        if "@${" in image or "_IMAGE_REF" in image or "_DIGEST" in image:
            return "digest"
    return "unresolved"


def primary_backend(services):
    for n in ("backend-a", "backend"):
        if n in services:
            return n
    for n in services:
        if n.startswith("backend"):
            return n
    return None


def lock_backend_digest(lock):
    try:
        return lock["images"]["backend"]["digest"]
    except Exception:
        return None


def lock_is_placeholder(lock):
    if not lock:
        return True
    bid = str(lock.get("bundle_id", ""))
    if bid.startswith("develop-0000") or bid.endswith("0000000000000000"):
        return True
    digs = []
    for img in (lock.get("images") or {}).values():
        if isinstance(img, dict):
            digs.append(img.get("digest"))
    return all((d == ZERO_DIGEST or not d) for d in digs)


# ── verdict engine ────────────────────────────────────────────────────────────
class Rows:
    def __init__(self):
        self.rows = []
        self.fatal = 0

    def add(self, stage, dimension, status, direction, detail):
        self.rows.append({
            "stage": stage,
            "dimension": dimension,
            "status": status,
            "direction": direction,
            "check_family": "parity",  # phase 2 reserves `candidate_compat`
            "detail": detail,
        })
        if status == "RED":
            self.fatal += 1
        return self.rows[-1]


def evaluate_stage(rows, sc, root, systemd_dir):
    stage = sc["stage"]
    rank = RANK.get(stage, 0)
    compose_path = resolve(root, sc["compose"])
    compose = load_compose(compose_path)

    if compose is None:
        rows.add(stage, "stage-presence", "INFO", "n/a",
                 f"stage=not-materialised (no compose at {sc['compose']}) — informational, not a violation unless ADR-0040 requires this stage")
        return

    services = compose["services"]
    env_file = load_env_file(resolve(root, sc["env_file"]))
    lock = load_lock(resolve(root, sc["lock"]))
    be_name = primary_backend(services)
    be = services.get(be_name, {}) if be_name else {}
    fe = services.get("frontend", {})
    be_env = be.get("env", {})
    env_tag = env_file.get("OMNISIGHT_IMAGE_TAG")

    def envget(key):
        if key in be_env:
            return be_env[key]
        return env_file.get(key)

    lock_be_digest = lock_backend_digest(lock)

    # 1. registry + namespace (ADR-0042: GitLab CR is the SOLE registry; GHCR
    #    decommissioned). Wrong-registry-for-stage = violation, every stage.
    reg = registry_kind(be.get("image", "")) if be else "unknown"
    if reg == "ghcr":
        rows.add(stage, "registry+namespace", "RED", "all-stages: GitLab CR only (ADR-0042)",
                 "backend image defaults to ghcr.io — ADR-0042 decommissions GHCR; GitLab CR (sora.services:49160) is the sole registry")
    elif reg in ("gitlab-cr", "required"):
        rows.add(stage, "registry+namespace", "OK", "all-stages: GitLab CR only (ADR-0042)",
                 f"registry={reg} (GitLab CR / fail-closed-required)")
    else:
        rows.add(stage, "registry+namespace", "WARN", "all-stages: GitLab CR only (ADR-0042)",
                 "could not determine registry host from backend image ref")
    if lock and not lock_is_placeholder(lock):
        rep = ""
        try:
            rep = lock["images"]["backend"]["repository"]
        except Exception:
            rep = ""
        if rep.startswith("ghcr.io"):
            rows.add(stage, "registry+namespace", "WARN", "lock repo should be GitLab CR",
                     f"env-lock backend repository is ghcr.io ({rep}) — ADR-0042 drift")

    # 2. image identity (tag vs digest). prod/canary MUST be digest-pinned;
    #    staging SHOULD be (release-train rides digests); dev MAY float.
    pin = image_pin_kind(be.get("image", ""), env_tag, lock_be_digest) if be else "unresolved"
    direction = "prod/canary:digest · staging:digest/immutable · dev:any"
    if pin == "mutable":
        if rank >= 2:
            rows.add(stage, "image-identity", "RED", direction,
                     f"backend image resolves to a MUTABLE tag (latest) — {stage} must pin an immutable digest/tag (deep-audit #13)")
        else:
            rows.add(stage, "image-identity", "OK", direction,
                     "backend on a mutable tag — right-direction for dev (loosest)")
    elif pin in ("digest", "tag-immutable"):
        rows.add(stage, "image-identity", "OK", direction,
                 f"backend image is {pin}-pinned")
    else:  # unresolved
        sev = "WARN" if rank >= 3 else "INFO"
        rows.add(stage, "image-identity", sev, direction,
                 "backend image tag is fail-closed `:?` but unresolved statically (resolved host-side from .env / OMNISIGHT_BACKEND_IMAGE_REF) — cannot prove drift")

    # 3. frontend/backend PAIR identity (RT-21) — compare the pair, not backend alone.
    if not fe:
        rows.add(stage, "pair-identity", "INFO", "rank>=2: pair must match",
                 "no frontend service in this stage's compose (dev is backend-only #19) — pair check n/a")
    else:
        fe_pin = image_pin_kind(fe.get("image", ""), env_tag,
                                (lock.get("images", {}).get("frontend", {}) or {}).get("digest") if lock else None)
        be_class = "mutable" if pin == "mutable" else ("pinned" if pin in ("digest", "tag-immutable") else "unresolved")
        fe_class = "mutable" if fe_pin == "mutable" else ("pinned" if fe_pin in ("digest", "tag-immutable") else "unresolved")
        if rank >= 2 and be_class != fe_class and "unresolved" not in (be_class, fe_class):
            rows.add(stage, "pair-identity", "RED", "rank>=2: pair must match",
                     f"backend pin-kind ({be_class}) != frontend pin-kind ({fe_class}) — RT-21 requires the pair move together")
        else:
            rows.add(stage, "pair-identity", "OK", "rank>=2: pair must match",
                     f"backend/frontend pin-kinds consistent (be={be_class} fe={fe_class})")

    # 4. env-contract — OMNISIGHT_ENV marker + DEBUG + required DB-URL presence.
    want_env = {"prod": "production", "canary": "production", "staging": "staging", "dev": "dev"}.get(stage)
    cur_env = envget("OMNISIGHT_ENV")
    if want_env and cur_env and cur_env != want_env:
        rows.add(stage, "env-contract", "RED", f"OMNISIGHT_ENV must == {want_env}",
                 f"OMNISIGHT_ENV={cur_env} != {want_env}")
    elif want_env and not cur_env:
        rows.add(stage, "env-contract", "WARN", f"OMNISIGHT_ENV must == {want_env}",
                 "OMNISIGHT_ENV not declared in compose backend environment")
    else:
        rows.add(stage, "env-contract", "OK", f"OMNISIGHT_ENV must == {want_env}",
                 f"OMNISIGHT_ENV={cur_env}")
    debug = (envget("OMNISIGHT_DEBUG") or "").lower()
    if rank >= 2 and debug in ("1", "true", "yes"):
        rows.add(stage, "env-contract", "RED", "rank>=2: DEBUG must be off",
                 f"OMNISIGHT_DEBUG={debug} — must be false for {stage}")
    elif debug:
        rows.add(stage, "env-contract", "OK", "rank>=2: DEBUG must be off",
                 f"OMNISIGHT_DEBUG={debug}" + (" (dev may debug)" if rank < 2 else ""))

    # 5. DB topology — PG vs SQLite from the DSN.
    dsn = envget("OMNISIGHT_DATABASE_URL") or envget("DATABASE_URL")
    sqlite_path = envget("OMNISIGHT_DATABASE_PATH")
    dsn_l = (dsn or "").lower()
    is_pg = bool(dsn) and ("postgres" in dsn_l or "asyncpg" in dsn_l)
    is_sqlite = bool(dsn) and "sqlite" in dsn_l
    if is_pg:
        rows.add(stage, "db-topology", "OK", "rank>=2: PostgreSQL required",
                 "DSN resolves to PostgreSQL")
        if stage == "staging":
            rows.add(stage, "db-topology", "INFO", "co-tenant is inferred, never proven",
                     "staging PG co-tenant with prod host is INFERRED (not proven) from host/port/db/user — see deploy-audit #34; isolation by DB-name/port/volume only")
    elif is_sqlite or (sqlite_path and not dsn):
        where = dsn if is_sqlite else f"OMNISIGHT_DATABASE_PATH={sqlite_path}"
        if rank >= 2:
            rows.add(stage, "db-topology", "RED", "rank>=2: PostgreSQL required",
                     f"DSN resolves to SQLite ({where}), no PG — {stage} must use PostgreSQL")
        else:
            rows.add(stage, "db-topology", "WARN", "rank>=2: PostgreSQL required",
                     "SQLite path set (dev/test legacy default)")
    else:
        sev = "WARN" if rank >= 2 else "INFO"
        rows.add(stage, "db-topology", sev, "rank>=2: PostgreSQL required",
                 "no DB DSN/path resolvable from compose backend environment statically")

    # 6. gate wiring — declared units present (static); enabled/active only --live.
    if not sc["gate_units"]:
        rows.add(stage, "gate-wiring", "INFO", "declared units must exist; active=--live only",
                 "no gate units declared for this stage in config")
    else:
        for unit in sc["gate_units"]:
            unit_path = os.path.join(systemd_dir, unit)
            if os.path.isfile(unit_path):
                rows.add(stage, "gate-wiring", "OK", "declared units must exist; active=--live only",
                         f"gate unit declared+present: {unit}")
            else:
                rows.add(stage, "gate-wiring", "RED", "declared units must exist; active=--live only",
                         f"gate unit MISSING from {sc.get('systemd_dir', systemd_dir)}: {unit}")

    # 7. overlay-lock enforcement — OMNISIGHT_REQUIRE_DEPLOY_OVERLAY + lock substance.
    req = envget("OMNISIGHT_REQUIRE_DEPLOY_OVERLAY")
    if rank == 1:
        rows.add(stage, "overlay-lock", "OK", "rank>=3: enforce + real lock · dev: off ok",
                 f"OMNISIGHT_REQUIRE_DEPLOY_OVERLAY={req} (dev may disable)")
    else:
        if req == "1":
            rows.add(stage, "overlay-lock", "OK", "rank>=3: enforce + real lock · dev: off ok",
                     "OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=1 (enforced)")
        elif req in ("0",) and rank >= 3:
            rows.add(stage, "overlay-lock", "RED", "rank>=3: enforce + real lock · dev: off ok",
                     f"OMNISIGHT_REQUIRE_DEPLOY_OVERLAY=0 disables the overlay gate for {stage}")
        else:
            rows.add(stage, "overlay-lock", "WARN", "rank>=3: enforce + real lock · dev: off ok",
                     "OMNISIGHT_REQUIRE_DEPLOY_OVERLAY not set in compose (expected via host .env / B1a still default-OFF, OP-1693/1711)")
        if sc["lock"]:
            if lock is None:
                rows.add(stage, "overlay-lock", "WARN", "lock must carry a real digest",
                         f"declared lock {sc['lock']} unreadable/absent")
            elif lock_is_placeholder(lock):
                rows.add(stage, "overlay-lock", "WARN", "lock must carry a real digest",
                         "committed env-lock is a placeholder template (all-zero digest / develop-0000 bundle); the real lock is written host-side by scripts/write_deploy_overlay_lock.py")
            else:
                rows.add(stage, "overlay-lock", "OK", "lock must carry a real digest",
                         "env-lock carries a non-placeholder digest")

    # 8. version/schema — bundle.json digest + repo alembic head (static).
    bundle_path = resolve(root, "bundle.json")
    bundle = load_lock(bundle_path)
    if bundle:
        head = (bundle.get("contracts") or {}).get("db_migration_head")
        be_d = (bundle.get("images") or {}).get("backend", {}).get("digest")
        placeholder = (be_d == ZERO_DIGEST) or head in (None, "unknown", "")
        rows.add(stage, "version/schema", "INFO", "static: bundle+alembic · live: /api/version+DB head",
                 f"bundle.json db_migration_head={head}; backend digest={'placeholder' if placeholder else 'real'} (static; --live reads /api/version + DB head)")
    else:
        rows.add(stage, "version/schema", "INFO", "static: bundle+alembic · live: /api/version+DB head",
                 "no bundle.json resolvable (static); --live reads /api/version")

    # 9. auth mode / secrets source — AUTH_MODE + secrets-file-vs-env.
    auth = envget("OMNISIGHT_AUTH_MODE")
    if rank >= 2 and auth and auth != "strict":
        rows.add(stage, "auth/secrets", "RED", "rank>=2: AUTH_MODE=strict",
                 f"OMNISIGHT_AUTH_MODE={auth} — {stage} must be strict")
    elif auth:
        rows.add(stage, "auth/secrets", "OK", "rank>=2: AUTH_MODE=strict",
                 f"OMNISIGHT_AUTH_MODE={auth}" + (" (dev may be open)" if rank < 2 else ""))
    else:
        rows.add(stage, "auth/secrets", "WARN", "rank>=2: AUTH_MODE=strict",
                 "OMNISIGHT_AUTH_MODE not declared in compose backend environment")
    uses_secrets = bool(be.get("secrets")) or bool(compose.get("root_secrets"))
    inline_secret = any(
        re.search(r"(SECRET|TOKEN|PASSWORD|API_KEY)", k) and v and "${" not in v and "/run/secrets" not in v
        for k, v in be_env.items()
    )
    if inline_secret and rank >= 2:
        rows.add(stage, "auth/secrets", "RED", "rank>=2: secrets via file/docker-secret",
                 "a SECRET/TOKEN/PASSWORD/API_KEY appears inline (literal) in compose environment")
    else:
        rows.add(stage, "auth/secrets", "OK", "rank>=2: secrets via file/docker-secret",
                 f"secrets source = {'docker-secrets' if uses_secrets else 'env_file/.env'} (no inline literal secret)")

    # ── optional best-effort --live annotation (NEVER fatal) ──────────────────
    if LIVE:
        annotate_live(rows, sc, systemd_dir)


def annotate_live(rows, sc, systemd_dir):
    stage = sc["stage"]
    base = sc.get("live_url")
    if base:
        ver = http_get_json(base.rstrip("/") + "/api/version")
        ready = http_get(base.rstrip("/") + "/readyz")
        if ver is None and ready is None:
            rows.add(stage, "live", "INFO", "best-effort", f"live=unknown ({base} unreachable)")
        else:
            detail = "live="
            if ready is not None:
                detail += f"readyz:{ready} "
            if ver is not None:
                dd = ver.get("deployed_digest_backend") or ver.get("image_digest_backend") or "?"
                detail += f"version.backend_digest={str(dd)[:19]}"
            rows.add(stage, "live", "INFO", "best-effort", detail.strip())
    else:
        rows.add(stage, "live", "INFO", "best-effort", "live=unknown (no probe URL configured)")
    # systemctl status for gate units (best-effort, --user then system).
    for unit in sc["gate_units"]:
        state = systemctl_state(unit)
        rows.add(stage, "live", "INFO", "best-effort", f"live=systemctl {unit}: {state}")


def http_get(url):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=4) as r:  # noqa: S310 (best-effort local probe)
            return r.status
    except Exception:
        return None


def http_get_json(url):
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=4) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def systemctl_state(unit):
    for scope in (["--user"], []):
        try:
            out = subprocess.run(["systemctl", *scope, "is-active", unit],
                                 capture_output=True, text=True, timeout=4)
            st = out.stdout.strip() or "unknown"
            if st != "unknown":
                return f"{('user' if scope else 'system')}:{st}"
        except Exception:
            continue
    return "unknown"


# ── report ────────────────────────────────────────────────────────────────────
LIVE = False


def render_table(rows):
    print()
    fmt = "%-6s  %-8s  %-18s  %-44s  %s"
    print(fmt % ("STATUS", "STAGE", "DIMENSION", "DIRECTION", "DETAIL"))
    print(fmt % ("------", "-----", "------------------",
                 "--------------------------------------------", "------"))
    marks = {"OK": "✓ OK", "RED": "✗ RED", "WARN": "? WARN", "INFO": "· INFO"}
    for r in rows:
        print(fmt % (marks.get(r["status"], r["status"]), r["stage"],
                     r["dimension"], r["direction"][:44], r["detail"]))


def write_jsonl(result, counts, rows):
    log = os.environ.get("DEPLOY_PARITY_JSONL_LOG") or os.environ.get("DEPLOYMENT_AUDIT_JSONL_LOG")
    if not log:
        return
    try:
        os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": "deploy_line_parity",
            "source": "scripts/deploy_line_parity.sh",
            "host": socket.gethostname(),
            "result": result,
            "green": counts["OK"],
            "red": counts["RED"],
            "warn": counts["WARN"],
            "info": counts["INFO"],
            "fatal_red": counts["RED"],
            "rows": rows,
        }
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - sink failures must not crash the gate
        sys.stderr.write(f"deploy-line-parity: JSONL sink failed: {exc}\n")


def main():
    global LIVE
    args = parse_args()
    LIVE = args.live
    root = args.root or args.repo
    systemd_dir = resolve(root, args.systemd_dir)

    stages = read_config(args)
    configured = {s["stage"] for s in stages}

    print("deploy-line-parity (OP-1720 / phase-1 declarative parity) — "
          f"host={socket.gethostname()} root={root} "
          f"mode={'static+live' if LIVE else 'static'} "
          f"date={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

    rows = Rows()
    for sc in stages:
        sc["systemd_dir"] = systemd_dir
        evaluate_stage(rows, sc, root, systemd_dir)

    # Any of dev/staging/canary/prod absent from config => not-materialised row.
    for st in ("dev", "staging", "canary", "prod"):
        if st not in configured:
            rows.add(st, "stage-presence", "INFO", "n/a",
                     "stage=not-materialised (absent from config) — informational unless ADR-0040 requires it")
    # testing-env is informational only (deep-audit #20 — no distinct testing env).
    rows.add("testing-env", "stage-presence", "INFO", "n/a",
             "testing-env=informational only (#20 — CI gates only, no distinct integration-test environment)")

    render_table(rows.rows)

    counts = {"OK": 0, "RED": 0, "WARN": 0, "INFO": 0}
    for r in rows.rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print()
    print(f"summary: {counts['OK']} green · {counts['RED']} red · "
          f"{counts['WARN']} warn · {counts['INFO']} info "
          f"(check_family=parity; candidate_compat reserved for phase 2)")

    if rows.fatal > 0:
        write_jsonl("FAIL", counts, rows.rows)
        print(f"RESULT: FAIL — {rows.fatal} wrong-direction parity violation(s). "
              "Fix the cross-stage drift before promotion (REPORT-ONLY: this tool does not remediate).")
        sys.exit(1)
    write_jsonl("PASS", counts, rows.rows)
    print("RESULT: PASS — no wrong-direction parity violations (warn/info rows are advisory).")
    sys.exit(0)


main()
PYEOF
