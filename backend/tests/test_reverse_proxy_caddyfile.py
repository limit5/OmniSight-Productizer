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
