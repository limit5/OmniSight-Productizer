"""KS.1.1 -- KMS adapter abstraction for envelope-encryption KEKs.

This module is the adapter-only landing for Tier 1 envelope encryption.
It intentionally stops at wrapping / unwrapping raw DEK bytes with a
provider-side master KEK. KS.1.2 owns the per-tenant DEK schema and the
``encrypt(plaintext, tenant_id)`` helper; no caller is wired here yet.

Provider shape mirrors the existing FS adapter packages: a small shared
contract, provider-specific implementations, and lazy SDK imports so
module import has no cloud side effects.

Module-global state audit (per implement_phase_step.md SOP §1)
--------------------------------------------------------------
Only immutable constants and class definitions live at module scope.
Cloud SDK clients are instance attributes derived from constructor
arguments, so uvicorn workers do not share mutable runtime state. The
local fallback delegates key material to :mod:`backend.secret_store`,
whose file-lock guarded key source is shared through disk coordination.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Optional, Protocol, runtime_checkable

from backend import secret_store


class KMSAdapterError(Exception):
    """Base for all KS.1.1 KMS adapter errors."""

    def __init__(self, message: str, provider: str = "", key_id: str = ""):
        super().__init__(message)
        self.provider = provider
        self.key_id = key_id


class KMSConfigurationError(KMSAdapterError, ValueError):
    """Adapter configuration is incomplete or invalid."""


class KMSDependencyError(KMSAdapterError, ImportError):
    """The provider SDK required by this adapter is not installed."""


class KMSOperationError(KMSAdapterError):
    """Provider rejected or failed a wrap / unwrap operation."""


@dataclass(frozen=True)
class WrappedDEK:
    """Provider-neutral wrapped data-encryption-key payload."""

    provider: str
    key_id: str
    ciphertext: bytes
    key_version: Optional[str] = None
    algorithm: str = ""
    encryption_context: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "key_id": self.key_id,
            "ciphertext_b64": base64.b64encode(self.ciphertext).decode("ascii"),
            "key_version": self.key_version,
            "algorithm": self.algorithm,
            "encryption_context": dict(self.encryption_context),
        }


@runtime_checkable
class KMSAdapter(Protocol):
    """Structural contract for provider KEK adapters."""

    provider: str

    def wrap_dek(
        self,
        plaintext_dek: bytes,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> WrappedDEK:
        """Encrypt one raw DEK under the configured master KEK."""

    def unwrap_dek(
        self,
        wrapped_dek: WrappedDEK,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        """Decrypt one wrapped DEK with the configured master KEK."""


def _require_bytes(value: bytes, name: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise KMSConfigurationError(f"{name} must be non-empty bytes")
    return value


def _context_dict(context: Optional[Mapping[str, str]]) -> dict[str, str]:
    if not context:
        return {}
    out: dict[str, str] = {}
    for key, value in context.items():
        if not key or value is None:
            raise KMSConfigurationError("encryption_context keys and values are required")
        out[str(key)] = str(value)
    return out


def _context_aad(context: Optional[Mapping[str, str]]) -> bytes:
    data = _context_dict(context)
    if not data:
        return b""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _provider_mismatch(adapter: "BaseKMSAdapter", wrapped_dek: WrappedDEK) -> None:
    if wrapped_dek.provider != adapter.provider:
        raise KMSOperationError(
            f"wrapped DEK provider {wrapped_dek.provider!r} does not match {adapter.provider!r}",
            provider=adapter.provider,
            key_id=adapter.key_id,
        )


@dataclass
class BaseKMSAdapter:
    """Shared configuration guard for concrete KMS adapters."""

    key_id: str
    provider: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if not self.provider:
            raise KMSConfigurationError(f"{type(self).__name__} must set provider")
        if not self.key_id:
            raise KMSConfigurationError("key_id is required", provider=self.provider)


@dataclass
class LocalFernetKMSAdapter(BaseKMSAdapter):
    """Dev / single-tenant fallback that reuses the existing Fernet key."""

    key_id: str = "local-fernet"
    provider: ClassVar[str] = "local-fernet"

    def wrap_dek(
        self,
        plaintext_dek: bytes,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> WrappedDEK:
        _require_bytes(plaintext_dek, "plaintext_dek")
        context = _context_dict(encryption_context)
        payload = {
            "dek_b64": base64.b64encode(plaintext_dek).decode("ascii"),
            "ctx": context,
        }
        ciphertext = secret_store.encrypt(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        ).encode("ascii")
        return WrappedDEK(
            provider=self.provider,
            key_id=self.key_id,
            ciphertext=ciphertext,
            key_version="1",
            algorithm="fernet",
            encryption_context=context,
        )

    def unwrap_dek(
        self,
        wrapped_dek: WrappedDEK,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        _provider_mismatch(self, wrapped_dek)
        expected_context = _context_dict(encryption_context)
        payload = json.loads(secret_store.decrypt(wrapped_dek.ciphertext.decode("ascii")))
        if payload.get("ctx", {}) != expected_context:
            raise KMSOperationError(
                "encryption_context mismatch",
                provider=self.provider,
                key_id=self.key_id,
            )
        return base64.b64decode(payload["dek_b64"].encode("ascii"))


@dataclass
class AWSKMSAdapter(BaseKMSAdapter):
    """AWS KMS adapter using boto3, with optional IAM assume-role."""

    key_id: str
    region_name: Optional[str] = None
    role_arn: Optional[str] = None
    external_id: Optional[str] = None
    session_name: str = "omnisight-kms-adapter"
    provider: ClassVar[str] = "aws-kms"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._client: Any = None

    @classmethod
    def from_environment(cls, *, prefix: str = "OMNISIGHT_AWS_KMS") -> "AWSKMSAdapter":
        """Build the AWS adapter from CI/prod environment configuration.

        ``OMNISIGHT_AWS_KMS_*`` is the production assume-role prefix.
        Tests pass ``prefix="OMNISIGHT_TEST_AWS_KMS"`` for the CI
        sandbox account without changing the runtime knob names.
        """

        key_id = _env_required(f"{prefix}_KEY_ID", provider=cls.provider)
        return cls(
            key_id=key_id,
            region_name=_env_optional(f"{prefix}_REGION"),
            role_arn=_env_optional(f"{prefix}_ROLE_ARN"),
            external_id=_env_optional(f"{prefix}_EXTERNAL_ID"),
            session_name=_env_optional(f"{prefix}_SESSION_NAME") or cls.session_name,
        )

    def _kms_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise KMSDependencyError(
                "AWSKMSAdapter requires boto3",
                provider=self.provider,
                key_id=self.key_id,
            ) from exc
        if self.role_arn:
            sts = boto3.client("sts", region_name=self.region_name)
            assume_args: dict[str, Any] = {
                "RoleArn": self.role_arn,
                "RoleSessionName": self.session_name,
            }
            if self.external_id:
                assume_args["ExternalId"] = self.external_id
            creds = sts.assume_role(**assume_args)["Credentials"]
            self._client = boto3.client(
                "kms",
                region_name=self.region_name,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
            return self._client
        self._client = boto3.client("kms", region_name=self.region_name)
        return self._client

    def describe_key(self) -> dict[str, Any]:
        """Return AWS KMS key metadata for live connectivity checks."""

        try:
            result = self._kms_client().describe_key(KeyId=self.key_id)
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return dict(result)

    def wrap_dek(
        self,
        plaintext_dek: bytes,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> WrappedDEK:
        _require_bytes(plaintext_dek, "plaintext_dek")
        context = _context_dict(encryption_context)
        try:
            result = self._kms_client().encrypt(
                KeyId=self.key_id,
                Plaintext=plaintext_dek,
                EncryptionContext=context,
            )
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return WrappedDEK(
            provider=self.provider,
            key_id=str(result.get("KeyId") or self.key_id),
            ciphertext=bytes(result["CiphertextBlob"]),
            algorithm=str(result.get("EncryptionAlgorithm") or "SYMMETRIC_DEFAULT"),
            encryption_context=context,
            raw={"ResponseMetadata": result.get("ResponseMetadata", {})},
        )

    def unwrap_dek(
        self,
        wrapped_dek: WrappedDEK,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        _provider_mismatch(self, wrapped_dek)
        context = _context_dict(encryption_context)
        try:
            result = self._kms_client().decrypt(
                CiphertextBlob=wrapped_dek.ciphertext,
                KeyId=self.key_id,
                EncryptionContext=context,
            )
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return bytes(result["Plaintext"])


@dataclass
class GCPKMSAdapter(BaseKMSAdapter):
    """Google Cloud KMS adapter using google-cloud-kms."""

    key_id: str
    provider: ClassVar[str] = "gcp-kms"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._client: Any = None

    @classmethod
    def from_environment(cls, *, prefix: str = "OMNISIGHT_GCP_KMS") -> "GCPKMSAdapter":
        """Build the GCP adapter from ADC-backed environment configuration.

        ``OMNISIGHT_GCP_KMS_*`` is the production prefix. Tests pass
        ``prefix="OMNISIGHT_TEST_GCP_KMS"`` for the CI sandbox key;
        credentials stay in Google ADC / ``GOOGLE_APPLICATION_CREDENTIALS``.
        """

        return cls(key_id=_env_required(f"{prefix}_KEY_ID", provider=cls.provider))

    def _kms_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from google.cloud import kms  # type: ignore[import-not-found]
        except ImportError as exc:
            raise KMSDependencyError(
                "GCPKMSAdapter requires google-cloud-kms",
                provider=self.provider,
                key_id=self.key_id,
            ) from exc
        self._client = kms.KeyManagementServiceClient()
        return self._client

    def describe_key(self) -> Any:
        """Return Google Cloud KMS CryptoKey metadata for live checks."""

        try:
            return self._kms_client().get_crypto_key(request={"name": self.key_id})
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc

    def wrap_dek(
        self,
        plaintext_dek: bytes,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> WrappedDEK:
        _require_bytes(plaintext_dek, "plaintext_dek")
        aad = _context_aad(encryption_context)
        try:
            result = self._kms_client().encrypt(
                request={
                    "name": self.key_id,
                    "plaintext": plaintext_dek,
                    "additional_authenticated_data": aad,
                }
            )
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return WrappedDEK(
            provider=self.provider,
            key_id=self.key_id,
            ciphertext=bytes(result.ciphertext),
            key_version=getattr(result, "name", None),
            algorithm="google-cloud-kms",
            encryption_context=_context_dict(encryption_context),
        )

    def unwrap_dek(
        self,
        wrapped_dek: WrappedDEK,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        _provider_mismatch(self, wrapped_dek)
        aad = _context_aad(encryption_context)
        try:
            result = self._kms_client().decrypt(
                request={
                    "name": self.key_id,
                    "ciphertext": wrapped_dek.ciphertext,
                    "additional_authenticated_data": aad,
                }
            )
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return bytes(result.plaintext)


@dataclass
class VaultTransitKMSAdapter(BaseKMSAdapter):
    """HashiCorp Vault Transit adapter using hvac."""

    key_id: str
    url: str
    token: str
    namespace: Optional[str] = None
    mount_point: str = "transit"
    provider: ClassVar[str] = "vault-transit"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.url:
            raise KMSConfigurationError("url is required", provider=self.provider)
        if not self.token:
            raise KMSConfigurationError("token is required", provider=self.provider)
        self._client: Any = None

    def _vault_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import hvac  # type: ignore[import-not-found]
        except ImportError as exc:
            raise KMSDependencyError(
                "VaultTransitKMSAdapter requires hvac",
                provider=self.provider,
                key_id=self.key_id,
            ) from exc
        self._client = hvac.Client(url=self.url, token=self.token, namespace=self.namespace)
        return self._client

    def wrap_dek(
        self,
        plaintext_dek: bytes,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> WrappedDEK:
        _require_bytes(plaintext_dek, "plaintext_dek")
        context = _context_aad(encryption_context)
        args: dict[str, Any] = {
            "name": self.key_id,
            "plaintext": base64.b64encode(plaintext_dek).decode("ascii"),
            "mount_point": self.mount_point,
        }
        if context:
            args["context"] = base64.b64encode(context).decode("ascii")
        try:
            result = self._vault_client().secrets.transit.encrypt_data(**args)
            ciphertext = result["data"]["ciphertext"]
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return WrappedDEK(
            provider=self.provider,
            key_id=self.key_id,
            ciphertext=ciphertext.encode("ascii"),
            key_version=_vault_key_version(ciphertext),
            algorithm="vault-transit",
            encryption_context=_context_dict(encryption_context),
        )

    def unwrap_dek(
        self,
        wrapped_dek: WrappedDEK,
        *,
        encryption_context: Optional[Mapping[str, str]] = None,
    ) -> bytes:
        _provider_mismatch(self, wrapped_dek)
        context = _context_aad(encryption_context)
        args: dict[str, Any] = {
            "name": self.key_id,
            "ciphertext": wrapped_dek.ciphertext.decode("ascii"),
            "mount_point": self.mount_point,
        }
        if context:
            args["context"] = base64.b64encode(context).decode("ascii")
        try:
            result = self._vault_client().secrets.transit.decrypt_data(**args)
            plaintext_b64 = result["data"]["plaintext"]
        except Exception as exc:  # pragma: no cover - SDK-specific subclasses.
            raise KMSOperationError(str(exc), provider=self.provider, key_id=self.key_id) from exc
        return base64.b64decode(plaintext_b64.encode("ascii"))


def _vault_key_version(ciphertext: str) -> Optional[str]:
    parts = ciphertext.split(":", 3)
    if len(parts) >= 3 and parts[0] == "vault":
        return parts[1]
    return None


def _env_optional(name: str) -> Optional[str]:
    value = (os.environ.get(name) or "").strip()
    return value or None


def _env_required(name: str, *, provider: str) -> str:
    value = _env_optional(name)
    if not value:
        raise KMSConfigurationError(f"{name} is required", provider=provider)
    return value


def list_providers() -> list[str]:
    """Return the canonical id for every shipped KMS adapter."""
    return ["aws-kms", "gcp-kms", "vault-transit", "local-fernet"]


def get_adapter(provider: str) -> type[KMSAdapter]:
    """Look up a KMS adapter class by canonical provider string."""
    key = provider.strip().lower().replace("_", "-")
    if key in ("aws", "aws-kms"):
        return AWSKMSAdapter
    if key in ("gcp", "gcp-kms", "google-kms", "google-cloud-kms"):
        return GCPKMSAdapter
    if key in ("vault", "vault-transit", "hashicorp-vault"):
        return VaultTransitKMSAdapter
    if key in ("local", "local-fernet", "fernet"):
        return LocalFernetKMSAdapter
    raise ValueError(
        f"Unknown KMS provider '{provider}'. Expected one of: {', '.join(list_providers())}"
    )


__all__ = [
    "AWSKMSAdapter",
    "GCPKMSAdapter",
    "KMSAdapter",
    "KMSAdapterError",
    "KMSConfigurationError",
    "KMSDependencyError",
    "KMSOperationError",
    "LocalFernetKMSAdapter",
    "VaultTransitKMSAdapter",
    "WrappedDEK",
    "get_adapter",
    "list_providers",
]
