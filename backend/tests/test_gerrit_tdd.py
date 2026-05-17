"""BP.G.4 -- Gerrit TDD dual-patchset contract matrix.

BP.G.1/BP.G.2/BP.G.3 own the implementation modules and Gerrit submit
rule. This BP.G.4 file pins the executable contract for the two-patchset
workflow so later implementation work has a green, backend-only target:

* Patchset A is tests-only.
* Patchset B is implementation-only.
* Patchset B carries ``Depends-On`` pointing at Patchset A.
* The generated runner command remains a local pytest command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import pytest


CONTRACT_VERSION = "BP.G.4/1"
CHANGE_ID_RE = re.compile(r"^I[0-9a-f]{40}$")
TEST_PATH = "backend/tests/test_gerrit_tdd.py"
PHASE_SEQUENCE = (
    ("BP.G.1", "backend/gerrit_tdd.py", "dual patchset generator"),
    ("BP.G.2", "backend/hooks/gerrit_depends_on.py", "Depends-On verifier"),
    ("BP.G.3", ".gerrit/rules.pl", "submit rule extension"),
    ("BP.G.4", TEST_PATH, "contract test matrix"),
)


@dataclass(frozen=True)
class PatchsetSpec:
    name: str
    ticket: str
    role: str
    subject: str
    paths: tuple[str, ...]
    commit_message: str


@dataclass(frozen=True)
class ManifestSpec:
    ticket: str
    base_branch: str
    patchsets: tuple[PatchsetSpec, ...]
    ci_command: tuple[str, ...]


PATCHSET_A = PatchsetSpec(
    name="A",
    ticket="OP-269",
    role="tests",
    subject="[OP-269] Add Gerrit TDD contract tests",
    paths=(TEST_PATH,),
    commit_message=(
        "[OP-269] Add Gerrit TDD contract tests\n\n"
        "BP.G.4 test-only patchset.\n\n"
        "Change-Id: Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    ),
)

PATCHSET_B = PatchsetSpec(
    name="B",
    ticket="OP-269",
    role="implementation",
    subject="[OP-269] Implement Gerrit TDD generator",
    paths=("backend/gerrit_tdd.py",),
    commit_message=(
        "[OP-269] Implement Gerrit TDD generator\n\n"
        "Depends-On: Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "Change-Id: Ibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
    ),
)

VALID_MANIFEST = ManifestSpec(
    ticket="OP-269",
    base_branch="develop",
    patchsets=(PATCHSET_A, PATCHSET_B),
    ci_command=("backend/.venv/bin/pytest", TEST_PATH),
)


def _is_backend_test_path(path: str) -> bool:
    return path.startswith("backend/tests/test_") and path.endswith(".py")


def _is_backend_implementation_path(path: str) -> bool:
    return path.startswith("backend/") and not path.startswith("backend/tests/")


def _extract_trailers(commit_message: str, name: str) -> list[str]:
    prefix = f"{name}:"
    return [
        line[len(prefix) :].strip()
        for line in commit_message.splitlines()
        if line.startswith(prefix)
    ]


def _validate_subject(spec: PatchsetSpec) -> None:
    if f"[{spec.ticket}]" not in spec.subject:
        raise ValueError("subject must include the JIRA key")
    if spec.subject != spec.subject.strip():
        raise ValueError("subject must not carry outer whitespace")
    if len(spec.subject) > 72:
        raise ValueError("subject must fit Gerrit summary conventions")


def _validate_patchset(spec: PatchsetSpec) -> None:
    _validate_subject(spec)
    if not spec.paths:
        raise ValueError("patchset must touch at least one file")

    if spec.role == "tests":
        if any(not _is_backend_test_path(path) for path in spec.paths):
            raise ValueError("test patchset must touch backend tests only")
        if _extract_trailers(spec.commit_message, "Depends-On"):
            raise ValueError("test patchset must not depend on implementation")
    elif spec.role == "implementation":
        if any(not _is_backend_implementation_path(path) for path in spec.paths):
            raise ValueError(
                "implementation patchset must touch backend implementation only"
            )
        depends_on = _extract_trailers(spec.commit_message, "Depends-On")
        if len(depends_on) != 1 or not CHANGE_ID_RE.match(depends_on[0]):
            raise ValueError(
                "implementation patchset must carry one Depends-On Change-Id"
            )
    else:
        raise ValueError("unknown patchset role")

    change_ids = _extract_trailers(spec.commit_message, "Change-Id")
    if len(change_ids) != 1 or not CHANGE_ID_RE.match(change_ids[0]):
        raise ValueError("patchset must carry one valid Change-Id")


def _validate_manifest(manifest: ManifestSpec) -> None:
    if manifest.ticket != "OP-269":
        raise ValueError("manifest ticket drift")
    if manifest.base_branch != "develop":
        raise ValueError("dual-patchset flow targets develop")
    if tuple(p.name for p in manifest.patchsets) != ("A", "B"):
        raise ValueError("manifest must contain Patchset A then Patchset B")

    for patchset in manifest.patchsets:
        _validate_patchset(patchset)

    first_change_id = _extract_trailers(
        manifest.patchsets[0].commit_message,
        "Change-Id",
    )[0]
    second_depends_on = _extract_trailers(
        manifest.patchsets[1].commit_message,
        "Depends-On",
    )[0]
    if first_change_id != second_depends_on:
        raise ValueError("Patchset B must depend on Patchset A")

    _validate_ci_command(manifest.ci_command)


def _validate_ci_command(command: Iterable[str]) -> None:
    argv = tuple(command)
    if argv[:1] != ("backend/.venv/bin/pytest",):
        raise ValueError("CI command must use the project pytest venv")
    if TEST_PATH not in argv:
        raise ValueError("CI command must include the BP.G.4 test file")
    forbidden = {"-k", "--maxfail=1", "--lf", "--ff", "--pdb"}
    if any(arg in forbidden for arg in argv):
        raise ValueError("CI command must not narrow or debug the contract run")


def _copy_patchset(spec: PatchsetSpec, **overrides: object) -> PatchsetSpec:
    values = {
        "name": spec.name,
        "ticket": spec.ticket,
        "role": spec.role,
        "subject": spec.subject,
        "paths": spec.paths,
        "commit_message": spec.commit_message,
    }
    values.update(overrides)
    return PatchsetSpec(**values)


def _copy_manifest(manifest: ManifestSpec, **overrides: object) -> ManifestSpec:
    values = {
        "ticket": manifest.ticket,
        "base_branch": manifest.base_branch,
        "patchsets": manifest.patchsets,
        "ci_command": manifest.ci_command,
    }
    values.update(overrides)
    return ManifestSpec(**values)


def test_contract_version_is_pinned() -> None:
    assert CONTRACT_VERSION == "BP.G.4/1"


@pytest.mark.parametrize(
    ("phase", "path", "summary"),
    PHASE_SEQUENCE,
)
def test_phase_sequence_is_complete(phase: str, path: str, summary: str) -> None:
    assert phase.startswith("BP.G.")
    assert path
    assert summary


@pytest.mark.parametrize("patchset", (PATCHSET_A, PATCHSET_B))
def test_patchset_contract_accepts_valid_fixture(patchset: PatchsetSpec) -> None:
    _validate_patchset(patchset)


@pytest.mark.parametrize(
    ("patchset", "paths", "message"),
    (
        (PATCHSET_A, ("backend/gerrit_tdd.py",), "tests only"),
        (PATCHSET_A, ("frontend/app/page.tsx",), "tests only"),
        (PATCHSET_A, ("docs/sop/gerrit_tdd.md",), "tests only"),
        (PATCHSET_A, ("backend/tests/helpers/gerrit_tdd.py",), "tests only"),
        (PATCHSET_B, (TEST_PATH,), "implementation only"),
        (PATCHSET_B, ("db/migrations/0001.sql",), "implementation only"),
        (PATCHSET_B, ("devops/gerrit.yaml",), "implementation only"),
        (PATCHSET_B, (), "at least one file"),
    ),
)
def test_patchset_contract_rejects_invalid_paths(
    patchset: PatchsetSpec,
    paths: tuple[str, ...],
    message: str,
) -> None:
    invalid = _copy_patchset(patchset, paths=paths)
    with pytest.raises(ValueError, match=message):
        _validate_patchset(invalid)


@pytest.mark.parametrize(
    ("subject", "message"),
    (
        ("Add Gerrit TDD contract tests", "JIRA key"),
        (" [OP-269] Add Gerrit TDD contract tests", "outer whitespace"),
        ("[OP-269] " + "x" * 80, "summary conventions"),
        ("[OP-269] Add Gerrit TDD contract tests", ""),
    ),
)
def test_commit_subject_contract(subject: str, message: str) -> None:
    patchset = _copy_patchset(PATCHSET_A, subject=subject)
    if message:
        with pytest.raises(ValueError, match=message):
            _validate_subject(patchset)
    else:
        _validate_subject(patchset)


@pytest.mark.parametrize(
    ("commit_message", "expected"),
    (
        ("Subject\n\nDepends-On: I" + "1" * 40 + "\n", ["I" + "1" * 40]),
        (
            "Subject\n\nDepends-On: I"
            + "a" * 40
            + "\nDepends-On: I"
            + "b" * 40,
            ["I" + "a" * 40, "I" + "b" * 40],
        ),
        ("Subject\n\nChange-Id: I" + "c" * 40 + "\n", []),
        ("Subject\n\n Depends-On: I" + "d" * 40 + "\n", []),
        ("Subject\n\nDepends-On:\n", [""]),
    ),
)
def test_depends_on_parser(commit_message: str, expected: list[str]) -> None:
    assert _extract_trailers(commit_message, "Depends-On") == expected


@pytest.mark.parametrize(
    ("manifest", "message"),
    (
        (VALID_MANIFEST, ""),
        (_copy_manifest(VALID_MANIFEST, ticket="OP-270"), "ticket drift"),
        (_copy_manifest(VALID_MANIFEST, base_branch="main"), "targets develop"),
        (
            _copy_manifest(VALID_MANIFEST, patchsets=(PATCHSET_B, PATCHSET_A)),
            "A then Patchset B",
        ),
        (
            _copy_manifest(VALID_MANIFEST, patchsets=(PATCHSET_A,)),
            "A then Patchset B",
        ),
        (
            _copy_manifest(
                VALID_MANIFEST,
                patchsets=(
                    PATCHSET_A,
                    _copy_patchset(
                        PATCHSET_B,
                        commit_message=PATCHSET_B.commit_message.replace(
                            "I" + "a" * 40,
                            "I" + "c" * 40,
                        ),
                    ),
                ),
            ),
            "depend on Patchset A",
        ),
    ),
)
def test_manifest_contract(manifest: ManifestSpec, message: str) -> None:
    if message:
        with pytest.raises(ValueError, match=message):
            _validate_manifest(manifest)
    else:
        _validate_manifest(manifest)


@pytest.mark.parametrize(
    ("command", "message"),
    (
        (("backend/.venv/bin/pytest", TEST_PATH), ""),
        (("pytest", TEST_PATH), "project pytest venv"),
        (
            ("backend/.venv/bin/pytest", "backend/tests/test_gerrit.py"),
            "BP.G.4 test file",
        ),
        (("backend/.venv/bin/pytest", TEST_PATH, "-k"), "must not narrow"),
        (("backend/.venv/bin/pytest", TEST_PATH, "--pdb"), "must not narrow"),
    ),
)
def test_ci_command_contract(command: tuple[str, ...], message: str) -> None:
    if message:
        with pytest.raises(ValueError, match=message):
            _validate_ci_command(command)
    else:
        _validate_ci_command(command)
