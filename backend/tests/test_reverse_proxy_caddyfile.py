"""G2 #1 — `deploy/reverse-proxy/Caddyfile` contract tests.

Scope is intentionally narrow: verify that the checked-in Caddyfile
satisfies the TODO row 1345 deliverable (HTTPS :443 front-end whose
upstream is backend-a:8000 + backend-b:8001). These are pure
filesystem/string assertions — we do not spin up caddy in CI.

The subsequent G2 deliverables (rolling deploy script, dual-replica
compose, soak test) will add their own test modules; this file stays
focused on the listener + upstream contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CADDYFILE = PROJECT_ROOT / "deploy" / "reverse-proxy" / "Caddyfile"


@pytest.fixture(scope="module")
def caddyfile_text() -> str:
    assert CADDYFILE.exists(), f"Caddyfile missing at {CADDYFILE}"
    return CADDYFILE.read_text(encoding="utf-8")


class TestFileLayout:
    def test_file_lives_under_deploy_reverse_proxy(self) -> None:
        assert CADDYFILE.parent.name == "reverse-proxy"
        assert CADDYFILE.parent.parent.name == "deploy"

    def test_file_not_empty(self, caddyfile_text: str) -> None:
        assert len(caddyfile_text.strip()) > 200

    def test_file_is_utf8_clean(self) -> None:
        CADDYFILE.read_text(encoding="utf-8")


class TestHttpsListener:
    def test_listens_on_port_443(self, caddyfile_text: str) -> None:
        # Accept either bare `:443 {` or `{$OMNISIGHT_PUBLIC_HOSTNAME::443} {`
        # on a non-comment line — that's what opens the HTTPS site block.
        found = False
        for raw in caddyfile_text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.endswith("{") and (
                re.search(r"(^|\s):443\s*\{\s*$", line)
                or "::443}" in line
            ):
                found = True
                break
        assert found, "No HTTPS listener targeting :443 found"

    def test_http_to_https_redirect_present(self, caddyfile_text: str) -> None:
        assert re.search(r"(?m)^\s*:80\s*\{", caddyfile_text)
        assert "redir https://" in caddyfile_text

    def test_tls_block_present(self, caddyfile_text: str) -> None:
        assert re.search(r"\btls\b[^\n]*\{", caddyfile_text)
        assert "issuer acme" in caddyfile_text
        assert "issuer internal" in caddyfile_text


class TestUpstreams:
    def test_backend_a_on_port_8000(self, caddyfile_text: str) -> None:
        assert "backend-a:8000" in caddyfile_text

    def test_backend_b_on_port_8001(self, caddyfile_text: str) -> None:
        assert "backend-b:8001" in caddyfile_text

    def test_both_upstreams_on_same_reverse_proxy_line(
        self, caddyfile_text: str
    ) -> None:
        # Both replicas must be pooled in ONE reverse_proxy directive so
        # lb_policy / health_uri apply uniformly — otherwise round_robin
        # degenerates to per-directive single-upstream lookups.
        for line in caddyfile_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("reverse_proxy"):
                continue
            if "backend-a:8000" in stripped and "backend-b:8001" in stripped:
                return
        pytest.fail(
            "backend-a:8000 and backend-b:8001 must be on the same "
            "`reverse_proxy` directive (shared upstream pool)"
        )

    def test_upstreams_are_env_overridable(self, caddyfile_text: str) -> None:
        # Operators need to swap hosts per-env without forking the file.
        assert "{$OMNISIGHT_UPSTREAM_A:backend-a:8000}" in caddyfile_text
        assert "{$OMNISIGHT_UPSTREAM_B:backend-b:8001}" in caddyfile_text


class TestLoadBalancingPolicy:
    def test_round_robin_lb_policy(self, caddyfile_text: str) -> None:
        assert "lb_policy round_robin" in caddyfile_text

    def test_lb_try_duration_set(self, caddyfile_text: str) -> None:
        assert re.search(r"lb_try_duration\s+\d+s", caddyfile_text)

    def test_flush_interval_disabled_for_streaming(
        self, caddyfile_text: str
    ) -> None:
        # SSE (backend/events.py) depends on unbuffered proxying.
        assert "flush_interval -1" in caddyfile_text


class TestHealthChecks:
    def test_active_probe_targets_readyz(self, caddyfile_text: str) -> None:
        assert "health_uri /readyz" in caddyfile_text

    def test_active_probe_interval_reasonable(self, caddyfile_text: str) -> None:
        match = re.search(r"health_interval\s+(\d+)s", caddyfile_text)
        assert match, "health_interval directive missing"
        interval = int(match.group(1))
        # Matches rolling-restart SLO: ~3 probes → eject within ≤10 s.
        assert 1 <= interval <= 10

    def test_passive_eject_configured(self, caddyfile_text: str) -> None:
        # TODO row 1348 — fail_timeout / automatic eject.
        assert re.search(r"fail_duration\s+\d+s", caddyfile_text)
        assert re.search(r"max_fails\s+\d+", caddyfile_text)


class TestIdentityHeaders:
    def test_forwards_real_ip(self, caddyfile_text: str) -> None:
        assert "header_up X-Real-IP" in caddyfile_text

    def test_forwards_proto(self, caddyfile_text: str) -> None:
        assert "header_up X-Forwarded-Proto" in caddyfile_text

    def test_forwards_for(self, caddyfile_text: str) -> None:
        assert "header_up X-Forwarded-For" in caddyfile_text


class TestHardening:
    def test_server_header_stripped(self, caddyfile_text: str) -> None:
        assert "-Server" in caddyfile_text

    def test_content_type_sniff_blocked(self, caddyfile_text: str) -> None:
        assert 'X-Content-Type-Options "nosniff"' in caddyfile_text

    def test_admin_api_disabled(self, caddyfile_text: str) -> None:
        assert re.search(r"(?m)^\s*admin\s+off\b", caddyfile_text)

    def test_structured_access_log(self, caddyfile_text: str) -> None:
        assert re.search(r"log\s*\{", caddyfile_text)
        assert "format json" in caddyfile_text


class TestBraceBalance:
    def test_curly_braces_balanced(self, caddyfile_text: str) -> None:
        # Strip comments + string-like placeholders so `{$VAR:default}` and
        # `{http.reverse_proxy.upstream.hostport}` don't skew the count.
        stripped = re.sub(r"#.*$", "", caddyfile_text, flags=re.MULTILINE)
        stripped = re.sub(r"\{\$[^}]*\}", "", stripped)
        stripped = re.sub(r"\{http\.[^}]*\}", "", stripped)
        stripped = re.sub(r"\{(remote_host|scheme|host|uri)\}", "", stripped)
        opens = stripped.count("{")
        closes = stripped.count("}")
        assert opens == closes, (
            f"Unbalanced braces: {opens} open vs {closes} close"
        )


# ─────────────────────────────────────────────────────────────────────
# Phase-3 P2 (2026-04-20) — edge path router :8080 drift guard.
#
# This class locks the root-cause fix for the SSE-buffering cascade.
# The prior setup routed CF Tunnel Public Hostnames at frontend:3000,
# which made Next.js's rewrites() API the gateway for /api/v1/*. That
# buffered every streaming response (SSE, NDJSON, chunked) and
# bounced operators back to /login in a rate-limit cascade. The fix
# is a Caddy :8080 listener that does edge path routing:
#   - /api/v1/*   → backend-a / backend-b       (flush_interval -1)
#   - /           → frontend:3000                (flush_interval -1)
#
# If any of the assertions below regress, the symptom returns. In
# particular:
#   (a) if the :8080 block is removed, CF Tunnel would still work
#       (pointing at :8080 would 404), but someone pointing it back
#       at frontend:3000 would silently re-break SSE.
#   (b) if `flush_interval -1` is missing on either handler, Caddy
#       will buffer streaming responses by default.
#   (c) if the API handle and catch-all handle are swapped or the
#       API handle loses its path filter, all requests go to the
#       wrong upstream and SSR breaks.
# ─────────────────────────────────────────────────────────────────────


class TestPort8080EdgeRouter:
    """Phase-3 P2 — :8080 path-aware router drift guard."""

    # Slice the :8080 block out of the Caddyfile so every assertion below
    # operates on the correct scope (not the whole file — the :443 block
    # also has `reverse_proxy backend-a ...` and would false-positive).
    @staticmethod
    def _block(text: str) -> str:
        # Match `:8080 {` through its balanced closing brace. The block
        # contains nested `handle { ... }` + `reverse_proxy { ... }`
        # sub-blocks, so a greedy counter is the simplest parser.
        start = text.find(":8080 {")
        assert start != -1, (
            "`:8080 {` site block is missing from Caddyfile — CF Tunnel "
            "would lose its canonical target and SSE would buffer the "
            "next time someone points the ingress at frontend:3000. "
            "See Phase-3 P2 commit for rationale."
        )
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        raise AssertionError("Unclosed :8080 block — Caddyfile is malformed")

    def test_block_exists(self, caddyfile_text: str) -> None:
        block = self._block(caddyfile_text)
        assert block.startswith(":8080 {")

    def test_api_handle_routes_to_backend_pool(self, caddyfile_text: str) -> None:
        block = self._block(caddyfile_text)
        # `handle /api/v1/* {` must be present and reference both backend
        # services (either hard-coded or via the $OMNISIGHT_UPSTREAM_{A,B}
        # env-substitution pattern used elsewhere in the file).
        assert re.search(r"handle\s+/api/v1/\*\s*\{", block), (
            "`:8080` block must contain `handle /api/v1/*` — this is the "
            "API-path split that bypasses Next.js rewrites()."
        )
        # At least one reference to each backend upstream inside the
        # /api/v1/* handle scope. We re-slice on the handle block to
        # make sure the upstreams are INSIDE the handler, not in a
        # sibling block.
        api_block = re.search(
            r"handle\s+/api/v1/\*\s*\{(.+?)\n\t\}", block, re.DOTALL,
        )
        assert api_block, "Could not isolate the /api/v1/* handle body"
        api_body = api_block.group(1)
        assert "backend-a:8000" in api_body, "backend-a missing from :8080 API upstreams"
        assert "backend-b:8001" in api_body, "backend-b missing from :8080 API upstreams"

    def test_catch_all_handle_routes_to_frontend(self, caddyfile_text: str) -> None:
        block = self._block(caddyfile_text)
        # Catch-all `handle {` (no path filter) must route to frontend:3000
        # so the UI paths (/, /login, /workspace, /_next/*, /setup-required)
        # reach Next.js SSR.
        catch_all = re.search(
            r"(?<!/\*\s)handle\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", block,
        )
        # The regex above matches any `handle { ... }` without a preceding
        # path — we search for the one that references `frontend:3000`.
        assert "frontend:3000" in block, (
            "`:8080` block must route the catch-all handler to frontend:3000 "
            "for Next.js SSR (dashboard, login page, static assets)."
        )
        # Guard: make sure the :8080 block does NOT route /api/v1/* to
        # frontend (would put Next.js rewrites back in the path and
        # reintroduce the buffering bug).
        frontend_in_api = re.search(
            r"handle\s+/api/v1/\*\s*\{[^}]*frontend:3000", block, re.DOTALL,
        )
        assert not frontend_in_api, (
            "CRITICAL: `:8080` /api/v1/* handle references frontend:3000 — "
            "this would reintroduce the SSE buffering bug that Phase-3 P2 "
            "fixed. The API handle must go DIRECT to the backend pool."
        )

    def test_flush_interval_minus_one_on_api_handle(self, caddyfile_text: str) -> None:
        block = self._block(caddyfile_text)
        api_block = re.search(
            r"handle\s+/api/v1/\*\s*\{(.+?)\n\t\}", block, re.DOTALL,
        )
        assert api_block
        api_body = api_block.group(1)
        assert "flush_interval -1" in api_body, (
            "CRITICAL: /api/v1/* handle missing `flush_interval -1` — "
            "without it Caddy buffers streaming responses (SSE, NDJSON, "
            "chunked) and the browser sees 0 bytes for the life of the "
            "connection. This is the primary knob that makes the whole "
            "edge-path-routing fix actually work."
        )

    def test_flush_interval_minus_one_on_catch_all(self, caddyfile_text: str) -> None:
        block = self._block(caddyfile_text)
        # Find the frontend:3000 reverse_proxy block and assert flush
        # interval is set. This future-proofs against Next.js server-
        # sent UI streams (React 18 renderToPipeableStream) landing
        # in this project.
        frontend_block = re.search(
            r"reverse_proxy\s+frontend:3000\s*\{(.+?)\n\t\t\}", block, re.DOTALL,
        )
        assert frontend_block, "Could not isolate frontend:3000 reverse_proxy body"
        assert "flush_interval -1" in frontend_block.group(1), (
            "Catch-all handler (frontend:3000) missing `flush_interval -1`. "
            "While Next.js doesn't stream today, keeping flushing hot means "
            "a future upgrade to React 18 streaming SSR works without "
            "surfacing this buffering bug a second time."
        )

    def test_block_is_http_only_not_tls(self, caddyfile_text: str) -> None:
        block = self._block(caddyfile_text)
        # :8080 must be plain HTTP — TLS is terminated at CF edge, the
        # tunnel carries it over QUIC, and adding `tls internal` here
        # would be TLS-inside-TLS-inside-QUIC overhead for no benefit.
        assert "tls " not in block and "tls\n" not in block, (
            "`:8080` must be plain HTTP (CF tunnel handles TLS at the "
            "edge). Adding a TLS directive is dead weight."
        )

    def test_port_8080_not_exposed_to_host(self) -> None:
        # Guard: the edge router is docker-internal only. Exposing it on
        # the host would bypass Caddy's :443 TLS + let anyone on the LAN
        # hit the unauthenticated :8080. docker-compose.prod.yml `caddy`
        # service's `ports:` list must NOT include 8080.
        compose_path = PROJECT_ROOT / "docker-compose.prod.yml"
        compose = compose_path.read_text(encoding="utf-8")
        # Isolate the caddy service block (lines between `caddy:` and the
        # next top-level service key).
        caddy_match = re.search(
            r"^\s{2}caddy:\s*$(.+?)(?=^\s{2}\w+:|^volumes:|^networks:)",
            compose, re.DOTALL | re.MULTILINE,
        )
        assert caddy_match, "caddy service block not found in docker-compose.prod.yml"
        caddy_block = caddy_match.group(1)
        # `ports:` list under the caddy service — look for any 8080 mapping.
        ports_match = re.search(
            r"ports:\s*\n((?:\s+-\s+\"[^\"]+\"\s*\n)+)", caddy_block,
        )
        if ports_match:
            ports_body = ports_match.group(1)
            assert "8080" not in ports_body, (
                "CRITICAL: caddy service exposes :8080 on the host. The "
                "edge router is meant to be docker-internal only (reached "
                "via `caddy:8080` DNS name by the cloudflared sidecar). "
                "Exposing it makes the unauthenticated path router "
                "reachable on the LAN."
            )
