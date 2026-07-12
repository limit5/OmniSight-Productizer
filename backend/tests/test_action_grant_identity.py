"""OP-2624 operation-identity and hashing substrate tests (offline)."""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from backend.agents import action_grant
from backend.agents.action_grant import (
    IDENTITY_SCHEMA_VERSION,
    ModelProvenanceRef,
    NoModelInputRef,
    OperationIdentity,
    args_hash,
    canonical_bytes,
    identity_matches,
    mint_action_instance_id,
    op_hash,
    prepared_action_digest,
    validate,
)


def _identity(**overrides) -> OperationIdentity:
    values = dict(
        tenant_id="tenant-1",
        principal_type="user",
        actor_id="actor-1",
        request_id="request-1",
        model_call_id="model-call-1",
        adapter_namespace="builtin",
        tool_name="write_file",
        schema_version=IDENTITY_SCHEMA_VERSION,
        family="filesystem",
        canonical_target="/workspace/result.txt",
        args_hash="a" * 64,
        provenance=ModelProvenanceRef("snapshot-1"),
    )
    values.update(overrides)
    return OperationIdentity(**values)


def test_op_hash_and_canonical_bytes_are_deterministic_and_sensitive() -> None:
    identity = _identity()
    assert canonical_bytes(identity) == canonical_bytes(_identity())
    assert op_hash(identity) == op_hash(_identity())

    replacements = {
        "tenant_id": "tenant-2",
        "principal_type": "service",
        "actor_id": "actor-2",
        "request_id": "request-2",
        "model_call_id": "model-call-2",
        "adapter_namespace": "mcp",
        "tool_name": "read_file",
        "schema_version": "v2",
        "family": "network",
        "canonical_target": "/workspace/other.txt",
        "args_hash": "b" * 64,
        "provenance": ModelProvenanceRef("snapshot-2"),
    }
    for field_name, new_value in replacements.items():
        changed = dataclasses.replace(identity, **{field_name: new_value})
        assert canonical_bytes(changed) != canonical_bytes(identity), field_name
        assert op_hash(changed) != op_hash(identity), field_name

    no_model = dataclasses.replace(
        identity,
        model_call_id="",
        provenance=NoModelInputRef("slash-command"),
    )
    assert op_hash(no_model) != op_hash(identity)

    left = _identity(tenant_id="a", principal_type="bc")
    right = _identity(tenant_id="ab", principal_type="c")
    assert left.tenant_id + left.principal_type == right.tenant_id + right.principal_type
    assert canonical_bytes(left) != canonical_bytes(right)
    assert op_hash(left) != op_hash(right)


def test_prepared_action_digest_is_volatile_free_and_content_sensitive() -> None:
    identity = _identity()
    executable_args = {"path": "result.txt", "options": {"mode": "safe"}}

    first = prepared_action_digest(identity, executable_args)
    second = prepared_action_digest(identity, executable_args)
    assert first == second  # prepare time is deliberately not an input
    assert first != prepared_action_digest(
        identity,
        {"path": "other.txt", "options": {"mode": "safe"}},
    )
    assert first != prepared_action_digest(
        _identity(tool_name="read_file"),
        executable_args,
    )


def test_args_hash_is_deterministic_and_key_order_independent() -> None:
    first = {"target": "café", "nested": {"b": 2, "a": 1}}
    reordered = {"nested": {"a": 1, "b": 2}, "target": "café"}
    assert args_hash(first) == args_hash(first)
    assert args_hash(first) == args_hash(reordered)
    assert args_hash(first) != args_hash({"target": "cafe", "nested": {"a": 1, "b": 2}})


def test_validate_enforces_provenance_model_call_xor() -> None:
    validate(_identity())
    validate(_identity(model_call_id="", provenance=NoModelInputRef("slash-command")))

    with pytest.raises(ValueError, match="requires a model_call_id"):
        validate(_identity(model_call_id=""))
    with pytest.raises(ValueError, match="requires an empty model_call_id"):
        validate(_identity(provenance=NoModelInputRef("slash-command")))


def test_identity_matches_all_columns_and_provenance_discriminant() -> None:
    identity = _identity()
    assert identity_matches(identity, _identity())

    replacements = {
        "tenant_id": "tenant-2",
        "principal_type": "service",
        "actor_id": "actor-2",
        "request_id": "request-2",
        "model_call_id": "model-call-2",
        "adapter_namespace": "mcp",
        "tool_name": "read_file",
        "schema_version": "v2",
        "family": "network",
        "canonical_target": "/workspace/other.txt",
        "args_hash": "b" * 64,
        "provenance": ModelProvenanceRef("snapshot-2"),
    }
    for field_name, new_value in replacements.items():
        changed = dataclasses.replace(identity, **{field_name: new_value})
        assert not identity_matches(identity, changed), field_name

    no_model = dataclasses.replace(
        identity,
        model_call_id="",
        provenance=NoModelInputRef("snapshot-1"),
    )
    assert not identity_matches(identity, no_model)


def test_mint_action_instance_id_returns_distinct_prefixed_ids() -> None:
    first = mint_action_instance_id()
    second = mint_action_instance_id()
    assert first.startswith("aiid-")
    assert second.startswith("aiid-")
    assert first != second
    assert len(first) == len("aiid-") + 32


def test_action_grant_module_is_stdlib_only_leaf() -> None:
    source = pathlib.Path(action_grant.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    for module in imported:
        assert not module.startswith("backend"), f"backend import in leaf: {module}"
