"""X3 #299 — Build & package adapters.

Single dispatcher for every distributable artifact format an OmniSight
software skill can produce. Sits one layer above ``software_simulator``
(X1 #297) and mirrors the W4 ``deploy/base.py`` shape so the CLI
driver, agents, and HMI all consume the same adapter interface.

Supported formats (per X3 ticket):

    docker        — OCI image build + push (GHCR / Docker Hub / ECR / private)
    helm          — k8s Helm chart skeleton + lint + package
    deb           — Debian / Ubuntu .deb (via dpkg-deb / fpm fallback)
    rpm           — RHEL / Fedora .rpm (via rpmbuild / fpm fallback)
    msi           — Windows MSI (via WiX / candle+light)
    nsis          — Windows NSIS installer (via makensis)
    dmg           — macOS DMG (via hdiutil / create-dmg)
    pkg           — macOS PKG (via pkgbuild + productbuild)

Skill-hook adapters (X3 ticket — language-native release tools):

    cargo-dist        — Rust multi-platform binary release
    goreleaser        — Go multi-platform binary release
    pyinstaller       — Python single-file executable
    electron-builder  — Electron desktop multi-platform installer

Design
------
Each external tool (``docker`` / ``helm`` / ``dpkg-deb`` / ``rpmbuild`` /
``makensis`` / ``hdiutil`` / ``pkgbuild`` / ``cargo-dist`` /
``goreleaser`` / ``pyinstaller`` / ``electron-builder``) is **optional**.
If the binary is not on PATH the adapter returns a ``mock`` result with
``available=False`` — the caller can distinguish "tool missing" from
"build failed". Nothing here fabricates a real artifact when the runner
is absent. This is the same contract used by ``software_simulator``.

Public API
----------

    build_artifact(*, target, app_path, version, **kwargs) -> BuildResult
        Resolve the target id (e.g. ``docker``, ``deb``, ``cargo-dist``)
        to the right adapter, run it, return the result.

    list_targets() -> list[str]
        Enumerate every registered target id.

    get_adapter(target) -> BuildAdapter
        Return the adapter class for the target.

The CLI driver ``scripts/build_package.py`` is a thin shell that
invokes ``build_artifact()`` once and emits a single JSON summary. The
HMI, agents, and CI jobs all reuse the same module.

Why not shell out everything from bash
--------------------------------------
Same rationale as X1: artifact-path computation, version resolution,
JSON aggregation, and registry endpoint mapping are miserable in bash.
The shell layer remains a thin dispatcher.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Constants & enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Native package targets — produce a single binary distributable.
NATIVE_TARGETS: tuple[str, ...] = (
    "docker", "helm", "deb", "rpm", "msi", "nsis", "dmg", "pkg",
)

# Skill-hook targets — wrap a language-native release toolchain.
SKILL_HOOK_TARGETS: tuple[str, ...] = (
    "cargo-dist", "goreleaser", "pyinstaller", "electron-builder",
    "maven", "gradle",
)

ALL_TARGETS: tuple[str, ...] = NATIVE_TARGETS + SKILL_HOOK_TARGETS

# Container registry endpoints. Adapter selects via ``registry`` arg.
DOCKER_REGISTRIES: Mapping[str, str] = {
    "ghcr": "ghcr.io",
    "dockerhub": "docker.io",
    "ecr": "{account}.dkr.ecr.{region}.amazonaws.com",
    "gcr": "gcr.io",
    "acr": "{registry_name}.azurecr.io",
    "private": "",  # caller supplies the full host
}

# Per-target → host platform required by the underlying tool. The CI /
# agent layer can refuse to dispatch a Windows-only target on Linux
# without first booking a Windows runner.
TARGET_HOST_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "docker": ("linux", "darwin", "windows"),
    "helm": ("linux", "darwin", "windows"),
    "deb": ("linux",),
    "rpm": ("linux",),
    "msi": ("windows",),
    "nsis": ("windows", "linux"),  # makensis ports exist on Linux
    "dmg": ("darwin",),
    "pkg": ("darwin",),
    "cargo-dist": ("linux", "darwin", "windows"),
    "goreleaser": ("linux", "darwin", "windows"),
    "pyinstaller": ("linux", "darwin", "windows"),
    "electron-builder": ("linux", "darwin", "windows"),
    "maven": ("linux", "darwin", "windows"),
    "gradle": ("linux", "darwin", "windows"),
}

# Tool binary names (resolved via shutil.which()).
TOOL_BINARIES: Mapping[str, tuple[str, ...]] = {
    "docker": ("docker", "podman"),
    "helm": ("helm",),
    "deb": ("dpkg-deb", "fpm"),
    "rpm": ("rpmbuild", "fpm"),
    "msi": ("candle", "light", "wix"),
    "nsis": ("makensis",),
    "dmg": ("hdiutil", "create-dmg"),
    "pkg": ("pkgbuild", "productbuild"),
    "cargo-dist": ("cargo-dist", "cargo"),
    "goreleaser": ("goreleaser",),
    "pyinstaller": ("pyinstaller",),
    "electron-builder": ("electron-builder", "npx"),
    "maven": ("mvn", "mvnw"),
    "gradle": ("gradle", "gradlew"),
}

# Output filename pattern per target. ``{name}`` and ``{version}`` are
# substituted; ``{arch}`` defaults to ``noarch`` if not supplied.
OUTPUT_PATTERNS: Mapping[str, str] = {
    "docker": "{name}:{version}",                  # image tag, not a file
    "helm": "{name}-{version}.tgz",
    "deb": "{name}_{version}_{arch}.deb",
    "rpm": "{name}-{version}.{arch}.rpm",
    "msi": "{name}-{version}.msi",
    "nsis": "{name}-{version}-setup.exe",
    "dmg": "{name}-{version}.dmg",
    "pkg": "{name}-{version}.pkg",
    "cargo-dist": "{name}-{version}-{arch}.tar.xz",
    "goreleaser": "{name}_{version}_{arch}.tar.gz",
    "pyinstaller": "{name}-{version}",             # binary, no extension on Linux/macOS
    "electron-builder": "{name}-{version}.{ext}",  # ext varies per platform
    "maven": "{name}-{version}.jar",
    "gradle": "{name}-{version}.jar",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BuildAdapterError(Exception):
    """Base for build adapter errors."""


class UnknownTargetError(BuildAdapterError):
    """Target id not registered."""


class InvalidVersionError(BuildAdapterError):
    """Version string fails semver / Docker-tag rules."""


class HostMismatchError(BuildAdapterError):
    """Current host OS cannot run this target's underlying tool."""


class ArtifactSourceError(BuildAdapterError):
    """app_path missing / not a directory / lacks expected manifest."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Data models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass(frozen=True)
class BuildSource:
    """Immutable handle to the source tree being packaged.

    ``path`` is the project root; ``manifest`` is an optional explicit
    pointer to the build manifest (Dockerfile / Cargo.toml /
    package.json / Chart.yaml) — when omitted the adapter auto-detects
    via the same convention as ``software_simulator.resolve_language``.
    """

    path: Path
    manifest: Optional[Path] = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.path.exists():
            raise ArtifactSourceError(f"Source path does not exist: {self.path}")
        if not self.path.is_dir():
            raise ArtifactSourceError(f"Source path is not a directory: {self.path}")


@dataclass
class BuildResult:
    """Outcome of an adapter run.

    ``available`` distinguishes "tool not on PATH" (False, mock) from
    "tool ran" (True). ``ok`` distinguishes "tool succeeded" from "tool
    ran but exited non-zero". A mock run sets ``available=False`` and
    ``ok=False``; the caller treats this as ``skip``, never ``pass``.
    """

    target: str
    name: str
    version: str
    arch: str = "noarch"
    available: bool = False
    ok: bool = False
    artifact_path: Optional[str] = None
    artifact_uri: Optional[str] = None        # registry URL for docker / helm push
    digest: Optional[str] = None              # sha256 for OCI / sha1 for tarballs
    duration_s: float = 0.0
    runner: Optional[str] = None              # binary that produced the artifact
    stdout_tail: str = ""
    stderr_tail: str = ""
    notes: list[str] = field(default_factory=list)
    skill_hook: Optional[str] = None          # cargo-dist / goreleaser / etc.

    def to_dict(self) -> dict:
        return asdict(self)

    def status(self) -> str:
        if not self.available:
            return "skip"
        return "pass" if self.ok else "fail"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Validation helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Docker tag rules: lowercase, digits, hyphen, underscore, dot. 1..128.
_DOCKER_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
# Semver-ish — accepts 1.2.3, 1.2.3-rc.1, 1.2.3+build.42, 1.2.
_SEMVER_RE = re.compile(r"^\d+(\.\d+){0,3}([-+][A-Za-z0-9.\-]+)*$")
# Helm chart name: kebab-case, must start with letter.
_HELM_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
# Package name (deb/rpm): per Debian Policy §5.6.7 — lowercase letters,
# digits, plus, minus, dot. Must start with a letter or digit.
_PKG_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]+$")


def normalize_version(version: str, *, target: str) -> str:
    """Coerce a version string into the shape the target requires.

    - docker: must match the tag regex (lowercase). Plus signs become
      hyphens (Docker tags don't allow ``+``).
    - deb: ``+`` and ``~`` allowed; uppercase rejected.
    - rpm: dashes are reserved as the release separator — replaced with
      underscore in the version field.
    - else: returned unchanged but checked against semver-ish.
    """
    if not version:
        raise InvalidVersionError("version is empty")
    v = version.strip()
    if target == "docker":
        v = v.replace("+", "-").lower()
        if not _DOCKER_TAG_RE.match(v):
            raise InvalidVersionError(
                f"docker tag rejects {version!r}; must be [a-z0-9][a-z0-9._-]{{0,127}}"
            )
        return v
    # Validate against semver-ish first; THEN apply target-specific
    # transforms (rpm strips '-' which would invalidate the regex).
    if not _SEMVER_RE.match(v):
        raise InvalidVersionError(
            f"version {version!r} is not semver-shaped (1.2.3 / 1.2.3-rc.1 / 1.2.3+build)"
        )
    if target == "rpm":
        # rpm version field cannot contain '-' (reserved for release).
        v = v.replace("-", "_")
    return v


def validate_artifact_name(name: str, *, target: str) -> str:
    if not name:
        raise BuildAdapterError("artifact name is empty")
    if target == "helm":
        if not _HELM_NAME_RE.match(name):
            raise BuildAdapterError(
                f"helm chart name {name!r}: must be kebab-case starting with letter, ≤63 chars"
            )
    elif target in {"deb", "rpm"}:
        if not _PKG_NAME_RE.match(name):
            raise BuildAdapterError(
                f"{target} package name {name!r}: must match [a-z0-9][a-z0-9+.\\-]+"
            )
    elif target == "docker":
        # Docker repo: lowercase, digit, dot, hyphen, underscore, slash.
        if not re.match(r"^[a-z0-9][a-z0-9._/\-]{0,254}$", name):
            raise BuildAdapterError(f"docker image name {name!r} invalid")
    return name


def current_host_kind() -> str:
    """Map ``sys.platform`` to the strings used in TARGET_HOST_REQUIREMENTS."""
    import sys
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p.startswith("win"):
        return "windows"
    return "linux"  # fallback — best effort for BSDs


def _which(*candidates: str) -> Optional[str]:
    for cand in candidates:
        path = shutil.which(cand)
        if path:
            return path
    return None


def _run(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 600,
) -> tuple[int, str, str, float]:
    """Run a subprocess capturing stdout/stderr; return (rc, out, err, elapsed)."""
    import time
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        elapsed = time.monotonic() - t0
        return proc.returncode, proc.stdout, proc.stderr, elapsed
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        return 124, exc.stdout or "", f"timeout after {timeout}s", elapsed
    except FileNotFoundError as exc:
        elapsed = time.monotonic() - t0
        return 127, "", str(exc), elapsed


def _tail(text: str, *, lines: int = 20) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Base class
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BuildAdapter(ABC):
    """Abstract base for every build/package target adapter.

    Subclasses MUST set the ``target`` and ``binaries`` classvars and
    implement ``_build()``. They MAY override ``_validate_source()``
    when the target needs a specific manifest (Dockerfile / Chart.yaml /
    Cargo.toml) at the source root.
    """

    target: ClassVar[str] = ""
    binaries: ClassVar[tuple[str, ...]] = ()

    def __init__(
        self,
        *,
        name: str,
        version: str,
        arch: str = "noarch",
        output_dir: Optional[Path] = None,
        push: bool = False,
        registry: Optional[str] = None,
        registry_args: Optional[Mapping[str, str]] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ):
        if not self.target:
            raise BuildAdapterError(f"{type(self).__name__} must set classvar 'target'")
        self._raw_name = name
        self._name = validate_artifact_name(name, target=self.target)
        self._version = normalize_version(version, target=self.target)
        self._arch = arch or "noarch"
        self._output_dir = output_dir or Path(".artifacts/builds")
        self._push = push
        self._registry = registry
        self._registry_args = dict(registry_args or {})
        self._extra = dict(extra or {})

    # ── Public API ──

    def expected_artifact_path(self) -> str:
        pat = OUTPUT_PATTERNS[self.target]
        return pat.format(
            name=self._name,
            version=self._version,
            arch=self._arch,
            ext=self._extra.get("ext", "AppImage"),
        )

    def host_compatible(self) -> bool:
        allowed = TARGET_HOST_REQUIREMENTS.get(self.target, ())
        return current_host_kind() in allowed

    def runner_path(self) -> Optional[str]:
        return _which(*self.binaries)

    def build(self, source: BuildSource) -> BuildResult:
        """Run the build, returning a BuildResult.

        Wraps ``_validate_source()`` and ``_build()`` and never raises
        for missing tools / mock paths — those return a skip result.
        """
        source.validate()
        if not self.host_compatible():
            raise HostMismatchError(
                f"target {self.target!r} cannot run on host "
                f"{current_host_kind()!r} (allowed: "
                f"{TARGET_HOST_REQUIREMENTS.get(self.target, ())})"
            )
        try:
            self._validate_source(source)
        except ArtifactSourceError:
            raise
        runner = self.runner_path()
        if runner is None:
            return BuildResult(
                target=self.target,
                name=self._name,
                version=self._version,
                arch=self._arch,
                available=False,
                ok=False,
                runner=None,
                notes=[f"runner not on PATH ({', '.join(self.binaries)}); mock skip"],
            )
        return self._build(source, runner)

    # ── Hooks ──

    def _validate_source(self, source: BuildSource) -> None:
        """Override to check for the target-specific manifest file."""

    @abstractmethod
    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        """Run the target-specific build with an already-resolved runner."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Native adapters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DockerImageAdapter(BuildAdapter):
    """Docker / OCI image build + optional push.

    Supports GHCR / Docker Hub / ECR / GCR / ACR / private registry. The
    adapter never logs the registry token; it relies on the ambient
    docker login state (or ``DOCKER_CONFIG``) for credentials so this
    module stays free of secret-handling code.
    """

    target = "docker"
    binaries = ("docker", "podman")

    def _validate_source(self, source: BuildSource) -> None:
        dockerfile = source.manifest or (source.path / "Dockerfile")
        if not dockerfile.exists():
            raise ArtifactSourceError(
                f"docker build needs a Dockerfile at {dockerfile}"
            )

    def resolve_image_uri(self) -> str:
        """Compose the full registry URI for the image tag."""
        if not self._registry:
            return f"{self._name}:{self._version}"
        host_pattern = DOCKER_REGISTRIES.get(self._registry, "")
        if self._registry == "private":
            host = self._registry_args.get("host", "")
            if not host:
                raise BuildAdapterError("registry=private requires registry_args.host")
        elif self._registry == "ecr":
            account = self._registry_args.get("account", "")
            region = self._registry_args.get("region", "us-east-1")
            if not account:
                raise BuildAdapterError("registry=ecr requires registry_args.account")
            host = host_pattern.format(account=account, region=region)
        elif self._registry == "acr":
            reg_name = self._registry_args.get("registry_name", "")
            if not reg_name:
                raise BuildAdapterError("registry=acr requires registry_args.registry_name")
            host = host_pattern.format(registry_name=reg_name)
        else:
            host = host_pattern
        # Optional namespace (org/user) under the host.
        ns = self._registry_args.get("namespace", "")
        repo = f"{ns}/{self._name}" if ns else self._name
        return f"{host}/{repo}:{self._version}"

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        image_uri = self.resolve_image_uri()
        dockerfile = source.manifest or (source.path / "Dockerfile")
        cmd = [
            runner, "build",
            "-f", str(dockerfile),
            "-t", image_uri,
            str(source.path),
        ]
        platform = self._extra.get("platform")
        if platform:
            cmd.insert(2, "--platform")
            cmd.insert(3, str(platform))
        rc, out, err, elapsed = _run(cmd, cwd=source.path)
        notes: list[str] = []
        digest: Optional[str] = None
        if rc == 0:
            # Resolve the digest for traceability — best-effort.
            d_rc, d_out, _, _ = _run(
                [runner, "image", "inspect", "--format", "{{.Id}}", image_uri],
                cwd=source.path,
                timeout=30,
            )
            if d_rc == 0:
                digest = d_out.strip() or None
            if self._push:
                p_rc, p_out, p_err, _ = _run(
                    [runner, "push", image_uri],
                    cwd=source.path,
                )
                if p_rc != 0:
                    notes.append(f"push failed rc={p_rc}: {_tail(p_err)}")
                    return BuildResult(
                        target=self.target, name=self._name,
                        version=self._version, arch=self._arch,
                        available=True, ok=False,
                        artifact_uri=image_uri, digest=digest,
                        duration_s=elapsed, runner=runner,
                        stdout_tail=_tail(out), stderr_tail=_tail(p_err),
                        notes=notes,
                    )
                notes.append(f"pushed to {image_uri}")
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0),
            artifact_uri=image_uri, digest=digest,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
            notes=notes,
        )


class HelmChartAdapter(BuildAdapter):
    """``helm package`` wrapper with lint pre-flight.

    Source dir must contain a ``Chart.yaml`` (or a sub-dir matching
    ``self._name``). The adapter runs ``helm lint`` first; lint failures
    are reported but do not block packaging — the result carries notes.
    """

    target = "helm"
    binaries = ("helm",)

    def _validate_source(self, source: BuildSource) -> None:
        chart_dir = source.manifest or source.path
        if (chart_dir / "Chart.yaml").exists():
            return
        # Try sub-dir named after the chart.
        if (source.path / self._name / "Chart.yaml").exists():
            return
        raise ArtifactSourceError(
            f"helm package needs Chart.yaml at {chart_dir} or {source.path / self._name}"
        )

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        chart_dir = source.manifest or source.path
        if not (chart_dir / "Chart.yaml").exists():
            chart_dir = source.path / self._name
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        lint_rc, _, lint_err, _ = _run(
            [runner, "lint", str(chart_dir)],
            cwd=source.path,
            timeout=120,
        )
        if lint_rc != 0:
            notes.append(f"lint warnings: {_tail(lint_err, lines=5)}")
        cmd = [
            runner, "package", str(chart_dir),
            "--destination", str(out_dir),
            "--version", self._version,
        ]
        rc, out, err, elapsed = _run(cmd, cwd=source.path)
        artifact = out_dir / self.expected_artifact_path()
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0 and artifact.exists()),
            artifact_path=str(artifact) if artifact.exists() else None,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
            notes=notes,
        )


class _LinuxPackageAdapter(BuildAdapter):
    """Common scaffolding for .deb / .rpm packages."""

    pkg_format: ClassVar[str] = ""

    def _validate_source(self, source: BuildSource) -> None:
        # Both deb and rpm need a control / spec file OR an fpm-friendly
        # source tree (a `usr/` or `opt/` layout). We allow either.
        ok_markers = ("debian/control", "rpm.spec", "usr", "opt", "etc")
        for marker in ok_markers:
            if (source.path / marker).exists():
                return
        raise ArtifactSourceError(
            f"{self.pkg_format} build needs one of: debian/control, rpm.spec, "
            f"or a usr|opt|etc/ tree under {source.path}"
        )

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / self.expected_artifact_path()
        # Prefer fpm if available — it builds both deb and rpm from a
        # plain dir without a control / spec file. dpkg-deb / rpmbuild
        # paths require fully-prepared metadata which the caller stages.
        if Path(runner).name == "fpm":
            cmd = [
                runner,
                "-s", "dir",
                "-t", self.pkg_format,
                "-n", self._name,
                "-v", self._version,
                "-a", self._arch,
                "-p", str(artifact),
                "-C", str(source.path),
                ".",
            ]
            rc, out, err, elapsed = _run(cmd, cwd=source.path)
        elif self.pkg_format == "deb":
            # dpkg-deb path — caller has already staged DEBIAN/control.
            cmd = [runner, "--build", str(source.path), str(artifact)]
            rc, out, err, elapsed = _run(cmd, cwd=source.path)
        else:  # rpm
            spec = source.manifest or (source.path / "rpm.spec")
            cmd = [runner, "-bb", str(spec), "--define", f"_rpmdir {out_dir}"]
            rc, out, err, elapsed = _run(cmd, cwd=source.path)
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0 and artifact.exists()),
            artifact_path=str(artifact) if artifact.exists() else None,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
        )


class DebPackageAdapter(_LinuxPackageAdapter):
    target = "deb"
    binaries = ("dpkg-deb", "fpm")
    pkg_format = "deb"


class RpmPackageAdapter(_LinuxPackageAdapter):
    target = "rpm"
    binaries = ("rpmbuild", "fpm")
    pkg_format = "rpm"


class MsiInstallerAdapter(BuildAdapter):
    """WiX-based MSI builder.

    Two-phase: ``candle`` compiles ``.wxs`` → ``.wixobj``, ``light``
    links ``.wixobj`` → ``.msi``. Caller stages the .wxs.
    """

    target = "msi"
    binaries = ("candle", "light", "wix")

    def _validate_source(self, source: BuildSource) -> None:
        manifest = source.manifest or (source.path / "installer.wxs")
        if not manifest.exists():
            raise ArtifactSourceError(f"msi build needs a .wxs file at {manifest}")

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        wxs = source.manifest or (source.path / "installer.wxs")
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / self.expected_artifact_path()
        wixobj = out_dir / f"{self._name}-{self._version}.wixobj"
        # Candle phase.
        candle = _which("candle") or runner
        rc1, out1, err1, t1 = _run(
            [candle, str(wxs), "-out", str(wixobj)],
            cwd=source.path,
        )
        if rc1 != 0:
            return BuildResult(
                target=self.target, name=self._name,
                version=self._version, arch=self._arch,
                available=True, ok=False,
                duration_s=t1, runner=candle,
                stdout_tail=_tail(out1), stderr_tail=_tail(err1),
                notes=["candle (compile) failed"],
            )
        light = _which("light") or runner
        rc2, out2, err2, t2 = _run(
            [light, str(wixobj), "-out", str(artifact)],
            cwd=source.path,
        )
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc2 == 0 and artifact.exists()),
            artifact_path=str(artifact) if artifact.exists() else None,
            duration_s=t1 + t2, runner=f"{candle}+{light}",
            stdout_tail=_tail(out2), stderr_tail=_tail(err2),
        )


class NsisInstallerAdapter(BuildAdapter):
    target = "nsis"
    binaries = ("makensis",)

    def _validate_source(self, source: BuildSource) -> None:
        nsi = source.manifest or (source.path / "installer.nsi")
        if not nsi.exists():
            raise ArtifactSourceError(f"nsis build needs an .nsi file at {nsi}")

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        nsi = source.manifest or (source.path / "installer.nsi")
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / self.expected_artifact_path()
        cmd = [
            runner,
            f"-DOUTFILE={artifact}",
            f"-DPRODUCT_VERSION={self._version}",
            f"-DPRODUCT_NAME={self._name}",
            str(nsi),
        ]
        rc, out, err, elapsed = _run(cmd, cwd=source.path)
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0 and artifact.exists()),
            artifact_path=str(artifact) if artifact.exists() else None,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
        )


class DmgInstallerAdapter(BuildAdapter):
    """macOS DMG via hdiutil (preferred) or create-dmg (nicer UX)."""

    target = "dmg"
    binaries = ("hdiutil", "create-dmg")

    def _validate_source(self, source: BuildSource) -> None:
        # Caller must stage a .app bundle or a folder to image.
        for child in source.path.iterdir():
            if child.suffix == ".app" or child.is_dir():
                return
        raise ArtifactSourceError(
            f"dmg build needs at least a folder or .app bundle under {source.path}"
        )

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / self.expected_artifact_path()
        if Path(runner).name == "create-dmg":
            cmd = [runner, "--volname", self._name, str(artifact), str(source.path)]
        else:
            cmd = [
                runner, "create",
                "-volname", self._name,
                "-srcfolder", str(source.path),
                "-ov", "-format", "UDZO",
                str(artifact),
            ]
        rc, out, err, elapsed = _run(cmd, cwd=source.path)
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0 and artifact.exists()),
            artifact_path=str(artifact) if artifact.exists() else None,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
        )


class PkgInstallerAdapter(BuildAdapter):
    """macOS PKG via pkgbuild + productbuild."""

    target = "pkg"
    binaries = ("pkgbuild", "productbuild")

    def _validate_source(self, source: BuildSource) -> None:
        if not (source.path / "root").exists():
            raise ArtifactSourceError(
                f"pkg build needs a `root/` payload directory under {source.path}"
            )

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        out_dir = self._output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        artifact = out_dir / self.expected_artifact_path()
        identifier = self._extra.get("identifier", f"com.omnisight.{self._name}")
        cmd = [
            runner,
            "--root", str(source.path / "root"),
            "--identifier", identifier,
            "--version", self._version,
            "--install-location", self._extra.get("install_location", "/Applications"),
            str(artifact),
        ]
        rc, out, err, elapsed = _run(cmd, cwd=source.path)
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0 and artifact.exists()),
            artifact_path=str(artifact) if artifact.exists() else None,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill-hook adapters
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _SkillHookAdapter(BuildAdapter):
    """Common shape for adapters that defer to a language-native release tool.

    These tools (cargo-dist / goreleaser / pyinstaller / electron-builder)
    each own their own multi-platform / multi-format pipeline. The
    adapter's job is just to invoke them with the right argv and capture
    a uniform BuildResult.
    """

    skill_hook: ClassVar[str] = ""

    def _build(self, source: BuildSource, runner: str) -> BuildResult:
        cmd = self._compose_cmd(source, runner)
        rc, out, err, elapsed = _run(cmd, cwd=source.path)
        artifact_path = self._locate_artifact(source)
        return BuildResult(
            target=self.target, name=self._name,
            version=self._version, arch=self._arch,
            available=True, ok=(rc == 0),
            artifact_path=str(artifact_path) if artifact_path else None,
            duration_s=elapsed, runner=runner,
            stdout_tail=_tail(out), stderr_tail=_tail(err),
            skill_hook=self.skill_hook,
        )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        raise NotImplementedError

    def _locate_artifact(self, source: BuildSource) -> Optional[Path]:
        return None


class CargoDistAdapter(_SkillHookAdapter):
    target = "cargo-dist"
    binaries = ("cargo-dist", "cargo")
    skill_hook = "cargo-dist"

    def _validate_source(self, source: BuildSource) -> None:
        if not (source.path / "Cargo.toml").exists():
            raise ArtifactSourceError(
                f"cargo-dist needs Cargo.toml at {source.path / 'Cargo.toml'}"
            )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        if Path(runner).name == "cargo":
            return [runner, "dist", "build"]
        return [runner, "build"]


class GoreleaserAdapter(_SkillHookAdapter):
    target = "goreleaser"
    binaries = ("goreleaser",)
    skill_hook = "goreleaser"

    def _validate_source(self, source: BuildSource) -> None:
        candidates = (".goreleaser.yml", ".goreleaser.yaml", "go.mod")
        for c in candidates:
            if (source.path / c).exists():
                return
        raise ArtifactSourceError(
            f"goreleaser needs .goreleaser.y[a]ml or go.mod under {source.path}"
        )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        snapshot = "--snapshot" if not self._push else ""
        cmd = [runner, "release", "--clean"]
        if snapshot:
            cmd.append(snapshot)
        return cmd


class PyInstallerAdapter(_SkillHookAdapter):
    target = "pyinstaller"
    binaries = ("pyinstaller",)
    skill_hook = "pyinstaller"

    def _validate_source(self, source: BuildSource) -> None:
        entrypoint = source.manifest or (source.path / "main.py")
        if not entrypoint.exists():
            # Allow an explicit override via extra["entrypoint"].
            ep = self._extra.get("entrypoint")
            if ep and (source.path / ep).exists():
                return
            raise ArtifactSourceError(
                f"pyinstaller needs an entrypoint .py at {entrypoint} "
                "(or extra.entrypoint)"
            )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        ep = self._extra.get("entrypoint", "main.py")
        return [
            runner, "--onefile",
            "--name", self._name,
            "--distpath", str(self._output_dir),
            ep,
        ]


class ElectronBuilderAdapter(_SkillHookAdapter):
    target = "electron-builder"
    binaries = ("electron-builder", "npx")
    skill_hook = "electron-builder"

    def _validate_source(self, source: BuildSource) -> None:
        if not (source.path / "package.json").exists():
            raise ArtifactSourceError(
                f"electron-builder needs package.json at {source.path / 'package.json'}"
            )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        if Path(runner).name == "npx":
            return [runner, "electron-builder", "--publish", "never"]
        return [runner, "--publish", "never"]


class MavenAdapter(_SkillHookAdapter):
    """X9 #305 — Spring Boot / JVM 21 fat-jar release via ``mvn package``.

    Added for SKILL-SPRING-BOOT (the fifth and final priority-X
    software-vertical skill pack). The real build is delegated to the
    Maven wrapper / system ``mvn``; the adapter's job is to assert the
    scaffold ships a ``pom.xml`` at the given path and, on build, to
    locate the fat jar under ``target/`` (Maven final-name pattern
    ``<artifactId>-<version>.jar``).
    """

    target = "maven"
    binaries = ("mvn", "mvnw")
    skill_hook = "maven"

    def _validate_source(self, source: BuildSource) -> None:
        if not (source.path / "pom.xml").exists():
            raise ArtifactSourceError(
                f"maven needs pom.xml at {source.path / 'pom.xml'}"
            )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        # `mvn package -DskipTests` is the standard Spring Boot fat-jar
        # build. Tests and coverage verification run via `mvn verify`
        # from X1 software_simulator, not from X3 release path.
        return [runner, "-B", "-DskipTests", "package"]

    def _locate_artifact(self, source: BuildSource) -> Optional[Path]:
        target_dir = source.path / "target"
        if not target_dir.is_dir():
            return None
        # Prefer `<artifactId>-<version>.jar`, fall back to any *.jar
        # the build emitted that isn't the `original-*.jar` layering
        # artefact from spring-boot-maven-plugin repackage.
        candidates = sorted(
            p for p in target_dir.glob("*.jar")
            if not p.name.startswith("original-")
        )
        return candidates[-1] if candidates else None


class GradleAdapter(_SkillHookAdapter):
    """X9 #305 — Spring Boot / JVM 21 fat-jar release via Gradle.

    Mirrors ``MavenAdapter`` but for Gradle Kotlin-DSL projects. The
    adapter accepts either a wrapper (``gradlew`` / ``gradlew.bat``)
    or a system ``gradle`` binary; when the wrapper is present the
    scaffold prefers it so builds are reproducible across hosts.
    """

    target = "gradle"
    binaries = ("gradle", "gradlew")
    skill_hook = "gradle"

    def _validate_source(self, source: BuildSource) -> None:
        has_kts = (source.path / "build.gradle.kts").exists()
        has_groovy = (source.path / "build.gradle").exists()
        if not (has_kts or has_groovy):
            raise ArtifactSourceError(
                f"gradle needs build.gradle[.kts] at {source.path}"
            )

    def _compose_cmd(self, source: BuildSource, runner: str) -> list[str]:
        # `bootJar` is the Spring Boot plugin goal; for plain JVM
        # projects it degrades to Gradle's `jar` task via the
        # `io.spring.dependency-management` plugin. Using `bootJar`
        # directly keeps the release path unambiguous.
        return [runner, "--no-daemon", "bootJar"]

    def _locate_artifact(self, source: BuildSource) -> Optional[Path]:
        libs = source.path / "build" / "libs"
        if not libs.is_dir():
            return None
        candidates = sorted(
            p for p in libs.glob("*.jar")
            if not p.name.endswith("-plain.jar")  # bootJar ships main + plain
        )
        return candidates[-1] if candidates else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Registry & dispatch
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_REGISTRY: dict[str, type[BuildAdapter]] = {
    "docker": DockerImageAdapter,
    "helm": HelmChartAdapter,
    "deb": DebPackageAdapter,
    "rpm": RpmPackageAdapter,
    "msi": MsiInstallerAdapter,
    "nsis": NsisInstallerAdapter,
    "dmg": DmgInstallerAdapter,
    "pkg": PkgInstallerAdapter,
    "cargo-dist": CargoDistAdapter,
    "goreleaser": GoreleaserAdapter,
    "pyinstaller": PyInstallerAdapter,
    "electron-builder": ElectronBuilderAdapter,
    "maven": MavenAdapter,
    "gradle": GradleAdapter,
}


def list_targets() -> list[str]:
    """Return every registered target id."""
    return sorted(_REGISTRY.keys())


def get_adapter(target: str) -> type[BuildAdapter]:
    if target not in _REGISTRY:
        raise UnknownTargetError(
            f"unknown target {target!r}; available: {list_targets()}"
        )
    return _REGISTRY[target]


def build_artifact(
    *,
    target: str,
    app_path: Path,
    name: str,
    version: str,
    arch: str = "noarch",
    output_dir: Optional[Path] = None,
    push: bool = False,
    registry: Optional[str] = None,
    registry_args: Optional[Mapping[str, str]] = None,
    manifest: Optional[Path] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> BuildResult:
    """Top-level entry point — build one artifact.

    Returns a BuildResult; never raises for missing tools (those become
    skip results). Raises ``UnknownTargetError`` /
    ``HostMismatchError`` / ``ArtifactSourceError`` /
    ``InvalidVersionError`` for caller-side bugs.
    """
    cls = get_adapter(target)
    adapter = cls(
        name=name,
        version=version,
        arch=arch,
        output_dir=output_dir,
        push=push,
        registry=registry,
        registry_args=registry_args,
        extra=extra,
    )
    return adapter.build(BuildSource(path=Path(app_path), manifest=manifest))


def build_matrix(
    *,
    targets: Sequence[str],
    app_path: Path,
    name: str,
    version: str,
    arch: str = "noarch",
    output_dir: Optional[Path] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> dict[str, BuildResult]:
    """Run ``build_artifact()`` over a list of targets.

    Targets that the host can't run (HostMismatchError) are returned as
    skip results so the matrix completes — caller sees one row per
    requested target. Other validation errors propagate.
    """
    results: dict[str, BuildResult] = {}
    for tgt in targets:
        try:
            results[tgt] = build_artifact(
                target=tgt,
                app_path=app_path,
                name=name,
                version=version,
                arch=arch,
                output_dir=output_dir,
                extra=extra,
            )
        except HostMismatchError as exc:
            results[tgt] = BuildResult(
                target=tgt, name=name, version=version, arch=arch,
                available=False, ok=False,
                notes=[f"host mismatch: {exc}"],
            )
    return results


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Skill hook wiring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Map X2 software role IDs to the skill-hook target they prefer. Used by
# orchestrator / agents when a role asks for a "release" without naming
# a target — e.g. a backend-rust skill defaults to cargo-dist.
ROLE_DEFAULT_TARGETS: Mapping[str, tuple[str, ...]] = {
    "backend-python": ("docker", "pyinstaller"),
    "backend-go": ("docker", "goreleaser"),
    "backend-rust": ("docker", "cargo-dist"),
    "backend-node": ("docker",),
    "backend-java": ("docker", "maven", "gradle"),
    "cli-tooling": ("goreleaser", "cargo-dist", "pyinstaller"),
    "desktop-electron": ("electron-builder",),
    "desktop-tauri": ("cargo-dist",),
    "desktop-qt": ("deb", "rpm", "dmg", "msi"),
}


def default_targets_for_role(role_id: str) -> tuple[str, ...]:
    """Return the ordered list of build targets a role prefers."""
    return ROLE_DEFAULT_TARGETS.get(role_id, ())


__all__ = [
    "ALL_TARGETS",
    "NATIVE_TARGETS",
    "SKILL_HOOK_TARGETS",
    "DOCKER_REGISTRIES",
    "TARGET_HOST_REQUIREMENTS",
    "TOOL_BINARIES",
    "OUTPUT_PATTERNS",
    "ROLE_DEFAULT_TARGETS",
    # Errors
    "BuildAdapterError",
    "UnknownTargetError",
    "InvalidVersionError",
    "HostMismatchError",
    "ArtifactSourceError",
    # Models
    "BuildSource",
    "BuildResult",
    # Validators
    "normalize_version",
    "validate_artifact_name",
    "current_host_kind",
    # Adapters
    "BuildAdapter",
    "DockerImageAdapter",
    "HelmChartAdapter",
    "DebPackageAdapter",
    "RpmPackageAdapter",
    "MsiInstallerAdapter",
    "NsisInstallerAdapter",
    "DmgInstallerAdapter",
    "PkgInstallerAdapter",
    "CargoDistAdapter",
    "GoreleaserAdapter",
    "PyInstallerAdapter",
    "ElectronBuilderAdapter",
    "MavenAdapter",
    "GradleAdapter",
    # Dispatch
    "list_targets",
    "get_adapter",
    "build_artifact",
    "build_matrix",
    "default_targets_for_role",
]
