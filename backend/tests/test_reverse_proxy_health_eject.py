"""G2 #4 — Upstream health check + automatic eject (fail_timeout) contract.

TODO row 1348. The Caddyfile shipped in G2 #1 already contained the
active + passive eject directives (they were co-implemented because
splitting them across files would be absurd); this test module is the
deliverable that **pins the eject semantics** so a later edit to the
Caddyfile cannot silently weaken the contract.

Scope is intentionally broader than `test_reverse_proxy_caddyfile.py`:

  * test_reverse_proxy_caddyfile.py — G2 #1 listener + upstream-pool
    contract (the Caddyfile exists, listens on :443, names both
    replicas).
  * THIS FILE — G2 #4 ejection contract (the active probe pulls a
    dying replica within a tight budget, passive eject catches real-
    traffic failure modes the probe can't see, the eject budget
    aligns with `scripts/deploy.sh` rolling drain + `backend/lifecycle.py`
    SIGTERM drain, and the operator runbook `deploy/reverse-proxy/README.md`
    documents the contract end-to-end).

These are pure filesystem / text assertions — we do not spin up caddy
or a backend in CI. The Caddyfile's semantics are public Caddy v2
behaviour; the only thing we can go wrong on is drift between what
the file says, what the runbook claims, and what the rolling-restart
script is expecting.

The test count is deliberately high (~30 cases) because each
directive + each timing invariant is its own regression trap: if
someone lowers `health_fails` to 1 "because the incident was slow to
eject", they need a loud, named failure pointing them at the
runbook, not a silent diff-review miss.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"
README = PROJECT_ROOT / "deploy" / "reverse-proxy" / "README.md"
DEPLOY_SH = PROJECT_ROOT / "scripts" / "deploy.sh"
LIFECYCLE_PY = PROJECT_ROOT / "backend" / "lifecycle.py"


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def caddyfile_text() -> str:
    assert CADDYFILE.exists(), f"Caddyfile missing at {CADDYFILE}"
    return CADDYFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    assert README.exists(), (
        f"Operator runbook missing at {README} — G2 #4 deliverable is "
        "incomplete without it"
    )
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deploy_sh_text() -> str:
    assert DEPLOY_SH.exists(), f"scripts/deploy.sh missing at {DEPLOY_SH}"
    return DEPLOY_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def reverse_proxy_block(caddyfile_text: str) -> str:
    """Return the text of the `reverse_proxy … { … }` directive body.

    Brace-balancing parser (regex alone isn't enough because the
    directive contains nested placeholders like
    `{http.reverse_proxy.upstream.hostport}` — those curly braces
    are not Caddy block scopes but we need to discount them
    anyway).
    """
    # Find ALL `reverse_proxy` directive blocks (the Caddyfile may
    # contain multiple — e.g. :443 external + :8000 internal proxy).
    # Concatenate them so health-directive assertions cover every block.
    lines = caddyfile_text.splitlines()
    all_blocks: list[str] = []

    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("reverse_proxy") and lines[i].rstrip().endswith("{"):
            depth = 0
            body_lines: list[str] = []
            for ln in lines[i:]:
                scrubbed = re.sub(r"\{\$[^}]*\}", "", ln)
                scrubbed = re.sub(r"\{http\.[^}]*\}", "", scrubbed)
                scrubbed = re.sub(r"\{(remote_host|scheme|host|uri)\}", "", scrubbed)
                depth += scrubbed.count("{") - scrubbed.count("}")
                body_lines.append(ln)
                if depth == 0 and body_lines:
                    break
            assert depth == 0, "reverse_proxy directive braces never closed"
            all_blocks.append("\n".join(body_lines))
            i += len(body_lines)
        else:
            i += 1

    assert all_blocks, "no reverse_proxy directive with body found"
    return "\n".join(all_blocks)


# ── 1. Active probe — the /readyz contract ──────────────────────────


class TestActiveProbe:
    def test_probe_targets_readyz(self, reverse_proxy_block: str) -> None:
        # The backend ships `/readyz` as its readiness endpoint
        # (G1 #2 split from `/healthz`). Using any other URI breaks
        # the SIGTERM drain contract in backend/lifecycle.py.
        assert "health_uri /readyz" in reverse_proxy_block

    def test_probe_uses_explicit_get_method(
        self, reverse_proxy_block: str
    ) -> None:
        # Explicit GET so the probe never inherits the verb of a
        # failed in-flight request — cheap defence-in-depth.
        assert re.search(r"(?m)^\s*health_method\s+GET\b", reverse_proxy_block)

    def test_probe_port_inherits_upstream_port(
        self, reverse_proxy_block: str
    ) -> None:
        # `health_port 0` is Caddy shorthand for "same port as the
        # upstream definition". Both replicas listen on their LB
        # port and expose /readyz there; any other value means a
        # sidecar that would drift from the main app.
        assert re.search(r"(?m)^\s*health_port\s+0\b", reverse_proxy_block)

    def test_probe_interval_between_1_and_5_seconds(
        self, reverse_proxy_block: str
    ) -> None:
        # 1 s floor: probing too fast burns CPU + masks noise as health.
        # 5 s ceiling: a replica must be ejected inside the rolling
        # drain budget (35 s). At >5 s interval × 3 fails, that's >15 s
        # just to eject, leaving <20 s for recreate + re-admit.
        m = re.search(r"(?m)^\s*health_interval\s+(\d+)s\b", reverse_proxy_block)
        assert m, "health_interval missing from reverse_proxy block"
        assert 1 <= int(m.group(1)) <= 5

    def test_probe_timeout_not_greater_than_interval(
        self, reverse_proxy_block: str
    ) -> None:
        # A probe that takes longer than the interval would queue up
        # — meaning when a replica is slow, probes stack instead of
        # ejecting. Enforce timeout ≤ interval.
        i = re.search(r"(?m)^\s*health_interval\s+(\d+)s\b", reverse_proxy_block)
        t = re.search(r"(?m)^\s*health_timeout\s+(\d+)s\b", reverse_proxy_block)
        assert i and t, "health_interval/timeout missing"
        assert int(t.group(1)) <= int(i.group(1))

    def test_probe_only_2xx_counts_as_success(
        self, reverse_proxy_block: str
    ) -> None:
        # /readyz returns 200 or 503 flat. 2xx / 3xx / 4xx are all
        # abnormal and should NOT keep the replica in rotation.
        assert re.search(
            r"(?m)^\s*health_status\s+2xx\b", reverse_proxy_block
        )

    def test_probe_redirects_not_followed(
        self, reverse_proxy_block: str
    ) -> None:
        # /readyz is a flat 200/503 endpoint. A 3xx here is a config
        # bug (auth middleware injecting a redirect, etc.); follow-
        # redirects would mask it.
        assert re.search(
            r"(?m)^\s*health_follow_redirects\s+false\b", reverse_proxy_block
        )

    def test_one_good_probe_readmits_drained_replica(
        self, reverse_proxy_block: str
    ) -> None:
        # Fast recovery after rolling restart — the OTHER replica is
        # about to be drained next, so we want the just-restarted one
        # back in rotation asap.
        assert re.search(
            r"(?m)^\s*health_passes\s+1\b", reverse_proxy_block
        )

    def test_three_bad_probes_eject(self, reverse_proxy_block: str) -> None:
        # 1 = single-point-of-failure (GC pause flaps the pool).
        # 5+ = eject too slowly to meet the rolling-drain SLO.
        # 2-4 is the safe band; we pin 3 explicitly.
        m = re.search(r"(?m)^\s*health_fails\s+(\d+)\b", reverse_proxy_block)
        assert m, "health_fails missing"
        assert 2 <= int(m.group(1)) <= 4

    def test_active_eject_budget_stays_under_ten_seconds(
        self, reverse_proxy_block: str
    ) -> None:
        # The headline SLO: a dying replica must be ejected within
        # 10 s or clients start seeing 5xx bursts during rolling
        # drain. = health_interval × health_fails.
        i = re.search(r"(?m)^\s*health_interval\s+(\d+)s\b", reverse_proxy_block)
        f = re.search(r"(?m)^\s*health_fails\s+(\d+)\b", reverse_proxy_block)
        assert i and f
        assert int(i.group(1)) * int(f.group(1)) <= 10


# ── 2. Passive eject — the "fail_timeout" contract ──────────────────


class TestPassiveEject:
    def test_fail_duration_is_observation_window(
        self, reverse_proxy_block: str
    ) -> None:
        # Caddy's `fail_duration` == nginx's `fail_timeout`: observation
        # window AND eject duration. 10-120 s is the sensible band.
        m = re.search(
            r"(?m)^\s*fail_duration\s+(\d+)s\b", reverse_proxy_block
        )
        assert m, "fail_duration missing (this is the 'fail_timeout' knob)"
        assert 10 <= int(m.group(1)) <= 120

    def test_max_fails_tolerates_single_blip(
        self, reverse_proxy_block: str
    ) -> None:
        # `max_fails 1` ejects on ANY 5xx (one DB blip → whole
        # replica ejected for 30 s). >5 is too tolerant. 2-5 is safe.
        m = re.search(r"(?m)^\s*max_fails\s+(\d+)\b", reverse_proxy_block)
        assert m, "max_fails missing"
        assert 2 <= int(m.group(1)) <= 5

    def test_only_5xx_counts_as_unhealthy(
        self, reverse_proxy_block: str
    ) -> None:
        # 4xx is the client's problem and never the upstream's fault;
        # counting it would eject a perfectly healthy replica whenever
        # one tenant hits a rate-limit.
        assert re.search(
            r"(?m)^\s*unhealthy_status\s+5xx\b", reverse_proxy_block
        )

    def test_unhealthy_latency_ceiling_present(
        self, reverse_proxy_block: str
    ) -> None:
        # A replica answering 200 but slowly is worse than ejected —
        # it collects connections via the thundering-herd pattern.
        m = re.search(
            r"(?m)^\s*unhealthy_latency\s+(\d+)s\b", reverse_proxy_block
        )
        assert m, "unhealthy_latency missing"
        # Backend p99 is sub-second under nominal load; 5-15 s is
        # generous enough to not false-positive but tight enough to
        # catch genuine hangs.
        assert 5 <= int(m.group(1)) <= 15

    def test_unhealthy_request_count_burst_protection(
        self, reverse_proxy_block: str
    ) -> None:
        # Protects against the case where a partial outage has 50+
        # in-flight requests stuck on one replica — eject regardless
        # of whether individual responses crossed `max_fails`.
        assert re.search(
            r"(?m)^\s*unhealthy_request_count\s+\d+\b", reverse_proxy_block
        ), "unhealthy_request_count missing (burst protection)"


# ── 3. Load-balancing retry — what makes "0 × 5xx" possible ─────────


class TestRetryOnEject:
    def test_lb_try_duration_covers_eject_window(
        self, reverse_proxy_block: str
    ) -> None:
        # When a probe hasn't yet ejected a draining replica but the
        # replica has already started returning 503, Caddy's per-
        # request retry budget is what saves us from a 5xx spike.
        # lb_try_duration must be >0 for retry to happen at all.
        m = re.search(
            r"(?m)^\s*lb_try_duration\s+(\d+)s\b", reverse_proxy_block
        )
        assert m, "lb_try_duration missing"
        assert int(m.group(1)) >= 3

    def test_lb_try_interval_is_sub_second(
        self, reverse_proxy_block: str
    ) -> None:
        # Retrying once per second during a 5 s budget is barely 5
        # attempts — not enough. Sub-second interval keeps client
        # latency invisible during failover.
        assert re.search(
            r"(?m)^\s*lb_try_interval\s+\d+(ms|\.\d+s)\b", reverse_proxy_block
        )


# ── 4. Timing alignment with scripts/deploy.sh rolling ──────────────


class TestRollingAlignment:
    """The eject budget must fit inside `scripts/deploy.sh` rolling
    drain so the script's drain-confirmation loop doesn't time out
    before Caddy notices the replica is gone."""

    def test_deploy_sh_drain_timeout_covers_active_eject(
        self, reverse_proxy_block: str, deploy_sh_text: str
    ) -> None:
        # deploy.sh waits up to ROLL_DRAIN_TIMEOUT seconds for the
        # replica's /readyz to stop returning 200. That timeout must
        # be ≥ active eject budget (health_interval × health_fails).
        i = re.search(r"(?m)^\s*health_interval\s+(\d+)s\b", reverse_proxy_block)
        f = re.search(r"(?m)^\s*health_fails\s+(\d+)\b", reverse_proxy_block)
        d = re.search(
            r"ROLL_DRAIN_TIMEOUT=\"?\$\{OMNISIGHT_ROLL_DRAIN_TIMEOUT:-(\d+)\}",
            deploy_sh_text,
        )
        assert i and f and d, "timing directives missing"
        eject_budget = int(i.group(1)) * int(f.group(1))
        drain_timeout = int(d.group(1))
        assert drain_timeout >= eject_budget, (
            f"deploy.sh drain timeout {drain_timeout}s is tighter than "
            f"Caddy's active eject budget {eject_budget}s — rolling "
            f"restart will abort before Caddy ejects the replica"
        )

    def test_deploy_sh_drain_timeout_leaves_budget_for_probe(
        self, reverse_proxy_block: str, deploy_sh_text: str
    ) -> None:
        # Not just ≥ eject budget; there must be some slack for the
        # in-flight probe to finish after the last 503.
        i = re.search(r"(?m)^\s*health_interval\s+(\d+)s\b", reverse_proxy_block)
        f = re.search(r"(?m)^\s*health_fails\s+(\d+)\b", reverse_proxy_block)
        d = re.search(
            r"ROLL_DRAIN_TIMEOUT=\"?\$\{OMNISIGHT_ROLL_DRAIN_TIMEOUT:-(\d+)\}",
            deploy_sh_text,
        )
        assert i and f and d
        eject_budget = int(i.group(1)) * int(f.group(1))
        drain_timeout = int(d.group(1))
        # Require ≥2× slack so one missed probe doesn't cascade.
        assert drain_timeout >= 2 * eject_budget

    def test_deploy_sh_references_caddy_in_rolling_doc(
        self, deploy_sh_text: str
    ) -> None:
        # The rolling-restart function's docstring must mention Caddy
        # and /readyz so a future maintainer doesn't tweak the drain
        # flow in isolation from the proxy config.
        assert "Caddy" in deploy_sh_text
        assert "/readyz" in deploy_sh_text


# ── 5. Operator runbook (deploy/reverse-proxy/README.md) ────────────


class TestOperatorRunbook:
    def test_readme_exists(self) -> None:
        assert README.is_file(), (
            "Operator runbook missing — G2 #4 delivers "
            "deploy/reverse-proxy/README.md documenting the eject "
            "contract end-to-end"
        )

    def test_readme_is_substantive(self, readme_text: str) -> None:
        # A stub README is worse than none — it creates the illusion
        # of documentation. Require enough body for the eight sections
        # named in §references.
        assert len(readme_text) >= 3500

    def test_readme_names_both_eject_mechanisms(
        self, readme_text: str
    ) -> None:
        assert re.search(r"active\s+probe", readme_text, re.IGNORECASE)
        assert re.search(r"passive\s+eject", readme_text, re.IGNORECASE)

    def test_readme_maps_fail_duration_to_nginx_fail_timeout(
        self, readme_text: str
    ) -> None:
        # Critical for operators coming from nginx — they will look
        # for "fail_timeout" when triaging; the README must bridge
        # the terminology gap.
        assert "fail_timeout" in readme_text
        assert "fail_duration" in readme_text

    def test_readme_documents_timing_budget(self, readme_text: str) -> None:
        # The whole point of the runbook is the timing table. Require
        # both ends of the budget to be named.
        assert re.search(r"health_interval", readme_text)
        assert re.search(r"health_fails", readme_text)
        assert re.search(r"OMNISIGHT_ROLL_DRAIN_TIMEOUT", readme_text)

    def test_readme_lists_triage_steps(self, readme_text: str) -> None:
        # On-call operators need a triage checklist. Having the
        # section present (even minimally) is the contract.
        assert re.search(r"(?i)triage", readme_text)
        assert re.search(r"(?i)curl.*readyz", readme_text)

    def test_readme_cross_links_sibling_deliverables(
        self, readme_text: str
    ) -> None:
        # The reverse-proxy contract is one leg of a tripod with
        # backend/lifecycle.py + scripts/deploy.sh. Losing the
        # cross-link makes isolated edits dangerous.
        assert "backend/lifecycle.py" in readme_text
        assert "scripts/deploy.sh" in readme_text
        assert "docker-compose.prod.yml" in readme_text

    def test_readme_points_at_contract_tests(self, readme_text: str) -> None:
        # Self-referential: the README tells operators which test
        # file to run when in doubt. Missing this link means a
        # failing eject contract gets diagnosed from Caddy logs
        # instead of from a named test.
        assert "test_reverse_proxy_health_eject.py" in readme_text
        assert "test_reverse_proxy_caddyfile.py" in readme_text

    def test_readme_documents_smoke_command(self, readme_text: str) -> None:
        # `caddy validate` is the zero-cost check every operator
        # should run before rolling a Caddyfile change.
        assert re.search(
            r"caddy\s+validate", readme_text
        ) or re.search(r"caddy\s+fmt", readme_text)

    def test_readme_declares_worst_case_traffic_gap(
        self, readme_text: str
    ) -> None:
        # The "0 × 5xx during deploy" promise in TODO row 1349
        # depends on this number. Pin it.
        assert re.search(
            r"(?i)(worst[-\s]case|traffic gap|0\s*[×x]\s*5xx|0\s*5xx)",
            readme_text,
        )


# ── 6. Cross-file structural invariants ─────────────────────────────


class TestStructuralInvariants:
    def test_all_health_directives_sit_inside_reverse_proxy_block(
        self, caddyfile_text: str, reverse_proxy_block: str
    ) -> None:
        # If any health_* directive leaks outside the reverse_proxy
        # block (e.g. stray indentation), Caddy parses it as a site
        # directive and silently does nothing for health checking.
        for directive in (
            "health_uri",
            "health_interval",
            "health_fails",
            "health_passes",
            "fail_duration",
            "max_fails",
            "unhealthy_status",
            "unhealthy_latency",
        ):
            # All occurrences in the full file must be inside the
            # reverse_proxy block slice.
            full_count = len(
                re.findall(rf"(?m)^\s*{directive}\b", caddyfile_text)
            )
            inside_count = len(
                re.findall(rf"(?m)^\s*{directive}\b", reverse_proxy_block)
            )
            assert full_count == inside_count, (
                f"{directive} appears {full_count - inside_count} times "
                f"outside the reverse_proxy block — will be parsed as a "
                f"no-op site directive"
            )

    def test_no_hardcoded_upstream_outside_env_override_pattern(
        self, reverse_proxy_block: str
    ) -> None:
        # The G2 #1 contract uses `{$OMNISIGHT_UPSTREAM_A:backend-a:8000}`
        # form. A bare `backend-a:8000` on the reverse_proxy line (NOT
        # inside a `{$…}` default) means an operator can't override per
        # env. Inspect the reverse_proxy header specifically.
        header = next(
            ln for ln in reverse_proxy_block.splitlines()
            if ln.strip().startswith("reverse_proxy")
        )
        # Bare occurrences (not preceded by a colon, which is the env-
        # var default separator) would be the violation.
        bare_a = re.search(r"(?<![:])backend-a:8000", header)
        bare_b = re.search(r"(?<![:])backend-b:8001", header)
        assert bare_a is None, (
            "backend-a:8000 appears outside the env-override pattern"
        )
        assert bare_b is None, (
            "backend-b:8001 appears outside the env-override pattern"
        )

    def test_active_and_passive_block_both_present(
        self, reverse_proxy_block: str
    ) -> None:
        # Defence-in-depth: both mechanisms are required; neither is
        # allowed to be removed "because the other covers it".
        has_active = "health_uri" in reverse_proxy_block
        has_passive = "fail_duration" in reverse_proxy_block
        assert has_active, "Active health check removed"
        assert has_passive, "Passive eject removed"


# ── 7. Sanity for the project-wide invariants ──────────────────────


class TestProjectWideSanity:
    def test_readyz_still_exists_as_endpoint(self) -> None:
        # The active probe points at /readyz — if backend/lifecycle.py
        # or the health router ever drops the /readyz route, the
        # probe would ALWAYS 404 and the pool would be permanently
        # ejected. This test pins the cross-component dependency.
        health_files = list(
            (PROJECT_ROOT / "backend").rglob("health*.py")
        )
        hits = 0
        for hf in health_files:
            try:
                text = hf.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "/readyz" in text or '"readyz"' in text or "'readyz'" in text:
                hits += 1
        assert hits >= 1, (
            "No backend source file references /readyz — the Caddy "
            "active probe would 404 against every replica"
        )

    def test_lifecycle_drives_readyz_false_on_sigterm(self) -> None:
        # The whole eject-on-drain flow depends on lifecycle.py
        # flipping readiness to false when SIGTERM arrives. If that
        # wiring is lost, rolling restarts will leave replicas
        # answering 200 forever even while stopping.
        assert LIFECYCLE_PY.exists(), "backend/lifecycle.py missing"
        text = LIFECYCLE_PY.read_text(encoding="utf-8")
        # Look for any of the common drain/ready-flag patterns.
        # lifecycle.py uses a `begin_draining()` coordinator + SIGTERM
        # signal handler; /readyz reads the draining flag and 503s.
        patterns = [
            r"begin_draining",
            r"is_draining",
            r"is_ready\s*=\s*False",
            r"_ready\s*=\s*False",
            r"set_not_ready",
            r"mark_not_ready",
        ]
        signal_patterns = [r"SIGTERM", r"signal\.SIGTERM"]
        assert any(re.search(p, text) for p in patterns), (
            "backend/lifecycle.py doesn't appear to drive a drain "
            "flag on shutdown — Caddy active eject would never fire "
            "during rolling restart"
        )
        assert any(re.search(p, text) for p in signal_patterns), (
            "backend/lifecycle.py doesn't reference SIGTERM — the "
            "signal handler that flips /readyz to 503 is missing"
        )
