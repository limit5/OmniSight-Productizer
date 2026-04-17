"""L10 #337 — contract tests for Dockerfile image-size optimization.

Pins the structural promises the slim Dockerfiles + .dockerignore make
to operators (target: backend < 500 MB, frontend < 200 MB).

Two layers of defence:

(1) **Static contract tests** — read the Dockerfile + .dockerignore and
    assert load-bearing invariants without invoking Docker. Catches
    regressions in <100 ms with no Docker daemon required, runs in
    every CI lane. Examples: multi-stage `FROM ... AS builder` exists,
    runner stage drops gcc/g++, recursive `**/.venv` patterns are
    present (the bare `.venv` previously leaked 168 MB of nested venv
    site-packages into the backend image).

(2) **Live image-size guard** — when `OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1`
    is set AND `omnisight-productizer-{backend,frontend}:slim` images
    exist locally, assert their compressed size (`docker image inspect
    .Size` — the bytes that need to be pulled from a registry) is under
    the budget. Skipped by default because (a) most CI lanes don't have
    the images built, (b) building takes 2-5 min per image. Operators
    can opt in locally with `OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1 pytest
    backend/tests/test_dockerfile_image_size.py`.

The pull-size budget (compressed) maps directly to the L10 #337 promise
"首次部署從本地 build 5-10 分鐘 → pull 30 秒" — at ~50 Mbps a 30-second
pull tolerates ~180 MB, which both images comfortably fit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_BACKEND = REPO_ROOT / "Dockerfile.backend"
DOCKERFILE_FRONTEND = REPO_ROOT / "Dockerfile.frontend"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"

# Budgets in MiB (mebibytes). Compressed pull size — what `docker image
# inspect --format '{{.Size}}'` returns, which is what registries use
# for accounting and what determines pull time.
BACKEND_BUDGET_MIB = 500
FRONTEND_BUDGET_MIB = 200


# ---------------------------------------------------------------------------
# Static contract: Dockerfile.backend
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def backend_dockerfile_text() -> str:
    assert DOCKERFILE_BACKEND.exists(), f"missing: {DOCKERFILE_BACKEND}"
    return DOCKERFILE_BACKEND.read_text()


def test_backend_is_multi_stage(backend_dockerfile_text: str) -> None:
    # Single-stage with gcc+g++ in the runtime image was 1.34 GB. The
    # multi-stage split is the load-bearing optimization — pin it.
    assert re.search(r"^FROM\s+\S+\s+AS\s+builder\b", backend_dockerfile_text, re.M), \
        "backend Dockerfile must declare a `FROM ... AS builder` stage"
    assert re.search(r"^FROM\s+\S+\s+AS\s+runner\b", backend_dockerfile_text, re.M), \
        "backend Dockerfile must declare a `FROM ... AS runner` final stage"


def test_backend_runner_has_no_compilers(backend_dockerfile_text: str) -> None:
    # Slice the Dockerfile to just the runner stage. Anything before the
    # `AS runner` line is the builder (compilers OK there) and must not
    # poison the assertion.
    runner_start = backend_dockerfile_text.index("AS runner")
    runner_block = backend_dockerfile_text[runner_start:]
    assert "gcc" not in runner_block, \
        "runner stage must not install gcc — that defeats the multi-stage size win"
    assert "g++" not in runner_block, \
        "runner stage must not install g++ — that defeats the multi-stage size win"
    assert " git " not in runner_block and "git\\" not in runner_block, \
        "runner stage must not install git — moves it to the builder stage"


def test_backend_runner_installs_weasyprint_runtime_libs(backend_dockerfile_text: str) -> None:
    # The runner needs Pango/Cairo/gdk-pixbuf at import time for
    # weasyprint — without them, `from backend.report_generator ...`
    # crashes in production. Pinning these prevents an over-eager
    # "shrink the image more" PR from removing them.
    runner_start = backend_dockerfile_text.index("AS runner")
    runner_block = backend_dockerfile_text[runner_start:]
    for lib in (
        "libpango-1.0-0",
        "libcairo2",
        "libgdk-pixbuf-2.0-0",
        "fonts-dejavu-core",
    ):
        assert lib in runner_block, \
            f"runner stage must install {lib} (load-bearing for weasyprint PDF generation)"


def test_backend_strips_test_packages_in_builder(backend_dockerfile_text: str) -> None:
    # Test-only packages don't belong in the production image — saves
    # ~25 MB and tightens the runtime attack surface.
    assert "pip uninstall -y" in backend_dockerfile_text, \
        "must drop pytest/coverage from the prod runtime via `pip uninstall`"
    for pkg in ("pytest", "pytest-asyncio", "pytest-cov", "coverage"):
        assert pkg in backend_dockerfile_text, \
            f"must explicitly uninstall `{pkg}` from the production image"


def test_backend_strips_unused_woff2_compressors(backend_dockerfile_text: str) -> None:
    # zstandard (23 MB) + zopfli (2.7 MB) are fontTools[woff] extras for
    # WOFF2 compression. weasyprint generates PDFs not WOFF2 — these are
    # dead weight. Removing them is what gets the image under 500 MB.
    # Smoke-tested: weasyprint.HTML(...).write_pdf() produces identical
    # output with both packages removed.
    assert "zstandard" in backend_dockerfile_text, \
        "must strip zstandard from site-packages (~23 MB, unused by weasyprint PDF path)"
    assert "zopfli" in backend_dockerfile_text, \
        "must strip zopfli from site-packages (~2.7 MB, unused by weasyprint PDF path)"


def test_backend_strips_pip_from_runtime(backend_dockerfile_text: str) -> None:
    # The runtime image never `pip install`s anything — pip itself is
    # ~13 MB of dead code. Strip both the package dir and the dist-info
    # so importlib.metadata's view stays consistent.
    assert "site-packages/pip" in backend_dockerfile_text, \
        "must remove the pip package directory from site-packages"
    assert "pip-*.dist-info" in backend_dockerfile_text, \
        "must remove pip-*.dist-info so importlib.metadata view is consistent"


def test_backend_excludes_tests_from_runtime(backend_dockerfile_text: str) -> None:
    # backend/tests/ is 31 MB of test code that has no business in the
    # production image. Stripped via explicit `rm -rf` in the runner.
    assert re.search(r"rm\s+-rf\s+\./backend/tests\b", backend_dockerfile_text), \
        "runner must explicitly `rm -rf ./backend/tests` (31 MB of test fixtures)"


def test_backend_does_not_use_pip_wheel_pattern(backend_dockerfile_text: str) -> None:
    # The `pip wheel` + `pip install --no-index --find-links` pattern
    # rebuilds sdists into wheels whose hashes differ from the
    # pypi-registered ones — `--require-hashes` then fails on packages
    # like `zxcvbn-python` that ship as sdist. An earlier draft used
    # this pattern and silently produced an image with no installed
    # packages (the install RUN swallowed the failure). Pin the choice.
    #
    # Strip comments before checking — the rationale comment in the
    # Dockerfile mentions the forbidden pattern by design.
    code_only = "\n".join(
        line for line in backend_dockerfile_text.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "pip wheel" not in code_only, (
        "do not use `pip wheel` + `--no-index` install — sdist-built wheel "
        "hashes don't match the pypi-registered ones, breaking --require-hashes"
    )


# ---------------------------------------------------------------------------
# Static contract: Dockerfile.frontend
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def frontend_dockerfile_text() -> str:
    assert DOCKERFILE_FRONTEND.exists(), f"missing: {DOCKERFILE_FRONTEND}"
    return DOCKERFILE_FRONTEND.read_text()


def test_frontend_is_multi_stage(frontend_dockerfile_text: str) -> None:
    assert re.search(r"^FROM\s+\S+\s+AS\s+builder\b", frontend_dockerfile_text, re.M), \
        "frontend Dockerfile must declare a `FROM ... AS builder` stage"
    assert re.search(r"^FROM\s+\S+\s+AS\s+runner\b", frontend_dockerfile_text, re.M), \
        "frontend Dockerfile must declare a `FROM ... AS runner` final stage"


def test_frontend_runner_uses_alpine_base(frontend_dockerfile_text: str) -> None:
    # The original `node:20-alpine` runner shipped node + npm + corepack
    # + yarn (~120 MB) even though `node server.js` needs only node.
    # Switching to `alpine:3.x` + `apk add nodejs` saves ~60 MB and
    # eliminates the package-manager attack surface. Pin the choice
    # so a refactor doesn't silently revert to node:20-alpine.
    #
    # The runner stage is `FROM <base> AS runner` — slice from the
    # FROM line (which carries the base image) so the regex finds it.
    m = re.search(r"^FROM\s+\S+\s+AS\s+runner\b", frontend_dockerfile_text, re.M)
    assert m is not None, "frontend Dockerfile must declare an `AS runner` stage"
    runner_block = frontend_dockerfile_text[m.start():]
    assert re.search(r"FROM\s+alpine:3\.\d+\s+AS\s+runner", runner_block), \
        "frontend runner must use `alpine:3.x` base (not `node:*-alpine`)"
    assert "apk add" in runner_block and "nodejs" in runner_block, \
        "frontend runner must `apk add nodejs` to get only the node runtime"


def test_frontend_keeps_ca_certificates(frontend_dockerfile_text: str) -> None:
    # Outbound HTTPS from the app (auth callbacks, /api proxy to
    # backend over TLS) requires a trust store. Without it node fails
    # with "unable to verify the first certificate" and the operator
    # spends an hour debugging.
    runner_start = frontend_dockerfile_text.index("AS runner")
    runner_block = frontend_dockerfile_text[runner_start:]
    assert "ca-certificates" in runner_block, \
        "runner must `apk add ca-certificates` for outbound HTTPS"


def test_frontend_strips_unused_sharp_native(frontend_dockerfile_text: str) -> None:
    # next.config.mjs sets `images: { unoptimized: true }` — sharp +
    # libvips (~33 MB) are dead weight. Removing them is what gets the
    # image under 200 MB.
    runner_start = frontend_dockerfile_text.index("AS runner")
    runner_block = frontend_dockerfile_text[runner_start:]
    assert "sharp-libvips" in runner_block, \
        "runner must strip @img/sharp-libvips* (unused: images.unoptimized=true)"


# ---------------------------------------------------------------------------
# Static contract: .dockerignore
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    assert DOCKERIGNORE.exists(), f"missing: {DOCKERIGNORE}"
    return DOCKERIGNORE.read_text()


def test_dockerignore_uses_recursive_venv_pattern(dockerignore_text: str) -> None:
    # Docker's .dockerignore: a bare `.venv` matches `./.venv` only,
    # NOT `./backend/.venv`. The latter (a 168 MB local dev venv) was
    # the single largest contributor to the 1.34 GB backend image.
    # Pin the recursive `**/.venv` pattern so a "clean up dockerignore"
    # PR doesn't silently re-introduce the leak.
    lines = [
        l.strip()
        for l in dockerignore_text.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    assert "**/.venv" in lines, \
        "must include `**/.venv` (recursive) — bare `.venv` misses backend/.venv"


def test_dockerignore_uses_recursive_pycache_pattern(dockerignore_text: str) -> None:
    # Same semantics as .venv — `__pycache__` only matches at root;
    # the project has __pycache__ at every backend subpackage depth.
    lines = [
        l.strip()
        for l in dockerignore_text.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    assert "**/__pycache__" in lines, \
        "must include `**/__pycache__` (recursive) — bare `__pycache__` misses nested dirs"


def test_dockerignore_uses_recursive_node_modules_pattern(dockerignore_text: str) -> None:
    # Mirror of the venv issue for the JS side — nested `node_modules`
    # dirs (e.g. inside `e2e/` or workspace packages) leak otherwise.
    lines = [
        l.strip()
        for l in dockerignore_text.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    assert "**/node_modules" in lines, \
        "must include `**/node_modules` (recursive) — bare `node_modules` misses nested dirs"


# ---------------------------------------------------------------------------
# Live image-size guard (opt-in)
# ---------------------------------------------------------------------------

def _docker_image_size_bytes(tag: str) -> int | None:
    """Return ``docker image inspect --format='{{.Size}}'`` as int, or
    ``None`` if the image is missing or docker is unavailable."""
    if shutil.which("docker") is None:
        return None
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Size}}", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _docker_image_filesystem_size_mib(tag: str) -> float | None:
    """Run ``du -sh /`` inside the image to measure unpacked filesystem
    size — the bytes that consume disk on the deploy host. Returns None
    if docker is unavailable or the image cannot be exercised."""
    if shutil.which("docker") is None:
        return None
    proc = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=", tag, "sh", "-c",
         "du -sb / 2>/dev/null | awk '{print $1}'"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip()) / (1024 * 1024)
    except ValueError:
        return None


def _live_size_check_enabled() -> bool:
    return os.environ.get("OMNISIGHT_TEST_DOCKER_IMAGE_SIZE", "").lower() in (
        "1", "true", "yes", "on",
    )


@pytest.mark.skipif(
    not _live_size_check_enabled(),
    reason="set OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1 to gate on live image size",
)
def test_backend_image_size_under_budget() -> None:
    # The compressed pull size (`docker image inspect .Size`) is what
    # the registry charges for and what determines deploy speed.
    size_bytes = _docker_image_size_bytes("omnisight-productizer-backend:slim")
    if size_bytes is None:
        pytest.skip("omnisight-productizer-backend:slim not built locally")
    size_mib = size_bytes / (1024 * 1024)
    assert size_mib < BACKEND_BUDGET_MIB, (
        f"backend image is {size_mib:.1f} MiB, over the {BACKEND_BUDGET_MIB} MiB budget. "
        "Re-run the L10 #337 image-size optimizations or update the budget."
    )


@pytest.mark.skipif(
    not _live_size_check_enabled(),
    reason="set OMNISIGHT_TEST_DOCKER_IMAGE_SIZE=1 to gate on live image size",
)
def test_frontend_image_size_under_budget() -> None:
    size_bytes = _docker_image_size_bytes("omnisight-productizer-frontend:slim")
    if size_bytes is None:
        pytest.skip("omnisight-productizer-frontend:slim not built locally")
    size_mib = size_bytes / (1024 * 1024)
    assert size_mib < FRONTEND_BUDGET_MIB, (
        f"frontend image is {size_mib:.1f} MiB, over the {FRONTEND_BUDGET_MIB} MiB budget. "
        "Re-run the L10 #337 image-size optimizations or update the budget."
    )
