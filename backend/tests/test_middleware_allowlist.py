"""OP-1152 -- middleware public path allowlist contract tests."""

from __future__ import annotations

import pytest

from backend.middleware_allowlist import PUBLIC_PATH_ALLOWLIST, is_public


@pytest.mark.parametrize(
    "path",
    [
        "/livez",
        "/readyz",
        "/healthz",
        "/api/v1/livez",
        "/api/v1/readyz",
        "/api/v1/healthz",
        "/metrics",
        "/openapi.json",
        "/docs",
        "/redoc",
    ],
)
def test_known_public_path_is_public(path):
    assert is_public(path) is True


def test_non_public_path_is_not_public():
    assert is_public("/api/v1/users") is False


def test_health_not_in_allowlist_yet():
    assert is_public("/health") is False


def test_case_sensitivity():
    assert is_public("/LIVEZ") is False


def test_trailing_slash_not_normalized():
    assert is_public("/livez/") is False


def test_allowlist_is_immutable():
    assert isinstance(PUBLIC_PATH_ALLOWLIST, frozenset)
    with pytest.raises(AttributeError):
        PUBLIC_PATH_ALLOWLIST.add("/health")
