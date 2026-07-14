"""OP-2656 neutral shadow-root registry contract tests (offline)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from backend.agents.shadow_root_registry import (
    ShadowRootRegistryError,
    freeze_registry,
    is_frozen,
    register_shadow_root,
    reset_for_tests,
    resolve_shadow_root,
)


_KEY = ("runner_sdk", "Write", "v1")


@pytest.fixture(autouse=True)
def isolate_shadow_root_registry() -> Iterator[None]:
    """Prevent roots, poison, or readiness from leaking between tests."""
    reset_for_tests()
    yield


def test_registered_root_resolves_only_after_freeze() -> None:
    register_shadow_root(*_KEY, workspace_root="/abs/a")

    assert resolve_shadow_root(*_KEY) is None

    freeze_registry()

    assert resolve_shadow_root(*_KEY) == "/abs/a"


def test_frozen_registry_returns_none_for_unregistered_key() -> None:
    freeze_registry()

    assert resolve_shadow_root("runner_sdk", "Edit", "v1") is None


@pytest.mark.parametrize(
    "dispatch_key",
    [
        (None, "Write", "v1"),
        ("runner_sdk", None, "v1"),
        ("runner_sdk", "Write", None),
        (1, "Write", "v1"),
    ],
)
@pytest.mark.parametrize("frozen", [False, True])
def test_malformed_key_resolve_is_raise_free_before_and_after_freeze(
    dispatch_key: tuple[object, object, object],
    frozen: bool,
) -> None:
    if frozen:
        freeze_registry()

    assert resolve_shadow_root(*dispatch_key) is None


@pytest.mark.parametrize(
    "dispatch_key",
    [
        ("", "Write", "v1"),
        ("runner_sdk", "", "v1"),
        ("runner_sdk", "Write", ""),
    ],
)
def test_register_rejects_empty_dispatch_key_component(
    dispatch_key: tuple[str, str, str],
) -> None:
    with pytest.raises(ShadowRootRegistryError, match="^empty_dispatch_key$"):
        register_shadow_root(*dispatch_key, workspace_root="/abs/a")


@pytest.mark.parametrize(
    "dispatch_key",
    [
        (None, "Write", "v1"),
        ("runner_sdk", None, "v1"),
        ("runner_sdk", "Write", None),
    ],
)
def test_register_rejects_non_str_dispatch_key_component(
    dispatch_key: tuple[object, object, object],
) -> None:
    with pytest.raises(
        ShadowRootRegistryError,
        match="^non_str_dispatch_key$",
    ):
        register_shadow_root(  # type: ignore[arg-type]
            *dispatch_key,
            workspace_root="/abs/a",
        )


@pytest.mark.parametrize("workspace_root", ["", None, 1])
def test_register_rejects_empty_or_non_str_root(
    workspace_root: object,
) -> None:
    with pytest.raises(
        ShadowRootRegistryError,
        match="^invalid_workspace_root$",
    ):
        register_shadow_root(  # type: ignore[arg-type]
            *_KEY,
            workspace_root=workspace_root,
        )


@pytest.mark.parametrize("workspace_root", [".", "rel/x", " "])
def test_register_rejects_non_absolute_root(workspace_root: str) -> None:
    with pytest.raises(
        ShadowRootRegistryError,
        match="^workspace_root_not_absolute$",
    ):
        register_shadow_root(*_KEY, workspace_root=workspace_root)


def test_identical_root_registration_before_freeze_is_idempotent() -> None:
    register_shadow_root(*_KEY, workspace_root="/abs/a")

    register_shadow_root(*_KEY, workspace_root="/abs/a")
    freeze_registry()

    assert resolve_shadow_root(*_KEY) == "/abs/a"


def test_identical_root_registration_after_freeze_is_idempotent() -> None:
    register_shadow_root(*_KEY, workspace_root="/abs/a")
    freeze_registry()

    register_shadow_root(*_KEY, workspace_root="/abs/a")

    assert resolve_shadow_root(*_KEY) == "/abs/a"


def test_new_key_registration_after_freeze_raises() -> None:
    freeze_registry()

    with pytest.raises(
        ShadowRootRegistryError,
        match=r"^registry_frozen:runner_sdk/Write@v1$",
    ):
        register_shadow_root(*_KEY, workspace_root="/abs/a")


def test_conflicting_registration_poisons_key() -> None:
    register_shadow_root(*_KEY, workspace_root="/abs/a")

    with pytest.raises(
        ShadowRootRegistryError,
        match=r"^conflicting_shadow_root:runner_sdk/Write@v1$",
    ):
        register_shadow_root(*_KEY, workspace_root="/abs/b")

    freeze_registry()
    assert resolve_shadow_root(*_KEY) is None

    with pytest.raises(
        ShadowRootRegistryError,
        match=r"^poisoned_shadow_root:runner_sdk/Write@v1$",
    ):
        register_shadow_root(*_KEY, workspace_root="/abs/a")


def test_freeze_is_idempotent_and_reflected_by_is_frozen() -> None:
    assert not is_frozen()

    freeze_registry()
    freeze_registry()

    assert is_frozen()


def test_multi_key_lifecycle_keeps_clean_roots_resolvable() -> None:
    first_key = ("runner_sdk", "Write", "v1")
    poisoned_key = ("runner_sdk", "Edit", "v1")
    third_key = ("text_editor", "str_replace", "v1")
    register_shadow_root(*first_key, workspace_root="/abs/a")
    register_shadow_root(*poisoned_key, workspace_root="/abs/b")
    register_shadow_root(*third_key, workspace_root="/abs/c")

    with pytest.raises(ShadowRootRegistryError, match="^conflicting_shadow_root:"):
        register_shadow_root(*poisoned_key, workspace_root="/abs/d")

    freeze_registry()

    assert resolve_shadow_root(*first_key) == "/abs/a"
    assert resolve_shadow_root(*poisoned_key) is None
    assert resolve_shadow_root(*third_key) == "/abs/c"


def test_reset_for_tests_clears_roots_poison_and_unfreezes() -> None:
    other_key = ("runner_sdk", "Edit", "v1")
    register_shadow_root(*_KEY, workspace_root="/abs/a")
    register_shadow_root(*other_key, workspace_root="/abs/b")
    with pytest.raises(ShadowRootRegistryError):
        register_shadow_root(*_KEY, workspace_root="/abs/c")
    freeze_registry()

    reset_for_tests()

    assert not is_frozen()
    freeze_registry()
    assert resolve_shadow_root(*_KEY) is None
    assert resolve_shadow_root(*other_key) is None

    reset_for_tests()
    register_shadow_root(*_KEY, workspace_root="/abs/fresh")
    freeze_registry()
    assert resolve_shadow_root(*_KEY) == "/abs/fresh"
