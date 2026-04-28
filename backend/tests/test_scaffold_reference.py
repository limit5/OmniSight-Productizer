"""W12.4 — :mod:`backend.scaffold_reference` contract tests.

Pins the ``--reference-url`` flag wiring against:

* Public-surface invariants — alphabetised ``__all__``, canonical
  flag literal, scheme allowlist, length cap.
* :func:`add_reference_url_argument` — registers exactly the
  expected flag, default ``None``, dest ``reference_url``, idempotency
  protected by argparse.
* :func:`normalize_reference_url` — accepts None / empty / whitespace
  as "absent"; rejects non-``http(s)``; rejects missing scheme;
  rejects overlong; trims whitespace; rejects non-string types.
* :func:`resolve_reference_url` — ``None`` short-circuit; injected
  fetcher routes through :func:`extract_brand_from_url`; fail-soft
  envelope preserved (network failure ⇒ empty :class:`BrandSpec`
  carrying provenance, not exception); ``ReferenceURLError`` for
  caller-side bugs.
* End-to-end argparse → resolve plumbing — operator passes the flag
  on the CLI, resolver returns a usable :class:`BrandSpec`.
* **Network discipline** — no test in this file may hit the live
  network.  Every resolve path injects a fake fetcher.
"""

from __future__ import annotations

import argparse

import pytest

from backend import scaffold_reference as sr
from backend.brand_spec import BrandSpec
from backend.scaffold_reference import (
    MAX_REFERENCE_URL_LENGTH,
    REFERENCE_URL_DEST,
    REFERENCE_URL_FLAG,
    SUPPORTED_REFERENCE_SCHEMES,
    ReferenceURLError,
    add_reference_url_argument,
    normalize_reference_url,
    resolve_reference_url,
)


# ── Module-level invariants ─────────────────────────────────────────


class TestModuleInvariants:
    def test_exports_alphabetised(self):
        assert sr.__all__ == sorted(sr.__all__)

    def test_public_surface_exported(self):
        for name in (
            "MAX_REFERENCE_URL_LENGTH",
            "REFERENCE_URL_DEST",
            "REFERENCE_URL_FLAG",
            "ReferenceURLError",
            "SUPPORTED_REFERENCE_SCHEMES",
            "add_reference_url_argument",
            "normalize_reference_url",
            "resolve_reference_url",
        ):
            assert name in sr.__all__, name

    def test_flag_literal_pinned(self):
        # Operators / docs / future scripts grep for this exact string.
        assert REFERENCE_URL_FLAG == "--reference-url"

    def test_dest_matches_flag(self):
        # PEP 8 snake_case mirror of the flag — argparse dest convention.
        assert REFERENCE_URL_DEST == "reference_url"

    def test_supported_schemes(self):
        assert SUPPORTED_REFERENCE_SCHEMES == frozenset({"http", "https"})

    def test_supported_schemes_is_frozenset(self):
        # Mutability would let test pollution across files corrupt the
        # allowlist — pin the type so a regression surfaces here.
        assert isinstance(SUPPORTED_REFERENCE_SCHEMES, frozenset)

    def test_max_url_length_matches_url_to_reference(self):
        # Defence-in-depth — keep aligned with the sibling URL-fetch
        # entry point so one accepts what the other rejects never
        # surprises an operator.
        from backend.url_to_reference import MAX_URL_LENGTH

        assert MAX_REFERENCE_URL_LENGTH == MAX_URL_LENGTH

    def test_reference_url_error_subclass_of_value_error(self):
        # Existing `except ValueError` chains in scaffolders need to
        # catch us cleanly.
        assert issubclass(ReferenceURLError, ValueError)


# ── add_reference_url_argument ──────────────────────────────────────


class TestAddReferenceUrlArgument:
    def test_registers_flag(self):
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        ns = parser.parse_args([REFERENCE_URL_FLAG, "https://example.com"])
        assert getattr(ns, REFERENCE_URL_DEST) == "https://example.com"

    def test_default_none_when_absent(self):
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        ns = parser.parse_args([])
        assert getattr(ns, REFERENCE_URL_DEST) is None

    def test_help_text_appears(self):
        # The default help text mentions BrandSpec + the W12.5 file
        # path so an operator running ``--help`` understands the wire.
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        help_str = parser.format_help()
        assert REFERENCE_URL_FLAG in help_str
        assert "BrandSpec" in help_str

    def test_custom_help_text(self):
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser, help_text="custom-help-text-marker")
        assert "custom-help-text-marker" in parser.format_help()

    def test_returns_action(self):
        parser = argparse.ArgumentParser()
        action = add_reference_url_argument(parser)
        assert isinstance(action, argparse.Action)
        assert action.dest == REFERENCE_URL_DEST

    def test_metavar_url(self):
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        assert "URL" in parser.format_help()

    def test_idempotency_raises_on_double_register(self):
        # argparse enforces uniqueness — surface the misuse loud.
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        with pytest.raises(argparse.ArgumentError):
            add_reference_url_argument(parser)

    def test_rejects_non_parser(self):
        with pytest.raises(TypeError):
            add_reference_url_argument("not-a-parser")  # type: ignore[arg-type]

    def test_works_with_subparsers(self):
        # The future unified ``scripts/scaffold.py`` will dispatch via
        # subparsers — make sure registration on a sub still works.
        parent = argparse.ArgumentParser()
        sub = parent.add_subparsers(dest="cmd")
        nextjs = sub.add_parser("nextjs")
        add_reference_url_argument(nextjs)
        ns = parent.parse_args(["nextjs", REFERENCE_URL_FLAG, "https://x.com"])
        assert getattr(ns, REFERENCE_URL_DEST) == "https://x.com"


# ── normalize_reference_url ─────────────────────────────────────────


class TestNormalizeReferenceUrl:
    def test_none_returns_none(self):
        assert normalize_reference_url(None) is None

    def test_empty_string_returns_none(self):
        assert normalize_reference_url("") is None

    def test_whitespace_only_returns_none(self):
        # YAML / .env interpolations sometimes resolve to "" or "   "
        # — treat them as absence so the scaffold does not crash.
        assert normalize_reference_url("   ") is None
        assert normalize_reference_url("\t\n  \n") is None

    def test_https_url_passes_through(self):
        assert normalize_reference_url("https://example.com") == "https://example.com"

    def test_http_url_passes_through(self):
        assert normalize_reference_url("http://example.com") == "http://example.com"

    def test_strips_surrounding_whitespace(self):
        assert (
            normalize_reference_url("  https://example.com/path  ")
            == "https://example.com/path"
        )

    def test_preserves_path_query_fragment(self):
        url = "https://example.com/a/b?x=1&y=2#frag"
        assert normalize_reference_url(url) == url

    def test_rejects_ftp_scheme(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url("ftp://example.com")

    def test_rejects_javascript_scheme(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url("javascript:alert(1)")

    def test_rejects_file_scheme(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url("file:///etc/passwd")

    def test_rejects_data_uri(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url("data:text/html,<h1>hi</h1>")

    def test_rejects_missing_scheme(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url("example.com/path")

    def test_rejects_overlong(self):
        too_long = "https://" + ("a" * (MAX_REFERENCE_URL_LENGTH + 1))
        with pytest.raises(ReferenceURLError):
            normalize_reference_url(too_long)

    def test_accepts_at_length_cap(self):
        # ``len(trimmed) > MAX`` rejects, ``len == MAX`` accepts —
        # boundary check.
        url = "https://example.com/" + ("x" * (MAX_REFERENCE_URL_LENGTH - len("https://example.com/")))
        assert len(url) == MAX_REFERENCE_URL_LENGTH
        assert normalize_reference_url(url) == url

    def test_rejects_non_string_type(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url(12345)  # type: ignore[arg-type]

    def test_rejects_bytes(self):
        with pytest.raises(ReferenceURLError):
            normalize_reference_url(b"https://example.com")  # type: ignore[arg-type]

    def test_scheme_case_insensitive(self):
        # CLI users sometimes paste from URL bars that uppercase the
        # scheme; we lowercase before the allowlist check.
        assert (
            normalize_reference_url("HTTPS://example.com")
            == "HTTPS://example.com"
        )


# ── resolve_reference_url ───────────────────────────────────────────


class TestResolveReferenceUrl:
    def _payload(self) -> str:
        # Synthetic page with one of every dimension the extractor
        # surfaces — keeps the assertion concrete + readable.
        return (
            "<html><head><style>"
            ":root{--brand:#0066ff;}"
            "body{font-family:'Inter',sans-serif;color:#0066ff;}"
            "h1{font-size:48px;}"
            "h2{font-size:32px;}"
            "div{padding:8px;margin:16px;border-radius:4px;}"
            "</style></head><body></body></html>"
        )

    def _fake_fetch_ok(self, url: str) -> tuple[int, str]:
        return 200, self._payload()

    def _fake_now(self) -> str:
        return "2026-04-29T00:00:00+00:00"

    def test_none_returns_none(self):
        assert resolve_reference_url(None) is None

    def test_empty_string_returns_none(self):
        assert resolve_reference_url("") is None

    def test_whitespace_returns_none(self):
        assert resolve_reference_url("   \t\n") is None

    def test_returns_brand_spec_on_success(self):
        spec = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert isinstance(spec, BrandSpec)
        assert spec.source_url == "https://example.com"
        assert spec.extracted_at == "2026-04-29T00:00:00+00:00"

    def test_palette_extracted(self):
        spec = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec is not None
        assert "#0066ff" in spec.palette

    def test_fonts_extracted(self):
        spec = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec is not None
        assert "inter" in spec.fonts  # canonicalised lowercase

    def test_heading_extracted(self):
        spec = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec is not None
        assert spec.heading.h1 == 48.0
        assert spec.heading.h2 == 32.0

    def test_spacing_extracted(self):
        spec = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec is not None
        # spacing is canonicalised (sorted ascending + dedup)
        assert 8.0 in spec.spacing
        assert 16.0 in spec.spacing

    def test_radius_extracted(self):
        spec = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec is not None
        assert 4.0 in spec.radius

    def test_strips_whitespace_before_resolving(self):
        spec = resolve_reference_url(
            "  https://example.com  ",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec is not None
        # source_url should be the *trimmed* string, not the raw one
        assert spec.source_url == "https://example.com"

    def test_fail_soft_on_fetch_exception(self):
        # Network blip ⇒ empty spec, NOT exception (W12.5 will write
        # this to .omnisight/brand.json so an audit record exists).
        def boom(url: str) -> tuple[int, str]:
            raise OSError("network unreachable")

        spec = resolve_reference_url(
            "https://flaky.example.com",
            fetch=boom,
            now=self._fake_now,
        )
        assert spec is not None
        assert spec.is_empty
        assert spec.source_url == "https://flaky.example.com"
        assert spec.extracted_at == "2026-04-29T00:00:00+00:00"

    def test_fail_soft_on_non_200(self):
        def four_oh_four(url: str) -> tuple[int, str]:
            return 404, ""

        spec = resolve_reference_url(
            "https://gone.example.com",
            fetch=four_oh_four,
            now=self._fake_now,
        )
        assert spec is not None
        assert spec.is_empty
        assert spec.source_url == "https://gone.example.com"

    def test_fail_soft_on_empty_body(self):
        def empty_ok(url: str) -> tuple[int, str]:
            return 200, ""

        spec = resolve_reference_url(
            "https://blank.example.com",
            fetch=empty_ok,
            now=self._fake_now,
        )
        assert spec is not None
        assert spec.is_empty

    def test_raises_on_bad_scheme(self):
        # Caller bug — fail loud, do NOT silently fail-soft.
        def never_called(url: str) -> tuple[int, str]:
            raise AssertionError("fetch must not be invoked for bad scheme")

        with pytest.raises(ReferenceURLError):
            resolve_reference_url("ftp://example.com", fetch=never_called)

    def test_raises_on_overlong(self):
        with pytest.raises(ReferenceURLError):
            resolve_reference_url(
                "https://" + ("a" * (MAX_REFERENCE_URL_LENGTH + 1))
            )

    def test_raises_on_non_string(self):
        with pytest.raises(ReferenceURLError):
            resolve_reference_url(12345)  # type: ignore[arg-type]

    def test_determinism_identical_input(self):
        # The W12.6 reference matrix relies on the extractor being
        # deterministic — verify the resolver façade preserves it.
        spec_a = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        spec_b = resolve_reference_url(
            "https://example.com",
            fetch=self._fake_fetch_ok,
            now=self._fake_now,
        )
        assert spec_a == spec_b


# ── End-to-end argparse → resolve plumbing ──────────────────────────


class TestEndToEndPlumbing:
    def test_cli_to_resolver_round_trip(self):
        # Operator runs:  scaffold --reference-url https://example.com
        # The resolver pulls the value out of the parsed Namespace and
        # produces a BrandSpec.
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        ns = parser.parse_args([REFERENCE_URL_FLAG, "https://example.com"])

        def fake_fetch(url: str) -> tuple[int, str]:
            return 200, "<style>h1{font-size:64px;color:#abcdef}</style>"

        spec = resolve_reference_url(
            getattr(ns, REFERENCE_URL_DEST),
            fetch=fake_fetch,
            now=lambda: "2026-04-29T00:00:00+00:00",
        )
        assert spec is not None
        assert spec.source_url == "https://example.com"
        assert spec.heading.h1 == 64.0
        assert "#abcdef" in spec.palette

    def test_cli_absence_yields_none_spec(self):
        # Operator runs:  scaffold       (no --reference-url)
        # The resolver returns None — caller falls back to project tokens.
        parser = argparse.ArgumentParser()
        add_reference_url_argument(parser)
        ns = parser.parse_args([])
        spec = resolve_reference_url(getattr(ns, REFERENCE_URL_DEST))
        assert spec is None


# ── Network discipline guard ────────────────────────────────────────


class TestNetworkDiscipline:
    def test_module_does_not_import_urllib_at_load(self):
        # The default fetcher in brand_extractor lazy-imports
        # urllib.request inside ``_default_fetch``.  This module sits
        # one layer above and must not trigger that import on its own
        # load — operators should be able to ``import scaffold_reference``
        # in air-gapped builds without the network stack waking up.
        import sys

        # We cannot assert urllib.request is not imported at all
        # (other test files / pytest collection trigger it).  But we
        # CAN assert that scaffold_reference's own module body does not
        # reference it.
        import backend.scaffold_reference as mod

        # No urllib symbols leak into the module namespace.
        assert not any(
            name.startswith("urllib") for name in dir(mod)
        ), [name for name in dir(mod) if name.startswith("urllib")]
        # No bare import of urllib at module level.
        src = open(mod.__file__, encoding="utf-8").read()
        assert "import urllib" not in src
        assert "from urllib" not in src
        # Sanity — sys is loaded (we just used it) so the assertion is
        # not vacuously hitting a "no imports at all" case.
        assert "sys" in sys.modules
