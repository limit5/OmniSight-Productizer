"""U6-0 T9/T10 G1 operation-identity substrate (dormant, stdlib-only).

This module defines the immutable operation identity and deterministic hashes
used by later prepared-action, challenge, grant, and execution leaves.  It is
additive and dormant: no runtime path constructs these records yet.

The module has no mutable global state and imports nothing from ``backend``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass


IDENTITY_SCHEMA_VERSION = "v1"

_IDENTITY_DOMAIN = b"u6-op-identity-v1\0"
_PREPARED_ACTION_DOMAIN = b"u6-prepared-action-v1\0"


@dataclass(frozen=True)
class ModelProvenanceRef:
    """Reference to the provenance snapshot for a model turn."""

    model_snapshot_id: str


@dataclass(frozen=True)
class NoModelInputRef:
    """Reference to the deterministic issuer for a no-model operation."""

    no_model_input_source: str


ProvenanceRef = ModelProvenanceRef | NoModelInputRef


@dataclass(frozen=True)
class OperationIdentity:
    """Immutable identity shared by every artifact for one operation."""

    tenant_id: str
    principal_type: str
    actor_id: str
    request_id: str
    model_call_id: str
    adapter_namespace: str
    tool_name: str
    schema_version: str
    family: str
    canonical_target: str
    args_hash: str
    provenance: ProvenanceRef


def validate(identity: OperationIdentity) -> None:
    """Validate the provenance discriminant and model-call coupling."""
    if isinstance(identity.provenance, ModelProvenanceRef):
        if identity.model_call_id == "":
            raise ValueError("model provenance requires a model_call_id")
        return
    if isinstance(identity.provenance, NoModelInputRef):
        if identity.model_call_id != "":
            raise ValueError("no-model provenance requires an empty model_call_id")
        return
    raise ValueError("unsupported provenance reference")


def mint_action_instance_id() -> str:
    """Mint the per-tool-call uniqueness spine."""
    return "aiid-" + uuid.uuid4().hex


def _frame(part: str) -> bytes:
    """Encode one UTF-8 string with an unambiguous byte-length prefix."""
    encoded = part.encode("utf-8", "surrogatepass")
    return str(len(encoded)).encode("ascii") + b":" + encoded


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_bytes(identity: OperationIdentity) -> bytes:
    """Serialize every identity field in fixed, domain-separated frames."""
    parts = (
        identity.tenant_id,
        identity.principal_type,
        identity.actor_id,
        identity.request_id,
        identity.model_call_id,
        identity.adapter_namespace,
        identity.tool_name,
        identity.schema_version,
        identity.family,
        identity.canonical_target,
        identity.args_hash,
    )
    serialized = bytearray(_IDENTITY_DOMAIN)
    for part in parts:
        serialized.extend(_frame(part))

    if isinstance(identity.provenance, ModelProvenanceRef):
        serialized.extend(b"m")
        serialized.extend(_frame(identity.provenance.model_snapshot_id))
    elif isinstance(identity.provenance, NoModelInputRef):
        serialized.extend(b"n")
        serialized.extend(_frame(identity.provenance.no_model_input_source))
    else:
        raise ValueError("unsupported provenance reference")
    return bytes(serialized)


def op_hash(identity: OperationIdentity) -> str:
    """Return the diagnostic/index hash for an operation identity."""
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def args_hash(executable_args: dict) -> str:
    """Hash canonical JSON for post-default executable arguments."""
    encoded = _canonical_json(executable_args).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def prepared_action_digest(
    identity: OperationIdentity,
    executable_args: dict,
    human_rendering: dict,
) -> str:
    """Hash stable prepared-action content, excluding volatile metadata."""
    payload = bytearray(_PREPARED_ACTION_DOMAIN)
    payload.extend(_frame(op_hash(identity)))
    payload.extend(_frame(args_hash(executable_args)))
    payload.extend(_frame(_canonical_json(human_rendering)))
    return hashlib.sha256(payload).hexdigest()


def identity_matches(a: OperationIdentity, b: OperationIdentity) -> bool:
    """Match all identity columns; never treat ``op_hash`` as authority."""
    return (
        a.tenant_id == b.tenant_id
        and a.principal_type == b.principal_type
        and a.actor_id == b.actor_id
        and a.request_id == b.request_id
        and a.model_call_id == b.model_call_id
        and a.adapter_namespace == b.adapter_namespace
        and a.tool_name == b.tool_name
        and a.schema_version == b.schema_version
        and a.family == b.family
        and a.canonical_target == b.canonical_target
        and a.args_hash == b.args_hash
        and a.provenance == b.provenance
    )
