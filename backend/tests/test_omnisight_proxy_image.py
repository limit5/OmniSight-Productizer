"""KS.3.1 — contract tests for the omnisight-proxy container image.

Pins the narrow scope of the first BYOG proxy row: a distroless,
non-root, <100 MB, single Go binary image. Later KS.3 rows own mTLS,
nonce replay protection, provider config, forwarding, streaming, and
heartbeat semantics; this file only verifies the image skeleton.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PROXY = REPO_ROOT / "Dockerfile.omnisight-proxy"
PROXY_ROOT = REPO_ROOT / "omnisight-proxy"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-publish.yml"

PROXY_BUDGET_MIB = 100
PROXY_IMAGE_TAG = "omnisight-proxy:ks3.1-test"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    assert DOCKERFILE_PROXY.exists(), f"missing: {DOCKERFILE_PROXY}"
    return DOCKERFILE_PROXY.read_text()


def test_proxy_dockerfile_is_go_multistage(dockerfile_text: str) -> None:
    assert re.search(r"^FROM\s+golang:\$\{GO_VERSION\}-alpine\s+AS\s+builder\b", dockerfile_text, re.M), (
        "proxy Dockerfile must use the mirrored Go builder stage"
    )
    assert re.search(r"^FROM\s+gcr\.io/distroless/static-debian12:nonroot\s+AS\s+runtime\b", dockerfile_text, re.M), (
        "proxy runtime must be distroless/static-debian12:nonroot"
    )


def test_proxy_builds_fully_static_single_binary(dockerfile_text: str) -> None:
    assert "CGO_ENABLED=0" in dockerfile_text, (
        "proxy binary must be fully static for distroless/static runtime"
    )
    assert 'go build -trimpath -ldflags "-s -w -buildid="' in dockerfile_text, (
        "proxy build must strip debug tables and build id to keep image small"
    )
    assert "-o /out/omnisight-proxy ./cmd/omnisight-proxy" in dockerfile_text
    assert "COPY --from=builder /out/omnisight-proxy /app/omnisight-proxy" in dockerfile_text
    assert 'ENTRYPOINT ["/app/omnisight-proxy"]' in dockerfile_text


def test_proxy_runtime_has_no_shell_or_package_manager(dockerfile_text: str) -> None:
    runtime_start = dockerfile_text.index("AS runtime")
    runtime_block = dockerfile_text[runtime_start:]
    for forbidden in ("apk add", "apt-get", " sh ", "/bin/sh", "bash", "curl", "wget"):
        assert forbidden not in runtime_block, (
            f"runtime stage must not contain {forbidden!r}; distroless image should carry only the binary"
        )
    assert "USER nonroot:nonroot" in runtime_block


def test_proxy_go_module_has_no_external_dependencies() -> None:
    go_mod = PROXY_ROOT / "go.mod"
    assert go_mod.exists(), f"missing: {go_mod}"
    body = go_mod.read_text()
    assert "require " not in body, (
        "KS.3.1 proxy must stay stdlib-only; new deps increase image and audit surface"
    )
    assert not (PROXY_ROOT / "go.sum").exists(), (
        "stdlib-only proxy should not need go.sum"
    )


def test_proxy_env_reads_are_centralised_in_config() -> None:
    offenders: list[str] = []
    for path in PROXY_ROOT.rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        body = path.read_text()
        if "os.Getenv" in body or "os.LookupEnv" in body:
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel != "omnisight-proxy/internal/config/config.go":
                offenders.append(rel)
    assert offenders == [], (
        "proxy env reads must stay centralised in internal/config: "
        + ", ".join(offenders)
    )


def test_publish_workflow_builds_proxy_image() -> None:
    workflow = WORKFLOW_PATH.read_text()
    assert "omnisight-proxy" in workflow, (
        "Docker publish workflow must include the customer-side proxy image"
    )
    assert "Dockerfile.omnisight-proxy" in workflow


def _live_image_check_enabled() -> bool:
    return os.environ.get("OMNISIGHT_TEST_PROXY_IMAGE", "").lower() in (
        "1", "true", "yes", "on",
    )


def _docker_image_size_bytes(tag: str) -> int | None:
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


@pytest.mark.skipif(
    not _live_image_check_enabled(),
    reason="set OMNISIGHT_TEST_PROXY_IMAGE=1 to build and measure the proxy image",
)
def test_live_proxy_image_under_100mb() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    build = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(DOCKERFILE_PROXY),
            "-t",
            PROXY_IMAGE_TAG,
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert build.returncode == 0, build.stderr[-4000:]
    size_bytes = _docker_image_size_bytes(PROXY_IMAGE_TAG)
    assert size_bytes is not None, "proxy image was built but docker inspect could not read its size"
    size_mib = size_bytes / (1024 * 1024)
    assert size_mib < PROXY_BUDGET_MIB, (
        f"proxy image is {size_mib:.1f} MiB, over the {PROXY_BUDGET_MIB} MiB budget"
    )
