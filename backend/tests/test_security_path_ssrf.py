"""SC.7.5 — Unit tests for OWASP path-traversal / SSRF helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.security import path_ssrf as ps


def _issue(exc: pytest.ExceptionInfo[ps.PathSsrError]) -> ps.PathSsrIssue:
    return exc.value.issue


class TestValidateRelativePath:
    def test_normalizes_slashes_and_strips(self):
        assert ps.validate_relative_path("  assets\\icons/app.png  ") == "assets/icons/app.png"

    @pytest.mark.parametrize(
        "value, code",
        [
            (None, "type"),
            ("", "empty"),
            ("   ", "empty"),
            ("/etc/passwd", "absolute"),
            ("\\server\\share", "absolute"),
            ("C:\\Windows\\win.ini", "drive_letter"),
            ("../secrets.txt", "traversal"),
            ("safe/../../secrets.txt", "traversal"),
            ("%2e%2e/secrets.txt", "traversal"),
            ("safe\x00name", "control_char"),
        ],
    )
    def test_rejects_traversal_and_ambiguous_paths(self, value: object, code: str):
        with pytest.raises(ps.PathSsrError) as exc:
            ps.validate_relative_path(value)
        assert _issue(exc).code == code

    def test_rejects_overlong_paths(self):
        with pytest.raises(ps.PathSsrError) as exc:
            ps.validate_relative_path("a" * 6, max_length=5)
        assert _issue(exc).field == "path"
        assert _issue(exc).code == "too_long"

    def test_invalid_max_length_is_configuration_error(self):
        with pytest.raises(ps.PathSsrError) as exc:
            ps.validate_relative_path("ok.txt", max_length=0)
        assert _issue(exc).field == "max_length"
        assert _issue(exc).code == "too_small"


class TestResolvePathWithinBase:
    def test_resolves_safe_path_under_base(self, tmp_path: Path):
        out = ps.resolve_path_within_base(tmp_path, "reports/summary.json")
        assert out == (tmp_path / "reports" / "summary.json").resolve(strict=False)

    def test_rejects_relative_path_before_joining(self, tmp_path: Path):
        with pytest.raises(ps.PathSsrError) as exc:
            ps.resolve_path_within_base(tmp_path, "../secrets")
        assert _issue(exc).code == "traversal"

    def test_rejects_existing_symlink_escape(self, tmp_path: Path):
        outside = tmp_path.parent / f"{tmp_path.name}-outside"
        outside.mkdir()
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ps.PathSsrError) as exc:
            ps.resolve_path_within_base(tmp_path, "link/secret.txt")
        assert _issue(exc).code == "traversal"


class TestNormalizePublicUrl:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("HTTPS://Example.COM/", "https://example.com"),
            ("http://example.com:80/path", "http://example.com/path"),
            ("https://example.com:443/path?Q=1#frag", "https://example.com/path?Q=1"),
            ("https://example.com:8443/path", "https://example.com:8443/path"),
            ("https://éxample.com/", "https://xn--xample-9ua.com"),
            (
                "https://[2606:4700:4700::1111]/dns-query",
                "https://[2606:4700:4700::1111]/dns-query",
            ),
        ],
    )
    def test_returns_canonical_http_url(self, raw: str, expected: str):
        assert ps.normalize_public_url(raw) == expected

    @pytest.mark.parametrize(
        "value, code",
        [
            (None, "type"),
            ("", "empty"),
            ("ftp://example.com", "scheme"),
            ("file:///etc/passwd", "scheme"),
            ("//example.com/path", "scheme"),
            ("https:///path", "host"),
            ("https://user:pass@example.com", "userinfo"),
            ("https://example.com:bad", "port"),
            ("https://exa mple.com", "host"),
            ("https://example.com/\nHost: localhost", "control_char"),
        ],
    )
    def test_rejects_invalid_or_fetch_unsafe_url_syntax(self, value: object, code: str):
        with pytest.raises(ps.PathSsrError) as exc:
            ps.normalize_public_url(value)
        assert _issue(exc).code == code


class TestIsPublicDestination:
    @pytest.mark.parametrize(
        "host",
        [
            "example.com",
            "www.example.com",
            "8.8.8.8",
            "1.1.1.1",
            "2606:4700:4700::1111",
        ],
    )
    def test_accepts_public_hosts(self, host: str):
        assert ps.is_public_destination(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "",
            None,
            "localhost",
            "LOCALHOST",
            "ip6-localhost",
            "service.local",
            "service.internal",
            "router.lan",
            "router.home",
            "router.home.arpa",
            "hidden.onion",
            "hidden.onion.",
            "anything.localhost",
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.0.1",
            ps.CLOUD_METADATA_IP,
            "224.0.0.1",
            "255.255.255.255",
            "::1",
            "fe80::1",
            "fc00::1",
            "ff02::1",
            "2130706433",
        ],
    )
    def test_blocks_unsafe_hosts(self, host: object):
        assert ps.is_public_destination(host) is False


class TestValidatePublicUrl:
    def test_accepts_and_canonicalizes_public_url(self):
        assert ps.validate_public_url("HTTPS://Example.COM/") == "https://example.com"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/admin",
            f"http://{ps.CLOUD_METADATA_IP}/latest/meta-data/",
            "http://192.168.1.1/",
            "http://hidden.onion/",
            "http://2130706433/",
            "http://[::1]/",
        ],
    )
    def test_rejects_static_ssrf_destinations(self, url: str):
        with pytest.raises(ps.PathSsrError) as exc:
            ps.validate_public_url(url)
        assert _issue(exc).field == "url"
        assert _issue(exc).code == "blocked_destination"

    def test_extract_hostname_returns_normalized_hostname(self):
        assert ps.extract_hostname("https://Example.COM:443/path") == "example.com"
